"""Coverage improvement tests for app.api.integrations blueprint.

Focuses on edge cases and error paths not fully covered by test_integrations.py.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


def _passthrough(*dargs, **dkwargs):
    """Decorator passthrough: supports both @decorator and @decorator() styles."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


@pytest.fixture()
def integrations_app(monkeypatch):
    """Create a Quart app with integrations blueprint registered."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough)

    import app.api.integrations as integrations_mod
    integrations_mod = importlib.reload(integrations_mod)

    from quart import Quart, g

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.url_map.strict_slashes = False
    app.register_blueprint(integrations_mod.integrations_bp)

    @app.before_request
    async def _inject_identity():
        g.current_user = {
            "id": 1,
            "email": "admin@test.com",
            "role": "admin"
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return app, integrations_mod


# ============================================================================
# Integration Status Table Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_integration_status_success(integrations_app, monkeypatch):
    """Should return integration status table successfully."""
    app, integrations_mod = integrations_app

    mock_provisioner = MagicMock()
    mock_provisioner.get_status_row = MagicMock(return_value=None)

    monkeypatch.setattr(integrations_mod, "_get_provisioner", lambda: mock_provisioner)

    client = app.test_client()
    response = await client.get("/status")

    assert response.status_code == 200
    data = await response.get_json()
    assert "integrations" in data or "table" in data
    # Response should have some data structure
    assert len(data) > 0


@pytest.mark.asyncio
async def test_get_integration_status_has_products(integrations_app, monkeypatch):
    """Should return status for supported products."""
    app, integrations_mod = integrations_app

    mock_provisioner = MagicMock()
    mock_provisioner.get_status_row = MagicMock(return_value=None)

    monkeypatch.setattr(integrations_mod, "_get_provisioner", lambda: mock_provisioner)

    client = app.test_client()
    response = await client.get("/status")

    assert response.status_code == 200
    data = await response.get_json()

    # Check that response has integrations or table key with list
    integration_list = data.get("integrations") or data.get("table")
    assert isinstance(integration_list, list)
    # Should have at least a few products
    assert len(integration_list) > 0


# ============================================================================
# Get Integration Details Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_integration_unsupported_product(integrations_app):
    """Should return 404 for unsupported product."""
    app, _ = integrations_app

    client = app.test_client()
    response = await client.get("/unknown-product")

    assert response.status_code == 404
    data = await response.get_json()
    assert "error" in data




# ============================================================================
# Credential Masking Tests
# ============================================================================


@pytest.mark.asyncio
async def test_safe_credentials_view_masks_secret(integrations_app):
    """Should mask client_secret in credential views."""
    app, integrations_mod = integrations_app

    from app.workers.integration_provisioner import Credentials

    creds = Credentials(
        product="test-product",
        account_id="account-123",
        client_id="client-abc",
        client_secret="very-long-secret-key-that-should-be-masked",
        scopes=["read", "write"],
        issued_at="2025-01-01T00:00:00Z",
        expires_at="2025-02-01T00:00:00Z"
    )

    view = integrations_mod._safe_credentials_view(creds)

    assert "client_secret_masked" in view
    # Check format: starts with first 4 chars, ends with last 4 chars, has ... in middle
    assert view["client_secret_masked"].startswith("very")
    assert view["client_secret_masked"].endswith("sked")
    assert "..." in view["client_secret_masked"]
    # Full secret should not be in view
    assert "secret-key-that-should" not in str(view)


@pytest.mark.asyncio
async def test_safe_credentials_view_short_secret(integrations_app):
    """Should mask short secrets as ****."""
    app, integrations_mod = integrations_app

    from app.workers.integration_provisioner import Credentials

    creds = Credentials(
        product="test-product",
        account_id="account-123",
        client_id="client-abc",
        client_secret="short",
        scopes=["read"],
        issued_at="2025-01-01T00:00:00Z",
        expires_at="2025-02-01T00:00:00Z"
    )

    view = integrations_mod._safe_credentials_view(creds)

    assert view["client_secret_masked"] == "****"


@pytest.mark.asyncio
async def test_safe_credentials_view_structure(integrations_app):
    """Should include required fields in credential view."""
    app, integrations_mod = integrations_app

    from app.workers.integration_provisioner import Credentials

    creds = Credentials(
        product="test-product",
        account_id="account-123",
        client_id="client-abc",
        client_secret="secret123",
        scopes=["read", "write"],
        issued_at="2025-01-01T00:00:00Z",
        expires_at="2025-02-01T00:00:00Z"
    )

    view = integrations_mod._safe_credentials_view(creds)

    assert view["product"] == "test-product"
    assert view["account_id"] == "account-123"
    assert view["client_id"] == "client-abc"
    assert "client_secret_masked" in view
    assert view["scopes"] == ["read", "write"]
    assert view["issued_at"] == "2025-01-01T00:00:00Z"
    assert view["expires_at"] == "2025-02-01T00:00:00Z"


# ============================================================================
# Product Validation Tests
# ============================================================================


@pytest.mark.asyncio
async def test_validate_product_returns_none_for_valid(integrations_app):
    """Should return None when validating a known product."""
    app, integrations_mod = integrations_app

    # tobogganing is in SUPPORTED_PRODUCTS
    result = integrations_mod._validate_product("tobogganing")
    assert result is None


@pytest.mark.asyncio
async def test_validate_product_checks_all_known_products(integrations_app):
    """Should recognize all officially supported products."""
    app, integrations_mod = integrations_app

    from app.workers.integration_provisioner import SUPPORTED_PRODUCTS

    # Test all supported products return None
    for product in SUPPORTED_PRODUCTS:
        result = integrations_mod._validate_product(product)
        assert result is None, f"Product {product} should be valid but returned {result}"


# ============================================================================
# Integration Row Structure Tests
# ============================================================================


@pytest.mark.asyncio
async def test_integration_status_row_structure(integrations_app):
    """Should create integration status row with required fields."""
    app, integrations_mod = integrations_app

    row = integrations_mod.IntegrationStatusRow(
        product="test-product",
        provider="external",
        status="configured",
        version="1.0.0",
        auth="SPIFFE",
        action_needed="—"
    )

    row_dict = row.to_dict()

    assert row_dict["product"] == "test-product"
    assert row_dict["provider"] == "external"
    assert row_dict["status"] == "configured"
    assert row_dict["version"] == "1.0.0"
    assert row_dict["auth"] == "SPIFFE"
    assert row_dict["action_needed"] == "—"


@pytest.mark.asyncio
async def test_default_integration_rows_exist(integrations_app):
    """Should have default integration rows for known products."""
    app, integrations_mod = integrations_app

    # _DEFAULT_ROWS should contain expected products
    default_rows = integrations_mod._DEFAULT_ROWS

    assert "tobogganing" in default_rows
    assert "squawk" in default_rows
    assert "skauswatch" in default_rows
    assert "waddleai" in default_rows


# ============================================================================
# Unsupported Product Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_integration_unsupported_returns_404(integrations_app):
    """Should return 404 for unknown products."""
    app, _ = integrations_app

    client = app.test_client()
    response = await client.get("/definitely-not-a-product")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_configure_unsupported_product(integrations_app):
    """Should return 404 for unsupported product in configure."""
    app, _ = integrations_app

    client = app.test_client()
    response = await client.post(
        "/unknown-product/configure",
        json={"config": {}}
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rotate_unsupported_product(integrations_app):
    """Should return 404 for unsupported product in rotate."""
    app, _ = integrations_app

    client = app.test_client()
    response = await client.post(
        "/unknown-product/rotate-credentials",
        json={}
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_validate_scope_unsupported_product(integrations_app):
    """Should return 404 for unsupported product in validate."""
    app, _ = integrations_app

    client = app.test_client()
    response = await client.post(
        "/unknown-product/validate-scope",
        json={"scope": "read"}
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_compromise_unsupported_product(integrations_app):
    """Should return 404 for unsupported product in compromise."""
    app, _ = integrations_app

    client = app.test_client()
    response = await client.post(
        "/unknown-product/compromise-response",
        json={"reason": "Test"}
    )

    assert response.status_code == 404
