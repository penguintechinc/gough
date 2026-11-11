"""Enforcement tests for the reviewed scope-policy decisions.

Runs the REAL ``scope_enforcement_middleware`` (the fail-closed
``before_request`` gate) in front of stub handlers mounted at the exact
policy paths, so each test exercises the policy table plus the middleware
exactly as production evaluates them -- independent of handler internals.

Regression: security review of 385dd0f (tenant ``admin`` = ``gough.cluster.admin``
could reach global user/secret stores and CA operations).
"""

from __future__ import annotations

import re
from typing import Any, cast

import pytest
from quart import Quart, request

from app.security.scope_enforcement import scope_enforcement_middleware
from app.security.scope_policy import (
    INTENTIONALLY_UNMAPPED,
    KNOWN_SCOPES,
    SCOPE_POLICY,
)

SUPERADMIN = "gough.cluster.superadmin"

#: Every non-superadmin, non-dangerous scope -- the broadest token a tenant
#: admin can legitimately hold.
TENANT_ADMIN_SCOPES = sorted(
    KNOWN_SCOPES
    - {
        SUPERADMIN,
        "gough.dr.promote",
        "gough.biomes.unsafe-skip-signing",
        "gough.migration.override-lock",
    }
)

SUPERADMIN_ONLY: list[tuple[str, str]] = [
    ("GET", "/api/v1/users"),
    ("POST", "/api/v1/users"),
    ("GET", "/api/v1/users/roles"),
    ("GET", "/api/v1/users/<int:user_id>"),
    ("PUT", "/api/v1/users/<int:user_id>"),
    ("DELETE", "/api/v1/users/<int:user_id>"),
    ("GET", "/api/v1/secrets"),
    ("GET", "/api/v1/secrets/backends"),
    ("POST", "/api/v1/secrets/backends"),
    ("GET", "/api/v1/secrets/<path:secret_path>"),
    ("POST", "/api/v1/secrets/<path:secret_path>"),
    ("PUT", "/api/v1/secrets/<path:secret_path>"),
    ("DELETE", "/api/v1/secrets/<path:secret_path>"),
    ("POST", "/api/v1/ssh-ca/initialize"),
    ("POST", "/api/v1/primary/rotate-ca"),
    ("POST", "/api/v1/primary/frontend-switch"),
    ("POST", "/api/v1/vault/rotate-keys"),
]


def _concrete(path: str) -> str:
    """Substitute each ``<conv:name>`` placeholder with a concrete segment."""
    return re.sub(r"<[^>]+>", "1", path)


def _build_app(routes: list[tuple[str, str]]) -> Quart:
    """Stub app: claims injector -> real scope middleware -> 200 stub handlers."""
    app = Quart(__name__)

    @app.before_request
    async def _inject_claims() -> None:
        scopes = request.headers.get("X-Test-Scopes", "")
        asgi_scope = cast("dict[str, Any]", request.scope)
        asgi_scope.setdefault("state", {})["claims"] = {
            "sub": "u-1",
            "tenant": "acme",
            "scope": scopes,
        }

    async def _ok(**_kwargs: Any) -> tuple[dict[str, str], int]:
        return {"reached": "handler"}, 200

    for i, (method, path) in enumerate(routes):
        app.add_url_rule(path, endpoint=f"stub_{i}", view_func=_ok, methods=[method])
    return app


@pytest.fixture
async def gated_app() -> Quart:
    """App gating every superadmin-only and intentionally-unmapped route."""
    app = _build_app(SUPERADMIN_ONLY + sorted(INTENTIONALLY_UNMAPPED))
    await scope_enforcement_middleware(app)
    return app


async def _call(
    app: Quart, method: str, path: str, scopes: list[str]
) -> tuple[int, dict[str, Any]]:
    """Issue one request with the given scope claim and return (status, json)."""
    client = app.test_client()
    send = getattr(client, method.lower())  # get/post/put/patch/delete
    resp = await send(_concrete(path), headers={"X-Test-Scopes": " ".join(scopes)})
    body = await resp.get_json(silent=True) or {}
    return resp.status_code, body


