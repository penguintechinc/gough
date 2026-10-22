"""Tests for Vault client."""

import base64
import os
from unittest.mock import MagicMock, patch

import hvac
import pytest

from app.clients.vault import (
    VaultClient,
    VaultError,
    VaultKvReadResponse,
    VaultKvWriteRequest,
    VaultPermissionDenied,
    VaultSealedError,
    VaultTransitDecryptRequest,
    VaultTransitDecryptResponse,
    VaultTransitEncryptRequest,
    VaultTransitEncryptResponse,
    VaultTransitKeyMissing,
)


class TestVaultModels:
    """Test Pydantic models for Vault requests/responses."""

    def test_transit_encrypt_request(self) -> None:
        """VaultTransitEncryptRequest accepts plaintext bytes."""
        req = VaultTransitEncryptRequest(plaintext=b"test")
        assert req.plaintext == b"test"

    def test_transit_encrypt_response(self) -> None:
        """VaultTransitEncryptResponse has required fields."""
        resp = VaultTransitEncryptResponse(ciphertext="vault:v1:abc", key_version=1)
        assert resp.ciphertext == "vault:v1:abc"
        assert resp.key_version == 1

    def test_transit_decrypt_request(self) -> None:
        """VaultTransitDecryptRequest accepts ciphertext."""
        req = VaultTransitDecryptRequest(ciphertext="vault:v1:abc")
        assert req.ciphertext == "vault:v1:abc"

    def test_transit_decrypt_response(self) -> None:
        """VaultTransitDecryptResponse has plaintext field."""
        resp = VaultTransitDecryptResponse(plaintext=b"decrypted")
        assert resp.plaintext == b"decrypted"

    def test_kv_write_request(self) -> None:
        """VaultKvWriteRequest accepts key-value data."""
        req = VaultKvWriteRequest(data={"user": "admin"})
        assert req.data["user"] == "admin"

    def test_kv_read_response(self) -> None:
        """VaultKvReadResponse includes data and metadata."""
        resp = VaultKvReadResponse(data={"key": "val"}, metadata={"v": 1})
        assert resp.data == {"key": "val"}
        assert resp.metadata["v"] == 1


