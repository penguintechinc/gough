"""Tests for IntegrationProvisioner worker.

Tests credential provisioning, validation, rotation, and revocation flows.
Per spec "PenguinTech Product Integration Matrix -> Service-account lifecycle".
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.clients.vault import VaultClient, VaultKvReadResponse, VaultError
from app.workers.integration_provisioner import (
    Credentials,
    CredentialMissingError,
    IntegrationError,
    IntegrationProvisioner,
    ProductApiError,
    REQUIRED_SCOPES,
    ScopeValidationResult,
    UnsupportedProductError,
    _check_product,
    _expiry_iso,
    _now_iso,
    _vault_path,
)


@pytest.fixture
def vault_client():
    """Mock Vault client."""
    return MagicMock(spec=VaultClient)


@pytest.fixture
def provisioner(vault_client):
    """Integration provisioner with mocked Vault."""
    return IntegrationProvisioner(
        vault_client=vault_client,
        cluster_id="test-cluster",
        request_timeout_seconds=5.0,
    )


@pytest.fixture
def sample_creds():
    return Credentials(
        product="tobogganing",
        account_id="acct-123",
        client_id="client-456",
        client_secret="secret-789",
        scopes=["tobogganing.tunnel.write", "tobogganing.health.read"],
        issued_at=_now_iso(),
        expires_at=_expiry_iso(),
    )


# ---------- Utility tests ---------------------------------------------------


class TestCheckProduct:
    def test_valid_products(self):
        """_check_product accepts supported products."""
        for product in ["tobogganing", "squawk", "skauswatch", "waddleai", "nest", "license-server", "waddlebot"]:
            _check_product(product)  # Should not raise

    def test_invalid_product(self):
        """_check_product rejects unknown products."""
        with pytest.raises(UnsupportedProductError):
            _check_product("unknown-product")


class TestVaultPath:
    def test_primary_slot(self):
        path = _vault_path("cluster-x", "tobogganing", "primary")
        assert path == "gough/cluster-x/integrations/tobogganing/primary"

    def test_secondary_slot(self):
        path = _vault_path("cluster-x", "tobogganing", "secondary")
        assert path == "gough/cluster-x/integrations/tobogganing/secondary"

    def test_invalid_slot(self):
        with pytest.raises(ValueError):
            _vault_path("cluster-x", "tobogganing", "tertiary")


class TestIsoHelpers:
    def test_now_iso_format(self):
        ts = _now_iso()
        assert ts.endswith("Z")
        assert "T" in ts

    def test_expiry_iso_format(self):
        ts = _expiry_iso(90 * 24 * 60 * 60)  # 90 days
        assert ts.endswith("Z")
        assert "T" in ts


# ---------- Credentials storage tests ----------------------------------------


class TestCredentialsStorage:
    def test_read_credential_exists(self, provisioner, vault_client, sample_creds):
        """Read existing credential from Vault."""
        vault_client.kv_read.return_value = VaultKvReadResponse(
            data=sample_creds.to_vault(),
            metadata={"version": 1},
        )
        creds = provisioner._read_credential("tobogganing", "primary")
        assert creds is not None
        assert creds.client_id == "client-456"

    def test_read_credential_missing(self, provisioner, vault_client):
        """Read missing credential returns None."""
        vault_client.kv_read.return_value = VaultKvReadResponse(data={}, metadata={})
        creds = provisioner._read_credential("tobogganing", "primary")
        assert creds is None

    def test_read_credential_vault_error(self, provisioner, vault_client):
        """Read credential with Vault error returns None."""
        vault_client.kv_read.side_effect = VaultError("Vault sealed")
        creds = provisioner._read_credential("tobogganing", "primary")
        assert creds is None

    def test_write_credential(self, provisioner, vault_client, sample_creds):
        """Write credential to Vault."""
        provisioner._write_credential("tobogganing", sample_creds, "primary")
        vault_client.kv_write.assert_called_once()
        call_args = vault_client.kv_write.call_args
        assert "tobogganing" in call_args[0][0]  # path
        assert call_args[0][1]["client_id"] == "client-456"


# ---------- Service account provisioning tests --------------------------------


class TestEnsureServiceAccount:
    @pytest.mark.asyncio
    async def test_existing_credentials(self, provisioner, vault_client, sample_creds):
        """ensure_service_account returns existing credentials."""
        vault_client.kv_read.return_value = VaultKvReadResponse(
            data=sample_creds.to_vault(),
            metadata={"version": 1},
        )
        creds = await provisioner.ensure_service_account("tobogganing")
        assert creds.client_id == "client-456"
        # Should not call mint endpoint
        assert not vault_client.kv_write.called

    @pytest.mark.asyncio
    async def test_provision_new_account(self, provisioner, vault_client):
        """ensure_service_account provisions new account when missing."""
        # Existing creds missing
        vault_client.kv_read.return_value = VaultKvReadResponse(data={}, metadata={})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "account_id": "acct-new",
            "client_id": "client-new",
            "client_secret": "secret-new",
            "scopes": list(REQUIRED_SCOPES["tobogganing"]),
            "issued_at": _now_iso(),
            "expires_at": _expiry_iso(),
        }

        with patch(
            "app.workers.integration_provisioner._load_endpoint"
        ) as mock_endpoint:
            with patch("httpx.AsyncClient") as mock_client_factory:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_factory.return_value = mock_client

                endpoint = MagicMock()
                endpoint.base_url = "https://tobogganing.cluster.svc"
                endpoint.admin_token = "admin-token"
                endpoint.provision_path = "/api/v1/admin/service-accounts"
                mock_endpoint.return_value = endpoint

                creds = await provisioner.ensure_service_account("tobogganing")
                assert creds.account_id == "acct-new"
                assert vault_client.kv_write.called


# ---------- Scope validation tests -------------------------------------------


class TestValidateScope:
    @pytest.mark.asyncio
    async def test_validate_scope_success(self, provisioner, vault_client, sample_creds):
        """Validate scope returns valid result."""
        vault_client.kv_read.return_value = VaultKvReadResponse(
            data=sample_creds.to_vault(),
            metadata={"version": 1},
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "active": True,
            "scopes": list(REQUIRED_SCOPES["tobogganing"]),
            "expires_at": _expiry_iso(),
        }

        with patch(
            "app.workers.integration_provisioner._load_endpoint"
        ) as mock_endpoint:
            with patch("httpx.AsyncClient") as mock_client_factory:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_factory.return_value = mock_client

                endpoint = MagicMock()
                endpoint.base_url = "https://tobogganing.cluster.svc"
                endpoint.admin_token = "admin-token"
                endpoint.introspect_path = "/api/v1/admin/service-accounts/introspect"
                mock_endpoint.return_value = endpoint

                result = await provisioner.validate_scope("tobogganing")
                assert result.valid is True
                assert result.missing_scopes == []

    @pytest.mark.asyncio
    async def test_validate_scope_missing_scope(
        self, provisioner, vault_client, sample_creds
    ):
        """Validate scope with missing scope."""
        vault_client.kv_read.return_value = VaultKvReadResponse(
            data=sample_creds.to_vault(),
            metadata={"version": 1},
        )

        # Only grant one scope, missing the second
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "active": True,
            "scopes": ["tobogganing.tunnel.write"],  # Missing health.read
            "expires_at": _expiry_iso(),
        }

        with patch(
            "app.workers.integration_provisioner._load_endpoint"
        ) as mock_endpoint:
            with patch("httpx.AsyncClient") as mock_client_factory:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_factory.return_value = mock_client

                endpoint = MagicMock()
                endpoint.base_url = "https://tobogganing.cluster.svc"
                endpoint.admin_token = "admin-token"
                endpoint.introspect_path = "/api/v1/admin/service-accounts/introspect"
                mock_endpoint.return_value = endpoint

                result = await provisioner.validate_scope("tobogganing")
                assert result.valid is False
                assert len(result.missing_scopes) > 0

    @pytest.mark.asyncio
    async def test_validate_scope_no_credentials(self, provisioner, vault_client):
        """Validate scope with no credentials raises error."""
        vault_client.kv_read.return_value = VaultKvReadResponse(data={}, metadata={})
        with pytest.raises(CredentialMissingError):
            await provisioner.validate_scope("tobogganing")


# ---------- Rotation tests (dual-write) ----------------------------------------


class TestRotateCredentials:
    @pytest.mark.asyncio
    async def test_rotate_success(self, provisioner, vault_client, sample_creds):
        """Rotate credentials successfully."""
        old_creds = sample_creds
        new_creds = Credentials(
            product="tobogganing",
            account_id="acct-new",
            client_id="client-new",
            client_secret="secret-new",
            scopes=sample_creds.scopes,
            issued_at=_now_iso(),
            expires_at=_expiry_iso(),
        )

        # Setup vault reads/writes
        read_count = 0
        def vault_read_side_effect(path):
            nonlocal read_count
            read_count += 1
            # First call: read existing primary (for rotation check)
            if read_count == 1 and "primary" in path:
                return VaultKvReadResponse(
                    data=old_creds.to_vault(),
                    metadata={"version": 1},
                )
            # Subsequent calls return what we write
            return VaultKvReadResponse(data=None, metadata={})

        vault_client.kv_read.side_effect = vault_read_side_effect

        # Mock HTTP responses for provision and validate
        provision_response = MagicMock()
        provision_response.status_code = 200
        provision_response.json.return_value = new_creds.to_vault()

        validate_response = MagicMock()
        validate_response.status_code = 200
        validate_response.json.return_value = {
            "active": True,
            "scopes": list(new_creds.scopes),
            "expires_at": _expiry_iso(),
        }

        revoke_response = MagicMock()
        revoke_response.status_code = 204

        with patch(
            "app.workers.integration_provisioner._load_endpoint"
        ) as mock_endpoint:
            with patch("httpx.AsyncClient") as mock_client_factory:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(
                    side_effect=[provision_response, validate_response]
                )
                mock_client.delete = AsyncMock(return_value=revoke_response)
                mock_client_factory.return_value = mock_client

                endpoint = MagicMock()
                endpoint.base_url = "https://tobogganing.cluster.svc"
                endpoint.admin_token = "admin-token"
                endpoint.provision_path = "/api/v1/admin/service-accounts"
                endpoint.introspect_path = "/api/v1/admin/service-accounts/introspect"
                endpoint.revoke_path = MagicMock(
                    return_value=f"/api/v1/admin/service-accounts/{old_creds.account_id}"
                )
                mock_endpoint.return_value = endpoint

                result = await provisioner.rotate("tobogganing")
                assert result.account_id == "acct-new"
                assert result.client_id == "client-new"


# ---------- Compromise response tests ----------------------------------------


class TestCompromiseResponse:
    @pytest.mark.asyncio
    async def test_compromise_response(self, provisioner, vault_client, sample_creds):
        """Compromise response revokes and provisions new account."""
        vault_client.kv_read.return_value = VaultKvReadResponse(
            data=sample_creds.to_vault(),
            metadata={"version": 1},
        )

        new_creds = Credentials(
            product="tobogganing",
            account_id="acct-emergency",
            client_id="client-emergency",
            client_secret="secret-emergency",
            scopes=sample_creds.scopes,
            issued_at=_now_iso(),
            expires_at=_expiry_iso(),
        )

        provision_response = MagicMock()
        provision_response.status_code = 200
        provision_response.json.return_value = new_creds.to_vault()

        revoke_response = MagicMock()
        revoke_response.status_code = 204

        with patch(
            "app.workers.integration_provisioner._load_endpoint"
        ) as mock_endpoint:
            with patch("httpx.AsyncClient") as mock_client_factory:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=provision_response)
                mock_client.delete = AsyncMock(return_value=revoke_response)
                mock_client_factory.return_value = mock_client

                endpoint = MagicMock()
                endpoint.base_url = "https://tobogganing.cluster.svc"
                endpoint.admin_token = "admin-token"
                endpoint.provision_path = "/api/v1/admin/service-accounts"
                endpoint.revoke_path = MagicMock(
                    return_value=f"/api/v1/admin/service-accounts/{sample_creds.account_id}"
                )
                mock_endpoint.return_value = endpoint

                result = await provisioner.compromise_response("tobogganing")
                assert result.account_id == "acct-emergency"
