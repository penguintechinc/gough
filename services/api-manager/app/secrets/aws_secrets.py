"""AWS Secrets Manager Backend.

Integrates with AWS Secrets Manager for cloud-native secrets management.
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


class AWSSecretsManager(BaseSecretsManager):
    """Secrets manager using AWS Secrets Manager.

    Configuration (via environment or Flask config):
        AWS_REGION: AWS region (e.g., us-east-1)
        AWS_ACCESS_KEY_ID: AWS access key (optional if using IAM role)
        AWS_SECRET_ACCESS_KEY: AWS secret key (optional if using IAM role)
    """

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        """Get AWS Secrets Manager client."""
        if self._client is None:
            try:
                import boto3

                region = current_app.config.get("AWS_REGION", "us-east-1")
                access_key = current_app.config.get("AWS_ACCESS_KEY_ID", "")
                secret_key = current_app.config.get("AWS_SECRET_ACCESS_KEY", "")

                if access_key and secret_key:
                    self._client = boto3.client(
                        "secretsmanager",
                        region_name=region,
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key
                    )
                else:
                    # Use default credentials (IAM role, environment, etc.)
                    self._client = boto3.client(
                        "secretsmanager",
                        region_name=region
                    )

                log.info(f"Connected to AWS Secrets Manager in {region}")

            except ImportError:
                raise SecretsManagerError(
                    "boto3 package not installed. "
                    "Install with: pip install boto3"
                )

        return self._client

    def _normalize_path(self, path: str) -> str:
        """Normalize path to AWS secret name format."""
        # AWS uses / as separator, same as our convention
        return path.replace(".", "/")

    async def get_secret(self, path: str) -> dict[str, Any]:
        """Retrieve a secret from AWS Secrets Manager."""
        try:
            from botocore.exceptions import ClientError

            secret_name = self._normalize_path(path)

            response = await asyncio.to_thread(
                self.client.get_secret_value,
                SecretId=secret_name
            )

            # Secret can be string or binary
            if "SecretString" in response:
                secret_string = response["SecretString"]
                try:
                    return json.loads(secret_string)
                except json.JSONDecodeError:
                    return {"value": secret_string}
            else:
                import base64
                decoded = base64.b64decode(response["SecretBinary"])
                return {"value": decoded.decode()}

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "ResourceNotFoundException":
                raise SecretNotFoundError(path)
            elif error_code == "AccessDeniedException":
                raise SecretsManagerError(f"Access denied to secret: {path}")
            else:
                raise SecretsManagerError(f"AWS error: {e}")

    async def set_secret(self, path: str, data: dict[str, Any]) -> bool:
        """Store or update a secret in AWS Secrets Manager."""
        try:
            from botocore.exceptions import ClientError

            secret_name = self._normalize_path(path)
            secret_string = json.dumps(data)

            try:
                # Try to update existing secret
                await asyncio.to_thread(
                    self.client.put_secret_value,
                    SecretId=secret_name,
                    SecretString=secret_string
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    # Create new secret
                    await asyncio.to_thread(
                        self.client.create_secret,
                        Name=secret_name,
                        SecretString=secret_string
                    )
                else:
                    raise

            log.info(f"Secret stored in AWS at path: {path}")
            return True

        except ClientError as e:
            raise SecretsManagerError(f"AWS error storing secret: {e}")

    async def delete_secret(self, path: str) -> bool:
        """Delete a secret from AWS Secrets Manager."""
        try:
            from botocore.exceptions import ClientError

            secret_name = self._normalize_path(path)

            await asyncio.to_thread(
                self.client.delete_secret,
                SecretId=secret_name,
                ForceDeleteWithoutRecovery=True
            )

            log.info(f"Secret deleted from AWS at path: {path}")
            return True

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return False
            raise SecretsManagerError(f"AWS error deleting secret: {e}")

    async def list_secrets(self, path: str = "") -> list[str]:
        """List secrets in AWS Secrets Manager."""
        try:
            secrets = []

            prefix = self._normalize_path(path) if path else ""

            paginator = await asyncio.to_thread(
                self.client.get_paginator,
                "list_secrets"
            )

            for page in paginator.paginate():
                for secret in page.get("SecretList", []):
                    name = secret["Name"]
                    if not prefix or name.startswith(prefix):
                        secrets.append(name)

            return sorted(secrets)

        except Exception as e:
            raise SecretsManagerError(f"AWS error listing secrets: {e}")
