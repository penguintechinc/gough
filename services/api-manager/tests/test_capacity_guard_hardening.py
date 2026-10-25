"""Hardening tests for the capacity guard: snapshot trust, agent-node link, placement.

Real SQLite engine (no mocks) for every DB-backed test. Covers the security fixes:
future-dated snapshots cannot release reservations, and snapshot ingest maps an
agent to a node only through the server-side link.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app import capacity_guard as cg
from app.models_m1 import (
    RESERVATION_ACTIVE,
    RESERVATION_CONFIRMED,
    RESERVATION_RELEASED,
)
from app.models_sqlalchemy import Base
from sqlalchemy import create_engine, insert, select

from .test_capacity_guard import GIB, NOW, _cap, _policy, _req, _snapshot_dict

SECOND = timedelta(seconds=1)


@pytest.fixture()
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """SQLite engine with two nodes (host-a, host-b), agents and capacity tables."""
    monkeypatch.setattr(cg, "_feature_enabled_sync", lambda *_a, **_k: False)
    eng = create_engine(f"sqlite:///{tmp_path}/hardening.db")
    Base.metadata.create_all(
        eng,
        tables=[
            Base.metadata.tables["nodes"],
            Base.metadata.tables["access_agents"],
            cg.NodeResourceSnapshot.__table__,
            cg.CapacityReservation.__table__,
        ],
    )
    nodes = Base.metadata.tables["nodes"]
    with eng.begin() as conn:
        conn.execute(
            insert(nodes).values(id=1, name="host-a", tenant_id="t1", state="ready")
        )
        conn.execute(
            insert(nodes).values(id=2, name="host-b", tenant_id="t1", state="ready")
        )
        conn.execute(
            insert(nodes).values(id=3, name="dup", tenant_id="t1", state="ready")
        )
        conn.execute(
            insert(nodes).values(id=4, name="dup", tenant_id="t2", state="ready")
        )
    return eng


def _agent(
    engine: Any, agent_id: str, *, node_id: int | None, status: str = "active"
) -> None:
    """Insert an access agent row with an optional server-side node link."""
    agents = Base.metadata.tables["access_agents"]
    with engine.begin() as conn:
        conn.execute(
            insert(agents).values(
                agent_id=agent_id,
                hostname="self-reported",
                status=status,
                node_id=node_id,
                tenant_id="t1",
            )
        )


def _record(
    engine: Any, node_id: int, collected: datetime, received: datetime, **kw: Any
) -> Any:
    """Record a snapshot for ``node_id`` and return the outcome."""
    snap = cg.parse_snapshot(_snapshot_dict(collected, **kw))
    with engine.begin() as conn:
        return cg.record_snapshot(conn, node_id, snap, received)


def _status(engine: Any, reservation_id: str) -> int:
    """Return a reservation's status code."""
    table = cg.CapacityReservation.__table__
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(table.c.status).where(table.c.public_id == reservation_id)
            ).scalar_one()
        )


GROWN = {
    "committed": {
        "cpu_cores": 4.0,
        "memory_bytes": 8 * GIB,
        "disk_bytes": {"default": 0},
        "instances": 1,
        "instances_without_limits": 0,
    }
}


# --- snapshot ordering / future-dating ------------------------------------------------


def test_future_dated_snapshot_rejected_and_reservations_survive(engine: Any) -> None:
    """collected_at far in the future is refused: stored row and reservations untouched."""
    assert _record(engine, 1, NOW, NOW) is cg.SnapshotOutcome.STORED
    held = cg.reserve(engine, 1, _req(cpu=4.0, mem_gib=8.0), now=NOW, policy=_policy())
    assert held.reservation_id and cg.confirm(engine, held.reservation_id, now=NOW)
    forged = _record(engine, 1, NOW + timedelta(hours=1), NOW + 5 * SECOND, **GROWN)
    assert forged is cg.SnapshotOutcome.FUTURE_DATED
    assert _status(engine, held.reservation_id) == RESERVATION_CONFIRMED
    table = cg.NodeResourceSnapshot.__table__
    with engine.connect() as conn:
        assert conn.execute(select(table.c.committed_cpu_cores)).scalar_one() == 0.0


