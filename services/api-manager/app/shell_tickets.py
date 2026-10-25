"""Single-use WebSocket shell tickets and the one-attach-per-session guard.

Browsers cannot set an ``Authorization`` header on a WebSocket, so an
authenticated REST call mints a short-lived opaque ticket that the WS upgrade
presents as ``?ticket=``. Only the SHA-256 of the ticket is stored; consuming
it is atomic and one-shot, so a replayed ticket is always refused.

Backed exclusively by Redis/Valkey (``REDIS_URL``). There is deliberately NO
in-process fallback: it would silently break across replicas and hide an
outage. With Redis unavailable every operation raises
:class:`TicketStoreUnavailable` (surfaced as HTTP 503) and the
``gough_shell_ticket_store_up`` gauge is 0.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .metrics import shell_ticket_store_up

log = logging.getLogger(__name__)


class TicketStoreUnavailable(RuntimeError):
    """Raised when the Redis/Valkey ticket backend is missing or unreachable."""


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
        """Create the store; connect to Redis if ``redis_url`` is usable.

        Never falls back to memory: on failure the store stays unavailable,
        logs at ERROR and sets the availability gauge to 0.
        """
        self._redis: Any = None
        self._connect_error: str = "REDIS_URL not configured"
        if redis_url:
            try:
                import redis

                client = redis.from_url(redis_url)
                client.ping()
                self._redis = client
            except Exception as exc:  # noqa: BLE001 -- recorded, surfaced as 503
                self._connect_error = f"{type(exc).__name__}: {exc}"
        if self._redis is None:
            log.error(
                "Shell ticket store unavailable (%s); failing closed",
                self._connect_error,
            )
            shell_ticket_store_up.set(0)
        else:
            shell_ticket_store_up.set(1)

    def _require(self) -> Any:
        """Return the Redis client or raise :class:`TicketStoreUnavailable`."""
        if self._redis is None:
            shell_ticket_store_up.set(0)
            raise TicketStoreUnavailable(self._connect_error)
        return self._redis

    def _guard(self, fn: Callable[[Any], Any]) -> Any:
        """Run ``fn(redis)``, mapping backend errors to TicketStoreUnavailable."""
        client = self._require()
        try:
            return fn(client)
        except Exception as exc:  # noqa: BLE001
            log.error("Shell ticket store Redis error: %s", exc)
            shell_ticket_store_up.set(0)
            raise TicketStoreUnavailable(str(exc)) from exc

    def _mint_sync(self, claims: TicketClaims) -> str:
        ticket = secrets.token_urlsafe(32)
        key = hash_ticket(ticket)
        payload = json.dumps(
            {"u": claims.user_id, "s": claims.session_id, "t": claims.tenant}
        )
        self._guard(
            lambda r: r.set(_TICKET_PREFIX + key, payload, ex=TICKET_TTL_SECONDS)
        )
        return ticket

    def _consume_sync(self, ticket: str) -> TicketClaims | None:
        key = hash_ticket(ticket)
        raw = self._guard(lambda r: r.getdel(_TICKET_PREFIX + key))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return TicketClaims(int(data["u"]), str(data["s"]), data.get("t"))
        except (ValueError, KeyError, TypeError):
            return None

    def _attach_sync(self, session_id: str) -> bool:
        return bool(
            self._guard(
                lambda r: r.set(
                    _ATTACH_PREFIX + session_id, "1", nx=True, ex=ATTACH_TTL_SECONDS
                )
            )
        )

    def _release_sync(self, session_id: str) -> None:
        self._guard(lambda r: r.delete(_ATTACH_PREFIX + session_id))

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
