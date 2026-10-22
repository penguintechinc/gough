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
DISCOVERY_TOKEN_TTL_SECONDS = 10 * 60
_KEY_PREFIX = "gough:boot-token:"  # keyed by MAC: one live token per machine
_RATE_PREFIX = "gough:boot-rate:"


class StoreUnavailableError(RuntimeError):
    """The shared state store could not be reached; callers must fail closed."""


class RedisLike(Protocol):
    """Subset of redis.asyncio.Redis used here (lets tests inject a fake)."""

    async def set(self, name: str, value: str, ex: int | None = ...) -> object:
        """Store a string value with optional TTL."""

    async def get(self, name: str) -> object:
        """Fetch a key."""

    async def delete(self, name: str) -> int:
        """Delete a key, returning how many were removed."""

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

    async def issue(
        self,
        machine: str,
        source_ip: str,
        ttl_seconds: int | None = None,
        stages: frozenset[str] | None = None,
    ) -> str:
        """Mint the one live token for `machine` (normalised MAC), bound to `source_ip`.

        The record is keyed by MAC, so minting overwrites any previous token for that
        machine (one live token per MAC). Only the token's SHA-256 is stored, with the
        bound IP, expiry and allowed stages (None = every stage).
        """
        ttl = ttl_seconds or self._ttl
        token = secrets.token_urlsafe(32)
        record = json.dumps(
            {
                "machine": machine,
                "hash": _digest(token),
                "ip": source_ip,
                "exp": int(time.time()) + ttl,
                "stages": sorted(stages) if stages is not None else None,
            }
        )
        try:
            await self._client.set(_KEY_PREFIX + machine, record, ex=ttl)
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc
        return token

    async def redeem(self, token: str, machine: str, source_ip: str, stage: str) -> bool:
        """Consume the token for `machine`; True only if hash, IP, expiry and stage all match.

        A failed attempt does not burn the live token (otherwise anyone could lock a
        machine out by guessing). Single use is atomic: DEL reports how many keys it
        removed, so of two concurrent valid redemptions exactly one sees 1.
        """
        key = _KEY_PREFIX + machine
        try:
            raw = await self._client.get(key)
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc
        if not raw:
            return False
        try:
            record = json.loads(raw)
            stored_hash = str(record["hash"])
            stored_ip = str(record["ip"])
            stored_machine = str(record["machine"])
            expires = int(record["exp"])
            stages = record["stages"]
            if stages is not None and not isinstance(stages, list):
                raise TypeError("stages")
        except (ValueError, KeyError, TypeError):
            logger.error("boot_token_record_corrupt")
            return False
        valid = (
            hmac.compare_digest(stored_hash.encode(), _digest(token).encode())
            & hmac.compare_digest(stored_ip.encode(), source_ip.encode())
            & hmac.compare_digest(stored_machine.encode(), machine.encode())
            & (expires > time.time())
            & (stages is None or stage in stages)
        )
        if not valid:
            return False
        try:
            return bool(await self._client.delete(key))
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc

    async def allow(self, source: str, limit: int, window_seconds: int) -> bool:
        """Fixed-window rate limit on `source` (any namespaced key) shared across replicas; True if under `limit`."""
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
