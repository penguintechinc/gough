"""Regression tests for api-manager resilience and telemetry fixes.

Covers Redis socket timeouts, fail-loud rate-limiter startup, graceful shutdown
(after_serving teardown and hypercorn graceful timeout), request latency and
bounded route-template labels, and audit NATS publish task retention/failures.
Tests use real Quart apps, real prometheus registry reads and real unreachable
TCP endpoints (port 1) rather than mocks wherever the behavior allows it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prometheus_client import REGISTRY
from quart import Quart, g

from app.audit import (
    _PENDING_PUBLISH_TASKS,
    AuditEvent,
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    drain_audit_publish_tasks,
)
from app.lifecycle import (
    build_hypercorn_config,
    close_runtime_resources,
    redis_client_kwargs,
)
from app.middleware import _mark_request_start, _record_request_metrics
from app.rate_limit import (
    InMemoryStorage,
    RateLimiter,
    RateLimiterBackendUnavailable,
    RedisStorage,
)

# Port 1 on loopback is closed: connecting is refused immediately, a real
# unreachable-Redis path without relying on DNS or network timeouts.
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
UNIMPORTABLE = None


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Read one prometheus sample from the default registry (0.0 if absent)."""
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def _app(**config: object) -> Quart:
    """Build a bare Quart app with rate limiting enabled and the given config."""
    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["RATE_LIMIT_ENABLED"] = True
    app.config.update(config)
    return app


def _metrics_app() -> Quart:
    """Quart app wired exactly like create_app: start timer first, metrics after."""
    app = Quart(__name__)
    app.config["TESTING"] = True
    app.before_request(_mark_request_start)
    app.after_request(_record_request_metrics)

    @app.route("/api/v1/things/<uuid:thing_id>", methods=["GET"])
    async def get_thing(thing_id: uuid.UUID) -> dict[str, str]:
        """Sleep briefly so latency is measurably non-zero."""
        await asyncio.sleep(0.02)
        return {"id": str(thing_id)}

    return app


async def _get(app: Quart, path: str) -> int:
    """Issue a GET through Quart's test client and return the status code."""
    response = await app.test_client().get(path)
    return response.status_code


# ---------------------------------------------------------------------------
# Redis timeouts
# ---------------------------------------------------------------------------


def test_redis_kwargs_default_to_two_second_timeouts() -> None:
    """Defaults bound socket and connect timeouts at 2s with a 30s health check."""
    assert redis_client_kwargs({}) == {
        "socket_timeout": 2.0,
        "socket_connect_timeout": 2.0,
        "health_check_interval": 30,
    }


def test_redis_kwargs_read_env_driven_config() -> None:
    """Config overrides flow into the redis.from_url kwargs."""
    kwargs = redis_client_kwargs({"REDIS_SOCKET_TIMEOUT": 1.5, "REDIS_SOCKET_CONNECT_TIMEOUT": 0.5})
    assert kwargs["socket_timeout"] == 1.5
    assert kwargs["socket_connect_timeout"] == 0.5


def test_rate_limiter_redis_client_created_with_timeouts() -> None:
    """The rate limiter's Redis client is constructed with bounded timeouts."""
    app = _app(
        RATE_LIMIT_REDIS_URL="redis://cache:6379/0",
        REDIS_SOCKET_TIMEOUT=1.5,
        REDIS_SOCKET_CONNECT_TIMEOUT=0.5,
    )
    fake_client = MagicMock()
    with patch("redis.from_url", return_value=fake_client) as from_url:
        limiter = RateLimiter(app)

    from_url.assert_called_once_with(
        "redis://cache:6379/0",
        socket_timeout=1.5,
        socket_connect_timeout=0.5,
        health_check_interval=30,
    )
    fake_client.ping.assert_called_once()
    assert isinstance(limiter._storage, RedisStorage)


# ---------------------------------------------------------------------------
# Fail-loud rate limiter
# ---------------------------------------------------------------------------


def test_unreachable_redis_raises_without_fallback_flag() -> None:
    """Configured-but-down Redis aborts init instead of silently using memory."""
    app = _app(RATE_LIMIT_REDIS_URL=UNREACHABLE_REDIS, REDIS_SOCKET_CONNECT_TIMEOUT=0.2)
    with pytest.raises(RateLimiterBackendUnavailable, match="RATE_LIMIT_ALLOW_MEMORY_FALLBACK"):
        RateLimiter(app)


def test_fallback_flag_uses_memory_and_sets_degraded_gauge() -> None:
    """Explicit opt-in falls back to in-memory storage and raises the degraded gauge."""
    app = _app(
        RATE_LIMIT_REDIS_URL=UNREACHABLE_REDIS,
        REDIS_SOCKET_CONNECT_TIMEOUT=0.2,
        RATE_LIMIT_ALLOW_MEMORY_FALLBACK=True,
    )
    limiter = RateLimiter(app)

    assert isinstance(limiter._storage, InMemoryStorage)
    assert _sample("gough_rate_limit_degraded") == 1.0


