"""Joiner-secret envelope encryption — four-layer protection.

Per Gough spec Biome Model → Joiner Secrets. Layer 1 = Vault transit KEK;
Layer 2 = per-row DEK + AES-256-GCM envelope; Layer 3 = Postgres TDE
(infra-managed); Layer 4 = RLS (migration-managed). This module owns
Layers 1+2 — DEK lifecycle and envelope encrypt/decrypt.
"""

import ctypes
import secrets
import uuid
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel

if TYPE_CHECKING:
    from app.clients.vault import VaultClient


DEK_BYTES = 32  # AES-256
IV_BYTES = 12  # GCM standard
TAG_BYTES = 16  # GCM auth tag


class AuthTagMismatchError(Exception):
    """Raised when envelope authentication tag verification fails (tampering detected)."""

    def __init__(
        self, message: str = "Envelope authentication tag mismatch", joiner_secret_id: uuid.UUID | None = None
    ) -> None:
        """Initialize exception with optional joiner_secret_id."""
        super().__init__(message)
        self.joiner_secret_id = joiner_secret_id


class EnvelopeCiphertext(BaseModel):
    """Encrypted envelope with DEK-wrapped layer."""

    ciphertext: bytes
    iv: bytes
    auth_tag: bytes
    dek_wrapped: bytes
    vault_kek_name: str

    class Config:
        """Pydantic config."""

        arbitrary_types_allowed = True


def generate_dek() -> bytes:
    """Generate a random Data Encryption Key (32 bytes for AES-256).

    Returns:
        32-byte random key.
    """
    return secrets.token_bytes(DEK_BYTES)


def generate_iv() -> bytes:
    """Generate a random initialization vector (12 bytes for GCM).

    Returns:
        12-byte random IV.
    """
    return secrets.token_bytes(IV_BYTES)


def zero_bytes(buf: bytes | bytearray) -> None:
    """Zero out sensitive bytes in memory.

    Best-effort zeroization; handles both bytes and bytearray.
    Bytes objects are immutable so we zero a mutable copy.

    Args:
        buf: Bytes or bytearray to zero.
    """
    if isinstance(buf, bytes):
        ba = bytearray(buf)
    else:
        ba = buf
    ptr = (ctypes.c_char * len(ba)).from_buffer(ba)
    ctypes.memset(ptr, 0, len(ba))


def encrypt_envelope(
    plaintext: bytes, vault_client: "VaultClient", vault_kek_name: str = "gough-joiner-dek-wrap"
) -> EnvelopeCiphertext:
    """Encrypt plaintext with AES-256-GCM envelope and Vault-wrapped DEK.

    Layers:
    1. Generate DEK (32 bytes) and IV (12 bytes)
    2. Encrypt plaintext with AES-256-GCM using DEK
    3. Wrap DEK via Vault transit engine (KEK layer)
    4. Return envelope with ciphertext, IV, auth tag, wrapped DEK

    Args:
        plaintext: Data to encrypt.
        vault_client: VaultClient instance for transit encryption.
        vault_kek_name: Name of Vault transit key for DEK wrapping.

    Returns:
        EnvelopeCiphertext with encrypted data and wrapped key.

    Raises:
        ValueError: If plaintext is empty.
    """
    if not plaintext:
        raise ValueError("Plaintext cannot be empty")

    # Generate ephemeral DEK and IV
    dek = generate_dek()
    iv = generate_iv()

    # Encrypt plaintext with AES-256-GCM
    cipher = AESGCM(dek)
    ciphertext_and_tag = cipher.encrypt(iv, plaintext, None)

    # Split ciphertext and auth tag (AESGCM appends tag to ciphertext)
    ciphertext = ciphertext_and_tag[:-TAG_BYTES]
    auth_tag = ciphertext_and_tag[-TAG_BYTES:]

    # Wrap DEK via Vault transit engine
    dek_wrapped_response = vault_client.transit_encrypt(vault_kek_name, dek)
    dek_wrapped = dek_wrapped_response.ciphertext.encode() if isinstance(dek_wrapped_response.ciphertext, str) else dek_wrapped_response.ciphertext

    # Zero DEK from memory
    zero_bytes(dek)

    return EnvelopeCiphertext(
        ciphertext=ciphertext,
        iv=iv,
        auth_tag=auth_tag,
        dek_wrapped=dek_wrapped,
        vault_kek_name=vault_kek_name,
    )


def decrypt_envelope(
    envelope: EnvelopeCiphertext, vault_client: "VaultClient", joiner_secret_id: uuid.UUID | None = None
) -> bytes:
    """Decrypt envelope ciphertext using Vault-unwrapped DEK.

    Layers:
    1. Unwrap DEK via Vault transit engine
    2. Decrypt ciphertext with AES-256-GCM using DEK
    3. Verify auth tag (raises AuthTagMismatchError on tampering)
    4. Zero DEK from memory
    5. Return plaintext

    Args:
        envelope: EnvelopeCiphertext to decrypt.
        vault_client: VaultClient instance for transit decryption.
        joiner_secret_id: Optional secret ID for error context.

    Returns:
        Decrypted plaintext bytes.

    Raises:
        AuthTagMismatchError: If auth tag verification fails (tampering detected).
        Exceptions from vault_client are propagated unchanged.
    """
    # Unwrap DEK via Vault transit engine
    dek_wrapped_str = envelope.dek_wrapped.decode() if isinstance(envelope.dek_wrapped, bytes) else envelope.dek_wrapped
    dek_unwrapped_response = vault_client.transit_decrypt(envelope.vault_kek_name, dek_wrapped_str)
    dek = dek_unwrapped_response.plaintext

    try:
        # Decrypt ciphertext with AES-256-GCM
        cipher = AESGCM(dek)
        # Reconstruct ciphertext + tag for AESGCM
        ciphertext_with_tag = envelope.ciphertext + envelope.auth_tag
        plaintext = cipher.decrypt(envelope.iv, ciphertext_with_tag, None)
    except InvalidTag as e:
        raise AuthTagMismatchError(joiner_secret_id=joiner_secret_id) from e
    finally:
        # Zero DEK from memory
        zero_bytes(dek)

    return plaintext
