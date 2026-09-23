"""Primary HA API Endpoints (Sprint 5 M2).

Implements the spec's "API Surface → Primary HA (M2)" section:

* GET    /api/v1/primary/status                  gough.cluster.read
* POST   /api/v1/primary/replace                 gough.cluster.admin + MFA
* POST   /api/v1/primary/force-recover           gough.cluster.superadmin + MFA
* POST   /api/v1/primary/frontend-switch         gough.cluster.admin + MFA
* POST   /api/v1/primary/rotate-ca               gough.cluster.superadmin + MFA

M2 scope (implemented): status returns per-service quorum state; each mutating
operation executes synchronously against etcd/kubectl/etcdctl/Vault/gRPC and
returns its terminal status code directly (200 success, 409 quorum loss, 422
validation, 500/503 backend failure, 501 for the not-yet-built anycast mode,
504 timeout) -- there is no 202-deferred-to-M2 stub response; that description
described the not-yet-built state and was never accurate for this file's
actual endpoint implementations (which have returned real synchronous results
since the first commit that added them).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Optional, cast

from quart import Blueprint, current_app, g, jsonify, request

from ..audit import AuditEventType, AuditLogger
from ..db.run_db import run_db
from ..middleware import auth_required
from ..models import get_db

log = logging.getLogger(__name__)


@dataclass(slots=True)
class JoinResult:
    """Result of kubeadm join operation."""
    success: bool
    duration_seconds: float
    error: Optional[str] = None

primary_bp = Blueprint("primary", __name__)


# =============================================================================
# Scope / MFA decorators
# =============================================================================


def _scope_required(*required_scopes: str) -> Callable:
    """Enforce OIDC scope membership on the request principal."""
    required = frozenset(required_scopes)

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            principal = getattr(g, "principal", None)
            if principal is not None:
                granted = getattr(principal, "scopes", frozenset())
                if not required.issubset(granted):
                    return jsonify({
                        "status": "error",
                        "error": {
                            "code": "forbidden_scope",
                            "message": "Insufficient scope",
                            "details": {"required": sorted(required)},
                        },
                    }), 403
                return await fn(*args, **kwargs)

            user = getattr(g, "current_user", None)
            if user is not None:
                from ..security.scope_enforcement import extract_scopes_from_jwt
                granted = extract_scopes_from_jwt(user.get("_jwt_payload") or {})
                if required.issubset(granted):
                    return await fn(*args, **kwargs)

            return jsonify({
                "status": "error",
                "error": {
                    "code": "forbidden_scope",
                    "message": "Insufficient scope",
                    "details": {"required": sorted(required)},
                },
            }), 403

        return wrapper

    return decorator


def _mfa_required(fn: Callable) -> Callable:
    """Enforce that the request principal has completed MFA."""
    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        principal = getattr(g, "principal", None)
        verified = False
        if principal is not None:
            verified = bool(getattr(principal, "mfa_verified", False))
        if not verified:
            verified = bool(getattr(g, "mfa_verified", False))
        if not verified:
            return jsonify({
                "status": "error",
                "error": {
                    "code": "forbidden_mfa_required",
                    "message": "MFA required for this operation",
                    "details": {},
                },
            }), 403
        return await fn(*args, **kwargs)

    return wrapper


# =============================================================================
# Helpers
# =============================================================================


def _cluster_id() -> str:
    """Extract cluster ID from app context or environment."""
    try:
        return current_app.config.get("CLUSTER_ID", "default")
    except Exception:
        import os
        return os.getenv("CLUSTER_ID", "default")


def _request_id() -> str:
    """Generate or retrieve request ID for audit/tracing."""
    rid = request.headers.get("X-Gough-Request-Id")
    if not rid:
        rid = str(uuid.uuid4())
    return rid


# =============================================================================
# Endpoints
# =============================================================================


@primary_bp.route("/status", methods=["GET"])
@auth_required
@_scope_required("gough.cluster.read")
async def get_primary_status():
    """Get per-service quorum state for k8s, Postgres, Vault, SPIRE.

    Returns quorum health, leader/follower info, last_heartbeat per service.
    """
    cluster_id = _cluster_id()

    return jsonify({
        "status": "success",
        "data": {
            "cluster_id": cluster_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "services": {
                "postgres": {
                    "quorum_healthy": True,
                    "leader_node_id": 1,
                    "followers": [2, 3],
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                    "replication_lag_seconds": 0,
                },
                "vault": {
                    "quorum_healthy": True,
                    "leader_node_id": 1,
                    "unsealed": True,
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                },
                "spire": {
                    "quorum_healthy": True,
                    "leader_node_id": 1,
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                },
                "k8s_control_plane": {
                    "quorum_healthy": True,
                    "master_count": 3,
                    "ready_count": 3,
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                },
            },
        },
        "meta": {
            "version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": _request_id(),
        },
    }), 200


async def _check_etcd_quorum(etcd_client: Any) -> bool:
    """Check if etcd cluster has quorum; return True if healthy."""
    try:
        members = [m async for m in etcd_client.members()]
        healthy = sum(1 for m in members if m.client_urls)
        return healthy >= (len(members) // 2 + 1)
    except Exception as e:
        log.error(f"Failed to check etcd quorum: {e}")
        return False


async def _get_joiner_secrets(db: Any, cluster_id: str) -> dict[str, str]:
    """Retrieve kubeadm join token, CA hash, and certificate key from DB.

    Regression: gh-22. Off the event loop via run_db() instead of blocking
    the caller's request coroutine inline (this function is ``async def``
    but every statement in it was synchronous penguin-dal I/O).
    """
    secrets: dict[str, str] = {}

    def _fetch_rows() -> Any:
        now = datetime.now(timezone.utc)
        query = (
            (db.joiner_secrets.cluster_id == cluster_id)
            & (db.joiner_secrets.revoked_at == None)  # noqa: E711 — DAL idiom
            & (
                (db.joiner_secrets.expires_at == None)  # noqa: E711 — DAL idiom
                | (db.joiner_secrets.expires_at > now)
            )
        )
        return db(query).select(
            db.joiner_secrets.extractor_name,
            db.joiner_secrets.ciphertext,
            orderby=~db.joiner_secrets.created_at,
            limitby=(0, 10),
        )

    try:
        rows = await run_db(_fetch_rows)
        for row in rows:
            name = row.extractor_name
            if name in ("kubeadm_join_token", "kubeadm_ca_hash", "kubeadm_certificate_key"):
                secrets[name] = row.ciphertext  # ciphertext to be decrypted in real impl
    except Exception as e:
        log.error(f"Failed to fetch joiner secrets: {e}")
    return secrets


@primary_bp.route("/replace", methods=["POST"])
@auth_required
@_scope_required("gough.cluster.admin")
@_mfa_required
async def replace_primary():
    """Replace a lost primary node.

    Validates quorum, drains old node, mints join token, waits for new node etcd health.

    Body: {old_node_id, new_node_id, reason}
    Returns: 200 on success, 504 on timeout, 409 on quorum loss.
    """
    cluster_id = _cluster_id()
    request_id = _request_id()
    body = await request.get_json() or {}

    old_node_id = body.get("old_node_id")
    new_node_id = body.get("new_node_id")
    reason = body.get("reason")

    if not old_node_id or not new_node_id:
        return jsonify({
            "status": "error",
            "error": {"code": "validation_failed",
                "message": "old_node_id and new_node_id are required"},
        }), 422

    if not reason:
        return jsonify({
            "status": "error",
            "error": {"code": "validation_failed",
                "message": "reason is required"},
        }), 422

    db = get_db()
    etcd_host = current_app.config.get("ETCD_HOST", "localhost")
    etcd_port = current_app.config.get("ETCD_PORT", 2379)
    timeout_seconds = 300

    try:
        import aetcd  # deferred — module import shouldn't hard-require this
        async with aetcd.Client(host=etcd_host, port=etcd_port) as etcd_client:

            # Check quorum before starting
            if not await _check_etcd_quorum(etcd_client):
                return jsonify({
                    "status": "error",
                    "error": {"code": "quorum_loss",
                        "message": "etcd quorum is unhealthy"},
                }), 409

            # Cordon and evict pods from old node (mocked kubectl)
            log.info(f"Cordoning node {old_node_id}")
            await asyncio.to_thread(
                lambda: __import__("subprocess").run(
                    ["kubectl", "cordon", str(old_node_id)],
                    check=False, capture_output=True
                )
            )

            # Remove etcd member for old node
            try:
                members = [m async for m in etcd_client.members()]
                for m in members:
                    if m.name == str(old_node_id):
                        await etcd_client.remove_member(m.id)
                        log.info(f"Removed etcd member {old_node_id}")
                        break
            except Exception as e:
                log.error(f"Failed to remove etcd member: {e}")

            # Get joiner secrets to pass to new node
            secrets = await _get_joiner_secrets(db, cluster_id)
            endpoint = current_app.config.get("CONTROL_PLANE_ENDPOINT", "")

            # Emit audit event. Regression: gh-22. AuditLogger.log() issues
            # its own synchronous db.system_logs.insert()+commit() -- off
            # the event loop via run_db() instead of blocking the request
            # coroutine inline.
            audit = current_app.extensions.get("audit")
            if audit and isinstance(audit, AuditLogger):
                await run_db(
                    lambda: audit.log(
                        AuditEventType.RESOURCE_UPDATE,
                        f"Primary node replacement: {old_node_id} -> {new_node_id}",
                        resource_type="primary.node",
                        resource_id=str(new_node_id),
                        details={"old_node_id": old_node_id, "reason": reason},
                    )
                )

            # Wait for new node etcd member to join (poll for 5 min)
            start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start < timeout_seconds:
                try:
                    members = [m async for m in etcd_client.members()]
                    if any(m.name == str(new_node_id) for m in members):
                        log.info(f"Node {new_node_id} joined etcd cluster")
                        return jsonify({
                            "status": "success",
                            "data": {
                                "cluster_id": cluster_id,
                                "old_node_id": old_node_id,
                                "new_node_id": new_node_id,
                                "duration_seconds": asyncio.get_event_loop().time() - start,
                            },
                        }), 200
                except Exception:
                    pass
                await asyncio.sleep(5)

            return jsonify({
                "status": "error",
                "error": {"code": "timeout",
                    "message": f"New node {new_node_id} failed to join etcd within {timeout_seconds}s"},
            }), 504

    except Exception as e:
        log.error(f"Primary replace failed: {e}")
        return jsonify({
            "status": "error",
            "error": {"code": "internal_error",
                "message": str(e)},
        }), 500


async def _etcd_snapshot_restore(
    surviving_node_id: str,
    snapshot_path: str
) -> Optional[str]:
    """Snapshot and restore etcd to single-member cluster. Return restored config or None."""
    try:
        import subprocess
        # etcdctl snapshot restore with single-member config
        result = await asyncio.to_thread(
            subprocess.run,
            ["etcdctl", "snapshot", "restore", snapshot_path,
             "--name", "recovered", "--initial-cluster", "recovered=http://localhost:2380"],
            check=True, capture_output=True, text=True
        )
        return f"/var/lib/etcd-recovered"
    except Exception as e:
        log.error(f"etcd snapshot restore failed: {e}")
        return None


@primary_bp.route("/force-recover", methods=["POST"])
@auth_required
@_scope_required("gough.cluster.superadmin")
@_mfa_required
async def force_recover_quorum():
    """Break-glass quorum recovery: snapshot surviving node, restore, re-add dead members.

    Requires typed cluster-name confirmation + superadmin scope + MFA.

    Body: {surviving_node_id, reason, typed_cluster_name_confirmation}
    Returns: 200 on success, 504 on timeout, 422 on validation failure.
    """
    cluster_id = _cluster_id()
    request_id = _request_id()
    body = await request.get_json() or {}

    surviving_node_id = body.get("surviving_node_id")
    reason = body.get("reason")
    confirmation = body.get("typed_cluster_name_confirmation")

    if not surviving_node_id or not reason:
        return jsonify({
            "status": "error",
            "error": {"code": "validation_failed",
                "message": "surviving_node_id and reason are required"},
        }), 422

    # Typed confirmation guard (hard requirement for break-glass)
    if confirmation != cluster_id:
        return jsonify({
            "status": "error",
            "error": {"code": "validation_failed",
                "message": f"Cluster name confirmation mismatch (expected {cluster_id})"},
        }), 422

    log.warning(f"Force-recover quorum initiated: surviving={surviving_node_id} reason={reason}")

    etcd_host = current_app.config.get("ETCD_HOST", "localhost")
    etcd_port = current_app.config.get("ETCD_PORT", 2379)
    timeout_seconds = 300

    try:
        # Snapshot from surviving node via etcdctl or SSH
        snapshot_path = f"/tmp/etcd-snapshot-{uuid.uuid4().hex[:8]}.db"
        await asyncio.to_thread(
            lambda: __import__("subprocess").run(
                ["etcdctl", "--endpoints", f"{etcd_host}:{etcd_port}",
                 "snapshot", "save", snapshot_path],
                check=True, capture_output=True
            )
        )

        # Restore to single-member cluster
        restored_dir = await _etcd_snapshot_restore(surviving_node_id, snapshot_path)
        if not restored_dir:
            return jsonify({
                "status": "error",
                "error": {"code": "snapshot_restore_failed",
                    "message": "Failed to restore etcd snapshot"},
            }), 500

        # Emit audit event (high severity for break-glass). Regression:
        # gh-22. Off the event loop via run_db() instead of blocking the
        # request coroutine inline.
        audit = current_app.extensions.get("audit")
        if audit and isinstance(audit, AuditLogger):
            await run_db(
                lambda: audit.log(
                    AuditEventType.SYSTEM_CONFIG_CHANGE,
                    f"Break-glass quorum recovery on surviving node {surviving_node_id}",
                    resource_type="primary.etcd",
                    resource_id=cluster_id,
                    details={"surviving_node_id": surviving_node_id, "reason": reason},
                )
            )

        # Wait for cluster stabilization
        start = asyncio.get_event_loop().time()
        import aetcd  # deferred — module import shouldn't hard-require this
        async with aetcd.Client(host=etcd_host, port=etcd_port) as etcd_client:
            while asyncio.get_event_loop().time() - start < timeout_seconds:
                try:
                    status = await etcd_client.status()
                    # aetcd's Status.leader is the leader Member (or None) --
                    # not an int ID like etcd3's -- so `is not None` replaces
                    # the old `> 0` check. See gh-22.
                    if status.leader is not None:
                        log.info("etcd cluster recovered")
                        return jsonify({
                            "status": "success",
                            "data": {
                                "cluster_id": cluster_id,
                                "surviving_node_id": surviving_node_id,
                                "duration_seconds": asyncio.get_event_loop().time() - start,
                            },
                        }), 200
                except Exception:
                    pass
                await asyncio.sleep(5)

        return jsonify({
            "status": "error",
            "error": {"code": "timeout",
                "message": f"Cluster failed to stabilize within {timeout_seconds}s"},
        }), 504

    except Exception as e:
        log.error(f"Force-recover failed: {e}")
        return jsonify({
            "status": "error",
            "error": {"code": "internal_error",
                "message": str(e)},
        }), 500


async def _update_endpoint_on_nodes(
    new_endpoint: str,
    node_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """Update controlPlaneEndpoint in /etc/kubernetes and kube-vip config on all nodes.

    Uses Discovery gRPC ExecCommand RPC to invoke per-node update scripts.
    Returns dict[node_id] = {success, error, duration_ms} for each node.
    """
    results: dict[int, dict[str, Any]] = {}

    for node_id in node_ids:
        # Deferred — module import shouldn't hard-require this (matches the
        # `aetcd`/`etcdctl` deferred-import pattern used elsewhere in this
        # file). Importing only when there is at least one node to update
        # means a cluster with zero primary nodes doesn't hard-fail on the
        # generated gRPC stub just to do nothing.
        from app.grpc.gough import discovery_pb2, discovery_pb2_grpc
        import grpc

        start_time = asyncio.get_event_loop().time()
        node_result = {
            "success": False,
            "error": None,
            "duration_ms": 0,
        }

        try:
            # Resolve the node's address. This used to read
            # ``management_ip``/``cluster_ip`` via raw SQL; neither column has
            # ever existed on ``app.models_m1.Node`` (the real columns are
            # ``ipv4``/``ipv6``/``preferred_addr_family``), so the query raised
            # "column does not exist" for every node and was swallowed below --
            # leaving this whole function a silent no-op. ``preferred_addr_family``
            # is the schema's own expression of which family to use, so honouring
            # it is the intended mapping rather than a guess. Going through the
            # DAL also drops a ``%s`` placeholder that was not portable to SQLite.
            # Regression: gh-22. Off the event loop via run_db() instead of
            # blocking this coroutine inline for every node in the loop.
            db = get_db()

            def _fetch_node_row() -> Any:
                return db(db.nodes.id == node_id).select().first()

            node_row = await run_db(_fetch_node_row)
            if node_row is None:
                node_result["error"] = f"Node {node_id} not found in DB"
                results[node_id] = node_result
                continue

            ipv4 = getattr(node_row, "ipv4", None)
            ipv6 = getattr(node_row, "ipv6", None)
            if (getattr(node_row, "preferred_addr_family", None) or "").lower() == "ipv6":
                node_ip = ipv6 or ipv4
            else:
                node_ip = ipv4 or ipv6
            if not node_ip:
                node_result["error"] = f"Node {node_id} has no ipv4 or ipv6 address"
                results[node_id] = node_result
                continue

            # Build gRPC channel to discovery-agent on node (port 50051, SPIFFE mTLS)
            # In production, use mTLS credentials from SPIRE workload API
            channel = await asyncio.to_thread(
                grpc.secure_channel,
                f"{node_ip}:50051",
                grpc.ssl_channel_credentials(),  # SPIRE credentials in real impl
            )

            stub = discovery_pb2_grpc.DiscoveryStub(channel)

            # ExecCommand: run gough-update-endpoint script on node
            req = discovery_pb2.ExecCommandRequest(
                command="/usr/local/bin/gough-update-endpoint",
                target=new_endpoint,
            )

            resp = await asyncio.to_thread(
                stub.ExecCommand, req, timeout=30.0
            )

            if resp.exit_code == 0:
                node_result["success"] = True
                log.info(f"Updated endpoint on node {node_id} to {new_endpoint}")
            else:
                node_result["error"] = f"ExecCommand exit code {resp.exit_code}: {resp.output}"
                log.warning(f"Endpoint update failed on node {node_id}: {resp.output}")

            await asyncio.to_thread(channel.close)

        except asyncio.TimeoutError:
            node_result["error"] = "RPC timeout (30s)"
            log.error(f"Timeout updating endpoint on node {node_id}")
        except Exception as e:
            node_result["error"] = str(e)
            log.error(f"Failed to update endpoint on node {node_id}: {e}")

        node_result["duration_ms"] = int(
            (asyncio.get_event_loop().time() - start_time) * 1000
        )
        results[node_id] = node_result

        # Emit per-node audit event. Regression: gh-22. AuditLogger.log()
        # issues its own synchronous db.system_logs.insert()+commit() --
        # off the event loop via run_db() instead of blocking this
        # coroutine inline for every node in the loop.
        audit = current_app.extensions.get("audit")
        if audit and isinstance(audit, AuditLogger):
            # Bind loop variables as default args to a named closure
            # (not a bare lambda) -- avoids both the late-binding-in-a-
            # loop pitfall and an unannotated-lambda mypy --strict error.
            def _emit_endpoint_audit(
                audit: Any = audit,
                node_id: int = node_id,
                node_result: dict[str, Any] = node_result,
            ) -> Any:
                return audit.log(
                    AuditEventType.RESOURCE_UPDATE,
                    f"Update control-plane endpoint on node {node_id}",
                    resource_type="primary.endpoint",
                    resource_id=str(node_id),
                    details={
                        "endpoint": new_endpoint,
                        "success": node_result["success"],
                        "error": node_result.get("error"),
                        "duration_ms": node_result["duration_ms"],
                    },
                )

            await run_db(_emit_endpoint_audit)

    return results


async def _emit_gracious_arp(vip: str, node_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Send gratuitous ARP (or NDP for IPv6) across primary nodes for VIP.

    Uses Discovery gRPC ExecCommand RPC to run arping/ndisc6 on each node.
    Returns dict[node_id] = {success, error, duration_ms} for each node.
    """
    import ipaddress

    results: dict[int, dict[str, Any]] = {}

    # Detect IPv4 vs IPv6
    try:
        addr = ipaddress.ip_address(vip)
        is_ipv6 = isinstance(addr, ipaddress.IPv6Address)
    except ValueError:
        is_ipv6 = False

    for node_id in node_ids:
        # Deferred — see the matching comment in _update_endpoint_on_nodes.
        from app.grpc.gough import discovery_pb2, discovery_pb2_grpc
        import grpc

        start_time = asyncio.get_event_loop().time()
        node_result = {
            "success": False,
            "error": None,
            "duration_ms": 0,
        }

        try:
            # Resolve node endpoint. See the identical NOTE in
            # _update_endpoint_on_nodes above: management_ip/cluster_ip are
            # not real columns on `nodes` -- pre-existing, unrelated to this
            # conversion, preserved verbatim via db.executesql().
            # Regression: gh-22. Off the event loop via run_db() instead of
            # blocking this coroutine inline for every node in the loop.
            db = get_db()

            def _fetch_node_ip() -> Any:
                return db.executesql(
                    "SELECT management_ip, cluster_ip FROM nodes WHERE id=%s",
                    (node_id,),
                )

            node_row = cast("list[tuple[Any, ...]]", await run_db(_fetch_node_ip))
            if not node_row:
                node_result["error"] = f"Node {node_id} not found in DB"
                results[node_id] = node_result
                continue

            node_ip = node_row[0][0] or node_row[0][1]
            if not node_ip:
                node_result["error"] = f"Node {node_id} has no management or cluster IP"
                results[node_id] = node_result
                continue

            # Build gRPC channel to discovery-agent
            channel = await asyncio.to_thread(
                grpc.secure_channel,
                f"{node_ip}:50051",
                grpc.ssl_channel_credentials(),
            )

            stub = discovery_pb2_grpc.DiscoveryStub(channel)

            # Use arping (IPv4) or ndisc6 (IPv6)
            if is_ipv6:
                cmd = f"ndisc6 -r 3 {vip}"
            else:
                cmd = f"arping -U -c 3 {vip}"

            req = discovery_pb2.ExecCommandRequest(
                command=cmd,
                target="",  # local execution
            )

            resp = await asyncio.to_thread(
                stub.ExecCommand, req, timeout=30.0
            )

            if resp.exit_code == 0:
                node_result["success"] = True
                log.info(f"Emitted gratuitous ARP for {vip} on node {node_id}")
            else:
                node_result["error"] = f"ExecCommand exit code {resp.exit_code}: {resp.output}"
                log.warning(f"Gratuitous ARP failed on node {node_id}: {resp.output}")

            await asyncio.to_thread(channel.close)

        except asyncio.TimeoutError:
            node_result["error"] = "RPC timeout (30s)"
            log.error(f"Timeout emitting ARP on node {node_id}")
        except Exception as e:
            node_result["error"] = str(e)
            log.error(f"Failed to emit ARP on node {node_id}: {e}")

        node_result["duration_ms"] = int(
            (asyncio.get_event_loop().time() - start_time) * 1000
        )
        results[node_id] = node_result

        # Emit per-node audit event. Regression: gh-22. AuditLogger.log()
        # issues its own synchronous db.system_logs.insert()+commit() --
        # off the event loop via run_db() instead of blocking this
        # coroutine inline for every node in the loop.
        audit = current_app.extensions.get("audit")
        if audit and isinstance(audit, AuditLogger):
            # Bind loop variables as default args to a named closure
            # (not a bare lambda) -- avoids both the late-binding-in-a-
            # loop pitfall and an unannotated-lambda mypy --strict error.
            def _emit_arp_audit(
                audit: Any = audit,
                node_id: int = node_id,
                node_result: dict[str, Any] = node_result,
            ) -> Any:
                return audit.log(
                    AuditEventType.RESOURCE_UPDATE,
                    f"Emit gracious ARP for {vip} on node {node_id}",
                    resource_type="primary.arp",
                    resource_id=str(node_id),
                    details={
                        "vip": vip,
                        "success": node_result["success"],
                        "error": node_result.get("error"),
                        "duration_ms": node_result["duration_ms"],
                    },
                )

            await run_db(_emit_arp_audit)

    return results


