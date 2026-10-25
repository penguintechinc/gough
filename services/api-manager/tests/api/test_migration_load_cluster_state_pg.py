"""Real-Postgres regression test for ``app.api.migration._load_cluster_state`` (gh-22).

Regression: gh-22. ``_load_cluster_state`` used to run its full ``nodes``
table scan + in-flight migration count synchronously, inline in the request
coroutine -- now both reads happen in one ``run_db()`` closure. This proves
the async conversion still returns a correct, complete ``ClusterState``
snapshot (every seeded node present, in-flight count accurate) against real
Postgres, not just "doesn't crash".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


def _seed_node(
    db: Any, *, name: str, state: str = "ready", hardware_json: dict | None = None
) -> int:
    now = datetime.now(timezone.utc)
    node_id = db.nodes.insert(
        tenant_id="acme",
        name=name,
        state=state,
        hardware_json=hardware_json or {},
        created_at=now,
        updated_at=now,
    )
    db.commit()
    return int(node_id)


def _seed_migration_event(db: Any, *, result: str) -> None:
    now = datetime.now(timezone.utc)
    db.migration_events.insert(
        id=str(uuid.uuid4()),
        result=result,
        started_at=now,
    )
    db.commit()


class TestLoadClusterStatePg:
    async def test_returns_snapshot_for_every_seeded_node(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """# regression: gh-22

        3 seeded nodes with distinct hardware_json capacity figures -- the
        run_db()-wrapped closure must still return one NodeSnapshot per row,
        with the right free-capacity fields, not a truncated/empty result.
        """
        from datetime import datetime, timezone

        import app.api.migration as migration_mod
        from app import capacity_guard

        monkeypatch.setattr(migration_mod, "get_db", lambda: pg_db)

        # Free CPU comes from the host snapshot (100 - usage_pct), not hardware_json.
        now = datetime.now(timezone.utc)
        for name, usage in (("node-a", 20.0), ("node-b", 60.0), ("node-c", 90.0)):
            node_id = _seed_node(pg_db, name=name)
            body = _healthy_snapshot_body(now, usage)
            with pg_db.engine.begin() as conn:
                capacity_guard.record_snapshot(
                    conn, node_id, capacity_guard.parse_snapshot(body), now
                )

        state = await migration_mod._load_cluster_state("test-cluster")

        assert state.cluster_id == "test-cluster"
        assert len(state.nodes) == 3
        # NodeSnapshot carries no name field -- assert on the set of
        # per-node free-capacity values (order-independent) instead.
        assert {round(n.cpu_free_pct, 1) for n in state.nodes} == {80.0, 40.0, 10.0}
        assert all(n.telemetry_fresh for n in state.nodes)


def _healthy_snapshot_body(now: Any, usage_pct: float) -> dict[str, Any]:
    """Return a valid gough.host_resources.v1 body with the given CPU usage."""
    gib = 1024**3
    return {
        "schema": "gough.host_resources.v1",
        "collected_at": now.isoformat(),
        "cpu": {
            "logical_cores": 16,
            "usage_pct": usage_pct,
            "load1": 1.0,
            "load5": 1.0,
            "load15": 1.0,
        },
        "memory": {"total_bytes": 64 * gib, "available_bytes": 40 * gib},
        "pressure": None,
        "storage_pools": [
            {
                "name": "default",
                "driver": "dir",
                "total_bytes": 1000 * gib,
                "used_bytes": 100 * gib,
            }
        ],
        "host_root_fs": {"total_bytes": 100 * gib, "free_bytes": 60 * gib},
        "committed": {
            "cpu_cores": 0.0,
            "memory_bytes": 0,
            "disk_bytes": {"default": 0},
            "instances": 0,
            "instances_without_limits": 0,
        },
        "errors": [],
    }

    async def test_counts_in_flight_migrations(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """# regression: gh-22

        The in-flight count read shares the same run_db() closure as the
        node scan -- proves both queries in the unit actually ran and
        returned correct data, not just the first one.
        """
        import app.api.migration as migration_mod

        monkeypatch.setattr(migration_mod, "get_db", lambda: pg_db)

        _seed_migration_event(pg_db, result="in_flight")
        _seed_migration_event(pg_db, result="in_flight")
        _seed_migration_event(pg_db, result="succeeded")

        state = await migration_mod._load_cluster_state("test-cluster")

        assert state.in_flight_migrations == 2

    async def test_empty_cluster_returns_empty_snapshot(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.api.migration as migration_mod

        monkeypatch.setattr(migration_mod, "get_db", lambda: pg_db)

        state = await migration_mod._load_cluster_state("test-cluster")

        assert state.nodes == ()
        assert state.in_flight_migrations == 0
