"""Access Agent Management API Endpoints.

Provides REST API for agent enrollment, authentication, and management.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from pydantic import ValidationError
from quart import Blueprint, current_app, g, jsonify, request

from .. import capacity_guard
from ..db.run_db import run_db
from ..middleware import auth_required, get_current_user, roles_required, user_has_role
from ..audit import get_audit_logger
from ..models import get_db
from ._helpers import (
    request_is_cross_tenant,
    request_tenant_id,
    resolve_node_id_by_name,
    tenant_owns,
)
from .lxd_cluster import parse_metadata

log = logging.getLogger(__name__)

agents_bp = Blueprint("agents", __name__, url_prefix="/api/v1/agents")


def _parse_capabilities(raw: str | None) -> list[str]:
    """Safely parse a stored ``capabilities`` value into a list of strings.

    ``capabilities`` is persisted as a JSON-encoded string (rows written after
    this fix). Rows written before this fix hold a Python ``repr()`` of a list
    (``str(["ssh"])`` -> ``"['ssh']"``), so ``ast.literal_eval`` (literals
    only -- never arbitrary code) is tried as a compatibility fallback.
    NEVER uses ``eval``: an enrolling caller fully controls this field's
    content via the request body, so ``eval`` on it is remote code execution
    (regression: audit eval RCE agents.py:395). Anything falsy, unparsable, or
    not a list of strings falls back to the safe default ``["ssh"]``.
    """
    if not raw:
        return ["ssh"]
    try:
        parsed: Any = json.loads(raw)
    except (ValueError, TypeError):
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError, TypeError):
            return ["ssh"]
    if isinstance(parsed, list) and all(isinstance(c, str) for c in parsed):
        return parsed
    return ["ssh"]


# ============================================================================
# Enrollment Key Management
# ============================================================================


@agents_bp.route("/enrollment-keys", methods=["POST"])
@auth_required
@roles_required("admin")
async def create_enrollment_key():
    """Generate a new enrollment key for agent enrollment.

    Request Body (optional):
        expires_in_hours: Key validity in hours (default: 24)
        metadata: Optional JSON metadata for the agent

    Returns:
        201: Enrollment key created
        500: Server error
    """
    data = await request.get_json() or {}
    expires_in_hours = data.get("expires_in_hours", 24)
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return jsonify({"error": "metadata must be an object"}), 400
    # The key's tenant is stamped from the creating admin's token, never the body:
    # it scopes the agent<->node link made at enrollment.
    metadata = {**(metadata or {}), "tenant_id": request_tenant_id()}

    db = get_db()

    # Generate secure enrollment key
    # Format: ENROLL-XXXX-XXXX-XXXX-XXXX
    key_parts = [secrets.token_hex(2).upper() for _ in range(4)]
    enrollment_key = f"ENROLL-{'-'.join(key_parts)}"

    # Hash the key for storage
    key_hash = hashlib.sha256(enrollment_key.encode()).hexdigest()

    # Calculate expiry
    expires_at = datetime.utcnow() + timedelta(hours=expires_in_hours)

    current_user = get_current_user()

    # Regression: gh-22. Insert + commit is one unit of work -- stays in
    # one run_db() closure per the house rule (see app/db/run_db.py),
    # single rollback point inside the closure.
    def _store_key() -> tuple[bool, Any]:
        try:
            key_id = db.enrollment_keys.insert(
                key_hash=key_hash,
                created_by=current_user["id"],
                expires_at=expires_at,
                metadata=json.dumps(metadata),
            )
            db.commit()
            return True, key_id
        except Exception as e:
            db.rollback()
            log.exception(f"Error creating enrollment key: {e}")
            return False, e

    ok, result = await run_db(_store_key)
    if not ok:
        return jsonify({"error": str(result)}), 500

    key_id = result
    log.info(f"Enrollment key created by user {current_user['id']}")

    return jsonify({
        "message": "Enrollment key created",
        "enrollment_key": enrollment_key,
        "expires_at": expires_at.isoformat(),
        "key_id": key_id,
    }), 201


@agents_bp.route("/enrollment-keys", methods=["GET"])
@auth_required
@roles_required("admin")
async def list_enrollment_keys():
    """List all enrollment keys.

    Query Parameters:
        include_used: Include used keys (default: false)

    Returns:
        200: List of enrollment keys
    """
    db = get_db()
    include_used = request.args.get("include_used", "false").lower() == "true"

    query = db.enrollment_keys.id > 0
    if not include_used:
        # Pre-existing bug (fix-round, gh-22): `is False` is a Python
        # identity check against a PyDAL field proxy -- never true for a
        # real Field object, so this branch always silently no-opped
        # instead of actually filtering. `== False` is the correct DAL
        # idiom (see e.g. this file's own enroll_agent() claim query, and
        # `noqa: E712` usage throughout the codebase).
        query &= db.enrollment_keys.is_used == False  # noqa: E712 -- DAL idiom

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    keys = await run_db(lambda: db(query).select(orderby=~db.enrollment_keys.created_at).as_list())

    keys_data = []
    for key in keys:
        # Tenant isolation: a key is owned by the tenant stamped into its metadata at creation.
        if not tenant_owns(parse_metadata(key.get("metadata")).get("tenant_id")):
            continue
        keys_data.append({
            "id": key["id"],
            "created_by": key["created_by"],
            "expires_at": key["expires_at"].isoformat()
            if key["expires_at"] else None,
            "is_used": key["is_used"],
            "used_by_agent": key["used_by_agent"],
            "created_at": key["created_at"].isoformat()
            if key["created_at"] else None,
        })

    return jsonify({
        "enrollment_keys": keys_data,
        "count": len(keys_data),
    }), 200


@agents_bp.route("/enrollment-keys/<int:key_id>", methods=["DELETE"])
@auth_required
@roles_required("admin")
async def revoke_enrollment_key(key_id: int):
    """Revoke an enrollment key.

    Args:
        key_id: ID of the enrollment key

    Returns:
        200: Key revoked
        404: Key not found
    """
    db = get_db()

    # Regression: gh-22. Fetch + delete + commit is one unit of work --
    # stays in one run_db() closure per the house rule (see
    # app/db/run_db.py), single rollback point inside the closure.
    def _revoke() -> tuple[str, Any]:
        key = db.enrollment_keys(key_id)
        if not key or not tenant_owns(parse_metadata(key["metadata"]).get("tenant_id")):
            return "not_found", None
        try:
            # Soft-delete: keep the row so a revoked key can never be re-seeded or re-used.
            db(db.enrollment_keys.id == key_id).update(revoked=True)
            db.commit()
            return "ok", None
        except Exception as e:
            db.rollback()
            log.exception(f"Error revoking enrollment key: {e}")
            return "error", e

    status, err = await run_db(_revoke)
    if status == "not_found":
        return jsonify({"error": "Enrollment key not found"}), 404
    if status == "error":
        return jsonify({"error": str(err)}), 500

    return jsonify({"message": "Enrollment key revoked"}), 200


# ============================================================================
# Agent Enrollment
# ============================================================================


@agents_bp.route("/enroll", methods=["POST"])
async def enroll_agent():
    """Enroll a new access agent.

    Uses enrollment key for authentication.

    Headers:
        X-Enrollment-Key: The enrollment key

    Request Body:
        hostname: Agent hostname (required)
        ip_address: Agent IP address (optional)
        agent_version: Agent version string (optional)
        capabilities: List of capabilities (optional)

    Returns:
        201: Agent enrolled, returns JWT tokens
        400: Invalid request
        401: Invalid or expired enrollment key
        409: Enrollment key already used
    """
    # Get enrollment key from header
    enrollment_key = request.headers.get("X-Enrollment-Key")
    if not enrollment_key:
        return jsonify({"error": "Enrollment key required"}), 401

    data = await request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    hostname = data.get("hostname", "").strip()
    if not hostname:
        return jsonify({"error": "Hostname required"}), 400

    db = get_db()

    # Hash and find enrollment key. Regression: gh-22. Off the event loop
    # via run_db() instead of blocking the request coroutine inline.
    key_hash = hashlib.sha256(enrollment_key.encode()).hexdigest()
    key_record = await run_db(
        lambda: db(db.enrollment_keys.key_hash == key_hash).select().first()
    )

    if not key_record:
        return jsonify({"error": "Invalid enrollment key"}), 401

    if isinstance(key_record.revoked, (bool, int)) and bool(key_record.revoked):
        return jsonify({"error": "Enrollment key revoked"}), 401

    if key_record.is_used:
        return jsonify({"error": "Enrollment key already used"}), 409

    if key_record.expires_at and key_record.expires_at < datetime.utcnow():
        return jsonify({"error": "Enrollment key expired"}), 401

    # Validate capabilities is a list[str] before it ever touches storage or a
    # JWT claim. Regression: audit eval RCE agents.py:395 -- the untrusted
    # request body used to be stored via str(...) and later eval()'d on
    # refresh; rejecting anything but list[str] here closes the taint path at
    # its source.
    raw_capabilities = data.get("capabilities", ["ssh"])
    if not isinstance(raw_capabilities, list) or not all(
        isinstance(c, str) for c in raw_capabilities
    ):
        return jsonify({"error": "capabilities must be a list of strings"}), 400
    caps = raw_capabilities

    # Server-side agent<->node link. The enrollment key (not the request body)
    # carries the intended hostname; when present the agent must match it, and the
    # node is resolved from it. The capacity guard trusts only this link.

    key_meta = parse_metadata(key_record["metadata"])
    intended_raw = key_meta.get("intended_hostname")
    intended = intended_raw if isinstance(intended_raw, str) and intended_raw else None
    if intended is not None and hostname != intended:
        log.warning("enrollment refused: hostname does not match key intended_hostname")
        return jsonify({"error": "Hostname does not match enrollment key"}), 403
    link_node_id = (
        await run_db(lambda: _resolve_enrollment_link(db, key_meta, intended))
        if intended is not None
        else None
    )
    if intended is not None and link_node_id is None:
        capacity_guard.agent_enroll_unlinked.labels(
            "no_tenant" if not key_meta.get("tenant_id") else "no_unique_node"
        ).inc()
        log.warning(
            "agent enrolled UNLINKED (no capacity ingest): key has no tenant_id or no unique "
            "tenant-scoped node named %s",
            intended,
        )

    # Generate agent ID + JWT tokens (non-DB, pure/local)
    agent_id = str(uuid.uuid4())
    access_token = _create_agent_access_token(agent_id, caps)
    refresh_token, refresh_expires = _create_agent_refresh_token(agent_id)

    # Fix-round (gh-22): the is_used check above is a fast-path only -- two
    # concurrent enroll requests can both read is_used=False before either
    # writes (a run_db() closure boundary is a thread boundary, not a
    # transaction boundary; see app/db/run_db.py). The *authoritative*
    # guard is this closure's conditional UPDATE: it flips is_used only if
    # the row still reads False at write time, and Postgres serializes
    # concurrent UPDATEs to the same row, so at most one concurrent
    # request's UPDATE affects a row -- the rowcount is checked before any
    # agent is inserted, so a losing request never creates an agent row.
    def _enroll() -> tuple[str, Any]:
        try:
            claimed = db(
                (db.enrollment_keys.id == key_record.id)
                & (db.enrollment_keys.is_used == False)  # noqa: E712 -- DAL idiom
                & (db.enrollment_keys.revoked == False)  # noqa: E712 -- DAL idiom
            ).update(is_used=True)
            if not claimed:
                db.commit()
                return "already_used", None

            agent_db_id = db.access_agents.insert(
                agent_id=agent_id,
                hostname=hostname,
                node_id=link_node_id,
                tenant_id=key_meta.get("tenant_id") if isinstance(key_meta.get("tenant_id"), str) else None,
                ip_address=data.get("ip_address"),
                enrollment_key_hash=key_hash,
                enrollment_completed=True,
                status="active",
                capabilities=json.dumps(caps),
                enrolled_at=datetime.utcnow(),
                last_heartbeat=datetime.utcnow(),
            )

            # Record which agent claimed the key (already marked used above).
            db(db.enrollment_keys.id == key_record.id).update(
                used_by_agent=agent_db_id,
            )

            db.commit()

            # Get CA public key.
            # Pre-existing bug (fix-round, gh-22): `is True` is a Python
            # identity check against a PyDAL field proxy, never true for a
            # real Field object -- `db(False)` then raises inside
            # QuerySet's table-extraction (AttributeError: 'bool' object
            # has no attribute 'table'), turning every successful
            # enrollment into a 500. `== True` is the correct DAL idiom.
            ca_config = db(db.ssh_ca_config.is_active == True).select().first()  # noqa: E712 -- DAL idiom
            ca_public_key = ca_config.public_key if ca_config else None

            # Audit log
            audit_logger = get_audit_logger()
            if audit_logger:
                audit_logger.log_agent_enroll(
                    agent_id=agent_id,
                    hostname=hostname,
                    agent_version=data.get("agent_version", "unknown"),
                    details={
                        "ip_address": data.get("ip_address"),
                        "capabilities": caps,
                    },
                )

            return "ok", ca_public_key
        except Exception as e:
            db.rollback()
            log.exception(f"Error enrolling agent: {e}")
            return "error", e

    status, result = await run_db(_enroll)
    if status == "already_used":
        return jsonify({"error": "Enrollment key already used"}), 409
    if status == "error":
        return jsonify({"error": str(result)}), 500

    ca_public_key = result
    log.info(f"Agent {hostname} enrolled as {agent_id}")

    return jsonify({
        "message": "Agent enrolled successfully",
        "agent_id": agent_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_expires_in": 3600,
        "refresh_token_expires_in": 2592000,
        "ca_public_key": ca_public_key,
        "config": {
            "heartbeat_interval": 30,
        },
    }), 201


def _resolve_enrollment_link(db: Any, key_meta: dict[str, Any], intended: str) -> int | None:
    """Resolve the node an enrolling agent is bound to, from server-side key metadata.

    Requires the key's stamped ``tenant_id``: a key without one (e.g. issued before
    tenant stamping; re-link such agents with ``PUT /agents/{id}/node``) yields None and
    NEVER triggers an unscoped, cross-tenant name lookup. The node must be the unique
    node named ``intended_hostname`` within that tenant, and equal an explicit
    ``node_id`` in the metadata when one is present.
    """
    tenant = key_meta.get("tenant_id")
    if not isinstance(tenant, str) or not tenant:
        return None
    node_id = resolve_node_id_by_name(db, intended, tenant)
    explicit = key_meta.get("node_id")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return node_id if node_id == explicit else None
    return node_id


@agents_bp.route("/<string:agent_id>/node", methods=["PUT"])
@auth_required
@roles_required("admin")
async def link_agent_node(agent_id: str):
    """Link an enrolled agent to the node it reports capacity for (admin only).

    Body: ``{"node_id": <int>}``. The binding is server-side and is the only way the
    capacity guard maps heartbeat snapshots to a node.

    Returns:
        200: linked; 400: bad body; 404: agent or node unknown;
        409: node already linked to another active agent.
    """
    body = await request.get_json(silent=True)
    node_id = body.get("node_id") if isinstance(body, dict) else None
    if not isinstance(node_id, int) or isinstance(node_id, bool) or node_id < 1:
        return jsonify({"error": "node_id must be a positive integer"}), 400
    db = get_db()

    # Tenant-scoped admins may only link nodes of their own tenant; only a
    # cross-tenant super-admin token may link across tenants.
    expected_tenant = None if request_is_cross_tenant() else request_tenant_id()

    def _link() -> str | None:
        with db.engine.begin() as conn:
            return capacity_guard.link_agent_to_node(
                conn, agent_id, node_id, expected_tenant=expected_tenant
            )

    refusal = await run_db(_link)
    if refusal in ("agent_not_found", "node_not_found", "tenant_mismatch"):
        # all three look identical to the caller: no cross-tenant existence leak
        # tenant_mismatch reports as not-found: never confirm another tenant's node exists.
        return jsonify({"error": "not_found"}), 404
    if refusal is not None:
        return jsonify({"error": refusal}), 409
    log.info("agent %s linked to node %s", agent_id, node_id)
    return jsonify({"agent_id": agent_id, "node_id": node_id}), 200


# ============================================================================
# Agent Authentication
# ============================================================================


@agents_bp.route("/refresh", methods=["POST"])
async def refresh_agent_token():
    """Refresh agent JWT tokens.

    Uses refresh token for authentication.

    Headers:
        Authorization: Bearer <refresh_token>

    Returns:
        200: New tokens issued
        401: Invalid or expired refresh token
    """
    # Get refresh token from header
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Authorization header required"}), 401

    refresh_token = auth_header[7:]

    try:
        # Decode and validate token
        payload = jwt.decode(
            refresh_token,
            current_app.config["JWT_SECRET_KEY"],
            algorithms=["HS256"],
        )

        if payload.get("type") != "agent_refresh":
            return jsonify({"error": "Invalid token type"}), 401

        agent_id = payload.get("sub", "").replace("agent:", "")
        if not agent_id:
            return jsonify({"error": "Invalid token"}), 401

        db = get_db()

        # Verify agent exists and is active. Regression: gh-22. Off the
        # event loop via run_db() instead of blocking the request
        # coroutine inline.
        agent = await run_db(
            lambda: db(db.access_agents.agent_id == agent_id).select().first()
        )
        if not agent:
            return jsonify({"error": "Agent not found"}), 401

        if agent.status != "active":
            return jsonify({"error": "Agent is not active"}), 401

        # Generate new tokens. Regression: audit eval RCE agents.py:395 --
        # agent.capabilities is attacker-influenced (set at enrollment from the
        # request body), so it is parsed via _parse_capabilities (JSON, with a
        # literal-only ast.literal_eval fallback for pre-fix rows) and NEVER
        # eval()'d.
        capabilities = _parse_capabilities(agent.capabilities)
        access_token = _create_agent_access_token(agent_id, capabilities)
        new_refresh_token, _ = _create_agent_refresh_token(agent_id)

        log.debug(f"Tokens refreshed for agent {agent_id}")

        return jsonify({
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "access_token_expires_in": 3600,
            "refresh_token_expires_in": 2592000,
        }), 200

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Refresh token expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid refresh token"}), 401


@agents_bp.route("/heartbeat", methods=["POST"])
async def agent_heartbeat():
    """Receive heartbeat from agent.

    Headers:
        Authorization: Bearer <access_token>

    Request Body:
        agent_id: Agent ID
        status: Agent status
        active_sessions: Number of active sessions
        resource_usage: Resource usage metrics

    Returns:
        200: Heartbeat acknowledged
        401: Authentication failed
    """
    # Validate agent token
    agent_id = await _validate_agent_token()
    if not agent_id:
        return jsonify({"error": "Authentication required"}), 401

    data = await request.get_json() or {}

    db = get_db()

    # Regression: gh-22. Update + commit is one unit of work -- stays in
    # one run_db() closure per the house rule (see app/db/run_db.py),
    # single rollback point inside the closure.
    def _update_heartbeat() -> tuple[bool, Any]:
        try:
            stored = db(db.access_agents.agent_id == agent_id).select().first()
            if stored is not None and stored.status != "active":
                # A revoked/inactive agent can never re-activate itself by claiming
                # "active" in its own heartbeat body.
                db(db.access_agents.agent_id == agent_id).update(
                    last_heartbeat=datetime.utcnow(),
                )
                db.commit()
                return True, None
            db(db.access_agents.agent_id == agent_id).update(
                last_heartbeat=datetime.utcnow(),
                status=data.get("status", "active"),
            )
            db.commit()
            return True, None
        except Exception as e:
            db.rollback()
            log.exception(f"Error processing heartbeat: {e}")
            return False, e

    ok, err = await run_db(_update_heartbeat)
    if not ok:
        return jsonify({"error": str(err)}), 500

    # The heartbeat is recorded above even when the resource snapshot is rejected.
    raw_snapshot = data.get("resource_snapshot")
    if raw_snapshot is not None:
        snapshot_error = await run_db(
            lambda: _ingest_resource_snapshot(db, agent_id, raw_snapshot)
        )
        if snapshot_error is not None:
            return jsonify({
                "status": "heartbeat_recorded",
                "error": snapshot_error,
            }), 400

    # Check for pending commands (future feature)
    commands = []

    return jsonify({
        "status": "ok",
        "commands": commands,
    }), 200


def _ingest_resource_snapshot(db: Any, agent_id: str, raw: object) -> str | None:
    """Validate and persist a heartbeat's ``resource_snapshot`` for the agent's node.

    The node comes ONLY from the server-side ``access_agents.node_id`` link; the
    agent's self-reported hostname is never consulted. Refused when the agent is not
    active, is unlinked, or shares its node with another active agent. The hub's own
    clock is the ordering and freshness anchor; future-dated, replayed or already
    stale snapshots are rejected.

    Args:
        db: penguin-dal handle; its SQLAlchemy engine is used for the write.
        agent_id: Authenticated agent identifier from the access token.
        raw: Untrusted ``resource_snapshot`` value from the request body.

    Returns:
        None on success; a short error string when refused (-> 400).
    """
    try:
        snap = capacity_guard.parse_snapshot(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
        log.warning("rejected resource_snapshot from agent %s: %s", agent_id, details)
        return f"invalid resource_snapshot: {details}"

    with db.engine.begin() as conn:
        link = capacity_guard.resolve_agent_node(conn, agent_id)
        if link.node_id is None:
            log.warning("resource_snapshot refused for agent %s: %s", agent_id, link.reason)
            return f"resource_snapshot refused: {link.reason}"
        outcome = capacity_guard.record_snapshot(
            conn, link.node_id, snap, datetime.now(timezone.utc)
        )
    if outcome is not capacity_guard.SnapshotOutcome.STORED:
        log.warning("resource_snapshot from agent %s not stored: %s", agent_id, outcome.name)
        return f"resource_snapshot rejected: {outcome.name.lower()}"
    return None


# ============================================================================
# Agent Management
# ============================================================================


@agents_bp.route("/", methods=["GET"])
@auth_required
@roles_required("admin")
async def list_agents():
    """List all access agents.

    Query Parameters:
        status: Filter by status (active, suspended, pending)

    Returns:
        200: List of agents
    """
    db = get_db()

    status_filter = request.args.get("status")

    query = db.access_agents.id > 0
    if status_filter:
        query &= db.access_agents.status == status_filter

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    agents = await run_db(
        lambda: db(query).select(orderby=~db.access_agents.last_heartbeat).as_list()
    )

    agents_data = []
    for agent in agents:
        # Tenant isolation: unstamped legacy agents are visible to cross-tenant callers only.
        if not tenant_owns(agent.get("tenant_id")):
            continue
        agents_data.append({
            "id": agent["id"],
            "agent_id": agent["agent_id"],
            "hostname": agent["hostname"],
            "ip_address": agent["ip_address"],
            "status": agent["status"],
            "capabilities": _parse_capabilities(agent["capabilities"]),
            "enrollment_completed": agent["enrollment_completed"],
            "last_heartbeat": agent["last_heartbeat"].isoformat()
            if agent["last_heartbeat"] else None,
            "enrolled_at": agent["enrolled_at"].isoformat()
            if agent["enrolled_at"] else None,
        })

    return jsonify({
        "agents": agents_data,
        "count": len(agents_data),
    }), 200


@agents_bp.route("/<agent_id>", methods=["GET"])
@auth_required
@roles_required("admin")
async def get_agent(agent_id: str):
    """Get agent details.

    Args:
        agent_id: Agent UUID

    Returns:
        200: Agent details
        404: Agent not found
    """
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    agent = await run_db(
        lambda: db(db.access_agents.agent_id == agent_id).select().first()
    )
    if not agent or not tenant_owns(getattr(agent, "tenant_id", None)):
        return jsonify({"error": "Agent not found"}), 404

    return jsonify({
        "agent": {
            "id": agent.id,
            "agent_id": agent.agent_id,
            "hostname": agent.hostname,
            "ip_address": agent.ip_address,
            "status": agent.status,
            "capabilities": _parse_capabilities(agent.capabilities),
            "enrollment_completed": agent.enrollment_completed,
            "last_heartbeat": agent.last_heartbeat.isoformat()
            if agent.last_heartbeat else None,
            "enrolled_at": agent.enrolled_at.isoformat()
            if agent.enrolled_at else None,
            "created_at": agent.created_at.isoformat()
            if agent.created_at else None,
        },
    }), 200


@agents_bp.route("/<agent_id>/suspend", methods=["POST"])
@auth_required
@roles_required("admin")
async def suspend_agent(agent_id: str):
    """Suspend an agent.

    Args:
        agent_id: Agent UUID

    Returns:
        200: Agent suspended
        404: Agent not found
    """
    db = get_db()

    # Regression: gh-22. Fetch + update + commit is one unit of work --
    # stays in one run_db() closure per the house rule (see
    # app/db/run_db.py), single rollback point inside the closure.
    def _suspend() -> tuple[str, Any]:
        agent = db(db.access_agents.agent_id == agent_id).select().first()
        if not agent or not tenant_owns(getattr(agent, "tenant_id", None)):
            return "not_found", None
        try:
            db(db.access_agents.agent_id == agent_id).update(
                status="suspended",
                updated_at=datetime.utcnow(),
            )
            db.commit()
            return "ok", None
        except Exception as e:
            db.rollback()
            return "error", e

    status, err = await run_db(_suspend)
    if status == "not_found":
        return jsonify({"error": "Agent not found"}), 404
    if status == "error":
        return jsonify({"error": str(err)}), 500

    current_user = get_current_user()
    log.info(f"Agent {agent_id} suspended by user {current_user['id']}")

    return jsonify({"message": "Agent suspended"}), 200


@agents_bp.route("/<agent_id>/resume", methods=["POST"])
@auth_required
@roles_required("admin")
async def resume_agent(agent_id: str):
    """Resume a suspended agent.

    Args:
        agent_id: Agent UUID

    Returns:
        200: Agent resumed
        404: Agent not found
    """
    db = get_db()

    # Regression: gh-22. Fetch + update + commit is one unit of work --
    # stays in one run_db() closure per the house rule (see
    # app/db/run_db.py), single rollback point inside the closure.
    def _resume() -> tuple[str, Any]:
        agent = db(db.access_agents.agent_id == agent_id).select().first()
        if not agent or not tenant_owns(getattr(agent, "tenant_id", None)):
            return "not_found", None
        try:
            db(db.access_agents.agent_id == agent_id).update(
                status="active",
                updated_at=datetime.utcnow(),
            )
            db.commit()
            return "ok", None
        except Exception as e:
            db.rollback()
            return "error", e

    status, err = await run_db(_resume)
    if status == "not_found":
        return jsonify({"error": "Agent not found"}), 404
    if status == "error":
        return jsonify({"error": str(err)}), 500

    current_user = get_current_user()
    log.info(f"Agent {agent_id} resumed by user {current_user['id']}")

    return jsonify({"message": "Agent resumed"}), 200


# ============================================================================
# Helper Functions
# ============================================================================


def _create_agent_access_token(agent_id: str, capabilities: list) -> str:
    """Create JWT access token for agent.

    Args:
        agent_id: Agent UUID
        capabilities: List of agent capabilities

    Returns:
        JWT access token
    """
    expires = datetime.utcnow() + timedelta(hours=1)
    payload = {
        "sub": f"agent:{agent_id}",
        "type": "agent_access",
        "capabilities": capabilities,
        "exp": expires,
        "iat": datetime.utcnow(),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(
        payload,
        current_app.config["JWT_SECRET_KEY"],
        algorithm="HS256",
    )


def _create_agent_refresh_token(agent_id: str) -> tuple:
    """Create JWT refresh token for agent.

    Args:
        agent_id: Agent UUID

    Returns:
        Tuple of (token, expires_at)
    """
    expires = datetime.utcnow() + timedelta(days=30)
    payload = {
        "sub": f"agent:{agent_id}",
        "type": "agent_refresh",
        "exp": expires,
        "iat": datetime.utcnow(),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(
        payload,
        current_app.config["JWT_SECRET_KEY"],
        algorithm="HS256",
    )
    return token, expires


async def _validate_agent_token() -> Optional[str]:
    """Validate agent access token from request.

    Returns:
        Agent ID if valid, None otherwise
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]

    try:
        payload = jwt.decode(
            token,
            current_app.config["JWT_SECRET_KEY"],
            algorithms=["HS256"],
        )

        if payload.get("type") != "agent_access":
            return None

        agent_id = payload.get("sub", "").replace("agent:", "")
        return agent_id if agent_id else None

    except jwt.InvalidTokenError:
        return None
