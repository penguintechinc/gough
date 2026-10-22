"""/boot-event: per-boot token, machine binding, single use, schema allowlist, rate limit, no echo."""

import hashlib
import json
import time
from typing import Any

import httpx
import pytest
import respx

from conftest import FakeRedis
from worker.boot_tokens import BootTokenStore
from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager
from worker.services import http_server as http_mod
from worker.services.http_server import HTTPBootServer

API = "http://api.test"
UPSTREAM = f"{API}/api/v1/internal/boot-event"
CLIENT_IP = "10.1.1.1"
CLIENT = {"client": (CLIENT_IP, 4000)}
MAC = "aabbccddeeff"
OTHER_MAC = "112233445566"


def _server() -> HTTPBootServer:
    """HTTP server with a session-enrolled manager and the Redis-double token store."""
    cfg = WorkerConfig(
        api_manager_url=API, worker_api_key="w" * 32, worker_id="w1", dhcp_mode="disabled",
        dhcp_interface="eth0", redis_url="redis://test:6379/0", http_boot_url="http://10.0.0.1:8080",
    )
    enrollment = EnrollmentManager(cfg)
    enrollment.session_token = "sess"
    return HTTPBootServer(cfg, enrollment)


def _event(**over: Any) -> dict[str, Any]:
    """A valid boot-event body with optional overrides."""
    body: dict[str, Any] = {"mac": MAC, "stage": "installing"}
    body.update(over)
    return body


async def _post(server: HTTPBootServer, body: Any, token: str | None, **kw: Any) -> Any:
    """POST /boot-event with an optional X-Boot-Token header."""
    headers = {"X-Boot-Token": token} if token is not None else {}
    kw.setdefault("scope_base", CLIENT)
    return await server.app.test_client().post("/boot-event", json=body, headers=headers, **kw)


@pytest.mark.asyncio
async def test_valid_token_forwards_only_validated_fields_and_returns_202() -> None:
    """A good token + valid body forwards exactly the validated fields with the worker credentials."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    body = _event(machine_id="m-1", timestamp="2026-10-22T10:00:00Z", message="partitioning")
    with respx.mock(assert_all_called=True) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201, json={"secret": "hub-detail"}))
        resp = await _post(server, body, token)
        payload = await resp.get_json()

    assert resp.status_code == 202
    assert payload == {"status": "accepted"}
    sent = route.calls.last.request
    assert sent.headers["X-Worker-API-Key"] == "w" * 32
    assert json.loads(sent.content) == {
        "mac": MAC, "stage": "installing", "machine_id": "m-1",
        "timestamp": "2026-10-22T10:00:00Z", "message": "partitioning",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "", "x" * 129, "not-a-real-token", "a" * 43])
async def test_missing_or_unknown_token_is_401_and_never_forwarded(token: str | None) -> None:
    """Without a valid token the hub is never contacted (no confused-deputy forwarding)."""
    server = _server()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        resp = await _post(server, _event(), token)

    assert resp.status_code == 401
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_token_for_other_machine_is_rejected_and_not_burned() -> None:
    """A token minted for machine A cannot report for B; the failed try does not burn A's token."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        wrong = await _post(server, _event(mac=OTHER_MAC), token)
        right = await _post(server, _event(), token)

    assert (wrong.status_code, right.status_code) == (401, 202)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_token_redeemed_from_other_ip_is_rejected() -> None:
    """A token is bound to the IP that fetched the script; redemption elsewhere fails."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        resp = await _post(server, _event(), token, scope_base={"client": ("10.9.9.9", 1)})

    assert resp.status_code == 401
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_new_script_fetch_replaces_previous_token() -> None:
    """Only one live token per MAC: minting again invalidates the earlier token."""
    server = _server()
    old = await server.token_store.issue(MAC, CLIENT_IP)
    new = await server.token_store.issue(MAC, CLIENT_IP)
    with respx.mock(assert_all_called=False) as router:
        router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        stale = await _post(server, _event(), old)
        fresh = await _post(server, _event(), new)

    assert (stale.status_code, fresh.status_code) == (401, 202)


@pytest.mark.asyncio
async def test_discovery_token_only_allows_discovery_stage_and_short_ttl(fake_redis: FakeRedis) -> None:
    """Unknown MACs get a discovery-only token with a 10 minute TTL; other stages are refused."""
    server = _server()
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/boot-script/{MAC}").mock(return_value=httpx.Response(404))
        resp = await server.app.test_client().get(f"/ipxe/{MAC}.ipxe", scope_base=CLIENT)
        script = await resp.get_data(as_text=True)
    token = script.split("set boot_token ")[1].split("\n")[0]
    key = f"gough:boot-token:{MAC}:{CLIENT_IP}"
    assert await fake_redis.ttl(key) == 600
    assert json.loads(await fake_redis.get(key))["stages"] == ["discovery"]
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        denied = await _post(server, _event(stage="installing"), token)
        allowed = await _post(server, _event(stage="discovery"), token)

    assert (denied.status_code, allowed.status_code) == (401, 202)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_missing_client_address_is_rejected_without_shared_bucket(fake_redis: FakeRedis) -> None:
    """A request with no usable remote address gets 400 and touches no rate-limit bucket."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    await fake_redis.flushall()

    resp = await _post(server, _event(), token, scope_base={"client": None})

    assert resp.status_code == 400
    assert await fake_redis.rate_keys() == []


