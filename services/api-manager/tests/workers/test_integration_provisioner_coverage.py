"""Extended coverage tests for integration_provisioner.py missed lines."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import httpx

from app.workers.integration_provisioner import (
    IntegrationProvisioner,
    Credentials,
    UnsupportedProductError,
    ProductApiError,
    CredentialMissingError,
    ScopeValidationResult,
    _check_product,
    _vault_path,
    _load_endpoint,
)
from app.clients.vault import VaultError


class TestIntegrationProvisionerHelpers:
    """Test helper functions."""

    def test_check_product_unsupported(self):
        """_check_product raises for unsupported product."""
        with pytest.raises(UnsupportedProductError):
            _check_product("unknown-product")

    def test_check_product_supported(self):
        """_check_product succeeds for supported products."""
        # Should not raise
        _check_product("tobogganing")
        _check_product("squawk")
        _check_product("skauswatch")

    def test_vault_path_invalid_slot(self):
        """_vault_path raises for invalid slot."""
        with pytest.raises(ValueError, match="slot must be"):
            _vault_path("cluster-123", "tobogganing", "invalid_slot")

    def test_vault_path_primary(self):
        """_vault_path constructs correct primary path."""
        path = _vault_path("cluster-123", "tobogganing", "primary")
        assert path == "gough/cluster-123/integrations/tobogganing/primary"

    def test_vault_path_secondary(self):
        """_vault_path constructs correct secondary path."""
        path = _vault_path("cluster-123", "squawk", "secondary")
        assert path == "gough/cluster-123/integrations/squawk/secondary"

    def test_load_endpoint_missing_url(self):
        """_load_endpoint raises when URL env var missing."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(Exception):  # Catches IntegrationError
                _load_endpoint("tobogganing")

    def test_load_endpoint_missing_token(self):
        """_load_endpoint raises when token env var missing."""
        with patch.dict("os.environ", {"INTEGRATION_TOBOGGANING_URL": "http://localhost:8080"}):
            with pytest.raises(Exception):  # Catches IntegrationError
                _load_endpoint("tobogganing")

    def test_load_endpoint_success(self):
        """_load_endpoint constructs endpoint from env vars."""
        env = {
            "INTEGRATION_TOBOGGANING_URL": "http://localhost:8080",
            "INTEGRATION_TOBOGGANING_ADMIN_TOKEN": "token123",
        }
        with patch.dict("os.environ", env):
            endpoint = _load_endpoint("tobogganing")
            assert endpoint.base_url == "http://localhost:8080"
            assert endpoint.admin_token == "token123"

    def test_load_endpoint_hyphenated_product(self):
        """_load_endpoint handles hyphenated product names (env var conversion)."""
        env = {
            "INTEGRATION_LICENSE_SERVER_URL": "http://license:8080",
            "INTEGRATION_LICENSE_SERVER_ADMIN_TOKEN": "token123",
        }
        with patch.dict("os.environ", env):
            endpoint = _load_endpoint("license-server")
            assert endpoint.base_url == "http://license:8080"


class TestCredentials:
    """Test Credentials dataclass."""

    def test_credentials_to_vault(self):
        """Credentials.to_vault returns dict."""
        creds = Credentials(
            product="tobogganing",
            account_id="acc123",
            client_id="cid123",
            client_secret="secret123",
            scopes=["read", "write"],
            issued_at="2025-01-01T00:00:00Z",
            expires_at="2026-01-01T00:00:00Z",
        )
        vault_dict = creds.to_vault()
        assert vault_dict["account_id"] == "acc123"
        assert vault_dict["product"] == "tobogganing"

    def test_credentials_from_vault_missing_fields(self):
        """Credentials.from_vault handles missing fields."""
        creds = Credentials.from_vault("tobogganing", {})
        assert creds.product == "tobogganing"
        assert creds.account_id == ""
        assert creds.client_id == ""
        assert creds.client_secret == ""
        assert creds.scopes == []

    def test_credentials_from_vault_with_data(self):
        """Credentials.from_vault populates from dict."""
        data = {
            "account_id": "acc123",
            "client_id": "cid123",
            "client_secret": "secret123",
            "scopes": ["read", "write"],
        }
        creds = Credentials.from_vault("squawk", data)
        assert creds.account_id == "acc123"
        assert creds.scopes == ["read", "write"]


