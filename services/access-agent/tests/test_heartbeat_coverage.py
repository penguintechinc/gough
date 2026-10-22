"""Behaviour tests for HeartbeatService: request shape, errors, commands and loop."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import httpx
import psutil
import pytest

from agent import heartbeat as heartbeat_mod
from agent.auth import AgentAuth
from agent.config import AgentConfig
from agent.heartbeat import HeartbeatService


@dataclass(slots=True)
class FakeSnapshot:
    """Stand-in for a host resource snapshot with a to_dict method."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return the snapshot payload."""
        return self.payload


def make_service(
    handler: Any,
    command_handler: Any = None,
    interval: int = 30,
) -> tuple[HeartbeatService, MagicMock]:
    """Create a HeartbeatService whose HTTP client routes through ``handler``.

    Args:
        handler: MockTransport handler standing in for the management server.
        command_handler: Optional callback for server-issued commands.
        interval: Heartbeat interval in seconds.

    Returns:
        Tuple of (service, auth mock).
    """
    config = AgentConfig(
        management_server_url="https://mgmt.test",
        agent_id="agent-9",
        heartbeat_interval=interval,
        heartbeat_timeout=5,
    )
    auth = MagicMock(spec=AgentAuth)
    auth.get_auth_headers.return_value = {"Authorization": "Bearer tok"}
    svc = HeartbeatService(config, auth, command_handler=command_handler)
    svc._http_client = httpx.AsyncClient(
        base_url="https://mgmt.test", transport=httpx.MockTransport(handler)
    )
    return svc, auth


@pytest.fixture(autouse=True)
def fast_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace psutil probes with deterministic values and no 100ms sampling delay."""
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 12.5)
    mem = MagicMock(percent=40.0, available=3 * 1024 * 1024 * 1024)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: mem)
    monkeypatch.setattr(psutil, "net_connections", lambda: [1, 2, 3])


@pytest.fixture(autouse=True)
def fake_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the host resource collector with a fixed snapshot."""

    async def _collect(timeout_s: float, lxd_socket: str) -> FakeSnapshot:
        return FakeSnapshot({"host": "h1", "timeout_s": timeout_s, "lxd": lxd_socket})

    monkeypatch.setattr(heartbeat_mod, "collect_host_resources", _collect)


async def test_successful_heartbeat_body_and_headers() -> None:
    """A successful heartbeat posts identity, sessions, usage and the resource snapshot."""
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={})

    svc, _ = make_service(handler)
    svc.set_active_sessions(3)
    await svc._send_heartbeat()

    assert seen["path"] == "/api/v1/agents/heartbeat"
    assert seen["auth"] == "Bearer tok"
    body = seen["body"]
    assert body["agent_id"] == "agent-9"
    assert body["status"] == "active"
    assert body["active_sessions"] == 3
    assert body["resource_usage"] == {
        "cpu_percent": 12.5,
        "memory_percent": 40.0,
        "memory_available_mb": 3072,
        "connections": 3,
    }
    assert body["resource_snapshot"]["host"] == "h1"
    assert body["resource_snapshot"]["timeout_s"] == 5.0
    assert "timestamp" in body
    await svc.stop()


async def test_commands_are_dispatched_and_handler_errors_isolated() -> None:
    """Every server command reaches the handler; a raising handler does not abort the rest."""
    received: list[dict[str, Any]] = []

    def command_handler(cmd: dict[str, Any]) -> None:
        received.append(cmd)
        if cmd["type"] == "boom":
            raise RuntimeError("handler exploded")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"commands": [{"type": "boom"}, {"type": "reload_config"}]}
        )

    svc, _ = make_service(handler, command_handler=command_handler)
    await svc._send_heartbeat()
    assert [c["type"] for c in received] == ["boom", "reload_config"]
    await svc.stop()


async def test_commands_ignored_without_handler() -> None:
    """Commands in the response are silently skipped when no handler is registered."""
    svc, _ = make_service(lambda req: httpx.Response(200, json={"commands": [{"type": "x"}]}))
    await svc._send_heartbeat()  # must not raise
    await svc.stop()


