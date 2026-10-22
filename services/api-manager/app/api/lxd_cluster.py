"""LXD Cluster Join Token and Bootstrap Enrollment API.

Serves the two hub contracts the bare-metal installer needs:

- ``POST /api/v1/lxd/cluster-join-tokens`` mints a one-time LXD cluster join
  token for a new member by calling the LXD cluster primary.
- Seeds the one-time bootstrap enrollment key from ``GOUGH_BOOTSTRAP_TOKEN``
  into ``enrollment_keys`` so the existing ``/agents/enroll`` path consumes it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
import jwt
from quart import Blueprint, current_app, jsonify, request

from ..audit import AuditEventType, AuditSeverity, get_audit_logger
from ..models import get_db
from ..rate_limit import rate_limit

log = logging.getLogger(__name__)

lxd_bp = Blueprint("lxd", __name__, url_prefix="/api/v1/lxd")

# LXD operations are always addressed as /1.0/operations/<uuid>; anything else is refused.
_LXD_OPERATION_PREFIX = "/1.0/operations/"
_LXD_OPERATION_WAIT_SECONDS = 30
_LXD_TIMEOUT = httpx.Timeout(connect=5.0, read=35.0, write=10.0, pool=5.0)

_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^{_HOST_LABEL}(?:\.{_HOST_LABEL})*$")
_HOSTNAME_MAX_LEN = 63

_BOOTSTRAP_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{32,256}$")
_BOOTSTRAP_TTL_DEFAULT_HOURS = 24
_BOOTSTRAP_TTL_MAX_HOURS = 24


@dataclass(slots=True, frozen=True)
class JoinTokenRequest:
    """Validated body of a cluster-join-token request."""

    member_name: str


@dataclass(slots=True, frozen=True)
class JoinTokenResponse:
    """Response body carrying the LXD cluster join token."""

    token: str


class LxdUpstreamError(Exception):
    """LXD refused or failed the request; carries the HTTP status to surface."""

    def __init__(self, status: int, message: str) -> None:
        """Store the status to map back to the client and a non-secret message."""
        super().__init__(message)
        self.status = status


class LxdUnavailableError(Exception):
    """LXD is unreachable, timed out, or not configured; surfaces as 503."""


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


async def request_lxd_join_token(member_name: str) -> str:
    """Call LXD ``POST /1.0/cluster/members`` and return the join token from the completed operation."""
    api_url, cert, verify = _lxd_settings()
    try:
        async with httpx.AsyncClient(
            base_url=api_url, cert=cert, verify=verify, timeout=_LXD_TIMEOUT,
        ) as client:
            resp = await client.post("/1.0/cluster/members", json={"server_name": member_name})
            if resp.status_code == 409:
                raise LxdUpstreamError(409, "cluster member already exists")
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


def _agent_status_sync(agent_id: str) -> Optional[str]:
    """Return the access_agents.status for an agent, or None when no such agent exists."""
    db = get_db()
    row = db(db.access_agents.agent_id == agent_id).select().first()
    return row.status if row else None


async def _agent_status(agent_id: str) -> Optional[str]:
    """Run the blocking DB lookup in a worker thread so the event loop stays free."""
    return await asyncio.to_thread(_agent_status_sync, agent_id)


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


@lxd_bp.route("/cluster-join-tokens", methods=["POST"])
@rate_limit("10/minute")
async def create_cluster_join_token():
    """Mint a one-time LXD cluster join token for a new member.

    Auth: ``Authorization: Bearer <agent access token>`` issued by ``/agents/enroll``.

    Body: ``{"member_name": "<RFC 1123 hostname>"}``

    Returns:
        200: ``{"token": "<lxd join token>"}``
        400: invalid body or hostname
        401: missing, invalid, or expired agent token
        403: agent not active
        409: LXD member already exists
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

    status = await _agent_status(agent_id)
    if status is None:
        return jsonify({"error": "Authentication required"}), 401
    if status != "active":
        _audit("lxd.cluster_join_token.denied", "LXD join token denied: agent not active",
               AuditSeverity.WARNING, {"member_name": join_req.member_name, "agent_id": agent_id})
        return jsonify({"error": "Agent is not active"}), 403

    try:
        token = await request_lxd_join_token(join_req.member_name)
    except LxdUpstreamError as exc:
        log.warning("LXD join token request failed for %s: HTTP %s", join_req.member_name, exc.status)
        _audit("lxd.cluster_join_token.failed", f"LXD join token failed: {exc}",
               AuditSeverity.ERROR, {"member_name": join_req.member_name, "agent_id": agent_id,
                                     "upstream_status": exc.status})
        return jsonify({"error": str(exc)}), exc.status
    except LxdUnavailableError as exc:
        log.error("LXD unavailable for join token request: %s", exc)
        _audit("lxd.cluster_join_token.failed", f"LXD unavailable: {exc}",
               AuditSeverity.ERROR, {"member_name": join_req.member_name, "agent_id": agent_id})
        return jsonify({"error": "LXD cluster unavailable, retry later"}), 503

    _audit("lxd.cluster_join_token.issued", "LXD cluster join token issued",
           AuditSeverity.INFO, {"member_name": join_req.member_name, "agent_id": agent_id,
                                "token": mask_token(token)})
    log.info("issued LXD join token for member %s (%s)", join_req.member_name, mask_token(token))
    return jsonify(asdict(JoinTokenResponse(token=token))), 200


