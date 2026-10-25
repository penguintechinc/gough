"""Startup/shutdown helpers shared by the app factory and the server entrypoint.

Provides the Redis socket-timeout kwargs, the hypercorn config with a bounded
graceful-shutdown window, and the ordered teardown of runtime resources.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from hypercorn.asyncio import serve as hypercorn_serve
from hypercorn.config import Config as HypercornConfig
from quart import Quart

# Kubernetes terminationGracePeriodSeconds in the chart; the graceful window must stay under it.
_TERMINATION_GRACE_SECONDS = 30.0


def redis_client_kwargs(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return redis.from_url kwargs bounding every socket operation.

    Timeouts come from REDIS_SOCKET_TIMEOUT / REDIS_SOCKET_CONNECT_TIMEOUT
    (default 2s each); health_check_interval=30 detects dead idle connections.
    """
    return {
        "socket_timeout": float(config.get("REDIS_SOCKET_TIMEOUT", 2.0)),
        "socket_connect_timeout": float(
            config.get("REDIS_SOCKET_CONNECT_TIMEOUT", 2.0)
        ),
        "health_check_interval": 30,
    }


def build_hypercorn_config(
    bind: str, graceful_timeout: float | None = None
) -> HypercornConfig:
    """Build a hypercorn config whose graceful shutdown fits the pod grace period.

    Uses ``graceful_timeout`` if given, else GRACEFUL_SHUTDOWN_TIMEOUT from the
    environment (default 25s). Raises ValueError unless 0 < timeout < 30s so a
    misconfiguration fails at startup instead of getting the pod SIGKILLed.
    """
    import os

    if graceful_timeout is None:
        graceful_timeout = float(os.getenv("GRACEFUL_SHUTDOWN_TIMEOUT", "25"))
    if not 0 < graceful_timeout < _TERMINATION_GRACE_SECONDS:
        raise ValueError(
            f"graceful shutdown timeout must be in (0, {_TERMINATION_GRACE_SECONDS}); "
            f"got {graceful_timeout}"
        )
    config = HypercornConfig()
    config.bind = [bind]
    config.graceful_timeout = graceful_timeout
    return config


async def _run_step(app: Quart, name: str, step: Callable[[], Awaitable[None]]) -> None:
    """Run one teardown step; log ERROR with traceback on failure and continue."""
    try:
        await step()
    except Exception:
        app.logger.error("shutdown step failed: %s", name, exc_info=True)


async def _cancel_grpc_task(app: Quart) -> None:
    """Cancel the background gRPC server task and wait for it to finish."""
    task = app.config.get("GRPC_TASK")
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _drain_audit_tasks(app: Quart) -> None:
    """Wait briefly for in-flight audit NATS publishes, then cancel the rest."""
    from .audit import drain_audit_publish_tasks

    await drain_audit_publish_tasks()


async def _close_nats(app: Quart) -> None:
    """Close the NATS connection if one was attached to the app."""
    client = app.config.get("NATS_CLIENT") or getattr(app, "nats_client", None)
    if client is not None:
        await client.close()


async def _close_redis(app: Quart) -> None:
    """Close the async capacity-cache Redis client and the sync rate-limit client."""
    capacity_redis = app.config.get("CAPACITY_REDIS_CLIENT")
    if capacity_redis is not None:
        await capacity_redis.aclose()
    limiter = getattr(app, "extensions", {}).get("rate_limiter")
    if limiter is not None:
        limiter.close()


async def _dispose_db(app: Quart) -> None:
    """Dispose the penguin-dal SQLAlchemy engine pool off the event loop."""
    db = app.config.get("db")
    if db is not None:
        await asyncio.to_thread(db.engine.dispose)


_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def install_shutdown_signals(event: asyncio.Event) -> None:
    """Set ``event`` on SIGINT/SIGTERM so the server begins graceful shutdown.

    Must be called from within the running loop. Replaces the default handlers
    for those signals until :func:`remove_shutdown_signals` is called.
    """
    loop = asyncio.get_running_loop()
    for sig in _SHUTDOWN_SIGNALS:
        loop.add_signal_handler(sig, event.set)


def remove_shutdown_signals() -> None:
    """Restore default SIGINT/SIGTERM handling for the running loop."""
    loop = asyncio.get_running_loop()
    for sig in _SHUTDOWN_SIGNALS:
        loop.remove_signal_handler(sig)


async def serve_app(
    app: Quart,
    config: HypercornConfig,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Serve ``app`` with hypercorn until SIGINT/SIGTERM (or ``shutdown_event``).

    Hypercorn drives the ASGI lifespan, so ``before_serving``/``after_serving``
    hooks (including :func:`close_runtime_resources`) run around the serve loop.
    The graceful window is ``config.graceful_timeout``.
    """
    event = shutdown_event or asyncio.Event()
    install_shutdown_signals(event)
    try:
        await hypercorn_serve(app, config, shutdown_trigger=event.wait)
    finally:
        remove_shutdown_signals()


async def close_runtime_resources(app: Quart) -> None:
    """Release background tasks, NATS, Redis and DB in dependency-safe order.

    Stops producers first (gRPC, audit publishes), then the transports they use,
    then the database pool. Each step is isolated so one failure is logged at
    ERROR and the remaining resources are still released.
    """
    await _run_step(app, "grpc_task", lambda: _cancel_grpc_task(app))
    await _run_step(app, "audit_publish_tasks", lambda: _drain_audit_tasks(app))
    await _run_step(app, "nats", lambda: _close_nats(app))
    await _run_step(app, "redis", lambda: _close_redis(app))
    await _run_step(app, "db_engine", lambda: _dispose_db(app))
