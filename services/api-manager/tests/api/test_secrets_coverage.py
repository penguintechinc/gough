"""Coverage improvement tests for app.api.secrets blueprint.

Focuses on edge cases and error paths not fully covered by test_secrets_api.py.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


def _passthrough(*dargs, **dkwargs):
    """Decorator passthrough: supports both @decorator and @decorator() styles."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


@pytest.fixture()
def secrets_app(monkeypatch):
    """Create a Quart app with secrets blueprint registered."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough)

    import app.api.secrets as secrets_mod
    secrets_mod = importlib.reload(secrets_mod)

    from quart import Quart, g

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["SECRETS_BACKEND"] = "encrypted_db"
    app.url_map.strict_slashes = False
    app.register_blueprint(secrets_mod.secrets_bp)

    @app.before_request
    async def _inject_identity():
        g.current_user = {
            "id": 1,
            "email": "admin@test.com",
            "role": "admin"
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return app, secrets_mod


# ============================================================================
# List Backends Tests
# ============================================================================


@pytest.mark.asyncio
async def test_list_backends_success(secrets_app):
    """Should list available backends with configuration info."""
    app, _ = secrets_app

    client = app.test_client()
    response = await client.get("/backends")

    assert response.status_code == 200
    data = await response.get_json()
    assert "backends" in data
    assert "current_backend" in data
    assert isinstance(data["backends"], list)
    assert len(data["backends"]) > 0

    # Check structure of backend entries
    for backend in data["backends"]:
        assert "name" in backend
        assert "description" in backend
        assert "is_configured" in backend
        assert isinstance(backend["is_configured"], bool)


@pytest.mark.asyncio
async def test_list_backends_current_backend_shown(secrets_app):
    """Should show which backend is currently configured."""
    app, _ = secrets_app

    client = app.test_client()
    response = await client.get("/backends")

    assert response.status_code == 200
    data = await response.get_json()
    assert "current_backend" in data
    # encrypted_db is default from app.config
    assert data["current_backend"] == "encrypted_db"


@pytest.mark.asyncio
async def test_list_backends_has_required_configs(secrets_app):
    """Should include configuration requirements for each backend."""
    app, _ = secrets_app

    client = app.test_client()
    response = await client.get("/backends")

    assert response.status_code == 200
    data = await response.get_json()

    # Find specific backends and check their config_required
    backend_names = {b["name"]: b for b in data["backends"]}

    if "vault" in backend_names:
        assert "config_required" in backend_names["vault"]
        assert "VAULT_ADDR" in backend_names["vault"]["config_required"]

    if "aws" in backend_names:
        assert "config_required" in backend_names["aws"]
        assert "AWS_REGION" in backend_names["aws"]["config_required"]


# ============================================================================
# Configure Backend Tests
# ============================================================================


@pytest.mark.asyncio
async def test_configure_backend_no_body(secrets_app):
    """Should return 400 if no request body."""
    app, _ = secrets_app

    client = app.test_client()
    response = await client.post("/backends", json=None)

    assert response.status_code == 400
    data = await response.get_json()
    assert "Request body required" in data["error"]


@pytest.mark.asyncio
async def test_configure_backend_no_name(secrets_app):
    """Should return 400 if backend name missing."""
    app, _ = secrets_app

    client = app.test_client()
    response = await client.post("/backends", json={"config": {}})

    assert response.status_code == 400
    data = await response.get_json()
    assert "Backend name required" in data["error"]


@pytest.mark.asyncio
async def test_configure_backend_unknown_backend(secrets_app):
    """Should return 400 if backend is unknown."""
    app, _ = secrets_app

    client = app.test_client()
    response = await client.post(
        "/backends",
        json={"backend": "unknown_backend"}
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "Unknown backend" in data["error"]
    assert "available" in data


# ============================================================================
# Get Secret Tests - Basic
# ============================================================================


@pytest.mark.asyncio
async def test_get_secret_with_valid_path(secrets_app, monkeypatch):
    """Should retrieve secret when path is provided."""
    app, secrets_mod = secrets_app

    secret_data = {"key": "value"}
    mock_manager = AsyncMock()
    mock_manager.get_secret.return_value = secret_data

    monkeypatch.setattr(
        secrets_mod,
        "get_secrets_manager",
        AsyncMock(return_value=mock_manager)
    )

    client = app.test_client()
    response = await client.get("/test/secret")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["path"] == "test/secret"
    assert data["data"] == secret_data


@pytest.mark.asyncio
async def test_get_secret_not_found(secrets_app, monkeypatch):
    """Should return 404 if secret doesn't exist."""
    app, secrets_mod = secrets_app

    from app.secrets import SecretNotFoundError

    mock_manager = AsyncMock()
    mock_manager.get_secret.side_effect = SecretNotFoundError("Not found")

    monkeypatch.setattr(
        secrets_mod,
        "get_secrets_manager",
        AsyncMock(return_value=mock_manager)
    )

    client = app.test_client()
    response = await client.get("/cloud/aws/missing")

    assert response.status_code == 404
    data = await response.get_json()
    assert "Secret not found" in data["error"]


@pytest.mark.asyncio
async def test_get_secret_backend_error(secrets_app, monkeypatch):
    """Should return 500 if backend error."""
    app, secrets_mod = secrets_app

    from app.secrets import SecretsManagerError

    mock_manager = AsyncMock()
    mock_manager.get_secret.side_effect = SecretsManagerError("Backend error")

    monkeypatch.setattr(
        secrets_mod,
        "get_secrets_manager",
        AsyncMock(return_value=mock_manager)
    )

    client = app.test_client()
    response = await client.get("/cloud/aws/creds")

    assert response.status_code == 500
    data = await response.get_json()
    assert "error" in data


