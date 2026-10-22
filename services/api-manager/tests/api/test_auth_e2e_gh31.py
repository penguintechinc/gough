"""End-to-end auth regression suite driving the REAL penguin-aaa ASGI stack.

regression: gh-31

Every "valid" case here uses a REAL ES256 token minted with the booted app's own
keystore key (via the ``real_auth_env`` fixture) and driven through the genuine
``OIDCAuthMiddleware`` gate + tenant bridge + fail-closed scope enforcement.
Nothing is injected into ``g`` or ``request.scope["state"]["claims"]`` -- the only
thing that authenticates a request is a token that survives the real gate, so a
"valid" case is always distinguishable from the missing/garbage cases.

These lock the gh-31 migration (HS256 hand-rolled -> penguin-aaa ES256/OIDC) and
the hardenings that shipped with it:
  * a login token works end-to-end (login was minting tokens its own gate rejected)
  * ``_get_user_roles`` no longer uses pyDAL ``.ALL`` (Finding B, would 500)
  * id tokens are rejected as access tokens (token_use gate)
  * a deactivated user's still-valid token is rejected (fail-closed shim)
  * non-OIDC endpoints (enrollment key / bootstrap token / public JWKS) stay
    reachable past the OIDC gate and enforce their OWN auth in the handler
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


# Edge (ASGI-gate) rejection messages -- distinct from a handler's own 401, so a
# test can prove a request reached the handler rather than being stopped at the gate.
_EDGE_MESSAGES = {"Missing or invalid Bearer token", "Token verification failed"}


def _reached_handler(body: Any) -> bool:
    """True if the JSON body is NOT one of the ASGI gate's own rejections."""
    if not isinstance(body, dict):
        return True
    err = body.get("error")
    return not (isinstance(err, str) and err in _EDGE_MESSAGES)


# ==============================================================================
# Login -> usable token end-to-end (the core gh-31 bug)
# ==============================================================================