@pytest.mark.asyncio
async def test_malformed_token_does_not_consume_rate_budget(fake_redis: FakeRedis) -> None:
    """Junk tokens are rejected before the store, so they cannot exhaust a NAT'd client's budget."""
    server = _server()
    for _ in range(50):
        assert (await _post(server, _event(), "junk")).status_code == 401

    assert await fake_redis.rate_keys() == []


@pytest.mark.asyncio
async def test_other_ip_mint_does_not_affect_legit_token() -> None:
    """An attacker minting for the same MAC from another IP leaves the real machine's token valid."""
    server = _server()
    legit = await server.token_store.issue(MAC, CLIENT_IP)
    await server.token_store.issue(MAC, "10.6.6.6")
    with respx.mock(assert_all_called=False) as router:
        router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        resp = await _post(server, _event(), legit)

    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_other_ip_failures_do_not_block_legit_redemption(monkeypatch: pytest.MonkeyPatch) -> None:
    """Junk from other IPs exhausts only their own (MAC, IP) budgets; the legit token still redeems."""
    monkeypatch.setattr(http_mod, "BOOT_EVENT_FAIL_LIMIT", 2)
    server = _server()
    legit = await server.token_store.issue(MAC, CLIENT_IP)
    for i in range(6):
        await _post(server, _event(), "a" * 43, scope_base={"client": (f"10.2.2.{i % 2}", 1)})
    with respx.mock(assert_all_called=False) as router:
        router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        resp = await _post(server, _event(), legit)

    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_failure_budget_is_per_mac_and_ip_and_valid_token_still_works_under_own_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failures from one (MAC, IP) are limited with 429; a different MAC from the same IP is not."""
    monkeypatch.setattr(http_mod, "BOOT_EVENT_FAIL_LIMIT", 3)
    server = _server()
    codes = [(await _post(server, _event(), "a" * 43)).status_code for _ in range(5)]
    other_mac = await _post(server, _event(mac=OTHER_MAC), "a" * 43)

    assert codes == [401, 401, 401, 429, 429]
    assert other_mac.status_code == 401


@pytest.mark.asyncio
async def test_successful_redemptions_charge_no_budget(fake_redis: FakeRedis) -> None:
    """Valid tokens never touch a rate counter."""
    server = _server()
    with respx.mock:
        respx.post(UPSTREAM).mock(return_value=httpx.Response(201))
        for _ in range(15):
            token = await server.token_store.issue(MAC, CLIENT_IP)
            assert (await _post(server, _event(), token)).status_code == 202

    assert [int(await fake_redis.get(k)) for k in await fake_redis.rate_keys()] == [0, 0]


@pytest.mark.asyncio
async def test_coarse_per_ip_failure_budget_across_macs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures spread over many MACs from one IP still hit the coarse per-IP budget (429)."""
    monkeypatch.setattr(http_mod, "BOOT_EVENT_IP_FAIL_LIMIT", 3)
    server = _server()
    codes = [
        (await _post(server, _event(mac=f"0000000000{i:02d}"), "a" * 43)).status_code for i in range(5)
    ]
    other_ip = await _post(server, _event(), "a" * 43, scope_base={"client": ("10.4.4.4", 1)})

    assert codes == [401, 401, 401, 429, 429]
    assert other_ip.status_code == 401


@pytest.mark.asyncio
async def test_concurrent_failures_cannot_overshoot_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """30 simultaneous bad attempts yield exactly BOOT_EVENT_FAIL_LIMIT 401s (INCR decides atomically)."""
    import asyncio

    monkeypatch.setattr(http_mod, "BOOT_EVENT_FAIL_LIMIT", 5)
    server = _server()
    codes = await asyncio.gather(*(_post(server, _event(), "a" * 43) for _ in range(30)))

    assert sorted(r.status_code for r in codes).count(401) == 5


