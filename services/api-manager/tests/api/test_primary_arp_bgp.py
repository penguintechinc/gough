"""Behaviour of ``_emit_gracious_arp`` and ``_advertise_bgp_route`` in app/api/primary.py.

Covers the VIP literal-IP guard (the VIP is interpolated into a command run on
each node), the DAL-based node address lookup, the ARP/BGP command issued per
address family, and per-node audit events on every exit path including early
exits that never reach gRPC.
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
    """Column stub whose ``==`` yields a tuple the fake DB can read."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __eq__(self, other: object) -> tuple[str, str, Any]:  # type: ignore[override]
        return (self._name, "eq", other)


class _FakeDB:
    """penguin-dal stand-in serving ``nodes`` rows by id; records raw SQL attempts."""

    def __init__(self, rows: dict[int, Any]) -> None:
        self._rows = rows
        self.nodes = SimpleNamespace(id=_Col("id"))
        self.raw_sql: list[str] = []

    def __call__(self, cond: tuple[str, str, Any]) -> "_FakeDB":
        self._cond = cond
        return self

    def select(self) -> "_FakeDB":
        return self

    def first(self) -> Any:
        return self._rows.get(self._cond[2])

    def executesql(self, sql: str, *_args: Any) -> Any:
        self.raw_sql.append(sql)
        return []


class _Stub:
    """Discovery stub; ExecCommand behaviour and last request are injected."""

    behaviour: Any = None
    last_request: Any = None

    def __init__(self, channel: Any) -> None:
        self.channel = channel

    def ExecCommand(self, req: Any, timeout: float) -> Any:  # noqa: N802 - gRPC name
        _Stub.last_request = req
        return _Stub.behaviour(req, timeout)


def _node(
    ipv4: str | None = "10.0.0.5", ipv6: str | None = None, family: str | None = None
) -> Any:
    """Node row with the given address columns."""
    return SimpleNamespace(ipv4=ipv4, ipv6=ipv6, preferred_addr_family=family)


@pytest.fixture()
def grpc_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace gRPC channel + stub with recorders; default ExecCommand succeeds."""
    rec: dict[str, Any] = {"targets": [], "requests": []}

    def _secure_channel(target: str, creds: Any) -> MagicMock:
        rec["targets"].append(target)
        return MagicMock()

    def _stub_exec(req: Any, timeout: float) -> Any:
        rec["requests"].append(req)
        return SimpleNamespace(exit_code=0, output="ok")

    monkeypatch.setattr(grpc, "secure_channel", _secure_channel)
    monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: "creds")
    monkeypatch.setattr(discovery_pb2_grpc, "DiscoveryStub", _Stub)
    _Stub.behaviour = lambda req, timeout: _stub_exec(req, timeout)
    _Stub.last_request = None
    return rec


def _install_db(monkeypatch: pytest.MonkeyPatch, rows: dict[int, Any]) -> _FakeDB:
    """Point primary.get_db at a fake DB holding ``rows``."""
    db = _FakeDB(rows)
    monkeypatch.setattr(primary, "get_db", lambda: db)
    return db


async def _arp(
    vip: str, node_ids: list[int], audit: Any = None
) -> dict[int, dict[str, Any]]:
    """Run the ARP helper inside an app context, optionally with an audit extension."""
    app = Quart(__name__)
    if audit is not None:
        app.extensions["audit"] = audit
    async with app.app_context():
        return await primary._emit_gracious_arp(vip, node_ids)


async def _bgp(
    vip: str, node_ids: list[int], action: str = "advertise", audit: Any = None
) -> dict[int, dict[str, Any]]:
    """Run the BGP helper inside an app context, optionally with an audit extension."""
    app = Quart(__name__)
    if audit is not None:
        app.extensions["audit"] = audit
    async with app.app_context():
        return await primary._advertise_bgp_route(vip, node_ids, action=action)


async def test_arp_rejects_non_ip_vip_without_any_rpc(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """A VIP carrying shell metacharacters is refused; no channel is opened, no command runs."""
    _install_db(monkeypatch, {1: _node()})

    result = await _arp("10.0.0.1; rm -rf /", [1])

    assert result[1]["success"] is False
    assert "not an IP address" in result[1]["error"]
    assert grpc_calls["targets"] == []
    assert grpc_calls["requests"] == []


async def test_arp_invalid_vip_is_still_audited_per_node(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """A refused VIP emits one audit event per node, so the attempt is recorded."""
    _install_db(monkeypatch, {1: _node(), 2: _node()})
    audit = MagicMock(spec=AuditLogger)

    await _arp("not-an-ip", [1, 2], audit=audit)

    assert audit.log.call_count == 2
    assert audit.log.call_args_list[0].kwargs["resource_type"] == "primary.arp"


async def test_arp_ipv4_uses_arping_on_resolved_node(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """IPv4 VIP runs arping -U on the node, dialled at the node's ipv4 address."""
    _install_db(monkeypatch, {1: _node(ipv4="10.0.0.5")})

    result = await _arp("10.0.0.100", [1])

    assert result[1]["success"] is True
    assert grpc_calls["targets"] == ["10.0.0.5:50051"]
    assert grpc_calls["requests"][0].command == "arping -U -c 3 10.0.0.100"


