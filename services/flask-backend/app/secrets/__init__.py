"""Secrets Management Module for Gough.

This module provides a unified interface for managing secrets across multiple
backends including:
- Encrypted Database (default, using Fernet encryption)
- HashiCorp Vault
- Infisical
- AWS Secrets Manager
- GCP Secret Manager
- Azure Key Vault

Usage:
    from app.secrets import get_secrets_manager

    secrets = get_secrets_manager()
    secret_data = secrets.get_secret("cloud/aws/credentials")
    secrets.set_secret("cloud/aws/credentials", {"access_key": "...", "secret_key": "..."})
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from flask import current_app

from .base import BaseSecretsManager
from .encrypted_db import EncryptedDBSecretsManager

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# Registry of available backends
_BACKENDS: dict[str, type[BaseSecretsManager]] = {}


def register_backend(name: str, backend_class: type[BaseSecretsManager]) -> None:
    """Register a secrets backend."""
    _BACKENDS[name] = backend_class


def get_secrets_manager(backend: str | None = None) -> BaseSecretsManager:
    """Get the configured secrets manager instance.

    Args:
        backend: Optional backend name override. If not provided,
                 uses SECRETS_BACKEND from config.

    Returns:
        Configured secrets manager instance.

    Raises:
        ValueError: If the requested backend is not available.
    """
    if backend is None:
        backend = current_app.config.get("SECRETS_BACKEND", "encrypted_db")

    # Lazy import backends to avoid circular imports
    _ensure_backends_registered()

    if backend not in _BACKENDS:
        available = ", ".join(_BACKENDS.keys())
        raise ValueError(
            f"Unknown secrets backend: {backend}. Available: {available}"
        )

    backend_class = _BACKENDS[backend]
    return backend_class()


def _ensure_backends_registered() -> None:
    """Ensure all backends are registered."""
    if _BACKENDS:
        return

    # Register encrypted_db (always available)
    register_backend("encrypted_db", EncryptedDBSecretsManager)

    # Try to register optional backends
    try:
        from .vault import VaultSecretsManager
        register_backend("vault", VaultSecretsManager)
    except ImportError:
        log.debug("Vault backend not available (hvac not installed)")

    try:
        from .infisical import InfisicalSecretsManager
        register_backend("infisical", InfisicalSecretsManager)
    except ImportError:
        log.debug("Infisical backend not available")

    try:
        from .aws_secrets import AWSSecretsManager
        register_backend("aws", AWSSecretsManager)
    except ImportError:
        log.debug("AWS Secrets Manager not available (boto3 not installed)")

    try:
        from .gcp_secrets import GCPSecretsManager
        register_backend("gcp", GCPSecretsManager)
    except ImportError:
        log.debug("GCP Secret Manager not available")

    try:
        from .azure_keyvault import AzureKeyVaultSecretsManager
        register_backend("azure", AzureKeyVaultSecretsManager)
    except ImportError:
        log.debug("Azure Key Vault not available")


__all__ = [
    "BaseSecretsManager",
    "get_secrets_manager",
    "register_backend",
]
