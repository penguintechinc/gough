"""Behaviour tests for worker.enrollment: request shape, auth headers, retry/backoff and errors."""

from types import SimpleNamespace

import httpx
import pytest
import respx
from worker import enrollment as enrollment_mod
from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager

API = "http://api.test"
ENROLL_URL = f"{API}/api/v1/workers/enroll"


def _cfg(**overrides: object) -> WorkerConfig:
    """Build a WorkerConfig pointed at the test API with fast retry settings."""
    base: dict[str, object] = {
        "api_manager_url": API,
        "worker_api_key": "api-key-123",
        "worker_id": "worker-7",
        "dhcp_mode": "full",
        "dhcp_interface": "eth1",
        "tftp_enabled": True,
        "tftp_port": 69,
        "http_port": 8080,
        "enrollment_retry_interval": 10,
        "max_enrollment_retries": 60,
    }
    base.update(overrides)
    return WorkerConfig(**base)  # type: ignore[arg-type]


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace enrollment's asyncio.sleep with a recorder so backoff is asserted without waiting."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record the requested delay instead of sleeping."""
        waits.append(seconds)

    monkeypatch.setattr(enrollment_mod, "asyncio", SimpleNamespace(sleep=fake_sleep))
    return waits


@pytest.mark.asyncio
async def test_enroll_success_posts_expected_body_and_headers() -> None:
    """A 200 enrollment POSTs the capability payload with the API key and stores the session token."""
    mgr = EnrollmentManager(_cfg())
    with respx.mock(assert_all_called=True) as router:
        route = router.post(ENROLL_URL).mock(
            return_value=httpx.Response(
                200, json={"session_token": "tok-abcdefghijklmnopqrstuv"}
            )
        )
        ok = await mgr.enroll()

    assert ok is True
    assert mgr.enrolled is True
    assert mgr.session_token == "tok-abcdefghijklmnopqrstuv"

    sent = route.calls.last.request
    assert sent.headers["X-Worker-API-Key"] == "api-key-123"
    assert sent.headers["Content-Type"] == "application/json"
    body = httpx.Response(200, content=sent.content).json()
    assert body == {
        "worker_id": "worker-7",
        "worker_type": "ipxe",
        "capabilities": {
            "dhcp_mode": "full",
            "tftp_enabled": True,
            "http_boot_enabled": True,
            "power_management": ["ipmi", "redfish", "wol"],
        },
        "network": {"interface": "eth1", "http_port": 8080, "tftp_port": 69},
    }


@pytest.mark.asyncio
async def test_enroll_reports_tftp_disabled_in_capabilities() -> None:
    """Capabilities reflect TFTP being disabled in config."""
    mgr = EnrollmentManager(_cfg(tftp_enabled=False, dhcp_mode="disabled"))
    with respx.mock(assert_all_called=True) as router:
        route = router.post(ENROLL_URL).mock(return_value=httpx.Response(200, json={}))
        await mgr.enroll()

    body = httpx.Response(200, content=route.calls.last.request.content).json()
    assert body["capabilities"]["tftp_enabled"] is False
    assert body["capabilities"]["dhcp_mode"] == "disabled"


@pytest.mark.asyncio
async def test_enroll_200_without_token_marks_enrolled_with_empty_token() -> None:
    """A 200 with no session_token still counts as enrolled but yields empty auth token header."""
    mgr = EnrollmentManager(_cfg())
    with respx.mock:
        respx.post(ENROLL_URL).mock(return_value=httpx.Response(200, json={"other": 1}))
        ok = await mgr.enroll()

    assert ok is True
    assert mgr.session_token is None
    assert mgr.get_auth_headers()["X-Worker-Session-Token"] == ""


@pytest.mark.asyncio
async def test_enroll_non_200_returns_false_and_stays_unenrolled() -> None:
    """A 401 from api-manager fails enrollment and leaves no token behind."""
    mgr = EnrollmentManager(_cfg())
    with respx.mock:
        respx.post(ENROLL_URL).mock(return_value=httpx.Response(401, text="bad key"))
        ok = await mgr.enroll()

    assert ok is False
    assert mgr.enrolled is False
    assert mgr.session_token is None


