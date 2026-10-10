"""Tests for app/clients/cloud_kms.py (cloud-KMS LUKS key sealing)."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from app.clients.cloud_kms import (
    CloudKMSClient,
    CloudKMSEncryptResult,
    CloudKMSError,
)


class TestCloudKMSClientInit:
    """Test CloudKMSClient construction and validation."""

    def test_init_rejects_unsupported_provider(self) -> None:
        with pytest.raises(CloudKMSError, match="Unsupported cloud KMS provider"):
            CloudKMSClient(provider="oracle", key_id="k1")

    def test_init_rejects_missing_key_id(self) -> None:
        with pytest.raises(CloudKMSError, match="key_id is required"):
            CloudKMSClient(provider="aws", key_id="")

    def test_init_rejects_azure_without_vault_url(self) -> None:
        with pytest.raises(CloudKMSError, match="vault_url is required"):
            CloudKMSClient(provider="azure", key_id="my-key")

    def test_init_normalises_provider_case(self) -> None:
        client = CloudKMSClient(provider="AWS", key_id="k1")
        assert client.provider == "aws"

    def test_init_azure_accepts_vault_url(self) -> None:
        client = CloudKMSClient(
            provider="azure", key_id="my-key", vault_url="https://v.vault.azure.net/"
        )
        assert client.vault_url == "https://v.vault.azure.net"


class TestCloudKMSClientFromEnv:
    """Test CloudKMSClient.from_env()."""

    def test_from_env_missing_provider_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GOUGH_CLOUD_KMS_PROVIDER", raising=False)
        monkeypatch.delenv("GOUGH_CLOUD_KMS_KEY_ID", raising=False)
        with pytest.raises(CloudKMSError, match="must both be"):
            CloudKMSClient.from_env()

    def test_from_env_missing_key_id_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOUGH_CLOUD_KMS_PROVIDER", "aws")
        monkeypatch.delenv("GOUGH_CLOUD_KMS_KEY_ID", raising=False)
        with pytest.raises(CloudKMSError, match="must both be"):
            CloudKMSClient.from_env()

    def test_from_env_builds_aws_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOUGH_CLOUD_KMS_PROVIDER", "aws")
        monkeypatch.setenv("GOUGH_CLOUD_KMS_KEY_ID", "arn:aws:kms:us-east-1:1:key/a")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        client = CloudKMSClient.from_env()
        assert client.provider == "aws"
        assert client.region == "us-east-1"

    def test_from_env_builds_azure_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOUGH_CLOUD_KMS_PROVIDER", "azure")
        monkeypatch.setenv("GOUGH_CLOUD_KMS_KEY_ID", "my-key")
        monkeypatch.setenv("GOUGH_AZURE_KEY_VAULT_URL", "https://v.vault.azure.net")
        client = CloudKMSClient.from_env()
        assert client.provider == "azure"
        assert client.vault_url == "https://v.vault.azure.net"


class TestCloudKMSClientEncryptAWS:
    """Test AWS KMS encrypt dispatch via boto3."""

    def test_encrypt_aws_success(self) -> None:
        fake_boto3_client = MagicMock()
        fake_boto3_client.encrypt.return_value = {
            "CiphertextBlob": b"aws-ciphertext",
            "KeyId": "arn:aws:kms:us-east-1:1:key/a",
        }
        client = CloudKMSClient(
            provider="aws",
            key_id="arn:aws:kms:us-east-1:1:key/a",
            boto3_client=fake_boto3_client,
        )
        result = client.encrypt(b"raw-luks-key")

        assert isinstance(result, CloudKMSEncryptResult)
        assert result.ciphertext == b"aws-ciphertext"
        assert result.provider == "aws"
        fake_boto3_client.encrypt.assert_called_once_with(
            KeyId="arn:aws:kms:us-east-1:1:key/a", Plaintext=b"raw-luks-key"
        )

    def test_encrypt_aws_lazily_constructs_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_boto3_client = MagicMock()
        fake_boto3_client.encrypt.return_value = {"CiphertextBlob": b"x"}
        with patch("boto3.client", return_value=fake_boto3_client) as mock_boto3:
            client = CloudKMSClient(
                provider="aws", key_id="k1", region="eu-west-1"
            )
            client.encrypt(b"raw")
        mock_boto3.assert_called_once_with("kms", region_name="eu-west-1")

    def test_encrypt_aws_api_error_raises_cloud_kms_error(self) -> None:
        fake_boto3_client = MagicMock()
        fake_boto3_client.encrypt.side_effect = Exception("AccessDeniedException")
        client = CloudKMSClient(provider="aws", key_id="k1", boto3_client=fake_boto3_client)
        with pytest.raises(CloudKMSError, match="AWS KMS encrypt failed"):
            client.encrypt(b"raw")

    def test_encrypt_aws_no_ciphertext_raises(self) -> None:
        fake_boto3_client = MagicMock()
        fake_boto3_client.encrypt.return_value = {}
        client = CloudKMSClient(provider="aws", key_id="k1", boto3_client=fake_boto3_client)
        with pytest.raises(CloudKMSError, match="no CiphertextBlob"):
            client.encrypt(b"raw")


class TestCloudKMSClientEncryptGCP:
    """Test GCP Cloud KMS encrypt dispatch via REST + google-auth session."""

    def test_encrypt_gcp_success(self) -> None:
        fake_session = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "ciphertext": base64.b64encode(b"gcp-ciphertext").decode("ascii"),
            "name": "projects/p/locations/l/keyRings/r/cryptoKeys/k",
        }
        fake_session.post.return_value = fake_resp
        client = CloudKMSClient(
            provider="gcp",
            key_id="projects/p/locations/l/keyRings/r/cryptoKeys/k",
            session=fake_session,
        )
        result = client.encrypt(b"raw-luks-key")

        assert result.ciphertext == b"gcp-ciphertext"
        assert result.provider == "gcp"
        fake_resp.raise_for_status.assert_called_once()

    def test_encrypt_gcp_api_error_raises_cloud_kms_error(self) -> None:
        fake_session = MagicMock()
        fake_session.post.side_effect = Exception("network error")
        client = CloudKMSClient(provider="gcp", key_id="k1", session=fake_session)
        with pytest.raises(CloudKMSError, match="GCP Cloud KMS encrypt failed"):
            client.encrypt(b"raw")

    def test_encrypt_gcp_no_ciphertext_raises(self) -> None:
        fake_session = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {}
        fake_session.post.return_value = fake_resp
        client = CloudKMSClient(provider="gcp", key_id="k1", session=fake_session)
        with pytest.raises(CloudKMSError, match="no ciphertext"):
            client.encrypt(b"raw")


class TestCloudKMSClientEncryptAzure:
    """Test Azure Key Vault encrypt dispatch via REST + azure-identity token."""

    def test_encrypt_azure_success(self) -> None:
        fake_session = MagicMock()
        fake_resp = MagicMock()
        value_b64 = base64.urlsafe_b64encode(b"azure-ciphertext").decode("ascii").rstrip("=")
        fake_resp.json.return_value = {
            "value": value_b64,
            "kid": "https://v.vault.azure.net/keys/my-key/abc",
        }
        fake_session.post.return_value = fake_resp
        client = CloudKMSClient(
            provider="azure",
            key_id="my-key",
            vault_url="https://v.vault.azure.net",
            session=fake_session,
            azure_token="fake-token",
        )
        result = client.encrypt(b"raw-luks-key")

        assert result.ciphertext == b"azure-ciphertext"
        assert result.provider == "azure"
        _, kwargs = fake_session.post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer fake-token"

    def test_encrypt_azure_token_acquisition_failure_raises(self) -> None:
        client = CloudKMSClient(
            provider="azure", key_id="my-key", vault_url="https://v.vault.azure.net"
        )
        with (
            patch(
                "azure.identity.DefaultAzureCredential",
                side_effect=Exception("no credential found"),
            ),
            pytest.raises(CloudKMSError, match="Azure AD token acquisition failed"),
        ):
            client.encrypt(b"raw")

    def test_encrypt_azure_api_error_raises_cloud_kms_error(self) -> None:
        fake_session = MagicMock()
        fake_session.post.side_effect = Exception("network error")
        client = CloudKMSClient(
            provider="azure",
            key_id="my-key",
            vault_url="https://v.vault.azure.net",
            session=fake_session,
            azure_token="fake-token",
        )
        with pytest.raises(CloudKMSError, match="Azure Key Vault encrypt failed"):
            client.encrypt(b"raw")

    def test_encrypt_azure_no_value_raises(self) -> None:
        fake_session = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {}
        fake_session.post.return_value = fake_resp
        client = CloudKMSClient(
            provider="azure",
            key_id="my-key",
            vault_url="https://v.vault.azure.net",
            session=fake_session,
            azure_token="fake-token",
        )
        with pytest.raises(CloudKMSError, match="no value"):
            client.encrypt(b"raw")
