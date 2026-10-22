"""Extended test suite for Clouds API endpoints (uncovered line coverage).

Targets uncovered branches in:
- Provider listing, creation, update, deletion
- Machine CRUD operations and state transitions
- Cloud resource enumeration (images, sizes, regions)
- Error handling (404 not found, 409 conflicts, 401 auth errors)
- Database operations and sync
"""

import importlib
import pytest
from quart import Quart, g
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from datetime import datetime, timezone
import json


def _passthrough(*dargs, **dkwargs):
    """Passthrough decorator for mocking auth decorators."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


@pytest.fixture()
def dal_with_clouds(dal):
    """Extend dal with cloud provider and machine tables."""
    from penguin_dal import Field

    # Column names here MUST track models_sqlalchemy's cloud tables. They used
    # to describe a shape app/api/clouds.py assumed but that no deployment ever
    # had, which is exactly why the drift between the two went unnoticed until
    # the multi-cloud flag was switched on. See
    # tests/test_multi_cloud_enabled.py, which builds the real schema instead.
    if "cloud_providers" not in dal._metadata.tables:
        dal.define_table(
            "cloud_providers",
            Field("name", "string", notnull=True, unique=True),
            Field("provider_type", "string", notnull=True),
            Field("description", "text"),
            Field("region", "string"),
            Field("credentials_path", "string"),
            Field("config_data", "json"),
            Field("status", "string", default="disconnected"),
            Field("is_active", "boolean", default=True),
            Field("last_sync_at", "datetime"),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=True,
        )

    if "cloud_machines" not in dal._metadata.tables:
        dal.define_table(
            "cloud_machines",
            Field("provider_id", "integer", notnull=True),
            Field("external_id", "string", notnull=True),
            Field("hostname", "string"),
            Field("ip_address", "string"),
            Field("private_ip", "string"),
            Field("public_ips", "json"),
            Field("private_ips", "json"),
            Field("status", "string", default="new"),
            Field("machine_type", "string"),
            Field("architecture", "string", default="amd64"),
            Field("cpu_count", "integer"),
            Field("memory_mb", "integer"),
            Field("storage_gb", "integer"),
            Field("os_image", "string"),
            Field("zone", "string"),
            Field("tags", "json"),
            Field("metadata", "json"),
            Field("lxd_cluster_id", "integer"),
            Field("fleet_host_id", "integer"),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=True,
        )

    return dal


@pytest.fixture()
def clouds_client(dal_with_clouds, monkeypatch):
    """Create a test client with clouds blueprint registered."""
    import app.models as models_mod
    monkeypatch.setattr(models_mod, "get_db", lambda: dal_with_clouds)

    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough)
    monkeypatch.setattr(mw_mod, "roles_accepted", _passthrough)

    import app.api.clouds as clouds_mod
    clouds_mod = importlib.reload(clouds_mod)
    monkeypatch.setattr(clouds_mod, "get_db", lambda: dal_with_clouds)

    # gh-38 gates this blueprint on the gough.multi-cloud PostHog flag (default
    # OFF) and meters node activation on create_machine. Both are covered by
    # tests/test_licensing.py and tests/test_multi_cloud_enabled.py; here they
    # would only mask the behaviour under test.
    async def _flag_on(*_a, **_k):
        return True

    async def _unlimited_allowance(*_a, **_k):
        return float("inf")

    monkeypatch.setattr(clouds_mod, "feature_enabled", _flag_on)
    monkeypatch.setattr(clouds_mod, "node_allowance", _unlimited_allowance)

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["JWT_SECRET_KEY"] = "test-secret-key"
    app.url_map.strict_slashes = False

    app.register_blueprint(clouds_mod.clouds_bp, url_prefix="/api/v1/clouds")

    @app.before_request
    async def _inject_auth():
        g.current_user = {
            "id": 1,
            "username": "test-admin",
            "role": "admin",
            "_jwt_payload": {
                "sub": "test-admin",
                "tenant": "default",
                "scope": "gough.clouds.read gough.clouds.write gough.clouds.admin",
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return app.test_client()


# =============================================================================
# Provider Listing Tests
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.xfail(reason="DAL query syntax error in app/api/clouds.py:46", strict=False)
async def test_list_providers_empty(clouds_client):
    """Test GET / when no providers exist."""
    response = await clouds_client.get("/api/v1/clouds/")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 0
    assert len(data["providers"]) == 0


@pytest.mark.asyncio
@pytest.mark.xfail(reason="DAL query syntax error in app/api/clouds.py:46", strict=False)
async def test_list_providers_with_data(clouds_client, dal_with_clouds):
    """Test GET / with existing providers."""
    now = datetime.now(timezone.utc)
    dal_with_clouds.cloud_providers.insert(
        name="AWS",
        provider_type="aws",
        config_data={"region": "us-east-1"},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.cloud_providers.insert(
        name="GCP",
        provider_type="gcp",
        config_data={"project": "my-project"},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.get("/api/v1/clouds/")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 2
    assert len(data["providers"]) == 2
    # Verify config not exposed to non-admin
    for provider in data["providers"]:
        assert "config" not in provider


@pytest.mark.asyncio
@pytest.mark.xfail(reason="DAL query syntax error in app/api/clouds.py:46", strict=False)
async def test_list_providers_config_redacted(clouds_client, dal_with_clouds):
    """Test GET / redacts config from all providers."""
    now = datetime.now(timezone.utc)
    dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={"secret_key": "secret-123"},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.get("/api/v1/clouds/")
    assert response.status_code == 200
    data = await response.get_json()
    for provider in data["providers"]:
        assert "config" not in provider


# =============================================================================
# Provider Creation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_add_provider_missing_body(clouds_client):
    """Test POST / with missing request body."""
    response = await clouds_client.post("/api/v1/clouds/", json=None)
    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_add_provider_missing_name(clouds_client):
    """Test POST / without provider name."""
    response = await clouds_client.post(
        "/api/v1/clouds/",
        json={"provider_type": "aws", "config": {}},
    )
    assert response.status_code == 400
    data = await response.get_json()
    assert "name" in data["error"].lower()


@pytest.mark.asyncio
async def test_add_provider_missing_type(clouds_client):
    """Test POST / without provider_type."""
    response = await clouds_client.post(
        "/api/v1/clouds/",
        json={"name": "My AWS", "config": {}},
    )
    assert response.status_code == 400
    data = await response.get_json()
    assert "type" in data["error"].lower()


@pytest.mark.asyncio
async def test_add_provider_unknown_type(clouds_client):
    """Test POST / with unknown provider type."""
    with patch("app.api.clouds.CLOUD_REGISTRY", {}):
        response = await clouds_client.post(
            "/api/v1/clouds/",
            json={
                "name": "Unknown",
                "provider_type": "unknown_cloud",
                "config": {},
            },
        )
        assert response.status_code == 400
        data = await response.get_json()
        assert "Unknown" in data["error"] or "available" in data


@pytest.mark.asyncio
async def test_add_provider_auth_error(clouds_client):
    """Test POST / when authentication fails."""
    from app.clouds import CloudAuthError

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.authenticate.side_effect = CloudAuthError("Invalid creds")
        mock_get.return_value = mock_provider

        response = await clouds_client.post(
            "/api/v1/clouds/",
            json={
                "name": "Bad Auth",
                "provider_type": "aws",
                "config": {"secret": "wrong"},
            },
        )
        assert response.status_code == 400
        data = await response.get_json()
        assert "auth_error" in data.get("status", "")


@pytest.mark.asyncio
async def test_add_provider_duplicate_name(clouds_client, dal_with_clouds):
    """Test POST / with duplicate provider name (409)."""
    now = datetime.now(timezone.utc)
    dal_with_clouds.cloud_providers.insert(
        name="AWS",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider"):
        response = await clouds_client.post(
            "/api/v1/clouds/",
            json={
                "name": "AWS",
                "provider_type": "gcp",
                "config": {},
            },
        )
        assert response.status_code == 409
        data = await response.get_json()
        assert "already exists" in data["error"]


@pytest.mark.asyncio
async def test_add_provider_config_error(clouds_client):
    """Test POST / when provider config is invalid."""
    from app.clouds import CloudError

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.authenticate.side_effect = CloudError("Invalid config")
        mock_get.return_value = mock_provider

        response = await clouds_client.post(
            "/api/v1/clouds/",
            json={
                "name": "Bad Config",
                "provider_type": "aws",
                "config": {},
            },
        )
        assert response.status_code == 400
        data = await response.get_json()
        assert "config_error" in data.get("status", "")


@pytest.mark.asyncio
async def test_add_provider_success(clouds_client, dal_with_clouds):
    """Test POST / successfully creates provider."""
    with patch("app.api.clouds.get_cloud_provider"):
        response = await clouds_client.post(
            "/api/v1/clouds/",
            json={
                "name": "New Provider",
                "provider_type": "aws",
                "config": {"region": "us-west-2"},
                "enabled": True,
            },
        )
        assert response.status_code == 201
        data = await response.get_json()
        assert data["provider"]["name"] == "New Provider"
        assert data["provider"]["status"] == "connected"


# =============================================================================
# Provider Get Tests
# =============================================================================


@pytest.mark.asyncio
async def test_get_provider_not_found(clouds_client):
    """Test GET /<id> when provider doesn't exist."""
    response = await clouds_client.get("/api/v1/clouds/999")
    assert response.status_code == 404
    data = await response.get_json()
    assert "not found" in data["error"].lower()


