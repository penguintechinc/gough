"""Shared fixtures: in-memory Redis double, token store, and a deterministic storage resolver."""

import pytest

from worker.boot_tokens import BootTokenStore
from worker.services import http_server as http_mod

PUBLIC_IP = "93.184.216.34"


class FakeRedis:
    """Async stand-in for the redis.asyncio commands BootTokenStore uses (single-process test double)."""

    def __init__(self) -> None:
        """Start empty; `fail` makes every command raise ConnectionError."""
        self.data: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.fail = False

    def _check(self) -> None:
        """Raise when the double is told the store is down."""
        if self.fail:
            raise ConnectionError("redis down")

    async def set(self, name: str, value: str, ex: int | None = None) -> bool:
        """Store a value and remember its TTL."""
        self._check()
        self.data[name] = value
        if ex is not None:
            self.ttls[name] = ex
        return True

    async def get(self, name: str) -> str | None:
        """Fetch a key."""
        self._check()
        if name in self.counters:
            return str(self.counters[name])
        return self.data.get(name)

    async def delete(self, name: str) -> int:
        """Delete a key; returns how many were removed (0 or 1)."""
        self._check()
        return 1 if self.data.pop(name, None) is not None else 0

    async def incr(self, name: str) -> int:
        """Increment a counter."""
        self._check()
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    async def zadd(self, name: str, mapping: dict[str, float]) -> int:
        """Add members to a sorted set."""
        self._check()
        self.zsets.setdefault(name, {}).update(mapping)
        return len(mapping)

    async def zrem(self, name: str, *members: str) -> int:
        """Remove members from a sorted set."""
        self._check()
        z = self.zsets.get(name, {})
        return sum(1 for m in members if z.pop(m, None) is not None)

    async def zcard(self, name: str) -> int:
        """Sorted-set size."""
        self._check()
        return len(self.zsets.get(name, {}))

    async def zremrangebyscore(self, name: str, low: float, high: float) -> int:
        """Drop members with low <= score <= high."""
        self._check()
        z = self.zsets.get(name, {})
        gone = [m for m, sc in z.items() if low <= sc <= high]
        for m in gone:
            del z[m]
        return len(gone)

    async def expire(self, name: str, time: int) -> bool:
        """Record a TTL."""
        self._check()
        self.ttls[name] = time
        return True


@pytest.fixture
def fake_redis() -> FakeRedis:
    """A fresh in-memory Redis double."""
    return FakeRedis()


@pytest.fixture
def token_store(fake_redis: FakeRedis) -> BootTokenStore:
    """A boot-token store backed by the Redis double."""
    return BootTokenStore(fake_redis)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _fake_token_backend(monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis) -> None:
    """Make HTTPBootServer build its token store on the Redis double instead of a real connection."""
    monkeypatch.setattr(
        http_mod, "create_token_store", lambda url: BootTokenStore(fake_redis)  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _public_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every DNS name to a public IP so storage tests never touch real DNS."""

    async def fake_resolve(host: str, port: int) -> list[str]:
        """Return a documentation-range public address."""
        return [PUBLIC_IP]

    monkeypatch.setattr(http_mod, "_resolve_host", fake_resolve)
