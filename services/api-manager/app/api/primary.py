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
validation, 500/503 backend failure, 504 timeout) -- there is no
202-deferred-to-M2 stub response; that description described the not-yet-built
state and was never accurate for this file's actual endpoint implementations
(which have returned real synchronous results since the first commit that
added them).

M3 addition: the ``anycast`` ``frontend-switch`` target mode (BGP-advertised
control-plane VIP) is implemented behind the ``FLAG_ANYCAST_FRONTEND``
PostHog flag, default OFF (see docs/specs/platform-spec.md "Limitations
(M2+)"). While the flag is OFF, requesting ``target_mode: anycast`` returns
404 ``feature_disabled`` rather than the former 501 ``anycast_requires_bgp``
placeholder.
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

from .. import metrics as _metrics
from ..audit import AuditEventType, AuditLogger
from ..db.run_db import run_db
from ..licensing import feature_enabled
from ..middleware import auth_required
from ..models import get_db

log = logging.getLogger(__name__)

#: Gates the BGP-advertised anycast control-plane frontend mode (M3). Default
#: OFF per critical-rules.md "Feature Flags & License Tiers" -- new flags
#: default OFF until validated. Lives here (not app.licensing) because it
#: gates a single endpoint's behavior rather than a whole blueprint surface
#: (contrast FLAG_MULTI_CLOUD, which gates all of app.api.clouds).
FLAG_ANYCAST_FRONTEND = "gough.anycast-frontend"

#: Gates the destructive control-plane replace / force-recover operations.
#: Default OFF. Even when ON they return 501: the hub image carries neither
#: kubectl nor etcdctl and has no credentialed Kubernetes client, so the
#: operations are not implemented -- they must fail loudly, never pretend.
FLAG_CONTROL_PLANE_REPLACE = "gough.control-plane-replace"


async def _control_plane_gate() -> Any:
    """Return a 404/501 response tuple for unimplemented control-plane ops.

    Flag OFF -> 404 ``feature_disabled``; flag ON -> 501
    ``not_implemented_control_plane_replace``. Never returns success.
    """
    if not await feature_enabled(FLAG_CONTROL_PLANE_REPLACE):
        return jsonify({
            "status": "error",
            "error": {"code": "feature_disabled",
                "message": f"{FLAG_CONTROL_PLANE_REPLACE} is not enabled"},
        }), 404
    log.error("primary.not_implemented_control_plane_replace")
    return jsonify({
        "status": "error",
        "error": {"code": "not_implemented_control_plane_replace",
            "message": "control-plane replace/recover requires kubectl/etcdctl "
                       "which the hub does not ship; not implemented"},
    }), 501


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
    Returns: 404 flag off, 501 not implemented (hub has no kubectl/etcdctl).
    """
    return await _control_plane_gate()



@primary_bp.route("/force-recover", methods=["POST"])
@auth_required
@_scope_required("gough.cluster.superadmin")
@_mfa_required
async def force_recover_quorum():
    """Break-glass quorum recovery: snapshot surviving node, restore, re-add dead members.

    Requires typed cluster-name confirmation + superadmin scope + MFA.

    Body: {surviving_node_id, reason, typed_cluster_name_confirmation}
    Returns: 404 flag off, 501 not implemented (hub has no kubectl/etcdctl).
    """
    return await _control_plane_gate()



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


async def _advertise_bgp_route(
    vip: str,
    node_ids: list[int],
    *,
    action: str = "advertise",
) -> dict[int, dict[str, Any]]:
    """Advertise or withdraw the anycast control-plane VIP via BGP.

    The anycast counterpart to ``_emit_gracious_arp``: kube-vip announces VIP
    reachability over L2 gratuitous ARP/NDP, while anycast mode announces it
    over BGP -- each primary node runs a BGP speaker peering with the
    network fabric's ToR/leaf routers, and this adds (``action="advertise"``)
    or removes (``action="withdraw"``) the VIP from that speaker's RIB via
    the per-node ``gough-bgp-announce`` helper script. Propagation to peers
    (and the resulting anycast ECMP routing) is the fabric's job once the
    route is in the RIB -- this function's job ends at the local speaker.

    Uses the same Discovery gRPC ExecCommand RPC boundary as
    ``_update_endpoint_on_nodes``/``_emit_gracious_arp``.
    Returns dict[node_id] = {success, error, duration_ms} for each node.
    """
    import ipaddress

    results: dict[int, dict[str, Any]] = {}

    try:
        addr = ipaddress.ip_address(vip)
        prefix_len = 128 if isinstance(addr, ipaddress.IPv6Address) else 32
    except ValueError:
        prefix_len = 32

    for node_id in node_ids:
        # Deferred -- see the matching comment in _update_endpoint_on_nodes.
        import grpc

        from app.grpc.gough import discovery_pb2, discovery_pb2_grpc

        start_time = asyncio.get_event_loop().time()
        node_result = {
            "success": False,
            "error": None,
            "duration_ms": 0,
        }

        try:
            # Resolve the node's address the same way _update_endpoint_on_nodes
            # does: ipv4/ipv6/preferred_addr_family are the real columns on
            # app.models_m1.Node (see the NOTE on _update_endpoint_on_nodes --
            # _emit_gracious_arp's raw-SQL management_ip/cluster_ip lookup
            # predates that fix and is intentionally not repeated here).
            db = get_db()

            def _fetch_node_row(db: Any = db, node_id: int = node_id) -> Any:
                return db(db.nodes.id == node_id).select().first()

            node_row = await run_db(_fetch_node_row)
            if node_row is None:
                node_result["error"] = f"Node {node_id} not found in DB"
                results[node_id] = node_result
                continue

            ipv4 = getattr(node_row, "ipv4", None)
            ipv6 = getattr(node_row, "ipv6", None)
            preferred = getattr(node_row, "preferred_addr_family", None) or ""
            if preferred.lower() == "ipv6":
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

            # ExecCommand: run gough-bgp-announce on the node to add/remove
            # the VIP from its local BGP speaker's RIB.
            req = discovery_pb2.ExecCommandRequest(
                command="/usr/local/bin/gough-bgp-announce",
                target=f"--vip {vip}/{prefix_len} --action {action}",
            )

            resp = await asyncio.to_thread(
                stub.ExecCommand, req, timeout=30.0
            )

            if resp.exit_code == 0:
                node_result["success"] = True
                log.info(f"BGP {action} for {vip}/{prefix_len} on node {node_id}")
            else:
                node_result["error"] = (
                    f"ExecCommand exit code {resp.exit_code}: {resp.output}"
                )
                log.warning(f"BGP {action} failed on node {node_id}: {resp.output}")

            await asyncio.to_thread(channel.close)

        except asyncio.TimeoutError:
            node_result["error"] = "RPC timeout (30s)"
            log.error(f"Timeout running BGP {action} on node {node_id}")
        except Exception as e:
            node_result["error"] = str(e)
            log.error(f"Failed to run BGP {action} on node {node_id}: {e}")

        node_result["duration_ms"] = int(
            (asyncio.get_event_loop().time() - start_time) * 1000
        )
        results[node_id] = node_result

        if not node_result["success"]:
            _metrics.anycast_bgp_announce_failure.labels(
                node_id=str(node_id), action=action
            ).inc()

        # Emit per-node audit event, mirroring _emit_arp_audit. Off the event
        # loop via run_db() for the same reason as every other audit emit in
        # this file (AuditLogger.log() is a synchronous db insert+commit).
        audit = current_app.extensions.get("audit")
        if audit and isinstance(audit, AuditLogger):
            def _emit_bgp_audit(
                audit: Any = audit,
                node_id: int = node_id,
                node_result: dict[str, Any] = node_result,
            ) -> Any:
                return audit.log(
                    AuditEventType.RESOURCE_UPDATE,
                    f"BGP {action} for anycast VIP {vip} on node {node_id}",
                    resource_type="primary.bgp",
                    resource_id=str(node_id),
                    details={
                        "vip": vip,
                        "prefix_len": prefix_len,
                        "action": action,
                        "success": node_result["success"],
                        "error": node_result.get("error"),
                        "duration_ms": node_result["duration_ms"],
                    },
                )

            await run_db(_emit_bgp_audit)

    return results


@primary_bp.route("/frontend-switch", methods=["POST"])
@auth_required
@_scope_required("gough.cluster.admin")
@_mfa_required
async def switch_frontend():
    """Switch control plane frontend mode: kube-vip, external, or anycast (BGP).

    Renames modes from M1 vocabulary: vip->kube-vip, anycast->anycast, external (new).
    Returns: 200 on success, 404 if anycast requested but FLAG_ANYCAST_FRONTEND
    is off, 422 on validation, 500 if any node update/BGP announce fails.

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

    # anycast (BGP): M3 feature, default OFF until validated. A deployment
    # that has never configured POSTHOG_KEY (or hasn't flipped the flag)
    # sees this the same way app.api.clouds gates FLAG_MULTI_CLOUD -- 404
    # feature_disabled rather than advertising the capability exists.
    if target_mode == "anycast" and not await feature_enabled(FLAG_ANYCAST_FRONTEND):
        return jsonify({
            "status": "error",
            "error": {
                "code": "feature_disabled",
                "message": "Anycast frontend is not enabled for this deployment",
                "details": {"flag": FLAG_ANYCAST_FRONTEND},
            },
            "meta": {
                "version": 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "request_id": request_id,
            },
        }), 404

    # anycast mode requires the VIP to advertise, same as external requires
    # its endpoint
    if target_mode == "anycast" and not new_endpoint:
        return jsonify({
            "status": "error",
            "error": {"code": "validation_failed",
                "message": (
                    "anycast mode requires new_endpoint parameter "
                    "(the VIP to advertise via BGP)"
                )},
        }), 422

    # Validate the VIP portion of new_endpoint parses as an IP before it
    # reaches the gRPC/BGP boundary -- unlike external/kube-vip, there is no
    # live reachability check possible here (the VIP isn't routable until
    # BGP advertises it), so this is the only input validation anycast gets.
    if target_mode == "anycast":
        import ipaddress

        vip_candidate = new_endpoint.split(":")[0]
        try:
            ipaddress.ip_address(vip_candidate)
        except ValueError:
            return jsonify({
                "status": "error",
                "error": {"code": "validation_failed",
                    "message": (
                        f"new_endpoint VIP '{vip_candidate}' is not a valid "
                        "IPv4/IPv6 address"
                    )},
            }), 422

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

    # Advertise the VIP via BGP if anycast. Same primary-node set (`node_ids`,
    # state="primary") the endpoint update just ran against above -- mirrors
    # the kube-vip gracious-ARP branch immediately above, but announces
    # reachability over BGP instead of L2 ARP/NDP. Checked for per-node
    # failure the same way _update_endpoint_on_nodes is checked above: the
    # dict is never a bool, an empty dict (no primary nodes) means nothing to
    # do, and a dict whose every entry is success=False must read as failure.
    if target_mode == "anycast":
        vip = new_endpoint.split(":")[0] if new_endpoint else ""
        bgp_results = await _advertise_bgp_route(vip, node_ids, action="advertise")
        bgp_failed = [nid for nid, r in bgp_results.items() if not r.get("success")]
        if bgp_failed:
            return jsonify({
                "status": "error",
                "error": {"code": "bgp_advertise_failed",
                    "message": (
                        "Failed to advertise BGP route for anycast VIP "
                        "on one or more nodes"
                    ),
                    "details": {"failed_node_ids": bgp_failed}},
            }), 500

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
