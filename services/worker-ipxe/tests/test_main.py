"""Behaviour tests for worker.main lifecycle: start/stop ordering, enrollment gating and service failure."""

import asyncio
import os
import signal

import pytest
import structlog
from worker import main as main_mod
from worker.config import WorkerConfig
from worker.main import WorkerIPXE, main

Events = list[str]


def _cfg(**overrides: object) -> WorkerConfig:
    """Build a WorkerConfig with metrics on, TFTP on and proxy DHCP."""
    base: dict[str, object] = {
        "api_manager_url": "http://api.test",
        "worker_api_key": "k",
        "worker_id": "w-main",
        "dhcp_mode": "proxy",
        "dhcp_interface": "eth0",
        "tftp_enabled": True,
        "metrics_enabled": True,
        "metrics_port": 9191,
    }
    base.update(overrides)
    return WorkerConfig(**base)  # type: ignore[arg-type]


class FakeService:
    """Stand-in for a service class: records init/start/stop and blocks in start until stopped."""

    def __init__(
        self, name: str, events: Events, fail_with: Exception | None = None
    ) -> None:
        """Remember the event log and optional failure to raise from start()."""
        self.name = name
        self.events = events
        self.fail_with = fail_with
        self.stop_event = asyncio.Event()

    async def start(self) -> None:
        """Record start; raise the configured failure or block until stop()."""
        self.events.append(f"{self.name}.start")
        if self.fail_with is not None:
            raise self.fail_with
        await self.stop_event.wait()

    async def stop(self) -> None:
        """Record stop and release start()."""
        self.events.append(f"{self.name}.stop")
        self.stop_event.set()


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    events: Events,
    *,
    enroll_ok: bool = True,
    fail: dict[str, Exception] | None = None,
) -> dict[str, type]:
    """Replace every external service class and side effect in worker.main with recorders."""
    failures = fail or {}

    def make_factory(name: str) -> type:
        """Build a class that constructs a FakeService and logs construction order."""

        class Factory:
            """Constructed in place of the real service; instances are FakeService."""

            def __new__(cls, config: WorkerConfig, *args: object, **kwargs: object) -> FakeService:  # type: ignore[misc]
                """Record construction and return a FakeService for this name."""
                events.append(f"{name}.init")
                return FakeService(name, events, failures.get(name))

        return Factory

    factories = {
        "tftp": make_factory("tftp"),
        "http": make_factory("http"),
        "dhcp_proxy": make_factory("dhcp_proxy"),
        "dhcp_full": make_factory("dhcp_full"),
    }
    monkeypatch.setattr(main_mod, "TFTPServer", factories["tftp"])
    monkeypatch.setattr(main_mod, "HTTPBootServer", factories["http"])
    monkeypatch.setattr(main_mod, "DHCPProxyServer", factories["dhcp_proxy"])
    monkeypatch.setattr(main_mod, "DHCPFullServer", factories["dhcp_full"])

    async def fake_enroll(self: object) -> bool:
        """Record enrollment and report the configured outcome."""
        events.append("enroll")
        return enroll_ok

    monkeypatch.setattr(main_mod.EnrollmentManager, "enroll_with_retry", fake_enroll)

    def fake_metrics(port: int) -> None:
        """Record that the Prometheus server was started on the given port."""
        events.append(f"metrics:{port}")

    monkeypatch.setattr(main_mod, "start_http_server", fake_metrics)
    return factories