def test_skew_within_limit_accepted_but_does_not_release_unconfirmed(
    engine: Any,
) -> None:
    """A small clock lead is tolerated; ACTIVE (unconfirmed) reservations never release."""
    assert _record(engine, 1, NOW, NOW) is cg.SnapshotOutcome.STORED
    held = cg.reserve(engine, 1, _req(cpu=4.0, mem_gib=8.0), now=NOW, policy=_policy())
    assert held.reservation_id
    out = _record(engine, 1, NOW + 40 * SECOND, NOW + 10 * SECOND, **GROWN)
    assert out is cg.SnapshotOutcome.STORED
    assert _status(engine, held.reservation_id) == RESERVATION_ACTIVE


def test_skewed_snapshot_does_not_release_confirmed_reservation(engine: Any) -> None:
    """Agent clock ahead: min(collected, received) is not after confirmation -> kept."""
    assert _record(engine, 1, NOW, NOW) is cg.SnapshotOutcome.STORED
    held = cg.reserve(
        engine, 1, _req(cpu=4.0, mem_gib=8.0), now=NOW + 20 * SECOND, policy=_policy()
    )
    assert held.reservation_id
    cg.confirm(engine, held.reservation_id, now=NOW + 30 * SECOND)
    # received before the confirm even though collected_at claims later
    _record(engine, 1, NOW + 50 * SECOND, NOW + 25 * SECOND, **GROWN)
    assert _status(engine, held.reservation_id) == RESERVATION_CONFIRMED


def test_confirmed_not_released_when_committed_did_not_grow(engine: Any) -> None:
    """A fresh, honest snapshot whose committed totals did not grow keeps the reservation."""
    assert _record(engine, 1, NOW, NOW) is cg.SnapshotOutcome.STORED
    held = cg.reserve(engine, 1, _req(cpu=4.0, mem_gib=8.0), now=NOW, policy=_policy())
    assert held.reservation_id and cg.confirm(engine, held.reservation_id, now=NOW)
    _record(engine, 1, NOW + 10 * SECOND, NOW + 10 * SECOND)
    assert _status(engine, held.reservation_id) == RESERVATION_CONFIRMED
    _record(engine, 1, NOW + 20 * SECOND, NOW + 20 * SECOND, **GROWN)
    assert _status(engine, held.reservation_id) == RESERVATION_RELEASED


def test_stale_on_arrival_snapshot_rejected(engine: Any) -> None:
    """A replayed snapshot older than the freshness window is refused (TOO_OLD)."""
    out = _record(engine, 1, NOW - timedelta(minutes=10), NOW)
    assert out is cg.SnapshotOutcome.TOO_OLD


def test_confirmed_reservation_counts_as_pending(engine: Any) -> None:
    """Confirmation keeps holding headroom until a snapshot absorbs it."""
    _record(engine, 1, NOW, NOW)
    first = cg.reserve(engine, 1, _req(cpu=40.0), now=NOW, policy=_policy())
    assert first.reservation_id and cg.confirm(engine, first.reservation_id, now=NOW)
    second = cg.reserve(engine, 1, _req(cpu=40.0), now=NOW, policy=_policy())
    assert not second.fit.ok and "insufficient_cpu" in second.fit.reasons


# --- agent <-> node link --------------------------------------------------------------


