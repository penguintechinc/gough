"""Regression tests for ProxyDHCP datagram dispatch.

Covers the in-flight cap with drop counting, strong task references until
completion, handler exception logging via the done-callback, histogram
observation, and cancellation of outstanding handlers on stop().
"""

import asyncio
import gc

import pytest
import structlog
from prometheus_client import REGISTRY
from worker.config import WorkerConfig
from worker.services.dhcp_proxy import DHCPProxyServer, _load_max_inflight

DROP_METRIC = "gough_ipxe_dhcp_dropped_total"
HANDLE_COUNT_METRIC = "gough_ipxe_dhcp_handle_seconds_count"
ADDR = ("10.0.0.5", 68)


def _make_server(monkeypatch: pytest.MonkeyPatch, max_inflight: int) -> DHCPProxyServer:
    """Create a DHCPProxyServer with a bounded in-flight cap."""
    monkeypatch.setenv("DHCP_MAX_INFLIGHT", str(max_inflight))
    cfg = WorkerConfig(
        api_manager_url="http://api.test",
        worker_api_key="k",
        worker_id="w",
        dhcp_mode="proxy",
        dhcp_interface="eth0",
    )
    return DHCPProxyServer(cfg)


def _metric(name: str) -> float:
    """Return a Prometheus sample value, or 0.0 if the series does not exist yet."""
    return REGISTRY.get_sample_value(name) or 0.0


async def _drain(server: DHCPProxyServer) -> None:
    """Yield to the loop until every in-flight task has completed and its callback ran."""
    for _ in range(10):
        if not server._tasks:
            return
        await asyncio.gather(*list(server._tasks), return_exceptions=True)
        await asyncio.sleep(0)


def test_load_max_inflight_default_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DHCP_MAX_INFLIGHT defaults to 256 and rejects values below 1."""
    monkeypatch.delenv("DHCP_MAX_INFLIGHT", raising=False)
    assert _load_max_inflight() == 256

    monkeypatch.setenv("DHCP_MAX_INFLIGHT", "0")
    with pytest.raises(ValueError, match="DHCP_MAX_INFLIGHT"):
        _load_max_inflight()


@pytest.mark.asyncio
async def test_inflight_cap_drops_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Datagrams beyond the cap are dropped and increment the dropped counter."""
    server = _make_server(monkeypatch, max_inflight=2)
    gate = asyncio.Event()
    handled: list[bytes] = []

    async def blocking_handle(data: bytes, addr: tuple) -> None:
        """Block until released, recording each completed datagram."""
        await gate.wait()
        handled.append(data)

    server._handle_packet = blocking_handle  # type: ignore[method-assign]
    before = _metric(DROP_METRIC)

    for i in range(5):
        server.dispatch_packet(bytes([i]), ADDR)

    assert len(server._tasks) == 2
    assert _metric(DROP_METRIC) - before == 3

    gate.set()
    await _drain(server)
    assert len(handled) == 2


@pytest.mark.asyncio
async def test_tasks_strongly_referenced_until_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-flight handlers survive garbage collection and the set empties on completion.

    Regression: un-referenced create_task results can be collected mid-flight.
    """
    server = _make_server(monkeypatch, max_inflight=8)
    gate = asyncio.Event()
    finished: list[int] = []

    async def slow_handle(data: bytes, addr: tuple) -> None:
        """Wait on the gate, then mark this datagram finished."""
        await gate.wait()
        finished.append(data[0])

    server._handle_packet = slow_handle  # type: ignore[method-assign]
    server.dispatch_packet(b"\x01", ADDR)
    server.dispatch_packet(b"\x02", ADDR)

    gc.collect()
    assert len(server._tasks) == 2

    gate.set()
    await _drain(server)

    assert sorted(finished) == [1, 2]
    assert server._tasks == set()


@pytest.mark.asyncio
async def test_handler_exception_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler exception is logged by the done-callback and never silently dropped."""
    server = _make_server(monkeypatch, max_inflight=4)

    async def failing_handle(data: bytes, addr: tuple) -> None:
        """Raise to simulate a handler bug."""
        raise RuntimeError("boom")

    server._handle_packet = failing_handle  # type: ignore[method-assign]

    with structlog.testing.capture_logs() as logs:
        server.dispatch_packet(b"\x01", ADDR)
        await _drain(server)

    errors = [e for e in logs if e["event"] == "dhcp_packet_handler_error"]
    assert len(errors) == 1
    assert errors[0]["error"] == "boom"
    assert errors[0]["error_type"] == "RuntimeError"
    assert server._tasks == set()


@pytest.mark.asyncio
async def test_handle_histogram_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each handled datagram increments the handle-duration histogram count."""
    server = _make_server(monkeypatch, max_inflight=4)

    async def quick_handle(data: bytes, addr: tuple) -> None:
        """Return immediately."""
        return None

    server._handle_packet = quick_handle  # type: ignore[method-assign]
    before = _metric(HANDLE_COUNT_METRIC)

    server.dispatch_packet(b"\x01", ADDR)
    server.dispatch_packet(b"\x02", ADDR)
    await _drain(server)

    assert _metric(HANDLE_COUNT_METRIC) - before == 2


@pytest.mark.asyncio
async def test_stop_cancels_inflight_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() cancels and awaits outstanding handlers and leaves no tasks behind."""
    server = _make_server(monkeypatch, max_inflight=4)
    cancelled: list[bool] = []

    async def hanging_handle(data: bytes, addr: tuple) -> None:
        """Hang until cancelled, recording the cancellation."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    server._handle_packet = hanging_handle  # type: ignore[method-assign]
    server.dispatch_packet(b"\x01", ADDR)
    server.dispatch_packet(b"\x02", ADDR)
    await asyncio.sleep(0)
    assert len(server._tasks) == 2

    await server.stop()

    assert len(cancelled) == 2
    assert server._tasks == set()
