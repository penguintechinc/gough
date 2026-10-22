"""Audit chain — hash-chained immutable audit log.

Per Gough spec Observability → Audit Log Hash-Chain Format. UUIDv7 IDs;
RFC 8785 JCS canonicalization; SHA-256 chain hash; optional Vault-transit
signature (Ed25519 enterprise / ECDSA P-256 fedramp lane).

Genesis row insertion is handled by `gough init` at cluster bootstrap.

penguin-dal conversion notes (Task 8a):

* ``AuditEventWriter.append()`` takes an optional ``tx`` (a penguin-dal
  ``Tx`` from ``DB.transaction()``). Callers that need the advisory-lock +
  hash-read + insert to share one pinned connection (``AuditChainWriter
  .append_event()`` in ``app.workers.audit_chain_writer``) pass their own
  ``tx``; callers that just want a single self-contained append (none exist
  live today -- see below) can omit it and a private transaction is opened.
* The chain-head lookup (``SELECT hash FROM audit_events ORDER BY ts DESC,
  id DESC LIMIT 1``) is intentionally NOT tenant-filtered -- the hash chain
  is one global, cluster-wide sequence spanning every tenant's events, not
  one chain per tenant. FIX (gh-22): ``append()`` now self-applies the RLS
  cross-tenant sentinel (``app.db.rls.CROSS_TENANT_SENTINEL``, via the
  local ``_cross_tenant_scope()`` helper below) tightly around its own DB
  work (the caller-provided-``tx`` branch and the self-opened-transaction
  branch alike) -- running the chain-head SELECT under a single-tenant GUC
  scope would otherwise silently return a prev_hash that skips every other
  tenant's more-recent rows and fork the chain. This used to be left to the
  caller (matching the ``persist_extracted_material``/
  ``_cross_tenant_scope()`` precedent in ``app.workers.joiner_secret_emitter``
  / ``app.grpc_server``); two live request-path callers never applied it
  (see the fixed bullet below) and could fork the chain under an ordinary
  per-tenant GUC. ``AuditChainWriter.append_event()`` still wraps its own
  call in ``_cross_tenant_scope()`` too -- no longer load-bearing (its
  ``tx``'s connection is already checked out under that scope by the time
  it reaches ``append()``), kept as-is since it's harmless (setting an
  already-sentinel ContextVar to the sentinel again) and this task doesn't
  touch that module.
  ``verify_chain()`` deliberately does NOT get the same self-applied scope
  -- unlike ``append()`` it has two callers with genuinely different scope
  needs: ``AuditChainWriter.verify_chain_scheduled`` (background, needs the
  full cross-tenant chain, wraps it itself) and
  ``app.api.audit.verify_audit_chain`` (the ``POST /verify`` endpoint,
  intentionally tenant-scoped -- a non-superadmin caller's verification is
  scoped to their own tenant's rows via the ordinary per-request GUC).
  Baking the sentinel into ``verify_chain()`` itself would silently break
  that intentional per-tenant behavior, so scope selection stays the
  caller's call to make there.
* Constructor keyword is still named ``db_session`` for source compatibility.
  ``app.api.audit._build_audit_writer`` and ``app.api.joiner_secrets
  ._build_audit_writer`` (FIX #7a cleanup) now pass ``app.models.get_db()``
  -- both are LIVE callers reachable from ``export_audit_log`` /
  ``revoke_joiner_secret`` respectively, no longer the dead
  always-``RuntimeError`` paths they were before that cleanup.
  ``app.grpc_server.AuditServicer.AppendEvent`` still passes its own
  ``_get_db_session()`` (imported from ``app.api.audit``, kept there for
  this reason) and remains dead -- ``DB_SESSION_FACTORY`` is never wired
  into the app factory, out of scope for both this file and that one.
  Despite the keyword name, the value must be a penguin-dal ``DB`` instance,
  not a SQLAlchemy Session.
* FIX (gh-22): ``export_audit_log``'s self-audit-log write and
  ``revoke_joiner_secret``'s both call ``AuditEventWriter.append()`` without
  wrapping it in ``app.db.rls.CROSS_TENANT_SENTINEL`` themselves -- unlike
  ``AuditChainWriter`` (the background worker), they ran under whatever
  tenant GUC the caller's own JWT set (their own tenant, or the sentinel
  only if that JWT carried ``cross_tenant=True``), matching the
  pre-conversion ``set_tenant_guc(db, tenant_id)`` behavior exactly (that
  also used the caller's own ``tenant_id``, never the sentinel) -- so a
  superadmin ``/export`` call without a ``cross_tenant=True`` token risked
  forking the chain head. ``append()`` now self-applies the cross-tenant
  scope (see the chain-head lookup bullet above), fixing this at the
  source for every caller regardless of what GUC scope it happens to be
  running under. The row's own ``tenant_id`` *column* is unaffected by this
  -- it's still the caller-supplied ``tenant_id=`` keyword argument, read
  from the request's JWT/tenant context by both callers before ``append()``
  is ever invoked; only the RLS *visibility* scope for the chain-head
  SELECT/INSERT changes.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading as _threading
import time
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Callable, Optional, cast

from pydantic import BaseModel

from app.db.rls import CROSS_TENANT_SENTINEL, get_current_tenant, set_current_tenant

GENESIS_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")
ZERO_HASH = b"\x00" * 32

_uuidv7_lock = _threading.Lock()
_uuidv7_state: dict[str, int] = {"last_ms": 0, "seq": 0}


def generate_uuidv7() -> uuid.UUID:
    """Generate RFC 9562 UUIDv7 with monotonic sub-ms sequence."""
    with _uuidv7_lock:
        ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
        if ts_ms > _uuidv7_state["last_ms"]:
            _uuidv7_state["last_ms"] = ts_ms
            _uuidv7_state["seq"] = secrets.randbits(12)
        else:
            ts_ms = _uuidv7_state["last_ms"]
            _uuidv7_state["seq"] = (_uuidv7_state["seq"] + 1) & 0xFFF
        seq = _uuidv7_state["seq"]

    rand_62 = secrets.randbits(62)
    return uuid.UUID(
        fields=(
            (ts_ms >> 16) & 0xFFFFFFFF,
            ts_ms & 0xFFFF,
            0x7000 | seq,
            0x80 | ((rand_62 >> 56) & 0x3F),
            (rand_62 >> 48) & 0xFF,
            rand_62 & 0xFFFFFFFFFFFF,
        )
    )


def canonicalize_record(fields: dict[str, Any]) -> bytes:
    """RFC 8785 JCS: sorted keys, compact JSON, reject NaN/Inf."""

    def check_valid(obj: Any) -> Any:
        if isinstance(obj, float):
            if obj != obj or obj == float("inf") or obj == float("-inf"):
                raise ValueError(f"NaN/Inf not allowed in JCS: {obj}")
        elif isinstance(obj, dict):
            return {k: check_valid(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [check_valid(v) for v in obj]
        return obj

    check_valid(fields)
    return json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def compute_chain_hash(prev_hash: bytes, record_fields: dict[str, Any]) -> bytes:
    """SHA-256(prev_hash || JCS(record_fields)). prev_hash must be 32 bytes."""
    if len(prev_hash) != 32:
        raise ValueError(f"prev_hash must be 32 bytes, got {len(prev_hash)}")
    canonical = canonicalize_record(record_fields)
    return hashlib.sha256(prev_hash + canonical).digest()


class AuditEvent(BaseModel):
    """Audit event record."""

    id: uuid.UUID
    ts: datetime
    cluster_id: str
    tenant_id: Optional[str] = None
    actor_sub: str
    actor_scope: list[str]
    action: str
    resource_kind: str
    resource_id: Optional[str] = None
    before_json: Optional[dict[str, Any]] = None
    after_json: Optional[dict[str, Any]] = None
    request_id: Optional[str] = None
    source_ip: Optional[str] = None
    user_agent: Optional[str] = None
    prev_hash: bytes
    hash: bytes
    signature: Optional[bytes] = None


@contextmanager
def _cross_tenant_scope() -> Iterator[None]:
    """Push the RLS cross-tenant sentinel for one cluster-global DB unit.

    Used by :func:`insert_genesis_row` -- a one-time, cluster-bootstrap
    operation with no tenant of its own (its INSERT carries no ``tenant_id``
    column at all, so the row's ``tenant_id`` is NULL; the baseline
    ``tenant_isolation`` policy's ``USING (current_setting(...) IN
    (tenant_id, '__default__', '__all__'))`` never matches a NULL
    ``tenant_id`` against an ordinary per-tenant setting, only against the
    literal ``'__default__'``/``'__all__'`` sentinels -- so both the
    idempotency SELECT and the INSERT itself need this scope to work at
    all, let alone correctly), and by :meth:`AuditEventWriter.append` (FIX
    gh-22) -- the chain-head SELECT + INSERT must always see the true
    global chain head regardless of the caller's own per-tenant GUC scope.
    See module docstring for why ``verify_chain`` alone still leaves this to
    its callers instead of self-applying it.
    """
    previous = get_current_tenant()
    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        yield
    finally:
        set_current_tenant(previous)


def _as_bytes32(value: Any, fallback: bytes) -> bytes:
    """Normalise a driver-returned hash column to ``bytes``, or ``fallback``.

    psycopg2 can hand back ``bytea`` columns as ``bytes`` or ``memoryview``
    depending on driver configuration; ``compute_chain_hash``/Pydantic's
    ``bytes`` field both need a real ``bytes`` object.
    """
    if isinstance(value, (bytes, bytearray, memoryview)) and len(value) == 32:
        return bytes(value)
    return fallback


class AuditEventWriter:
    """Single-writer leader: appends audit events with chain hash."""

    def __init__(
        self,
        db_session: Any,
        cluster_id: str,
        signer: Optional[Callable[[bytes], bytes]] = None,
    ) -> None:
        """Initialize writer. signer: optional Vault-transit Ed25519 signer.

        ``db_session``: a penguin-dal ``DB`` instance (see module docstring
        for why the parameter keeps this legacy name).
        """
        self.db = db_session
        self.cluster_id = cluster_id
        self.signer = signer

    def append(
        self,
        *,
        tx: Optional[Any] = None,
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
        """Append audit event; fetch latest hash; compute chain hash; optionally sign.

        ``tx``: an already-open penguin-dal ``Tx`` (``DB.transaction()``) to
        run the read+insert on -- pass this when the caller also needs to
        hold a Postgres advisory lock (or any other statement) on the same
        pinned connection for the duration (see ``AuditChainWriter
        .append_event()``). If omitted, a private transaction is opened here
        so the read+insert still commit/rollback together as one unit.

        FIX (gh-22): the read+insert always runs under the RLS cross-tenant
        sentinel (``app.db.rls.CROSS_TENANT_SENTINEL``), self-applied here
        via ``_cross_tenant_scope()`` -- see the module docstring's
        chain-head-lookup bullet for why. This is a visibility-scope change
        only: the inserted row's own ``tenant_id`` column is whatever the
        caller passed as ``tenant_id=`` above, never the sentinel.
        """
        with _cross_tenant_scope():
            if tx is not None:
                return self._append_on_tx(
                    tx,
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
            with self.db.transaction() as opened_tx:
                return self._append_on_tx(
                    opened_tx,
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

    def _append_on_tx(
        self,
        tx: Any,
        *,
        actor_sub: str,
        action: str,
        resource_kind: str,
        resource_id: Optional[str],
        tenant_id: Optional[str],
        actor_scope: Optional[list[str]],
        before: Optional[dict[str, Any]],
        after: Optional[dict[str, Any]],
        request_id: Optional[str],
        source_ip: Optional[str],
        user_agent: Optional[str],
    ) -> AuditEvent:
        actor_scope = actor_scope or []

        prev_rows = cast(
            "list[tuple[Any, ...]]",
            tx.executesql(
                "SELECT hash FROM audit_events ORDER BY ts DESC, id DESC LIMIT 1"
            ),
        )
        prev_hash_raw = prev_rows[0][0] if prev_rows else None
        prev_hash = _as_bytes32(prev_hash_raw, ZERO_HASH)

        event_id = generate_uuidv7()
        ts_utc = datetime.now(timezone.utc)

        record_fields = {
            "id": str(event_id),
            "ts": ts_utc.isoformat(),
            "cluster_id": self.cluster_id,
            "tenant_id": tenant_id,
            "actor_sub": actor_sub,
            "actor_scope": actor_scope,
            "action": action,
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "before_json": before,
            "after_json": after,
            "request_id": request_id,
            "source_ip": source_ip,
            "user_agent": user_agent,
        }

        chain_hash = compute_chain_hash(prev_hash, record_fields)
        signature = self.signer(chain_hash) if self.signer else None

        tx.executesql(
            """
            INSERT INTO audit_events
            (id, ts, cluster_id, tenant_id, actor_sub, actor_scope, action,
             resource_kind, resource_id, before_json, after_json, request_id,
             source_ip, user_agent, prev_hash, hash, signature)
            VALUES
            (%(id)s, %(ts)s, %(cluster_id)s, %(tenant_id)s, %(actor_sub)s,
             %(actor_scope)s, %(action)s, %(resource_kind)s, %(resource_id)s,
             %(before_json)s, %(after_json)s, %(request_id)s, %(source_ip)s,
             %(user_agent)s, %(prev_hash)s, %(hash)s, %(signature)s)
            """,
            {
                "id": str(event_id),
                "ts": ts_utc,
                "cluster_id": self.cluster_id,
                "tenant_id": tenant_id,
                "actor_sub": actor_sub,
                "actor_scope": json.dumps(actor_scope),
                "action": action,
                "resource_kind": resource_kind,
                "resource_id": resource_id,
                "before_json": json.dumps(before) if before else None,
                "after_json": json.dumps(after) if after else None,
                "request_id": request_id,
                "source_ip": source_ip,
                "user_agent": user_agent,
                "prev_hash": prev_hash,
                "hash": chain_hash,
                "signature": signature,
            },
        )

        return AuditEvent(
            id=event_id,
            ts=ts_utc,
            cluster_id=self.cluster_id,
            tenant_id=tenant_id,
            actor_sub=actor_sub,
            actor_scope=actor_scope,
            action=action,
            resource_kind=resource_kind,
            resource_id=resource_id,
            before_json=before,
            after_json=after,
            request_id=request_id,
            source_ip=source_ip,
            user_agent=user_agent,
            prev_hash=prev_hash,
            hash=chain_hash,
            signature=signature,
        )


def verify_chain(
    db: Any,
    since: Optional[datetime] = None,
    to: Optional[datetime] = None,
) -> dict[str, Any]:
    """Verify chain integrity: recompute hashes, detect breaks.

    Read-only -- a single SELECT, no multi-statement transaction needed.
    ``db`` is a penguin-dal ``DB`` (or ``Tx``, either exposes
    ``executesql``). This spans the whole ``audit_events`` table across all
    tenants matched by ``since``/``to`` only; callers running this under a
    background/worker context (no per-request tenant) MUST wrap it in
    ``app.db.rls``'s cross-tenant sentinel themselves (see module docstring)
    -- otherwise RLS fail-closes to zero rows and this silently reports a
    false "0 rows checked, 0 breaks" instead of actually verifying anything.
    ``AuditChainWriter.verify_chain_scheduled`` (the only in-scope caller)
    does this.
    """
    query = (
        "SELECT id, ts, cluster_id, tenant_id, actor_sub, actor_scope, action, "
        "resource_kind, resource_id, before_json, after_json, request_id, "
        "source_ip, user_agent, prev_hash, hash FROM audit_events"
    )
    params: dict[str, Any] = {}
    conditions = []

    if since:
        conditions.append("ts >= %(since)s")
        params["since"] = since
    if to:
        conditions.append("ts <= %(to)s")
        params["to"] = to

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY ts ASC, id ASC"

    rows = cast(
        "list[tuple[Any, ...]]",
        db.executesql(query, params if params else None),
    )

    rows_checked = 0
    breaks = 0
    first_break_id: Optional[Any] = None
    last_break_id: Optional[Any] = None

    for row in rows:
        (
            event_id,
            ts,
            cluster_id,
            tenant_id,
            actor_sub,
            actor_scope,
            action,
            resource_kind,
            resource_id,
            before_json,
            after_json,
            request_id,
            source_ip,
            user_agent,
            stored_prev,
            stored_hash,
        ) = row
        record_fields = {
            "id": str(event_id),
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "cluster_id": cluster_id or "",
            "tenant_id": tenant_id,
            "actor_sub": actor_sub,
            "actor_scope": actor_scope if isinstance(actor_scope, list) else [],
            "action": action,
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "before_json": before_json,
            "after_json": after_json,
            "request_id": request_id,
            "source_ip": source_ip,
            "user_agent": user_agent,
        }

        is_binary = (bytes, bytearray, memoryview)
        stored_prev_bytes = (
            bytes(stored_prev) if isinstance(stored_prev, is_binary) else stored_prev
        )
        computed_hash = compute_chain_hash(stored_prev_bytes, record_fields)
        rows_checked += 1

        stored_hash_bytes = (
            bytes(stored_hash) if isinstance(stored_hash, is_binary) else stored_hash
        )
        if computed_hash != stored_hash_bytes:
            breaks += 1
            if first_break_id is None:
                first_break_id = event_id
            last_break_id = event_id

    return {
        "rows_checked": rows_checked,
        "breaks": breaks,
        "first_break_id": first_break_id,
        "last_break_id": last_break_id,
    }


def insert_genesis_row(db: Any, cluster_id: str) -> AuditEvent:
    """Idempotent genesis: returns existing or inserts new.

    Cluster-global bootstrap operation (no tenant of its own) -- see
    ``_cross_tenant_scope()`` docstring for why this always needs the
    cross-tenant RLS sentinel, regardless of caller context. Wrapped in a
    single ``db.transaction()`` so the idempotency check and the insert
    commit/rollback together.
    """
    with _cross_tenant_scope(), db.transaction() as tx:
        rows = cast(
            "list[tuple[Any, ...]]",
            tx.executesql(
                "SELECT hash FROM audit_events WHERE id = %(id)s",
                {"id": str(GENESIS_ID)},
            ),
        )

        if rows:
            existing_hash = _as_bytes32(rows[0][0], ZERO_HASH)
            return AuditEvent(
                id=GENESIS_ID,
                ts=datetime.now(timezone.utc),
                cluster_id=cluster_id,
                actor_sub="system",
                actor_scope=["system:admin"],
                action="cluster_init",
                resource_kind="cluster",
                prev_hash=ZERO_HASH,
                hash=existing_hash,
            )

        genesis_hash = hashlib.sha256(
            b"gough-cluster-genesis:" + cluster_id.encode("utf-8")
        ).digest()

        tx.executesql(
            """
            INSERT INTO audit_events
            (id, ts, cluster_id, actor_sub, actor_scope, action, resource_kind,
             prev_hash, hash)
            VALUES
            (%(id)s, %(ts)s, %(cluster_id)s, %(actor_sub)s, %(actor_scope)s,
             %(action)s, %(resource_kind)s, %(prev_hash)s, %(hash)s)
            """,
            {
                "id": str(GENESIS_ID),
                "ts": datetime.now(timezone.utc),
                "cluster_id": cluster_id,
                "actor_sub": "system",
                "actor_scope": json.dumps(["system:admin"]),
                "action": "cluster_init",
                "resource_kind": "cluster",
                "prev_hash": ZERO_HASH,
                "hash": genesis_hash,
            },
        )

        return AuditEvent(
            id=GENESIS_ID,
            ts=datetime.now(timezone.utc),
            cluster_id=cluster_id,
            actor_sub="system",
            actor_scope=["system:admin"],
            action="cluster_init",
            resource_kind="cluster",
            prev_hash=ZERO_HASH,
            hash=genesis_hash,
        )