class TestSuperadminOnly:
    """Global stores and CA/frontend operations are superadmin-only."""

    @pytest.mark.parametrize(("method", "path"), SUPERADMIN_ONLY)
    def test_policy_requires_exactly_superadmin(self, method: str, path: str) -> None:
        """The policy entry is exactly the superadmin scope (no admin alternative)."""
        assert SCOPE_POLICY[(method, path)] == frozenset({SUPERADMIN})

    @pytest.mark.parametrize(("method", "path"), SUPERADMIN_ONLY)
    async def test_tenant_admin_gets_403(
        self, gated_app: Quart, method: str, path: str
    ) -> None:
        """A tenant admin holding every non-superadmin scope is refused."""
        status, body = await _call(gated_app, method, path, TENANT_ADMIN_SCOPES)
        assert status == 403
        assert body.get("required") == [SUPERADMIN]
        assert body.get("reached") is None

    @pytest.mark.parametrize(("method", "path"), SUPERADMIN_ONLY)
    async def test_superadmin_passes(
        self, gated_app: Quart, method: str, path: str
    ) -> None:
        """The superadmin scope alone reaches the handler."""
        status, body = await _call(gated_app, method, path, [SUPERADMIN])
        assert status == 200
        assert body == {"reached": "handler"}


class TestIntentionallyUnmapped:
    """Routes held back pending a tenant/DTO fix stay fail-closed for everyone."""

    @pytest.mark.parametrize(("method", "path"), sorted(INTENTIONALLY_UNMAPPED))
    async def test_returns_403_even_with_every_scope(
        self, gated_app: Quart, method: str, path: str
    ) -> None:
        """Even a token carrying every known scope (incl. superadmin) is denied."""
        status, body = await _call(gated_app, method, path, sorted(KNOWN_SCOPES))
        assert status == 403
        assert body.get("error") == "Endpoint not registered in scope policy"

    async def test_static_segment_not_shadowed_by_template(
        self, gated_app: Quart
    ) -> None:
        """GET /biomes/deployments must not inherit GET /biomes/<int:biome_id>'s scope.

        Regression: the middleware regex-matched concrete paths, so this
        unmapped route was reachable with gough.biomes.read via the template.
        """
        assert ("GET", "/api/v1/biomes/<int:biome_id>") in SCOPE_POLICY
        status, _ = await _call(
            gated_app, "GET", "/api/v1/biomes/deployments", ["gough.biomes.read"]
        )
        assert status == 403

    def test_review_findings_are_held_back(self) -> None:
        """The specific routes the review flagged are in the unmapped allowlist."""
        required = {
            ("POST", "/api/v1/storage/objects/<path:object_key>/presigned-url"),
            ("DELETE", "/api/v1/storage/objects/<path:object_key>"),
            ("GET", "/api/v1/storage/quotas"),
            ("GET", "/api/v1/teams/"),
            ("GET", "/api/v1/clouds/"),
            ("GET", "/api/v1/ipxe/machines/<string:machine_id>"),
        }
        assert required <= set(INTENTIONALLY_UNMAPPED)


class TestDeadEntriesRemoved:
    """Entries pointing at non-existent routes were deleted, not left dormant."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/disks/<int:node_id>/disks",
            "/api/v1/joiner-secrets/clusters/<uuid:cluster_id>/joiner-secrets",
        ],
    )
    def test_dead_prefixes_absent(self, path: str) -> None:
        """No policy entry survives under the dead /disks or /joiner-secrets prefixes."""
        prefix = path.split("<", 1)[0]
        assert not [k for k in SCOPE_POLICY if k[1].startswith(prefix)]

    def test_hello_protected_is_read_scoped(self) -> None:
        """The example protected route needs only the cluster read scope."""
        assert SCOPE_POLICY[("GET", "/api/v1/hello/protected")] == frozenset(
            {"gough.cluster.read"}
        )
