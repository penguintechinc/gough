"""Two-tenant Row-Level-Security isolation test (FIX #7a).

Proves the actual production bug and its fix: Postgres RLS policies filter
every row to zero for the scoped ``api-manager-rw`` role unless the
``app.current_tenant`` GUC is pushed onto the SAME connection penguin-dal's
queries run on (``app.db.rls.install_rls_events`` + ``set_current_tenant``).

``pg_db`` (tests/pg_fixtures.py) connects as the migration/table OWNER,
which Postgres exempts from RLS entirely -- no test built on ``pg_db`` could
ever have caught this bug, which is exactly why it shipped unnoticed. Every
test here uses ``pg_db_scoped`` instead, which connects as the actual
``api-manager-rw`` role the baseline migration ``GRANT``s to, so RLS is
genuinely enforced.

Proof this test is sensitive to the wiring (not just to seed data): with
``install_rls_events()`` never called (or its checkout listener gutted), a
freshly checked-out ``api-manager-rw`` connection never has
``app.current_tenant`` set at all. ``current_setting('app.current_tenant',
true)`` then returns SQL NULL, ``NULL IN (tenant_id, '__default__',
'__all__')`` evaluates to NULL (not TRUE), and the RLS ``USING`` clause
treats NULL as "filter this row out" -- every assertion below expecting
tenant A to see its own row fails (0 rows instead of 1). Verified manually
while writing this test by commenting out the ``install_rls_events(...)``
call in each test below: every non-fail-closed assertion failed as
expected.
"""

from __future__ import annotations

import pytest
from penguin_dal import DB

from app.db.rls import CROSS_TENANT_SENTINEL, install_rls_events, set_current_tenant

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _seed_biome(owner_db: DB, *, name: str, tenant_id: str) -> int:
    """Insert a minimal ``biomes`` row as the table OWNER (bypasses RLS on insert)."""
    return owner_db.biomes.insert(name=name, tenant_id=tenant_id)


@pytest.fixture
def two_tenant_biomes(pg_db: DB) -> tuple[int, int]:
    """Seed one ``biomes`` row each for TENANT_A and TENANT_B, as the table owner.

    ``biomes`` is one of the tables the baseline migration enables RLS +
    the generic ``tenant_isolation`` policy on.
    """
    id_a = _seed_biome(pg_db, name="biome-a", tenant_id=TENANT_A)
    id_b = _seed_biome(pg_db, name="biome-b", tenant_id=TENANT_B)
    return id_a, id_b


def test_tenant_a_sees_only_own_row(
    pg_db_scoped: DB, two_tenant_biomes: tuple[int, int]
) -> None:
    """With the GUC set to tenant A, a scoped-role query returns ONLY tenant A's row."""
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        rows = pg_db_scoped(pg_db_scoped.biomes.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A}
    assert len(rows) == 1


def test_tenant_b_sees_only_own_row(
    pg_db_scoped: DB, two_tenant_biomes: tuple[int, int]
) -> None:
    """Symmetric check: tenant B's GUC value returns only tenant B's row, never tenant A's."""
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_B)
    try:
        rows = pg_db_scoped(pg_db_scoped.biomes.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_B}
    assert len(rows) == 1


def test_unset_tenant_is_fail_closed(
    pg_db_scoped: DB, two_tenant_biomes: tuple[int, int]
) -> None:
    """No tenant on the ContextVar -> zero rows, never every row (fail closed, not fail open).

    This is the case that most directly distinguishes "wiring present but no
    tenant set" from "wiring absent": both look like "zero rows" from this
    test alone, which is why ``test_tenant_a_sees_only_own_row`` (expecting
    a *non-empty*, tenant-scoped result) is the assertion that actually
    catches the GUC wiring being removed.
    """
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(None)

    rows = pg_db_scoped(pg_db_scoped.biomes.id > 0).select()

    assert len(rows) == 0


def test_cross_tenant_sentinel_sees_all_rows(
    pg_db_scoped: DB, two_tenant_biomes: tuple[int, int]
) -> None:
    """Super-admin (``__all__`` sentinel) bypasses tenant filtering on the generic RLS policy.

    ``__all__`` is a literal member of the ``tenant_isolation`` policy's
    ``IN`` list (see the baseline migration), so it matches every row
    regardless of that row's own ``tenant_id`` -- this is how
    ``TenantContext.cross_tenant=True`` tokens see across tenants.
    """
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        rows = pg_db_scoped(pg_db_scoped.biomes.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}
    assert len(rows) == 2


