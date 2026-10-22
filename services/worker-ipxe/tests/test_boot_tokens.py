"""BootTokenStore: hashing, TTL, binding, single use, corruption and outage handling."""

import json

import pytest

from conftest import FakeRedis
from worker.boot_tokens import BootTokenStore, StoreUnavailableError


@pytest.mark.asyncio
async def test_issue_stores_hash_only_with_ttl(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """The raw token is never stored; the key is its digest and carries the TTL."""
    token = await token_store.issue("aabbccddeeff")

    (key, value), = fake_redis.data.items()
    assert token not in key and token not in value
    assert fake_redis.ttls[key] == 1800
    assert json.loads(value)["machine"] == "aabbccddeeff"


@pytest.mark.asyncio
async def test_tokens_are_unique_and_long(token_store: BootTokenStore) -> None:
    """Tokens come from token_urlsafe(32) and never repeat."""
    tokens = {await token_store.issue("aabbccddeeff") for _ in range(20)}

    assert len(tokens) == 20
    assert all(len(t) >= 43 for t in tokens)


@pytest.mark.asyncio
async def test_redeem_success_then_replay_fails(token_store: BootTokenStore) -> None:
    """Redemption works once for the bound machine and never again."""
    token = await token_store.issue("aabbccddeeff")

    assert await token_store.redeem(token, "aabbccddeeff") is True
    assert await token_store.redeem(token, "aabbccddeeff") is False


@pytest.mark.asyncio
async def test_redeem_wrong_machine_fails_and_burns(token_store: BootTokenStore) -> None:
    """A mismatched machine is refused and the token is consumed."""
    token = await token_store.issue("aabbccddeeff")

    assert await token_store.redeem(token, "112233445566") is False
    assert await token_store.redeem(token, "aabbccddeeff") is False


@pytest.mark.asyncio
async def test_redeem_unknown_token_fails(token_store: BootTokenStore) -> None:
    """A token that was never issued is refused."""
    assert await token_store.redeem("nope", "aabbccddeeff") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["not json", "{}", '{"hash": "x", "machine": "m", "exp": "soon"}'])
async def test_corrupt_record_is_refused(token_store: BootTokenStore, fake_redis: FakeRedis, raw: str) -> None:
    """Garbage in the store never validates."""
    import hashlib

    token = "t" * 43
    fake_redis.data["gough:boot-token:" + hashlib.sha256(token.encode()).hexdigest()] = raw

    assert await token_store.redeem(token, "aabbccddeeff") is False


@pytest.mark.asyncio
async def test_hash_mismatch_in_record_is_refused(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """A record whose stored hash differs from the presented token's hash is refused (constant-time check)."""
    import hashlib
    import time

    token = "t" * 43
    key = "gough:boot-token:" + hashlib.sha256(token.encode()).hexdigest()
    fake_redis.data[key] = json.dumps({"hash": "0" * 64, "machine": "m", "exp": int(time.time()) + 100})

    assert await token_store.redeem(token, "m") is False


@pytest.mark.asyncio
async def test_rate_window_counts_and_sets_expiry_once(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """The first hit starts the window; the limit is inclusive; sources are independent."""
    results = [await token_store.allow("1.1.1.1", 2, 60) for _ in range(3)]

    assert results == [True, True, False]
    assert await token_store.allow("2.2.2.2", 2, 60) is True
    assert fake_redis.ttls["gough:boot-event-rate:1.1.1.1"] == 60


@pytest.mark.asyncio
async def test_store_outage_raises_for_every_operation(token_store: BootTokenStore, fake_redis: FakeRedis) -> None:
    """Every operation reports an unavailable store explicitly so callers fail closed."""
    fake_redis.fail = True

    with pytest.raises(StoreUnavailableError):
        await token_store.issue("m")
    with pytest.raises(StoreUnavailableError):
        await token_store.redeem("t", "m")
    with pytest.raises(StoreUnavailableError):
        await token_store.allow("s", 1, 1)
