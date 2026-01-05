"""Base class for secrets management backends.

All secrets backends must implement this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseSecretsManager(ABC):
    """Abstract base class for secrets management.

    All secrets backends must implement this interface to provide
    consistent access to secrets storage.
    """

    @abstractmethod
    def get_secret(self, path: str) -> dict[str, Any]:
        """Retrieve a secret by path.

        Args:
            path: The path/key identifying the secret (e.g., "cloud/aws/credentials")

        Returns:
            Dictionary containing the secret data.

        Raises:
            SecretNotFoundError: If the secret doesn't exist.
            SecretsManagerError: If there's an error accessing the backend.
        """
        pass

    @abstractmethod
    def set_secret(self, path: str, data: dict[str, Any]) -> bool:
        """Store or update a secret.

        Args:
            path: The path/key for the secret.
            data: Dictionary containing the secret data to store.

        Returns:
            True if the operation was successful.

        Raises:
            SecretsManagerError: If there's an error storing the secret.
        """
        pass

    @abstractmethod
    def delete_secret(self, path: str) -> bool:
        """Delete a secret.

        Args:
            path: The path/key of the secret to delete.

        Returns:
            True if the secret was deleted, False if it didn't exist.

        Raises:
            SecretsManagerError: If there's an error deleting the secret.
        """
        pass

    @abstractmethod
    def list_secrets(self, path: str = "") -> list[str]:
        """List secrets at a given path.

        Args:
            path: The path prefix to list secrets under. Empty string for root.

        Returns:
            List of secret paths/keys.

        Raises:
            SecretsManagerError: If there's an error listing secrets.
        """
        pass

    def exists(self, path: str) -> bool:
        """Check if a secret exists.

        Args:
            path: The path/key to check.

        Returns:
            True if the secret exists, False otherwise.
        """
        try:
            self.get_secret(path)
            return True
        except SecretNotFoundError:
            return False

    def get_or_default(
        self, path: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Get a secret or return a default value.

        Args:
            path: The path/key of the secret.
            default: Default value if secret doesn't exist.

        Returns:
            The secret data or the default value.
        """
        try:
            return self.get_secret(path)
        except SecretNotFoundError:
            return default or {}


class SecretsManagerError(Exception):
    """Base exception for secrets manager errors."""

    pass


class SecretNotFoundError(SecretsManagerError):
    """Raised when a secret is not found."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"Secret not found: {path}")


class SecretAccessError(SecretsManagerError):
    """Raised when access to a secret is denied."""

    def __init__(self, path: str, reason: str = "") -> None:
        self.path = path
        self.reason = reason
        msg = f"Access denied to secret: {path}"
        if reason:
            msg += f" - {reason}"
        super().__init__(msg)


class SecretValidationError(SecretsManagerError):
    """Raised when secret data is invalid."""

    pass
