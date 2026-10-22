"""Scope-based enforcement layer for OIDC permission model.

This module implements the scope-enforcement decorator and middleware that
consumes the SCOPE_POLICY catalog from scope_policy.py. All endpoints must
declare required scopes in SCOPE_POLICY; missing endpoints are deny-by-default
(fail-closed) for security.

Scope validation:
- Extract 'scope' claim from JWT (supports space-separated string or JSON array)
- Check provided scopes contain ALL required scopes (AND logic)
- Never branch on role names; authorization decisions use scopes only
"""

from __future__ import annotations

import re
from functools import wraps
from typing import Any, Callable, cast

from quart import g, jsonify, request

from .scope_policy import SCOPE_POLICY, is_anonymous_request


class InsufficientScopeError(Exception):
    """Raised when token scopes do not satisfy endpoint requirements."""

    def __init__(
        self,
        required: frozenset[str],
        provided: frozenset[str],
    ) -> None:
        """Initialize InsufficientScopeError with required and provided scopes.

        Args:
            required: Set of scopes required for the endpoint
            provided: Set of scopes present in the token
        """
        self.required = required
        self.provided = provided
        missing = required - provided
        super().__init__(
            f"Insufficient scopes: required {required}, provided {provided}, "
            f"missing {missing}"
        )


def extract_scopes_from_jwt(payload: dict) -> frozenset[str]:
    """Extract and parse scope claim from JWT payload.

    Supports both OAuth2 RFC 8693 formats:
    - Space-separated string: "a b c"
    - JSON array: ["a", "b", "c"]

    Args:
        payload: Decoded JWT payload dict

    Returns:
        Frozenset of scopes; empty frozenset if 'scope' claim absent or empty
    """
    scope_claim = payload.get("scope", "")

    if not scope_claim:
        return frozenset()

    # Handle list/array format
    if isinstance(scope_claim, list):
        return frozenset(s.strip() for s in scope_claim if s and isinstance(s, str))

    # Handle string format (space-separated)
    if isinstance(scope_claim, str):
        return frozenset(s.strip() for s in scope_claim.split() if s)

    return frozenset()


def check_scopes(
    provided: frozenset[str],
    required: frozenset[str],
) -> None:
    """Validate that provided scopes contain all required scopes.

    Args:
        provided: Scopes present in token
        required: Scopes needed for endpoint

    Raises:
        InsufficientScopeError: If provided does not contain all required scopes
    """
    if not required.issubset(provided):
        raise InsufficientScopeError(required=required, provided=provided)


def normalize_path(path: str) -> str:
    """Normalize Flask URL pattern to comparable form.

    Converts Flask path params (e.g., <int:id>, <uuid:id>) to normalized
    placeholders for template matching.

    Args:
        path: Flask URL pattern

    Returns:
        Normalized path suitable for comparison
    """
    # Match <type:name> or <name> patterns
    return re.sub(r"<[^>]+>", "<param>", path)


def _policy_to_regex(policy_path: str) -> re.Pattern:
    """Convert a Flask-style policy path with <type:name> params to a regex.

    Args:
        policy_path: Flask URL pattern with <type:name> parameters

    Returns:
        Compiled regex pattern that matches concrete paths
    """
    parts = re.split(r'<[^>]+>', policy_path)
    pattern = r'[^/]+'.join(re.escape(p) for p in parts)
    return re.compile(r'^' + pattern + r'$')


