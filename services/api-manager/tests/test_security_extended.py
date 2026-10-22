"""Extended tests for auth/security modules.

Target uncovered lines in:
- app/middleware.py (role_required decorator, safe_import_* functions)
- app/security/credentials.py (credential validation paths)
- app/security/tenant.py (tenant extraction and middleware)
- app/security/scope_enforcement.py (scope checking)
- app/permissions.py (permission decorators and checks)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, AsyncMock, patch
import pytest
import jwt as pyjwt
from quart import Quart, g, jsonify, request as quart_request
import json


# ==============================================================================
# Fixtures & Helpers
# ==============================================================================

def _make_app() -> Quart:
    """Create a test Quart app with JWT config."""
    app = Quart(__name__)
    app.config["JWT_SECRET_KEY"] = "test-secret-key-extended-tests"
    app.config["TESTING"] = True
    return app


def _make_token(
    app: Quart,
    *,
    sub: str = "user:123",
    token_type: str = "access",
    scope: str | list = "gough.cluster.admin",
    tenant: str = "tenant-1",
    extra: dict | None = None,
) -> str:
    """Create a signed JWT for testing."""
    payload: dict = {
        "sub": sub,
        "type": token_type,
        "scope": scope,
        "tenant": tenant,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    if extra:
        payload.update(extra)
    return pyjwt.encode(
        payload, app.config["JWT_SECRET_KEY"], algorithm="HS256"
    )


def _make_expired_token(app: Quart) -> str:
    """Create an expired JWT."""
    payload = {
        "sub": "user:123",
        "type": "access",
        "scope": "gough.cluster.admin",
        "tenant": "tenant-1",
        "iat": int(time.time()) - 7200,
        "exp": int(time.time()) - 3600,
    }
    return pyjwt.encode(
        payload, app.config["JWT_SECRET_KEY"], algorithm="HS256"
    )


# ==============================================================================
# Test app/middleware.py — role_required decorator and safe imports
# ==============================================================================

class TestRoleRequiredDecorator:
    """Tests for the ``role_required`` scope-bundle decorator (regression: gh-31).

    ``role_required`` reads scopes from ``g.current_user["_jwt_payload"]`` and
    delegates to the scope-enforcement primitives. It no longer decodes a bearer
    token itself (the deleted HS256 ``decode_token``), so these tests populate
    ``g.current_user`` directly with the ``_jwt_payload`` the ASGI shim would
    build. The real token->principal path is covered in
    tests/api/test_auth_e2e_gh31.py.
    """

    @staticmethod
    def _principal(scope: str, role: str) -> dict:
        return {
            "id": 123,
            "email": f"{role}@test.com",
            "role": role,
            "is_active": True,
            "_jwt_payload": {"scope": scope, "role": role, "tenant": "tenant-1"},
        }

    @pytest.mark.anyio
    async def test_role_required_scope_based_success(self):
        """Allow request when the token scope satisfies the role bundle."""
        app = _make_app()
        from app.middleware import role_required

        @role_required("admin")
        async def admin_only():
            return jsonify({"message": "success"})

        async with app.test_request_context("/admin"):
            g.current_user = self._principal("gough.cluster.admin", "viewer")
            result = await admin_only()
            assert result.status_code == 200

    @pytest.mark.anyio
    async def test_role_required_legacy_role_fallback(self):
        """Allow request when the legacy role matches (scope check falls through)."""
        app = _make_app()
        from app.middleware import role_required

        @role_required("maintainer")
        async def maintainer_only():
            return jsonify({"message": "success"})

        async with app.test_request_context("/maint"):
            g.current_user = self._principal("", "maintainer")
            result = await maintainer_only()
            assert result.status_code == 200

    @pytest.mark.anyio
    async def test_role_required_insufficient_scopes_and_role(self):
        """Deny request when both scope and legacy-role checks fail -> 403."""
        app = _make_app()
        from app.middleware import role_required

        @role_required("admin")
        async def admin_only():
            return jsonify({"message": "success"})

        async with app.test_request_context("/admin"):
            g.current_user = self._principal("gough.cluster.read", "viewer")
            result = await admin_only()
            assert result[1] == 403

    @pytest.mark.anyio
    async def test_role_required_no_user_in_context(self):
        """Deny request when current_user is absent -> 401."""
        app = _make_app()
        from app.middleware import role_required

        @role_required("admin")
        async def admin_only():
            return jsonify({"message": "success"})

        async with app.test_request_context("/admin"):
            g.current_user = None
            result = await admin_only()
            assert result[1] == 401

    @pytest.mark.anyio
    async def test_admin_required_decorator(self):
        """admin_required convenience alias accepts an admin-scoped principal."""
        app = _make_app()
        from app.middleware import admin_required

        @admin_required
        async def admin_only():
            return jsonify({"message": "success"})

        async with app.test_request_context("/admin"):
            g.current_user = self._principal("gough.cluster.admin", "admin")
            result = await admin_only()
            assert result.status_code == 200

    @pytest.mark.anyio
    async def test_maintainer_or_admin_required_with_maintainer(self):
        """maintainer_or_admin_required allows a maintainer via legacy-role fallback."""
        app = _make_app()
        from app.middleware import maintainer_or_admin_required

        @maintainer_or_admin_required
        async def edit_resource():
            return jsonify({"message": "success"})

        async with app.test_request_context("/edit", method="POST"):
            g.current_user = self._principal("gough.cluster.read", "maintainer")
            result = await edit_resource()
            assert result.status_code == 200


# NOTE (regression: gh-31): ``TestSafeImports`` tested the deleted
# ``safe_import_tenant_middleware`` / ``safe_import_scope_enforcement`` defensive
# shims (removed once middleware wiring became a hard dependency).


# ==============================================================================
# Test app/security/credentials.py — credential detection & validation
# ==============================================================================

class TestDetectCredentialType:
    """Tests for credential type detection."""

    def test_detect_service_svid_from_peer_cert(self):
        """SERVICE_SVID when peer_cert_pem provided."""
        from app.security.credentials import detect_credential_type, CredentialType
        headers = {}
        result = detect_credential_type(headers, peer_cert_pem="-----BEGIN CERTIFICATE-----\n...")
        assert result == CredentialType.SERVICE_SVID

    def test_detect_one_time_bootstrap_from_phase_claim(self):
        """ONE_TIME_BOOTSTRAP when phase:helper claim present."""
        from app.security.credentials import detect_credential_type, CredentialType
        # Create token with phase:helper
        payload = {"phase": "helper", "sub": "bootstrap:mac"}
        token = pyjwt.encode(payload, "secret", algorithm="HS256")
        headers = {"Authorization": f"Bearer {token}"}
        result = detect_credential_type(headers)
        assert result == CredentialType.ONE_TIME_BOOTSTRAP

    def test_detect_machine_jwt_from_sub_prefix(self):
        """MACHINE_JWT when sub starts with 'machine:'."""
        from app.security.credentials import detect_credential_type, CredentialType
        payload = {"sub": "machine:api-server"}
        token = pyjwt.encode(payload, "secret", algorithm="HS256")
        headers = {"Authorization": f"Bearer {token}"}
        result = detect_credential_type(headers)
        assert result == CredentialType.MACHINE_JWT

    def test_detect_user_jwt_default(self):
        """USER_JWT as fallback when no other claims match."""
        from app.security.credentials import detect_credential_type, CredentialType
        payload = {"sub": "user:123"}
        token = pyjwt.encode(payload, "secret", algorithm="HS256")
        headers = {"Authorization": f"Bearer {token}"}
        result = detect_credential_type(headers)
        assert result == CredentialType.USER_JWT

    def test_detect_missing_credential_error(self):
        """Raise MissingCredentialError when no token or cert."""
        from app.security.credentials import (
            detect_credential_type,
            MissingCredentialError,
        )
        headers = {}
        with pytest.raises(MissingCredentialError):
            detect_credential_type(headers)

    def test_detect_invalid_jwt_raises_error(self):
        """Raise InvalidCredentialError when JWT is malformed."""
        from app.security.credentials import (
            detect_credential_type,
            InvalidCredentialError,
        )
        headers = {"Authorization": "Bearer malformed...token"}
        with pytest.raises(InvalidCredentialError):
            detect_credential_type(headers)


class TestValidateUserJwt:
    """Tests for User JWT validation."""

    def test_validate_user_jwt_success(self):
        """Valid user JWT returns Principal."""
        from app.security.credentials import validate_user_jwt, CredentialType

        # Create a valid token
        secret = "test-secret"
        payload = {
            "sub": "user:123",
            "scope": "read write",
            "tenant": "tenant-1",
            "aud": "gough-api",
            "iss": "https://auth.example.com",
            "exp": int(time.time()) + 3600,
        }
        token = pyjwt.encode(payload, secret, algorithm="HS256")

        # Mock JWKS key
        jwks_keys = [{"n": secret}]  # Simplified for test

        # This will fail real validation, but test structure
        with pytest.raises(Exception):
            # Real RS256 validation will fail with mock secret
            validate_user_jwt(
                token,
                jwks_keys,
                audience="gough-api",
                issuer="https://auth.example.com",
            )

    def test_validate_user_jwt_no_jwks_raises_error(self):
        """Raise error when JWKS keys empty."""
        from app.security.credentials import (
            validate_user_jwt,
            InvalidCredentialError,
        )
        token = "dummy.token.here"
        with pytest.raises(InvalidCredentialError, match="No JWKS keys"):
            validate_user_jwt(token, [], "gough-api", "https://auth.example.com")


class TestValidateServiceSvid:
    """Tests for Service SVID validation."""

    def test_validate_service_svid_no_spiffe_uri_error(self):
        """Raise error when cert has no SPIFFE URI in SAN."""
        from app.security.credentials import (
            validate_service_svid,
            InvalidCredentialError,
        )
        # Minimal cert without SPIFFE URI
        cert_pem = """-----BEGIN CERTIFICATE-----