def _wire_heartbeat(worker: WorkerIPXE, events: Events) -> None:
    """Replace heartbeat start/stop on the instance with recorders."""

    async def hb_start() -> None:
        """Record heartbeat start."""
        events.append("heartbeat.start")

    async def hb_stop() -> None:
        """Record heartbeat stop."""
        events.append("heartbeat.stop")

    worker.heartbeat.start = hb_start  # type: ignore[method-assign]
    worker.heartbeat.stop = hb_stop  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_start_orders_enrollment_then_metrics_heartbeat_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start enrolls first, then metrics, heartbeat, TFTP, HTTP and the proxy DHCP service."""
    events: Events = []
    _install_fakes(monkeypatch, events)
    worker = WorkerIPXE(_cfg())
    _wire_heartbeat(worker, events)

    await worker.start()
    await asyncio.sleep(0)  # let created service tasks reach their start()
    await worker.stop()

    assert events[:6] == [
        "enroll",
        "metrics:9191",
        "heartbeat.start",
        "tftp.init",
        "http.init",
        "dhcp_proxy.init",
    ]
    assert worker.running is False  # cleared by stop()
    assert isinstance(worker.tftp_server, FakeService)
    assert worker.dhcp_server is not None and worker.dhcp_server.name == "dhcp_proxy"


@pytest.mark.asyncio
async def test_stop_order_is_heartbeat_dhcp_http_tftp_then_tasks_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop tears down heartbeat, then DHCP, then HTTP, then TFTP, and drains all service tasks."""
    events: Events = []
    _install_fakes(monkeypatch, events)
    worker = WorkerIPXE(
        _cfg(
            dhcp_mode="full",
            dhcp_subnet="10.0.0.0/24",
            dhcp_range_start="10.0.0.2",
            dhcp_range_end="10.0.0.9",
            dhcp_gateway="10.0.0.1",
        )
    )
    _wire_heartbeat(worker, events)

    await worker.start()
    await asyncio.sleep(0)
    events.clear()
    await worker.stop()

    assert events == ["heartbeat.stop", "dhcp_full.stop", "http.stop", "tftp.stop"]
    assert len(worker.tasks) == 3
    assert all(task.done() for task in worker.tasks)


@pytest.mark.asyncio
async def test_enrollment_failure_exits_before_starting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If enrollment never succeeds the worker exits with status 1 and starts no services."""
    events: Events = []
    _install_fakes(monkeypatch, events, enroll_ok=False)
    worker = WorkerIPXE(_cfg())
    _wire_heartbeat(worker, events)

    with pytest.raises(SystemExit) as excinfo:
        await worker.start()

    assert excinfo.value.code == 1
    assert events == ["enroll"]
    assert worker.running is False
    assert worker.tftp_server is None and worker.http_server is None


@pytest.mark.asyncio
async def test_metrics_disabled_skips_prometheus_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """METRICS_ENABLED=false never starts the Prometheus HTTP endpoint."""
    events: Events = []
    _install_fakes(monkeypatch, events)
    worker = WorkerIPXE(_cfg(metrics_enabled=False))
    _wire_heartbeat(worker, events)

    await worker.start()
    await worker.stop()

    assert not any(e.startswith("metrics:") for e in events)


@pytest.mark.asyncio
async def test_tftp_disabled_and_dhcp_disabled_start_only_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With TFTP and DHCP disabled only the HTTP boot server (and heartbeat) start."""
    events: Events = []
    _install_fakes(monkeypatch, events)
    worker = WorkerIPXE(_cfg(tftp_enabled=False, dhcp_mode="disabled"))
    _wire_heartbeat(worker, events)

    await worker.start()
    await asyncio.sleep(0)
    await worker.stop()

    assert worker.tftp_server is None
    assert worker.dhcp_server is None
    assert "tftp.init" not in events
    assert "dhcp_proxy.init" not in events and "dhcp_full.init" not in events
    assert "http.init" in events


@pytest.mark.asyncio
async def test_stop_on_unstarted_worker_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping a worker that never started does not touch any service and does not raise."""
    events: Events = []
    _install_fakes(monkeypatch, events)
    worker = WorkerIPXE(_cfg())
    _wire_heartbeat(worker, events)

    await worker.stop()

    assert events == ["heartbeat.stop"]


@pytest.mark.asyncio
async def test_failed_service_task_stops_worker_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service crashing on its own triggers shutdown of everything else and a RuntimeError."""
    events: Events = []
    _install_fakes(monkeypatch, events, fail={"tftp": OSError("port 69 busy")})
    worker = WorkerIPXE(_cfg())
    _wire_heartbeat(worker, events)

    with pytest.raises(RuntimeError, match="service failed: port 69 busy"):
        await worker.run()

    assert "heartbeat.stop" in events
    assert "http.stop" in events
    assert "tftp.stop" in events
    assert worker.running is False