def test_memory_only_config_clears_degraded_gauge() -> None:
    """Without a Redis URL the limiter is healthy by definition: gauge reads 0."""
    RateLimiter(_app())
    assert _sample("gough_rate_limit_degraded") == 0.0


def test_missing_redis_package_is_never_swallowed() -> None:
    """ImportError for the redis package propagates even with the fallback flag set."""
    app = _app(RATE_LIMIT_REDIS_URL=UNREACHABLE_REDIS, RATE_LIMIT_ALLOW_MEMORY_FALLBACK=True)
    with patch.dict(sys.modules, {"redis": UNIMPORTABLE}):
        with pytest.raises(ImportError):
            RateLimiter(app)


def test_rate_limiter_close_releases_owned_redis_client() -> None:
    """close() closes the owned sync client and is idempotent."""
    app = _app(RATE_LIMIT_REDIS_URL="redis://cache:6379/0")
    fake_client = MagicMock()
    with patch("redis.from_url", return_value=fake_client):
        limiter = RateLimiter(app)
    limiter.close()
    limiter.close()
    fake_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


def test_close_runtime_resources_releases_everything() -> None:
    """Shutdown cancels gRPC, closes NATS/Redis/rate-limit client and disposes the DB pool."""
    app = Quart(__name__)
    capacity_redis = MagicMock()
    capacity_redis.aclose = AsyncMock()
    nats = MagicMock()
    nats.close = AsyncMock()
    db = MagicMock()
    limiter = MagicMock()
    app.config["CAPACITY_REDIS_CLIENT"] = capacity_redis
    app.config["NATS_CLIENT"] = nats
    app.config["db"] = db
    app.extensions["rate_limiter"] = limiter

    async def run() -> asyncio.Future[None]:
        """Start a long-lived stand-in gRPC task, run teardown, return the task."""
        grpc_task = asyncio.ensure_future(asyncio.sleep(3600))
        app.config["GRPC_TASK"] = grpc_task
        await close_runtime_resources(app)
        return grpc_task

    grpc_task = asyncio.run(run())

    assert grpc_task.cancelled()
    capacity_redis.aclose.assert_awaited_once()
    nats.close.assert_awaited_once()
    limiter.close.assert_called_once()
    db.engine.dispose.assert_called_once()


def test_failing_shutdown_step_is_logged_and_others_still_close(caplog: pytest.LogCaptureFixture) -> None:
    """One resource failing to close is logged at ERROR; remaining resources still close."""
    app = Quart(__name__)
    capacity_redis = MagicMock()
    capacity_redis.aclose = AsyncMock(side_effect=ConnectionError("redis gone"))
    nats = MagicMock()
    nats.close = AsyncMock()
    db = MagicMock()
    app.config["CAPACITY_REDIS_CLIENT"] = capacity_redis
    app.config["NATS_CLIENT"] = nats
    app.config["db"] = db

    with caplog.at_level(logging.ERROR, logger=app.logger.name):
        asyncio.run(close_runtime_resources(app))

    nats.close.assert_awaited_once()
    db.engine.dispose.assert_called_once()
    assert "shutdown step failed: redis" in caplog.text


