"""Real-Postgres tests for the M2 cluster-execution paths in
``app.api.clusters``.

Covers the real (flag-gated) executions that replace the M1 preview/
request-only behavior:

* ``POST /api/v1/clusters/{id}/storage/switch-primary`` --
  ``FLAG_STORAGE_SWITCH_PRIMARY`` -- real cutover execution against the
  storage-orchestrator boundary (mocked), success + rollback-on-failure.
* ``POST /api/v1/clusters/{id}/adopt`` -- ``FLAG_CLUSTER_ADOPT`` -- real
  brownfield inventory ingestion against the cluster-discovery boundary
  (mocked), success + rollback-on-failure.

Uses the real-Postgres ``pg_db`` fixture (``tests/pg_fixtures.py``), not the
SQLite ``dal``/``client`` fixtures: ``storage_backends.id`` and
``storage_backends.cluster_id`` are genuine ``UUID`` columns in the real
schema (``app.models_m1.StorageBackend``), and SQLite has no native UUID
type -- comparing a Python ``uuid.UUID`` against a SQLite TEXT column (what
``test_clusters_extended.py``'s hand-rolled fixture falls back to) silently
never matches, which is exactly why those tests are marked
``xfail(reason="SQLite datatype mismatch with UUID id field")`` instead of
exercising the real comparison. Mirrors ``test_clusters_pg.py``'s
``_build_app``/``_passthrough_decorator``/``_seed_cluster`` pattern.

Flag-OFF (M1, default) regression coverage lives in ``test_clusters.py``/
``test_clusters_extended.py``/``test_clusters_coverage2.py`` and is
intentionally NOT duplicated here -- this file only exercises flag-ON.

Note: ``storage_backends.cluster_id`` is UUID-typed but ``clusters.id`` is
``String(255)`` (pre-existing schema inconsistency, not introduced here --
see this PR's report) -- every switch-primary test below therefore uses a
UUID-*formatted* string as its ``cluster_id`` so it satisfies both columns
at once, same as a real deployment would have to.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart, g

pytestmark = pytest.mark.asyncio


def _passthrough_decorator(*dargs: Any, **dkwargs: Any) -> Any:
    """Stub that replaces ``auth_required`` with a no-op (scope enforcement
    stays real -- ``app.api.clusters`` has its own local
    ``_scope_required``, satisfied via the injected JWT ``scope`` claim)."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def _wrap(fn: Any) -> Any:
        return fn

    return _wrap


def _build_app(
    *,
    monkeypatch: pytest.MonkeyPatch,
    dal_db: Any,
    tenant: str,
    scope: str,
) -> tuple[Quart, Any]:
    """Build an ephemeral Quart app with a freshly-reloaded ``clusters_bp``.

    Returns ``(app, clusters_mod)`` -- tests monkeypatch
    ``clusters_mod.feature_enabled`` / ``.storage_orchestrator`` /
    ``.cluster_discovery`` directly on the reloaded module.
    """
    import app.middleware as mw_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)

    import app.api.clusters as clusters_mod

    clusters_mod = importlib.reload(clusters_mod)
    monkeypatch.setattr(clusters_mod, "get_db", lambda: dal_db)

    application = Quart(__name__)
    application.config["TESTING"] = True
    application.register_blueprint(
        clusters_mod.clusters_bp, url_prefix="/api/v1/clusters"
    )

    @application.before_request
    async def inject_user() -> None:
        g.current_user = {
            "_jwt_payload": {
                "tenant": tenant,
                "scope": scope,
                "sub": "user-123",
                "cross_tenant": False,
            }
        }
        g.mfa_verified = True

    return application, clusters_mod


def _seed_cluster(owner_db: Any, *, cluster_id: str, tenant_id: str) -> None:
    now = datetime.now(timezone.utc)
    owner_db.clusters.insert(
        id=cluster_id,
        tenant_id=tenant_id,
        name=cluster_id,
        created_at=now,
        updated_at=now,
    )
    owner_db.commit()


def _seed_backend(
    owner_db: Any, *, cluster_id: str, name: str, is_default: bool, status: str
) -> str:
    now = datetime.now(timezone.utc)
    backend_id = str(uuid.uuid4())
    owner_db.storage_backends.insert(
        id=backend_id,
        cluster_id=cluster_id,
        name=name,
        kind="longhorn",
        is_default=is_default,
        config_json={},
        status=status,
        created_at=now,
        updated_at=now,
    )
    owner_db.commit()
    return backend_id


