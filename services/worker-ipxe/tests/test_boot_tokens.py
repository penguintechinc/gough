"""BootTokenStore: MAC-keyed single live token, IP/stage binding, expiry, atomic single use, outages."""

import json
import time

import pytest

from conftest import FakeRedis
from worker.boot_tokens import BootTokenStore, StoreUnavailableError

MAC = "aabbccddeeff"
IP = "10.1.1.1"


@pytest.mark.asyncio
async def test_issue_stores_hash_only_keyed_by_mac_with_ttl(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """The raw token is never stored; the key is the MAC; the record binds IP, expiry and stages."""
    token = await token_store.issue(MAC, IP)

    (key, value), = fake_redis.data.items()
    record = json.loads(value)
    assert key == f"gough:boot-token:{MAC}"
    assert token not in key and token not in value
    assert fake_redis.ttls[key] == 1800
    assert record["ip"] == IP and record["machine"] == MAC and record["stages"] is None


@pytest.mark.asyncio
async def test_tokens_are_unique_and_long(token_store: BootTokenStore) -> None:
    """Tokens come from token_urlsafe(32) and never repeat."""
    tokens = {await token_store.issue(MAC, IP) for _ in range(20)}

    assert len(tokens) == 20
    assert all(len(t) >= 43 for t in tokens)


@pytest.mark.asyncio
async def test_reissue_overwrites_previous_token(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """One live token per MAC: the older token stops working when a new one is minted."""
    old = await token_store.issue(MAC, IP)
    new = await token_store.issue(MAC, IP)

    assert len(fake_redis.data) == 1
    assert await token_store.redeem(old, MAC, IP, "installing") is False
    assert await token_store.redeem(new, MAC, IP, "installing") is True


@pytest.mark.asyncio
async def test_redeem_success_then_replay_fails(token_store: BootTokenStore) -> None:
    """Redemption works once for the bound machine and IP and never again."""
    token = await token_store.issue(MAC, IP)

    assert await token_store.redeem(token, MAC, IP, "complete") is True
    assert await token_store.redeem(token, MAC, IP, "complete") is False


@pytest.mark.asyncio
async def test_lost_delete_race_is_not_a_success(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """If another replica already consumed the key (DEL removes 0), this redemption fails."""
    token = await token_store.issue(MAC, IP)

    async def already_gone(name: str) -> int:
        """Simulate the other replica winning the DEL."""
        fake_redis.data.pop(name, None)
        return 0

    fake_redis.delete = already_gone  # type: ignore[method-assign]

    assert await token_store.redeem(token, MAC, IP, "complete") is False


@pytest.mark.asyncio
async def test_failed_attempts_do_not_burn_the_token(token_store: BootTokenStore) -> None:
    """Wrong token, IP, machine or stage are refused without invalidating the legitimate token."""
    token = await token_store.issue(MAC, IP, stages=frozenset({"discovery"}))

    assert await token_store.redeem("x" * 43, MAC, IP, "discovery") is False
    assert await token_store.redeem(token, MAC, "10.9.9.9", "discovery") is False
    assert await token_store.redeem(token, "112233445566", IP, "discovery") is False
    assert await token_store.redeem(token, MAC, IP, "installing") is False
    assert await token_store.redeem(token, MAC, IP, "discovery") is True


@pytest.mark.asyncio
async def test_custom_ttl_is_applied(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """A shorter TTL (discovery) is used for both the key expiry and the stored deadline."""
    await token_store.issue(MAC, IP, ttl_seconds=600)

    key = f"gough:boot-token:{MAC}"
    assert fake_redis.ttls[key] == 600
    assert json.loads(fake_redis.data[key])["exp"] <= int(time.time()) + 600


@pytest.mark.asyncio
async def test_expired_record_is_refused(token_store: BootTokenStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A record past its deadline fails even if the key still exists."""
    token = await token_store.issue(MAC, IP)
    real = time.time
    monkeypatch.setattr("worker.boot_tokens.time.time", lambda: real() + 1900)

    assert await token_store.redeem(token, MAC, IP, "installing") is False


@pytest.mark.asyncio
async def test_redeem_unknown_machine_fails(token_store: BootTokenStore) -> None:
    """No record for the MAC means refusal."""
    assert await token_store.redeem("t" * 43, MAC, IP, "installing") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        '{"hash": "x", "machine": "m", "ip": "i", "exp": "soon", "stages": null}',
        '{"hash": "x", "machine": "m", "ip": "i", "exp": 1, "stages": "discovery"}',
    ],
)
async def test_corrupt_record_is_refused(token_store: BootTokenStore, fake_redis: FakeRedis, raw: str) -> None:
    """Garbage in the store never validates."""
    fake_redis.data[f"gough:boot-token:{MAC}"] = raw

    assert await token_store.redeem("t" * 43, MAC, IP, "installing") is False


@pytest.mark.asyncio
async def test_rate_window_counts_and_sets_expiry_once(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """The first hit starts the window; the limit is inclusive; keys are independent."""
    results = [await token_store.allow("ip:1.1.1.1", 2, 60) for _ in range(3)]

    assert results == [True, True, False]
    assert await token_store.allow("ip:2.2.2.2", 2, 60) is True
    assert fake_redis.ttls["gough:boot-rate:ip:1.1.1.1"] == 60


@pytest.mark.asyncio
async def test_store_outage_raises_for_every_operation(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """Every operation reports an unavailable store explicitly so callers fail closed."""
    token = await token_store.issue(MAC, IP)
    fake_redis.fail = True

    with pytest.raises(StoreUnavailableError):
        await token_store.issue(MAC, IP)
    with pytest.raises(StoreUnavailableError):
        await token_store.redeem(token, MAC, IP, "installing")
    with pytest.raises(StoreUnavailableError):
        await token_store.allow("s", 1, 1)


@pytest.mark.asyncio
async def test_delete_failure_after_successful_check_raises(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """If the store dies between the check and the DEL, the caller sees an outage, not a success."""
    token = await token_store.issue(MAC, IP)

    async def broken(name: str) -> int:
        """Fail the DEL."""
        raise ConnectionError("gone")

    fake_redis.delete = broken  # type: ignore[method-assign]

    with pytest.raises(StoreUnavailableError):
        await token_store.redeem(token, MAC, IP, "installing")
