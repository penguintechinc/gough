"""BootTokenStore: MAC-keyed single live token, IP/stage binding, expiry, atomic single use, outages."""

import asyncio
import json
import time

import pytest
from conftest import FakeRedis
from worker.boot_tokens import BootTokenStore, StoreUnavailableError

MAC = "aabbccddeeff"
IP = "10.1.1.1"


@pytest.mark.asyncio
async def test_issue_stores_hash_only_keyed_by_mac_with_ttl(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
    """The raw token is never stored; the key is the MAC; the record binds IP, expiry and stages."""
    token = await token_store.issue(MAC, IP)

    ((key, value),) = (await fake_redis.tokens()).items()
    record = json.loads(value)
    assert key == f"gough:boot-token:{MAC}:{IP}"
    assert token not in key and token not in value
    assert await fake_redis.ttl(key) == 1800
    assert record["ip"] == IP and record["machine"] == MAC and record["stages"] is None


@pytest.mark.asyncio
async def test_tokens_are_unique_and_long(token_store: BootTokenStore) -> None:
    """Tokens come from token_urlsafe(32) and never repeat."""
    tokens = {await token_store.issue(MAC, IP) for _ in range(20)}

    assert len(tokens) == 20
    assert all(len(t) >= 43 for t in tokens)


@pytest.mark.asyncio
async def test_reissue_overwrites_previous_token(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
    """One live token per MAC: the older token stops working when a new one is minted."""
    old = await token_store.issue(MAC, IP)
    new = await token_store.issue(MAC, IP)

    assert len(await fake_redis.tokens()) == 1
    assert await token_store.redeem(old, MAC, IP, "installing") is False
    assert await token_store.redeem(new, MAC, IP, "installing") is True


@pytest.mark.asyncio
async def test_redeem_success_then_replay_fails(token_store: BootTokenStore) -> None:
    """Redemption works once for the bound machine and IP and never again."""
    token = await token_store.issue(MAC, IP)

    assert await token_store.redeem(token, MAC, IP, "complete") is True
    assert await token_store.redeem(token, MAC, IP, "complete") is False


@pytest.mark.asyncio
async def test_lost_delete_race_is_not_a_success(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
    """If another replica already consumed the key (DEL removes 0), this redemption fails."""
    token = await token_store.issue(MAC, IP)

    async def already_gone(*names: str) -> int:
        name = names[0]
        """Simulate the other replica winning the DEL."""
        await fake_redis.execute_command("DEL", name)
        return 0

    fake_redis.delete = already_gone  # type: ignore[method-assign]

    assert await token_store.redeem(token, MAC, IP, "complete") is False


@pytest.mark.asyncio
async def test_failed_attempts_do_not_burn_the_token(
    token_store: BootTokenStore,
) -> None:
    """Wrong token, IP, machine or stage are refused without invalidating the legitimate token."""
    token = await token_store.issue(MAC, IP, stages=frozenset({"discovery"}))

    assert await token_store.redeem("x" * 43, MAC, IP, "discovery") is False
    assert (
        await token_store.redeem(token, MAC, "10.9.9.9", "discovery") is False
    )  # no record for that IP
    assert await token_store.redeem(token, "112233445566", IP, "discovery") is False
    assert await token_store.redeem(token, MAC, IP, "installing") is False
    assert await token_store.redeem(token, MAC, IP, "discovery") is True


@pytest.mark.asyncio
async def test_custom_ttl_is_applied(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
    """A shorter TTL (discovery) is used for both the key expiry and the stored deadline."""
    await token_store.issue(MAC, IP, ttl_seconds=600)

    key = f"gough:boot-token:{MAC}:{IP}"
    assert await fake_redis.ttl(key) == 600
    assert json.loads(await fake_redis.get(key))["exp"] <= int(time.time()) + 600


@pytest.mark.asyncio
async def test_expired_record_is_refused(
    token_store: BootTokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
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
async def test_corrupt_record_is_refused(
    token_store: BootTokenStore, fake_redis: FakeRedis, raw: str
) -> None:
    """Garbage in the store never validates."""
    await fake_redis.set(f"gough:boot-token:{MAC}:{IP}", raw)

    assert await token_store.redeem("t" * 43, MAC, IP, "installing") is False


@pytest.mark.asyncio
async def test_rate_window_counts_and_sets_expiry_once(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
    """The first hit starts the window; the limit is inclusive; keys are independent."""
    results = [await token_store.allow("ip:1.1.1.1", 2, 60) for _ in range(3)]

    assert results == [True, True, False]
    assert await token_store.allow("ip:2.2.2.2", 2, 60) is True
    assert await fake_redis.ttl("gough:boot-rate:ip:1.1.1.1") == 60


@pytest.mark.asyncio
async def test_store_outage_raises_for_every_operation(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
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
async def test_delete_failure_after_successful_check_raises(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
    """If the store dies between the check and the DEL, the caller sees an outage, not a success."""
    token = await token_store.issue(MAC, IP)

    async def broken(*names: str) -> int:
        """Fail the DEL."""
        raise ConnectionError("gone")

    fake_redis.delete = broken  # type: ignore[method-assign]

    with pytest.raises(StoreUnavailableError):
        await token_store.redeem(token, MAC, IP, "installing")


@pytest.mark.asyncio
async def test_other_ip_mint_never_touches_existing_token(
    token_store: BootTokenStore,
) -> None:
    """Tokens are keyed (MAC, IP): minting from IP B leaves IP A's token valid."""
    a = await token_store.issue(MAC, "10.1.1.1")
    b = await token_store.issue(MAC, "10.2.2.2")

    assert await token_store.redeem(a, MAC, "10.1.1.1", "installing") is True
    assert await token_store.redeem(b, MAC, "10.2.2.2", "installing") is True


@pytest.mark.asyncio
async def test_cap_refuses_new_ip_without_evicting_and_frees_on_redeem(
    token_store: BootTokenStore,
) -> None:
    """The 5th distinct IP is refused; redeeming (or expiry) frees a slot; same-IP re-mint is allowed."""
    from worker.boot_tokens import TokenCapError

    tokens = {
        f"10.0.0.{i}": await token_store.issue(MAC, f"10.0.0.{i}") for i in range(4)
    }

    with pytest.raises(TokenCapError):
        await token_store.issue(MAC, "10.0.0.50")
    await token_store.issue(MAC, "10.0.0.0")  # overwrite own slot: fine
    assert await token_store.redeem(tokens["10.0.0.1"], MAC, "10.0.0.1", "x") is True
    assert await token_store.issue(MAC, "10.0.0.50")
    assert await token_store.redeem(tokens["10.0.0.2"], MAC, "10.0.0.2", "x") is True


@pytest.mark.asyncio
async def test_expired_slots_do_not_count_toward_cap(
    token_store: BootTokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live-set entries past their expiry are pruned before the cap check."""
    for i in range(4):
        await token_store.issue(MAC, f"10.0.0.{i}", ttl_seconds=10)
    real = time.time
    monkeypatch.setattr("worker.boot_tokens.time.time", lambda: real() + 60)

    assert await token_store.issue(MAC, "10.0.0.77")


@pytest.mark.asyncio
async def test_charge_failure_is_atomic_and_always_has_ttl(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
    """Both counters are charged together, report False past either limit, and always carry a TTL."""
    results = [await token_store.charge_failure("p", "i", 2, 5, 60) for _ in range(3)]

    assert results == [True, True, False]
    for key in ("gough:boot-rate:p", "gough:boot-rate:i"):
        assert 0 < await fake_redis.ttl(key) <= 60


@pytest.mark.asyncio
async def test_charge_failure_outage_raises(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
    """A store failure while charging is surfaced so callers fail closed."""
    fake_redis.fail = True

    with pytest.raises(StoreUnavailableError):
        await token_store.charge_failure("p", "i", 1, 1, 60)


@pytest.mark.asyncio
async def test_ip_may_mint_for_only_three_distinct_macs(
    token_store: BootTokenStore,
) -> None:
    """One source IP cannot squat slots for more than 3 MACs; re-minting a known MAC is fine."""
    from worker.boot_tokens import IPMintLimitError

    for i in range(3):
        await token_store.issue(f"00000000000{i}", IP)
    with pytest.raises(IPMintLimitError):
        await token_store.issue("000000000009", IP)
    assert await token_store.issue("000000000000", IP)  # already counted
    assert await token_store.issue("000000000009", "10.9.9.9")  # other IPs unaffected


@pytest.mark.asyncio
async def test_full_cap_refuses_and_never_evicts(token_store: BootTokenStore) -> None:
    """With the cap full a new IP is refused; every existing token stays valid (no eviction path)."""
    from worker.boot_tokens import TokenCapError

    tokens = {
        f"10.0.0.{i}": await token_store.issue(MAC, f"10.0.0.{i}") for i in range(4)
    }
    with pytest.raises(TokenCapError):
        await token_store.issue(MAC, "10.0.0.50")

    for ip, token in tokens.items():
        assert await token_store.redeem(token, MAC, ip, "x") is True


@pytest.mark.asyncio
async def test_bookkeeping_keys_always_expire_at_max_ttl(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
    """Zsets keep the full token TTL even after a short-lived (discovery) mint; none lacks a TTL."""
    await token_store.issue(MAC, IP)
    await token_store.issue(MAC, "10.2.2.2", ttl_seconds=60)

    for key in (
        f"gough:boot-token-live:{MAC}",
        f"gough:boot-ip-macs:{IP}",
        "gough:boot-ip-macs:10.2.2.2",
    ):
        assert await fake_redis.ttl(key) == 1800


@pytest.mark.asyncio
async def test_concurrent_mints_never_exceed_cap(
    token_store: BootTokenStore, fake_redis: FakeRedis
) -> None:
    """40 simultaneous mints from distinct IPs for one MAC yield exactly `cap` live tokens (Lua is atomic)."""
    from worker.boot_tokens import TokenCapError

    async def mint(i: int) -> bool:
        """Try one mint; True if admitted."""
        try:
            await token_store.issue(MAC, f"10.5.{i // 250}.{i % 250}")
        except TokenCapError:
            return False
        return True

    results = await asyncio.gather(*(mint(i) for i in range(40)))

    assert sum(results) == 4
    assert len(await fake_redis.tokens()) == 4


@pytest.mark.asyncio
async def test_concurrent_mints_never_exceed_per_ip_mac_limit(
    token_store: BootTokenStore,
) -> None:
    """20 simultaneous mints from one IP for distinct MACs admit exactly 3."""
    from worker.boot_tokens import TokenCapError

    async def mint(i: int) -> bool:
        """Try one mint; True if admitted."""
        try:
            await token_store.issue(f"0000000000{i:02d}", IP)
        except TokenCapError:
            return False
        return True

    assert sum(await asyncio.gather(*(mint(i) for i in range(20)))) == 3