@pytest.mark.asyncio
async def test_service_exiting_normally_is_also_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service whose start() returns on its own is treated as unexpected exit, not silent success."""
    events: Events = []
    _install_fakes(monkeypatch, events)
    worker = WorkerIPXE(_cfg(tftp_enabled=False, dhcp_mode="disabled"))
    _wire_heartbeat(worker, events)

    class QuietHTTP(FakeService):
        """HTTP stand-in whose start() returns immediately."""

        async def start(self) -> None:
            """Return straight away, as a broken serve loop would."""
            self.events.append(f"{self.name}.start")

    monkeypatch.setattr(
        main_mod, "HTTPBootServer", lambda cfg, enr, **kw: QuietHTTP("http", events)
    )

    with pytest.raises(RuntimeError, match="service task exited unexpectedly"):
        await worker.run()

    assert "heartbeat.stop" in events


@pytest.mark.asyncio
async def test_run_shuts_down_on_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGTERM ends run() cleanly after stopping every service."""
    events: Events = []
    _install_fakes(monkeypatch, events)
    worker = WorkerIPXE(_cfg(tftp_enabled=False, dhcp_mode="disabled"))
    _wire_heartbeat(worker, events)

    loop = asyncio.get_running_loop()

    async def send_term() -> None:
        """Deliver SIGTERM to this process once run() is waiting on the stop event."""
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = asyncio.create_task(send_term())
    try:
        await asyncio.wait_for(worker.run(), timeout=5)
    finally:
        await sender
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)

    assert events == [
        "enroll",
        "metrics:9191",
        "heartbeat.start",
        "http.init",
        "http.start",
        "heartbeat.stop",
        "http.stop",
    ]
    assert worker.running is False


def test_main_exits_one_on_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() maps a configuration ValueError to exit status 1 before any service work."""
    monkeypatch.setenv("DHCP_MODE", "bogus")

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(main())

    assert excinfo.value.code == 1


def test_main_exits_one_without_worker_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no WORKER_API_KEY the worker refuses to start (no default credential)."""
    monkeypatch.delenv("WORKER_API_KEY", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://test:6379/0")

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(main())

    assert excinfo.value.code == 1


def test_main_exits_one_when_worker_run_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fatal error from run() is logged and converted into exit status 1."""
    monkeypatch.setenv("DHCP_MODE", "disabled")
    monkeypatch.setenv("TFTP_ENABLED", "False")
    monkeypatch.setenv("METRICS_ENABLED", "False")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("WORKER_API_KEY", "k" * 32)
    monkeypatch.setenv("REDIS_URL", "redis://test:6379/0")

    async def boom(self: WorkerIPXE) -> None:
        """Simulate a fatal runtime failure inside the worker."""
        raise RuntimeError("fatal boot")

    monkeypatch.setattr(WorkerIPXE, "run", boom)
    try:
        with pytest.raises(SystemExit) as excinfo:
            asyncio.run(main())
    finally:
        structlog.reset_defaults()

    assert excinfo.value.code == 1


def test_main_runs_worker_and_returns_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean run() lets main() return normally and passes the parsed config into WorkerIPXE."""
    monkeypatch.setenv("DHCP_MODE", "disabled")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("WORKER_API_KEY", "k" * 32)
    monkeypatch.setenv("REDIS_URL", "redis://test:6379/0")
    seen: list[WorkerConfig] = []

    async def ok(self: WorkerIPXE) -> None:
        """Record the config the worker was built with and finish successfully."""
        seen.append(self.config)

    monkeypatch.setattr(WorkerIPXE, "run", ok)
    try:
        asyncio.run(main())
    finally:
        structlog.reset_defaults()

    assert len(seen) == 1
    assert seen[0].dhcp_mode == "disabled"
