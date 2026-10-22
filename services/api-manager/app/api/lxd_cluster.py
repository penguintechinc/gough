"""LXD cluster join token and bootstrap enrollment API.

Installer hub contracts:

- ``POST /api/v1/lxd/cluster-join-tokens`` mints a one-time LXD cluster join token.
  Authorization is server-side only, checked in this order:
    1. caller is an active agent (``agent_access`` bearer token);
    2. the agent's enrollment key is join-eligible (purpose ``node-join`` or ``bootstrap``)
       and not revoked;
    3. for ``node-join`` keys, the key's server-side ``intended_hostname`` equals the
       agent's enrolled hostname;
    4. ``member_name`` equals the agent's enrolled hostname.
  Single issuance is enforced by the database, not by process memory: the grant row is
  inserted first, and unique "live" columns (agent, member, key) reject a second live
  grant with IntegrityError, mapped to 409. A row becomes FAILED (and releases its live
  columns) only when the LXD mint fails. A successful ISSUED row consumes the agent's
  join right permanently; it is never re-granted by expiry.
- ``POST /api/v1/lxd/node-join-keys`` (admin) issues a single-use join-eligible enrollment
  key bound to ``intended_hostname``, returned once.
- ``seed_bootstrap_enrollment_key`` seeds ``GOUGH_BOOTSTRAP_TOKEN`` at most once per token
  hash. A ``bootstrap_token_seeds`` tombstone is written with the key and never deleted; if
  a key row exists without a tombstone, the tombstone is written and nothing is inserted.

Operators: remove GOUGH_BOOTSTRAP_TOKEN from the Secret and the deployment env after the
first successful enrollment, then rotate it. Upgrading from a build that predates the
tombstone table: remove the env var before upgrading, or rely on the migration backfill
for bootstrap keys it can identify.
"""

from __future__ import annotations

import base64
import ast
import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
import jwt
from quart import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from ..audit import AuditEventType, AuditSeverity, get_audit_logger
from ..db.run_db import run_db
from ..middleware import auth_required, get_current_user, roles_required
from ..models import get_db
from ..rate_limit import rate_limit

log = logging.getLogger(__name__)

lxd_bp = Blueprint("lxd", __name__, url_prefix="/api/v1/lxd")

PURPOSE_NODE_JOIN = "node-join"
PURPOSE_BOOTSTRAP = "bootstrap"
_JOIN_ELIGIBLE_PURPOSES = frozenset({PURPOSE_NODE_JOIN, PURPOSE_BOOTSTRAP})

ISSUANCE_ISSUED = 0
ISSUANCE_FAILED = 1
_TOKEN_VALIDITY = timedelta(minutes=15)  # informational: recorded as expires_at only

# LXD operations are always addressed as /1.0/operations/<uuid>; anything else is refused.
_LXD_OPERATION_PREFIX = "/1.0/operations/"
_LXD_OPERATION_WAIT_SECONDS = 30
_LXD_TIMEOUT = httpx.Timeout(connect=5.0, read=35.0, write=10.0, pool=5.0)

_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^{_HOST_LABEL}(?:\.{_HOST_LABEL})*$")
_HOSTNAME_MAX_LEN = 63

_BOOTSTRAP_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{32,256}$")
_KEY_TTL_DEFAULT_HOURS = 24
_KEY_TTL_MAX_HOURS = 24


@dataclass(slots=True, frozen=True)
class JoinTokenRequest:
    """Validated body of a cluster-join-token request."""

    member_name: str


@dataclass(slots=True, frozen=True)
class JoinTokenResponse:
    """Response body carrying the LXD cluster join token."""

    token: str


@dataclass(slots=True, frozen=True)
class AgentRecord:
    """Server-side agent facts used for authorization; never taken from the request."""

    agent_id: str
    status: str
    hostname: str
    enrollment_key_hash: Optional[str]


@dataclass(slots=True, frozen=True)
class KeyJoinInfo:
    """Server-side enrollment key facts relevant to join authorization."""

    key_hash: str
    purpose: Optional[str]
    intended_hostname: Optional[str]
    revoked: bool


