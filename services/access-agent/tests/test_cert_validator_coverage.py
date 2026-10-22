"""Behaviour tests for CertificateValidator: time, principal, and CA-signature paths.

Certificates are minted for real with ``tests.ssh_certs`` so ssh-keygen parsing
and the cryptographic signature check run against genuine OpenSSH artefacts.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Literal

import pytest
from paramiko.message import Message

from agent.cert_validator import (
    CertificateInfo,
    CertificateValidationError,
    CertificateValidator,
)
from tests.ssh_certs import (
    SSHKeyPair,
    cert_blob_b64,
    generate_keypair,
    sign_user_certificate,
)

KeyKind = Literal["ed25519", "rsa", "ecdsa"]


@pytest.fixture
def ca(tmp_path: Path) -> SSHKeyPair:
    """Trusted CA key pair (Ed25519)."""
    return generate_keypair(tmp_path, "trusted_ca")


@pytest.fixture
def user(tmp_path: Path) -> SSHKeyPair:
    """User key pair that is certified in the happy-path tests."""
    return generate_keypair(tmp_path, "alice")


@pytest.fixture
def validator(ca: SSHKeyPair) -> CertificateValidator:
    """Validator trusting the fixture CA; cleaned up after the test."""
    v = CertificateValidator(ca.public_line)
    yield v
    v.cleanup()


def test_valid_certificate_is_accepted(
    validator: CertificateValidator, ca: SSHKeyPair, user: SSHKeyPair, tmp_path: Path
) -> None:
    """A certificate signed by the trusted CA, in-window, with a matching principal validates."""
    cert = sign_user_certificate(ca, user, ["alice"], key_id="key-42")
    info = validator.validate_certificate(cert, expected_principals=["alice"])
    assert info.key_id == "key-42"
    assert info.principals == ["alice"]
    assert info.cert_type == "user"
    assert info.is_valid is True
    assert 0 < info.remaining_seconds <= 3600


@pytest.mark.parametrize("kind", ["rsa", "ecdsa"])
def test_non_ed25519_ca_types_are_verified(tmp_path: Path, kind: KeyKind) -> None:
    """RSA and ECDSA CA keys are reconstructed and their signatures verified."""
    ca_kp = generate_keypair(tmp_path, f"ca_{kind}", kind)
    user_kp = generate_keypair(tmp_path, f"user_{kind}")
    cert = sign_user_certificate(ca_kp, user_kp, ["alice"])
    v = CertificateValidator(ca_kp.public_line)
    try:
        assert v.validate_certificate(cert).principals == ["alice"]
    finally:
        v.cleanup()


def test_expired_certificate_is_rejected(
    validator: CertificateValidator, ca: SSHKeyPair, user: SSHKeyPair
) -> None:
    """A certificate whose validity window ended in the past is rejected as expired."""
    cert = sign_user_certificate(ca, user, ["alice"], validity="-2h:-1h")
    with pytest.raises(CertificateValidationError, match="expired"):
        validator.validate_certificate(cert)


def test_not_yet_valid_certificate_is_rejected(
    validator: CertificateValidator, ca: SSHKeyPair, user: SSHKeyPair
) -> None:
    """A certificate whose validity starts in the future is rejected as not yet valid."""
    cert = sign_user_certificate(ca, user, ["alice"], validity="+1h:+2h")
    with pytest.raises(CertificateValidationError, match="not yet valid"):
        validator.validate_certificate(cert)


def test_principal_mismatch_is_rejected(
    validator: CertificateValidator, ca: SSHKeyPair, user: SSHKeyPair
) -> None:
    """Expected principals that do not intersect the certificate's principals are rejected."""
    cert = sign_user_certificate(ca, user, ["alice"])
    with pytest.raises(CertificateValidationError, match="do not match"):
        validator.validate_certificate(cert, expected_principals=["bob"])


