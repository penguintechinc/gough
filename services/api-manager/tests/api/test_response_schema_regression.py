"""Output-validation regression tests (security.md "Output Validation").

Companion audit to ``tests/test_multi_cloud_enabled.py::TestProviderCredentialsNeverLeave``
(the original ``app/api/clouds.py`` credential leak: a ``del row["config"]``
redaction that missed the real column name, ``config_data``, so every
response carried the provider's credentials). This module covers the two
other confirmed sites of the same bug class found in the wider
``app/api/*.py`` output-validation audit:

* ``app/api/storage.py::get_storage_config`` -- used to echo
  ``credentials_path`` and ``config_data`` (both of which can carry inline
  secrets, e.g. a GCS service-account key) straight from the row.
* ``app/api/ipxe.py::update_elder_config`` -- used to echo the just-submitted
  row, including ``api_key`` (the Elder service credential), via
  ``.as_dict()``.

Both are fixed via explicit allow-list projections in ``app/api/_dto.py``.
Also unit-tests ``_dto.project()``/the two serializers directly, and asserts
the exact field set (not just the absence of one name) per the referenced
clouds.py test's own rationale.

# regression: audit output-validation
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from quart import Quart, g

from app.api._dto import (
    ELDER_CONFIG_PUBLIC_FIELDS,
    STORAGE_CONFIG_PUBLIC_FIELDS,
    project,
    serialize_elder_config,
    serialize_storage_config,
)


def _passthrough(*dargs, **dkwargs):
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


# =============================================================================
# Unit tests: app/api/_dto.py
# =============================================================================


class TestProject:
    """The generic allow-list projection helper."""

    def test_projects_plain_dict(self):
        row = {"id": 1, "secret": "s3kr3t", "name": "x"}
        out = project(row, ("id", "name"))
        assert out == {"id": 1, "name": "x"}
        assert "secret" not in out

    def test_projects_row_with_as_dict(self):
        row = MagicMock()
        row.as_dict.return_value = {"id": 1, "secret": "s3kr3t", "name": "x"}
        out = project(row, ("id", "name"))
        assert out == {"id": 1, "name": "x"}
        assert "secret" not in out

    def test_projects_attribute_object_without_as_dict(self):
        row = SimpleNamespace(id=1, secret="s3kr3t", name="x")
        out = project(row, ("id", "name"))
        assert out == {"id": 1, "name": "x"}
        assert "secret" not in out

    def test_missing_fields_are_omitted_not_raised(self):
        row = {"id": 1}
        out = project(row, ("id", "does_not_exist"))
        assert out == {"id": 1}


class TestSerializeStorageConfig:
    """``storage_config`` allow-list -- the sibling of the clouds.py leak."""

    SENSITIVE = ("credentials_path", "config_data")

    def _row(self, **overrides):
        defaults = {
            "id": 1,
            "name": "s3-prod",
            "provider_type": "s3",
            "endpoint_url": None,
            "region": "us-east-1",
            "bucket_name": "prod-bucket",
            "credentials_path": "/vault/secret/storage/s3-prod",
            "is_default": True,
            "is_active": True,
            "use_ssl": True,
            "config_data": '{"aws_access_key_id": "AKIAIOSFODNN7EXAMPLE"}',
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_omits_credentials_path_and_config_data(self):
        out = serialize_storage_config(self._row())
        leaked = [k for k in self.SENSITIVE if k in out]
        assert not leaked, f"credential fields returned: {leaked}"

    def test_exact_field_set(self):
        """A column added to storage_config later must not reach the API
        just by existing on the row -- only fields listed in
        STORAGE_CONFIG_PUBLIC_FIELDS are ever returned."""
        row = self._row(unexpected_future_column="surprise")
        out = serialize_storage_config(row)
        assert set(out) == set(STORAGE_CONFIG_PUBLIC_FIELDS)

    def test_still_useful(self):
        out = serialize_storage_config(self._row())
        assert out["name"] == "s3-prod"
        assert out["provider_type"] == "s3"
        assert out["created_at"] == "2026-01-01T00:00:00+00:00"


class TestSerializeElderConfig:
    """``elder_config`` allow-list -- ``api_key`` must never round-trip."""

    def _row(self, **overrides):
        defaults = {
            "id": 1,
            "name": "default",
            "elder_url": "https://elder.example.com",
            "api_key": "super-secret-elder-key",
            "timeout": 10,
            "max_retries": 3,
            "is_active": True,
            "last_sync_at": None,
            "last_error": None,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_omits_api_key(self):
        out = serialize_elder_config(self._row())
        assert "api_key" not in out

    def test_omits_last_error(self):
        out = serialize_elder_config(self._row(last_error="connection refused to 10.0.0.5"))
        assert "last_error" not in out

    def test_exact_field_set(self):
        out = serialize_elder_config(self._row())
        assert set(out) == set(ELDER_CONFIG_PUBLIC_FIELDS)

    def test_still_useful(self):
        out = serialize_elder_config(self._row())
        assert out["elder_url"] == "https://elder.example.com"
        assert out["is_active"] is True


# =============================================================================
# Integration: GET/POST/PUT /api/v1/storage/configs (app/api/storage.py)
# =============================================================================


def _make_storage_config_row(**kwargs):
    defaults = {
        "id": 1,
        "name": "test-config",
        "provider_type": "s3",
        "endpoint_url": None,
        "region": "us-east-1",
        "bucket_name": "test-bucket",
        "credentials_path": "/vault/secret/storage/test-config",
        "is_default": False,
        "is_active": True,
        "use_ssl": True,
        "config_data": '{"aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}',
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_storage_db(config_row=None):
    db = MagicMock()
    select_result = MagicMock()
    select_result.first = MagicMock(return_value=config_row)
    select_result.__iter__ = MagicMock(return_value=iter([config_row] if config_row else []))
    db.storage_config = MagicMock()
    db.return_value = MagicMock()
    db.return_value.select = MagicMock(return_value=select_result)
    db.commit = MagicMock()
    return db


@pytest.fixture()
def storage_app():
    with patch("app.auth.require_auth", _passthrough), \
         patch("app.auth.require_role", _passthrough):
        import app.api.storage as _storage_mod
        importlib.reload(_storage_mod)
        from app.api.storage import storage_bp

    qapp = Quart(__name__)
    qapp.register_blueprint(storage_bp, url_prefix="/api/v1/storage")

    @qapp.before_request
    async def _set_request_user():
        from quart import request as _req
        _req.user = SimpleNamespace(id=1, email="admin@example.com")

    yield qapp
    # Reload once more so a later test module importing app.api.storage
    # fresh doesn't inherit this module's patched-then-reloaded state.
    importlib.reload(_storage_mod)


@pytest.fixture()
def storage_client(storage_app):
    return storage_app.test_client()


class TestStorageConfigNeverLeaksCredentials:
    """Regression: audit output-validation.

    ``get_storage_config`` used to parse and echo ``config_data`` and echo
    ``credentials_path`` straight from the row -- the same raw-row-echo
    leak class as the fixed ``clouds.py`` provider credential leak.
    """

    SECRETS = ("config_data", "credentials_path")

    @pytest.mark.asyncio
    async def test_get_config_omits_credentials(self, storage_client):
        row = _make_storage_config_row()
        db = _make_storage_db(row)
        with patch("app.api.storage.get_db", return_value=db):
            resp = await storage_client.get("/api/v1/storage/configs/1")
        assert resp.status_code == 200
        body = await resp.get_json()

        leaked = [k for k in self.SECRETS if k in body]
        assert not leaked, f"credential fields returned: {leaked}"
        assert "wJalrXUtnFEMI" not in await resp.get_data(as_text=True)
        assert "/vault/secret/storage/test-config" not in await resp.get_data(as_text=True)
        # The route still has to be useful.
        assert body["name"] == "test-config"
        assert body["provider_type"] == "s3"

    @pytest.mark.asyncio
    async def test_list_configs_omits_credentials(self, storage_client):
        row = _make_storage_config_row()
        db = _make_storage_db(row)
        with patch("app.api.storage.get_db", return_value=db):
            resp = await storage_client.get("/api/v1/storage/configs")
        assert resp.status_code == 200
        body = await resp.get_json()

        assert body["configs"], "seeded config was not listed"
        for cfg in body["configs"]:
            leaked = [k for k in self.SECRETS if k in cfg]
            assert not leaked, f"credential fields returned: {leaked}"

    @pytest.mark.asyncio
    async def test_get_config_is_an_exact_allow_list(self, storage_client):
        """A column added to storage_config later must not reach the API
        just by existing on the row."""
        row = _make_storage_config_row(some_future_secret_column="leak-me-not")
        db = _make_storage_db(row)
        with patch("app.api.storage.get_db", return_value=db):
            resp = await storage_client.get("/api/v1/storage/configs/1")
        body = await resp.get_json()
        assert set(body) == set(STORAGE_CONFIG_PUBLIC_FIELDS)


# =============================================================================
# Integration: PUT /api/v1/ipxe/elder/config (app/api/ipxe.py)
# =============================================================================


@pytest.fixture()
def ipxe_app():
    from app.api import ipxe as ipxe_module

    app = Quart(__name__)
    app.config["JWT_SECRET_KEY"] = "test-secret-key"
    app.redis_client = None
    app.vault_client = None
    app.register_blueprint(ipxe_module.ipxe_bp, url_prefix="/api/v1/ipxe")

    @app.before_request
    async def _inject_admin():
        g.current_user = {"id": 1, "role": "admin", "_jwt_payload": {}}

    return app


@pytest.fixture()
def ipxe_client(ipxe_app):
    return ipxe_app.test_client()


def _make_elder_db(existing_row, created_or_updated_row):
    """``db.elder_config`` mock: first ``.first()`` call is the existence
    check, the second is the post-write refetch this handler always does."""
    db = MagicMock()
    db.tables = ["elder_config"]
    db.elder_config = MagicMock()
    db.elder_config.insert = MagicMock(return_value=99)
    db.return_value = MagicMock()
    db.return_value.select.return_value.first = MagicMock(
        side_effect=[existing_row, created_or_updated_row]
    )
    db.return_value.update = MagicMock()
    db.commit = MagicMock()
    return db


class TestElderConfigNeverLeaksApiKey:
    """Regression: audit output-validation.

    ``update_elder_config`` used to echo the just-submitted row (including
    ``api_key``, the Elder service credential) straight back via
    ``.as_dict()`` on both the create and update branches.
    """

    @pytest.mark.asyncio
    async def test_create_branch_omits_api_key(self, ipxe_client):
        created_row = SimpleNamespace(
            id=99,
            name="default",
            elder_url="https://elder.example.com",
            api_key="super-secret-elder-key",
            timeout=10,
            max_retries=3,
            is_active=True,
            last_sync_at=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        db = _make_elder_db(existing_row=None, created_or_updated_row=created_row)
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await ipxe_client.put(
                "/api/v1/ipxe/elder/config",
                json={
                    "elder_url": "https://elder.example.com",
                    "api_key": "super-secret-elder-key",
                },
            )
        assert resp.status_code == 201
        body = await resp.get_json()
        assert "api_key" not in body["config"]
        assert "super-secret-elder-key" not in await resp.get_data(as_text=True)
        assert body["config"]["elder_url"] == "https://elder.example.com"

    @pytest.mark.asyncio
    async def test_update_branch_omits_api_key(self, ipxe_client):
        existing_row = SimpleNamespace(id=99, api_key="old-key")
        updated_row = SimpleNamespace(
            id=99,
            name="default",
            elder_url="https://elder.example.com",
            api_key="rotated-super-secret-key",
            timeout=15,
            max_retries=5,
            is_active=True,
            last_sync_at=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        db = _make_elder_db(existing_row=existing_row, created_or_updated_row=updated_row)
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await ipxe_client.put(
                "/api/v1/ipxe/elder/config",
                json={
                    "elder_url": "https://elder.example.com",
                    "api_key": "rotated-super-secret-key",
                    "timeout": 15,
                    "max_retries": 5,
                },
            )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert "api_key" not in body["config"]
        assert "rotated-super-secret-key" not in await resp.get_data(as_text=True)

    @pytest.mark.asyncio
    async def test_response_is_an_exact_allow_list(self, ipxe_client):
        created_row = SimpleNamespace(
            id=99,
            name="default",
            elder_url="https://elder.example.com",
            api_key="super-secret-elder-key",
            timeout=10,
            max_retries=3,
            is_active=True,
            last_sync_at=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_error="internal diagnostic detail",
        )
        db = _make_elder_db(existing_row=None, created_or_updated_row=created_row)
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await ipxe_client.put(
                "/api/v1/ipxe/elder/config",
                json={
                    "elder_url": "https://elder.example.com",
                    "api_key": "super-secret-elder-key",
                },
            )
        body = await resp.get_json()
        assert set(body["config"]) == set(ELDER_CONFIG_PUBLIC_FIELDS)
