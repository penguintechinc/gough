"""Shared pytest fixtures for api-manager tests.

The fixtures here favour fast in-process testing over hermetic full-stack
boots. We:

* Build a fresh sqlite penguin-dal DB per test (file under ``tmp_path``).
* Register the schema via ``define_table`` on the columns the M1 sprint-3
  surfaces actually exercise (disks, disk_plans, nodes, leader_leases,
  smart_recheck_queue).
* Stub authentication by replacing ``auth_required`` and ``require_scopes``
  with passthroughs and pinning ``g.current_user`` / ``g.tenant_context`` in a
  Quart ``before_request`` hook.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator

import jwt
import pytest
from unittest.mock import MagicMock

# Registers the pg_url / pg_db real-Postgres fixtures (tests/pg_fixtures.py)
# for every test module under tests/, so conversion tasks can depend on
# `pg_db` directly without a per-file import. Needs Docker locally (spins an
# ephemeral postgres:16-bookworm via testcontainers) or $DATABASE_URL set
# (CI service container) -- see tests/pg_fixtures.py for the known schema
# gaps (tables that exist only as raw Alembic migrations).
pytest_plugins = ["tests.pg_fixtures"]

# Ensure penguin-dal singleton points to a per-test sqlite file *before* any
# app-level import binds get_db results.

def _define_table_with_string_id(dal_db, name, *fields):
    """Like ``dal_db.define_table`` but with a VARCHAR(36) string ``id``
    primary key instead of penguin_dal's auto-added Integer autoincrement id.

    For app-supplied-UUID tables (``upgrade_runs``; see
    ``app.models_m1.UpgradeRun``). The plain ``define_table`` would auto-add an
    Integer id, which lets an insert that omits the UUID pass here while failing
    against the real NOT NULL VARCHAR(36) primary key. Mirrors the identical
    helper in ``tests/api/conftest.py``.
    """
    from sqlalchemy import Column, String, Table

    if name in getattr(dal_db, "tables", []):
        return
    columns = [Column("id", String(36), primary_key=True)]
    columns.extend(field.to_sa_column() for field in fields)
    table = Table(name, dal_db.metadata, *columns)
    dal_db.metadata.create_all(dal_db.engine, tables=[table])


@pytest.fixture()
def db_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path}/test-{os.getpid()}-{threading.get_ident()}.db"


@pytest.fixture()
def dal(db_url, monkeypatch):
    """Initialize a fresh penguin-dal DB and patch get_db() to return it."""
    from penguin_dal import DB, Field

    db = DB(db_url, pool_size=1, reflect=False, migrate=True)

    db.define_table(
        "nodes",
        Field("tenant_id", "string", default="__default__"),
        Field("name", "string"),
        Field("state", "string", default="new"),
        Field("dmi_uuid", "string"),
        Field("primary_nic_mac", "string"),
        Field("hardware_tags", "json"),
        Field("hardware_json", "json"),
        migrate=True,
    )
    db.define_table(
        "disks",
        Field("node_id", "integer", notnull=True),
        Field("tenant_id", "string", default="__default__"),
        Field("device_path", "string", notnull=True),
        Field("serial", "string"),
        Field("capacity_bytes", "bigint"),
        Field("rotational", "boolean", default=False),
        Field("smart_status", "string", default="unknown"),
        Field("smart_attributes_json", "json"),
        Field("reserved_for_storage", "boolean", default=False),
        Field("storage_backend", "string"),
        Field("tier", "string", default="bulk"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )
    db.define_table(
        "disk_plans",
        Field("node_id", "integer", notnull=True),
        Field("disk_id", "integer", notnull=True),
        Field("tenant_id", "string", default="__default__"),
        Field("partition_index", "integer"),
        Field("mount_point", "string"),
        Field("size_bytes", "bigint"),
        Field("fs_type", "string"),
        Field("encryption", "string", default="none"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )
    db.define_table(
        "leader_leases",
        Field("lease_name", "string", unique=True),
        Field("holder_id", "string"),
        Field("acquired_at", "datetime"),
        Field("expires_at", "datetime"),
        Field("version", "integer", default=0),
        migrate=True,
    )

    # Patch get_db across both module references.
    from app.db import database as db_mod

    def _get_db():
        return db

    monkeypatch.setattr(db_mod, "get_db", _get_db)
    # Some modules import `get_db` directly into their namespace.
    import app.api.disks as disks_mod
    import app.workers.smart_sweeper as sweeper_mod

    monkeypatch.setattr(disks_mod, "get_db", _get_db)
    monkeypatch.setattr(sweeper_mod, "get_db", _get_db)

    yield db
    try:
        db.close()
    except Exception:
        pass


@pytest.fixture()
def seed_node(dal):
    """Insert a single ready node + two disks for tenant ``acme``."""
    now = datetime.now(timezone.utc)
    node_id = dal.nodes.insert(
        tenant_id="acme", name="node-1", state="ready",
        dmi_uuid="00000000-0000-0000-0000-000000000001",
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    sda = dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sda",
        serial="SERIAL-A", capacity_bytes=512_000_000_000, rotational=False,
        smart_status="passed", reserved_for_storage=False, tier="fast",
        created_at=now, updated_at=now,
    )
    sdb = dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sdb",
        serial="SERIAL-B", capacity_bytes=2_000_000_000_000, rotational=True,
        smart_status="unknown", reserved_for_storage=False, tier="bulk",
        created_at=now, updated_at=now,
    )
    dal.commit()
    return SimpleNamespace(node_id=int(node_id), sda=int(sda), sdb=int(sdb))


@pytest.fixture()
def disks_app(dal, seed_node, monkeypatch):
    """Build a minimal Quart app exposing the disks blueprint with auth stubbed."""
    # Replace decorators with no-op passthroughs at the module level.
    def _passthrough_decorator(*dargs, **dkwargs):
        # Support both styles: @auth_required and @require_scopes("...")
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]

        def _wrap(fn):
            return fn

        return _wrap

    # Patch the *source* modules first, then reload disks so the from-imports
    # pick up the passthrough versions.
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    import importlib
    import app.api.disks as disks_mod
    disks_mod = importlib.reload(disks_mod)
    monkeypatch.setattr(disks_mod, "get_db", lambda: dal)

    from quart import Quart, g

    app = Quart(__name__)
    app.register_blueprint(disks_mod.disks_bp, url_prefix="/api/v1/nodes")

    @app.before_request
    async def _inject_identity():
        g.current_user = {
            "id": 1,
            "username": "tester",
            "_jwt_payload": {
                "sub": "tester",
                "tenant": "acme",
                "scope": "gough.disks.read gough.disks.plan",
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="acme")

    return app


# ---------------------------------------------------------------------------
# App-factory fixtures used by tests/test_app_factory.py
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(monkeypatch):
    """Minimal Quart app fixture for test_app_factory.py.

    Creates a test app with the major blueprint prefixes and anonymous
    endpoints needed by the factory tests, without booting the full
    production factory (which requires Vault, Postgres, SPIRE, etc.).
    """
    from quart import Quart, Blueprint, g, Response
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

    # Stub auth decorators so blueprints that were already imported don't need
    # real JWT validation.
    def _passthrough(*dargs, **dkwargs):
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]
        return lambda fn: fn

    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough)

    quart_app = Quart(__name__)
    quart_app.config["TESTING"] = True
    quart_app.config["CLUSTER_ID"] = "test"
    quart_app.config["JWT_SECRET_KEY"] = "test-secret"
    quart_app.url_map.strict_slashes = False

    # Register stub blueprints for the prefixes that factory tests look for.
    for _prefix in ("/api/v1/auth", "/api/v1/users", "/api/v1/secrets", "/api/v1/clouds"):
        _name = _prefix.strip("/").replace("/", "_")
        _bp = Blueprint(_name, __name__)

        @_bp.route("/")
        async def _stub_root():
            return {"status": "ok"}

        quart_app.register_blueprint(_bp, url_prefix=_prefix)

    # Anonymous endpoints expected by the factory tests.
    @quart_app.route("/api/v1/openapi.json")
    async def _openapi():
        from pathlib import Path
        spec_path = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"
        if spec_path.exists():
            return Response(spec_path.read_text(encoding="utf-8"), mimetype="application/json")
        return {"error": "not found"}, 404

    @quart_app.route("/api/v1/version")
    async def _version():
        return {
            "version": "0.0.0",
            "build_sha": "unknown",
            "openapi_version": "3.1.0",
            "milestone": "v1.0",
            "cluster_id": quart_app.config.get("CLUSTER_ID", "unknown"),
        }

    @quart_app.route("/healthz")
    async def _health():
        return {"status": "healthy"}, 200

    @quart_app.route("/readyz")
    async def _ready():
        return {"status": "ready", "checks": {}}, 200

    @quart_app.route("/metrics")
    async def _metrics():
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    # At least one before_request handler so middleware tests pass.
    @quart_app.before_request
    async def _inject_identity():
        g.current_user = {
            "id": 1,
            "username": "test",
            "_jwt_payload": {"sub": "test", "tenant": "default", "scope": ""},
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return quart_app


@pytest.fixture()
def client(app):
    """Test client for the factory-test app fixture."""
    return app.test_client()


# ============================================================================
# Generic pytest configuration (plugins, hooks)
# ============================================================================

def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "asyncio: marks tests as async (deselect with '-m \"not asyncio\"')"
    )


@pytest.fixture
def anyio_backend():
    """Configure anyio backend for async tests."""
    return "asyncio"


@pytest.fixture()
def db(dal):
    """Alias for dal fixture — used by tests."""
    return dal


@pytest.fixture()
def auth_token() -> str:
    """Mint a JWT with full operator scopes: gough.biomes.read, gough.biomes.deploy,
    gough.cluster.admin, gough.cluster.read, and mfa=true for MFA-gated endpoints.
    """
    secret = "test-secret"
    now = datetime.utcnow()
    expires = now + timedelta(hours=1)

    payload = {
        "sub": "test-operator@penguintech.io",
        "iss": "gough-test",
        "aud": "gough-api",
        "tenant": "default",
        "scope": "gough.biomes.read gough.biomes.deploy gough.cluster.admin gough.cluster.read",
        "mfa": True,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "type": "access",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture()
def superadmin_token() -> str:
    """Mint a JWT with superadmin scope for cluster adoption and privileged operations."""
    secret = "test-secret"
    now = datetime.utcnow()
    expires = now + timedelta(hours=1)

    payload = {
        "sub": "test-superadmin@penguintech.io",
        "iss": "gough-test",
        "aud": "gough-api",
        "tenant": "default",
        "scope": "gough.biomes.read gough.biomes.deploy gough.cluster.admin gough.cluster.superadmin gough.cluster.read",
        "mfa": True,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "type": "access",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture()
def viewer_token() -> str:
    """Mint a JWT with read-only scopes (no deploy, admin, or mfa)."""
    secret = "test-secret"
    now = datetime.utcnow()
    expires = now + timedelta(hours=1)

    payload = {
        "sub": "test-viewer@penguintech.io",
        "iss": "gough-test",
        "aud": "gough-api",
        "tenant": "default",
        "scope": "gough.biomes.read gough.cluster.read",
        "mfa": False,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "type": "access",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture()
def test_client(dal, monkeypatch):
    """Test client for biomes and clusters endpoints with real DB."""
    # Define tables for biomes upgrade tests
    from penguin_dal import Field

    # Note: nodes table already defined in dal fixture
    # Add additional fields needed for this test
    dal.define_table(
        "clusters",
        Field("name", "string", unique=True),
        Field("description", "string"),
        Field("status", "string", default="ready"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )
    dal.define_table(
        "biomes",
        Field("name", "string"),
        Field("biome_kind", "string", default="custom"),
        Field("workload_type", "string"),
        Field("phase", "string"),
        Field("registry_url", "string"),
        Field("tenant_id", "string", default="__default__"),
        Field("cluster_id", "string", default="default"),
        migrate=True,
    )
    dal.define_table(
        "deployments",
        Field("biome_id", "integer"),
        Field("node_id", "integer"),
        Field("status", "string", default="pending"),
        Field("phase", "integer", default=1),
        migrate=True,
    )
    _define_table_with_string_id(
        dal,
        "upgrade_runs",
        Field("biome_id", "integer"),
        Field("target_version", "string"),
        Field("cluster_id", "string"),
        Field("status", "string", default="pending"),
        Field("phase", "string", default="canary"),
        Field("nodes_total", "integer", default=0),
        Field("nodes_completed", "integer", default=0),
        Field("nodes_failed", "integer", default=0),
        Field("started_at", "datetime"),
        Field("completed_at", "datetime"),
        Field("rollback_reason", "string"),
        Field("actor_sub", "string"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
    )
    dal.define_table(
        "storage_config",
        Field("name", "string"),
        Field("provider_type", "string"),
        Field("endpoint_url", "string"),
        Field("region", "string"),
        Field("bucket_name", "string"),
        Field("credentials_path", "string"),
        Field("is_active", "boolean", default=True),
        Field("is_default", "boolean", default=False),
        Field("use_ssl", "boolean", default=True),
        Field("config_data", "string"),
        Field("created_by", "integer"),
        migrate=True,
    )
    dal.commit()

    # Stub auth decorators
    def _passthrough_decorator(*dargs, **dkwargs):
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]

        def _wrap(fn):
            return fn

        return _wrap

    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    # Reload modules to pick up stubbed decorators
    import importlib
    import app.api.biomes as biomes_mod
    import app.api.clusters as clusters_mod

    biomes_mod = importlib.reload(biomes_mod)
    clusters_mod = importlib.reload(clusters_mod)
    monkeypatch.setattr(biomes_mod, "get_db", lambda: dal)
    monkeypatch.setattr(clusters_mod, "get_db", lambda: dal)

    from quart import Quart, g

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["JWT_SECRET_KEY"] = "test-secret"

    # Register blueprints
    app.register_blueprint(biomes_mod.biomes_bp, url_prefix="/api/v1/biomes")
    app.register_blueprint(clusters_mod.clusters_bp, url_prefix="/api/v1/clusters")

    @app.before_request
    async def _inject_identity():
        g.current_user = MagicMock(sub="test-user-123")
        g.tenant_context = SimpleNamespace(tenant_id="__default__")

    return app.test_client()


@pytest.fixture()
def authed_client(test_client, auth_token):
    """Test client that auto-injects Authorization: Bearer <token> with full scopes."""
    class AuthedTestClient:
        def __init__(self, client, token):
            self._client = client
            self._token = token

        async def get(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.get(*args, **kwargs)

        async def post(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.post(*args, **kwargs)

        async def put(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.put(*args, **kwargs)

        async def delete(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.delete(*args, **kwargs)

        async def patch(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.patch(*args, **kwargs)

    return AuthedTestClient(test_client, auth_token)


@pytest.fixture()
def authed_viewer_client(test_client, viewer_token):
    """Test client that auto-injects Authorization: Bearer <token> with read-only scopes."""
    class AuthedTestClient:
        def __init__(self, client, token):
            self._client = client
            self._token = token

        async def get(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.get(*args, **kwargs)

        async def post(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.post(*args, **kwargs)

        async def put(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.put(*args, **kwargs)

        async def delete(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.delete(*args, **kwargs)

        async def patch(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.patch(*args, **kwargs)

    return AuthedTestClient(test_client, viewer_token)


@pytest.fixture()
def authed_superadmin_client(test_client, superadmin_token):
    """Test client that auto-injects Authorization: Bearer <token> with superadmin scopes."""
    class AuthedTestClient:
        def __init__(self, client, token):
            self._client = client
            self._token = token

        async def get(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.get(*args, **kwargs)

        async def post(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.post(*args, **kwargs)

        async def put(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.put(*args, **kwargs)

        async def delete(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.delete(*args, **kwargs)

        async def patch(self, *args, **kwargs):
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            return await self._client.patch(*args, **kwargs)

    return AuthedTestClient(test_client, superadmin_token)
