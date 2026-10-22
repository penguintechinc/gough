"""Tests for SPIRE client."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("pyspiffe")

from app.clients.spire import (
    SpireClient,
    SpireError,
    SpireRegistrationEntry,
    SpireRegistrationFailed,
    SpireSvid,
    SpireSvidUnavailable,
    SpireWorkloadApiUnavailable,
)


class TestSpireModels:
    """Test Pydantic models for SPIRE."""

    def test_spire_svid(self) -> None:
        """SpireSvid accepts required fields."""
        now = datetime.now(timezone.utc)
        svid = SpireSvid(
            spiffe_id="spiffe://ex.com/service/api",
            cert_pem="-----BEGIN CERTIFICATE-----",
            key_pem="-----BEGIN PRIVATE KEY-----",
            expires_at=now,
        )
        assert svid.spiffe_id == "spiffe://ex.com/service/api"
        assert svid.expires_at == now

    def test_spire_entry_defaults(self) -> None:
        """SpireRegistrationEntry has defaults."""
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://ex.com/app",
            parent_id="spiffe://ex.com/agent/k8s",
        )
        assert entry.selectors == {}
        assert entry.ttl_seconds == 3600

    def test_spire_entry_custom(self) -> None:
        """SpireRegistrationEntry accepts custom values."""
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://ex.com/app",
            parent_id="spiffe://ex.com/agent",
            selectors={"k8s:pod": "p1"},
            ttl_seconds=7200,
        )
        assert entry.selectors["k8s:pod"] == "p1"
        assert entry.ttl_seconds == 7200


class TestSpireClientInit:
    """Test SpireClient initialization."""

    def test_init_explicit_args(self) -> None:
        """SpireClient accepts explicit args."""
        client = SpireClient(
            workload_api_socket="unix:///run/spire/agent.sock",
            server_address="spire-server:8081",
        )
        assert client.workload_api_socket == "unix:///run/spire/agent.sock"
        assert client.server_address == "spire-server:8081"

    def test_init_reads_env_socket_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SpireClient reads workload socket from env."""
        monkeypatch.setenv("SPIRE_AGENT_SOCKET", "unix:///custom/api.sock")
        client = SpireClient()
        assert client.workload_api_socket == "unix:///custom/api.sock"

    def test_init_reads_env_server_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SpireClient reads server address from env."""
        monkeypatch.setenv("SPIRE_SERVER_ADDRESS", "spire:9081")
        client = SpireClient(workload_api_socket="unix:///tmp/api.sock")
        assert client.server_address == "spire:9081"

    def test_init_defaults(self) -> None:
        """SpireClient has sensible defaults."""
        client = SpireClient()
        assert client.workload_api_socket == "unix:///tmp/spire-agent/public/api.sock"
        assert client.server_address == "localhost:8081"

    def test_init_rejects_invalid_socket_uri(self) -> None:
        """SpireClient rejects socket without unix:// prefix."""
        with pytest.raises(ValueError, match="Must start with 'unix://'"):
            SpireClient(workload_api_socket="/run/spire/agent.sock")

    def test_init_rejects_invalid_server_address(self) -> None:
        """SpireClient rejects server address without :port."""
        with pytest.raises(ValueError, match="Must contain ':' separator"):
            SpireClient(
                workload_api_socket="unix:///tmp/api.sock", server_address="localhost"
            )


