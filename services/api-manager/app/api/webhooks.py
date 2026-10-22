"""Webhook configuration + JWKS API endpoints.

Spec reference (API Surface → Webhooks):

    GET    /api/v1/webhooks                 list endpoints (admin)
    POST   /api/v1/webhooks                 register endpoint (admin)
    DELETE /api/v1/webhooks/{id}            remove endpoint (admin)
    POST   /api/v1/webhooks/{id}/test       send synthetic test event (admin)
    GET    /api/v1/webhooks/keys/{tenant}   anonymous JWKS for verification

All admin endpoints require the ``gough.cluster.admin`` scope and a valid
tenant claim. The JWKS endpoint is anonymous so consumers can verify
asymmetric webhook signatures without authentication, which is also why it
intentionally never returns HMAC secrets.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from quart import Blueprint, current_app, g, jsonify, request

from ..db.run_db import run_db
from ..middleware import auth_required
from ..models import get_db
from ..security.scope_enforcement import require_scopes
from ..security.tenant import (
    TenantClaimMissingError,
    extract_tenant_from_jwt,
)
from ..security.webhook_keys import (
    ECDSA_P256_SHA256,
    ED25519,
    HMAC_SHA256,
    VALID_MODES,
    WebhookKeyManager,
)
from ..workers.webhook_dispatcher import WebhookDispatcher, WebhookEndpoint

log = logging.getLogger(__name__)

webhooks_bp = Blueprint("webhooks", __name__)

DEFAULT_SIGNING_MODE: str = ED25519
MAX_EVENT_FILTER_PATTERNS: int = 64
MAX_URL_LENGTH: int = 2048

_FILTER_PATTERN_RE = re.compile(r"^[a-zA-Z0-9_.\-*]{1,128}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _vault_client() -> Any:
    """Return the application-wide Vault client.

    Tests inject ``current_app.vault_client`` directly; production paths fall
    back to ``hvac.Client(VAULT_ADDR)`` configured by the secrets layer.
    """
    client = getattr(current_app, "vault_client", None)
    if client is not None:
        return client
    raise RuntimeError("vault_client is not configured on the application")


def _key_manager() -> WebhookKeyManager:
    return WebhookKeyManager(_vault_client())


def _parse_webhook_id(webhook_id: str) -> int | None:
    """Parse the ``<webhook_id>`` path segment into the table's integer PK.

    The route accepts any string (no ``<int:...>`` converter, unlike
    ``nodes.py``), so a non-numeric id is a normal "not found" rather than a
    routing 404 -- callers return the same 404 either way.
    """
    try:
        return int(webhook_id)
    except (TypeError, ValueError):
        return None


def _current_tenant() -> str:
    user = g.get("current_user") or {}
    payload = user.get("_jwt_payload", {}) if isinstance(user, dict) else {}
    ctx = extract_tenant_from_jwt(payload)
    return ctx.tenant_id


def _validate_url(url: str) -> tuple[bool, str]:
    if not isinstance(url, str) or not url:
        return False, "url is required"
    if len(url) > MAX_URL_LENGTH:
        return False, "url exceeds maximum length"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "url scheme must be http or https"
    if not parsed.netloc:
        return False, "url must include a host"
    return True, ""


def _validate_event_filter(value: Any) -> tuple[bool, str, list[str]]:
    if value is None:
        return True, "", []
    if not isinstance(value, list):
        return False, "event_filter must be a list", []
    if len(value) > MAX_EVENT_FILTER_PATTERNS:
        return False, "event_filter has too many patterns", []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _FILTER_PATTERN_RE.match(item):
            return False, f"invalid event_filter pattern: {item!r}", []
        out.append(item)
    return True, "", out


def _validate_retry_policy(value: Any) -> tuple[bool, str, dict]:
    """Spec retries are server-mandated; ``retry_policy`` only carries the
    ``mode`` ("standard"|"none") plus optional ``max_attempts`` cap."""
    if value is None:
        return True, "", {"mode": "standard"}
    if not isinstance(value, dict):
        return False, "retry_policy must be an object", {}
    mode = value.get("mode", "standard")
    if mode not in ("standard", "none"):
        return False, "retry_policy.mode must be 'standard' or 'none'", {}
    max_attempts = value.get("max_attempts", 8)
    if not isinstance(max_attempts, int) or not (1 <= max_attempts <= 8):
        return False, "retry_policy.max_attempts must be 1..8", {}
    return True, "", {"mode": mode, "max_attempts": max_attempts}


def _row_to_dict(row: Any) -> dict:
    """Serialize a ``webhook_endpoints`` row (penguin-dal ``Row``) to a JSON-safe dict.

    ``event_filter``/``retry_policy`` are native JSONB columns -- penguin-dal
    reflects them as already-deserialized Python list/dict, not JSON text --
    but the ``isinstance(..., str)`` fallback is kept defensively in case a
    caller ever feeds this a raw ``executesql(as_dict=True)`` row instead.
    """
    ef = row.event_filter
    if isinstance(ef, str):
        try:
            ef = json.loads(ef)
        except Exception:
            ef = []
    rp = row.retry_policy
    if isinstance(rp, str):
        try:
            rp = json.loads(rp)
        except Exception:
            rp = {}
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "url": str(row.url),
        "signing_mode": str(row.signing_mode),
        "active": bool(row.active),
        "event_filter": list(ef or []),
        "retry_policy": dict(rp or {}),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@webhooks_bp.route("", methods=["GET"])
@auth_required
@require_scopes("gough.cluster.admin")
async def list_webhooks():
    """List webhook endpoints for the caller's tenant."""
    tenant_id = _current_tenant()
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch() -> Any:
        return db(db.webhook_endpoints.tenant_id == tenant_id).select(
            orderby=db.webhook_endpoints.id
        )

    rows = await run_db(_fetch)
    return jsonify({"endpoints": [_row_to_dict(r) for r in rows]}), 200


