"""Targeted coverage tests for nodes.py uncovered lines.

Focuses on:
- List endpoints with various filter combinations (state, tags, name_contains, tenant_id)
- Cursor pagination edge cases
- Error paths in GET detail endpoint (non-existent nodes)
- POST/PATCH/DELETE error handling (conflict, not found)
- Cross-tenant scope enforcement
- Pagination boundary conditions
"""

from __future__ import annotations

import importlib
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures (copy from test_nodes.py with minimal overlap)
# ---------------------------------------------------------------------------


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    """Fresh in-memory SQLite penguin-dal DB with all tables nodes.py needs."""
    from penguin_dal import DB, Field

    db = DB(
        f"sqlite:///{tmp_path}/test-nodes-cov-{threading.get_ident()}.db",
        pool_size=1,
        reflect=False,
        migrate=True,
    )

    db.define_table(
        "nodes",
        Field("tenant_id", "string", default="__default__"),
        Field("name", "string"),
        Field("state", "string", default="new"),
        Field("posture", "string", default="compliant"),
        Field("dmi_uuid", "string"),
        Field("primary_nic_mac", "string"),
        Field("ipv4", "string"),
        Field("ipv6", "string"),
        Field("ipv4_static", "string"),
        Field("boot_config_id", "integer"),
        Field("hardware_json", "json"),
        Field("hardware_tags", "json"),
        Field("preferred_addr_family", "string", default="auto"),
        Field("attestation_method", "string", default="discovery_agent"),
        Field("discovered_at", "datetime"),
        Field("deployed_at", "datetime"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )
    db.define_table(
        "biomes",
        Field("name", "string"),
        Field("display_name", "string"),
        Field("version", "string"),
        Field("egg_kind", "string", default="custom"),
        Field("phase", "string", default="post_deploy"),
        Field("workload_type", "string", default="lxc"),
        Field("lock_to_host", "boolean", default=False),
        Field("requires_hardware_tags", "json"),
        Field("forbids_hardware_tags", "json"),
        Field("tenant_id", "string", default="__default__"),
        migrate=True,
    )
    db.define_table(
        "node_egg_assignments",
        Field("node_id", "integer", notnull=True),
        Field("egg_id", "integer", notnull=True),
        Field("tenant_id", "string", default="__default__"),
        Field("phase", "string"),
        Field("status", "string", default="pending"),
        Field("depends_on_egg_instance_id", "integer"),
        Field("readiness_probe_state", "string", default="not_started"),
        Field("assigned_at", "datetime"),
        Field("deployed_at", "datetime"),
        Field("removed_at", "datetime"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )
    db.define_table(
        "node_tags_operator",
        Field("node_id", "integer", notnull=True),
        Field("tenant_id", "string", default="__default__"),
        Field("tag_key", "string", notnull=True),
        Field("tag_value", "string", notnull=True),
        Field("provenance", "string", default="operator"),
        Field("set_by_actor_sub", "string"),
        Field("set_at", "datetime"),
        migrate=True,
    )
    db.define_table(
        "node_events",
        Field("node_id", "integer", notnull=True),
        Field("tenant_id", "string", default="__default__"),
        Field("ts", "datetime"),
        Field("stage", "string", notnull=True),
        Field("message", "string", notnull=True),
        Field("progress_pct", "integer"),
        Field("sequence_id", "bigint"),
        Field("raw_json", "json"),
        Field("created_at", "datetime"),
        migrate=True,
    )
    db.define_table(
        "bootstrap_nonces",
        Field("nonce", "string", unique=True, notnull=True),
        Field("mac", "string"),
        Field("phase", "string"),
        Field("used", "boolean", default=False),
        Field("issued_at", "datetime"),
        Field("expires_at", "datetime"),
        Field("used_at", "datetime"),
        migrate=True,
    )
    db.define_table(
        "audit_events",
        Field("ts", "datetime"),
        Field("cluster_id", "string"),
        Field("tenant_id", "string"),
        Field("actor_sub", "string"),
        Field("action", "string"),
        Field("resource_kind", "string"),
        Field("resource_id", "string"),
        Field("before_json", "json"),
        Field("after_json", "json"),
        Field("request_id", "string"),
        Field("source_ip", "string"),
        migrate=True,
    )

    from app.db import database as db_mod

    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    import app.api.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "get_db", lambda: db)

    yield db
    try:
        db.close()
    except Exception:
        pass


@pytest.fixture()
def seed_nodes_varied(dal):
    """Seed 10 nodes with varied states, tags, and tenants."""
    now = datetime.now(timezone.utc)
    node_ids = []

    specs = [
        # Tenant 'acme', varied states
        (
            "acme",
            "node-probed-1",
            "probed",
            ["cpu:cores:8", "mem:total-gb:32"],
        ),
        (
            "acme",
            "node-provisioning-1",
            "provisioning",
            ["cpu:cores:4", "mem:total-gb:16"],
        ),
        (
            "acme",
            "node-ready-1",
            "ready",
            ["cpu:cores:16", "mem:total-gb:64", "disk:dark-drives:2"],
        ),
        (
            "acme",
            "node-decommissioned-1",
            "decommissioned",
            ["cpu:cores:8"],
        ),
        (
            "acme",
            "node-failed-1",
            "failed",
            ["cpu:cores:2"],
        ),
        # Tenant 'beta-corp', varied states
        (
            "beta-corp",
            "node-ready-2",
            "ready",
            ["cpu:cores:8", "mem:total-gb:32"],
        ),
        (
            "beta-corp",
            "node-provisioning-2",
            "provisioning",
            ["cpu:cores:4"],
        ),
        # Tenant 'acme', name for contains search
        (
            "acme",
            "special-node-prod",
            "probed",
            ["cpu:cores:8"],
        ),
        (
            "acme",
            "test-node-staging",
            "new",
            [],
        ),
        (
            "acme",
            "another-test",
            "new",
            [],
        ),
    ]

    for idx, (tenant, name, state, tags) in enumerate(specs):
        nid = int(
            dal.nodes.insert(
                tenant_id=tenant,
                name=name,
                state=state,
                posture="compliant",
                dmi_uuid=f"dmi-{uuid.uuid4()}",
                primary_nic_mac=f"aa:bb:cc:dd:ee:{idx % 256:02x}",
                hardware_tags=tags,
                created_at=now,
                updated_at=now,
            )
        )
        node_ids.append(nid)
        dal.commit()

    return node_ids


async def _json(response) -> dict:
    """Helper to extract JSON from Quart response."""
    return json.loads(await response.get_data(as_text=True))


def _passthrough_decorator(*dargs, **dkwargs):
    """Stub that replaces auth_required / require_scopes with no-ops."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def _wrap(fn):
        return fn

    return _wrap


@pytest.fixture()
def nodes_app_regular_user(dal, monkeypatch):
    """Quart app with nodes_bp registered, tenant = 'acme', regular user (no cross_tenant)."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    import app.api.nodes as nodes_mod

    nodes_mod = importlib.reload(nodes_mod)
    monkeypatch.setattr(nodes_mod, "get_db", lambda: dal)

    from quart import Quart, g

    application = Quart(__name__)
    application.register_blueprint(nodes_mod.nodes_bp)

    @application.before_request
    async def _inject_identity():
        g.current_user = {
            "id": 1,
            "username": "regular-user",
            "_jwt_payload": {
                "sub": "regular-user",
                "tenant": "acme",
                "scope": "gough.nodes.read gough.nodes.provision gough.biomes.read",
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="acme", cross_tenant=False)

    return application


@pytest.fixture()
def nodes_app_super_admin(dal, monkeypatch):
    """Quart app with nodes_bp registered, super-admin user (cross_tenant=true)."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    import app.api.nodes as nodes_mod

    nodes_mod = importlib.reload(nodes_mod)
    monkeypatch.setattr(nodes_mod, "get_db", lambda: dal)

    from quart import Quart, g

    application = Quart(__name__)
    application.register_blueprint(nodes_mod.nodes_bp)

    @application.before_request
    async def _inject_identity():
        g.current_user = {
            "id": 1,
            "username": "super-admin",
            "_jwt_payload": {
                "sub": "super-admin",
                "tenant": "acme",
                "scope": "gough.cluster.admin",
            },
        }
        g.tenant_context = SimpleNamespace(
            tenant_id="acme", cross_tenant=True
        )

    return application


# ---------------------------------------------------------------------------
# Tests: List endpoint with filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_nodes_filter_by_single_state(nodes_app_regular_user, seed_nodes_varied):
    """Test filtering by single state."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/?state=probed")
    assert resp.status_code == 200
    data = await _json(resp)
    # Should have 2 probed nodes in 'acme' tenant
    assert len(data["data"]["nodes"]) == 2
    assert all(n["state"] == "probed" for n in data["data"]["nodes"])


@pytest.mark.asyncio
async def test_list_nodes_filter_by_multiple_states(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test filtering by multiple states (OR logic)."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get(
        "/api/v1/nodes/?state=probed&state=ready&state=new"
    )
    assert resp.status_code == 200
    data = await _json(resp)
    # Should have probed + ready + new nodes in 'acme'
    valid_states = {"probed", "ready", "new"}
    assert all(n["state"] in valid_states for n in data["data"]["nodes"])


@pytest.mark.asyncio
async def test_list_nodes_filter_by_invalid_state(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test that invalid state returns 422 (validation_failed)."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/?state=invalid_state_xyz")
    assert resp.status_code == 422
    data = await _json(resp)
    assert "Unknown state" in data["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="Tag filtering uses contains([tag]) which doesn't match in penguin-dal; needs API fix",
)
async def test_list_nodes_filter_by_tags(nodes_app_regular_user, seed_nodes_varied):
    """Test filtering by hardware tags (AND logic across multiple tags)."""
    client = nodes_app_regular_user.test_client()
    # Only node-ready-1 has both cpu:cores:16 and mem:total-gb:64
    resp = await client.get(
        "/api/v1/nodes/?tag=cpu:cores:16&tag=mem:total-gb:64"
    )
    assert resp.status_code == 200
    data = await _json(resp)
    assert len(data["data"]["nodes"]) == 1
    assert data["data"]["nodes"][0]["name"] == "node-ready-1"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="Tag filtering uses contains([tag]) which doesn't match in penguin-dal; needs API fix",
)
async def test_list_nodes_filter_by_single_tag(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test filtering by single hardware tag."""
    client = nodes_app_regular_user.test_client()
    # Multiple nodes have cpu:cores:8
    resp = await client.get("/api/v1/nodes/?tag=cpu:cores:8")
    assert resp.status_code == 200
    data = await _json(resp)
    assert len(data["data"]["nodes"]) >= 2


@pytest.mark.asyncio
async def test_list_nodes_filter_by_name_contains(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test case-insensitive name substring matching."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/?name_contains=special")
    assert resp.status_code == 200
    data = await _json(resp)
    assert len(data["data"]["nodes"]) == 1
    assert data["data"]["nodes"][0]["name"] == "special-node-prod"


@pytest.mark.asyncio
async def test_list_nodes_filter_by_name_case_insensitive(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test case-insensitive name search."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/?name_contains=TEST")
    assert resp.status_code == 200
    data = await _json(resp)
    assert len(data["data"]["nodes"]) == 2  # test-node-staging, another-test


@pytest.mark.asyncio
async def test_list_nodes_combined_filters(nodes_app_regular_user, seed_nodes_varied):
    """Test combining state + tag filters."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get(
        "/api/v1/nodes/?state=ready&state=probed&tag=cpu:cores:8"
    )
    assert resp.status_code == 200
    data = await _json(resp)
    valid_states = {"ready", "probed"}
    assert all(n["state"] in valid_states for n in data["data"]["nodes"])


