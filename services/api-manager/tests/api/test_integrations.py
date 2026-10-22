"""Tests for the Integrations API Blueprint.

Tests the REST endpoints for integration configuration, validation, rotation,
and compromise response, per spec "API Surface -> Integrations".
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.integrations import SUPPORTED_PRODUCTS, integrations_bp
from app.workers.integration_provisioner import (
    Credentials,
    CredentialMissingError,
    IntegrationError,
    ScopeValidationResult,
)


@pytest.fixture
def app_with_integrations(app):
    """Quart app with the integrations blueprint registered.

    The module-level ``integrations_bp`` imported at the top of this file bound
    the real auth/scope decorators when it was first imported, before the
    ``app`` fixture stubbed them -- so its routes would still run genuine scope
    enforcement and 403. Reload here, after the stubs are installed, so the
    registered blueprint is the one carrying the passthroughs.
    """
    import importlib

    import app.api.integrations as integrations_mod

    integrations_mod = importlib.reload(integrations_mod)
    app.register_blueprint(
        integrations_mod.integrations_bp, url_prefix="/api/v1/integrations"
    )
    return app


@pytest.fixture
def client(app_with_integrations):
    return app_with_integrations.test_client()


@pytest.fixture
def auth_headers():
    """Bearer token headers."""
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def sample_creds():
    return Credentials(
        product="tobogganing",
        account_id="acct-123",
        client_id="client-456",
        client_secret="secret-789",
        scopes=["tobogganing.tunnel.write", "tobogganing.health.read"],
        issued_at="2026-04-28T00:00:00Z",
        expires_at="2026-07-27T00:00:00Z",
    )


class TestGetIntegrationStatus:
    """GET /api/v1/integrations/status"""

    @pytest.mark.asyncio
    async def test_status_returns_all_products(self, client, auth_headers):
        """Status endpoint returns integration rows for all products."""
        with patch(
            "app.api.integrations._get_provisioner"
        ) as mock_prov_factory:
            prov = AsyncMock()
            prov._read_credential = MagicMock(return_value=None)
            mock_prov_factory.return_value = prov

            resp = await client.get("/api/v1/integrations/status", headers=auth_headers)
            assert resp.status_code == 200
            data = await resp.get_json()
            assert "integrations" in data
            integrations = data["integrations"]
            assert len(integrations) == len(SUPPORTED_PRODUCTS)

            for integ in integrations:
                assert "product" in integ
                assert "status" in integ
                assert integ["product"] in SUPPORTED_PRODUCTS


class TestGetProductConfig:
    """GET /api/v1/integrations/{product}"""

    @pytest.mark.asyncio
    async def test_get_known_product(self, client, auth_headers, sample_creds):
        """Get config for a known product."""
        with patch(
            "app.api.integrations._get_provisioner"
        ) as mock_prov_factory:
            prov = AsyncMock()
            prov._read_credential = MagicMock(return_value=sample_creds)
            mock_prov_factory.return_value = prov

            resp = await client.get(
                "/api/v1/integrations/tobogganing", headers=auth_headers
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data["product"] == "tobogganing"
            assert data["configured"] is True
            assert "required_scopes" in data

    @pytest.mark.asyncio
    async def test_get_unknown_product(self, client, auth_headers):
        """Get config for unknown product returns 404."""
        resp = await client.get(
            "/api/v1/integrations/unknown-product", headers=auth_headers
        )
        assert resp.status_code == 404
        data = await resp.get_json()
        assert "error" in data


class TestConfigureIntegration:
    """POST /api/v1/integrations/{product}/configure"""

    @pytest.mark.asyncio
    async def test_configure_unknown_product(self, client, auth_headers):
        """Configure unknown product returns 404."""
        resp = await client.post(
            "/api/v1/integrations/unknown/configure",
            headers=auth_headers,
            json={},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_configure_success(self, client, auth_headers, sample_creds):
        """Configure integration successfully provisions service account."""
        with patch(
            "app.api.integrations._get_provisioner"
        ) as mock_prov_factory:
            prov = AsyncMock()
            prov.ensure_service_account = AsyncMock(return_value=sample_creds)
            mock_prov_factory.return_value = prov

            resp = await client.post(
                "/api/v1/integrations/tobogganing/configure",
                headers=auth_headers,
                json={},
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data["product"] == "tobogganing"
            assert data["configured"] is True

    @pytest.mark.asyncio
    async def test_configure_api_error(self, client, auth_headers):
        """Configure with API error returns 500."""
        with patch(
            "app.api.integrations._get_provisioner"
        ) as mock_prov_factory:
            prov = AsyncMock()
            prov.ensure_service_account = AsyncMock(
                side_effect=IntegrationError("API unreachable")
            )
            mock_prov_factory.return_value = prov

            resp = await client.post(
                "/api/v1/integrations/tobogganing/configure",
                headers=auth_headers,
                json={},
            )
            assert resp.status_code == 500
            data = await resp.get_json()
            assert "error" in data


class TestRotateCredentials:
    """POST /api/v1/integrations/{product}/rotate-credentials"""

    @pytest.mark.asyncio
    async def test_rotate_success(self, client, auth_headers, sample_creds):
        """Rotate credentials successfully returns new credentials."""
        new_creds = Credentials(
            product="tobogganing",
            account_id="acct-new",
            client_id="client-new",
            client_secret="secret-new",
            scopes=sample_creds.scopes,
            issued_at="2026-05-28T00:00:00Z",
            expires_at="2026-08-27T00:00:00Z",
        )
        with patch(
            "app.api.integrations._get_provisioner"
        ) as mock_prov_factory:
            prov = AsyncMock()
            prov.rotate = AsyncMock(return_value=new_creds)
            mock_prov_factory.return_value = prov

            resp = await client.post(
                "/api/v1/integrations/tobogganing/rotate-credentials",
                headers=auth_headers,
                json={},
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data["product"] == "tobogganing"
            assert data["rotated"] is True

    @pytest.mark.asyncio
    async def test_rotate_no_credentials(self, client, auth_headers):
        """Rotate with no existing credentials returns 404."""
        with patch(
            "app.api.integrations._get_provisioner"
        ) as mock_prov_factory:
            prov = AsyncMock()
            prov.rotate = AsyncMock(
                side_effect=CredentialMissingError("No credentials")
            )
            mock_prov_factory.return_value = prov

            resp = await client.post(
                "/api/v1/integrations/tobogganing/rotate-credentials",
                headers=auth_headers,
                json={},
            )
            assert resp.status_code == 404


class TestCompromiseResponse:
    """POST /api/v1/integrations/{product}/compromise-response"""

    @pytest.mark.asyncio
    async def test_compromise_success(self, client, auth_headers, sample_creds):
        """Compromise response revokes and provisions new account."""
        new_creds = Credentials(
            product="tobogganing",
            account_id="acct-new-compromise",
            client_id="client-compromise",
            client_secret="secret-compromise",
            scopes=sample_creds.scopes,
            issued_at="2026-05-28T00:00:00Z",
            expires_at="2026-08-27T00:00:00Z",
        )
        with patch(
            "app.api.integrations._get_provisioner"
        ) as mock_prov_factory:
            prov = AsyncMock()
            prov.compromise_response = AsyncMock(return_value=new_creds)
            mock_prov_factory.return_value = prov

            resp = await client.post(
                "/api/v1/integrations/tobogganing/compromise-response",
                headers=auth_headers,
                json={},
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data["product"] == "tobogganing"
            assert data["revoked"] is True


class TestValidateScope:
    """POST /api/v1/integrations/{product}/validate-scope"""

    @pytest.mark.asyncio
    async def test_validate_success(self, client, auth_headers):
        """Validate scope returns validation result."""
        result = ScopeValidationResult(
            product="tobogganing",
            valid=True,
            granted_scopes=["tobogganing.tunnel.write", "tobogganing.health.read"],
            missing_scopes=[],
            expires_at="2026-07-27T00:00:00Z",
        )
        with patch(
            "app.api.integrations._get_provisioner"
        ) as mock_prov_factory:
            prov = AsyncMock()
            prov.validate_scope = AsyncMock(return_value=result)
            mock_prov_factory.return_value = prov

            resp = await client.post(
                "/api/v1/integrations/tobogganing/validate-scope",
                headers=auth_headers,
                json={},
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data["product"] == "tobogganing"
            assert data["valid"] is True
            assert "missing_scopes" in data

    @pytest.mark.asyncio
    async def test_validate_missing_scope(self, client, auth_headers):
        """Validate scope with missing scope."""
        result = ScopeValidationResult(
            product="tobogganing",
            valid=False,
            granted_scopes=["tobogganing.tunnel.write"],
            missing_scopes=["tobogganing.health.read"],
            expires_at="2026-07-27T00:00:00Z",
        )
        with patch(
            "app.api.integrations._get_provisioner"
        ) as mock_prov_factory:
            prov = AsyncMock()
            prov.validate_scope = AsyncMock(return_value=result)
            mock_prov_factory.return_value = prov

            resp = await client.post(
                "/api/v1/integrations/tobogganing/validate-scope",
                headers=auth_headers,
                json={},
            )
            assert resp.status_code == 200
            data = await resp.get_json()
            assert data["valid"] is False
            assert len(data["missing_scopes"]) > 0
