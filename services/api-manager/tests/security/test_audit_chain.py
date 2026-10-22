"""Tests for audit_chain: UUIv7, JCS canonicalization, hash-chain verification.

The DB-touching classes below (``TestAuditEventWriter``, ``TestVerifyChain``,
``TestInsertGenesisRow``) run against real Postgres via the ``pg_db`` /
``pg_db_scoped`` fixtures (``tests/pg_fixtures.py``) rather than mocking
``.execute()`` — penguin-dal's raw-SQL ``executesql()``/``Tx.executesql()``
surface doesn't lend itself to the old SQLAlchemy-Session-shaped mocks, and
real Postgres is the established convention for this migration (see
``testing-python.md``: integration tests hit a real database, not mocks).
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from penguin_dal import DB

from app.db.rls import (
    CROSS_TENANT_SENTINEL,
    get_current_tenant,
    install_rls_events,
    set_current_tenant,
)
from app.security.audit_chain import (
    GENESIS_ID,
    ZERO_HASH,
    AuditEvent,
    AuditEventWriter,
    canonicalize_record,
    compute_chain_hash,
    generate_uuidv7,
    insert_genesis_row,
    verify_chain,
)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    """Narrow an ``executesql()`` result to ``list[tuple]`` for test assertions."""
    return cast("list[tuple[Any, ...]]", result)


class TestGenerateUUIDv7:
    """UUIDv7 generation and ordering."""

    def test_generate_uuidv7_is_time_ordered(self) -> None:
        """Generate 100 UUIDs; sorting by bytes preserves generation order."""
        uuids = [generate_uuidv7() for _ in range(100)]
        sorted_uuids = sorted(uuids, key=lambda u: u.bytes)
        assert sorted_uuids == uuids, "UUIDs must be time-ordered"

    def test_generate_uuidv7_version_7(self) -> None:
        """Generated UUIDs have version 7 marker in time_hi_version field."""
        u = generate_uuidv7()
        assert (u.time_hi_version >> 12) == 7, "Version must be 7"

    def test_generate_uuidv7_variant_rfc(self) -> None:
        """Generated UUIDs have RFC 4122 variant (10xxxxxx)."""
        u = generate_uuidv7()
        variant_bits = (u.clock_seq_hi_variant >> 6) & 0b11
        assert variant_bits == 0b10, "Variant must be RFC 4122 (10)"


class TestCanonicalizeRecord:
    """RFC 8785 JCS canonicalization."""

    def test_canonicalize_record_sorted_keys(self) -> None:
        """Out-of-order input keys are sorted lexicographically."""
        record = {"z": 1, "a": 2, "m": 3}
        canonical = canonicalize_record(record)
        expected = b'{"a":2,"m":3,"z":1}'
        assert canonical == expected

    def test_canonicalize_record_no_whitespace(self) -> None:
        """Canonical form is compact: no spaces, separators minimal."""
        record = {"key": "value", "num": 42}
        canonical = canonicalize_record(record)
        assert b" " not in canonical, "Must be compact (no spaces)"
        assert canonical == b'{"key":"value","num":42}'

    def test_canonicalize_record_nested_objects(self) -> None:
        """Nested objects are also sorted."""
        record = {"outer": {"z": 1, "a": 2}}
        canonical = canonicalize_record(record)
        assert canonical == b'{"outer":{"a":2,"z":1}}'

    def test_canonicalize_record_arrays(self) -> None:
        """Arrays are preserved as-is (order matters)."""
        record = {"items": [3, 1, 2]}
        canonical = canonicalize_record(record)
        assert canonical == b'{"items":[3,1,2]}'

    def test_canonicalize_record_rejects_nan(self) -> None:
        """NaN raises ValueError or JSONEncodeError."""
        record = {"value": float("nan")}
        with pytest.raises((ValueError, json.JSONDecodeError)):
            canonicalize_record(record)

    def test_canonicalize_record_rejects_infinity(self) -> None:
        """Infinity raises ValueError or JSONEncodeError."""
        record = {"value": float("inf")}
        with pytest.raises((ValueError, json.JSONDecodeError)):
            canonicalize_record(record)

    def test_canonicalize_record_null_true_false(self) -> None:
        """null, true, false lowercased."""
        record = {"a": None, "b": True, "c": False}
        canonical = canonicalize_record(record)
        assert b"null" in canonical
        assert b"true" in canonical
        assert b"false" in canonical


class TestComputeChainHash:
    """SHA-256 chain hash computation."""

    def test_compute_chain_hash_deterministic(self) -> None:
        """Same input always produces same output."""
        prev = ZERO_HASH
        record = {"action": "test", "id": "123"}
        h1 = compute_chain_hash(prev, record)
        h2 = compute_chain_hash(prev, record)
        assert h1 == h2
        assert len(h1) == 32

    def test_compute_chain_hash_validates_prev_length(self) -> None:
        """prev_hash must be exactly 32 bytes."""
        short_prev = b"\x00" * 31
        record = {"action": "test"}
        with pytest.raises(ValueError, match="32 bytes"):
            compute_chain_hash(short_prev, record)

    def test_compute_chain_hash_chains(self) -> None:
        """Small chain of 3 records: hashes link correctly."""
        h0 = ZERO_HASH
        rec1 = {"action": "create", "id": "1"}
        h1 = compute_chain_hash(h0, rec1)

        rec2 = {"action": "update", "id": "2"}
        h2 = compute_chain_hash(h1, rec2)

        rec3 = {"action": "delete", "id": "3"}
        h3 = compute_chain_hash(h2, rec3)

        assert h1 != h0
        assert h2 != h1
        assert h3 != h2
        assert len(h3) == 32

    def test_compute_chain_hash_different_records_different_hash(self) -> None:
        """Different records produce different hashes."""
        prev = ZERO_HASH
        h1 = compute_chain_hash(prev, {"value": "a"})
        h2 = compute_chain_hash(prev, {"value": "b"})
        assert h1 != h2

    def test_compute_chain_hash_different_prev_different_hash(self) -> None:
        """Different prev_hash produces different result."""
        record = {"action": "test"}
        h1 = compute_chain_hash(ZERO_HASH, record)
        h2 = compute_chain_hash(b"\xff" * 32, record)
        assert h1 != h2


class TestAuditEventWriter:
    """AuditEventWriter: append events with chain linking (real Postgres)."""

    def test_audit_event_writer_first_event_uses_zero_prev(self, pg_db: DB) -> None:
        """First event (empty audit_events) uses ZERO_HASH as prev."""
        writer = AuditEventWriter(pg_db, "test-cluster")
        event = writer.append(
            actor_sub="user@example.com",
            action="test_action",
            resource_kind="resource",
        )

        assert event.prev_hash == ZERO_HASH

    def test_audit_event_writer_subsequent_event_links_to_last(self, pg_db: DB) -> None:
        """Subsequent event's prev_hash matches last row's hash."""
        writer = AuditEventWriter(pg_db, "test-cluster")
        first = writer.append(
            actor_sub="user@example.com",
            action="create",
            resource_kind="resource",
        )
        second = writer.append(
            actor_sub="user@example.com",
            action="update",
            resource_kind="resource",
        )

        assert second.prev_hash == first.hash

    def test_audit_event_writer_with_signer(self, pg_db: DB) -> None:
        """With signer, signature is populated."""

        def mock_signer(data: bytes) -> bytes:
            return hashlib.sha256(b"sig_" + data).digest()

        writer = AuditEventWriter(pg_db, "test-cluster", signer=mock_signer)
        event = writer.append(
            actor_sub="user@example.com",
            action="test_action",
            resource_kind="resource",
        )

        assert event.signature is not None
        assert len(event.signature) == 32

    def test_audit_event_writer_without_signer(self, pg_db: DB) -> None:
        """Without signer, signature is None."""
        writer = AuditEventWriter(pg_db, "test-cluster", signer=None)
        event = writer.append(
            actor_sub="user@example.com",
            action="test_action",
            resource_kind="resource",
        )

        assert event.signature is None

    def test_audit_event_writer_append_returns_audit_event(self, pg_db: DB) -> None:
        """append() returns AuditEvent with all fields populated."""
        writer = AuditEventWriter(pg_db, "test-cluster")
        event = writer.append(
            actor_sub="user@example.com",
            action="create",
            resource_kind="user",
            resource_id="user-123",
            tenant_id="tenant-xyz",
            actor_scope=["read", "write"],
            before={"count": 0},
            after={"count": 1},
            request_id="req-abc",
            source_ip="192.168.1.1",
            user_agent="Mozilla/5.0",
        )

        assert isinstance(event, AuditEvent)
        assert event.actor_sub == "user@example.com"
        assert event.action == "create"
        assert event.resource_kind == "user"
        assert event.resource_id == "user-123"
        assert event.tenant_id == "tenant-xyz"
        assert event.actor_scope == ["read", "write"]
        assert event.before_json == {"count": 0}
        assert event.after_json == {"count": 1}
        assert event.request_id == "req-abc"
        assert event.source_ip == "192.168.1.1"
        assert event.user_agent == "Mozilla/5.0"

    def test_audit_event_writer_persists_row_visible_via_raw_select(
        self, pg_db: DB
    ) -> None:
        """The row genuinely lands in ``audit_events`` (not just the return value)."""
        writer = AuditEventWriter(pg_db, "test-cluster")
        event = writer.append(
            actor_sub="user@example.com",
            action="create",
            resource_kind="resource",
        )

        rows = _rows(
            pg_db.executesql(
                "SELECT actor_sub, action, hash FROM audit_events WHERE id = %(id)s",
                {"id": str(event.id)},
            )
        )
        assert len(rows) == 1
        actor_sub, action, hash_col = rows[0]
        assert (actor_sub, action) == ("user@example.com", "create")
        assert bytes(hash_col) == event.hash

    def test_audit_event_writer_append_with_external_tx_shares_connection(
        self, pg_db: DB
    ) -> None:
        """``tx=`` composes with a caller-owned transaction (the AuditChainWriter path)."""
        writer = AuditEventWriter(pg_db, "test-cluster")
        with pg_db.transaction() as tx:
            event = writer.append(
                tx=tx,
                actor_sub="user@example.com",
                action="create",
                resource_kind="resource",
            )
        rows = _rows(
            pg_db.executesql(
                "SELECT id FROM audit_events WHERE id = %(id)s", {"id": str(event.id)}
            )
        )
        assert len(rows) == 1

    def test_audit_event_writer_rollback_on_tx_exception(self, pg_db: DB) -> None:
        """An exception inside the caller's tx rolls back the insert (nothing persisted)."""
        writer = AuditEventWriter(pg_db, "test-cluster")
        with pytest.raises(RuntimeError):
            with pg_db.transaction() as tx:
                writer.append(
                    tx=tx,
                    actor_sub="user@example.com",
                    action="create",
                    resource_kind="resource",
                )
                raise RuntimeError("boom")

        rows = _rows(pg_db.executesql("SELECT COUNT(*) FROM audit_events"))
        assert rows[0][0] == 0


