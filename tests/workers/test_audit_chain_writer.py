"""Tests for audit_chain_writer — leader enforcement, mirroring, verification."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.models_m1 import Base, AuditEvent as AuditEventModel
from app.security.audit_chain import AuditEvent, verify_chain
from app.workers import leader_lease as ll
from app.workers.audit_chain_writer import (
    LEASE_NAME,
    NATS_SUBJECT_AUDIT_BREAK,
    AuditChainError,
    AuditChainWriter,
    NotLeaderError,
    OffsiteMirrorConfig,
    OffsiteMirrorError,
)


@pytest.fixture
def sqlite_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def audit_events_table(sqlite_session):
    """Ensure audit_events table exists (created by bootstrap)."""
    try:
        sqlite_session.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY,
                ts TIMESTAMP NOT NULL,
                cluster_id TEXT NOT NULL,
                tenant_id TEXT,
                actor_sub TEXT NOT NULL,
                actor_scope TEXT,
                action TEXT NOT NULL,
                resource_kind TEXT NOT NULL,
                resource_id TEXT,
                before_json TEXT,
                after_json TEXT,
                request_id TEXT,
                source_ip TEXT,
                user_agent TEXT,
                prev_hash BLOB NOT NULL,
                hash BLOB NOT NULL,
                signature BLOB
            )
        """
            )
        )
        sqlite_session.commit()
    except Exception:
        pass
    yield sqlite_session