def test_owner_connection_is_rls_exempt(
    pg_db: DB, two_tenant_biomes: tuple[int, int]
) -> None:
    """Documents WHY ``pg_db`` can't be used to catch FIX #7a: owners bypass RLS entirely.

    Not a test of ``app.db.rls`` -- a regression guard on the test
    infrastructure itself, proving ``pg_db_scoped`` is exercising something
    ``pg_db`` structurally cannot (no GUC set here at all, and every row
    from both tenants still comes back).
    """
    rows = pg_db(pg_db.biomes.id > 0).select()

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}


# =============================================================================
# webhook_endpoints -- added to rls_tables by the penguin-dal runtime
# migration (task 6b); previously missing from the baseline's rls_tables
# list entirely, so it had no RLS policy at all regardless of GUC wiring.
#
# Queried directly here (``pg_db_scoped.webhook_endpoints.id > 0``, no
# ``tenant_id ==`` filter) rather than through the HTTP endpoint, because
# ``app.api.webhooks.list_webhooks`` already applies its own app-level
# ``tenant_id ==`` filter -- a test that goes through the endpoint can't
# distinguish "RLS isolates the rows" from "the app-level filter isolates
# the rows" (see tests/api/test_webhooks.py's
# ``test_list_rls_isolates_two_tenants`` docstring for that endpoint-level
# smoke test instead).
# =============================================================================


def _seed_webhook_endpoint(owner_db: DB, *, url: str, tenant_id: str) -> int:
    """Insert a minimal ``webhook_endpoints`` row as the table OWNER (bypasses
    RLS on insert). created_at/updated_at have no server-side DEFAULT (see
    the note in app.api.webhooks.create_webhook) -- supplied explicitly."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return owner_db.webhook_endpoints.insert(
        tenant_id=tenant_id, url=url, signing_mode="ed25519", active=True,
        event_filter=[], retry_policy={"mode": "standard"},
        created_at=now, updated_at=now,
    )


@pytest.fixture
def two_tenant_webhook_endpoints(pg_db: DB) -> tuple[int, int]:
    id_a = _seed_webhook_endpoint(
        pg_db, url="https://a.example.com/hook", tenant_id=TENANT_A
    )
    id_b = _seed_webhook_endpoint(
        pg_db, url="https://b.example.com/hook", tenant_id=TENANT_B
    )
    return id_a, id_b


def test_webhook_endpoints_tenant_a_sees_only_own_row(
    pg_db_scoped: DB, two_tenant_webhook_endpoints: tuple[int, int]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        rows = pg_db_scoped(pg_db_scoped.webhook_endpoints.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A}
    assert len(rows) == 1


def test_webhook_endpoints_tenant_b_sees_only_own_row(
    pg_db_scoped: DB, two_tenant_webhook_endpoints: tuple[int, int]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_B)
    try:
        rows = pg_db_scoped(pg_db_scoped.webhook_endpoints.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_B}
    assert len(rows) == 1


def test_webhook_endpoints_unset_tenant_is_fail_closed(
    pg_db_scoped: DB, two_tenant_webhook_endpoints: tuple[int, int]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(None)

    rows = pg_db_scoped(pg_db_scoped.webhook_endpoints.id > 0).select()

    assert len(rows) == 0


def test_webhook_endpoints_owner_connection_is_rls_exempt(
    pg_db: DB, two_tenant_webhook_endpoints: tuple[int, int]
) -> None:
    rows = pg_db(pg_db.webhook_endpoints.id > 0).select()

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}


# =============================================================================
# node_events -- regression: gh-22. Two distinct, previously-broken things:
#
# 1. This table carries its own bespoke RLS policy
#    (``node_events_tenant_isolation``, distinct from the generic
#    ``tenant_isolation`` policy every other table above uses) that used to
#    recognize the cross-tenant override sentinel as the literal string
#    '__super__' instead of ``app.db.rls.CROSS_TENANT_SENTINEL`` ('__all__').
#    Nothing in this codebase ever set '__super__' on the GUC, so a
#    cross-tenant/super-admin caller got RLS bypass on every other table but
#    NOT on node_events -- silently.
# 2. Unlike every other table this file exercises, the baseline migration's
#    ``m1_tables`` GRANT loop omitted ``node_events`` entirely --
#    "api-manager-rw" (the role app.api.nodes' POST /nodes/{id}/events
#    handler actually runs as in production) had no SELECT/INSERT grant on
#    it at all, so ``pg_db_scoped`` got "permission denied" before RLS was
#    even evaluated -- the sentinel fix above would have been moot in prod
#    without this, since the role couldn't reach the table regardless of
#    policy correctness.
#
# Both are now fixed in the baseline migration. These tests prove: the
# ordinary per-tenant isolation case (worked before, must keep working), the
# cross-tenant-bypass case (was broken, now fixed), and that the granted
# operations actually succeed as the real scoped role under a tenant GUC
# (the test that would have caught the missing-grant gap in the first
# place).
# =============================================================================


def _seed_node(owner_db: DB, *, name: str, tenant_id: str) -> int:
    """Insert a minimal ``nodes`` row as the table OWNER (bypasses RLS on
    insert). ``node_events.node_id`` is a NOT NULL FK to it. created_at/
    updated_at have no server-side DEFAULT (ORM-side Python default only,
    invisible to penguin-dal's reflected-table insert) -- supplied
    explicitly, same convention as every other direct-insert seed helper in
    this test suite."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return int(
        owner_db.nodes.insert(
            name=name,
            state="new",
            tenant_id=tenant_id,
            created_at=now,
            updated_at=now,
        )
    )


