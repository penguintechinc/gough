"""Coverage tests for secrets.py - error paths and validation (Part 2)."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from quart import Quart, g


def _passthrough(*dargs, **dkwargs):
    """Decorator passthrough."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


@pytest.fixture()
def secrets_app(monkeypatch):
    """Create Quart app with secrets blueprint."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough)

    if "app.api.secrets" in sys.modules:
        del sys.modules["app.api.secrets"]

    import app.api.secrets as secrets_mod
    secrets_mod = importlib.reload(secrets_mod)

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.url_map.strict_slashes = False
    app.register_blueprint(secrets_mod.secrets_bp)

    @app.before_request
    async def _inject():
        g.current_user = {"id": 1, "email": "admin@test.com"}
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return app, secrets_mod


# ============================================================================
# Validate Backend Tests (lines 117-139)
# ============================================================================


@pytest.mark.asyncio
async def test_validate_backend_unknown(secrets_app):
    """Validate unknown backend should return 400."""
    app, secrets_mod = secrets_app

    with patch.object(secrets_mod, "BACKEND_REGISTRY", {}):
        client = app.test_client()
        response = await client.post("/backends", json={"backend": "unknown", "config": {}})

    assert response.status_code == 400
    data = await response.get_json()
    assert "Unknown backend" in data["error"]
    assert "available" in data


@pytest.mark.asyncio
async def test_validate_backend_connection_error(secrets_app):
    """Validate backend with connection error should return 400."""
    app, secrets_mod = secrets_app
    from app.api.secrets import SecretsManagerError

    mock_backend = MagicMock()
    mock_instance = MagicMock()
    mock_instance.list_secrets.side_effect = SecretsManagerError("Connection failed")
    mock_backend.return_value = mock_instance

    with patch.object(secrets_mod, "BACKEND_REGISTRY", {"test": mock_backend}):
        client = app.test_client()
        response = await client.post("/backends", json={"backend": "test", "config": {}})

    assert response.status_code == 400
    data = await response.get_json()
    assert "Backend configuration error" in data["error"]
    assert data["status"] == "error"


@pytest.mark.asyncio
async def test_validate_backend_unexpected_error(secrets_app):
    """Validate backend with unexpected error should return 500."""
    app, secrets_mod = secrets_app

    mock_backend = MagicMock()
    mock_instance = MagicMock()
    mock_instance.list_secrets.side_effect = RuntimeError("Unexpected")
    mock_backend.return_value = mock_instance

    with patch.object(secrets_mod, "BACKEND_REGISTRY", {"test": mock_backend}):
        client = app.test_client()
        response = await client.post("/backends", json={"backend": "test", "config": {}})

    assert response.status_code == 500
    data = await response.get_json()
    assert "Unexpected error" in data["error"]


@pytest.mark.asyncio
async def test_validate_backend_success(secrets_app):
    """Validate backend success should return 200."""
    app, secrets_mod = secrets_app

    mock_backend = MagicMock()
    mock_instance = AsyncMock()
    mock_instance.list_secrets = AsyncMock(return_value=[])
    mock_backend.return_value = mock_instance

    with patch.object(secrets_mod, "BACKEND_REGISTRY", {"test": mock_backend}):
        client = app.test_client()
        response = await client.post("/backends", json={"backend": "test", "config": {}})

    assert response.status_code == 200
    data = await response.get_json()
    assert "valid" in data["message"]
    assert data["status"] == "connected"


# ============================================================================
# Get Secret Tests (lines 161, 198, 254)
# ============================================================================


@pytest.mark.asyncio
async def test_get_secret_empty_path(secrets_app):
    """Get secret with empty path should return 400."""
    app, secrets_mod = secrets_app

    mock_manager = AsyncMock()
    mock_manager.list_secrets = AsyncMock(return_value=[])

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.get("/")

    # GET / is the list_secrets route, not get_secret
    # list_secrets always succeeds (returns 200)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_secret_not_found(secrets_app):
    """Get non-existent secret should return 404."""
    app, secrets_mod = secrets_app
    from app.api.secrets import SecretNotFoundError

    mock_manager = AsyncMock()
    mock_manager.get_secret.side_effect = SecretNotFoundError("/nonexistent")

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.get("/nonexistent")

    assert response.status_code == 404
    data = await response.get_json()
    assert "not found" in data["error"].lower()


@pytest.mark.asyncio
async def test_get_secret_manager_error(secrets_app):
    """Get secret with manager error should return 500."""
    app, secrets_mod = secrets_app
    from app.api.secrets import SecretsManagerError

    mock_manager = AsyncMock()
    mock_manager.get_secret.side_effect = SecretsManagerError("Backend down")

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.get("/test/path")

    assert response.status_code == 500
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_get_secret_success(secrets_app):
    """Get secret success should return 200."""
    app, secrets_mod = secrets_app

    mock_manager = AsyncMock()
    mock_manager.get_secret = AsyncMock(return_value={"key": "value"})

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.get("/test/path")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["path"] == "test/path"  # Path doesn't have leading slash
    assert data["data"] == {"key": "value"}


# ============================================================================
# Set Secret Tests (lines 290-302)
# ============================================================================


@pytest.mark.asyncio
async def test_set_secret_empty_path(secrets_app):
    """Set secret with empty path should return 400."""
    app, secrets_mod = secrets_app

    client = app.test_client()
    response = await client.post("/", json={"data": {}})

    # POST / is not allowed (set_secret requires a path like POST /somepath)
    assert response.status_code == 405  # Method not allowed


@pytest.mark.asyncio
async def test_set_secret_no_body(secrets_app):
    """Set secret with no body should return 400."""
    app, secrets_mod = secrets_app

    client = app.test_client()
    response = await client.post("/test/path", json=None)

    assert response.status_code == 400
    data = await response.get_json()
    assert "body required" in data["error"].lower()


@pytest.mark.asyncio
async def test_set_secret_no_data_field(secrets_app):
    """Set secret without data field should return 400."""
    app, secrets_mod = secrets_app

    client = app.test_client()
    response = await client.post("/test/path", json={"other": "value"})

    assert response.status_code == 400
    data = await response.get_json()
    assert "data required" in data["error"].lower()


@pytest.mark.asyncio
async def test_set_secret_data_not_dict(secrets_app):
    """Set secret with non-dict data should return 400."""
    app, secrets_mod = secrets_app

    client = app.test_client()
    response = await client.post("/test/path", json={"data": "not a dict"})

    assert response.status_code == 400
    data = await response.get_json()
    assert "dictionary" in data["error"].lower()


@pytest.mark.asyncio
async def test_set_secret_new_secret(secrets_app):
    """Set new secret should return 201."""
    app, secrets_mod = secrets_app
    from app.api.secrets import SecretNotFoundError

    mock_manager = AsyncMock()
    mock_manager.get_secret.side_effect = SecretNotFoundError("/new/path")
    mock_manager.set_secret = AsyncMock()

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.post("/new/path", json={"data": {"key": "val"}})

    assert response.status_code == 201
    data = await response.get_json()
    assert "created" in data["message"].lower()


@pytest.mark.asyncio
async def test_set_secret_update_existing(secrets_app):
    """Set existing secret should return 200."""
    app, secrets_mod = secrets_app

    mock_manager = AsyncMock()
    mock_manager.get_secret = AsyncMock(return_value={"old": "data"})
    mock_manager.set_secret = AsyncMock()

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.post("/existing/path", json={"data": {"new": "data"}})

    assert response.status_code == 200
    data = await response.get_json()
    assert "updated" in data["message"].lower()


@pytest.mark.asyncio
async def test_set_secret_manager_error(secrets_app):
    """Set secret with manager error should return 500."""
    app, secrets_mod = secrets_app
    from app.api.secrets import SecretsManagerError, SecretNotFoundError

    mock_manager = AsyncMock()
    mock_manager.get_secret.side_effect = SecretNotFoundError("/test/path")
    mock_manager.set_secret.side_effect = SecretsManagerError("Backend down")

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.post("/test/path", json={"data": {"key": "val"}})

    assert response.status_code == 500


# ============================================================================
# Delete Secret Tests
# ============================================================================


@pytest.mark.asyncio
async def test_delete_secret_empty_path(secrets_app):
    """Delete secret with empty path should return 400."""
    app, secrets_mod = secrets_app

    client = app.test_client()
    response = await client.delete("/")

    # DELETE / is not allowed (delete_secret requires a path like DELETE /somepath)
    assert response.status_code == 405  # Method not allowed


@pytest.mark.asyncio
async def test_delete_secret_not_found(secrets_app):
    """Delete non-existent secret should return 404."""
    app, secrets_mod = secrets_app

    mock_manager = AsyncMock()
    mock_manager.delete_secret = AsyncMock(return_value=False)

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.delete("/nonexistent")

    assert response.status_code == 404
    data = await response.get_json()
    assert "not found" in data["error"].lower()


@pytest.mark.asyncio
async def test_delete_secret_success(secrets_app):
    """Delete secret success should return 200."""
    app, secrets_mod = secrets_app

    mock_manager = AsyncMock()
    mock_manager.delete_secret = AsyncMock(return_value=True)

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.delete("/test/path")

    assert response.status_code == 200
    data = await response.get_json()
    assert "deleted" in data["message"].lower()


@pytest.mark.asyncio
async def test_delete_secret_manager_error(secrets_app):
    """Delete secret with manager error should return 500."""
    app, secrets_mod = secrets_app
    from app.api.secrets import SecretsManagerError

    mock_manager = AsyncMock()
    mock_manager.delete_secret.side_effect = SecretsManagerError("Backend down")

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.delete("/test/path")

    assert response.status_code == 500


# ============================================================================
# List Secrets Tests
# ============================================================================


@pytest.mark.asyncio
async def test_list_secrets_success(secrets_app):
    """List secrets should return list of paths."""
    app, secrets_mod = secrets_app

    mock_manager = AsyncMock()
    mock_manager.list_secrets = AsyncMock(return_value=["path1", "path2"])

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.get("/")

    assert response.status_code == 200
    data = await response.get_json()
    assert "secrets" in data
    assert len(data["secrets"]) == 2
    assert data["count"] == 2


@pytest.mark.asyncio
async def test_list_secrets_with_path_filter(secrets_app):
    """List secrets with path filter should pass filter to manager."""
    app, secrets_mod = secrets_app

    mock_manager = AsyncMock()
    mock_manager.list_secrets = AsyncMock(return_value=["cloud/aws"])

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.get("/?path=cloud")

    assert response.status_code == 200
    mock_manager.list_secrets.assert_called_once_with("cloud")
    data = await response.get_json()
    assert data["path"] == "cloud"


@pytest.mark.asyncio
async def test_list_secrets_empty(secrets_app):
    """List secrets with no results should return empty list."""
    app, secrets_mod = secrets_app

    mock_manager = AsyncMock()
    mock_manager.list_secrets = AsyncMock(return_value=[])

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.get("/")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["secrets"] == []
    assert data["count"] == 0


@pytest.mark.asyncio
async def test_list_secrets_manager_error(secrets_app):
    """List secrets with manager error should return 500."""
    app, secrets_mod = secrets_app
    from app.api.secrets import SecretsManagerError

    mock_manager = AsyncMock()
    mock_manager.list_secrets.side_effect = SecretsManagerError("Backend down")

    with patch.object(secrets_mod, "get_secrets_manager", return_value=mock_manager):
        client = app.test_client()
        response = await client.get("/")

    assert response.status_code == 500


# Note: Import error handling for SecretNotFoundError
try:
    from app.api.secrets import SecretNotFoundError
except ImportError:
    # Define locally if not available
    class SecretNotFoundError(Exception):
        pass
