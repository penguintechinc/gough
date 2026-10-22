"""Tests for scope enforcement module."""

from __future__ import annotations

import pytest

from app.security.scope_enforcement import (
    InsufficientScopeError,
    check_scopes,
    extract_scopes_from_jwt,
    lookup_required_scopes,
    normalize_path,
    require_scopes,
)
from app.security.scope_policy import SCOPE_POLICY


class TestExtractScopesFromJwt:
    """Test scope extraction from JWT payload."""

    def test_extract_scopes_from_string_claim(self) -> None:
        """Space-separated scope string is parsed correctly."""
        payload = {"scope": "a b c"}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset({"a", "b", "c"})

    def test_extract_scopes_from_array_claim(self) -> None:
        """JSON array scope claim is parsed correctly."""
        payload = {"scope": ["a", "b", "c"]}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset({"a", "b", "c"})

    def test_extract_scopes_empty_string(self) -> None:
        """Empty string returns empty frozenset."""
        payload = {"scope": ""}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset()

    def test_extract_scopes_missing_claim(self) -> None:
        """Missing scope claim returns empty frozenset."""
        payload = {}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset()

    def test_extract_scopes_empty_array(self) -> None:
        """Empty array returns empty frozenset."""
        payload = {"scope": []}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset()

    def test_extract_scopes_with_extra_whitespace(self) -> None:
        """Extra whitespace is trimmed correctly."""
        payload = {"scope": "  a   b   c  "}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset({"a", "b", "c"})

    def test_extract_scopes_none_type(self) -> None:
        """Non-string, non-list types return empty frozenset."""
        payload = {"scope": 123}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset()


class TestCheckScopes:
    """Test scope validation logic."""

    def test_check_scopes_pass_exact_match(self) -> None:
        """Exact scope match does not raise."""
        provided = frozenset({"a", "b", "c"})
        required = frozenset({"a", "b"})
        check_scopes(provided, required)  # Should not raise

    def test_check_scopes_pass_with_extras(self) -> None:
        """Extra scopes beyond required does not raise."""
        provided = frozenset({"a", "b", "c", "d"})
        required = frozenset({"a", "b"})
        check_scopes(provided, required)  # Should not raise

    def test_check_scopes_pass_empty_required(self) -> None:
        """Empty required scopes always pass."""
        provided = frozenset({"a"})
        required = frozenset()
        check_scopes(provided, required)  # Should not raise

    def test_check_scopes_fail_missing_one(self) -> None:
        """Missing a single scope raises InsufficientScopeError."""
        provided = frozenset({"a", "b"})
        required = frozenset({"a", "b", "c"})
        with pytest.raises(InsufficientScopeError) as exc_info:
            check_scopes(provided, required)
        assert exc_info.value.required == required
        assert exc_info.value.provided == provided

    def test_check_scopes_fail_missing_all(self) -> None:
        """Missing all required scopes raises."""
        provided = frozenset({"x", "y"})
        required = frozenset({"a", "b", "c"})
        with pytest.raises(InsufficientScopeError):
            check_scopes(provided, required)

    def test_insufficient_scope_error_attributes(self) -> None:
        """InsufficientScopeError stores required and provided."""
        required = frozenset({"a", "b"})
        provided = frozenset({"a"})
        err = InsufficientScopeError(required=required, provided=provided)
        assert err.required == required
        assert err.provided == provided


class TestNormalizePath:
    """Test URL path normalization."""

    def test_normalize_int_param(self) -> None:
        """<int:id> is normalized."""
        path = "/api/v1/biomes/<int:biome_id>"
        normalized = normalize_path(path)
        assert "<int:biome_id>" not in normalized
        assert "<param>" in normalized

    def test_normalize_uuid_param(self) -> None:
        """<uuid:id> is normalized."""
        path = "/api/v1/clusters/<uuid:cluster_id>"
        normalized = normalize_path(path)
        assert "<uuid:cluster_id>" not in normalized
        assert "<param>" in normalized

    def test_normalize_multiple_params(self) -> None:
        """Multiple params are all normalized."""
        path = "/api/v1/<int:org>/biomes/<int:biome_id>"
        normalized = normalize_path(path)
        assert normalized.count("<param>") == 2


