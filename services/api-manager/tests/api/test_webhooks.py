"""Tests for ``app.api.webhooks`` blueprint.

Covers:
- list/create/delete/test endpoints happy paths, against real Postgres
- webhook signing mode validation (hmac_sha256, ecdsa_p256_sha256, ed25519)
- event filter validation and pattern matching
- retry policy validation
- tenant scoping (cross-tenant denied), including genuine RLS enforcement
- scope enforcement: gough.cluster.admin required for admin endpoints
- JWKS endpoint: anonymous, returns active + grace keys
- response contracts: create returns full endpoint record
- not_found behavior (including non-numeric ids, since `id` is integer)
- test endpoint sends synthetic gough.webhook.test event

DB-touching endpoints (list/create/delete/test) are now backed by
penguin-dal (``app.models.get_db()``) instead of a raw SQLAlchemy
``conn.execute(text(...))`` session, so these run against real Postgres via
the ``pg_db``/``pg_db_scoped`` fixtures rather than a ``MagicMock`` engine.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from quart import Quart, g

from app.api.webhooks import (
    MAX_EVENT_FILTER_PATTERNS,
    MAX_URL_LENGTH,
)
from app.security.webhook_keys import (
    ECDSA_P256_SHA256,
    ED25519,
    HMAC_SHA256,
    WebhookKeyManager,
)
from tests._webhook_fakes import FakeVaultClient


# =============================================================================
# Fixtures / helpers
# =============================================================================


@pytest.fixture
def vault() -> FakeVaultClient:
    return FakeVaultClient()


def _passthrough_decorator(*dargs: Any, **dkwargs: Any) -> Any:
    """Stub that replaces auth_required / require_scopes with no-ops."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def _wrap(fn: Any) -> Any:
        return fn

    return _wrap


def _seed_webhook(dal_db: Any, **overrides: Any) -> int:
    """Insert a real ``webhook_endpoints`` row via penguin-dal; return its id.

    created_at/updated_at have no server-side DEFAULT (see the matching note
    in app.api.webhooks.create_webhook) -- supplied explicitly here too,
    same as any other direct-insert seed helper in this test suite.
    """
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        tenant_id="acme",
        url="https://example.com/hook",
        signing_mode=ED25519,
        active=True,
        event_filter=[],
        retry_policy={"mode": "standard"},
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.webhook_endpoints.insert(**base))


def _build_app(
    *,
    vault_client: FakeVaultClient,
    monkeypatch: pytest.MonkeyPatch,
    tenant: str = "acme",
    dal_db: Any = None,
    rls_scoped: bool = False,
) -> tuple[Quart, Any]:
    """Build an ephemeral Quart app with a freshly-reloaded ``webhooks_bp``.

    ``auth_required``/``require_scopes`` are real decorators bound at
    import/reload time -- monkeypatch them *before* reloading
    ``app.api.webhooks`` so the passthrough actually takes effect (matches
    this file's pre-existing pattern). Returns the reloaded module too, so
    callers can monkeypatch ``get_db`` on the exact module object the
    registered blueprint's routes look up at call time.

    ``rls_scoped=True`` additionally wires ``app.db.rls``'s GUC events onto
    ``dal_db.engine`` and pushes ``tenant`` onto the ``set_current_tenant``
    ContextVar per request, mirroring the real ``tenant_middleware`` -- pass
    a ``pg_db_scoped`` fixture (the non-owner ``api-manager-rw`` role) as
    ``dal_db`` when using this, since ``pg_db`` (the migration/table owner)
    is RLS-exempt and would prove nothing.
    """
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    import app.api.webhooks as webhooks_mod

    webhooks_mod = importlib.reload(webhooks_mod)

    application = Quart(__name__)
    application.config["TESTING"] = True
    application.vault_client = vault_client

    if dal_db is not None:
        monkeypatch.setattr(webhooks_mod, "get_db", lambda: dal_db)

        if rls_scoped:
            from app.db.rls import install_rls_events, set_current_tenant

            install_rls_events(dal_db.engine)

            @application.before_request
            async def _push_rls_tenant() -> None:
                set_current_tenant(tenant)

            @application.after_request
            async def _pop_rls_tenant(response: Any) -> Any:
                set_current_tenant(None)
                return response

    application.register_blueprint(webhooks_mod.webhooks_bp, url_prefix="/api/v1/webhooks")

    @application.before_request
    async def inject_user() -> None:
        g.current_user = {
            "_jwt_payload": {
                "tenant": tenant,
                "scope": "gough.cluster.admin",
                "sub": "user-123",
            }
        }

    return application, webhooks_mod


