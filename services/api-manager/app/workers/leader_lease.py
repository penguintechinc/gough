"""Generic 60s-TTL leader-lease primitive backed by the ``leader_leases`` table.

Used by every leader-only worker (audit chain writer, migration engine, capacity
predictor, compliance monitor, etc.). One lease row per ``lease_name``; the
holder renews periodically and releases (or expires) on shutdown / crash.

Per spec section "Leader-only workers" — non-leader instances idle-poll every
15 s to claim the lease if expired; the holder renews every ~ttl/3 s.

The table schema (``app.models_m1.LeaderLease``):

    lease_name TEXT PRIMARY KEY
    holder_id  TEXT NULL
    acquired_at TIMESTAMPTZ NULL
    expires_at  TIMESTAMPTZ NULL
    version     INTEGER NOT NULL DEFAULT 0

Atomic acquisition uses a CAS update guarded by ``version`` so two callers
racing on an expired lease cannot both succeed. Every statement below is its
own autocommitted ``db.executesql()`` call (no ``db.transaction()`` wrapping)
-- unlike the advisory-lock + hash-chain units in ``audit_chain_writer.py`` /
``audit_chain.py``, this primitive doesn't need a shared pinned connection
across statements. The CAS is enforced by Postgres itself at the row level
during each ``UPDATE ... WHERE version = :old_version`` (a lost race simply
returns ``rowcount == 0``), and the INSERT race is caught via the
``lease_name`` primary key uniqueness constraint -- both are safe to split
across independent autocommitted round-trips.

``leader_leases`` carries no RLS policy (it's cluster-wide coordination
state, not tenant data -- confirmed absent from the baseline migration's
``rls_tables`` list), so no tenant/RLS scoping is required here.

This module is fully synchronous; callers running in async loops should call
through ``asyncio.to_thread()`` (the audit_chain_writer worker does this).
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, cast

from penguin_dal import DB

logger = logging.getLogger(__name__)


DEFAULT_TTL_SECONDS = 60


def _default_holder_id() -> str:
    """Build a stable holder id: ``<hostname>/<pid>/<rand>``.

    The random suffix prevents two replicas with identical hostname+pid
    (rare but seen in container orchestrators) from looking identical.
    """

    hostname = os.getenv("HOSTNAME") or socket.gethostname() or "unknown"
    return f"{hostname}/{os.getpid()}/{uuid.uuid4().hex[:8]}"


@dataclass
class LeaderLeaseHandle:
    """Handle returned by :func:`acquire`. Pass to :func:`renew` / :func:`release`."""

    lease_name: str
    holder_id: str
    ttl_seconds: int
    acquired_at: datetime
    expires_at: datetime
    version: int
    extra: dict[str, Any] = field(default_factory=dict)


class LeaderLeaseError(RuntimeError):
    """Base class for lease errors."""


class LeaseLostError(LeaderLeaseError):
    """Raised when renew() / release() finds the lease no longer held by us."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def acquire(
    db: DB,
    lease_name: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    holder_id: Optional[str] = None,
) -> Optional[LeaderLeaseHandle]:
    """Attempt to acquire ``lease_name``. Returns a handle on success, else ``None``.

    Acquisition succeeds when the row is missing, the holder is null, the
    current holder is us, or the lease has expired. The update is guarded by
    ``version`` (optimistic CAS) -- see module docstring for why no explicit
    transaction is needed to make this safe under concurrent callers.
    """

    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    holder = holder_id or _default_holder_id()
    now = _now()
    new_expires = now + timedelta(seconds=ttl_seconds)

    select_sql = (
        "SELECT lease_name, holder_id, acquired_at, expires_at, version "
        "FROM leader_leases WHERE lease_name = %(name)s"
    )

    existing_rows = cast(
        "list[dict[str, Any]]",
        db.executesql(select_sql, {"name": lease_name}, as_dict=True),
    )
    existing_row: Optional[dict[str, Any]] = existing_rows[0] if existing_rows else None

    if existing_row is None:
        # Race-safe insert: another writer may insert between SELECT and INSERT,
        # in which case the INSERT will fail (lease_name PRIMARY KEY) -- we
        # catch and fall through to the CAS update below.
        try:
            db.executesql(
                "INSERT INTO leader_leases "
                "(lease_name, holder_id, acquired_at, expires_at, version) "
                "VALUES (%(name)s, %(holder)s, %(acquired)s, %(expires)s, 1)",
                {
                    "name": lease_name,
                    "holder": holder,
                    "acquired": now,
                    "expires": new_expires,
                },
            )
            return LeaderLeaseHandle(
                lease_name=lease_name,
                holder_id=holder,
                ttl_seconds=ttl_seconds,
                acquired_at=now,
                expires_at=new_expires,
                version=1,
            )
        except Exception as exc:  # noqa: BLE001 — IntegrityError on the PK, etc.
            logger.debug(
                "Lease %s insert race lost: %s — falling through to UPDATE",
                lease_name,
                exc,
            )
            fallback_rows = cast(
                "list[dict[str, Any]]",
                db.executesql(select_sql, {"name": lease_name}, as_dict=True),
            )
            existing_row = fallback_rows[0] if fallback_rows else None
            if existing_row is None:
                return None

    current_holder = existing_row.get("holder_id")
    expires_at = existing_row.get("expires_at")
    current_version = existing_row.get("version") or 0

    is_expired = expires_at is None or expires_at <= now
    is_unowned = not current_holder
    is_self = current_holder == holder

    if not (is_expired or is_unowned or is_self):
        return None

    # CAS update — bump version; only succeed if version unchanged.
    new_version = current_version + 1
    rowcount = cast(
        int,
        db.executesql(
            "UPDATE leader_leases SET holder_id = %(holder)s, acquired_at = %(acquired)s, "
            "expires_at = %(expires)s, version = %(new_version)s "
            "WHERE lease_name = %(name)s AND version = %(old_version)s",
            {
                "holder": holder,
                "acquired": now,
                "expires": new_expires,
                "new_version": new_version,
                "name": lease_name,
                "old_version": current_version,
            },
            return_rowcount=True,
        ),
    )

    if not rowcount:
        # CAS lost — somebody else acquired between our SELECT and UPDATE.
        return None

    return LeaderLeaseHandle(
        lease_name=lease_name,
        holder_id=holder,
        ttl_seconds=ttl_seconds,
        acquired_at=now,
        expires_at=new_expires,
        version=new_version,
    )


