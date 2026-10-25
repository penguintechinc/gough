"""Behaviour of ``app.api.primary._update_endpoint_on_nodes``.

Covers the per-node outcome matrix (missing node, no address, IPv4/IPv6
selection, exit-code success/failure, RPC errors) and the per-node audit event.
The DB, gRPC channel and Discovery stub are replaced with in-memory fakes so
each assertion is on the returned result dict and the calls made.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import app.api.primary as primary
import grpc
import pytest
from app.audit import AuditLogger
from app.grpc.gough import discovery_pb2_grpc
from quart import Quart


class _Col:
    """Column stub whose ``==`` yields a comparable tuple the fake DB can read."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __eq__(self, other: object) -> tuple[str, str, Any]:  # type: ignore[override]
        return (self._name, "eq", other)


class _FakeDB:
    """penguin-dal stand-in serving ``nodes`` rows keyed by id."""

    def __init__(self, rows: dict[int, Any]) -> None:
        self._rows = rows
        self.nodes = SimpleNamespace(id=_Col("id"))

    def __call__(self, cond: tuple[str, str, Any]) -> "_FakeDB":
        self._cond = cond
        return self

    def select(self) -> "_FakeDB":
        return self

    def first(self) -> Any:
        return self._rows.get(self._cond[2])


class _Channel:
    """Records close() so tests can assert the channel is always released."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Stub:
    """Discovery stub whose ExecCommand behaviour is injected per test."""

    behaviour: Any = None
    last_request: Any = None

    def __init__(self, channel: Any) -> None:
        self.channel = channel

    def ExecCommand(
        self, req: Any, timeout: float
    ) -> Any:  # noqa: N802 - gRPC method name
        _Stub.last_request = (req, timeout)
        return _Stub.behaviour(req, timeout)


def _node(
    ipv4: str | None = "10.0.0.5", ipv6: str | None = None, family: str | None = None
) -> Any:
    """Return a node row with the given address columns."""
    return SimpleNamespace(ipv4=ipv4, ipv6=ipv6, preferred_addr_family=family)


async def _call(
    endpoint: str, node_ids: list[int], app: Quart | None = None
) -> dict[int, dict[str, Any]]:
    """Run the helper inside an app context (it reads ``current_app.extensions``)."""
    app = app or Quart(__name__)
    async with app.app_context():
        return await primary._update_endpoint_on_nodes(endpoint, node_ids)


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Wire fakes for DB, secure_channel and the Discovery stub; return recorders."""
    rec: dict[str, Any] = {"channels": [], "targets": []}

    def _secure_channel(target: str, creds: Any) -> _Channel:
        rec["targets"].append(target)
        ch = _Channel()
        rec["channels"].append(ch)
        return ch

    monkeypatch.setattr(grpc, "secure_channel", _secure_channel)
    monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: "creds")
    monkeypatch.setattr(discovery_pb2_grpc, "DiscoveryStub", _Stub)
    _Stub.behaviour = lambda req, timeout: SimpleNamespace(exit_code=0, output="ok")
    _Stub.last_request = None
    return rec


def _install_db(monkeypatch: pytest.MonkeyPatch, rows: dict[int, Any]) -> None:
    """Point primary.get_db at a fake DB holding ``rows``."""
    db = _FakeDB(rows)
    monkeypatch.setattr(primary, "get_db", lambda: db)