# =============================================================================
# Test list_webhooks (GET /api/v1/webhooks)
# =============================================================================


@pytest.mark.asyncio
class TestListWebhooks:
    async def test_lists_empty(
        self, pg_db: Any, pg_db_scoped: Any, vault: FakeVaultClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty-result case: no rows for this tenant."""
        _seed_webhook(pg_db, tenant_id="other-tenant")
        app, _ = _build_app(
            vault_client=vault, monkeypatch=monkeypatch,
            dal_db=pg_db_scoped, rls_scoped=True,
        )
        client = app.test_client()
        resp = await client.get("/api/v1/webhooks")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["endpoints"] == []

    async def test_lists_webhooks(
        self, pg_db: Any, pg_db_scoped: Any, vault: FakeVaultClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Filter case: only the caller's tenant's rows are returned."""
        wid = _seed_webhook(
            pg_db, tenant_id="acme", url="https://example.com/hook",
            signing_mode=ED25519, event_filter=["gough.cluster.*"],
            retry_policy={"mode": "standard"},
        )
        _seed_webhook(pg_db, tenant_id="other-tenant")

        app, _ = _build_app(
            vault_client=vault, monkeypatch=monkeypatch,
            dal_db=pg_db_scoped, rls_scoped=True,
        )
        client = app.test_client()
        resp = await client.get("/api/v1/webhooks")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["endpoints"]) == 1
        ep = data["endpoints"][0]
        assert ep["id"] == str(wid)
        assert ep["url"] == "https://example.com/hook"
        assert ep["signing_mode"] == ED25519
        assert ep["event_filter"] == ["gough.cluster.*"]

    async def test_list_rls_isolates_two_tenants(
        self, pg_db: Any, pg_db_scoped: Any, vault: FakeVaultClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end smoke test through the real endpoint + scoped role.

        NOTE: ``list_webhooks`` applies its own app-level ``tenant_id ==``
        filter, so this alone can't distinguish "RLS isolates the rows"
        from "the app-level filter does" -- that distinction is proven by
        the direct-query tests in tests/test_rls_isolation.py
        (``test_webhook_endpoints_*``), which query
        ``pg_db_scoped.webhook_endpoints`` with no tenant_id filter at all.
        This test just confirms the endpoint still works correctly end to
        end against the actual scoped role (RLS enabled, real GUC wiring).
        """
        _seed_webhook(pg_db, tenant_id="tenant-a", url="https://a.example.com/hook")
        _seed_webhook(pg_db, tenant_id="tenant-b", url="https://b.example.com/hook")

        app_a, _ = _build_app(
            vault_client=vault, monkeypatch=monkeypatch, tenant="tenant-a",
            dal_db=pg_db_scoped, rls_scoped=True,
        )
        resp_a = await app_a.test_client().get("/api/v1/webhooks")
        assert resp_a.status_code == 200
        body_a = await resp_a.get_json()
        assert len(body_a["endpoints"]) == 1
        assert body_a["endpoints"][0]["url"] == "https://a.example.com/hook"

        app_b, _ = _build_app(
            vault_client=vault, monkeypatch=monkeypatch, tenant="tenant-b",
            dal_db=pg_db_scoped, rls_scoped=True,
        )
        resp_b = await app_b.test_client().get("/api/v1/webhooks")
        assert resp_b.status_code == 200
        body_b = await resp_b.get_json()
        assert len(body_b["endpoints"]) == 1
        assert body_b["endpoints"][0]["url"] == "https://b.example.com/hook"


# =============================================================================
# Test create_webhook (POST /api/v1/webhooks)
# =============================================================================


@pytest.mark.asyncio
class TestCreateWebhook:
    async def test_creates_webhook(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Valid payload creates webhook and provisions key."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://example.com/hook",
                "signing_mode": ED25519,
                "event_filter": [],
            },
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["signing_mode"] == ED25519
        assert data["url"] == "https://example.com/hook"
        assert data["tenant_id"] == "acme"
        assert data["active"] is True
        # Row genuinely persisted -- independent re-read confirms it.
        row = pg_db(pg_db.webhook_endpoints.id == int(data["id"])).select().first()
        assert row is not None
        assert row.url == "https://example.com/hook"

    async def test_defaults_to_ed25519(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """signing_mode defaults to ed25519 if omitted."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.post(
            "/api/v1/webhooks", json={"url": "https://example.com/hook"},
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["signing_mode"] == ED25519

    async def test_all_signing_modes(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All three modes accepted."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        for mode in [HMAC_SHA256, ECDSA_P256_SHA256, ED25519]:
            resp = await client.post(
                "/api/v1/webhooks",
                json={"url": "https://example.com/hook", "signing_mode": mode},
            )
            assert resp.status_code == 201
            data = await resp.get_json()
            assert data["signing_mode"] == mode

    async def test_invalid_signing_mode(
        self, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid signing_mode rejected before any DB access."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch)
        client = app.test_client()
        resp = await client.post(
            "/api/v1/webhooks",
            json={"url": "https://example.com/hook", "signing_mode": "rsa-2048"},
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "signing_mode must be one of" in data["error"]

    async def test_invalid_url(
        self, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid URL rejected."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch)
        client = app.test_client()
        resp = await client.post("/api/v1/webhooks", json={"url": "not-a-url"})
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "url" in data["error"].lower()

    async def test_url_too_long(
        self, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """URL exceeding max length rejected."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch)
        client = app.test_client()
        resp = await client.post(
            "/api/v1/webhooks",
            json={"url": "https://example.com/" + "x" * MAX_URL_LENGTH},
        )
        assert resp.status_code == 400

    async def test_event_filter_validation(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Valid event filter patterns accepted and persisted."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://example.com/hook",
                "event_filter": ["gough.cluster.*", "gough.biome.created"],
            },
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert set(data["event_filter"]) == {"gough.cluster.*", "gough.biome.created"}

    async def test_event_filter_too_many(
        self, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Event filter exceeding max patterns rejected."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch)
        client = app.test_client()
        resp = await client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://example.com/hook",
                "event_filter": [f"pattern_{i}" for i in range(MAX_EVENT_FILTER_PATTERNS + 1)],
            },
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "too many patterns" in data["error"]

    async def test_invalid_event_filter_pattern(
        self, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid characters in pattern rejected."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch)
        client = app.test_client()
        resp = await client.post(
            "/api/v1/webhooks",
            json={"url": "https://example.com/hook", "event_filter": ["bad@pattern"]},
        )
        assert resp.status_code == 400

    async def test_retry_policy_defaults(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Retry policy defaults to standard mode."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.post(
            "/api/v1/webhooks", json={"url": "https://example.com/hook"},
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["retry_policy"]["mode"] == "standard"

    async def test_retry_policy_none_mode(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Retry policy mode=none accepted."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.post(
            "/api/v1/webhooks",
            json={"url": "https://example.com/hook", "retry_policy": {"mode": "none"}},
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["retry_policy"]["mode"] == "none"


# =============================================================================
# Test delete_webhook (DELETE /api/v1/webhooks/{id})
# =============================================================================


@pytest.mark.asyncio
class TestDeleteWebhook:
    async def test_deletes_webhook(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Valid webhook ID, owned by the caller's tenant, deletes."""
        wid = _seed_webhook(pg_db, tenant_id="acme")
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.delete(f"/api/v1/webhooks/{wid}")
        assert resp.status_code == 204
        assert pg_db(pg_db.webhook_endpoints.id == wid).select().first() is None

    async def test_not_found(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-existent (but numeric) webhook ID returns 404."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.delete("/api/v1/webhooks/999999")
        assert resp.status_code == 404
        data = await resp.get_json()
        assert data["error"] == "not_found"

    async def test_non_numeric_id_returns_not_found(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``id`` is an integer PK -- a non-numeric path segment is a normal
        404, not a 500 (regression guard for the str->int PK conversion)."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.delete("/api/v1/webhooks/not-a-number")
        assert resp.status_code == 404

    async def test_cross_tenant_delete_denied(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Webhook owned by another tenant is not deletable (app-level filter)."""
        wid = _seed_webhook(pg_db, tenant_id="other-tenant")
        app, _ = _build_app(
            vault_client=vault, monkeypatch=monkeypatch, tenant="acme", dal_db=pg_db,
        )
        client = app.test_client()
        resp = await client.delete(f"/api/v1/webhooks/{wid}")
        assert resp.status_code == 404
        # Row must still exist -- only the tenant filter blocked the delete.
        assert pg_db(pg_db.webhook_endpoints.id == wid).select().first() is not None


# =============================================================================
# Test test_webhook (POST /api/v1/webhooks/{id}/test)
# =============================================================================


@pytest.mark.asyncio
class TestTestWebhook:
    async def test_sends_synthetic_event(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test endpoint sends gough.webhook.test event."""
        wid = _seed_webhook(pg_db, tenant_id="acme", url="https://example.com/hook")
        app, webhooks_mod = _build_app(
            vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db,
        )

        from app.workers.webhook_dispatcher import DeliveryResult
        fake_result = DeliveryResult(
            endpoint_id=str(wid),
            event_id=str(uuid.uuid4()),
            delivered=True,
            attempts=1,
            final_status=200,
            final_error=None,
        )

        with patch.object(webhooks_mod, "WebhookDispatcher") as mock_cls:
            mock_dispatcher = AsyncMock()
            mock_dispatcher.dispatch = AsyncMock(return_value=[fake_result])
            mock_dispatcher.aclose = AsyncMock()
            mock_cls.return_value = mock_dispatcher

            client = app.test_client()
            resp = await client.post(f"/api/v1/webhooks/{wid}/test")

        assert resp.status_code == 200
        data = await resp.get_json()
        assert "results" in data
        assert len(data["results"]) > 0

    async def test_not_found(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-existent webhook returns 404."""
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.post("/api/v1/webhooks/999999/test")
        assert resp.status_code == 404

    async def test_non_numeric_id_returns_not_found(
        self, pg_db: Any, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch, dal_db=pg_db)
        client = app.test_client()
        resp = await client.post("/api/v1/webhooks/not-a-number/test")
        assert resp.status_code == 404


# =============================================================================
# Test webhook_jwks (GET /api/v1/webhooks/keys/{tenant})
# =============================================================================


@pytest.mark.asyncio
class TestWebhookJWKS:
    """Anonymous, never touches the DB -- no ``pg_db`` needed."""

    async def test_jwks_anonymous(
        self, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch)
        client = app.test_client()
        async with app.app_context():
            resp = await client.get("/api/v1/webhooks/keys/acme")
            assert resp.status_code == 200
            data = await resp.get_json()
            assert "keys" in data
            assert isinstance(data["keys"], list)

    async def test_jwks_empty_tenant(
        self, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch)
        client = app.test_client()
        async with app.app_context():
            resp = await client.get("/api/v1/webhooks/keys/")
            assert resp.status_code in (200, 400, 404)

    async def test_jwks_includes_ed25519(
        self, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch)
        mgr = WebhookKeyManager(vault)
        mgr.ensure_keys_for_tenant("acme", ED25519)

        client = app.test_client()
        async with app.app_context():
            resp = await client.get("/api/v1/webhooks/keys/acme")
            assert resp.status_code == 200
            data = await resp.get_json()
            keys = data["keys"]
            assert len(keys) > 0
            ed_keys = [k for k in keys if k["kty"] == "OKP" and k["crv"] == "Ed25519"]
            assert len(ed_keys) > 0

    async def test_jwks_includes_ecdsa(
        self, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch)
        mgr = WebhookKeyManager(vault)
        mgr.ensure_keys_for_tenant("acme", ECDSA_P256_SHA256)

        client = app.test_client()
        async with app.app_context():
            resp = await client.get("/api/v1/webhooks/keys/acme")
            assert resp.status_code == 200
            data = await resp.get_json()
            keys = data["keys"]
            ec_keys = [k for k in keys if k["kty"] == "EC" and k["crv"] == "P-256"]
            assert len(ec_keys) > 0

    async def test_jwks_never_includes_hmac(
        self, vault: FakeVaultClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app, _ = _build_app(vault_client=vault, monkeypatch=monkeypatch)
        mgr = WebhookKeyManager(vault)
        mgr.ensure_keys_for_tenant("acme", HMAC_SHA256)

        client = app.test_client()
        async with app.app_context():
            resp = await client.get("/api/v1/webhooks/keys/acme")
            assert resp.status_code == 200
            data = await resp.get_json()
            keys = data["keys"]
            hmac_keys = [k for k in keys if k.get("kty") == "HMAC" or k.get("mode") == HMAC_SHA256]
            assert len(hmac_keys) == 0
