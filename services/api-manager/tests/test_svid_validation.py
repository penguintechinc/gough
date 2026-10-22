"""Regression tests for hardened Service SVID (mTLS X.509) validation.

Covers the checks added in the trust-chain remediation: certificate validity
window, CA BasicConstraints, and multi-key-type signature verification.
"""

from __future__ import annotations

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.security.credentials import validate_service_svid, InvalidCredentialError

SPIFFE_ID = "spiffe://penguintech.io/prod/discovery-agent"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _make_ca(key, *, is_ca=True):
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gough-test-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )


def _make_svid(ca_key, ca_cert, leaf_key, *, valid=True):
    now = datetime.datetime.now(datetime.timezone.utc)
    if valid:
        nb, na = now - datetime.timedelta(hours=1), now + datetime.timedelta(hours=1)
    else:  # expired
        nb, na = now - datetime.timedelta(days=2), now - datetime.timedelta(days=1)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "svid")]))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(nb)
        .not_valid_after(na)
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(SPIFFE_ID)]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )


def test_valid_svid_accepted():
    ca_key = _key()
    ca = _make_ca(ca_key)
    leaf = _make_svid(ca_key, ca, _key(), valid=True)
    principal = validate_service_svid(_pem(leaf), _pem(ca), frozenset({SPIFFE_ID}))
    assert principal.sub == SPIFFE_ID


def test_expired_svid_rejected():
    ca_key = _key()
    ca = _make_ca(ca_key)
    leaf = _make_svid(ca_key, ca, _key(), valid=False)
    with pytest.raises(InvalidCredentialError, match="expired or not yet valid"):
        validate_service_svid(_pem(leaf), _pem(ca), frozenset({SPIFFE_ID}))


def test_non_ca_trust_anchor_rejected():
    ca_key = _key()
    not_ca = _make_ca(ca_key, is_ca=False)  # BasicConstraints ca=False
    leaf = _make_svid(ca_key, not_ca, _key(), valid=True)
    with pytest.raises(InvalidCredentialError, match="not a CA"):
        validate_service_svid(_pem(leaf), _pem(not_ca), frozenset({SPIFFE_ID}))


def test_svid_signed_by_other_ca_rejected():
    ca_key, ca = _key(), None
    ca = _make_ca(ca_key)
    other_key = _key()
    # Leaf claims our CA's issuer name but is signed by a different key.
    leaf = _make_svid(other_key, ca, _key(), valid=True)
    with pytest.raises(InvalidCredentialError):
        validate_service_svid(_pem(leaf), _pem(ca), frozenset({SPIFFE_ID}))