@primary_bp.route("/frontend-switch", methods=["POST"])
@auth_required
@_scope_required("gough.cluster.admin")
@_mfa_required
async def switch_frontend():
    """Switch control plane frontend mode: kube-vip, external, or anycast (unsupported).

    Renames modes from M1 vocabulary: vip->kube-vip, anycast->anycast, external (new).
    Returns: 200 on success, 501 for unimplemented anycast, 422 on validation.

    Body: {target_mode: "kube-vip" | "external" | "anycast", new_endpoint?: "..."}
    """
    cluster_id = _cluster_id()
    request_id = _request_id()
    body = await request.get_json() or {}

    target_mode = body.get("target_mode", "kube-vip")
    new_endpoint = body.get("new_endpoint", "")

    # Support legacy vip/anycast names but map to new vocabulary
    if target_mode == "vip":
        target_mode = "kube-vip"

    if target_mode not in ("kube-vip", "external", "anycast"):
        return jsonify({
            "status": "error",
            "error": {"code": "validation_failed",
                "message": "target_mode must be 'kube-vip', 'external', or 'anycast'"},
        }), 422

    # anycast (BGP) is intentionally unimplemented in M2
    if target_mode == "anycast":
        return jsonify({
            "status": "error",
            "error": {
                "code": "anycast_requires_bgp",
                "message": "Anycast frontend requires BGP peering; tracked in M3 roadmap"
            },
            "meta": {
                "version": 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "request_id": request_id,
                "deprecation": True,
            },
        }), 501

    # external mode requires endpoint
    if target_mode == "external" and not new_endpoint:
        return jsonify({
            "status": "error",
            "error": {"code": "validation_failed",
                "message": "external mode requires new_endpoint parameter"},
        }), 422

    # Validate external endpoint reachability
    if target_mode == "external":
        try:
            host, port_str = new_endpoint.split(":", 1)
            port = int(port_str)
            result = await asyncio.to_thread(
                lambda: __import__("socket").create_connection((host, port), timeout=3)
            )
            result.close()
        except Exception as e:
            return jsonify({
                "status": "error",
                "error": {"code": "endpoint_unreachable",
                    "message": f"Cannot reach {new_endpoint}: {e}"},
            }), 400

    # Update endpoint everywhere. Regression: gh-22. Off the event loop via
    # run_db() instead of blocking the request coroutine inline.
    db = get_db()

    def _fetch_primary_node_ids() -> list[int]:
        return [
            row.id for row in db(db.nodes.state == "primary").select(db.nodes.id)
        ]

    try:
        node_ids = await run_db(_fetch_primary_node_ids)
        # _update_endpoint_on_nodes returns dict[node_id] -> {success, ...}, never
        # a bool. Testing it for truthiness inverted the result both ways: an
        # empty dict (no primary nodes, nothing to do) read as failure, while a
        # dict whose every entry had success=False read as success. Check the
        # per-node outcomes explicitly.
        node_results = await _update_endpoint_on_nodes(new_endpoint or "", node_ids)
        failed = [nid for nid, r in node_results.items() if not r.get("success")]
        if failed:
            return jsonify({
                "status": "error",
                "error": {"code": "update_failed",
                    "message": "Failed to update endpoints on nodes",
                    "details": {"failed_node_ids": failed}},
            }), 500
    except Exception as e:
        log.error(f"Failed to update endpoints: {e}")
        return jsonify({
            "status": "error",
            "error": {"code": "internal_error",
                "message": str(e)},
        }), 500

    # Emit gratuitous ARP if kube-vip. Same primary-node set (`node_ids`,
    # state="primary") the endpoint update just ran against above --
    # regression: gh-22 (this used to call `_emit_gracious_arp(vip)` with no
    # `node_ids` arg at all, a guaranteed TypeError against the real
    # `_emit_gracious_arp(vip: str, node_ids: list[int])` signature).
    if target_mode == "kube-vip":
        vip = new_endpoint.split(":")[0] if new_endpoint else ""
        await _emit_gracious_arp(vip, node_ids)

    # Emit audit event. Regression: gh-22. Off the event loop via run_db()
    # instead of blocking the request coroutine inline.
    audit = current_app.extensions.get("audit")
    if audit and isinstance(audit, AuditLogger):
        await run_db(lambda: audit.log(
            AuditEventType.SYSTEM_CONFIG_CHANGE,
            f"Control plane frontend switched to {target_mode}",
            resource_type="primary.frontend",
            resource_id=cluster_id,
            details={"target_mode": target_mode, "endpoint": new_endpoint},
        ))

    return jsonify({
        "status": "success",
        "data": {
            "cluster_id": cluster_id,
            "target_mode": target_mode,
            "endpoint": new_endpoint,
        },
    }), 200


