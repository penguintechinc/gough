"""Real-Postgres tests for the penguin-dal conversion of ``app.workers.plan_compiler``.

Covers the three read sites converted off raw SQLAlchemy ``Session.execute
(text(...))`` onto penguin-dal (task-7b runtime-DAL migration):

* ``PlanCompiler._load_node`` (former ``SELECT * FROM nodes WHERE id = :id``)
* ``PlanCompiler._load_eggs`` (former ``SELECT * FROM biomes WHERE id IN (...)``)
* ``_resolve_joiner_secrets`` (former parameterized ``SELECT id FROM
  joiner_secrets WHERE ...``)

``PlanCompiler`` is constructed in exactly one place in this codebase --
``app.api.nodes.deploy_node``, a Quart request handler that has already
called ``app.models.get_db()`` (i.e. run *after* ``tenant_middleware`` set
the request's tenant on ``app.db.rls``'s ContextVar) -- so none of these
three reads apply an app-level tenant filter; they rely entirely on ambient
Postgres RLS carrying the request's tenant onto the connection. This is
the opposite trust boundary from ``app.workers.joiner_secret_emitter`` /
``app.grpc_server`` (which run with no request context and must explicitly
push ``app.db.rls.CROSS_TENANT_SENTINEL``) -- see ``PlanCompiler.__init__``'s
docstring for the full reasoning and ``tests/test_rls_isolation.py`` for the
generic two-tenant RLS proof this file specializes to ``PlanCompiler``.

Uses ``pg_db`` (table OWNER, RLS-exempt) to seed fixture rows and
``pg_db_scoped`` (the real ``api-manager-rw`` role the baseline migration
``GRANT``s to) as ``PlanCompiler.db_session``/``_resolve_joiner_secrets``'s
``db_session`` argument, so Row Level Security is genuinely enforced --
matching ``tests/test_rls_isolation.py`` and ``tests/test_grpc_server.py``'s
``TestCrossTenantScopeRLS``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

from app.db.rls import install_rls_events, set_current_tenant

from app.workers.plan_compiler import (
    DiskPlanInput,
    EggAssignmentRef,
    PlanCompiler,
    PlanRequest,
    _EggNode,
    _resolve_joiner_secrets,
)

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


# =============================================================================
# Seed helpers (real penguin-dal inserts -- pg_db is the owner role, RLS-exempt)
# =============================================================================


def _seed_node(dal_db: Any, **overrides: Any) -> int:
    """created_at/updated_at have no server-side DEFAULT (ORM-side
    ``default=`` only) -- supplied explicitly, matching every other
    direct-insert seed helper in this test suite (see
    ``tests/test_grpc_server.py``)."""
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        name=f"node-{uuid.uuid4().hex[:8]}",
        state="ready",
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.nodes.insert(**base))


def _seed_biome(dal_db: Any, **overrides: Any) -> int:
    base: dict[str, Any] = dict(name=f"biome-{uuid.uuid4().hex[:8]}")
    base.update(overrides)
    return int(dal_db.biomes.insert(**base))


def _seed_joiner_secret(dal_db: Any, *, emitter_biome_id: int, **overrides: Any) -> str:
    """Insert a real ``joiner_secrets`` row; return its id (str).

    ``_resolve_joiner_secrets`` never decrypts the row -- only its
    existence/id matters here, so ciphertext/iv/auth_tag/dek_wrapped are
    dummy (but present, since all four are NOT NULL) bytes rather than a
    genuine Vault-wrapped envelope.
    """
    base: dict[str, Any] = dict(
        id=str(uuid.uuid4()),
        cluster_id=str(uuid.uuid4()),
        biome_kind="k8s-primary",
        emitter_biome_id=emitter_biome_id,
        emitter_node_id=None,
        extractor_name="kubeadm_join_token",
        scope="cluster",
        ciphertext=b"secret-bytes",
        iv=b"IIIIIIIIIIII",
        auth_tag=b"TTTTTTTTTTTTTTTT",
        dek_wrapped=b"vault:v1:DEK",
        vault_kek_name="gough-joiner-dek-wrap",
        ttl_seconds=3600,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    base["id"] = str(base["id"])
    base["cluster_id"] = str(base["cluster_id"])
    return str(dal_db.joiner_secrets.insert(**base))


def _make_compiler(db_session: Any) -> PlanCompiler:
    return PlanCompiler(db_session=db_session, vault_client=Mock(), lxd_client=Mock())


# =============================================================================
# PlanCompiler._load_node
# =============================================================================


class TestLoadNodeAmbientTenantScoping:
    def test_negative_control_no_tenant_set_sees_nothing(
        self, pg_db: Any, pg_db_scoped: Any
    ) -> None:
        """Proves the scoped role's RLS is genuinely active for `nodes`
        absent any tenant context -- same negative-control style as
        ``tests/test_rls_isolation.py``. If this ever starts passing with
        rows visible, the tests below asserting tenant isolation are not
        proving anything."""
        node_id = _seed_node(pg_db, tenant_id=TENANT_A)
        install_rls_events(pg_db_scoped.engine)
        set_current_tenant(None)

        compiler = _make_compiler(pg_db_scoped)
        assert compiler._load_node(node_id) is None

    def test_own_tenant_row_visible_cross_tenant_row_hidden(
        self, pg_db: Any, pg_db_scoped: Any
    ) -> None:
        """PlanCompiler._load_node applies NO app-level tenant filter --
        this proves ambient RLS (the request's tenant, already set on the
        ContextVar by tenant_middleware before PlanCompiler is ever
        constructed) is what scopes the read, not an accident of the
        fixture data."""
        node_a_id = _seed_node(pg_db, tenant_id=TENANT_A)
        node_b_id = _seed_node(pg_db, tenant_id=TENANT_B)
        install_rls_events(pg_db_scoped.engine)
        set_current_tenant(TENANT_A)
        try:
            compiler = _make_compiler(pg_db_scoped)
            own_row = compiler._load_node(node_a_id)
            cross_tenant_row = compiler._load_node(node_b_id)
        finally:
            set_current_tenant(None)

        assert own_row is not None
        assert own_row.id == node_a_id
        assert own_row.tenant_id == TENANT_A
        assert cross_tenant_row is None


# =============================================================================
# PlanCompiler._load_eggs
# =============================================================================


class TestLoadEggsAmbientTenantScoping:
    def test_own_tenant_biome_loaded_cross_tenant_biome_reported_missing(
        self, pg_db: Any, pg_db_scoped: Any
    ) -> None:
        """A cross-tenant biome_id is indistinguishable from a genuinely
        nonexistent one under RLS -- _load_eggs reports it via the normal
        ``egg_not_found`` path, not a separate "forbidden" error."""
        biome_a_id = _seed_biome(pg_db, name="biome-a", tenant_id=TENANT_A)
        biome_b_id = _seed_biome(pg_db, name="biome-b", tenant_id=TENANT_B)
        install_rls_events(pg_db_scoped.engine)
        set_current_tenant(TENANT_A)
        try:
            compiler = _make_compiler(pg_db_scoped)
            plan_request = PlanRequest(
                biome_assignments=[
                    EggAssignmentRef(biome_id=biome_a_id),
                    EggAssignmentRef(biome_id=biome_b_id),
                ],
                disk_plan=DiskPlanInput(),
                cluster_id="test-cluster",
            )
            biome_nodes, errors = compiler._load_eggs(plan_request)
        finally:
            set_current_tenant(None)

        assert set(biome_nodes.keys()) == {biome_a_id}
        assert biome_nodes[biome_a_id].biome_name == "biome-a"
        assert len(errors) == 1
        assert errors[0].code == "egg_not_found"
        assert errors[0].details["biome_id"] == biome_b_id


# =============================================================================
# _resolve_joiner_secrets
# =============================================================================


class TestResolveJoinerSecretsAmbientTenantScoping:
    def test_own_tenant_secret_resolved_cross_tenant_secret_unavailable(
        self, pg_db: Any, pg_db_scoped: Any
    ) -> None:
        """joiner_secrets is one of the baseline's rls_tables too --
        ``_resolve_joiner_secrets`` (a module-level helper, not a
        PlanCompiler method) gets the same ambient-tenant treatment."""
        biome_a = _seed_biome(pg_db, tenant_id=TENANT_A)
        biome_b = _seed_biome(pg_db, tenant_id=TENANT_B)
        cluster_a = str(uuid.uuid4())
        cluster_b = str(uuid.uuid4())
        secret_a_id = _seed_joiner_secret(
            pg_db, emitter_biome_id=biome_a, tenant_id=TENANT_A, cluster_id=cluster_a
        )
        _seed_joiner_secret(
            pg_db, emitter_biome_id=biome_b, tenant_id=TENANT_B, cluster_id=cluster_b
        )

        install_rls_events(pg_db_scoped.engine)
        set_current_tenant(TENANT_A)
        now = datetime.now(timezone.utc)
        try:
            egg_node = _EggNode(
                egg_id=1,
                egg_name="consumer",
                phase="post_deploy",
                consumes_joiner_secrets_from=[
                    {
                        "biome_kind": "k8s-primary",
                        "extractor_name": "kubeadm_join_token",
                        "scope": "cluster",
                    }
                ],
            )
            own_refs, own_errors = _resolve_joiner_secrets(
                pg_db_scoped, egg_node, cluster_a, now
            )
            cross_tenant_refs, cross_tenant_errors = _resolve_joiner_secrets(
                pg_db_scoped, egg_node, cluster_b, now
            )
        finally:
            set_current_tenant(None)

        assert own_errors == []
        assert own_refs == [secret_a_id]

        assert cross_tenant_refs == []
        assert len(cross_tenant_errors) == 1
        assert cross_tenant_errors[0].code == "joiner_secret_unavailable"
