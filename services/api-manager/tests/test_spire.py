"""Tests for SPIRE client module.

Covers SPIRE workload API, registration, and error handling.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, Mock
from pydantic import ValidationError


# ============================================================================
# Tests for app/clients/spire.py
# ============================================================================


class TestSpireSvid:
    """Tests for SpireSvid model."""

    def test_spire_svid_creation(self):
        """Test SpireSvid model creation."""
        from app.clients.spire import SpireSvid

        now = datetime.now(timezone.utc)
        svid = SpireSvid(
            spiffe_id="spiffe://penguintech.io/api-manager",
            cert_pem="-----BEGIN CERTIFICATE-----\n...",
            key_pem="-----BEGIN PRIVATE KEY-----\n...",
            expires_at=now,
        )

        assert svid.spiffe_id == "spiffe://penguintech.io/api-manager"
        assert svid.cert_pem == "-----BEGIN CERTIFICATE-----\n..."
        assert svid.key_pem == "-----BEGIN PRIVATE KEY-----\n..."
        assert svid.expires_at == now

    def test_spire_svid_validation_required_fields(self):
        """Test SpireSvid validation requires all fields."""
        from app.clients.spire import SpireSvid

        with pytest.raises(ValidationError):
            SpireSvid(spiffe_id="spiffe://...")


class TestSpireRegistrationEntry:
    """Tests for SpireRegistrationEntry model."""

    def test_registration_entry_creation(self):
        """Test SpireRegistrationEntry model creation."""
        from app.clients.spire import SpireRegistrationEntry

        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://penguintech.io/api-manager",
            parent_id="spiffe://penguintech.io",
            selectors={"k8s": "pod", "k8s_ns": "gough"},
            ttl_seconds=3600,
        )

        assert entry.spiffe_id == "spiffe://penguintech.io/api-manager"
        assert entry.parent_id == "spiffe://penguintech.io"
        assert entry.ttl_seconds == 3600

    def test_registration_entry_default_ttl(self):
        """Test SpireRegistrationEntry uses default TTL."""
        from app.clients.spire import SpireRegistrationEntry

        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://penguintech.io/api-manager",
            parent_id="spiffe://penguintech.io",
        )

        assert entry.ttl_seconds == 3600


class TestSpireExceptions:
    """Tests for SPIRE exception classes."""

    def test_spire_error_base(self):
        """Test SpireError base exception."""
        from app.clients.spire import SpireError

        exc = SpireError("Test error")
        assert str(exc) == "Test error"

    def test_spire_workload_api_unavailable(self):
        """Test SpireWorkloadApiUnavailable exception."""
        from app.clients.spire import SpireWorkloadApiUnavailable

        exc = SpireWorkloadApiUnavailable("Socket not found")
        assert isinstance(exc, Exception)

    def test_spire_svid_unavailable(self):
        """Test SpireSvidUnavailable exception."""
        from app.clients.spire import SpireSvidUnavailable

        exc = SpireSvidUnavailable("No SVID returned")
        assert isinstance(exc, Exception)

    def test_spire_registration_failed(self):
        """Test SpireRegistrationFailed exception."""
        from app.clients.spire import SpireRegistrationFailed

        exc = SpireRegistrationFailed("Registration error")
        assert isinstance(exc, Exception)


class TestSpireClient:
    """Tests for SpireClient class."""

    def test_spire_client_init_defaults(self, monkeypatch):
        """Test SpireClient initialization with defaults."""
        from app.clients.spire import SpireClient

        monkeypatch.delenv('SPIRE_AGENT_SOCKET', raising=False)
        monkeypatch.delenv('SPIRE_SERVER_ADDRESS', raising=False)

        client = SpireClient()

        assert client.workload_api_socket == "unix:///tmp/spire-agent/public/api.sock"
        assert client.server_address == "localhost:8081"

    def test_spire_client_init_with_env_vars(self, monkeypatch):
        """Test SpireClient initialization with environment variables."""
        from app.clients.spire import SpireClient

        monkeypatch.setenv('SPIRE_AGENT_SOCKET', 'unix:///var/run/spire/agent.sock')
        monkeypatch.setenv('SPIRE_SERVER_ADDRESS', 'spire-server:8081')

        client = SpireClient()

        assert client.workload_api_socket == 'unix:///var/run/spire/agent.sock'
        assert client.server_address == 'spire-server:8081'

    def test_spire_client_init_custom_socket(self):
        """Test SpireClient initialization with custom socket path."""
        from app.clients.spire import SpireClient

        client = SpireClient(workload_api_socket='unix:///custom/socket')

        assert client.workload_api_socket == 'unix:///custom/socket'

    def test_spire_client_init_custom_server(self):
        """Test SpireClient initialization with custom server address."""
        from app.clients.spire import SpireClient

        client = SpireClient(server_address='custom-server:9081')

        assert client.server_address == 'custom-server:9081'

    def test_spire_client_invalid_socket_format(self):
        """Test SpireClient raises ValueError for invalid socket format."""
        from app.clients.spire import SpireClient

        with pytest.raises(ValueError, match="Invalid workload_api_socket"):
            SpireClient(workload_api_socket='invalid://socket')

    def test_spire_client_invalid_server_address(self):
        """Test SpireClient raises ValueError for invalid server address."""
        from app.clients.spire import SpireClient

        with pytest.raises(ValueError, match="Invalid server_address"):
            SpireClient(server_address='no-colon-here')

    def test_spire_client_workload_api_client_lazy_init(self):
        """Test SpireClient lazily initializes workload API client."""
        from app.clients.spire import SpireClient

        client = SpireClient()
        assert client._workload_api_client is None

    @patch('app.clients.spire.DefaultWorkloadApiClient')
    def test_get_workload_api_client_creates_instance(self, mock_client_class):
        """Test _get_workload_api_client() creates instance on first call."""
        from app.clients.spire import SpireClient

        mock_instance = MagicMock()
        mock_client_class.return_value = mock_instance

        client = SpireClient()
        result = client._get_workload_api_client()

        assert result is mock_instance
        mock_client_class.assert_called_once()

    @patch('app.clients.spire.DefaultWorkloadApiClient')
    def test_get_workload_api_client_returns_cached(self, mock_client_class):
        """Test _get_workload_api_client() returns cached instance."""
        from app.clients.spire import SpireClient

        mock_instance = MagicMock()
        mock_client_class.return_value = mock_instance

        client = SpireClient()
        result1 = client._get_workload_api_client()
        result2 = client._get_workload_api_client()

        assert result1 is result2
        # Should only be called once due to caching
        mock_client_class.assert_called_once()

    @patch('app.clients.spire.DefaultWorkloadApiClient')
    def test_fetch_x509_svid_success(self, mock_client_class):
        """Test fetch_x509_svid() retrieves X.509 SVID successfully."""
        from app.clients.spire import SpireClient

        # Mock the certificate and key objects
        mock_cert = MagicMock()
        mock_cert.public_bytes_pem.return_value = "-----BEGIN CERTIFICATE-----\n..."
        mock_cert.expires_at = datetime.now(timezone.utc)

        mock_key = MagicMock()
        mock_key.private_bytes_pem.return_value = "-----BEGIN PRIVATE KEY-----\n..."

        mock_svid = MagicMock()
        mock_svid.spiffe_id = "spiffe://penguintech.io/api-manager"
        mock_svid.cert = mock_cert
        mock_svid.private_key = mock_key

        mock_context = MagicMock()
        mock_context.svid_list = [mock_svid]

        mock_instance = MagicMock()
        mock_instance.fetch_x509_context.return_value = mock_context
        mock_client_class.return_value = mock_instance

        client = SpireClient()
        result = client.fetch_x509_svid()

        assert result.spiffe_id == "spiffe://penguintech.io/api-manager"
        assert "-----BEGIN CERTIFICATE-----" in result.cert_pem
        assert "-----BEGIN PRIVATE KEY-----" in result.key_pem

    @patch('app.clients.spire.DefaultWorkloadApiClient')
    def test_fetch_x509_svid_unavailable(self, mock_client_class):
        """Test fetch_x509_svid() raises when socket unavailable."""
        from app.clients.spire import SpireClient, SpireWorkloadApiUnavailable

        mock_instance = MagicMock()
        mock_instance.fetch_x509_context.side_effect = Exception("Socket not found")
        mock_client_class.return_value = mock_instance

        client = SpireClient()

        with pytest.raises(SpireWorkloadApiUnavailable):
            client.fetch_x509_svid()

    @patch('app.clients.spire.DefaultWorkloadApiClient')
    def test_fetch_x509_svid_no_svids(self, mock_client_class):
        """Test fetch_x509_svid() raises when no SVIDs returned."""
        from app.clients.spire import SpireClient, SpireSvidUnavailable

        mock_context = MagicMock()
        mock_context.svid_list = []

        mock_instance = MagicMock()
        mock_instance.fetch_x509_context.return_value = mock_context
        mock_client_class.return_value = mock_instance

        client = SpireClient()

        with pytest.raises(SpireSvidUnavailable):
            client.fetch_x509_svid()

    @patch('app.clients.spire.DefaultWorkloadApiClient')
    def test_fetch_jwt_svid_success(self, mock_client_class):
        """Test fetch_jwt_svid() retrieves JWT SVID successfully."""
        from app.clients.spire import SpireClient

        mock_jwt = MagicMock()
        mock_jwt.token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9..."

        mock_instance = MagicMock()
        mock_instance.fetch_jwt_svid.return_value = mock_jwt
        mock_client_class.return_value = mock_instance

        client = SpireClient()
        result = client.fetch_jwt_svid(['api-manager'])

        assert result == "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9..."
        mock_instance.fetch_jwt_svid.assert_called_once_with(audiences=['api-manager'])

    @patch('app.clients.spire.DefaultWorkloadApiClient')
    def test_fetch_jwt_svid_unavailable(self, mock_client_class):
        """Test fetch_jwt_svid() raises when socket unavailable."""
        from app.clients.spire import SpireClient, SpireWorkloadApiUnavailable

        mock_instance = MagicMock()
        mock_instance.fetch_jwt_svid.side_effect = Exception("Socket not found")
        mock_client_class.return_value = mock_instance

        client = SpireClient()

        with pytest.raises(SpireWorkloadApiUnavailable):
            client.fetch_jwt_svid(['api-manager'])

    @patch('app.clients.spire.DefaultWorkloadApiClient')
    def test_fetch_jwt_svid_no_token(self, mock_client_class):
        """Test fetch_jwt_svid() raises when no token returned."""
        from app.clients.spire import SpireClient, SpireSvidUnavailable

        mock_instance = MagicMock()
        mock_instance.fetch_jwt_svid.return_value = None
        mock_client_class.return_value = mock_instance

        client = SpireClient()

        with pytest.raises(SpireSvidUnavailable):
            client.fetch_jwt_svid(['api-manager'])

    @patch('app.clients.spire.subprocess.run')
    def test_register_workload_success(self, mock_run):
        """Test register_workload() registers with SPIRE server."""
        from app.clients.spire import SpireClient, SpireRegistrationEntry

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Entry ID: 12345"
        mock_run.return_value = mock_result

        client = SpireClient()
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://penguintech.io/api-manager",
            parent_id="spiffe://penguintech.io",
            selectors={"k8s": "pod"},
        )

        result = client.register_workload(entry)

        assert result == "12345"
        mock_run.assert_called_once()

    @patch('app.clients.spire.subprocess.run')
    def test_register_workload_failure(self, mock_run):
        """Test register_workload() raises on registration failure."""
        from app.clients.spire import SpireClient, SpireRegistrationEntry, SpireRegistrationFailed

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Registration failed"
        mock_run.return_value = mock_result

        client = SpireClient()
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://penguintech.io/api-manager",
            parent_id="spiffe://penguintech.io",
        )

        with pytest.raises(SpireRegistrationFailed):
            client.register_workload(entry)

    @patch('app.clients.spire.subprocess.run')
    def test_register_workload_with_multiple_selectors(self, mock_run):
        """Test register_workload() includes all selectors in command."""
        from app.clients.spire import SpireClient, SpireRegistrationEntry

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Entry ID: 12345"
        mock_run.return_value = mock_result

        client = SpireClient()
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://penguintech.io/api-manager",
            parent_id="spiffe://penguintech.io",
            selectors={"k8s": "pod", "k8s_ns": "gough", "k8s_sa": "api-manager"},
        )

        client.register_workload(entry)

        # Verify command was called with all selectors
        call_args = mock_run.call_args
        cmd = call_args[0][0] if call_args[0] else []
        assert any("-selector" in str(arg) for arg in cmd)

    @patch('app.clients.spire.subprocess.run')
    def test_register_workload_no_selectors(self, mock_run):
        """Test register_workload() works with no selectors."""
        from app.clients.spire import SpireClient, SpireRegistrationEntry

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Entry ID: 12345"
        mock_run.return_value = mock_result

        client = SpireClient()
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://penguintech.io/api-manager",
            parent_id="spiffe://penguintech.io",
        )

        result = client.register_workload(entry)

        assert result == "12345"

    def test_spire_client_audience_list(self):
        """Test fetch_jwt_svid() accepts list of audiences."""
        from app.clients.spire import SpireClient

        with patch('app.clients.spire.DefaultWorkloadApiClient') as mock_client_class:
            mock_instance = MagicMock()
            mock_jwt = MagicMock()
            mock_jwt.token = "token123"
            mock_instance.fetch_jwt_svid.return_value = mock_jwt
            mock_client_class.return_value = mock_instance

            client = SpireClient()
            audiences = ['api-manager', 'internal']

            result = client.fetch_jwt_svid(audiences)

            mock_instance.fetch_jwt_svid.assert_called_once_with(audiences=audiences)
            assert result == "token123"
