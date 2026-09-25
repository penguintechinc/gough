"""Pytest fixtures for API endpoint tests.

Provides a Quart test client with blueprints registered and auth stubbed.

Also provides ``real_auth_env`` (regression: gh-31): a NON-injecting client
that drives the genuine authentication middleware chain. Unlike ``client`` /
``app_with_auth`` (which pin ``g.current_user`` in a ``before_request`` hook and
never install the real middleware), ``real_auth_env`` installs the production
``wire_middleware`` chain and populates ``g.current_user`` ONLY via a real
``Authorization: Bearer`` token flowing through ``_credential_validation``.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
import pytest_asyncio
from quart import Quart, g
from sqlalchemy import Column, String, Table

from app.security.scope_policy import _expand_roles_to_scopes


def _define_table_with_string_id(dal_db, name, *fields):
    """Like ``dal_db.define_table`` but with a VARCHAR(36) string ``id``
    primary key instead of penguin_dal's auto-added Integer autoincrement id.

    ``penguin_dal.field.Field``'s ``type_="id"`` shortcut is hardcoded to an
    Integer column with no way to override -- but the real physical schema
    for app-supplied-UUID tables (``migration_policy``, ``migration_events``;
    see ``app.models_m1.UUID``, VARCHAR(36)) has a string primary key with no
    default. On SQLite, an auto-added ``INTEGER PRIMARY KEY`` becomes a rowid
    alias that rejects a string id with ``datatype mismatch`` -- this builds
    the table via raw SQLAlchemy against the DB's own metadata/engine
    (mirroring what ``define_table`` does internally, minus the auto-id
    logic) so ``dal_db.<name>`` resolves normally via ``DB.__getattr__``.
    """
    if name in getattr(dal_db, "tables", []):
        return
    columns = [Column("id", String(36), primary_key=True)]
    columns.extend(field.to_sa_column() for field in fields)
    table = Table(name, dal_db.metadata, *columns)
    dal_db.metadata.create_all(dal_db.engine, tables=[table])


def _passthrough_decorator(*dargs, **dkwargs):
    """Stub for auth_required / require_scopes.

    Returns the decorated function unchanged, allowing tests to run without
    JWT validation.
    """
    # Pattern: @decorator or @decorator(args)
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        # Direct decoration: @auth_required
        return dargs[0]
    # Parameterized decoration: @require_scopes("a", "b")
    def _wrap(fn):
        return fn
    return _wrap


@pytest.fixture()
def client(dal, monkeypatch):
    """Create a Quart test client with all API blueprints."""
    # Patch get_db in app.models before reloading blueprints
    import app.models as models_mod
    monkeypatch.setattr(models_mod, "get_db", lambda: dal)

    # Stub auth decorators before importing blueprints
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    # Add migration-related tables to the test DAL
    from penguin_dal import Field
    from datetime import datetime, timezone

    if "biomes" not in getattr(dal, "tables", []):
        dal.define_table(
            "biomes",
            Field("name", "string", notnull=True),
            Field("tenant_id", "string", default="__default__"),
            Field("biome_kind", "string", default="custom"),
            Field("lock_to_host", "boolean", default=False),
            Field("requires_hardware_tags", "json"),
            Field("storage_requirements_json", "json"),
            migrate=True,
        )
    # ``node_egg_assignments`` is the real, baseline-created table (gh-21:
    # ``node_biome_assignments`` was a phantom name that never existed).
    if "node_egg_assignments" not in getattr(dal, "tables", []):
        dal.define_table(
            "node_egg_assignments",
            Field("node_id", "integer", notnull=True),
            Field("egg_id", "integer", notnull=True),
            Field("tenant_id", "string", default="__default__"),
            Field("status", "string", default="pending"),
            migrate=True,
        )
    # ``id`` is app-supplied VARCHAR(36) UUID on the real table (see
    # app.api.migration.patch_migration_policy's ``id=str(uuid.uuid4())``)
    # -- use the string-id helper, not ``dal.define_table``'s auto Integer id.
    _define_table_with_string_id(
        dal,
        "migration_policy",
        Field("cluster_id", "string", notnull=True),
        Field("enabled", "boolean", default=False),
        Field("evaluation_interval_seconds", "integer", default=300),
        Field("min_healthy_nodes", "integer", default=3),
        Field("max_concurrent_migrations", "integer", default=1),
        Field("require_target_capacity_headroom_cpu_pct", "integer", default=20),
        Field("require_target_capacity_headroom_mem_pct", "integer", default=20),
        Field("require_target_capacity_headroom_disk_pct", "integer", default=15),
        Field("rollback_on_destination_failure", "boolean", default=True),
        Field("rollback_window_seconds", "integer", default=300),
        Field("forbid_migration_during_partition", "boolean", default=True),
        Field("forbid_migration_during_maintenance", "boolean", default=True),
        Field("waddleai_risk_threshold", "float", default=0.75),
        Field("capacity_forecast_horizon_days", "integer", default=7),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
    )
    # ``id`` is app-supplied VARCHAR(36) UUID on the real table (see
    # app.api.migration._record_safety_event's ``id=str(uuid.uuid4())``).
    _define_table_with_string_id(
        dal,
        "migration_events",
        Field("biome_instance_id", "integer"),
        Field("biome_id", "integer"),
        Field("biome_kind", "string"),
        Field("src_node_id", "integer"),
        Field("dst_node_id", "integer"),
        Field("reason", "string"),
        Field("result", "string"),
        Field("rejection_reason", "string"),
        Field("safety_check_details_json", "json"),
        Field("started_at", "datetime"),
        Field("completed_at", "datetime"),
        Field("duration_seconds", "float"),
    )

    # Seed nodes, biome, assignment, and policy for migration tests
    _now = datetime.now(timezone.utc)
    _node_id = dal.nodes.insert(
        tenant_id="default", name="node-1", state="ready",
        dmi_uuid="00000000-0000-0000-0000-000000000001",
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    # Second node as migration target
    dal.nodes.insert(
        tenant_id="default", name="node-2", state="ready",
        dmi_uuid="00000000-0000-0000-0000-000000000002",
        primary_nic_mac="aa:bb:cc:dd:ee:02",
    )
    _biome_id = dal.biomes.insert(
        name="test-biome", biome_kind="custom", lock_to_host=False,
    )
    # First insert gets id=1 in SQLite — used by POST trigger tests
    dal.node_egg_assignments.insert(
        node_id=int(_node_id), egg_id=int(_biome_id),
        tenant_id="default", status="active",
    )
    # Seed policy with min_healthy_nodes=1 to match single-node test cluster.
    # ``id`` is app-supplied VARCHAR(36) UUID -- no default (see
    # _define_table_with_string_id above).
    dal.migration_policy.insert(
        id=str(uuid.uuid4()),
        cluster_id="default",
        enabled=False,
        evaluation_interval_seconds=300,
        min_healthy_nodes=1,
        max_concurrent_migrations=1,
        require_target_capacity_headroom_cpu_pct=20,
        require_target_capacity_headroom_mem_pct=20,
        require_target_capacity_headroom_disk_pct=15,
        rollback_on_destination_failure=True,
        rollback_window_seconds=300,
        forbid_migration_during_partition=True,
        forbid_migration_during_maintenance=True,
        waddleai_risk_threshold=0.75,
        capacity_forecast_horizon_days=7,
        created_at=_now,
        updated_at=_now,
    )
    dal.commit()

    # Reload blueprints to pick up stubbed decorators
    import app.api.migration as migration_mod
    import app.api.clusters as clusters_mod
    import app.api.primary as primary_mod

    migration_mod = importlib.reload(migration_mod)
    monkeypatch.setattr(migration_mod, "get_db", lambda: dal)

    clusters_mod = importlib.reload(clusters_mod)
    monkeypatch.setattr(clusters_mod, "get_db", lambda: dal)

    primary_mod = importlib.reload(primary_mod)
    monkeypatch.setattr(primary_mod, "get_db", lambda: dal)

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["CLUSTER_ID"] = "default"
    app.config["JWT_SECRET_KEY"] = "test-secret-key"
    app.url_map.strict_slashes = False

    app.register_blueprint(migration_mod.migration_bp, url_prefix="/api/v1/migration")
    app.register_blueprint(clusters_mod.clusters_bp, url_prefix="/api/v1/clusters")
    app.register_blueprint(primary_mod.primary_bp, url_prefix="/api/v1/primary")

    @app.before_request
    async def _inject_auth():
        """Stub authentication with default admin user."""
        g.current_user = {
            "id": 1,
            "username": "test-operator",
            "role": "admin",  # Full access for testing
            "_jwt_payload": {
                "sub": "test-operator",
                "tenant": "default",
                "scope": (
                    "gough.capacity.read gough.migration.policy "
                    "gough.migration.trigger gough.migration.override-lock "
                    "gough.storage.read gough.storage.configure "
                    "gough.cluster.read gough.cluster.admin gough.cluster.superadmin"
                ),
                "mfa": True,
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")
        g.mfa_verified = True  # Assume MFA for testing

    return app.test_client()


@pytest.fixture()
def app_with_auth(dal, monkeypatch):
    """Create a Quart app with biomes blueprint and auth stubbed.

    Returns the app instance (not test_client), allowing fixture to call
    app.test_client() multiple times if needed.
    """
    # Patch get_db to return the test DAL FIRST, before any imports
    import app.models as models_mod
    monkeypatch.setattr(models_mod, "get_db", lambda: dal)

    # Stub auth decorators
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    # Reload biomes blueprint to pick up stubbed decorators and patched get_db
    import app.api.biomes as biomes_mod
    import app.api.nodes as nodes_mod

    biomes_mod = importlib.reload(biomes_mod)
    nodes_mod = importlib.reload(nodes_mod)

    # Patch get_db for nodes module too
    monkeypatch.setattr(nodes_mod, "get_db", lambda: dal)

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["CLUSTER_ID"] = "default"
    app.config["JWT_SECRET_KEY"] = "test-secret-key"
    app.url_map.strict_slashes = False

    app.register_blueprint(biomes_mod.biomes_bp)
    app.register_blueprint(nodes_mod.nodes_bp)

    @app.before_request
    async def _inject_auth():
        """Stub authentication with default admin user."""
        g.current_user = {
            "id": 1,
            "username": "test-operator",
            "role": "admin",  # Full access for testing
            "_jwt_payload": {
                "sub": "test-operator",
                "tenant": "__default__",
                "scope": (
                    "gough.biomes.read gough.biomes.create gough.biomes.write "
                    "gough.biomes.delete gough.biomes.sign gough.biomes.upgrade "
                    "gough.cluster.admin gough.cluster.superadmin"
                ),
                "mfa": True,
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="__default__")
        g.mfa_verified = True

    return app


@pytest.fixture()
def app(monkeypatch):
    """Bare Quart app with auth stubbed — used as base for blueprint-specific fixtures."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    # decode_token/get_user_by_id used to be stubbed here so the REAL
    # auth_required decorator (bound at import time on blueprint routes) would
    # pass through. The penguin-aaa migration removed both from app.middleware,
    # so those monkeypatches raised AttributeError and errored out every test
    # using this fixture. The seam is gone for good: a blueprint-specific
    # fixture that needs the stubs above to actually apply must reload its
    # blueprint module after this fixture runs (see app_with_integrations in
    # tests/api/test_integrations.py), because decorators bind at import time.

    from quart import Quart, g

    quart_app = Quart(__name__)
    quart_app.config["TESTING"] = True
    quart_app.config["CLUSTER_ID"] = "default"
    quart_app.config["JWT_SECRET_KEY"] = "test-secret-key"
    quart_app.url_map.strict_slashes = False

    @quart_app.before_request
    async def _inject_auth():
        g.current_user = {
            "id": 1,
            "username": "test-operator",
            "role": "admin",
            "_jwt_payload": {
                "sub": "test-operator",
                "tenant": "default",
                "scope": (
                    "gough.integrations.read gough.integrations.write "
                    "gough.integrations.admin gough.cluster.superadmin"
                ),
                "mfa": True,
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")
        g.mfa_verified = True

    return quart_app


@pytest.fixture()
def dal_with_eggs(dal):
    """Alias for dal_with_biomes for backward-compat.

    Extends dal with biomes + node_egg_assignments tables.
    """
    from penguin_dal import Field

    # Define biomes table
    dal.define_table(
        "biomes",
        Field("tenant_id", "string", default="__default__"),
        Field("name", "string", notnull=True),
        Field("display_name", "string"),
        Field("description", "string"),
        Field("biome_type", "string"),
        Field("egg_type", "string"),
        Field("version", "string"),
        Field("category", "string"),
        Field("snap_name", "string"),
        Field("snap_channel", "string", default="stable"),
        Field("snap_classic", "boolean", default=False),
        Field("cloud_init_content", "text"),
        Field("lxd_image_alias", "string"),
        Field("lxd_image_url", "string"),
        Field("lxd_profiles", "json"),
        Field("is_hypervisor_config", "boolean", default=False),
        Field("dependencies", "json"),
        Field("min_ram_mb", "integer"),
        Field("min_disk_gb", "integer"),
        Field("required_architecture", "string", default="any"),
        Field("is_active", "boolean", default=True),
        Field("is_default", "boolean", default=False),
        Field("checksum", "string"),
        Field("size_bytes", "bigint"),
        # M1 extensions
        Field("biome_kind", "string", default="custom"),
        Field("phase", "string", default="post_deploy"),
        Field("workload_type", "string", default="lxc"),
        Field("lock_to_host", "boolean", default=False),
        Field("auto_join_cluster", "boolean", default=False),
        Field("upgrade_strategy", "string", default="rolling"),
        Field("requires_hardware_tags", "json"),
        Field("prefers_hardware_tags", "json"),
        Field("forbids_hardware_tags", "json"),
        Field("storage_requirements_json", "json"),
        Field("readiness_probe", "json"),
        Field("emits_joiner_secrets", "boolean", default=False),
        Field("joiner_emit_spec", "json"),
        Field("consumes_joiner_secrets_from", "json"),
        Field("joiner_consume_spec", "json"),
        Field("snapshot_schedule_json", "json"),
        Field("required_interfaces", "json"),
        Field("signing_key_id", "string"),
        Field("sbom_url", "string"),
        Field("registry_url", "string"),
        Field("signing_status", "string", default="unsigned"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )

    # Define node_egg_assignments table (gh-21: the real, baseline-created
    # table -- ``node_biome_assignments`` was a phantom name that never
    # existed in production).
    dal.define_table(
        "node_egg_assignments",
        Field("node_id", "integer", notnull=True),
        Field("egg_id", "integer", notnull=True),
        Field("tenant_id", "string", default="__default__"),
        Field("status", "string", default="pending"),
        Field("annotation", "string"),
        Field("phase", "string", default="post_deploy"),
        Field("readiness_probe_state", "string", default="pending"),
        Field("depends_on_egg_instance_id", "integer"),
        Field("assigned_at", "datetime"),
        Field("deployed_at", "datetime"),
        Field("removed_at", "datetime"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )

    # ``deployments`` is read by _execute_upgrade_orchestration's background
    # task (app.api.biomes) to derive target nodes for an upgrade rollout.
    # Full field set (matches TestDeployments' fixture further down in
    # tests/api/test_biomes.py) so both consumers share one definition.
    if "deployments" not in getattr(dal, "tables", []):
        dal.define_table(
            "deployments",
            Field("biome_id", "integer"),
            Field("node_id", "integer"),
            Field("phase", "integer", default=0),
            Field("status", "string", default="pending"),
            Field("logs_url", "string"),
            Field("tenant_id", "string", default="__default__"),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=True,
        )

    # ``upgrade_runs`` backs POST /api/v1/biomes/{id}/upgrade
    # (app.api.biomes.upgrade_biome / _execute_upgrade_orchestration) --
    # mirrors the field set used in ``tests/conftest.py``'s ``test_client``
    # fixture for the equivalent table.
    # upgrade_runs.id is an app-supplied VARCHAR(36) UUID, not an
    # autoincrement integer (app.models_m1.UpgradeRun). Using the plain
    # define_table here would auto-add an Integer id and let a bad insert
    # pass in tests while failing on a real database.
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

    return dal


# ============================================================================
# Real auth-chain fixture (regression: gh-31) -- REAL ES256 through the FULL
# penguin-aaa ASGI stack.
# ============================================================================
#
# The ``client`` / ``app_with_auth`` fixtures above deliberately pin
# ``g.current_user`` in a ``before_request`` hook and never boot the real auth
# gate. That is fine for exercising handler bodies, but it makes it impossible
# to test authentication itself: every request already has a principal, so any
# assertion of "valid token accepted" / "missing token rejected" against those
# fixtures is self-confirming -- satisfied by the injector, not the code.
#
# ``real_auth_env`` closes that gap. It boots the WHOLE production app via
# ``create_app`` (so requests pass through the real ``OIDCAuthMiddleware`` ASGI
# gate + the tenant bridge + fail-closed scope enforcement) and injects NOTHING
# into ``g``/``request.scope``. Tokens are minted with the app's OWN ES256
# signing key (the same key its ``StaticKeyVerifier`` validates against), so the
# only way a request is authenticated is a genuinely-signed bearer token that
# survives the real gate -- exactly the production path.
#
# The auth JWT deletion (gh-31) replaced HS256 ``decode_token``/``generate_jwt_token``
# with this ES256/OIDC model; the former HS256-minting fixture is gone with them.


@dataclass(slots=True)
class RealAuthEnv:
    """Full-stack test client + a REAL ES256 token minter (regression: gh-31).

    ``mint()`` signs tokens with the booted app's own keystore private key, which
    its ``StaticKeyVerifier`` (wired into ``OIDCAuthMiddleware``) validates -- so
    a token issued here drives the genuine ASGI gate -> tenant bridge -> scope
    enforcement path. Nothing is injected into ``g`` or ``request.scope``.
    """

    client: Any
    app: Any
    provider: Any
    settings: Any
    user_id: int
    viewer_id: int
    deactivated_id: int
    _ds: Any

    def mint(
        self,
        scope: str | list[str] | None = None,
        *,
        sub: str | int | None = None,
        tenant: str | None = "__default__",
        roles: list[str] | None = None,
        user_id: int | None = None,  # accepted for back-compat; identity is ``sub``
        expired: bool = False,
        token_use: str = "access",
        audience: str | None = None,
    ) -> str:
        """Sign a REAL ES256 token with the app's own keystore key.

        ``sub`` defaults to the seeded ACTIVE admin's id. ``scope`` may be a
        space-separated string or a list; when omitted it is expanded from
        ``roles`` (default ``["admin"]``). ``tenant=None`` omits the tenant claim
        entirely (rejected at the verifier -> 401). ``expired=True`` back-dates
        ``exp``. ``token_use="id"`` produces an id token (rejected as an access
        token -> 401).
        """
        role_names = ["admin"] if roles is None else list(roles)
        if scope is None:
            scope_list = _expand_roles_to_scopes(role_names)
        elif isinstance(scope, str):
            scope_list = scope.split()
        else:
            scope_list = list(scope)

        subject = str(self.user_id if sub is None else sub)
        signing_key, kid = self.provider._keystore.get_signing_key()
        now = datetime.now(timezone.utc)
        exp = now - timedelta(hours=1) if expired else now + timedelta(minutes=30)
        payload: dict[str, Any] = {
            "sub": subject,
            "iss": self.settings.issuer,
            "aud": [audience or self.settings.audience],
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "scope": scope_list,
            "roles": role_names,
            "teams": [],
            "ext": {},
            "token_use": token_use,
        }
        if tenant is not None:
            payload["tenant"] = tenant
        return jwt.encode(
            payload,
            signing_key,
            algorithm=self.settings.algorithm,
            headers={"kid": kid},
        )

    def mint_id_token(self, **kwargs: Any) -> str:
        """Mint a REAL ES256 *id* token (must be rejected as an access token)."""
        return self.mint(token_use="id", **kwargs)

    def create_user(
        self,
        email: str,
        *,
        roles: tuple[str, ...] = ("admin",),
        active: bool = True,
        password: str = "real-pass-123",
    ) -> int:
        """Seed a user with the given roles via the real datastore; return id."""
        import bcrypt

        role_objs = [
            self._ds.find_role(rn) or self._ds.create_role(name=rn, description=rn)
            for rn in roles
        ]
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        user = self._ds.create_user(
            email=email,
            password=pw_hash,
            full_name=email,
            roles=role_objs,
            active=active,
        )
        return int(user.id)


@pytest_asyncio.fixture()
async def real_auth_env(tmp_path, monkeypatch):
    """Full-ASGI client that drives the REAL auth gate (regression: gh-31).

    Boots the production app via ``create_app`` (in-memory ES256 keystore under
    ``TESTING``), seeds an ACTIVE admin, an ACTIVE viewer, a DEACTIVATED user,
    and a ``__default__`` node. Every request flows through the real
    ``OIDCAuthMiddleware`` -> tenant bridge -> scope enforcement. No principal is
    ever injected -- authentication happens only via a genuinely-signed token.
    """
    import bcrypt

    from app import create_app
    from app.config import Config

    # Config.DB_* are class attributes frozen from os.getenv at import time, and
    # init_db() reads the BASE ``Config.get_db_uri()`` (not the passed config
    # class), so a post-import setenv is ignored. Monkeypatch the base class
    # attributes directly with a per-test unique sqlite path so every boot gets
    # a fresh, isolated DB (restored automatically after the test).
    _db_path = os.path.join(str(tmp_path), f"gh31-{os.getpid()}")
    monkeypatch.setattr(Config, "DB_TYPE", "sqlite")
    monkeypatch.setattr(Config, "DB_NAME", _db_path)

    class _RealAuthTestConfig(Config):
        TESTING = True  # in-memory ES256 keystore + validate_secrets skip
        DEBUG = False
        GRPC_ENABLED = False
        DB_TYPE = "sqlite"
        DB_NAME = _db_path
        RATE_LIMIT_ENABLED = False
        AUDIT_ENABLED = False

    app = await create_app(_RealAuthTestConfig)
    async with app.test_app() as ta:
        ds = app.user_datastore
        admin_role = ds.find_role("admin") or ds.create_role(
            name="admin", description="Full system access"
        )
        viewer_role = ds.find_role("viewer") or ds.create_role(
            name="viewer", description="Read only"
        )
        pw_hash = bcrypt.hashpw(b"real-pass-123", bcrypt.gensalt()).decode()
        admin = ds.create_user(
            email="operator@gough.test", password=pw_hash,
            full_name="GH31 Operator", roles=[admin_role], active=True,
        )
        viewer = ds.create_user(
            email="viewer@gough.test", password=pw_hash,
            full_name="GH31 Viewer", roles=[viewer_role], active=True,
        )
        dead = ds.create_user(
            email="dead@gough.test", password=pw_hash,
            full_name="GH31 Dead", roles=[admin_role], active=True,
        )
        ds.deactivate_user(dead)

        # Seed one node in the default tenant so a scoped GET returns a row.
        # get_db() needs a Quart context; the DB lives on app.config["db"].
        db = app.config.get("db")
        if db is not None:
            _now = datetime.now(timezone.utc)
            db.nodes.insert(
                tenant_id="__default__", name="gh31-node", state="ready",
                dmi_uuid="00000000-0000-0000-0000-0000000000aa",
                primary_nic_mac="aa:bb:cc:dd:ee:aa",
                created_at=_now, updated_at=_now,
            )
            db.commit()

        env = RealAuthEnv(
            client=ta.test_client(),
            app=app,
            provider=app.config["OIDC_PROVIDER"],
            settings=app.config["OIDC_SETTINGS"],
            user_id=int(admin.id),
            viewer_id=int(viewer.id),
            deactivated_id=int(dead.id),
            _ds=ds,
        )
        yield env
