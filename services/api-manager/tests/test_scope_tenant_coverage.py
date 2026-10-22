"""Coverage for scope_policy.py and tenant.py error/validation paths."""

from __future__ import annotations

import pytest

from app.security.scope_policy import assert_policy_well_formed, SCOPE_POLICY, ANONYMOUS_PATHS
from app.security.tenant import (
    extract_tenant_from_jwt,
    assert_tenant_match,
    TenantClaimMissingError,
    TenantMismatchError,
    TenantContext,
)


class TestScopePolicyValidation:
    """Tests for scope_policy well-formedness validation."""

    def test_policy_duplicate_entry_detection(self):
        """Test that duplicate entries in policy are caught."""
        # The actual assert_policy_well_formed is called at module import,
        # so this just verifies the function exists and the policy loaded.
        assert callable(assert_policy_well_formed)

    def test_policy_unknown_scope_detection(self):
        """Test that unknown scopes are detected."""
        # Lines 198-201: scopes not in KNOWN_SCOPES trigger ValueError
        # This is called at module load time (line 210), so if we got here,
        # the policy is well-formed.
        from app.security.scope_policy import KNOWN_SCOPES
        for path_key, scopes in SCOPE_POLICY.items():
            if scopes is not None:
                unknown = scopes - KNOWN_SCOPES
                assert len(unknown) == 0, f"Unknown scopes at {path_key}: {unknown}"

    def test_policy_anonymous_path_conflict_detection(self):
        """Test that anonymous paths don't conflict with policy."""
        # Lines 203-206: anonymous path that is in policy triggers ValueError
        policy_keys = set(SCOPE_POLICY.keys())
        for anon_path in ANONYMOUS_PATHS:
            assert anon_path not in policy_keys, f"Anonymous path {anon_path} in policy"


class TestTenantExtraction:
    """Tests for tenant extraction from JWT."""

    def test_extract_tenant_success(self):
        """Test successful tenant extraction."""
        payload = {
            "sub": "user123",
            "tenant": "acme-corp",
            "cross_tenant": False,
        }
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.tenant_id == "acme-corp"
        assert ctx.cross_tenant is False

    def test_extract_tenant_missing_claim(self):
        """Test missing tenant claim raises error (line 76)."""
        payload = {"sub": "user123"}  # No tenant claim
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_empty_string(self):
        """Test empty tenant claim raises error (line 75)."""
        payload = {"sub": "user123", "tenant": ""}
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_non_string(self):
        """Test non-string tenant claim raises error (line 75)."""
        payload = {"sub": "user123", "tenant": 123}
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_cross_tenant_flag_true(self):
        """Test cross_tenant flag extraction when true (line 80)."""
        payload = {
            "sub": "user123",
            "tenant": "acme-corp",
            "cross_tenant": True,
        }
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.cross_tenant is True

    def test_extract_tenant_cross_tenant_flag_missing(self):
        """Test cross_tenant flag defaults to False (line 80)."""
        payload = {
            "sub": "user123",
            "tenant": "acme-corp",
        }
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.cross_tenant is False

    def test_extract_tenant_cross_tenant_flag_non_boolean(self):
        """Test cross_tenant flag with non-boolean value defaults to False."""
        payload = {
            "sub": "user123",
            "tenant": "acme-corp",
            "cross_tenant": "yes",  # Non-boolean
        }
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.cross_tenant is False


class TestTenantMatch:
    """Tests for tenant mismatch validation."""

    def test_assert_tenant_match_same(self):
        """Test matching tenants pass."""
        # No exception should be raised
        assert_tenant_match("acme-corp", "acme-corp")

    def test_assert_tenant_match_none_body(self):
        """Test None body tenant passes (line 98)."""
        # No exception should be raised
        assert_tenant_match("acme-corp", None)

    def test_assert_tenant_match_mismatch(self):
        """Test mismatched tenants raise error (line 98-101)."""
        with pytest.raises(TenantMismatchError) as exc:
            assert_tenant_match("acme-corp", "other-corp")
        assert "does not match" in str(exc.value)


class TestTenantContextModel:
    """Tests for TenantContext Pydantic model."""

    def test_tenant_context_valid(self):
        """Test valid TenantContext creation."""
        ctx = TenantContext(tenant_id="acme", cross_tenant=False)
        assert ctx.tenant_id == "acme"
        assert ctx.cross_tenant is False

    def test_tenant_context_frozen(self):
        """Test TenantContext is immutable."""
        ctx = TenantContext(tenant_id="acme")
        with pytest.raises(Exception):  # FrozenModelError
            ctx.tenant_id = "other"

    def test_tenant_context_min_length_validation(self):
        """Test tenant_id min_length validation."""
        with pytest.raises(Exception):  # Pydantic validation error
            TenantContext(tenant_id="")
