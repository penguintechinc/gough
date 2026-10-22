"""Coverage improvement tests for middleware, scope_enforcement, permissions, and tenant modules.

Focus on unit-testable functions without complex Quart context requirements.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt as pyjwt
import pytest

# ============================================================================
# middleware.py coverage tests
# ============================================================================


# NOTE (regression: gh-31): ``TestMiddlewareTokenHandling`` tested the deleted
# HS256 ``decode_token`` helper. Bearer validation now lives in the ASGI
# ``OIDCAuthMiddleware`` (ES256 StaticKeyVerifier); its valid / expired / invalid
# token behaviour is exercised end-to-end in tests/api/test_auth_e2e_gh31.py.


class TestMiddlewareGetCurrentUser:
    """Tests for get_current_user (middleware.py lines 72-90)."""

    def test_get_current_user_when_set(self):
        """Return user when g.current_user is set (lines 72-75)."""
        from app.middleware import get_current_user
        from quart import g, Quart

        app = Quart(__name__)
        import asyncio
        async def test_async():
            async with app.test_request_context("/"):
                g.current_user = {"id": 1, "email": "test@example.com"}
                user = get_current_user()
                assert user == {"id": 1, "email": "test@example.com"}

        asyncio.run(test_async())

    def test_get_current_user_when_not_set(self):
        """Return None when g.current_user is not set."""
        from app.middleware import get_current_user
        from quart import Quart

        app = Quart(__name__)
        import asyncio
        async def test_async():
            async with app.test_request_context("/"):
                user = get_current_user()
                assert user is None

        asyncio.run(test_async())

    def test_user_has_role_when_user_exists(self):
        """Return True when user has role."""
        from app.middleware import user_has_role
        from quart import g, Quart

        app = Quart(__name__)
        import asyncio
        async def test_async():
            async with app.test_request_context("/"):
                g.current_user = {"id": 1, "role": "admin"}
                assert user_has_role("admin") is True
                assert user_has_role("viewer") is False

        asyncio.run(test_async())

    def test_user_has_role_when_user_missing(self):
        """Return False when user not authenticated."""
        from app.middleware import user_has_role
        from quart import Quart

        app = Quart(__name__)
        import asyncio
        async def test_async():
            async with app.test_request_context("/"):
                assert user_has_role("admin") is False

        asyncio.run(test_async())


# NOTE (regression: gh-31): ``TestMiddlewareHelpers`` tested the deleted
# ``safe_import_tenant_middleware`` / ``safe_import_scope_enforcement`` defensive
# shims (removed once middleware wiring became a hard dependency).


# ============================================================================
# scope_enforcement.py coverage tests
# ============================================================================


class TestScopeExtractionAndValidation:
    """Tests for scope parsing and validation."""

    def test_extract_scopes_space_separated(self):
        """Parse space-separated scope string."""
        from app.security.scope_enforcement import extract_scopes_from_jwt

        payload = {"scope": "read write admin"}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset({"read", "write", "admin"})

    def test_extract_scopes_list_format(self):
        """Parse list/array scope format."""
        from app.security.scope_enforcement import extract_scopes_from_jwt

        payload = {"scope": ["read", "write", "admin"]}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset({"read", "write", "admin"})

    def test_extract_scopes_empty(self):
        """Return empty frozenset for missing/empty scope."""
        from app.security.scope_enforcement import extract_scopes_from_jwt

        assert extract_scopes_from_jwt({}) == frozenset()
        assert extract_scopes_from_jwt({"scope": ""}) == frozenset()
        assert extract_scopes_from_jwt({"scope": None}) == frozenset()

    def test_extract_scopes_list_with_whitespace(self):
        """Handle whitespace in list items."""
        from app.security.scope_enforcement import extract_scopes_from_jwt

        payload = {"scope": ["  read  ", " write ", "admin"]}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset({"read", "write", "admin"})

    def test_check_scopes_exact_match(self):
        """Pass when provided scopes exactly match required."""
        from app.security.scope_enforcement import check_scopes

        provided = frozenset({"read", "write"})
        required = frozenset({"read", "write"})
        check_scopes(provided, required)  # Should not raise

    def test_check_scopes_superset(self):
        """Pass when provided scopes are superset of required."""
        from app.security.scope_enforcement import check_scopes

        provided = frozenset({"read", "write", "admin"})
        required = frozenset({"read", "write"})
        check_scopes(provided, required)  # Should not raise

    def test_check_scopes_insufficient(self):
        """Raise when provided scopes insufficient."""
        from app.security.scope_enforcement import (
            check_scopes,
            InsufficientScopeError,
        )

        provided = frozenset({"read"})
        required = frozenset({"read", "write"})
        with pytest.raises(InsufficientScopeError) as exc_info:
            check_scopes(provided, required)
        assert exc_info.value.required == required
        assert exc_info.value.provided == provided


class TestPathNormalization:
    """Tests for path normalization and regex compilation."""

    def test_normalize_path_no_params(self):
        """Path without params returns unchanged."""
        from app.security.scope_enforcement import normalize_path

        result = normalize_path("/api/v1/users")
        assert result == "/api/v1/users"

    def test_normalize_path_with_params(self):
        """Replace all param patterns with <param>."""
        from app.security.scope_enforcement import normalize_path

        result = normalize_path("/api/v1/users/<int:user_id>/posts/<uuid:post_id>")
        assert result == "/api/v1/users/<param>/posts/<param>"

    def test_policy_to_regex_static_path(self):
        """Regex for static path matches exactly."""
        from app.security.scope_enforcement import _policy_to_regex

        pattern = _policy_to_regex("/api/v1/users")
        assert pattern.match("/api/v1/users") is not None
        assert pattern.match("/api/v1/users/123") is None

    def test_policy_to_regex_with_param(self):
        """Regex handles param placeholders."""
        from app.security.scope_enforcement import _policy_to_regex

        pattern = _policy_to_regex("/api/v1/users/<int:id>")
        assert pattern.match("/api/v1/users/123") is not None
        assert pattern.match("/api/v1/users/abc") is not None
        assert pattern.match("/api/v1/users/123/extra") is None

    def test_lookup_required_scopes_exact_match(self):
        """Lookup finds exact (method, path) match in policy."""
        from app.security.scope_enforcement import lookup_required_scopes

        with patch("app.security.scope_enforcement.SCOPE_POLICY", {
            ("GET", "/api/v1/users"): frozenset({"read"}),
            ("POST", "/api/v1/users"): frozenset({"write"}),
        }):
            result = lookup_required_scopes("GET", "/api/v1/users")
            assert result == frozenset({"read"})

    def test_lookup_required_scopes_not_found(self):
        """Lookup returns None for unknown endpoint."""
        from app.security.scope_enforcement import lookup_required_scopes

        with patch("app.security.scope_enforcement.SCOPE_POLICY", {}):
            result = lookup_required_scopes("GET", "/api/v1/unknown")
            assert result is None


# ============================================================================
# permissions.py coverage tests
# ============================================================================


class TestTeamAccessValidation:
    """Tests for team access checking."""

    def test_check_team_access_member_role_hierarchy(self):
        """Verify role hierarchy: owner > admin > member > viewer."""
        from app.permissions import TEAM_ROLES

        # Verify the order
        assert TEAM_ROLES == ["owner", "admin", "member", "viewer"]
        # owner is at index 0 (highest), viewer at index 3 (lowest)
        owner_idx = TEAM_ROLES.index("owner")
        admin_idx = TEAM_ROLES.index("admin")
        member_idx = TEAM_ROLES.index("member")
        viewer_idx = TEAM_ROLES.index("viewer")

        # Higher index = higher privilege
        assert owner_idx < admin_idx < member_idx < viewer_idx

    def test_resource_permissions_list(self):
        """Verify resource permission types."""
        from app.permissions import RESOURCE_PERMISSIONS

        assert RESOURCE_PERMISSIONS == ["read", "write", "execute", "admin", "shell"]

    def test_check_shell_access_delegates_correctly(self):
        """check_shell_access calls check_resource_permission with 'shell'."""
        from app.permissions import check_shell_access

        with patch("app.permissions.check_resource_permission", return_value=True) as mock_check:
            result = check_shell_access(user_id=1, resource_type="lxd", resource_id=5)
            assert result is True
            # Verify it was called with permission="shell"
            call_args = mock_check.call_args
            assert call_args[0] == (1, "lxd", 5, "shell")


# ============================================================================
# tenant.py coverage tests
# ============================================================================


class TestTenantExtraction:
    """Tests for tenant context extraction from JWT."""

    def test_extract_tenant_valid(self):
        """Extract tenant from valid JWT payload."""
        from app.security.tenant import extract_tenant_from_jwt

        payload = {"tenant": "tenant-acme", "cross_tenant": False}
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.tenant_id == "tenant-acme"
        assert ctx.cross_tenant is False

    def test_extract_tenant_missing_raises(self):
        """Missing tenant claim raises TenantClaimMissingError."""
        from app.security.tenant import (
            extract_tenant_from_jwt,
            TenantClaimMissingError,
        )

        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt({})

    def test_extract_tenant_empty_string_raises(self):
        """Empty tenant claim raises TenantClaimMissingError."""
        from app.security.tenant import (
            extract_tenant_from_jwt,
            TenantClaimMissingError,
        )

        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt({"tenant": ""})

    def test_extract_tenant_cross_tenant_flag(self):
        """Extract cross_tenant flag, defaulting to False."""
        from app.security.tenant import extract_tenant_from_jwt

        payload = {"tenant": "tenant-1", "cross_tenant": True}
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.cross_tenant is True

        payload = {"tenant": "tenant-2"}  # No cross_tenant
        ctx = extract_tenant_from_jwt(payload)
        assert ctx.cross_tenant is False

    def test_tenant_context_frozen(self):
        """TenantContext is immutable."""
        from app.security.tenant import TenantContext

        ctx = TenantContext(tenant_id="tenant-1")
        with pytest.raises(Exception):
            ctx.tenant_id = "tenant-2"

    def test_assert_tenant_match_identical(self):
        """Identical tenants pass without error."""
        from app.security.tenant import assert_tenant_match

        assert_tenant_match("tenant-1", "tenant-1")  # No error

    def test_assert_tenant_match_body_none(self):
        """Body tenant None is allowed (optional field)."""
        from app.security.tenant import assert_tenant_match

        assert_tenant_match("tenant-1", None)  # No error

    def test_assert_tenant_match_mismatch_raises(self):
        """Mismatched tenants raise TenantMismatchError."""
        from app.security.tenant import (
            assert_tenant_match,
            TenantMismatchError,
        )

        with pytest.raises(TenantMismatchError):
            assert_tenant_match("tenant-1", "tenant-2")


# ============================================================================
# Integration-like tests for decorator/middleware logic
# ============================================================================


class TestInsufficientScopeError:
    """Tests for InsufficientScopeError exception details."""

    def test_insufficient_scope_error_message(self):
        """InsufficientScopeError includes required and provided scopes."""
        from app.security.scope_enforcement import InsufficientScopeError

        error = InsufficientScopeError(
            required=frozenset({"read", "write"}),
            provided=frozenset({"read"}),
        )
        error_msg = str(error)
        assert "read" in error_msg
        assert "write" in error_msg
        assert "missing" in error_msg.lower()

    def test_insufficient_scope_error_attributes(self):
        """InsufficientScopeError stores required/provided attributes."""
        from app.security.scope_enforcement import InsufficientScopeError

        req = frozenset({"admin"})
        prov = frozenset({"read"})
        error = InsufficientScopeError(required=req, provided=prov)
        assert error.required == req
        assert error.provided == prov


class TestTenantErrors:
    """Tests for tenant-related exceptions."""

    def test_tenant_claim_missing_error(self):
        """TenantClaimMissingError can be raised and caught."""
        from app.security.tenant import TenantClaimMissingError

        with pytest.raises(TenantClaimMissingError):
            raise TenantClaimMissingError("Tenant missing")

    def test_tenant_mismatch_error(self):
        """TenantMismatchError can be raised and caught."""
        from app.security.tenant import TenantMismatchError

        with pytest.raises(TenantMismatchError):
            raise TenantMismatchError("Tenant mismatch")
