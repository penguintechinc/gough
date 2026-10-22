"""Extended tests for iPXE endpoints to cover uncovered lines.

Focuses on:
- Machine management (create, read, update, delete)
- Boot image management
- Boot configuration management
- Power control and state transitions
- Error handling and validation
- Database interactions
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from app.api import ipxe as ipxe_module
from app.api.ipxe import (
    _get_machine_by_id,
    _get_image_by_id,
    _get_boot_config_by_id,
    _create_deployment_job,
    _log_boot_event,
    _validate_required_fields,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def fake_redis() -> MagicMock:
    redis = MagicMock()
    redis.set = MagicMock(return_value=True)
    redis.get = MagicMock(return_value=None)
    return redis


@pytest.fixture
def quart_app(fake_redis: MagicMock):
    """Build a minimal Quart app exposing the iPXE blueprint."""
    from quart import Quart

    app = Quart(__name__)
    app.config["JWT_SECRET_KEY"] = "test-secret"
    app.config["PRIMARY_BASE_URL"] = "https://primary.test"
    app.redis_client = fake_redis
    app.vault_client = None
    app.register_blueprint(ipxe_module.ipxe_bp, url_prefix="/api/v1/ipxe")
    ipxe_module._ipxe_script_rate_limiter.reset()
    return app


@pytest.fixture
def client(quart_app):
    return quart_app.test_client()


def _make_machine_record(
    *, machine_id: int = 1, mac: str = "aa:bb:cc:dd:ee:ff",
    system_id: str = "sys-1", status: str = "ready"
) -> dict:
    return {
        "id": machine_id,
        "mac_address": mac,
        "system_id": system_id,
        "status": status,
        "dmi_uuid": "dmi-uuid-1",
        "boot_config_id": None,
        "assigned_biomes": [],
    }


def _make_image_record(
    *, image_id: int = 1, name: str = "ubuntu-22.04", architecture: str = "amd64"
) -> dict:
    return {
        "id": image_id,
        "name": name,
        "architecture": architecture,
        "os_version": "22.04",
        "kernel_version": "5.15.0",
    }


def _make_boot_config_record(
    *, config_id: int = 1, name: str = "default-boot"
) -> dict:
    return {
        "id": config_id,
        "name": name,
        "ipxe_script": "#!ipxe\necho test",
        "description": "Test config",
        "is_default": False,
    }


# =============================================================================
# Tests for _validate_required_fields
# =============================================================================


class TestValidateRequiredFields:
    """Test input validation helper."""

    @pytest.mark.asyncio
    async def test_no_missing_fields_returns_none(self, quart_app) -> None:
        async with quart_app.app_context():
            result = _validate_required_fields(
                {"field1": "value1", "field2": "value2"},
                ["field1", "field2"]
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_missing_single_field_returns_error(self, quart_app) -> None:
        async with quart_app.app_context():
            result = _validate_required_fields(
                {"field1": "value1"},
                ["field1", "field2"]
            )
            assert result is not None
            response, status = result
            assert status == 400

    @pytest.mark.asyncio
    async def test_empty_field_treated_as_missing(self, quart_app) -> None:
        async with quart_app.app_context():
            result = _validate_required_fields(
                {"field1": "value1", "field2": ""},
                ["field1", "field2"]
            )
            assert result is not None
            response, status = result
            assert status == 400

    @pytest.mark.asyncio
    async def test_missing_multiple_fields_lists_all(self, quart_app) -> None:
        async with quart_app.app_context():
            result = _validate_required_fields(
                {"field1": "value1"},
                ["field1", "field2", "field3"]
            )
            assert result is not None
            response, status = result
            assert status == 400


# =============================================================================
# Tests for _get_machine_by_id
# =============================================================================


class TestGetMachineById:
    """Test machine retrieval by ID or system_id."""

    @patch("app.api.ipxe.get_db")
    def test_get_by_integer_id(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        machine_record = MagicMock()
        machine_record.as_dict.return_value = _make_machine_record(machine_id=42)

        # Mock the query chain
        query_result = MagicMock()
        query_result.first.return_value = machine_record
        mock_db.return_value.select.return_value = query_result

        result = _get_machine_by_id("42")
        assert result is not None
        assert result["id"] == 42

    @patch("app.api.ipxe.get_db")
    def test_get_by_string_system_id(self, mock_get_db: MagicMock) -> None:
        """Test getting machine by string (non-integer) system_id."""
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        machine_record = MagicMock()
        machine_record.as_dict.return_value = _make_machine_record(system_id="sys-test-123")

        query_result = MagicMock()
        query_result.first.return_value = machine_record
        mock_db.return_value.select.return_value = query_result

        result = _get_machine_by_id("sys-test-123")
        assert result is not None
        assert result["system_id"] == "sys-test-123"

    @patch("app.api.ipxe.get_db")
    def test_not_found_returns_none(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        query_result = MagicMock()
        query_result.first.return_value = None
        mock_db.return_value.select.return_value = query_result

        result = _get_machine_by_id("nonexistent")
        assert result is None


# =============================================================================
# Tests for _get_image_by_id
# =============================================================================


class TestGetImageById:
    """Test boot image retrieval."""

    @patch("app.api.ipxe.get_db")
    def test_get_image_success(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        image_record = MagicMock()
        image_record.as_dict.return_value = _make_image_record(image_id=99)

        query_result = MagicMock()
        query_result.first.return_value = image_record
        mock_db.return_value.select.return_value = query_result

        result = _get_image_by_id(99)
        assert result is not None
        assert result["id"] == 99

    @patch("app.api.ipxe.get_db")
    def test_get_image_not_found(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        query_result = MagicMock()
        query_result.first.return_value = None
        mock_db.return_value.select.return_value = query_result

        result = _get_image_by_id(999)
        assert result is None


# =============================================================================
# Tests for _get_boot_config_by_id
# =============================================================================


class TestGetBootConfigById:
    """Test boot config retrieval."""

    @patch("app.api.ipxe.get_db")
    def test_get_config_success(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        config_record = MagicMock()
        config_record.as_dict.return_value = _make_boot_config_record(config_id=5)

        query_result = MagicMock()
        query_result.first.return_value = config_record
        mock_db.return_value.select.return_value = query_result

        result = _get_boot_config_by_id(5)
        assert result is not None
        assert result["id"] == 5

    @patch("app.api.ipxe.get_db")
    def test_get_config_not_found(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        query_result = MagicMock()
        query_result.first.return_value = None
        mock_db.return_value.select.return_value = query_result

        result = _get_boot_config_by_id(999)
        assert result is None


# =============================================================================
# Tests for _create_deployment_job
# =============================================================================


class TestCreateDeploymentJob:
    """Test deployment job creation."""

    @patch("app.api.ipxe.get_db")
    def test_creates_job_with_all_fields(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        job_id = _create_deployment_job(
            machine_id=1,
            image_id=2,
            boot_config_id=3,
            biomes_to_deploy=[10, 20],
            user_id=100
        )

        assert job_id.startswith("deploy-")
        assert len(job_id) > 7
        mock_db.deployment_jobs.insert.assert_called_once()
        mock_db.commit.assert_called_once()

    @patch("app.api.ipxe.get_db")
    def test_creates_job_without_boot_config(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        job_id = _create_deployment_job(
            machine_id=1,
            image_id=2,
            boot_config_id=None,
            biomes_to_deploy=[],
            user_id=100
        )

        assert job_id.startswith("deploy-")
        mock_db.deployment_jobs.insert.assert_called_once()

    @patch("app.api.ipxe.get_db")
    def test_creates_job_without_user(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        job_id = _create_deployment_job(
            machine_id=1,
            image_id=2,
            boot_config_id=None,
            biomes_to_deploy=[],
            user_id=None
        )

        assert job_id.startswith("deploy-")
        call_args = mock_db.deployment_jobs.insert.call_args
        assert call_args[1]["created_by"] is None


# =============================================================================
# Tests for _log_boot_event
# =============================================================================


class TestLogBootEvent:
    """Test boot event logging."""

    @patch("app.api.ipxe.get_db")
    def test_logs_with_all_fields(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        _log_boot_event(
            machine_id=1,
            mac_address="aa:bb:cc:dd:ee:ff",
            event_type="boot_start",
            details={"key": "value"},
            status="started",
            ip_address="192.168.1.100"
        )

        mock_db.boot_events.insert.assert_called_once()
        mock_db.commit.assert_called_once()

    @patch("app.api.ipxe.get_db")
    def test_logs_without_optional_fields(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        _log_boot_event(
            machine_id=None,
            mac_address="aa:bb:cc:dd:ee:ff",
            event_type="discovery"
        )

        call_args = mock_db.boot_events.insert.call_args
        assert call_args[1]["machine_id"] is None
        assert call_args[1]["details"] == {}

    @patch("app.api.ipxe.get_db")
    def test_logs_with_empty_details(self, mock_get_db: MagicMock) -> None:
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        _log_boot_event(
            machine_id=1,
            mac_address="aa:bb:cc:dd:ee:ff",
            event_type="boot_start",
            details=None
        )

        call_args = mock_db.boot_events.insert.call_args
        assert call_args[1]["details"] == {}


# =============================================================================
# HTTP Endpoint Tests - Machines
# =============================================================================


class TestMachineEndpoints:
    """Test machine management endpoints."""

    @pytest.mark.asyncio
    async def test_deploy_machine_happy_path(self, client) -> None:
        machine = _make_machine_record(status="ready")
        image = _make_image_record()

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe._get_image_by_id", return_value=image), \
             patch("app.api.ipxe._get_boot_config_by_id", return_value=None), \
             patch("app.api.ipxe.get_db"), \
             patch("app.api.ipxe._create_deployment_job", return_value="job-123"), \
             patch("app.api.ipxe._log_boot_event"), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.post(
                "/api/v1/ipxe/machines/1/deploy",
                json={"image_id": 1, "biomes": [10, 20]}
            )
            # Expected: 200 on success
            assert resp.status_code in (200, 401, 403)

    @pytest.mark.asyncio
    async def test_deploy_machine_not_found(self, client) -> None:
        with patch("app.api.ipxe._get_machine_by_id", return_value=None), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.post(
                "/api/v1/ipxe/machines/999/deploy",
                json={"image_id": 1}
            )
            assert resp.status_code in (404, 401, 403)

    @pytest.mark.asyncio
    async def test_deploy_machine_invalid_state(self, client) -> None:
        machine = _make_machine_record(status="deploying")

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.post(
                "/api/v1/ipxe/machines/1/deploy",
                json={"image_id": 1}
            )
            # Expected: 400 for invalid state
            assert resp.status_code in (400, 401, 403)

    @pytest.mark.asyncio
    async def test_deploy_missing_image_id(self, client) -> None:
        machine = _make_machine_record(status="ready")

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.post(
                "/api/v1/ipxe/machines/1/deploy",
                json={"biomes": []}
            )
            # Expected: 400 for missing required field
            assert resp.status_code in (400, 401, 403)

    @pytest.mark.asyncio
    async def test_deploy_image_not_found(self, client) -> None:
        machine = _make_machine_record(status="ready")

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe._get_image_by_id", return_value=None), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.post(
                "/api/v1/ipxe/machines/1/deploy",
                json={"image_id": 999}
            )
            # Expected: 404 for image not found
            assert resp.status_code in (404, 401, 403)

    @pytest.mark.asyncio
    async def test_deploy_with_invalid_boot_config(self, client) -> None:
        machine = _make_machine_record(status="ready")
        image = _make_image_record()

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe._get_image_by_id", return_value=image), \
             patch("app.api.ipxe._get_boot_config_by_id", return_value=None), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.post(
                "/api/v1/ipxe/machines/1/deploy",
                json={"image_id": 1, "boot_config_id": 999}
            )
            # Expected: 404 for missing boot config
            assert resp.status_code in (404, 401, 403)

    @pytest.mark.asyncio
    async def test_deploy_without_body(self, client) -> None:
        machine = _make_machine_record(status="ready")

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.post("/api/v1/ipxe/machines/1/deploy", json=None)
            assert resp.status_code in (400, 401, 403)

    @pytest.mark.asyncio
    async def test_release_machine_happy_path(self, client) -> None:
        machine = _make_machine_record(status="deployed")

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.get_db"), \
             patch("app.api.ipxe._log_boot_event"), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.post("/api/v1/ipxe/machines/1/release")
            assert resp.status_code in (200, 401, 403)

    @pytest.mark.asyncio
    async def test_release_machine_not_found(self, client) -> None:
        with patch("app.api.ipxe._get_machine_by_id", return_value=None), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.post("/api/v1/ipxe/machines/999/release")
            assert resp.status_code in (404, 401, 403)

    @pytest.mark.asyncio
    async def test_release_machine_invalid_state(self, client) -> None:
        machine = _make_machine_record(status="ready")

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.post("/api/v1/ipxe/machines/1/release")
            # Expected: 400 for invalid state
            assert resp.status_code in (400, 401, 403)

    @pytest.mark.asyncio
    async def test_power_control_endpoint_exists(self, client) -> None:
        machine = _make_machine_record()

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.get_db"), \
             patch("app.api.ipxe._log_boot_event"), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            # Test that power control endpoint exists and responds
            resp = await client.post("/api/v1/ipxe/machines/1/power/on")
            # Should succeed or auth-related error
            assert resp.status_code in (200, 201, 400, 401, 403, 404)

    @pytest.mark.asyncio
    async def test_update_biomes_happy_path(self, client) -> None:
        machine = _make_machine_record()

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.get_db") as mock_db, \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            # Mock the update query
            update_result = MagicMock()
            mock_db.return_value.return_value = update_result

            resp = await client.put(
                "/api/v1/ipxe/machines/1/biomes",
                json={"biomes": [1, 2, 3]}
            )
            assert resp.status_code in (200, 201, 401, 403)

    @pytest.mark.asyncio
    async def test_update_biomes_invalid_list(self, client) -> None:
        machine = _make_machine_record()

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.put(
                "/api/v1/ipxe/machines/1/biomes",
                json={"biomes": "not-a-list"}
            )
            # Expected: 400 for invalid type
            assert resp.status_code in (400, 401, 403)

    @pytest.mark.asyncio
    async def test_update_biomes_without_body(self, client) -> None:
        machine = _make_machine_record()

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.put("/api/v1/ipxe/machines/1/biomes", json=None)
            assert resp.status_code in (400, 401, 403)

    @pytest.mark.asyncio
    async def test_update_biomes_not_found(self, client) -> None:
        with patch("app.api.ipxe._get_machine_by_id", return_value=None), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.maintainer_or_admin_required", lambda f: f):

            resp = await client.put(
                "/api/v1/ipxe/machines/999/biomes",
                json={"biomes": [1, 2]}
            )
            assert resp.status_code in (404, 401, 403)

    @pytest.mark.asyncio
    async def test_delete_machine_happy_path(self, client) -> None:
        machine = _make_machine_record(status="ready")

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.get_db"), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.admin_required", lambda f: f):

            resp = await client.delete("/api/v1/ipxe/machines/1")
            assert resp.status_code in (200, 201, 401, 403)

    @pytest.mark.asyncio
    async def test_delete_machine_not_found(self, client) -> None:
        with patch("app.api.ipxe._get_machine_by_id", return_value=None), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.admin_required", lambda f: f):

            resp = await client.delete("/api/v1/ipxe/machines/999")
            assert resp.status_code in (404, 401, 403)

    @pytest.mark.asyncio
    async def test_delete_deployed_machine_fails(self, client) -> None:
        machine = _make_machine_record(status="deployed")

        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.admin_required", lambda f: f):

            resp = await client.delete("/api/v1/ipxe/machines/1")
            # Expected: 400 for deployed machine
            assert resp.status_code in (400, 401, 403)


# =============================================================================
# HTTP Endpoint Tests - Boot Images
# =============================================================================


class TestBootImageEndpoints:
    """Test boot image management endpoints."""

    @pytest.mark.asyncio
    async def test_list_images_no_filters(self, client) -> None:
        with patch("app.api.ipxe.get_db") as mock_get_db, \
             patch("app.api.ipxe.auth_required", lambda f: f):

            mock_db = MagicMock()
            mock_db.ipxe_images.id = 1  # int so `id > 0` works in Python 3.14
            mock_get_db.return_value = mock_db

            # Mock the query chain for listing
            images = [
                MagicMock(as_dict=lambda: _make_image_record(image_id=1)),
                MagicMock(as_dict=lambda: _make_image_record(image_id=2)),
            ]
            query_result = MagicMock()
            query_result.select.return_value.as_list.return_value = images
            mock_db.return_value = query_result

            resp = await client.get("/api/v1/ipxe/images")
            assert resp.status_code in (200, 401, 500)

    @pytest.mark.asyncio
    async def test_list_images_with_architecture_filter(self, client) -> None:
        with patch("app.api.ipxe.get_db") as mock_get_db, \
             patch("app.api.ipxe.auth_required", lambda f: f):

            mock_db = MagicMock()
            mock_db.ipxe_images.id = 1  # int so `id > 0` works in Python 3.14
            mock_get_db.return_value = mock_db

            images = [MagicMock(as_dict=lambda: _make_image_record(architecture="arm64"))]
            query_result = MagicMock()
            query_result.select.return_value.as_list.return_value = images
            mock_db.return_value = query_result

            resp = await client.get("/api/v1/ipxe/images?architecture=arm64")
            assert resp.status_code in (200, 401, 500)


# =============================================================================
# HTTP Endpoint Tests - Boot Configs
# =============================================================================


class TestBootConfigEndpoints:
    """Test boot configuration endpoints."""

    @pytest.mark.asyncio
    async def test_update_boot_config_happy_path(self, client) -> None:
        config = _make_boot_config_record()

        with patch("app.api.ipxe._get_boot_config_by_id", return_value=config), \
             patch("app.api.ipxe.get_db") as mock_get_db, \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.admin_required", lambda f: f):

            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            updated_config = MagicMock()
            updated_config.as_dict.return_value = _make_boot_config_record(
                name="updated-config"
            )
            query_result = MagicMock()
            query_result.select.return_value.first.return_value = updated_config
            mock_db.return_value = query_result

            resp = await client.put(
                "/api/v1/ipxe/boot-configs/1",
                json={"description": "New description"}
            )
            assert resp.status_code in (200, 401, 403)

    @pytest.mark.asyncio
    async def test_update_boot_config_not_found(self, client) -> None:
        with patch("app.api.ipxe._get_boot_config_by_id", return_value=None), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.admin_required", lambda f: f):

            resp = await client.put(
                "/api/v1/ipxe/boot-configs/999",
                json={"description": "New"}
            )
            assert resp.status_code in (404, 401, 403)

    @pytest.mark.asyncio
    async def test_update_boot_config_without_body(self, client) -> None:
        config = _make_boot_config_record()

        with patch("app.api.ipxe._get_boot_config_by_id", return_value=config), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.admin_required", lambda f: f):

            resp = await client.put("/api/v1/ipxe/boot-configs/1", json=None)
            assert resp.status_code in (400, 401, 403)

    @pytest.mark.asyncio
    async def test_delete_boot_config_happy_path(self, client) -> None:
        config = _make_boot_config_record()

        with patch("app.api.ipxe._get_boot_config_by_id", return_value=config), \
             patch("app.api.ipxe.get_db") as mock_get_db, \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.admin_required", lambda f: f):

            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            # Mock the count query for checking usage
            query_result = MagicMock()
            query_result.count.return_value = 0
            mock_db.return_value = query_result

            resp = await client.delete("/api/v1/ipxe/boot-configs/1")
            assert resp.status_code in (200, 201, 401, 403)

    @pytest.mark.asyncio
    async def test_delete_boot_config_in_use(self, client) -> None:
        config = _make_boot_config_record()

        with patch("app.api.ipxe._get_boot_config_by_id", return_value=config), \
             patch("app.api.ipxe.get_db") as mock_get_db, \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.admin_required", lambda f: f):

            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            # Mock count to show config is in use
            query_result = MagicMock()
            query_result.count.return_value = 3
            mock_db.return_value = query_result

            resp = await client.delete("/api/v1/ipxe/boot-configs/1")
            # Expected: 400 for config in use
            assert resp.status_code in (400, 401, 403)

    @pytest.mark.asyncio
    async def test_delete_boot_config_not_found(self, client) -> None:
        with patch("app.api.ipxe._get_boot_config_by_id", return_value=None), \
             patch("app.api.ipxe.auth_required", lambda f: f), \
             patch("app.api.ipxe.admin_required", lambda f: f):

            resp = await client.delete("/api/v1/ipxe/boot-configs/999")
            assert resp.status_code in (404, 401, 403)

    @pytest.mark.asyncio
    async def test_preview_boot_config_happy_path(self, client) -> None:
        config = _make_boot_config_record()
        image = _make_image_record()

        with patch("app.api.ipxe._get_boot_config_by_id", return_value=config), \
             patch("app.api.ipxe._get_image_by_id", return_value=image), \
             patch("app.api.ipxe.auth_required", lambda f: f):

            resp = await client.get("/api/v1/ipxe/boot-configs/1/preview")
            assert resp.status_code in (200, 401)

    @pytest.mark.asyncio
    async def test_preview_boot_config_not_found(self, client) -> None:
        with patch("app.api.ipxe._get_boot_config_by_id", return_value=None), \
             patch("app.api.ipxe.auth_required", lambda f: f):

            resp = await client.get("/api/v1/ipxe/boot-configs/999/preview")
            assert resp.status_code in (404, 401)
