"""Extended coverage tests for app/clients/spire.py missed lines."""

import json
import re
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.clients.spire import (
    SpireClient,
    SpireError,
    SpireJoinTokenFailed,
    SpireWorkloadApiUnavailable,
    SpireSvidUnavailable,
    SpireRegistrationFailed,
    SpireSvid,
    SpireRegistrationEntry,
)


class TestSpireClientInitialization:
    """Test SpireClient initialization and validation."""

    def test_init_default_socket(self):
        """SpireClient uses default socket when not provided."""
        client = SpireClient()
        assert client.workload_api_socket == "unix:///tmp/spire-agent/public/api.sock"

    def test_init_default_server(self):
        """SpireClient uses default server address when not provided."""
        client = SpireClient()
        assert client.server_address == "localhost:8081"

    def test_init_custom_socket(self):
        """SpireClient accepts custom socket path."""
        custom_socket = "unix:///var/run/spire-agent.sock"
        client = SpireClient(workload_api_socket=custom_socket)
        assert client.workload_api_socket == custom_socket

    def test_init_custom_server(self):
        """SpireClient accepts custom server address."""
        custom_server = "spire.example.com:8081"
        client = SpireClient(server_address=custom_server)
        assert client.server_address == custom_server

    def test_init_socket_env_var(self):
        """SpireClient reads socket from env var."""
        with patch.dict("os.environ", {"SPIRE_AGENT_SOCKET": "unix:///custom/socket"}):
            client = SpireClient()
            assert client.workload_api_socket == "unix:///custom/socket"

    def test_init_server_env_var(self):
        """SpireClient reads server address from env var."""
        with patch.dict("os.environ", {"SPIRE_SERVER_ADDRESS": "custom.server:9000"}):
            client = SpireClient()
            assert client.server_address == "custom.server:9000"

    def test_init_invalid_socket_format(self):
        """SpireClient raises on invalid socket format."""
        with pytest.raises(ValueError, match="Must start with 'unix://'"):
            SpireClient(workload_api_socket="invalid://socket")

    def test_init_invalid_server_format(self):
        """SpireClient raises on server without colon separator."""
        with pytest.raises(ValueError, match="Must contain ':' separator"):
            SpireClient(server_address="localhost")


class TestSpireClientWorkloadApi:
    """Test workload API client initialization."""

    def test_get_workload_api_client_lazy_init(self):
        """_get_workload_api_client initializes on first call."""
        client = SpireClient()
        # First call should initialize
        with patch("app.clients.spire.DefaultWorkloadApiClient") as mock_api:
            mock_api.return_value = MagicMock()
            api1 = client._get_workload_api_client()
            assert api1 is not None
            # Second call should return same instance
            api2 = client._get_workload_api_client()
            assert api1 is api2

    def test_get_workload_api_client_uses_socket(self):
        """_get_workload_api_client passes socket to DefaultWorkloadApiClient."""
        custom_socket = "unix:///var/run/spire.sock"
        client = SpireClient(workload_api_socket=custom_socket)

        with patch("app.clients.spire.DefaultWorkloadApiClient") as mock_api:
            mock_api.return_value = MagicMock()
            client._get_workload_api_client()
            mock_api.assert_called_once_with(socket_path=custom_socket)


class TestSpireClientFetchX509Svid:
    """Test X.509 SVID fetching."""

    def test_fetch_x509_svid_success(self):
        """fetch_x509_svid returns SpireSvid on success."""
        mock_client = MagicMock()
        mock_context = MagicMock()
        mock_svid = MagicMock()
        mock_svid.spiffe_id = "spiffe://example.com/service"
        mock_svid.cert.public_bytes_pem.return_value = "-----BEGIN CERTIFICATE-----"
        mock_svid.private_key.private_bytes_pem.return_value = "-----BEGIN PRIVATE KEY-----"
        mock_svid.cert.expires_at = datetime(2025, 12, 31, tzinfo=timezone.utc)
        mock_context.svid_list = [mock_svid]
        mock_client.fetch_x509_context.return_value = mock_context

        client = SpireClient()
        with patch.object(client, "_get_workload_api_client", return_value=mock_client):
            result = client.fetch_x509_svid()
            assert isinstance(result, SpireSvid)
            assert result.spiffe_id == "spiffe://example.com/service"

    def test_fetch_x509_svid_api_error(self):
        """fetch_x509_svid raises SpireWorkloadApiUnavailable on error."""
        mock_client = MagicMock()
        mock_client.fetch_x509_context.side_effect = Exception("API error")

        client = SpireClient()
        with patch.object(client, "_get_workload_api_client", return_value=mock_client):
            with pytest.raises(SpireWorkloadApiUnavailable, match="Failed to fetch X.509"):
                client.fetch_x509_svid()

    def test_fetch_x509_svid_no_svids(self):
        """fetch_x509_svid raises SpireSvidUnavailable when no SVIDs returned."""
        mock_client = MagicMock()
        mock_context = MagicMock()
        mock_context.svid_list = []
        mock_client.fetch_x509_context.return_value = mock_context

        client = SpireClient()
        with patch.object(client, "_get_workload_api_client", return_value=mock_client):
            with pytest.raises(SpireSvidUnavailable, match="No SVIDs returned"):
                client.fetch_x509_svid()

    def test_fetch_x509_svid_context_none(self):
        """fetch_x509_svid raises when context is None."""
        mock_client = MagicMock()
        mock_client.fetch_x509_context.return_value = None

        client = SpireClient()
        with patch.object(client, "_get_workload_api_client", return_value=mock_client):
            with pytest.raises(SpireSvidUnavailable):
                client.fetch_x509_svid()


