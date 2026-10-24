"""Real-Postgres tests for the capacity guard: concurrent reservations and agent link.

SQLite only serialises through the in-process per-node lock; production runs
Postgres where ``reserve`` relies on ``SELECT ... FOR UPDATE`` on the node row.
These tests run many threads, each on its own pooled connection, to prove the
last slot is granted exactly once. Schema comes from ``alembic upgrade head``.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert, select

from app import capacity_guard as cg
from app.models_sqlalchemy import Base

from .test_capacity_guard import _policy, _req, _snapshot_dict


@pytest.fixture()
def node_id(pg_db: Any, monkeypatch: pytest.MonkeyPatch) -> int:
    """A node with a fresh healthy snapshot (16 cores: allocatable 56 CPU), kill switch OFF."""
    monkeypatch.setattr(cg, "_feature_enabled_sync", lambda *_a, **_k: False)
    nodes = Base.metadata.tables["nodes"]
    now = datetime.now(UTC)
    with pg_db.engine.begin() as conn:
        nid = conn.execute(
            insert(nodes).values(tenant_id="acme", name=f"pg-{uuid.uuid4().hex[:8]}", state="ready").returning(nodes.c.id)
        ).scalar_one()
        snap = cg.parse_snapshot(_snapshot_dict(now))
        assert cg.record_snapshot(conn, nid, snap, now) is cg.SnapshotOutcome.STORED
    return int(nid)


def _race(engine: Any, node: int, threads: int, cpu: float) -> list[cg.ReserveResult]:
    """Fire ``threads`` simultaneous ``reserve`` calls for ``cpu`` cores each and collect results."""
    barrier = threading.Barrier(threads)
    results: list[cg.ReserveResult] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _attempt() -> None:
        try:
            barrier.wait(timeout=30)
            res = cg.reserve(engine, node, _req(cpu=cpu), policy=_policy())
            with lock:
                results.append(res)
        except BaseException as exc:  # noqa: BLE001 -- surfaced by the assertion below
            with lock:
                errors.append(exc)

    pool = [threading.Thread(target=_attempt) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=60)
    assert not errors, errors
    assert len(results) == threads
    return results


def test_pg_concurrent_reserve_last_slot_exactly_one_wins(pg_db: Any, node_id: int) -> None:
    """12 racers each want 40 of 56 CPU: exactly one reservation is granted."""
    results = _race(pg_db.engine, node_id, 12, 40.0)
    granted = [r for r in results if r.fit.ok]
    assert len(granted) == 1
    assert all("insufficient_cpu" in r.fit.reasons for r in results if not r.fit.ok)
    table = cg.CapacityReservation.__table__
    with pg_db.engine.connect() as conn:
        active = conn.execute(select(table.c.public_id).where(table.c.node_id == node_id)).all()
    assert len(active) == 1


def test_pg_concurrent_reserve_never_oversubscribes(pg_db: Any, node_id: int) -> None:
    """16 racers each want 7 CPU of 56: exactly 8 win, total reserved never exceeds allocatable."""
    results = _race(pg_db.engine, node_id, 16, 7.0)
    assert sum(1 for r in results if r.fit.ok) == 8
    table = cg.CapacityReservation.__table__
    with pg_db.engine.connect() as conn:
        total = sum(float(c) for (c,) in conn.execute(select(table.c.cpu_cores).where(table.c.node_id == node_id)))
    assert total == pytest.approx(56.0)


def test_pg_agent_link_refusals(pg_db: Any, node_id: int) -> None:
    """Agent resolution on Postgres: link used, hostname ignored, shared node refused."""
    agents = Base.metadata.tables["access_agents"]
    with pg_db.engine.begin() as conn:
        for agent_id, status in (("pg-ag-1", "active"), ("pg-ag-2", "active")):
            conn.execute(
                insert(agents).values(agent_id=agent_id, hostname="spoofed", status=status, node_id=node_id)
            )
    with pg_db.engine.connect() as conn:
        assert cg.resolve_agent_node(conn, "pg-ag-1").reason == "node_has_multiple_active_agents"
        assert cg.resolve_agent_node(conn, "ghost").reason == "unknown_agent"
    with pg_db.engine.begin() as conn:
        conn.execute(agents.update().where(agents.c.agent_id == "pg-ag-2").values(status="revoked"))
    with pg_db.engine.connect() as conn:
        assert cg.resolve_agent_node(conn, "pg-ag-1").node_id == node_id
