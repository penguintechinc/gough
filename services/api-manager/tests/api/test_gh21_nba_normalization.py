"""Real-Postgres regression tests for gh-21 (phantom ``node_biome_assignments``
table normalization).

Every callsite touched by this fix used to reference ``db.node_biome_assignments``
-- a table name that never existed anywhere in the schema (Alembic baseline
only ever created ``node_egg_assignments``, see ``app.models_m1.NodeBiomeAssignment
.__tablename__``). The bugs this hid varied by callsite but shared one root
cause -- these tests each seed a real ``node_egg_assignments`` row via the
``pg_db`` fixture (real Postgres, not a hand-rolled sqlite mock that happened
to define a table under the phantom name) and prove the now-corrected code
path actually uses it:

- ``app.grpc_server.BiomesServicer.DeployBiome`` -- covered separately in
  ``tests/test_grpc_server.py::TestDeployBiome`` (always raised RuntimeError).
- ``app.api.migration._load_biome_snapshot`` -- always returned None, so
  ``POST /api/v1/migration/biome/<id>`` 404'd unconditionally.
- ``app.api.biomes.delete_biome`` -- the active-assignment guard never ran
  (``hasattr(db, "node_biome_assignments")`` was always False), so a biome
  could be hard/soft deleted while still actively assigned to a node.
- ``app.api.nodes.deploy_node`` / ``assign_biome_to_node`` /
  ``list_node_biomes`` -- the ``_get_assignments_table`` two-name
  preference/fallback always resolved to ``node_egg_assignments`` in
  practice; this also covers the ``_serialize_assignment`` egg_id->biome_id
  response-field mapping bug that came with collapsing that helper.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from quart import Quart, g


def _passthrough_decorator(*dargs: Any, **dkwargs: Any) -> Any:
    """Stub that replaces auth_required / require_scopes with no-ops."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def _wrap(fn: Any) -> Any:
        return fn

    return _wrap


def _seed_node(dal_db: Any, **overrides: Any) -> int:
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        name=f"node-{uuid.uuid4().hex[:8]}",
        state="ready",
        tenant_id="acme",
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.nodes.insert(**base))


def _seed_biome(dal_db: Any, **overrides: Any) -> int:
    base: dict[str, Any] = dict(name=f"biome-{uuid.uuid4().hex[:8]}", tenant_id="acme")
    base.update(overrides)
    return int(dal_db.biomes.insert(**base))


def _seed_assignment(dal_db: Any, *, node_id: int, egg_id: int, **overrides: Any) -> int:
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        node_id=node_id,
        egg_id=egg_id,
        tenant_id="acme",
        phase="post_deploy",
        status="ready",
        assigned_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.node_egg_assignments.insert(**base))


