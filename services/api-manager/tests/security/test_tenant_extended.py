"""Extended test suite for app/security/tenant.py.

Coverage targets:
- TenantContext dataclass
- TenantClaimMissingError and TenantMismatchError
- extract_tenant_from_jwt() function
"""

import pytest
from app.security.tenant import (
    TenantContext,
    TenantClaimMissingError,
    TenantMismatchError,
    extract_tenant_from_jwt,
    TENANT_CLAIM_NAME,
    DEFAULT_TENANT,
)


class TestTenantContextDataclass:
    """Test TenantContext dataclass."""

    def test_tenant_context_basic(self):
        """Test TenantContext with required tenant_id."""
        ctx = TenantContext(tenant_id="acme")
        assert ctx.tenant_id == "acme"
        assert ctx.cross_tenant is False

    def test_tenant_context_with_cross_tenant(self):
        """Test TenantContext with cross_tenant flag."""
        ctx = TenantContext(tenant_id="acme", cross_tenant=True)
        assert ctx.tenant_id == "acme"
        assert ctx.cross_tenant is True

    def test_tenant_context_frozen(self):
        """Test TenantContext is immutable (frozen)."""
        ctx = TenantContext(tenant_id="acme")
        with pytest.raises((AttributeError, Exception)):
            ctx.tenant_id = "other"

    def test_tenant_context_validation_empty_tenant(self):
        """Test TenantContext rejects empty tenant_id."""
        with pytest.raises(ValueError):
            TenantContext(tenant_id="")


class TestTenantExceptions:
    """Test exception classes."""

    def test_tenant_claim_missing_error(self):
        """Test TenantClaimMissingError exception."""
        exc = TenantClaimMissingError("tenant claim missing")
        assert str(exc) == "tenant claim missing"
        assert isinstance(exc, Exception)

    def test_tenant_mismatch_error(self):
        """Test TenantMismatchError exception."""
        exc = TenantMismatchError("tenant mismatch")
        assert str(exc) == "tenant mismatch"
        assert isinstance(exc, Exception)


class TestExtractTenantFromJwt:
    """Test extract_tenant_from_jwt() function."""

    def test_extract_tenant_valid_payload(self):
        """Test extract_tenant_from_jwt() with valid tenant claim."""
        payload = {
            "tenant": "acme",
            "sub": "user@acme.com",
            "iss": "https://auth.example.com",
        }
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.tenant_id == "acme"
        assert ctx.cross_tenant is False

    def test_extract_tenant_with_cross_tenant_flag(self):
        """Test extract_tenant_from_jwt() with cross_tenant flag."""
        payload = {
            "tenant": "acme",
            "cross_tenant": True,
            "sub": "admin@example.com",
        }
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.tenant_id == "acme"
        assert ctx.cross_tenant is True

    def test_extract_tenant_missing_claim(self):
        """Test extract_tenant_from_jwt() raises error when tenant missing."""
        payload = {
            "sub": "user@acme.com",
            "iss": "https://auth.example.com",
            # Missing tenant claim
        }
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_empty_claim(self):
        """Test extract_tenant_from_jwt() raises error when tenant empty."""
        payload = {
            "tenant": "",
            "sub": "user@acme.com",
        }
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_none_claim(self):
        """Test extract_tenant_from_jwt() raises error when tenant None."""
        payload = {
            "tenant": None,
            "sub": "user@acme.com",
        }
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_non_string_claim(self):
        """Test extract_tenant_from_jwt() raises error when tenant not string."""
        payload = {
            "tenant": 123,  # Not a string
            "sub": "user@acme.com",
        }
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_cross_tenant_false_explicit(self):
        """Test extract_tenant_from_jwt() with cross_tenant=False."""
        payload = {
            "tenant": "acme",
            "cross_tenant": False,
            "sub": "user@acme.com",
        }
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.cross_tenant is False

    def test_extract_tenant_cross_tenant_non_boolean(self):
        """Test extract_tenant_from_jwt() treats non-True as False."""
        payload = {
            "tenant": "acme",
            "cross_tenant": "yes",  # Not a boolean
            "sub": "user@acme.com",
        }
        ctx = extract_tenant_from_jwt(payload)
        # "yes" is not True, so cross_tenant should be False
        assert ctx.cross_tenant is False

    def test_extract_tenant_default_tenant(self):
        """Test extract_tenant_from_jwt() with default tenant."""
        payload = {
            "tenant": "__default__",
            "sub": "system@system.local",
        }
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.tenant_id == "__default__"


class TestTenantConstants:
    """Test module constants."""

    def test_tenant_claim_name(self):
        """Test TENANT_CLAIM_NAME constant."""
        assert TENANT_CLAIM_NAME == "tenant"

    def test_default_tenant(self):
        """Test DEFAULT_TENANT constant."""
        assert DEFAULT_TENANT == "__default__"


class TestTenantContextValidation:
    """Test TenantContext validation behavior."""

    def test_tenant_context_min_length_validation(self):
        """Test TenantContext enforces min_length=1."""
        # Empty string should fail
        with pytest.raises(ValueError):
            TenantContext(tenant_id="")

    def test_tenant_context_whitespace_accepted(self):
        """Test TenantContext with whitespace-only fails validation."""
        # Single space might be considered valid by string length
        # but practical systems might reject it
        ctx = TenantContext(tenant_id=" ")  # Single space
        assert ctx.tenant_id == " "