class TestFetchX509Svid:
    """Test fetch_x509_svid."""

    @patch("app.clients.spire.DefaultWorkloadApiClient")
    def test_fetch_x509_svid_returns_model(self, mock_api_class: MagicMock) -> None:
        """fetch_x509_svid returns SpireSvid model."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        mock_svid = MagicMock()
        mock_svid.spiffe_id = "spiffe://ex.com/service/api"
        mock_svid.cert.public_bytes_pem.return_value = "CERT_PEM"
        mock_svid.private_key.private_bytes_pem.return_value = "KEY_PEM"
        now = datetime.now(timezone.utc)
        mock_svid.cert.expires_at = now

        mock_context = MagicMock()
        mock_context.svid_list = [mock_svid]
        mock_api.fetch_x509_context.return_value = mock_context

        client = SpireClient(workload_api_socket="unix:///tmp/api.sock")
        svid = client.fetch_x509_svid()

        assert isinstance(svid, SpireSvid)
        assert svid.spiffe_id == "spiffe://ex.com/service/api"
        assert svid.cert_pem == "CERT_PEM"
        assert svid.key_pem == "KEY_PEM"

    @patch("app.clients.spire.DefaultWorkloadApiClient")
    def test_fetch_x509_svid_unavailable_raises(self, mock_api_class: MagicMock) -> None:
        """fetch_x509_svid raises SpireWorkloadApiUnavailable on error."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.fetch_x509_context.side_effect = Exception("Socket error")

        client = SpireClient(workload_api_socket="unix:///tmp/api.sock")
        with pytest.raises(SpireWorkloadApiUnavailable):
            client.fetch_x509_svid()

    @patch("app.clients.spire.DefaultWorkloadApiClient")
    def test_fetch_x509_svid_no_svids_raises(self, mock_api_class: MagicMock) -> None:
        """fetch_x509_svid raises SpireSvidUnavailable if no SVIDs."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_context = MagicMock()
        mock_context.svid_list = []
        mock_api.fetch_x509_context.return_value = mock_context

        client = SpireClient(workload_api_socket="unix:///tmp/api.sock")
        with pytest.raises(SpireSvidUnavailable):
            client.fetch_x509_svid()


class TestFetchJwtSvid:
    """Test fetch_jwt_svid."""

    @patch("app.clients.spire.DefaultWorkloadApiClient")
    def test_fetch_jwt_svid_returns_token(self, mock_api_class: MagicMock) -> None:
        """fetch_jwt_svid returns JWT token string."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_jwt = MagicMock()
        mock_jwt.token = "eyJhbGc..."
        mock_api.fetch_jwt_svid.return_value = mock_jwt

        client = SpireClient(workload_api_socket="unix:///tmp/api.sock")
        token = client.fetch_jwt_svid(audience=["api"])

        assert token == "eyJhbGc..."
        mock_api.fetch_jwt_svid.assert_called_once_with(audiences=["api"])

    @patch("app.clients.spire.DefaultWorkloadApiClient")
    def test_fetch_jwt_svid_api_error_raises(self, mock_api_class: MagicMock) -> None:
        """fetch_jwt_svid propagates API errors."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.fetch_jwt_svid.side_effect = Exception("API error")

        client = SpireClient(workload_api_socket="unix:///tmp/api.sock")
        with pytest.raises(SpireWorkloadApiUnavailable):
            client.fetch_jwt_svid(audience=["api"])

    @patch("app.clients.spire.DefaultWorkloadApiClient")
    def test_fetch_jwt_svid_no_token_raises(self, mock_api_class: MagicMock) -> None:
        """fetch_jwt_svid raises if no token returned."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.fetch_jwt_svid.return_value = None

        client = SpireClient(workload_api_socket="unix:///tmp/api.sock")
        with pytest.raises(SpireSvidUnavailable):
            client.fetch_jwt_svid(audience=["api"])


class TestRegisterWorkload:
    """Test register_workload."""

    @patch("app.clients.spire.subprocess.run")
    def test_register_workload_success(self, mock_run: MagicMock) -> None:
        """register_workload calls spire-server and returns entry ID."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Entry ID : abc-123-def-456\n", stderr=""
        )

        client = SpireClient(server_address="spire:8081")
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://ex.com/app",
            parent_id="spiffe://ex.com/agent",
            selectors={"k8s:pod": "p1"},
            ttl_seconds=3600,
        )

        entry_id = client.register_workload(entry)
        assert entry_id == "abc-123-def-456"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "spire-server" in call_args
        assert "-spiffeID" in call_args

    @patch("app.clients.spire.subprocess.run")
    def test_register_workload_nonzero_exit_raises(self, mock_run: MagicMock) -> None:
        """register_workload raises on non-zero exit."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Error message"
        )

        client = SpireClient(server_address="spire:8081")
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://ex.com/app", parent_id="spiffe://ex.com/agent"
        )

        with pytest.raises(SpireRegistrationFailed):
            client.register_workload(entry)

    @patch("app.clients.spire.subprocess.run")
    def test_register_workload_timeout_raises(self, mock_run: MagicMock) -> None:
        """register_workload raises on subprocess timeout."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 30)

        client = SpireClient(server_address="spire:8081")
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://ex.com/app", parent_id="spiffe://ex.com/agent"
        )

        with pytest.raises(SpireRegistrationFailed):
            client.register_workload(entry)

    @patch("app.clients.spire.subprocess.run")
    def test_register_workload_parse_failure(self, mock_run: MagicMock) -> None:
        """register_workload raises if entry ID not in output."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="No entry ID here\n", stderr=""
        )

        client = SpireClient(server_address="spire:8081")
        entry = SpireRegistrationEntry(
            spiffe_id="spiffe://ex.com/app", parent_id="spiffe://ex.com/agent"
        )

        with pytest.raises(SpireRegistrationFailed, match="Could not parse entry ID"):
            client.register_workload(entry)