class TestSpireClientFetchJwtSvid:
    """Test JWT SVID fetching."""

    def test_fetch_jwt_svid_success(self):
        """fetch_jwt_svid returns token on success."""
        mock_client = MagicMock()
        mock_jwt = MagicMock()
        mock_jwt.token = "eyJhbGciOiJFUzI1NiJ9..."
        mock_client.fetch_jwt_svid.return_value = mock_jwt

        client = SpireClient()
        with patch.object(client, "_get_workload_api_client", return_value=mock_client):
            result = client.fetch_jwt_svid(["audience1", "audience2"])
            assert result == "eyJhbGciOiJFUzI1NiJ9..."

    def test_fetch_jwt_svid_api_error(self):
        """fetch_jwt_svid raises SpireWorkloadApiUnavailable on error."""
        mock_client = MagicMock()
        mock_client.fetch_jwt_svid.side_effect = Exception("API error")

        client = SpireClient()
        with patch.object(client, "_get_workload_api_client", return_value=mock_client):
            with pytest.raises(SpireWorkloadApiUnavailable, match="Failed to fetch JWT"):
                client.fetch_jwt_svid(["audience1"])

    def test_fetch_jwt_svid_no_token(self):
        """fetch_jwt_svid raises SpireSvidUnavailable when no token returned."""
        mock_client = MagicMock()
        mock_jwt = MagicMock()
        mock_jwt.token = None
        mock_client.fetch_jwt_svid.return_value = mock_jwt

        client = SpireClient()
        with patch.object(client, "_get_workload_api_client", return_value=mock_client):
            with pytest.raises(SpireSvidUnavailable, match="No JWT SVID returned"):
                client.fetch_jwt_svid(["audience1"])

    def test_fetch_jwt_svid_jwt_none(self):
        """fetch_jwt_svid raises when JWT object is None."""
        mock_client = MagicMock()
        mock_client.fetch_jwt_svid.return_value = None

        client = SpireClient()
        with patch.object(client, "_get_workload_api_client", return_value=mock_client):
            with pytest.raises(SpireSvidUnavailable):
                client.fetch_jwt_svid(["audience1"])


class TestSpireClientRegisterWorkload:
    """Test workload registration."""

    def test_register_workload_timeout(self):
        """register_workload raises on timeout."""
        import subprocess

        client = SpireClient()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            with pytest.raises(SpireRegistrationFailed, match="timeout"):
                entry = SpireRegistrationEntry(
                    spiffe_id="spiffe://example.com/service",
                    parent_id="spiffe://example.com/agent",
                )
                client.register_workload(entry)

    def test_register_workload_non_zero_exit(self):
        """register_workload raises on non-zero exit code."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Entry already exists"

        client = SpireClient()
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(SpireRegistrationFailed, match="entry create failed"):
                entry = SpireRegistrationEntry(
                    spiffe_id="spiffe://example.com/service",
                    parent_id="spiffe://example.com/agent",
                )
                client.register_workload(entry)

    def test_register_workload_parse_error(self):
        """register_workload raises when entry ID not found in output."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Invalid output format"  # No entry ID

        client = SpireClient()
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(SpireRegistrationFailed, match="Could not parse entry ID"):
                entry = SpireRegistrationEntry(
                    spiffe_id="spiffe://example.com/service",
                    parent_id="spiffe://example.com/agent",
                )
                client.register_workload(entry)


