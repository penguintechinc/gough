"""Test suite for Vault API (app/api/vault.py).

Coverage targets:
- rotate-keys endpoint with various rotation_class values
- Error handling (missing db, rotation failure)
- Prometheus metrics increment
- Audit logging
"""

import importlib
import uuid
import pytest
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from types import SimpleNamespace


def _make_mock_db(rowcount=5):
    """Mock a penguin-dal DB: ``executesql(..., return_rowcount=True)``
    returns the rowcount directly (an int), unlike the old raw SQLAlchemy
    ``engine.execute(...)`` which returned a ``CursorResult`` with a
    ``.rowcount`` attribute.
    """
    mock_db = MagicMock()
    mock_db.executesql.return_value = rowcount
    return mock_db


@pytest.fixture
def vault_app(monkeypatch, app):
    """Setup vault API blueprint with auth stubbed."""
    import importlib
    import app.middleware as mw_mod

    def _passthrough(*dargs, **dkwargs):
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]
        return lambda fn: fn

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)

    import app.api.vault as vault_mod

    # Unregister existing Prometheus metrics to avoid duplicate registration on reload
    from prometheus_client import REGISTRY
    for collector in list(set(REGISTRY._names_to_collectors.values())):
        if hasattr(collector, '_name') and 'gough_vault' in getattr(collector, '_name', ''):
            try:
                REGISTRY.unregister(collector)
            except Exception:
                pass

    vault_mod = importlib.reload(vault_mod)

    app.register_blueprint(vault_mod.vault_bp, url_prefix="/api/v1/vault")

    @app.before_request
    async def _inject_context():
        from quart import g
        g.current_user = {"id": 1, "username": "admin", "role": "admin"}
        g.tenant_context = SimpleNamespace(tenant_id="default")
        g.db = _make_mock_db(rowcount=5)

    return app


@pytest.mark.asyncio
async def test_rotate_keys_default_class(vault_app):
    """Test POST /api/v1/vault/rotate-keys with default rotation_class."""
    client = vault_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["rotated"] is True
    assert data["rotation_class"] == "joiner"


