"""Flag-gated real-execution lifecycle tests for ``migration_engine``.

Covers the M2 live-migration execution engine end to end with the LXD and
NATS boundaries mocked (per this change's task spec): feature-flag OFF
short-circuit, a full success lifecycle (drain -> move -> verify ->
executed), and the destination-failure -> rollback branch. DB access goes
through a real in-memory penguin-dal SQLite instance (same convention as
``tests/api/conftest.py``'s ``client`` fixture) so the ``migration_events``
and ``node_egg_assignments`` row transitions are asserted against genuine
rows, not mocks.

Monkeypatches ``app.models.get_db`` and ``app.api.migration.get_db``
directly on the already-imported module objects (not via Quart app
context) -- ``app.workers.migration_engine``'s ``evaluate_safety``/
``execute_migration`` defensively fall back to environment defaults for
anything that needs ``quart.g``/``current_app`` (cluster id resolution,
best-effort NATS emission), so no Quart app/request context is required
here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Column, String, Table

pytestmark = pytest.mark.asyncio


def _define_table_with_string_id(dal_db: Any, name: str, *fields: Any) -> None:
    """Like ``dal_db.define_table`` but with a VARCHAR(36) string ``id``.

    Copied from ``tests/api/conftest.py`` (same rationale: penguin_dal's
    ``type_="id"`` shortcut is hardcoded to an autoincrement Integer column
    with no override, but ``migration_events.id`` is an app-supplied UUID
    string with no default).
    """
    if name in getattr(dal_db, "tables", []):
        return
    columns = [Column("id", String(36), primary_key=True)]
    columns.extend(field.to_sa_column() for field in fields)
    table = Table(name, dal_db.metadata, *columns)
    dal_db.metadata.create_all(dal_db.engine, tables=[table])


@pytest.fixture()
def dal(tmp_path: Any) -> Any:
    """Fresh in-memory-backed SQLite penguin-dal with the tables this
    module's execute_migration/evaluate_safety touch.

    Deliberately omits ``migration_policy`` -- its absence exercises the
    same soft-schema fallback (``_load_or_default_policy`` ->
    ``_default_policy_dict``) the production code already relies on, and
    conveniently gives ``rollback_on_destination_failure=True`` for free.
    """
    from penguin_dal import DB, Field

    db = DB(
        f"sqlite:///{tmp_path}/test-migration-live-exec.db",
        pool_size=1,
        reflect=False,
        migrate=True,
    )
    db.define_table(
        "nodes",
        Field("tenant_id", "string", default="__default__"),
        Field("name", "string"),
        Field("state", "string", default="ready"),
        migrate=True,
    )
    db.define_table(
        "biomes",
        Field("name", "string"),
        Field("biome_kind", "string", default="custom"),
        Field("lock_to_host", "boolean", default=False),
        Field("requires_hardware_tags", "json"),
        Field("storage_requirements_json", "json"),
        migrate=True,
    )
    db.define_table(
        "node_egg_assignments",
        Field("node_id", "integer", notnull=True),
        Field("egg_id", "integer", notnull=True),
        Field("tenant_id", "string", default="__default__"),
        Field("status", "string", default="pending"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )
    _define_table_with_string_id(
        db,
        "migration_events",
        Field("biome_instance_id", "integer"),
        Field("biome_id", "integer"),
        Field("biome_kind", "string"),
        Field("src_node_id", "integer"),
        Field("dst_node_id", "integer"),
        Field("reason", "string"),
        Field("result", "string"),
        Field("rejection_reason", "string"),
        Field("safety_check_details_json", "json"),
        Field("started_at", "datetime"),
        Field("completed_at", "datetime"),
        Field("duration_seconds", "float"),
    )
    yield db
    try:
        db.close()
    except Exception as exc:  # noqa: BLE001 -- teardown must never fail the test
        import logging

        logging.getLogger(__name__).debug("dal teardown close() failed: %s", exc)


@pytest.fixture()
def wired(dal: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point ``app.models.get_db`` / ``app.api.migration.get_db`` at ``dal``."""
    import app.api.migration as migration_mod
    import app.models as models_mod

    monkeypatch.setattr(models_mod, "get_db", lambda: dal)
    monkeypatch.setattr(migration_mod, "get_db", lambda: dal)
    return dal


