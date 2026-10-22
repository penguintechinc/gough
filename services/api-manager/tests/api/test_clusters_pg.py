"""Real-Postgres tests for ``app.api.clusters`` (gh-21).

Covers the SECURITY-CRITICAL regression this task exists to close: before
the ``clusters`` table existed, ``app.api.clusters._require_cluster_tenant``
could never look up a cluster's owning tenant
(``hasattr(db, "clusters")`` was always False), so the tenant-ownership
check on every ``/api/v1/clusters/<cluster_id>/*`` route silently no-opped
-- a fail-open cross-tenant IDOR gate. With the table in place, the same
guard now genuinely rejects cross-tenant requests with 404.

Mirrors ``tests/api/test_webhooks.py``'s ``_build_app``/``_passthrough_decorator``
pattern: build an ephemeral Quart app with a freshly-reloaded ``clusters_bp``,
``auth_required`` stubbed to a passthrough (bound at import/reload time, so
must be monkeypatched before reloading the module), and ``get_db`` pointed
at a real-Postgres ``penguin_dal.DB`` fixture.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from typing import Any

import pytest
from quart import Quart, g

pytestmark = pytest.mark.asyncio


def _passthrough_decorator(*dargs: Any, **dkwargs: Any) -> Any:
    """Stub that replaces ``auth_required`` with a no-op.

    ``app.api.clusters`` implements its own local ``_scope_required`` (not
    ``app.security.scope_enforcement.require_scopes``), so only
    ``auth_required`` needs stubbing -- scope enforcement stays real and is
    satisfied by setting the right ``scope`` claim on the injected JWT
    payload below.
    """
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
    scope: str = "gough.cluster.read",
    cross_tenant: bool = False,
) -> Quart:
    """Build an ephemeral Quart app with a freshly-reloaded ``clusters_bp``."""
    import app.middleware as mw_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)

    import app.api.clusters as clusters_mod

    clusters_mod = importlib.reload(clusters_mod)
    monkeypatch.setattr(clusters_mod, "get_db", lambda: dal_db)

    application = Quart(__name__)
    application.config["TESTING"] = True
    application.register_blueprint(clusters_mod.clusters_bp, url_prefix="/api/v1/clusters")

    @application.before_request
    async def inject_user() -> None:
        g.current_user = {
            "_jwt_payload": {
                "tenant": tenant,
                "scope": scope,
                "sub": "user-123",
                "cross_tenant": cross_tenant,
            }
        }

    return application


def _seed_cluster(owner_db: Any, *, cluster_id: str, tenant_id: str, name: str) -> None:
    """Insert a ``clusters`` row as the table OWNER (bypasses RLS on insert)."""
    now = datetime.now(timezone.utc)
    owner_db.clusters.insert(
        id=cluster_id, tenant_id=tenant_id, name=name,
        created_at=now, updated_at=now,
    )
    owner_db.commit()


class TestClusterTenantIdorGate:
    """The regression this task exists to close."""

    async def test_cross_tenant_request_404s(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """# regression: gh-21

        Seed a cluster owned by tenant-a. A request for that same cluster
        made with a tenant-b token must 404 -- not silently succeed. Before
        this task, with no ``clusters`` table, ``_require_cluster_tenant``'s
        ``hasattr(db, "clusters")`` check was always False and this request
        would have returned 200 with tenant-a's data.
        """
        _seed_cluster(pg_db, cluster_id="cluster-a", tenant_id="tenant-a", name="cluster-a")

        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db, tenant="tenant-b")
        client = app.test_client()

        response = await client.get("/api/v1/clusters/cluster-a/network-pools")

        assert response.status_code == 404
        body = await response.get_json()
        assert body["error"] == "Cluster not found"

    async def test_same_tenant_request_succeeds(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity: the gate must not also block the legitimate owning tenant."""
        _seed_cluster(pg_db, cluster_id="cluster-a", tenant_id="tenant-a", name="cluster-a")

        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db, tenant="tenant-a")
        client = app.test_client()

        response = await client.get("/api/v1/clusters/cluster-a/network-pools")

        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "success"
        assert body["data"]["cluster_id"] == "cluster-a"

    async def test_cross_tenant_super_admin_bypasses_gate(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cross_tenant=true (super-admin) token bypasses the ownership check by design."""
        _seed_cluster(pg_db, cluster_id="cluster-a", tenant_id="tenant-a", name="cluster-a")

        app = _build_app(
            monkeypatch=monkeypatch, dal_db=pg_db, tenant="tenant-b", cross_tenant=True,
        )
        client = app.test_client()

        response = await client.get("/api/v1/clusters/cluster-a/network-pools")

        assert response.status_code == 200

    async def test_nonexistent_cluster_still_404s(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cluster_id that matches no row at all must also 404 (not 500/other)."""
        app = _build_app(monkeypatch=monkeypatch, dal_db=pg_db, tenant="tenant-a")
        client = app.test_client()

        response = await client.get("/api/v1/clusters/does-not-exist/network-pools")

        assert response.status_code == 404


class TestClusterInsertSelectAsScopedRole:
    """Real-Postgres insert+select as the ``api-manager-rw`` scoped role."""

    async def test_insert_and_select_as_scoped_role(self, pg_db_scoped: Any) -> None:
        from app.db.rls import install_rls_events, set_current_tenant

        install_rls_events(pg_db_scoped.engine)
        set_current_tenant("tenant-a")
        try:
            now = datetime.now(timezone.utc)
            pg_db_scoped.clusters.insert(
                id="scoped-cluster", tenant_id="tenant-a", name="scoped-cluster",
                created_at=now, updated_at=now,
            )
            rows = pg_db_scoped(pg_db_scoped.clusters.id == "scoped-cluster").select()
        finally:
            set_current_tenant(None)

        assert len(rows) == 1
        assert rows[0].tenant_id == "tenant-a"
        assert rows[0].status == "ready"

    async def test_cluster_config_insert_and_select_as_scoped_role(
        self, pg_db: Any, pg_db_scoped: Any,
    ) -> None:
        """``cluster_config`` is cluster-scoped, not tenant-scoped -- no RLS,
        so this only needs to prove the grant (not tenant isolation)."""
        _seed_cluster(pg_db, cluster_id="cfg-cluster", tenant_id="tenant-a", name="cfg-cluster")

        row_id = pg_db_scoped.cluster_config.insert(
            cluster_id="cfg-cluster", key="network_pools", value_json=[{"name": "mgmt"}],
        )
        rows = pg_db_scoped(pg_db_scoped.cluster_config.id == row_id).select()

        assert len(rows) == 1
        assert rows[0].cluster_id == "cfg-cluster"
        assert rows[0].value_json == [{"name": "mgmt"}]