class TestVerifyChain:
    """verify_chain: integrity checking (real Postgres)."""

    def test_verify_chain_passes_for_clean(self, pg_db: DB) -> None:
        """Clean chain: verify_chain returns 0 breaks."""
        writer = AuditEventWriter(pg_db, "test-cluster")
        writer.append(
            actor_sub="user@example.com", action="create", resource_kind="resource"
        )
        writer.append(
            actor_sub="user@example.com", action="update", resource_kind="resource"
        )

        result = verify_chain(pg_db, since=None, to=None)

        assert result["rows_checked"] == 2
        assert result["breaks"] == 0
        assert result["first_break_id"] is None
        assert result["last_break_id"] is None

    def test_verify_chain_detects_tamper(self, pg_db: DB) -> None:
        """One row's hash mutated: verify_chain detects break."""
        writer = AuditEventWriter(pg_db, "test-cluster")
        writer.append(
            actor_sub="user@example.com", action="create", resource_kind="resource"
        )
        second = writer.append(
            actor_sub="user@example.com", action="update", resource_kind="resource"
        )

        pg_db.executesql(
            "UPDATE audit_events SET hash = %(hash)s WHERE id = %(id)s",
            {"hash": b"\xff" * 32, "id": str(second.id)},
        )

        result = verify_chain(pg_db, since=None, to=None)

        # audit_events.id is physically VARCHAR(36) (app.models_m1.UUID
        # TypeDecorator), so a raw executesql() row returns the id column as
        # a plain str, not a uuid.UUID object -- same driver-level behavior
        # the pre-conversion SQLAlchemy text() query had.
        assert result["breaks"] == 1
        assert result["first_break_id"] == str(second.id)
        assert result["last_break_id"] == str(second.id)

    def test_verify_chain_respects_since_to_window(self, pg_db: DB) -> None:
        """Rows outside [since, to] are excluded from rows_checked."""
        writer = AuditEventWriter(pg_db, "test-cluster")
        first = writer.append(
            actor_sub="user@example.com", action="create", resource_kind="resource"
        )
        pg_db.executesql(
            "UPDATE audit_events SET ts = %(ts)s WHERE id = %(id)s",
            {"ts": datetime.now(timezone.utc) - timedelta(days=2), "id": str(first.id)},
        )
        writer.append(
            actor_sub="user@example.com", action="update", resource_kind="resource"
        )

        result = verify_chain(
            pg_db, since=datetime.now(timezone.utc) - timedelta(hours=1), to=None
        )
        assert result["rows_checked"] == 1

    def test_verify_chain_empty_table(self, pg_db: DB) -> None:
        """No rows: 0 checked, 0 breaks (not an error)."""
        result = verify_chain(pg_db, since=None, to=None)
        assert result == {
            "rows_checked": 0,
            "breaks": 0,
            "first_break_id": None,
            "last_break_id": None,
        }