class LxdUpstreamError(Exception):
    """LXD refused or failed the request; carries the HTTP status to surface."""

    def __init__(self, status: int, message: str) -> None:
        """Store the status to map back to the client and a non-secret message."""
        super().__init__(message)
        self.status = status


class LxdMemberExistsError(Exception):
    """The member already exists in the LXD cluster; surfaces as 409."""


class LxdUnavailableError(Exception):
    """LXD is unreachable, timed out, or not configured; surfaces as 503."""


class IssuanceConflictError(Exception):
    """A live issuance already exists for the agent, member, or key (unique constraint)."""


def mask_token(token: str) -> str:
    """Return a log-safe rendering of a token: ``tok_****1234``."""
    return f"tok_****{token[-4:]}" if len(token) >= 4 else "tok_****"


def is_valid_hostname(name: str) -> bool:
    """Check RFC 1123 hostname syntax and the 63-character length limit."""
    return len(name) <= _HOSTNAME_MAX_LEN and _HOSTNAME_RE.fullmatch(name) is not None


def parse_join_token_request(payload: Any) -> JoinTokenRequest:
    """Validate the request body; raise ValueError with a client-safe message on failure."""
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    unknown = set(payload) - {"member_name"}
    if unknown:
        raise ValueError("unknown field(s) in request body")
    member_name = payload.get("member_name")
    if not isinstance(member_name, str) or not member_name:
        raise ValueError("member_name is required")
    if not is_valid_hostname(member_name):
        raise ValueError("member_name must be a valid RFC 1123 hostname (max 63 characters)")
    return JoinTokenRequest(member_name=member_name)


def parse_metadata(raw: Any) -> dict[str, Any]:
    """Parse enrollment_keys.metadata written as JSON or as a Python dict repr (agents.py).

    ``ast.literal_eval`` only accepts literals, so this never executes code.
    """
    if not isinstance(raw, str) or not raw:
        return {}
    for loader in (json.loads, ast.literal_eval):
        try:
            value = loader(raw)
        except (ValueError, SyntaxError, TypeError):
            continue
        return value if isinstance(value, dict) else {}
    return {}


