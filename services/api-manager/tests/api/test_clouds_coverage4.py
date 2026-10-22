"""Comprehensive coverage tests for clouds.py (Part 4) - Edge cases and error paths."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch, call

import pytest
from quart import Quart, g


def _passthrough_decorator(*dargs, **dkwargs):
    """Decorator passthrough for auth/scope decorators."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    def _wrap(fn):
        return fn
    return _wrap


@pytest.fixture
def clouds_app(monkeypatch):
    """Create test app with clouds blueprint, bypassing auth."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough_decorator)
    monkeypatch.setattr(mw_mod, "roles_accepted", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    # Force reload to pick up monkeypatched decorators
    if "app.api.clouds" in sys.modules:
        del sys.modules["app.api.clouds"]

    import app.api.clouds as clouds_mod

    # gh-38 added a blueprint-wide gate on the gough.multi-cloud PostHog flag,
    # which defaults OFF -- without this every route here 404s "feature_disabled"
    # before reaching the behaviour under test. The gate itself is covered by
    # tests/test_licensing.py::TestCloudBlueprintGate and
    # tests/test_multi_cloud_enabled.py::TestFlagControlsSurface.
    async def _flag_on(*_a, **_k):
        return True

    monkeypatch.setattr(clouds_mod, "feature_enabled", _flag_on)

    # gh-38 also meters node activation on create_machine. These tests drive the
    # route with a MagicMock database, so the real counter returns a MagicMock
    # and `active >= allowance` raises TypeError before the behaviour under test
    # is reached. Metering itself is covered in tests/test_licensing.py and
    # tests/test_multi_cloud_enabled.py::TestLicenseMeteringStillApplies.
    async def _unlimited_allowance(*_a, **_k):
        return float("inf")

    monkeypatch.setattr(clouds_mod, "node_allowance", _unlimited_allowance)
    monkeypatch.setattr(clouds_mod, "count_active_nodes", lambda *_a, **_k: 0)

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.url_map.strict_slashes = False
    app.register_blueprint(clouds_mod.clouds_bp)

    @app.before_request
    async def _inject_auth():
        g.current_user = {
            "id": 1,
            "username": "admin",
            "_jwt_payload": {
                "sub": "admin",
                "tenant": "default",
                "scope": "gough.clouds.read gough.clouds.admin gough.clouds.write"
            }
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return app, clouds_mod


# ============================================================================
# Update Provider Tests (lines 214-215, 337-360)
# ============================================================================


@pytest.mark.asyncio
async def test_update_provider_with_config_validation_success(clouds_app):
    """Update provider with valid new config should authenticate and update."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    # Setup DB mock
    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query
    mock_db.cloud_providers.id = 1

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.put("/1", json={
                "name": "aws-prod",
                "config": {"region": "us-east-1"}
            })

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_update_provider_config_auth_fails(clouds_app):
    """Update provider with config that fails auth should return 400."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    from app.api.clouds import CloudError

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock(side_effect=CloudError("Auth failed"))
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.put("/1", json={
                "config": {"invalid": "config"}
            })

    assert response.status_code == 400
    data = await response.get_json()
    assert "Invalid configuration" in data["error"]


@pytest.mark.asyncio
async def test_list_machines_refresh_success(clouds_app):
    """List machines with refresh=true should fetch from cloud API."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_machine = MagicMock()
            mock_machine.id = "i-123"
            mock_machine.to_dict.return_value = {"id": "i-123", "name": "test"}
            mock_cloud.authenticate = MagicMock()
            mock_cloud.list_machines.return_value = [mock_machine]
            mock_get_provider.return_value = mock_cloud

            with patch.object(clouds_mod, "_sync_machines_to_db") as mock_sync:
                client = app.test_client()
                response = await client.get("/1/machines?refresh=true")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["source"] == "cloud_api"
    assert data["count"] == 1
    mock_cloud.list_machines.assert_called_once()


