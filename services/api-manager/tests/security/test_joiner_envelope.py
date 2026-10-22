"""Tests for joiner_envelope encryption module."""

import uuid
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.clients.vault import VaultTransitDecryptResponse, VaultTransitEncryptResponse
from app.security.joiner_envelope import (
    AuthTagMismatchError,
    DEK_BYTES,
    IV_BYTES,
    TAG_BYTES,
    EnvelopeCiphertext,
    decrypt_envelope,
    encrypt_envelope,
    generate_dek,
    generate_iv,
    zero_bytes,
)


@pytest.fixture
def mock_vault() -> MagicMock:
    """Mock VaultClient."""
    client = MagicMock()
    client.transit_encrypt.return_value = VaultTransitEncryptResponse(
        ciphertext="vault:v1:wrapped", key_version=1
    )
    return client


def test_generate_dek_length() -> None:
    """DEK must be 32 bytes."""
    assert len(generate_dek()) == DEK_BYTES == 32


def test_generate_dek_uniqueness() -> None:
    """Multiple DEK calls produce unique keys."""
    assert len({generate_dek() for _ in range(100)}) == 100


def test_generate_iv_length() -> None:
    """IV must be 12 bytes."""
    assert len(generate_iv()) == IV_BYTES == 12


def test_generate_iv_uniqueness() -> None:
    """Multiple IV calls produce unique IVs."""
    assert len({generate_iv() for _ in range(100)}) == 100


def test_zero_bytes_does_not_raise_on_immutable_bytes() -> None:
    """zero_bytes handles bytes without raising."""
    zero_bytes(b"sensitive")


def test_zero_bytes_handles_bytearray() -> None:
    """zero_bytes handles bytearray without raising."""
    zero_bytes(bytearray(b"sensitive"))


def test_envelope_ciphertext_pydantic_model_round_trip() -> None:
    """Model dump/load preserves all fields."""
    original = EnvelopeCiphertext(
        ciphertext=b"ct", iv=b"0" * 12, auth_tag=b"t" * 16,
        dek_wrapped=b"wd", vault_kek_name="kek"
    )
    restored = EnvelopeCiphertext.model_validate(original.model_dump())
    assert restored.ciphertext == original.ciphertext
    assert restored.vault_kek_name == original.vault_kek_name


def test_envelope_ciphertext_differs_from_plaintext(mock_vault: MagicMock) -> None:
    """Ciphertext != plaintext."""
    plaintext = b"secret"
    envelope = encrypt_envelope(plaintext, mock_vault)
    assert envelope.ciphertext != plaintext


def test_envelope_iv_differs_per_call(mock_vault: MagicMock) -> None:
    """Different IVs per encryption."""
    plaintext = b"data"
    iv1 = encrypt_envelope(plaintext, mock_vault).iv
    iv2 = encrypt_envelope(plaintext, mock_vault).iv
    assert iv1 != iv2


def test_envelope_round_trip(mock_vault: MagicMock) -> None:
    """Encrypt then decrypt yields plaintext."""
    plaintext = b"secret"
    dek = generate_dek()
    iv = generate_iv()

    cipher = AESGCM(dek)
    ct_tag = cipher.encrypt(iv, plaintext, None)
    ct, tag = ct_tag[:-TAG_BYTES], ct_tag[-TAG_BYTES:]

    envelope = EnvelopeCiphertext(
        ciphertext=ct, iv=iv, auth_tag=tag,
        dek_wrapped=b"w", vault_kek_name="k"
    )
    mock_vault.transit_decrypt.return_value = VaultTransitDecryptResponse(plaintext=dek)
    assert decrypt_envelope(envelope, mock_vault) == plaintext


def test_decrypt_envelope_authtag_mismatch(mock_vault: MagicMock) -> None:
    """Bad auth tag raises AuthTagMismatchError with joiner_secret_id."""
    dek, iv = generate_dek(), generate_iv()
    cipher = AESGCM(dek)
    ct_tag = cipher.encrypt(iv, b"data", None)
    bad_tag = bytearray(ct_tag[-TAG_BYTES:])
    bad_tag[0] ^= 0xFF

    envelope = EnvelopeCiphertext(
        ciphertext=ct_tag[:-TAG_BYTES], iv=iv, auth_tag=bytes(bad_tag),
        dek_wrapped=b"w", vault_kek_name="k"
    )
    mock_vault.transit_decrypt.return_value = VaultTransitDecryptResponse(plaintext=dek)

    sid = uuid.uuid4()
    with pytest.raises(AuthTagMismatchError) as exc:
        decrypt_envelope(envelope, mock_vault, joiner_secret_id=sid)
    assert exc.value.joiner_secret_id == sid


def test_decrypt_envelope_ciphertext_tamper(mock_vault: MagicMock) -> None:
    """Bad ciphertext raises AuthTagMismatchError."""
    dek, iv = generate_dek(), generate_iv()
    cipher = AESGCM(dek)
    ct_tag = cipher.encrypt(iv, b"data", None)
    bad_ct = bytearray(ct_tag[:-TAG_BYTES])
    bad_ct[0] ^= 0xFF

    envelope = EnvelopeCiphertext(
        ciphertext=bytes(bad_ct), iv=iv, auth_tag=ct_tag[-TAG_BYTES:],
        dek_wrapped=b"w", vault_kek_name="k"
    )
    mock_vault.transit_decrypt.return_value = VaultTransitDecryptResponse(plaintext=dek)
    with pytest.raises(AuthTagMismatchError):
        decrypt_envelope(envelope, mock_vault)


def test_decrypt_envelope_iv_tamper(mock_vault: MagicMock) -> None:
    """Bad IV raises AuthTagMismatchError."""
    dek, iv = generate_dek(), generate_iv()
    cipher = AESGCM(dek)
    ct_tag = cipher.encrypt(iv, b"data", None)
    bad_iv = bytearray(iv)
    bad_iv[0] ^= 0xFF

    envelope = EnvelopeCiphertext(
        ciphertext=ct_tag[:-TAG_BYTES], iv=bytes(bad_iv), auth_tag=ct_tag[-TAG_BYTES:],
        dek_wrapped=b"w", vault_kek_name="k"
    )
    mock_vault.transit_decrypt.return_value = VaultTransitDecryptResponse(plaintext=dek)
    with pytest.raises(AuthTagMismatchError):
        decrypt_envelope(envelope, mock_vault)


def test_decrypt_envelope_propagates_vault_errors(mock_vault: MagicMock) -> None:
    """Vault errors propagate unchanged."""
    envelope = EnvelopeCiphertext(
        ciphertext=b"c", iv=generate_iv(), auth_tag=b"t" * 16,
        dek_wrapped=b"w", vault_kek_name="k"
    )
    mock_vault.transit_decrypt.side_effect = ConnectionError("Vault down")
    with pytest.raises(ConnectionError, match="Vault down"):
        decrypt_envelope(envelope, mock_vault)