def _seed_node_event(db: DB, *, node_id: int, tenant_id: str, stage: str) -> int:
    """Insert a minimal ``node_events`` row via ``db`` -- either the table
    OWNER (``pg_db``, bypasses RLS) or the scoped role (``pg_db_scoped``,
    subject to RLS/grants -- see ``test_node_events_insert_and_select_
    succeed_as_scoped_role`` below). ``ts``/``created_at`` have no
    server-side DEFAULT either (same ORM-side-only gap as ``nodes``) --
    supplied explicitly."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return int(
        db.node_events.insert(
            node_id=node_id,
            tenant_id=tenant_id,
            ts=now,
            stage=stage,
            message=f"{stage} for {tenant_id}",
            created_at=now,
        )
    )


@pytest.fixture
def two_tenant_node_events(pg_db: DB) -> tuple[int, int]:
    node_a = _seed_node(pg_db, name="node-a", tenant_id=TENANT_A)
    node_b = _seed_node(pg_db, name="node-b", tenant_id=TENANT_B)
    id_a = _seed_node_event(pg_db, node_id=node_a, tenant_id=TENANT_A, stage="boot")
    id_b = _seed_node_event(pg_db, node_id=node_b, tenant_id=TENANT_B, stage="boot")
    return id_a, id_b


def test_node_events_tenant_a_sees_only_own_row(
    pg_db_scoped: DB, two_tenant_node_events: tuple[int, int]
) -> None:
    """Ordinary per-tenant isolation on node_events' bespoke policy -- this
    half already worked before the fix; must keep working."""
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        rows = pg_db_scoped(pg_db_scoped.node_events.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A}
    assert len(rows) == 1


def test_node_events_unset_tenant_is_fail_closed(
    pg_db_scoped: DB, two_tenant_node_events: tuple[int, int]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(None)

    rows = pg_db_scoped(pg_db_scoped.node_events.id > 0).select()

    assert len(rows) == 0


def test_node_events_cross_tenant_sentinel_sees_all_rows(
    pg_db_scoped: DB, two_tenant_node_events: tuple[int, int]
) -> None:
    """Regression: gh-22. Pre-fix, the bespoke ``node_events_tenant_isolation``
    policy only recognized '__super__' (which nothing ever sets) for the
    cross-tenant override -- ``CROSS_TENANT_SENTINEL`` ('__all__') got NO
    bypass here even though it bypasses every other RLS-protected table.
    Post-fix, the policy matches '__all__' like the generic
    ``tenant_isolation`` policy every other table uses, so both tenants'
    rows become visible under the sentinel exactly as they do on
    ``biomes``/``webhook_endpoints`` above.
    """
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        rows = pg_db_scoped(pg_db_scoped.node_events.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}
    assert len(rows) == 2


def test_node_events_owner_connection_is_rls_exempt(
    pg_db: DB, two_tenant_node_events: tuple[int, int]
) -> None:
    rows = pg_db(pg_db.node_events.id > 0).select()

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}


def test_node_events_insert_and_select_succeed_as_scoped_role(
    pg_db: DB, pg_db_scoped: DB
) -> None:
    """Regression: gh-22. The baseline migration's ``m1_tables`` GRANT loop
    omitted ``node_events`` entirely -- "api-manager-rw" (the role
    ``app.api.nodes``' ``POST /nodes/{id}/events`` handler actually runs as
    in production) had no SELECT/INSERT grant on it at all, so every write
    through that handler would have failed with "permission denied for
    table node_events" regardless of the RLS policy fix above.

    This is the test that would have caught that gap: it issues an INSERT
    (mirroring ``db.node_events.insert(**row_data)`` in
    ``app.api.nodes.post_node_event``) and a follow-up SELECT, both as the
    real scoped role (``pg_db_scoped``, never the table-owner ``pg_db``)
    under an ordinary per-tenant GUC -- not just proving the RLS policy is
    correct, but that the role can reach the table at all. SELECT is
    required in addition to INSERT here for the same reason it's required
    in production: penguin-dal's ``insert()`` uses SQLAlchemy's
    ``INSERT ... RETURNING``, and Postgres requires SELECT privilege on any
    column named in a RETURNING clause.
    """
    node_id = _seed_node(pg_db, name="node-scoped", tenant_id=TENANT_A)

    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        new_id = _seed_node_event(
            pg_db_scoped, node_id=node_id, tenant_id=TENANT_A, stage="boot"
        )
        rows = pg_db_scoped(pg_db_scoped.node_events.id == new_id).select()
    finally:
        set_current_tenant(None)

    assert len(rows) == 1
    assert rows[0].stage == "boot"
    assert rows[0].tenant_id == TENANT_A


# =============================================================================
# gh-22 FIX 5 -- class-level regression, not just node_events. FIX 4's
# node_events-specific sequence grant fixed one instance of a pattern that
# turned out to affect every SERIAL-PK table "api-manager-rw" has INSERT on
# (confirmed identically on ``nodes`` while implementing FIX 5): the
# baseline migration now derives and grants USAGE+SELECT on each such
# table's PK sequence dynamically (``_grant_insert_table_sequences`` in the
# migration, via ``pg_get_serial_sequence``) rather than a one-off hand
# grant. This is the test that would have caught the pattern across the
# whole set instead of one table at a time: INSERT (+ a follow-up read-back)
# as the real scoped role under an ordinary tenant GUC, on a representative
# mix of SERIAL-PK tables (nodes, webhook_endpoints, node_events) AND
# UUID-PK tables with no sequence at all (audit_events, joiner_secrets) --
# the latter two prove the fix doesn't depend on a sequence existing, i.e.
# the "no sequence" case is handled gracefully both by the migration's
# derivation and by this test covering it explicitly.
# =============================================================================


def test_api_manager_rw_can_insert_into_every_insert_granted_table_kind(
    pg_db: DB, pg_db_scoped: DB
) -> None:
    """Regression: gh-22 FIX 5. Exercises INSERT-as-scoped-role across both
    SERIAL-PK tables (needed the sequence grant) and UUID-PK tables (never
    needed one) to prove the class of bug is closed, not just node_events.
    """
    import uuid
    from datetime import datetime, timedelta, timezone

    from app.security.audit_chain import AuditEventWriter

    now = datetime.now(timezone.utc)

    # FK/seed targets, inserted as the table owner (pg_db) -- these aren't
    # what's under test here (node/biome INSERT-as-scoped-role is already
    # covered above / by test_tenant_a_sees_only_own_row).
    node_id = _seed_node(pg_db, name="probe-node", tenant_id=TENANT_A)
    biome_id = int(pg_db.biomes.insert(name="probe-biome", tenant_id=TENANT_A))

    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        # --- SERIAL-PK tables: needed the FIX 5 sequence grant ---
        new_node_id = pg_db_scoped.nodes.insert(
            name="scoped-node",
            state="new",
            tenant_id=TENANT_A,
            created_at=now,
            updated_at=now,
        )
        assert len(pg_db_scoped(pg_db_scoped.nodes.id == new_node_id).select()) == 1

        new_webhook_id = pg_db_scoped.webhook_endpoints.insert(
            tenant_id=TENANT_A,
            url="https://scoped.example.com/hook",
            signing_mode="ed25519",
            active=True,
            event_filter=[],
            retry_policy={"mode": "standard"},
            created_at=now,
            updated_at=now,
        )
        assert (
            len(
                pg_db_scoped(
                    pg_db_scoped.webhook_endpoints.id == new_webhook_id
                ).select()
            )
            == 1
        )

        new_event_id = _seed_node_event(
            pg_db_scoped, node_id=node_id, tenant_id=TENANT_A, stage="probe"
        )
        assert (
            len(pg_db_scoped(pg_db_scoped.node_events.id == new_event_id).select())
            == 1
        )

        # --- UUID-PK tables: no sequence involved, must keep working ---
        writer = AuditEventWriter(pg_db_scoped, "probe-cluster")
        audit_event = writer.append(
            actor_sub="probe@example.com",
            action="probe.insert",
            resource_kind="probe",
            tenant_id=TENANT_A,
        )
        assert audit_event.id is not None

        js_id = str(uuid.uuid4())
        pg_db_scoped.joiner_secrets.insert(
            id=js_id,
            cluster_id=str(uuid.uuid4()),
            tenant_id=TENANT_A,
            biome_kind="vault",
            emitter_biome_id=biome_id,
            extractor_name="kubeadm_join_token",
            scope="cluster",
            ciphertext=b"secret-bytes",
            iv=b"IIIIIIIIIIII",
            auth_tag=b"TTTTTTTTTTTTTTTT",
            dek_wrapped=b"vault:v1:DEK",
            vault_kek_name="probe-kek",
            ttl_seconds=3600,
            expires_at=now + timedelta(hours=1),
            rotation_class="vault-unseal",
            created_at=now,
        )
        assert (
            len(pg_db_scoped(pg_db_scoped.joiner_secrets.id == js_id).select()) == 1
        )
    finally:
        set_current_tenant(None)


# =============================================================================
# gh-21 -- four orphan tables gained RLS as part of the same task that added
# their schemas: clusters (SECURITY-CRITICAL -- see
# tests/api/test_clusters_pg.py for the app-level IDOR regression test this
# table's existence closes; the tests below are the DB-level RLS proof, same
# division of labor as biomes/webhook_endpoints above vs. their own endpoint
# tests), storage_quotas + storage_quota_requests (RLS is the PRIMARY
# tenant-isolation enforcement for these two -- app.api.storage trusts a
# caller-supplied/request-body tenant_id with no cross-check of its own),
# and biome_groups (tenant_id is a new, currently-always-'__default__'
# column -- seeded here with real per-test tenant ids via the table OWNER,
# which bypasses INSERT-time RLS, so the SELECT-side isolation this section
# actually tests is unaffected by that default).
# =============================================================================


def _seed_cluster(owner_db: DB, *, cluster_id: str, name: str, tenant_id: str) -> str:
    """Insert a minimal ``clusters`` row as the table OWNER (bypasses RLS on insert)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    owner_db.clusters.insert(
        id=cluster_id, tenant_id=tenant_id, name=name, created_at=now, updated_at=now,
    )
    return cluster_id


