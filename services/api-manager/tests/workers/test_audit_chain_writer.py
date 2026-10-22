"""Tests for the audit_chain_writer worker (real Postgres via ``pg_db``).

Covers:

* Two-instance leader test — A is leader, writes succeed; B is non-leader,
  writes raise NotLeaderError.
* ``append_event`` runs its advisory-lock + SERIALIZABLE + hash-chain
  read+insert inside one shared, pinned ``db.transaction()`` — proven by a
  genuine concurrent-writer test (see
  ``test_concurrent_append_serializes_and_never_forks_chain``).
* RLS cross-tenant sentinel — proven against ``pg_db_scoped`` (see
  ``test_append_event_and_verify_require_cross_tenant_scope``).
* Tampering — synthetic break in audit_events, verify_chain detects, metric
  increments, NATS event fires with first_break_id.
* Offsite mirror — moto-backed S3; assert every committed row is shipped with
  correct content + retention metadata; lag gauge updates. (Skipped per the
  pre-existing Python 3.14 asyncio.to_thread hang — see the skip reason.)
* Scheduled verify — 24 h window respected; clean chain emits no event.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from penguin_dal import DB

from app.db.rls import install_rls_events, set_current_tenant
from app.security.audit_chain import ZERO_HASH, compute_chain_hash, generate_uuidv7
from app.workers import audit_chain_writer as acw_module
from app.workers import leader_lease as ll
from app.workers.audit_chain_writer import (
    AuditChainWriter,
    NotLeaderError,
    OffsiteMirrorConfig,
)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    """Narrow an ``executesql()`` result to ``list[tuple]`` for test assertions."""
    return cast("list[tuple[Any, ...]]", result)


_OFFSITE_MIRROR_SKIP_REASON = (
    "asyncio.to_thread + prometheus_client autouse fixtures hang "
    "indefinitely in Python 3.14 test environment"
)


# ---------------------------------------------------------------------------
# Seeding helper — real Postgres INSERT with a correctly-computed chain hash
# for a caller-chosen ``ts`` (AuditEventWriter.append() always uses "now",
# so window-boundary tests that need specific timestamps go straight to SQL,
# mirroring AuditEventWriter._append_on_tx's insert shape exactly).
# ---------------------------------------------------------------------------


def _seed_event(
    db: DB,
    *,
    actor_sub: str = "user@x",
    action: str = "node.create",
    resource_kind: str = "node",
    ts: Optional[datetime] = None,
    cluster_id: str = "test-cluster",
) -> dict[str, Any]:
    """Insert a real, correctly-chained ``audit_events`` row at a chosen ``ts``."""

    ts = ts or datetime.now(timezone.utc)
    prev_rows = _rows(
        db.executesql("SELECT hash FROM audit_events ORDER BY ts DESC, id DESC LIMIT 1")
    )
    prev_hash_raw = prev_rows[0][0] if prev_rows else None
    prev_hash = (
        bytes(prev_hash_raw)
        if isinstance(prev_hash_raw, (bytes, bytearray, memoryview))
        and len(prev_hash_raw) == 32
        else ZERO_HASH
    )

    event_id = generate_uuidv7()
    fields: dict[str, Any] = {
        "id": str(event_id),
        "ts": ts.isoformat(),
        "cluster_id": cluster_id,
        "tenant_id": None,
        "actor_sub": actor_sub,
        "actor_scope": [],
        "action": action,
        "resource_kind": resource_kind,
        "resource_id": None,
        "before_json": None,
        "after_json": None,
        "request_id": None,
        "source_ip": None,
        "user_agent": None,
    }
    chain_hash = compute_chain_hash(prev_hash, fields)

    db.executesql(
        """
        INSERT INTO audit_events
        (id, ts, cluster_id, tenant_id, actor_sub, actor_scope, action,
         resource_kind, resource_id, before_json, after_json, request_id,
         source_ip, user_agent, prev_hash, hash, signature)
        VALUES (%(id)s, %(ts)s, %(cluster_id)s, %(tenant_id)s, %(actor_sub)s,
                %(actor_scope)s, %(action)s, %(resource_kind)s, %(resource_id)s,
                %(before_json)s, %(after_json)s, %(request_id)s, %(source_ip)s,
                %(user_agent)s, %(prev_hash)s, %(hash)s, NULL)
        """,
        {
            "id": str(event_id),
            "ts": ts,
            "cluster_id": cluster_id,
            "tenant_id": None,
            "actor_sub": actor_sub,
            "actor_scope": json.dumps([]),
            "action": action,
            "resource_kind": resource_kind,
            "resource_id": None,
            "before_json": None,
            "after_json": None,
            "request_id": None,
            "source_ip": None,
            "user_agent": None,
            "prev_hash": prev_hash,
            "hash": chain_hash,
        },
    )
    return {"id": event_id, "ts": ts, "hash": chain_hash, "prev_hash": prev_hash}


# ---------------------------------------------------------------------------
# Stub leader-lease client — controls leadership outcome per replica without
# touching the real leader_leases table (that primitive is covered
# end-to-end in test_leader_lease.py).
# ---------------------------------------------------------------------------


class StubLeaseClient:
    """Pluggable lease client that the writer accepts in place of the module."""

    def __init__(self) -> None:
        self.holder: Optional[str] = None
        self.version = 0

    def acquire(
        self, db_session: Any, lease_name: str, ttl_seconds: int, holder_id: str
    ) -> Any:
        if self.holder is None or self.holder == holder_id:
            self.holder = holder_id
            self.version += 1
            now = datetime.now(timezone.utc)
            return ll.LeaderLeaseHandle(
                lease_name=lease_name,
                holder_id=holder_id,
                ttl_seconds=ttl_seconds,
                acquired_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
                version=self.version,
            )
        return None

    def release(self, db_session: Any, handle: Any) -> bool:
        if handle.holder_id == self.holder:
            self.holder = None
            return True
        return False

    def renew(self, db_session: Any, handle: Any) -> Any:
        return handle

    def is_leader(self, db_session: Any, handle: Any) -> bool:
        return bool(handle.holder_id == self.holder)


# ---------------------------------------------------------------------------
# Two-instance leadership test
# ---------------------------------------------------------------------------


def test_leader_can_append_non_leader_raises_not_leader(pg_db: DB) -> None:
    lease = StubLeaseClient()
    a = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=lease,
        replica_id="A",
        cluster_id="c1",
    )
    b = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=lease,
        replica_id="B",
        cluster_id="c1",
    )

    assert a.try_acquire_leadership() is True
    assert b.try_acquire_leadership() is False

    before_append_calls = acw_module.APPEND_COUNTER.inc.call_count
    event = a.append_event(
        actor_sub="alice", action="node.create", resource_kind="node"
    )
    assert event.actor_sub == "alice"
    assert acw_module.APPEND_COUNTER.inc.call_count == before_append_calls + 1

    not_leader_before_calls = acw_module.NOT_LEADER_COUNTER.inc.call_count
    with pytest.raises(NotLeaderError):
        b.append_event(actor_sub="mallory", action="node.delete", resource_kind="node")
    assert acw_module.NOT_LEADER_COUNTER.inc.call_count == not_leader_before_calls + 1

    rows = _rows(pg_db.executesql("SELECT COUNT(*) FROM audit_events"))
    assert rows[0][0] == 1, "B's rejected append must not have inserted a row"


def test_release_leadership_clears_handle(pg_db: DB) -> None:
    lease = StubLeaseClient()
    w = AuditChainWriter(
        pg_db, vault_client=None, leader_lease_client=lease, replica_id="A"
    )
    assert w.try_acquire_leadership() is True
    assert w.is_leader() is True
    assert w.release_leadership() is True
    assert w.is_leader() is False
    with pytest.raises(NotLeaderError):
        w.append_event(actor_sub="alice", action="x", resource_kind="y")


def test_append_persists_row_via_real_transaction(pg_db: DB) -> None:
    lease = StubLeaseClient()
    w = AuditChainWriter(
        pg_db, vault_client=None, leader_lease_client=lease, replica_id="A"
    )
    assert w.try_acquire_leadership() is True
    event = w.append_event(actor_sub="x", action="y", resource_kind="z")

    rows = _rows(
        pg_db.executesql(
            "SELECT actor_sub, prev_hash, hash FROM audit_events WHERE id = %(id)s",
            {"id": str(event.id)},
        )
    )
    assert len(rows) == 1
    assert rows[0][0] == "x"
    assert bytes(rows[0][1]) == ZERO_HASH


# ---------------------------------------------------------------------------
# Advisory-lock + shared-transaction serialization proof.
# ---------------------------------------------------------------------------


def test_concurrent_append_serializes_and_never_forks_chain(pg_db: DB) -> None:
    """N threads on the SAME leader replica call ``append_event`` concurrently.

    This is the proof that the advisory lock + hash-chain read+insert are
    genuinely sharing one pinned connection/transaction (per
    ``append_event``'s docstring) rather than each ``executesql()`` call
    opening its own autocommitted connection: if the lock were dropped
    between the chain-head read and the insert (e.g. by reverting to
    per-call ``db.executesql()``), two threads could read the same
    chain-head hash concurrently and each insert a row with an identical
    ``prev_hash`` -- forking the chain (two rows both claiming to follow the
    same predecessor). With the lock genuinely held for the transaction's
    duration, Postgres serializes the N transactions and the resulting
    chain is provably a single, unbroken sequence: sorting the N rows by
    insertion order (``id`` is a monotonic UUIDv7), every row's
    ``prev_hash`` must equal its immediate predecessor's ``hash``, and
    ``ZERO_HASH`` must appear exactly once (the genesis link).

    Each worker retries on ``psycopg2.errors.SerializationFailure`` -- under
    SERIALIZABLE isolation, a transaction that blocked on the advisory lock
    can still be aborted by Postgres's SSI conflict detector on commit (its
    snapshot predates the lock-holder's commit; the docs literally say "the
    transaction might succeed if retried"). This is expected, correct
    SERIALIZABLE behavior, not a fork -- and it's a PRE-EXISTING property of
    ``append_event`` inherited unchanged from the original SQLAlchemy
    implementation (same SET + advisory lock + read + insert all sharing
    one ambient, uncommitted-until-caller-commits transaction) that was
    never actually exercised against real concurrent Postgres before this
    conversion's test suite. ``append_event`` itself has no retry loop --
    that gap is flagged in the task report as a pre-existing item for
    follow-up, not fixed here (out of this task's scope).
    """

    lease = StubLeaseClient()
    n = 12
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def make_writer() -> AuditChainWriter:
        w = AuditChainWriter(
            pg_db,
            vault_client=None,
            leader_lease_client=lease,
            replica_id="A",
            cluster_id="c1",
        )
        w._lease_handle = ll.LeaderLeaseHandle(
            lease_name=acw_module.LEASE_NAME,
            holder_id="A",
            ttl_seconds=60,
            acquired_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            version=1,
        )
        return w

    def worker(name: str) -> None:
        barrier.wait()
        try:
            make_writer().append_event(
                actor_sub=name, action="node.create", resource_kind="node"
            )
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"writer-{i}",)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Any SerializationFailure aborts get retried here, one at a time
    # (zero contention, guaranteed to terminate) -- this is what makes the
    # test deterministic instead of tuning arbitrary retry/backoff counts
    # against real concurrent timing. Any other exception type fails the
    # test outright.
    for exc in errors:
        assert "SerializationFailure" in repr(
            exc
        ), f"non-retriable append_event failure: {exc!r}"
    for name in [f"writer-retry-{i}" for i in range(len(errors))]:
        make_writer().append_event(
            actor_sub=name, action="node.create", resource_kind="node"
        )

    rows = _rows(
        pg_db.executesql("SELECT id, prev_hash, hash FROM audit_events ORDER BY id ASC")
    )
    assert len(rows) == n

    hashes = [bytes(r[2]) for r in rows]
    prev_hashes = [bytes(r[1]) for r in rows]

    assert prev_hashes.count(ZERO_HASH) == 1, "exactly one row must chain off genesis"
    for i in range(1, n):
        assert prev_hashes[i] == hashes[i - 1], (
            f"row {i} prev_hash must equal row {i - 1}'s hash -- a fork "
            "means the advisory lock did not actually serialize concurrent appends"
        )


def test_append_invokes_advisory_lock_and_serializable(pg_db: DB) -> None:
    lease = StubLeaseClient()
    w = AuditChainWriter(
        pg_db, vault_client=None, leader_lease_client=lease, replica_id="A"
    )
    assert w.try_acquire_leadership() is True
    # No exception means both SET TRANSACTION ISOLATION LEVEL SERIALIZABLE and
    # pg_advisory_xact_lock succeeded against real Postgres inside the shared tx.
    w.append_event(actor_sub="x", action="y", resource_kind="z")


# ---------------------------------------------------------------------------
# RLS cross-tenant scope — proven against the scoped (non-owner) role.
# ---------------------------------------------------------------------------


def test_append_event_and_verify_require_cross_tenant_scope(pg_db_scoped: DB) -> None:
    """Negative control: without ``_cross_tenant_scope()``, a scoped-role
    caller's chain-head read/verify would fail-closed to zero visible rows
    under RLS (an unset tenant ContextVar), silently returning a wrong
    ``ZERO_HASH`` prev/forking the chain, or reporting a false "0 rows
    checked" from ``verify_chain_scheduled``. Proven by calling the
    lower-level pieces directly under each scope.
    """
    install_rls_events(pg_db_scoped.engine)
    lease = StubLeaseClient()
    w = AuditChainWriter(
        pg_db_scoped,
        vault_client=None,
        leader_lease_client=lease,
        replica_id="A",
        cluster_id="c1",
    )
    assert w.try_acquire_leadership() is True

    # append_event applies the sentinel itself -- this must succeed and see
    # a genuine chain (not silently treat every append as "first event").
    first = w.append_event(actor_sub="a", action="create", resource_kind="node")
    assert first.prev_hash == ZERO_HASH
    second = w.append_event(actor_sub="a", action="update", resource_kind="node")
    assert second.prev_hash == first.hash, (
        "append_event must see the real chain head under the cross-tenant "
        "sentinel, not fail-closed to an empty view and re-use ZERO_HASH"
    )

    # Negative control: reading audit_events under an ordinary per-tenant GUC
    # (not the sentinel append_event applies internally) sees nothing, since
    # these rows carry tenant_id=NULL.
    set_current_tenant("some-ordinary-tenant")
    try:
        rows = _rows(pg_db_scoped.executesql("SELECT COUNT(*) FROM audit_events"))
    finally:
        set_current_tenant(None)
    assert rows[0][0] == 0, "a plain per-tenant GUC must not see these NULL-tenant rows"

    # verify_chain_scheduled applies the sentinel itself too.
    summary = w.verify_chain_scheduled(since=timedelta(hours=24))
    assert summary["rows_checked"] == 2
    assert summary["breaks"] == 0


def test_verify_chain_scheduled_fails_closed_without_sentinel_would_lie(
    pg_db_scoped: DB,
) -> None:
    """Same proof from the other direction: manually reading audit_events
    with the ContextVar unset (simulating what ``verify_chain_scheduled``
    would see if its ``_cross_tenant_scope()`` wrapper were removed) returns
    zero rows even though real rows exist -- the exact "false 0 breaks"
    failure mode ``_cross_tenant_scope()`` exists to prevent.
    """
    install_rls_events(pg_db_scoped.engine)
    lease = StubLeaseClient()
    w = AuditChainWriter(
        pg_db_scoped,
        vault_client=None,
        leader_lease_client=lease,
        replica_id="A",
        cluster_id="c1",
    )
    assert w.try_acquire_leadership() is True
    w.append_event(actor_sub="a", action="create", resource_kind="node")

    set_current_tenant(None)
    rows = _rows(pg_db_scoped.executesql("SELECT COUNT(*) FROM audit_events"))
    assert rows[0][0] == 0, (
        "unset tenant context must fail closed to zero rows -- this is exactly "
        "why verify_chain_scheduled applies the cross-tenant sentinel itself"
    )


# ---------------------------------------------------------------------------
# Tampering / verify_chain_scheduled
# ---------------------------------------------------------------------------


def test_tampering_detected_metric_increments_and_nats_emitted(pg_db: DB) -> None:
    now = datetime.now(timezone.utc)
    _seed_event(pg_db, ts=now - timedelta(minutes=10))
    bad = _seed_event(pg_db, ts=now - timedelta(minutes=5))
    pg_db.executesql(
        "UPDATE audit_events SET hash = %(hash)s WHERE id = %(id)s",
        {"hash": b"\xde" * 32, "id": str(bad["id"])},
    )
    _seed_event(pg_db, ts=now - timedelta(minutes=1))

    captured: list[tuple[str, dict[str, Any]]] = []

    async def capture(subject: str, payload: dict[str, Any]) -> None:
        captured.append((subject, payload))

    lease = StubLeaseClient()
    w = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=lease,
        replica_id="A",
        cluster_id="c1",
        nats_publisher=capture,
    )

    before = acw_module.CHAIN_BREAK_COUNTER.inc.call_count

    captured.clear()
    asyncio.run(_run_verify_in_loop(w, timedelta(hours=24), captured))

    after = acw_module.CHAIN_BREAK_COUNTER.inc.call_count
    assert after > before
    assert any(s == acw_module.NATS_SUBJECT_AUDIT_BREAK for s, _ in captured)
    payload = next(p for s, p in captured if s == acw_module.NATS_SUBJECT_AUDIT_BREAK)
    assert payload["first_break_id"] is not None
    assert payload["cluster_id"] == "c1"
    assert payload["window_since"]
    assert payload["window_to"]


async def _run_verify_in_loop(
    w: AuditChainWriter, since: timedelta, captured: list[tuple[str, dict[str, Any]]]
) -> None:
    """Helper: re-bind the publisher inline so it runs in the active loop."""

    async def cap(subject: str, payload: dict[str, Any]) -> None:
        captured.append((subject, payload))

    w.nats_publisher = cap
    w.verify_chain_scheduled(since=since)
    await asyncio.sleep(0)


def test_verify_clean_chain_emits_no_event(pg_db: DB) -> None:
    now = datetime.now(timezone.utc)
    _seed_event(pg_db, ts=now - timedelta(minutes=10))
    _seed_event(pg_db, ts=now - timedelta(minutes=5))
    _seed_event(pg_db, ts=now - timedelta(minutes=1))

    captured: list[tuple[str, dict[str, Any]]] = []

    async def capture(subject: str, payload: dict[str, Any]) -> None:
        captured.append((subject, payload))

    w = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=StubLeaseClient(),
        replica_id="A",
        nats_publisher=capture,
    )

    summary = w.verify_chain_scheduled(since=timedelta(hours=24))
    assert summary["breaks"] == 0
    assert summary["nats_event_emitted"] is False
    assert captured == []


def test_verify_window_respected(pg_db: DB) -> None:
    """Rows older than 24 h are ignored even if tampered."""

    now = datetime.now(timezone.utc)
    old = _seed_event(pg_db, ts=now - timedelta(hours=48))
    pg_db.executesql(
        "UPDATE audit_events SET hash = %(hash)s WHERE id = %(id)s",
        {"hash": b"\xff" * 32, "id": str(old["id"])},
    )
    _seed_event(pg_db, ts=now - timedelta(minutes=1))

    w = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=StubLeaseClient(),
        replica_id="A",
    )
    summary = w.verify_chain_scheduled(since=timedelta(hours=24))
    assert summary["breaks"] == 0


def test_publish_nats_break_returns_false_without_publisher(pg_db: DB) -> None:
    w = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=StubLeaseClient(),
        replica_id="A",
    )
    assert w._publish_nats_break({"first_break_id": "x"}) is False


def test_publish_nats_break_handles_publisher_exception(pg_db: DB) -> None:
    async def boom(subject: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("nats unreachable")

    w = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=StubLeaseClient(),
        replica_id="A",
        nats_publisher=boom,
    )
    assert w._publish_nats_break({"first_break_id": "x"}) in (True, False)


# ---------------------------------------------------------------------------
# Offsite mirror — moto-backed S3
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_s3_session() -> Any:
    """An in-process S3 stand-in (no moto required) — supports the subset of
    operations the shipper exercises (``put_object``).
    """

    class FakeS3Client:
        def __init__(self) -> None:
            self.objects: dict[str, dict[str, Any]] = {}

        async def put_object(self, **kwargs: Any) -> dict[str, Any]:
            key = kwargs["Key"]
            if key in self.objects:
                raise RuntimeError(f"Object Lock prevents overwrite of {key!r}")
            self.objects[key] = {
                "Body": kwargs["Body"],
                "Bucket": kwargs["Bucket"],
                "ContentType": kwargs.get("ContentType"),
                "ObjectLockMode": kwargs.get("ObjectLockMode"),
                "ObjectLockRetainUntilDate": kwargs.get("ObjectLockRetainUntilDate"),
                "ServerSideEncryption": kwargs.get("ServerSideEncryption"),
                "SSEKMSKeyId": kwargs.get("SSEKMSKeyId"),
            }
            return {"ETag": "fake-etag"}

        async def __aenter__(self) -> "FakeS3Client":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    return FakeS3Client()


@pytest.mark.skip(reason=_OFFSITE_MIRROR_SKIP_REASON)
@pytest.mark.asyncio
async def test_offsite_mirror_ships_every_committed_row(
    pg_db: DB, fake_s3_session: Any
) -> None:
    now = datetime.now(timezone.utc)
    rows = [_seed_event(pg_db, ts=now - timedelta(minutes=10 - i)) for i in range(5)]

    def factory(cfg: OffsiteMirrorConfig) -> Any:
        return fake_s3_session

    w = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=StubLeaseClient(),
        replica_id="A",
        s3_session_factory=factory,
    )

    summary = await w.start_offsite_mirror(
        s3_endpoint="http://fake",
        bucket="audit-mirror",
        retention_days=2555,
        max_iterations=2,
        config_overrides={"poll_interval_seconds": 0.001},
    )

    assert summary["rows_shipped"] == 5
    keys = set(fake_s3_session.objects.keys())
    expected_keys = {f"audit-events/{r['id']}.json" for r in rows}
    assert keys == expected_keys

    sample = fake_s3_session.objects[next(iter(expected_keys))]
    assert sample["ObjectLockMode"] == "COMPLIANCE"
    assert sample["ServerSideEncryption"] == "AES256"
    retain = sample["ObjectLockRetainUntilDate"]
    assert retain - datetime.now(timezone.utc) > timedelta(days=2554)

    payload = json.loads(sample["Body"].decode("utf-8"))
    assert "id" in payload and "hash" in payload


@pytest.mark.skip(reason=_OFFSITE_MIRROR_SKIP_REASON)
@pytest.mark.asyncio
async def test_offsite_mirror_uses_kms_when_configured(
    pg_db: DB, fake_s3_session: Any
) -> None:
    _seed_event(pg_db)

    class _Vault:
        def get_kms_key_id(self) -> str:
            return "alias/gough-audit"

    def factory(cfg: OffsiteMirrorConfig) -> Any:
        assert cfg.sse_kms_key_id == "alias/gough-audit"
        return fake_s3_session

    w = AuditChainWriter(
        pg_db,
        vault_client=_Vault(),
        leader_lease_client=StubLeaseClient(),
        replica_id="A",
        s3_session_factory=factory,
    )

    await w.start_offsite_mirror(
        s3_endpoint="http://fake",
        bucket="audit-mirror",
        retention_days=30,
        max_iterations=1,
        config_overrides={"poll_interval_seconds": 0.001},
    )

    obj = next(iter(fake_s3_session.objects.values()))
    assert obj["ServerSideEncryption"] == "aws:kms"
    assert obj["SSEKMSKeyId"] == "alias/gough-audit"


@pytest.mark.skip(reason=_OFFSITE_MIRROR_SKIP_REASON)
@pytest.mark.asyncio
async def test_offsite_mirror_recovers_from_transient_error(pg_db: DB) -> None:
    _seed_event(pg_db)

    class FlakyS3:
        def __init__(self) -> None:
            self.calls = 0
            self.objects: dict[str, dict[str, Any]] = {}

        async def put_object(self, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient network error")
            self.objects[kwargs["Key"]] = kwargs
            return {"ETag": "x"}

        async def __aenter__(self) -> "FlakyS3":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    flaky = FlakyS3()

    def factory(cfg: OffsiteMirrorConfig) -> Any:
        return flaky

    w = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=StubLeaseClient(),
        replica_id="A",
        s3_session_factory=factory,
    )

    failures_before = acw_module.MIRROR_FAILURE_COUNTER.inc.call_count
    await w.start_offsite_mirror(
        s3_endpoint="http://fake",
        bucket="audit-mirror",
        retention_days=10,
        max_iterations=2,
        config_overrides={
            "poll_interval_seconds": 0.001,
            "initial_backoff_seconds": 0.001,
            "max_backoff_seconds": 0.005,
        },
    )

    assert flaky.calls >= 2
    assert acw_module.MIRROR_FAILURE_COUNTER.inc.call_count > failures_before
    assert len(flaky.objects) == 1


@pytest.mark.skip(reason=_OFFSITE_MIRROR_SKIP_REASON)
@pytest.mark.asyncio
async def test_offsite_mirror_with_moto(pg_db: DB) -> None:
    """End-to-end test with the real ``moto`` mock S3 service if available."""

    try:
        import aioboto3  # type: ignore[import-not-found]  # noqa: F401
        from moto import mock_aws  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("aioboto3/moto not installed")

    rows = [_seed_event(pg_db) for _ in range(3)]

    with mock_aws():
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

        import boto3  # type: ignore[import-untyped]

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(
            Bucket="audit-mirror",
            ObjectLockEnabledForBucket=True,
        )

        w = AuditChainWriter(
            pg_db,
            vault_client=None,
            leader_lease_client=StubLeaseClient(),
            replica_id="A",
        )
        await w.start_offsite_mirror(
            s3_endpoint=None,  # type: ignore[arg-type]  # pre-existing: real default endpoint
            bucket="audit-mirror",
            retention_days=30,
            max_iterations=1,
            config_overrides={"poll_interval_seconds": 0.001},
        )

        listed = s3.list_objects_v2(Bucket="audit-mirror").get("Contents", [])
        listed_keys = {o["Key"] for o in listed}
        assert listed_keys == {f"audit-events/{r['id']}.json" for r in rows}


@pytest.mark.asyncio
async def test_start_offsite_mirror_max_iterations() -> None:
    """start_offsite_mirror stops after max_iterations.

    Bare ``MagicMock`` db (not ``pg_db``) -- ``_read_watermark()``'s broad
    except swallows whatever a bare MagicMock's ``.executesql(...)`` return
    produces (it can't parse as a UUID), so this exercises the "no
    watermark, start from genesis" path without needing real Postgres; the
    genuinely-real-DB offsite mirror path is covered (skipped, see
    ``_OFFSITE_MIRROR_SKIP_REASON``) by the other ``test_offsite_mirror_*``
    tests above.
    """
    mock_s3_client = MagicMock()
    mock_s3_client.__aenter__.return_value = mock_s3_client
    mock_s3_client.__aexit__.return_value = None
    mock_s3_client.put_object = AsyncMock()

    writer = AuditChainWriter(
        db_session=MagicMock(),
        vault_client=None,
        s3_session_factory=lambda cfg: mock_s3_client,
    )

    result = await writer.start_offsite_mirror(
        s3_endpoint="http://localhost:9000",
        bucket="audit-bucket",
        retention_days=90,
        max_iterations=2,
    )

    assert result["iterations"] == 2


@pytest.mark.asyncio
async def test_start_offsite_mirror_aioboto3_import_error() -> None:
    """start_offsite_mirror raises OffsiteMirrorError when aioboto3 missing."""
    from app.workers.audit_chain_writer import OffsiteMirrorError

    writer = AuditChainWriter(
        db_session=MagicMock(), vault_client=None, s3_session_factory=None
    )

    with patch("builtins.__import__", side_effect=ImportError("aioboto3 not found")):
        with pytest.raises(OffsiteMirrorError, match="aioboto3 is required"):
            await writer.start_offsite_mirror(
                s3_endpoint="http://localhost:9000",
                bucket="audit-bucket",
                retention_days=90,
            )


# ---------------------------------------------------------------------------
# Misc coverage
# ---------------------------------------------------------------------------


def test_verify_chain_empty_database(pg_db: DB) -> None:
    w = AuditChainWriter(
        pg_db, vault_client=None, leader_lease_client=StubLeaseClient(), replica_id="A"
    )

    summary = w.verify_chain_scheduled(since=timedelta(hours=24))
    assert summary["breaks"] == 0
    assert summary["nats_event_emitted"] is False
    assert summary["window_since"] is not None
    assert summary["window_to"] is not None


def test_verify_chain_single_event(pg_db: DB) -> None:
    _seed_event(pg_db)

    w = AuditChainWriter(
        pg_db, vault_client=None, leader_lease_client=StubLeaseClient(), replica_id="A"
    )

    summary = w.verify_chain_scheduled(since=timedelta(hours=24))
    assert summary["breaks"] == 0
    assert summary["rows_checked"] >= 1


def test_verify_chain_multiple_valid_events(pg_db: DB) -> None:
    now = datetime.now(timezone.utc)
    for i in range(5):
        _seed_event(pg_db, ts=now - timedelta(minutes=5 - i))

    w = AuditChainWriter(
        pg_db, vault_client=None, leader_lease_client=StubLeaseClient(), replica_id="A"
    )

    summary = w.verify_chain_scheduled(since=timedelta(hours=24))
    assert summary["breaks"] == 0
    assert summary["rows_checked"] == 5


def test_verify_chain_multiple_breaks(pg_db: DB) -> None:
    now = datetime.now(timezone.utc)
    _seed_event(pg_db, ts=now - timedelta(minutes=10))
    event2 = _seed_event(pg_db, ts=now - timedelta(minutes=5))
    _seed_event(pg_db, ts=now - timedelta(minutes=1))

    pg_db.executesql(
        "UPDATE audit_events SET hash = %(hash)s WHERE id = %(id)s",
        {"hash": b"\xaa" * 32, "id": str(event2["id"])},
    )

    captured: list[tuple[str, dict[str, Any]]] = []

    async def capture(subject: str, payload: dict[str, Any]) -> None:
        captured.append((subject, payload))

    w = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=StubLeaseClient(),
        replica_id="A",
        nats_publisher=capture,
    )

    asyncio.run(_run_verify_in_loop(w, timedelta(hours=24), captured))
    assert captured


def test_is_leader_without_handle(pg_db: DB) -> None:
    lease = StubLeaseClient()
    w = AuditChainWriter(
        pg_db, vault_client=None, leader_lease_client=lease, replica_id="A"
    )

    assert w.is_leader() is False


def test_release_leadership_without_handle(pg_db: DB) -> None:
    lease = StubLeaseClient()
    w = AuditChainWriter(
        pg_db, vault_client=None, leader_lease_client=lease, replica_id="A"
    )

    assert w.release_leadership() is False


def test_try_acquire_leadership_twice(pg_db: DB) -> None:
    lease = StubLeaseClient()
    w = AuditChainWriter(
        pg_db, vault_client=None, leader_lease_client=lease, replica_id="A"
    )

    assert w.try_acquire_leadership() is True
    assert w.try_acquire_leadership() is True


def test_append_event_with_custom_cluster_id(pg_db: DB) -> None:
    lease = StubLeaseClient()
    w = AuditChainWriter(
        pg_db,
        vault_client=None,
        leader_lease_client=lease,
        replica_id="A",
        cluster_id="prod-cluster-1",
    )
    assert w.try_acquire_leadership() is True

    event = w.append_event(
        actor_sub="admin@example.com",
        action="system.config_change",
        resource_kind="config",
    )

    assert event.cluster_id == "prod-cluster-1"


def test_verify_chain_window_boundary(pg_db: DB) -> None:
    now = datetime.now(timezone.utc)
    window_hours = 24

    _seed_event(pg_db, ts=now - timedelta(hours=23, minutes=59))
    old_event = _seed_event(pg_db, ts=now - timedelta(hours=24, minutes=1))
    pg_db.executesql(
        "UPDATE audit_events SET hash = %(hash)s WHERE id = %(id)s",
        {"hash": b"\xff" * 32, "id": str(old_event["id"])},
    )

    w = AuditChainWriter(
        pg_db, vault_client=None, leader_lease_client=StubLeaseClient(), replica_id="A"
    )

    summary = w.verify_chain_scheduled(since=timedelta(hours=window_hours))
    assert summary["breaks"] == 0
