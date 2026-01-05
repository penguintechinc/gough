"""HashiCorp Vault Secrets Backend.

Integrates with HashiCorp Vault for enterprise-grade secrets management.
Supports both token and AppRole authentication.
"""

from __future__ import annotations

import logging
from typing import Any

import hvac
from flask import current_app

from .base import (
    BaseSecretsManager,
    SecretAccessError,
    SecretNotFoundError,
    SecretsManagerError,
)

log = logging.getLogger(__name__)


class VaultSecretsManager(BaseSecretsManager):
    """Secrets manager using HashiCorp Vault.

    Supports KV v2 secrets engine with token or AppRole authentication.

    Configuration (via environment or Flask config):
        VAULT_ADDR: Vault server address (e.g., http://vault:8200)
        VAULT_TOKEN: Vault token for authentication
        VAULT_ROLE_ID: AppRole role ID (alternative to token)
        VAULT_SECRET_ID: AppRole secret ID (alternative to token)
        VAULT_MOUNT_POINT: KV secrets engine mount point (default: "secret")
    """

    def __init__(self) -> None:
        self._client: hvac.Client | None = None
        self._authenticated = False

    @property
    def client(self) -> hvac.Client:
        """Get authenticated Vault client."""
        if self._client is None:
            vault_addr = current_app.config.get("VAULT_ADDR", "http://vault:8200")
            self._client = hvac.Client(url=vault_addr)

        if not self._authenticated:
            self._authenticate()

        return self._client

    @property
    def mount_point(self) -> str:
        """Get the KV secrets engine mount point."""
        return current_app.config.get("VAULT_MOUNT_POINT", "secret")

    def _authenticate(self) -> None:
        """Authenticate with Vault using configured method."""
        token = current_app.config.get("VAULT_TOKEN", "")
        role_id = current_app.config.get("VAULT_ROLE_ID", "")
        secret_id = current_app.config.get("VAULT_SECRET_ID", "")

        if role_id and secret_id:
            # AppRole authentication
            try:
                self._client.auth.approle.login(
                    role_id=role_id,
                    secret_id=secret_id
                )
                log.info("Authenticated with Vault using AppRole")
            except hvac.exceptions.InvalidRequest as e:
                raise SecretsManagerError(f"Vault AppRole authentication failed: {e}")
        elif token:
            # Token authentication
            self._client.token = token
            log.info("Authenticated with Vault using token")
        else:
            raise SecretsManagerError(
                "No Vault credentials configured. "
                "Set VAULT_TOKEN or VAULT_ROLE_ID/VAULT_SECRET_ID"
            )

        # Verify authentication
        try:
            if not self._client.is_authenticated():
                raise SecretsManagerError("Vault authentication failed")
        except hvac.exceptions.VaultDown:
            raise SecretsManagerError("Vault server is sealed or unavailable")

        self._authenticated = True

    def get_secret(self, path: str) -> dict[str, Any]:
        """Retrieve a secret from Vault KV v2."""
        try:
            result = self.client.secrets.kv.v2.read_secret_version(
                path=path,
                mount_point=self.mount_point
            )
            return result["data"]["data"]
        except hvac.exceptions.InvalidPath:
            raise SecretNotFoundError(path)
        except hvac.exceptions.Forbidden:
            raise SecretAccessError(path, "Insufficient permissions")
        except hvac.exceptions.VaultError as e:
            raise SecretsManagerError(f"Vault error reading secret: {e}")

    def set_secret(self, path: str, data: dict[str, Any]) -> bool:
        """Store or update a secret in Vault KV v2."""
        try:
            self.client.secrets.kv.v2.create_or_update_secret(
                path=path,
                secret=data,
                mount_point=self.mount_point
            )
            log.info(f"Secret stored in Vault at path: {path}")
            return True
        except hvac.exceptions.Forbidden:
            raise SecretAccessError(path, "Insufficient permissions to write")
        except hvac.exceptions.VaultError as e:
            raise SecretsManagerError(f"Vault error writing secret: {e}")

    def delete_secret(self, path: str) -> bool:
        """Delete a secret from Vault KV v2."""
        try:
            # Delete all versions and metadata
            self.client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=path,
                mount_point=self.mount_point
            )
            log.info(f"Secret deleted from Vault at path: {path}")
            return True
        except hvac.exceptions.InvalidPath:
            return False
        except hvac.exceptions.Forbidden:
            raise SecretAccessError(path, "Insufficient permissions to delete")
        except hvac.exceptions.VaultError as e:
            raise SecretsManagerError(f"Vault error deleting secret: {e}")

    def list_secrets(self, path: str = "") -> list[str]:
        """List secrets in Vault at the given path."""
        try:
            result = self.client.secrets.kv.v2.list_secrets(
                path=path,
                mount_point=self.mount_point
            )
            keys = result["data"]["keys"]

            # Build full paths
            prefix = f"{path}/" if path else ""
            return [f"{prefix}{key}" for key in keys]
        except hvac.exceptions.InvalidPath:
            return []
        except hvac.exceptions.Forbidden:
            raise SecretAccessError(path, "Insufficient permissions to list")
        except hvac.exceptions.VaultError as e:
            raise SecretsManagerError(f"Vault error listing secrets: {e}")

    def get_secret_metadata(self, path: str) -> dict[str, Any]:
        """Get metadata about a secret (versions, creation time, etc.)."""
        try:
            result = self.client.secrets.kv.v2.read_secret_metadata(
                path=path,
                mount_point=self.mount_point
            )
            return result["data"]
        except hvac.exceptions.InvalidPath:
            raise SecretNotFoundError(path)
        except hvac.exceptions.VaultError as e:
            raise SecretsManagerError(f"Vault error reading metadata: {e}")