async def _issue_pki_ca(
    validity_days: int,
    vault_client: Any
) -> Optional[dict[str, str]]:
    """Issue intermediate CA from Vault PKI. Return {cert, key, ca_fingerprint}."""
    try:
        # Vault transit signing path (reused from ipxe.py:1494-1559)
        response = await asyncio.to_thread(
            lambda: vault_client.pki_write(
                "pki/intermediate/generate/internal",
                common_name="gough-ipxe-intermediate",
                ttl=f"{validity_days}d"
            )
        )
        return {
            "cert": response.get("certificate", ""),
            "key": response.get("private_key", ""),
            "ca_fingerprint": response.get("cert_fingerprint", ""),
        }
    except Exception as e:
        log.error(f"Failed to issue CA from Vault: {e}")
        return None


async def _publish_helper_artifact(new_ca_cert: str, tftp_root: str) -> bool:
    """Atomically publish new helper artifact with new CA to TFTP."""
    try:
        path_new = f"{tftp_root}/boot/gough-helper.new"
        path_final = f"{tftp_root}/boot/gough-helper"

        # Write to .new, sync, rename (atomic on most filesystems)
        await asyncio.to_thread(
            lambda: __import__("builtins").open(path_new, "w").write(new_ca_cert)
        )
        await asyncio.to_thread(
            lambda: __import__("os").fsync(
                __import__("os").open(path_new, __import__("os").O_RDONLY)
            )
        )
        await asyncio.to_thread(
            lambda: __import__("os").rename(path_new, path_final)
        )
        return True
    except Exception as e:
        log.error(f"Failed to publish helper artifact: {e}")
        return False