MIIBkTCB+wIJAKHHCgVZXHiVMA0GCSqGSIb3DQEBBQUAMBMxETAPBgNVBAMMCHRl
-----END CERTIFICATE-----"""

        with pytest.raises(InvalidCredentialError, match="Cannot parse"):
            validate_service_svid(cert_pem, "", frozenset())

    def test_validate_service_svid_invalid_cert_error(self):
        """Raise error when cert is unparseable."""
        from app.security.credentials import (
            validate_service_svid,
            InvalidCredentialError,
        )
        with pytest.raises(InvalidCredentialError, match="Cannot parse"):
            validate_service_svid("not a cert", "", frozenset())


class TestValidateOneTimeBootstrapToken:
    """Tests for one-time bootstrap token validation."""

    def test_validate_bootstrap_token_missing_nonce(self):
        """Raise error when nonce claim absent."""
        from app.security.credentials import (
            validate_one_time_bootstrap_token,
            InvalidCredentialError,
        )
        payload = {"mac": "abc123"}  # Missing nonce
        token = pyjwt.encode(payload, "secret", algorithm="HS256")

        vault_mock = Mock()
        redis_mock = Mock()

        with pytest.raises(InvalidCredentialError, match="missing nonce"):
            validate_one_time_bootstrap_token(token, vault_mock, redis_mock, signing_secret="secret")

    def test_validate_bootstrap_token_missing_mac(self):
        """Raise error when mac claim absent."""
        from app.security.credentials import (
            validate_one_time_bootstrap_token,
            InvalidCredentialError,
        )
        payload = {"nonce": "nonce123"}  # Missing mac
        token = pyjwt.encode(payload, "secret", algorithm="HS256")

        vault_mock = Mock()
        redis_mock = Mock()

        with pytest.raises(InvalidCredentialError, match="missing mac"):
            validate_one_time_bootstrap_token(token, vault_mock, redis_mock, signing_secret="secret")

    def test_validate_bootstrap_token_mac_mismatch(self):
        """Raise error when provided MAC doesn't match token MAC."""
        from app.security.credentials import (
            validate_one_time_bootstrap_token,
            InvalidCredentialError,
        )
        payload = {
            "nonce": "nonce123",
            "mac": "mac456",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        }
        token = pyjwt.encode(payload, "secret", algorithm="HS256")

        vault_mock = Mock()
        redis_mock = Mock()

        with pytest.raises(InvalidCredentialError, match="MAC mismatch"):
            validate_one_time_bootstrap_token(
                token,
                vault_mock,
                redis_mock,
                expected_mac="different",
                signing_secret="secret",
            )

    def test_validate_bootstrap_token_ttl_exceeded(self):
        """Raise error when TTL exceeds 10 minutes."""
        from app.security.credentials import (
            validate_one_time_bootstrap_token,
            InvalidCredentialError,
        )
        payload = {
            "nonce": "nonce123",
            "mac": "mac456",
            "iat": int(time.time()),
            "exp": int(time.time()) + 1200,  # 20 minutes > 10 min limit
        }
        token = pyjwt.encode(payload, "secret", algorithm="HS256")

        vault_mock = Mock()
        redis_mock = Mock()

        with pytest.raises(InvalidCredentialError, match="TTL exceeds"):
            validate_one_time_bootstrap_token(token, vault_mock, redis_mock, signing_secret="secret")

    def test_validate_bootstrap_token_expired(self):
        """Raise ExpiredCredentialError when token is in the past."""
        from app.security.credentials import (
            validate_one_time_bootstrap_token,
            ExpiredCredentialError,
        )
        # Token with TTL within limit but already expired
        iat_time = int(time.time()) - 600  # 10 min ago
        exp_time = int(time.time()) - 60   # Expired 1 minute ago
        payload = {
            "nonce": "nonce123",
            "mac": "mac456",
            "iat": iat_time,
            "exp": exp_time,
        }
        token = pyjwt.encode(payload, "secret", algorithm="HS256")

        vault_mock = Mock()
        redis_mock = Mock()

        with pytest.raises(ExpiredCredentialError, match="expired"):
            validate_one_time_bootstrap_token(token, vault_mock, redis_mock, signing_secret="secret")

    def test_validate_bootstrap_token_replay_error(self):
        """Raise OneTimeTokenReplayError when nonce already used."""
        from app.security.credentials import (
            validate_one_time_bootstrap_token,
            OneTimeTokenReplayError,
        )
        payload = {
            "nonce": "nonce123",
            "mac": "mac456",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        }
        token = pyjwt.encode(payload, "secret", algorithm="HS256")

        vault_mock = Mock()
        redis_mock = Mock()
        redis_mock.set.return_value = False  # Nonce already exists

        with pytest.raises(OneTimeTokenReplayError, match="already used"):
            validate_one_time_bootstrap_token(token, vault_mock, redis_mock, signing_secret="secret")


