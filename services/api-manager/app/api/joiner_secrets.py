"""Joiner Secrets REST API.

Per Gough spec Sprint 4 (Biome Model -> Joiner Secrets). All endpoints are
tenant-scoped; cluster-id from the URL is filtered against the JWT's tenant
claim through Layer 4 RLS (the GUC ``app.current_tenant`` is pushed via the
``contextvars.ContextVar`` in ``app.db.rls``, applied to every penguin-dal
connection on pool checkout -- see ``app.middleware.install_security_middleware``).

Response payloads NEVER expose any portion of the encrypted envelope. The
fields ``ciphertext``, ``dek_wrapped``, ``iv``, and ``auth_tag`` are read
straight from the database for storage, but the API serializer in this module
explicitly redacts them — no caller, including ``gough.cluster.superadmin``,
can pull plaintext or wrapped DEK material through this surface. Plaintext
extraction is reserved for the gRPC ``Joiner.Consume`` RPC on a different
trust boundary.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

from penguin_dal import Row
from quart import Blueprint, current_app, g, jsonify, request

from app import metrics as _metrics

from app.security.audit_chain import AuditEventWriter
from app.security.credentials import (
    CredentialError,
    InvalidCredentialError,
    validate_user_jwt,
)
from app.security.joiner_envelope import zero_bytes
from app.security.scope_enforcement import require_scopes
from app.workers.joiner_secret_emitter import (
    ExtractedMaterial,
    persist_extracted_material,
)

from ..db.run_db import run_db
from ..models import get_db

log = logging.getLogger(__name__)

joiner_secrets_bp = Blueprint("joiner_secrets", __name__)

EXPIRING_SOON_WINDOW = timedelta(hours=24)

# Compliance lanes that mandate MFA for sensitive flows. Pulled from
# ``current_app.config["COMPLIANCE_LANES_REQUIRING_MFA"]`` if present so test
# fixtures and tenants can override; default mirrors the spec.
DEFAULT_MFA_REQUIRED_LANES: frozenset[str] = frozenset({"fedramp", "hipaa", "pci"})


# =============================================================================
# Helpers
# =============================================================================


def _get_tenant_id() -> str:
    """Return the tenant id from the validated JWT context.

    The credentials middleware stores ``g.principal`` (Pydantic Principal); if
    fixtures pre-populate ``g.tenant_context`` directly, that path is honored.
    """
    tenant_ctx = g.get("tenant_context", None)
    if tenant_ctx is not None:
        return tenant_ctx.tenant_id
    principal = g.get("principal", None)
    if principal is not None:
        return principal.tenant_id
    raise PermissionError("tenant_context missing — request not authenticated")


def _get_actor_sub() -> str:
    """Return the actor sub for audit logging."""
    principal = g.get("principal", None)
    if principal is not None:
        return principal.sub
    user = g.get("current_user", None)
    if user is not None:
        return str(user.get("sub") or user.get("email") or "unknown")
    return "unknown"


def _get_actor_scope() -> list[str]:
    """Return the actor scopes for audit logging."""
    principal = g.get("principal", None)
    if principal is not None:
        return sorted(principal.scopes)
    return []


def _build_audit_writer(cluster_id: uuid.UUID) -> AuditEventWriter:
    """Construct an AuditEventWriter bound to the cluster + Vault transit signer.

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
        db_session=get_db(),
        cluster_id=str(cluster_id),
        signer=signer,
    )


def _mfa_required_for_tenant(tenant_id: str) -> bool:
    """Return True when this tenant's compliance lane mandates MFA."""
    lane = current_app.config.get("TENANT_COMPLIANCE_LANE", {}).get(tenant_id)
    if lane is None:
        return False
    required = current_app.config.get(
        "COMPLIANCE_LANES_REQUIRING_MFA", DEFAULT_MFA_REQUIRED_LANES
    )
    return lane in required


def _request_has_mfa() -> bool:
    """Return True when the JWT carries an ``amr`` claim with MFA evidence."""
    principal = g.get("principal", None)
    if principal is None:
        return False
    amr = principal.claims.get("amr") or []
    if isinstance(amr, str):
        amr = [amr]
    return any(m in {"mfa", "totp", "webauthn", "u2f", "hwk"} for m in amr)


def _enforce_mfa_if_required(tenant_id: str) -> Optional[tuple[Any, int]]:
    """Return a 401 response if MFA is required but absent, else None."""
    if _mfa_required_for_tenant(tenant_id) and not _request_has_mfa():
        return (
            jsonify(
                {
                    "error": "mfa_required",
                    "tenant_id": tenant_id,
                    "message": "Compliance lane requires MFA for this action",
                }
            ),
            401,
        )
    return None