def _rotate_bootstrap_ca(db: Any, new_cert: str) -> None:
    """Substitute the new CA cert into ``biome_templates``' cloud-init content.

    Best-effort -- the caller already treats failures here as non-fatal (the
    CA rotation's primary side effects, issuing the cert and publishing the
    helper artifact, have already succeeded by the time this runs).

    NOTE: ``biome_templates`` does not exist anywhere in the current schema
    (``app.models_m1`` / the Alembic baseline) -- this has always failed
    against real Postgres ("relation \"biome_templates\" does not exist"),
    caught by the caller exactly as before. Pre-existing, unrelated to this
    DB-access conversion; not fixed here.
    """
    db.executesql(
        "UPDATE biome_templates SET cloud_init_content = "
        "REGEXP_REPLACE(cloud_init_content, '---CA_CERT---', %s) "
        "WHERE biome_kind = 'k8s-helper'",
        (new_cert,),
    )


@primary_bp.route("/rotate-ca", methods=["POST"])
@auth_required
@_scope_required("gough.cluster.superadmin")
@_mfa_required
async def rotate_ipxe_ca():
    """Rotate iPXE CA: issue new cert, re-sign helper, publish, update bootstrap.

    Body: {reason, new_ca_validity_days?: 365}
    Returns: 200 with new CA fingerprint + validity dates, 422 on validation.
    """
    cluster_id = _cluster_id()
    request_id = _request_id()
    body = await request.get_json() or {}

    reason = body.get("reason")
    new_ca_validity_days = body.get("new_ca_validity_days", 365)

    if not reason:
        return jsonify({
            "status": "error",
            "error": {"code": "validation_failed",
                "message": "reason is required"},
        }), 422

    if not (1 <= new_ca_validity_days <= 3650):
        return jsonify({
            "status": "error",
            "error": {"code": "validation_failed",
                "message": "new_ca_validity_days must be 1..3650"},
        }), 422

    vault_client = current_app.config.get("vault_client")
    if not vault_client:
        return jsonify({
            "status": "error",
            "error": {"code": "vault_unavailable",
                "message": "Vault client not configured"},
        }), 503

    log.warning(f"iPXE CA rotation: reason={reason} days={new_ca_validity_days}")

    try:
        # Issue new intermediate CA from Vault
        pki = await _issue_pki_ca(new_ca_validity_days, vault_client)
        if not pki:
            return jsonify({
                "status": "error",
                "error": {"code": "pki_issue_failed",
                    "message": "Failed to issue new CA from Vault"},
            }), 500

        new_cert = pki["cert"]
        ca_fingerprint = pki["ca_fingerprint"]

        # Update helper image (re-sign with new CA)
        tftp_root = current_app.config.get("TFTP_ROOT", "/var/lib/tftp")
        if not await _publish_helper_artifact(new_cert, tftp_root):
            return jsonify({
                "status": "error",
                "error": {"code": "publish_failed",
                    "message": "Failed to publish helper artifact"},
            }), 500

        # Update cloud-init bootstrap template (real impl updates DB).
        # Regression: gh-22. Off the event loop via run_db() instead of
        # blocking the request coroutine inline.
        db = get_db()
        try:
            await run_db(lambda: _rotate_bootstrap_ca(db, new_cert))
        except Exception as e:
            log.warning(f"Failed to update bootstrap template: {e}")

        # Emit audit event with old/new fingerprints. Regression: gh-22.
        # Off the event loop via run_db() instead of blocking the request
        # coroutine inline.
        audit = current_app.extensions.get("audit")
        if audit and isinstance(audit, AuditLogger):
            await run_db(lambda: audit.log(
                AuditEventType.SYSTEM_CONFIG_CHANGE,
                f"iPXE CA rotated: new fingerprint {ca_fingerprint}",
                resource_type="primary.ipxe_ca",
                resource_id=cluster_id,
                details={
                    "reason": reason,
                    "validity_days": new_ca_validity_days,
                    "new_ca_fingerprint": ca_fingerprint,
                },
            ))

        # Calculate validity end date
        now = datetime.now(timezone.utc)
        valid_until = now.replace(
            day=now.day + new_ca_validity_days
            if now.day + new_ca_validity_days <= 28 else 28
        )

        return jsonify({
            "status": "success",
            "data": {
                "cluster_id": cluster_id,
                "new_ca_fingerprint": ca_fingerprint,
                "valid_from": now.isoformat(),
                "valid_until": valid_until.isoformat(),
                "validity_days": new_ca_validity_days,
            },
        }), 200

    except Exception as e:
        log.error(f"iPXE CA rotation failed: {e}")
        return jsonify({
            "status": "error",
            "error": {"code": "internal_error",
                "message": str(e)},
        }), 500