class TestListRegistrations:
    """Test list_registrations."""

    @patch("app.clients.spire.subprocess.run")
    def test_list_registrations_success(self, mock_run: MagicMock) -> None:
        """list_registrations parses JSON output."""
        json_output = json.dumps(
            {
                "entries": [
                    {
                        "spiffeID": "spiffe://ex.com/app1",
                        "parentID": "spiffe://ex.com/agent",
                        "selectors": [
                            {"type": "k8s", "value": "pod:p1"}
                        ],
                        "ttl": 3600,
                    }
                ]
            }
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=json_output, stderr="")

        client = SpireClient(server_address="spire:8081")
        entries = client.list_registrations()

        assert len(entries) == 1
        assert entries[0].spiffe_id == "spiffe://ex.com/app1"
        assert entries[0].selectors.get("k8s") == "pod:p1"

    @patch("app.clients.spire.subprocess.run")
    def test_list_registrations_filtered_parent(self, mock_run: MagicMock) -> None:
        """list_registrations filters by parent_id."""
        mock_run.return_value = MagicMock(returncode=0, stdout='{"entries": []}', stderr="")

        client = SpireClient(server_address="spire:8081")
        client.list_registrations(parent_id="spiffe://ex.com/agent")

        call_args = mock_run.call_args[0][0]
        assert "-parentID" in call_args
        assert "spiffe://ex.com/agent" in call_args

    @patch("app.clients.spire.subprocess.run")
    def test_list_registrations_error_raises(self, mock_run: MagicMock) -> None:
        """list_registrations raises on subprocess error."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Error")

        client = SpireClient(server_address="spire:8081")
        with pytest.raises(SpireError):
            client.list_registrations()


class TestWaitForWorkloadApi:
    """Test wait_for_workload_api."""

    @patch("app.clients.spire.DefaultWorkloadApiClient")
    def test_wait_for_workload_api_success(self, mock_api_class: MagicMock) -> None:
        """wait_for_workload_api succeeds on first attempt."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_context = MagicMock()
        mock_svid = MagicMock()
        mock_svid.spiffe_id = "spiffe://ex.com/service"
        mock_svid.cert.public_bytes_pem.return_value = "CERT"
        mock_svid.private_key.private_bytes_pem.return_value = "KEY"
        mock_svid.cert.expires_at = datetime.now(timezone.utc)
        mock_context.svid_list = [mock_svid]
        mock_api.fetch_x509_context.return_value = mock_context

        client = SpireClient(workload_api_socket="unix:///tmp/api.sock")
        client.wait_for_workload_api(timeout=5)

    @patch("app.clients.spire.DefaultWorkloadApiClient")
    @patch("app.clients.spire.sleep")
    def test_wait_for_workload_api_timeout(
        self, mock_sleep: MagicMock, mock_api_class: MagicMock
    ) -> None:
        """wait_for_workload_api raises on timeout."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.fetch_x509_context.side_effect = Exception("Not ready")

        client = SpireClient(workload_api_socket="unix:///tmp/api.sock")
        with pytest.raises(SpireWorkloadApiUnavailable):
            client.wait_for_workload_api(timeout=1)


class TestNoPrivateKeyLogging:
    """Test that private keys are never logged."""

    @patch("app.clients.spire.DefaultWorkloadApiClient")
    def test_no_private_key_in_logs(
        self, mock_api_class: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify private keys not logged on fetch."""
        import logging

        caplog.set_level(logging.INFO)

        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_svid = MagicMock()
        mock_svid.spiffe_id = "spiffe://ex.com/service"
        mock_svid.cert.public_bytes_pem.return_value = "CERT_PEM_SECRET"
        mock_svid.private_key.private_bytes_pem.return_value = "KEY_PEM_SECRET"
        mock_svid.cert.expires_at = datetime.now(timezone.utc)
        mock_context = MagicMock()
        mock_context.svid_list = [mock_svid]
        mock_api.fetch_x509_context.return_value = mock_context

        client = SpireClient(workload_api_socket="unix:///tmp/api.sock")
        client.fetch_x509_svid()

        assert "KEY_PEM_SECRET" not in caplog.text
