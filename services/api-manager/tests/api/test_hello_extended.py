"""Test suite for app/hello.py endpoint."""

import pytest
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock


@pytest.fixture
def hello_app(monkeypatch, app):
    """Setup hello API blueprint with auth stubbed."""
    import importlib
    import app.middleware as mw_mod

    def _passthrough(*dargs, **dkwargs):
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]
        return lambda fn: fn

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(mw_mod, "maintainer_or_admin_required", _passthrough)

    import app.hello as hello_mod
    hello_mod = importlib.reload(hello_mod)

    app.register_blueprint(hello_mod.hello_bp)

    @app.before_request
    async def _inject_user():
        from quart import g
        g.current_user = {
            "id": 1,
            "email": "admin@test.local",
            "role": "admin",
            "full_name": "Admin User",
        }

    return app


@pytest.mark.asyncio
async def test_hello_endpoint_success(hello_app):
    """Test GET /hello returns greeting with user info."""
    client = hello_app.test_client()
    response = await client.get("/hello")
    assert response.status_code == 200
    data = await response.get_json()
    assert "message" in data
    assert "Hello" in data["message"]
    assert "Admin User" in data["message"]
    assert data["user"]["id"] == 1
    assert data["user"]["email"] == "admin@test.local"
    assert data["user"]["role"] == "admin"


@pytest.mark.asyncio
async def test_hello_endpoint_uses_full_name(hello_app):
    """Test /hello uses full_name if available."""
    client = hello_app.test_client()

    @hello_app.before_request
    async def _set_full_name():
        from quart import g
        g.current_user = {
            "id": 1,
            "email": "admin@test.local",
            "role": "admin",
            "full_name": "Mr. Administrator",
        }

    response = await client.get("/hello")
    data = await response.get_json()
    assert "Mr. Administrator" in data["message"]


@pytest.mark.asyncio
async def test_hello_endpoint_fallback_to_email(hello_app):
    """Test /hello falls back to email when full_name missing."""
    client = hello_app.test_client()

    @hello_app.before_request
    async def _no_full_name():
        from quart import g
        g.current_user = {
            "id": 1,
            "email": "user@test.local",
            "role": "viewer",
            # No full_name
        }

    response = await client.get("/hello")
    data = await response.get_json()
    assert "user@test.local" in data["message"]


@pytest.mark.asyncio
async def test_hello_endpoint_includes_timestamp(hello_app):
    """Test /hello response includes timestamp."""
    client = hello_app.test_client()
    response = await client.get("/hello")
    data = await response.get_json()
    assert "timestamp" in data
    # Verify it's ISO format
    assert "T" in data["timestamp"]


@pytest.mark.asyncio
async def test_hello_protected_admin_access(hello_app):
    """Test GET /hello/protected grants access to admin."""
    client = hello_app.test_client()

    @hello_app.before_request
    async def _set_admin():
        from quart import g
        g.current_user = {
            "id": 1,
            "email": "admin@test.local",
            "role": "admin",
            "full_name": "Admin User",
        }

    response = await client.get("/hello/protected")
    assert response.status_code == 200
    data = await response.get_json()
    assert "elevated access" in data["message"]
    assert data["access_level"] == "maintainer_or_admin"
    assert data["your_role"] == "admin"


@pytest.mark.asyncio
async def test_hello_protected_maintainer_access(hello_app):
    """Test GET /hello/protected grants access to maintainer."""
    client = hello_app.test_client()

    @hello_app.before_request
    async def _set_maintainer():
        from quart import g
        g.current_user = {
            "id": 2,
            "email": "maint@test.local",
            "role": "maintainer",
            "full_name": "Maintainer User",
        }

    response = await client.get("/hello/protected")
    assert response.status_code == 200
    data = await response.get_json()
    assert "elevated access" in data["message"]
    assert data["your_role"] == "maintainer"


@pytest.mark.asyncio
async def test_status_endpoint_public(hello_app):
    """Test GET /status is publicly accessible."""
    client = hello_app.test_client()
    response = await client.get("/status")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "running"
    assert data["service"] == "flask-backend"
    assert data["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_status_endpoint_includes_timestamp(hello_app):
    """Test /status response includes timestamp."""
    client = hello_app.test_client()
    response = await client.get("/status")
    data = await response.get_json()
    assert "timestamp" in data
    assert "T" in data["timestamp"]


@pytest.mark.asyncio
async def test_hello_user_info_structure(hello_app):
    """Test /hello returns properly structured user object."""
    client = hello_app.test_client()
    response = await client.get("/hello")
    data = await response.get_json()
    user = data["user"]
    assert "id" in user
    assert "email" in user
    assert "role" in user
    assert isinstance(user["id"], int)
    assert isinstance(user["email"], str)
    assert isinstance(user["role"], str)
