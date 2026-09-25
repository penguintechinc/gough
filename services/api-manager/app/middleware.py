"""Authentication and Authorization Middleware.

Authentication itself is now performed by penguin-aaa's ASGI
``OIDCAuthMiddleware`` (wired in ``app.create_app``): it validates the bearer
token against gough's own ES256 signing key and populates
``request.scope["state"]["claims"]`` on success, or returns 401 before Quart
routing runs. This module wires the three ``before_request`` layers that run
*after* that ASGI gate, in the order required by the Gough security model:

  1. Tenant bridge               — reads the ``tenant`` claim from the
                                    ASGI-validated claims, stores
                                    ``g.tenant_context``, and pushes the
                                    Postgres ``app.current_tenant`` GUC (RLS).
                                    Missing tenant claim -> 403.
  2. Principal shim              — populates ``g.current_user`` (with a
                                    ``_jwt_payload``-shaped dict) from the
                                    claims + a DB user load, so existing
                                    ``@auth_required`` / ``get_current_user()``
                                    handlers keep working unchanged.
  3. Scope enforcement middleware — ``app.security.scope_enforcement
                                    .scope_enforcement_middleware`` checks
                                    ``SCOPE_POLICY`` (fail-closed).

The legacy ``auth_required`` / ``admin_required`` /
``maintainer_or_admin_required`` decorators are preserved for source
compatibility with existing routes; ``role_required`` is now implemented as
a thin shim over the scope-enforcement primitives so existing routes keep
working without rewrites.
"""

import hmac
from collections.abc import Awaitable
from functools import wraps
from http.cookies import CookieError, SimpleCookie
from typing import Any, Callable, Optional, cast

from quart import g, jsonify, request


# ==============================================================================
# Browser cookie auth (regression: security audit 2026-09-22 -- HIGH)
# ==============================================================================
#
# The web UI previously stored JWTs in localStorage, which is readable by any
# script that achieves XSS on the page (exfiltratable). These three cookie
# names are the full browser-auth contract shared with the frontend:
#
#   gough_access  -- HttpOnly, Path=/                 -- promoted to a Bearer
#                                                         header by
#                                                         CookieAuthShimMiddleware
#   gough_refresh -- HttpOnly, Path=/api/v1/auth       -- read by POST /refresh
#   gough_csrf    -- NOT HttpOnly, Path=/              -- JS reads this and
#                                                         echoes it back as
#                                                         X-CSRF-Token (double
#                                                         submit CSRF defense)
#
# All three are set by app.auth.login/refresh and cleared by app.auth.logout.

COOKIE_ACCESS_NAME = "gough_access"
COOKIE_REFRESH_NAME = "gough_refresh"
COOKIE_CSRF_NAME = "gough_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

ASGIApp = Callable[..., Awaitable[None]]


def cookies_are_secure(app_config: Any) -> bool:
    """Whether auth cookies should carry the ``Secure`` flag.

    Gated on app config (``DEBUG``/``TESTING``) ONLY, never on request-derived
    data (Host header, X-Forwarded-Proto) a client could spoof. Dev/test runs
    over plain http and needs ``Secure`` off so the browser/test client will
    actually store and resend the cookie; every other environment forces it on.
    """
    return not (app_config.get("DEBUG") or app_config.get("TESTING"))


def _extract_cookie(cookie_header: bytes, name: str) -> str | None:
    """Parse a raw ASGI ``cookie`` header for a single cookie value by name."""
    if not cookie_header:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(cookie_header.decode("latin-1"))
    except CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