def test_hostname_style_principal_mismatch_is_rejected(
    validator: CertificateValidator, ca: SSHKeyPair, user: SSHKeyPair
) -> None:
    """A certificate issued for one host principal cannot be used against another."""
    cert = sign_user_certificate(ca, user, ["web-01.internal"])
    with pytest.raises(CertificateValidationError):
        validator.validate_certificate(cert, expected_principals=["db-02.internal"])


def test_expected_principal_overlap_is_accepted(
    validator: CertificateValidator, ca: SSHKeyPair, user: SSHKeyPair
) -> None:
    """Any single overlapping principal is sufficient when several are expected."""
    cert = sign_user_certificate(ca, user, ["alice", "ops"])
    info = validator.validate_certificate(cert, expected_principals=["nobody", "ops"])
    assert "ops" in info.principals


def test_certificate_from_wrong_ca_is_rejected(
    tmp_path: Path, validator: CertificateValidator, user: SSHKeyPair
) -> None:
    """A valid certificate signed by a different CA is rejected by the signature check."""
    rogue_ca = generate_keypair(tmp_path, "rogue_ca")
    cert = sign_user_certificate(rogue_ca, user, ["alice"])
    with pytest.raises(CertificateValidationError, match="not signed by the configured trusted CA"):
        validator.validate_certificate(cert)


def test_spliced_trusted_ca_key_fails_signature_check(
    tmp_path: Path, validator: CertificateValidator, ca: SSHKeyPair, user: SSHKeyPair
) -> None:
    """Embedding the trusted CA key in a cert signed by another CA is detected.

    This is the forged-signing-key attack the signature check exists to stop.
    """
    rogue_ca = generate_keypair(tmp_path, "rogue_ca_splice")
    cert = sign_user_certificate(rogue_ca, user, ["alice"])
    rogue_blob = base64.b64decode(rogue_ca.public_line.split()[1])
    trusted_blob = base64.b64decode(ca.public_line.split()[1])
    assert len(rogue_blob) == len(trusted_blob)
    cert_blob = base64.b64decode(cert_blob_b64(cert))
    offset = cert_blob.find(rogue_blob)
    assert offset != -1
    forged = cert_blob[:offset] + trusted_blob + cert_blob[offset + len(trusted_blob):]
    forged_line = f"{cert.split()[0]} {base64.b64encode(forged).decode()}"
    # ssh-keygen -L rejects the forged signature first; either layer failing closed is acceptable.
    with pytest.raises(CertificateValidationError):
        validator.validate_certificate(forged_line)


def test_tampered_principals_fail_signature_check(
    validator: CertificateValidator, ca: SSHKeyPair, user: SSHKeyPair
) -> None:
    """Changing signed content after issuance invalidates the CA signature."""
    cert = sign_user_certificate(ca, user, ["alice"])
    blob = bytearray(base64.b64decode(cert_blob_b64(cert)))
    idx = blob.find(b"alice")
    assert idx != -1
    blob[idx : idx + 5] = b"mallo"
    tampered = f"{cert.split()[0]} {base64.b64encode(bytes(blob)).decode()}"
    with pytest.raises(CertificateValidationError):
        validator.validate_certificate(tampered)


def test_unparseable_certificate_is_rejected(validator: CertificateValidator) -> None:
    """Garbage input fails at ssh-keygen parsing with a validation error."""
    with pytest.raises(CertificateValidationError, match="Failed to parse"):
        validator.validate_certificate("ssh-ed25519-cert-v01@openssh.com AAAA-not-a-cert")


def test_parse_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch, validator: CertificateValidator
) -> None:
    """A hung ssh-keygen is surfaced as a validation error, not an unhandled timeout."""

    def _hang(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="ssh-keygen", timeout=10)

    monkeypatch.setattr(subprocess, "run", _hang)
    with pytest.raises(CertificateValidationError, match="timed out"):
        validator.validate_certificate("ssh-ed25519-cert-v01@openssh.com AAAA")


def test_signature_check_rejects_malformed_blob_directly(validator: CertificateValidator) -> None:
    """A certificate line with no key blob fails closed before any crypto runs."""
    with pytest.raises(CertificateValidationError, match="missing key blob"):
        validator._verify_signature("ssh-ed25519-cert-v01@openssh.com")