def test_hypercorn_graceful_timeout_defaults_to_25s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default graceful window is 25s, leaving headroom under the 30s pod grace period."""
    monkeypatch.delenv("GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
    config = build_hypercorn_config("0.0.0.0:5000")
    assert config.graceful_timeout == 25.0
    assert config.bind == ["0.0.0.0:5000"]


def test_hypercorn_graceful_timeout_is_env_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """GRACEFUL_SHUTDOWN_TIMEOUT overrides the default."""
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "10")
    assert build_hypercorn_config("0.0.0.0:5000").graceful_timeout == 10.0


@pytest.mark.parametrize("bad_timeout", [0.0, 30.0, 45.0])
def test_hypercorn_graceful_timeout_must_fit_grace_period(bad_timeout: float) -> None:
    """Timeouts that would outlive terminationGracePeriodSeconds are rejected at startup."""
    with pytest.raises(ValueError, match="graceful shutdown timeout"):
        build_hypercorn_config("0.0.0.0:5000", graceful_timeout=bad_timeout)


# ---------------------------------------------------------------------------
# Request latency and label cardinality
# ---------------------------------------------------------------------------


def test_request_latency_recorded_positive_and_route_template_labelled() -> None:
    """Latency is observed (>0) under the route template, never the raw UUID path."""
    app = _metrics_app()
    thing_id = uuid.uuid4()
    labels = {"method": "GET", "endpoint": "/api/v1/things/<uuid:thing_id>", "status_code": "200"}
    sum_before = _sample("gough_api_request_latency_seconds_sum", labels)
    count_before = _sample("gough_api_request_latency_seconds_count", labels)

    status = asyncio.run(_get(app, f"/api/v1/things/{thing_id}"))

    assert status == 200
    assert _sample("gough_api_request_latency_seconds_count", labels) - count_before == 1
    observed = _sample("gough_api_request_latency_seconds_sum", labels) - sum_before
    assert observed >= 0.02
    raw_label = {"method": "GET", "endpoint": f"/api/v1/things/{thing_id}", "status_code": "200"}
    assert _sample("gough_api_request_latency_seconds_count", raw_label) == 0


def test_unmatched_route_labelled_unmatched() -> None:
    """A 404 (no route matched) uses the bounded literal label 'unmatched'."""
    app = _metrics_app()
    labels = {"method": "GET", "endpoint": "unmatched", "status_code": "404"}
    count_before = _sample("gough_api_request_latency_seconds_count", labels)

    status = asyncio.run(_get(app, f"/no/such/{uuid.uuid4()}"))

    assert status == 404
    assert _sample("gough_api_request_latency_seconds_count", labels) - count_before == 1


def test_metrics_failure_is_counted_and_never_fails_response() -> None:
    """A metrics exception is counted in gough_metrics_record_errors_total, response returned."""
    app = _metrics_app()
    response = MagicMock()
    response.status_code = 200
    before = _sample("gough_metrics_record_errors_total")

    async def run() -> object:
        """Run the after_request hook with the latency histogram forced to fail."""
        async with app.test_request_context("/api/v1/things/1"):
            g.request_started_at = time.perf_counter()
            with patch("app.metrics.api_request_latency_seconds") as latency:
                latency.labels.side_effect = RuntimeError("registry exploded")
                return await _record_request_metrics(response)

    result = asyncio.run(run())

    assert result is response
    assert _sample("gough_metrics_record_errors_total") - before == 1


# ---------------------------------------------------------------------------
# Audit NATS publish
# ---------------------------------------------------------------------------


def _audit_event() -> AuditEvent:
    """Build a minimal login audit event."""
    return AuditEvent(
        event_type=AuditEventType.AUTH_LOGIN,
        severity=AuditSeverity.INFO,
        message="Login",
    )


def _audit_app(publish: object) -> Quart:
    """Quart app with a stub NATS client whose publish is the given coroutine function."""
    app = Quart(__name__)
    app.config["AUDIT_RECORDING_ENABLED"] = False
    nats_client = MagicMock()
    nats_client.publish = publish
    app.nats_client = nats_client
    return app


def test_failed_audit_publish_is_retained_then_counted_and_released() -> None:
    """Publish task is strongly referenced while in flight; failure increments counter."""

    async def failing_publish(**_kwargs: object) -> None:
        """Simulate NATS rejecting the publish."""
        raise RuntimeError("nats down")

    app = _audit_app(failing_publish)
    logger = AuditLogger(app)
    failures_before = _sample("gough_audit_nats_publish_failures_total")

    async def run() -> None:
        """Schedule one publish, verify retention, then drain."""
        async with app.app_context():
            pending_before = set(_PENDING_PUBLISH_TASKS)
            logger._publish_audit_event_nats(_audit_event())
            new_tasks = _PENDING_PUBLISH_TASKS - pending_before
            assert len(new_tasks) == 1
            (task,) = new_tasks
            assert not task.done()
            await drain_audit_publish_tasks(timeout=2.0)
            assert task.done()
            assert task not in _PENDING_PUBLISH_TASKS

    asyncio.run(run())

    assert _sample("gough_audit_nats_publish_failures_total") - failures_before == 1


def test_successful_audit_publish_does_not_increment_failures() -> None:
    """Successful publish is awaited and leaves the failure counter untouched."""
    publish = AsyncMock()
    app = _audit_app(publish)
    logger = AuditLogger(app)
    failures_before = _sample("gough_audit_nats_publish_failures_total")

    async def run() -> None:
        """Schedule one publish and drain it."""
        async with app.app_context():
            logger._publish_audit_event_nats(_audit_event())
            await drain_audit_publish_tasks(timeout=2.0)

    asyncio.run(run())

    publish.assert_awaited_once()
    assert _sample("gough_audit_nats_publish_failures_total") == failures_before


def test_drain_cancels_publish_that_exceeds_timeout() -> None:
    """A publish still pending after the drain budget is cancelled, not leaked."""

    async def hanging_publish(**_kwargs: object) -> None:
        """Simulate a NATS publish that never completes."""
        await asyncio.sleep(3600)

    app = _audit_app(hanging_publish)
    logger = AuditLogger(app)

    async def run() -> asyncio.Task[None]:
        """Schedule a hanging publish, drain with a tiny budget, return the task."""
        async with app.app_context():
            pending_before = set(_PENDING_PUBLISH_TASKS)
            logger._publish_audit_event_nats(_audit_event())
            (task,) = _PENDING_PUBLISH_TASKS - pending_before
            await drain_audit_publish_tasks(timeout=0.05)
            return task

    task = asyncio.run(run())

    assert task.cancelled()