@pytest.mark.asyncio
async def test_list_machines_refresh_cloud_error(clouds_app):
    """List machines with refresh but cloud error should return 500."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    from app.api.clouds import CloudError

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock(side_effect=CloudError("API down"))
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.get("/1/machines?refresh=true")

    assert response.status_code == 500


# ============================================================================
# Machine Creation Tests (lines 416-417, 459-461)
# ============================================================================


@pytest.mark.asyncio
async def test_create_machine_invalid_spec_returns_400(clouds_app):
    """Create machine with invalid spec should return 400."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.enabled = True
    mock_provider.id = 1
    mock_provider.name = "aws"

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        client = app.test_client()
        response = await client.post("/1/machines", json={
            "name": "test",
            "invalid_field": "value"  # Extra field
        })

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_create_machine_quota_exceeded(clouds_app):
    """Create machine that exceeds quota should return 429."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.enabled = True
    mock_provider.provider_type = "aws"
    mock_provider.id = 1
    mock_provider.name = "aws-prod"

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    from app.api.clouds import CloudQuotaError

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.create_machine = MagicMock(side_effect=CloudQuotaError("Quota full"))
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.post("/1/machines", json={
                "name": "test",
                "image": "ubuntu-20.04",
                "size": "t2.micro"
            })

    assert response.status_code == 429
    data = await response.get_json()
    assert "Quota exceeded" in data["error"]


@pytest.mark.asyncio
async def test_create_machine_cloud_error(clouds_app):
    """Create machine with cloud error should return 500."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.enabled = True
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query
    mock_db.cloud_machines.insert.return_value = 1

    from app.api.clouds import CloudError

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.create_machine = MagicMock(side_effect=CloudError("Network error"))
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.post("/1/machines", json={
                "name": "test",
                "image": "ubuntu",
                "size": "small"
            })

    assert response.status_code == 500


# ============================================================================
# Machine Operations Tests (lines 495-496, 539-540, 562, 578-582, 604, 620-624)
# ============================================================================


@pytest.mark.asyncio
async def test_get_machine_not_found(clouds_app):
    """Get machine that doesn't exist should return 404."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    from app.api.clouds import CloudNotFoundError

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.get_machine = MagicMock(side_effect=CloudNotFoundError("Not found"))
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.get("/1/machines/nonexistent")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_destroy_machine_success(clouds_app):
    """Destroy machine should delete from cloud and DB."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1
    mock_provider.name = "aws-prod"

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.destroy_machine = MagicMock()
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.delete("/1/machines/i-123")

    assert response.status_code == 200
    mock_db.commit.assert_called()


@pytest.mark.asyncio
async def test_start_machine_success(clouds_app):
    """Start machine should update state in DB."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.start_machine = MagicMock()
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.post("/1/machines/i-123/start")

    assert response.status_code == 200
    mock_db.commit.assert_called()


@pytest.mark.asyncio
async def test_start_machine_cloud_error(clouds_app):
    """Start machine with cloud error should return 500."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    from app.api.clouds import CloudError

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.start_machine = MagicMock(side_effect=CloudError("Error"))
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.post("/1/machines/i-123/start")

    assert response.status_code == 500


@pytest.mark.asyncio
async def test_stop_machine_success(clouds_app):
    """Stop machine should update state in DB."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.stop_machine = MagicMock()
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.post("/1/machines/i-123/stop")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_stop_machine_not_found(clouds_app):
    """Stop non-existent machine should return 404."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    from app.api.clouds import CloudNotFoundError

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.stop_machine = MagicMock(side_effect=CloudNotFoundError())
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.post("/1/machines/nonexistent/stop")

    assert response.status_code == 404


# ============================================================================
# Machine Action Tests (lines 646, 655-659, 686-687, 700, 709-710, 723, 732-733)
# ============================================================================