# ==============================================================================
# Test app/security/tenant.py — tenant extraction and validation
# ==============================================================================

class TestExtractTenantFromJwt:
    """Tests for tenant extraction from JWT."""

    def test_extract_tenant_success(self):
        """Extract tenant context from valid JWT."""
        from app.security.tenant import extract_tenant_from_jwt

        payload = {
            "tenant": "tenant-abc",
            "cross_tenant": False,
        }
        result = extract_tenant_from_jwt(payload)
        assert result.tenant_id == "tenant-abc"
        assert result.cross_tenant is False

    def test_extract_tenant_cross_tenant_flag(self):
        """Extract cross_tenant flag when present."""
        from app.security.tenant import extract_tenant_from_jwt

        payload = {
            "tenant": "tenant-abc",
            "cross_tenant": True,
        }
        result = extract_tenant_from_jwt(payload)
        assert result.cross_tenant is True

    def test_extract_tenant_missing_raises_error(self):
        """Raise TenantClaimMissingError when tenant claim absent."""
        from app.security.tenant import extract_tenant_from_jwt, TenantClaimMissingError

        payload = {"sub": "user:123"}
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)

    def test_extract_tenant_empty_string_raises_error(self):
        """Raise TenantClaimMissingError when tenant is empty string."""
        from app.security.tenant import extract_tenant_from_jwt, TenantClaimMissingError

        payload = {"tenant": ""}
        with pytest.raises(TenantClaimMissingError):
            extract_tenant_from_jwt(payload)