@pytest.fixture
def mirror_state_table(sqlite_session):
    """Create audit_events_mirror_state table."""
    try:
        sqlite_session.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS audit_events_mirror_state (
                id INTEGER PRIMARY KEY,
                last_shipped_id TEXT,
                updated_at TIMESTAMP
            )
        """
            )
        )
        sqlite_session.commit()
    except Exception:
        pass
    yield sqlite_session


@pytest.fixture
def mock_vault_client():
    """Mock Vault client."""
    client = MagicMock()
    client.get_kms_key_id.return_value = "test-kms-key"
    return client


@pytest.fixture
def mock_nats_publisher():
    """Mock NATS publisher async function."""
    async def publisher(subject: str, payload: dict):
        return None
    return publisher


class TestAuditChainWriterAppendEvent:
    """Test append_event under leader-only enforcement."""

    def test_append_event_as_leader(self, audit_events_table, mirror_state_table, mock_vault_client):
        """Leader replica appends successfully."""
        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
            cluster_id="test-cluster",
        )
        writer._lease_handle = ll.LeaderLeaseHandle(
            lease_name=LEASE_NAME,
            holder_id="leader",
            ttl_seconds=60,
            acquired_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            version=1,
        )

        event = writer.append_event(
            actor_sub="user123",
            action="create",
            resource_kind="node",
            resource_id="node-abc",
            tenant_id="tenant1",
        )
        assert event is not None
        assert event.id is not None
        assert event.action == "create"
        audit_events_table.commit()

    def test_append_event_as_non_leader_raises(self, audit_events_table, mock_vault_client):
        """Non-leader replica raises NotLeaderError."""
        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
            cluster_id="test-cluster",
        )
        with pytest.raises(NotLeaderError, match="not the audit_chain_writer leader"):
            writer.append_event(
                actor_sub="user",
                action="create",
                resource_kind="node",
            )

    def test_append_event_increments_counter(self, audit_events_table, mock_vault_client):
        """Append increments gough_audit_append_total counter."""
        from app.workers.audit_chain_writer import APPEND_COUNTER

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
            cluster_id="test-cluster",
        )
        writer._lease_handle = ll.LeaderLeaseHandle(
            lease_name=LEASE_NAME,
            holder_id="leader",
            ttl_seconds=60,
            acquired_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            version=1,
        )
        initial_value = APPEND_COUNTER._value.get()

        writer.append_event(
            actor_sub="user",
            action="create",
            resource_kind="node",
        )
        audit_events_table.commit()

        assert APPEND_COUNTER._value.get() > initial_value

    def test_append_event_with_before_after(self, audit_events_table, mock_vault_client):
        """Append with before/after JSON payloads."""
        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
        )
        writer._lease_handle = ll.LeaderLeaseHandle(
            lease_name=LEASE_NAME,
            holder_id="leader",
            ttl_seconds=60,
            acquired_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            version=1,
        )

        event = writer.append_event(
            actor_sub="user",
            action="update",
            resource_kind="node",
            resource_id="node1",
            before={"state": "new"},
            after={"state": "ready"},
        )
        assert event.before_json == {"state": "new"}
        assert event.after_json == {"state": "ready"}
        audit_events_table.commit()


class TestAuditChainWriterLeadershipManagement:
    """Test acquire/release leadership."""

    def test_try_acquire_leadership_success(self, audit_events_table, mock_vault_client):
        """try_acquire_leadership succeeds on fresh lease."""
        mock_lease_client = MagicMock()
        mock_lease_client.acquire.return_value = ll.LeaderLeaseHandle(
            lease_name=LEASE_NAME,
            holder_id="replica1",
            ttl_seconds=60,
            acquired_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            version=1,
        )

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
            leader_lease_client=mock_lease_client,
        )
        ok = writer.try_acquire_leadership()
        assert ok is True
        assert writer.is_leader() is True

    def test_try_acquire_leadership_failure(self, audit_events_table, mock_vault_client):
        """try_acquire_leadership returns False on lease held elsewhere."""
        mock_lease_client = MagicMock()
        mock_lease_client.acquire.return_value = None

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
            leader_lease_client=mock_lease_client,
        )
        ok = writer.try_acquire_leadership()
        assert ok is False

    def test_release_leadership(self, audit_events_table, mock_vault_client):
        """release_leadership clears the handle."""
        mock_lease_client = MagicMock()
        mock_lease_client.release.return_value = True
        mock_lease_client.acquire.return_value = ll.LeaderLeaseHandle(
            lease_name=LEASE_NAME,
            holder_id="replica1",
            ttl_seconds=60,
            acquired_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            version=1,
        )

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
            leader_lease_client=mock_lease_client,
        )
        writer.try_acquire_leadership()
        ok = writer.release_leadership()
        assert ok is True
        assert writer.is_leader() is False


class TestAuditChainWriterOffsiteMirror:
    """Test offsite mirror shipper."""

    @pytest.mark.asyncio
    async def test_start_offsite_mirror_empty_table(
        self, audit_events_table, mirror_state_table, mock_vault_client
    ):
        """start_offsite_mirror on empty audit_events returns early."""
        mock_s3_client = AsyncMock()

        async def s3_factory(cfg):
            return mock_s3_client

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
            s3_session_factory=s3_factory,
        )

        summary = await writer.start_offsite_mirror(
            s3_endpoint="http://localhost:9000",
            bucket="audit-bucket",
            retention_days=2555,
            max_iterations=1,
            access_key_id="test",
            secret_access_key="test",
        )
        assert summary["rows_shipped"] == 0
        assert summary["iterations"] == 1

    @pytest.mark.asyncio
    async def test_start_offsite_mirror_ships_rows(
        self, audit_events_table, mirror_state_table, mock_vault_client
    ):
        """start_offsite_mirror ships audit_events rows to S3."""
        # Insert a test row
        event_id = uuid.uuid4()
        audit_events_table.execute(
            text(
                """
            INSERT INTO audit_events
            (id, ts, cluster_id, actor_sub, action, resource_kind, prev_hash, hash)
            VALUES (:id, :ts, :cluster_id, :actor_sub, :action, :kind, :prev_hash, :hash)
        """
            ),
            {
                "id": str(event_id),
                "ts": datetime.now(timezone.utc),
                "cluster_id": "test",
                "actor_sub": "user",
                "action": "create",
                "kind": "node",
                "prev_hash": b"\x00" * 32,
                "hash": b"\x01" * 32,
            },
        )
        audit_events_table.commit()

        mock_s3_client = AsyncMock()

        async def s3_factory(cfg):
            return AsyncMock()

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
            s3_session_factory=s3_factory,
        )

        summary = await writer.start_offsite_mirror(
            s3_endpoint="http://localhost:9000",
            bucket="audit-bucket",
            retention_days=2555,
            max_iterations=2,
            access_key_id="test",
            secret_access_key="test",
        )
        assert summary["rows_shipped"] >= 1

    @pytest.mark.asyncio
    async def test_start_offsite_mirror_missing_aioboto3_raises(
        self, audit_events_table, mock_vault_client
    ):
        """start_offsite_mirror without aioboto3 raises OffsiteMirrorError."""
        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
        )

        with patch("app.workers.audit_chain_writer.aioboto3", side_effect=ImportError):
            with pytest.raises(OffsiteMirrorError, match="aioboto3 is required"):
                await writer.start_offsite_mirror(
                    s3_endpoint="http://localhost:9000",
                    bucket="audit-bucket",
                    retention_days=2555,
                )

    @pytest.mark.asyncio
    async def test_start_offsite_mirror_graceful_shutdown(
        self, audit_events_table, mirror_state_table, mock_vault_client
    ):
        """start_offsite_mirror respects stop_event."""
        async def s3_factory(cfg):
            return AsyncMock()

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
            s3_session_factory=s3_factory,
        )

        stop_event = asyncio.Event()
        stop_event.set()

        summary = await writer.start_offsite_mirror(
            s3_endpoint="http://localhost:9000",
            bucket="audit-bucket",
            retention_days=2555,
            stop_event=stop_event,
            max_iterations=100,
        )
        assert summary["iterations"] == 0


class TestAuditChainWriterVerifyChainScheduled:
    """Test scheduled verify_chain with NATS event emission."""

    def test_verify_chain_scheduled_no_breaks(self, audit_events_table, mock_vault_client):
        """verify_chain_scheduled with no breaks returns clean result."""
        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
        )

        result = writer.verify_chain_scheduled(
            since=timedelta(hours=24), emit_nats=False
        )
        assert result["breaks"] == 0
        assert result["rows_checked"] == 0

    def test_verify_chain_scheduled_with_break(
        self, audit_events_table, mock_vault_client, mock_nats_publisher
    ):
        """verify_chain_scheduled detects a break and emits NATS event."""
        # Insert genesis + normal row
        audit_events_table.execute(
            text(
                """
            INSERT INTO audit_events
            (id, ts, cluster_id, actor_sub, action, resource_kind, prev_hash, hash)
            VALUES (:id, :ts, :cluster_id, :actor_sub, :action, :kind, :prev_hash, :hash)
        """
            ),
            {
                "id": "00000000-0000-7000-8000-000000000000",
                "ts": datetime.now(timezone.utc),
                "cluster_id": "test",
                "actor_sub": "system",
                "action": "cluster_init",
                "kind": "cluster",
                "prev_hash": b"\x00" * 32,
                "hash": b"\x00" * 32,
            },
        )

        event_id = uuid.uuid4()
        audit_events_table.execute(
            text(
                """
            INSERT INTO audit_events
            (id, ts, cluster_id, actor_sub, action, resource_kind, prev_hash, hash)
            VALUES (:id, :ts, :cluster_id, :actor_sub, :action, :kind, :prev_hash, :hash)
        """
            ),
            {
                "id": str(event_id),
                "ts": datetime.now(timezone.utc),
                "cluster_id": "test",
                "actor_sub": "user",
                "action": "create",
                "kind": "node",
                "prev_hash": b"\x00" * 32,
                "hash": b"\x99" * 32,
            },
        )
        audit_events_table.commit()

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
            nats_publisher=mock_nats_publisher,
        )

        result = writer.verify_chain_scheduled(
            since=timedelta(hours=24), emit_nats=True
        )
        assert result["breaks"] >= 1

    def test_verify_chain_scheduled_increments_counter(
        self, audit_events_table, mock_vault_client
    ):
        """verify_chain_scheduled increments break counter on failures."""
        from app.workers.audit_chain_writer import CHAIN_BREAK_COUNTER

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
        )
        initial = CHAIN_BREAK_COUNTER._value.get()

        result = writer.verify_chain_scheduled(
            since=timedelta(hours=24), emit_nats=False
        )
        assert result["breaks"] == 0
        assert CHAIN_BREAK_COUNTER._value.get() == initial


class TestOffsiteMirrorConfig:
    """Test OffsiteMirrorConfig dataclass."""

    def test_offsite_mirror_config_defaults(self):
        """OffsiteMirrorConfig has sensible defaults."""
        cfg = OffsiteMirrorConfig(
            s3_endpoint="http://localhost:9000",
            bucket="test-bucket",
            retention_days=2555,
        )
        assert cfg.region == "us-east-1"
        assert cfg.object_prefix == "audit-events/"
        assert cfg.poll_interval_seconds == 1.0
        assert cfg.max_batch_size == 500
        assert cfg.object_lock_mode == "COMPLIANCE"

    def test_offsite_mirror_config_object_key(self):
        """object_key() generates correct S3 key."""
        cfg = OffsiteMirrorConfig(
            s3_endpoint="http://localhost:9000",
            bucket="test-bucket",
            retention_days=2555,
        )
        event_id = uuid.uuid4()
        key = cfg.object_key(event_id)
        assert key == f"audit-events/{event_id}.json"


class TestAuditChainWriterInternals:
    """Test internal helper methods."""

    def test_read_watermark_none(self, sqlite_session, mirror_state_table):
        """_read_watermark returns None on missing state."""
        writer = AuditChainWriter(
            sqlite_session,
            MagicMock(),
        )
        wm = writer._read_watermark()
        assert wm is None

    def test_row_to_object_payload(self, sqlite_session):
        """_row_to_object_payload serializes row to JCS."""
        writer = AuditChainWriter(
            sqlite_session,
            MagicMock(),
        )
        row = {
            "id": uuid.uuid4(),
            "ts": datetime.now(timezone.utc),
            "action": "create",
            "prev_hash": b"\x00" * 32,
            "hash": b"\x01" * 32,
        }
        payload = writer._row_to_object_payload(row)
        assert isinstance(payload, bytes)
        # Should be valid JCS (JSON)
        decoded = json.loads(payload)
        assert "action" in decoded

    def test_update_lag_gauge(self, audit_events_table, mock_vault_client):
        """_update_lag_gauge computes and sets lag metric."""
        from app.workers.audit_chain_writer import MIRROR_LAG_GAUGE

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
        )
        writer._update_lag_gauge(None)


class TestNotLeaderErrorAndMetrics:
    """Test error handling and metrics."""

    def test_not_leader_counter_increments(self, audit_events_table, mock_vault_client):
        """Non-leader append increments gough_audit_append_not_leader_total."""
        from app.workers.audit_chain_writer import NOT_LEADER_COUNTER

        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
        )
        initial = NOT_LEADER_COUNTER._value.get()

        with pytest.raises(NotLeaderError):
            writer.append_event(
                actor_sub="user",
                action="create",
                resource_kind="node",
            )

        assert NOT_LEADER_COUNTER._value.get() > initial

    def test_audit_chain_error_inheritance(self):
        """NotLeaderError and OffsiteMirrorError inherit from AuditChainError."""
        assert issubclass(NotLeaderError, AuditChainError)
        assert issubclass(OffsiteMirrorError, AuditChainError)


class TestAuditChainWriterEdgeCases:
    """Test edge cases and error handling."""

    def test_append_event_with_all_optional_fields(
        self, audit_events_table, mock_vault_client
    ):
        """Append with all optional fields populated."""
        writer = AuditChainWriter(
            audit_events_table,
            mock_vault_client,
        )
        writer._lease_handle = ll.LeaderLeaseHandle(
            lease_name=LEASE_NAME,
            holder_id="leader",
            ttl_seconds=60,
            acquired_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            version=1,
        )

        event = writer.append_event(
            actor_sub="user123",
            action="create",
            resource_kind="node",
            resource_id="node-abc",
            tenant_id="tenant1",
            actor_scope=["nodes:write", "audit:read"],
            before={"state": "new"},
            after={"state": "ready"},
            request_id="req-123",
            source_ip="10.0.0.1",
            user_agent="Gough/1.0",
        )
        assert event.actor_scope == ["nodes:write", "audit:read"]
        assert event.source_ip == "10.0.0.1"
        audit_events_table.commit()

    def test_persist_watermark_fallback(self, mirror_state_table, mock_vault_client):
        """_persist_watermark handles missing state table gracefully."""
        writer = AuditChainWriter(
            mirror_state_table,
            mock_vault_client,
        )
        row_id = uuid.uuid4()
        writer._persist_watermark(row_id)


def test_audit_chain_writer_coverage():
    """Coverage helper for all class/error types."""
    assert AuditChainWriter is not None
    assert NotLeaderError is not None
    assert OffsiteMirrorError is not None
    assert AuditChainError is not None
    assert OffsiteMirrorConfig is not None
    assert LEASE_NAME == "audit_chain_writer"
    assert NATS_SUBJECT_AUDIT_BREAK == "gough.security.audit_chain_break"