def test_resolve_ignores_hostname_and_uses_link(engine: Any) -> None:
    """The self-reported hostname 'host-a' is irrelevant: only node_id link counts."""
    _agent(engine, "ag-1", node_id=2)
    with engine.connect() as conn:
        link = cg.resolve_agent_node(conn, "ag-1")
    assert link.node_id == 2 and link.reason is None


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("unknown", "unknown_agent"),
        ("inactive", "agent_not_active"),
        ("unlinked", "agent_not_linked_to_node"),
        ("shared", "node_has_multiple_active_agents"),
    ],
)
def test_resolve_refusals(engine: Any, setup: str, reason: str) -> None:
    """Each refusal reason: unknown, inactive, unlinked, or node shared by two active agents."""
    if setup == "inactive":
        _agent(engine, "ag-1", node_id=1, status="revoked")
    elif setup == "unlinked":
        _agent(engine, "ag-1", node_id=None)
    elif setup == "shared":
        _agent(engine, "ag-1", node_id=1)
        _agent(engine, "ag-2", node_id=1)
    with engine.connect() as conn:
        link = cg.resolve_agent_node(conn, "ag-1")
    assert link.node_id is None and link.reason == reason


def test_inactive_second_agent_does_not_block_active_one(engine: Any) -> None:
    """Only ACTIVE duplicates count: a revoked agent on the same node is harmless."""
    _agent(engine, "ag-1", node_id=1)
    _agent(engine, "ag-old", node_id=1, status="revoked")
    with engine.connect() as conn:
        assert cg.resolve_agent_node(conn, "ag-1").node_id == 1


def test_link_agent_refusals_and_success(engine: Any) -> None:
    """Admin link: unknown agent/node, tenant mismatch, node already linked, success."""
    _agent(engine, "ag-1", node_id=None)
    _agent(engine, "ag-2", node_id=None)
    with engine.begin() as conn:
        assert cg.link_agent_to_node(conn, "nope", 1) == "agent_not_found"
        assert cg.link_agent_to_node(conn, "ag-1", 99) == "node_not_found"
        assert (
            cg.link_agent_to_node(conn, "ag-1", 1, expected_tenant="other")
            == "tenant_mismatch"
        )
        assert (
            cg.link_agent_to_node(conn, "ag-1", 4, expected_tenant="t1")
            == "tenant_mismatch"
        )
        assert cg.link_agent_to_node(conn, "ag-1", 1, expected_tenant="t1") is None
        assert cg.link_agent_to_node(conn, "ag-2", 1) == "node_already_linked"


def test_resolve_node_by_name_ambiguous_fails_closed(engine: Any) -> None:
    """A name present in two tenants resolves to None unless the tenant disambiguates."""
    with engine.connect() as conn:
        assert cg.resolve_node_by_name(conn, "host-a") == 1
        assert cg.resolve_node_by_name(conn, "dup") is None
        assert cg.resolve_node_by_name(conn, "dup", "t2") == 4
        assert cg.resolve_node_by_name(conn, "ghost") is None


# --- placement ------------------------------------------------------------------------


def test_place_picks_node_with_most_free_memory_and_reserves(engine: Any) -> None:
    """Both fit: the node with the higher MemAvailable fraction wins; a reservation is held."""
    now = datetime.now(UTC)
    _record(engine, 1, now, now)  # 40/64 free
    snap = _snapshot_dict(now)
    snap["memory"] = {"total_bytes": 64 * GIB, "available_bytes": 60 * GIB}
    with engine.begin() as conn:
        cg.record_snapshot(conn, 2, cg.parse_snapshot(snap), now)
    result = cg.place(
        engine, {"host-a": 1, "host-b": 2}, _req(cpu=2.0, mem_gib=4.0), now=now
    )
    assert isinstance(result, cg.Placement)
    assert (result.member, result.node_id) == ("host-b", 2)
    assert (
        result.reservation_id
        and _status(engine, result.reservation_id) == RESERVATION_ACTIVE
    )


