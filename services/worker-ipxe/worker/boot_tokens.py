"""Per-boot tokens and shared rate limiting for the unauthenticated boot endpoints.

State lives in Redis/Valkey (REDIS_URL) so every worker-ipxe replica sees the same
tokens and counters; an in-process store would let a token minted by one replica be
rejected (or replayed) on another. Only the SHA-256 of a token is ever stored.

Tokens are keyed by (MAC, client IP): a mint from one IP can never replace or affect
another IP's token, and only FAILED redemptions are rate-limited (per MAC+IP), so a
valid token always redeems no matter what other clients do.
"""

import hashlib
import hmac
import json
import secrets
import time
from typing import Protocol

import structlog
from prometheus_client import Counter

logger = structlog.get_logger()

TOKEN_TTL_SECONDS = 30 * 60
DISCOVERY_TOKEN_TTL_SECONDS = 10 * 60
MAX_LIVE_TOKENS_PER_MAC = 4
_KEY_PREFIX = "gough:boot-token:"  # + "{mac}:{ip}"
_LIVE_PREFIX = "gough:boot-token-live:"  # sorted set per MAC: ip -> expiry
_RATE_PREFIX = "gough:boot-rate:"

TOKEN_CAP_REFUSED = Counter(
    "worker_ipxe_boot_token_cap_refused_total", "Boot token mints refused: per-MAC live-token cap reached"
)


class StoreUnavailableError(RuntimeError):
    """The shared state store could not be reached; callers must fail closed."""


class TokenCapError(RuntimeError):
    """A MAC already has the maximum number of live tokens (across IPs); the mint is refused."""


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

    async def zadd(self, name: str, mapping: dict[str, float]) -> object:
        """Add members to a sorted set."""

    async def zrem(self, name: str, *members: str) -> object:
        """Remove members from a sorted set."""

    async def zcard(self, name: str) -> int:
        """Sorted-set size."""

    async def zremrangebyscore(self, name: str, low: float, high: float) -> object:
        """Remove sorted-set members by score range."""


def _digest(token: str) -> str:
    """SHA-256 hex digest of a token (the only form that is stored)."""
    return hashlib.sha256(token.encode()).hexdigest()


class BootTokenStore:
    """Issues, redeems and rate-limits boot tokens over a shared Redis client."""

    def __init__(
        self,
        client: RedisLike,
        ttl_seconds: int = TOKEN_TTL_SECONDS,
        max_live_per_mac: int = MAX_LIVE_TOKENS_PER_MAC,
    ) -> None:
        """Wrap a Redis client; ttl_seconds is the default token lifetime."""
        self._client = client
        self._ttl = ttl_seconds
        self._cap = max_live_per_mac

    async def issue(
        self,
        machine: str,
        source_ip: str,
        ttl_seconds: int | None = None,
        stages: frozenset[str] | None = None,
    ) -> str:
        """Mint the live token for (`machine`, `source_ip`); only its SHA-256 is stored.

        Minting again from the same IP overwrites that IP's token only. A different IP
        gets its own token, up to `max_live_per_mac` live tokens per MAC; beyond that the
        mint is refused (TokenCapError) rather than evicting anyone else's token.
        """
        ttl = ttl_seconds or self._ttl
        now = int(time.time())
        key = f"{_KEY_PREFIX}{machine}:{source_ip}"
        live_key = _LIVE_PREFIX + machine
        try:
            await self._client.zremrangebyscore(live_key, 0, now)
            if not await self._client.get(key) and await self._client.zcard(live_key) >= self._cap:
                TOKEN_CAP_REFUSED.inc()
                logger.warning("boot_token_cap_reached", cap=self._cap)
                raise TokenCapError("too many live boot tokens for this machine")
            token = secrets.token_urlsafe(32)
            record = json.dumps(
                {
                    "machine": machine,
                    "hash": _digest(token),
                    "ip": source_ip,
                    "exp": now + ttl,
                    "stages": sorted(stages) if stages is not None else None,
                }
            )
            await self._client.set(key, record, ex=ttl)
            await self._client.zadd(live_key, {source_ip: now + ttl})
            await self._client.expire(live_key, self._ttl)
        except TokenCapError:
            raise
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc
        return token

    async def redeem(self, token: str, machine: str, source_ip: str, stage: str) -> bool:
        """Consume the (machine, ip) token; True only if hash, expiry and stage all match.

        A failed attempt does not burn the live token. Single use is atomic: DEL reports
        how many keys it removed, so of two concurrent valid redemptions exactly one wins.
        """
        key = f"{_KEY_PREFIX}{machine}:{source_ip}"
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
            removed = bool(await self._client.delete(key))
            if removed:
                await self._client.zrem(_LIVE_PREFIX + machine, source_ip)
            return removed
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

    async def is_limited(self, source: str, limit: int) -> bool:
        """True if `source` has already used up `limit` charges in its window (read-only)."""
        try:
            raw = await self._client.get(_RATE_PREFIX + source)
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc
        return bool(raw) and int(str(raw)) >= limit

    async def record_failure(self, source: str, window_seconds: int) -> None:
        """Charge one failure to `source`'s window (starts the window on the first one)."""
        await self.allow(source, 0, window_seconds)


def create_token_store(redis_url: str) -> BootTokenStore:
    """Build a store on a lazily-connecting redis.asyncio client for `redis_url`."""
    import redis.asyncio as aioredis

    return BootTokenStore(aioredis.from_url(redis_url, decode_responses=True))