async def test_401_raises_authentication_failure() -> None:
    """A 401 is reported as an authentication failure so the loop can count it."""
    svc, _ = make_service(lambda req: httpx.Response(401))
    with pytest.raises(Exception, match="Authentication failed"):
        await svc._send_heartbeat()
    await svc.stop()


async def test_non_200_status_is_reported() -> None:
    """Any other non-200 status raises with the status code."""
    svc, _ = make_service(lambda req: httpx.Response(503))
    with pytest.raises(Exception, match="Heartbeat returned 503"):
        await svc._send_heartbeat()
    await svc.stop()


async def test_transport_error_is_wrapped() -> None:
    """Connection failures are re-raised as a generic heartbeat exception."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=req)

    svc, _ = make_service(handler)
    with pytest.raises(Exception, match="Network error"):
        await svc._send_heartbeat()
    await svc.stop()


async def test_missing_auth_aborts_before_network() -> None:
    """An AuthError from the auth handler stops the heartbeat before any request is sent."""
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={})

    svc, auth = make_service(handler)
    auth.get_auth_headers.side_effect = RuntimeError("no token")
    with pytest.raises(RuntimeError):
        await svc._send_heartbeat()
    assert calls == []
    await svc.stop()


def test_resource_usage_degrades_to_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A psutil failure yields an empty usage dict rather than failing the heartbeat."""

    def broken() -> None:
        raise OSError("proc unreadable")

    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: broken())
    svc, _ = make_service(lambda req: httpx.Response(200, json={}))
    assert svc._get_resource_usage() == {}


async def test_loop_counts_consecutive_failures_and_resets_on_success() -> None:
    """Failures increment the counter, a success resets it, and the loop keeps running."""
    outcomes = [500, 500, 200]
    seen = asyncio.Event()

    def handler(req: httpx.Request) -> httpx.Response:
        status = outcomes.pop(0) if outcomes else 200
        if not outcomes:
            seen.set()
        return httpx.Response(status, json={})

    svc, _ = make_service(handler, interval=0)
    await svc.start()
    await asyncio.wait_for(seen.wait(), timeout=5)
    # Let the loop complete the post-success iteration and reset the counter.
    for _ in range(50):
        if svc._consecutive_failures == 0 and not outcomes:
            break
        await asyncio.sleep(0.01)
    await svc.stop()
    assert svc._consecutive_failures == 0


async def test_loop_reaches_max_failures_and_keeps_retrying(caplog: pytest.LogCaptureFixture) -> None:
    """After max consecutive failures the loop logs critical but does not stop."""
    hits = 0
    limit = asyncio.Event()

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        if hits >= 6:
            limit.set()
        return httpx.Response(500, json={})

    svc, _ = make_service(handler, interval=0)
    await svc.start()
    await asyncio.wait_for(limit.wait(), timeout=5)
    await svc.stop()
    assert svc._consecutive_failures >= svc._max_failures
    assert hits >= 6


async def test_start_is_idempotent_and_stop_is_safe_when_idle() -> None:
    """start twice runs one loop; stop on a never-started service is harmless."""
    idle, _ = make_service(lambda req: httpx.Response(200, json={}), interval=3600)
    await idle.stop()  # never started

    svc, _ = make_service(lambda req: httpx.Response(200, json={}), interval=3600)
    await svc.start()
    first_task = svc._task
    await svc.start()
    assert svc._task is first_task
    await svc.stop()
    assert svc._http_client is None
    assert first_task is not None and first_task.done()
    assert svc._running is False


def test_set_active_sessions_updates_count() -> None:
    """set_active_sessions stores the value that the next heartbeat will report."""
    svc, _ = make_service(lambda req: httpx.Response(200, json={}))
    svc.set_active_sessions(7)
    assert svc._active_sessions == 7


def test_http_client_property_builds_once_from_config() -> None:
    """The lazily-created async client honours the configured base URL and timeout."""
    config = AgentConfig(management_server_url="https://cfg.test", heartbeat_timeout=9)
    svc = HeartbeatService(config, MagicMock(spec=AgentAuth))
    client = svc.http_client
    assert str(client.base_url) == "https://cfg.test"
    assert svc.http_client is client
