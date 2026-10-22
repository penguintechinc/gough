"""Tests for cloud backend secrets managers."""

import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from app.secrets.aws_secrets import AWSSecretsManager
from app.secrets.gcp_secrets import GCPSecretsManager
from app.secrets.azure_keyvault import AzureKeyVaultSecretsManager
from app.secrets.vault import VaultSecretsManager
from app.secrets.infisical import InfisicalSecretsManager
from app.secrets.base import SecretNotFoundError, SecretsManagerError


class TestAWSSecretsManager:
    @pytest.fixture
    def manager(self):
        return AWSSecretsManager()

    def test_normalize(self, manager):
        assert manager._normalize_path("a.b.c") == "a/b/c"

    @pytest.mark.asyncio
    async def test_get_json(self, mock_current_app, manager):
        mock_current_app.config["AWS_REGION"] = "us-east-1"
        manager._client = MagicMock()
        manager._client.get_secret_value.return_value = {"SecretString": json.dumps({"k": "v"})}
        result = await manager.get_secret("p")
        assert result == {"k": "v"}

    @pytest.mark.asyncio
    async def test_set(self, mock_current_app, manager):
        mock_current_app.config["AWS_REGION"] = "us-east-1"
        manager._client = MagicMock()
        assert await manager.set_secret("p", {}) is True

    @pytest.mark.asyncio
    async def test_delete(self, mock_current_app, manager):
        mock_current_app.config["AWS_REGION"] = "us-east-1"
        manager._client = MagicMock()
        assert await manager.delete_secret("p") is True

    @pytest.mark.asyncio
    async def test_list(self, mock_current_app, manager):
        mock_current_app.config["AWS_REGION"] = "us-east-1"
        manager._client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"SecretList": [{"Name": "s1"}]}]
        manager._client.get_paginator.return_value = paginator
        result = await manager.list_secrets()
        assert "s1" in result


class TestGCPSecretsManager:
    @pytest.fixture
    def manager(self):
        return GCPSecretsManager()

    def test_normalize(self, manager):
        assert manager._normalize_name("a/b") == "a-b"

    def test_project_id(self, mock_current_app, manager):
        mock_current_app.config["GCP_PROJECT_ID"] = "proj"
        assert manager.project_id == "proj"

    @pytest.mark.asyncio
    async def test_get_json(self, mock_current_app, manager):
        mock_current_app.config["GCP_PROJECT_ID"] = "proj"
        manager._client = MagicMock()
        payload = MagicMock()
        payload.data.decode.return_value = json.dumps({"k": "v"})
        manager._client.access_secret_version.return_value = MagicMock(payload=payload)
        result = await manager.get_secret("s")
        assert result == {"k": "v"}

    @pytest.mark.asyncio
    async def test_set(self, mock_current_app, manager):
        mock_current_app.config["GCP_PROJECT_ID"] = "proj"
        manager._client = MagicMock()
        assert await manager.set_secret("s", {}) is True

    @pytest.mark.asyncio
    async def test_delete(self, mock_current_app, manager):
        mock_current_app.config["GCP_PROJECT_ID"] = "proj"
        manager._client = MagicMock()
        assert await manager.delete_secret("s") is True

    @pytest.mark.asyncio
    async def test_list(self, mock_current_app, manager):
        mock_current_app.config["GCP_PROJECT_ID"] = "proj"
        m = MagicMock()
        m.name = "projects/proj/secrets/s1"
        manager._client = MagicMock()
        manager._client.list_secrets.return_value = [m]
        result = await manager.list_secrets()
        assert "s1" in result


class TestAzureKeyVaultSecretsManager:
    @pytest.fixture
    def manager(self):
        return AzureKeyVaultSecretsManager()

    def test_normalize(self, manager):
        assert manager._normalize_name("a/b") == "a-b"

    @pytest.mark.asyncio
    async def test_get_json(self, mock_current_app, manager):
        mock_current_app.config["AZURE_VAULT_URL"] = "https://v.azure.net/"
        manager._client = MagicMock()
        secret = MagicMock()
        secret.value = json.dumps({"k": "v"})
        manager._client.get_secret.return_value = secret
        result = await manager.get_secret("s")
        assert result == {"k": "v"}

    @pytest.mark.asyncio
    async def test_set(self, mock_current_app, manager):
        mock_current_app.config["AZURE_VAULT_URL"] = "https://v.azure.net/"
        manager._client = MagicMock()
        assert await manager.set_secret("s", {}) is True

    @pytest.mark.asyncio
    async def test_delete_secret(self, mock_current_app, manager):
        mock_current_app.config["AZURE_VAULT_URL"] = "https://v.azure.net/"
        manager._client = MagicMock()
        manager._client.begin_delete_secret.return_value = MagicMock()
        assert await manager.delete_secret("s") is True

    @pytest.mark.asyncio
    async def test_list(self, mock_current_app, manager):
        mock_current_app.config["AZURE_VAULT_URL"] = "https://v.azure.net/"
        prop = MagicMock()
        prop.name = "s1"
        manager._client = MagicMock()
        manager._client.list_properties_of_secrets.return_value = [prop]
        result = await manager.list_secrets()
        assert "s1" in result