def _serialize_joiner_secret(row: Any) -> dict[str, Any]:
    """Serialize a joiner_secrets row, omitting all envelope/secret material.

    Strict allow-list serialization. ``row`` is a penguin-dal ``Row`` (dict
    with attribute access) reflecting the ``joiner_secrets`` table. The
    fields ``ciphertext``, ``iv``, ``auth_tag``, and ``dek_wrapped`` are
    present on the row but MUST NEVER appear in API output. Tests assert
    this contract explicitly.
    """
    now = datetime.now(timezone.utc)
    revoked = row.revoked_at is not None
    expires_at = row.expires_at
    if revoked:
        status = "revoked"
    elif expires_at is not None and expires_at < now + EXPIRING_SOON_WINDOW:
        status = "expiring-soon"
    else:
        status = "active"
    expiring_soon = (
        not revoked
        and expires_at is not None
        and expires_at < now + EXPIRING_SOON_WINDOW
    )
    return {
        "id": str(row.id),
        "cluster_id": str(row.cluster_id),
        "tenant_id": row.tenant_id,
        "biome_kind": row.biome_kind,
        "emitter_biome_id": row.emitter_biome_id,
        "emitter_node_id": row.emitter_node_id,
        "extractor_name": row.extractor_name,
        "scope": row.scope,
        "vault_kek_name": row.vault_kek_name,
        "ttl_seconds": row.ttl_seconds,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "rotation_class": row.rotation_class,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "rotated_at": row.rotated_at.isoformat() if row.rotated_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "audit_event_id": (
            str(row.audit_event_id) if row.audit_event_id else None
        ),
        "status": status,
        "expiring_soon": expiring_soon,
    }


# =============================================================================
# Endpoints
# =============================================================================


@joiner_secrets_bp.route(
    "/clusters/<uuid:cluster_id>/joiner-secrets", methods=["GET"]
)
@require_scopes("gough.joiner.read")
async def list_joiner_secrets(cluster_id: uuid.UUID):
    """List joiner secrets for a cluster (metadata only).

    Query params:
        biome_kind: optional filter
        scope: optional filter
        status: ``active|expiring-soon|revoked`` filter

    Runtime read via the penguin-dal overlay (``get_db()``) -- tenant
    isolation is both the explicit ``tenant_id ==`` filter below
    (Layer 1/2 app-level scoping) and RLS via the ContextVar the tenant
    middleware already set (``app.db.rls``), same as every other
    penguin-dal-backed API module in this service.
    """
    tenant_id = _get_tenant_id()
    db = get_db()

    args = request.args
    biome_kind_filter = args.get("biome_kind")
    scope_filter = args.get("scope")
    status_filter = args.get("status")

    # cluster_id/id-like columns are physically VARCHAR(36) (see
    # app.models_m1.UUID TypeDecorator) -- penguin-dal reflects the raw
    # column type, so compare against the string form of the UUID.
    query = (db.joiner_secrets.cluster_id == str(cluster_id)) & (
        db.joiner_secrets.tenant_id == tenant_id
    )
    if biome_kind_filter:
        query = query & (db.joiner_secrets.biome_kind == biome_kind_filter)
    if scope_filter:
        query = query & (db.joiner_secrets.scope == scope_filter)

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_rows() -> Any:
        return db(query).select(orderby=db.joiner_secrets.created_at.desc())

    rows = await run_db(_fetch_rows)
    items = [_serialize_joiner_secret(r) for r in rows]

    if status_filter in {"active", "expiring-soon", "revoked"}:
        items = [i for i in items if i["status"] == status_filter]

    return (
        jsonify(
            {
                "cluster_id": str(cluster_id),
                "tenant_id": tenant_id,
                "count": len(items),
                "items": items,
            }
        ),
        200,
    )