@pytest.mark.asyncio
async def test_ip_mac_limit_and_observed_admission_over_http() -> None:
    """/ipxe: a 4th distinct MAC from one IP is 429; one ProxyDHCP observed for that IP is admitted."""
    from worker.observations import ObservedClients

    server = _server()
    observed = ObservedClients()
    server.ipxe_handler.observed = observed
    observed.record("0000000000ff", CLIENT_IP)
    macs = [f"0000000000{i:02d}" for i in range(4)]
    with respx.mock:
        for m in [*macs, "0000000000ff"]:
            respx.get(f"{API}/api/v1/internal/boot-script/{m}").mock(return_value=httpx.Response(404))
        client = server.app.test_client()
        codes = [(await client.get(f"/ipxe/{m}.ipxe", scope_base=CLIENT)).status_code for m in macs]
        seen = await client.get("/ipxe/0000000000ff.ipxe", scope_base=CLIENT)

    assert codes == [200, 200, 200, 429]
    assert seen.status_code == 200


@pytest.mark.asyncio
async def test_live_token_cap_per_mac_is_enforced_with_429() -> None:
    """A MAC may hold 4 live tokens across IPs; the 5th IP is refused (429) and evicts nobody."""
    server = _server()
    ips = [f"10.3.3.{i}" for i in range(4)]
    for ip in ips:
        await server.token_store.issue(MAC, ip)
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/boot-script/{MAC}").mock(return_value=httpx.Response(404))
        refused = await server.app.test_client().get(f"/ipxe/{MAC}.ipxe", scope_base={"client": ("10.3.3.99", 1)})
        same_ip = await server.app.test_client().get(f"/ipxe/{MAC}.ipxe", scope_base={"client": (ips[0], 1)})

    assert refused.status_code == 429
    assert same_ip.status_code == 200  # an existing IP may re-mint (overwrites only itself)


@pytest.mark.asyncio
async def test_dhcp_observation_mismatch_is_counted_but_not_fatal() -> None:
    """If ProxyDHCP saw another IP for this MAC, the mint still succeeds and the mismatch metric increments."""
    from worker.observations import ObservedClients
    from worker.services.ipxe_handler import OBSERVATION_MISMATCH

    observed = ObservedClients()
    observed.record(MAC, "10.8.8.8")
    server = _server()
    server.ipxe_handler.observed = observed
    before = OBSERVATION_MISMATCH._value.get()
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/boot-script/{MAC}").mock(return_value=httpx.Response(404))
        resp = await server.app.test_client().get(f"/ipxe/{MAC}.ipxe", scope_base=CLIENT)
        matching = observed.record(MAC, CLIENT_IP)
        await server.app.test_client().get(f"/ipxe/{MAC}.ipxe", scope_base=CLIENT)

    assert resp.status_code == 200 and matching is None
    assert OBSERVATION_MISMATCH._value.get() == before + 1  # second fetch matched, no increment


@pytest.mark.asyncio
async def test_ipxe_endpoint_is_rate_limited_per_source_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """/ipxe mints tokens, so it is limited per client IP; other IPs are unaffected."""
    monkeypatch.setattr(http_mod, "IPXE_RATE_LIMIT", 2)
    server = _server()
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/boot-script/{MAC}").mock(return_value=httpx.Response(404))
        client = server.app.test_client()
        codes = [(await client.get(f"/ipxe/{MAC}.ipxe", scope_base=CLIENT)).status_code for _ in range(3)]
        other = await client.get(f"/ipxe/{MAC}.ipxe", scope_base={"client": ("10.7.7.7", 1)})

    assert codes == [200, 200, 429]
    assert other.status_code == 200


