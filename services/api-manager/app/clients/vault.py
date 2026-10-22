"""Vault client for transit encryption/decryption and KV v2 operations."""

import base64
import logging
import os
from typing import Any
from urllib.parse import urlparse

import hvac
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class VaultError(Exception):
    """Base Vault error."""

    pass


class VaultSealedError(VaultError):
    """Vault is sealed or unreachable."""

    pass


class VaultPermissionDenied(VaultError):
    """Insufficient permissions for operation."""

    pass


class VaultTransitKeyMissing(VaultError):
    """Transit key does not exist."""

    pass


class VaultTransitEncryptRequest(BaseModel):
    """Request model for Vault transit encryption."""

    plaintext: bytes = Field(..., description="Plaintext data to encrypt")


class VaultTransitEncryptResponse(BaseModel):
    """Response model for Vault transit encryption."""

    ciphertext: str = Field(..., description="Encrypted ciphertext")
    key_version: int = Field(..., description="Key version used for encryption")


class VaultTransitDecryptRequest(BaseModel):
    """Request model for Vault transit decryption."""

    ciphertext: str = Field(..., description="Encrypted ciphertext to decrypt")


class VaultTransitDecryptResponse(BaseModel):
    """Response model for Vault transit decryption."""

    plaintext: bytes = Field(..., description="Decrypted plaintext data")


class VaultKvWriteRequest(BaseModel):
    """Request model for Vault KV v2 write."""

    data: dict[str, Any] = Field(..., description="Key-value pairs to store")


class VaultKvReadResponse(BaseModel):
    """Response model for Vault KV v2 read."""

    data: dict[str, Any] = Field(..., description="Retrieved key-value pairs")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="KV metadata (version, created_time, etc.)"
    )


