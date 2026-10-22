"""Tests for credential chain validation."""

from __future__ import annotations

import pytest
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtensionOID
from datetime import datetime, timedelta

from app.security.credentials import (
    validate_service_svid,
    InvalidCredentialError,
    CredentialType,
)


def create_self_signed_cert(common_name: str, spiffe_id: str) -> tuple[str, str]:
    """Create a self-signed certificate with SPIFFE URI SAN."""
    # Generate key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    # Build certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])

    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.utcnow()
    ).not_valid_after(
        datetime.utcnow() + timedelta(days=1)
    ).add_extension(
        x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(spiffe_id),
        ]),
        critical=False,
    ).add_extension(
        # Real SPIRE trust-anchor certs are CAs; the hardened validator requires it.
        x509.BasicConstraints(ca=True, path_length=None),
        critical=True,
    ).sign(private_key, hashes.SHA256(), default_backend())

    # Serialize to PEM
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    return cert_pem, key_pem


def test_validate_service_svid_success():
    """Test successful SPIFFE SVID validation."""
    spiffe_id = "spiffe://penguintech.io/alpha/api-manager"
    # Create a self-signed cert (issuer = subject, so chain validation passes)
    cert_pem, key_pem = create_self_signed_cert("api-manager", spiffe_id)

    # For a self-signed cert, the cert itself is the trust bundle
    principal = validate_service_svid(
        cert_pem,
        cert_pem,  # Use same cert as trust bundle (self-signed)
        frozenset([spiffe_id]),
    )

    assert principal.cred_type == CredentialType.SERVICE_SVID
    assert principal.sub == spiffe_id
    assert principal.spiffe_id == spiffe_id


def test_validate_service_svid_no_san():
    """Test rejection when no SPIFFE URI in SAN."""
    # Create cert without SPIFFE SAN
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "test"),
    ])

    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.utcnow()
    ).not_valid_after(
        datetime.utcnow() + timedelta(days=1)
    ).sign(private_key, hashes.SHA256(), default_backend())

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()

    with pytest.raises(InvalidCredentialError, match="No SPIFFE URI found"):
        validate_service_svid(cert_pem, cert_pem, frozenset())


def test_validate_service_svid_not_in_allowlist():
    """Test rejection when SPIFFE ID not in allowed list."""
    spiffe_id = "spiffe://penguintech.io/alpha/unauthorized"
    cert_pem, _ = create_self_signed_cert("unauthorized", spiffe_id)

    with pytest.raises(InvalidCredentialError, match="not in allowed list"):
        validate_service_svid(
            cert_pem,
            cert_pem,
            frozenset(["spiffe://penguintech.io/alpha/api-manager"]),
        )


def test_validate_service_svid_invalid_pem():
    """Test rejection of invalid certificate PEM."""
    with pytest.raises(InvalidCredentialError, match="Cannot parse peer certificate"):
        validate_service_svid("invalid-pem", "invalid-pem", frozenset())


def test_validate_service_svid_chain_failure():
    """Test rejection when certificate issuer does not match CA."""
    spiffe_id = "spiffe://penguintech.io/alpha/api-manager"
    cert_pem, _ = create_self_signed_cert("api-manager", spiffe_id)

    # Create different self-signed cert as "trust bundle" (issuer mismatch)
    other_pem, _ = create_self_signed_cert("other-ca", "spiffe://other")

    with pytest.raises(InvalidCredentialError, match="issuer does not match|Certificate chain"):
        validate_service_svid(
            cert_pem,
            other_pem,
            frozenset([spiffe_id]),
        )