@pytest.mark.asyncio
async def test_ipxe_endpoint_rejects_unknown_client_and_store_outage(fake_redis: FakeRedis) -> None:
    """No client address -> 400; store down -> 503; neither calls api-manager."""
    server = _server()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=r".*").mock(return_value=httpx.Response(404))
        noaddr = await server.app.test_client().get(f"/ipxe/{MAC}.ipxe", scope_base={"client": None})
        fake_redis.fail = True
        down = await server.app.test_client().get(f"/ipxe/{MAC}.ipxe", scope_base=CLIENT)

    assert (noaddr.status_code, down.status_code) == (400, 503)
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_forwarded_for_binds_client_ip_only_via_trusted_proxy() -> None:
    """Behind a trusted proxy the token is bound to the XFF client; from an untrusted peer XFF is ignored."""
    cfg = WorkerConfig(
        api_manager_url=API, worker_api_key="w" * 32, worker_id="w1", dhcp_mode="disabled",
        dhcp_interface="eth0", redis_url="redis://t/0", trusted_proxies=("10.0.0.0/8",),
    )
    enrollment = EnrollmentManager(cfg)
    enrollment.session_token = "sess"
    server = HTTPBootServer(cfg, enrollment)
    token = await server.token_store.issue(MAC, "203.0.113.5")
    hdr = {"X-Boot-Token": token, "X-Forwarded-For": "203.0.113.5"}
    client = server.app.test_client()
    with respx.mock(assert_all_called=False) as router:
        router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        untrusted = await client.post(
            "/boot-event", json=_event(), headers=hdr, scope_base={"client": ("198.51.100.1", 1)}
        )
        trusted = await client.post(
            "/boot-event", json=_event(), headers=hdr, scope_base={"client": ("10.0.0.2", 1)}
        )

    assert (untrusted.status_code, trusted.status_code) == (401, 202)


@pytest.mark.parametrize(
    "peer, xff, expected",
    [
        (None, None, None),
        ("<local>", None, None),
        ("192.0.2.1", "1.2.3.4", "192.0.2.1"),
        ("10.0.0.2", None, "10.0.0.2"),
        ("10.0.0.2", "203.0.113.5", "203.0.113.5"),
        ("10.0.0.2", "6.6.6.6, 203.0.113.5", "203.0.113.5"),
        ("10.0.0.2", "203.0.113.5, 10.0.0.9", "203.0.113.5"),
        ("10.0.0.2", "garbage", None),
        ("10.0.0.2", "10.0.0.5, 10.0.0.9", None),
    ],
)
def test_resolve_client_ip(peer: str | None, xff: str | None, expected: str | None) -> None:
    """XFF is honoured only from trusted peers, walking right to left past trusted hops."""
    import ipaddress

    trusted = (ipaddress.ip_network("10.0.0.0/8"),)

    assert http_mod._resolve_client_ip(peer, xff, trusted) == expected


@pytest.mark.asyncio
async def test_token_is_single_use() -> None:
    """Replaying a token after a successful event is refused."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        first = await _post(server, _event(), token)
        second = await _post(server, _event(), token)

    assert (first.status_code, second.status_code) == (202, 401)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_expired_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token past its expiry is refused even if the key is still present."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    real = time.time
    monkeypatch.setattr("worker.boot_tokens.time.time", lambda: real() + 31 * 60)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        resp = await _post(server, _event(), token)

    assert resp.status_code == 401
    assert route.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _event(extra="field"),
        _event(stage="rooted"),
        {"stage": "installing"},
        _event(mac="not-a-mac"),
        _event(mac=["a"]),
        _event(machine_id="../../etc"),
        _event(timestamp="yesterday!"),
        _event(message="x" * 257),
        _event(message="line\nbreak"),
        ["not", "an", "object"],
    ],
    ids=["unknown-field", "bad-stage", "no-mac", "bad-mac", "non-string", "bad-machine-id",
         "bad-timestamp", "long-message", "control-chars", "array-body"],
)
async def test_schema_violations_are_400_and_do_not_burn_token(body: Any) -> None:
    """Invalid bodies are rejected before the token is consumed or anything is forwarded."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        resp = await _post(server, body, token)
        good = await _post(server, _event(), token)

    assert resp.status_code == 400
    assert route.call_count == 1  # only the follow-up valid request, proving the token survived
    assert good.status_code == 202


@pytest.mark.asyncio
async def test_non_json_body_is_400() -> None:
    """A body that is not JSON is a 400 with the token untouched."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)

    resp = await server.app.test_client().post(
        "/boot-event", data="not json", headers={"X-Boot-Token": token, "Content-Type": "text/plain"},
        scope_base=CLIENT,
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_oversize_body_is_rejected() -> None:
    """Bodies over the size cap never reach validation or the hub."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    big = _event(message="y" * 10_000)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        resp = await _post(server, big, token)

    assert resp.status_code == 413
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_upstream_failure_is_generic_502_without_upstream_details() -> None:
    """A hub rejection is reported as a generic 502; status code and body are never echoed."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    with respx.mock:
        respx.post(UPSTREAM).mock(return_value=httpx.Response(418, text="hub-internal-detail"))
        resp = await _post(server, _event(), token)
        text = await resp.get_data(as_text=True)

    assert resp.status_code == 502
    assert json.loads(text) == {"status": "error"}
    assert "418" not in text and "hub-internal-detail" not in text


@pytest.mark.asyncio
async def test_upstream_transport_error_is_generic_502() -> None:
    """A transport failure never leaks its message to the caller."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    with respx.mock:
        respx.post(UPSTREAM).mock(side_effect=httpx.ConnectError("10.9.8.7 refused"))
        resp = await _post(server, _event(), token)
        text = await resp.get_data(as_text=True)

    assert resp.status_code == 502
    assert "10.9.8.7" not in text