class TestScopeValidationResult:
    """Test ScopeValidationResult."""

    def test_scope_validation_result_to_dict(self):
        """ScopeValidationResult.to_dict returns proper dict."""
        result = ScopeValidationResult(
            product="tobogganing",
            valid=True,
            granted_scopes=["read", "write"],
            missing_scopes=[],
            expires_at="2026-01-01T00:00:00Z",
        )
        d = result.to_dict()
        assert d["product"] == "tobogganing"
        assert d["valid"] is True
        assert d["missing_scopes"] == []


@pytest.mark.asyncio
class TestIntegrationProvisionerEnsureServiceAccount:
    """Test ensure_service_account flow."""

    @pytest.mark.asyncio
    async def test_ensure_service_account_existing_primary(self):
        """ensure_service_account returns existing primary credentials."""
        mock_vault = MagicMock()
        existing_creds = Credentials(
            product="tobogganing",
            account_id="existing123",
            client_id="cid123",
            client_secret="secret123",
        )
        mock_vault.kv_read.return_value.data = existing_creds.to_vault()

        provisioner = IntegrationProvisioner(mock_vault, cluster_id="test-cluster")

        with patch.object(provisioner, "_read_credential", return_value=existing_creds):
            result = await provisioner.ensure_service_account("tobogganing")
            assert result.account_id == "existing123"

    @pytest.mark.asyncio
    async def test_ensure_service_account_provision_new(self):
        """ensure_service_account mints new credentials when primary missing."""
        mock_vault = MagicMock()
        provisioner = IntegrationProvisioner(mock_vault, cluster_id="test-cluster")

        new_creds = Credentials(
            product="tobogganing",
            account_id="new123",
            client_id="new_cid",
            client_secret="new_secret",
        )

        with patch.object(provisioner, "_read_credential", return_value=None):
            with patch.object(provisioner, "_mint_service_account", return_value=new_creds):
                with patch.object(provisioner, "_write_credential"):
                    result = await provisioner.ensure_service_account("tobogganing")
                    assert result.account_id == "new123"

    @pytest.mark.asyncio
    async def test_ensure_service_account_unsupported_product(self):
        """ensure_service_account raises for unsupported product."""
        mock_vault = MagicMock()
        provisioner = IntegrationProvisioner(mock_vault, cluster_id="test-cluster")

        with pytest.raises(UnsupportedProductError):
            await provisioner.ensure_service_account("unsupported-product")


@pytest.mark.asyncio
class TestIntegrationProvisionerValidateScope:
    """Test validate_scope."""

    @pytest.mark.asyncio
    async def test_validate_scope_missing_credentials(self):
        """validate_scope raises when credentials missing."""
        mock_vault = MagicMock()
        provisioner = IntegrationProvisioner(mock_vault, cluster_id="test-cluster")

        with patch.object(provisioner, "_read_credential", return_value=None):
            with pytest.raises(CredentialMissingError):
                await provisioner.validate_scope("tobogganing")

    @pytest.mark.asyncio
    async def test_validate_scope_http_error(self):
        """validate_scope raises on HTTP error."""
        mock_vault = MagicMock()
        mock_http = AsyncMock()
        mock_http.post.side_effect = httpx.RequestError("Connection failed")

        provisioner = IntegrationProvisioner(
            mock_vault,
            cluster_id="test-cluster",
            http_client=mock_http,
        )

        creds = Credentials(
            product="tobogganing",
            account_id="acc123",
            client_id="cid123",
            client_secret="secret123",
        )

        with patch.object(provisioner, "_read_credential", return_value=creds):
            with patch("app.workers.integration_provisioner._load_endpoint"):
                with pytest.raises(ProductApiError):
                    await provisioner.validate_scope("tobogganing")

    @pytest.mark.asyncio
    async def test_validate_scope_non_2xx_status(self):
        """validate_scope raises on non-2xx response."""
        mock_vault = MagicMock()
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Server error"
        mock_http.post.return_value = mock_response

        provisioner = IntegrationProvisioner(
            mock_vault,
            cluster_id="test-cluster",
            http_client=mock_http,
        )

        creds = Credentials(
            product="tobogganing",
            account_id="acc123",
            client_id="cid123",
            client_secret="secret123",
        )

        with patch.object(provisioner, "_read_credential", return_value=creds):
            with patch("app.workers.integration_provisioner._load_endpoint") as mock_load:
                from app.workers.integration_provisioner import ProductEndpoint

                mock_load.return_value = ProductEndpoint(
                    base_url="http://test:8080",
                    admin_token="token",
                )

                with pytest.raises(ProductApiError, match="500"):
                    await provisioner.validate_scope("tobogganing")