def _seed_biome_instance(
    dal: Any, *, node_id: int, node_name: str, dst_id: int, dst_name: str,
) -> tuple[int, int]:
    """Seed src + dst nodes and one active biome assignment on ``node_id``.

    Returns (biome_instance_id, biome_id).
    """
    now = datetime.now(timezone.utc)
    dal.nodes.insert(id=node_id, tenant_id="default", name=node_name, state="ready")
    dal.nodes.insert(id=dst_id, tenant_id="default", name=dst_name, state="ready")
    biome_id = dal.biomes.insert(name="test-biome", biome_kind="custom", lock_to_host=False)
    instance_id = dal.node_egg_assignments.insert(
        node_id=node_id, egg_id=int(biome_id), tenant_id="default",
        status="ready", created_at=now, updated_at=now,
    )
    dal.commit()
    return int(instance_id), int(biome_id)


def _seed_migration_event(dal: Any, *, biome_instance_id: int, src: int, dst: int) -> str:
    event_id = str(uuid.uuid4())
    dal.migration_events.insert(
        id=event_id,
        biome_instance_id=biome_instance_id,
        src_node_id=src,
        dst_node_id=dst,
        reason="test",
        result="safety_check",
        started_at=datetime.now(timezone.utc),
    )
    dal.commit()
    return event_id


class TestFeatureFlagGate:
    async def test_execute_migration_disabled_when_flag_off(self) -> None:
        """Default-OFF flag short-circuits before touching DB/LXD/NATS."""
        from app.workers.migration_engine import execute_migration

        result = await execute_migration(
            biome_instance_id=1, src_node_id=1, dst_node_id=2,
            live=True, reason="rebalance",
        )
        assert result == {
            "status": "disabled",
            "duration_ms": 0,
            "verdict": "feature_flag_disabled",
        }