@pytest.mark.asyncio
async def test_logs_only_stage_and_machine_id(capsys: pytest.CaptureFixture[str]) -> None:
    """The accepted-event log line carries stage and machine_id, never the body or message text."""
    import structlog

    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    with structlog.testing.capture_logs() as logs, respx.mock:
        respx.post(UPSTREAM).mock(return_value=httpx.Response(201))
        await _post(server, _event(machine_id="m-9", message="SECRET-TEXT"), token)

    received = [entry for entry in logs if entry["event"] == "boot_event_received"]
    assert len(received) == 1
    assert received[0]["stage"] == "installing" and received[0]["machine_id"] == "m-9"
    assert "SECRET-TEXT" not in json.dumps(logs, default=str)
    assert "payload" not in received[0]


@pytest.mark.asyncio
async def test_store_outage_fails_closed_with_503(fake_redis: FakeRedis) -> None:
    """If the shared store is down the endpoint refuses (503) rather than skipping the check."""
    server = _server()
    token = await server.token_store.issue(MAC, CLIENT_IP)
    fake_redis.fail = True
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        resp = await _post(server, _event(), token)

    assert resp.status_code == 503
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_ipxe_script_embeds_token_stored_only_as_hash(fake_redis: FakeRedis) -> None:
    """Rendering a script mints a token bound to the MAC; only its SHA-256 is in the shared store."""
    server = _server()
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/boot-script/{MAC}").mock(
            return_value=httpx.Response(200, json={"script": "#!ipxe\nboot\n"})
        )
        resp = await server.app.test_client().get(f"/ipxe/{MAC}.ipxe", scope_base=CLIENT)
        script = await resp.get_data(as_text=True)

    token = script.split("set boot_token ")[1].split("\n")[0]
    assert len(token) >= 43
    digest = hashlib.sha256(token.encode()).hexdigest()
    stored = await fake_redis.tokens()
    key = f"gough:boot-token:{MAC}:{CLIENT_IP}"
    assert list(stored) == [key]
    assert token not in json.dumps(stored)
    assert json.loads(stored[key])["machine"] == MAC
    assert await fake_redis.ttl(key) == 30 * 60
    assert json.loads(stored[key])["ip"] == CLIENT_IP
    assert json.loads(stored[key])["hash"] == digest


@pytest.mark.asyncio
async def test_script_without_ipxe_shebang_still_gets_token_and_store_outage_yields_error_script(
    fake_redis: FakeRedis,
) -> None:
    """A hub script lacking #!ipxe is prefixed; if the store is down no script with a missing token is served."""
    server = _server()
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/boot-script/{MAC}").mock(
            return_value=httpx.Response(200, json={"script": "boot\n"})
        )
        ok = await server.ipxe_handler.generate_script(MAC, CLIENT_IP)
        fake_redis.fail = True
        down = await server.ipxe_handler.generate_script(MAC, CLIENT_IP)

    assert ok.startswith("#!ipxe\nset boot_token ") and ok.endswith("boot\n")
    assert "set boot_token" not in down and "Error" in down


@pytest.mark.asyncio
async def test_server_requires_redis_url_without_injected_store() -> None:
    """Constructing the server without REDIS_URL or a store fails loudly."""
    cfg = WorkerConfig(
        api_manager_url=API, worker_api_key="w" * 32, worker_id="w1", dhcp_mode="disabled", dhcp_interface="eth0"
    )

    with pytest.raises(ValueError, match="REDIS_URL"):
        HTTPBootServer(cfg, EnrollmentManager(cfg))


def test_create_token_store_builds_lazy_redis_client() -> None:
    """The production factory returns a store on a redis.asyncio client without connecting."""
    from worker.boot_tokens import create_token_store

    assert isinstance(create_token_store("redis://localhost:6379/0"), BootTokenStore)