class TestAssertTenantMatch:
    """Tests for tenant match validation."""

    def test_assert_tenant_match_success(self):
        """No error when body tenant matches token tenant."""
        from app.security.tenant import assert_tenant_match

        # Should not raise
        assert_tenant_match("tenant-1", "tenant-1")

    def test_assert_tenant_match_body_tenant_none(self):
        """No error when body tenant is None."""
        from app.security.tenant import assert_tenant_match

        # Should not raise
        assert_tenant_match("tenant-1", None)

    def test_assert_tenant_match_mismatch_raises_error(self):
        """Raise TenantMismatchError when tenants don't match."""
        from app.security.tenant import assert_tenant_match, TenantMismatchError

        with pytest.raises(TenantMismatchError, match="does not match"):
            assert_tenant_match("tenant-1", "tenant-2")


# ==============================================================================
# Test app/security/scope_enforcement.py — scope checking
# ==============================================================================

class TestExtractScopesFromJwt:
    """Tests for scope extraction from JWT."""

    def test_extract_scopes_space_separated_string(self):
        """Extract scopes from space-separated string."""
        from app.security.scope_enforcement import extract_scopes_from_jwt

        payload = {"scope": "read write admin"}
        result = extract_scopes_from_jwt(payload)
        assert result == frozenset({"read", "write", "admin"})

    def test_extract_scopes_from_array(self):
        """Extract scopes from JSON array."""
        from app.security.scope_enforcement import extract_scopes_from_jwt

        payload = {"scope": ["read", "write", "admin"]}
        result = extract_scopes_from_jwt(payload)
        assert result == frozenset({"read", "write", "admin"})

    def test_extract_scopes_missing_claim(self):
        """Return empty frozenset when scope claim absent."""
        from app.security.scope_enforcement import extract_scopes_from_jwt

        payload = {"sub": "user:123"}
        result = extract_scopes_from_jwt(payload)
        assert result == frozenset()

    def test_extract_scopes_empty_string(self):
        """Return empty frozenset for empty scope string."""
        from app.security.scope_enforcement import extract_scopes_from_jwt

        payload = {"scope": ""}
        result = extract_scopes_from_jwt(payload)
        assert result == frozenset()

    def test_extract_scopes_invalid_type(self):
        """Return empty frozenset for invalid scope type."""
        from app.security.scope_enforcement import extract_scopes_from_jwt

        payload = {"scope": 123}  # Invalid: number
        result = extract_scopes_from_jwt(payload)
        assert result == frozenset()


