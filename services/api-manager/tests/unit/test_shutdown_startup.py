"""Tests for graceful serve/shutdown wiring, audit NATS publish shape, and startup fail-loud.

Signals are real (os.kill to this process), the server is a real hypercorn
serve on a free loopback port, and the startup test boots the real create_app
against a closed Redis port (no mocks on the Redis path).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import uuid
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import REGISTRY
from quart import Quart

from app.audit import AuditEvent, AuditEventType, AuditLogger, AuditSeverity, drain_audit_publish_tasks
from app.lifecycle import (
    build_hypercorn_config,
    install_shutdown_signals,
    remove_shutdown_signals,
    serve_app,
)
from app.rate_limit import RateLimiterBackendUnavailable

CLOSED_REDIS = "redis://127.0.0.1:1/0"


def _sample(name: str) -> float:
    """Read an unlabelled prometheus sample (0.0 if absent)."""
    value = REGISTRY.get_sample_value(name)
    return 0.0 if value is None else value


def _free_port() -> int:
    """Return a loopback TCP port that is free at call time."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Signals and serve loop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_shutdown_signal_sets_event(sig: signal.Signals) -> None:
    """A real SIGTERM/SIGINT to this process sets the shutdown event."""

    async def run() -> bool:
        """Install handlers, deliver the signal, and wait for the event."""
        event = asyncio.Event()
        install_shutdown_signals(event)
        try:
            os.kill(os.getpid(), sig)
            await asyncio.wait_for(event.wait(), timeout=2.0)
            return event.is_set()
        finally:
            remove_shutdown_signals()

    assert asyncio.run(run()) is True


def test_serve_app_runs_lifespan_and_stops_on_event(tmp_path: Path) -> None:
    """serve_app drives before/after_serving and returns once the event is set."""
    app = Quart(__name__)
    marks: list[str] = []

    @app.before_serving
    async def _start() -> None:
        """Record that startup ran."""
        marks.append("before")

    @app.after_serving
    async def _stop() -> None:
        """Record that teardown ran."""
        marks.append("after")

    @app.route("/ping")
    async def ping() -> dict[str, str]:
        """Liveness probe used to confirm the server is actually serving."""
        return {"ok": "yes"}

    port = _free_port()
    config = build_hypercorn_config(f"127.0.0.1:{port}", graceful_timeout=5.0)
    event = asyncio.Event()

    async def run() -> None:
        """Serve, probe over HTTP, then request shutdown and await serve_app."""
        server = asyncio.ensure_future(serve_app(app, config, shutdown_event=event))
        reader, writer = None, None
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.05)
        assert writer is not None, "server never accepted connections"
        writer.write(b"GET /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        await writer.drain()
        body = await asyncio.wait_for(reader.read(), timeout=5.0)
        writer.close()
        assert body.startswith(b"HTTP/1.1 200 ")
        event.set()
        await asyncio.wait_for(server, timeout=10.0)

    asyncio.run(run())

    assert marks == ["before", "after"]


# ---------------------------------------------------------------------------
# Audit NATS publish shape
# ---------------------------------------------------------------------------


class _FakeNats:
    """Stub NATS client with nats-py's exact publish signature; records calls."""

    def __init__(self) -> None:
        """Start with no recorded publishes."""
        self.calls: list[dict[str, Any]] = []

    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        reply: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        """Record a publish; a TypeError here means the call shape is not nats-py's."""
        self.calls.append({"subject": subject, "payload": payload, "reply": reply, "headers": headers})


def test_audit_publish_matches_nats_py_signature() -> None:
    """Publish uses nats-py kwargs only, with bytes JSON payload carrying the tenant."""
    fake = _FakeNats()
    app = Quart(__name__)
    app.config["AUDIT_RECORDING_ENABLED"] = False
    app.nats_client = fake  # type: ignore[attr-defined]
    logger = AuditLogger(app)
    event = AuditEvent(
        event_type=AuditEventType.AUTH_LOGIN,
        severity=AuditSeverity.INFO,
        message="Login",
    )
    failures_before = _sample("gough_audit_nats_publish_failures_total")

    async def run() -> None:
        """Schedule the publish and drain it."""
        async with app.app_context():
            logger._publish_audit_event_nats(event)
            await drain_audit_publish_tasks(timeout=2.0)

    asyncio.run(run())

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["subject"] == "gough.audit.entry-written"
    assert isinstance(call["payload"], bytes)
    body = json.loads(call["payload"].decode("utf-8"))
    assert body["tenant_id"] == "__default__"
    assert body["event_id"] == event.event_id
    assert _sample("gough_audit_nats_publish_failures_total") == failures_before


# ---------------------------------------------------------------------------
# Startup-level fail-loud (real create_app, closed Redis port)
# ---------------------------------------------------------------------------


def _startup_config(tmp_path: Path, **overrides: object) -> type:
    """Build a Config subclass for create_app: sqlite DB, no gRPC, Redis on a closed port."""
    from app.config import Config

    db_path = tmp_path / f"startup-{uuid.uuid4().hex}.db"
    attrs: dict[str, object] = {
        "TESTING": True,
        "DEBUG": False,
        "GRPC_ENABLED": False,
        "DB_TYPE": "sqlite",
        "DB_NAME": str(db_path),
        "AUDIT_ENABLED": False,
        "RATE_LIMIT_ENABLED": True,
        "RATE_LIMIT_REDIS_URL": CLOSED_REDIS,
        "REDIS_URL": CLOSED_REDIS,
        "REDIS_SOCKET_CONNECT_TIMEOUT": 0.2,
        "REDIS_SOCKET_TIMEOUT": 0.2,
    }
    attrs.update(overrides)
    return type("StartupTestConfig", (Config,), attrs)


def test_create_app_startup_fails_on_unreachable_redis_without_fallback(tmp_path: Path) -> None:
    """Configured Redis down + no fallback flag: startup raises instead of degrading silently."""
    from app import create_app

    config_cls = _startup_config(tmp_path, RATE_LIMIT_ALLOW_MEMORY_FALLBACK=False)

    async def run() -> None:
        """Create the app and run its before_serving hooks as the server would."""
        app = await create_app(config_cls)
        await app.startup()

    with pytest.raises(RateLimiterBackendUnavailable):
        asyncio.run(run())


def test_create_app_starts_in_memory_with_explicit_fallback(tmp_path: Path) -> None:
    """Explicit fallback flag: app starts, gough_rate_limit_degraded=1, teardown runs cleanly."""
    from app import create_app

    config_cls = _startup_config(tmp_path, RATE_LIMIT_ALLOW_MEMORY_FALLBACK=True)

    async def run() -> Quart:
        """Start the app, then run after_serving teardown."""
        app = await create_app(config_cls)
        await app.startup()
        await app.shutdown()
        return app

    app = asyncio.run(run())

    assert app is not None
    assert _sample("gough_rate_limit_degraded") == 1.0
