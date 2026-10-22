"""Unit tests for tenant isolation (layer 1 + 4 primitives)."""

from __future__ import annotations

import pytest

from app.security.tenant import (
    TENANT_CLAIM_NAME,
    DEFAULT_TENANT,
    TenantContext,
    TenantClaimMissingError,
    TenantMismatchError,
    extract_tenant_from_jwt,
    assert_tenant_match,
)


# ==============================================================================
# Test: TenantContext Pydantic Model
# ==============================================================================

class TestTenantContext:
    """TenantContext data model tests."""

    def test_tenant_context_creation(self) -> None:
        """Test basic TenantContext instantiation."""
        ctx = TenantContext(tenant_id="acme-corp", cross_tenant=False)
        assert ctx.tenant_id == "acme-corp"
        assert ctx.cross_tenant is False

    def test_tenant_context_defaults(self) -> None:
        """Test TenantContext cross_tenant defaults to False."""
        ctx = TenantContext(tenant_id="default")
        assert ctx.cross_tenant is False

    def test_tenant_context_frozen(self) -> None:
        """Test TenantContext is immutable (frozen)."""
        ctx = TenantContext(tenant_id="test")
        with pytest.raises(Exception):  # FrozenInstanceError
            ctx.tenant_id = "modified"


# ==============================================================================
# Test: JWT Tenant Extraction
# ==============================================================================

class TestExtractTenantFromJWT:
    """Tests for extract_tenant_from_jwt function."""

    def test_extract_tenant_from_valid_jwt(self) -> None:
        """Test successful tenant extraction from JWT payload."""
        payload = {
            "sub": "user-123",
            TENANT_CLAIM_NAME: "acme-corp",
            "iss": "auth-service",
        }
        ctx = extract_tenant_from_jwt(payload)

        assert isinstance(ctx, TenantContext)
        assert ctx.tenant_id == "acme-corp"
        assert ctx.cross_tenant is False

    def test_extract_tenant_missing_claim_raises(self) -> None:
        """Test TenantClaimMissingError when tenant claim absent."""
        payload = {
            "sub": "user-123",
            "iss": "auth-service",
        }
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_null_claim_raises(self) -> None:
        """Test TenantClaimMissingError when tenant claim is null."""
        payload = {
            "sub": "user-123",
            TENANT_CLAIM_NAME: None,
        }
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_empty_string_raises(self) -> None:
        """Test TenantClaimMissingError when tenant claim is empty string."""
        payload = {
            "sub": "user-123",
            TENANT_CLAIM_NAME: "",
        }
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_non_string_claim_raises(self) -> None:
        """Test TenantClaimMissingError when tenant claim is not a string."""
        payload = {
            "sub": "user-123",
            TENANT_CLAIM_NAME: 12345,
        }
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_cross_tenant_super_admin(self) -> None:
        """Test extraction with cross_tenant override (super-admin)."""
        payload = {
            "sub": "admin-user",
            TENANT_CLAIM_NAME: "admin-tenant",
            "cross_tenant": True,
        }
        ctx = extract_tenant_from_jwt(payload)

        assert ctx.tenant_id == "admin-tenant"
        assert ctx.cross_tenant is True

    def test_extract_tenant_cross_tenant_false_normal_user(self) -> None:
        """Test cross_tenant is False for normal users."""
        payload = {
            "sub": "user-123",
            TENANT_CLAIM_NAME: "acme-corp",
            "cross_tenant": False,
        }
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.cross_tenant is False

    def test_extract_tenant_cross_tenant_missing_defaults_false(self) -> None:
        """Test cross_tenant defaults to False if claim absent."""
        payload = {
            "sub": "user-123",
            TENANT_CLAIM_NAME: "acme-corp",
        }
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.cross_tenant is False


# ==============================================================================
# Test: Tenant Match Validation
# ==============================================================================

class TestAssertTenantMatch:
    """Tests for assert_tenant_match function."""

    def test_assert_tenant_match_pass(self) -> None:
        """Test no error when token_tenant == body_tenant."""
        # Should return without raising
        assert_tenant_match("acme-corp", "acme-corp")

    def test_assert_tenant_match_none_body_passes(self) -> None:
        """Test no error when body_tenant is None."""
        # Should return without raising
        assert_tenant_match("acme-corp", None)

    def test_assert_tenant_match_mismatch_raises(self) -> None:
        """Test TenantMismatchError when tenant IDs differ."""
        with pytest.raises(TenantMismatchError):
            assert_tenant_match("acme-corp", "rival-corp")

    def test_assert_tenant_match_error_message(self) -> None:
        """Test TenantMismatchError includes both tenant IDs."""
        with pytest.raises(TenantMismatchError) as exc_info:
            assert_tenant_match("acme-corp", "rival-corp")

        error_msg = str(exc_info.value)
        assert "rival-corp" in error_msg
        assert "acme-corp" in error_msg


# ==============================================================================
# Test: Constants
# ==============================================================================

class TestConstants:
    """Tests for module-level constants."""

    def test_tenant_claim_name_constant(self) -> None:
        """Test TENANT_CLAIM_NAME constant."""
        assert TENANT_CLAIM_NAME == "tenant"

    def test_default_tenant_constant(self) -> None:
        """Test DEFAULT_TENANT constant for single-tenant fallback."""
        assert DEFAULT_TENANT == "__default__"