_ADMIN_SCOPE = (
    "gough.cluster.read gough.cluster.admin gough.storage.read gough.storage.configure"
)
_SUPERADMIN_SCOPE = "gough.cluster.read gough.cluster.superadmin"


# =============================================================================
# switch_primary_storage -- M2 real execution
# =============================================================================


class TestSwitchPrimaryStorageExecution:
    async def test_execution_success(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag ON + all stages succeed: DB flips primary, history
        recorded, 200 with completed_stages."""
        cluster_id = str(uuid.uuid4())
        _seed_cluster(pg_db, cluster_id=cluster_id, tenant_id="tenant-a")
        current_id = _seed_backend(
            pg_db,
            cluster_id=cluster_id,
            name="current",
            is_default=True,
            status="ready",
        )
        target_id = _seed_backend(
            pg_db,
            cluster_id=cluster_id,
            name="target",
            is_default=False,
            status="standby",
        )

        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_ADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=True)
        )

        async def _fake_execute_stage(**kwargs: Any) -> Any:
            return clusters_mod.storage_orchestrator.StageResult(
                stage=kwargs["stage"], ok=True
            )

        monkeypatch.setattr(
            clusters_mod.storage_orchestrator, "execute_stage", _fake_execute_stage
        )

        client = app.test_client()
        response = await client.post(
            f"/api/v1/clusters/{cluster_id}/storage/switch-primary",
            json={"target_backend_id": target_id, "reason": "capacity"},
        )
        assert response.status_code == 200
        data = await response.get_json()
        assert data["status"] == "success"
        assert data["data"]["completed_stages"] == [
            "preflight",
            "drain_writes",
            "snapshot_sync",
            "promote",
            "demote_old",
        ]
        assert data["data"]["rollback_performed"] is False
        assert data["data"]["current_primary"]["id"] == target_id

        old = pg_db(pg_db.storage_backends.id == current_id).select().first()
        new = pg_db(pg_db.storage_backends.id == target_id).select().first()
        assert old.is_default is False and old.status == "standby"
        assert new.is_default is True and new.status == "ready"

        history = (
            pg_db(
                (pg_db.cluster_config.cluster_id == cluster_id)
                & (pg_db.cluster_config.key == "storage_primary_switch_history")
            )
            .select()
            .first()
        )
        assert history is not None
        assert len(history.value_json) == 1
        assert history.value_json[0]["to"] == target_id

    async def test_execution_rollback_on_demote_failure(
        self,
        pg_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failure AFTER promote (at demote_old) reverts the DB flip -- the
        caller gets rollback_performed=True and backends end up exactly
        where they started."""
        cluster_id = str(uuid.uuid4())
        _seed_cluster(pg_db, cluster_id=cluster_id, tenant_id="tenant-a")
        current_id = _seed_backend(
            pg_db,
            cluster_id=cluster_id,
            name="current",
            is_default=True,
            status="ready",
        )
        target_id = _seed_backend(
            pg_db,
            cluster_id=cluster_id,
            name="target",
            is_default=False,
            status="standby",
        )

        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_ADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=True)
        )

        async def _fake_execute_stage(**kwargs: Any) -> Any:
            if kwargs["stage"] == "demote_old":
                raise clusters_mod.storage_orchestrator.StorageOrchestratorUnavailable(
                    "orchestrator down"
                )
            return clusters_mod.storage_orchestrator.StageResult(
                stage=kwargs["stage"], ok=True
            )

        monkeypatch.setattr(
            clusters_mod.storage_orchestrator, "execute_stage", _fake_execute_stage
        )

        client = app.test_client()
        response = await client.post(
            f"/api/v1/clusters/{cluster_id}/storage/switch-primary",
            json={"target_backend_id": target_id},
        )
        assert response.status_code == 502
        data = await response.get_json()
        assert data["error"] == "storage_migration_failed"
        assert data["data"]["failed_stage"] == "demote_old"
        assert data["data"]["rollback_performed"] is True

        old = pg_db(pg_db.storage_backends.id == current_id).select().first()
        new = pg_db(pg_db.storage_backends.id == target_id).select().first()
        assert old.is_default is True and old.status == "ready"
        assert new.is_default is False and new.status == "standby"

    async def test_execution_failure_before_promote_no_rollback(
        self,
        pg_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failure at preflight (before any DB write) needs no rollback --
        backends are untouched and rollback_performed is False."""
        cluster_id = str(uuid.uuid4())
        _seed_cluster(pg_db, cluster_id=cluster_id, tenant_id="tenant-a")
        current_id = _seed_backend(
            pg_db,
            cluster_id=cluster_id,
            name="current",
            is_default=True,
            status="ready",
        )
        target_id = _seed_backend(
            pg_db,
            cluster_id=cluster_id,
            name="target",
            is_default=False,
            status="standby",
        )

        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_ADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=True)
        )

        async def _fake_execute_stage(**kwargs: Any) -> Any:
            raise clusters_mod.storage_orchestrator.StorageOrchestratorUnavailable(
                "unreachable"
            )

        monkeypatch.setattr(
            clusters_mod.storage_orchestrator, "execute_stage", _fake_execute_stage
        )

        client = app.test_client()
        response = await client.post(
            f"/api/v1/clusters/{cluster_id}/storage/switch-primary",
            json={"target_backend_id": target_id},
        )
        assert response.status_code == 502
        data = await response.get_json()
        assert data["data"]["failed_stage"] == "preflight"
        assert data["data"]["rollback_performed"] is False
        assert data["data"]["completed_stages"] == []

        old = pg_db(pg_db.storage_backends.id == current_id).select().first()
        new = pg_db(pg_db.storage_backends.id == target_id).select().first()
        assert old.is_default is True
        assert new.is_default is False

    async def test_execution_schema_unavailable_fails_closed(
        self,
        pg_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flag ON but no storage backend resolves (target not seeded):
        the existing target_not_found 404 still applies -- M2 never
        fabricates a success from nothing to execute."""
        cluster_id = str(uuid.uuid4())
        _seed_cluster(pg_db, cluster_id=cluster_id, tenant_id="tenant-a")

        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_ADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=True)
        )

        client = app.test_client()
        response = await client.post(
            f"/api/v1/clusters/{cluster_id}/storage/switch-primary",
            json={"target_backend_id": str(uuid.uuid4())},
        )
        assert response.status_code == 404
        data = await response.get_json()
        assert data["error"] == "target_not_found"

    async def test_flag_off_still_preview(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: flag OFF on this same real-Postgres setup still
        returns the unchanged M1 preview, never attempts execution."""
        cluster_id = str(uuid.uuid4())
        _seed_cluster(pg_db, cluster_id=cluster_id, tenant_id="tenant-a")
        target_id = _seed_backend(
            pg_db,
            cluster_id=cluster_id,
            name="target",
            is_default=False,
            status="standby",
        )

        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_ADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=False)
        )

        client = app.test_client()
        response = await client.post(
            f"/api/v1/clusters/{cluster_id}/storage/switch-primary",
            json={"target_backend_id": target_id},
        )
        assert response.status_code == 202
        data = await response.get_json()
        assert "M2" in data["data"]["note"]

        unchanged = pg_db(pg_db.storage_backends.id == target_id).select().first()
        assert unchanged.is_default is False


# =============================================================================
# adopt_cluster -- M2 real execution
# =============================================================================


class TestAdoptClusterExecution:
    async def test_execution_success_creates_cluster_and_inventory(
        self,
        pg_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flag ON + discovery succeeds: a new clusters row is created
        (status ready) and the member inventory lands in cluster_config."""
        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_SUPERADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=True)
        )

        members = [
            clusters_mod.cluster_discovery.DiscoveredMember(
                name="node-a",
                address="10.0.0.1",
                role="leader",
                status="online",
            ),
            clusters_mod.cluster_discovery.DiscoveredMember(
                name="node-b",
                address="10.0.0.2",
                role=None,
                status="online",
            ),
        ]
        monkeypatch.setattr(
            clusters_mod.cluster_discovery,
            "discover_inventory",
            AsyncMock(return_value=members),
        )

        client = app.test_client()
        response = await client.post(
            "/api/v1/clusters/newcluster/adopt",
            json={
                "object_type": "lxd_cluster",
                "endpoint": "https://lxd.example.internal:8443",
                "credentials_ref": "vault:secret/data/newcluster",
                "reason": "brownfield_adoption",
            },
        )
        assert response.status_code == 200
        data = await response.get_json()
        assert data["status"] == "success"
        assert data["data"]["member_count"] == 2
        assert data["data"]["kind"] == "lxd"

        row = pg_db(pg_db.clusters.id == "newcluster").select().first()
        assert row is not None
        assert row.status == "ready"

        doc = (
            pg_db(
                (pg_db.cluster_config.cluster_id == "newcluster")
                & (pg_db.cluster_config.key == "adopted_inventory")
            )
            .select()
            .first()
        )
        assert doc is not None
        assert doc.value_json["member_count"] == 2
        assert doc.value_json["members"][0]["name"] == "node-a"

    async def test_execution_rollback_deletes_freshly_created_cluster(
        self,
        pg_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Discovery fails on a brand-new cluster_id: the clusters row this
        request created is rolled back (deleted)."""
        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_SUPERADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            clusters_mod.cluster_discovery,
            "discover_inventory",
            AsyncMock(
                side_effect=clusters_mod.cluster_discovery.ClusterDiscoveryUnavailable(
                    "unreachable"
                )
            ),
        )

        client = app.test_client()
        response = await client.post(
            "/api/v1/clusters/brandnew/adopt",
            json={"kind": "lxd", "endpoint": "https://lxd.example.internal:8443"},
        )
        assert response.status_code == 502
        data = await response.get_json()
        assert data["error"] == "cluster_adoption_failed"
        assert data["data"]["rollback_performed"] is True

        row = pg_db(pg_db.clusters.id == "brandnew").select().first()
        assert row is None

    async def test_execution_failure_preserves_preexisting_cluster(
        self,
        pg_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Discovery fails on a re-adopt of an already-known cluster: the
        pre-existing row is untouched, never deleted by the rollback path."""
        _seed_cluster(pg_db, cluster_id="known", tenant_id="tenant-a")

        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_SUPERADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            clusters_mod.cluster_discovery,
            "discover_inventory",
            AsyncMock(
                side_effect=clusters_mod.cluster_discovery.ClusterDiscoveryUnavailable(
                    "unreachable"
                )
            ),
        )

        client = app.test_client()
        response = await client.post(
            "/api/v1/clusters/known/adopt",
            json={"kind": "k8s", "endpoint": "https://k8s.example.internal:6443"},
        )
        assert response.status_code == 502
        data = await response.get_json()
        assert data["data"]["rollback_performed"] is False

        row = pg_db(pg_db.clusters.id == "known").select().first()
        assert row is not None

    async def test_execution_missing_endpoint_422(
        self,
        pg_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_SUPERADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=True)
        )

        client = app.test_client()
        response = await client.post(
            "/api/v1/clusters/newcluster/adopt",
            json={"kind": "lxd"},
        )
        assert response.status_code == 422
        data = await response.get_json()
        assert data["details"]["violations"][0]["code"] == "missing_endpoint"

    async def test_execution_invalid_kind_422(
        self,
        pg_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_SUPERADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=True)
        )

        client = app.test_client()
        response = await client.post(
            "/api/v1/clusters/newcluster/adopt",
            json={"object_type": "unknown_type", "endpoint": "https://x"},
        )
        assert response.status_code == 422
        data = await response.get_json()
        assert data["details"]["violations"][0]["code"] == "invalid_kind"

    async def test_flag_off_still_m1_request_only(
        self,
        pg_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: flag OFF on this same real-Postgres setup still only
        emits the M1 request event, never touches the DB."""
        app, clusters_mod = _build_app(
            monkeypatch=monkeypatch,
            dal_db=pg_db,
            tenant="tenant-a",
            scope=_SUPERADMIN_SCOPE,
        )
        monkeypatch.setattr(
            clusters_mod, "feature_enabled", AsyncMock(return_value=False)
        )

        client = app.test_client()
        response = await client.post(
            "/api/v1/clusters/untouched/adopt",
            json={"kind": "lxd", "endpoint": "https://lxd.example.internal:8443"},
        )
        assert response.status_code == 202
        data = await response.get_json()
        assert data["data"]["event"] == "gough.cluster.adopt.requested"

        row = pg_db(pg_db.clusters.id == "untouched").select().first()
        assert row is None
