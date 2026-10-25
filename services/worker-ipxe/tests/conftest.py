"""Shared fixtures: in-memory Redis double, token store, and a deterministic storage resolver."""

import fakeredis.aioredis
import pytest
from worker.boot_tokens import BootTokenStore
from worker.services import http_server as http_mod

PUBLIC_IP = "93.184.216.34"


class FakeRedis(fakeredis.aioredis.FakeRedis):
    """fakeredis (real Lua support) with an outage switch and small inspection helpers."""

    fail = False

    async def execute_command(self, *args: object, **options: object) -> object:
        """Raise ConnectionError while `fail` is set, else run the command."""
        if self.fail:
            raise ConnectionError("redis down")
        return await super().execute_command(*args, **options)

    async def tokens(self) -> dict[str, str]:
        """All stored token records keyed by redis key."""
        return {k: await self.get(k) for k in await self.keys("gough:boot-token:*:*")}

    async def rate_keys(self) -> list[str]:
        """All rate-limit counter keys."""
        return list(await self.keys("gough:boot-rate:*"))


@pytest.fixture
def fake_redis() -> FakeRedis:
    """A fresh in-memory Redis double."""
    return FakeRedis(decode_responses=True)


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