def test_signature_check_rejects_unsupported_certificate_type(
    validator: CertificateValidator,
) -> None:
    """Non-certificate or unknown certificate types are refused."""
    msg = Message()
    msg.add_string("ssh-dss-cert-v01@openssh.com")
    blob = base64.b64encode(msg.asbytes()).decode()
    with pytest.raises(CertificateValidationError, match="Unsupported certificate type"):
        validator._verify_signature(f"ssh-dss-cert-v01@openssh.com {blob}")


def test_signature_check_wraps_unexpected_parse_errors(validator: CertificateValidator) -> None:
    """Any non-validation exception during signature parsing is wrapped and fails closed."""
    with pytest.raises(CertificateValidationError, match="verification failed"):
        validator._verify_signature("ssh-ed25519-cert-v01@openssh.com !!!notbase64!!!")


def test_load_pubkey_rejects_unsupported_ca_type() -> None:
    """A CA key of an unknown algorithm cannot be reconstructed."""
    msg = Message()
    msg.add_string("ssh-dss")
    with pytest.raises(CertificateValidationError, match="Unsupported CA key type"):
        CertificateValidator._load_pubkey_from_blob(msg.asbytes())


def test_ca_key_file_is_written_once_and_reused(validator: CertificateValidator, ca: SSHKeyPair) -> None:
    """The CA public key is materialised to a temp file once and reused on later calls."""
    first = validator._ensure_ca_key_file()
    assert first.read_text() == ca.public_line
    assert validator._ensure_ca_key_file() == first


def test_ca_key_file_is_recreated_after_removal(validator: CertificateValidator) -> None:
    """If the temp CA file disappears, a fresh one is written on next use."""
    first = validator._ensure_ca_key_file()
    first.unlink()
    second = validator._ensure_ca_key_file()
    assert second.exists()


def test_cleanup_removes_temp_ca_file(validator: CertificateValidator) -> None:
    """cleanup deletes the temp CA file and forgets its path."""
    path = validator._ensure_ca_key_file()
    validator.cleanup()
    assert not path.exists()
    assert validator._ca_key_file is None
    validator.cleanup()  # idempotent


def test_keygen_output_parses_host_certificate_and_bad_serial() -> None:
    """Host-type certs are recognised; a non-numeric serial falls back to zero."""
    output = (
        "  Type: ssh-ed25519-cert-v01@openssh.com host certificate\n"
        '        Public key: ED25519-CERT SHA256:abc\n'
        "        Signing CA: ED25519 SHA256:def\n"
        '        Key ID: "web-01"\n'
        "        Serial: notanumber\n"
        "        Valid: from 2026-01-01T00:00:00 to 2030-01-01T00:00:00\n"
        "        Principals:\n"
        "                web-01.internal\n"
        "        Critical Options: (none)\n"
    )
    info = CertificateValidator("ssh-ed25519 AAAA")._parse_keygen_output(output)
    assert info.cert_type == "host"
    assert info.key_id == "web-01"
    assert info.serial == 0
    assert info.principals == ["web-01.internal"]
    assert info.valid_after > 0 and info.valid_before > info.valid_after


def test_keygen_output_without_validity_or_quotes_keeps_defaults() -> None:
    """Missing validity lines and unquoted key ids leave the defaults in place."""
    info = CertificateValidator("ssh-ed25519 AAAA")._parse_keygen_output(
        "Key ID: unquoted\nValid: forever\n"
    )
    assert info.key_id == ""
    assert info.valid_after == 0 and info.valid_before == 0
    assert info.cert_type == "user"


def test_certificate_info_window_helpers() -> None:
    """is_valid and remaining_seconds reflect the window relative to now."""
    future = CertificateInfo("k", 1, [], 0, 10**10, "user")
    past = CertificateInfo("k", 1, [], 0, 1, "user")
    assert future.is_valid is True and future.remaining_seconds > 0
    assert past.is_valid is False and past.remaining_seconds == 0