class CookieAuthShimMiddleware:
    """ASGI middleware: promote a ``gough_access`` cookie to a Bearer header.

    Wired in ``app.create_app`` so it runs BEFORE penguin-aaa's
    ``OIDCAuthMiddleware`` in the ASGI stack -- when a request carries no
    ``Authorization`` header but does carry a ``gough_access`` cookie, this
    injects ``Authorization: Bearer <cookie>`` into the ASGI scope headers
    before the OIDC gate ever sees the request. This means cookie auth and
    header auth validate through the EXACT SAME path (the real
    ``StaticKeyVerifier``), with zero changes to penguin-aaa itself.

    A request already carrying an ``Authorization`` header is left completely
    untouched (CLI/service clients keep working unmodified). When the
    promotion happens, ``scope["state"]["auth_via_cookie"] = True`` is set so
    the CSRF bridge (see ``install_security_middleware`` below) knows to
    demand a matching ``X-CSRF-Token`` on state-changing requests -- a request
    that already had an ``Authorization`` header is exempt from that check
    because browsers never attach that header automatically, so it carries no
    CSRF risk.

    The ``scope`` dict is mutated in place (never replaced) so the mutation is
    visible to every other layer sharing the same ``scope`` object, including
    ``AuditMiddleware``, which is wired OUTSIDE this shim and reads
    ``scope["state"]`` after the request completes.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        has_auth_header = any(k.lower() == b"authorization" for k, _v in headers)
        if not has_auth_header:
            cookie_header = next(
                (v for k, v in headers if k.lower() == b"cookie"), b""
            )
            access_token = _extract_cookie(cookie_header, COOKIE_ACCESS_NAME)
            if access_token:
                scope["headers"] = [
                    *headers,
                    (b"authorization", f"Bearer {access_token}".encode("latin-1")),
                ]
                scope.setdefault("state", {})["auth_via_cookie"] = True

        await self._app(scope, receive, send)


# ==============================================================================
# Role → scope mapping (for the legacy role_required shim)
# ==============================================================================
#
# The legacy decorators express intent in role-name terms, but the spec
# requires authorization to be evaluated only on scopes. We map each role to
# the broadest known-scope bundle that role implies, then delegate to
# ``check_scopes``. Tokens that already carry adequate scopes (issued by a
# modern auth service) succeed without ever consulting the role claim.

_ROLE_TO_SCOPE_BUNDLE: dict[str, frozenset[str]] = {
    "admin": frozenset({"gough.cluster.admin"}),
    "maintainer": frozenset({"gough.cluster.read"}),
    "viewer": frozenset({"gough.cluster.read"}),
}


def get_token_from_header() -> Optional[str]:
    """Extract JWT token from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def get_current_user() -> Optional[dict]:
    """Get current authenticated user from request context."""
    return getattr(g, "current_user", None)


def user_has_role(role: str) -> bool:
    """Check if current user has a specific role.

    Args:
        role: The role name to check for

    Returns:
        True if user has the role, False otherwise
    """
    user = get_current_user()
    if not user:
        return False
    return user.get("role") == role


def auth_required(f: Callable) -> Callable:
    """Decorator to require authentication.

    Usage: @auth_required (without parentheses)

    Authentication is performed upstream by the ASGI ``OIDCAuthMiddleware``
    (bearer validation) and the ``_populate_current_user`` shim (which sets
    ``g.current_user`` from the validated claims). This decorator now only
    asserts a principal is present; a request that reached a protected handler
    without one is rejected 401.
    """
    @wraps(f)
    async def decorated(*args, **kwargs):
        if get_current_user() is None:
            return jsonify({"error": "Authentication required"}), 401

        result = f(*args, **kwargs)
        if hasattr(result, '__await__'):
            return await result
        return result

    return decorated


def role_required(*allowed_roles: str) -> Callable:
    """Decorator to require ANY of the listed roles.

    Backwards-compatible shim that delegates to
    ``app.security.scope_enforcement.check_scopes``. A request is allowed if
    *either*:

    1. The token's scope claim already satisfies the scope bundle for any of
       ``allowed_roles`` (preferred — pure scope-based authz, per spec), OR
    2. The legacy ``user.role`` claim is one of ``allowed_roles`` (fallback
       so existing tokens issued before scope rollout keep working).

    Pure role-name comparison without scope evaluation is forbidden by the
    Gough security model; routes hardened post-Wave-1 should switch to the
    explicit ``@require_scopes(...)`` decorator instead.
    """
    # Lazy import to avoid a circular import with security/scope_enforcement.
    from .security.scope_enforcement import (
        check_scopes,
        extract_scopes_from_jwt,
        InsufficientScopeError,
    )

    allowed = tuple(allowed_roles)

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def decorated(*args, **kwargs):
            user = get_current_user()

            if not user:
                return jsonify({"error": "Authentication required"}), 401

            payload = user.get("_jwt_payload", {})
            provided_scopes = extract_scopes_from_jwt(payload)

            # 1. Scope-based path: any role's bundle satisfied?
            for role in allowed:
                bundle = _ROLE_TO_SCOPE_BUNDLE.get(role, frozenset())
                if not bundle:
                    continue
                try:
                    check_scopes(provided_scopes, bundle)
                except InsufficientScopeError:
                    continue
                else:
                    break
            else:
                # 2. Fallback: legacy role-name comparison.
                if user.get("role", "") not in allowed:
                    return (
                        jsonify(
                            {
                                "error": "Insufficient permissions",
                                "status": "forbidden_scope",
                                "required_roles": list(allowed),
                                "your_role": user.get("role", ""),
                            }
                        ),
                        403,
                    )

            result = f(*args, **kwargs)
            if hasattr(result, "__await__"):
                return await result
            return result

        return decorated

    return decorator