# ============================================================================
# Bootstrap enrollment key seeding
# ============================================================================


def bootstrap_token_hash(token: str) -> str:
    """Return the SHA-256 hex digest stored in enrollment_keys.key_hash (matches /agents/enroll)."""
    return hashlib.sha256(token.encode()).hexdigest()


def seed_bootstrap_enrollment_key(token: str, ttl_hours: int = _BOOTSTRAP_TTL_DEFAULT_HOURS) -> bool:
    """Insert the bootstrap token as a single-use enrollment key if it is not already present.

    Returns True when a row was inserted, False when the token was already seeded (used or not).
    Raises ValueError for a malformed token or TTL, and RuntimeError when no user can own the key.
    """
    if not _BOOTSTRAP_TOKEN_RE.fullmatch(token):
        raise ValueError("GOUGH_BOOTSTRAP_TOKEN must be 32-256 characters of [A-Za-z0-9_-]")
    if not 1 <= ttl_hours <= _BOOTSTRAP_TTL_MAX_HOURS:
        raise ValueError(f"bootstrap token TTL must be 1-{_BOOTSTRAP_TTL_MAX_HOURS} hours")

    db = get_db()
    key_hash = bootstrap_token_hash(token)
    if db(db.enrollment_keys.key_hash == key_hash).select().first():
        return False
    owner = db(db.auth_user.is_active == True).select(orderby=db.auth_user.id).first()  # noqa: E712
    if owner is None:
        raise RuntimeError("no active user exists to own the bootstrap enrollment key")
    db.enrollment_keys.insert(
        key_hash=key_hash,
        created_by=owner.id,
        expires_at=datetime.utcnow() + timedelta(hours=ttl_hours),
        is_used=False,
    )
    db.commit()
    log.info("seeded bootstrap enrollment key %s", mask_token(token))
    return True


@lxd_bp.before_app_serving
async def seed_bootstrap_token_on_startup() -> None:
    """Seed GOUGH_BOOTSTRAP_TOKEN into enrollment_keys at startup; fail loud when misconfigured."""
    token = os.environ.get("GOUGH_BOOTSTRAP_TOKEN", "")
    if not token:
        return
    ttl_raw = os.environ.get("GOUGH_BOOTSTRAP_TOKEN_TTL_HOURS", str(_BOOTSTRAP_TTL_DEFAULT_HOURS))
    try:
        ttl = int(ttl_raw)
    except ValueError as exc:
        raise RuntimeError("GOUGH_BOOTSTRAP_TOKEN_TTL_HOURS must be an integer") from exc
    seeded = seed_bootstrap_enrollment_key(token, ttl)
    current_app.logger.info("bootstrap enrollment key %s", "seeded" if seeded else "already present")