class TestSpireClientGenerateJoinToken:
    """Test generate_join_token (the M2 'full token flow' -- spire-server token generate)."""

    def test_generate_join_token_success(self):
        """generate_join_token calls spire-server and returns the parsed token."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Token: abc-123-join-token\n"
        mock_result.stderr = ""

        client = SpireClient(server_address="spire:8081")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            token = client.generate_join_token("spiffe://example.com/node/helper/1", 3600)

        assert token == "abc-123-join-token"
        call_args = mock_run.call_args[0][0]
        assert "spire-server" in call_args
        assert "token" in call_args
        assert "generate" in call_args
        assert "-spiffeID" in call_args
        assert "spiffe://example.com/node/helper/1" in call_args

    def test_generate_join_token_timeout_raises(self):
        """generate_join_token raises SpireJoinTokenFailed on subprocess timeout."""
        import subprocess

        client = SpireClient(server_address="spire:8081")
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)),
            pytest.raises(SpireJoinTokenFailed, match="timeout"),
        ):
            client.generate_join_token("spiffe://example.com/node/helper/1")

    def test_generate_join_token_non_zero_exit_raises(self):
        """generate_join_token raises SpireJoinTokenFailed on non-zero exit."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "server unreachable"

        client = SpireClient(server_address="spire:8081")
        with (
            patch("subprocess.run", return_value=mock_result),
            pytest.raises(SpireJoinTokenFailed, match="token generate failed"),
        ):
            client.generate_join_token("spiffe://example.com/node/helper/1")

    def test_generate_join_token_parse_failure_raises(self):
        """generate_join_token raises SpireJoinTokenFailed when no Token: in output."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "no token here"
        mock_result.stderr = ""

        client = SpireClient(server_address="spire:8081")
        with (
            patch("subprocess.run", return_value=mock_result),
            pytest.raises(SpireJoinTokenFailed, match="Could not parse join token"),
        ):
            client.generate_join_token("spiffe://example.com/node/helper/1")


class TestSpireClientListRegistrations:
    """Test listing registration entries."""

    def test_list_registrations_success(self):
        """list_registrations returns list of entries."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "entries": [
                {
                    "spiffeID": "spiffe://example.com/service1",
                    "parentID": "spiffe://example.com/agent",
                    "selectors": [{"type": "unix", "value": "user:app"}],
                    "ttl": 3600,
                }
            ]
        })

        client = SpireClient()
        with patch("subprocess.run", return_value=mock_result):
            result = client.list_registrations()
            assert len(result) == 1
            assert result[0].spiffe_id == "spiffe://example.com/service1"

    def test_list_registrations_empty(self):
        """list_registrations returns empty list on empty output."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        client = SpireClient()
        with patch("subprocess.run", return_value=mock_result):
            result = client.list_registrations()
            assert result == []

    def test_list_registrations_timeout(self):
        """list_registrations raises on timeout."""
        import subprocess

        client = SpireClient()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            with pytest.raises(SpireError, match="timeout"):
                client.list_registrations()

    def test_list_registrations_non_zero_exit(self):
        """list_registrations raises on non-zero exit code."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Command failed"

        client = SpireClient()
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(SpireError, match="entry show failed"):
                client.list_registrations()

    def test_list_registrations_json_error(self):
        """list_registrations raises on JSON parse error."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "invalid json {"

        client = SpireClient()
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(SpireError, match="Failed to parse"):
                client.list_registrations()

    def test_list_registrations_with_parent_filter(self):
        """list_registrations accepts parent_id filter."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"entries": []}'

        client = SpireClient()
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            client.list_registrations(parent_id="spiffe://example.com/agent")

            # Verify parent_id was added to command
            call_args = mock_run.call_args[0][0]
            assert "-parentID" in call_args
            assert "spiffe://example.com/agent" in call_args


class TestSpireClientWaitForWorkloadApi:
    """Test workload API availability polling."""

    def test_wait_for_workload_api_ready_immediately(self):
        """wait_for_workload_api returns immediately when API ready."""
        client = SpireClient()

        with patch.object(client, "fetch_x509_svid", return_value=MagicMock()):
            # Should not raise
            client.wait_for_workload_api(timeout=30)

    def test_wait_for_workload_api_timeout(self):
        """wait_for_workload_api raises after timeout."""
        client = SpireClient()

        with patch.object(client, "fetch_x509_svid", side_effect=SpireWorkloadApiUnavailable("Not ready")):
            with patch("time.sleep"):  # Fast-path sleep
                with pytest.raises(SpireWorkloadApiUnavailable, match="not ready after"):
                    client.wait_for_workload_api(timeout=1)

    def test_wait_for_workload_api_retries(self):
        """wait_for_workload_api retries on failures."""
        client = SpireClient()

        call_count = 0

        def side_effect_fn(*args):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise SpireWorkloadApiUnavailable("Not ready")
            return MagicMock()

        with patch.object(client, "fetch_x509_svid", side_effect=side_effect_fn):
            with patch("time.sleep"):
                # Should succeed on 3rd attempt
                client.wait_for_workload_api(timeout=30)


class TestSpireDataClasses:
    """Test SpireSvid and SpireRegistrationEntry models."""

    def test_spire_svid_fields(self):
        """SpireSvid validates required fields."""
        svid = SpireSvid(
            spiffe_id="spiffe://example.com/service",
            cert_pem="-----BEGIN CERTIFICATE-----",
            key_pem="-----BEGIN PRIVATE KEY-----",
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
        )
        assert svid.spiffe_id == "spiffe://example.com/service"

    def test_spire_registration_entry_defaults(self):
        """SpireRegistrationEntry has correct defaults."""
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://example.com/service",
            parent_id="spiffe://example.com/agent",
        )
        assert entry.ttl_seconds == 3600
        assert entry.selectors == {}
