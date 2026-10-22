"""Vault key management endpoints.

POST /api/v1/vault/rotate-keys — operator-triggered key rotation
"""

from datetime import datetime, timezone
from quart import Blueprint, current_app, jsonify, request
from prometheus_client import Counter

from typing import Any

from ..db.run_db import run_db
from ..middleware import auth_required
from ..models import get_db
from ..security.scope_enforcement import require_scopes

# Prometheus metrics
vault_key_rotation_total = Counter(
    "gough_vault_key_rotation_total",
    "Key rotation events",
    ["rotation_class"],
)

vault_bp = Blueprint("vault", __name__)


@vault_bp.route("/rotate-keys", methods=["POST"])
@auth_required
@require_scopes("gough.cluster.superadmin")
async def rotate_keys():
    """Operator-triggered key rotation endpoint.

    Requires scope: gough.cluster.superadmin (enforced by the decorators
    above -- mirrors every other superadmin-class endpoint in this codebase,
    e.g. ``app.api.primary.force_recover_quorum``/``rotate_ipxe_ca``,
    ``app.api.webhooks``' admin routes: ``@auth_required`` first so an
    unauthenticated caller gets a clean 401 before anything else runs, then
    ``@require_scopes(...)`` for the 403 insufficient-scope case).

    Previously had no decorator at all (a stale ``TODO`` assumed the global
    scope-enforcement middleware alone was sufficient) -- an unauthenticated
    caller reached the handler, and since the (now-fixed) RLS-protected
    query fails closed to 0 rows without a tenant on the request, got a
    misleading ``200 {"rotated": true, "revoked_count": 0}`` instead of
    being rejected. Fixed here with explicit per-route decorators so this
    endpoint is safe regardless of whether the global middleware chain is
    fully wired for a given caller (defense in depth, not a replacement for
    it).

    Request body (JSON):
    {
      "rotation_class": "joiner",  # optional, defaults to "joiner"
      "reason": "scheduled"         # optional, for audit logging
    }

    Response:
    {
      "rotated": true,
      "rotation_class": "joiner",
      "revoked_count": 5
    }
    """
    data = await request.get_json()
    if not data:
        data = {}

    rotation_class = data.get("rotation_class", "joiner").strip()
    reason = data.get("reason", "operator-triggered").strip()

    if not rotation_class:
        return jsonify({"error": "rotation_class is required"}), 400

    # penguin-dal DB for the current request (RLS-wired -- app.models.get_db).
    # joiner_secrets is RLS-protected (baseline migration `rls_tables`, see
    # alembic/versions/20260805_1000_baseline_full_schema.py); this is a
    # request-path handler, so app.middleware's tenant bridge has already
    # pushed the caller's tenant (or the cross-tenant sentinel for a
    # super-admin token) onto the RLS GUC before this handler runs -- no extra
    # scoping needed here (see app.middleware.install_security_middleware).
    db = get_db()
    if db is None:
        return jsonify({"error": "Database not available"}), 503

    try:
        # Revoke all active joiner secrets of the given rotation class.
        #
        # The original raw SQL referenced `revoked` (boolean), `updated_at`,
        # and `used_at` -- none of which exist on `joiner_secrets`
        # (app.models_m1.JoinerSecret): this query has always raised
        # "column does not exist" against real Postgres. Fixed here to the
        # table's actual columns, matching the established revoke pattern
        # used everywhere else this table is written (app.api.joiner_secrets
        # `revoke_secret`/`rotate_secret`, app.grpc_server): `revoked_at`
        # (nullable timestamp; NULL == not revoked) instead of a boolean
        # `revoked` column plus a nonexistent `updated_at`, and
        # `revoked_at IS NULL` instead of a nonexistent `used_at` column for
        # "not already revoked".
        now = datetime.now(timezone.utc)

        # Regression: gh-22. Off the event loop via run_db() instead of
        # blocking the request coroutine inline.
        def _rotate() -> Any:
            return db.executesql(
                "UPDATE joiner_secrets "
                "SET revoked_at = %(now)s "
                "WHERE rotation_class = %(rotation_class)s "
                "AND revoked_at IS NULL "
                "AND expires_at > %(now)s",
                {"rotation_class": rotation_class, "now": now},
                return_rowcount=True,
            )

        revoked_count = await run_db(_rotate)

        # Log audit event via AuditEventWriter if available
        try:
            from ..audit import get_audit_logger
            audit_logger = get_audit_logger()
            if audit_logger:
                audit_logger.log_event(
                    event_type="vault.key_rotation",
                    resource_type="vault",
                    resource_id=rotation_class,
                    action="rotate",
                    status="success",
                    details={
                        "rotation_class": rotation_class,
                        "reason": reason,
                        "revoked_count": revoked_count,
                    },
                )
        except Exception as e:
            current_app.logger.warning("Failed to log audit event: %s", e)

        # Increment Prometheus counter
        vault_key_rotation_total.labels(rotation_class=rotation_class).inc()

        return jsonify({
            "rotated": True,
            "rotation_class": rotation_class,
            "revoked_count": revoked_count,
        }), 200

    except Exception as e:
        current_app.logger.error("Key rotation failed: %s", e)
        return jsonify({"error": "rotation_failed"}), 500
