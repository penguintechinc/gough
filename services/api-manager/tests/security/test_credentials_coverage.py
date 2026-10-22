"""Coverage improvement tests for app/security/credentials.py.

Tests for missed coverage lines focusing on:
- Credential validation edge cases
- Error handling paths
- JWT decoding and parsing
- Missing/invalid credentials
"""

from __future__ import annotations

import pytest
import jwt
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

from app.security.credentials import (
    CredentialType,
    Principal,
    CredentialError,
    MissingCredentialError,
    InvalidCredentialError,
    ExpiredCredentialError,
    OneTimeTokenReplayError,
    detect_credential_type,
)


# ============================================================================
# Credential Detection Tests
# ============================================================================


class TestDetectCredentialType:
    """Tests for detect_credential_type() - lines 107-153."""

    def test_detect_service_svid_with_peer_cert(self):
        """Return SERVICE_SVID when peer_cert_pem provided - line 129-130."""
        headers = {"Authorization": "Bearer token"}
        result = detect_credential_type(headers, peer_cert_pem="-----BEGIN CERTIFICATE-----")

        assert result == CredentialType.SERVICE_SVID

    def test_detect_missing_credential(self):
        """Raise MissingCredentialError when no auth header or cert - line 134."""
        headers = {}

        with pytest.raises(MissingCredentialError):
            detect_credential_type(headers, peer_cert_pem=None)

    def test_detect_missing_auth_header_no_bearer(self):
        """Raise MissingCredentialError when Authorization header malformed - line 134."""
        headers = {"Authorization": "Basic user:pass"}

        with pytest.raises(MissingCredentialError):
            detect_credential_type(headers, peer_cert_pem=None)

    def test_detect_invalid_jwt_decode_error(self):
        """Raise InvalidCredentialError on JWT decode error - line 142."""
        headers = {"Authorization": "Bearer invalid.jwt.token"}

        with pytest.raises(InvalidCredentialError, match="Cannot decode JWT"):
            detect_credential_type(headers, peer_cert_pem=None)

    def test_detect_one_time_bootstrap_token(self):
        """Return ONE_TIME_BOOTSTRAP for helper phase token - line 145-146."""
        token = jwt.encode(
            {"phase": "helper", "sub": "bootstrap"},
            "secret",
            algorithm="HS256"
        )
        headers = {"Authorization": f"Bearer {token}"}

        result = detect_credential_type(headers, peer_cert_pem=None)

        assert result == CredentialType.ONE_TIME_BOOTSTRAP

    def test_detect_machine_jwt(self):
        """Return MACHINE_JWT for machine: prefixed sub - line 149-151."""
        token = jwt.encode(
            {"sub": "machine:api-server", "phase": "online"},
            "secret",
            algorithm="HS256"
        )
        headers = {"Authorization": f"Bearer {token}"}

        result = detect_credential_type(headers, peer_cert_pem=None)

        assert result == CredentialType.MACHINE_JWT

    def test_detect_user_jwt_default(self):
        """Return USER_JWT for regular user token - line 152."""
        token = jwt.encode(
            {"sub": "user-123", "email": "user@example.com"},
            "secret",
            algorithm="HS256"
        )
        headers = {"Authorization": f"Bearer {token}"}

        result = detect_credential_type(headers, peer_cert_pem=None)

        assert result == CredentialType.USER_JWT

    def test_detect_case_insensitive_authorization_header(self):
        """Handle lowercase authorization header - line 132."""
        token = jwt.encode(
            {"sub": "user-123"},
            "secret",
            algorithm="HS256"
        )
        headers = {"authorization": f"Bearer {token}"}  # lowercase

        result = detect_credential_type(headers, peer_cert_pem=None)

        assert result == CredentialType.USER_JWT

    def test_detect_machine_jwt_with_machine_prefix(self):
        """Machine JWT with various machine: subjects."""
        for subject in ["machine:api", "machine:worker-1", "machine:batch-processor"]:
            token = jwt.encode(
                {"sub": subject},
                "secret",
                algorithm="HS256"
            )
            headers = {"Authorization": f"Bearer {token}"}

            result = detect_credential_type(headers, peer_cert_pem=None)

            assert result == CredentialType.MACHINE_JWT

    def test_detect_token_without_sub_claim(self):
        """Handle token with no sub claim - line 149."""
        token = jwt.encode(
            {"phase": "online", "email": "user@example.com"},
            "secret",
            algorithm="HS256"
        )
        headers = {"Authorization": f"Bearer {token}"}

        result = detect_credential_type(headers, peer_cert_pem=None)

        # Should default to USER_JWT
        assert result == CredentialType.USER_JWT

    def test_detect_phase_claim_precedence(self):
        """Phase claim has precedence over sub - line 145-146."""
        token = jwt.encode(
            {
                "phase": "helper",
                "sub": "machine:something"  # Would be MACHINE_JWT without phase
            },
            "secret",
            algorithm="HS256"
        )
        headers = {"Authorization": f"Bearer {token}"}

        result = detect_credential_type(headers, peer_cert_pem=None)

        assert result == CredentialType.ONE_TIME_BOOTSTRAP


# ============================================================================
# Credential Type Enum Tests
# ============================================================================