class TestExecuteMigrationFullLifecycle:
    async def test_executed_success_updates_rows_and_calls_lxd(
        self, wired: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flag ON + LXD move + verify all succeed -> executed, rows updated."""
        import app.clients.lxd_extra as lxd_extra_mod
        import app.licensing as licensing_mod
        from app.workers.migration_engine import execute_migration

        dal = wired
        instance_id, _ = _seed_biome_instance(
            dal, node_id=1, node_name="node-1", dst_id=2, dst_name="node-2",
        )
        event_id = _seed_migration_event(dal, biome_instance_id=instance_id, src=1, dst=2)

        monkeypatch.setattr(licensing_mod, "feature_enabled", AsyncMock(return_value=True))
        live_migrate_mock = MagicMock(return_value=MagicMock(status="Success"))
        monkeypatch.setattr(lxd_extra_mod, "live_migrate", live_migrate_mock)
        monkeypatch.setattr(
            lxd_extra_mod, "get_cluster_status",
            MagicMock(return_value=SimpleNamespace(members=[
                SimpleNamespace(name="node-2", status="Online"),
            ])),
        )

        result = await execute_migration(
            biome_instance_id=instance_id, src_node_id=1, dst_node_id=2,
            live=True, reason="rebalance", migration_event_id=event_id,
        )

        assert result["status"] == "executed"
        assert result["verdict"] == "executed"
        assert isinstance(result["duration_ms"], int)

        # LXD move called exactly once, moving the src-node-suffixed
        # instance name to the destination node's name -- never rolled back.
        live_migrate_mock.assert_called_once()
        call_args = live_migrate_mock.call_args
        assert call_args.args[0] == "test-biome-1"
        assert call_args.args[1] == "node-2"

        assignment = dal(dal.node_egg_assignments.id == instance_id).select().first()
        assert assignment.node_id == 2
        assert assignment.status == "ready"

        event = dal(dal.migration_events.id == event_id).select().first()
        assert event.result == "executed"
        assert event.completed_at is not None
        assert event.duration_seconds is not None

    async def test_lxd_move_failure_marks_failed_no_rollback_attempted(
        self, wired: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LXD move itself raises -> failed, instance stays on src, no rollback call."""
        import app.clients.lxd_extra as lxd_extra_mod
        import app.licensing as licensing_mod
        from app.workers.migration_engine import execute_migration

        dal = wired
        instance_id, _ = _seed_biome_instance(
            dal, node_id=1, node_name="node-1", dst_id=2, dst_name="node-2",
        )
        event_id = _seed_migration_event(dal, biome_instance_id=instance_id, src=1, dst=2)

        monkeypatch.setattr(licensing_mod, "feature_enabled", AsyncMock(return_value=True))
        live_migrate_mock = MagicMock(
            side_effect=lxd_extra_mod.LXDMigrationError("operation failed: timeout")
        )
        monkeypatch.setattr(lxd_extra_mod, "live_migrate", live_migrate_mock)

        result = await execute_migration(
            biome_instance_id=instance_id, src_node_id=1, dst_node_id=2,
            live=True, reason="rebalance", migration_event_id=event_id,
        )

        assert result["status"] == "failed"
        # Exactly one LXD call -- the failed move itself; nothing to roll
        # back since the instance never left src (see execute_migration's
        # docstring on this branch).
        live_migrate_mock.assert_called_once()

        assignment = dal(dal.node_egg_assignments.id == instance_id).select().first()
        assert assignment.node_id == 1  # unchanged -- never moved
        assert assignment.status == "ready"

        event = dal(dal.migration_events.id == event_id).select().first()
        assert event.result == "failed"
        assert "operation failed" in (event.rejection_reason or "")

    async def test_destination_verify_failure_triggers_rollback(
        self, wired: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LXD move succeeds but dst never comes Online -> rollback attempted
        and succeeds (default policy: rollback_on_destination_failure=True).
        """
        import app.clients.lxd_extra as lxd_extra_mod
        import app.licensing as licensing_mod
        from app.workers.migration_engine import execute_migration

        dal = wired
        instance_id, _ = _seed_biome_instance(
            dal, node_id=1, node_name="node-1", dst_id=2, dst_name="node-2",
        )
        event_id = _seed_migration_event(dal, biome_instance_id=instance_id, src=1, dst=2)

        monkeypatch.setattr(licensing_mod, "feature_enabled", AsyncMock(return_value=True))
        live_migrate_mock = MagicMock(return_value=MagicMock(status="Success"))
        monkeypatch.setattr(lxd_extra_mod, "live_migrate", live_migrate_mock)
        # Destination never reports Online -> verify fails every time.
        monkeypatch.setattr(
            lxd_extra_mod, "get_cluster_status",
            MagicMock(return_value=SimpleNamespace(members=[
                SimpleNamespace(name="node-2", status="Offline"),
            ])),
        )

        result = await execute_migration(
            biome_instance_id=instance_id, src_node_id=1, dst_node_id=2,
            live=True, reason="rebalance", migration_event_id=event_id,
        )

        assert result["status"] == "rolled_back"

        # Two LXD move calls: the original move to node-2, then the
        # rollback move back to node-1.
        assert live_migrate_mock.call_count == 2
        first_call, second_call = live_migrate_mock.call_args_list
        assert first_call.args == ("test-biome-1", "node-2")
        assert second_call.args == ("test-biome-1", "node-1")

        assignment = dal(dal.node_egg_assignments.id == instance_id).select().first()
        assert assignment.node_id == 1  # rollback never updates node_id (only set on confirmed success)
        assert assignment.status == "ready"

        event = dal(dal.migration_events.id == event_id).select().first()
        assert event.result == "rolled_back"

    async def test_rollback_disabled_by_policy_marks_failed(
        self, wired: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Destination verify fails but rollback_on_destination_failure=False
        in the policy row -> failed, no rollback attempted."""
        from penguin_dal import Field

        import app.clients.lxd_extra as lxd_extra_mod
        import app.licensing as licensing_mod
        from app.workers.migration_engine import execute_migration

        dal = wired
        instance_id, _ = _seed_biome_instance(
            dal, node_id=1, node_name="node-1", dst_id=2, dst_name="node-2",
        )
        event_id = _seed_migration_event(dal, biome_instance_id=instance_id, src=1, dst=2)

        _define_table_with_string_id(
            dal, "migration_policy",
            Field("cluster_id", "string", notnull=True),
            Field("enabled", "boolean", default=False),
            Field("evaluation_interval_seconds", "integer", default=300),
            Field("min_healthy_nodes", "integer", default=3),
            Field("max_concurrent_migrations", "integer", default=1),
            Field("require_target_capacity_headroom_cpu_pct", "integer", default=20),
            Field("require_target_capacity_headroom_mem_pct", "integer", default=20),
            Field("require_target_capacity_headroom_disk_pct", "integer", default=15),
            Field("rollback_on_destination_failure", "boolean", default=True),
            Field("rollback_window_seconds", "integer", default=300),
            Field("forbid_migration_during_partition", "boolean", default=True),
            Field("forbid_migration_during_maintenance", "boolean", default=True),
            Field("waddleai_risk_threshold", "float", default=0.75),
            Field("capacity_forecast_horizon_days", "integer", default=7),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
        )
        dal.migration_policy.insert(
            id=str(uuid.uuid4()), cluster_id="default", enabled=False,
            evaluation_interval_seconds=300, min_healthy_nodes=1,
            max_concurrent_migrations=1,
            require_target_capacity_headroom_cpu_pct=20,
            require_target_capacity_headroom_mem_pct=20,
            require_target_capacity_headroom_disk_pct=15,
            rollback_on_destination_failure=False, rollback_window_seconds=300,
            forbid_migration_during_partition=True,
            forbid_migration_during_maintenance=True,
            waddleai_risk_threshold=0.75, capacity_forecast_horizon_days=7,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        dal.commit()

        monkeypatch.setattr(licensing_mod, "feature_enabled", AsyncMock(return_value=True))
        live_migrate_mock = MagicMock(return_value=MagicMock(status="Success"))
        monkeypatch.setattr(lxd_extra_mod, "live_migrate", live_migrate_mock)
        monkeypatch.setattr(
            lxd_extra_mod, "get_cluster_status",
            MagicMock(return_value=SimpleNamespace(members=[
                SimpleNamespace(name="node-2", status="Offline"),
            ])),
        )

        result = await execute_migration(
            biome_instance_id=instance_id, src_node_id=1, dst_node_id=2,
            live=True, reason="rebalance", migration_event_id=event_id,
        )

        assert result["status"] == "failed"
        live_migrate_mock.assert_called_once()  # no rollback attempt

        event = dal(dal.migration_events.id == event_id).select().first()
        assert event.result == "failed"


class TestEvaluateSafetyReal:
    async def test_load_failure_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A state-load error (DB down, etc.) fails closed, never crashes.

        Raises directly from ``app.api.migration.get_db`` rather than
        relying on "no app context pushed" -- this test package's own
        conftest proactively stubs ``app.models``/``app.api.migration`` as
        permissive MagicMocks at collection time (see that conftest's gh-41
        note), so an un-mocked call here would hit a benign mock (zero
        tables, not an exception) instead of exercising this path.
        """
        import app.api.migration as migration_mod
        from app.workers.migration_engine import evaluate_safety

        def _raise() -> None:
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(migration_mod, "get_db", _raise)

        result = await evaluate_safety(node_id=1)
        assert result["safe"] is False
        assert "Failed to load" in result["note"]
        assert result["violations"] == []

    async def test_no_active_biomes_trivially_safe(self, wired: Any) -> None:
        """A node with zero active biome assignments is trivially safe."""
        from app.workers.migration_engine import evaluate_safety

        dal = wired
        dal.nodes.insert(id=1, tenant_id="default", name="node-1", state="ready")
        dal.commit()

        result = await evaluate_safety(node_id=1)
        assert result["safe"] is True
        assert "nothing to evacuate" in result["note"]

    async def test_unsafe_single_node_cluster_with_active_biome(self, wired: Any) -> None:
        """One active biome + no other ready node -> envelope rejects."""
        from app.workers.migration_engine import evaluate_safety

        dal = wired
        now = datetime.now(timezone.utc)
        dal.nodes.insert(id=1, tenant_id="default", name="node-1", state="ready")
        biome_id = dal.biomes.insert(name="pinned", biome_kind="custom", lock_to_host=False)
        dal.node_egg_assignments.insert(
            node_id=1, egg_id=int(biome_id), tenant_id="default",
            status="ready", created_at=now, updated_at=now,
        )
        dal.commit()

        result = await evaluate_safety(node_id=1)
        assert result["safe"] is False
        assert result["violations"]