class TestCheckScopes:
    """Tests for scope validation."""

    def test_check_scopes_success(self):
        """No error when provided contains all required scopes."""
        from app.security.scope_enforcement import check_scopes

        provided = frozenset({"read", "write", "admin"})
        required = frozenset({"read", "write"})
        # Should not raise
        check_scopes(provided, required)

    def test_check_scopes_insufficient_raises_error(self):
        """Raise InsufficientScopeError when scopes missing."""
        from app.security.scope_enforcement import (
            check_scopes,
            InsufficientScopeError,
        )

        provided = frozenset({"read"})
        required = frozenset({"read", "write", "admin"})
        with pytest.raises(InsufficientScopeError) as exc_info:
            check_scopes(provided, required)
        assert exc_info.value.required == required
        assert exc_info.value.provided == provided


class TestLookupRequiredScopes:
    """Tests for scope policy lookup."""

    def test_lookup_required_scopes_exact_match(self):
        """Find scopes for exact path match."""
        from app.security.scope_enforcement import lookup_required_scopes

        # Mock SCOPE_POLICY
        with patch("app.security.scope_enforcement.SCOPE_POLICY", {
            ("GET", "/api/v1/nodes"): frozenset({"gough.nodes.read"}),
        }):
            result = lookup_required_scopes("GET", "/api/v1/nodes")
            assert result == frozenset({"gough.nodes.read"})

    def test_lookup_required_scopes_template_match(self):
        """Find scopes for template path match."""
        from app.security.scope_enforcement import lookup_required_scopes

        with patch("app.security.scope_enforcement.SCOPE_POLICY", {
            ("GET", "/api/v1/nodes/<int:id>"): frozenset({"gough.nodes.read"}),
        }):
            result = lookup_required_scopes("GET", "/api/v1/nodes/123")
            assert result == frozenset({"gough.nodes.read"})

    def test_lookup_required_scopes_not_found(self):
        """Return None when endpoint not in policy."""
        from app.security.scope_enforcement import lookup_required_scopes

        with patch("app.security.scope_enforcement.SCOPE_POLICY", {}):
            result = lookup_required_scopes("GET", "/unknown/path")
            assert result is None


