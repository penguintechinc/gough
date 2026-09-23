"""Audit Events REST API.

Per Gough spec Sprint 4 (Observability -> Audit Log Hash-Chain Format).

Endpoints:
  GET  /api/v1/audit/events     paginated, tenant-scoped query
  POST /api/v1/audit/verify     re-hashes the chain over [since, to]
  GET  /api/v1/audit/export     superadmin JSONL stream + Vault-signed footer

Chain integrity is the core deliverable: ``verify_chain`` is invoked exactly
as written in ``app.security.audit_chain``; on any break the Prometheus
counter ``gough_audit_chain_break_total`` is incremented before responding.

Export is a separate trust boundary — only ``gough.cluster.superadmin`` may
trigger it, MFA is mandatory in compliance lanes, and every export is itself
audit-logged with cross-referenced actor / target subjects so a future audit
sweep can trace which operator pulled which dataset.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from prometheus_client import Counter

from ..metrics import get_or_create
from quart import Blueprint, Response, current_app, g, jsonify, request
from quart import stream_with_context

from app.security.audit_chain import (
    AuditEventWriter,
    canonicalize_record,
    verify_chain,
)
from app.security.scope_enforcement import require_scopes

from ..db.run_db import run_db
from ..models import get_db

log = logging.getLogger(__name__)

audit_bp = Blueprint("audit", __name__)


# Module-level Prometheus counter. Tests reset it via
# ``audit_chain_break_total._value.set(0)``.
#
# app.workers.audit_chain_writer declares the same metric name, so whichever
# module imported second used to raise ValueError("Duplicated timeseries")
# at import time -- which showed up as blueprint-import failures once both
# landed in one test session. get_or_create() returns the existing collector.
audit_chain_break_total = get_or_create(
    Counter,
    "gough_audit_chain_break_total",
    "Audit hash-chain break detections",
    ("cluster_id",),
)


DEFAULT_MFA_REQUIRED_LANES: frozenset[str] = frozenset({"fedramp", "hipaa", "pci"})

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000


# =============================================================================
# Helpers
# =============================================================================


def _get_db_session() -> Any:
    """Return a SQLAlchemy-session-shaped object bound to the request.

    No longer used by any handler in this module (FIX #6a/#7a converted
    ``verify_audit_chain``/``export_audit_log``/``_build_audit_writer`` to
    penguin-dal's ``get_db()`` + the ContextVar-wired RLS pool events --
    see ``app.db.rls``). Kept only because ``app.grpc_server``'s
    ``AuditServicer.AppendEvent`` still imports it directly; that handler
    remains documented out-of-scope dead code (``DB_SESSION_FACTORY`` is
    never wired into ``create_app()``, so this always raises below).
    """
    sess = g.get("db_session", None)
    if sess is not None:
        return sess
    factory = current_app.config.get("DB_SESSION_FACTORY")
    if factory is None:
        raise RuntimeError("No DB session bound to request context")
    sess = factory()
    g.db_session = sess
    return sess


def _get_tenant_id() -> str:
    tenant_ctx = g.get("tenant_context", None)
    if tenant_ctx is not None:
        return tenant_ctx.tenant_id
    principal = g.get("principal", None)
    if principal is not None:
        return principal.tenant_id
    raise PermissionError("tenant_context missing — request not authenticated")


def _get_actor_sub() -> str:
    principal = g.get("principal", None)
    if principal is not None:
        return principal.sub
    user = g.get("current_user", None)
    if user is not None:
        return str(user.get("sub") or user.get("email") or "unknown")
    return "unknown"


def _get_actor_scope() -> list[str]:
    principal = g.get("principal", None)
    if principal is not None:
        return sorted(principal.scopes)
    return []


def _is_super_admin() -> bool:
    principal = g.get("principal", None)
    if principal is None:
        return False
    return "gough.cluster.superadmin" in principal.scopes


def _mfa_required_for_tenant(tenant_id: str) -> bool:
    lane = current_app.config.get("TENANT_COMPLIANCE_LANE", {}).get(tenant_id)
    if lane is None:
        return False
    required = current_app.config.get(
        "COMPLIANCE_LANES_REQUIRING_MFA", DEFAULT_MFA_REQUIRED_LANES
    )
    return lane in required


def _request_has_mfa() -> bool:
    principal = g.get("principal", None)
    if principal is None:
        return False
    amr = principal.claims.get("amr") or []
    if isinstance(amr, str):
        amr = [amr]
    return any(m in {"mfa", "totp", "webauthn", "u2f", "hwk"} for m in amr)


def _parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    # Accept Z suffix as +00:00
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _serialize_audit_event(row: Any) -> dict[str, Any]:
    """Serialize a row from ``audit_events`` to JSON-safe dict.

    The hash, prev_hash, and signature columns are exposed (this is the audit
    surface; chain transparency is the point) but rendered as base64 so
    binary doesn't poison JSON streams.
    """

    def _b64(buf: Any) -> Optional[str]:
        if buf is None:
            return None
        if isinstance(buf, memoryview):
            buf = bytes(buf)
        if isinstance(buf, (bytes, bytearray)):
            return base64.b64encode(bytes(buf)).decode("ascii")
        return base64.b64encode(str(buf).encode("utf-8")).decode("ascii")

    return {
        "id": str(row.id),
        "ts": row.ts.isoformat() if row.ts else None,
        "cluster_id": row.cluster_id,
        "tenant_id": row.tenant_id,
        "actor_sub": row.actor_sub,
        "actor_scope": row.actor_scope or [],
        "action": row.action,
        "resource_kind": row.resource_kind,
        "resource_id": row.resource_id,
        "before_json": row.before_json,
        "after_json": row.after_json,
        "request_id": row.request_id,
        "source_ip": row.source_ip,
        "user_agent": row.user_agent,
        "prev_hash_b64": _b64(row.prev_hash),
        "hash_b64": _b64(row.hash),
        "signature_b64": _b64(row.signature),
    }


def _build_audit_writer(cluster_id: str) -> AuditEventWriter:
    """Construct an ``AuditEventWriter`` bound to the penguin-dal overlay.

    ``AuditEventWriter`` (``app.security.audit_chain``, Task 8a) takes a
    penguin-dal ``DB`` instance despite the constructor keyword still being
    named ``db_session`` -- ``get_db()`` is the same connection-pool-backed
    instance the ContextVar-wired RLS events (``app.db.rls``) apply the
    tenant GUC to on checkout, so the writer's chain-head read + insert are
    tenant-scoped exactly like every other penguin-dal call in this module.
    """
    vault_client = current_app.config.get("VAULT_CLIENT")
    signer = None
    if vault_client is not None:
        signing_key = current_app.config.get(
            "AUDIT_VAULT_SIGNING_KEY", "gough-audit-chain"
        )

        def _signer(record_hash: bytes) -> bytes:
            sig = vault_client.transit_sign(signing_key, record_hash)
            return sig.encode("utf-8") if isinstance(sig, str) else sig

        signer = _signer
    return AuditEventWriter(
        db_session=get_db(), cluster_id=cluster_id, signer=signer
    )


# =============================================================================
# Endpoints
# =============================================================================


@audit_bp.route("/events", methods=["GET"])
@require_scopes("gough.audit.read")
async def list_audit_events():
    """Paginated, tenant-scoped query over ``audit_events``.

    Runtime reads go through the penguin-dal overlay (``get_db()``), not the
    SQLAlchemy session -- there is no other SQLAlchemy-session-bound call in
    this handler that needs to share a connection/transaction with this
    query, so it does not call ``set_tenant_guc``/``_get_db_session()``
    either; this matches every other penguin-dal-backed API module in this
    service (nodes/biomes/webhooks/clusters/disks), none of which call
    ``set_tenant_guc`` -- tenant isolation here is enforced by the explicit
    ``tenant_id ==`` filter below (Layer 1/2 app-level scoping), same as
    those modules.

    Regression: gh-22. The page SELECT runs off the event loop via
    ``run_db()`` (``app.db.run_db``) -- this service is single-process,
    single-loop (gRPC shares it), so a synchronous multi-hundred-row query
    here would stall every other request/RPC until it returned.
    """
    tenant_id = _get_tenant_id()

    args = request.args
    since = _parse_iso8601(args.get("since"))
    until = _parse_iso8601(args.get("until"))
    actor_sub = args.get("actor_sub")
    action = args.get("action")
    resource_kind = args.get("resource_kind")
    resource_id = args.get("resource_id")
    request_id = args.get("request_id")
    cluster_id_filter = args.get("cluster_id")
    fmt = args.get("format", "json").lower()
    if fmt not in {"json", "jsonl"}:
        return (
            jsonify(
                {"error": "invalid_format", "allowed": ["json", "jsonl"]}
            ),
            400,
        )

    try:
        page_size = min(int(args.get("limit", DEFAULT_PAGE_SIZE)), MAX_PAGE_SIZE)
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    cursor = args.get("cursor")

    if cluster_id_filter and not _is_super_admin():
        return (
            jsonify(
                {
                    "error": "forbidden",
                    "message": (
                        "cluster_id filter requires gough.cluster.superadmin"
                    ),
                }
            ),
            403,
        )

    cursor_uuid: Optional[uuid.UUID] = None
    if cursor:
        try:
            cursor_uuid = uuid.UUID(cursor)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_cursor"}), 400

    db = get_db()
    query = db.audit_events.tenant_id == tenant_id

    if cluster_id_filter:
        query = query & (db.audit_events.cluster_id == cluster_id_filter)
    if since:
        query = query & (db.audit_events.ts >= since)
    if until:
        query = query & (db.audit_events.ts <= until)
    if actor_sub:
        query = query & (db.audit_events.actor_sub == actor_sub)
    if action:
        query = query & (db.audit_events.action == action)
    if resource_kind:
        query = query & (db.audit_events.resource_kind == resource_kind)
    if resource_id:
        query = query & (db.audit_events.resource_id == resource_id)
    if request_id:
        query = query & (db.audit_events.request_id == request_id)
    if cursor_uuid is not None:
        # ``id`` is physically VARCHAR(36) (app.models_m1.UUID TypeDecorator
        # stores as string) -- reflected by penguin-dal as a String column,
        # so compare against the string form, not the uuid.UUID object.
        query = query & (db.audit_events.id > str(cursor_uuid))

    def _fetch_page() -> Any:
        return db(query).select(
            orderby=db.audit_events.id, limitby=(0, page_size + 1)
        )

    rows = await run_db(_fetch_page)
    has_more = len(rows) > page_size
    rows = list(rows)[:page_size]
    next_cursor = str(rows[-1].id) if rows and has_more else None
    items = [_serialize_audit_event(r) for r in rows]

    if fmt == "jsonl":
        body_lines = [json.dumps(i, separators=(",", ":")) for i in items]
        body = "\n".join(body_lines) + ("\n" if body_lines else "")
        return Response(body, status=200, mimetype="application/x-ndjson")

    return (
        jsonify(
            {
                "tenant_id": tenant_id,
                "count": len(items),
                "next_cursor": next_cursor,
                "items": items,
            }
        ),
        200,
    )


@audit_bp.route("/verify", methods=["POST"])
@require_scopes("gough.audit.read")
async def verify_audit_chain():
    """Verify hash chain integrity over an optional time window.

    Reads via the penguin-dal overlay (``get_db()``) -- tenant isolation is
    enforced by RLS through the ContextVar the tenant middleware already set
    (``app.db.rls``), not by an explicit call here, matching every other
    penguin-dal-backed handler in this module. A non-superadmin caller's
    verification is therefore naturally scoped to their own tenant's rows.
    """
    body = await request.get_json(silent=True) or {}
    since = _parse_iso8601(body.get("since"))
    to = _parse_iso8601(body.get("to"))

    # Preserved from the pre-conversion behavior even though the returned
    # tenant_id is no longer needed here (RLS applies it via the ContextVar):
    # raises PermissionError if the request has no tenant_context/principal,
    # same defense-in-depth check every other handler in this module makes.
    _get_tenant_id()
    db = get_db()

    # Regression: gh-22. verify_chain() issues a synchronous db.executesql()
    # (app.security.audit_chain) -- off the event loop via run_db() instead
    # of blocking the request coroutine inline.
    def _verify() -> dict[str, Any]:
        return verify_chain(db, since=since, to=to)

    result = await run_db(_verify)

    cluster_id_label = current_app.config.get("CLUSTER_ID", "unknown")
    if result.get("breaks", 0) > 0:
        audit_chain_break_total.labels(cluster_id=cluster_id_label).inc(
            result["breaks"]
        )

    out = {
        "rows_checked": result["rows_checked"],
        "breaks": result["breaks"],
        "first_break_id": (
            str(result["first_break_id"]) if result.get("first_break_id") else None
        ),
        "last_break_id": (
            str(result["last_break_id"]) if result.get("last_break_id") else None
        ),
    }
    return jsonify(out), 200


def _fetch_audit_export_batch(db: Any, last_id: str, batch_size: int) -> list[Any]:
    """Blocking: fetch one keyset-paginated batch of ``audit_events`` rows.

    Split out of ``_stream_audit_events_jsonl`` so each batch's SELECT (the
    only DB statement in this unit of work) can run off the event loop via
    ``run_db()`` -- see that function's docstring for why.
    """
    return list(
        db(db.audit_events.id > last_id).select(
            orderby=db.audit_events.id, limitby=(0, batch_size)
        )
    )


def _sign_export_digest(
    vault_client: Optional[Any], signing_key: str, digest: bytes
) -> Any:
    """Blocking: Vault-transit-sign the rolling export digest.

    Returns ``"unsigned"`` if no Vault client is configured, or a
    ``"vault-sign-error:..."`` string if signing fails -- both surfaced
    verbatim in the export footer rather than failing the whole export.
    """
    if vault_client is None:
        return "unsigned"
    try:
        return vault_client.transit_sign(signing_key, digest)
    except Exception as exc:  # pragma: no cover — surfaced to footer
        return f"vault-sign-error:{exc}"


async def _stream_audit_events_jsonl(
    db: Any,
    cluster_id_label: str,
    target_sub: Optional[str],
    vault_client: Optional[Any],
    signing_key: str,
    batch_size: int = 500,
) -> AsyncIterator[bytes]:
    """Asynchronously yield JSONL bytes for the entire ``audit_events`` table.

    ``db`` is a penguin-dal ``DB`` instance. penguin-dal's ``.select()`` has
    no server-side cursor (it materializes its full result set per call), so
    there is no direct ``yield_per``-equivalent -- memory-bounded iteration
    over the whole table is done here with a keyset-paginated loop instead:
    batches of ``batch_size`` rows ordered by ``id`` (UUIDv7, monotonically
    increasing), each batch's last id feeding the next batch's ``WHERE id >
    :last_id``. Cross-tenant by design (superadmin-only export, unchanged
    from the pre-conversion behavior -- no tenant filter here).

    Regression: gh-22. This used to be a synchronous generator run inline
    inside the request's async stream -- every batch SELECT and the final
    Vault signature call blocked the single shared event loop for their
    entire duration (this service is single-process/single-loop, gRPC
    included). Each batch fetch and the signing call now go through
    ``run_db()`` (``app.db.run_db``) individually, so the generator stays
    both memory-bounded (only one batch materialized at a time) and
    loop-friendly (every other request/RPC can make progress between
    batches). Per-row canonicalization/hashing stays inline -- pure CPU, no
    I/O, and cheap enough per row not to warrant its own thread hop.

    Each row is hashed (SHA-256 over JCS canonical form) into a rolling export
    digest. The final line is a Vault-transit signature over that digest so a
    downstream verifier can detect tampering during transport.
    """
    rolling = hashlib.sha256()
    count = 0
    # ``id`` is physically VARCHAR(36) (see app.models_m1.UUID
    # TypeDecorator); an all-zero UUID sentinel sorts before every real
    # UUIDv7 id as a string too, so it's a safe "no rows yet" starting point.
    last_id = str(uuid.UUID(int=0))

    while True:
        current_last_id = last_id

        def _fetch_batch() -> list[Any]:
            return _fetch_audit_export_batch(db, current_last_id, batch_size)

        batch = await run_db(_fetch_batch)
        if not batch:
            break
        for row in batch:
            record = _serialize_audit_event(row)
            canonical = canonicalize_record(record)
            rolling.update(canonical)
            count += 1
            yield canonical + b"\n"
        last_id = batch[-1].id
        if len(batch) < batch_size:
            break

    digest = rolling.digest()

    signature = await run_db(
        lambda: _sign_export_digest(vault_client, signing_key, digest)
    )

    if isinstance(signature, bytes):
        signature_str = signature.decode("utf-8", errors="replace")
    else:
        signature_str = str(signature)

    footer = {
        "_signature": True,
        "rows_exported": count,
        "digest_b64": base64.b64encode(digest).decode("ascii"),
        "signing_key": signing_key,
        "signature": signature_str,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "cluster_id": cluster_id_label,
        "target_sub": target_sub,
    }
    yield (json.dumps(footer, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


@audit_bp.route("/export", methods=["GET"])
@require_scopes("gough.cluster.superadmin")
async def export_audit_log():
    """Stream audit log as JSONL with Vault-transit signed footer.

    The self-audit-log write below (``audit_writer.append(...)``) and the
    export stream read both go through the penguin-dal overlay now
    (``_build_audit_writer`` -> ``get_db()`` / ``dal_db = get_db()``) --
    tenant scoping for the write comes from RLS via the ContextVar the
    tenant middleware set (``app.db.rls``), same as every other
    penguin-dal-backed handler; no explicit GUC call needed here. The
    export stream itself remains deliberately cross-tenant (see
    ``_stream_audit_events_jsonl``'s docstring) -- callers whose token
    carries ``cross_tenant=True`` get the RLS cross-tenant sentinel pushed
    by the tenant middleware, which is required for the export stream.
    The chain-head lookup inside ``audit_writer.append()`` no longer
    depends on that caller-supplied claim: ``append()`` now self-applies
    the cross-tenant scope internally via its own ``_cross_tenant_scope()``
    helper, so it sees the true global chain head regardless of the
    caller's own tenant claim.
    """
    tenant_id = _get_tenant_id()
    mfa_block_required = _mfa_required_for_tenant(tenant_id) and not _request_has_mfa()
    if mfa_block_required:
        return (
            jsonify(
                {"error": "mfa_required", "message": "Compliance lane requires MFA"}
            ),
            401,
        )

    target_sub = request.args.get("target_sub")
    cluster_id_label = current_app.config.get("CLUSTER_ID", "unknown")
    signing_key = current_app.config.get(
        "AUDIT_EXPORT_SIGNING_KEY", "gough-audit-export"
    )
    vault_client = current_app.config.get("VAULT_CLIENT")

    actor_sub = _get_actor_sub()
    audit_writer = _build_audit_writer(cluster_id_label)

    # Regression: gh-22. audit_writer.append() opens a penguin-dal
    # transaction and issues synchronous executesql() calls -- off the
    # event loop via run_db() instead of blocking the request coroutine
    # inline. Insert + its own commit stays one closure (see app/db/run_db.py).
    def _append_export_audit_event() -> Any:
        return audit_writer.append(
            actor_sub=actor_sub,
            actor_scope=_get_actor_scope(),
            action="audit.log.export",
            resource_kind="audit_log",
            tenant_id=tenant_id,
            after={
                "acting_sub": actor_sub,
                "target_sub": target_sub,
                "exported_at": datetime.now(timezone.utc).isoformat(),
            },
            request_id=request.headers.get("X-Request-Id"),
        )

    await run_db(_append_export_audit_event)

    nats_client = current_app.config.get("NATS_CLIENT")
    if nats_client is not None:
        try:
            payload = json.dumps(
                {
                    "actor_sub": actor_sub,
                    "tenant_id": tenant_id,
                    "target_sub": target_sub,
                    "cluster_id": cluster_id_label,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            ).encode("utf-8")
            nats_client.publish("gough.audit.exported", payload)
        except Exception:
            log.exception("Failed to publish gough.audit.exported NATS event")

    dal_db = get_db()

    @stream_with_context
    async def _async_stream() -> AsyncIterator[bytes]:
        async for chunk in _stream_audit_events_jsonl(
            dal_db,
            cluster_id_label=cluster_id_label,
            target_sub=target_sub,
            vault_client=vault_client,
            signing_key=signing_key,
        ):
            yield chunk

    headers = {
        "Content-Type": "application/x-ndjson",
        "Content-Disposition": (
            f'attachment; filename="gough-audit-{cluster_id_label}.jsonl"'
        ),
    }
    return Response(_async_stream(), headers=headers, status=200)


__all__ = [
    "audit_bp",
    "audit_chain_break_total",
    "DEFAULT_MFA_REQUIRED_LANES",
    "_serialize_audit_event",
    "_stream_audit_events_jsonl",
]