class TestLookupRequiredScopes:
    """Test scope policy lookup."""

    def test_lookup_exact_match(self) -> None:
        """Exact path match returns scopes."""
        scopes = lookup_required_scopes("GET", "/api/v1/biomes")
        assert scopes == frozenset({"gough.biomes.read"})

    def test_lookup_template_match_int_param(self) -> None:
        """Template path matches with concrete int parameter."""
        # /api/v1/biomes/<int:biome_id> should match /api/v1/biomes/123
        scopes = lookup_required_scopes("GET", "/api/v1/biomes/123")
        assert scopes == frozenset({"gough.biomes.read"})

    def test_lookup_template_match_group_id(self) -> None:
        """Template path matches group endpoints."""
        scopes = lookup_required_scopes("GET", "/api/v1/biomes/groups/456")
        assert scopes == frozenset({"gough.biomes.read"})

    def test_lookup_unknown_returns_none(self) -> None:
        """Unknown endpoint returns None."""
        scopes = lookup_required_scopes("GET", "/api/v1/unknown/endpoint")
        assert scopes is None

    def test_lookup_wrong_method_returns_none(self) -> None:
        """Wrong HTTP method returns None."""
        scopes = lookup_required_scopes("PATCH", "/api/v1/biomes")
        assert scopes is None

    def test_lookup_post_eggs_list(self) -> None:
        """POST to biomes list requires author scope."""
        scopes = lookup_required_scopes("POST", "/api/v1/biomes")
        assert scopes == frozenset({"gough.biomes.author"})

    def test_lookup_delete_egg(self) -> None:
        """DELETE requires admin scope."""
        scopes = lookup_required_scopes("DELETE", "/api/v1/biomes/999")
        assert scopes == frozenset({"gough.cluster.admin"})


class TestRequiresScopesDecorator:
    """Test @require_scopes decorator."""

    @pytest.mark.asyncio
    async def test_require_scopes_pass(self) -> None:
        """Decorator passes when scopes are sufficient."""
        from unittest.mock import AsyncMock
        from quart import Quart, g

        app = Quart(__name__)
        async with app.test_request_context('/'):
            handler = AsyncMock(return_value="success")
            decorated = require_scopes("gough.biomes.read")(handler)
            g.current_user = {
                "id": 1,
                "email": "test@example.com",
                "_jwt_payload": {"scope": "gough.biomes.read gough.biomes.author"},
            }
            result = await decorated()
            assert result == "success"
            handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_require_scopes_fail_insufficient(self) -> None:
        """Decorator returns 403 when scopes insufficient."""
        from unittest.mock import AsyncMock
        from quart import Quart, g

        app = Quart(__name__)
        async with app.test_request_context('/'):
            handler = AsyncMock()
            decorated = require_scopes("gough.biomes.author", "gough.cluster.admin")(
                handler
            )
            g.current_user = {
                "id": 1,
                "email": "test@example.com",
                "_jwt_payload": {"scope": "gough.biomes.read"},  # Missing author and admin
            }
            response = await decorated()
            assert isinstance(response, tuple)
            assert response[1] == 403
            response_json = await response[0].json
            assert "Insufficient scopes" in response_json["error"]

    @pytest.mark.asyncio
    async def test_require_scopes_no_user(self) -> None:
        """Decorator returns 401 when no user authenticated."""
        from unittest.mock import AsyncMock
        from quart import Quart, g

        app = Quart(__name__)
        async with app.test_request_context('/'):
            handler = AsyncMock()
            decorated = require_scopes("gough.biomes.read")(handler)
            # g.current_user not set
            try:
                delattr(g, "current_user")
            except AttributeError:
                pass
            response = await decorated()
            assert isinstance(response, tuple)
            assert response[1] == 401


class TestScopeEnforcementIntegration:
    """Integration tests for scope enforcement."""

    def test_scope_policy_well_formed(self) -> None:
        """SCOPE_POLICY is well-formed (no duplicate keys)."""
        seen = set()
        for key in SCOPE_POLICY.keys():
            assert key not in seen, f"Duplicate policy key: {key}"
            seen.add(key)

    def test_eggs_endpoints_all_have_scopes(self) -> None:
        """Biomes endpoints have scopes defined."""
        eggs_get = lookup_required_scopes("GET", "/api/v1/biomes")
        eggs_post = lookup_required_scopes("POST", "/api/v1/biomes")
        eggs_get_one = lookup_required_scopes("GET", "/api/v1/biomes/123")

        assert eggs_get is not None
        assert eggs_post is not None
        assert eggs_get_one is not None

    def test_unknown_path_missing_from_policy_returns_none(self) -> None:
        """Unknown path (not in SCOPE_POLICY) returns None."""
        result = lookup_required_scopes("GET", "/api/v1/totally-unknown-endpoint-xyz")
        assert result is None

    def test_scope_case_sensitive(self) -> None:
        """Scope matching is case-sensitive."""
        provided = frozenset({"Gough.Biomes.Read"})
        required = frozenset({"gough.biomes.read"})
        with pytest.raises(InsufficientScopeError):
            check_scopes(provided, required)

    def test_extract_array_with_mixed_types_filters_non_strings(self) -> None:
        """Array with non-strings filters them out."""
        payload = {"scope": ["a", 123, "b", None, "c"]}
        scopes = extract_scopes_from_jwt(payload)
        assert scopes == frozenset({"a", "b", "c"})