class TestInsertGenesisRow:
    """insert_genesis_row: idempotent cluster initialization (real Postgres).

    ``insert_genesis_row`` self-applies the RLS cross-tenant sentinel (see
    ``app.security.audit_chain._cross_tenant_scope`` docstring), so these
    tests run against ``pg_db_scoped`` (the non-owner role RLS actually
    enforces against) to prove that scope is genuinely required and correct
    -- ``pg_db`` (table owner) is RLS-exempt and wouldn't catch a regression
    here. Each test calls ``install_rls_events(pg_db_scoped.engine)`` first
    -- without it, ``set_current_tenant()``'s ContextVar has no effect on
    ``pg_db_scoped``'s connections at all (see ``tests/test_rls_isolation.py``
    module docstring), which would make every assertion here pass or fail
    for the wrong reason.
    """

    def test_insert_genesis_row_idempotent(self, pg_db_scoped: DB) -> None:
        """Call twice: only one genesis exists (idempotent)."""
        install_rls_events(pg_db_scoped.engine)
        genesis1 = insert_genesis_row(pg_db_scoped, "test-cluster")
        assert genesis1.id == GENESIS_ID

        genesis2 = insert_genesis_row(pg_db_scoped, "test-cluster")
        assert genesis2.id == GENESIS_ID
        assert genesis1.hash == genesis2.hash

        # Count filtered to the genesis id specifically (not the whole
        # table) -- pg_db_scoped intentionally never truncates, so other
        # tests in this class may leave their own (also-idempotent, same
        # singleton id) genesis rows around; this only asserts THIS id
        # never duplicates. Read under the cross-tenant sentinel directly --
        # insert_genesis_row already restored the ContextVar to its prior
        # (unset) value by the time it returns, and an unset tenant fails
        # closed to zero rows under RLS.
        set_current_tenant(CROSS_TENANT_SENTINEL)
        try:
            rows = _rows(
                pg_db_scoped.executesql(
                    "SELECT COUNT(*) FROM audit_events WHERE id = %(id)s",
                    {"id": str(GENESIS_ID)},
                )
            )
        finally:
            set_current_tenant(None)
        assert rows[0][0] == 1

    def test_genesis_row_id_constant(self, pg_db_scoped: DB) -> None:
        """insert_genesis_row produces id == GENESIS_ID."""
        install_rls_events(pg_db_scoped.engine)
        genesis = insert_genesis_row(pg_db_scoped, "test-cluster")
        assert genesis.id == GENESIS_ID

    def test_genesis_row_hash_deterministic(self, pg_db_scoped: DB) -> None:
        """Genesis hash is deterministic: sha256('gough-cluster-genesis:' + cluster_id)."""
        install_rls_events(pg_db_scoped.engine)
        cluster_id = "test-cluster"
        expected_hash = hashlib.sha256(
            b"gough-cluster-genesis:" + cluster_id.encode("utf-8")
        ).digest()

        genesis = insert_genesis_row(pg_db_scoped, cluster_id)
        assert genesis.hash == expected_hash

    def test_genesis_row_prev_hash_zero(self, pg_db_scoped: DB) -> None:
        """Genesis prev_hash is ZERO_HASH."""
        install_rls_events(pg_db_scoped.engine)
        genesis = insert_genesis_row(pg_db_scoped, "test-cluster")
        assert genesis.prev_hash == ZERO_HASH

    def test_genesis_row_requires_cross_tenant_visibility(
        self, pg_db_scoped: DB
    ) -> None:
        """Negative control: without the sentinel, a scoped-role caller under an
        ordinary per-tenant GUC can't even see an existing genesis row (NULL
        tenant_id never matches a real tenant in the RLS policy's IN-list) --
        proving ``_cross_tenant_scope()`` inside ``insert_genesis_row`` is
        load-bearing, not decorative.
        """
        install_rls_events(pg_db_scoped.engine)
        insert_genesis_row(pg_db_scoped, "test-cluster")

        previous = get_current_tenant()
        set_current_tenant("some-ordinary-tenant")
        try:
            rows = _rows(
                pg_db_scoped.executesql(
                    "SELECT id FROM audit_events WHERE id = %(id)s",
                    {"id": str(GENESIS_ID)},
                )
            )
        finally:
            set_current_tenant(previous)

        assert rows == [], "genesis row must be invisible under a plain per-tenant GUC"

        set_current_tenant(CROSS_TENANT_SENTINEL)
        try:
            rows = _rows(
                pg_db_scoped.executesql(
                    "SELECT id FROM audit_events WHERE id = %(id)s",
                    {"id": str(GENESIS_ID)},
                )
            )
        finally:
            set_current_tenant(previous)
        assert (
            len(rows) == 1
        ), "genesis row must be visible under the cross-tenant sentinel"


