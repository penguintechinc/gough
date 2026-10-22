"""Baseline migration: full schema from ORM models.

Revision ID: 20260805_1000_baseline
Revises:
Create Date: 2026-08-05 10:00:00.000000

Gough has never gone to production -- there is no migrated database whose
history must be preserved. This migration replaces the entire prior chain
(which had drifted from the ORM models and never applied cleanly against a
fresh database -- missing/renamed tables, wrong FK targets, an unusably
short alembic_version.version_num column, a literal-string database name in
a GRANT statement) with a single baseline that builds the schema directly
from app.models_sqlalchemy.Base (which app.models_m1 registers all M1 tables
onto) and app.db.init_db.Base. This guarantees model/migration parity by
construction: every column type, default, and index is exactly what the
models declare, including the dialect-guarded constructs already present in
the models (nodes.hardware_tags JSONB+GIN, joiner_secrets partial indexes,
webhook_endpoints JSONB) -- so this migration is portable across PostgreSQL,
MySQL/MariaDB Galera, and SQLite the same way the models already are.

Three tables are used at runtime via penguin-dal but have no SQLAlchemy
model in either Base (pre-existing gap, not introduced here -- see
tests/pg_fixtures.py's module docstring): ``vault_bootstrap_tokens`` and
``alert_rules``. Their DDL is carried over verbatim from the migrations that
used to create them so those tables keep being created; giving them proper
models is a follow-up, not done here. ``upgrade_runs`` was a third such table
until app.models_m1.UpgradeRun was added -- create_all() now builds it and its
raw DDL has been removed from below.

Also carries over, verbatim in intent, four PostgreSQL views, five DB roles
with grants, and row-level-security policies that were previously created by
dedicated migrations -- none of this is representable in SQLAlchemy
metadata, so it runs as raw postgres-only ``op.execute()`` calls after the
model-driven ``create_all()``. The database-name GRANT bug (a literal
"current_database" identifier instead of the actual database name) is fixed
here rather than carried over.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260805_1000_baseline'
down_revision = None
branch_labels = None
depends_on = None


def _grant_insert_table_sequences(
    bind: sa.engine.Connection, tables: list[str], role: str
) -> None:
    """Grant USAGE + SELECT on every SERIAL/IDENTITY sequence backing a
    column of each table in ``tables``, for ``role`` (gh-22, FIX 5).

    Postgres-only (see the ``dialect != 'postgresql'`` guard around this
    function's only call site) -- MariaDB's ``AUTO_INCREMENT`` has no
    sequence object to grant on at all, so this concept doesn't apply
    there.

    Why this exists: ``GRANT ... ON <table> TO <role>`` does NOT include
    privileges on the sequence backing that table's autoincrement column.
    Without ``USAGE`` on the sequence, every INSERT relying on the
    column's ``nextval(...)`` DEFAULT fails with "permission denied for
    sequence <name>" even when the table grant is otherwise perfectly
    correct -- discovered via ``node_events`` (gh-22 FIX 4), then found to
    apply identically to every other SERIAL-PK table this role can INSERT
    into (``nodes`` reproduces the same failure). ``SELECT`` is additionally
    required because penguin-dal's ``insert()`` compiles to SQLAlchemy's
    ``INSERT ... RETURNING``, and Postgres requires SELECT privilege on any
    column named in a RETURNING clause -- including the sequence-backed PK
    column being returned.

    Derived dynamically per table/column via ``pg_get_serial_sequence``
    rather than a hand-maintained table-type list -- roughly half of this
    role's INSERT-granted tables use a UUID or string PK with no sequence
    at all (``joiner_secrets``, ``audit_events``, ``storage_backends``,
    ``migration_events``, ``migration_policy``, ``dr_drills``) or a
    composite/FK-derived PK with no *owned* sequence (``node_bmc``,
    ``leader_leases``) -- ``pg_get_serial_sequence`` returns NULL for all
    of those and they're silently skipped, exactly the "no sequence" case
    this must handle gracefully. Least-privilege: only sequences backing
    tables this specific role actually has INSERT on, nothing broader.
    """
    for tbl in tables:
        columns = (
            bind.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :tbl"
                ),
                {"tbl": tbl},
            )
            .scalars()
            .all()
        )
        for col in columns:
            seq = bind.execute(
                sa.text("SELECT pg_get_serial_sequence(:tbl, :col)"),
                {"tbl": tbl, "col": col},
            ).scalar()
            if seq:
                op.execute(f'GRANT USAGE, SELECT ON SEQUENCE {seq} TO "{role}"')


def upgrade() -> None:
    """Build the full schema from ORM models, plus modelless tables/views/roles/RLS."""
    # Widen alembic_version.version_num up front. Alembic hardcodes this
    # column as VARCHAR(32); this revision's own id fits, but a future
    # descriptively-named revision easily won't (several previously did).
    # SQLite has no enforced VARCHAR length, so this is a no-op there.
    if op.get_bind().dialect.name != 'sqlite':
        op.alter_column(
            'alembic_version',
            'version_num',
            existing_type=sa.String(32),
            type_=sa.String(255),
        )

    import sys
    import types
    from pathlib import Path

    # app.models_m1 does `from .models_sqlalchemy import Base` -- a relative
    # import that only resolves if models_m1 is loaded as part of the "app"
    # package, so a bare `import models_m1` (the pattern env.py itself uses
    # for models_sqlalchemy) doesn't work here. But the real `app/__init__.py`
    # is the Quart application factory (imports Quart, penguin_aaa, wires
    # middleware, ...) -- entirely inappropriate to execute as a side effect
    # of a migration, and importing it here also triggers a real bug: env.py
    # already put app/ itself on sys.path, and once werkzeug does its own
    # `import secrets`, Python resolves that to app/secrets/ (a package in
    # this codebase) instead of the stdlib module, since app/ is directly on
    # sys.path. Register a lightweight stand-in "app" package instead --
    # skips app/__init__.py's body entirely while still making
    # `app.models_sqlalchemy` / `app.models_m1` / `app.db.init_db` resolve as
    # genuine submodules (relative imports and all), matching how
    # tests/pg_fixtures.py imports these same three names.
    service_root = Path(__file__).resolve().parent.parent.parent
    app_dir = service_root / "app"
    if "app" not in sys.modules:
        app_pkg = types.ModuleType("app")
        app_pkg.__path__ = [str(app_dir)]
        sys.modules["app"] = app_pkg
    if str(service_root) not in sys.path:
        sys.path.insert(0, str(service_root))

    from app.models_sqlalchemy import Base as MainBase
    # Importing models_m1 registers its classes (nodes, biomes,
    # node_egg_assignments, deployments, joiner_secrets, audit_events,
    # migration_events, migration_policy, leader_leases, dr_drills,
    # slo_definitions, node_bmc, hardware_firmware, node_tags_operator,
    # spiffe_trust_entries, storage_backends, disks, disk_plans, node_events,
    # webhook_endpoints, ...) onto MainBase.metadata -- it shares MainBase
    # rather than declaring its own (see app.models_sqlalchemy.create_all_tables,
    # which does the same import for exactly this reason).
    from app import models_m1  # noqa: F401
    from app.db.init_db import Base as InitBase

    bind = op.get_bind()
    MainBase.metadata.create_all(bind=bind, checkfirst=True)
    InitBase.metadata.create_all(bind=bind, checkfirst=True)

    dialect = bind.dialect.name

    # --- Modelless tables (pre-existing gap; DDL carried over verbatim from
    # 20260430_1100_plan4_security / 20260509_1000_create_upgrade_runs_table) ---
    op.create_table(
        "vault_bootstrap_tokens",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Integer(), nullable=True),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vault_bootstrap_tokens_node_id", "vault_bootstrap_tokens", ["node_id"])
    op.create_index("ix_vault_bootstrap_tokens_expires_at", "vault_bootstrap_tokens", ["expires_at"])

    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Numeric(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default=sa.literal(60)),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # upgrade_runs now has a real model (app.models_m1.UpgradeRun), so
    # create_all() above already built it -- the raw DDL that used to live
    # here would now fail with "table already exists". This was the
    # follow-up this migration's docstring called for.

    if dialect != 'postgresql':
        return

    # --- Postgres-only views (webui-ro role), carried over from
    # 20260427_2000_create_views_and_redactions ---
    op.execute("""
        CREATE VIEW v_nodes_public AS
        SELECT id, tenant_id, name, state, dmi_uuid, primary_nic_mac,
               ipv4, ipv6, hardware_tags, posture, discovered_at, deployed_at
        FROM nodes
    """)
    op.execute("""
        CREATE VIEW v_eggs_public AS
        SELECT id, name, display_name, description, version,
               egg_kind, phase, workload_type, lock_to_host,
               requires_hardware_tags, prefers_hardware_tags,
               forbids_hardware_tags, signing_key_id, sbom_url,
               is_active, is_default, created_at, updated_at
        FROM biomes
    """)
    op.execute("""
        CREATE VIEW v_capacity_public AS
        SELECT n.id AS node_id, n.tenant_id, n.name, n.state
        FROM nodes n
        WHERE n.state IN ('ready', 'draining', 'quarantined')
    """)
    op.execute("""
        CREATE VIEW v_audit_events_redacted AS
        SELECT id, ts, cluster_id, tenant_id, actor_sub, action,
               resource_kind, resource_id, request_id
        FROM audit_events
    """)

    # --- Per-service DB roles + grants, carried over from
    # 20260427_2030_create_db_roles_and_grants, fixing the literal
    # "current_database" identifier bug (GRANT ON DATABASE takes a literal
    # identifier, not an expression -- must look the name up and interpolate
    # it) and dropping the dangling "egg_groups" grant (no migration or model
    # anywhere ever created that table). ---
    db_name = bind.execute(sa.text('SELECT current_database()')).scalar()

    for role in ("api-manager-rw", "worker-ipxe-rw", "webui-ro", "audit-reader", "migration-runner"):
        op.execute(f'CREATE ROLE "{role}" WITH LOGIN')
    for role in ("api-manager-rw", "worker-ipxe-rw", "webui-ro", "audit-reader", "migration-runner"):
        op.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO "{role}"')
    for role in ("api-manager-rw", "worker-ipxe-rw", "webui-ro", "audit-reader", "migration-runner"):
        op.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')

    m1_tables = ['nodes', 'disks', 'disk_plans', 'node_egg_assignments', 'biomes',
                 'storage_backends', 'migration_events', 'migration_policy',
                 'joiner_secrets', 'node_bmc', 'hardware_firmware', 'spiffe_trust_entries',
                 'leader_leases', 'dr_drills', 'slo_definitions', 'node_tags_operator']
    for tbl in m1_tables:
        op.execute(f'GRANT SELECT, INSERT, UPDATE ON {tbl} TO "api-manager-rw"')
    op.execute('GRANT SELECT, INSERT ON audit_events TO "api-manager-rw"')
    op.execute('GRANT DELETE ON node_egg_assignments TO "api-manager-rw"')
    op.execute('GRANT DELETE ON migration_events TO "api-manager-rw"')
    op.execute('GRANT DELETE ON disk_plans TO "api-manager-rw"')
    # biomes: app.api.biomes.delete_biome's hard-delete path
    # (``db(db.biomes.id == biome_id).delete()``) needs DELETE in addition to
    # the SELECT/INSERT/UPDATE the m1_tables loop above already grants.
    op.execute('GRANT DELETE ON biomes TO "api-manager-rw"')
    # webhook_endpoints (app.models_m1.WebhookEndpoint) was simply left out
    # of the m1_tables list above, even though app.api.webhooks runs under
    # this same role in production (Config.DB_USER) -- same gap class as
    # audit_events/biomes. UPDATE is required too: the penguin-dal-converted
    # delete_webhook/create_webhook/list_webhooks/test_webhook handlers don't
    # currently issue UPDATEs, but webhook_endpoints has an updated_at column
    # future writers will need, and there's no reason this role should be
    # missing the one DML verb the m1_tables loop grants everyone else.
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_endpoints TO "api-manager-rw"')
    # node_events (app.models_m1.NodeEvent) -- same gap class as
    # webhook_endpoints/audit_events above: left out of the m1_tables list
    # entirely (gh-22), so "api-manager-rw" had no grant on it at all. The
    # only writer today is app.api.nodes' POST /nodes/{id}/events handler
    # (``db.node_events.insert(**row_data)``); no reader exists in app/ yet.
    # SELECT is required in addition to INSERT even though nothing SELECTs
    # the table directly: penguin-dal's insert() runs SQLAlchemy's
    # ``INSERT ... RETURNING`` to get the new row's id back, and Postgres
    # requires SELECT privilege on any column named in a RETURNING clause,
    # not just INSERT. No UPDATE/DELETE grant -- neither is used anywhere.
    op.execute('GRANT SELECT, INSERT ON node_events TO "api-manager-rw"')

    # --- gh-21: orphan tables (queried at runtime throughout app/api/ and
    # app/permissions.py, but had no model/migration anywhere) -- per-table
    # grants, matched to each table's approved profile in
    # .superpowers/sdd/followups/orphan-schemas-brief.md. Not folded into the
    # m1_tables loop above: several of these tables need a different verb
    # set than that loop's uniform SELECT/INSERT/UPDATE (some need DELETE,
    # boot_events needs no UPDATE at all), so bespoke GRANT lines keep each
    # table's actual privilege set explicit and reviewable, same pattern
    # already used above for webhook_endpoints/node_events. Added
    # incrementally across several commits as each table's model lands;
    # see this migration's git history for the per-table commit boundaries.
    op.execute('GRANT SELECT, INSERT, UPDATE ON clusters TO "api-manager-rw"')
    op.execute('GRANT SELECT, INSERT, UPDATE ON cluster_config TO "api-manager-rw"')
    op.execute('GRANT SELECT, INSERT, UPDATE ON storage_quotas TO "api-manager-rw"')
    op.execute('GRANT SELECT, INSERT, UPDATE ON storage_quota_requests TO "api-manager-rw"')
    # deployment_logs is SELECT-only in app.api.biomes.get_deployment_logs
    # today; INSERT is granted ahead of a future writer per the approved
    # profile, same "future writer" rationale as clusters/storage_quotas
    # above.
    op.execute('GRANT SELECT, INSERT ON deployment_logs TO "api-manager-rw"')
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON resource_permissions TO "api-manager-rw"')
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON biome_groups TO "api-manager-rw"')
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON ipxe_machines TO "api-manager-rw"')
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON ipxe_images TO "api-manager-rw"')
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON ipxe_boot_configs TO "api-manager-rw"')
    op.execute('GRANT SELECT, INSERT, UPDATE ON ipxe_config TO "api-manager-rw"')
    # boot_events is an append-only log written by app.api.ipxe._log_boot_event
    # -- no UPDATE/DELETE anywhere in the codebase. Only api-manager-rw writes
    # it today (no worker-ipxe code path touches boot_events), so unlike
    # audit_events there is no matching worker-ipxe-rw INSERT grant here.
    op.execute('GRANT SELECT, INSERT ON boot_events TO "api-manager-rw"')

    # gh-22 FIX 5: grant USAGE+SELECT on the PK sequence of every table each
    # role above was just given INSERT on -- see
    # ``_grant_insert_table_sequences``'s docstring for the full rationale
    # (this closes the class of bug node_events' own sequence grant,
    # FIX 4, was a single instance of).
    #
    # Every role this migration creates was checked for INSERT grants, not
    # just api-manager-rw: "webui-ro" and "audit-reader" are read-only
    # (SELECT-only, see below) and "migration-runner" only has database-
    # level ``GRANT ALL ON DATABASE`` (schema/DDL ownership, not a
    # table-level INSERT grant) -- neither needs sequence grants. Only
    # api-manager-rw and worker-ipxe-rw actually hold table-level INSERT
    # grants; both are covered here.
    api_manager_insert_tables = m1_tables + [
        "audit_events",
        "webhook_endpoints",
        "node_events",
        # gh-21 orphan tables -- every table given an INSERT grant above.
        # clusters uses a non-SERIAL (String) PK, so pg_get_serial_sequence
        # returns NULL for it and it's silently skipped by
        # ``_grant_insert_table_sequences`` -- included anyway (rather than
        # hand-picking only the SERIAL-PK ones) for the same reason
        # worker-ipxe-rw's audit_events entry above is: a future INSERT
        # target that changes PK strategy gets covered automatically.
        "clusters",
        "cluster_config",
        # storage_quotas/storage_quota_requests use a UUID-as-String PK
        # (app-generated uuid4), so pg_get_serial_sequence returns NULL for
        # both -- included for the same "future-proof, don't hand-pick"
        # reason as clusters above.
        "storage_quotas",
        "storage_quota_requests",
        "deployment_logs",
        "resource_permissions",
        "biome_groups",
        "ipxe_machines",
        "ipxe_images",
        "ipxe_boot_configs",
        "ipxe_config",
        "boot_events",
    ]
    _grant_insert_table_sequences(bind, api_manager_insert_tables, "api-manager-rw")

    op.execute('GRANT SELECT ON nodes TO "worker-ipxe-rw"')
    op.execute('GRANT SELECT ON node_egg_assignments TO "worker-ipxe-rw"')
    op.execute('GRANT SELECT ON biomes TO "worker-ipxe-rw"')
    op.execute('GRANT SELECT ON spiffe_trust_entries TO "worker-ipxe-rw"')
    op.execute('GRANT INSERT ON audit_events TO "worker-ipxe-rw"')
    # worker-ipxe-rw's only INSERT grant is audit_events, whose PK is a
    # UUID (app-generated UUIDv7, no sequence) -- pg_get_serial_sequence
    # returns NULL and this is a deliberate no-op, not a gap. Called anyway
    # (rather than hardcoding "skip this role") so a future INSERT grant
    # added to this role for a SERIAL-PK table gets covered automatically.
    _grant_insert_table_sequences(bind, ["audit_events"], "worker-ipxe-rw")

    op.execute('GRANT SELECT ON v_nodes_public TO "webui-ro"')
    op.execute('GRANT SELECT ON v_eggs_public TO "webui-ro"')
    op.execute('GRANT SELECT ON v_capacity_public TO "webui-ro"')
    op.execute('GRANT SELECT ON v_audit_events_redacted TO "webui-ro"')

    op.execute('GRANT SELECT ON audit_events TO "audit-reader"')
    op.execute(
        'GRANT SELECT (id, cluster_id, tenant_id, biome_kind, scope, ttl_seconds, '
        'expires_at, rotation_class, created_at, rotated_at, revoked_at) '
        'ON joiner_secrets TO "audit-reader"'
    )

    op.execute(f'GRANT ALL ON DATABASE "{db_name}" TO "migration-runner"')

    # --- Row-level security (tenant isolation), carried over from
    # 20260427_2030_create_db_roles_and_grants. migration_policy is
    # excluded -- unlike every other table here, app.models_m1.MigrationPolicy
    # has no tenant_id column (it's cluster-scoped, keyed by a unique
    # cluster_id, not tenant-scoped), so a tenant_isolation policy referencing
    # tenant_id would reference a column that doesn't exist.
    # gh-21: clusters is SECURITY-CRITICAL here -- RLS is a second,
    # defense-in-depth layer behind app.api.clusters._require_cluster_tenant's
    # application-level check. cluster_config is intentionally NOT in this
    # list (no tenant_id column -- cluster-scoped like migration_policy).
    # storage_quotas/storage_quota_requests are the PRIMARY enforcement here
    # (app.api.storage's handlers trust a caller-supplied/request-body
    # tenant_id with no cross-check of their own -- see
    # app.models_m1.StorageQuota/StorageQuotaRequest docstrings). biome_groups'
    # tenant_id defaults to '__default__' (current handlers don't set it --
    # see app.models_m1.BiomeGroup docstring), so every row is visible to the
    # default tenant under this policy's IN (tenant_id, '__default__',
    # '__all__') clause until a follow-up threads tenant through the handlers
    # -- accepted interim behavior per the approved profile, not a bug here.
    rls_tables = ['nodes', 'disks', 'disk_plans', 'node_egg_assignments', 'biomes',
                  'storage_backends', 'migration_events',
                  'joiner_secrets', 'audit_events', 'dr_drills', 'slo_definitions',
                  'hardware_firmware', 'node_tags_operator', 'node_bmc',
                  'webhook_endpoints',
                  'clusters', 'storage_quotas', 'storage_quota_requests', 'biome_groups']
    for tbl in rls_tables:
        op.execute(f'ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY')
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {tbl}
            USING (current_setting('app.current_tenant', true) IN (tenant_id, '__default__', '__all__'))
        """)

    # node_events has its own bespoke RLS policy (distinct name and USING
    # clause), carried over from 20260429_0900_node_events -- it was
    # intentionally never part of the generic rls_tables loop above.
    #
    # FIX (gh-22): the cross-tenant override sentinel here now matches the
    # app-wide one (app.db.rls.CROSS_TENANT_SENTINEL == '__all__'). The
    # original carried-over policy checked for '__super__' instead, which
    # nothing in the codebase ever sets -- app.db.rls.set_current_tenant()
    # only ever pushes a real tenant id or CROSS_TENANT_SENTINEL
    # ('__all__'), so a cross-tenant/super-admin caller got zero bypass on
    # this table specifically while getting one on every other RLS-enabled
    # table via the generic tenant_isolation policy above. Editing the
    # baseline in place is correct here -- Gough has never gone to
    # production, this migration has never applied against a real database
    # (see module docstring).
    op.execute('ALTER TABLE node_events ENABLE ROW LEVEL SECURITY')
    op.execute("""
        CREATE POLICY node_events_tenant_isolation
          ON node_events
          USING (
            tenant_id = current_setting('app.current_tenant', true)
            OR current_setting('app.current_tenant', true) = '__all__'
          )
    """)


def downgrade() -> None:
    """Not supported for the baseline -- drop and recreate the schema/database instead."""
    raise NotImplementedError(
        "Downgrading past the baseline is not supported. Drop and recreate "
        "the schema/database instead."
    )
