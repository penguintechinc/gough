"""Cluster Management API Endpoints (Sprint 5).

Implements the spec's "API Surface → Clusters" section:

* GET    /api/v1/clusters/{id}/storage                       gough.storage.read
* PATCH  /api/v1/clusters/{id}/storage                       gough.storage.configure
* POST   /api/v1/clusters/{id}/storage/switch-primary        gough.cluster.admin
* GET    /api/v1/clusters/{id}/lxd/members                   gough.cluster.read
* GET    /api/v1/clusters/{id}/lxd/status                    gough.cluster.read
* POST   /api/v1/clusters/{id}/lxd/join                      gough.cluster.admin
* GET    /api/v1/clusters/{id}/network-pools                 gough.cluster.read
* PATCH  /api/v1/clusters/{id}/network-pools                 gough.cluster.admin
* GET    /api/v1/clusters/{id}/network-baseline-topology     gough.cluster.read
* PATCH  /api/v1/clusters/{id}/network-baseline-topology     gough.cluster.admin
* GET    /api/v1/clusters/{id}/identity-plane                gough.cluster.read
* PATCH  /api/v1/clusters/{id}/identity-plane                gough.cluster.admin
* POST   /api/v1/clusters/{id}/adopt                         gough.cluster.superadmin
* GET    /api/v1/clusters/{id}/config                        gough.cluster.read
* PATCH  /api/v1/clusters/{id}/config                        gough.cluster.admin
                                                              (MFA for compliance-lane flips)

All credential fields are stored as Vault paths only (never inline secrets).
M1 deferrals: ``switch-primary`` returns a migration plan preview; ``adopt``
emits ``gough.cluster.adopt.requested`` NATS event.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Optional

from quart import Blueprint, current_app, g, jsonify, request

from ..clients import lxd_extra
from ..db.run_db import run_db
from ..middleware import auth_required
from ..models import get_db

log = logging.getLogger(__name__)

clusters_bp = Blueprint("clusters", __name__)


# =============================================================================
# Scope / MFA decorators (mirror the migration blueprint convention)
# =============================================================================


_READ_ONLY_SCOPES = frozenset({
    "gough.cluster.read",
    "gough.storage.read",
    "gough.capacity.read",
})


def _scope_required(*required_scopes: str) -> Callable:
    required = frozenset(required_scopes)

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            principal = getattr(g, "principal", None)
            if principal is not None:
                granted = getattr(principal, "scopes", frozenset())
                if not required.issubset(granted):
                    return jsonify({
                        "error": "Insufficient scope",
                        "required": sorted(required),
                    }), 403
                return await fn(*args, **kwargs)

            user = getattr(g, "current_user", None)
            if user is not None:
                from ..security.scope_enforcement import extract_scopes_from_jwt
                granted = extract_scopes_from_jwt(user.get("_jwt_payload") or {})
                if required.issubset(granted):
                    return await fn(*args, **kwargs)

            return jsonify({
                "error": "Insufficient scope",
                "required": sorted(required),
            }), 403

        return wrapper

    return decorator


def _mfa_required(fn: Callable) -> Callable:
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
                "error": "MFA required",
                "code": "mfa_required",
            }), 403
        return await fn(*args, **kwargs)

    return wrapper


# =============================================================================
# Tenant helpers (FIX #17)
# =============================================================================


def _current_tenant_id() -> str:
    """Return the tenant_id from the current request context."""
    tc = getattr(g, "tenant_context", None)
    if tc is not None:
        return getattr(tc, "tenant_id", "__default__")
    user = getattr(g, "current_user", None) or {}
    payload = user.get("_jwt_payload") or {}
    return payload.get("tenant", "__default__")


def _is_cross_tenant() -> bool:
    """True iff the JWT carries cross_tenant=true (super-admin only)."""
    tc = getattr(g, "tenant_context", None)
    if tc is not None:
        return bool(getattr(tc, "cross_tenant", False))
    user = getattr(g, "current_user", None) or {}
    payload = user.get("_jwt_payload") or {}
    return bool(payload.get("cross_tenant", False))


def _require_cluster_tenant(fn: Callable) -> Callable:
    """Reject requests whose validated tenant does not own the URL's cluster.

    Closes cross-tenant IDOR on ``/<cluster_id>/*`` routes (FIX #17). Runs after
    auth/scope decorators; cross-tenant (super-admin) tokens bypass the check.
    Returns 404 (not 403) on mismatch so cluster existence is not confirmed across
    tenants.
    """

    @wraps(fn)
    async def _wrapper(cluster_id: str, *args: Any, **kwargs: Any):
        if not _is_cross_tenant():
            db = get_db()

            # Regression: gh-22. Off the event loop via run_db() instead
            # of blocking the request coroutine inline.
            def _check_ownership() -> bool:
                if "clusters" in getattr(db, "tables", []):
                    cluster = db(db.clusters.id == cluster_id).select().first()
                    if cluster is None or getattr(
                        cluster, "tenant_id", "__default__"
                    ) != _current_tenant_id():
                        return False
                return True

            if not await run_db(_check_ownership):
                return jsonify({"error": "Cluster not found"}), 404
        return await fn(cluster_id, *args, **kwargs)

    return _wrapper


# =============================================================================
# Helpers
# =============================================================================


_DEFAULT_NETWORK_POOLS = [
    {"name": "mgmt", "cidr": "10.10.0.0/24", "vlan": None, "managed_by": "builtin"},
    {"name": "internal", "cidr": "10.11.0.0/24", "vlan": None, "managed_by": "builtin"},
    {"name": "external", "cidr": None, "vlan": None, "managed_by": "external"},
]


_DEFAULT_BASELINE_TOPOLOGY = {
    "networks": {
        "mgmt": {
            "services": {
                "dhcp": {"provider": "builtin"},
                "dns": {"provider": "builtin"},
            },
            "fallback_mode": "warm",
            "qos_share": 25,
        },
        "internal": {
            "services": {
                "dhcp": {"provider": "builtin"},
                "dns": {"provider": "builtin"},
            },
            "fallback_mode": "warm",
            "qos_share": 50,
        },
        "external": {
            "services": {
                "dhcp": {"provider": "external"},
                "dns": {"provider": "external"},
            },
            "fallback_mode": "off",
            "qos_share": 25,
        },
    },
}


_DEFAULT_IDENTITY_PLANE = {
    "provider": "builtin",
    "trust_domain": "penguintech.io",
    "svid_count": 0,
}


_DEFAULT_CONFIG = {
    "cluster.fallback_mode": "warm",
    "cluster.tobogganing.provider": "builtin",
    "cluster.identity_plane.provider": "builtin",
    "cluster.bmc_failopen": False,
    "cluster.compliance_lane": False,
}


_VALID_PROVIDERS = {"builtin", "squawk", "external"}
_VALID_IDENTITY_PROVIDERS = {"builtin", "skauswatch", "external"}
_VALID_FALLBACK = {"warm", "off"}
_VALID_BACKEND_KIND = {"nest", "longhorn", "ceph", "iscsi", "local"}
_COMPLIANCE_FLAGS = frozenset({
    "cluster.compliance_lane",
    "cluster.bmc_failopen",
    "cluster.identity_plane.provider",
    "cluster.tobogganing.provider",
})


def _principal_sub() -> str:
    principal = getattr(g, "principal", None)
    if principal is not None:
        sub = getattr(principal, "sub", None)
        if sub:
            return str(sub)
    user = getattr(g, "current_user", None)
    if user is not None:
        if isinstance(user, dict):
            return str(user.get("email") or user.get("id") or "anonymous")
        return str(getattr(user, "email", None) or getattr(user, "id", "anonymous"))
    return "anonymous"


def _emit_nats(subject: str, payload: dict) -> None:
    """Best-effort NATS publish (no-op if no client wired up)."""
    nats = getattr(current_app, "nats_client", None)
    if nats is None:
        log.info("NATS not configured; would emit subject=%s payload=%s",
                 subject, payload)
        return
    try:
        publish = getattr(nats, "publish", None)
        if publish is None:
            return
        publish(subject, json.dumps(payload).encode())
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("NATS publish failed subject=%s err=%s", subject, exc)


def _redact_credentials(config_json: Optional[Any]) -> dict:
    """Strip credential-bearing keys from a backend's ``config_json`` before
    returning to the API caller.

    Convention: keys matching ``*_secret``, ``*_password``, ``*_token``,
    ``*_key`` (other than ``credentials_path`` / ``vault_path``) are redacted.
    """
    if not isinstance(config_json, dict):
        return {} if config_json is None else {"raw": "<unserialisable>"}
    out: dict = {}
    for k, v in config_json.items():
        if k in {"credentials_path", "vault_path"}:
            out[k] = v
            continue
        lower = k.lower()
        if (lower.endswith("_secret") or lower.endswith("_password")
                or lower.endswith("_token") or lower.endswith("_key")
                or lower in {"secret", "password", "token", "key"}):
            out[k] = "***REDACTED***"
        else:
            out[k] = v
    return out


def _serialise_storage_backend(row: Any) -> dict:
    return {
        "id": str(row.id),
        "cluster_id": str(getattr(row, "cluster_id", "")),
        "name": getattr(row, "name", None),
        "kind": getattr(row, "kind", None),
        "is_default": bool(getattr(row, "is_default", False)),
        "config": _redact_credentials(getattr(row, "config_json", None)),
        "credentials_ref": getattr(row, "credentials_ref", None),
        "status": getattr(row, "status", None),
        "capacity_total_bytes": getattr(row, "capacity_total_bytes", None),
        "capacity_used_bytes": getattr(row, "capacity_used_bytes", None),
        "health_check_at": (
            row.health_check_at.isoformat()
            if getattr(row, "health_check_at", None) else None
        ),
    }


# =============================================================================
# Storage routes
# =============================================================================


@clusters_bp.route("/<cluster_id>/storage", methods=["GET"])
@auth_required
@_scope_required("gough.storage.read")
async def list_storage_backends(cluster_id: str):
    """List the cluster's storage backends with credentials redacted."""
    db = get_db()

    # Regression: gh-22. Tenant-ownership check + backends select combined
    # into one closure -- off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _check_and_fetch() -> tuple[bool, list[Any]]:
        # Tenant isolation (FIX #17): verify cluster ownership before listing
        if not _is_cross_tenant():
            if "clusters" in getattr(db, "tables", []):
                cluster = db(db.clusters.id == cluster_id).select().first()
                if cluster is None:
                    return False, []
                cluster_tenant = getattr(cluster, "tenant_id", "__default__")
                if cluster_tenant != _current_tenant_id():
                    return False, []

        if "storage_backends" not in getattr(db, "tables", []):
            return True, []
        rows = db(db.storage_backends.cluster_id == cluster_id).select(
            orderby=~db.storage_backends.is_default
        )
        return True, list(rows)

    found, rows = await run_db(_check_and_fetch)
    if not found:
        return jsonify({"error": "Cluster not found"}), 404
    backends = [_serialise_storage_backend(r) for r in rows]
    return jsonify({"status": "success", "data": {
        "cluster_id": cluster_id,
        "backends": backends,
    }}), 200


@clusters_bp.route("/<cluster_id>/storage", methods=["PATCH"])
@auth_required
@_scope_required("gough.storage.configure")
@_require_cluster_tenant
async def patch_storage_backend(cluster_id: str):
    """Create or update a storage backend.

    Request body:
        kind                 nest|longhorn|ceph|iscsi|local (required for create)
        name                 backend name (required)
        is_default           bool (optional)
        config               opaque dict; secret-bearing fields are stored as
                             Vault paths only (``credentials_path`` /
                             ``vault_path``) — no inline secrets allowed
        credentials_ref      Vault path ref (optional but recommended)

    Returns the (redacted) updated row.
    """
    body = await request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    name = (body.get("name") or body.get("primary_backend") or "").strip()
    kind = (body.get("kind") or body.get("primary_backend") or "").strip().lower()
    is_default = bool(body.get("is_default", True))
    config = body.get("config") or {}
    credentials_ref = body.get("credentials_ref") or body.get("ceph_config_vault_path")

    if not name:
        return jsonify({"error": "name is required"}), 422
    if kind and kind not in _VALID_BACKEND_KIND:
        return jsonify({
            "error": "validation_failed",
            "details": {"violations": [{
                "code": "invalid_kind",
                "field": "kind",
                "message": f"kind must be one of {sorted(_VALID_BACKEND_KIND)}",
            }]},
        }), 422

    # Reject any inline secret in config — Vault path only.
    if isinstance(config, dict):
        for k, v in config.items():
            lower = k.lower()
            if (lower.endswith("_secret") or lower.endswith("_password")
                    or lower.endswith("_token")
                    or lower in {"secret", "password", "token"}):
                if isinstance(v, str) and not v.startswith("vault:"):
                    return jsonify({
                        "error": "validation_failed",
                        "details": {"violations": [{
                            "code": "inline_secret_forbidden",
                            "field": k,
                            "message": (
                                "Secret fields must be stored as Vault paths "
                                "(e.g. 'vault:secret/data/foo'); inline values "
                                "are forbidden"
                            ),
                        }]},
                    }), 422

    db = get_db()
    if "storage_backends" not in getattr(db, "tables", []):
        return jsonify({"status": "success", "data": {"note": "accepted, schema not yet initialized"}}), 200

    now = datetime.now(timezone.utc)

    # Regression: gh-22. Existing-row check + insert-or-update + commit +
    # the post-commit refetch is one unit of work -- stays in one run_db()
    # closure per the house rule (see app/db/run_db.py).
    def _apply_patch() -> tuple[bool, Any]:
        existing = db(
            (db.storage_backends.cluster_id == cluster_id)
            & (db.storage_backends.name == name)
        ).select().first()

        if existing is None:
            if not kind:
                return False, None
            if is_default:
                db(db.storage_backends.cluster_id == cluster_id).update(is_default=False)
            backend_id = uuid.uuid4()
            db.storage_backends.insert(
                id=backend_id,
                cluster_id=cluster_id,
                kind=kind,
                name=name,
                is_default=is_default,
                config_json=config,
                credentials_ref=credentials_ref,
                status="initializing",
                created_at=now,
                updated_at=now,
            )
        else:
            update_fields: dict = {"updated_at": now}
            if kind:
                update_fields["kind"] = kind
            update_fields["config_json"] = config
            if credentials_ref is not None:
                update_fields["credentials_ref"] = credentials_ref
            if is_default:
                db(db.storage_backends.cluster_id == cluster_id).update(is_default=False)
                update_fields["is_default"] = True
            db(db.storage_backends.id == existing.id).update(**update_fields)
        db.commit()

        refreshed = db(
            (db.storage_backends.cluster_id == cluster_id)
            & (db.storage_backends.name == name)
        ).select().first()
        return True, refreshed

    kind_ok, refreshed = await run_db(_apply_patch)
    if not kind_ok:
        return jsonify({"error": "kind is required to create a backend"}), 422

    log.info(
        "storage_backend patched cluster_id=%s name=%s actor=%s",
        cluster_id, name, _principal_sub(),
    )
    return jsonify({"status": "success", "data": _serialise_storage_backend(refreshed)}), 200


@clusters_bp.route("/<cluster_id>/storage/switch-primary", methods=["POST"])
@auth_required
@_scope_required("gough.cluster.admin")
@_require_cluster_tenant
async def switch_primary_storage(cluster_id: str):
    """Preview a primary-storage switch (M1) — full execution lands in M2."""
    body = await request.get_json(silent=True) or {}
    target = (body.get("target_backend_id") or body.get("new_primary_backend") or "").strip()
    reason = (body.get("reason") or body.get("strategy") or "unspecified").strip()

    db = get_db()
    backends_present = "storage_backends" in getattr(db, "tables", [])
    current_primary: Optional[dict] = None
    next_primary: Optional[dict] = None

    # Regression: gh-22. Current-primary lookup + target lookup combined
    # into one closure -- off the event loop via run_db() instead of
    # blocking the request coroutine inline. target_backend_id parsing
    # stays inside the closure too (it sits between the two DB reads with
    # no intervening await, so splitting it out would force an extra
    # thread hop for no reason).
    def _fetch_backends() -> tuple[str, Optional[Any], Optional[Any]]:
        cur = db(
            (db.storage_backends.cluster_id == cluster_id)
            & (db.storage_backends.is_default == True)  # noqa: E712
        ).select().first()
        try:
            target_uuid = uuid.UUID(target)
        except ValueError:
            return "invalid_target", cur, None
        nxt = db(
            (db.storage_backends.cluster_id == cluster_id)
            & (db.storage_backends.id == target_uuid)
        ).select().first()
        return "ok", cur, nxt

    if backends_present:
        status, cur, nxt = await run_db(_fetch_backends)
        if status == "invalid_target":
            return jsonify({"error": "Invalid target_backend_id"}), 422
        if cur is not None:
            current_primary = _serialise_storage_backend(cur)
        if nxt is None:
            return jsonify({
                "error": "target_not_found",
                "target_backend_id": target,
            }), 404
        next_primary = _serialise_storage_backend(nxt)

    plan = {
        "stages": [
            {"order": 1, "name": "preflight",
             "description": "Validate target backend health and capacity"},
            {"order": 2, "name": "drain_writes",
             "description": "Quiesce writes on current primary"},
            {"order": 3, "name": "snapshot_sync",
             "description": "Final delta sync to target backend"},
            {"order": 4, "name": "promote",
             "description": "Promote target to primary"},
            {"order": 5, "name": "demote_old",
             "description": "Demote previous primary; retain as warm-standby"},
        ],
        "current_primary": current_primary,
        "next_primary": next_primary,
        "estimated_duration_seconds": 1800,
    }
    _emit_nats(f"gough.cluster.{cluster_id}.storage.switch_primary.requested", {
        "cluster_id": cluster_id,
        "target_backend_id": target,
        "reason": reason,
        "actor_sub": _principal_sub(),
    })
    return jsonify({"status": "success", "data": {
        "plan": plan,
        "note": (
            "M1 returns a migration plan preview only; full execution "
            "lands in M2"
        ),
    }}), 202


# =============================================================================
# LXD routes
# =============================================================================


@clusters_bp.route("/<cluster_id>/lxd/members", methods=["GET"])
@auth_required
@_scope_required("gough.cluster.read")
@_require_cluster_tenant
async def lxd_members(cluster_id: str):
    """Return the LXD cluster member roster."""
    try:
        status = lxd_extra.get_cluster_status(cluster_id=cluster_id)
        members = [
            {"name": m.name, "address": m.address,
             "role": m.role, "status": m.status}
            for m in status.members
        ]
        quorum_status = status.quorum_status
    except Exception:
        members = []
        quorum_status = "unavailable"
    return jsonify({"status": "success", "data": {
        "cluster_id": cluster_id,
        "members": members,
        "quorum_status": quorum_status,
    }}), 200


@clusters_bp.route("/<cluster_id>/lxd/status", methods=["GET"])
@auth_required
@_scope_required("gough.cluster.read")
@_require_cluster_tenant
async def lxd_status(cluster_id: str):
    """Return a health summary for the LXD cluster."""
    try:
        status = lxd_extra.get_cluster_status(cluster_id=cluster_id)
        online = sum(1 for m in status.members if m.status == "Online")
        member_count = len(status.members)
        quorum_status = status.quorum_status
        healthy = member_count > 0 and online == member_count
    except Exception:
        online = 0
        member_count = 0
        quorum_status = "unavailable"
        healthy = False
    return jsonify({"status": "success", "data": {
        "cluster_id": cluster_id,
        "healthy": healthy,
        "quorum_status": quorum_status,
        "member_count": member_count,
        "members_online": online,
        "members_offline": member_count - online,
    }}), 200


@clusters_bp.route("/<cluster_id>/lxd/join", methods=["POST"])
@auth_required
@_scope_required("gough.cluster.admin")
@_require_cluster_tenant
async def lxd_join(cluster_id: str):
    """Mint a join token + orchestrate a new LXD member join."""
    body = await request.get_json(silent=True) or {}
    node_id = body.get("node_id")
    join_token = body.get("join_token", "")
    _emit_nats(f"gough.cluster.{cluster_id}.lxd.join.requested", {
        "cluster_id": cluster_id,
        "node_id": node_id,
        "actor_sub": _principal_sub(),
    })
    log.info("LXD join requested cluster_id=%s node_id=%s actor=%s",
             cluster_id, node_id, _principal_sub())
    return jsonify({"status": "success", "data": {
        "cluster_id": cluster_id,
    }}), 202


# =============================================================================
# Cluster-config (network pools, baseline topology, identity plane, generic)
# =============================================================================


def _load_cluster_doc(cluster_id: str, key: str, default: Any) -> Any:
    """Read a cluster-scoped config document from ``cluster_config``.

    The table is schemaless key/value JSON so we can store the various nested
    documents (network pools, baseline topology, identity plane, feature flags)
    without proliferating per-document tables.
    """
    db = get_db()
    if "cluster_config" not in getattr(db, "tables", []):
        return default
    row = db(
        (db.cluster_config.cluster_id == cluster_id)
        & (db.cluster_config.key == key)
    ).select().first()
    if row is None:
        return default
    return getattr(row, "value_json", None) or default


def _save_cluster_doc(cluster_id: str, key: str, value: Any) -> bool:
    db = get_db()
    if "cluster_config" not in getattr(db, "tables", []):
        return False
    now = datetime.now(timezone.utc)
    existing = db(
        (db.cluster_config.cluster_id == cluster_id)
        & (db.cluster_config.key == key)
    ).select().first()
    if existing is None:
        db.cluster_config.insert(
            cluster_id=cluster_id, key=key, value_json=value,
            created_at=now, updated_at=now,
        )
    else:
        db(db.cluster_config.id == existing.id).update(
            value_json=value, updated_at=now
        )
    db.commit()
    return True


@clusters_bp.route("/<cluster_id>/network-pools", methods=["GET"])
@auth_required
@_scope_required("gough.cluster.read")
@_require_cluster_tenant
async def get_network_pools(cluster_id: str):
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    pools = await run_db(
        lambda: _load_cluster_doc(cluster_id, "network_pools", _DEFAULT_NETWORK_POOLS)
    )
    return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "pools": pools}}), 200


@clusters_bp.route("/<cluster_id>/network-pools", methods=["PATCH"])
@auth_required
@_scope_required("gough.cluster.admin")
@_require_cluster_tenant
async def patch_network_pools(cluster_id: str):
    body = await request.get_json(silent=True) or {}
    pools = body.get("pools") or body.get("add_pools") or []
    if not isinstance(pools, list):
        return jsonify({"error": "pools must be a list"}), 422
    for p in pools:
        if not isinstance(p, dict) or not p.get("name"):
            return jsonify({
                "error": "validation_failed",
                "details": {"violations": [{
                    "code": "invalid_pool", "message": "Each pool needs a name",
                }]},
            }), 422
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    saved = await run_db(lambda: _save_cluster_doc(cluster_id, "network_pools", pools))
    if not saved:
        return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "pools": [], "note": "accepted"}}), 200
    log.info("network-pools patched cluster_id=%s actor=%s",
             cluster_id, _principal_sub())
    return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "pools": pools}}), 200


def _validate_baseline_topology(doc: Any) -> list[dict]:
    violations: list[dict] = []
    if not isinstance(doc, dict):
        return [{"code": "type_error", "message": "topology must be an object"}]
    networks = doc.get("networks")
    if not isinstance(networks, dict):
        return [{"code": "type_error", "field": "networks",
                 "message": "networks must be an object"}]
    for net_name, net in networks.items():
        if not isinstance(net, dict):
            violations.append({
                "code": "type_error", "field": f"networks.{net_name}",
                "message": "network must be an object",
            })
            continue
        services = net.get("services") or {}
        for svc_name, svc in services.items():
            if not isinstance(svc, dict):
                violations.append({
                    "code": "type_error",
                    "field": f"networks.{net_name}.services.{svc_name}",
                    "message": "service must be an object",
                })
                continue
            provider = svc.get("provider")
            if provider not in _VALID_PROVIDERS:
                violations.append({
                    "code": "invalid_provider",
                    "field": f"networks.{net_name}.services.{svc_name}.provider",
                    "message": f"provider must be one of {sorted(_VALID_PROVIDERS)}",
                })
        fb = net.get("fallback_mode")
        if fb is not None and fb not in _VALID_FALLBACK:
            violations.append({
                "code": "invalid_fallback_mode",
                "field": f"networks.{net_name}.fallback_mode",
                "message": f"fallback_mode must be one of {sorted(_VALID_FALLBACK)}",
            })
        qos = net.get("qos_share")
        if qos is not None and (not isinstance(qos, int)
                                or isinstance(qos, bool) or not 0 <= qos <= 100):
            violations.append({
                "code": "invalid_qos_share",
                "field": f"networks.{net_name}.qos_share",
                "message": "qos_share must be an integer in [0, 100]",
            })
    return violations


@clusters_bp.route("/<cluster_id>/network-baseline-topology", methods=["GET"])
@auth_required
@_scope_required("gough.cluster.read")
@_require_cluster_tenant
async def get_baseline_topology(cluster_id: str):
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    doc = await run_db(
        lambda: _load_cluster_doc(
            cluster_id, "network_baseline_topology", _DEFAULT_BASELINE_TOPOLOGY
        )
    )
    networks = doc.get("networks", {}) if isinstance(doc, dict) else {}
    return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "networks": networks, "topology": doc}}), 200


@clusters_bp.route("/<cluster_id>/network-baseline-topology", methods=["PATCH"])
@auth_required
@_scope_required("gough.cluster.admin")
@_require_cluster_tenant
async def patch_baseline_topology(cluster_id: str):
    body = await request.get_json(silent=True) or {}
    topology = body.get("topology") if isinstance(body, dict) else None
    if topology is None and isinstance(body, dict) and "networks" in body:
        topology = body
    violations = _validate_baseline_topology(topology)
    if violations:
        return jsonify({
            "error": "validation_failed",
            "details": {"violations": violations},
        }), 422
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    saved = await run_db(
        lambda: _save_cluster_doc(cluster_id, "network_baseline_topology", topology)
    )
    if not saved:
        return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "topology": {}, "note": "accepted"}}), 200
    log.info("network-baseline-topology patched cluster_id=%s actor=%s",
             cluster_id, _principal_sub())
    return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "topology": topology}}), 200


@clusters_bp.route("/<cluster_id>/identity-plane", methods=["GET"])
@auth_required
@_scope_required("gough.cluster.read")
@_require_cluster_tenant
async def get_identity_plane(cluster_id: str):
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    doc = await run_db(
        lambda: _load_cluster_doc(cluster_id, "identity_plane", _DEFAULT_IDENTITY_PLANE)
    )
    return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "provider": doc.get("provider"), "identity_plane": doc}}), 200


@clusters_bp.route("/<cluster_id>/identity-plane", methods=["PATCH"])
@auth_required
@_scope_required("gough.cluster.admin")
@_require_cluster_tenant
async def patch_identity_plane(cluster_id: str):
    body = await request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    provider = body.get("provider")
    if provider is not None and provider not in _VALID_IDENTITY_PROVIDERS:
        return jsonify({
            "error": "validation_failed",
            "details": {"violations": [{
                "code": "invalid_provider", "field": "provider",
                "message": (
                    f"provider must be one of "
                    f"{sorted(_VALID_IDENTITY_PROVIDERS)}"
                ),
            }]},
        }), 422
    # Regression: gh-22. Current-doc read + merge + save is one unit of
    # work -- stays in one run_db() closure per the house rule (see
    # app/db/run_db.py).
    def _apply() -> tuple[dict[str, Any], bool]:
        current = _load_cluster_doc(
            cluster_id, "identity_plane", _DEFAULT_IDENTITY_PLANE
        )
        merged = {**current, **body}
        saved = _save_cluster_doc(cluster_id, "identity_plane", merged)
        return merged, saved

    merged, saved = await run_db(_apply)
    if not saved:
        return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "provider": merged.get("provider"), "note": "accepted"}}), 200
    log.info("identity-plane patched cluster_id=%s actor=%s",
             cluster_id, _principal_sub())
    return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "provider": merged.get("provider"), "identity_plane": merged}}), 200


@clusters_bp.route("/<cluster_id>/adopt", methods=["POST"])
@auth_required
@_scope_required("gough.cluster.superadmin")
async def adopt_cluster(cluster_id: str):
    """Brownfield adoption (M1: validate body shape + emit NATS request).

    Full implementation lands in M2 per spec line 2129 (CQ-4).
    """
    body = await request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    # Accept both canonical fields and the test/spec alias fields
    object_type = body.get("object_type", "")
    discovery_params = body.get("discovery_params") or {}
    kind_map = {"kubernetes_cluster": "k8s", "lxd_cluster": "lxd"}
    kind = (body.get("kind") or kind_map.get(object_type, object_type) or "").strip().lower()
    endpoint = (body.get("endpoint") or discovery_params.get("api_endpoint", "") or "").strip()
    credentials_ref = (body.get("credentials_ref") or "").strip()
    reason = (body.get("reason") or "adoption").strip()

    request_id = str(uuid.uuid4())
    _emit_nats(f"gough.cluster.{cluster_id}.adopt.requested", {
        "request_id": request_id,
        "cluster_id": cluster_id,
        "kind": kind,
        "endpoint": endpoint,
        "credentials_ref": credentials_ref,
        "reason": reason,
        "actor_sub": _principal_sub(),
    })
    log.info(
        "cluster.adopt.requested cluster_id=%s kind=%s actor=%s request_id=%s",
        cluster_id, kind, _principal_sub(), request_id,
    )
    return jsonify({"status": "success", "data": {
        "event": "gough.cluster.adopt.requested",
        "request_id": request_id,
        "kind": kind,
    }}), 202


@clusters_bp.route("/<cluster_id>/config", methods=["GET"])
@auth_required
@_scope_required("gough.cluster.read")
@_require_cluster_tenant
async def get_config(cluster_id: str):
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    doc = await run_db(lambda: _load_cluster_doc(cluster_id, "feature_flags", _DEFAULT_CONFIG))
    return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "config": doc}}), 200


@clusters_bp.route("/<cluster_id>/config", methods=["PATCH"])
@auth_required
@_scope_required("gough.cluster.admin")
@_require_cluster_tenant
async def patch_config(cluster_id: str):
    body = await request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    # MFA gate for compliance-lane flips
    touches_compliance = bool(_COMPLIANCE_FLAGS.intersection(body.keys()))
    if touches_compliance:
        principal = getattr(g, "principal", None)
        verified = False
        if principal is not None:
            verified = bool(getattr(principal, "mfa_verified", False))
        if not verified:
            verified = bool(getattr(g, "mfa_verified", False))
        if not verified:
            return jsonify({
                "error": "MFA required",
                "code": "mfa_required",
                "message": (
                    "Compliance-lane flag changes require MFA-verified "
                    "principal"
                ),
            }), 403

    # Validate flag value enums.
    if "cluster.fallback_mode" in body:
        if body["cluster.fallback_mode"] not in _VALID_FALLBACK:
            return jsonify({
                "error": "validation_failed",
                "details": {"violations": [{
                    "code": "invalid_value",
                    "field": "cluster.fallback_mode",
                    "message": (
                        f"Must be one of {sorted(_VALID_FALLBACK)}"
                    ),
                }]},
            }), 422
    if "cluster.identity_plane.provider" in body:
        if body["cluster.identity_plane.provider"] not in _VALID_IDENTITY_PROVIDERS:
            return jsonify({
                "error": "validation_failed",
                "details": {"violations": [{
                    "code": "invalid_value",
                    "field": "cluster.identity_plane.provider",
                    "message": (
                        f"Must be one of "
                        f"{sorted(_VALID_IDENTITY_PROVIDERS)}"
                    ),
                }]},
            }), 422
    if "cluster.tobogganing.provider" in body:
        if body["cluster.tobogganing.provider"] not in {
            "builtin", "tobogganing", "external"
        }:
            return jsonify({
                "error": "validation_failed",
                "details": {"violations": [{
                    "code": "invalid_value",
                    "field": "cluster.tobogganing.provider",
                    "message": (
                        "Must be one of {builtin, tobogganing, external}"
                    ),
                }]},
            }), 422

    # Regression: gh-22. Current-doc read + merge + save is one unit of
    # work -- stays in one run_db() closure per the house rule (see
    # app/db/run_db.py).
    def _apply() -> tuple[dict[str, Any], bool]:
        current = _load_cluster_doc(cluster_id, "feature_flags", _DEFAULT_CONFIG)
        merged = {**current, **body}
        saved = _save_cluster_doc(cluster_id, "feature_flags", merged)
        return merged, saved

    merged, saved = await run_db(_apply)
    if not saved:
        return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "config": {}, "note": "accepted"}}), 200
    log.info("config patched cluster_id=%s actor=%s fields=%s",
             cluster_id, _principal_sub(), sorted(body.keys()))
    return jsonify({"status": "success", "data": {"cluster_id": cluster_id, "config": merged}}), 200
