"""GCP Secret Manager Backend.

Integrates with Google Cloud Secret Manager.
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


class GCPSecretsManager(BaseSecretsManager):
    """Secrets manager using GCP Secret Manager.

    Configuration (via environment or Flask config):
        GCP_PROJECT_ID: GCP project ID
        GCP_CREDENTIALS_FILE: Path to service account JSON (optional)
        GOOGLE_APPLICATION_CREDENTIALS: Alternative to GCP_CREDENTIALS_FILE
    """

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        """Get GCP Secret Manager client."""
        if self._client is None:
            try:
                from google.cloud import secretmanager
                import os

                # Set credentials file if specified
                creds_file = current_app.config.get("GCP_CREDENTIALS_FILE", "")
                if creds_file:
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_file

                self._client = secretmanager.SecretManagerServiceClient()
                log.info("Connected to GCP Secret Manager")

            except ImportError:
                raise SecretsManagerError(
                    "google-cloud-secret-manager package not installed. "
                    "Install with: pip install google-cloud-secret-manager"
                )

        return self._client

    @property
    def project_id(self) -> str:
        project_id = current_app.config.get("GCP_PROJECT_ID", "")
        if not project_id:
            raise SecretsManagerError("GCP_PROJECT_ID not configured")
        return project_id

    def _normalize_name(self, path: str) -> str:
        """Normalize path to GCP secret name format."""
        # GCP secrets can't have / in names, use - instead
        return path.replace("/", "-").replace(".", "-")

    def _secret_path(self, path: str) -> str:
        """Get the full GCP secret path."""
        name = self._normalize_name(path)
        return f"projects/{self.project_id}/secrets/{name}"

    def _version_path(self, path: str, version: str = "latest") -> str:
        """Get the full GCP secret version path."""
        secret_path = self._secret_path(path)
        return f"{secret_path}/versions/{version}"

    async def get_secret(self, path: str) -> dict[str, Any]:
        """Retrieve a secret from GCP Secret Manager."""
        try:
            from google.api_core import exceptions

            version_path = self._version_path(path)
            response = await asyncio.to_thread(
                self.client.access_secret_version,
                name=version_path
            )

            payload = response.payload.data.decode("UTF-8")

            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return {"value": payload}

        except exceptions.NotFound:
            raise SecretNotFoundError(path)
        except exceptions.PermissionDenied:
            raise SecretsManagerError(f"Permission denied accessing secret: {path}")
        except Exception as e:
            raise SecretsManagerError(f"GCP error: {e}")

    async def set_secret(self, path: str, data: dict[str, Any]) -> bool:
        """Store or update a secret in GCP Secret Manager."""
        try:
            from google.api_core import exceptions

            secret_path = self._secret_path(path)
            payload = json.dumps(data).encode("UTF-8")

            # Try to add version to existing secret
            try:
                await asyncio.to_thread(
                    self.client.add_secret_version,
                    parent=secret_path,
                    payload={"data": payload}
                )
            except exceptions.NotFound:
                # Create new secret
                parent = f"projects/{self.project_id}"
                secret_id = self._normalize_name(path)

                await asyncio.to_thread(
                    self.client.create_secret,
                    parent=parent,
                    secret_id=secret_id,
                    secret={"replication": {"automatic": {}}}
                )

                # Add the first version
                await asyncio.to_thread(
                    self.client.add_secret_version,
                    parent=secret_path,
                    payload={"data": payload}
                )

            log.info(f"Secret stored in GCP at path: {path}")
            return True

        except Exception as e:
            raise SecretsManagerError(f"GCP error storing secret: {e}")

    async def delete_secret(self, path: str) -> bool:
        """Delete a secret from GCP Secret Manager."""
        try:
            from google.api_core import exceptions

            secret_path = self._secret_path(path)
            await asyncio.to_thread(
                self.client.delete_secret,
                name=secret_path
            )

            log.info(f"Secret deleted from GCP at path: {path}")
            return True

        except exceptions.NotFound:
            return False
        except Exception as e:
            raise SecretsManagerError(f"GCP error deleting secret: {e}")

    async def list_secrets(self, path: str = "") -> list[str]:
        """List secrets in GCP Secret Manager."""
        try:
            parent = f"projects/{self.project_id}"
            secrets = []

            prefix = self._normalize_name(path) if path else ""

            secrets_list = await asyncio.to_thread(
                self.client.list_secrets,
                parent=parent
            )

            for secret in secrets_list:
                # Extract name from full path
                name = secret.name.split("/")[-1]
                if not prefix or name.startswith(prefix):
                    secrets.append(name)

            return sorted(secrets)

        except Exception as e:
            raise SecretsManagerError(f"GCP error listing secrets: {e}")