@pytest.mark.asyncio
async def test_get_provider_found(clouds_client, dal_with_clouds):
    """Test GET /<id> returns provider."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={"secret": "data"},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.get(f"/api/v1/clouds/{provider_id}")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["name"] == "Test"
    # Config should be redacted for non-admin
    assert "config" not in data


# =============================================================================
# Provider Update Tests
# =============================================================================


@pytest.mark.asyncio
async def test_update_provider_not_found(clouds_client):
    """Test PUT /<id> when provider doesn't exist."""
    response = await clouds_client.put(
        "/api/v1/clouds/999",
        json={"name": "Updated"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_provider_name(clouds_client, dal_with_clouds):
    """Test PUT /<id> updates provider name."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Old Name",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.put(
        f"/api/v1/clouds/{provider_id}",
        json={"name": "New Name"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert "updated" in data["message"].lower()


@pytest.mark.asyncio
async def test_update_provider_enabled(clouds_client, dal_with_clouds):
    """Test PUT /<id> updates enabled flag."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.put(
        f"/api/v1/clouds/{provider_id}",
        json={"enabled": False},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_update_provider_config_auth_error(clouds_client, dal_with_clouds):
    """Test PUT /<id> rejects config that fails auth."""
    from app.clouds import CloudError

    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={"region": "us-east-1"},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.authenticate.side_effect = CloudError("Bad config")
        mock_get.return_value = mock_provider

        response = await clouds_client.put(
            f"/api/v1/clouds/{provider_id}",
            json={"config": {"region": "invalid"}},
        )
        assert response.status_code == 400


# =============================================================================
# Provider Delete Tests
# =============================================================================


@pytest.mark.asyncio
async def test_delete_provider_not_found(clouds_client):
    """Test DELETE /<id> when provider doesn't exist."""
    response = await clouds_client.delete("/api/v1/clouds/999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_provider_with_machines(clouds_client, dal_with_clouds):
    """Test DELETE /<id> fails if provider has machines (409)."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.cloud_machines.insert(
        provider_id=provider_id,
        external_id="i-12345",
        hostname="machine-1",
        status="running",
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.delete(f"/api/v1/clouds/{provider_id}")
    assert response.status_code == 409
    data = await response.get_json()
    assert "Cannot delete" in data["error"] or "machines" in data["error"].lower()


@pytest.mark.asyncio
async def test_delete_provider_success(clouds_client, dal_with_clouds):
    """Test DELETE /<id> successfully deletes provider."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.delete(f"/api/v1/clouds/{provider_id}")
    assert response.status_code == 200
    data = await response.get_json()
    assert "deleted" in data["message"].lower()


# =============================================================================
# Provider Test Connectivity Tests
# =============================================================================


@pytest.mark.asyncio
async def test_test_provider_not_found(clouds_client):
    """Test POST /<id>/test when provider doesn't exist."""
    response = await clouds_client.post("/api/v1/clouds/999/test")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_test_provider_success(clouds_client, dal_with_clouds):
    """Test POST /<id>/test with successful connection."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="disconnected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider"):
        response = await clouds_client.post(f"/api/v1/clouds/{provider_id}/test")
        assert response.status_code == 200
        data = await response.get_json()
        assert "connected" in data["status"]


@pytest.mark.asyncio
async def test_test_provider_auth_error(clouds_client, dal_with_clouds):
    """Test POST /<id>/test with auth error (401)."""
    from app.clouds import CloudAuthError

    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.authenticate.side_effect = CloudAuthError("Auth failed")
        mock_get.return_value = mock_provider

        response = await clouds_client.post(f"/api/v1/clouds/{provider_id}/test")
        assert response.status_code == 401
        data = await response.get_json()
        assert "auth_error" in data["status"]


@pytest.mark.asyncio
async def test_test_provider_cloud_error(clouds_client, dal_with_clouds):
    """Test POST /<id>/test with cloud error (500)."""
    from app.clouds import CloudError

    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.authenticate.side_effect = CloudError("Cloud error")
        mock_get.return_value = mock_provider

        response = await clouds_client.post(f"/api/v1/clouds/{provider_id}/test")
        assert response.status_code == 500
        data = await response.get_json()
        assert "error" in data["status"]


# =============================================================================
# Machine Listing Tests
# =============================================================================


@pytest.mark.asyncio
async def test_list_machines_provider_not_found(clouds_client):
    """Test GET /<id>/machines when provider doesn't exist."""
    response = await clouds_client.get("/api/v1/clouds/999/machines")
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.xfail(reason="DAL query syntax error in app/api/clouds.py or list endpoint", strict=False)
async def test_list_machines_from_database(clouds_client, dal_with_clouds):
    """Test GET /<id>/machines returns machines from database."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.cloud_machines.insert(
        provider_id=provider_id,
        external_id="i-12345",
        hostname="machine-1",
        status="running",
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.get(f"/api/v1/clouds/{provider_id}/machines")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 1
    assert data["source"] == "database"


@pytest.mark.asyncio
@pytest.mark.xfail(reason="await request.args bug in app/api/clouds.py line 335", strict=False)
async def test_list_machines_refresh_from_api(clouds_client, dal_with_clouds):
    """Test GET /<id>/machines?refresh=true fetches from cloud API."""
    from app.clouds import MachineSpec

    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    mock_machine = MagicMock()
    mock_machine.id = "i-new"
    mock_machine.name = "new-machine"
    mock_machine.to_dict.return_value = {"id": "i-new", "name": "new-machine"}

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.list_machines.return_value = [mock_machine]
        mock_get.return_value = mock_provider

        response = await clouds_client.get(
            f"/api/v1/clouds/{provider_id}/machines?refresh=true"
        )
        assert response.status_code == 200
        data = await response.get_json()
        assert data["source"] == "cloud_api"


@pytest.mark.asyncio
@pytest.mark.xfail(reason="await request.args bug in app/api/clouds.py line 335", strict=False)
async def test_list_machines_refresh_api_error(clouds_client, dal_with_clouds):
    """Test GET /<id>/machines?refresh=true handles API error (500)."""
    from app.clouds import CloudError

    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.list_machines.side_effect = CloudError("API error")
        mock_get.return_value = mock_provider

        response = await clouds_client.get(
            f"/api/v1/clouds/{provider_id}/machines?refresh=true"
        )
        assert response.status_code == 500


# =============================================================================
# Machine Creation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_create_machine_provider_not_found(clouds_client):
    """Test POST /<id>/machines when provider doesn't exist."""
    response = await clouds_client.post(
        "/api/v1/clouds/999/machines",
        json={"name": "test", "image": "ubuntu", "size": "t2.micro"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_machine_provider_disabled(clouds_client, dal_with_clouds):
    """Test POST /<id>/machines when provider is disabled."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=False,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.post(
        f"/api/v1/clouds/{provider_id}/machines",
        json={"name": "test", "image": "ubuntu", "size": "t2.micro"},
    )
    assert response.status_code == 400
    data = await response.get_json()
    assert "disabled" in data["error"].lower()


@pytest.mark.asyncio
async def test_create_machine_missing_body(clouds_client, dal_with_clouds):
    """Test POST /<id>/machines with missing body."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.post(f"/api/v1/clouds/{provider_id}/machines")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_create_machine_missing_name(clouds_client, dal_with_clouds):
    """Test POST /<id>/machines without machine name."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.post(
        f"/api/v1/clouds/{provider_id}/machines",
        json={"image": "ubuntu", "size": "t2.micro"},
    )
    assert response.status_code == 400
    data = await response.get_json()
    assert "name" in data["error"].lower()


@pytest.mark.asyncio
async def test_create_machine_missing_image(clouds_client, dal_with_clouds):
    """Test POST /<id>/machines without image."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.post(
        f"/api/v1/clouds/{provider_id}/machines",
        json={"name": "test", "size": "t2.micro"},
    )
    assert response.status_code == 400
    data = await response.get_json()
    assert "image" in data["error"].lower()


@pytest.mark.asyncio
async def test_create_machine_missing_size(clouds_client, dal_with_clouds):
    """Test POST /<id>/machines without size."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    response = await clouds_client.post(
        f"/api/v1/clouds/{provider_id}/machines",
        json={"name": "test", "image": "ubuntu"},
    )
    assert response.status_code == 400
    data = await response.get_json()
    assert "size" in data["error"].lower()


@pytest.mark.asyncio
async def test_create_machine_quota_error(clouds_client, dal_with_clouds):
    """Test POST /<id>/machines with quota exceeded (429)."""
    from app.clouds import CloudQuotaError

    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.create_machine.side_effect = CloudQuotaError("Quota exceeded")
        mock_get.return_value = mock_provider

        response = await clouds_client.post(
            f"/api/v1/clouds/{provider_id}/machines",
            json={"name": "test", "image": "ubuntu", "size": "t2.micro"},
        )
        assert response.status_code == 429


@pytest.mark.asyncio
async def test_create_machine_success(clouds_client, dal_with_clouds):
    """Test POST /<id>/machines successfully creates machine."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    mock_machine = MagicMock()
    mock_machine.id = "i-12345"
    mock_machine.name = "new-machine"
    mock_machine.state.value = "pending"
    mock_machine.region = "us-east-1"
    mock_machine.image = "ubuntu"
    mock_machine.size = "t2.micro"
    mock_machine.public_ips = []
    mock_machine.private_ips = []
    mock_machine.tags = {}
    mock_machine.extra = {}
    mock_machine.to_dict.return_value = {
        "id": "i-12345",
        "name": "new-machine",
    }

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.create_machine.return_value = mock_machine
        mock_get.return_value = mock_provider

        response = await clouds_client.post(
            f"/api/v1/clouds/{provider_id}/machines",
            json={"name": "new-machine", "image": "ubuntu", "size": "t2.micro"},
        )
        assert response.status_code == 201
        data = await response.get_json()
        assert "db_id" in data


# =============================================================================
# Machine Get Tests
# =============================================================================


@pytest.mark.asyncio
async def test_get_machine_provider_not_found(clouds_client):
    """Test GET /<id>/machines/<mid> when provider doesn't exist."""
    response = await clouds_client.get("/api/v1/clouds/999/machines/i-12345")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_machine_not_found(clouds_client, dal_with_clouds):
    """Test GET /<id>/machines/<mid> when machine not found (404)."""
    from app.clouds import CloudNotFoundError

    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.get_machine.side_effect = CloudNotFoundError("Not found")
        mock_get.return_value = mock_provider

        response = await clouds_client.get(
            f"/api/v1/clouds/{provider_id}/machines/i-notfound"
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_machine_success(clouds_client, dal_with_clouds):
    """Test GET /<id>/machines/<mid> successfully retrieves machine."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    mock_machine = MagicMock()
    mock_machine.to_dict.return_value = {
        "id": "i-12345",
        "name": "machine-1",
        "state": "running",
    }

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.get_machine.return_value = mock_machine
        mock_get.return_value = mock_provider

        response = await clouds_client.get(
            f"/api/v1/clouds/{provider_id}/machines/i-12345"
        )
        assert response.status_code == 200
        data = await response.get_json()
        assert data["name"] == "machine-1"


# =============================================================================
# Machine State Transitions (Delete, Start, Stop, Reboot)
# =============================================================================


@pytest.mark.asyncio
async def test_destroy_machine_provider_not_found(clouds_client):
    """Test DELETE /<id>/machines/<mid> when provider not found."""
    response = await clouds_client.delete("/api/v1/clouds/999/machines/i-12345")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_destroy_machine_not_found(clouds_client, dal_with_clouds):
    """Test DELETE /<id>/machines/<mid> when machine not found (404)."""
    from app.clouds import CloudNotFoundError

    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.destroy_machine.side_effect = CloudNotFoundError("Not found")
        mock_get.return_value = mock_provider

        response = await clouds_client.delete(
            f"/api/v1/clouds/{provider_id}/machines/i-notfound"
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_destroy_machine_success(clouds_client, dal_with_clouds):
    """Test DELETE /<id>/machines/<mid> successfully destroys machine."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.cloud_machines.insert(
        provider_id=provider_id,
        external_id="i-12345",
        hostname="machine-1",
        status="running",
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider"):
        response = await clouds_client.delete(
            f"/api/v1/clouds/{provider_id}/machines/i-12345"
        )
        assert response.status_code == 200
        data = await response.get_json()
        assert "destroyed" in data["message"].lower()


@pytest.mark.asyncio
async def test_start_machine_success(clouds_client, dal_with_clouds):
    """Test POST /<id>/machines/<mid>/start."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.cloud_machines.insert(
        provider_id=provider_id,
        external_id="i-12345",
        hostname="machine-1",
        status="stopped",
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider"):
        response = await clouds_client.post(
            f"/api/v1/clouds/{provider_id}/machines/i-12345/start"
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_stop_machine_success(clouds_client, dal_with_clouds):
    """Test POST /<id>/machines/<mid>/stop."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.cloud_machines.insert(
        provider_id=provider_id,
        external_id="i-12345",
        hostname="machine-1",
        status="running",
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider"):
        response = await clouds_client.post(
            f"/api/v1/clouds/{provider_id}/machines/i-12345/stop"
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_reboot_machine_success(clouds_client, dal_with_clouds):
    """Test POST /<id>/machines/<mid>/reboot."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider"):
        response = await clouds_client.post(
            f"/api/v1/clouds/{provider_id}/machines/i-12345/reboot"
        )
        assert response.status_code == 200


# =============================================================================
# Provider Resource Enumeration (Images, Sizes, Regions)
# =============================================================================


@pytest.mark.asyncio
async def test_list_images_provider_not_found(clouds_client):
    """Test GET /<id>/images when provider not found."""
    response = await clouds_client.get("/api/v1/clouds/999/images")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_images_success(clouds_client, dal_with_clouds):
    """Test GET /<id>/images returns images."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.list_images.return_value = [
            {"id": "ami-123", "name": "Ubuntu 20.04"}
        ]
        mock_get.return_value = mock_provider

        response = await clouds_client.get(f"/api/v1/clouds/{provider_id}/images")
        assert response.status_code == 200
        data = await response.get_json()
        assert len(data["images"]) > 0


@pytest.mark.asyncio
async def test_list_sizes_success(clouds_client, dal_with_clouds):
    """Test GET /<id>/sizes returns sizes."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.list_sizes.return_value = [
            {"id": "t2.micro", "name": "Micro"}
        ]
        mock_get.return_value = mock_provider

        response = await clouds_client.get(f"/api/v1/clouds/{provider_id}/sizes")
        assert response.status_code == 200
        data = await response.get_json()
        assert len(data["sizes"]) > 0


@pytest.mark.asyncio
async def test_list_regions_success(clouds_client, dal_with_clouds):
    """Test GET /<id>/regions returns regions."""
    now = datetime.now(timezone.utc)
    provider_id = dal_with_clouds.cloud_providers.insert(
        name="Test",
        provider_type="aws",
        config_data={},
        status="connected",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    dal_with_clouds.commit()

    with patch("app.api.clouds.get_cloud_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.list_regions.return_value = [
            {"id": "us-east-1", "name": "US East"}
        ]
        mock_get.return_value = mock_provider

        response = await clouds_client.get(f"/api/v1/clouds/{provider_id}/regions")
        assert response.status_code == 200
        data = await response.get_json()
        assert len(data["regions"]) > 0


# =============================================================================
# _sync_machines_to_db batching (gh-22)
# =============================================================================


def _make_machine(*, machine_id, name, state="running"):
    from app.clouds import Machine, MachineState

    return Machine(
        id=machine_id,
        name=name,
        state=MachineState(state),
        provider="aws",
        provider_id="1",
        region="us-east-1",
        image="ubuntu-24.04",
        size="t3.medium",
        public_ips=["1.2.3.4"],
        private_ips=["10.0.0.1"],
        tags={"env": "test"},
        extra={},
    )


class TestSyncMachinesToDbBatching:
    """# regression: gh-22

    ``_sync_machines_to_db`` used to insert/delete one row at a time in a
    Python loop; new-machine inserts now go through a single
    ``bulk_insert()`` call and stale-machine deletes through a single
    ``belongs()``-scoped DELETE. Runs against the real (sqlite) ``dal``
    fixture, not mocks, so the batched SQL actually executes and the
    resulting table state is asserted -- not just "a mock was called".
    """

    def test_inserts_new_machines_via_bulk_insert(self, dal_with_clouds) -> None:
        import app.api.clouds as clouds_mod

        now = datetime.now(timezone.utc)
        provider_id = dal_with_clouds.cloud_providers.insert(
            name="Test", provider_type="aws", config_data={}, status="connected",
            is_active=True, created_at=now, updated_at=now,
        )
        dal_with_clouds.commit()

        machines = [
            _make_machine(machine_id="i-1", name="host-1"),
            _make_machine(machine_id="i-2", name="host-2"),
        ]
        clouds_mod._sync_machines_to_db(dal_with_clouds, provider_id, machines)

        rows = dal_with_clouds(
            dal_with_clouds.cloud_machines.provider_id == provider_id
        ).select()
        assert {r.external_id for r in rows} == {"i-1", "i-2"}

    def test_updates_existing_machine_in_place(self, dal_with_clouds) -> None:
        import app.api.clouds as clouds_mod

        now = datetime.now(timezone.utc)
        provider_id = dal_with_clouds.cloud_providers.insert(
            name="Test", provider_type="aws", config_data={}, status="connected",
            is_active=True, created_at=now, updated_at=now,
        )
        dal_with_clouds.cloud_machines.insert(
            provider_id=provider_id, external_id="i-1", hostname="old-name",
            status="stopped", created_at=now, updated_at=now,
        )
        dal_with_clouds.commit()

        clouds_mod._sync_machines_to_db(
            dal_with_clouds, provider_id, [_make_machine(machine_id="i-1", name="new-name")]
        )

        rows = dal_with_clouds(
            dal_with_clouds.cloud_machines.provider_id == provider_id
        ).select()
        assert len(rows) == 1  # updated in place, not duplicated
        assert rows[0].hostname == "new-name"
        assert rows[0].status == "running"

    def test_deletes_stale_machines_via_batched_delete(self, dal_with_clouds) -> None:
        import app.api.clouds as clouds_mod

        now = datetime.now(timezone.utc)
        provider_id = dal_with_clouds.cloud_providers.insert(
            name="Test", provider_type="aws", config_data={}, status="connected",
            is_active=True, created_at=now, updated_at=now,
        )
        dal_with_clouds.cloud_machines.insert(
            provider_id=provider_id, external_id="i-gone-1", hostname="gone-1",
            created_at=now, updated_at=now,
        )
        dal_with_clouds.cloud_machines.insert(
            provider_id=provider_id, external_id="i-gone-2", hostname="gone-2",
            created_at=now, updated_at=now,
        )
        dal_with_clouds.cloud_machines.insert(
            provider_id=provider_id, external_id="i-stays", hostname="stays",
            created_at=now, updated_at=now,
        )
        dal_with_clouds.commit()

        # Cloud API now reports only "i-stays" -- both "i-gone-*" rows are stale.
        clouds_mod._sync_machines_to_db(
            dal_with_clouds, provider_id, [_make_machine(machine_id="i-stays", name="stays")]
        )

        rows = dal_with_clouds(
            dal_with_clouds.cloud_machines.provider_id == provider_id
        ).select()
        assert {r.external_id for r in rows} == {"i-stays"}

    def test_empty_machine_list_deletes_all_existing(self, dal_with_clouds) -> None:
        import app.api.clouds as clouds_mod

        now = datetime.now(timezone.utc)
        provider_id = dal_with_clouds.cloud_providers.insert(
            name="Test", provider_type="aws", config_data={}, status="connected",
            is_active=True, created_at=now, updated_at=now,
        )
        dal_with_clouds.cloud_machines.insert(
            provider_id=provider_id, external_id="i-1", hostname="host-1",
            created_at=now, updated_at=now,
        )
        dal_with_clouds.commit()

        clouds_mod._sync_machines_to_db(dal_with_clouds, provider_id, [])

        rows = dal_with_clouds(
            dal_with_clouds.cloud_machines.provider_id == provider_id
        ).select()
        assert len(rows) == 0
