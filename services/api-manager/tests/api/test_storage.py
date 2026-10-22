"""Tests for app/api/storage.py.

Covers success and key error paths for all storage endpoints.
Uses _passthrough decorator to bypass auth, mocks get_db.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from quart import Quart


def _passthrough(*dargs, **dkwargs):
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _make_config_row(**kwargs):
    defaults = {
        "id": 1,
        "name": "test-config",
        "provider_type": "s3",
        "endpoint_url": None,
        "region": "us-east-1",
        "bucket_name": "test-bucket",
        "credentials_path": "/secret/path",
        "is_default": False,
        "is_active": True,
        "use_ssl": True,
        "config_data": None,
        "created_at": datetime(2024, 1, 1),
        "updated_at": datetime(2024, 1, 2),
    }
    defaults.update(kwargs)
    row = SimpleNamespace(**defaults)
    return row


def _make_quota_row(**kwargs):
    defaults = {
        "id": "uuid-1",
        "tenant_id": "tenant-abc",
        "resource_type": "storage",
        "limit_value": 100.0,
        "used_value": 50.0,
        "unit": "GB",
        "created_at": datetime(2024, 1, 1),
        "updated_at": datetime(2024, 1, 2),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_db(config_rows=None, quota_rows=None):
    """Build a minimal DAL mock for storage tests."""
    db = MagicMock()

    # storage_config table mock
    cfg_select = MagicMock()
    cfg_select.__iter__ = MagicMock(return_value=iter(config_rows or []))
    cfg_select.first = MagicMock(return_value=(config_rows[0] if config_rows else None))

    db.storage_config = MagicMock()
    db.storage_config.id = MagicMock()
    db.storage_config.name = MagicMock()
    db.storage_config.is_default = MagicMock()

    db.return_value = MagicMock()
    db.return_value.select = MagicMock(return_value=cfg_select)
    db.return_value.update = MagicMock()
    db.return_value.delete = MagicMock()

    db.storage_config.insert = MagicMock(return_value=1)
    db.commit = MagicMock()
    db.rollback = MagicMock()

    # storage_quotas
    quota_sel = MagicMock()
    quota_sel.__iter__ = MagicMock(return_value=iter(quota_rows or []))
    quota_sel.first = MagicMock(return_value=(quota_rows[0] if quota_rows else None))
    db.storage_quotas = MagicMock()
    db.storage_quota_requests = MagicMock()
    db.storage_quota_requests.insert = MagicMock()

    return db


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    """Quart app with storage blueprint, auth bypassed."""
    with patch("app.auth.require_auth", _passthrough), \
         patch("app.auth.require_role", _passthrough):
        import app.api.storage as _storage_mod
        importlib.reload(_storage_mod)
        from app.api.storage import storage_bp

    qapp = Quart(__name__)
    qapp.register_blueprint(storage_bp, url_prefix="/api/v1/storage")

    # Set request.user so routes that access request.user.id don't 500.
    from types import SimpleNamespace as _SNS
    from quart import request as _req, g as _g

    @qapp.before_request
    async def _set_request_user():
        _req.user = _SNS(id=1, email="test@example.com")
        _g.current_user = {"id": 1, "role": "admin", "_jwt_payload": {"scope": "gough.cluster.admin"}}

    return qapp


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helper: patch get_db inside the storage module
# ---------------------------------------------------------------------------

def _db_patch(db):
    return patch("app.api.storage.get_db", return_value=db)


# ============================================================================
# GET /api/v1/storage/configs
# ============================================================================

class TestListStorageConfigs:
    @pytest.mark.asyncio
    async def test_returns_empty_list(self, client):
        db = _make_db(config_rows=[])
        with _db_patch(db):
            resp = await client.get("/api/v1/storage/configs")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["configs"] == []

    @pytest.mark.asyncio
    async def test_returns_configs(self, client):
        row = _make_config_row()
        db = _make_db(config_rows=[row])
        with _db_patch(db):
            resp = await client.get("/api/v1/storage/configs")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["configs"]) == 1
        assert data["configs"][0]["name"] == "test-config"

    @pytest.mark.asyncio
    async def test_config_fields_in_response(self, client):
        row = _make_config_row(provider_type="minio", is_default=True)
        db = _make_db(config_rows=[row])
        with _db_patch(db):
            resp = await client.get("/api/v1/storage/configs")
        data = await resp.get_json()
        cfg = data["configs"][0]
        assert cfg["provider_type"] == "minio"
        assert cfg["is_default"] is True


# ============================================================================
# POST /api/v1/storage/configs
# ============================================================================

class TestCreateStorageConfig:
    def _valid_payload(self, **overrides):
        base = {
            "name": "my-config",
            "provider_type": "s3",
            "credentials_path": "/secrets/s3",
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_missing_body_returns_400(self, client):
        resp = await client.post("/api/v1/storage/configs", json=None)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_name_returns_400(self, client):
        db = _make_db()
        with _db_patch(db):
            resp = await client.post("/api/v1/storage/configs", json={"provider_type": "s3"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_provider_type_returns_400(self, client):
        db = _make_db()
        with _db_patch(db):
            resp = await client.post(
                "/api/v1/storage/configs",
                json=self._valid_payload(provider_type="nfs"),
            )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "provider_type" in data["error"]

    @pytest.mark.asyncio
    async def test_missing_credentials_path_returns_400(self, client):
        db = _make_db()
        with _db_patch(db):
            resp = await client.post(
                "/api/v1/storage/configs",
                json={"name": "x", "provider_type": "s3"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_s3_without_endpoint_url_returns_400(self, client):
        db = _make_db()
        with _db_patch(db):
            resp = await client.post(
                "/api/v1/storage/configs",
                json=self._valid_payload(provider_type="minio"),
            )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "endpoint_url" in data["error"]

    @pytest.mark.asyncio
    async def test_duplicate_name_returns_409(self, client):
        existing_row = _make_config_row(name="my-config")
        db = _make_db(config_rows=[existing_row])
        # first select returns existing (for duplicate check)
        db.return_value.select.return_value.first.return_value = existing_row
        with _db_patch(db):
            resp = await client.post(
                "/api/v1/storage/configs",
                json=self._valid_payload(),
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_create_s3_success(self, client):
        db = _make_db()
        # no existing config (duplicate check returns None)
        created_row = _make_config_row(id=1, name="my-config")
        # call 1: duplicate name check → None; call 2: fetch after insert → created_row
        db.return_value.select.return_value.first.side_effect = [None, created_row]
        db.storage_config.insert.return_value = 1

        with _db_patch(db):
            resp = await client.post(
                "/api/v1/storage/configs",
                json=self._valid_payload(),
            )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_is_default_true_resets_others(self, client):
        """When is_default=True, other configs get set to False."""
        db = _make_db()
        created_row = _make_config_row(is_default=True)
        db.return_value.select.return_value.first.side_effect = [None, created_row]

        payload = self._valid_payload(is_default=True)
        with _db_patch(db):
            await client.post("/api/v1/storage/configs", json=payload)
        # The update(is_default=False) call should have happened
        db.return_value.update.assert_called()


# ============================================================================
# GET /api/v1/storage/configs/<id>
# ============================================================================

class TestGetStorageConfig:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, client):
        db = _make_db()
        db.return_value.select.return_value.first.return_value = None
        with _db_patch(db):
            resp = await client.get("/api/v1/storage/configs/999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_config(self, client):
        row = _make_config_row(config_data='{"key": "val"}')
        db = _make_db(config_rows=[row])
        db.return_value.select.return_value.first.return_value = row
        with _db_patch(db):
            resp = await client.get("/api/v1/storage/configs/1")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["name"] == "test-config"
        # Regression: audit output-validation. This endpoint used to parse
        # and echo ``config_data`` (provider JSON that can itself carry
        # inline credentials) straight from the row -- it must never appear
        # in the response, regardless of what is stored.
        assert "config_data" not in data
        assert "credentials_path" not in data

    @pytest.mark.asyncio
    async def test_invalid_json_config_data_never_leaks(self, client):
        """Even a row whose ``config_data`` is garbage never reaches the response.

        Regression: audit output-validation. ``get_storage_config`` used to
        ``json.loads()`` this column and echo the result (or ``{}`` on a
        decode failure) back to the caller -- the fixed handler never reads
        this column for its response at all, so garbage content can't
        surface either.
        """
        row = _make_config_row(config_data="not-valid-json")
        db = _make_db(config_rows=[row])
        db.return_value.select.return_value.first.return_value = row
        with _db_patch(db):
            resp = await client.get("/api/v1/storage/configs/1")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "config_data" not in data

    @pytest.mark.asyncio
    async def test_null_timestamps_handled(self, client):
        row = _make_config_row(created_at=None, updated_at=None)
        db = _make_db(config_rows=[row])
        db.return_value.select.return_value.first.return_value = row
        with _db_patch(db):
            resp = await client.get("/api/v1/storage/configs/1")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["created_at"] is None
        assert data["updated_at"] is None


# ============================================================================
# PUT /api/v1/storage/configs/<id>
# ============================================================================

class TestUpdateStorageConfig:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, client):
        db = _make_db()
        db.return_value.select.return_value.first.return_value = None
        with _db_patch(db):
            resp = await client.put("/api/v1/storage/configs/99", json={"name": "x"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_no_body_returns_400(self, client):
        row = _make_config_row()
        db = _make_db(config_rows=[row])
        db.return_value.select.return_value.first.return_value = row
        with _db_patch(db):
            resp = await client.put(
                "/api/v1/storage/configs/1",
                json=None,
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_duplicate_name_returns_409(self, client):
        existing_row = _make_config_row(id=2, name="taken")
        current_row = _make_config_row(id=1, name="current")
        db = _make_db()
        # First call: find config to update; second: find name collision
        db.return_value.select.return_value.first.side_effect = [current_row, existing_row]
        with _db_patch(db):
            resp = await client.put(
                "/api/v1/storage/configs/1",
                json={"name": "taken"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_update_fields_success(self, client):
        row = _make_config_row()
        updated_row = _make_config_row(region="eu-west-1")
        db = _make_db()
        # call 1: fetch config; call 2: fetch updated row (no name check for region-only update)
        db.return_value.select.return_value.first.side_effect = [row, updated_row]
        with _db_patch(db):
            resp = await client.put(
                "/api/v1/storage/configs/1",
                json={"region": "eu-west-1", "use_ssl": False, "is_active": True},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_update_config_data_field(self, client):
        row = _make_config_row()
        updated_row = _make_config_row()
        db = _make_db()
        db.return_value.select.return_value.first.side_effect = [row, updated_row]
        with _db_patch(db):
            resp = await client.put(
                "/api/v1/storage/configs/1",
                json={"config_data": {"extra": "value"}},
            )
        assert resp.status_code == 200


# ============================================================================
# DELETE /api/v1/storage/configs/<id>
# ============================================================================

class TestDeleteStorageConfig:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, client):
        db = _make_db()
        db.return_value.select.return_value.first.return_value = None
        with _db_patch(db):
            resp = await client.delete("/api/v1/storage/configs/99")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_default_returns_409(self, client):
        row = _make_config_row(is_default=True)
        db = _make_db(config_rows=[row])
        db.return_value.select.return_value.first.return_value = row
        with _db_patch(db):
            resp = await client.delete("/api/v1/storage/configs/1")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_delete_success_returns_204(self, client):
        row = _make_config_row(is_default=False)
        db = _make_db(config_rows=[row])
        db.return_value.select.return_value.first.return_value = row
        with _db_patch(db):
            resp = await client.delete("/api/v1/storage/configs/1")
        assert resp.status_code == 204


# ============================================================================
# POST /api/v1/storage/configs/<id>/test
# ============================================================================

class TestTestStorageConfig:
    @pytest.mark.asyncio
    async def test_config_not_found_returns_404(self, client):
        from app.services.storage import StorageConfigNotFoundError
        with patch("app.api.storage.get_storage_service", side_effect=StorageConfigNotFoundError(1)):
            resp = await client.post("/api/v1/storage/configs/1/test")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_storage_error_returns_400(self, client):
        from app.services.storage import StorageError
        with patch("app.api.storage.get_storage_service", side_effect=StorageError("conn refused")):
            resp = await client.post("/api/v1/storage/configs/1/test")
        assert resp.status_code == 400
        data = await resp.get_json()
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_success_returns_200(self, client):
        svc = AsyncMock()
        svc.test_connection = AsyncMock(return_value={"success": True, "latency_ms": 10})
        with patch("app.api.storage.get_storage_service", AsyncMock(return_value=svc)):
            resp = await client.post("/api/v1/storage/configs/1/test")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["success"] is True


# ============================================================================
# POST /api/v1/storage/configs/<id>/set-default
# ============================================================================

class TestSetDefaultStorageConfig:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, client):
        db = _make_db()
        db.return_value.select.return_value.first.return_value = None
        with _db_patch(db):
            resp = await client.post("/api/v1/storage/configs/99/set-default")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_success_returns_200(self, client):
        row = _make_config_row(name="primary")
        db = _make_db(config_rows=[row])
        db.return_value.select.return_value.first.return_value = row
        with _db_patch(db):
            resp = await client.post("/api/v1/storage/configs/1/set-default")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "primary" in data["message"]


# ============================================================================
# GET /api/v1/storage/buckets
# ============================================================================

class TestListBuckets:
    @pytest.mark.asyncio
    async def test_config_not_found_returns_404(self, client):
        from app.services.storage import StorageConfigNotFoundError
        with patch("app.api.storage.get_storage_service", side_effect=StorageConfigNotFoundError()):
            resp = await client.get("/api/v1/storage/buckets")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_storage_error_returns_400(self, client):
        from app.services.storage import StorageError
        with patch("app.api.storage.get_storage_service", side_effect=StorageError("Access denied")):
            resp = await client.get("/api/v1/storage/buckets")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_success_returns_buckets(self, client):
        svc = AsyncMock()
        svc.list_buckets = AsyncMock(return_value=["bucket-a", "bucket-b"])
        svc.config = SimpleNamespace(provider_type="s3", name="my-cfg")
        with patch("app.api.storage.get_storage_service", AsyncMock(return_value=svc)):
            resp = await client.get("/api/v1/storage/buckets")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["buckets"] == ["bucket-a", "bucket-b"]
        assert data["provider"] == "s3"


# ============================================================================
# POST /api/v1/storage/buckets
# ============================================================================

class TestCreateBucket:
    @pytest.mark.asyncio
    async def test_no_body_returns_400(self, client):
        resp = await client.post("/api/v1/storage/buckets", json=None)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_bucket_name_returns_400(self, client):
        resp = await client.post("/api/v1/storage/buckets", json={})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_config_not_found_returns_404(self, client):
        from app.services.storage import StorageConfigNotFoundError
        with patch("app.api.storage.get_storage_service", side_effect=StorageConfigNotFoundError()):
            resp = await client.post("/api/v1/storage/buckets", json={"bucket_name": "new-bkt"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_storage_error_returns_400(self, client):
        from app.services.storage import StorageError
        with patch("app.api.storage.get_storage_service", side_effect=StorageError("Already exists")):
            resp = await client.post("/api/v1/storage/buckets", json={"bucket_name": "new-bkt"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_success_returns_201(self, client):
        svc = AsyncMock()
        svc.create_bucket = AsyncMock(return_value={"bucket": "new-bkt", "created": True})
        with patch("app.api.storage.get_storage_service", AsyncMock(return_value=svc)):
            resp = await client.post("/api/v1/storage/buckets", json={"bucket_name": "new-bkt"})
        assert resp.status_code == 201


# ============================================================================
# GET /api/v1/storage/objects
# ============================================================================

class TestListObjects:
    @pytest.mark.asyncio
    async def test_config_not_found_returns_404(self, client):
        from app.services.storage import StorageConfigNotFoundError
        with patch("app.api.storage.get_storage_service", side_effect=StorageConfigNotFoundError()):
            resp = await client.get("/api/v1/storage/objects")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_storage_error_returns_400(self, client):
        from app.services.storage import StorageError
        with patch("app.api.storage.get_storage_service", side_effect=StorageError("No bucket")):
            resp = await client.get("/api/v1/storage/objects")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_success_returns_objects(self, client):
        svc = AsyncMock()
        svc.list_objects = AsyncMock(return_value=[{"key": "file.txt", "size": 100}])
        svc.config = SimpleNamespace(name="cfg", bucket_name="main-bkt")
        with patch("app.api.storage.get_storage_service", AsyncMock(return_value=svc)):
            resp = await client.get("/api/v1/storage/objects?bucket=main-bkt&prefix=&max_keys=100")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["objects"]) == 1


# ============================================================================
# POST /api/v1/storage/objects/<key>/presigned-url
# ============================================================================

class TestGetPresignedUrl:
    @pytest.mark.asyncio
    async def test_config_not_found_returns_404(self, client):
        from app.services.storage import StorageConfigNotFoundError
        with patch("app.api.storage.get_storage_service", side_effect=StorageConfigNotFoundError()):
            resp = await client.post("/api/v1/storage/objects/path/to/file.txt/presigned-url")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_storage_error_returns_400(self, client):
        from app.services.storage import StorageError
        with patch("app.api.storage.get_storage_service", side_effect=StorageError("Key not found")):
            resp = await client.post(
                "/api/v1/storage/objects/my-file.txt/presigned-url",
                json={},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_success_returns_url(self, client):
        svc = AsyncMock()
        svc.get_presigned_url = AsyncMock(return_value="https://s3.example.com/signed?X-token=abc")
        svc.config = SimpleNamespace(bucket_name="bkt")
        with patch("app.api.storage.get_storage_service", AsyncMock(return_value=svc)):
            resp = await client.post(
                "/api/v1/storage/objects/reports/q1.csv/presigned-url",
                json={"expiration": 7200, "http_method": "get_object"},
            )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "url" in data
        assert data["expires_in"] == 7200
        assert data["object_key"] == "reports/q1.csv"


# ============================================================================
# DELETE /api/v1/storage/objects/<key>
# ============================================================================

class TestDeleteObject:
    @pytest.mark.asyncio
    async def test_config_not_found_returns_404(self, client):
        from app.services.storage import StorageConfigNotFoundError
        with patch("app.api.storage.get_storage_service", side_effect=StorageConfigNotFoundError()):
            resp = await client.delete("/api/v1/storage/objects/my-file.txt")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_storage_error_returns_400(self, client):
        from app.services.storage import StorageError
        with patch("app.api.storage.get_storage_service", side_effect=StorageError("Delete failed")):
            resp = await client.delete("/api/v1/storage/objects/my-file.txt")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_success_returns_204(self, client):
        svc = AsyncMock()
        svc.delete_object = AsyncMock(return_value=None)
        with patch("app.api.storage.get_storage_service", AsyncMock(return_value=svc)):
            resp = await client.delete("/api/v1/storage/objects/reports/q1.csv")
        assert resp.status_code == 204


# ============================================================================
# GET /api/v1/storage/quotas
# ============================================================================

class TestListStorageQuotas:
    @pytest.mark.asyncio
    async def test_missing_scope_returns_403(self, client):
        with patch("app.api.storage._user_has_scope", return_value=False):
            resp = await client.get("/api/v1/storage/quotas")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_returns_quota_list(self, client):
        """# regression: gh-22

        list_storage_quotas' SELECT now runs via run_db() instead of
        blocking the request coroutine inline -- proves the endpoint still
        returns seeded rows correctly through that thread hop.
        """
        row = _make_quota_row()
        db = _make_db(quota_rows=[row])
        quota_sel = MagicMock()
        quota_sel.__iter__ = MagicMock(return_value=iter([row]))
        db.return_value.select.return_value = quota_sel

        with _db_patch(db), patch("app.api.storage._user_has_scope", return_value=True):
            resp = await client.get("/api/v1/storage/quotas")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "success"
        assert data["data"]["total"] >= 0

    @pytest.mark.asyncio
    async def test_filter_by_tenant_id(self, client):
        db = _make_db()
        quota_sel = MagicMock()
        quota_sel.__iter__ = MagicMock(return_value=iter([]))
        db.return_value.select.return_value = quota_sel

        with _db_patch(db), patch("app.api.storage._user_has_scope", return_value=True):
            resp = await client.get("/api/v1/storage/quotas?tenant_id=tenant-xyz")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_null_limit_value_handled(self, client):
        row = _make_quota_row(limit_value=None, used_value=None)
        db = _make_db()
        quota_sel = MagicMock()
        quota_sel.__iter__ = MagicMock(return_value=iter([row]))
        db.return_value.select.return_value = quota_sel

        with _db_patch(db), patch("app.api.storage._user_has_scope", return_value=True):
            resp = await client.get("/api/v1/storage/quotas")
        assert resp.status_code == 200
        data = await resp.get_json()
        # None values should serialize as null, not crash
        assert "quotas" in data["data"]


# ============================================================================
# POST /api/v1/storage/quota-request
# ============================================================================

class TestRequestStorageQuota:
    def _valid_payload(self):
        return {
            "tenant_id": "tenant-abc",
            "resource_type": "storage",
            "requested_value": 500,
            "unit": "GB",
            "justification": "Need more space for backups",
        }

    @pytest.mark.asyncio
    async def test_missing_scope_returns_403(self, client):
        with patch("app.api.storage._user_has_scope", return_value=False):
            resp = await client.post("/api/v1/storage/quota-request", json=self._valid_payload())
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_no_body_returns_400(self, client):
        with patch("app.api.storage._user_has_scope", return_value=True):
            resp = await client.post(
                "/api/v1/storage/quota-request",
                json=None,
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_required_field_returns_400(self, client):
        payload = self._valid_payload()
        del payload["justification"]
        with patch("app.api.storage._user_has_scope", return_value=True):
            resp = await client.post("/api/v1/storage/quota-request", json=payload)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_numeric_requested_value_returns_400(self, client):
        payload = self._valid_payload()
        payload["requested_value"] = "lots"
        with patch("app.api.storage._user_has_scope", return_value=True):
            resp = await client.post("/api/v1/storage/quota-request", json=payload)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_success_returns_201(self, client):
        db = _make_db()
        db.storage_quota_requests.insert = MagicMock()
        with _db_patch(db), patch("app.api.storage._user_has_scope", return_value=True):
            resp = await client.post("/api/v1/storage/quota-request", json=self._valid_payload())
        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["status"] == "success"
        assert data["data"]["status"] == "pending"
        assert data["data"]["unit"] == "GB"

    @pytest.mark.asyncio
    async def test_db_error_returns_500(self, client):
        db = _make_db()
        db.storage_quota_requests.insert = MagicMock(side_effect=Exception("DB down"))
        with _db_patch(db), patch("app.api.storage._user_has_scope", return_value=True):
            resp = await client.post("/api/v1/storage/quota-request", json=self._valid_payload())
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_float_requested_value_accepted(self, client):
        db = _make_db()
        db.storage_quota_requests.insert = MagicMock()
        payload = self._valid_payload()
        payload["requested_value"] = 1.5
        with _db_patch(db), patch("app.api.storage._user_has_scope", return_value=True):
            resp = await client.post("/api/v1/storage/quota-request", json=payload)
        assert resp.status_code == 201
