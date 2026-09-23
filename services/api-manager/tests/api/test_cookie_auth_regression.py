"""Regression tests: browser cookie auth + CSRF double-submit.

regression: security audit 2026-09-22 (HIGH)

The web UI previously stored JWTs in localStorage, which is readable by any
script that achieves XSS on the page (exfiltratable). This suite locks the
cookie-based auth contract added to close that gap, driven through the REAL
penguin-aaa ASGI stack via ``real_auth_env`` (see tests/api/conftest.py) --
nothing is injected into ``g``/``request.scope``; the only thing that
authenticates a request is either a genuinely-signed bearer token or a cookie
set by a real login response.

Cookie contract (for the frontend agent):
    gough_access  -- HttpOnly, Secure*, SameSite=Lax, Path=/
    gough_refresh -- HttpOnly, Secure*, SameSite=Lax, Path=/api/v1/auth
    gough_csrf    -- Secure*, SameSite=Lax, Path=/ (NOT HttpOnly -- JS reads
                      this and echoes it back as the ``X-CSRF-Token`` header)
    * Secure is OFF under DEBUG/TESTING (gated on app config, never request
      data) so local dev/test over plain http can read the cookie back.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

_LOGIN_CREDS = {"email": "operator@gough.test", "password": "real-pass-123"}
_EXPECTED_COOKIE_NAMES = {"gough_access", "gough_refresh", "gough_csrf"}


def _parse_set_cookie_headers(resp: Any) -> dict[str, SimpleCookie]:
    """Parse every ``Set-Cookie`` header on a response into name -> morsel."""
    morsels: dict[str, SimpleCookie] = {}
    for raw in resp.headers.getlist("set-cookie"):
        jar: SimpleCookie = SimpleCookie()
        jar.load(raw)
        morsels.update(jar)
    return morsels


async def _login(env) -> Any:
    resp = await env.client.post("/api/v1/auth/login", json=_LOGIN_CREDS)
    assert resp.status_code == 200
    return resp


def _csrf_cookie_value(env) -> str:
    """Read gough_csrf straight out of the test client's cookie jar."""
    for cookie in env.client.cookie_jar:
        if cookie.name == "gough_csrf":
            return cookie.value
    raise AssertionError("gough_csrf cookie was not set in the client jar")


# ==============================================================================
# Login sets all three cookies with the correct flags
# ==============================================================================


class TestLoginSetsCookies:
    async def test_login_sets_all_three_cookies_with_correct_flags(self, real_auth_env):
        resp = await _login(real_auth_env)
        cookies = _parse_set_cookie_headers(resp)
        assert set(cookies) == _EXPECTED_COOKIE_NAMES

        access = cookies["gough_access"]
        assert access["httponly"]
        assert access["samesite"].lower() == "lax"
        assert access["path"] == "/"

        refresh = cookies["gough_refresh"]
        assert refresh["httponly"]
        assert refresh["samesite"].lower() == "lax"
        assert refresh["path"] == "/api/v1/auth"

        csrf = cookies["gough_csrf"]
        assert not csrf["httponly"], "gough_csrf must be JS-readable for double-submit CSRF"
        assert csrf["samesite"].lower() == "lax"
        assert csrf["path"] == "/"

        # Tokens still returned in the JSON body for CLI/service clients.
        body = await resp.get_json()
        assert body.get("access_token")
        assert body.get("refresh_token")

    def test_cookies_are_secure_gated_on_app_config_not_request(self) -> None:
        """Secure flag is DEBUG/TESTING-conditional, never derived from the request."""
        from app.middleware import cookies_are_secure

        assert cookies_are_secure({"DEBUG": False, "TESTING": True}) is False
        assert cookies_are_secure({"DEBUG": True, "TESTING": False}) is False
        assert cookies_are_secure({"DEBUG": False, "TESTING": False}) is True


# ==============================================================================
# Cookie -> Bearer shim: a cookie-only request authenticates
# ==============================================================================