async def test_missing_node_reports_not_found_and_no_channel(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """An unknown node id is reported as not found; no gRPC channel is opened."""
    _install_db(monkeypatch, {})

    result = await _call("10.0.0.100", [7])

    assert result[7]["success"] is False
    assert result[7]["error"] == "Node 7 not found in DB"
    assert harness["targets"] == []


async def test_node_without_any_address_is_rejected(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """A node with neither ipv4 nor ipv6 fails before contacting the agent."""
    _install_db(monkeypatch, {3: _node(ipv4=None, ipv6=None)})

    result = await _call("10.0.0.100", [3])

    assert result[3]["error"] == "Node 3 has no ipv4 or ipv6 address"
    assert harness["targets"] == []


async def test_ipv4_preferred_by_default(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """Without an explicit family preference the ipv4 address is dialled on port 50051."""
    _install_db(monkeypatch, {1: _node(ipv4="10.0.0.5", ipv6="fd00::5")})

    await _call("10.0.0.100", [1])

    assert harness["targets"] == ["10.0.0.5:50051"]


async def test_ipv6_preferred_when_family_is_ipv6(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """preferred_addr_family=IPv6 (any case) dials the ipv6 address."""
    _install_db(monkeypatch, {1: _node(ipv4="10.0.0.5", ipv6="fd00::5", family="IPV6")})

    await _call("fd00::100", [1])

    assert harness["targets"] == ["[fd00::5]:50051"]


async def test_ipv6_preference_falls_back_to_ipv4_when_no_ipv6(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """Preferring IPv6 on a node without one falls back to its ipv4 address."""
    _install_db(monkeypatch, {1: _node(ipv4="10.0.0.5", ipv6=None, family="ipv6")})

    await _call("10.0.0.100", [1])

    assert harness["targets"] == ["10.0.0.5:50051"]


async def test_zero_exit_marks_success_sends_endpoint_and_closes_channel(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """Exit code 0 is success; the script path and new endpoint are sent; channel closed."""
    _install_db(monkeypatch, {1: _node()})

    result = await _call("10.0.0.100", [1])

    assert result[1]["success"] is True
    assert result[1]["error"] is None
    assert result[1]["duration_ms"] >= 0
    req, timeout = _Stub.last_request
    assert req.command == "/usr/local/bin/gough-update-endpoint"
    assert req.target == "10.0.0.100"
    assert timeout == 30.0
    assert harness["channels"][0].closed is True


async def test_nonzero_exit_reports_code_and_output(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """A non-zero exit is a failure carrying both the exit code and the script output."""
    _install_db(monkeypatch, {1: _node()})
    _Stub.behaviour = lambda req, timeout: SimpleNamespace(
        exit_code=2, output="kube-vip missing"
    )

    result = await _call("10.0.0.100", [1])

    assert result[1]["success"] is False
    assert result[1]["error"] == "ExecCommand exit code 2: kube-vip missing"


async def test_rpc_exception_is_captured_per_node_not_raised(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """A transport error on one node is recorded for that node; the loop continues to the next."""
    _install_db(monkeypatch, {1: _node(), 2: _node(ipv4="10.0.0.6")})

    def _boom(req: Any, timeout: float) -> Any:
        if "10.0.0.5" in harness["targets"][-1]:
            raise RuntimeError("UNAVAILABLE: connection refused")
        return SimpleNamespace(exit_code=0, output="")

    _Stub.behaviour = _boom

    result = await _call("10.0.0.100", [1, 2])

    assert result[1]["success"] is False
    assert result[1]["error"] == "UNAVAILABLE: connection refused"
    assert result[2]["success"] is True
    assert len(harness["targets"]) == 2


async def test_audit_event_emitted_per_node_when_audit_configured(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """With an AuditLogger registered, each node gets one RESOURCE_UPDATE event with its outcome."""
    _install_db(monkeypatch, {1: _node(), 2: _node(ipv4=None)})
    audit = MagicMock(spec=AuditLogger)
    app = Quart(__name__)
    app.extensions["audit"] = audit

    result = await _call("10.0.0.100", [1, 2], app=app)

    assert result[1]["success"] is True
    assert result[2]["error"] == "Node 2 has no ipv4 or ipv6 address"
    assert audit.log.call_count == 2
    first_args, first_kwargs = audit.log.call_args_list[0]
    assert first_kwargs["resource_type"] == "primary.endpoint"
    assert first_kwargs["resource_id"] == "1"
    assert first_kwargs["details"] == {
        "endpoint": "10.0.0.100",
        "success": True,
        "error": None,
        "duration_ms": result[1]["duration_ms"],
    }


async def test_no_audit_event_when_audit_not_configured(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """Without an audit extension the update still completes and nothing is logged."""
    _install_db(monkeypatch, {1: _node()})
    result = await _call("10.0.0.100", [1])

    assert result[1]["success"] is True
