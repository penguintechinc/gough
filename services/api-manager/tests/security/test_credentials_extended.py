"""Extended test suite for app/security/credentials.py.

Coverage targets:
- CredentialType enum
- Principal dataclass
- Exception classes
"""

import pytest
from app.security.credentials import (
    CredentialType,
    Principal,
    CredentialError,
    MissingCredentialError,
    InvalidCredentialError,
    ExpiredCredentialError,
    OneTimeTokenReplayError,
)


class TestCredentialTypeEnum:
    """Test CredentialType enum."""

    def test_credential_type_user_jwt(self):
        """Test USER_JWT credential type."""
        assert CredentialType.USER_JWT == "user_jwt"
        assert CredentialType.USER_JWT.value == "user_jwt"

    def test_credential_type_service_svid(self):
        """Test SERVICE_SVID credential type."""
        assert CredentialType.SERVICE_SVID == "service_svid"

    def test_credential_type_machine_jwt(self):
        """Test MACHINE_JWT credential type."""
        assert CredentialType.MACHINE_JWT == "machine_jwt"

    def test_credential_type_one_time_bootstrap(self):
        """Test ONE_TIME_BOOTSTRAP credential type."""
        assert CredentialType.ONE_TIME_BOOTSTRAP == "one_time_bootstrap"

    def test_credential_type_all_members(self):
        """Test all CredentialType members."""
        types = list(CredentialType)
        assert len(types) == 4
        assert CredentialType.USER_JWT in types
        assert CredentialType.SERVICE_SVID in types
        assert CredentialType.MACHINE_JWT in types
        assert CredentialType.ONE_TIME_BOOTSTRAP in types


class TestPrincipalDataclass:
    """Test Principal dataclass."""

    def test_principal_minimal(self):
        """Test Principal with required fields."""
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="user@example.com",
            tenant_id="acme",
        )
        assert principal.cred_type == CredentialType.USER_JWT
        assert principal.sub == "user@example.com"
        assert principal.tenant_id == "acme"
        assert principal.scopes == frozenset()
        assert principal.spiffe_id is None
        assert principal.claims == {}

    def test_principal_with_scopes(self):
        """Test Principal with scopes."""
        scopes = frozenset(["read:users", "write:users"])
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="admin@example.com",
            tenant_id="acme",
            scopes=scopes,
        )
        assert principal.scopes == scopes
        assert "read:users" in principal.scopes
        assert "write:users" in principal.scopes

    def test_principal_with_spiffe_id(self):
        """Test Principal with SPIFFE ID (SERVICE_SVID)."""
        principal = Principal(
            cred_type=CredentialType.SERVICE_SVID,
            sub="api-service",
            tenant_id="acme",
            spiffe_id="spiffe://example.com/api/service",
        )
        assert principal.spiffe_id == "spiffe://example.com/api/service"

    def test_principal_with_claims(self):
        """Test Principal with JWT claims."""
        claims = {
            "iss": "https://auth.example.com",
            "aud": "api",
            "iat": 1234567890,
            "exp": 1234571490,
        }
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="user@example.com",
            tenant_id="acme",
            claims=claims,
        )
        assert principal.claims == claims
        assert principal.claims["iss"] == "https://auth.example.com"

    def test_principal_frozen(self):
        """Test Principal is immutable (frozen)."""
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="user@example.com",
            tenant_id="acme",
        )
        with pytest.raises((AttributeError, Exception)):
            principal.sub = "other@example.com"

    def test_principal_scopes_frozenset(self):
        """Test Principal converts scopes to frozenset."""
        # Even if passed as list, should be frozen
        principal = Principal(
            cred_type=CredentialType.USER_JWT,
            sub="user@example.com",
            tenant_id="acme",
            scopes=frozenset(["read"]),
        )
        assert isinstance(principal.scopes, frozenset)

    def test_principal_machine_jwt_type(self):
        """Test Principal for machine JWT credentials."""
        principal = Principal(
            cred_type=CredentialType.MACHINE_JWT,
            sub="migration-worker",
            tenant_id="acme",
            scopes=frozenset(["gough.migration.trigger"]),
        )
        assert principal.cred_type == CredentialType.MACHINE_JWT
        assert "gough.migration.trigger" in principal.scopes

    def test_principal_one_time_bootstrap_type(self):
        """Test Principal for one-time bootstrap credentials."""
        principal = Principal(
            cred_type=CredentialType.ONE_TIME_BOOTSTRAP,
            sub="bootstrap-nonce-abc123",
            tenant_id="acme",
        )
        assert principal.cred_type == CredentialType.ONE_TIME_BOOTSTRAP


class TestCredentialExceptions:
    """Test exception classes."""

    def test_credential_error_base(self):
        """Test CredentialError base exception."""
        exc = CredentialError("invalid credential")
        assert str(exc) == "invalid credential"
        assert isinstance(exc, Exception)

    def test_missing_credential_error(self):
        """Test MissingCredentialError exception."""
        exc = MissingCredentialError("no authorization header")
        assert str(exc) == "no authorization header"
        assert isinstance(exc, CredentialError)

    def test_invalid_credential_error(self):
        """Test InvalidCredentialError exception."""
        exc = InvalidCredentialError("signature verification failed")
        assert str(exc) == "signature verification failed"
        assert isinstance(exc, CredentialError)

    def test_expired_credential_error(self):
        """Test ExpiredCredentialError exception."""
        exc = ExpiredCredentialError("token expired")
        assert str(exc) == "token expired"
        assert isinstance(exc, CredentialError)

    def test_one_time_token_replay_error_with_nonce(self):
        """Test OneTimeTokenReplayError with nonce."""
        nonce = "abc123def456"
        exc = OneTimeTokenReplayError(nonce=nonce)
        assert nonce in str(exc)
        assert exc.nonce == nonce
        assert isinstance(exc, CredentialError)

    def test_one_time_token_replay_error_message(self):
        """Test OneTimeTokenReplayError message format."""
        nonce = "test-nonce"
        exc = OneTimeTokenReplayError(nonce=nonce)
        assert "already used" in str(exc)
        assert nonce in str(exc)


class TestPrincipalWithAllFields:
    """Test Principal with all fields populated."""

    def test_principal_all_fields(self):
        """Test Principal with all fields."""
        principal = Principal(
            cred_type=CredentialType.SERVICE_SVID,
            sub="migration-worker",
            tenant_id="prod",
            scopes=frozenset(["gough.migration.trigger", "gough.capacity.read"]),
            spiffe_id="spiffe://prod.example.com/worker/migration",
            claims={
                "iss": "https://spire.example.com",
                "aud": "gough",
                "iat": 1234567890,
                "exp": 1234571490,
                "cn": "migration-worker",
            },
        )
        assert principal.cred_type == CredentialType.SERVICE_SVID
        assert principal.sub == "migration-worker"
        assert principal.tenant_id == "prod"
        assert len(principal.scopes) == 2
        assert principal.spiffe_id == "spiffe://prod.example.com/worker/migration"
        assert principal.claims["cn"] == "migration-worker"