@pytest.fixture
def two_tenant_clusters(pg_db: DB) -> tuple[str, str]:
    id_a = _seed_cluster(pg_db, cluster_id="cluster-a", name="cluster-a", tenant_id=TENANT_A)
    id_b = _seed_cluster(pg_db, cluster_id="cluster-b", name="cluster-b", tenant_id=TENANT_B)
    return id_a, id_b


def test_clusters_tenant_a_sees_only_own_row(
    pg_db_scoped: DB, two_tenant_clusters: tuple[str, str]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        rows = pg_db_scoped(pg_db_scoped.clusters.id != None).select()  # noqa: E711
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A}
    assert len(rows) == 1


def test_clusters_unset_tenant_is_fail_closed(
    pg_db_scoped: DB, two_tenant_clusters: tuple[str, str]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(None)

    rows = pg_db_scoped(pg_db_scoped.clusters.id != None).select()  # noqa: E711

    assert len(rows) == 0


def test_clusters_cross_tenant_sentinel_sees_all_rows(
    pg_db_scoped: DB, two_tenant_clusters: tuple[str, str]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        rows = pg_db_scoped(pg_db_scoped.clusters.id != None).select()  # noqa: E711
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}
    assert len(rows) == 2


def test_clusters_owner_connection_is_rls_exempt(
    pg_db: DB, two_tenant_clusters: tuple[str, str]
) -> None:
    rows = pg_db(pg_db.clusters.id != None).select()  # noqa: E711

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}


