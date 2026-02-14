"""Azure Key Vault Secrets Backend.

Integrates with Azure Key Vault for cloud-native secrets management.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from quart import current_app

from .base import (
    BaseSecretsManager,
    SecretNotFoundError,
    SecretsManagerError,
)

log = logging.getLogger(__name__)


class AzureKeyVaultSecretsManager(BaseSecretsManager):
    """Secrets manager using Azure Key Vault.

    Configuration (via environment or Flask config):
        AZURE_VAULT_URL: Key Vault URL (e.g., https://myvault.vault.azure.net/)
        AZURE_CLIENT_ID: Service principal client ID
        AZURE_CLIENT_SECRET: Service principal client secret
        AZURE_TENANT_ID: Azure AD tenant ID
    """

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        """Get Azure Key Vault client."""
        if self._client is None:
            try:
                from azure.keyvault.secrets import SecretClient
                from azure.identity import ClientSecretCredential, DefaultAzureCredential

                vault_url = current_app.config.get("AZURE_VAULT_URL", "")
                if not vault_url:
                    raise SecretsManagerError("AZURE_VAULT_URL not configured")

                client_id = current_app.config.get("AZURE_CLIENT_ID", "")
                client_secret = current_app.config.get("AZURE_CLIENT_SECRET", "")
                tenant_id = current_app.config.get("AZURE_TENANT_ID", "")

                if client_id and client_secret and tenant_id:
                    credential = ClientSecretCredential(
                        tenant_id=tenant_id,
                        client_id=client_id,
                        client_secret=client_secret
                    )
                else:
                    # Use default credentials (managed identity, environment, etc.)
                    credential = DefaultAzureCredential()

                self._client = SecretClient(
                    vault_url=vault_url,
                    credential=credential
                )

                log.info(f"Connected to Azure Key Vault at {vault_url}")

            except ImportError:
                raise SecretsManagerError(
                    "azure-keyvault-secrets and azure-identity packages not installed. "
                    "Install with: pip install azure-keyvault-secrets azure-identity"
                )

        return self._client

    def _normalize_name(self, path: str) -> str:
        """Normalize path to Azure secret name format.

        Azure Key Vault secret names can only contain alphanumeric and hyphens.
        """
        return path.replace("/", "-").replace(".", "-").replace("_", "-")

    async def get_secret(self, path: str) -> dict[str, Any]:
        """Retrieve a secret from Azure Key Vault."""
        try:
            from azure.core.exceptions import ResourceNotFoundError

            name = self._normalize_name(path)
            secret = await asyncio.to_thread(
                self.client.get_secret,
                name
            )

            value = secret.value

            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return {"value": value}

        except ResourceNotFoundError:
            raise SecretNotFoundError(path)
        except Exception as e:
            raise SecretsManagerError(f"Azure error: {e}")

    async def set_secret(self, path: str, data: dict[str, Any]) -> bool:
        """Store or update a secret in Azure Key Vault."""
        try:
            name = self._normalize_name(path)
            value = json.dumps(data)

            await asyncio.to_thread(
                self.client.set_secret,
                name,
                value
            )

            log.info(f"Secret stored in Azure Key Vault at path: {path}")
            return True

        except Exception as e:
            raise SecretsManagerError(f"Azure error storing secret: {e}")

    async def delete_secret(self, path: str) -> bool:
        """Delete a secret from Azure Key Vault."""
        try:
            from azure.core.exceptions import ResourceNotFoundError

            name = self._normalize_name(path)

            # Start deletion
            poller = await asyncio.to_thread(
                self.client.begin_delete_secret,
                name
            )
            await asyncio.to_thread(poller.wait)

            # Purge if soft-delete is enabled
            try:
                await asyncio.to_thread(
                    self.client.purge_deleted_secret,
                    name
                )
            except Exception:
                pass  # Purge may not be allowed or soft-delete disabled

            log.info(f"Secret deleted from Azure Key Vault at path: {path}")
            return True

        except ResourceNotFoundError:
            return False
        except Exception as e:
            raise SecretsManagerError(f"Azure error deleting secret: {e}")

    async def list_secrets(self, path: str = "") -> list[str]:
        """List secrets in Azure Key Vault."""
        try:
            secrets = []
            prefix = self._normalize_name(path) if path else ""

            secret_properties_list = await asyncio.to_thread(
                self.client.list_properties_of_secrets
            )

            for secret_properties in secret_properties_list:
                name = secret_properties.name
                if not prefix or name.startswith(prefix):
                    secrets.append(name)

            return sorted(secrets)

        except Exception as e:
            raise SecretsManagerError(f"Azure error listing secrets: {e}")