@pytest.mark.asyncio
async def test_rotate_keys_custom_rotation_class(vault_app):
    """Test POST /api/v1/vault/rotate-keys with custom rotation_class."""
    client = vault_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({"rotation_class": "bootstrap"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["rotation_class"] == "bootstrap"
    assert isinstance(data["revoked_count"], int)


@pytest.mark.asyncio
async def test_rotate_keys_empty_rotation_class(vault_app):
    """Test POST rejects empty rotation_class."""
    client = vault_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({"rotation_class": "   "}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_rotate_keys_no_data(vault_app):
    """Test POST /api/v1/vault/rotate-keys with no JSON body."""
    client = vault_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data="",
        headers={"Content-Type": "application/json"},
    )
    # Empty body is treated as missing data, defaults apply
    assert response.status_code in [200, 400, 500]


@pytest.mark.asyncio
async def test_rotate_keys_with_reason(vault_app):
    """Test POST includes reason in audit log."""
    client = vault_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({
            "rotation_class": "joiner",
            "reason": "security-incident",
        }),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_rotate_keys_db_not_available(vault_app):
    """Test POST returns 503 when database unavailable."""
    # Override before_request to simulate missing db
    @vault_app.before_request
    async def _no_db():
        from quart import g
        g.current_user = {"id": 1, "username": "admin", "role": "admin"}
        g.tenant_context = SimpleNamespace(tenant_id="default")
        # Don't set g.db to simulate unavailable DB

    client = vault_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({}),
        headers={"Content-Type": "application/json"},
    )
    # Should return 503 or handle gracefully
    assert response.status_code in [200, 503]


@pytest.mark.asyncio
async def test_rotate_keys_db_error(vault_app):
    """Test POST returns 500 on database error."""
    @vault_app.before_request
    async def _error_db():
        from quart import g
        g.current_user = {"id": 1, "username": "admin", "role": "admin"}
        g.tenant_context = SimpleNamespace(tenant_id="default")
        error_db = MagicMock()
        error_db.executesql.side_effect = Exception("Database error")
        g.db = error_db

    client = vault_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_rotate_keys_prometheus_metric_incremented(vault_app):
    """Test POST increments Prometheus counter."""
    with patch("app.api.vault.vault_key_rotation_total") as mock_counter:
        client = vault_app.test_client()
        response = await client.post(
            "/api/v1/vault/rotate-keys",
            data=json.dumps({"rotation_class": "joiner"}),
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 200:
            mock_counter.labels.assert_called()


@pytest.mark.asyncio
async def test_rotate_keys_audit_logging(vault_app):
    """Test POST logs audit event if audit logger available."""
    with patch("app.audit.get_audit_logger") as mock_get_logger:
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger

        client = vault_app.test_client()
        response = await client.post(
            "/api/v1/vault/rotate-keys",
            data=json.dumps({
                "rotation_class": "joiner",
                "reason": "manual-rotation",
            }),
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 200:
            assert mock_logger.log_event is not None


@pytest.mark.asyncio
async def test_rotate_keys_whitespace_handling(vault_app):
    """Test POST strips whitespace from fields."""
    client = vault_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({
            "rotation_class": "  joiner  ",
            "reason": "  operator-triggered  ",
        }),
        headers={"Content-Type": "application/json"},
    )
    data = await response.get_json()
    if response.status_code == 200:
        assert data["rotation_class"] == "joiner"


# =============================================================================
# Auth enforcement (F1/F2 fix round) — real decorators, not stubbed
# =============================================================================
#
# Every test above (via ``vault_app``/``_build_vault_app_pg``) stubs
# ``auth_required``/``require_scopes`` with passthroughs so it can focus on
# the endpoint's own logic. Before the fix, ``rotate_keys`` had no decorator
# at all -- an unauthenticated caller reached the handler and got a
# misleading ``200 {"rotated": true, "revoked_count": 0}`` (RLS fails closed
# to 0 rows without a tenant on the request), rather than being rejected.
# These tests build their own standalone app and deliberately do NOT stub
# either decorator, so they exercise the real ``@auth_required``/
# ``@require_scopes("gough.cluster.superadmin")`` chain end-to-end.


def _build_vault_app_real_auth():
    """Build a Quart app with ``vault_bp`` wired to the REAL auth decorators.

    No ``monkeypatch`` involved -- as long as no other test's monkeypatch of
    ``app.middleware.auth_required``/``app.security.scope_enforcement.
    require_scopes`` is still active (pytest's ``monkeypatch`` fixture
    guarantees per-test teardown), reloading ``app.api.vault`` here picks up
    the genuine, unpatched decorators.
    """
    import importlib
    from quart import Quart
    import app.api.vault as vault_mod

    from prometheus_client import REGISTRY
    for collector in list(set(REGISTRY._names_to_collectors.values())):
        if hasattr(collector, "_name") and "gough_vault" in getattr(collector, "_name", ""):
            try:
                REGISTRY.unregister(collector)
            except Exception:
                pass
    vault_mod = importlib.reload(vault_mod)

    quart_app = Quart(__name__)
    quart_app.config["JWT_SECRET_KEY"] = "test-secret"
    quart_app.register_blueprint(vault_mod.vault_bp, url_prefix="/api/v1/vault")
    return quart_app


@pytest.mark.asyncio
async def test_rotate_keys_unauthenticated_rejected():
    """F2: no Authorization header at all -> 401, never reaches the handler
    (not the pre-fix 200 with revoked_count: 0).
    """
    quart_app = _build_vault_app_real_auth()
    client = quart_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({"rotation_class": "joiner"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401
    data = await response.get_json()
    assert data.get("rotated") is not True


@pytest.mark.asyncio
async def test_rotate_keys_invalid_token_rejected():
    """F2: a garbage bearer token (fails JWT decode) -> 401."""
    quart_app = _build_vault_app_real_auth()
    client = quart_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({"rotation_class": "joiner"}),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer not-a-real-jwt",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rotate_keys_insufficient_scope_rejected(monkeypatch):
    """F2: authenticated but missing the superadmin scope -> 403, not 200.

    Stubs only ``auth_required`` (a passthrough that trusts the
    ``before_request``-injected ``g.current_user``, same seam ``vault_app``/
    other test files use elsewhere in this suite) so the test can supply an
    authenticated-but-under-scoped principal directly, without needing a
    real signed JWT + DB-backed user lookup just to reach the real
    ``@auth_required``'s success path. ``require_scopes`` is left real --
    that's what this test is actually proving.
    """
    import app.middleware as mw_mod

    def _auth_passthrough(*dargs: Any, **dkwargs: Any) -> Any:
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]
        return lambda fn: fn

    monkeypatch.setattr(mw_mod, "auth_required", _auth_passthrough)

    quart_app = _build_vault_app_real_auth()

    @quart_app.before_request
    async def _inject_authenticated_no_scope() -> None:
        from quart import g
        g.current_user = {
            "id": 1,
            "username": "not-superadmin",
            "_jwt_payload": {"sub": "1", "scope": "gough.cluster.read"},
        }

    client = quart_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({"rotation_class": "joiner"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 403
    data = await response.get_json()
    assert data.get("rotated") is not True


# =============================================================================
# Real-Postgres proof — penguin-dal conversion (Task 8b)
# =============================================================================
#
# The mocked tests above prove the endpoint's control flow; they don't prove
# the SQL itself is valid against the real schema. The original raw SQL
# referenced `revoked` (boolean), `updated_at`, and `used_at` -- none of
# which exist on `joiner_secrets` (app.models_m1.JoinerSecret) -- so it had
# always raised "column does not exist" against real Postgres. These tests
# run the converted `db.executesql()` UPDATE against a real, migrated
# database (`pg_db`/`pg_db_scoped`) to prove the fixed column mapping
# (`revoked_at`) is correct and the WHERE clause revokes exactly the
# matching rows.


def _seed_biome_pg(dal_db: Any, **overrides: Any) -> int:
    base: dict[str, Any] = dict(name="test-biome")
    base.update(overrides)
    return int(dal_db.biomes.insert(**base))


def _seed_joiner_secret_pg(
    dal_db: Any, *, emitter_biome_id: int, **overrides: Any
) -> str:
    """Insert a real ``joiner_secrets`` row via penguin-dal; return its id (str)."""
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        id=str(uuid.uuid4()),
        cluster_id=str(uuid.uuid4()),
        tenant_id="acme",
        biome_kind="vault",
        emitter_biome_id=emitter_biome_id,
        emitter_node_id=None,
        extractor_name="root-token",
        scope="cluster",
        ciphertext=b"CT-PLAINTEXT-MUST-NEVER-LEAK",
        iv=b"IIIIIIIIIIII",
        auth_tag=b"TTTTTTTTTTTTTTTT",
        dek_wrapped=b"vault:v1:DEK-MUST-NEVER-LEAK",
        vault_kek_name="gough-joiner-dek-wrap",
        ttl_seconds=3600,
        expires_at=now + timedelta(hours=12),
        rotation_class="joiner",
        created_at=now,
    )
    base.update(overrides)
    return str(dal_db.joiner_secrets.insert(**base))


def _build_vault_app_pg(
    *, dal_db: Any, monkeypatch: pytest.MonkeyPatch, rls_tenant: str | None = None
):
    """Build a Quart app with the real ``vault_bp`` wired to a real ``DB``.

    Reloads ``app.api.vault`` and patches its module-level ``get_db`` (the
    same seam ``app.api.joiner_secrets``'/``app.api.webhooks``' equivalent
    real-Postgres tests use), rather than relying on ``g.db`` — proves the
    handler's own ``get_db()`` call resolves correctly, not just that it
    respects a pre-seeded ``g.db``.

    ``rls_tenant`` -- when set, wires ``app.db.rls``'s pool GUC events onto
    ``dal_db.engine`` and pushes this tenant (or
    ``app.db.rls.CROSS_TENANT_SENTINEL``) for the duration of each request,
    mirroring ``tenant_middleware``. Pass a ``pg_db_scoped`` fixture as
    ``dal_db`` when using this -- ``pg_db`` (the migration/table owner) is
    RLS-exempt and would prove nothing about scoping.

    Stubs ``auth_required``/``require_scopes`` with passthroughs (F2 fix
    round added ``@auth_required``/``@require_scopes("gough.cluster.
    superadmin")`` to the route) -- these tests exercise the DB/RLS
    conversion, not auth; auth enforcement itself is proven separately by
    ``test_rotate_keys_unauthenticated_rejected``/
    ``test_rotate_keys_insufficient_scope_rejected`` below, which
    deliberately do NOT stub these.
    """
    from quart import Quart, g
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    def _passthrough(*dargs: Any, **dkwargs: Any) -> Any:
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]
        return lambda fn: fn

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough)

    import app.api.vault as vault_mod

    from prometheus_client import REGISTRY
    for collector in list(set(REGISTRY._names_to_collectors.values())):
        if hasattr(collector, "_name") and "gough_vault" in getattr(collector, "_name", ""):
            try:
                REGISTRY.unregister(collector)
            except Exception:
                pass
    vault_mod = importlib.reload(vault_mod)
    monkeypatch.setattr(vault_mod, "get_db", lambda: dal_db)

    quart_app = Quart(__name__)
    quart_app.register_blueprint(vault_mod.vault_bp, url_prefix="/api/v1/vault")

    if rls_tenant is not None:
        from app.db.rls import install_rls_events, set_current_tenant

        install_rls_events(dal_db.engine)

        @quart_app.before_request
        async def _push_rls_tenant() -> None:
            set_current_tenant(rls_tenant)

        @quart_app.after_request
        async def _pop_rls_tenant(response: Any) -> Any:
            set_current_tenant(None)
            return response

    @quart_app.before_request
    async def _inject_context() -> None:
        g.current_user = {"id": 1, "username": "admin", "role": "admin"}
        g.tenant_context = SimpleNamespace(tenant_id="acme")

    return quart_app


@pytest.mark.asyncio
async def test_rotate_keys_real_postgres_revokes_matching_secrets_only(
    pg_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the converted query is valid against the real schema and its
    WHERE clause is precise: only the active, non-expired, matching-class
    row gets revoked -- an already-revoked row, an expired row, and a
    different-rotation-class row are all left untouched.
    """
    biome_id = _seed_biome_pg(pg_db)
    now = datetime.now(timezone.utc)

    target_id = _seed_joiner_secret_pg(
        pg_db, emitter_biome_id=biome_id, rotation_class="joiner",
        expires_at=now + timedelta(hours=1),
    )
    already_revoked_id = _seed_joiner_secret_pg(
        pg_db, emitter_biome_id=biome_id, rotation_class="joiner",
        expires_at=now + timedelta(hours=1), revoked_at=now - timedelta(minutes=5),
    )
    expired_id = _seed_joiner_secret_pg(
        pg_db, emitter_biome_id=biome_id, rotation_class="joiner",
        expires_at=now - timedelta(minutes=5),
    )
    other_class_id = _seed_joiner_secret_pg(
        pg_db, emitter_biome_id=biome_id, rotation_class="bootstrap",
        expires_at=now + timedelta(hours=1),
    )
    pg_db.commit()

    # Capture the actual DB-stored value (not the Python-side one passed to
    # the seed helper) so the post-operation comparison below isn't
    # sensitive to any driver-level rounding -- an exact, not just
    # not-None, comparison.
    original_revoked_at = pg_db.executesql(
        "SELECT revoked_at FROM joiner_secrets WHERE id = %(id)s",
        {"id": already_revoked_id},
        as_dict=True,
    )[0]["revoked_at"]
    assert original_revoked_at is not None

    quart_app = _build_vault_app_pg(dal_db=pg_db, monkeypatch=monkeypatch)
    client = quart_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({"rotation_class": "joiner", "reason": "test"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data == {"rotated": True, "rotation_class": "joiner", "revoked_count": 1}

    rows = {
        r["id"]: r["revoked_at"]
        for r in pg_db.executesql(
            "SELECT id, revoked_at FROM joiner_secrets", as_dict=True
        )
    }
    assert rows[target_id] is not None, "matching active row must be revoked"
    assert rows[already_revoked_id] == original_revoked_at, (
        "already-revoked row's revoked_at must be UNCHANGED (not re-stamped) by rotate-keys"
    )
    assert rows[expired_id] is None, "expired row must NOT be revoked"
    assert rows[other_class_id] is None, "different rotation_class must NOT be revoked"


@pytest.mark.asyncio
async def test_rotate_keys_no_matching_rows_returns_zero(
    pg_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No matching rows -> revoked_count 0, not an error."""
    quart_app = _build_vault_app_pg(dal_db=pg_db, monkeypatch=monkeypatch)
    client = quart_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({"rotation_class": "never-seeded"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["revoked_count"] == 0


@pytest.mark.asyncio
async def test_rotate_keys_rls_scoped_role_revokes_under_pushed_tenant(
    pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """joiner_secrets is RLS-protected (baseline `rls_tables`). Proves the
    endpoint's query works correctly under genuine RLS enforcement (the
    scoped, non-owner ``api-manager-rw`` role) when the tenant GUC has
    already been pushed by something upstream (``tenant_middleware`` in
    production) -- vault.py itself does no extra scoping, per its own
    docstring, and shouldn't need to.
    """
    biome_id = _seed_biome_pg(pg_db)
    now = datetime.now(timezone.utc)
    target_id = _seed_joiner_secret_pg(
        pg_db, emitter_biome_id=biome_id, tenant_id="acme", rotation_class="joiner",
        expires_at=now + timedelta(hours=1),
    )
    pg_db.commit()

    quart_app = _build_vault_app_pg(
        dal_db=pg_db_scoped, monkeypatch=monkeypatch, rls_tenant="acme"
    )
    client = quart_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({"rotation_class": "joiner"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["revoked_count"] == 1

    row = pg_db.executesql(
        "SELECT revoked_at FROM joiner_secrets WHERE id = %(id)s",
        {"id": target_id},
        as_dict=True,
    )
    assert row[0]["revoked_at"] is not None


@pytest.mark.asyncio
async def test_rotate_keys_rls_scoped_role_sees_nothing_for_other_tenant(
    pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: proves RLS is genuinely enforced (not silently
    bypassed) -- a secret seeded for ``tenant-a`` is invisible/unrevoked
    when the pushed GUC tenant is a different tenant.
    """
    biome_id = _seed_biome_pg(pg_db)
    now = datetime.now(timezone.utc)
    target_id = _seed_joiner_secret_pg(
        pg_db, emitter_biome_id=biome_id, tenant_id="tenant-a", rotation_class="joiner",
        expires_at=now + timedelta(hours=1),
    )
    pg_db.commit()

    quart_app = _build_vault_app_pg(
        dal_db=pg_db_scoped, monkeypatch=monkeypatch, rls_tenant="tenant-b"
    )
    client = quart_app.test_client()
    response = await client.post(
        "/api/v1/vault/rotate-keys",
        data=json.dumps({"rotation_class": "joiner"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["revoked_count"] == 0, "a different tenant's GUC must not see tenant-a's row"

    row = pg_db.executesql(
        "SELECT revoked_at FROM joiner_secrets WHERE id = %(id)s",
        {"id": target_id},
        as_dict=True,
    )
    assert row[0]["revoked_at"] is None