@pytest.mark.asyncio
async def test_enroll_connect_error_returns_false() -> None:
    """A connection failure is reported as a failed enrollment, not raised."""
    mgr = EnrollmentManager(_cfg())
    with respx.mock:
        respx.post(ENROLL_URL).mock(side_effect=httpx.ConnectError("refused"))
        ok = await mgr.enroll()

    assert ok is False
    assert mgr.enrolled is False


@pytest.mark.asyncio
async def test_enroll_unexpected_exception_returns_false() -> None:
    """Any other transport error (e.g. timeout) is logged and treated as a failed attempt."""
    mgr = EnrollmentManager(_cfg())
    with respx.mock:
        respx.post(ENROLL_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        ok = await mgr.enroll()

    assert ok is False
    assert mgr.enrolled is False


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two failures then a success returns True and backs off 10s then 20s between attempts."""
    waits = _record_sleeps(monkeypatch)
    mgr = EnrollmentManager(_cfg())
    responses = [
        httpx.Response(503),
        httpx.ConnectError("down"),
        httpx.Response(200, json={"session_token": "t"}),
    ]
    with respx.mock:
        route = respx.post(ENROLL_URL).mock(side_effect=responses)
        ok = await mgr.enroll_with_retry()

    assert ok is True
    assert route.call_count == 3
    assert waits == [10, 20]
    assert mgr.session_token == "t"


@pytest.mark.asyncio
async def test_retry_gives_up_without_sleeping_after_final_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With max retries=3 and all attempts failing: 3 calls, 2 waits, no wait after the last attempt."""
    waits = _record_sleeps(monkeypatch)
    mgr = EnrollmentManager(_cfg(max_enrollment_retries=3))
    with respx.mock:
        route = respx.post(ENROLL_URL).mock(return_value=httpx.Response(500))
        ok = await mgr.enroll_with_retry()

    assert ok is False
    assert mgr.enrolled is False
    assert route.call_count == 3
    assert waits == [10, 20]


@pytest.mark.asyncio
async def test_retry_backoff_is_capped_at_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backoff doubles per attempt, the exponent saturates at 5, and each wait is capped at 60s."""
    waits = _record_sleeps(monkeypatch)
    mgr = EnrollmentManager(
        _cfg(enrollment_retry_interval=10, max_enrollment_retries=9)
    )
    with respx.mock:
        respx.post(ENROLL_URL).mock(return_value=httpx.Response(500))
        ok = await mgr.enroll_with_retry()

    assert ok is False
    # 8 waits between 9 attempts: 10, 20, 40, 60(cap of 80), 60(cap of 160), 60, 60, 60
    assert waits == [10, 20, 40, 60, 60, 60, 60, 60]


@pytest.mark.asyncio
async def test_retry_zero_attempts_makes_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_enrollment_retries=0 never contacts api-manager and reports failure."""
    waits = _record_sleeps(monkeypatch)
    mgr = EnrollmentManager(_cfg(max_enrollment_retries=0))
    with respx.mock(assert_all_called=False) as router:
        route = router.post(ENROLL_URL).mock(return_value=httpx.Response(200, json={}))
        ok = await mgr.enroll_with_retry()

    assert ok is False
    assert route.call_count == 0
    assert waits == []


def test_auth_headers_before_and_after_enrollment() -> None:
    """Auth headers carry the API key always, and the session token once one exists."""
    mgr = EnrollmentManager(_cfg())
    assert mgr.get_auth_headers() == {
        "X-Worker-API-Key": "api-key-123",
        "X-Worker-Session-Token": "",
    }

    mgr.session_token = "sess-9"
    assert mgr.get_auth_headers() == {
        "X-Worker-API-Key": "api-key-123",
        "X-Worker-Session-Token": "sess-9",
    }