class TestVaultClientInit:
    """Test VaultClient initialization."""

    def test_init_reads_env_addr_token_namespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """VaultClient reads addr, token, namespace from env vars."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "token123")
        monkeypatch.setenv("VAULT_NAMESPACE", "custom")
        client = VaultClient()
        assert client.addr == "http://vault:8200"
        assert client.token == "token123"
        assert client.namespace == "custom"

    def test_init_explicit_args_override_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit args override env vars."""
        monkeypatch.setenv("VAULT_ADDR", "http://old:8200")
        monkeypatch.setenv("VAULT_TOKEN", "old_token")
        client = VaultClient(addr="http://new:8200", token="new_token")
        assert client.addr == "http://new:8200"
        assert client.token == "new_token"

    def test_init_rejects_empty_token(self) -> None:
        """VaultClient raises ValueError if token is empty."""
        with pytest.raises(ValueError, match="VAULT_TOKEN must be provided"):
            VaultClient(addr="http://vault:8200", token="")

    def test_init_rejects_invalid_url(self) -> None:
        """VaultClient raises ValueError for invalid URL."""
        with pytest.raises(ValueError, match="must start with http:// or https://"):
            VaultClient(addr="not-a-url", token="tok")

    def test_init_warns_on_skip_verify(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """VaultClient logs warning when VAULT_SKIP_VERIFY=1."""
        monkeypatch.setenv("VAULT_SKIP_VERIFY", "1")
        with caplog.at_level("WARNING"):
            client = VaultClient(addr="http://vault:8200", token="tok")
        assert "VAULT_SKIP_VERIFY" in caplog.text


class TestVaultClientHealth:
    """Test health() method."""

    @patch("app.clients.vault.hvac.Client")
    def test_health_returns_sealed_status(self, mock_hvac_class: MagicMock) -> None:
        """health() returns dict with sealed, cluster_id, version, performance_standby."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.sys.read_health_status.return_value = {
            "sealed": False,
            "cluster_id": "abc123",
            "version": "1.15.0",
            "performance_standby": False,
        }

        client = VaultClient(addr="http://vault:8200", token="tok")
        result = client.health()

        assert result["sealed"] is False
        assert result["cluster_id"] == "abc123"
        assert result["version"] == "1.15.0"
        assert result["performance_standby"] is False

    @patch("app.clients.vault.hvac.Client")
    def test_health_propagates_vault_down_error(self, mock_hvac_class: MagicMock) -> None:
        """health() raises VaultSealedError when Vault is down."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.sys.read_health_status.side_effect = hvac.exceptions.VaultDown("down")

        client = VaultClient(addr="http://vault:8200", token="tok")
        with pytest.raises(VaultSealedError):
            client.health()


class TestVaultClientTransitEncrypt:
    """Test transit_encrypt() method."""

    @patch("app.clients.vault.hvac.Client")
    def test_transit_encrypt_round_trip(self, mock_hvac_class: MagicMock) -> None:
        """transit_encrypt() returns ciphertext and key_version."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.secrets.transit.encrypt_data.return_value = {
            "data": {"ciphertext": "vault:v1:xyz", "key_version": 2}
        }

        client = VaultClient(addr="http://vault:8200", token="tok")
        result = client.transit_encrypt("my_key", b"secret_data")

        assert result.ciphertext == "vault:v1:xyz"
        assert result.key_version == 2
        mock_client.secrets.transit.encrypt_data.assert_called_once()

    @patch("app.clients.vault.hvac.Client")
    def test_transit_encrypt_invalid_key_raises_missing(self, mock_hvac_class: MagicMock) -> None:
        """transit_encrypt() raises VaultTransitKeyMissing for InvalidPath."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.secrets.transit.encrypt_data.side_effect = hvac.exceptions.InvalidPath("no key")

        client = VaultClient(addr="http://vault:8200", token="tok")
        with pytest.raises(VaultTransitKeyMissing):
            client.transit_encrypt("bad_key", b"data")

    @patch("app.clients.vault.hvac.Client")
    def test_transit_encrypt_forbidden_raises_permission_denied(
        self, mock_hvac_class: MagicMock
    ) -> None:
        """transit_encrypt() raises VaultPermissionDenied for Forbidden."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.secrets.transit.encrypt_data.side_effect = hvac.exceptions.Forbidden("denied")

        client = VaultClient(addr="http://vault:8200", token="tok")
        with pytest.raises(VaultPermissionDenied):
            client.transit_encrypt("key", b"data")

    @patch("app.clients.vault.hvac.Client")
    def test_transit_encrypt_vault_down_raises_sealed_error(
        self, mock_hvac_class: MagicMock
    ) -> None:
        """transit_encrypt() raises VaultSealedError for VaultDown."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.secrets.transit.encrypt_data.side_effect = hvac.exceptions.VaultDown("down")

        client = VaultClient(addr="http://vault:8200", token="tok")
        with pytest.raises(VaultSealedError):
            client.transit_encrypt("key", b"data")


class TestVaultClientTransitDecrypt:
    """Test transit_decrypt() method."""

    @patch("app.clients.vault.hvac.Client")
    def test_transit_decrypt_returns_plaintext_bytes(self, mock_hvac_class: MagicMock) -> None:
        """transit_decrypt() returns plaintext bytes."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        plaintext_b64 = base64.b64encode(b"secret_data").decode()
        mock_client.secrets.transit.decrypt_data.return_value = {
            "data": {"plaintext": plaintext_b64}
        }

        client = VaultClient(addr="http://vault:8200", token="tok")
        result = client.transit_decrypt("my_key", "vault:v1:xyz")

        assert result.plaintext == b"secret_data"

    @patch("app.clients.vault.hvac.Client")
    def test_transit_decrypt_handles_b64_decoding(self, mock_hvac_class: MagicMock) -> None:
        """transit_decrypt() correctly decodes base64 plaintext."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        test_data = b"test_plaintext_123"
        plaintext_b64 = base64.b64encode(test_data).decode()
        mock_client.secrets.transit.decrypt_data.return_value = {
            "data": {"plaintext": plaintext_b64}
        }

        client = VaultClient(addr="http://vault:8200", token="tok")
        result = client.transit_decrypt("my_key", "vault:v1:xyz")

        assert result.plaintext == test_data


class TestVaultClientTransitSign:
    """Test transit_sign() method."""

    @patch("app.clients.vault.hvac.Client")
    def test_transit_sign_returns_opaque_signature_string(self, mock_hvac_class: MagicMock) -> None:
        """transit_sign() returns opaque signature string."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.secrets.transit.sign_data.return_value = {
            "data": {"signature": "vault:v1:sig123"}
        }

        client = VaultClient(addr="http://vault:8200", token="tok")
        result = client.transit_sign("sign_key", b"message")

        assert result == "vault:v1:sig123"


class TestVaultClientTransitVerify:
    """Test transit_verify_signature() method."""

    @patch("app.clients.vault.hvac.Client")
    def test_transit_verify_signature_returns_bool(self, mock_hvac_class: MagicMock) -> None:
        """transit_verify_signature() returns boolean."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.secrets.transit.verify_signed_data.return_value = {"data": {"valid": True}}

        client = VaultClient(addr="http://vault:8200", token="tok")
        result = client.transit_verify_signature("sign_key", b"message", "vault:v1:sig123")

        assert result is True


class TestVaultClientKvWrite:
    """Test kv_write() method."""

    @patch("app.clients.vault.hvac.Client")
    def test_kv_write_calls_kv_v2_create_or_update(self, mock_hvac_class: MagicMock) -> None:
        """kv_write() calls create_or_update_secret."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client

        client = VaultClient(addr="http://vault:8200", token="tok")
        client.kv_write("secret/data/app", {"key": "value"})

        mock_client.secrets.kv.v2.create_or_update_secret.assert_called_once_with(
            path="secret/data/app", secret={"key": "value"}
        )

    @patch("app.clients.vault.hvac.Client")
    def test_kv_write_rejects_null_chars(self, mock_hvac_class: MagicMock) -> None:
        """kv_write() raises ValueError if data contains null characters."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client

        client = VaultClient(addr="http://vault:8200", token="tok")
        with pytest.raises(ValueError, match="null characters"):
            client.kv_write("path", {"key": "value\x00bad"})


class TestVaultClientKvRead:
    """Test kv_read() method."""

    @patch("app.clients.vault.hvac.Client")
    def test_kv_read_returns_data(self, mock_hvac_class: MagicMock) -> None:
        """kv_read() returns VaultKvReadResponse with data."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"key": "val"}, "metadata": {"version": 1}}
        }

        client = VaultClient(addr="http://vault:8200", token="tok")
        result = client.kv_read("secret/data/app")

        assert result.data == {"key": "val"}
        assert result.metadata == {"version": 1}

    @patch("app.clients.vault.hvac.Client")
    def test_kv_read_missing_path_returns_empty(self, mock_hvac_class: MagicMock) -> None:
        """kv_read() returns empty data for InvalidPath."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.secrets.kv.v2.read_secret_version.side_effect = hvac.exceptions.InvalidPath(
            "not found"
        )

        client = VaultClient(addr="http://vault:8200", token="tok")
        result = client.kv_read("secret/data/missing")

        assert result.data == {}


class TestVaultClientSecurityLogging:
    """Test that sensitive data is not logged."""

    @patch("app.clients.vault.hvac.Client")
    def test_no_token_logging(self, mock_hvac_class: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
        """Vault operations do not log token values."""
        mock_client = MagicMock()
        mock_hvac_class.return_value = mock_client
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"key": "val"}, "metadata": {}}
        }

        client = VaultClient(addr="http://vault:8200", token="secret_token_12345")
        with caplog.at_level("INFO"):
            client.kv_read("secret/data/app")

        assert "secret_token_12345" not in caplog.text