def admin_required(f: Callable) -> Callable:
    """Decorator to require admin role."""
    return role_required("admin")(f)


def maintainer_or_admin_required(f: Callable) -> Callable:
    """Decorator to require maintainer or admin role."""
    return role_required("admin", "maintainer")(f)


def roles_required(*required_roles: str) -> Callable:
    """Decorator to require ALL specified roles."""
    return role_required(*required_roles)


def roles_accepted(*accepted_roles: str) -> Callable:
    """Decorator to require ANY of the specified roles."""
    return role_required(*accepted_roles)


# ==============================================================================
# Wiring helpers — install tenant + scope enforcement on the Quart app
# ==============================================================================


async def install_security_middleware(app) -> None:
    """Install the tenant bridge + principal shim + scope enforcement layers.

    Call this from the application factory exactly once, after all blueprints
    are registered. Authentication (bearer validation) happens upstream in the
    ASGI ``OIDCAuthMiddleware`` wired in ``create_app``; these ``before_request``
    handlers run after it and consume ``request.scope["state"]["claims"]``.

    The handlers run in registration order:

    Step 0.  CSRF bridge     — double-submit CSRF check (regression: security
             audit 2026-09-22) for state-changing requests that authenticated
             via the ``gough_access`` cookie (``CookieAuthShimMiddleware`` set
             ``scope["state"]["auth_via_cookie"]``). Requires ``X-CSRF-Token``
             to equal the ``gough_csrf`` cookie; mismatch/absent -> 403. A
             request authenticated via a real ``Authorization`` header is
             exempt (no CSRF risk — browsers never attach that header
             automatically). Runs first so a forged request never reaches the
             tenant bridge or a DB call.

    Step 1.  Tenant bridge   — reads the ``tenant`` claim, stores
             ``g.tenant_context``, and pushes the Postgres ``app.current_tenant``
             GUC (RLS). Missing tenant claim on a protected route -> 403.

    Step 2.  Principal shim  — populates ``g.current_user`` (with a
             ``_jwt_payload``-shaped dict) from the claims + a DB user load, so
             the legacy ``@auth_required`` / ``get_current_user()`` /
             ``@require_scopes`` handlers keep working unchanged.

    Step 3.  Scope enforcement — ``scope_enforcement_middleware`` consults
             ``SCOPE_POLICY`` (fail-closed for protected endpoints). If it fails
             to initialize, the service MUST refuse to start.
    """
    from app.db.rls import CROSS_TENANT_SENTINEL, set_current_tenant
    from app.security.scope_enforcement import scope_enforcement_middleware
    from app.security.scope_policy import is_anonymous_request
    from app.security.tenant import (
        TenantClaimMissingError,
        extract_tenant_from_jwt,
    )

    from .db.run_db import run_db
    from .models import get_user_by_id

    def _claims() -> Optional[dict]:
        """Return the ASGI-validated claims dict for this request, if any."""
        # request.scope is an ASGI-scope TypedDict; "state" is populated at
        # runtime by OIDCAuthMiddleware, so read it via a plain-dict view.
        scope = cast("dict[str, Any]", request.scope)
        return (scope.get("state") or {}).get("claims")

    # Step 0: CSRF double-submit check (cookie-authenticated requests only).
    @app.before_request
    async def _csrf_bridge():
        if request.method not in _STATE_CHANGING_METHODS:
            return None
        if is_anonymous_request(request.method, request.path):
            return None

        scope = cast("dict[str, Any]", request.scope)
        state = scope.get("state") or {}
        if not state.get("auth_via_cookie"):
            # Authenticated via a real Authorization header (or not
            # authenticated at all, in which case the ASGI OIDC gate already
            # rejected it before Quart routing ran) -- no CSRF exposure.
            return None

        csrf_cookie = request.cookies.get(COOKIE_CSRF_NAME)
        csrf_header = request.headers.get(CSRF_HEADER_NAME)
        if (
            not csrf_cookie
            or not csrf_header
            or not hmac.compare_digest(csrf_cookie, csrf_header)
        ):
            return jsonify({"error": "csrf_failed"}), 403
        return None

    # Step 1: tenant bridge (RLS GUC).
    @app.before_request
    async def _tenant_bridge():
        if is_anonymous_request(request.method, request.path):
            return None

        claims = _claims()
        if not claims:
            # OIDCAuthMiddleware should already have rejected a tokenless
            # protected request; if we somehow got here, let the scope layer 401.
            return None

        try:
            tenant_context = extract_tenant_from_jwt(claims)
        except TenantClaimMissingError:
            return jsonify({"error": "tenant_claim_missing"}), 403

        g.tenant_context = tenant_context
        # Super-admin (cross_tenant) tokens push the cross-tenant sentinel so the
        # generic RLS policy matches every row; everyone else scopes to their own
        # tenant. See app.db.rls for the full mechanism.
        guc_tenant = (
            CROSS_TENANT_SENTINEL
            if tenant_context.cross_tenant
            else tenant_context.tenant_id
        )
        set_current_tenant(guc_tenant)
        return None

    # FIX #7a: clear the RLS tenant ContextVar at the end of every request
    # (success or exception), so a tenant set by one request can never leak onto
    # a later one that skipped tenant extraction.
    @app.teardown_request
    async def _tenant_teardown_request(exc: BaseException | None = None) -> None:  # noqa: ARG001
        set_current_tenant(None)

    # Step 2: principal shim — g.current_user from claims + DB user load.
    @app.before_request
    async def _populate_current_user():
        if is_anonymous_request(request.method, request.path):
            return None
        if getattr(g, "current_user", None) is not None:
            return None

        claims = _claims()
        if not claims:
            return None  # Let scope layer 401.

        sub = claims.get("sub")
        try:
            user_id = int(sub)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            # Valid-signature token with a malformed subject -> fail closed.
            return jsonify({"error": "Invalid token subject"}), 401

        # get_user_by_id is a blocking penguin-dal unit; run it off the loop.
        user = await run_db(lambda: get_user_by_id(user_id))
        if not user or not user.get("is_active"):
            # Fail closed: a cryptographically valid token whose user has been
            # deleted or deactivated must be rejected here -- scope enforcement
            # trusts the token's own scope claim and would otherwise let it pass.
            return jsonify({"error": "User inactive or not found"}), 401

        roles = list(claims.get("roles") or [])
        g.current_user = {
            "id": user["id"],
            "email": user.get("email", ""),
            "full_name": user.get("full_name", ""),
            "roles": roles,
            "role": roles[0] if roles else user.get("role", "viewer"),
            "is_active": user.get("is_active", True),
            # Shape a legacy HS256-style payload from the ES256 claims so
            # handlers reading g.current_user["_jwt_payload"] keep working.
            "_jwt_payload": {
                "sub": str(sub),
                "scope": list(claims.get("scope") or []),
                "tenant": claims.get("tenant"),
                "roles": roles,
                "cross_tenant": bool(claims.get("cross_tenant", False)),
                "iat": claims.get("iat"),
                "exp": claims.get("exp"),
            },
        }
        return None

    # Step 3: scope enforcement (FAIL-CLOSED). Re-raise on failure; serving
    # without scope enforcement is a critical security failure.
    await scope_enforcement_middleware(app)


# Convenience alias used by app/__init__.py so the factory can simply call
# ``await wire_middleware(app)`` after registering all blueprints.
wire_middleware = install_security_middleware


async def _record_request_metrics(response):
    """Record per-request latency and error metrics."""
    from .metrics import api_request_latency_seconds, api_error_total
    from quart import request
    try:
        method = request.method
        # Normalize path: replace numeric segments with {id}
        import re
        endpoint = re.sub(r"/\d+", "/{id}", request.path)
        status = str(response.status_code)
        # latency — use X-Request-Start header if set by load balancer, else 0
        elapsed = float(response.headers.get("X-Response-Time-Seconds", 0))
        api_request_latency_seconds.labels(method=method, endpoint=endpoint, status_code=status).observe(elapsed)
        if response.status_code >= 400:
            api_error_total.labels(method=method, endpoint=endpoint, status_code=status).inc()
    except Exception:
        pass
    return response