@pytest.mark.asyncio
class TestIntegrationProvisionerRotate:
    """Test credential rotation."""

    @pytest.mark.asyncio
    async def test_rotate_validation_fails_rolls_back(self):
        """rotate rolls back and raises when secondary validation fails."""
        mock_vault = MagicMock()
        provisioner = IntegrationProvisioner(mock_vault, cluster_id="test-cluster")

        old_creds = Credentials(
            product="tobogganing",
            account_id="old123",
            client_id="old_cid",
            client_secret="old_secret",
        )
        new_creds = Credentials(
            product="tobogganing",
            account_id="new123",
            client_id="new_cid",
            client_secret="new_secret",
        )
        failed_validation = ScopeValidationResult(
            product="tobogganing",
            valid=False,
            granted_scopes=["read"],
            missing_scopes=["write"],
        )

        with patch.object(provisioner, "_read_credential", return_value=old_creds):
            with patch.object(provisioner, "_mint_service_account", return_value=new_creds):
                with patch.object(provisioner, "_write_credential"):
                    with patch.object(provisioner, "_delete_credential"):
                        with patch.object(
                            provisioner,
                            "validate_scope",
                            return_value=failed_validation,
                        ):
                            with patch.object(provisioner, "_revoke_on_product"):
                                with pytest.raises(ProductApiError, match="Scope validation failed"):
                                    await provisioner.rotate("tobogganing")

    @pytest.mark.asyncio
    async def test_rotate_successful(self):
        """rotate completes all steps successfully."""
        mock_vault = MagicMock()
        provisioner = IntegrationProvisioner(mock_vault, cluster_id="test-cluster")

        old_creds = Credentials(
            product="tobogganing",
            account_id="old123",
            client_id="old_cid",
            client_secret="old_secret",
        )
        new_creds = Credentials(
            product="tobogganing",
            account_id="new123",
            client_id="new_cid",
            client_secret="new_secret",
        )
        valid_validation = ScopeValidationResult(
            product="tobogganing",
            valid=True,
            granted_scopes=["read", "write"],
            missing_scopes=[],
        )

        with patch.object(provisioner, "_read_credential", return_value=old_creds):
            with patch.object(provisioner, "_mint_service_account", return_value=new_creds):
                with patch.object(provisioner, "_write_credential") as mock_write:
                    with patch.object(provisioner, "_delete_credential") as mock_delete:
                        with patch.object(
                            provisioner,
                            "validate_scope",
                            return_value=valid_validation,
                        ):
                            with patch.object(provisioner, "_revoke_on_product"):
                                result = await provisioner.rotate("tobogganing")
                                assert result.account_id == "new123"