class VaultClient:
    """Client for HashiCorp Vault transit encryption and KV v2 storage."""

    def __init__(
        self, addr: str | None = None, token: str | None = None, namespace: str | None = None
    ) -> None:
        """Initialize Vault client.

        Args:
            addr: Vault server address. If None, reads VAULT_ADDR env var.
            token: Vault auth token. If None, reads VAULT_TOKEN env var.
            namespace: Vault namespace. If None, reads VAULT_NAMESPACE env var.

        Raises:
            ValueError: If token is empty or address is invalid.
        """
        self.addr = addr or os.getenv("VAULT_ADDR", "http://localhost:8200")
        self.token = token or os.getenv("VAULT_TOKEN", "")
        self.namespace = namespace or os.getenv("VAULT_NAMESPACE")

        if not self.token:
            raise ValueError("VAULT_TOKEN must be provided or set as env var")

        # Validate URL
        parsed = urlparse(self.addr)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid VAULT_ADDR: must start with http:// or https://")

        # Check for TLS skip
        skip_verify = os.getenv("VAULT_SKIP_VERIFY") == "1"
        if skip_verify:
            logger.warning("VAULT_SKIP_VERIFY is enabled; TLS verification disabled")

        self._verify = not skip_verify
        self._client: hvac.Client | None = None

    @property
    def _hvac_client(self) -> hvac.Client:
        """Lazy-initialize hvac client."""
        if self._client is None:
            self._client = hvac.Client(url=self.addr, token=self.token, verify=self._verify)
            if self.namespace:
                self._client.adapter.namespace = self.namespace
        return self._client

    def health(self) -> dict[str, Any]:
        """Get Vault health status.

        Returns:
            Dict with sealed, cluster_id, version, performance_standby.
        """
        try:
            resp = self._hvac_client.sys.read_health_status()
            logger.info("Health check successful")
            return {
                "sealed": resp.get("sealed", True),
                "cluster_id": resp.get("cluster_id", ""),
                "version": resp.get("version", ""),
                "performance_standby": resp.get("performance_standby", False),
            }
        except hvac.exceptions.VaultDown as e:
            logger.error("Vault is sealed or unreachable")
            raise VaultSealedError(str(e)) from e

    def transit_encrypt(
        self, key_name: str, plaintext: bytes
    ) -> VaultTransitEncryptResponse:
        """Encrypt data using Vault transit engine.

        Args:
            key_name: Name of the transit key.
            plaintext: Data to encrypt.

        Returns:
            VaultTransitEncryptResponse with ciphertext and key version.

        Raises:
            VaultTransitKeyMissing: Key does not exist.
            VaultPermissionDenied: Insufficient permissions.
            VaultSealedError: Vault is sealed.
        """
        try:
            b64_plaintext = base64.b64encode(plaintext).decode()
            resp = self._hvac_client.secrets.transit.encrypt_data(
                name=key_name, plaintext=b64_plaintext
            )
            logger.info(f"Transit encrypt successful for key={key_name}")
            return VaultTransitEncryptResponse(
                ciphertext=resp["data"]["ciphertext"],
                key_version=resp["data"]["key_version"],
            )
        except hvac.exceptions.InvalidPath as e:
            logger.error(f"Transit key not found: {key_name}")
            raise VaultTransitKeyMissing(str(e)) from e
        except hvac.exceptions.Forbidden as e:
            logger.error(f"Permission denied for transit key: {key_name}")
            raise VaultPermissionDenied(str(e)) from e
        except hvac.exceptions.VaultDown as e:
            logger.error("Vault is sealed or unreachable")
            raise VaultSealedError(str(e)) from e

    def transit_decrypt(self, key_name: str, ciphertext: str) -> VaultTransitDecryptResponse:
        """Decrypt data using Vault transit engine.

        Args:
            key_name: Name of the transit key.
            ciphertext: Encrypted ciphertext.

        Returns:
            VaultTransitDecryptResponse with plaintext bytes.

        Raises:
            VaultTransitKeyMissing: Key does not exist.
            VaultPermissionDenied: Insufficient permissions.
            VaultSealedError: Vault is sealed.
        """
        try:
            resp = self._hvac_client.secrets.transit.decrypt_data(
                name=key_name, ciphertext=ciphertext
            )
            plaintext_b64 = resp["data"]["plaintext"]
            plaintext = base64.b64decode(plaintext_b64)
            logger.info(f"Transit decrypt successful for key={key_name}")
            return VaultTransitDecryptResponse(plaintext=plaintext)
        except hvac.exceptions.InvalidPath as e:
            logger.error(f"Transit key not found: {key_name}")
            raise VaultTransitKeyMissing(str(e)) from e
        except hvac.exceptions.Forbidden as e:
            logger.error(f"Permission denied for transit key: {key_name}")
            raise VaultPermissionDenied(str(e)) from e
        except hvac.exceptions.VaultDown as e:
            logger.error("Vault is sealed or unreachable")
            raise VaultSealedError(str(e)) from e

    def transit_sign(self, key_name: str, message: bytes, hash_algorithm: str = "sha2-256") -> str:
        """Sign data using Vault transit engine.

        Args:
            key_name: Name of the transit key.
            message: Data to sign.
            hash_algorithm: Hash algorithm (default: sha2-256).

        Returns:
            Opaque signature string (vault:v1:...).
        """
        b64_message = base64.b64encode(message).decode()
        resp = self._hvac_client.secrets.transit.sign_data(
            name=key_name, hash_input=b64_message, hash_algorithm=hash_algorithm
        )
        logger.info(f"Transit sign successful for key={key_name}")
        return resp["data"]["signature"]

    def transit_verify_signature(
        self, key_name: str, message: bytes, signature: str, hash_algorithm: str = "sha2-256"
    ) -> bool:
        """Verify a signature using Vault transit engine.

        Args:
            key_name: Name of the transit key.
            message: Original data.
            signature: Signature to verify.
            hash_algorithm: Hash algorithm (default: sha2-256).

        Returns:
            True if signature is valid, False otherwise.
        """
        b64_message = base64.b64encode(message).decode()
        resp = self._hvac_client.secrets.transit.verify_signed_data(
            name=key_name, hash_input=b64_message, signature=signature, hash_algorithm=hash_algorithm
        )
        result = resp["data"]["valid"]
        logger.info(f"Transit verify result={result} for key={key_name}")
        return result

    def kv_write(self, path: str, data: dict[str, Any]) -> None:
        """Write key-value data to Vault KV v2.

        Args:
            path: KV v2 path.
            data: Key-value pairs to store.

        Raises:
            ValueError: If data contains null characters.
            VaultPermissionDenied: Insufficient permissions.
        """
        for val in data.values():
            if isinstance(val, str) and "\x00" in val:
                raise ValueError("Data contains null characters")

        self._hvac_client.secrets.kv.v2.create_or_update_secret(path=path, secret=data)
        logger.info(f"KV write successful for path={path}")

    def kv_read(self, path: str) -> VaultKvReadResponse:
        """Read key-value data from Vault KV v2.

        Args:
            path: KV v2 path.

        Returns:
            VaultKvReadResponse with data and metadata.
        """
        try:
            resp = self._hvac_client.secrets.kv.v2.read_secret_version(path=path)
            logger.info(f"KV read successful for path={path}")
            return VaultKvReadResponse(
                data=resp["data"].get("data", {}),
                metadata=resp["data"].get("metadata", {}),
            )
        except hvac.exceptions.InvalidPath:
            logger.warning(f"KV path not found: {path}")
            return VaultKvReadResponse(data={})
        except hvac.exceptions.Forbidden as e:
            logger.error(f"Permission denied for KV path: {path}")
            raise VaultPermissionDenied(str(e)) from e