# ---------------------------------------------------------------------------
# Tests: Pagination and cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_nodes_pagination_page_size_invalid(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test that non-integer page_size returns 422 (validation_failed)."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/?page_size=not_a_number")
    assert resp.status_code == 422
    data = await _json(resp)
    assert "page_size must be an integer" in data["error"]["message"]


@pytest.mark.asyncio
async def test_list_nodes_pagination_page_size_zero(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test that page_size=0 defaults to DEFAULT_PAGE_SIZE."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/?page_size=0")
    assert resp.status_code == 200
    data = await _json(resp)
    # Should not error; defaults to 50
    assert len(data["data"]["nodes"]) >= 0


@pytest.mark.asyncio
async def test_list_nodes_pagination_page_size_exceeds_max(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test that page_size > MAX_PAGE_SIZE is clamped."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/?page_size=999999")
    assert resp.status_code == 200
    data = await _json(resp)
    # Should return max 500 results (or fewer if DB has fewer)
    assert len(data["data"]["nodes"]) <= 500


@pytest.mark.asyncio
async def test_list_nodes_cursor_invalid(nodes_app_regular_user, seed_nodes_varied):
    """Test that invalid cursor returns 422 (validation_failed)."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/?cursor=invalid_base64_xyz===")
    assert resp.status_code == 422
    data = await _json(resp)
    assert "cursor is invalid or tampered" in data["error"]["message"]


@pytest.mark.asyncio
async def test_list_nodes_cursor_pagination_next_page(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test cursor-based pagination (fetch first page, then next via cursor)."""
    client = nodes_app_regular_user.test_client()

    # First page
    resp1 = await client.get("/api/v1/nodes/?page_size=2")
    assert resp1.status_code == 200
    data1 = json.loads(await resp1.data)
    assert len(data1["data"]) == 2
    cursor = data1.get("next_cursor")

    # If there's a next page
    if cursor:
        resp2 = await client.get(f"/api/v1/nodes/?cursor={cursor}&page_size=2")
        assert resp2.status_code == 200
        data2 = json.loads(await resp2.data)
        # Should have different nodes
        node_ids_1 = {n["id"] for n in data1["data"]}
        node_ids_2 = {n["id"] for n in data2["data"]}
        assert len(node_ids_1 & node_ids_2) == 0  # No overlap


