"""WebSocket endpoint for interactive shell sessions (hub side).

SECURITY: the hub NEVER executes a local shell. This module contains no
process-spawning code of any kind (enforced by a test). Shell I/O is proxied to
the target hypervisor's access-agent reverse-SSH server through a
:class:`ShellTransport`.

Every upgrade is authenticated *before* ``accept``: a single-use 60s ticket
(minted by ``POST /api/v1/shell/sessions/<id>/ticket``) bound to user, tenant
and session; then the session owner (or admin) and ``check_shell_access`` are
re-verified and the session is atomically claimed (one connection per session).

FOLLOW-UP: no transport is wired yet. After authn/authz the endpoint closes
with 4501 "shell proxy not yet available". To implement: mint a short-lived
user cert via ``app.ssh_ca`` (principal = target host + user), dial the
agent's server in ``services/access-agent/agent/rssh_server.py`` and pass the
resulting channel to :func:`run_session` as a ``ShellTransport``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from quart import Quart, websocket

from .audit import get_audit_logger
from .db.run_db import run_db
from .licensing import feature_enabled
from .models import get_db
from .shell_tickets import get_ticket_store

log = logging.getLogger(__name__)

FLAG_DISABLE_SHELL = "gough.disable-shell"
MAX_MESSAGE_BYTES = 64 * 1024
MAX_DIM = 1000
VALID_MESSAGE_TYPES = frozenset({"input", "resize"})

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_PRE_AUTH_PLACEHOLDER = "unauthenticated"
PRE_AUTH_AUDIT_LIMIT = 10  # audited pre-auth denials per source IP per window
PRE_AUTH_AUDIT_WINDOW = 60.0
_pre_auth_hits: dict[str, tuple[float, int]] = {}
_pre_auth_lock = threading.Lock()

CLOSE_BAD_REQUEST = 4400
CLOSE_POLICY = 1008
CLOSE_ENDED = 4410
CLOSE_CONFLICT = 4409
CLOSE_BAD_MESSAGE = 1003
CLOSE_TOO_BIG = 1009
CLOSE_UNAVAILABLE = 4501
CLOSE_DISABLED = 4503


@dataclass(slots=True, frozen=True)
class ClientMessage:
    """A validated client frame: ``input`` text or ``resize`` dimensions."""

    type: str
    data: str = ""
    rows: int = 24
    cols: int = 80


class MalformedMessage(ValueError):
    """Raised when a client frame is oversized, non-JSON or invalid."""


class ShellTransport(Protocol):
    """Remote shell channel (reverse-SSH to the target agent)."""

    async def write(self, data: str) -> None:
        """Send terminal input to the remote shell."""

    async def resize(self, rows: int, cols: int) -> None:
        """Resize the remote PTY."""

    async def close(self) -> None:
        """Close the channel."""


def parse_client_message(raw: str | bytes) -> ClientMessage:
    """Validate one inbound frame (size cap, JSON object, known type, bounds)."""
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MalformedMessage("message too large")
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MalformedMessage("invalid JSON") from exc
    if not isinstance(obj, dict):
        raise MalformedMessage("message must be an object")
    mtype = obj.get("type")
    if mtype not in VALID_MESSAGE_TYPES:
        raise MalformedMessage("unknown message type")
    if mtype == "input":
        text = obj.get("input", "")
        if not isinstance(text, str):
            raise MalformedMessage("input must be a string")
        return ClientMessage(type="input", data=text)
    rows, cols = obj.get("rows", 24), obj.get("cols", 80)
    for dim in (rows, cols):
        if isinstance(dim, bool) or not isinstance(dim, int) or not 1 <= dim <= MAX_DIM:
            raise MalformedMessage("invalid terminal size")
    return ClientMessage(type="resize", rows=rows, cols=cols)


def _pre_auth_audit_allowed(source: str) -> bool:
    """Per-source-IP budget so unauthenticated denies cannot flood the audit log."""
    now = time.monotonic()
    with _pre_auth_lock:
        if len(_pre_auth_hits) > 4096:
            for k in [k for k, (t, _) in _pre_auth_hits.items() if now - t > PRE_AUTH_AUDIT_WINDOW]:
                del _pre_auth_hits[k]
        start, count = _pre_auth_hits.get(source, (now, 0))
        if now - start > PRE_AUTH_AUDIT_WINDOW:
            start, count = now, 0
        _pre_auth_hits[source] = (start, count + 1)
        return count < PRE_AUTH_AUDIT_LIMIT


def _audit_pre_auth_deny(source: str, reason: str, session_id: str = "") -> None:
    """Audit an unauthenticated deny using only fixed/derived values (never raw input)."""
    if not _pre_auth_audit_allowed(source):
        return
    digest = hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:16]
    _audit_attach(_PRE_AUTH_PLACEHOLDER, None, "deny", reason, {"sid_sha256": digest})


def _audit_attach(
    session_id: str,
    user_id: int | None,
    outcome: str,
    reason: str,
    details: dict[str, str] | None = None,
) -> None:
    """Best-effort audit of an attach/deny decision (never breaks the socket)."""
    try:
        audit = get_audit_logger()
        if audit:
            audit.log_shell_attach(session_id, user_id, outcome, reason, details)
    except Exception:  # noqa: BLE001
        log.exception("shell attach audit failed")
    log.info("shell attach %s session=%s user=%s reason=%s", outcome, session_id, user_id, reason)


async def run_session(transport: ShellTransport) -> None:
    """Pump validated client frames into ``transport`` until the client leaves."""
    try:
        async for raw in websocket:
            try:
                msg = parse_client_message(raw)
            except MalformedMessage as exc:
                code = CLOSE_TOO_BIG if "large" in str(exc) else CLOSE_BAD_MESSAGE
                await websocket.close(code, str(exc))
                return
            if msg.type == "input":
                await transport.write(msg.data)
            else:
                await transport.resize(msg.rows, msg.cols)
    finally:
        await transport.close()


async def open_transport(session: Any) -> ShellTransport | None:
    """Open the agent reverse-SSH transport for ``session`` (not yet implemented)."""
    return None


def _authorize(session_id: str, user_id: int) -> tuple[Any | None, str]:
    """Blocking authz: session exists, owner-or-admin, resource shell access."""
    from .api.shell import check_shell_access, user_is_admin

    db = get_db()
    session = db(db.shell_sessions.session_id == session_id).select().first()
    if not session:
        return None, "unknown session"
    if session.user_id != user_id and not user_is_admin(user_id):
        return None, "not session owner"
    ok, err = check_shell_access(user_id, session.resource_type, session.resource_id)
    if not ok:
        return None, err or "shell access denied"
    return session, ""


def init_websocket(app: Quart) -> None:
    """Register the authenticated ``/ws/shell`` route on ``app``."""

    @app.websocket("/ws/shell")
    async def shell_websocket() -> None:
        """Authenticate, authorize and claim a session, then proxy its shell."""
        raw_sid = websocket.args.get("session_id") or ""
        ticket = websocket.args.get("ticket") or ""
        source = str(websocket.remote_addr or "unknown")

        # Unauthenticated input: validate shape before it touches anything.
        if not _UUID_RE.match(raw_sid):
            _audit_pre_auth_deny(source, "malformed session_id")
            await websocket.close(CLOSE_BAD_REQUEST, "bad request")
            return
        session_id = raw_sid

        if await feature_enabled(FLAG_DISABLE_SHELL):
            _audit_pre_auth_deny(source, "kill switch", session_id)
            await websocket.close(CLOSE_DISABLED, "shell disabled")
            return

        claims = await get_ticket_store().consume(ticket) if ticket else None
        if claims is None or not claims.tenant or claims.session_id != session_id:
            _audit_pre_auth_deny(source, "bad ticket", session_id)
            await websocket.close(CLOSE_POLICY, "authentication required")
            return

        session, reason = await run_db(lambda: _authorize(session_id, claims.user_id))
        if session is None:
            _audit_attach(session_id, claims.user_id, "deny", reason)
            await websocket.close(CLOSE_POLICY, "access denied")
            return
        if session.ended_at:
            _audit_attach(session_id, claims.user_id, "deny", "session ended")
            await websocket.close(CLOSE_ENDED, "session ended")
            return

        store = get_ticket_store()
        if not await store.try_attach(session_id):
            _audit_attach(session_id, claims.user_id, "deny", "already attached")
            await websocket.close(CLOSE_CONFLICT, "session already attached")
            return

        try:
            await websocket.accept()
            transport = await open_transport(session)
            if transport is None:
                _audit_attach(session_id, claims.user_id, "deny", "proxy unavailable")
                await websocket.send(
                    json.dumps({"type": "error", "message": "shell proxy not yet available"})
                )
                await websocket.close(CLOSE_UNAVAILABLE, "shell proxy not yet available")
                return
            _audit_attach(session_id, claims.user_id, "attach", "ok")
            await run_session(transport)
        finally:
            await store.release(session_id)

    log.info("Authenticated shell WebSocket registered")