@pytest.mark.asyncio
class TestMigrationTriggerRealTable:
    """gh-21 site 2: ``app.api.migration._load_biome_snapshot``."""

    async def test_trigger_migration_finds_real_assignment(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before the fix, this unconditionally 404'd -- the phantom-table
        ``hasattr``-equivalent check never found a table by that name, so
        ``_load_biome_snapshot`` always returned None regardless of whether
        a real assignment existed. Seeds a real Postgres ``node_egg_assignments``
        row and proves the trigger endpoint finds it (not 404)."""
        src_node_id = _seed_node(pg_db)
        dst_node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db, lock_to_host=False)
        assignment_id = _seed_assignment(
            pg_db, node_id=src_node_id, egg_id=biome_id, status="ready"
        )
        pg_db.commit()

        import app.middleware as mw_mod
        import app.security.scope_enforcement as scope_mod

        monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
        monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

        import app.api.migration as migration_mod

        migration_mod = importlib.reload(migration_mod)
        monkeypatch.setattr(migration_mod, "get_db", lambda: pg_db)

        # min_healthy_nodes=1 so the seeded 2-node cluster clears the
        # cluster-health gate in app.workers.migration_engine.evaluate.
        # ``id`` is physically VARCHAR(36) (app.models_m1.UUID), no
        # server-side default -- supplied explicitly like every other
        # direct-insert seed in this suite.
        now = datetime.now(timezone.utc)
        pg_db.migration_policy.insert(
            id=str(uuid.uuid4()),
            cluster_id="test-cluster",
            enabled=False,
            min_healthy_nodes=1,
            max_concurrent_migrations=1,
            created_at=now,
            updated_at=now,
        )
        pg_db.commit()

        app = Quart(__name__)
        app.config["TESTING"] = True
        app.config["CLUSTER_ID"] = "test-cluster"
        app.register_blueprint(migration_mod.migration_bp, url_prefix="/api/v1/migration")

        @app.before_request
        async def _inject_context() -> None:
            g.current_user = {
                "_jwt_payload": {
                    "sub": "admin",
                    "tenant": "acme",
                    "scope": "gough.migration.trigger",
                },
            }
            g.tenant_context = SimpleNamespace(tenant_id="acme")
            g.principal = SimpleNamespace(
                sub="admin",
                scopes=frozenset({"gough.migration.trigger"}),
                mfa_verified=True,
            )
            g.cluster_id = "test-cluster"
            g.mfa_verified = True

        client = app.test_client()
        response = await client.post(
            f"/api/v1/migration/biome/{assignment_id}",
            json={"target_node_id": dst_node_id, "reason": "gh-21 regression"},
        )

        assert response.status_code == 202, await response.get_data(as_text=True)
        data = await response.get_json()
        assert data["data"]["biome_instance_id"] == assignment_id
        assert data["data"]["src_node_id"] == src_node_id


@pytest.mark.asyncio
class TestBiomeDeleteBlocksOnActiveAssignment:
    """gh-21 site 3: ``app.api.biomes.delete_biome`` in-use guard."""

    async def test_delete_blocked_while_actively_assigned(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db)
        _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id, status="ready")
        pg_db.commit()

        import app.middleware as mw_mod
        import app.security.scope_enforcement as scope_mod

        monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
        monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

        import app.api.biomes as biomes_mod

        biomes_mod = importlib.reload(biomes_mod)
        monkeypatch.setattr(biomes_mod, "get_db", lambda: pg_db)

        app = Quart(__name__)
        app.config["TESTING"] = True
        app.register_blueprint(biomes_mod.biomes_bp)

        @app.before_request
        async def _inject_context() -> None:
            g.current_user = {
                "_jwt_payload": {
                    "sub": "admin",
                    "tenant": "acme",
                    "scope": "gough.biomes.author gough.cluster.admin",
                },
            }
            g.tenant_context = SimpleNamespace(tenant_id="acme")

        client = app.test_client()
        response = await client.delete(f"/api/v1/biomes/{biome_id}")

        assert response.status_code == 409, await response.get_data(as_text=True)
        data = await response.get_json()
        assert data["error"]["details"]["active_assignments"] == 1

    async def test_delete_succeeds_once_unassigned(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: no active assignment row -> delete proceeds normally."""
        biome_id = _seed_biome(pg_db)
        pg_db.commit()

        import app.middleware as mw_mod
        import app.security.scope_enforcement as scope_mod

        monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
        monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

        import app.api.biomes as biomes_mod

        biomes_mod = importlib.reload(biomes_mod)
        monkeypatch.setattr(biomes_mod, "get_db", lambda: pg_db)

        app = Quart(__name__)
        app.config["TESTING"] = True
        app.register_blueprint(biomes_mod.biomes_bp)

        @app.before_request
        async def _inject_context() -> None:
            g.current_user = {
                "_jwt_payload": {
                    "sub": "admin",
                    "tenant": "acme",
                    "scope": "gough.biomes.author gough.cluster.admin",
                },
            }
            g.tenant_context = SimpleNamespace(tenant_id="acme")

        client = app.test_client()
        response = await client.delete(f"/api/v1/biomes/{biome_id}")

        assert response.status_code == 200, await response.get_data(as_text=True)


@pytest.mark.asyncio
class TestNodeDeployAssignRoundtrip:
    """gh-21 site 4: ``app.api.nodes._get_assignments_table`` collapse +
    ``_serialize_assignment``'s egg_id -> biome_id response mapping."""

    async def _build_app(self, pg_db: Any, monkeypatch: pytest.MonkeyPatch) -> Quart:
        import app.middleware as mw_mod
        import app.security.scope_enforcement as scope_mod

        monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
        monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

        import app.api.nodes as nodes_mod

        nodes_mod = importlib.reload(nodes_mod)
        monkeypatch.setattr(nodes_mod, "get_db", lambda: pg_db)

        app = Quart(__name__)
        app.config["TESTING"] = True
        app.register_blueprint(nodes_mod.nodes_bp)

        @app.before_request
        async def _inject_context() -> None:
            g.current_user = {
                "_jwt_payload": {"sub": "admin", "tenant": "acme", "scope": ""},
            }
            g.tenant_context = SimpleNamespace(tenant_id="acme")

        return app

    async def test_deploy_creates_real_assignment_and_lists_it(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db, phase="post_deploy")
        pg_db.commit()

        app = await self._build_app(pg_db, monkeypatch)
        client = app.test_client()

        deploy_resp = await client.post(
            f"/api/v1/nodes/{node_id}/deploy",
            json={"biome_assignments": [{"biome_id": biome_id}]},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert deploy_resp.status_code == 202, await deploy_resp.get_data(as_text=True)
        deploy_data = await deploy_resp.get_json()
        assignment_ids = deploy_data["data"]["assignment_ids"]
        assert len(assignment_ids) == 1

        row = pg_db(pg_db.node_egg_assignments.id == assignment_ids[0]).select().first()
        assert row is not None
        assert row.egg_id == biome_id
        assert row.node_id == node_id

        list_resp = await client.get(f"/api/v1/nodes/{node_id}/biomes")
        assert list_resp.status_code == 200
        list_data = await list_resp.get_json()
        assert list_data["data"]["total"] == 1
        # Regression: _serialize_assignment used to read the nonexistent
        # ``biome_id``/``depends_on_biome_instance_id`` attributes off a row
        # that only has ``egg_id``/``depends_on_egg_instance_id``, silently
        # returning None for both via the tolerant _g() getter.
        assert list_data["data"]["assignments"][0]["biome_id"] == biome_id

    async def test_legacy_assign_endpoint_response_maps_egg_id_to_biome_id(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db, phase="post_deploy")
        pg_db.commit()

        app = await self._build_app(pg_db, monkeypatch)
        client = app.test_client()

        response = await client.post(
            f"/api/v1/nodes/{node_id}/biomes",
            json={"biome_id": biome_id, "phase": "post_deploy"},
        )
        assert response.status_code == 201, await response.get_data(as_text=True)
        data = await response.get_json()
        assert data["data"]["assignment"]["biome_id"] == biome_id
