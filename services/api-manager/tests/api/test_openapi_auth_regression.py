"""Regression tests: the OpenAPI spec endpoints must require authentication.

regression: audit openapi-anon 2026-09-22

``/api/v1/openapi.json`` and ``/api/v1/openapi.yaml`` serve the full 176-route
API surface -- a reconnaissance map for an attacker -- and were previously
listed in ``ANONYMOUS_PATHS``, bypassing the ASGI ``OIDCAuthMiddleware`` gate
entirely. These drive the REAL production app (``real_auth_env``, see
``tests/api/conftest.py``) through the genuine gate -> tenant bridge -> scope
enforcement path, exactly like the gh-31 auth suite.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class TestOpenAPISpecRequiresAuth:
    async def test_openapi_json_unauthenticated_returns_401(self, real_auth_env):
        """No bearer token -> 401 at the ASGI gate, never reaches the handler."""
        resp = await real_auth_env.client.get("/api/v1/openapi.json")
        assert resp.status_code == 401

    async def test_openapi_yaml_unauthenticated_returns_401(self, real_auth_env):
        """No bearer token -> 401 at the ASGI gate, never reaches the handler."""
        resp = await real_auth_env.client.get("/api/v1/openapi.yaml")
        assert resp.status_code == 401

    async def test_openapi_json_garbage_token_returns_401(self, real_auth_env):
        """A garbage bearer token -> 401, same as any other protected route."""
        resp = await real_auth_env.client.get(
            "/api/v1/openapi.json",
            headers={"Authorization": "Bearer garbage.token.here"},
        )
        assert resp.status_code == 401

    async def test_openapi_json_authenticated_returns_200(self, real_auth_env):
        """A REAL, validly-signed token -> 200 with the actual spec body."""
        token = real_auth_env.mint(roles=["admin"])
        resp = await real_auth_env.client.get(
            "/api/v1/openapi.json", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["openapi"] == "3.1.0"

    async def test_openapi_yaml_authenticated_returns_200(self, real_auth_env):
        """A REAL, validly-signed token -> 200 with the YAML spec body."""
        token = real_auth_env.mint(roles=["admin"])
        resp = await real_auth_env.client.get(
            "/api/v1/openapi.yaml", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200

    async def test_openapi_json_no_specific_scope_required(self, real_auth_env):
        """Any authenticated principal may read the spec -- no dedicated scope.

        SCOPE_POLICY registers the path with an empty required-scope set, so a
        viewer-scoped (least-privileged) token must succeed too, not just admin.
        """
        token = real_auth_env.mint(roles=["viewer"], sub=real_auth_env.viewer_id)
        resp = await real_auth_env.client.get(
            "/api/v1/openapi.json", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200

    async def test_health_endpoint_stays_anonymous(self, real_auth_env):
        """/health remains anonymous -- this fix must not touch health checks."""
        resp = await real_auth_env.client.get("/health", follow_redirects=True)
        assert resp.status_code != 401
