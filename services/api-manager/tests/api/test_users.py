"""Tests for app.users blueprint endpoints (real module, mocked models)."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from quart import Quart, g

pytestmark = pytest.mark.asyncio


def _passthrough(*dargs, **dkwargs):
    """Stub for auth/admin decorators — returns the function unchanged."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


_USER_1 = {
    "id": 1,
    "email": "admin@test.com",
    "role": "admin",
    "is_active": True,
    "full_name": "Admin User",
    "password_hash": "hashed",
}
_USER_2 = {
    "id": 2,
    "email": "viewer@test.com",
    "role": "viewer",
    "is_active": True,
    "full_name": "Viewer User",
    "password_hash": "hashed",
}


@pytest.fixture()
def users_app(monkeypatch):
    """Quart test app with the REAL app.users blueprint, mocked model layer."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(mw_mod, "admin_required", _passthrough)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough)

    # Inject mock functions into app.models package (functions live in legacy
    # app/models.py but are shadowed by app/models/ package — inject before
    # reload so users_mod picks them up via `from .models import ...`).
    import app.models as models_mod

    def _list_users(page=1, per_page=20):
        return ([dict(_USER_1), dict(_USER_2)], 2)

    def _get_user_by_id(uid: int):
        if uid == 1:
            return dict(_USER_1)
        if uid == 2:
            return dict(_USER_2)
        return None

    def _get_user_by_email(email: str):
        return None

    def _create_user(email, password_hash, full_name="", role="viewer"):
        return {
            "id": 3,
            "email": email,
            "role": role,
            "is_active": True,
            "full_name": full_name,
            "password_hash": password_hash,
        }

    def _update_user(uid, **kw):
        return {
            "id": uid,
            "email": kw.get("email", _USER_2["email"]),
            "role": kw.get("role", _USER_2["role"]),
            "is_active": kw.get("is_active", _USER_2["is_active"]),
            "full_name": kw.get("full_name", _USER_2["full_name"]),
            "password_hash": kw.get("password_hash", _USER_2["password_hash"]),
        }

    def _delete_user(uid):
        return True

    # Add the missing functions to the models package so reload can import them
    monkeypatch.setattr(models_mod, "list_users", _list_users, raising=False)
    monkeypatch.setattr(models_mod, "get_user_by_id", _get_user_by_id)
    monkeypatch.setattr(models_mod, "get_user_by_email", _get_user_by_email, raising=False)
    monkeypatch.setattr(models_mod, "create_user", _create_user, raising=False)
    monkeypatch.setattr(models_mod, "update_user", _update_user, raising=False)
    monkeypatch.setattr(models_mod, "delete_user", _delete_user, raising=False)

    # Mock hash_password in app.auth
    import app.auth as auth_pkg

    monkeypatch.setattr(auth_pkg, "hash_password", lambda pw: "hashed_pw")

    # Mock get_current_user in middleware
    monkeypatch.setattr(mw_mod, "get_current_user", lambda: _USER_1)

    import app.users as users_mod

    users_mod = importlib.reload(users_mod)

    quart_app = Quart(__name__)
    quart_app.config["TESTING"] = True
    quart_app.url_map.strict_slashes = False
    quart_app.register_blueprint(users_mod.users_bp, url_prefix="/api/v1/users")

    @quart_app.before_request
    async def _inject():
        g.current_user = dict(_USER_1)
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return quart_app


# ---------------------------------------------------------------------------
# GET /api/v1/users
# ---------------------------------------------------------------------------


async def test_get_users_success(users_app):
    """List users returns paginated list."""
    client = users_app.test_client()
    response = await client.get("/api/v1/users?page=1&per_page=10")

    assert response.status_code == 200
    data = await response.get_json()
    assert "users" in data
    assert "pagination" in data
    assert len(data["users"]) == 2
    assert data["pagination"]["page"] == 1
    assert data["pagination"]["per_page"] == 10
    assert data["pagination"]["total"] == 2


async def test_get_users_default_pagination(users_app):
    """List users uses default pagination values."""
    client = users_app.test_client()
    response = await client.get("/api/v1/users")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["pagination"]["per_page"] == 20


async def test_get_users_no_password_hashes(users_app):
    """List users excludes password_hash from response."""
    client = users_app.test_client()
    response = await client.get("/api/v1/users")

    assert response.status_code == 200
    data = await response.get_json()
    for user in data["users"]:
        assert "password_hash" not in user


async def test_get_users_per_page_clamped_high(users_app):
    """per_page above 100 is clamped to 100."""
    client = users_app.test_client()
    response = await client.get("/api/v1/users?per_page=9999")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["pagination"]["per_page"] == 100


async def test_get_users_per_page_clamped_low(users_app):
    """per_page below 1 is clamped to 1."""
    client = users_app.test_client()
    response = await client.get("/api/v1/users?per_page=0")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["pagination"]["per_page"] == 1


# ---------------------------------------------------------------------------
# GET /api/v1/users/<id>
# ---------------------------------------------------------------------------


async def test_get_user_success(users_app):
    """Get single user by ID."""
    client = users_app.test_client()
    response = await client.get("/api/v1/users/1")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["id"] == 1
    assert data["email"] == "admin@test.com"
    assert "password_hash" not in data


async def test_get_user_not_found(users_app):
    """Get user returns 404 for non-existent ID."""
    client = users_app.test_client()
    response = await client.get("/api/v1/users/999")

    assert response.status_code == 404
    data = await response.get_json()
    assert "error" in data


# ---------------------------------------------------------------------------
# POST /api/v1/users
# ---------------------------------------------------------------------------


async def test_create_user_success(users_app):
    """Create user with valid data."""
    client = users_app.test_client()
    response = await client.post(
        "/api/v1/users",
        json={
            "email": "new@test.com",
            "password": "securepass123",
            "full_name": "New User",
            "role": "maintainer",
        },
    )

    assert response.status_code == 201
    data = await response.get_json()
    assert "user" in data
    assert data["user"]["email"] == "new@test.com"
    assert data["user"]["role"] == "maintainer"
    assert "password_hash" not in data["user"]


async def test_create_user_default_role_is_viewer(users_app):
    """Create user defaults to viewer role."""
    client = users_app.test_client()
    response = await client.post(
        "/api/v1/users",
        json={"email": "new@test.com", "password": "securepass123"},
    )

    assert response.status_code == 201
    data = await response.get_json()
    assert data["user"]["role"] == "viewer"


async def test_create_user_no_body(users_app):
    """Create user without body returns 400."""
    client = users_app.test_client()
    response = await client.post("/api/v1/users")

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


async def test_create_user_missing_email(users_app):
    """Create user without email returns 400."""
    client = users_app.test_client()
    response = await client.post(
        "/api/v1/users",
        json={"password": "securepass123"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


async def test_create_user_short_password(users_app):
    """Create user with password < 8 chars returns 400."""
    client = users_app.test_client()
    response = await client.post(
        "/api/v1/users",
        json={"email": "new@test.com", "password": "short"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


async def test_create_user_empty_password(users_app):
    """Create user with empty password returns 400."""
    client = users_app.test_client()
    response = await client.post(
        "/api/v1/users",
        json={"email": "new@test.com", "password": ""},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


async def test_create_user_invalid_role(users_app):
    """Create user with invalid role returns 400."""
    client = users_app.test_client()
    response = await client.post(
        "/api/v1/users",
        json={"email": "new@test.com", "password": "securepass123", "role": "superadmin"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


async def test_create_user_email_already_exists(users_app, monkeypatch):
    """Create user with duplicate email returns 409."""
    import app.users as users_mod

    monkeypatch.setattr(users_mod, "get_user_by_email", lambda email: dict(_USER_1))

    client = users_app.test_client()
    response = await client.post(
        "/api/v1/users",
        json={"email": "admin@test.com", "password": "securepass123"},
    )

    assert response.status_code == 409
    data = await response.get_json()
    assert "error" in data


# ---------------------------------------------------------------------------
# PUT /api/v1/users/<id>
# ---------------------------------------------------------------------------


async def test_update_user_success(users_app):
    """Update user full_name and role."""
    client = users_app.test_client()
    response = await client.put(
        "/api/v1/users/2",
        json={"full_name": "Updated Name", "role": "maintainer"},
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert "user" in data


async def test_update_user_password(users_app):
    """Update user password succeeds."""
    client = users_app.test_client()
    response = await client.put(
        "/api/v1/users/2",
        json={"password": "newpassword123"},
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert "user" in data


async def test_update_user_short_password(users_app):
    """Update user with too-short password returns 400."""
    client = users_app.test_client()
    response = await client.put(
        "/api/v1/users/2",
        json={"password": "short"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


async def test_update_user_email(users_app):
    """Update user email succeeds."""
    client = users_app.test_client()
    response = await client.put(
        "/api/v1/users/2",
        json={"email": "newemail@test.com"},
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert "user" in data


async def test_update_user_email_conflict(users_app, monkeypatch):
    """Update user email to an existing email returns 409."""
    import app.users as users_mod

    # New email conflicts with user 1
    monkeypatch.setattr(users_mod, "get_user_by_email", lambda email: dict(_USER_1))

    client = users_app.test_client()
    response = await client.put(
        "/api/v1/users/2",
        json={"email": "admin@test.com"},
    )

    assert response.status_code == 409
    data = await response.get_json()
    assert "error" in data


async def test_update_user_same_email_no_conflict(users_app):
    """Update user email to same value — no conflict, but email not in update_data → 400."""
    client = users_app.test_client()
    # users.py only adds email to update_data when it differs from current value;
    # submitting the unchanged email with no other fields → "No valid fields to update"
    response = await client.put(
        "/api/v1/users/2",
        json={"email": "viewer@test.com"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


async def test_update_user_is_active(users_app):
    """Update user active status."""
    client = users_app.test_client()
    response = await client.put(
        "/api/v1/users/2",
        json={"is_active": False},
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert "user" in data


async def test_update_user_invalid_role(users_app):
    """Update user with invalid role returns 400."""
    client = users_app.test_client()
    response = await client.put(
        "/api/v1/users/2",
        json={"role": "superadmin"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


async def test_update_user_not_found(users_app):
    """Update non-existent user returns 404."""
    client = users_app.test_client()
    response = await client.put(
        "/api/v1/users/999",
        json={"full_name": "Nobody"},
    )

    assert response.status_code == 404
    data = await response.get_json()
    assert "error" in data


async def test_update_user_no_valid_fields(users_app):
    """Update user with no known fields returns 400."""
    client = users_app.test_client()
    response = await client.put(
        "/api/v1/users/2",
        json={},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


async def test_update_user_no_body(users_app):
    """Update user without body returns 400."""
    client = users_app.test_client()
    response = await client.put("/api/v1/users/2")

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


# ---------------------------------------------------------------------------
# DELETE /api/v1/users/<id>
# ---------------------------------------------------------------------------


async def test_delete_user_success(users_app):
    """Delete user succeeds."""
    client = users_app.test_client()
    response = await client.delete("/api/v1/users/2")

    assert response.status_code == 200
    data = await response.get_json()
    assert "message" in data


async def test_delete_user_not_found(users_app):
    """Delete non-existent user returns 404."""
    client = users_app.test_client()
    response = await client.delete("/api/v1/users/999")

    assert response.status_code == 404
    data = await response.get_json()
    assert "error" in data


async def test_delete_user_self(users_app):
    """Delete own account returns 400."""
    client = users_app.test_client()
    response = await client.delete("/api/v1/users/1")

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


async def test_delete_user_model_failure(users_app, monkeypatch):
    """Delete user returns 500 when model returns False."""
    import app.users as users_mod

    monkeypatch.setattr(users_mod, "delete_user", lambda uid: False)

    client = users_app.test_client()
    response = await client.delete("/api/v1/users/2")

    assert response.status_code == 500
    data = await response.get_json()
    assert "error" in data


# ---------------------------------------------------------------------------
# GET /api/v1/users/roles
# ---------------------------------------------------------------------------


async def test_get_roles_success(users_app):
    """Get roles returns all valid roles with descriptions."""
    client = users_app.test_client()
    response = await client.get("/api/v1/users/roles")

    assert response.status_code == 200
    data = await response.get_json()
    assert "roles" in data
    assert "descriptions" in data
    assert "admin" in data["roles"]
    assert "maintainer" in data["roles"]
    assert "viewer" in data["roles"]
    assert "admin" in data["descriptions"]
    assert "maintainer" in data["descriptions"]
    assert "viewer" in data["descriptions"]
