"""Tests for the generic leader-lease primitive.

Real-Postgres tests (via the ``pg_db`` fixture from ``tests/pg_fixtures.py``)
exercise ``acquire``/``renew``/``release``/``is_leader``/``wait_for_leader``
against the actual ``leader_leases`` table + baseline schema/grants, and
prove mutual exclusion under genuine concurrent Postgres connections (not
just sequential mock calls) — see ``test_concurrent_acquirers_only_one_wins``.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from penguin_dal import DB

from app.workers import leader_lease as ll
from app.workers.leader_lease import (
    LeaderLeaseHandle,
    LeaseLostError,
    acquire,
    is_leader,
    release,
    renew,
    wait_for_leader,
)


# ---------------------------------------------------------------------------
# Pure helper — no DB.
# ---------------------------------------------------------------------------


def test_default_holder_id_includes_pid() -> None:
    holder = ll._default_holder_id()
    assert "/" in holder
    assert str(os.getpid()) in holder


def test_default_holder_id_unique_per_call() -> None:
    """The random suffix means two calls never collide."""
    assert ll._default_holder_id() != ll._default_holder_id()


# ---------------------------------------------------------------------------
# acquire / renew / release / is_leader — real Postgres.
# ---------------------------------------------------------------------------


def test_acquire_inserts_row_when_missing(pg_db: DB) -> None:
    handle = acquire(pg_db, "audit_chain_writer", ttl_seconds=60, holder_id="A")
    assert handle is not None
    assert handle.holder_id == "A"
    assert handle.version == 1
    assert handle.expires_at > handle.acquired_at

    rows = pg_db.executesql(
        "SELECT holder_id, version FROM leader_leases WHERE lease_name = %(name)s",
        {"name": "audit_chain_writer"},
    )
    assert rows == [("A", 1)]


def test_acquire_rejects_when_other_holder_active(pg_db: DB) -> None:
    a = acquire(pg_db, "lease", ttl_seconds=60, holder_id="A")
    assert a is not None
    b = acquire(pg_db, "lease", ttl_seconds=60, holder_id="B")
    assert b is None, "B must not steal an active lease from A"


def test_acquire_takes_over_after_expiry(pg_db: DB) -> None:
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    pg_db.executesql(
        "INSERT INTO leader_leases (lease_name, holder_id, acquired_at, expires_at, version) "
        "VALUES (%(name)s, %(holder)s, %(acquired)s, %(expires)s, %(version)s)",
        {
            "name": "lease",
            "holder": "A",
            "acquired": past,
            "expires": past,
            "version": 7,
        },
    )

    handle = acquire(pg_db, "lease", ttl_seconds=60, holder_id="B")
    assert handle is not None
    assert handle.holder_id == "B"
    assert handle.version == 8


def test_acquire_returns_handle_for_self(pg_db: DB) -> None:
    a1 = acquire(pg_db, "lease", ttl_seconds=60, holder_id="A")
    a2 = acquire(pg_db, "lease", ttl_seconds=60, holder_id="A")
    assert a1 is not None and a2 is not None
    assert a2.version > a1.version


def test_acquire_invalid_ttl_rejected(pg_db: DB) -> None:
    with pytest.raises(ValueError):
        acquire(pg_db, "lease", ttl_seconds=0, holder_id="A")


def test_renew_extends_expiry(pg_db: DB) -> None:
    h = acquire(pg_db, "lease", ttl_seconds=60, holder_id="A")
    assert h is not None
    h2 = renew(pg_db, h)
    assert h2.version == h.version + 1
    assert h2.expires_at >= h.expires_at


def test_renew_raises_when_lease_lost(pg_db: DB) -> None:
    h = acquire(pg_db, "lease", ttl_seconds=60, holder_id="A")
    assert h is not None
    # Simulate B stealing it after expiry by mutating state directly.
    pg_db.executesql(
        "UPDATE leader_leases SET holder_id = %(holder)s, version = %(version)s "
        "WHERE lease_name = %(name)s",
        {"holder": "B", "version": h.version + 5, "name": "lease"},
    )
    with pytest.raises(LeaseLostError):
        renew(pg_db, h)


def test_release_clears_holder_and_returns_true(pg_db: DB) -> None:
    h = acquire(pg_db, "lease", ttl_seconds=60, holder_id="A")
    assert h is not None
    ok = release(pg_db, h)
    assert ok is True

    rows = pg_db.executesql(
        "SELECT holder_id FROM leader_leases WHERE lease_name = %(name)s", {"name": "lease"}
    )
    assert rows == [(None,)]


def test_release_returns_false_if_not_held(pg_db: DB) -> None:
    h = acquire(pg_db, "lease", ttl_seconds=60, holder_id="A")
    assert h is not None
    # Steal it.
    pg_db.executesql(
        "UPDATE leader_leases SET holder_id = %(holder)s, version = %(version)s "
        "WHERE lease_name = %(name)s",
        {"holder": "B", "version": h.version + 1, "name": "lease"},
    )
    assert release(pg_db, h) is False


def test_is_leader_true_for_active_handle(pg_db: DB) -> None:
    h = acquire(pg_db, "lease", ttl_seconds=60, holder_id="A")
    assert h is not None
    assert is_leader(pg_db, h) is True


def test_is_leader_false_after_release(pg_db: DB) -> None:
    h = acquire(pg_db, "lease", ttl_seconds=60, holder_id="A")
    assert h is not None
    release(pg_db, h)
    assert is_leader(pg_db, h) is False


def test_is_leader_false_when_expired(pg_db: DB) -> None:
    h = acquire(pg_db, "lease", ttl_seconds=60, holder_id="A")
    assert h is not None
    pg_db.executesql(
        "UPDATE leader_leases SET expires_at = %(expires)s WHERE lease_name = %(name)s",
        {"expires": datetime.now(timezone.utc) - timedelta(seconds=1), "name": "lease"},
    )
    assert is_leader(pg_db, h) is False


def test_is_leader_false_for_missing_lease(pg_db: DB) -> None:
    fake_handle = LeaderLeaseHandle(
        lease_name="never-created",
        holder_id="A",
        ttl_seconds=60,
        acquired_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        version=1,
    )
    assert is_leader(pg_db, fake_handle) is False


# ---------------------------------------------------------------------------
# Mutual exclusion under genuine concurrency — the lock-serialization proof.
# ---------------------------------------------------------------------------


def test_concurrent_acquirers_only_one_wins(pg_db: DB) -> None:
    """N threads race ``acquire()`` on the same unclaimed lease via real,
    concurrent Postgres connections (drawn from ``pg_db``'s connection pool,
    each ``executesql()`` call its own round-trip) -- not sequential mock
    calls. Only one may win.

    This is a genuine proof of the CAS mechanism, not the pg_advisory lock
    (leader_leases carries no advisory lock -- see module docstring): if the
    ``UPDATE ... WHERE version = :old_version`` CAS were broken (e.g. missing
    the version predicate, or split across a read+write that wasn't
    atomically re-checked), concurrent threads reading the same
    pre-acquisition state would race past the check and more than one would
    successfully "win" the lease.
    """

    barrier = threading.Barrier(10)
    results: list[Optional[LeaderLeaseHandle]] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        barrier.wait()
        h = acquire(pg_db, "race", ttl_seconds=60, holder_id=name)
        with lock:
            results.append(h)

    threads = [threading.Thread(target=worker, args=(f"replica-{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r is not None]
    assert len(successes) == 1, f"expected exactly one winner, got {len(successes)}"

    rows = pg_db.executesql(
        "SELECT holder_id, version FROM leader_leases WHERE lease_name = %(name)s",
        {"name": "race"},
    )
    assert rows == [(successes[0].holder_id, 1)]


def test_concurrent_cas_on_expired_lease_only_one_wins(pg_db: DB) -> None:
    """Same proof, but racing to steal an already-expired lease (CAS on an
    existing row, not the INSERT-race path)."""

    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    pg_db.executesql(
        "INSERT INTO leader_leases (lease_name, holder_id, acquired_at, expires_at, version) "
        "VALUES (%(name)s, %(holder)s, %(acquired)s, %(expires)s, %(version)s)",
        {
            "name": "expired-race",
            "holder": "stale-holder",
            "acquired": past,
            "expires": past,
            "version": 3,
        },
    )

    barrier = threading.Barrier(10)
    results: list[Optional[LeaderLeaseHandle]] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        barrier.wait()
        h = acquire(pg_db, "expired-race", ttl_seconds=60, holder_id=name)
        with lock:
            results.append(h)

    threads = [
        threading.Thread(target=worker, args=(f"replica-{i}",)) for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r is not None]
    assert len(successes) == 1, f"expected exactly one winner, got {len(successes)}"
    assert successes[0].version == 4


# ---------------------------------------------------------------------------
# wait_for_leader
# ---------------------------------------------------------------------------


def test_wait_for_leader_returns_when_acquired(pg_url: str, pg_db: DB) -> None:
    def factory() -> DB:
        return DB(pg_url, pool_size=1, reflect=True)

    handle = wait_for_leader(
        factory,
        "lease",
        ttl_seconds=60,
        poll_interval_seconds=0.01,
        holder_id="A",
        max_attempts=3,
    )
    assert handle is not None
    assert handle.holder_id == "A"


def test_wait_for_leader_raises_after_max_attempts(pg_url: str, pg_db: DB) -> None:
    acquire(pg_db, "lease", ttl_seconds=3600, holder_id="B")

    def factory() -> DB:
        return DB(pg_url, pool_size=1, reflect=True)

    with pytest.raises(ll.LeaderLeaseError):
        wait_for_leader(
            factory,
            "lease",
            ttl_seconds=60,
            poll_interval_seconds=0.001,
            holder_id="A",
            max_attempts=2,
        )