def lookup_required_scopes(
    method: str,
    path: str,
) -> frozenset[str] | None:
    """Lookup required scopes for endpoint from SCOPE_POLICY.

    Path matching strategy:
    1. Exact match: /api/v1/biomes/123 vs /api/v1/biomes/123
    2. Template match: /api/v1/biomes/123 vs /api/v1/biomes/<int:biome_id>

    Args:
        method: HTTP method (GET, POST, etc.)
        path: Request path (e.g., /api/v1/biomes/123)

    Returns:
        Frozenset of required scopes, or None if endpoint not in SCOPE_POLICY
    """
    # Normalize a trailing slash: SCOPE_POLICY keys never carry one, but Quart's
    # strict-slashes routing canonicalises blueprint "/" routes WITH a trailing
    # slash (e.g. GET /api/v1/nodes -> 308 -> /api/v1/nodes/). Without this, the
    # canonical path fails the policy lookup and gets fail-closed 403'd.
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Exact match first
    if (method, path) in SCOPE_POLICY:
        return SCOPE_POLICY[(method, path)]

    for (policy_method, policy_path), scopes in SCOPE_POLICY.items():
        if policy_method != method:
            continue

        # Exact match on policy path (Flask pattern)
        if path == policy_path:
            return scopes

        # Template match: use regex if policy has params
        if '<' in policy_path:
            pattern = _policy_to_regex(policy_path)
            if pattern.match(path):
                return scopes

    # Not found in policy
    return None


def require_scopes(*required_scopes: str) -> Callable:
    """Decorator to enforce scope requirements on a handler.

    Composable with @auth_required: apply @auth_required first to populate
    g.current_user, then @require_scopes to validate.

    Usage:
        @auth_required
        @require_scopes("gough.biomes.read", "gough.cluster.read")
        async def get_biomes():
            ...

    Args:
        *required_scopes: Scopes required for handler

    Returns:
        Decorator function

    Raises:
        InsufficientScopeError: If token scopes insufficient
    """
    required_set = frozenset(required_scopes)

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def decorated(*args, **kwargs):
            user = g.get("current_user")

            if not user:
                return jsonify({"error": "Authentication required"}), 401

            # Extract scopes from user payload (set by auth_required)
            payload = user.get("_jwt_payload", {})
            provided_scopes = extract_scopes_from_jwt(payload)

            try:
                check_scopes(provided_scopes, required_set)
            except InsufficientScopeError as e:
                return jsonify({
                    "error": "Insufficient scopes",
                    "required": sorted(e.required),
                    "provided": sorted(e.provided),
                }), 403

            result = f(*args, **kwargs)
            if hasattr(result, "__await__"):
                return await result
            return result

        return decorated

    return decorator


async def scope_enforcement_middleware(app) -> None:
    """Register before_request handler for scope enforcement.

    Runs after auth_required (if applied) to validate token scopes against
    SCOPE_POLICY. Fail-closed: unknown endpoints are DENIED (403).

    Applies to:
    - All authenticated endpoints (non-ANONYMOUS_PATHS)
    - Endpoints registered in SCOPE_POLICY

    Returns:
        None (registers handler on app.before_request)
    """

    @app.before_request
    async def _enforce_scopes():
        method = request.method
        path = request.path

        # Skip anonymous paths (no auth required; method + template aware).
        if is_anonymous_request(method, path):
            return

        # Lookup scopes in policy
        required_scopes = lookup_required_scopes(method, path)

        if required_scopes is None:
            # Endpoint not in policy: fail-closed (deny by default)
            app.logger.warning(
                f"Endpoint not in scope policy: {method} {path}, denying"
            )
            return jsonify({
                "error": "Endpoint not registered in scope policy",
                "status": "forbidden_scope",
            }), 403

        # Scope source: the penguin-aaa claims populated by OIDCAuthMiddleware
        # (request.scope["state"]["claims"]) are authoritative. Fall back to the
        # g.current_user shim's _jwt_payload for any path that reached here
        # without ASGI-validated claims.
        scope = cast("dict[str, Any]", request.scope)
        claims = (scope.get("state") or {}).get("claims")
        if claims is not None:
            payload = claims
        else:
            user = g.get("current_user")
            if not user:
                # No validated principal on a protected endpoint -> deny.
                return jsonify({"error": "Authentication required"}), 401
            payload = user.get("_jwt_payload", {})

        provided_scopes = extract_scopes_from_jwt(payload)

        try:
            check_scopes(provided_scopes, required_scopes)
        except InsufficientScopeError as e:
            return jsonify({
                "error": "Insufficient scopes",
                "status": "forbidden_scope",
                "required": sorted(e.required),
                "provided": sorted(e.provided),
            }), 403