def renew(db: DB, handle: LeaderLeaseHandle) -> LeaderLeaseHandle:
    """Extend the handle's expiry by ttl_seconds. Raises :class:`LeaseLostError`
    if we no longer hold the lease (another holder, or row deleted).

    Returns a new handle reflecting the bumped version + expiry.
    """

    now = _now()
    new_expires = now + timedelta(seconds=handle.ttl_seconds)
    new_version = handle.version + 1

    rowcount = cast(
        int,
        db.executesql(
            "UPDATE leader_leases SET expires_at = %(expires)s, version = %(new_version)s "
            "WHERE lease_name = %(name)s AND holder_id = %(holder)s AND version = %(old_version)s",
            {
                "expires": new_expires,
                "new_version": new_version,
                "name": handle.lease_name,
                "holder": handle.holder_id,
                "old_version": handle.version,
            },
            return_rowcount=True,
        ),
    )

    if not rowcount:
        raise LeaseLostError(
            f"Lease {handle.lease_name!r} renewal failed — held by another or version drifted"
        )

    return LeaderLeaseHandle(
        lease_name=handle.lease_name,
        holder_id=handle.holder_id,
        ttl_seconds=handle.ttl_seconds,
        acquired_at=handle.acquired_at,
        expires_at=new_expires,
        version=new_version,
        extra=handle.extra,
    )


def release(db: DB, handle: LeaderLeaseHandle) -> bool:
    """Release the lease by clearing the holder. Returns ``True`` on success,
    ``False`` if we no longer held it (already-stolen lease).
    """

    rowcount = cast(
        int,
        db.executesql(
            "UPDATE leader_leases SET holder_id = NULL, expires_at = %(now)s, "
            "version = version + 1 "
            "WHERE lease_name = %(name)s AND holder_id = %(holder)s AND version = %(old_version)s",
            {
                "now": _now(),
                "name": handle.lease_name,
                "holder": handle.holder_id,
                "old_version": handle.version,
            },
            return_rowcount=True,
        ),
    )

    return bool(rowcount)


def is_leader(db: DB, handle: LeaderLeaseHandle) -> bool:
    """Return whether the handle still represents the live leader for the lease.

    Used by leader-only workers to short-circuit before touching shared state.
    """

    rows = cast(
        "list[dict[str, Any]]",
        db.executesql(
            "SELECT holder_id, expires_at, version FROM leader_leases "
            "WHERE lease_name = %(name)s",
            {"name": handle.lease_name},
            as_dict=True,
        ),
    )
    if not rows:
        return False
    data = rows[0]

    if data.get("holder_id") != handle.holder_id:
        return False
    if data.get("version") != handle.version:
        return False

    expires_at = data.get("expires_at")
    if expires_at is None:
        return False
    return bool(expires_at > _now())


def wait_for_leader(
    db_factory: Callable[[], DB],
    lease_name: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    poll_interval_seconds: float = 15.0,
    holder_id: Optional[str] = None,
    max_attempts: Optional[int] = None,
) -> LeaderLeaseHandle:
    """Block-and-poll until we successfully acquire the lease.

    ``db_factory`` is a zero-arg callable returning a fresh penguin-dal
    :class:`DB` (so the polling loop doesn't accumulate connection-pool state
    across attempts). Each attempt's ``DB`` is closed in ``finally``,
    including on the final successful attempt.

    Mostly used in tests / single-leader daemons. The audit_chain_writer
    background loop uses :func:`acquire` directly with its own backoff.
    """

    attempts = 0
    holder = holder_id or _default_holder_id()
    while True:
        db = db_factory()
        try:
            handle = acquire(db, lease_name, ttl_seconds=ttl_seconds, holder_id=holder)
            if handle is not None:
                return handle
        finally:
            db.close()

        attempts += 1
        if max_attempts is not None and attempts >= max_attempts:
            raise LeaderLeaseError(
                f"could not acquire lease {lease_name!r} within {max_attempts} attempts"
            )
        time.sleep(poll_interval_seconds)