async def test_arp_ipv6_uses_ndisc6(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """IPv6 VIP runs ndisc6 and the IPv6 node address is bracketed for the target."""
    _install_db(monkeypatch, {1: _node(ipv4=None, ipv6="fd00::5")})

    result = await _arp("fd00::100", [1])

    assert result[1]["success"] is True
    assert grpc_calls["targets"] == ["[fd00::5]:50051"]
    assert grpc_calls["requests"][0].command == "ndisc6 -r 3 fd00::100"


async def test_arp_address_lookup_uses_dal_not_raw_sql(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """The node address comes from the DAL; no raw SQL is executed."""
    db = _install_db(monkeypatch, {1: _node()})

    await _arp("10.0.0.100", [1])

    assert db.raw_sql == []


async def test_arp_missing_node_reported_and_audited(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """Unknown node -> not-found error, no RPC, and an audit event."""
    _install_db(monkeypatch, {})
    audit = MagicMock(spec=AuditLogger)

    result = await _arp("10.0.0.100", [9], audit=audit)

    assert result[9]["error"] == "Node 9 not found in DB"
    assert grpc_calls["targets"] == []
    assert audit.log.call_count == 1


async def test_arp_node_without_address_reported_and_audited(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """Node with no address -> error, no RPC, audited."""
    _install_db(monkeypatch, {4: _node(ipv4=None, ipv6=None)})
    audit = MagicMock(spec=AuditLogger)

    result = await _arp("10.0.0.100", [4], audit=audit)

    assert result[4]["error"] == "Node 4 has no ipv4 or ipv6 address"
    assert grpc_calls["targets"] == []
    assert audit.log.call_count == 1


async def test_arp_nonzero_exit_reported(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """A failing arping surfaces its exit code and output, not success."""
    _install_db(monkeypatch, {1: _node()})
    _Stub.behaviour = lambda req, timeout: SimpleNamespace(
        exit_code=1, output="no route"
    )

    result = await _arp("10.0.0.100", [1])

    assert result[1]["success"] is False
    assert result[1]["error"] == "ExecCommand exit code 1: no route"


async def test_bgp_rejects_non_ip_vip_without_rpc(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """A non-IP VIP never reaches the node's BGP helper; the refusal is audited."""
    _install_db(monkeypatch, {1: _node()})
    audit = MagicMock(spec=AuditLogger)

    result = await _bgp("10.0.0.1 --evil", [1], audit=audit)

    assert "not an IP address" in result[1]["error"]
    assert grpc_calls["requests"] == []
    assert audit.log.call_args.kwargs["resource_type"] == "primary.bgp"


async def test_bgp_ipv4_advertise_uses_slash_32(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """IPv4 VIP is announced as a /32 host route with the requested action."""
    _install_db(monkeypatch, {1: _node()})

    result = await _bgp("10.0.0.100", [1], action="advertise")

    assert result[1]["success"] is True
    req = grpc_calls["requests"][0]
    assert req.command == "/usr/local/bin/gough-bgp-announce"
    assert req.target == "--vip 10.0.0.100/32 --action advertise"


async def test_bgp_ipv6_withdraw_uses_slash_128(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """IPv6 VIP is withdrawn as a /128 host route."""
    _install_db(monkeypatch, {1: _node(ipv4=None, ipv6="fd00::5")})

    result = await _bgp("fd00::100", [1], action="withdraw")

    assert result[1]["success"] is True
    assert grpc_calls["requests"][0].target == "--vip fd00::100/128 --action withdraw"


async def test_bgp_missing_node_audited_on_early_exit(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """Missing node is audited on the BGP path too, with prefix and action in details."""
    _install_db(monkeypatch, {})
    audit = MagicMock(spec=AuditLogger)

    result = await _bgp("10.0.0.100", [3], action="withdraw", audit=audit)

    assert result[3]["error"] == "Node 3 not found in DB"
    assert grpc_calls["targets"] == []
    details = audit.log.call_args.kwargs["details"]
    assert details == {
        "vip": "10.0.0.100",
        "success": False,
        "error": "Node 3 not found in DB",
        "duration_ms": 0,
        "prefix_len": 32,
        "action": "withdraw",
    }


async def test_bgp_no_address_audited_on_early_exit(
    monkeypatch: pytest.MonkeyPatch, grpc_calls: dict[str, Any]
) -> None:
    """Node without an address is audited on the BGP path."""
    _install_db(monkeypatch, {2: _node(ipv4=None, ipv6=None)})
    audit = MagicMock(spec=AuditLogger)

    result = await _bgp("10.0.0.100", [2], audit=audit)

    assert result[2]["error"] == "Node 2 has no ipv4 or ipv6 address"
    assert audit.log.call_count == 1
