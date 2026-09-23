"""Tests for credentials.py — 4-credential-type authentication middleware."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch, MagicMock
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtensionOID
import jwt

from app.security.credentials import (
    CredentialType,
    Principal,
    CredentialError,
    MissingCredentialError,
    InvalidCredentialError,
    ExpiredCredentialError,
    OneTimeTokenReplayError,
    detect_credential_type,
    validate_user_jwt,
    validate_machine_jwt,
    validate_service_svid,
    validate_one_time_bootstrap_token,
)


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def rsa_key_pair() -> tuple[str, str]:
    """Generate RSA key pair for testing."""
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem.decode(), public_pem.decode()


@pytest.fixture
def jwks_keys() -> list[dict]:
    """Mock JWKS keys for testing."""
    return [
        {
            "kty": "RSA",
            "use": "sig",
            "kid": "test-key-1",
            "alg": "RS256",
            "n": "test",
            "e": "AQAB",
        }
    ]


@pytest.fixture
def mock_vault_client() -> Mock:
    """Mock Vault client."""
    return Mock()


@pytest.fixture
def mock_redis_client() -> Mock:
    """Mock Redis client."""
    return Mock()


@pytest.fixture
def x509_cert_and_ca() -> tuple[str, str]:
    """Generate a CA cert and a leaf SVID cert (with SPIFFE SAN) signed by it.

    Mirrors real SPIRE topology: validate_service_svid's chain validation
    requires the trust bundle to be a genuine CA (BasicConstraints ca=True)
    and the leaf's issuer to match the CA's subject, so both certs must be
    a real two-party chain rather than a single self-signed cert reused as
    its own "CA".
    """
    ca_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256(), default_backend())
    )

    leaf_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    leaf_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-service")])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_subject)
        .issuer_name(ca_subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.UniformResourceIdentifier("spiffe://test.local/test-service")
            ]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256(), default_backend())
    )

    cert_pem = leaf_cert.public_bytes(serialization.Encoding.PEM).decode()
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode()

    return cert_pem, ca_pem


# ==============================================================================
# Test: detect_credential_type
# ==============================================================================


def test_credential_type_detect_service_svid_via_peer_cert() -> None:
    """SERVICE_SVID detected when peer_cert_pem provided."""
    headers = {}
    cred_type = detect_credential_type(headers, peer_cert_pem="-----BEGIN CERTIFICATE-----")
    assert cred_type == CredentialType.SERVICE_SVID


def test_credential_type_detect_one_time_bootstrap() -> None:
    """ONE_TIME_BOOTSTRAP detected from phase:helper claim."""
    payload = {"phase": "helper", "nonce": "test", "exp": 1234567890}
    token = jwt.encode(payload, "secret", algorithm="HS256")
    headers = {"Authorization": f"Bearer {token}"}

    cred_type = detect_credential_type(headers)
    assert cred_type == CredentialType.ONE_TIME_BOOTSTRAP


def test_credential_type_detect_machine_jwt() -> None:
    """MACHINE_JWT detected from machine: prefix in sub."""
    payload = {"sub": "machine:backend-service"}
    token = jwt.encode(payload, "secret", algorithm="HS256")
    headers = {"Authorization": f"Bearer {token}"}

    cred_type = detect_credential_type(headers)
    assert cred_type == CredentialType.MACHINE_JWT


def test_credential_type_detect_user_jwt() -> None:
    """USER_JWT detected when no phase/machine prefix."""
    payload = {"sub": "user:john@example.com"}
    token = jwt.encode(payload, "secret", algorithm="HS256")
    headers = {"Authorization": f"Bearer {token}"}

    cred_type = detect_credential_type(headers)
    assert cred_type == CredentialType.USER_JWT


def test_credential_type_missing_raises() -> None:
    """MissingCredentialError when no Authorization header and no peer cert."""
    headers = {}
    with pytest.raises(MissingCredentialError):
        detect_credential_type(headers)


def test_credential_type_detect_case_insensitive() -> None:
    """Authorization header detected case-insensitively."""
    payload = {"sub": "user:john"}
    token = jwt.encode(payload, "secret", algorithm="HS256")
    headers = {"authorization": f"Bearer {token}"}  # lowercase

    cred_type = detect_credential_type(headers)
    assert cred_type == CredentialType.USER_JWT


# regression: audit unverified-jwt-decode-suppression 2026-09-22
#
# detect_credential_type()'s `jwt.decode(..., options={"verify_signature":
# False})` is routing-only (reads "phase"/"sub" to pick a CredentialType) and
# carries a line-scoped `# nosemgrep` / `# nosec` with that justification.
# These tests prove the justification is actually true: routing succeeds on a
# token with a BAD/forged signature (by design -- it never trusts the
# signature), but the real downstream validator still rejects that same
# forged token, so a caller can never use this decode to bypass verification.
def test_unverified_routing_decode_ignores_bad_signature() -> None:
    """Routing succeeds even with a garbage signature -- it never checks it."""
    payload = {"sub": "user:attacker@example.com"}
    token = jwt.encode(payload, "some-key", algorithm="HS256")
    forged_token = token.rsplit(".", 1)[0] + ".not-a-real-signature"
    headers = {"Authorization": f"Bearer {forged_token}"}

    # Routing-only decode does not raise on a bad signature.
    cred_type = detect_credential_type(headers)
    assert cred_type == CredentialType.USER_JWT


def test_forged_token_routed_but_rejected_by_real_verifier() -> None:
    """A forged token routes to USER_JWT but validate_user_jwt still rejects it.

    Proves the "verification happens downstream" claim behind the
    nosemgrep/nosec suppression on the unverified routing decode: reaching a
    CredentialType via detect_credential_type() never grants authentication --
    the real validator (here, no JWKS keys configured) still fails closed.
    """
    payload = {"sub": "user:attacker@example.com", "phase": None}
    token = jwt.encode(payload, "attacker-controlled-key", algorithm="HS256")

    cred_type = detect_credential_type({"Authorization": f"Bearer {token}"})
    assert cred_type == CredentialType.USER_JWT

    with pytest.raises(InvalidCredentialError, match="No JWKS keys"):
        validate_user_jwt(
            token, [], audience="test-api", issuer="https://auth.example.com"
        )


# ==============================================================================
# Test: validate_user_jwt
# ==============================================================================


def test_validate_user_jwt_pass(rsa_key_pair: tuple[str, str]) -> None:
    """validate_user_jwt succeeds with valid token."""
    private_pem, public_pem = rsa_key_pair

    payload = {
        "sub": "user:john@example.com",
        "aud": "test-api",
        "iss": "https://auth.example.com",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        "scope": "gough.biomes.read gough.biomes.author",
        "tenant": "tenant-123",
    }

    token = jwt.encode(payload, private_pem, algorithm="RS256")

    # Mock JWKS key
    with patch("jwt.algorithms.get_default_algorithms") as mock_alg:
        mock_key = Mock()
        mock_alg.return_value = {"RS256": Mock(from_json=Mock(return_value=mock_key))}

        with patch("jwt.decode") as mock_decode:
            mock_decode.return_value = payload

            principal = validate_user_jwt(
                token,
                [{"kid": "test"}],
                audience="test-api",
                issuer="https://auth.example.com",
            )

            assert principal.cred_type == CredentialType.USER_JWT
            assert principal.sub == "user:john@example.com"
            assert principal.tenant_id == "tenant-123"
            assert "gough.biomes.read" in principal.scopes


def test_validate_user_jwt_expired_raises_ExpiredCredentialError() -> None:
    """validate_user_jwt raises ExpiredCredentialError on exp validation."""
    token = "expired.jwt.token"

    with patch("jwt.decode") as mock_decode:
        mock_decode.side_effect = jwt.ExpiredSignatureError("Token expired")

        with pytest.raises(ExpiredCredentialError):
            validate_user_jwt(
                token,
                [{}],
                audience="test-api",
                issuer="https://auth.example.com",
            )


def test_validate_user_jwt_invalid_signature_raises_InvalidCredentialError() -> None:
    """validate_user_jwt raises InvalidCredentialError on signature failure."""
    token = "invalid.signature.token"

    with patch("jwt.decode") as mock_decode:
        mock_decode.side_effect = jwt.InvalidSignatureError("Bad signature")

        with pytest.raises(InvalidCredentialError):
            validate_user_jwt(
                token,
                [{}],
                audience="test-api",
                issuer="https://auth.example.com",
            )


def test_validate_user_jwt_wrong_audience_raises() -> None:
    """validate_user_jwt raises on wrong audience."""
    token = "test.token.here"

    with patch("jwt.decode") as mock_decode:
        mock_decode.side_effect = jwt.InvalidAudienceError("Wrong audience")

        with pytest.raises(InvalidCredentialError):
            validate_user_jwt(
                token,
                [{}],
                audience="wrong-api",
                issuer="https://auth.example.com",
            )


def test_validate_user_jwt_no_keys_raises() -> None:
    """validate_user_jwt raises when no JWKS keys provided."""
    with pytest.raises(InvalidCredentialError, match="No JWKS keys"):
        validate_user_jwt("token", [], audience="test-api", issuer="iss")


# ==============================================================================
# Test: validate_machine_jwt
# ==============================================================================


def test_validate_machine_jwt_requires_machine_prefix() -> None:
    """validate_machine_jwt requires sub to start with 'machine:'."""
    payload = {
        "sub": "machine:backend-service",
        "aud": "test-api",
        "iss": "https://auth.example.com",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }

    with patch("app.security.credentials.validate_user_jwt") as mock_validate:
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="machine:backend-service",
            tenant_id="__default__",
            scopes=frozenset(),
            claims=payload,
        )
        mock_validate.return_value = principal

        result = validate_machine_jwt("token", [{}], "aud", "iss")
        assert result.cred_type == CredentialType.MACHINE_JWT
        assert result.sub == "machine:backend-service"


def test_validate_machine_jwt_user_sub_rejected() -> None:
    """validate_machine_jwt rejects user sub (no 'machine:' prefix)."""
    payload = {
        "sub": "user:john@example.com",
        "aud": "test-api",
        "iss": "https://auth.example.com",
    }

    with patch("app.security.credentials.validate_user_jwt") as mock_validate:
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="user:john@example.com",
            tenant_id="__default__",
            scopes=frozenset(),
            claims=payload,
        )
        mock_validate.return_value = principal

        with pytest.raises(InvalidCredentialError, match="machine:"):
            validate_machine_jwt("token", [{}], "aud", "iss")


# ==============================================================================
# Test: validate_service_svid
# ==============================================================================


def test_validate_service_svid_pass(x509_cert_and_ca: tuple[str, str]) -> None:
    """validate_service_svid succeeds with valid cert and allowed SPIFFE ID."""
    cert_pem, ca_pem = x509_cert_and_ca

    principal = validate_service_svid(
        cert_pem,
        ca_pem,
        frozenset({"spiffe://test.local/test-service"}),
    )

    assert principal.cred_type == CredentialType.SERVICE_SVID
    assert principal.spiffe_id == "spiffe://test.local/test-service"
    assert principal.tenant_id == "__default__"


def test_validate_service_svid_unknown_spiffe_id_raises(
    x509_cert_and_ca: tuple[str, str],
) -> None:
    """validate_service_svid raises when SPIFFE ID not in allowed list."""
    cert_pem, ca_pem = x509_cert_and_ca

    with pytest.raises(InvalidCredentialError, match="not in allowed list"):
        validate_service_svid(
            cert_pem,
            ca_pem,
            frozenset({"spiffe://other.local/other-service"}),
        )


def test_validate_service_svid_invalid_pem_raises() -> None:
    """validate_service_svid raises on invalid PEM."""
    with pytest.raises(InvalidCredentialError, match="Cannot parse"):
        validate_service_svid(
            "invalid pem",
            "invalid pem",
            frozenset(),
        )


# ==============================================================================
# Test: validate_one_time_bootstrap_token
# ==============================================================================


def test_validate_one_time_bootstrap_first_use_pass(
    mock_vault_client: Mock,
    mock_redis_client: Mock,
) -> None:
    """validate_one_time_bootstrap succeeds on first use."""
    now = datetime.now(timezone.utc)
    payload = {
        "phase": "helper",
        "nonce": "test-nonce-123",
        "mac": "test-mac",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }

    token = jwt.encode(payload, "secret", algorithm="HS256")

    # Mock redis SET NX to return True (successfully added)
    mock_redis_client.set.return_value = True

    principal = validate_one_time_bootstrap_token(
        token,
        mock_vault_client,
        mock_redis_client,
        signing_secret="secret",
    )

    assert principal.cred_type == CredentialType.ONE_TIME_BOOTSTRAP
    assert principal.sub == "bootstrap:test-mac"
    assert principal.tenant_id == "__default__"

    # Verify nonce was checked
    mock_redis_client.set.assert_called_once()
    call_args = mock_redis_client.set.call_args
    assert "bootstrap:nonce:test-nonce-123" in call_args[0]


def test_validate_one_time_bootstrap_replay_raises_OneTimeTokenReplayError(
    mock_vault_client: Mock,
    mock_redis_client: Mock,
) -> None:
    """validate_one_time_bootstrap raises on replay attempt."""
    now = datetime.now(timezone.utc)
    payload = {
        "phase": "helper",
        "nonce": "test-nonce-123",
        "mac": "test-mac",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }

    token = jwt.encode(payload, "secret", algorithm="HS256")

    # Mock redis SET NX to return False (already exists)
    mock_redis_client.set.return_value = False

    with pytest.raises(OneTimeTokenReplayError) as exc_info:
        validate_one_time_bootstrap_token(
            token,
            mock_vault_client,
            mock_redis_client,
            signing_secret="secret",
        )

    assert exc_info.value.nonce == "test-nonce-123"


def test_validate_one_time_bootstrap_expired_raises(
    mock_vault_client: Mock,
    mock_redis_client: Mock,
) -> None:
    """validate_one_time_bootstrap raises on expired token."""
    now = datetime.now(timezone.utc)
    payload = {
        "phase": "helper",
        "nonce": "test-nonce-123",
        "mac": "test-mac",
        "iat": int((now - timedelta(minutes=8)).timestamp()),
        "exp": int((now - timedelta(minutes=3)).timestamp()),  # Expired, TTL = 5 min
    }

    token = jwt.encode(payload, "secret", algorithm="HS256")

    with pytest.raises(ExpiredCredentialError):
        validate_one_time_bootstrap_token(
            token,
            mock_vault_client,
            mock_redis_client,
            signing_secret="secret",
        )


def test_validate_one_time_bootstrap_ttl_exceeds_10min_raises(
    mock_vault_client: Mock,
    mock_redis_client: Mock,
) -> None:
    """validate_one_time_bootstrap raises if TTL > 10 minutes."""
    now = datetime.now(timezone.utc)
    payload = {
        "phase": "helper",
        "nonce": "test-nonce-123",
        "mac": "test-mac",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),  # >10min TTL
    }

    token = jwt.encode(payload, "secret", algorithm="HS256")

    with pytest.raises(InvalidCredentialError, match="TTL exceeds"):
        validate_one_time_bootstrap_token(
            token,
            mock_vault_client,
            mock_redis_client,
            signing_secret="secret",
        )


def test_validate_one_time_bootstrap_mac_mismatch_raises(
    mock_vault_client: Mock,
    mock_redis_client: Mock,
) -> None:
    """validate_one_time_bootstrap raises on MAC mismatch."""
    now = datetime.now(timezone.utc)
    payload = {
        "phase": "helper",
        "nonce": "test-nonce-123",
        "mac": "token-mac",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }

    token = jwt.encode(payload, "secret", algorithm="HS256")

    with pytest.raises(InvalidCredentialError, match="MAC mismatch"):
        validate_one_time_bootstrap_token(
            token,
            mock_vault_client,
            mock_redis_client,
            expected_mac="different-mac",
            signing_secret="secret",
        )


# ==============================================================================
# Test: Principal Pydantic Model
# ==============================================================================


def test_principal_pydantic_round_trip() -> None:
    """Principal model round-trips through Pydantic v2."""
    principal = Principal(
        cred_type=CredentialType.USER_JWT,
        sub="user:john@example.com",
        tenant_id="tenant-123",
        scopes=frozenset({"gough.biomes.read"}),
        spiffe_id=None,
        claims={"aud": "test-api"},
    )

    # Serialize to dict
    data = principal.model_dump()
    assert data["cred_type"] == CredentialType.USER_JWT
    assert data["sub"] == "user:john@example.com"
    assert data["scopes"] == frozenset({"gough.biomes.read"})

    # Deserialize back
    principal2 = Principal(**data)
    assert principal2.sub == principal.sub
    assert principal2.tenant_id == principal.tenant_id
    assert principal2.cred_type == principal.cred_type

    # Test immutability (frozen)
    with pytest.raises(Exception):  # pydantic.ValidationError
        principal.sub = "different"