@webhooks_bp.route("", methods=["POST"])
@auth_required
@require_scopes("gough.cluster.admin")
async def create_webhook():
    """Register a webhook endpoint and provision its signing key."""
    tenant_id = _current_tenant()
    body = await request.get_json(silent=True) or {}

    url = body.get("url")
    ok, err = _validate_url(url)
    if not ok:
        return jsonify({"error": err}), 400

    signing_mode = body.get("signing_mode", DEFAULT_SIGNING_MODE)
    if signing_mode not in VALID_MODES:
        return (
            jsonify(
                {
                    "error": (
                        f"signing_mode must be one of "
                        f"{sorted(VALID_MODES)}"
                    )
                }
            ),
            400,
        )

    ok, err, event_filter = _validate_event_filter(body.get("event_filter"))
    if not ok:
        return jsonify({"error": err}), 400

    ok, err, retry_policy = _validate_retry_policy(body.get("retry_policy"))
    if not ok:
        return jsonify({"error": err}), 400

    # Provision Vault key (idempotent).
    try:
        _key_manager().ensure_keys_for_tenant(tenant_id, signing_mode)
    except Exception as exc:
        log.exception("failed to provision webhook signing key")
        return jsonify({"error": f"key_provisioning_failed: {exc}"}), 500

    # created_at/updated_at have no server-side DEFAULT -- app.models_m1's
    # WebhookEndpoint sets them via SQLAlchemy ORM-side `default=`, which
    # only applies on ORM/Core inserts. penguin-dal inserts against the
    # reflected (DDL-only) Table, which has no knowledge of that Python-side
    # default, so it's supplied explicitly here -- same pattern as every
    # other penguin-dal insert of a created_at/updated_at row in this
    # service (see app/api/nodes.py, app/api/biomes.py). Pre-existing gap:
    # the original raw-SQL INSERT never supplied these either, so
    # create_webhook always violated the NOT NULL constraint against real
    # Postgres before this conversion -- fixed here since it's an
    # unambiguous, in-file, one-line correction, not a schema guess.
    now = datetime.now(timezone.utc)
    db = get_db()

    # Regression: gh-22. Insert + the post-insert refetch is one unit of
    # work -- stays in one run_db() closure per the house rule (see
    # app/db/run_db.py).
    def _insert_and_fetch() -> Any:
        new_id = db.webhook_endpoints.insert(
            tenant_id=tenant_id,
            url=url,
            signing_mode=signing_mode,
            active=True,
            event_filter=event_filter,
            retry_policy=retry_policy,
            created_at=now,
            updated_at=now,
        )
        return db(db.webhook_endpoints.id == new_id).select().first()

    row = await run_db(_insert_and_fetch)

    return jsonify(_row_to_dict(row)), 201


