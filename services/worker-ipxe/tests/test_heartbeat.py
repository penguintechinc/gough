"""Behaviour tests for worker.heartbeat: payload, auth, failure counting, re-enrollment and lifecycle."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import respx
from worker import heartbeat as heartbeat_mod
from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager
from worker.heartbeat import HeartbeatManager

API = "http://api.test"
HB_URL = f"{API}/api/v1/workers/heartbeat"


def _make(**overrides: object) -> HeartbeatManager:
    """Create a HeartbeatManager with a pre-enrolled EnrollmentManager."""
    base: dict[str, object] = {
        "api_manager_url": API,
        "worker_api_key": "hb-key",
        "worker_id": "worker-hb",
        "dhcp_mode": "proxy",
        "dhcp_interface": "eth0",
        "tftp_enabled": True,
        "heartbeat_interval": 30,
    }
    base.update(overrides)
    cfg = WorkerConfig(**base)  # type: ignore[arg-type]
    enrollment = EnrollmentManager(cfg)
    enrollment.session_token = "sess-hb"
    enrollment.enrolled = True
    return HeartbeatManager(cfg, enrollment)


@pytest.mark.asyncio
async def test_successful_heartbeat_payload_and_state() -> None:
    """A 200 heartbeat posts status, service flags and auth headers, and resets failure state."""
    hb = _make()
    hb.consecutive_failures = 3
    with respx.mock(assert_all_called=True) as router:
        route = router.post(HB_URL).mock(return_value=httpx.Response(200, json={}))
        ok = await hb.send_heartbeat()

    assert ok is True
    assert hb.consecutive_failures == 0
    assert hb.last_heartbeat is not None

    req = route.calls.last.request
    assert req.headers["X-Worker-API-Key"] == "hb-key"
    assert req.headers["X-Worker-Session-Token"] == "sess-hb"
    body = httpx.Response(200, content=req.content).json()
    assert body["worker_id"] == "worker-hb"
    assert body["status"] == "healthy"
    assert body["services"] == {"dhcp": True, "tftp": True, "http": True}
    assert "T" in body["timestamp"]  # ISO-8601


@pytest.mark.asyncio
async def test_heartbeat_reports_dhcp_and_tftp_disabled() -> None:
    """DHCP disabled and TFTP disabled are reflected as False in services."""
    hb = _make(dhcp_mode="disabled", tftp_enabled=False)
    with respx.mock:
        route = respx.post(HB_URL).mock(return_value=httpx.Response(200, json={}))
        await hb.send_heartbeat()

    body = httpx.Response(200, content=route.calls.last.request.content).json()
    assert body["services"] == {"dhcp": False, "tftp": False, "http": True}


@pytest.mark.asyncio
async def test_non_200_counts_failure_and_returns_false() -> None:
    """A 503 is a failed heartbeat and increments the consecutive failure counter."""
    hb = _make()
    with respx.mock:
        respx.post(HB_URL).mock(return_value=httpx.Response(503))
        ok = await hb.send_heartbeat()

    assert ok is False
    assert hb.consecutive_failures == 1
    assert hb.last_heartbeat is None


@pytest.mark.asyncio
async def test_connect_error_counts_failure() -> None:
    """Connection refused increments failures and returns False without raising."""
    hb = _make()
    with respx.mock:
        respx.post(HB_URL).mock(side_effect=httpx.ConnectError("refused"))
        ok = await hb.send_heartbeat()
        ok_again = await hb.send_heartbeat()

    assert ok is False and ok_again is False
    assert hb.consecutive_failures == 2


@pytest.mark.asyncio
async def test_unexpected_exception_counts_failure() -> None:
    """Non-connect errors are also counted as failures rather than escaping the loop."""
    hb = _make()
    with respx.mock:
        respx.post(HB_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        ok = await hb.send_heartbeat()

    assert ok is False
    assert hb.consecutive_failures == 1


@pytest.mark.asyncio
async def test_success_after_failures_resets_counter() -> None:
    """One success after failures clears the consecutive failure count."""
    hb = _make()
    with respx.mock:
        respx.post(HB_URL).mock(
            side_effect=[httpx.Response(500), httpx.Response(200, json={})]
        )
        await hb.send_heartbeat()
        assert hb.consecutive_failures == 1
        await hb.send_heartbeat()

    assert hb.consecutive_failures == 0


@pytest.mark.asyncio
async def test_loop_reenrolls_after_five_consecutive_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five consecutive failures mark the worker unenrolled, re-run enrollment and reset the counter."""
    hb = _make()
    enroll_seen_enrolled: list[bool] = []

    async def fake_enroll_with_retry() -> bool:
        """Record the enrolled flag at re-enrollment time and report success."""
        enroll_seen_enrolled.append(hb.enrollment.enrolled)
        return True

    hb.enrollment.enroll_with_retry = fake_enroll_with_retry  # type: ignore[method-assign]
    hb.running = True
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record the heartbeat interval and stop the loop after five iterations."""
        sleeps.append(seconds)
        if len(sleeps) >= 5:
            hb.running = False

    monkeypatch.setattr(heartbeat_mod, "asyncio", SimpleNamespace(sleep=fake_sleep))
    with respx.mock:
        route = respx.post(HB_URL).mock(return_value=httpx.Response(500))
        await hb.heartbeat_loop()

    assert route.call_count == 5
    assert enroll_seen_enrolled == [False]
    assert hb.consecutive_failures == 0
    assert sleeps == [30, 30, 30, 30, 30]


@pytest.mark.asyncio
async def test_loop_does_not_reenroll_while_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy loop never triggers enrollment and sleeps the configured interval."""
    hb = _make(heartbeat_interval=7)
    enroll_calls: list[int] = []

    async def fake_enroll_with_retry() -> bool:
        """Count unexpected re-enrollment calls."""
        enroll_calls.append(1)
        return True

    hb.enrollment.enroll_with_retry = fake_enroll_with_retry  # type: ignore[method-assign]
    hb.running = True
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record the interval and stop after two healthy beats."""
        sleeps.append(seconds)
        if len(sleeps) == 2:
            hb.running = False

    monkeypatch.setattr(heartbeat_mod, "asyncio", SimpleNamespace(sleep=fake_sleep))
    with respx.mock:
        route = respx.post(HB_URL).mock(return_value=httpx.Response(200, json={}))
        await hb.heartbeat_loop()

    assert route.call_count == 2
    assert enroll_calls == []
    assert sleeps == [7, 7]


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_cancels_task() -> None:
    """start() spawns one loop task; a second start is a no-op; stop() cancels and clears running."""
    hb = _make(heartbeat_interval=3600)
    with respx.mock:
        respx.post(HB_URL).mock(return_value=httpx.Response(200, json={}))
        await hb.start()
        first_task = hb.task
        await hb.start()  # already running: must not replace the task

        assert hb.running is True
        assert hb.task is first_task
        for _ in range(100):  # poll until the loop has sent its first beat
            if hb.last_heartbeat is not None:
                break
            await asyncio.sleep(0)
        assert hb.last_heartbeat is not None

        await hb.stop()

    assert hb.running is False
    assert first_task is not None and first_task.cancelled()


@pytest.mark.asyncio
async def test_stop_when_not_running_is_noop() -> None:
    """stop() on a never-started manager does nothing and leaves no task."""
    hb = _make()

    await hb.stop()

    assert hb.running is False
    assert hb.task is None
