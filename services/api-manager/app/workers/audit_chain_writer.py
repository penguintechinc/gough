"""Single-leader audit-event writer + offsite mirror shipper + integrity verifier.

Implements the spec sections "Audit & Compliance" + "Audit Log — Hash-Chain Format"
+ the "Critical Files → audit_chain_writer.py" entry. Three responsibilities:

1. **Append events** under a leader lease (``audit_chain_writer``) so that only
   one replica writes — even under HA, the chain hash advances monotonically.
   A Postgres advisory lock additionally prevents split-write within a single
   leader (e.g., two threads on the leader replica trying to append concurrently).
   Every append runs in one ``db.transaction()`` (SERIALIZABLE isolation +
   ``pg_advisory_xact_lock`` + the hash-chain read+insert all share the same
   pinned connection, so the lock is actually held for the read+insert's
   duration and is auto-released at commit — see ``append_event`` docstring).

2. **Offsite mirror** — a long-running async daemon that ships every committed
   row to an S3-compatible Object-Lock bucket using ``aioboto3``. The shipper
   tracks a sequence-id watermark (the last shipped ``id``), reconnects with
   exponential backoff, and exposes lag in ``gough_audit_mirror_lag_seconds``.

3. **Scheduled verify** — invoked daily by supercronic; runs ``verify_chain``
   over the last 24 h, increments ``gough_audit_chain_break_total`` on breaks,
   and emits a NATS ``gough.security.audit_chain_break`` event with the first
   break id.

Public API:

    AuditChainWriter(db_session, vault_client, leader_lease_client)
    AuditChainWriter.append_event(...)            # leader-only
    AuditChainWriter.start_offsite_mirror(...)    # async daemon
    AuditChainWriter.verify_chain_scheduled(...)  # cron entrypoint

Tests in ``tests/workers/test_audit_chain_writer.py`` (+ ``_extended.py``)
cover all four surfaces.

penguin-dal conversion notes (Task 8a):

* ``db_session`` (constructor param + attribute) is now a penguin-dal ``DB``
  instance, not a SQLAlchemy Session — name kept for continuity with
  ``AuditEventWriter``'s constructor keyword (see that module's docstring).
* ``append_event()``'s advisory-lock + SERIALIZABLE + hash-chain read+insert
  now run inside one ``with self.db_session.transaction() as tx:`` block, so
  they share a single pinned connection — a naive per-call ``executesql()``
  would open a new autocommitted connection per statement, releasing the
  advisory lock immediately and letting two concurrent leader-replica
  threads read the same chain-head hash and fork the chain.
* ``pg_advisory_xact_lock`` was already the transaction-scoped variant (no
  session-scoped ``pg_advisory_lock``/``pg_advisory_unlock`` to migrate) —
  it just needed to move inside the shared transaction to actually work.
  MariaDB has no equivalent function; the call is wrapped in the same
  broad try/except the original code used (now catching "function does not
  exist" instead of "not supported on this dialect"), so a MariaDB target
  degrades to "don't crash, no advisory lock" rather than failing the
  append — see ``append_event`` docstring for the residual gap this leaves.
* ``audit_events`` is RLS-protected and this worker runs with no
  request/tenant context, so every DB unit that reads/writes it here
  (``append_event``, the offsite mirror's ``_fetch_rows_after``/
  ``_update_lag_gauge``, ``verify_chain_scheduled``) is wrapped in
  ``app.db.rls``'s cross-tenant sentinel via the local ``_cross_tenant_scope()``
  helper below (same pattern as ``app.grpc_server._cross_tenant_scope()``).
  Omitting this would silently fail-closed to zero visible rows — a missing
  chain head (fork risk), a "0 rows shipped" mirror, or a false "0 breaks"
  compliance verify — not raise an error.
* ``audit_events_mirror_state`` (offsite mirror watermark) carries no RLS
  policy and isn't in the baseline migration's ``rls_tables`` list, so no
  tenant scoping is needed for ``_read_watermark``/``_persist_watermark``.
  It's also not a table the baseline migration actually creates (a
  pre-existing schema gap, same class as the ~12 orphaned tables tracked
  in GitHub issue #21) — the original code already handled that
  defensively (broad try/except, "watermark table missing" fallback),
  preserved as-is here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, cast

from prometheus_client import Counter, Gauge

from app.db.rls import CROSS_TENANT_SENTINEL, get_current_tenant, set_current_tenant
from app.security.audit_chain import (
    AuditEvent,
    AuditEventWriter,
    canonicalize_record,
    verify_chain,
)
from app.workers import leader_lease as leader_lease_module
from app.workers.leader_lease import LeaderLeaseHandle

logger = logging.getLogger(__name__)


LEASE_NAME = "audit_chain_writer"
ADVISORY_LOCK_NAMESPACE = 0xA00D17  # arbitrary 24-bit constant; "AUDIT" mangled
NATS_SUBJECT_AUDIT_BREAK = "gough.security.audit_chain_break"


@contextmanager
def _cross_tenant_scope() -> Iterator[None]:
    """Push the RLS cross-tenant sentinel for one background DB unit.

    This worker never runs inside Quart's HTTP request pipeline, so
    ``app.db.rls``'s tenant ContextVar is never set by anything upstream —
    without this, every RLS-protected read/write against ``audit_events``
    here would fail-closed to zero rows (see module docstring). Mirrors
    ``app.grpc_server._cross_tenant_scope()`` exactly; duplicated locally
    rather than imported, matching this codebase's existing convention of
    defining this helper per-module (``app.grpc_server``,
    ``app.workers.joiner_secret_emitter``) instead of a shared import.
    """
    previous = get_current_tenant()
    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        yield
    finally:
        set_current_tenant(previous)


# -----------------------------------------------------------------------------
# Metrics — module-level singletons (Prometheus client de-duplicates by name).
# -----------------------------------------------------------------------------


def _metric(
    factory: Any, name: str, doc: str, labels: Optional[list[str]] = None
) -> Any:
    """Return existing collector if registered, else create a new one.

    Prometheus client raises ``ValueError`` on duplicate registration; our worker
    can be imported from multiple call sites (tests, app, scheduled job) so we
    coalesce.
    """

    try:
        if labels is not None:
            return factory(name, doc, labels)
        return factory(name, doc)
    except ValueError:
        # Already registered — fish it out of the default registry.
        from prometheus_client import REGISTRY

        for collector in REGISTRY._collector_to_names:
            for n in REGISTRY._collector_to_names.get(collector, ()):
                if n == name:
                    return collector
        raise


CHAIN_BREAK_COUNTER = _metric(
    Counter,
    "gough_audit_chain_break_total",
    "Number of audit-chain hash breaks observed by verify_chain_scheduled.",
)
MIRROR_LAG_GAUGE = _metric(
    Gauge,
    "gough_audit_mirror_lag_seconds",
    "Seconds between the latest committed audit_events row and the latest "
    "row shipped to the offsite Object-Lock bucket.",
)
MIRROR_SHIPPED_COUNTER = _metric(
    Counter,
    "gough_audit_mirror_shipped_total",
    "audit_events rows successfully shipped to the offsite bucket.",
)
MIRROR_FAILURE_COUNTER = _metric(
    Counter,
    "gough_audit_mirror_failures_total",
    "audit_events rows that failed to ship (transient or permanent).",
)
APPEND_COUNTER = _metric(
    Counter,
    "gough_audit_append_total",
    "audit_events rows appended by AuditChainWriter (this replica, leader-only).",
)
NOT_LEADER_COUNTER = _metric(
    Counter,
    "gough_audit_append_not_leader_total",
    "audit_events append attempts rejected because we are not the chain leader.",
)


# -----------------------------------------------------------------------------
# Errors
# -----------------------------------------------------------------------------


class AuditChainError(RuntimeError):
    """Base class."""


class NotLeaderError(AuditChainError):
    """Raised when an append is attempted on a non-leader replica."""


class OffsiteMirrorError(AuditChainError):
    """Raised when the shipper cannot make forward progress permanently."""


# -----------------------------------------------------------------------------
# Mirror configuration
# -----------------------------------------------------------------------------


@dataclass
class OffsiteMirrorConfig:
    """Static configuration for the offsite mirror daemon."""

    s3_endpoint: str
    bucket: str
    retention_days: int
    region: str = "us-east-1"
    object_prefix: str = "audit-events/"
    sse_kms_key_id: Optional[str] = None
    poll_interval_seconds: float = 1.0
    max_batch_size: int = 500
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    object_lock_mode: str = "COMPLIANCE"

    def object_key(self, row_id: uuid.UUID) -> str:
        return f"{self.object_prefix}{row_id}.json"


# -----------------------------------------------------------------------------
# AuditChainWriter
# -----------------------------------------------------------------------------


class AuditChainWriter:
    """Single-leader audit log writer.

    The constructor takes three injected dependencies:

    * ``db_session`` — a penguin-dal ``DB`` instance used for advisory-lock +
      append operations (see module docstring for why the parameter keeps
      this legacy name). Tests pass a real ``pg_db``/``pg_db_scoped``
      fixture instance.
    * ``vault_client`` — a ``VaultClient`` used to source SSE-KMS key IDs / signing
      callbacks. ``None`` is permitted for clusters that disable transit signing.
    * ``leader_lease_client`` — the module/object exposing ``acquire`` / ``release``
      / ``renew`` / ``is_leader``. Defaults to :mod:`app.workers.leader_lease`.
      Tests pass a stub to simulate non-leader replicas.
    """

    def __init__(
        self,
        db_session: Any,
        vault_client: Any,
        leader_lease_client: Any = None,
        *,
        cluster_id: Optional[str] = None,
        replica_id: Optional[str] = None,
        signer: Optional[Callable[[bytes], bytes]] = None,
        lease_ttl_seconds: int = leader_lease_module.DEFAULT_TTL_SECONDS,
        nats_publisher: Optional[
            Callable[[str, dict[str, Any]], Awaitable[None]]
        ] = None,
        s3_session_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.db_session = db_session
        self.vault_client = vault_client
        self.leader_lease_client = leader_lease_client or leader_lease_module
        self.cluster_id: str = (
            cluster_id
            if cluster_id is not None
            else os.getenv("GOUGH_CLUSTER_ID", "gough-cluster")
        )
        self.replica_id = replica_id or leader_lease_module._default_holder_id()
        self.signer = signer
        self.lease_ttl_seconds = lease_ttl_seconds
        self.nats_publisher = nats_publisher
        self._lease_handle: Optional[LeaderLeaseHandle] = None
        self._s3_session_factory = s3_session_factory
        self._writer = AuditEventWriter(
            db_session=db_session, cluster_id=self.cluster_id, signer=signer
        )

    # ------------------------------------------------------------------
    # Leader-lease management
    # ------------------------------------------------------------------

    def try_acquire_leadership(self) -> bool:
        """Attempt to claim the ``audit_chain_writer`` lease. Returns leader status."""

        handle = self.leader_lease_client.acquire(
            self.db_session,
            LEASE_NAME,
            ttl_seconds=self.lease_ttl_seconds,
            holder_id=self.replica_id,
        )
        if handle is None:
            self._lease_handle = None
            return False
        self._lease_handle = handle
        return True

    def release_leadership(self) -> bool:
        """Release the lease if we hold it."""

        if self._lease_handle is None:
            return False
        ok = self.leader_lease_client.release(self.db_session, self._lease_handle)
        self._lease_handle = None
        return bool(ok)

    def is_leader(self) -> bool:
        """Whether this replica currently holds the lease (cheap, in-memory check)."""

        return self._lease_handle is not None

    # ------------------------------------------------------------------
    # Append (leader-only)
    # ------------------------------------------------------------------

    def append_event(
        self,
        *,
        actor_sub: str,
        action: str,
        resource_kind: str,
        resource_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        actor_scope: Optional[list[str]] = None,
        before: Optional[dict[str, Any]] = None,
        after: Optional[dict[str, Any]] = None,
        request_id: Optional[str] = None,
        source_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> AuditEvent:
        """Append an audit event under leader lease + advisory lock + SERIALIZABLE.

        Raises :class:`NotLeaderError` when this replica is not the leader, with
        the metric ``gough_audit_append_not_leader_total`` incremented for
        observability.

        The SERIALIZABLE-isolation SET, the ``pg_advisory_xact_lock`` call,
        and the hash-chain read+insert (``AuditEventWriter.append``) all run
        on the SAME pinned connection inside one ``db_session.transaction()``
        block, committing together on clean exit. This is load-bearing: a
        transaction-scoped advisory lock only serialises callers for as long
        as it's held, and it auto-releases the instant its owning
        transaction commits — if the lock were taken in one autocommitted
        statement and the hash-chain read+insert ran as separate
        autocommitted statements afterward (each opening its own
        connection/transaction under the hood), the lock would already be
        released before the read even ran, and two threads on this leader
        replica could both read the same chain-head hash and fork the chain.

        MariaDB has no ``pg_advisory_xact_lock`` equivalent; the call is
        wrapped in a broad try/except (matching the pre-conversion
        behavior) so a MariaDB target logs and continues rather than
        failing the append. On MariaDB this leaves the "two threads on one
        leader replica" split-write case unprotected (the leader-lease CAS
        already guarantees only one REPLICA appends; this residual gap is
        specifically about multiple threads within that one replica) — the
        SERIALIZABLE isolation level SET is itself dialect-portable (ANSI
        SQL, supported by MySQL/MariaDB too) and still applies. Primary
        target is Postgres; see task report for the full analysis.
        """

        if not self.is_leader():
            # Re-check live state: another replica may have just released and we
            # could grab leadership. We do not auto-acquire here — callers must
            # explicitly own that lifecycle (typically via a periodic worker).
            handle = self._lease_handle
            if handle is None or not self.leader_lease_client.is_leader(
                self.db_session, handle
            ):
                NOT_LEADER_COUNTER.inc()
                raise NotLeaderError(
                    f"Replica {self.replica_id!r} is not the audit_chain_writer leader; "
                    "refusing append."
                )

        with _cross_tenant_scope(), self.db_session.transaction() as tx:
            # SERIALIZABLE isolation for the append; ignore failures on dialects
            # that don't expose this knob.
            try:
                tx.executesql("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "could not set SERIALIZABLE isolation: %s (continuing)", exc
                )

            # Postgres advisory lock — serialises across all connections holding
            # the leader lease (single replica, but multiple threads). Lock is
            # released automatically at transaction end (pg_advisory_xact_lock).
            try:
                tx.executesql(
                    "SELECT pg_advisory_xact_lock(%(ns)s, %(key)s)",
                    {"ns": ADVISORY_LOCK_NAMESPACE, "key": 1},
                )
            except Exception as exc:  # noqa: BLE001 — non-postgres dialects
                logger.debug("pg_advisory_xact_lock unavailable: %s", exc)

            event = self._writer.append(
                tx=tx,
                actor_sub=actor_sub,
                action=action,
                resource_kind=resource_kind,
                resource_id=resource_id,
                tenant_id=tenant_id,
                actor_scope=actor_scope,
                before=before,
                after=after,
                request_id=request_id,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        APPEND_COUNTER.inc()
        return event

    # ------------------------------------------------------------------
    # Offsite mirror — long-running async shipper
    # ------------------------------------------------------------------

    async def start_offsite_mirror(
        self,
        s3_endpoint: str,
        bucket: str,
        retention_days: int,
        *,
        region: str = "us-east-1",
        sse_kms_key_id: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        max_iterations: Optional[int] = None,
        stop_event: Optional[asyncio.Event] = None,
        config_overrides: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Run the mirror shipper. Coroutine — call from the api-manager startup.

        Returns a summary dict on graceful shutdown (``stop_event`` set or
        ``max_iterations`` reached). Tests use ``max_iterations`` to bound runs.

        Per spec: real-time logical replication (``pgoutput`` / ``wal2json``).
        For the M1 implementation we use a polling shipper — every committed row
        has a unique time-ordered UUIDv7 ``id`` and we ship rows whose id is
        > the watermark. This satisfies the same correctness contract (every
        committed row is mirrored exactly once with Object Lock) without
        requiring a logical-replication plugin in the api-manager process.

        ``ttl_seconds`` reconnect with exponential backoff is implemented by
        catching shipper failures and sleeping a backoff interval; the gauge
        ``gough_audit_mirror_lag_seconds`` updates on every iteration.
        """

        cfg = OffsiteMirrorConfig(
            s3_endpoint=s3_endpoint,
            bucket=bucket,
            retention_days=retention_days,
            region=region,
            sse_kms_key_id=sse_kms_key_id
            or (
                self.vault_client.get_kms_key_id()
                if self.vault_client and hasattr(self.vault_client, "get_kms_key_id")
                else None
            ),
        )
        if config_overrides:
            for k, v in config_overrides.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)

        stop_event = stop_event or asyncio.Event()
        watermark: Optional[uuid.UUID] = await asyncio.to_thread(self._read_watermark)
        iterations = 0
        backoff = cfg.initial_backoff_seconds
        rows_shipped = 0

        client_factory = self._build_s3_client_factory(
            cfg, access_key_id=access_key_id, secret_access_key=secret_access_key
        )

        while not stop_event.is_set():
            try:
                async with client_factory() as s3_client:
                    while not stop_event.is_set():
                        rows = await asyncio.to_thread(
                            self._fetch_rows_after, watermark, cfg.max_batch_size
                        )
                        if rows:
                            for row in rows:
                                await self._ship_row(s3_client, cfg, row)
                                watermark = row["id"]
                                rows_shipped += 1
                                MIRROR_SHIPPED_COUNTER.inc()
                            await asyncio.to_thread(self._update_lag_gauge, watermark)
                            backoff = cfg.initial_backoff_seconds
                        else:
                            await asyncio.to_thread(self._update_lag_gauge, watermark)

                        iterations += 1
                        if max_iterations is not None and iterations >= max_iterations:
                            stop_event.set()
                            break

                        try:
                            await asyncio.wait_for(
                                stop_event.wait(), timeout=cfg.poll_interval_seconds
                            )
                        except asyncio.TimeoutError:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                MIRROR_FAILURE_COUNTER.inc()
                logger.warning(
                    "audit offsite mirror error (sleeping %.1fs): %s", backoff, exc
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(cfg.max_backoff_seconds, backoff * 2 + random.random())

        return {
            "rows_shipped": rows_shipped,
            "iterations": iterations,
            "watermark": str(watermark) if watermark else None,
        }

    # ------------------------------------------------------------------
    # Scheduled verify — supercronic entrypoint
    # ------------------------------------------------------------------

    def verify_chain_scheduled(
        self,
        since: timedelta = timedelta(hours=24),
        *,
        emit_nats: bool = True,
    ) -> dict[str, Any]:
        """Run ``verify_chain`` over ``[now - since, now]``.

        On any break: increment ``gough_audit_chain_break_total`` (by the
        number of breaks observed) and emit a NATS event
        ``gough.security.audit_chain_break`` with payload
        ``{cluster_id, first_break_id, last_break_id, breaks, rows_checked,
        window_since, window_to}``. Returns the verify_chain summary plus
        ``nats_event_emitted: bool``.

        Runs under the cross-tenant RLS sentinel (see module docstring) —
        without it this would fail-closed to zero visible rows and silently
        report "0 rows_checked, 0 breaks" every day instead of actually
        verifying the chain.
        """

        now = datetime.now(timezone.utc)
        window_since = now - since
        with _cross_tenant_scope():
            summary = verify_chain(self.db_session, since=window_since, to=now)
        breaks = int(summary.get("breaks") or 0)

        nats_emitted = False
        if breaks > 0:
            CHAIN_BREAK_COUNTER.inc(breaks)
            payload = {
                "cluster_id": self.cluster_id,
                "first_break_id": (
                    str(summary.get("first_break_id"))
                    if summary.get("first_break_id")
                    else None
                ),
                "last_break_id": (
                    str(summary.get("last_break_id"))
                    if summary.get("last_break_id")
                    else None
                ),
                "breaks": breaks,
                "rows_checked": int(summary.get("rows_checked") or 0),
                "window_since": window_since.isoformat(),
                "window_to": now.isoformat(),
            }
            if emit_nats:
                nats_emitted = self._publish_nats_break(payload)

        return {
            **summary,
            "window_since": window_since.isoformat(),
            "window_to": now.isoformat(),
            "nats_event_emitted": nats_emitted,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_watermark(self) -> Optional[uuid.UUID]:
        """Read ``last_shipped_id`` from a sidecar key-value row.

        We persist the watermark in ``audit_events_mirror_state`` if present;
        otherwise we fall back to ``ZERO`` — a fresh shipper will replay every
        row (Object Lock makes this idempotent: same key + content_md5 => same
        object, write is a no-op). No RLS scoping needed — see module
        docstring.
        """

        try:
            rows = cast(
                "list[tuple[Any, ...]]",
                self.db_session.executesql(
                    "SELECT last_shipped_id FROM audit_events_mirror_state WHERE id = 1"
                ),
            )
            if rows and rows[0][0]:
                return uuid.UUID(str(rows[0][0]))
        except Exception as exc:  # noqa: BLE001
            logger.debug("watermark table missing: %s (starting from genesis)", exc)
        return None

    def _fetch_rows_after(
        self, watermark: Optional[uuid.UUID], batch_size: int
    ) -> list[dict[str, Any]]:
        """Fetch rows with id > watermark, ordered by id ASC.

        Cross-tenant scoped (see module docstring) — the mirror ships every
        tenant's rows, not just one.
        """

        if watermark is None:
            query = (
                "SELECT id, ts, cluster_id, tenant_id, actor_sub, actor_scope, "
                "action, resource_kind, resource_id, before_json, after_json, "
                "request_id, source_ip, user_agent, prev_hash, hash, signature "
                "FROM audit_events ORDER BY id ASC LIMIT %(limit)s"
            )
            params: dict[str, Any] = {"limit": batch_size}
        else:
            query = (
                "SELECT id, ts, cluster_id, tenant_id, actor_sub, actor_scope, "
                "action, resource_kind, resource_id, before_json, after_json, "
                "request_id, source_ip, user_agent, prev_hash, hash, signature "
                "FROM audit_events WHERE id > %(watermark)s ORDER BY id ASC LIMIT %(limit)s"
            )
            params = {"watermark": str(watermark), "limit": batch_size}

        with _cross_tenant_scope():
            rows = cast(
                "list[dict[str, Any]]",
                self.db_session.executesql(query, params, as_dict=True),
            )
        return rows

    def _row_to_object_payload(self, row: dict[str, Any]) -> bytes:
        """Serialise a row into JCS bytes for upload."""

        def _normalise(value: Any) -> Any:
            if isinstance(value, (bytes, bytearray, memoryview)):
                return bytes(value).hex()
            if isinstance(value, datetime):
                return value.astimezone(timezone.utc).isoformat()
            if isinstance(value, uuid.UUID):
                return str(value)
            return value

        normalised = {k: _normalise(v) for k, v in row.items()}
        return canonicalize_record(normalised)

    async def _ship_row(
        self,
        s3_client: Any,
        cfg: OffsiteMirrorConfig,
        row: dict[str, Any],
    ) -> None:
        body = self._row_to_object_payload(row)
        key = cfg.object_key(row["id"])
        retain_until = datetime.now(timezone.utc) + timedelta(days=cfg.retention_days)

        put_kwargs: dict[str, Any] = {
            "Bucket": cfg.bucket,
            "Key": key,
            "Body": body,
            "ContentType": "application/json",
            "ObjectLockMode": cfg.object_lock_mode,
            "ObjectLockRetainUntilDate": retain_until,
        }
        if cfg.sse_kms_key_id:
            put_kwargs["ServerSideEncryption"] = "aws:kms"
            put_kwargs["SSEKMSKeyId"] = cfg.sse_kms_key_id
        else:
            put_kwargs["ServerSideEncryption"] = "AES256"

        await s3_client.put_object(**put_kwargs)

        # Persist the new watermark synchronously — we want crash recovery to
        # resume from the most-recently-shipped row, not re-ship rows we already
        # uploaded (which would no-op against Object Lock but waste API calls).
        await asyncio.to_thread(self._persist_watermark, row["id"])

    def _persist_watermark(self, row_id: uuid.UUID) -> None:
        """No RLS scoping needed — ``audit_events_mirror_state`` carries no
        tenant_id/RLS policy (see module docstring)."""
        try:
            # Postgres UPSERT
            self.db_session.executesql(
                "INSERT INTO audit_events_mirror_state (id, last_shipped_id, "
                "updated_at) VALUES (1, %(rid)s, %(ts)s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "last_shipped_id = EXCLUDED.last_shipped_id, "
                "updated_at = EXCLUDED.updated_at",
                {"rid": str(row_id), "ts": datetime.now(timezone.utc)},
            )
        except Exception as exc:  # noqa: BLE001
            # Fallback for dialects without ON CONFLICT / a missing table — an
            # UPDATE that affects 0 rows is fine, the next iteration will retry.
            logger.debug("watermark UPSERT fallback: %s", exc)
            try:
                self.db_session.executesql(
                    "UPDATE audit_events_mirror_state SET last_shipped_id = %(rid)s, "
                    "updated_at = %(ts)s WHERE id = 1",
                    {"rid": str(row_id), "ts": datetime.now(timezone.utc)},
                )
            except Exception as exc2:  # noqa: BLE001
                logger.debug("watermark UPDATE fallback failed: %s", exc2)

    def _update_lag_gauge(self, watermark: Optional[uuid.UUID]) -> None:
        """Update the lag gauge based on the most recent committed row's ts.

        Cross-tenant scoped (see module docstring) — lag is measured against
        the cluster-wide latest row, not one tenant's.
        """

        try:
            with _cross_tenant_scope():
                latest_rows = cast(
                    "list[tuple[Any, ...]]",
                    self.db_session.executesql(
                        "SELECT ts FROM audit_events ORDER BY ts DESC LIMIT 1"
                    ),
                )
                if not latest_rows:
                    MIRROR_LAG_GAUGE.set(0.0)
                    return
                latest_ts: datetime = latest_rows[0][0]
                if latest_ts.tzinfo is None:
                    latest_ts = latest_ts.replace(tzinfo=timezone.utc)

                if watermark is None:
                    lag = (datetime.now(timezone.utc) - latest_ts).total_seconds()
                    MIRROR_LAG_GAUGE.set(max(lag, 0.0))
                    return

                shipped_rows = cast(
                    "list[tuple[Any, ...]]",
                    self.db_session.executesql(
                        "SELECT ts FROM audit_events WHERE id = %(id)s",
                        {"id": str(watermark)},
                    ),
                )
                if not shipped_rows:
                    MIRROR_LAG_GAUGE.set(
                        (datetime.now(timezone.utc) - latest_ts).total_seconds()
                    )
                    return
                shipped_ts: datetime = shipped_rows[0][0]
                if shipped_ts.tzinfo is None:
                    shipped_ts = shipped_ts.replace(tzinfo=timezone.utc)
                lag = (latest_ts - shipped_ts).total_seconds()
                MIRROR_LAG_GAUGE.set(max(lag, 0.0))
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not update lag gauge: %s", exc)

    def _build_s3_client_factory(
        self,
        cfg: OffsiteMirrorConfig,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
    ) -> Callable[[], Any]:
        """Return a zero-arg callable that yields an async-context S3 client.

        Tests inject ``s3_session_factory`` to supply a pre-built mock session
        (e.g. ``moto`` standing up a fake S3). Production resolves to
        ``aioboto3.Session().client(...)``.
        """

        if self._s3_session_factory is not None:
            override = self._s3_session_factory

            def _override_factory() -> Any:
                return override(cfg)

            return _override_factory

        # Lazy import — aioboto3 is only required when running the daemon
        try:
            import aioboto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — covered by integration
            raise OffsiteMirrorError(
                "aioboto3 is required to run start_offsite_mirror; install via requirements"
            ) from exc

        session = aioboto3.Session(
            aws_access_key_id=access_key_id or os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=secret_access_key
            or os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=cfg.region,
        )

        def _boto_factory() -> Any:
            return session.client("s3", endpoint_url=cfg.s3_endpoint)

        return _boto_factory

    def _publish_nats_break(self, payload: dict[str, Any]) -> bool:
        """Publish the audit_chain_break event. Returns True on emission."""

        if self.nats_publisher is None:
            logger.warning(
                "audit chain break detected but no nats_publisher configured: %s",
                payload,
            )
            return False

        try:
            coro = self.nats_publisher(NATS_SUBJECT_AUDIT_BREAK, payload)
            if asyncio.iscoroutine(coro):
                # Use existing running loop if available, else create fresh one.
                try:
                    asyncio.get_running_loop()
                    # Fire and forget — schedule on the running loop.
                    asyncio.ensure_future(coro)
                except RuntimeError:
                    # No running loop — create one and run to completion.
                    asyncio.run(coro)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("nats publish failed for audit_chain_break: %s", exc)
            return False
