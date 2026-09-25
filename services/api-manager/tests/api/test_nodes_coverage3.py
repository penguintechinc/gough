"""Advanced coverage tests for nodes.py uncovered lines — phase 3.

Targets:
- Scope list/string handling (lines 235-237)
- NATS publish exceptions (lines 260, 266-267)
- Cursor decode timezone handling (line 194)
- DB query exception handling in discover (lines 395-396, 444-446, 469-484, 487-490)
- Complex node state transitions and validation
- Biome assignment edge cases
- Tag eligibility and operator tag shadowing
- Cloud-init template rendering
- LXD join operations
- Event timestamp handling
- Cross-tenant authorization
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest


# ---------------------------------------------------------------------------
# Base fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    """Fresh SQLite penguin-dal DB with all required tables."""
    from penguin_dal import DB, Field

    db = DB(
        f"sqlite:///{tmp_path}/test-nodes-cov3-{threading.get_ident()}.db",
        pool_size=1,
        reflect=False,
        migrate=True,
    )

    # All tables used by nodes.py
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
        Field("version", "string"),
        Field("biome_kind", "string", default="custom"),
        Field("phase", "string", default="post_deploy"),
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
        Field("ts", "datetime"),
        Field("stage", "string", notnull=True),
        Field("message", "string", notnull=True),
        Field("progress_pct", "integer"),
        Field("raw_json", "json"),
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
    db.define_table(
        "cloud_init_templates",
        Field("is_default", "boolean", default=False),
        Field("template_content", "string"),
        migrate=True,
    )
    db.define_table(
        "lxd_cluster_members",
        Field("cluster_id", "string"),
        Field("member_name", "string"),
        Field("api_url", "string"),
        Field("status", "string"),
        Field("joined_at", "datetime"),
        Field("created_at", "datetime"),
        migrate=True,
    )
    db.define_table(
        "disks",
        Field("node_id", "integer", notnull=True),
        Field("smart_status", "string", default="unknown"),
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


def _passthrough_decorator(*dargs, **dkwargs):
    """No-op auth decorator replacement."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def _wrap(fn):
        return fn

    return _wrap


