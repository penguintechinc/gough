"""Tests for app/middleware.py authentication and authorization middleware."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from quart import Quart, g, jsonify


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app() -> Quart:
    app = Quart(__name__)
    app.config["JWT_SECRET_KEY"] = "test-secret-key-for-middleware-tests-32"
    app.config["TESTING"] = True
    return app


def _make_token(
    app: Quart,
    *,
    sub: str = "1",
    token_type: str = "access",
    scope: str = "gough.cluster.admin",
    extra: dict | None = None,
) -> str:
    payload: dict = {
        "sub": sub,
        "type": token_type,
        "scope": scope,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    if extra:
        payload.update(extra)
    return pyjwt.encode(payload, app.config["JWT_SECRET_KEY"], algorithm="HS256")


def _make_expired_token(app: Quart) -> str:
    payload = {
        "sub": "1",
        "type": "access",
        "scope": "gough.cluster.admin",
        "iat": int(time.time()) - 7200,
        "exp": int(time.time()) - 3600,
    }
    return pyjwt.encode(payload, app.config["JWT_SECRET_KEY"], algorithm="HS256")


_FAKE_USER = {
    "id": 1,
    "email": "test@example.com",
    "role": "admin",
    "is_active": True,
}

_FAKE_VIEWER = {
    "id": 2,
    "email": "viewer@example.com",
    "role": "viewer",
    "is_active": True,
}


# ---------------------------------------------------------------------------
# NOTE (regression: gh-31): the HS256 ``decode_token`` helper and the
# ``safe_import_tenant_middleware`` / ``safe_import_scope_enforcement`` defensive
# shims were deleted in the penguin-aaa ES256 migration. Bearer validation now
# happens in the ASGI ``OIDCAuthMiddleware`` (StaticKeyVerifier), and its
# accept/reject behaviour for valid / garbage / expired / wrong-signature tokens
# is exercised end-to-end in tests/api/test_auth_e2e_gh31.py against the real
# gate. The former unit tests for those deleted symbols are removed here rather
# than kept as dead assertions.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------

class TestGetCurrentUser:
    @pytest.mark.anyio
    async def test_returns_none_when_not_set(self):
        app = _make_app()
        async with app.test_request_context("/"):
            from app.middleware import get_current_user
            assert get_current_user() is None

    @pytest.mark.anyio
    async def test_returns_user_when_set(self):
        app = _make_app()
        async with app.test_request_context("/"):
            from app.middleware import get_current_user
            g.current_user = _FAKE_USER
            assert get_current_user() == _FAKE_USER


# ---------------------------------------------------------------------------
# user_has_role
# ---------------------------------------------------------------------------

class TestUserHasRole:
    @pytest.mark.anyio
    async def test_returns_false_when_no_user(self):
        app = _make_app()
        async with app.test_request_context("/"):
            from app.middleware import user_has_role
            assert user_has_role("admin") is False

    @pytest.mark.anyio
    async def test_returns_true_for_matching_role(self):
        app = _make_app()
        async with app.test_request_context("/"):
            from app.middleware import user_has_role
            g.current_user = _FAKE_USER
            assert user_has_role("admin") is True

    @pytest.mark.anyio
    async def test_returns_false_for_wrong_role(self):
        app = _make_app()
        async with app.test_request_context("/"):
            from app.middleware import user_has_role
            g.current_user = _FAKE_USER
            assert user_has_role("viewer") is False


# ---------------------------------------------------------------------------
# get_token_from_header
# ---------------------------------------------------------------------------

class TestGetTokenFromHeader:
    @pytest.mark.anyio
    async def test_bearer_token_extracted(self):
        app = _make_app()
        async with app.test_request_context(
            "/", headers={"Authorization": "Bearer mytoken"}
        ):
            from app.middleware import get_token_from_header
            assert get_token_from_header() == "mytoken"

    @pytest.mark.anyio
    async def test_missing_header_returns_none(self):
        app = _make_app()
        async with app.test_request_context("/"):
            from app.middleware import get_token_from_header
            assert get_token_from_header() is None

    @pytest.mark.anyio
    async def test_non_bearer_header_returns_none(self):
        app = _make_app()
        async with app.test_request_context(
            "/", headers={"Authorization": "Basic abc123"}
        ):
            from app.middleware import get_token_from_header
            assert get_token_from_header() is None


# ---------------------------------------------------------------------------
# auth_required
# ---------------------------------------------------------------------------

class TestAuthRequired:
    """``auth_required`` now only asserts a principal is present (regression: gh-31).

    Bearer validation + principal population moved to the ASGI gate + the
    ``_populate_current_user`` before_request shim; the decorator no longer
    decodes tokens or loads users. So these tests set (or omit) ``g.current_user``
    directly to exercise the decorator's remaining contract. The real
    token->principal path is covered end-to-end in test_auth_e2e_gh31.py.
    """

    @pytest.mark.anyio
    async def test_missing_principal_returns_401(self):
        app = _make_app()
        from app.middleware import auth_required

        @auth_required
        async def protected():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            response, status = await protected()
            assert status == 401

    @pytest.mark.anyio
    async def test_present_principal_calls_handler(self):
        app = _make_app()
        from app.middleware import auth_required

        @auth_required
        async def protected():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            g.current_user = _FAKE_USER
            response, status = await protected()
            assert status == 200


# ---------------------------------------------------------------------------
# admin_required
# ---------------------------------------------------------------------------

class TestAdminRequired:
    @pytest.mark.anyio
    async def test_admin_user_passes(self):
        app = _make_app()
        admin_user = {
            **_FAKE_USER,
            "role": "admin",
            "_jwt_payload": {"scope": "gough.cluster.admin"},
        }

        from app.middleware import admin_required

        @admin_required
        async def protected():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            g.current_user = admin_user
            response, status = await protected()
            assert status == 200

    @pytest.mark.anyio
    async def test_no_user_returns_401(self):
        app = _make_app()
        from app.middleware import admin_required

        @admin_required
        async def protected():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            response, status = await protected()
            assert status == 401

    @pytest.mark.anyio
    async def test_non_admin_role_returns_403(self):
        app = _make_app()
        viewer_user = {**_FAKE_VIEWER, "_jwt_payload": {"scope": ""}}

        from app.middleware import admin_required

        @admin_required
        async def protected():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            g.current_user = viewer_user
            response, status = await protected()
            assert status == 403


# ---------------------------------------------------------------------------
# roles_required
# ---------------------------------------------------------------------------

class TestRolesRequired:
    @pytest.mark.anyio
    async def test_matching_role_passes_via_scope(self):
        app = _make_app()
        admin_user = {
            **_FAKE_USER,
            "role": "admin",
            "_jwt_payload": {"scope": "gough.cluster.admin"},
        }

        from app.middleware import roles_required

        @roles_required("admin")
        async def protected():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            g.current_user = admin_user
            response, status = await protected()
            assert status == 200

    @pytest.mark.anyio
    async def test_non_matching_role_returns_403(self):
        app = _make_app()
        viewer_user = {**_FAKE_VIEWER, "_jwt_payload": {"scope": ""}}

        from app.middleware import roles_required

        @roles_required("admin")
        async def protected():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            g.current_user = viewer_user
            response, status = await protected()
            assert status == 403


# ---------------------------------------------------------------------------
# roles_accepted
# ---------------------------------------------------------------------------

class TestRolesAccepted:
    @pytest.mark.anyio
    async def test_one_of_multiple_roles_passes(self):
        app = _make_app()
        maintainer_user = {
            "id": 3,
            "email": "m@example.com",
            "role": "maintainer",
            "is_active": True,
            "_jwt_payload": {"scope": "gough.cluster.read"},
        }

        from app.middleware import roles_accepted

        @roles_accepted("admin", "maintainer")
        async def protected():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            g.current_user = maintainer_user
            response, status = await protected()
            assert status == 200


# ---------------------------------------------------------------------------
# maintainer_or_admin_required
# ---------------------------------------------------------------------------

class TestMaintainerOrAdminRequired:
    @pytest.mark.anyio
    async def test_maintainer_passes_via_legacy_role(self):
        app = _make_app()
        maintainer_user = {
            "id": 3,
            "email": "m@example.com",
            "role": "maintainer",
            "is_active": True,
            "_jwt_payload": {"scope": "gough.cluster.read"},
        }

        from app.middleware import maintainer_or_admin_required

        @maintainer_or_admin_required
        async def protected():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            g.current_user = maintainer_user
            response, status = await protected()
            assert status == 200


# ---------------------------------------------------------------------------
# NOTE (regression: gh-31): ``safe_import_tenant_middleware`` /
# ``safe_import_scope_enforcement`` were defensive shims for the Wave-1 rollout
# and were deleted once middleware wiring became a hard dependency. Their tests
# are removed rather than kept against non-existent symbols.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _ROLE_TO_SCOPE_BUNDLE constant
# ---------------------------------------------------------------------------

class TestRoleToScopeBundle:
    def test_admin_bundle_exists(self):
        from app.middleware import _ROLE_TO_SCOPE_BUNDLE
        assert "admin" in _ROLE_TO_SCOPE_BUNDLE
        assert "gough.cluster.admin" in _ROLE_TO_SCOPE_BUNDLE["admin"]

    def test_maintainer_bundle_exists(self):
        from app.middleware import _ROLE_TO_SCOPE_BUNDLE
        assert "maintainer" in _ROLE_TO_SCOPE_BUNDLE

    def test_viewer_bundle_exists(self):
        from app.middleware import _ROLE_TO_SCOPE_BUNDLE
        assert "viewer" in _ROLE_TO_SCOPE_BUNDLE