# ============================================================================
# Set Secret Tests - Validation
# ============================================================================


@pytest.mark.asyncio
async def test_set_secret_no_body(secrets_app):
    """Should return 400 if no request body."""
    app, _ = secrets_app

    client = app.test_client()
    response = await client.post("/cloud/aws/creds", json=None)

    assert response.status_code == 400
    data = await response.get_json()
    assert "Request body required" in data["error"]


@pytest.mark.asyncio
async def test_set_secret_no_data(secrets_app):
    """Should return 400 if data field missing."""
    app, _ = secrets_app

    client = app.test_client()
    response = await client.post(
        "/cloud/aws/creds",
        json={"other_field": "value"}
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "Secret data required" in data["error"]


@pytest.mark.asyncio
async def test_set_secret_data_not_dict(secrets_app):
    """Should return 400 if data is not a dictionary."""
    app, _ = secrets_app

    client = app.test_client()
    response = await client.post(
        "/cloud/aws/creds",
        json={"data": "not_a_dict"}
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "must be a dictionary" in data["error"]


@pytest.mark.asyncio
async def test_set_secret_with_dict_data(secrets_app, monkeypatch):
    """Should accept dict data for secret."""
    app, secrets_mod = secrets_app

    from app.secrets import SecretNotFoundError

    mock_manager = AsyncMock()
    mock_manager.get_secret.side_effect = SecretNotFoundError("Not found")
    mock_manager.set_secret.return_value = None

    monkeypatch.setattr(
        secrets_mod,
        "get_secrets_manager",
        AsyncMock(return_value=mock_manager)
    )

    client = app.test_client()
    response = await client.post(
        "/cloud/aws/secret",
        json={"data": {"username": "admin", "password": "secret"}}
    )

    # Should be 201 (created) or 200 (updated)
    assert response.status_code in (200, 201)


@pytest.mark.asyncio
async def test_set_secret_update_success(secrets_app, monkeypatch):
    """Should return 200 when updating existing secret."""
    app, secrets_mod = secrets_app

    existing_secret = {"old": "value"}

    mock_manager = AsyncMock()
    mock_manager.get_secret.return_value = existing_secret
    mock_manager.set_secret.return_value = None

    monkeypatch.setattr(
        secrets_mod,
        "get_secrets_manager",
        AsyncMock(return_value=mock_manager)
    )

    client = app.test_client()
    response = await client.post(
        "/cloud/aws/existing-secret",
        json={"data": {"key": "new_value"}}
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert "updated" in data["message"]


@pytest.mark.asyncio
async def test_set_secret_backend_error(secrets_app, monkeypatch):
    """Should return 500 if backend error during set."""
    app, secrets_mod = secrets_app

    from app.secrets import SecretsManagerError

    mock_manager = AsyncMock()
    mock_manager.get_secret.side_effect = SecretsManagerError("Not found")
    mock_manager.set_secret.side_effect = SecretsManagerError("Backend error")

    monkeypatch.setattr(
        secrets_mod,
        "get_secrets_manager",
        AsyncMock(return_value=mock_manager)
    )

    client = app.test_client()
    response = await client.post(
        "/cloud/aws/creds",
        json={"data": {"key": "value"}}
    )

    assert response.status_code == 500
    data = await response.get_json()
    assert "error" in data


# ============================================================================
# Delete Secret Tests
# ============================================================================


@pytest.mark.asyncio
async def test_delete_secret_not_found(secrets_app, monkeypatch):
    """Should return 404 if secret doesn't exist."""
    app, secrets_mod = secrets_app

    mock_manager = AsyncMock()
    mock_manager.delete_secret.return_value = False

    monkeypatch.setattr(
        secrets_mod,
        "get_secrets_manager",
        AsyncMock(return_value=mock_manager)
    )

    client = app.test_client()
    response = await client.delete("/cloud/aws/missing")

    assert response.status_code == 404
    data = await response.get_json()
    assert "Secret not found" in data["error"]


@pytest.mark.asyncio
async def test_delete_secret_success(secrets_app, monkeypatch):
    """Should delete secret successfully."""
    app, secrets_mod = secrets_app

    mock_manager = AsyncMock()
    mock_manager.delete_secret.return_value = True

    monkeypatch.setattr(
        secrets_mod,
        "get_secrets_manager",
        AsyncMock(return_value=mock_manager)
    )

    client = app.test_client()
    response = await client.delete("/cloud/aws/creds")

    assert response.status_code == 200
    data = await response.get_json()
    assert "deleted successfully" in data["message"]


@pytest.mark.asyncio
async def test_delete_secret_backend_error(secrets_app, monkeypatch):
    """Should return 500 if backend error during delete."""
    app, secrets_mod = secrets_app

    from app.secrets import SecretsManagerError

    mock_manager = AsyncMock()
    mock_manager.delete_secret.side_effect = SecretsManagerError("Backend error")

    monkeypatch.setattr(
        secrets_mod,
        "get_secrets_manager",
        AsyncMock(return_value=mock_manager)
    )

    client = app.test_client()
    response = await client.delete("/cloud/aws/creds")

    assert response.status_code == 500
    data = await response.get_json()
    assert "error" in data