class TestCookieOnlyAuthentication:
    async def test_cookie_only_get_succeeds_via_shim(self, real_auth_env):
        """No Authorization header at all -- only the cookie jar from login."""
        await _login(real_auth_env)
        resp = await real_auth_env.client.get("/api/v1/nodes", follow_redirects=True)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body.get("status") == "success"

    async def test_no_cookie_no_header_still_401(self, real_auth_env):
        """Sanity: the shim doesn't manufacture auth out of nothing."""
        resp = await real_auth_env.client.get("/api/v1/nodes", follow_redirects=True)
        assert resp.status_code == 401


# ==============================================================================
# CSRF double-submit on cookie-authenticated, state-changing requests
# ==============================================================================


class TestCsrfDoubleSubmit:
    async def test_cookie_auth_post_without_csrf_header_returns_403(self, real_auth_env):
        await _login(real_auth_env)
        resp = await real_auth_env.client.post("/api/v1/webhooks", json={})
        assert resp.status_code == 403
        body = await resp.get_json()
        assert body.get("error") == "csrf_failed"

    async def test_cookie_auth_post_with_mismatched_csrf_header_returns_403(self, real_auth_env):
        await _login(real_auth_env)
        resp = await real_auth_env.client.post(
            "/api/v1/webhooks", json={}, headers={"X-CSRF-Token": "wrong-value"}
        )
        assert resp.status_code == 403
        body = await resp.get_json()
        assert body.get("error") == "csrf_failed"

    async def test_cookie_auth_post_with_matching_csrf_header_not_csrf_blocked(self, real_auth_env):
        await _login(real_auth_env)
        csrf_token = _csrf_cookie_value(real_auth_env)
        resp = await real_auth_env.client.post(
            "/api/v1/webhooks", json={}, headers={"X-CSRF-Token": csrf_token}
        )
        body = await resp.get_json()
        assert not (resp.status_code == 403 and body.get("error") == "csrf_failed")

    async def test_bearer_header_post_needs_no_csrf(self, real_auth_env):
        """A request with a real Authorization header is exempt from CSRF (no cookie jar used)."""
        token = real_auth_env.mint(roles=["admin"])
        resp = await real_auth_env.client.post(
            "/api/v1/webhooks",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        body = await resp.get_json()
        assert not (resp.status_code == 403 and body.get("error") == "csrf_failed")

    async def test_cookie_auth_get_needs_no_csrf(self, real_auth_env):
        """CSRF only applies to state-changing methods; GET is exempt."""
        await _login(real_auth_env)
        resp = await real_auth_env.client.get("/api/v1/nodes", follow_redirects=True)
        assert resp.status_code == 200


# ==============================================================================
# Logout clears all three cookies
# ==============================================================================


class TestLogoutClearsCookies:
    async def test_logout_clears_all_three_cookies(self, real_auth_env):
        login = await _login(real_auth_env)
        token = (await login.get_json())["access_token"]
        resp = await real_auth_env.client.post(
            "/api/v1/auth/logout",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

        cleared = _parse_set_cookie_headers(resp)
        assert set(cleared) == _EXPECTED_COOKIE_NAMES
        for name, morsel in cleared.items():
            assert morsel["max-age"] == "0", f"{name} was not cleared (max-age != 0)"


# ==============================================================================
# Refresh reads the refresh token from the cookie when the body omits it
# ==============================================================================


class TestRefreshFromCookie:
    async def test_refresh_with_cookie_only_no_body_token_succeeds(self, real_auth_env):
        login = await _login(real_auth_env)
        login_body = await login.get_json()
        assert login_body["refresh_token"]

        # No refresh_token in the body -- only the gough_refresh cookie set by
        # the login above (Path=/api/v1/auth, so it IS sent to this route).
        resp = await real_auth_env.client.post("/api/v1/auth/refresh", json={})
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body.get("access_token")
        assert body.get("refresh_token")

    async def test_refresh_no_body_no_cookie_returns_400(self, real_auth_env):
        resp = await real_auth_env.client.post("/api/v1/auth/refresh", json=None)
        assert resp.status_code == 400