class TestLoginProducesUsableToken:
    async def test_login_returns_token_set(self, real_auth_env):
        """POST /login with seeded creds -> 200 with access + refresh + id tokens."""
        resp = await real_auth_env.client.post(
            "/api/v1/auth/login",
            json={"email": "operator@gough.test", "password": "real-pass-123"},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body.get("access_token")
        assert body.get("refresh_token")
        assert body.get("id_token")

    async def test_login_token_reaches_protected_nodes(self, real_auth_env):
        """The login access token authenticates GET /api/v1/nodes -> 200 (not 401)."""
        login = await real_auth_env.client.post(
            "/api/v1/auth/login",
            json={"email": "operator@gough.test", "password": "real-pass-123"},
        )
        token = (await login.get_json())["access_token"]
        resp = await real_auth_env.client.get(
            "/api/v1/nodes",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        assert resp.status_code != 401
        assert resp.status_code == 200
        body = await resp.get_json()
        # A real 200 success envelope ({"status":"success","data":{"nodes":[...]}})
        # from the nodes handler -- proves the token authenticated end-to-end,
        # not a gate rejection body.
        assert isinstance(body, dict) and body.get("status") == "success"
        nodes = body["data"]["nodes"]
        assert isinstance(nodes, list)
        assert any(n.get("tenant_id") == "__default__" for n in nodes)

    async def test_login_token_on_auth_me(self, real_auth_env):
        """The login access token authenticates GET /api/v1/auth/me -> 200."""
        login = await real_auth_env.client.post(
            "/api/v1/auth/login",
            json={"email": "operator@gough.test", "password": "real-pass-123"},
        )
        token = (await login.get_json())["access_token"]
        resp = await real_auth_env.client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200

    async def test_storage_require_auth_route_not_500(self, real_auth_env):
        """A storage @require_auth route does not 500 (gh-31 Finding B: no .ALL).

        The role load (``_get_user_roles`` split-query) must not raise. The
        endpoint itself is absent from SCOPE_POLICY, so the fail-closed scope
        layer returns 403 before the handler -- the point is that it is NOT a
        500 from a crashing role query. ``/auth/me`` -> 200 (above) additionally
        proves the same split-query path succeeds when it IS reached.
        """
        token = real_auth_env.mint(roles=["admin"])
        resp = await real_auth_env.client.get(
            "/api/v1/storage/configs",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        assert resp.status_code != 500
        assert resp.status_code == 403  # fail-closed: not in SCOPE_POLICY


# ==============================================================================
# Rejection paths (all must fail closed) -- real tokens vs missing/garbage
# ==============================================================================


class TestRejectionPaths:
    async def test_no_token_returns_401(self, real_auth_env):
        resp = await real_auth_env.client.get("/api/v1/nodes", follow_redirects=True)
        assert resp.status_code == 401

    async def test_garbage_token_returns_401(self, real_auth_env):
        resp = await real_auth_env.client.get(
            "/api/v1/nodes",
            headers={"Authorization": "Bearer garbage.token.here"},
            follow_redirects=True,
        )
        assert resp.status_code == 401

    async def test_viewer_scope_on_admin_route_returns_403(self, real_auth_env):
        """A viewer-scoped REAL token on an admin route -> 403 (scope enforced)."""
        token = real_auth_env.mint(roles=["viewer"], sub=real_auth_env.viewer_id)
        resp = await real_auth_env.client.post(
            "/api/v1/webhooks",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        assert resp.status_code == 403

    async def test_viewer_scope_on_write_route_returns_403(self, real_auth_env):
        """A viewer-scoped REAL token on a write route (DELETE node) -> 403."""
        token = real_auth_env.mint(roles=["viewer"], sub=real_auth_env.viewer_id)
        resp = await real_auth_env.client.delete(
            "/api/v1/nodes/1", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403

    async def test_missing_tenant_claim_returns_401(self, real_auth_env):
        """A REAL token with no tenant claim -> 401 at the verifier.

        gh-31: ``StaticKeyVerifier`` requires ``tenant`` (jwt.decode ``require`` +
        strict ``Claims``), so a tenant-less token is rejected at the gate before
        the tenant bridge's 403 path is reachable. 401 is the real, fail-closed
        behaviour.
        """
        token = real_auth_env.mint(roles=["admin"], tenant=None)
        resp = await real_auth_env.client.get(
            "/api/v1/nodes",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        assert resp.status_code == 401

    async def test_id_token_used_as_access_returns_401(self, real_auth_env):
        """A REAL id token presented as a bearer access token -> 401 (token_use)."""
        token = real_auth_env.mint_id_token(roles=["admin"])
        resp = await real_auth_env.client.get(
            "/api/v1/nodes",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        assert resp.status_code == 401

    async def test_deactivated_user_valid_token_returns_401(self, real_auth_env):
        """A cryptographically-valid token for a DEACTIVATED user -> 401.

        The token signature/iss/aud/exp/tenant all verify; the fail-closed
        principal shim rejects it because the user row is inactive.
        """
        token = real_auth_env.mint(roles=["admin"], sub=real_auth_env.deactivated_id)
        resp = await real_auth_env.client.get(
            "/api/v1/nodes",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        assert resp.status_code == 401


# ==============================================================================
# Non-OIDC endpoints: reachable past the gate, enforce their OWN auth
# ==============================================================================


class TestNonOidcEndpointsEnforceOwnAuth:
    async def test_agents_enroll_no_key_rejected_by_handler(self, real_auth_env):
        """POST /agents/enroll with no X-Enrollment-Key -> 401 FROM THE HANDLER."""
        resp = await real_auth_env.client.post("/api/v1/agents/enroll", json={})
        assert resp.status_code == 401
        body = await resp.get_json()
        assert _reached_handler(body), "must reach the handler, not the OIDC gate"
        assert body.get("error") == "Enrollment key required"

    async def test_nodes_discover_bad_bootstrap_rejected_by_handler(self, real_auth_env):
        """POST /nodes/discover with a bad bootstrap token -> 401 from the handler."""
        resp = await real_auth_env.client.post(
            "/api/v1/nodes/discover", json={"bootstrap_token": "bogus"}
        )
        assert resp.status_code == 401
        body = await resp.get_json()
        assert _reached_handler(body), "must reach the handler, not the OIDC gate"

    async def test_webhooks_public_jwks_reachable(self, real_auth_env):
        """GET /webhooks/keys/<tenant> is a public JWKS -> 200, no token."""
        resp = await real_auth_env.client.get("/api/v1/webhooks/keys/__default__")
        assert resp.status_code == 200

    async def test_status_public(self, real_auth_env):
        """GET /api/v1/status is public -> 200, no token."""
        resp = await real_auth_env.client.get("/api/v1/status")
        assert resp.status_code == 200


# ==============================================================================
# Refresh-token rotation (valid -> 200; reused/revoked -> 401)
# ==============================================================================


class TestRefreshRotation:
    async def _login(self, env) -> dict[str, Any]:
        resp = await env.client.post(
            "/api/v1/auth/login",
            json={"email": "operator@gough.test", "password": "real-pass-123"},
        )
        assert resp.status_code == 200
        return await resp.get_json()

    async def test_valid_refresh_returns_new_token(self, real_auth_env):
        """POST /refresh with a valid stored refresh token -> 200 + new access token."""
        body = await self._login(real_auth_env)
        resp = await real_auth_env.client.post(
            "/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}
        )
        assert resp.status_code == 200
        rotated = await resp.get_json()
        assert rotated.get("access_token")
        assert rotated.get("refresh_token")

    async def test_reused_refresh_token_revoked_returns_401(self, real_auth_env):
        """A refresh token is single-use: the second use (now revoked) -> 401."""
        body = await self._login(real_auth_env)
        first = await real_auth_env.client.post(
            "/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}
        )
        assert first.status_code == 200
        reuse = await real_auth_env.client.post(
            "/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}
        )
        assert reuse.status_code == 401