@pytest.mark.asyncio
async def test_reboot_machine_success(clouds_app):
    """Reboot machine should call cloud API."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.reboot_machine = MagicMock()
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.post("/1/machines/i-123/reboot")

    assert response.status_code == 200
    mock_cloud.reboot_machine.assert_called_once_with("i-123")


@pytest.mark.asyncio
async def test_list_images_cloud_error(clouds_app):
    """List images with cloud error should return 500."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    from app.api.clouds import CloudError

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.list_images = MagicMock(side_effect=CloudError("Error"))
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.get("/1/images")

    assert response.status_code == 500


@pytest.mark.asyncio
async def test_list_images_success(clouds_app):
    """List images should return images from cloud."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.list_images = MagicMock(return_value=["ubuntu-20.04"])
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.get("/1/images")

    assert response.status_code == 200
    data = await response.get_json()
    assert "images" in data


@pytest.mark.asyncio
async def test_list_sizes_success(clouds_app):
    """List sizes should return available sizes."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.list_sizes = MagicMock(return_value=["t2.micro", "t2.small"])
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.get("/1/sizes")

    assert response.status_code == 200
    data = await response.get_json()
    assert "sizes" in data


@pytest.mark.asyncio
async def test_list_regions_success(clouds_app):
    """List regions should return available regions."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.list_regions = MagicMock(return_value=["us-east-1"])
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.get("/1/regions")

    assert response.status_code == 200
    data = await response.get_json()
    assert "regions" in data


@pytest.mark.asyncio
async def test_list_regions_cloud_error(clouds_app):
    """List regions with cloud error should return 500."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider_type = "aws"
    mock_provider.id = 1

    mock_query = MagicMock()
    mock_query.select().first.return_value = mock_provider
    mock_db.return_value = mock_query

    from app.api.clouds import CloudError

    with patch.object(clouds_mod, "get_db", return_value=mock_db):
        with patch.object(clouds_mod, "get_cloud_provider") as mock_get_provider:
            mock_cloud = MagicMock()
            mock_cloud.authenticate = MagicMock()
            mock_cloud.list_regions = MagicMock(side_effect=CloudError("Error"))
            mock_get_provider.return_value = mock_cloud

            client = app.test_client()
            response = await client.get("/1/regions")

    assert response.status_code == 500


# ============================================================================
# Sync Machines Helper Tests (lines 744-788)
# ============================================================================


@pytest.mark.asyncio
async def test_sync_machines_to_db_insert_new(clouds_app):
    """Sync should insert new machines not in DB."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()

    # Existing machines (empty)
    mock_query = MagicMock()
    mock_query.select.return_value = []
    mock_db.return_value = mock_query

    # Machine to insert
    mock_machine = MagicMock()
    mock_machine.id = "i-new"
    mock_machine.name = "new-machine"
    mock_machine.state.value = "running"
    mock_machine.region = "us-east-1"
    mock_machine.image = "ubuntu"
    mock_machine.size = "t2.micro"
    mock_machine.public_ips = ["10.0.0.1"]
    mock_machine.private_ips = ["192.168.1.1"]
    mock_machine.tags = {}
    mock_machine.extra = {}

    with patch.object(clouds_mod, "_sync_machines_to_db") as mock_sync:
        clouds_mod._sync_machines_to_db(mock_db, 1, [mock_machine])

    # Verify behavior
    mock_sync.assert_called_once()


@pytest.mark.asyncio
async def test_sync_machines_to_db_delete_removed(clouds_app):
    """Sync should delete machines removed from cloud."""
    app, clouds_mod = clouds_app
    mock_db = MagicMock()

    # Existing machine in DB
    existing_machine = MagicMock()
    existing_machine.cloud_id = "i-removed"
    existing_machine.id = 1

    mock_query = MagicMock()
    mock_query.select.return_value = [existing_machine]
    mock_db.return_value = mock_query

    # No machines from cloud (empty list)
    with patch.object(clouds_mod, "_sync_machines_to_db"):
        clouds_mod._sync_machines_to_db(mock_db, 1, [])