class TestVaultSecretsManager:
    @pytest.fixture
    def manager(self):
        return VaultSecretsManager()

    @pytest.mark.asyncio
    async def test_authenticate_token(self, mock_current_app, manager):
        mock_current_app.config["VAULT_ADDR"] = "http://vault:8200"
        mock_current_app.config["VAULT_TOKEN"] = "token"
        manager._client = MagicMock()
        manager._client.is_authenticated = MagicMock(return_value=True)
        await manager._authenticate()
        assert manager._authenticated is True

    @pytest.mark.asyncio
    async def test_get_secret(self, mock_current_app, manager):
        mock_current_app.config["VAULT_ADDR"] = "http://vault:8200"
        mock_current_app.config["VAULT_TOKEN"] = "token"
        manager._client = MagicMock()
        manager._client.is_authenticated = MagicMock(return_value=True)
        manager._client.secrets.kv.v2.read_secret_version.return_value = {"data": {"data": {"k": "v"}}}
        result = await manager.get_secret("p")
        assert result == {"k": "v"}

    @pytest.mark.asyncio
    async def test_set(self, mock_current_app, manager):
        mock_current_app.config["VAULT_ADDR"] = "http://vault:8200"
        mock_current_app.config["VAULT_TOKEN"] = "token"
        manager._client = MagicMock()
        manager._client.is_authenticated = MagicMock(return_value=True)
        assert await manager.set_secret("p", {}) is True

    @pytest.mark.asyncio
    async def test_delete(self, mock_current_app, manager):
        mock_current_app.config["VAULT_ADDR"] = "http://vault:8200"
        mock_current_app.config["VAULT_TOKEN"] = "token"
        manager._client = MagicMock()
        manager._client.is_authenticated = MagicMock(return_value=True)
        assert await manager.delete_secret("p") is True

    @pytest.mark.asyncio
    async def test_list(self, mock_current_app, manager):
        mock_current_app.config["VAULT_ADDR"] = "http://vault:8200"
        mock_current_app.config["VAULT_TOKEN"] = "token"
        manager._client = MagicMock()
        manager._client.is_authenticated = MagicMock(return_value=True)
        manager._client.secrets.kv.v2.list_secrets.return_value = {"data": {"keys": ["s1"]}}
        result = await manager.list_secrets()
        assert "s1" in result


class TestInfisicalSecretsManager:
    @pytest.fixture
    def manager(self):
        return InfisicalSecretsManager()

    @pytest.mark.asyncio
    async def test_get_secret(self, mock_current_app, manager):
        mock_current_app.config["INFISICAL_CLIENT_ID"] = "id"
        mock_current_app.config["INFISICAL_CLIENT_SECRET"] = "sec"
        mock_current_app.config["INFISICAL_PROJECT_ID"] = "proj"
        manager._client = MagicMock()
        secret = MagicMock()
        secret.secret_value = "val"
        manager._client.getSecret.return_value = secret
        result = await manager.get_secret("key")
        assert result == {"value": "val"}

    @pytest.mark.asyncio
    async def test_set(self, mock_current_app, manager):
        mock_current_app.config["INFISICAL_CLIENT_ID"] = "id"
        mock_current_app.config["INFISICAL_CLIENT_SECRET"] = "sec"
        mock_current_app.config["INFISICAL_PROJECT_ID"] = "proj"
        manager._client = MagicMock()
        assert await manager.set_secret("key", {}) is True

    @pytest.mark.asyncio
    async def test_delete(self, mock_current_app, manager):
        mock_current_app.config["INFISICAL_CLIENT_ID"] = "id"
        mock_current_app.config["INFISICAL_CLIENT_SECRET"] = "sec"
        mock_current_app.config["INFISICAL_PROJECT_ID"] = "proj"
        manager._client = MagicMock()
        assert await manager.delete_secret("key") is True

    @pytest.mark.asyncio
    async def test_list_secrets(self, mock_current_app, manager):
        mock_current_app.config["INFISICAL_CLIENT_ID"] = "id"
        mock_current_app.config["INFISICAL_CLIENT_SECRET"] = "sec"
        mock_current_app.config["INFISICAL_PROJECT_ID"] = "proj"
        manager._client = MagicMock()
        sec = MagicMock()
        sec.secret_key = "k1"
        manager._client.listSecrets.return_value = [sec]
        result = await manager.list_secrets()
        assert "k1" in result
