"""Encrypted Database Secrets Backend.

This is the default backend that stores secrets encrypted in the database
using Fernet symmetric encryption. No external services required.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from ..models import get_db
from .base import (
    BaseSecretsManager,
    SecretNotFoundError,
    SecretsManagerError,
)

log = logging.getLogger(__name__)


class EncryptedDBSecretsManager(BaseSecretsManager):
    """Secrets manager using encrypted database storage.

    Uses Fernet symmetric encryption to store secrets in PostgreSQL/MySQL/SQLite.
    The encryption key must be provided via ENCRYPTION_KEY environment variable.

    If no encryption key is provided, a warning is logged and a new key is
    generated (not recommended for production as it won't persist across restarts).
    """

    def __init__(self) -> None:
        self._fernet: Fernet | None = None

    @property
    def fernet(self) -> Fernet:
        """Get or create the Fernet encryption instance."""
        if self._fernet is None:
            key = current_app.config.get("ENCRYPTION_KEY", "")

            if not key:
                # Generate a key for development - NOT for production!
                log.warning(
                    "No ENCRYPTION_KEY set. Generating temporary key. "
                    "This is NOT suitable for production!"
                )
                key = Fernet.generate_key().decode()
                # Store it back so it's consistent within this process
                current_app.config["ENCRYPTION_KEY"] = key

            # Ensure key is properly formatted
            try:
                # Key might be base64-encoded bytes or a string
                if isinstance(key, str):
                    key_bytes = key.encode()
                else:
                    key_bytes = key

                # Validate it's a valid Fernet key (32 bytes, base64-encoded)
                self._fernet = Fernet(key_bytes)
            except Exception as e:
                # Key is not valid Fernet format, derive one from it
                log.warning(f"Invalid Fernet key format, deriving key: {e}")
                derived_key = self._derive_key(key)
                self._fernet = Fernet(derived_key)

        return self._fernet

    def _derive_key(self, password: str) -> bytes:
        """Derive a valid Fernet key from a password string."""
        import hashlib

        # Use SHA256 to get 32 bytes, then base64 encode for Fernet
        key_bytes = hashlib.sha256(password.encode()).digest()
        return base64.urlsafe_b64encode(key_bytes)

    def _encrypt(self, data: dict[str, Any]) -> str:
        """Encrypt data to a string."""
        json_data = json.dumps(data)
        encrypted = self.fernet.encrypt(json_data.encode())
        return encrypted.decode()

    def _decrypt(self, encrypted_data: str) -> dict[str, Any]:
        """Decrypt data from a string."""
        try:
            decrypted = self.fernet.decrypt(encrypted_data.encode())
            return json.loads(decrypted.decode())
        except InvalidToken:
            raise SecretsManagerError("Failed to decrypt secret - invalid key or data")
        except json.JSONDecodeError:
            raise SecretsManagerError("Failed to parse decrypted secret data")

    def get_secret(self, path: str) -> dict[str, Any]:
        """Retrieve a secret from the database."""
        db = get_db()
        row = db(db.encrypted_secrets.path == path).select().first()

        if not row:
            raise SecretNotFoundError(path)

        return self._decrypt(row.encrypted_data)

    def set_secret(self, path: str, data: dict[str, Any]) -> bool:
        """Store or update a secret in the database."""
        db = get_db()
        encrypted_data = self._encrypt(data)

        # Check if secret exists
        existing = db(db.encrypted_secrets.path == path).select().first()

        if existing:
            db(db.encrypted_secrets.path == path).update(
                encrypted_data=encrypted_data
            )
        else:
            db.encrypted_secrets.insert(
                path=path,
                encrypted_data=encrypted_data
            )

        db.commit()
        log.info(f"Secret stored at path: {path}")
        return True

    def delete_secret(self, path: str) -> bool:
        """Delete a secret from the database."""
        db = get_db()
        deleted = db(db.encrypted_secrets.path == path).delete()
        db.commit()

        if deleted:
            log.info(f"Secret deleted at path: {path}")
            return True

        return False

    def list_secrets(self, path: str = "") -> list[str]:
        """List all secrets, optionally filtered by path prefix."""
        db = get_db()

        if path:
            # Filter by path prefix
            rows = db(db.encrypted_secrets.path.startswith(path)).select(
                db.encrypted_secrets.path,
                orderby=db.encrypted_secrets.path
            )
        else:
            rows = db(db.encrypted_secrets).select(
                db.encrypted_secrets.path,
                orderby=db.encrypted_secrets.path
            )

        return [row.path for row in rows]

    @staticmethod
    def generate_key() -> str:
        """Generate a new Fernet encryption key.

        Returns:
            Base64-encoded encryption key suitable for ENCRYPTION_KEY config.
        """
        return Fernet.generate_key().decode()