def test_place_falls_back_when_best_node_is_full(engine: Any) -> None:
    """If the roomiest node cannot take the request, the next candidate is used."""
    now = datetime.now(UTC)
    snap = _snapshot_dict(now)
    snap["cpu"] = {
        "logical_cores": 4,
        "usage_pct": 1.0,
        "load1": 0.0,
        "load5": 0.0,
        "load15": 0.0,
    }
    snap["memory"] = {"total_bytes": 64 * GIB, "available_bytes": 60 * GIB}
    with engine.begin() as conn:
        cg.record_snapshot(conn, 2, cg.parse_snapshot(snap), now)  # tiny cpu: (4-2)*4=8
    _record(engine, 1, now, now)  # 16 cores
    result = cg.place(engine, {"host-a": 1, "host-b": 2}, _req(cpu=20.0), now=now)
    assert isinstance(result, cg.Placement) and result.member == "host-a"


def test_place_refuses_with_per_member_reasons(engine: Any) -> None:
    """No fit anywhere (and a node with no snapshot): refusal lists reasons per member."""
    now = datetime.now(UTC)
    _record(engine, 1, now, now)
    result = cg.place(engine, {"host-a": 1, "host-b": 2}, _req(cpu=999.0), now=now)
    assert isinstance(result, cg.PlacementRefusal)
    assert "insufficient_cpu" in result.reasons["host-a"]
    assert result.reasons["host-b"] == ("stale_or_missing_snapshot",)


def test_place_with_no_candidates_fails_closed(engine: Any) -> None:
    """A placement mappable to no known node is refused, never allowed through."""
    result = cg.place(engine, {}, _req(cpu=1.0))
    assert isinstance(result, cg.PlacementRefusal)
    assert result.reasons == {"": ("no_candidate_nodes",)}


# --- fit math additions ---------------------------------------------------------------


def test_pool_free_space_gate_refuses_when_pool_nearly_full() -> None:
    """Real used bytes count: limit-based room is not enough if the pool is physically full."""
    full = cg.evaluate_fit(
        _cap(pools_used={"default": 900 * GIB}),
        _req(disk_gib=1.0),
        cg.Load(),
        _policy(),
    )
    assert full.reasons == ("insufficient_disk_free:default",)
    roomy = cg.evaluate_fit(
        _cap(pools_used={"default": 100 * GIB}),
        _req(disk_gib=1.0),
        cg.Load(),
        _policy(),
    )
    assert roomy.ok


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4GiB", 4 * GIB),
        ("4GB", 4 * 1000**3),
        ("512MiB", 512 * 1024**2),
        ("1073741824", GIB),
        ("50%", None),
        ("0", None),
        ("abc", None),
        ("", None),
        ("4XB", None),
    ],
)
def test_parse_memory_limit(text: str, expected: int | None) -> None:
    """LXD memory limits parse to bytes; percentages and junk are refused."""
    assert cg.parse_memory_limit(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2", 2.0),
        ("0-3", 4.0),
        ("0-3,8", 5.0),
        ("0", None),
        ("", None),
        ("x", None),
        ("3-1", None),
    ],
)
def test_parse_cpu_limit(text: str, expected: float | None) -> None:
    """LXD cpu limits parse to a core count (counts pinned sets); junk is refused."""
    assert cg.parse_cpu_limit(text) == expected


def test_fresh_available_memory_fail_closed(engine: Any) -> None:
    """MemAvailable is returned only for a fresh, error-free snapshot."""
    assert cg.fresh_available_memory_bytes(engine, 1, now=NOW) is None
    _record(engine, 1, NOW, NOW)
    assert cg.fresh_available_memory_bytes(engine, 1, now=NOW) == 40 * GIB
    assert (
        cg.fresh_available_memory_bytes(engine, 1, now=NOW + timedelta(minutes=5))
        is None
    )
    _record(engine, 2, NOW, NOW, errors=["host_error"])
    assert cg.fresh_available_memory_bytes(engine, 2, now=NOW) is None


def test_kill_switch_on_lets_place_through(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the kill switch ON, place() passes the first candidate with no reservation."""
    monkeypatch.setattr(
        cg, "_feature_enabled_sync", lambda flag, _d="": flag == cg.KILL_SWITCH_FLAG
    )
    result = cg.place(engine, {"host-a": 1}, _req(cpu=999.0))
    assert isinstance(result, cg.Placement) and result.reservation_id is None