@pytest.mark.asyncio
class TestIntegrationProvisionerCompromiseResponse:
    """Test compromise response."""

    @pytest.mark.asyncio
    async def test_compromise_response_revokes_old(self):
        """compromise_response revokes old credential immediately."""
        mock_vault = MagicMock()
        provisioner = IntegrationProvisioner(mock_vault, cluster_id="test-cluster")

        old_creds = Credentials(
            product="tobogganing",
            account_id="old123",
            client_id="old_cid",
            client_secret="old_secret",
        )
        new_creds = Credentials(
            product="tobogganing",
            account_id="new123",
            client_id="new_cid",
            client_secret="new_secret",
        )

        with patch.object(provisioner, "_read_credential", return_value=old_creds):
            with patch.object(provisioner, "_mint_service_account", return_value=new_creds):
                with patch.object(provisioner, "_write_credential"):
                    with patch.object(provisioner, "_delete_credential"):
                        with patch.object(provisioner, "_revoke_on_product") as mock_revoke:
                            result = await provisioner.compromise_response("tobogganing")
                            assert result.account_id == "new123"
                            mock_revoke.assert_called_once()


@pytest.mark.asyncio
class TestIntegrationProvisionerRevoke:
    """Test revoke on product."""

    @pytest.mark.asyncio
    async def test_revoke_on_product_best_effort_recovers(self):
        """_revoke_on_product logs warning and returns on best-effort failure."""
        mock_vault = MagicMock()
        mock_http = AsyncMock()
        mock_http.delete.side_effect = httpx.RequestError("Connection failed")

        provisioner = IntegrationProvisioner(
            mock_vault,
            cluster_id="test-cluster",
            http_client=mock_http,
        )

        with patch("app.workers.integration_provisioner._load_endpoint") as mock_load:
            from app.workers.integration_provisioner import ProductEndpoint

            mock_load.return_value = ProductEndpoint(
                base_url="http://test:8080",
                admin_token="token",
            )

            with patch("app.workers.integration_provisioner.logger") as mock_logger:
                # Should not raise
                await provisioner._revoke_on_product("tobogganing", "acc123", best_effort=True)
                assert mock_logger.warning.called

    @pytest.mark.asyncio
    async def test_revoke_on_product_best_effort_404_ignored(self):
        """_revoke_on_product ignores 404 in best-effort mode."""
        mock_vault = MagicMock()
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_http.delete.return_value = mock_response

        provisioner = IntegrationProvisioner(
            mock_vault,
            cluster_id="test-cluster",
            http_client=mock_http,
        )

        with patch("app.workers.integration_provisioner._load_endpoint") as mock_load:
            from app.workers.integration_provisioner import ProductEndpoint

            mock_load.return_value = ProductEndpoint(
                base_url="http://test:8080",
                admin_token="token",
            )

            # Should not raise
            await provisioner._revoke_on_product("tobogganing", "acc123", best_effort=True)


class TestIntegrationProvisionerClientLifecycle:
    """Test client initialization and closure."""

    @pytest.mark.asyncio
    async def test_client_lazy_initialization(self):
        """_client lazily initializes HTTP client."""
        mock_vault = MagicMock()
        provisioner = IntegrationProvisioner(mock_vault, cluster_id="test-cluster")

        assert provisioner._http is None
        client1 = await provisioner._client()
        assert client1 is not None
        client2 = await provisioner._client()
        assert client1 is client2  # Same instance

    @pytest.mark.asyncio
    async def test_aclose_closes_owned_client(self):
        """aclose closes HTTP client if owned."""
        mock_vault = MagicMock()
        provisioner = IntegrationProvisioner(mock_vault, cluster_id="test-cluster")

        # Get client to initialize it
        await provisioner._client()
        assert provisioner._owns_http is True

        await provisioner.aclose()
        assert provisioner._http is None

    @pytest.mark.asyncio
    async def test_aclose_preserves_injected_client(self):
        """aclose does not close injected HTTP client."""
        mock_vault = MagicMock()
        mock_http = AsyncMock()

        provisioner = IntegrationProvisioner(
            mock_vault,
            cluster_id="test-cluster",
            http_client=mock_http,
        )
        assert provisioner._owns_http is False

        await provisioner.aclose()
        mock_http.aclose.assert_not_called()