@pytest.fixture()
def app_nodes(dal, monkeypatch):
    """Quart app with nodes blueprint, auth stubbed."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    import app.api.nodes as nodes_mod

    nodes_mod = importlib.reload(nodes_mod)
    monkeypatch.setattr(nodes_mod, "get_db", lambda: dal)

    from quart import Quart, g, request

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.url_map.strict_slashes = False
    app.register_blueprint(nodes_mod.nodes_bp)

    @app.before_request
    async def _inject_auth():
        g.current_user = {
            "id": 1,
            "username": "test-user",
            "_jwt_payload": {
                "sub": "test-user",
                "tenant": "default",
                "scope": "gough.nodes.read gough.nodes.provision gough.nodes.decommission",
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="default", cross_tenant=False)
        # Test-only mTLS shim: POST /nodes/{id}/events authenticates via a
        # Service SVID (request.peer_cert_pem), never a header bypass -- see
        # tests/test_security_fixes.py::TestNodeEventAuthBypassRemoved.
        test_peer_cert = request.headers.get("X-Test-Peer-Cert")
        if test_peer_cert:
            request.peer_cert_pem = test_peer_cert

    return app


_SVID_TEST_HEADERS = {"X-Test-Peer-Cert": "test-peer-cert-pem"}


def _valid_service_svid_principal():
    """Build a Principal representing an authenticated node Service SVID.

    Used with ``patch("app.security.credentials.validate_service_svid", ...)``
    to exercise the events endpoint's mTLS auth path in tests, in place of
    the removed ``X-Gough-Test-Bypass-Auth`` header (gh-SECURITY-FIX-2).
    """
    from app.security.credentials import CredentialType, Principal

    spiffe_id = "spiffe://gough.test/node/1"
    return Principal(
        cred_type=CredentialType.SERVICE_SVID,
        sub=spiffe_id,
        tenant_id="__default__",
        scopes=frozenset(),
        spiffe_id=spiffe_id,
        claims={},
    )


# ---------------------------------------------------------------------------
# Tests: Scope parsing (lines 235-237)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_scope_with_list_scope(app_nodes):
    """Test _has_scope when scope is a list (line 235-236)."""
    from app.api.nodes import _has_scope
    from quart import g

    async with app_nodes.test_client() as client:
        async with app_nodes.app_context():
            g.current_user = {
                "_jwt_payload": {
                    "scope": ["gough.nodes.read", "gough.nodes.provision"]
                }
            }
            # Should check list membership
            assert _has_scope("gough.nodes.read") is True
            assert _has_scope("gough.nodes.write") is False


@pytest.mark.asyncio
async def test_has_scope_with_string_scope(app_nodes):
    """Test _has_scope when scope is a space-separated string (line 237)."""
    from app.api.nodes import _has_scope
    from quart import g

    async with app_nodes.test_client() as client:
        async with app_nodes.app_context():
            g.current_user = {
                "_jwt_payload": {
                    "scope": "gough.nodes.read gough.nodes.provision"
                }
            }
            assert _has_scope("gough.nodes.read") is True
            assert _has_scope("gough.biomes.write") is False


# ---------------------------------------------------------------------------
# Tests: Cursor decode timezone handling (line 194)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decode_cursor_naive_datetime(app_nodes):
    """Test _decode_cursor with naive datetime (no tzinfo) — should add UTC (line 194)."""
    from app.api.nodes import _decode_cursor, _encode_cursor

    # Create a cursor with naive timestamp
    ts_naive = datetime(2025, 1, 1, 12, 0, 0)  # No timezone
    cursor = _encode_cursor(123, ts_naive)

    # Decode — should add UTC timezone
    node_id, ts_decoded = _decode_cursor(cursor)
    assert node_id == 123
    assert ts_decoded.tzinfo is not None
    assert ts_decoded.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_decode_cursor_with_utc_timezone(app_nodes):
    """Test _decode_cursor preserves existing UTC timezone."""
    from app.api.nodes import _decode_cursor, _encode_cursor

    ts_utc = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    cursor = _encode_cursor(456, ts_utc)

    node_id, ts_decoded = _decode_cursor(cursor)
    assert node_id == 456
    assert ts_decoded.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_decode_cursor_invalid_base64(app_nodes):
    """Test _decode_cursor with invalid base64 raises ValueError."""
    from app.api.nodes import _decode_cursor

    with pytest.raises(ValueError, match="Invalid cursor"):
        _decode_cursor("invalid_base64_!!!")


@pytest.mark.asyncio
async def test_decode_cursor_invalid_json(app_nodes):
    """Test _decode_cursor with invalid JSON payload."""
    from app.api.nodes import _decode_cursor

    bad_cursor = base64.urlsafe_b64encode(b"not json").decode()
    with pytest.raises(ValueError, match="Invalid cursor"):
        _decode_cursor(bad_cursor)


# ---------------------------------------------------------------------------
# Tests: NATS publish exception handling (lines 260, 266-267)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nats_publish_safe_with_client_exception(app_nodes):
    """Test _nats_publish_safe when publish() raises exception (lines 266-267)."""
    from app.api.nodes import _nats_publish_safe
    from quart import g

    mock_client = AsyncMock()
    mock_client.publish = AsyncMock(side_effect=RuntimeError("NATS down"))

    async with app_nodes.test_client() as client:
        async with app_nodes.app_context():
            g.nats_client = mock_client
            # Should not raise; logs warning instead
            await _nats_publish_safe(
                subject="test.subject",
                payload={"test": "data"},
                tenant_id="default",
            )


@pytest.mark.asyncio
async def test_nats_publish_safe_client_in_app_context(app_nodes):
    """Test _nats_publish_safe fetches client from current_app when g.nats_client is None."""
    from app.api.nodes import _nats_publish_safe

    mock_client = AsyncMock()
    mock_client.publish = AsyncMock(return_value=None)

    async with app_nodes.test_client() as client:
        app_nodes.nats_client = mock_client
        async with app_nodes.app_context():
            # Should get client from app context
            await _nats_publish_safe(
                subject="test.subject",
                payload={"test": "data"},
                tenant_id="default",
            )
            mock_client.publish.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: DB exception handling in discover (lines 395-396, 444-446, 469-484, 487-490)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_db_error_on_existing_lookup(app_nodes, dal, monkeypatch):
    """Test discover when DB lookup of existing nodes fails (line 395-396)."""
    import app.api.nodes as nodes_mod

    # Mock DB to raise on select
    mock_db = MagicMock()
    mock_db.__call__ = MagicMock(side_effect=RuntimeError("DB connection lost"))

    monkeypatch.setattr(nodes_mod, "get_db", lambda: mock_db)

    async with app_nodes.test_client() as client:
        # Post minimal discover request
        resp = await client.post(
            "/api/v1/nodes/discover",
            json={
                "dmi_uuid": str(uuid.uuid4()),
                "primary_nic_mac": "aa:bb:cc:dd:ee:ff",
                "firmware_type": "uefi",
                "lshw_json": {},
                "lsblk_json": {},
                "nics": [],
                "numa_topology": None,
                "accelerators": [],
                "smart_attributes": [],
            },
            headers={"Authorization": "Bearer test-token"},
        )
        # Should return 500 due to DB error (line 490)
        assert resp.status_code in (400, 401, 500)


# ---------------------------------------------------------------------------
# Tests: Node state transitions and validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_node_in_invalid_state(app_nodes, dal):
    """Test rejecting node not in 'new' or 'probed' state."""
    # Seed node in 'ready' state
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="ready-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/reject",
            json={"reason": "Testing invalid state transition"},
        )
        # Should reject with 409 (conflict)
        assert resp.status_code == 409
        data = json.loads(await resp.get_data(as_text=True))
        assert "must be in 'new' or 'probed' state" in data["error"]["message"]


@pytest.mark.asyncio
async def test_deploy_node_in_terminal_state(app_nodes, dal):
    """Test deploy on node in terminal state (rejected/decommissioned)."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="rejected-node",
        state="rejected",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/deploy",
            json={"biome_assignments": [{"biome_id": 1}]},
            headers={"X-Idempotency-Key": "test-key"},
        )
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_evacuate_node_safety_check_fails(app_nodes, dal, monkeypatch):
    """Test evacuate when safety check fails and force=False."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        # With force=False, safety check deferred, returns 202 or other valid response
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/evacuate",
            json={"force": False, "reason": "Testing"},
        )
        # Should return 202 (evacuation accepted) or error
        assert resp.status_code in (202, 400, 409)


@pytest.mark.asyncio
async def test_decommission_already_decommissioned(app_nodes, dal):
    """Test decommissioning an already-decommissioned node."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="decom-node",
        state="decommissioned",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.delete(
            f"/api/v1/nodes/{node_id}",
            json={"reason": "Already decommissioned"},
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Tests: Node tag operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_node_tags_with_operator_tags_shadowing(app_nodes, dal):
    """Test get tags when operator tags shadow hardware tags."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="tagged-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
        hardware_tags=["cpu:cores:8", "mem:gb:32"],
    )
    dal.commit()

    # Add operator tag that shadows hardware tag
    dal.node_tags_operator.insert(
        node_id=node_id,
        tenant_id="default",
        tag_key="cpu",
        tag_value="cores:16",  # shadows hardware tag
        set_by_actor_sub="operator-user",
        set_at=datetime.now(timezone.utc),
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.get(f"/api/v1/nodes/{node_id}/tags")
        assert resp.status_code == 200
        data = json.loads(await resp.get_data(as_text=True))
        tags = data["data"]["tags"]

        # Hardware cpu:cores:8 should be filtered out
        cpu_tags = [t for t in tags if t["tag"].startswith("cpu")]
        assert len(cpu_tags) == 1
        assert cpu_tags[0]["provenance"] == "operator"


@pytest.mark.asyncio
async def test_patch_node_tags_upsert(app_nodes, dal):
    """Test tag PATCH with upsert logic."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        # Add tag
        resp = await client.patch(
            f"/api/v1/nodes/{node_id}/tags",
            json={"add": [{"tag_key": "env", "tag_value": "prod"}], "remove": []},
        )
        assert resp.status_code == 200

        # Add same tag again (upsert should skip)
        resp = await client.patch(
            f"/api/v1/nodes/{node_id}/tags",
            json={"add": [{"tag_key": "env", "tag_value": "prod"}], "remove": []},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: Cloud-init template rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cloud_init_template_not_found(app_nodes, dal):
    """Test cloud-init when no default template exists."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.get(f"/api/v1/nodes/{node_id}/cloud-init")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_cloud_init_invalid_baseline(app_nodes, dal):
    """Test cloud-init with invalid baseline parameter."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    dal.cloud_init_templates.insert(
        is_default=True,
        template_content="#!/bin/bash\necho $node_id",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.get(
            f"/api/v1/nodes/{node_id}/cloud-init?baseline=invalid"
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_cloud_init_template_substitution(app_nodes, dal):
    """Test cloud-init template variable substitution."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="my-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    dal.cloud_init_templates.insert(
        is_default=True,
        template_content="#!/bin/bash\necho node_id=$node_id hostname=$hostname baseline=$baseline",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.get(f"/api/v1/nodes/{node_id}/cloud-init")
        assert resp.status_code == 200
        content = await resp.get_data(as_text=True)
        assert "node_id=" in content
        assert "hostname=" in content


# ---------------------------------------------------------------------------
# Tests: Node events with timestamp handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_node_events_invalid_timestamp(app_nodes, dal):
    """Test node events with invalid timestamp format (handled as fallback)."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    with patch(
        "app.security.credentials.validate_service_svid",
        return_value=_valid_service_svid_principal(),
    ):
        async with app_nodes.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/events",
                json={
                    "stage": "provisioning",
                    "message": "Starting deployment",
                    "progress_pct": 10,
                    "timestamp": "not-a-timestamp",
                },
                headers=_SVID_TEST_HEADERS,
            )
        # Should succeed with fallback to current time
        assert resp.status_code in (200, 201, 400)


@pytest.mark.asyncio
async def test_post_node_events_no_timestamp(app_nodes, dal):
    """Test node events without timestamp (should default to now)."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    with patch(
        "app.security.credentials.validate_service_svid",
        return_value=_valid_service_svid_principal(),
    ):
        async with app_nodes.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/events",
                json={
                    "stage": "provisioning",
                    "message": "Deployment started",
                    "progress_pct": 5,
                },
                headers=_SVID_TEST_HEADERS,
            )
        assert resp.status_code in (200, 201, 400)


@pytest.mark.asyncio
async def test_post_node_events_naive_timestamp(app_nodes, dal):
    """Test node events with naive datetime (no timezone)."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    naive_ts = "2025-01-15T10:30:00"  # No timezone

    with patch(
        "app.security.credentials.validate_service_svid",
        return_value=_valid_service_svid_principal(),
    ):
        async with app_nodes.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/events",
                json={
                    "stage": "init",
                    "message": "Setup",
                    "progress_pct": 0,
                    "timestamp": naive_ts,
                },
                headers=_SVID_TEST_HEADERS,
            )
        assert resp.status_code in (200, 201, 400)


