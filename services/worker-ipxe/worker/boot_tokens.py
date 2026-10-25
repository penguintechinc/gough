"""Per-boot tokens and shared rate limiting for the unauthenticated boot endpoints.

State lives in Redis/Valkey (REDIS_URL) so every worker-ipxe replica sees the same
tokens and counters; an in-process store would let a token minted by one replica be
rejected (or replayed) on another. Only the SHA-256 of a token is ever stored.

Tokens are keyed by (MAC, client IP): a mint from one IP can never replace or affect
another IP's token, and only FAILED redemptions are rate-limited (per MAC+IP), so a
valid token always redeems no matter what other clients do.

Residual risk (accepted): an attacker on the same L2 segment who can spoof a victim's
source IP can still mint for that IP/MAC pair or burn its failure budget. Source IPs
are not authenticated; the real fix is for api-manager to mint the token inside the
boot script it returns (the worker only verifies it). ProxyDHCP observations are
spoofable too, so they are a logged/metric signal only and grant no privilege.
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
MAX_MACS_PER_IP = 3
_KEY_PREFIX = "gough:boot-token:"  # + "{mac}:{ip}"
_LIVE_PREFIX = "gough:boot-token-live:"  # sorted set per MAC: ip -> expiry
_RATE_PREFIX = "gough:boot-rate:"
_IPMACS_PREFIX = "gough:boot-ip-macs:"  # per source IP: distinct MACs minted for

TOKEN_CAP_REFUSED = Counter(
    "worker_ipxe_boot_token_cap_refused_total",
    "Boot token mints refused: per-MAC live-token cap reached",
)


class StoreUnavailableError(RuntimeError):
    """The shared state store could not be reached; callers must fail closed."""


class TokenCapError(RuntimeError):
    """A MAC already has the maximum number of live tokens (across IPs); the mint is refused."""


class IPMintLimitError(TokenCapError):
    """One source IP already minted tokens for the maximum number of distinct MACs."""


class RedisLike(Protocol):
    """Subset of redis.asyncio.Redis used here (lets tests inject fakeredis)."""

    async def eval(
        self, script: str, numkeys: int, *keys_and_args: str | int
    ) -> object:
        """Run a Lua script atomically."""

    async def get(self, name: str) -> object:
        """Fetch a key."""

    async def delete(self, *names: str) -> int:
        """Delete keys, returning how many were removed."""

    async def incr(self, name: str) -> int:
        """Increment a counter."""

    async def expire(self, name: str, time: int) -> object:
        """Set a key TTL."""

    async def zrem(self, name: str, *members: str) -> object:
        """Remove members from a sorted set."""


# Atomic mint: prune, per-IP distinct-MAC limit, per-MAC cap and the insert happen in
# one script, so concurrent mints cannot overshoot either limit. There is no eviction
# and no privileged caller: over a limit means refuse. KEYS: token, live zset
# (ip->expiry), ip->macs zset. ARGV: now, token ttl, cap, max_macs, record, ip, mac,
# bookkeeping ttl. Returns 1 ok, -1 MAC cap reached, -2 per-IP MAC limit reached.
_MINT_LUA = """
local now = tonumber(ARGV[1]); local ttl = tonumber(ARGV[2])
local cap = tonumber(ARGV[3]); local max_macs = tonumber(ARGV[4])
local ip = ARGV[6]; local mac = ARGV[7]; local book_ttl = tonumber(ARGV[8])
redis.call('ZREMRANGEBYSCORE', KEYS[2], 0, now)
redis.call('ZREMRANGEBYSCORE', KEYS[3], 0, now)
if redis.call('EXISTS', KEYS[1]) == 0 then
  if not redis.call('ZSCORE', KEYS[3], mac) and redis.call('ZCARD', KEYS[3]) >= max_macs then
    return -2
  end
  if redis.call('ZCARD', KEYS[2]) >= cap then return -1 end
end
redis.call('SET', KEYS[1], ARGV[5], 'EX', ttl)
redis.call('ZADD', KEYS[2], now + ttl, ip)
redis.call('ZADD', KEYS[3], now + ttl, mac)
redis.call('EXPIRE', KEYS[2], book_ttl)
redis.call('EXPIRE', KEYS[3], book_ttl)
return 1
"""

# Failure charge: INCR + EXPIRE on every increment (a key never lacks a TTL); returns 1
# while both counters are within their limits, 0 once either is over.
_FAIL_LUA = """
local over = 0
for i = 1, 2 do
  local n = redis.call('INCR', KEYS[i])
  redis.call('EXPIRE', KEYS[i], tonumber(ARGV[3]))
  if n > tonumber(ARGV[i]) then over = 1 end
end
return 1 - over
"""


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
        max_macs_per_ip: int = MAX_MACS_PER_IP,
    ) -> str:
        """Mint the live token for (`machine`, `source_ip`); only its SHA-256 is stored.

        Same-IP re-mint overwrites only that IP's token. A new IP is refused
        (TokenCapError) once the MAC holds `max_live_per_mac` tokens, and an IP that has
        already minted for `max_macs_per_ip` distinct MACs is refused (IPMintLimitError)
        so one host cannot squat other machines' slots. Nothing bypasses these limits and
        nothing is evicted. All checks and the insert are one Lua script (atomic).
        """
        ttl = ttl_seconds or self._ttl
        now = int(time.time())
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
        try:
            result = int(
                await self._client.eval(  # type: ignore[arg-type]
                    _MINT_LUA,
                    3,
                    f"{_KEY_PREFIX}{machine}:{source_ip}",
                    _LIVE_PREFIX + machine,
                    _IPMACS_PREFIX + source_ip,
                    now,
                    ttl,
                    self._cap,
                    max_macs_per_ip,
                    record,
                    source_ip,
                    machine,
                    self._ttl,
                )
            )
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc
        if result == -2:
            TOKEN_CAP_REFUSED.inc()
            logger.warning("boot_token_ip_mac_limit", limit=max_macs_per_ip)
            raise IPMintLimitError(
                "this address has requested tokens for too many machines"
            )
        if result == -1:
            TOKEN_CAP_REFUSED.inc()
            logger.warning("boot_token_cap_reached", cap=self._cap)
            raise TokenCapError("too many live boot tokens for this machine")
        return token

    async def redeem(
        self, token: str, machine: str, source_ip: str, stage: str
    ) -> bool:
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
            await self._client.expire(key, window_seconds)
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc
        return count <= limit

    async def charge_failure(
        self,
        pair: str,
        ip_key: str,
        pair_limit: int,
        ip_limit: int,
        window_seconds: int,
    ) -> bool:
        """Atomically charge one failed redemption to both counters; False once either is over.

        One Lua script (INCR + EXPIRE each), so budgets cannot be overshot concurrently
        and no counter ever exists without a TTL. There is no refund path.
        """
        try:
            ok = await self._client.eval(  # type: ignore[arg-type]
                _FAIL_LUA,
                2,
                _RATE_PREFIX + pair,
                _RATE_PREFIX + ip_key,
                pair_limit,
                ip_limit,
                window_seconds,
            )
        except Exception as exc:
            raise StoreUnavailableError("boot token store unavailable") from exc
        return int(str(ok)) == 1


def create_token_store(redis_url: str) -> BootTokenStore:
    """Build a store on a lazily-connecting redis.asyncio client for `redis_url`."""
    import redis.asyncio as aioredis

    return BootTokenStore(aioredis.from_url(redis_url, decode_responses=True))
