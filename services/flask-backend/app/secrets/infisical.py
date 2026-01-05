"""Infisical Secrets Backend.

Integrates with Infisical for modern secrets management.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import current_app

from .base import (
    BaseSecretsManager,
    SecretNotFoundError,
    SecretsManagerError,
)

log = logging.getLogger(__name__)


class InfisicalSecretsManager(BaseSecretsManager):
    """Secrets manager using Infisical.

    Configuration (via environment or Flask config):
        INFISICAL_CLIENT_ID: Machine identity client ID
        INFISICAL_CLIENT_SECRET: Machine identity client secret
        INFISICAL_PROJECT_ID: Project ID
        INFISICAL_ENVIRONMENT: Environment (dev, staging, prod)
    """

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        """Get Infisical client."""
        if self._client is None:
            try:
                from infisical_client import (
                    ClientSettings,
                    InfisicalClient,
                    AuthenticationOptions,
                    UniversalAuthMethod,
                )

                client_id = current_app.config.get("INFISICAL_CLIENT_ID", "")
                client_secret = current_app.config.get("INFISICAL_CLIENT_SECRET", "")

                if not client_id or not client_secret:
                    raise SecretsManagerError(
                        "Infisical credentials not configured. "
                        "Set INFISICAL_CLIENT_ID and INFISICAL_CLIENT_SECRET"
                    )

                self._client = InfisicalClient(ClientSettings(
                    auth=AuthenticationOptions(
                        universal_auth=UniversalAuthMethod(
                            client_id=client_id,
                            client_secret=client_secret
                        )
                    )
                ))
                log.info("Connected to Infisical")

            except ImportError:
                raise SecretsManagerError(
                    "infisical-python package not installed. "
                    "Install with: pip install infisical-python"
                )

        return self._client

    @property
    def project_id(self) -> str:
        return current_app.config.get("INFISICAL_PROJECT_ID", "")

    @property
    def environment(self) -> str:
        return current_app.config.get("INFISICAL_ENVIRONMENT", "dev")

    def get_secret(self, path: str) -> dict[str, Any]:
        """Retrieve a secret from Infisical."""
        try:
            from infisical_client import GetSecretOptions

            # Parse path to get secret name and folder
            parts = path.rsplit("/", 1)
            if len(parts) == 2:
                folder, secret_name = parts
                folder = f"/{folder}"
            else:
                folder = "/"
                secret_name = parts[0]

            secret = self.client.getSecret(GetSecretOptions(
                secret_name=secret_name,
                project_id=self.project_id,
                environment=self.environment,
                path=folder
            ))

            return {"value": secret.secret_value}

        except Exception as e:
            if "not found" in str(e).lower():
                raise SecretNotFoundError(path)
            raise SecretsManagerError(f"Infisical error: {e}")

    def set_secret(self, path: str, data: dict[str, Any]) -> bool:
        """Store or update a secret in Infisical."""
        try:
            from infisical_client import CreateSecretOptions, UpdateSecretOptions

            # Parse path
            parts = path.rsplit("/", 1)
            if len(parts) == 2:
                folder, secret_name = parts
                folder = f"/{folder}"
            else:
                folder = "/"
                secret_name = parts[0]

            # Get the value to store
            if "value" in data:
                secret_value = str(data["value"])
            else:
                import json
                secret_value = json.dumps(data)

            # Try to update, create if doesn't exist
            try:
                self.client.updateSecret(UpdateSecretOptions(
                    secret_name=secret_name,
                    secret_value=secret_value,
                    project_id=self.project_id,
                    environment=self.environment,
                    path=folder
                ))
            except Exception:
                self.client.createSecret(CreateSecretOptions(
                    secret_name=secret_name,
                    secret_value=secret_value,
                    project_id=self.project_id,
                    environment=self.environment,
                    path=folder
                ))

            log.info(f"Secret stored in Infisical at path: {path}")
            return True

        except Exception as e:
            raise SecretsManagerError(f"Infisical error storing secret: {e}")

    def delete_secret(self, path: str) -> bool:
        """Delete a secret from Infisical."""
        try:
            from infisical_client import DeleteSecretOptions

            parts = path.rsplit("/", 1)
            if len(parts) == 2:
                folder, secret_name = parts
                folder = f"/{folder}"
            else:
                folder = "/"
                secret_name = parts[0]

            self.client.deleteSecret(DeleteSecretOptions(
                secret_name=secret_name,
                project_id=self.project_id,
                environment=self.environment,
                path=folder
            ))

            log.info(f"Secret deleted from Infisical at path: {path}")
            return True

        except Exception as e:
            if "not found" in str(e).lower():
                return False
            raise SecretsManagerError(f"Infisical error deleting secret: {e}")

    def list_secrets(self, path: str = "") -> list[str]:
        """List secrets in Infisical at the given path."""
        try:
            from infisical_client import ListSecretsOptions

            folder = f"/{path}" if path else "/"

            secrets = self.client.listSecrets(ListSecretsOptions(
                project_id=self.project_id,
                environment=self.environment,
                path=folder
            ))

            prefix = f"{path}/" if path else ""
            return [f"{prefix}{s.secret_key}" for s in secrets]

        except Exception as e:
            raise SecretsManagerError(f"Infisical error listing secrets: {e}")
