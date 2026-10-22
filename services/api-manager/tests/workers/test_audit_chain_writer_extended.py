"""Extended tests for audit_chain_writer.py uncovered lines.

Focuses on edge cases, error handling, and internal methods:
- _read_watermark / _persist_watermark (error paths + the graceful
  degradation ``audit_events_mirror_state`` needs today -- see class
  docstrings, that table has no schema anywhere yet, a pre-existing gap
  unrelated to this conversion)
- _fetch_rows_after (with/without watermark) -- real Postgres
- _row_to_object_payload (normalization) -- pure function, no DB
- _ship_row (S3 errors, retention) -- no DB (persist_watermark patched out)
- _update_lag_gauge (lag calculation, missing data) -- real Postgres
- _build_s3_client_factory (with/without factory override) -- no DB
- _publish_nats_break (no publisher, async coroutine handling) -- no DB
- append_event's SERIALIZABLE/advisory-lock try/except swallowing -- a
  lightweight fake ``db.transaction()``/``Tx.executesql`` stub, since real
  Postgres has no way to force those two specific statements to fail
  in-process (this is a control-flow/logging test, not a correctness test
  against a real chain -- that's ``test_audit_chain_writer.py``'s job).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from penguin_dal import DB

from app.workers.audit_chain_writer import (
    AuditChainWriter,
    OffsiteMirrorConfig,
    OffsiteMirrorError,
)

# ============================================================================
# _read_watermark / _persist_watermark
# ============================================================================


class TestReadWatermark:
    """``audit_events_mirror_state`` has no schema anywhere (pre-existing gap,
    same class as the ~12 orphaned tables tracked in GitHub issue #21) -- in
    today's real schema, every ``_read_watermark()`` call hits the "table
    missing" except branch. This is tested against real Postgres (the
    genuinely-current behavior); the hypothetical "table exists" path is
    covered separately with a lightweight stub so this suite still proves
    that code path works once the schema gap is closed.
    """

    def test_read_watermark_table_missing_real_pg(self, pg_db: DB) -> None:
        writer = AuditChainWriter(pg_db, None)
        assert writer._read_watermark() is None

    def test_read_watermark_success_with_stub_table(self) -> None:
        """Once ``audit_events_mirror_state`` exists, a present row round-trips."""
        test_uuid = uuid.uuid4()
        stub = _StubDB({"SELECT last_shipped_id": [(str(test_uuid),)]})
        writer = AuditChainWriter(stub, None)

        assert writer._read_watermark() == test_uuid

    def test_read_watermark_null_value_with_stub_table(self) -> None:
        stub = _StubDB({"SELECT last_shipped_id": [(None,)]})
        writer = AuditChainWriter(stub, None)

        assert writer._read_watermark() is None

    def test_read_watermark_no_row_with_stub_table(self) -> None:
        stub = _StubDB({"SELECT last_shipped_id": []})
        writer = AuditChainWriter(stub, None)

        assert writer._read_watermark() is None


class TestPersistWatermark:
    """Same schema-gap caveat as ``TestReadWatermark`` -- real Postgres
    exercises the "both UPSERT and UPDATE fail" fallback path (the table
    doesn't exist), and a stub proves the UPSERT-succeeds / UPSERT-fails
    -UPDATE-succeeds paths for once the table exists.
    """

    def test_persist_watermark_both_fail_real_pg(self, pg_db: DB) -> None:
        """Table missing -> both statements fail, no exception raised (logged only)."""
        writer = AuditChainWriter(pg_db, None)
        writer._persist_watermark(uuid.uuid4())  # must not raise

    def test_persist_watermark_upsert_success_with_stub_table(self) -> None:
        stub = _StubDB({}, writable=True)
        writer = AuditChainWriter(stub, None)

        writer._persist_watermark(uuid.uuid4())

        assert (
            stub.executed[0][0]
            .strip()
            .startswith("INSERT INTO audit_events_mirror_state")
        )

    def test_persist_watermark_upsert_fails_update_succeeds_with_stub_table(
        self,
    ) -> None:
        stub = _StubDB({}, writable=True, fail_first_write=True)
        writer = AuditChainWriter(stub, None)

        writer._persist_watermark(uuid.uuid4())

        assert len(stub.executed) == 2
        assert "UPDATE audit_events_mirror_state" in stub.executed[1][0]


# ============================================================================
# _fetch_rows_after -- real Postgres
# ============================================================================


class TestFetchRowsAfter:
    def test_fetch_rows_no_watermark(self, pg_db: DB) -> None:
        writer = AuditChainWriter(pg_db, None)
        _insert_raw_event(pg_db, actor_sub="user1@example.com")
        _insert_raw_event(pg_db, actor_sub="user2@example.com")

        result = writer._fetch_rows_after(None, 100)

        assert len(result) == 2
        assert {r["actor_sub"] for r in result} == {
            "user1@example.com",
            "user2@example.com",
        }

    def test_fetch_rows_with_watermark(self, pg_db: DB) -> None:
        writer = AuditChainWriter(pg_db, None)
        first_id = _insert_raw_event(pg_db, actor_sub="user1@example.com")
        second_id = _insert_raw_event(pg_db, actor_sub="user2@example.com")

        result = writer._fetch_rows_after(first_id, 100)

        assert len(result) == 1
        assert result[0]["id"] == str(second_id)

    def test_fetch_rows_respects_batch_size(self, pg_db: DB) -> None:
        writer = AuditChainWriter(pg_db, None)
        for i in range(5):
            _insert_raw_event(pg_db, actor_sub=f"user{i}@example.com")

        result = writer._fetch_rows_after(None, 2)
        assert len(result) == 2


# ============================================================================
# _row_to_object_payload -- pure function, no DB.
# ============================================================================


class TestRowToObjectPayload:
    def test_normalize_bytes(self) -> None:
        writer = AuditChainWriter(None, None)

        row = {
            "id": uuid.uuid4(),
            "binary_data": b"test",
            "signature": bytes([0, 255, 128]),
        }

        payload = writer._row_to_object_payload(row)
        assert isinstance(payload, bytes)
        assert b"74657374" in payload  # "test" in hex

    def test_normalize_datetime(self) -> None:
        writer = AuditChainWriter(None, None)

        dt = datetime(2025, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
        row = {"ts": dt, "id": uuid.uuid4()}

        payload = writer._row_to_object_payload(row)
        assert isinstance(payload, bytes)
        assert b"2025-04-30T12:00:00" in payload

    def test_normalize_uuid(self) -> None:
        writer = AuditChainWriter(None, None)

        test_uuid = uuid.uuid4()
        row = {"id": test_uuid}

        payload = writer._row_to_object_payload(row)
        assert isinstance(payload, bytes)
        assert str(test_uuid).encode() in payload

    def test_normalize_memoryview(self) -> None:
        writer = AuditChainWriter(None, None)

        mv = memoryview(b"test")
        row = {"data": mv, "id": uuid.uuid4()}

        payload = writer._row_to_object_payload(row)
        assert isinstance(payload, bytes)


# ============================================================================
# _ship_row -- no DB (persist_watermark patched out).
# ============================================================================


class TestShipRow:
    @pytest.mark.asyncio
    async def test_ship_row_with_kms(self) -> None:
        s3_client = AsyncMock()
        cfg = OffsiteMirrorConfig(
            s3_endpoint="https://s3.example.com",
            bucket="audit-bucket",
            retention_days=365,
            sse_kms_key_id="arn:aws:kms:us-east-1:123456789:key/abc123",
        )

        with patch.object(AuditChainWriter, "_persist_watermark", return_value=None):
            writer = AuditChainWriter(None, None)
            row = _sample_ship_row()
            await writer._ship_row(s3_client, cfg, row)

            s3_client.put_object.assert_called_once()
            call_kwargs = s3_client.put_object.call_args[1]
            assert call_kwargs["ServerSideEncryption"] == "aws:kms"
            assert call_kwargs["SSEKMSKeyId"] == cfg.sse_kms_key_id

    @pytest.mark.asyncio
    async def test_ship_row_without_kms(self) -> None:
        s3_client = AsyncMock()
        cfg = OffsiteMirrorConfig(
            s3_endpoint="https://s3.example.com",
            bucket="audit-bucket",
            retention_days=365,
            sse_kms_key_id=None,
        )

        with patch.object(AuditChainWriter, "_persist_watermark", return_value=None):
            writer = AuditChainWriter(None, None)
            row = _sample_ship_row()
            await writer._ship_row(s3_client, cfg, row)

            call_kwargs = s3_client.put_object.call_args[1]
            assert call_kwargs["ServerSideEncryption"] == "AES256"
            assert "SSEKMSKeyId" not in call_kwargs

    @pytest.mark.asyncio
    async def test_ship_row_object_lock(self) -> None:
        s3_client = AsyncMock()
        cfg = OffsiteMirrorConfig(
            s3_endpoint="https://s3.example.com",
            bucket="audit-bucket",
            retention_days=7,
            object_lock_mode="COMPLIANCE",
        )

        with patch.object(AuditChainWriter, "_persist_watermark", return_value=None):
            writer = AuditChainWriter(None, None)
            row = _sample_ship_row()
            await writer._ship_row(s3_client, cfg, row)

            call_kwargs = s3_client.put_object.call_args[1]
            assert call_kwargs["ObjectLockMode"] == "COMPLIANCE"
            assert "ObjectLockRetainUntilDate" in call_kwargs


def _sample_ship_row() -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "ts": datetime.now(timezone.utc),
        "cluster_id": "cluster-1",
        "tenant_id": "tenant-1",
        "actor_sub": "user@example.com",
        "actor_scope": ["read:all"],
        "action": "READ",
        "resource_kind": "node",
        "resource_id": "node-1",
        "before_json": None,
        "after_json": None,
        "request_id": "req-1",
        "source_ip": "192.168.1.1",
        "user_agent": "curl/7.0",
        "prev_hash": "prev-hash",
        "hash": "hash-1",
        "signature": "sig-1",
    }


# ============================================================================
# _update_lag_gauge -- real Postgres.
# ============================================================================


class TestUpdateLagGauge:
    def test_update_lag_no_rows(self, pg_db: DB) -> None:
        writer = AuditChainWriter(pg_db, None)

        with patch("app.workers.audit_chain_writer.MIRROR_LAG_GAUGE") as mock_gauge:
            writer._update_lag_gauge(None)
            mock_gauge.set.assert_called_once_with(0.0)

    def test_update_lag_no_watermark(self, pg_db: DB) -> None:
        writer = AuditChainWriter(pg_db, None)
        _insert_raw_event(pg_db, ts=datetime.now(timezone.utc) - timedelta(minutes=5))

        with patch("app.workers.audit_chain_writer.MIRROR_LAG_GAUGE") as mock_gauge:
            writer._update_lag_gauge(None)

        call_args = mock_gauge.set.call_args[0][0]
        assert 290 < call_args < 310

    def test_update_lag_with_watermark(self, pg_db: DB) -> None:
        writer = AuditChainWriter(pg_db, None)
        now = datetime.now(timezone.utc)
        shipped_id = _insert_raw_event(pg_db, ts=now - timedelta(minutes=10))
        _insert_raw_event(pg_db, ts=now)

        with patch("app.workers.audit_chain_writer.MIRROR_LAG_GAUGE") as mock_gauge:
            writer._update_lag_gauge(shipped_id)

        call_args = mock_gauge.set.call_args[0][0]
        assert 590 < call_args < 610

    def test_update_lag_watermark_row_not_found(self, pg_db: DB) -> None:
        """Watermark id doesn't match any row -- falls back to now()-latest_ts."""
        writer = AuditChainWriter(pg_db, None)
        _insert_raw_event(pg_db, ts=datetime.now(timezone.utc) - timedelta(minutes=1))

        with patch("app.workers.audit_chain_writer.MIRROR_LAG_GAUGE") as mock_gauge:
            writer._update_lag_gauge(uuid.uuid4())

        assert mock_gauge.set.called
        assert mock_gauge.set.call_args[0][0] >= 0

    def test_update_lag_exception_logged(self) -> None:
        """A DB error (e.g. connection failure) is swallowed, logged, not raised."""
        stub = _RaisingDB()
        writer = AuditChainWriter(stub, None)

        with patch("app.workers.audit_chain_writer.MIRROR_LAG_GAUGE") as mock_gauge:
            writer._update_lag_gauge(None)  # must not raise
        mock_gauge.set.assert_not_called()

    def test_update_lag_timezone_naive_handling_with_stub(self) -> None:
        """Defensive tzinfo-backfill branch -- unreachable via real Postgres
        (``audit_events.ts`` is ``TIMESTAMPTZ``, psycopg2 always returns
        tz-aware values for it), so this is exercised with a stub that
        returns a naive ``datetime`` for both reads, matching what the code
        defends against.
        """
        naive_ts = datetime.now()
        stub = _StubDB(
            {
                "SELECT ts FROM audit_events ORDER BY": [(naive_ts,)],
                "SELECT ts FROM audit_events WHERE": [(naive_ts,)],
            }
        )
        writer = AuditChainWriter(stub, None)

        with patch("app.workers.audit_chain_writer.MIRROR_LAG_GAUGE") as mock_gauge:
            writer._update_lag_gauge(uuid.uuid4())

        assert mock_gauge.set.called


# ============================================================================
# _build_s3_client_factory -- no DB.
# ============================================================================


class TestBuildS3ClientFactory:
    def test_build_s3_client_factory_with_override(self) -> None:
        mock_factory = MagicMock()
        mock_client = MagicMock()
        mock_factory.return_value = mock_client

        writer = AuditChainWriter(None, None, s3_session_factory=mock_factory)
        cfg = OffsiteMirrorConfig(
            s3_endpoint="https://s3.example.com",
            bucket="audit-bucket",
            retention_days=365,
        )

        factory = writer._build_s3_client_factory(cfg)
        result = factory()

        assert result == mock_client
        mock_factory.assert_called_once_with(cfg)

    @patch("builtins.__import__")
    def test_build_s3_client_factory_no_aioboto3(self, mock_import: Any) -> None:
        writer = AuditChainWriter(None, None)
        cfg = OffsiteMirrorConfig(
            s3_endpoint="https://s3.example.com",
            bucket="audit-bucket",
            retention_days=365,
        )

        def side_effect(name: str, *args: Any, **kwargs: Any) -> Any:
            if "aioboto3" in name:
                raise ImportError("aioboto3 not found")
            return __import__(name, *args, **kwargs)

        mock_import.side_effect = side_effect

        with pytest.raises(OffsiteMirrorError):
            writer._build_s3_client_factory(cfg)


# ============================================================================
# _publish_nats_break -- no DB.
# ============================================================================


class TestPublishNatsBreak:
    def test_publish_nats_break_no_publisher(self) -> None:
        writer = AuditChainWriter(None, None, nats_publisher=None)

        payload = {"cluster_id": "cluster-1", "breaks": 1}
        result = writer._publish_nats_break(payload)

        assert result is False

    def test_publish_nats_break_sync_function(self) -> None:
        def sync_publisher(subject: str, payload: dict[str, Any]) -> None:
            pass

        writer = AuditChainWriter(
            None, None, nats_publisher=sync_publisher  # type: ignore[arg-type]
        )

        payload = {"cluster_id": "cluster-1", "breaks": 1}
        result = writer._publish_nats_break(payload)

        assert result is True

    @pytest.mark.asyncio
    async def test_publish_nats_break_async_with_running_loop(self) -> None:
        async def async_publisher(subject: str, payload: dict[str, Any]) -> None:
            pass

        writer = AuditChainWriter(None, None, nats_publisher=async_publisher)

        payload = {"cluster_id": "cluster-1", "breaks": 1}
        result = writer._publish_nats_break(payload)

        assert result is True

    def test_publish_nats_break_exception(self) -> None:
        def failing_publisher(subject: str, payload: dict[str, Any]) -> None:
            raise ValueError("publish failed")

        writer = AuditChainWriter(
            None, None, nats_publisher=failing_publisher  # type: ignore[arg-type]
        )

        payload = {"cluster_id": "cluster-1", "breaks": 1}
        result = writer._publish_nats_break(payload)

        assert result is False

    def test_publish_nats_break_async_without_running_loop(self) -> None:
        """No running event loop -- falls back to ``asyncio.run(coro)``
        (sync test function, so there's genuinely no loop; distinct from
        ``test_publish_nats_break_async_with_running_loop`` above).
        """

        async def async_publisher(subject: str, payload: dict[str, Any]) -> None:
            pass

        writer = AuditChainWriter(None, None, nats_publisher=async_publisher)

        payload = {"cluster_id": "cluster-1", "breaks": 1}
        result = writer._publish_nats_break(payload)

        assert result is True


# ============================================================================
# append_event's SET/advisory-lock try/except swallowing.
# ============================================================================


class _FakeTx:
    """Minimal penguin-dal ``Tx`` stand-in -- selectively raises per-SQL."""

    def __init__(self, fail_on: Callable[[str], bool], log: list[str]) -> None:
        self._fail_on = fail_on
        self._log = log

    def executesql(self, query: str, placeholders: Any = None, **kwargs: Any) -> Any:
        self._log.append(query)
        if self._fail_on(query):
            raise RuntimeError(f"simulated failure for: {query.strip()[:40]}")
        return []


class _FakeDBForAppend:
    """Minimal penguin-dal ``DB`` stand-in exposing just ``transaction()``."""

    def __init__(self, fail_on: Callable[[str], bool]) -> None:
        self._fail_on = fail_on
        self.log: list[str] = []

    @contextmanager
    def transaction(self) -> Iterator[_FakeTx]:
        yield _FakeTx(self._fail_on, self.log)


class TestAppendEventExceptionLogging:
    """SERIALIZABLE / advisory-lock failures are caught and logged, never
    propagated -- real Postgres always succeeds at both statements (no way
    to force a failure in-process), so this uses a lightweight fake
    ``db.transaction()``/``Tx`` that selectively raises on one statement,
    proving the try/except around each is scoped correctly and the append
    still completes.
    """

    def test_isolation_level_exception_logged(self) -> None:
        fake_db = _FakeDBForAppend(fail_on=lambda q: "SERIALIZABLE" in q)
        writer = AuditChainWriter(fake_db, None)
        writer._lease_handle = MagicMock()
        writer._writer = MagicMock()
        writer._writer.append.return_value = MagicMock(id=uuid.uuid4())

        with patch("app.workers.audit_chain_writer.logger") as mock_logger:
            writer.append_event(
                actor_sub="user123", action="create", resource_kind="node"
            )
            assert mock_logger.debug.called

        # The advisory lock statement still ran despite the SERIALIZABLE failure.
        assert any("pg_advisory_xact_lock" in q for q in fake_db.log)

    def test_advisory_lock_exception_logged(self) -> None:
        fake_db = _FakeDBForAppend(fail_on=lambda q: "pg_advisory_xact_lock" in q)
        writer = AuditChainWriter(fake_db, None)
        writer._lease_handle = MagicMock()
        writer._writer = MagicMock()
        writer._writer.append.return_value = MagicMock(id=uuid.uuid4())

        with patch("app.workers.audit_chain_writer.logger") as mock_logger:
            writer.append_event(
                actor_sub="user123", action="create", resource_kind="node"
            )
            calls = [str(c) for c in mock_logger.debug.call_args_list]
            assert any("pg_advisory_xact_lock" in c for c in calls)

        # writer.append still ran (append_event doesn't abort on a lock failure).
        writer._writer.append.assert_called_once()

    def test_append_event_not_leader_raises_error(self) -> None:
        mock_lease = MagicMock()
        mock_lease.is_leader.return_value = False

        writer = AuditChainWriter(
            db_session=MagicMock(),
            vault_client=None,
            leader_lease_client=mock_lease,
        )
        assert writer.is_leader() is False

        with pytest.raises(Exception):
            writer.append_event(
                actor_sub="user123", action="create", resource_kind="node"
            )


# ============================================================================
# Fixtures / stubs shared by this file.
# ============================================================================


class _StubDB:
    """Minimal ``executesql``-only stand-in for the watermark tests above.

    ``responses`` maps a query-prefix to canned rows for reads; writes are
    recorded in ``self.executed`` (and, if ``fail_first_write``, the first
    write raises to exercise the UPSERT-fails/UPDATE-succeeds fallback).
    """

    def __init__(
        self,
        responses: dict[str, list[tuple[Any, ...]]],
        *,
        writable: bool = False,
        fail_first_write: bool = False,
    ) -> None:
        self._responses = responses
        self._writable = writable
        self._fail_first_write = fail_first_write
        self.executed: list[tuple[str, Any]] = []

    def executesql(self, query: str, placeholders: Any = None, **kwargs: Any) -> Any:
        stripped = query.strip()
        if stripped.upper().startswith("SELECT"):
            for prefix, rows in self._responses.items():
                if stripped.startswith(prefix):
                    return rows
            return []
        self.executed.append((query, placeholders))
        if self._fail_first_write and len(self.executed) == 1:
            raise RuntimeError("simulated UPSERT failure")
        return None


class _RaisingDB:
    """``executesql`` always raises -- exercises the outer broad except in
    ``_update_lag_gauge``."""

    def executesql(self, query: str, placeholders: Any = None, **kwargs: Any) -> Any:
        raise RuntimeError("database error")


def _insert_raw_event(
    db: DB,
    *,
    actor_sub: str = "user@example.com",
    ts: Optional[datetime] = None,
) -> uuid.UUID:
    """Insert a minimal, syntactically-valid ``audit_events`` row (not a
    correctly-chained one -- these tests don't exercise hash verification).
    """
    from app.security.audit_chain import ZERO_HASH, generate_uuidv7

    event_id = generate_uuidv7()
    ts = ts or datetime.now(timezone.utc)
    db.executesql(
        """
        INSERT INTO audit_events
        (id, ts, cluster_id, actor_sub, actor_scope, action, resource_kind, prev_hash, hash)
        VALUES (%(id)s, %(ts)s, 'test-cluster', %(actor_sub)s, '[]', 'create', 'node',
                %(zero)s, %(zero)s)
        """,
        {"id": str(event_id), "ts": ts, "actor_sub": actor_sub, "zero": ZERO_HASH},
    )
    return event_id