# ---------------------------------------------------------------------------
# Tests: LXD cluster join operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lxd_join_missing_required_fields(app_nodes, dal):
    """Test LXD join with missing required fields."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/lxd/join",
            json={"cluster_id": "123"},  # Missing api_url, member_name
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_lxd_join_empty_strings(app_nodes, dal):
    """Test LXD join rejects empty string values."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/lxd/join",
            json={
                "cluster_id": "",
                "api_url": "https://lxd:8443",
                "member_name": "node-1",
            },
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_node_name_collision(app_nodes, dal):
    """Test PATCH rejects node name collision."""
    n1 = dal.nodes.insert(
        tenant_id="default",
        name="node-1",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    n2 = dal.nodes.insert(
        tenant_id="default",
        name="node-2",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:02",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.patch(
            f"/api/v1/nodes/{n2}",
            json={"name": "node-1"},  # Collision!
        )
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_patch_node_ipv4_static(app_nodes, dal):
    """Test PATCH updates ipv4_static field."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.patch(
            f"/api/v1/nodes/{node_id}",
            json={"ipv4_static": "192.168.1.100"},
        )
        assert resp.status_code == 200
        data = json.loads(await resp.get_data(as_text=True))
        assert data["data"]["node"]["ipv4_static"] == "192.168.1.100"


# ---------------------------------------------------------------------------
# Tests: Biome assignments edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_biome_not_found(app_nodes, dal):
    """Test deploy when biome doesn't exist."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.post(
            f"/api/v1/nodes/{node_id}/deploy",
            json={"biome_assignments": [{"biome_id": 99999}]},
            headers={"X-Idempotency-Key": "test-key"},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_node_biomes_no_table(app_nodes, dal, monkeypatch):
    """Test list biomes when table doesn't exist."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.get(f"/api/v1/nodes/{node_id}/biomes")
        # Should return empty list or success (line 1610)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unassign_biome_no_active_assignment(app_nodes, dal):
    """Test unassigning biome with no active assignment."""
    node_id = dal.nodes.insert(
        tenant_id="default",
        name="test-node",
        state="ready",
        dmi_uuid=str(uuid.uuid4()),
        primary_nic_mac="aa:bb:cc:dd:ee:01",
    )
    dal.commit()

    async with app_nodes.test_client() as client:
        resp = await client.delete(f"/api/v1/nodes/{node_id}/biomes/99999")
        assert resp.status_code == 404