@webhooks_bp.route("/<webhook_id>", methods=["DELETE"])
@auth_required
@require_scopes("gough.cluster.admin")
async def delete_webhook(webhook_id: str):
    """Delete a webhook endpoint owned by the caller's tenant."""
    tenant_id = _current_tenant()
    wid = _parse_webhook_id(webhook_id)
    if wid is None:
        return jsonify({"error": "not_found"}), 404
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _delete() -> int:
        return db(
            (db.webhook_endpoints.id == wid) & (db.webhook_endpoints.tenant_id == tenant_id)
        ).delete()

    rowcount = await run_db(_delete)
    if not rowcount:
        return jsonify({"error": "not_found"}), 404
    return ("", 204)


@webhooks_bp.route("/<webhook_id>/test", methods=["POST"])
@auth_required
@require_scopes("gough.cluster.admin")
async def test_webhook(webhook_id: str):
    """Dispatch a synthetic ``gough.webhook.test`` event."""
    tenant_id = _current_tenant()
    wid = _parse_webhook_id(webhook_id)
    if wid is None:
        return jsonify({"error": "not_found"}), 404
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch() -> Any:
        return db(
            (db.webhook_endpoints.id == wid) & (db.webhook_endpoints.tenant_id == tenant_id)
        ).select().first()

    row = await run_db(_fetch)
    if row is None:
        return jsonify({"error": "not_found"}), 404
    record = _row_to_dict(row)

    endpoint = WebhookEndpoint(
        id=record["id"],
        tenant_id=record["tenant_id"],
        url=record["url"],
        signing_mode=record["signing_mode"],
        event_filter=tuple(record["event_filter"]),
        active=record["active"],
    )

    dispatcher = WebhookDispatcher(
        db_session=None,
        vault_client=_vault_client(),
        endpoint_loader=lambda _t: [endpoint],
    )
    try:
        results = await dispatcher.dispatch(
            {
                "type": "gough.webhook.test",
                "tenant_id": tenant_id,
                "data": {"endpoint_id": record["id"], "purpose": "configuration_test"},
            }
        )
    finally:
        await dispatcher.aclose()

    payload = [
        {
            "endpoint_id": r.endpoint_id,
            "event_id": r.event_id,
            "delivered": r.delivered,
            "attempts": r.attempts,
            "final_status": r.final_status,
            "final_error": r.final_error,
        }
        for r in results
    ]
    return jsonify({"results": payload}), 200


@webhooks_bp.route("/keys/<tenant>", methods=["GET"])
async def webhook_jwks(tenant: str):
    """Anonymous JWKS endpoint for consumer-side signature verification.

    Registered under ``ANONYMOUS_PATHS`` so no auth is required. Only
    asymmetric public keys (Ed25519, ECDSA P-256) are returned; HMAC keys
    are intentionally never exposed here.
    """
    if not tenant or len(tenant) > 128:
        return jsonify({"error": "invalid_tenant"}), 400
    try:
        jwks = _key_manager().get_jwks(tenant)
    except Exception:
        log.exception("failed to build JWKS for tenant %s", tenant)
        jwks = {"keys": []}
    resp = jsonify(jwks)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp, 200