@joiner_secrets_bp.route(
    "/clusters/<uuid:cluster_id>/joiner-secrets/<uuid:js_id>/rotate",
    methods=["POST"],
)
@require_scopes("gough.joiner.rotate")
async def rotate_joiner_secret(cluster_id: uuid.UUID, js_id: uuid.UUID):
    """Rotate a joiner secret: revoke old row + persist a fresh one, atomically.

    Revoke-mark, new-secret insert, and its audit-chain row all happen
    inside a single ``db.transaction()`` (one pinned Postgres connection,
    commit-on-clean-exit / rollback-on-exception) -- if persisting the new
    secret fails for any reason (Vault down, encryption error, etc.), the
    revoke of the old secret rolls back with it. Splitting these into
    separate auto-committing calls (the naive per-call penguin-dal
    ``.update()``/``.insert()`` pattern used elsewhere in this file) would
    leave the old secret permanently revoked with no replacement on a
    mid-rotation failure -- exactly the failure mode this transaction
    exists to prevent.

    Does not go through ``JoinerSecretEmitter.emit()`` -- that method's
    contract (``biome_instance_id`` -> run a fresh control-tunnel
    extraction -> ``list[JoinerSecretMetadata]``) is a different flow than
    "persist material this caller already extracted via
    ``JOINER_ROTATE_MATERIAL_PROVIDER``". Uses
    ``app.workers.joiner_secret_emitter.persist_extracted_material``
    instead, which is exactly that persistence step, factored out to take a
    penguin-dal ``Tx`` so it can run inside this transaction.
    """
    tenant_id = _get_tenant_id()
    mfa_block = _enforce_mfa_if_required(tenant_id)
    if mfa_block is not None:
        return mfa_block

    body = await request.get_json(silent=True) or {}
    reason = body.get("reason")
    if not reason or not isinstance(reason, str) or not reason.strip():
        return (
            jsonify({"error": "missing_reason", "message": "reason is required"}),
            400,
        )

    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_row() -> Any:
        return (
            db(
                (db.joiner_secrets.id == str(js_id))
                & (db.joiner_secrets.cluster_id == str(cluster_id))
                & (db.joiner_secrets.tenant_id == tenant_id)
            )
            .select()
            .first()
        )

    row: Any = await run_db(_fetch_row)
    if row is None:
        return jsonify({"error": "not_found"}), 404
    if row.revoked_at is not None:
        return (
            jsonify(
                {
                    "error": "already_revoked",
                    "message": "cannot rotate a revoked secret",
                }
            ),
            409,
        )

    new_plaintext_provider = current_app.config.get(
        "JOINER_ROTATE_MATERIAL_PROVIDER"
    )
    if new_plaintext_provider is None:
        return (
            jsonify(
                {
                    "error": "rotation_provider_not_configured",
                    "message": (
                        "JOINER_ROTATE_MATERIAL_PROVIDER must return fresh "
                        "ExtractedMaterial for the emitter"
                    ),
                }
            ),
            503,
        )

    material: ExtractedMaterial = new_plaintext_provider(row)

    actor_sub = _get_actor_sub()
    actor_scope = _get_actor_scope()
    request_id = request.headers.get("X-Request-Id")
    now = datetime.now(timezone.utc)

    # Regression: gh-22. The whole transaction (revoke-mark, persist the
    # new secret, refetch) stays in one run_db() closure -- a penguin-dal
    # transaction/connection checkout is not safe to resume from a
    # different thread hop (see app/db/run_db.py), so this can't be split
    # across multiple run_db() calls. db.transaction()'s own
    # commit-on-clean-exit / rollback-on-exception still applies unchanged,
    # just now running on the worker thread instead of blocking the event
    # loop.
    #
    # Fix-round (gh-22): the `row.revoked_at is not None` check above is a
    # fast-path only -- two concurrent rotate requests on the same js_id
    # can both read revoked_at=NULL before either writes (a run_db()
    # closure boundary is a thread boundary, not a transaction boundary).
    # The *authoritative* guard is this closure's revoke UPDATE: it's
    # conditional on `revoked_at IS NULL` and its rowcount is checked
    # *inside this same transaction*, before persist_extracted_material()
    # runs -- so a losing request never mints a second successor secret
    # for an already-rotated row.
    def _rotate_in_tx() -> tuple[str, Any, Any]:
        with db.transaction() as tx:
            claimed = tx.executesql(
                "UPDATE joiner_secrets SET revoked_at = %s, rotated_at = %s "
                "WHERE id = %s AND revoked_at IS NULL",
                (now, now, str(js_id)),
                return_rowcount=True,
            )
            if not claimed:
                return "already_revoked", None, None

            new_id, audit_event_id, _expires_at = persist_extracted_material(
                tx,
                cluster_id=str(cluster_id),
                tenant_id=tenant_id,
                biome_kind=row.biome_kind,
                emitter_biome_id=row.emitter_biome_id,
                emitter_node_id=row.emitter_node_id,
                extractor_name=row.extractor_name,
                scope=row.scope,
                vault_client=current_app.config["VAULT_CLIENT"],
                material=material,
                actor_sub=actor_sub,
                actor_scope=actor_scope,
                action="joiner.secret.rotate",
                request_id=request_id,
                reason=reason,
            )
            new_row_data = cast(
                "list[dict[str, Any]]",
                tx.executesql(
                    "SELECT * FROM joiner_secrets WHERE id = %s",
                    (new_id,),
                    as_dict=True,
                ),
            )
        return "ok", audit_event_id, new_row_data

    try:
        status, audit_event_id, new_row_data = await run_db(_rotate_in_tx)
        if status == "already_revoked":
            return (
                jsonify(
                    {
                        "error": "already_revoked",
                        "message": "cannot rotate a revoked secret",
                    }
                ),
                409,
            )
    except Exception:
        _metrics.joiner_secret_decryption_failure.inc()
        raise
    finally:
        try:
            zero_bytes(material.plaintext)
        except Exception:  # pragma: no cover - best-effort
            log.debug("zero_bytes(material.plaintext) failed; bytes will be GC'd")

    # Reflect the now-committed revoke onto the in-memory row for the
    # response (same validated row from above; the UPDATE ran inside the
    # transaction that just committed).
    row.revoked_at = now
    row.rotated_at = now
    new_row = Row(new_row_data[0]) if new_row_data else None

    return (
        jsonify(
            {
                "rotated": _serialize_joiner_secret(row),
                "new_secret": _serialize_joiner_secret(new_row),
                "audit_event_id": audit_event_id,
            }
        ),
        201,
    )