class TestCredentialTypeEnum:
    """Tests for CredentialType enum."""

    def test_credential_types_exist(self):
        """Verify all credential types are defined."""
        assert CredentialType.USER_JWT == "user_jwt"
        assert CredentialType.SERVICE_SVID == "service_svid"
        assert CredentialType.MACHINE_JWT == "machine_jwt"
        assert CredentialType.ONE_TIME_BOOTSTRAP == "one_time_bootstrap"

    def test_credential_type_string_conversion(self):
        """CredentialType can be converted to string."""
        # str() of an enum includes the enum name, so just check the value attribute
        assert CredentialType.USER_JWT.value == "user_jwt"


# ============================================================================
# Principal Model Tests
# ============================================================================


class TestPrincipalModel:
    """Tests for Principal dataclass - lines 37-63."""

    def test_principal_creation_basic(self):
        """Create Principal with required fields."""
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="user-123",
            tenant_id="tenant-1"
        )

        assert principal.cred_type == CredentialType.USER_JWT
        assert principal.sub == "user-123"
        assert principal.tenant_id == "tenant-1"
        assert principal.scopes == frozenset()
        assert principal.spiffe_id is None
        assert principal.claims == {}

    def test_principal_with_scopes(self):
        """Create Principal with scopes as frozenset."""
        scopes = frozenset(["read", "write", "admin"])
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="user-123",
            tenant_id="tenant-1",
            scopes=scopes
        )

        assert principal.scopes == scopes

    def test_principal_with_spiffe_id(self):
        """Create Principal with SPIFFE ID for SERVICE_SVID."""
        principal = Principal(
            cred_type=CredentialType.SERVICE_SVID,
            sub="api-server",
            tenant_id="tenant-1",
            spiffe_id="spiffe://penguintech.io/prod/api-server"
        )

        assert principal.spiffe_id == "spiffe://penguintech.io/prod/api-server"

    def test_principal_with_claims(self):
        """Create Principal with JWT claims."""
        claims = {"email": "user@example.com", "iat": 1234567890}
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="user-123",
            tenant_id="tenant-1",
            claims=claims
        )

        assert principal.claims == claims

    def test_principal_frozen(self):
        """Principal is immutable (frozen) - line 62."""
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="user-123",
            tenant_id="tenant-1"
        )

        with pytest.raises(Exception):  # Should raise when trying to modify
            principal.sub = "different-user"

    def test_principal_scopes_immutable(self):
        """Principal scopes are frozenset (immutable)."""
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="user-123",
            tenant_id="tenant-1",
            scopes=frozenset(["read"])
        )

        with pytest.raises(AttributeError):
            principal.scopes.add("write")  # frozenset has no add method


# ============================================================================
# Exception Tests
# ============================================================================


class TestCredentialExceptions:
    """Tests for credential exception classes."""

    def test_credential_error(self):
        """CredentialError is exception base class."""
        error = CredentialError("Test error")
        assert str(error) == "Test error"
        assert isinstance(error, Exception)

    def test_missing_credential_error(self):
        """MissingCredentialError for absent credentials."""
        error = MissingCredentialError("No auth header")
        assert str(error) == "No auth header"
        assert isinstance(error, CredentialError)

    def test_invalid_credential_error(self):
        """InvalidCredentialError for malformed credentials."""
        error = InvalidCredentialError("Bad signature")
        assert str(error) == "Bad signature"
        assert isinstance(error, CredentialError)

    def test_expired_credential_error(self):
        """ExpiredCredentialError for expired credentials."""
        error = ExpiredCredentialError("Token expired")
        assert str(error) == "Token expired"
        assert isinstance(error, CredentialError)

    def test_one_time_token_replay_error(self):
        """OneTimeTokenReplayError for reused nonce - lines 94-99."""
        error = OneTimeTokenReplayError("abc123")

        assert error.nonce == "abc123"
        assert "abc123" in str(error)
        assert "already used" in str(error)
        assert isinstance(error, CredentialError)


# ============================================================================
# Integration Tests
# ============================================================================


class TestCredentialDetectionIntegration:
    """Integration tests for credential detection."""

    def test_detect_all_types(self):
        """Verify detection works for all credential types."""
        # USER_JWT
        token = jwt.encode({"sub": "user-1"}, "secret", algorithm="HS256")
        assert detect_credential_type({"Authorization": f"Bearer {token}"}) == CredentialType.USER_JWT

        # MACHINE_JWT
        token = jwt.encode({"sub": "machine:api"}, "secret", algorithm="HS256")
        assert detect_credential_type({"Authorization": f"Bearer {token}"}) == CredentialType.MACHINE_JWT

        # ONE_TIME_BOOTSTRAP
        token = jwt.encode({"phase": "helper"}, "secret", algorithm="HS256")
        assert detect_credential_type({"Authorization": f"Bearer {token}"}) == CredentialType.ONE_TIME_BOOTSTRAP

        # SERVICE_SVID
        assert detect_credential_type({}, peer_cert_pem="cert") == CredentialType.SERVICE_SVID

    def test_bearer_token_extraction(self):
        """Verify Bearer token is correctly extracted - line 136."""
        token = jwt.encode({"sub": "user-1"}, "secret", algorithm="HS256")
        headers = {"Authorization": f"Bearer {token}"}

        # Should not raise and should detect correctly
        result = detect_credential_type(headers)
        assert result == CredentialType.USER_JWT

    def test_invalid_bearer_format(self):
        """Reject malformed Bearer headers - line 133."""
        for invalid_header in ["Bearer", "Bearer ", "Bearertoken", "Bearer\t"]:
            headers = {"Authorization": invalid_header}

            with pytest.raises((MissingCredentialError, InvalidCredentialError)):
                detect_credential_type(headers)