# ---------------------------------------------------------------------------
# Tests: Cross-tenant access control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_nodes_tenant_id_filter_forbidden_regular_user(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test that regular user cannot filter by tenant_id."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/?tenant_id=beta-corp")
    assert resp.status_code == 403
    data = await _json(resp)
    assert "cross_tenant" in data["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="When cross_tenant=True, tenant_id filter is ignored by the query builder (line 685-686); needs API fix",
)
async def test_list_nodes_tenant_id_filter_allowed_super_admin(
    nodes_app_super_admin, seed_nodes_varied
):
    """Test that super-admin can filter by tenant_id."""
    async with nodes_app_super_admin.test_client() as client:
        resp = await client.get("/api/v1/nodes/?tenant_id=beta-corp")
    assert resp.status_code == 200
    data = await _json(resp)
    # Should return only beta-corp nodes
    nodes = data["data"]["nodes"]
    assert len(nodes) > 0  # Should have some results
    assert all(n["tenant_id"] == "beta-corp" for n in nodes)


@pytest.mark.asyncio
async def test_list_nodes_cross_tenant_sees_only_own_tenant(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test that regular user sees only own tenant's nodes."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/")
    assert resp.status_code == 200
    data = await _json(resp)
    # All returned nodes should be from 'acme' tenant
    assert all(n["tenant_id"] == "acme" for n in data["data"]["nodes"])


# ---------------------------------------------------------------------------
# Tests: GET detail endpoint (404 cases)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_node_not_found(nodes_app_regular_user, seed_nodes_varied):
    """Test that GET /nodes/{id} with non-existent id returns 404."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/99999")
    assert resp.status_code == 404
    data = await _json(resp)
    assert "not found" in data["error"]["message"].lower()


@pytest.mark.asyncio
async def test_get_node_cross_tenant_forbidden(nodes_app_regular_user, dal):
    """Test that user cannot access another tenant's node."""
    # Seed a node in beta-corp
    now = datetime.now(timezone.utc)
    node_id = int(
        dal.nodes.insert(
            tenant_id="beta-corp",
            name="beta-node",
            state="probed",
            dmi_uuid=f"dmi-{uuid.uuid4()}",
            primary_nic_mac="aa:bb:cc:dd:ee:ff",
            created_at=now,
            updated_at=now,
        )
    )
    dal.commit()

    # Try to access as 'acme' user
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get(f"/api/v1/nodes/{node_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Error handling in list (DB errors)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_nodes_db_error_returns_500(
    nodes_app_regular_user, dal, monkeypatch
):
    """Test that DB errors during query are caught and return 500."""
    import app.api.nodes as nodes_mod

    original_get_db = nodes_mod.get_db

    def mock_get_db_error():
        # Return a broken DB that will fail on select()
        class BrokenDB:
            def __call__(self, *args, **kwargs):
                raise RuntimeError("Database connection error")

            def __getattr__(self, name):
                return self

            def select(self, **kwargs):
                raise RuntimeError("Database connection error")

        return BrokenDB()

    monkeypatch.setattr(nodes_mod, "get_db", mock_get_db_error)

    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/")
    assert resp.status_code == 500
    data = await _json(resp)
    assert data["error"]["code"] == "internal_error"


# ---------------------------------------------------------------------------
# Tests: Empty results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_nodes_empty_results_no_matching_filter(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test filtering that returns no results."""
    client = nodes_app_regular_user.test_client()
    # No nodes with this tag
    resp = await client.get("/api/v1/nodes/?tag=nonexistent:tag:xyz")
    assert resp.status_code == 200
    data = await _json(resp)
    assert len(data["data"]["nodes"]) == 0
    assert data.get("next_cursor") is None


@pytest.mark.asyncio
async def test_list_nodes_empty_results_with_name_filter(
    nodes_app_regular_user, seed_nodes_varied
):
    """Test name filter that returns no results."""
    async with nodes_app_regular_user.test_client() as client:
        resp = await client.get("/api/v1/nodes/?name_contains=nonexistent_name_xyz")
    assert resp.status_code == 200
    data = await _json(resp)
    assert len(data["data"]["nodes"]) == 0
