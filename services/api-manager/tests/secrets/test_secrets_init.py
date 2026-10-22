"""Tests for secrets module initialization."""

import pytest
from unittest.mock import patch

from app.secrets import (
    get_secrets_manager,
    register_backend,
    get_backend_registry,
    BACKEND_REGISTRY,
    BaseSecretsManager,
)
from app.secrets.encrypted_db import EncryptedDBSecretsManager


@pytest.mark.asyncio
async def test_get_secrets_manager_default(mock_current_app):
    """Test default backend is encrypted_db."""
    manager = await get_secrets_manager()
    assert isinstance(manager, EncryptedDBSecretsManager)


@pytest.mark.asyncio
async def test_get_secrets_manager_with_override(mock_current_app):
    """Test backend override."""
    manager = await get_secrets_manager(backend="encrypted_db")
    assert isinstance(manager, EncryptedDBSecretsManager)


@pytest.mark.asyncio
async def test_get_secrets_manager_respects_config(mock_current_app):
    """Test respects SECRETS_BACKEND config."""
    mock_current_app.config["SECRETS_BACKEND"] = "encrypted_db"
    manager = await get_secrets_manager()
    assert isinstance(manager, EncryptedDBSecretsManager)


@pytest.mark.asyncio
async def test_get_secrets_manager_invalid_backend(mock_current_app):
    """Test invalid backend raises ValueError."""
    with pytest.raises(ValueError) as exc:
        await get_secrets_manager(backend="nonexistent")
    assert "Unknown secrets backend" in str(exc.value)


@pytest.mark.asyncio
async def test_get_secrets_manager_creates_instances(mock_current_app):
    """Test each call creates new instance."""
    m1 = await get_secrets_manager()
    m2 = await get_secrets_manager()
    assert m1 is not m2
    assert type(m1) == type(m2)


def test_register_backend():
    """Test backend registration."""
    from app.secrets import _BACKENDS

    class TestBackend(BaseSecretsManager):
        async def get_secret(self, path):
            return {}
        async def set_secret(self, path, data):
            return True
        async def delete_secret(self, path):
            return True
        async def list_secrets(self, path=""):
            return []

    register_backend("test", TestBackend)
    assert "test" in _BACKENDS
    assert _BACKENDS["test"] == TestBackend


def test_get_backend_registry():
    """Test registry retrieval."""
    registry = get_backend_registry()
    assert isinstance(registry, dict)
    assert "encrypted_db" in registry
    assert registry["encrypted_db"] == EncryptedDBSecretsManager


def test_backend_registry_constant():
    """Test BACKEND_REGISTRY is initialized."""
    assert isinstance(BACKEND_REGISTRY, dict)
    assert "encrypted_db" in BACKEND_REGISTRY


def test_all_exports():
    """Test all exports available."""
    from app import secrets

    assert hasattr(secrets, "BaseSecretsManager")
    assert hasattr(secrets, "SecretAccessError")
    assert hasattr(secrets, "SecretNotFoundError")
    assert hasattr(secrets, "SecretValidationError")
    assert hasattr(secrets, "SecretsManagerError")
    assert hasattr(secrets, "get_secrets_manager")
    assert hasattr(secrets, "register_backend")
    assert hasattr(secrets, "get_backend_registry")
    assert hasattr(secrets, "BACKEND_REGISTRY")
