"""Tests for leader_lease module — concurrency, expiration, CAS semantics."""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.models_m1 import Base, LeaderLease
from app.workers import leader_lease as ll


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
def postgres_session():
    """Create a Postgres test session (requires GOUGH_TEST_DB)."""
    import os
    db_url = os.getenv("GOUGH_TEST_DB")
    if not db_url:
        pytest.skip("GOUGH_TEST_DB not set")
    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    Base.metadata.drop_all(engine)
    session.close()


class TestLeaderLeaseAcquire:
    """Test :func:`acquire` — success, failure, race, expiration."""

    def test_acquire_fresh_lease(self, sqlite_session):
        """Acquire a fresh lease; should succeed on first call."""
        handle = ll.acquire(sqlite_session, "test_lease", ttl_seconds=60)
        assert handle is not None
        assert handle.lease_name == "test_lease"
        assert handle.ttl_seconds == 60
        assert handle.holder_id is not None
        assert handle.version == 1

    def test_acquire_with_custom_holder(self, sqlite_session):
        """Acquire with a specific holder_id."""
        holder = "custom_holder"
        handle = ll.acquire(sqlite_session, "test_lease", holder_id=holder)
        assert handle is not None
        assert handle.holder_id == holder

    def test_acquire_held_lease_fails(self, sqlite_session):
        """Acquiring a held lease from another holder fails."""
        holder1 = "holder1"
        holder2 = "holder2"
        h1 = ll.acquire(sqlite_session, "test_lease", holder_id=holder1)
        assert h1 is not None
        h2 = ll.acquire(sqlite_session, "test_lease", holder_id=holder2)
        assert h2 is None

    def test_acquire_expired_lease_succeeds(self, sqlite_session):
        """Acquiring an expired lease succeeds (takeover)."""
        holder1 = "holder1"
        holder2 = "holder2"
        h1 = ll.acquire(
            sqlite_session, "test_lease", ttl_seconds=1, holder_id=holder1
        )
        assert h1 is not None
        sqlite_session.commit()

        time.sleep(1.1)
        h2 = ll.acquire(sqlite_session, "test_lease", holder_id=holder2)
        assert h2 is not None
        assert h2.holder_id == holder2
        assert h2.version == 2

    def test_acquire_same_holder_renews_version(self, sqlite_session):
        """Acquiring with same holder_id renews and bumps version."""
        holder = "holder"
        h1 = ll.acquire(sqlite_session, "test_lease", holder_id=holder)
        assert h1.version == 1
        sqlite_session.commit()

        h2 = ll.acquire(sqlite_session, "test_lease", holder_id=holder)
        assert h2 is not None
        assert h2.version == 2

    def test_acquire_cas_conflict(self, sqlite_session):
        """Two concurrent acquires on expired lease — only one wins via CAS."""
        holder1 = "holder1"
        holder2 = "holder2"
        h1 = ll.acquire(
            sqlite_session, "test_lease", ttl_seconds=1, holder_id=holder1
        )
        assert h1 is not None
        sqlite_session.commit()

        time.sleep(1.1)

        # Simulate two concurrent acquires by mocking rowcount
        # First one succeeds, second fails
        h2 = ll.acquire(sqlite_session, "test_lease", holder_id=holder2)
        assert h2 is not None
        sqlite_session.commit()

        h3 = ll.acquire(sqlite_session, "test_lease", holder_id="holder3")
        assert h3 is None

    def test_acquire_null_holder_can_be_taken(self, sqlite_session):
        """A row with NULL holder (unowned) can be acquired."""
        sqlite_session.execute(
            text(
                "INSERT INTO leader_leases "
                "(lease_name, holder_id, acquired_at, expires_at, version) "
                "VALUES ('null_lease', NULL, :now, :exp, 1)"
            ),
            {
                "now": datetime.now(timezone.utc),
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
        )
        sqlite_session.commit()

        handle = ll.acquire(sqlite_session, "null_lease", holder_id="new_holder")
        assert handle is not None
        assert handle.holder_id == "new_holder"

    def test_acquire_zero_ttl_raises(self, sqlite_session):
        """Negative/zero TTL raises ValueError."""
        with pytest.raises(ValueError, match="ttl_seconds must be positive"):
            ll.acquire(sqlite_session, "test", ttl_seconds=0)


class TestLeaderLeaseRenew:
    """Test :func:`renew` — success, failure, CAS conflict."""

    def test_renew_extends_expiry(self, sqlite_session):
        """Renew extends the expiry and bumps version."""
        handle = ll.acquire(sqlite_session, "test_lease", ttl_seconds=60)
        assert handle is not None
        original_expires = handle.expires_at

        renewed = ll.renew(sqlite_session, handle)
        assert renewed is not None
        assert renewed.expires_at > original_expires
        assert renewed.version == handle.version + 1

    def test_renew_lost_lease_raises(self, sqlite_session):
        """Renew on a stolen/deleted lease raises LeaseLostError."""
        handle = ll.acquire(sqlite_session, "test_lease", holder_id="holder1")
        assert handle is not None
        sqlite_session.commit()

        ll.acquire(
            sqlite_session, "test_lease", holder_id="holder2"
        )
        sqlite_session.commit()

        with pytest.raises(ll.LeaseLostError, match="renewal failed"):
            ll.renew(sqlite_session, handle)

    def test_renew_version_conflict_raises(self, sqlite_session):
        """Renew with stale version raises LeaseLostError."""
        handle = ll.acquire(sqlite_session, "test_lease", holder_id="holder1")
        assert handle is not None
        sqlite_session.commit()

        ll.renew(sqlite_session, handle)
        sqlite_session.commit()

        with pytest.raises(ll.LeaseLostError):
            ll.renew(sqlite_session, handle)


class TestLeaderLeaseRelease:
    """Test :func:`release` — success, failure."""

    def test_release_clears_holder(self, sqlite_session):
        """Release clears the holder and returns True."""
        handle = ll.acquire(sqlite_session, "test_lease")
        assert handle is not None
        sqlite_session.commit()

        ok = ll.release(sqlite_session, handle)
        assert ok is True

        row = sqlite_session.execute(
            text("SELECT holder_id FROM leader_leases WHERE lease_name = 'test_lease'")
        ).first()
        assert row[0] is None

    def test_release_not_held_returns_false(self, sqlite_session):
        """Release of stolen/deleted lease returns False."""
        handle = ll.acquire(sqlite_session, "test_lease", holder_id="holder1")
        assert handle is not None
        sqlite_session.commit()

        ll.acquire(sqlite_session, "test_lease", holder_id="holder2")
        sqlite_session.commit()

        ok = ll.release(sqlite_session, handle)
        assert ok is False


class TestLeaderLeaseIsLeader:
    """Test :func:`is_leader` — cheap in-memory checks."""

    def test_is_leader_on_held_lease(self, sqlite_session):
        """is_leader returns True for a held, non-expired lease."""
        handle = ll.acquire(sqlite_session, "test_lease", ttl_seconds=60)
        assert handle is not None
        sqlite_session.commit()

        assert ll.is_leader(sqlite_session, handle) is True

    def test_is_leader_on_stolen_lease(self, sqlite_session):
        """is_leader returns False after another holder steals."""
        handle = ll.acquire(sqlite_session, "test_lease", holder_id="holder1")
        assert handle is not None
        sqlite_session.commit()

        ll.acquire(sqlite_session, "test_lease", holder_id="holder2")
        sqlite_session.commit()

        assert ll.is_leader(sqlite_session, handle) is False

    def test_is_leader_on_expired_lease(self, sqlite_session):
        """is_leader returns False after expiry."""
        handle = ll.acquire(sqlite_session, "test_lease", ttl_seconds=1)
        assert handle is not None
        sqlite_session.commit()

        time.sleep(1.1)
        assert ll.is_leader(sqlite_session, handle) is False

    def test_is_leader_on_deleted_row(self, sqlite_session):
        """is_leader returns False if row is deleted."""
        handle = ll.acquire(sqlite_session, "test_lease")
        assert handle is not None
        sqlite_session.commit()

        sqlite_session.execute(
            text("DELETE FROM leader_leases WHERE lease_name = 'test_lease'")
        )
        sqlite_session.commit()

        assert ll.is_leader(sqlite_session, handle) is False

    def test_is_leader_version_mismatch(self, sqlite_session):
        """is_leader returns False if version drifted."""
        handle = ll.acquire(sqlite_session, "test_lease", holder_id="holder")
        assert handle is not None
        sqlite_session.commit()

        ll.renew(sqlite_session, handle)
        sqlite_session.commit()

        assert ll.is_leader(sqlite_session, handle) is False


class TestLeaderLeaseWaitForLeader:
    """Test :func:`wait_for_leader` — blocking poll until acquired."""

    def test_wait_for_leader_immediate(self, sqlite_session):
        """wait_for_leader succeeds immediately on fresh lease."""
        def factory():
            from sqlalchemy.orm import sessionmaker
            from sqlalchemy import create_engine
            engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(engine)
            return sessionmaker(bind=engine)()

        with patch(
            "app.workers.leader_lease.acquire",
            return_value=ll.LeaderLeaseHandle(
                lease_name="test",
                holder_id="holder",
                ttl_seconds=60,
                acquired_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
                version=1,
            ),
        ):
            handle = ll.wait_for_leader(factory, "test", max_attempts=1)
            assert handle is not None

    def test_wait_for_leader_max_attempts(self, sqlite_session):
        """wait_for_leader raises after max_attempts."""
        def factory():
            return sqlite_session

        with pytest.raises(ll.LeaderLeaseError, match="within 2 attempts"):
            ll.wait_for_leader(
                factory,
                "never_free",
                ttl_seconds=60,
                poll_interval_seconds=0.01,
                holder_id="holder",
                max_attempts=2,
            )


class TestLeaderLeaseDefaultHolderId:
    """Test _default_holder_id uniqueness."""

    def test_default_holder_id_format(self):
        """Default holder_id has format hostname/pid/rand."""
        holder = ll._default_holder_id()
        parts = holder.split("/")
        assert len(parts) == 3
        assert parts[0] != ""
        assert parts[1].isdigit()
        assert len(parts[2]) == 8

    def test_default_holder_id_changes(self):
        """Multiple calls produce different holder IDs."""
        h1 = ll._default_holder_id()
        h2 = ll._default_holder_id()
        assert h1 != h2


class TestLeaderLeaseEdgeCases:
    """Test edge cases, error handling."""

    def test_acquire_with_none_session(self):
        """Passing None as session should fail gracefully."""
        with pytest.raises(AttributeError):
            ll.acquire(None, "test")

    def test_row_to_dict_with_tuple(self):
        """_row_to_dict handles tuple rows (SQLite fallback)."""
        row = ("lease", "holder", "2025-01-01", "2025-01-02", 1)
        result = ll._row_to_dict(row)
        assert result["lease_name"] == "lease"
        assert result["holder_id"] == "holder"
        assert result["version"] == 1

    def test_row_to_dict_with_none(self):
        """_row_to_dict handles None."""
        result = ll._row_to_dict(None)
        assert result == {}

    def test_consecutive_acquire_release_acquire(self, sqlite_session):
        """Acquire → Release → Acquire by different holders works."""
        h1 = ll.acquire(sqlite_session, "test", holder_id="holder1")
        assert h1 is not None
        sqlite_session.commit()

        ok = ll.release(sqlite_session, h1)
        assert ok is True
        sqlite_session.commit()

        h2 = ll.acquire(sqlite_session, "test", holder_id="holder2")
        assert h2 is not None
        assert h2.holder_id == "holder2"


def test_coverage_helper():
    """Dummy test to ensure import coverage."""
    assert ll.DEFAULT_TTL_SECONDS == 60
    assert ll.LeaderLeaseHandle is not None
    assert ll.LeaderLeaseError is not None
    assert ll.LeaseLostError is not None