@joiner_secrets_bp.route(
    "/clusters/<uuid:cluster_id>/joiner-secrets/<uuid:js_id>", methods=["DELETE"]
)
@require_scopes("gough.cluster.admin")
async def revoke_joiner_secret(cluster_id: uuid.UUID, js_id: uuid.UUID):
    """Revoke a joiner secret (sets ``revoked_at``; never hard-deletes).

    The revoke-mark and the self-audit-log write (via ``_build_audit_writer``
    -> ``AuditEventWriter``) both go through the same penguin-dal overlay
    (``get_db()``) now -- tenant scoping comes from RLS via the ContextVar
    the tenant middleware already set (``app.db.rls``), same as every other
    penguin-dal-backed handler in this module. No explicit GUC call needed.
    """
    tenant_id = _get_tenant_id()
    body = await request.get_json(silent=True) or {}
    reason = body.get("reason")
    if not reason or not isinstance(reason, str) or not reason.strip():
        return (
            jsonify({"error": "missing_reason", "message": "reason is required"}),
            400,
        )

    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_row() -> Any:
        return (
            db(
                (db.joiner_secrets.id == str(js_id))
                & (db.joiner_secrets.cluster_id == str(cluster_id))
                & (db.joiner_secrets.tenant_id == tenant_id)
            )
            .select()
            .first()
        )

    row: Any = await run_db(_fetch_row)
    if row is None:
        return jsonify({"error": "not_found"}), 404
    if row.revoked_at is not None:
        return jsonify({"error": "already_revoked"}), 409

    now = datetime.now(timezone.utc)
    audit_writer = _build_audit_writer(cluster_id)

    # Regression: gh-22. Revoke-mark + self-audit-log write is one unit of
    # work -- stays in one run_db() closure per the house rule (see
    # app/db/run_db.py).
    def _revoke_and_audit() -> None:
        db(db.joiner_secrets.id == str(js_id)).update(revoked_at=now)
        audit_writer.append(
            actor_sub=_get_actor_sub(),
            actor_scope=_get_actor_scope(),
            action="joiner.secret.revoke",
            resource_kind="joiner_secret",
            resource_id=str(row.id),
            tenant_id=tenant_id,
            before={"revoked_at": None},
            after={"revoked_at": now.isoformat(), "reason": reason},
            request_id=request.headers.get("X-Request-Id"),
        )

    await run_db(_revoke_and_audit)
    row.revoked_at = now

    return jsonify({"revoked": _serialize_joiner_secret(row)}), 200


# Re-export for convenience in tests
__all__ = [
    "joiner_secrets_bp",
    "EXPIRING_SOON_WINDOW",
    "DEFAULT_MFA_REQUIRED_LANES",
    "_serialize_joiner_secret",
    "_mfa_required_for_tenant",
]


# Silence unused-import warnings for symbols re-exposed for fixture patching.
_ = (CredentialError, InvalidCredentialError, validate_user_jwt)