def _seed_storage_quota(owner_db: DB, *, tenant_id: str, resource_type: str = "storage") -> str:
    import uuid as _uuid

    quota_id = str(_uuid.uuid4())
    owner_db.storage_quotas.insert(
        id=quota_id, tenant_id=tenant_id, resource_type=resource_type, unit="GB",
    )
    return quota_id


@pytest.fixture
def two_tenant_storage_quotas(pg_db: DB) -> tuple[str, str]:
    id_a = _seed_storage_quota(pg_db, tenant_id=TENANT_A)
    id_b = _seed_storage_quota(pg_db, tenant_id=TENANT_B)
    return id_a, id_b


def test_storage_quotas_tenant_a_sees_only_own_row(
    pg_db_scoped: DB, two_tenant_storage_quotas: tuple[str, str]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        rows = pg_db_scoped(pg_db_scoped.storage_quotas.id != None).select()  # noqa: E711
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A}
    assert len(rows) == 1


def test_storage_quotas_unset_tenant_is_fail_closed(
    pg_db_scoped: DB, two_tenant_storage_quotas: tuple[str, str]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(None)

    rows = pg_db_scoped(pg_db_scoped.storage_quotas.id != None).select()  # noqa: E711

    assert len(rows) == 0


def test_storage_quotas_cross_tenant_sentinel_sees_all_rows(
    pg_db_scoped: DB, two_tenant_storage_quotas: tuple[str, str]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        rows = pg_db_scoped(pg_db_scoped.storage_quotas.id != None).select()  # noqa: E711
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}
    assert len(rows) == 2


def test_storage_quotas_owner_connection_is_rls_exempt(
    pg_db: DB, two_tenant_storage_quotas: tuple[str, str]
) -> None:
    rows = pg_db(pg_db.storage_quotas.id != None).select()  # noqa: E711

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}