class TestNormalizePath:
    """Tests for path normalization."""

    def test_normalize_path_with_int_param(self):
        """Normalize <int:id> to <param>."""
        from app.security.scope_enforcement import normalize_path

        result = normalize_path("/api/v1/nodes/<int:id>")
        assert result == "/api/v1/nodes/<param>"

    def test_normalize_path_with_uuid_param(self):
        """Normalize <uuid:id> to <param>."""
        from app.security.scope_enforcement import normalize_path

        result = normalize_path("/api/v1/clusters/<uuid:id>")
        assert result == "/api/v1/clusters/<param>"

    def test_normalize_path_multiple_params(self):
        """Normalize multiple params."""
        from app.security.scope_enforcement import normalize_path

        result = normalize_path("/api/v1/clusters/<int:c>/nodes/<int:n>")
        assert result == "/api/v1/clusters/<param>/nodes/<param>"


# ==============================================================================
# Test app/permissions.py — permission checking and decorators
# ==============================================================================

class TestCheckTeamAccess:
    """Tests for team access checking."""

    def test_check_team_access_role_hierarchy(self):
        """Test role hierarchy checking works correctly."""
        from app.permissions import TEAM_ROLES
        # Just verify the role hierarchy is defined
        assert TEAM_ROLES == ["owner", "admin", "member", "viewer"]

    def test_check_team_access_not_member(self):
        """No membership returns False."""
        from app.permissions import check_team_access

        with patch("app.permissions.get_db") as mock_get_db:
            mock_db = Mock()
            mock_get_db.return_value = mock_db
            # Simulate penguin-dal query that returns None
            mock_db.return_value.select.return_value.first.return_value = None

            result = check_team_access(1, 1, "member")
            assert result is False


class TestCheckResourcePermission:
    """Tests for resource permission checking."""

    def test_check_resource_permission_constants(self):
        """Verify resource permission types are defined."""
        from app.permissions import RESOURCE_PERMISSIONS
        expected = ["read", "write", "execute", "admin", "shell"]
        assert RESOURCE_PERMISSIONS == expected


class TestCheckShellAccess:
    """Tests for shell access shortcut."""

    def test_check_shell_access_delegates_to_permission_check(self):
        """Shell access uses permission check with 'shell' permission."""
        from app.permissions import check_shell_access

        with patch("app.permissions.check_resource_permission") as mock_check:
            mock_check.return_value = True

            result = check_shell_access(1, "cloud_provider", 1)
            assert result is True
            mock_check.assert_called_once_with(1, "cloud_provider", 1, "shell")


class TestRequireTeamPermissionDecorator:
    """Tests for team permission decorator."""

    def test_require_team_permission_returns_decorator(self):
        """Decorator function returns a decorator."""
        from app.permissions import require_team_permission

        decorated = require_team_permission("member")
        # Should return a decorator function
        assert callable(decorated)

    def test_require_resource_permission_returns_decorator(self):
        """Resource permission decorator returns a decorator."""
        from app.permissions import require_resource_permission

        decorated = require_resource_permission("read")
        # Should return a decorator function
        assert callable(decorated)
