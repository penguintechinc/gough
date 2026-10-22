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
    return await server.app.test_client().post("/boot-event", json=body, headers=headers, **kw)


@pytest.mark.asyncio
async def test_valid_token_forwards_only_validated_fields_and_returns_202() -> None:
    """A good token + valid body forwards exactly the validated fields with the worker credentials."""
    server = _server()
    token = await server.token_store.issue(MAC)
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
@pytest.mark.parametrize("token", [None, "", "x" * 129, "not-a-real-token"])
async def test_missing_or_unknown_token_is_401_and_never_forwarded(token: str | None) -> None:
    """Without a valid token the hub is never contacted (no confused-deputy forwarding)."""
    server = _server()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        resp = await _post(server, _event(), token)

    assert resp.status_code == 401
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_token_for_other_machine_is_rejected_and_burned() -> None:
    """A token minted for machine A cannot report for machine B, and the attempt consumes it."""
    server = _server()
    token = await server.token_store.issue(MAC)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        wrong = await _post(server, _event(mac=OTHER_MAC), token)
        retry = await _post(server, _event(), token)

    assert wrong.status_code == 401
    assert retry.status_code == 401
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_token_is_single_use() -> None:
    """Replaying a token after a successful event is refused."""
    server = _server()
    token = await server.token_store.issue(MAC)
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
    token = await server.token_store.issue(MAC)
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
    token = await server.token_store.issue(MAC)
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
    token = await server.token_store.issue(MAC)

    resp = await server.app.test_client().post(
        "/boot-event", data="not json", headers={"X-Boot-Token": token, "Content-Type": "text/plain"}
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_oversize_body_is_rejected() -> None:
    """Bodies over the size cap never reach validation or the hub."""
    server = _server()
    token = await server.token_store.issue(MAC)
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
    token = await server.token_store.issue(MAC)
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
    token = await server.token_store.issue(MAC)
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
    token = await server.token_store.issue(MAC)
    with structlog.testing.capture_logs() as logs, respx.mock:
        respx.post(UPSTREAM).mock(return_value=httpx.Response(201))
        await _post(server, _event(machine_id="m-9", message="SECRET-TEXT"), token)

    received = [entry for entry in logs if entry["event"] == "boot_event_received"]
    assert len(received) == 1
    assert received[0]["stage"] == "installing" and received[0]["machine_id"] == "m-9"
    assert "SECRET-TEXT" not in json.dumps(logs, default=str)
    assert "payload" not in received[0]


@pytest.mark.asyncio
async def test_rate_limit_per_source_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requests beyond the per-IP limit get 429 even with garbage tokens; hub never contacted."""
    monkeypatch.setattr(http_mod, "BOOT_EVENT_RATE_LIMIT", 3)
    server = _server()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPSTREAM).mock(return_value=httpx.Response(201))
        codes = [(await _post(server, _event(), "bad")).status_code for _ in range(5)]

    assert codes == [401, 401, 401, 429, 429]
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_store_outage_fails_closed_with_503(fake_redis: FakeRedis) -> None:
    """If the shared store is down the endpoint refuses (503) rather than skipping the check."""
    server = _server()
    token = await server.token_store.issue(MAC)
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
        resp = await server.app.test_client().get(f"/ipxe/{MAC}.ipxe")
        script = await resp.get_data(as_text=True)

    token = script.split("set boot_token ")[1].split("\n")[0]
    assert len(token) >= 43
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert list(fake_redis.data) == [f"gough:boot-token:{digest}"]
    assert token not in json.dumps(fake_redis.data)
    assert json.loads(next(iter(fake_redis.data.values())))["machine"] == MAC
    assert fake_redis.ttls[f"gough:boot-token:{digest}"] == 30 * 60


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
        ok = await server.ipxe_handler.generate_script(MAC)
        fake_redis.fail = True
        down = await server.ipxe_handler.generate_script(MAC)

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