def _seed_storage_quota_request(owner_db: DB, *, tenant_id: str) -> str:
    import uuid as _uuid

    req_id = str(_uuid.uuid4())
    owner_db.storage_quota_requests.insert(
        id=req_id, tenant_id=tenant_id, resource_type="storage",
        requested_value=100, unit="GB", justification="need more",
    )
    return req_id


@pytest.fixture
def two_tenant_storage_quota_requests(pg_db: DB) -> tuple[str, str]:
    id_a = _seed_storage_quota_request(pg_db, tenant_id=TENANT_A)
    id_b = _seed_storage_quota_request(pg_db, tenant_id=TENANT_B)
    return id_a, id_b


def test_storage_quota_requests_tenant_a_sees_only_own_row(
    pg_db_scoped: DB, two_tenant_storage_quota_requests: tuple[str, str]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        rows = pg_db_scoped(pg_db_scoped.storage_quota_requests.id != None).select()  # noqa: E711
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A}
    assert len(rows) == 1


def test_storage_quota_requests_unset_tenant_is_fail_closed(
    pg_db_scoped: DB, two_tenant_storage_quota_requests: tuple[str, str]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(None)

    rows = pg_db_scoped(pg_db_scoped.storage_quota_requests.id != None).select()  # noqa: E711

    assert len(rows) == 0


def test_storage_quota_requests_cross_tenant_sentinel_sees_all_rows(
    pg_db_scoped: DB, two_tenant_storage_quota_requests: tuple[str, str]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        rows = pg_db_scoped(pg_db_scoped.storage_quota_requests.id != None).select()  # noqa: E711
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}
    assert len(rows) == 2


def test_storage_quota_requests_owner_connection_is_rls_exempt(
    pg_db: DB, two_tenant_storage_quota_requests: tuple[str, str]
) -> None:
    rows = pg_db(pg_db.storage_quota_requests.id != None).select()  # noqa: E711

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}