def _lxd_settings() -> tuple[str, Any, Any]:
    """Read LXD cluster primary URL, client cert tuple, and TLS verify setting from the environment."""
    api_url = os.environ.get("LXD_API_URL", "").rstrip("/")
    if not api_url.startswith("https://"):
        raise LxdUnavailableError("LXD_API_URL must be an https URL")
    cert_path = os.environ.get("LXD_CLIENT_CERT", "")
    key_path = os.environ.get("LXD_CLIENT_KEY", "")
    cert: Optional[tuple[str, str]] = (cert_path, key_path) if cert_path and key_path else None
    ca_file = os.environ.get("LXD_CA_FILE", "")
    if ca_file:
        verify: Any = ca_file
    else:
        verify = os.environ.get("LXD_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}
    return api_url, cert, verify


def _encode_join_token(metadata: Any) -> str:
    """Encode the LXD join token metadata into the base64 form ``lxd init --preseed`` expects."""
    if isinstance(metadata, str) and metadata:
        return metadata
    if isinstance(metadata, dict) and metadata:
        raw = json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")
    raise LxdUpstreamError(502, "LXD operation returned no join token")


async def _lxd_member_exists(client: httpx.AsyncClient, member_name: str) -> bool:
    """Return True when LXD already lists the member; False on 404; other statuses raise."""
    resp = await client.get(f"/1.0/cluster/members/{member_name}")
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    raise LxdUpstreamError(502, f"LXD member lookup failed (HTTP {resp.status_code})")


async def request_lxd_join_token(member_name: str) -> str:
    """Check the member is absent, call LXD ``POST /1.0/cluster/members``, return the join token."""
    api_url, cert, verify = _lxd_settings()
    try:
        async with httpx.AsyncClient(
            base_url=api_url, cert=cert, verify=verify, timeout=_LXD_TIMEOUT,
        ) as client:
            if await _lxd_member_exists(client, member_name):
                raise LxdMemberExistsError()
            resp = await client.post("/1.0/cluster/members", json={"server_name": member_name})
            if resp.status_code == 409:
                raise LxdMemberExistsError()
            if resp.status_code >= 400:
                raise LxdUpstreamError(502, f"LXD rejected cluster member request (HTTP {resp.status_code})")
            op_path = resp.json().get("operation", "")
            if not isinstance(op_path, str) or not op_path.startswith(_LXD_OPERATION_PREFIX):
                raise LxdUpstreamError(502, "LXD returned an unexpected operation reference")
            waited = await client.get(f"{op_path}/wait", params={"timeout": _LXD_OPERATION_WAIT_SECONDS})
            if waited.status_code >= 400:
                raise LxdUpstreamError(502, f"LXD operation wait failed (HTTP {waited.status_code})")
            operation = waited.json().get("metadata")
            if not isinstance(operation, dict) or operation.get("status_code") != 200:
                raise LxdUpstreamError(502, "LXD operation did not succeed")
            return _encode_join_token(operation.get("metadata"))
    except httpx.TimeoutException as exc:
        raise LxdUnavailableError(f"LXD request timed out: {type(exc).__name__}") from exc
    except httpx.TransportError as exc:
        raise LxdUnavailableError(f"LXD unreachable: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise LxdUpstreamError(502, "LXD returned a non-JSON response") from exc


def _authenticate_agent() -> Optional[str]:
    """Return the agent ID from a valid ``agent_access`` bearer token, else None.

    Mirrors the agent token contract issued by /agents/enroll. Reads headers
    synchronously: Quart's ``request.headers`` is not awaitable.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(header[7:], current_app.config["JWT_SECRET_KEY"], algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    if payload.get("type") != "agent_access":
        return None
    sub = payload.get("sub", "")
    if not isinstance(sub, str) or not sub.startswith("agent:"):
        return None
    return sub[len("agent:"):] or None


# ============================================================================
# Database access (sync; always invoked through run_db)
# ============================================================================


def _is_unique_violation(exc: BaseException) -> bool:
    """True when an exception is a unique-constraint violation from either SQLAlchemy or the DAL."""
    if isinstance(exc, IntegrityError):
        return True
    text = str(exc).lower()
    return "unique" in text or "duplicate" in text


def _db_load_agent(agent_id: str) -> Optional[AgentRecord]:
    """Load the server-recorded agent facts, or None when the agent does not exist."""
    db = get_db()
    row = db(db.access_agents.agent_id == agent_id).select().first()
    if row is None:
        return None
    return AgentRecord(
        agent_id=row.agent_id,
        status=row.status or "",
        hostname=row.hostname or "",
        enrollment_key_hash=row.enrollment_key_hash,
    )


def _db_key_join_info(key_hash: str) -> Optional[KeyJoinInfo]:
    """Return purpose, intended hostname, and revocation for an enrollment key, or None if absent."""
    db = get_db()
    row = db(db.enrollment_keys.key_hash == key_hash).select().first()
    if row is None:
        return None
    meta = parse_metadata(row["metadata"])
    purpose = meta.get("purpose")
    intended = meta.get("intended_hostname")
    return KeyJoinInfo(
        key_hash=key_hash,
        purpose=purpose if isinstance(purpose, str) else None,
        intended_hostname=intended if isinstance(intended, str) else None,
        revoked=bool(row["revoked"]),
    )


def _db_record_issuance(agent_id: str, member_name: str, key_hash: str, now: datetime) -> int:
    """Insert the grant row first. Unique live columns reject a second live grant.

    Raises IssuanceConflictError when the agent, member, or key already holds a live grant.
    """
    db = get_db()
    try:
        issuance_id = db.lxd_join_token_issuances.insert(
            agent_id=agent_id,
            member_name=member_name,
            key_hash=key_hash,
            status=ISSUANCE_ISSUED,
            issued_at=now,
            expires_at=now + _TOKEN_VALIDITY,
            agent_live=agent_id,
            member_live=member_name,
            key_live=key_hash,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        if _is_unique_violation(exc):
            raise IssuanceConflictError() from exc
        raise
    return int(issuance_id)


def _db_mark_issuance_failed(issuance_id: int) -> None:
    """Mark a grant FAILED and clear its live columns, releasing the agent, member, and key."""
    db = get_db()
    db(db.lxd_join_token_issuances.id == issuance_id).update(
        status=ISSUANCE_FAILED, agent_live=None, member_live=None, key_live=None,
    )
    db.commit()


def _db_bootstrap_state(token_hash: str) -> tuple[bool, bool]:
    """Return (tombstone_exists, enrollment_key_row_exists) for a bootstrap token hash."""
    db = get_db()
    tombstone = db(db.bootstrap_token_seeds.token_hash == token_hash).select().first() is not None
    key = db(db.enrollment_keys.key_hash == token_hash).select().first() is not None
    return tombstone, key


def _db_write_tombstone(token_hash: str) -> None:
    """Record that this bootstrap token was seeded. Tombstones are never deleted."""
    db = get_db()
    db.bootstrap_token_seeds.insert(token_hash=token_hash, seeded_at=datetime.utcnow())
    db.commit()


def _db_first_active_user_id() -> Optional[int]:
    """Return the lowest-id active user, the owner of server-issued enrollment keys."""
    db = get_db()
    row = db(db.auth_user.is_active == True).select(orderby=db.auth_user.id).first()  # noqa: E712
    return int(row.id) if row else None


def _db_insert_enrollment_key(key_hash: str, owner_id: int, expires_at: datetime, metadata: dict[str, Any]) -> None:
    """Insert a single-use enrollment key whose purpose and binding live in metadata."""
    db = get_db()
    db.enrollment_keys.insert(
        key_hash=key_hash,
        created_by=owner_id,
        expires_at=expires_at,
        is_used=False,
        revoked=False,
        metadata=json.dumps(metadata),
    )
    db.commit()


def _db_seed_bootstrap(token_hash: str, owner_id: int, expires_at: datetime) -> None:
    """Insert the bootstrap key and its tombstone in one commit."""
    db = get_db()
    db.enrollment_keys.insert(
        key_hash=token_hash,
        created_by=owner_id,
        expires_at=expires_at,
        is_used=False,
        revoked=False,
        metadata=json.dumps({"purpose": PURPOSE_BOOTSTRAP}),
    )
    db.bootstrap_token_seeds.insert(token_hash=token_hash, seeded_at=datetime.utcnow())
    db.commit()


# ============================================================================
# Audit
# ============================================================================


def _audit(action: str, message: str, severity: AuditSeverity, details: dict[str, Any]) -> None:
    """Emit an audit event when the audit logger is configured; never includes token values."""
    audit_logger = get_audit_logger()
    if not audit_logger:
        log.warning("audit logger not configured; skipping %s", action)
        return
    audit_logger.log(
        event_type=AuditEventType.SYSTEM_CONFIG_CHANGE,
        message=message,
        severity=severity,
        resource_type="lxd_cluster_member",
        resource_id=details.get("member_name"),
        details={"action": action, **details},
    )


# ============================================================================
# Cluster join tokens
# ============================================================================


def _refuse(status: int, error: str, reason: str, member_name: str, agent_id: str) -> tuple[Any, int]:
    """Audit a refusal and build the client response; the reason is never secret."""
    _audit("lxd.cluster_join_token.refused", f"LXD join token refused: {reason}", AuditSeverity.WARNING,
           {"member_name": member_name, "agent_id": agent_id, "reason": reason})
    return jsonify({"error": error}), status


@lxd_bp.route("/cluster-join-tokens", methods=["POST"])
@rate_limit("10/minute")
async def create_cluster_join_token():
    """Mint a one-time LXD cluster join token for the calling agent's own hostname.

    Auth: ``Authorization: Bearer <agent access token>`` from ``/agents/enroll``.

    Body: ``{"member_name": "<RFC 1123 hostname>"}``

    Returns:
        200: ``{"token": "<lxd join token>"}``
        400: invalid body or hostname
        401: missing, invalid, or expired agent token, or unknown agent
        403: agent inactive; key missing, revoked, or not join-eligible; intended hostname
             or member_name not the agent's enrolled hostname
        409: join right already consumed by a live grant, or LXD member already exists
        502: LXD returned an error or unusable response
        503: LXD unreachable, timed out, or not configured
    """
    agent_id = _authenticate_agent()
    if not agent_id:
        return jsonify({"error": "Authentication required"}), 401

    try:
        join_req = parse_join_token_request(await request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    member_name = join_req.member_name
    agent = await run_db(lambda: _db_load_agent(agent_id))
    if agent is None:
        return jsonify({"error": "Authentication required"}), 401
    if agent.status != "active":
        return _refuse(403, "Agent is not active", "agent_not_active", member_name, agent_id)
    if member_name != agent.hostname:
        return _refuse(403, "member_name must match the enrolled agent hostname",
                       "hostname_mismatch", member_name, agent_id)
    key = None
    if agent.enrollment_key_hash:
        key_hash = agent.enrollment_key_hash
        key = await run_db(lambda: _db_key_join_info(key_hash))
    if key is None or key.purpose not in _JOIN_ELIGIBLE_PURPOSES:
        return _refuse(403, "agent enrollment key is not join-eligible",
                       "key_not_join_eligible", member_name, agent_id)
    if key.revoked:
        return _refuse(403, "agent enrollment key is revoked", "key_revoked", member_name, agent_id)
    if key.purpose == PURPOSE_NODE_JOIN and key.intended_hostname != agent.hostname:
        return _refuse(403, "enrollment key intended hostname does not match the agent",
                       "intended_hostname_mismatch", member_name, agent_id)

    now = datetime.utcnow()
    try:
        issuance_id = await run_db(
            lambda: _db_record_issuance(agent_id, member_name, key.key_hash, now),
        )
    except IssuanceConflictError:
        return _refuse(409, "join token already issued for this agent, member, or key",
                       "issuance_conflict", member_name, agent_id)

    try:
        token = await request_lxd_join_token(member_name)
    except LxdMemberExistsError:
        await run_db(lambda: _db_mark_issuance_failed(issuance_id))
        return _refuse(409, "cluster member already exists", "member_exists", member_name, agent_id)
    except LxdUpstreamError as exc:
        await run_db(lambda: _db_mark_issuance_failed(issuance_id))
        log.warning("LXD join token request failed for %s: HTTP %s", member_name, exc.status)
        _audit("lxd.cluster_join_token.failed", f"LXD join token failed: {exc}", AuditSeverity.ERROR,
               {"member_name": member_name, "agent_id": agent_id, "upstream_status": exc.status})
        return jsonify({"error": str(exc)}), exc.status
    except LxdUnavailableError as exc:
        await run_db(lambda: _db_mark_issuance_failed(issuance_id))
        log.error("LXD unavailable for join token request: %s", exc)
        _audit("lxd.cluster_join_token.failed", f"LXD unavailable: {exc}", AuditSeverity.ERROR,
               {"member_name": member_name, "agent_id": agent_id})
        return jsonify({"error": "LXD cluster unavailable, retry later"}), 503

    _audit("lxd.cluster_join_token.issued", "LXD cluster join token issued", AuditSeverity.INFO,
           {"member_name": member_name, "agent_id": agent_id, "issuance_id": issuance_id,
            "token": mask_token(token)})
    log.info("issued LXD join token for member %s (%s)", member_name, mask_token(token))
    return jsonify(asdict(JoinTokenResponse(token=token))), 200


# ============================================================================
# Admin: join-eligible enrollment keys
# ============================================================================


@lxd_bp.route("/node-join-keys", methods=["POST"])
@auth_required
@roles_required("admin")
async def create_node_join_key():
    """Issue a single-use, join-eligible enrollment key bound to one hostname. Shown once.

    Body: ``{"intended_hostname": "<RFC 1123 hostname>", "ttl_hours": 1..24 (optional, default 24)}``

    Returns:
        201: ``{"enrollment_key": "<secret>", "expires_at": "<iso8601>", "intended_hostname": "..."}``
        400: invalid body, hostname, or ttl
    """
    body = await request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    intended = body.get("intended_hostname")
    if not isinstance(intended, str) or not is_valid_hostname(intended):
        return jsonify({"error": "intended_hostname must be a valid RFC 1123 hostname"}), 400
    ttl_raw = body.get("ttl_hours", _KEY_TTL_DEFAULT_HOURS)
    if not isinstance(ttl_raw, int) or isinstance(ttl_raw, bool) or not 1 <= ttl_raw <= _KEY_TTL_MAX_HOURS:
        return jsonify({"error": f"ttl_hours must be an integer 1-{_KEY_TTL_MAX_HOURS}"}), 400
    owner_id = int((get_current_user() or {})["id"])
    key = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=ttl_raw)
    await run_db(lambda: _db_insert_enrollment_key(
        hashlib.sha256(key.encode()).hexdigest(), owner_id, expires_at,
        {"purpose": PURPOSE_NODE_JOIN, "intended_hostname": intended},
    ))
    _audit("lxd.node_join_key.issued", "node-join enrollment key issued", AuditSeverity.INFO,
           {"owner_id": owner_id, "intended_hostname": intended, "ttl_hours": ttl_raw})
    return jsonify({"enrollment_key": key, "expires_at": expires_at.isoformat(),
                    "intended_hostname": intended}), 201


# ============================================================================
# Bootstrap enrollment key seeding
# ============================================================================


def bootstrap_token_hash(token: str) -> str:
    """Return the SHA-256 hex digest of a bootstrap token (enrollment key hash and tombstone key)."""
    return hashlib.sha256(token.encode()).hexdigest()


def seed_bootstrap_enrollment_key(token: str, ttl_hours: int = _KEY_TTL_DEFAULT_HOURS) -> bool:
    """Seed the bootstrap token as a single-use enrollment key, at most once per token hash.

    Returns True only when a key and tombstone were inserted. Returns False when a tombstone
    exists (even if the key was later deleted or revoked), or when a key row exists without a
    tombstone, in which case the tombstone is written and nothing else changes.
    Raises ValueError for a malformed token or TTL, and RuntimeError when no user can own the key.
    """
    if not _BOOTSTRAP_TOKEN_RE.fullmatch(token):
        raise ValueError("GOUGH_BOOTSTRAP_TOKEN must be 32-256 characters of [A-Za-z0-9_-]")
    if not 1 <= ttl_hours <= _KEY_TTL_MAX_HOURS:
        raise ValueError(f"bootstrap token TTL must be 1-{_KEY_TTL_MAX_HOURS} hours")

    token_hash = bootstrap_token_hash(token)
    tombstone, key_exists = _db_bootstrap_state(token_hash)
    if tombstone:
        return False
    if key_exists:
        _db_write_tombstone(token_hash)
        return False
    owner_id = _db_first_active_user_id()
    if owner_id is None:
        raise RuntimeError("no active user exists to own the bootstrap enrollment key")
    _db_seed_bootstrap(token_hash, owner_id, datetime.utcnow() + timedelta(hours=ttl_hours))
    log.info("seeded bootstrap enrollment key %s", mask_token(token))
    return True


@lxd_bp.before_app_serving
async def seed_bootstrap_token_on_startup() -> None:
    """Seed GOUGH_BOOTSTRAP_TOKEN at startup; fail loud when misconfigured."""
    token = os.environ.get("GOUGH_BOOTSTRAP_TOKEN", "")
    if not token:
        return
    ttl_raw = os.environ.get("GOUGH_BOOTSTRAP_TOKEN_TTL_HOURS", str(_KEY_TTL_DEFAULT_HOURS))
    try:
        ttl = int(ttl_raw)
    except ValueError as exc:
        raise RuntimeError("GOUGH_BOOTSTRAP_TOKEN_TTL_HOURS must be an integer") from exc
    seeded = await run_db(lambda: seed_bootstrap_enrollment_key(token, ttl))
    current_app.logger.info("bootstrap enrollment key %s", "seeded" if seeded else "already seeded (tombstone)")
