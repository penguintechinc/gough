"""Per-boot tokens and shared rate limiting for the unauthenticated boot endpoints.

State lives in Redis/Valkey (REDIS_URL) so every worker-ipxe replica sees the same
tokens and counters; an in-process store would let a token minted by one replica be
rejected (or replayed) on another. Only the SHA-256 of a token is ever stored.
"""

import hashlib
import hmac
import json
import secrets
import time
from typing import Protocol

import structlog

logger = structlog.get_logger()

TOKEN_TTL_SECONDS = 30 * 60
_KEY_PREFIX = "gough:boot-token:"
_RATE_PREFIX = "gough:boot-event-rate:"


class StoreUnavailableError(RuntimeError):
    """The shared state store could not be reached; callers must fail closed."""


class RedisLike(Protocol):
    """Subset of redis.asyncio.Redis used here (lets tests inject a fake)."""

    async def set(self, name: str, value: str, ex: int | None = ...) -> object:
        """Store a string value with optional TTL."""

    async def getdel(self, name: str) -> object:
        """Atomically fetch and delete a key."""

    async def incr(self, name: str) -> int:
        """Increment a counter."""

    async def expire(self, name: str, time: int) -> object:
        """Set a key TTL."""


def _digest(token: str) -> str:
    """SHA-256 hex digest of a token (the only form that is stored)."""
    return hashlib.sha256(token.encode()).hexdigest()


class BootTokenStore:
    """Issues, redeems and rate-limits boot tokens over a shared Redis client."""

    def __init__(self, client: RedisLike, ttl_seconds: int = TOKEN_TTL_SECONDS) -> None:
        """Wrap a Redis client; ttl_seconds bounds how long a minted token stays valid."""
        self._client = client
        self._ttl = ttl_seconds

    async def issue(self, machine: str) -> str:
        """Mint a token bound to `machine` (normalised MAC); only its hash is stored."""
        token = secrets.token_urlsafe(32)
        digest = _digest(token)
        record = json.dumps({"machine": machine, "hash": digest, "exp": int(time.time()) + self._ttl})
        try:
            await self._client.set(_KEY_PREFIX + digest, record, ex=self._ttl)
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc
        return token

    async def redeem(self, token: str, machine: str) -> bool:
        """Consume `token` for `machine`; True only if valid, unexpired and bound to it.

        GETDEL makes redemption single-use even across replicas: a token is burned by
        the first attempt, including one presented for the wrong machine.
        """
        digest = _digest(token)
        try:
            raw = await self._client.getdel(_KEY_PREFIX + digest)
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc
        if not raw:
            return False
        try:
            record = json.loads(raw)
            stored_hash = str(record["hash"])
            stored_machine = str(record["machine"])
            expires = int(record["exp"])
        except (ValueError, KeyError, TypeError):
            logger.error("boot_token_record_corrupt")
            return False
        hash_ok = hmac.compare_digest(stored_hash.encode(), digest.encode())
        machine_ok = hmac.compare_digest(stored_machine.encode(), machine.encode())
        return hash_ok and machine_ok and expires > time.time()

    async def allow(self, source: str, limit: int, window_seconds: int) -> bool:
        """Fixed-window per-source rate limit shared across replicas; True if under `limit`."""
        key = _RATE_PREFIX + source
        try:
            count = await self._client.incr(key)
            if count == 1:
                await self._client.expire(key, window_seconds)
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc
        return count <= limit


def create_token_store(redis_url: str) -> BootTokenStore:
    """Build a store on a lazily-connecting redis.asyncio client for `redis_url`."""
    import redis.asyncio as aioredis

    return BootTokenStore(aioredis.from_url(redis_url, decode_responses=True))