class TestAuditEventWriterCrossTenantForkPrevention:
    """Regression: gh-22 -- ``AuditEventWriter.append()`` must always link a
    new event to the TRUE global chain head, even when called while a
    caller's own single-tenant RLS GUC is active (not the cross-tenant
    sentinel) -- exactly the situation ``export_audit_log``'s self-audit
    write and ``revoke_joiner_secret``'s hit in production, since neither
    wraps its ``append()`` call in the sentinel itself.

    Uses ``pg_db_scoped`` (the actual ``api-manager-rw`` role RLS is
    enforced against), same as ``TestInsertGenesisRow`` above -- ``pg_db``
    (table owner) is RLS-exempt, so the chain-head SELECT would see every
    row regardless of GUC scope and the fork this test guards against could
    never manifest under that fixture.
    """

    def test_append_under_different_tenant_guc_links_to_true_global_head(
        self, pg_db_scoped: DB
    ) -> None:
        """Seed tenant A's event, then append as tenant B (tenant B's GUC
        active at call time) -- the new event must link to tenant A's event
        (the true global head), never fork to ``ZERO_HASH``.
        """
        install_rls_events(pg_db_scoped.engine)
        writer = AuditEventWriter(pg_db_scoped, "test-cluster")

        set_current_tenant("tenant-a")
        try:
            tenant_a_event = writer.append(
                actor_sub="a@example.com",
                action="create",
                resource_kind="resource",
                tenant_id="tenant-a",
            )
        finally:
            set_current_tenant(None)

        # Negative control -- proves this test is actually sensitive to the
        # fix. Under tenant B's own GUC (no cross-tenant scope), a plain
        # SELECT genuinely cannot see tenant A's row. Pre-fix, append()'s
        # internal chain-head SELECT hit exactly this same fail-closed wall
        # while running under the caller's own per-tenant GUC -- silently
        # computing prev_hash=ZERO_HASH instead of linking to tenant A's
        # event, forking the chain.
        set_current_tenant("tenant-b")
        try:
            invisible = _rows(
                pg_db_scoped.executesql(
                    "SELECT id FROM audit_events WHERE id = %(id)s",
                    {"id": str(tenant_a_event.id)},
                )
            )
        finally:
            set_current_tenant(None)
        assert invisible == [], (
            "test setup invalid: tenant A's row must be invisible under "
            "tenant B's own GUC for this to be a meaningful regression test"
        )

        # The actual regression check: append() runs *while* tenant B's GUC
        # is active -- mirroring export_audit_log/revoke_joiner_secret
        # calling append() from inside their own request-scoped tenant GUC.
        set_current_tenant("tenant-b")
        try:
            tenant_b_event = writer.append(
                actor_sub="b@example.com",
                action="create",
                resource_kind="resource",
                tenant_id="tenant-b",
            )
        finally:
            set_current_tenant(None)

        assert tenant_b_event.prev_hash == tenant_a_event.hash, (
            "chain forked: tenant B's append() did not link to tenant A's "
            "event as the true global chain head"
        )
        # The RLS *visibility* scope changing must never leak into the
        # row's own tenant_id *column* -- it must still be the caller's
        # real tenant.
        assert tenant_b_event.tenant_id == "tenant-b"

        # Confirm via a fresh cross-tenant read (not just the returned
        # object) that both rows genuinely landed this way, scoped to just
        # these two ids so leftover rows from other pg_db_scoped-based
        # tests in this session (which never truncates) can't skew the
        # assertion.
        set_current_tenant(CROSS_TENANT_SENTINEL)
        try:
            rows = _rows(
                pg_db_scoped.executesql(
                    "SELECT id, tenant_id, prev_hash, hash FROM audit_events "
                    "WHERE id IN (%(a)s, %(b)s) ORDER BY ts ASC, id ASC",
                    {"a": str(tenant_a_event.id), "b": str(tenant_b_event.id)},
                )
            )
        finally:
            set_current_tenant(None)
        assert len(rows) == 2
        assert rows[0][1] == "tenant-a"
        assert rows[1][1] == "tenant-b"
        assert bytes(rows[1][2]) == bytes(
            rows[0][3]
        ), "tenant B's prev_hash must equal tenant A's hash"

    def test_append_restores_previous_tenant_guc_after_returning(
        self, pg_db_scoped: DB
    ) -> None:
        """The self-applied cross-tenant scope must not leak past the call
        -- same try/finally restore contract as every other
        ``_cross_tenant_scope()`` use in this codebase.
        """
        install_rls_events(pg_db_scoped.engine)
        writer = AuditEventWriter(pg_db_scoped, "test-cluster")

        set_current_tenant("tenant-a")
        try:
            writer.append(
                actor_sub="a@example.com",
                action="create",
                resource_kind="resource",
                tenant_id="tenant-a",
            )
            assert get_current_tenant() == "tenant-a"
        finally:
            set_current_tenant(None)
