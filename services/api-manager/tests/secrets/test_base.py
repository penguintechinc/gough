"""Tests for the base secrets manager module."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.secrets.base import (
    BaseSecretsManager,
    SecretAccessError,
    SecretNotFoundError,
    SecretValidationError,
    SecretsManagerError,
)


class ConcreteSecretsManager(BaseSecretsManager):
    """Concrete implementation of BaseSecretsManager for testing."""

    async def get_secret(self, path: str) -> dict:
        """Mock implementation."""
        if path == "missing":
            raise SecretNotFoundError(path)
        return {"key": f"value_for_{path}"}

    async def set_secret(self, path: str, data: dict) -> bool:
        """Mock implementation."""
        return True

    async def delete_secret(self, path: str) -> bool:
        """Mock implementation."""
        return True

    async def list_secrets(self, path: str = "") -> list[str]:
        """Mock implementation."""
        return ["secret1", "secret2"]


@pytest.mark.asyncio
async def test_exists_returns_true_when_secret_exists():
    """Test that exists() returns True for existing secrets."""
    manager = ConcreteSecretsManager()
    result = await manager.exists("some/path")
    assert result is True


@pytest.mark.asyncio
async def test_exists_returns_false_when_secret_missing():
    """Test that exists() returns False for missing secrets."""
    manager = ConcreteSecretsManager()
    result = await manager.exists("missing")
    assert result is False


@pytest.mark.asyncio
async def test_get_or_default_returns_secret_when_exists():
    """Test that get_or_default() returns the secret when it exists."""
    manager = ConcreteSecretsManager()
    result = await manager.get_or_default("some/path")
    assert result == {"key": "value_for_some/path"}


@pytest.mark.asyncio
async def test_get_or_default_returns_default_when_missing():
    """Test that get_or_default() returns default for missing secrets."""
    manager = ConcreteSecretsManager()
    result = await manager.get_or_default("missing", default={"default": "value"})
    assert result == {"default": "value"}


@pytest.mark.asyncio
async def test_get_or_default_returns_empty_dict_when_missing_no_default():
    """Test that get_or_default() returns empty dict when missing and no default provided."""
    manager = ConcreteSecretsManager()
    result = await manager.get_or_default("missing")
    assert result == {}


def test_secret_not_found_error_initialization():
    """Test SecretNotFoundError initialization."""
    error = SecretNotFoundError("test/path")
    assert error.path == "test/path"
    assert str(error) == "Secret not found: test/path"


def test_secret_access_error_initialization():
    """Test SecretAccessError initialization."""
    error = SecretAccessError("test/path", reason="Insufficient permissions")
    assert error.path == "test/path"
    assert error.reason == "Insufficient permissions"
    assert "Insufficient permissions" in str(error)


def test_secret_access_error_without_reason():
    """Test SecretAccessError without reason."""
    error = SecretAccessError("test/path")
    assert error.path == "test/path"
    assert error.reason == ""
    assert str(error) == "Access denied to secret: test/path"


def test_secret_validation_error():
    """Test SecretValidationError."""
    error = SecretValidationError("Invalid format")
    assert "Invalid format" in str(error)


def test_secrets_manager_error():
    """Test SecretsManagerError."""
    error = SecretsManagerError("Generic error")
    assert "Generic error" in str(error)
