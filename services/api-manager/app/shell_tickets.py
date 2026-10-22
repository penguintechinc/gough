"""Single-use WebSocket shell tickets and the one-attach-per-session guard.

Browsers cannot set an ``Authorization`` header on a WebSocket, so an
authenticated REST call mints a short-lived opaque ticket that the WS upgrade
presents as ``?ticket=``. Only the SHA-256 of the ticket is stored; consuming
it is atomic and one-shot, so a replayed ticket is always refused.

Backed by Redis/Valkey (``REDIS_URL``) when reachable, else an in-process store
(correct for this single-process service; not shared across replicas).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

TICKET_TTL_SECONDS = 60
ATTACH_TTL_SECONDS = 12 * 3600
_TICKET_PREFIX = "gough:shell:ticket:"
_ATTACH_PREFIX = "gough:shell:attach:"


@dataclass(slots=True, frozen=True)
class TicketClaims:
    """Identity a ticket is bound to: authenticated user, tenant and session."""

    user_id: int
    session_id: str
    tenant: str | None


def hash_ticket(ticket: str) -> str:
    """Return the hex SHA-256 of a ticket (the only form ever stored)."""
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


class ShellTicketStore:
    """Mint/consume tickets and arbitrate single attachment per session."""

    def __init__(self, redis_url: str | None = None) -> None:
        """Create the store; connect to Redis if ``redis_url`` is usable."""
        self._lock = threading.Lock()
        self._mem: dict[str, tuple[float, str]] = {}
        self._attached: dict[str, float] = {}
        self._redis = None
        if redis_url:
            try:
                import redis

                client = redis.from_url(redis_url)
                client.ping()
                self._redis = client
            except Exception as exc:  # noqa: BLE001 -- fall back, never crash
                log.warning("Shell ticket store: Redis unavailable (%s); in-memory", exc)

    def _mint_sync(self, claims: TicketClaims) -> str:
        ticket = secrets.token_urlsafe(32)
        key = hash_ticket(ticket)
        payload = json.dumps(
            {"u": claims.user_id, "s": claims.session_id, "t": claims.tenant}
        )
        if self._redis is not None:
            self._redis.set(_TICKET_PREFIX + key, payload, ex=TICKET_TTL_SECONDS)
        else:
            with self._lock:
                now = time.monotonic()
                self._mem = {k: v for k, v in self._mem.items() if v[0] > now}
                self._mem[key] = (now + TICKET_TTL_SECONDS, payload)
        return ticket

    def _consume_sync(self, ticket: str) -> TicketClaims | None:
        key = hash_ticket(ticket)
        raw: str | bytes | None
        if self._redis is not None:
            raw = self._redis.getdel(_TICKET_PREFIX + key)
        else:
            with self._lock:
                entry = self._mem.pop(key, None)
            raw = entry[1] if entry and entry[0] > time.monotonic() else None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return TicketClaims(int(data["u"]), str(data["s"]), data.get("t"))
        except (ValueError, KeyError, TypeError):
            return None

    def _attach_sync(self, session_id: str) -> bool:
        if self._redis is not None:
            return bool(
                self._redis.set(
                    _ATTACH_PREFIX + session_id, "1", nx=True, ex=ATTACH_TTL_SECONDS
                )
            )
        with self._lock:
            now = time.monotonic()
            if self._attached.get(session_id, 0) > now:
                return False
            self._attached[session_id] = now + ATTACH_TTL_SECONDS
            return True

    def _release_sync(self, session_id: str) -> None:
        if self._redis is not None:
            self._redis.delete(_ATTACH_PREFIX + session_id)
        else:
            with self._lock:
                self._attached.pop(session_id, None)

    async def mint(self, claims: TicketClaims) -> str:
        """Mint a ticket bound to ``claims``; valid once, for 60 seconds."""
        return await asyncio.to_thread(self._mint_sync, claims)

    async def consume(self, ticket: str) -> TicketClaims | None:
        """Atomically redeem a ticket; ``None`` if unknown, expired or replayed."""
        return await asyncio.to_thread(self._consume_sync, ticket)

    async def try_attach(self, session_id: str) -> bool:
        """Atomically claim the session's single attachment; False if taken."""
        return await asyncio.to_thread(self._attach_sync, session_id)

    async def release(self, session_id: str) -> None:
        """Release the session's attachment claim."""
        await asyncio.to_thread(self._release_sync, session_id)


_store: ShellTicketStore | None = None


def get_ticket_store() -> ShellTicketStore:
    """Return the process-wide ticket store, creating it on first use."""
    global _store
    if _store is None:
        _store = ShellTicketStore(os.getenv("REDIS_URL"))
    return _store


def set_ticket_store(store: ShellTicketStore | None) -> None:
    """Replace the process-wide store (tests)."""
    global _store
    _store = store