def test_storage_quota_requests_with_check_rejects_mismatched_tenant_insert(
    pg_db_scoped: DB,
) -> None:
    """SECURITY (gh-21): the profile's stated enforcement mechanism --
    ``app.api.storage.request_storage_quota`` takes ``tenant_id`` straight
    from the request body with no cross-check against the caller's own
    token. This proves the RLS WITH CHECK side (not just USING/SELECT-side
    filtering exercised above) actually rejects an INSERT whose tenant_id
    doesn't match the connection's GUC -- the generic ``tenant_isolation``
    policy has no separate WITH CHECK clause, so Postgres reuses its USING
    expression for both, and a mismatched insert must fail rather than
    silently write a row for an arbitrary tenant.
    """
    import uuid as _uuid

    import pytest as _pytest
    from sqlalchemy.exc import ProgrammingError

    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        with _pytest.raises(ProgrammingError, match="row-level security"):
            pg_db_scoped.storage_quota_requests.insert(
                id=str(_uuid.uuid4()), tenant_id=TENANT_B, resource_type="storage",
                requested_value=50, unit="GB", justification="cross-tenant attempt",
            )
    finally:
        set_current_tenant(None)


def _seed_biome_group(owner_db: DB, *, name: str, tenant_id: str) -> int:
    return int(
        owner_db.biome_groups.insert(
            tenant_id=tenant_id, name=name, display_name=name, biomes=[],
        )
    )


@pytest.fixture
def two_tenant_biome_groups(pg_db: DB) -> tuple[int, int]:
    id_a = _seed_biome_group(pg_db, name="group-a", tenant_id=TENANT_A)
    id_b = _seed_biome_group(pg_db, name="group-b", tenant_id=TENANT_B)
    return id_a, id_b


def test_biome_groups_tenant_a_sees_only_own_row(
    pg_db_scoped: DB, two_tenant_biome_groups: tuple[int, int]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        rows = pg_db_scoped(pg_db_scoped.biome_groups.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A}
    assert len(rows) == 1


def test_biome_groups_unset_tenant_is_fail_closed(
    pg_db_scoped: DB, two_tenant_biome_groups: tuple[int, int]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(None)

    rows = pg_db_scoped(pg_db_scoped.biome_groups.id > 0).select()

    assert len(rows) == 0


def test_biome_groups_cross_tenant_sentinel_sees_all_rows(
    pg_db_scoped: DB, two_tenant_biome_groups: tuple[int, int]
) -> None:
    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        rows = pg_db_scoped(pg_db_scoped.biome_groups.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}
    assert len(rows) == 2


def test_biome_groups_owner_connection_is_rls_exempt(
    pg_db: DB, two_tenant_biome_groups: tuple[int, int]
) -> None:
    rows = pg_db(pg_db.biome_groups.id > 0).select()

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}


# =============================================================================
# gh-21 -- extends the gh-22 FIX 5 sequence-sweep regression above to two
# newcomers from this task's ``api_manager_insert_tables`` addition:
# ``ipxe_images`` (SERIAL-PK, needed the sequence grant) and
# ``storage_quotas`` (UUID-PK, never needed one) -- same "one SERIAL + one
# non-SERIAL" pairing the original FIX 5 test used, proving the sweep picked
# up this task's tables the same way it already covered node_events/nodes.
# =============================================================================


def test_api_manager_rw_can_insert_into_gh21_orphan_serial_and_uuid_pk_tables(
    pg_db_scoped: DB,
) -> None:
    import uuid as _uuid

    install_rls_events(pg_db_scoped.engine)
    set_current_tenant(TENANT_A)
    try:
        image_id = pg_db_scoped.ipxe_images.insert(
            name="ubuntu-24.04-seq-probe", display_name="Ubuntu 24.04",
            os_version="24.04", architecture="amd64",
            kernel_path="minio://kernel", initrd_path="minio://initrd",
        )
        assert len(pg_db_scoped(pg_db_scoped.ipxe_images.id == image_id).select()) == 1

        quota_id = str(_uuid.uuid4())
        pg_db_scoped.storage_quotas.insert(
            id=quota_id, tenant_id=TENANT_A, resource_type="storage", unit="GB",
        )
        assert len(pg_db_scoped(pg_db_scoped.storage_quotas.id == quota_id).select()) == 1
    finally:
        set_current_tenant(None)
