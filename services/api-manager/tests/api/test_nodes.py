"""Comprehensive test suite for the Nodes Blueprint.

Coverage targets: every endpoint × success + 401/403/404/409/422 cases.
SPIRE, Vault, NATS, and Redis are mocked via unittest.mock.patch.

Database: fresh in-process SQLite via penguin-dal (same strategy as conftest.py).
Authentication: the ``nodes_app`` fixture stubs ``auth_required`` with a passthrough
and injects a pre-built ``g.current_user`` / ``g.tenant_context`` so that JWT scopes
and tenant isolation can be tested deterministically without a real OIDC server.
"""

from __future__ import annotations

import importlib
import json
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    """Fresh in-memory SQLite penguin-dal DB with all tables nodes.py needs."""
    from penguin_dal import DB, Field

    db = DB(
        f"sqlite:///{tmp_path}/test-nodes-{threading.get_ident()}.db",
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
def seed_node(dal):
    """Insert a node in 'probed' state for tenant 'acme'."""
    now = datetime.now(timezone.utc)
    node_id = int(
        dal.nodes.insert(
            tenant_id="acme",
            name="test-node-1",
            state="probed",
            posture="compliant",
            dmi_uuid="aabb-ccdd-1122",
            primary_nic_mac="aa:bb:cc:dd:ee:01",
            hardware_tags=["cpu:cores:8", "mem:total-gb:32"],
            created_at=now,
            updated_at=now,
        )
    )
    dal.commit()
    return node_id


@pytest.fixture()
def seed_egg(dal):
    """Insert a simple biome."""
    egg_id = int(
        dal.biomes.insert(
            name="test-biome",
            display_name="Test Biome",
            version="1.0.0",
            egg_kind="custom",
            phase="post_deploy",
            workload_type="lxc",
            lock_to_host=False,
            requires_hardware_tags=[],
            forbids_hardware_tags=[],
            tenant_id="acme",
        )
    )
    dal.commit()
    return egg_id


def _passthrough_decorator(*dargs, **dkwargs):
    """Stub that replaces auth_required / require_scopes with no-ops."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def _wrap(fn):
        return fn

    return _wrap


@pytest.fixture()
def nodes_app(dal, monkeypatch):
    """Quart app with nodes_bp registered, auth stubbed, tenant = 'acme'."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    import app.api.nodes as nodes_mod

    nodes_mod = importlib.reload(nodes_mod)
    monkeypatch.setattr(nodes_mod, "get_db", lambda: dal)

    from quart import Quart, g, request

    application = Quart(__name__)
    application.register_blueprint(nodes_mod.nodes_bp)

    @application.before_request
    async def _inject_identity():
        g.current_user = {
            "id": 1,
            "username": "tester",
            "_jwt_payload": {
                "sub": "tester",
                "tenant": "acme",
                "scope": (
                    "gough.nodes.read gough.nodes.provision "
                    "gough.nodes.decommission gough.biomes.deploy "
                    "gough.biomes.read gough.cluster.admin"
                ),
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="acme", cross_tenant=False)
        # Test-only mTLS shim: the discovery-agent/cloud-init events endpoint
        # (POST /nodes/{id}/events) authenticates via a Service SVID
        # (request.peer_cert_pem), never via a header bypass -- see
        # tests/test_security_fixes.py::TestNodeEventAuthBypassRemoved. Tests
        # that need to exercise that endpoint set this header and pair it
        # with a patch of app.security.credentials.validate_service_svid
        # (see _valid_service_svid_principal below) rather than relying on
        # any production auth-bypass logic.
        test_peer_cert = request.headers.get("X-Test-Peer-Cert")
        if test_peer_cert:
            request.peer_cert_pem = test_peer_cert

    return application


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


@pytest.fixture()
def super_admin_app(dal, monkeypatch):
    """Quart app with cross_tenant=True super-admin identity."""
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
    async def _inject_super_admin():
        g.current_user = {
            "id": 99,
            "username": "superadmin",
            "_jwt_payload": {
                "sub": "superadmin",
                "tenant": "acme",
                "cross_tenant": True,
                "scope": "gough.cluster.superadmin gough.nodes.decommission gough.nodes.provision",
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="acme", cross_tenant=True)

    return application


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _json(response) -> dict:
    return json.loads(await response.get_data(as_text=True))


def _discover_payload(**overrides) -> dict:
    base: dict[str, Any] = {
        "dmi_uuid": str(uuid.uuid4()),
        "primary_nic_mac": "de:ad:be:ef:00:01",
        "firmware_type": "uefi",
        "nics": [
            {"mac": "de:ad:be:ef:00:01", "is_primary": True}
        ],
        "hardware_tags": ["cpu:cores:4", "mem:total-gb:16"],
    }
    base.update(overrides)
    return base


async def _post_discover(client, payload: dict, *, token: str = "valid-token") -> Any:
    return await client.post(
        "/api/v1/nodes/discover",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


# ---------------------------------------------------------------------------
# POST /api/v1/nodes/discover
# ---------------------------------------------------------------------------


class TestDiscover:
    """POST /api/v1/nodes/discover"""

    @pytest.mark.asyncio
    async def test_discover_creates_new_node(self, nodes_app, dal):
        """Happy path: new node created, 201 returned."""
        from unittest.mock import patch as mpatch

        mock_principal = MagicMock()
        mock_principal.sub = "bootstrap:de:ad:be:ef:00:01"

        with mpatch(
            "app.security.credentials.validate_one_time_bootstrap_token",
            return_value=mock_principal,
        ), mpatch("app.clients.spire.SpireClient.register_workload", return_value="entry-123"):
            async with nodes_app.test_client() as client:
                resp = await _post_discover(client, _discover_payload())

        assert resp.status_code == 201
        data = await _json(resp)
        assert data["status"] == "success"
        assert data["data"]["state"] == "probed"
        assert "node_id" in data["data"]
        assert "spire_join_token" in data["data"]
        assert "control_tunnel_endpoint" in data["data"]

        node_id = data["data"]["node_id"]
        nodes = dal(dal.nodes.id == node_id).select()
        assert len(nodes) == 1
        node = nodes[0]
        assert node.state == "probed"

    @pytest.mark.asyncio
    async def test_discover_missing_bearer_token(self, nodes_app):
        """401 when no Authorization header is sent."""
        async with nodes_app.test_client() as client:
            resp = await client.post(
                "/api/v1/nodes/discover",
                json=_discover_payload(),
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_discover_expired_token(self, nodes_app):
        """401 when the bootstrap token is expired."""
        from app.security.credentials import ExpiredCredentialError

        with patch(
            "app.security.credentials.validate_one_time_bootstrap_token",
            side_effect=ExpiredCredentialError("expired"),
        ):
            async with nodes_app.test_client() as client:
                resp = await _post_discover(client, _discover_payload())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_discover_replay_nonce(self, nodes_app):
        """409 when the nonce has already been consumed."""
        from app.security.credentials import OneTimeTokenReplayError

        with patch(
            "app.security.credentials.validate_one_time_bootstrap_token",
            side_effect=OneTimeTokenReplayError("nonce-xyz"),
        ):
            async with nodes_app.test_client() as client:
                resp = await _post_discover(client, _discover_payload())
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_discover_bad_body(self, nodes_app):
        """422 when the body fails schema validation (missing required fields)."""
        mock_principal = MagicMock()
        mock_principal.sub = "bootstrap:xx"

        with patch(
            "app.security.credentials.validate_one_time_bootstrap_token",
            return_value=mock_principal,
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    "/api/v1/nodes/discover",
                    json={"firmware_type": "uefi"},
                    headers={"Authorization": "Bearer valid"},
                )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_discover_identity_conflict_flags_posture(self, nodes_app, dal):
        """Node posture set to identity_conflict when MAC changes for same DMI UUID."""
        from unittest.mock import patch as mpatch

        # Pre-insert a node with the same dmi_uuid but different MAC
        now = datetime.now(timezone.utc)
        dal.nodes.insert(
            tenant_id="__default__",
            name="old-node",
            state="probed",
            dmi_uuid="conflict-dmi-uuid",
            primary_nic_mac="11:22:33:44:55:66",
            created_at=now,
            updated_at=now,
        )
        dal.commit()

        mock_principal = MagicMock()
        mock_principal.sub = "bootstrap:conflict"

        with mpatch(
            "app.security.credentials.validate_one_time_bootstrap_token",
            return_value=mock_principal,
        ), mpatch("app.clients.spire.SpireClient.register_workload", return_value="e-456"):
            async with nodes_app.test_client() as client:
                resp = await _post_discover(
                    client,
                    _discover_payload(
                        dmi_uuid="conflict-dmi-uuid",
                        primary_nic_mac="aa:bb:cc:dd:ee:ff",
                    ),
                )

        assert resp.status_code == 201
        data = await _json(resp)
        node = dal(dal.nodes.id == data["data"]["node_id"]).select()[0]
        assert node.posture == "identity_conflict"

    @pytest.mark.asyncio
    async def test_discover_no_body(self, nodes_app):
        """400 when no body is provided."""
        mock_principal = MagicMock()
        with patch(
            "app.security.credentials.validate_one_time_bootstrap_token",
            return_value=mock_principal,
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    "/api/v1/nodes/discover",
                    headers={"Authorization": "Bearer valid"},
                )
        assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# GET /api/v1/nodes/
# ---------------------------------------------------------------------------


class TestListNodes:
    @pytest.mark.asyncio
    async def test_list_returns_tenant_nodes(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/")
        assert resp.status_code == 200
        data = await _json(resp)
        assert data["status"] == "success"
        nodes = data["data"]["nodes"]
        assert any(n["id"] == seed_node for n in nodes)

    @pytest.mark.asyncio
    async def test_list_filters_by_state(self, nodes_app, dal, seed_node):
        """Only probed nodes returned when state=probed."""
        now = datetime.now(timezone.utc)
        dal.nodes.insert(
            tenant_id="acme", name="ready-node", state="ready",
            created_at=now, updated_at=now
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/?state=probed")
        assert resp.status_code == 200
        nodes = (await _json(resp))["data"]["nodes"]
        for n in nodes:
            assert n["state"] == "probed"

    @pytest.mark.asyncio
    async def test_list_invalid_state(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/?state=nonexistent_state")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_list_name_contains(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/?name_contains=test-node")
        assert resp.status_code == 200
        nodes = (await _json(resp))["data"]["nodes"]
        assert len(nodes) >= 1
        assert all("test-node" in n["name"] for n in nodes)

    @pytest.mark.asyncio
    async def test_list_cross_tenant_forbidden_for_normal_user(self, nodes_app, seed_node):
        """Requesting tenant_id filter without cross_tenant scope → 403."""
        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/?tenant_id=other_tenant")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_list_cross_tenant_allowed_for_superadmin(self, super_admin_app, dal, seed_node):
        """Super-admin can filter by tenant_id."""
        async with super_admin_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/?tenant_id=acme")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_list_invalid_page_size(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/?page_size=notanint")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_list_invalid_cursor(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/?cursor=!!!bad!!!")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_list_pagination_cursor(self, nodes_app, dal):
        """Cursor returned when results exceed page_size=1."""
        now = datetime.now(timezone.utc)
        for i in range(3):
            dal.nodes.insert(
                tenant_id="acme", name=f"paged-node-{i}", state="probed",
                created_at=now, updated_at=now
            )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/?page_size=1")
        assert resp.status_code == 200
        meta = (await _json(resp))["meta"]
        assert meta.get("next_cursor") is not None

    @pytest.mark.asyncio
    async def test_list_select_runs_off_event_loop(self, nodes_app, seed_node):
        """# regression: gh-22

        list_nodes' SELECT now runs via run_db()/asyncio.to_thread() instead
        of blocking the request coroutine inline -- proves the endpoint
        still returns the seeded node correctly through that thread hop
        (thread-local penguin-dal connection + RLS ContextVar propagation).
        """
        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/")
        assert resp.status_code == 200
        nodes = (await _json(resp))["data"]["nodes"]
        assert any(n["id"] == seed_node for n in nodes)


# ---------------------------------------------------------------------------
# GET /api/v1/nodes/{id}
# ---------------------------------------------------------------------------


class TestGetNode:
    @pytest.mark.asyncio
    async def test_get_existing_node(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{seed_node}")
        assert resp.status_code == 200
        data = (await _json(resp))["data"]
        assert data["node"]["id"] == seed_node
        assert "hardware_json" in data["node"]

    @pytest.mark.asyncio
    async def test_get_not_found(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/99999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_wrong_tenant(self, nodes_app, dal):
        """Node belonging to a different tenant returns 404 for isolation."""
        now = datetime.now(timezone.utc)
        other_node_id = int(
            dal.nodes.insert(
                tenant_id="other-tenant", name="other-node", state="probed",
                created_at=now, updated_at=now
            )
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{other_node_id}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/v1/nodes/{id}
# ---------------------------------------------------------------------------


class TestPatchNode:
    @pytest.mark.asyncio
    async def test_patch_name(self, nodes_app, seed_node, dal):
        async with nodes_app.test_client() as client:
            resp = await client.patch(
                f"/api/v1/nodes/{seed_node}",
                json={"name": "renamed-node"},
            )
        assert resp.status_code == 200
        assert dal(dal.nodes.id == seed_node).select()[0].name == "renamed-node"

    @pytest.mark.asyncio
    async def test_patch_ipv4_static(self, nodes_app, seed_node, dal):
        async with nodes_app.test_client() as client:
            resp = await client.patch(
                f"/api/v1/nodes/{seed_node}",
                json={"ipv4_static": "10.0.0.5"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_not_found(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.patch("/api/v1/nodes/99999", json={"name": "x"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_no_body(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.patch(f"/api/v1/nodes/{seed_node}")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_name_collision(self, nodes_app, dal, seed_node):
        """409 when the new name is already taken by another node."""
        now = datetime.now(timezone.utc)
        dal.nodes.insert(
            tenant_id="acme", name="collision-name", state="new",
            created_at=now, updated_at=now
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.patch(
                f"/api/v1/nodes/{seed_node}",
                json={"name": "collision-name"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_patch_tenant_id_forbidden_non_superadmin(self, nodes_app, seed_node):
        """403 when non-super-admin tries to change tenant_id."""
        async with nodes_app.test_client() as client:
            resp = await client.patch(
                f"/api/v1/nodes/{seed_node}",
                json={"tenant_id": "other-tenant"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_patch_tenant_id_allowed_for_superadmin(self, super_admin_app, dal, seed_node):
        async with super_admin_app.test_client() as client:
            resp = await client.patch(
                f"/api/v1/nodes/{seed_node}",
                json={"tenant_id": "new-tenant"},
            )
        assert resp.status_code == 200
        assert dal(dal.nodes.id == seed_node).select()[0].tenant_id == "new-tenant"


# ---------------------------------------------------------------------------
# POST /api/v1/nodes/{id}/reject
# ---------------------------------------------------------------------------


class TestRejectNode:
    @pytest.mark.asyncio
    async def test_reject_probed_node(self, nodes_app, seed_node, dal):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/reject",
                json={"reason": "Hardware defect detected"},
            )
        assert resp.status_code == 200
        assert dal(dal.nodes.id == seed_node).select()[0].state == "rejected"

    @pytest.mark.asyncio
    async def test_reject_not_found(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.post("/api/v1/nodes/99999/reject", json={"reason": "x"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_reject_missing_reason(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/reject",
                json={},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_reject_already_rejected(self, nodes_app, dal, seed_node):
        dal(dal.nodes.id == seed_node).update(state="rejected")
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/reject",
                json={"reason": "already rejected"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_reject_non_rejectable_state(self, nodes_app, dal, seed_node):
        """409 when node is in 'ready' state (not rejectable without deploy)."""
        dal(dal.nodes.id == seed_node).update(state="ready")
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/reject",
                json={"reason": "wrong state"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_reject_no_body(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.post(f"/api/v1/nodes/{seed_node}/reject")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/v1/nodes/{id}/deploy
# ---------------------------------------------------------------------------


class TestDeployNode:
    @pytest.mark.asyncio
    async def test_deploy_success(self, nodes_app, seed_node, seed_egg, dal):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/deploy",
                json={
                    "egg_assignments": [{"egg_id": seed_egg}],
                    "reason": "initial deploy",
                },
                headers={"X-Idempotency-Key": str(uuid.uuid4())},
            )
        assert resp.status_code == 202
        data = (await _json(resp))["data"]
        assert len(data["assignment_ids"]) == 1

        # Verify DB row created
        assignments = dal(
            (dal.node_egg_assignments.node_id == seed_node)
            & (dal.node_egg_assignments.egg_id == seed_egg)
        ).select()
        assert len(assignments) == 1
        assert assignments[0].status == "pending"

    @pytest.mark.asyncio
    async def test_deploy_no_idempotency_key(self, nodes_app, seed_node, seed_egg):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/deploy",
                json={"egg_assignments": [{"egg_id": seed_egg}]},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_deploy_not_found(self, nodes_app, seed_egg):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                "/api/v1/nodes/99999/deploy",
                json={"egg_assignments": [{"egg_id": seed_egg}]},
                headers={"X-Idempotency-Key": "idem-1"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_deploy_egg_not_found(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/deploy",
                json={"egg_assignments": [{"egg_id": 99999}]},
                headers={"X-Idempotency-Key": "idem-2"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_deploy_empty_egg_assignments(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/deploy",
                json={"egg_assignments": []},
                headers={"X-Idempotency-Key": "idem-3"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_deploy_terminal_node(self, nodes_app, dal, seed_node, seed_egg):
        dal(dal.nodes.id == seed_node).update(state="decommissioned")
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/deploy",
                json={"egg_assignments": [{"egg_id": seed_egg}]},
                headers={"X-Idempotency-Key": "idem-4"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_deploy_no_body(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/deploy",
                headers={"X-Idempotency-Key": "idem-5"},
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/v1/nodes/{id}/evacuate
# ---------------------------------------------------------------------------


class TestEvacuateNode:
    @pytest.mark.asyncio
    async def test_evacuate_success(self, nodes_app, seed_node):
        """Happy path: safety envelope passes, evacuation is accepted.

        app.workers.migration_engine.evaluate_safety is a Phase-3 TODO stub
        that always returns safe=False (fail closed) -- it must be mocked
        to a passing result here to test the actual 202 accept path;
        the fail-closed default is covered by
        TestEvacuateEdgeCases (below) and test_nodes_coverage3/4.
        """
        with patch(
            "app.workers.migration_engine.evaluate_safety",
            new_callable=AsyncMock,
            return_value={"safe": True, "note": "ok"},
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/evacuate",
                    json={"reason": "maintenance"},
                )
        assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_evacuate_not_found(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                "/api/v1/nodes/99999/evacuate",
                json={"reason": "test"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_evacuate_terminal_node(self, nodes_app, dal, seed_node):
        dal(dal.nodes.id == seed_node).update(state="decommissioned")
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/evacuate",
                json={},
            )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /api/v1/nodes/{id}/events
# ---------------------------------------------------------------------------


class TestNodeEvents:
    @pytest.mark.asyncio
    async def test_post_event_with_valid_service_svid(self, nodes_app, seed_node, dal):
        """Accept event when a valid Service SVID (mTLS) is presented.

        Regression: gh-SECURITY-FIX-2 removed the X-Gough-Test-Bypass-Auth
        header bypass from app/api/nodes.py; the events endpoint is now
        exercised the same way production traffic authenticates -- via
        request.peer_cert_pem + validate_service_svid().
        """
        with (
            patch("app.api.nodes._nats_publish_safe", new_callable=AsyncMock),
            patch(
                "app.security.credentials.validate_service_svid",
                return_value=_valid_service_svid_principal(),
            ),
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/events",
                    json={
                        "stage": "disk_partition_start",
                        "message": "Partitioning /dev/sda",
                        "progress_pct": 10,
                    },
                    headers=_SVID_TEST_HEADERS,
                )
        assert resp.status_code == 201
        data = (await _json(resp))["data"]
        assert data["stage"] == "disk_partition_start"

    @pytest.mark.asyncio
    async def test_post_event_no_auth(self, nodes_app, seed_node):
        """401 when no mTLS cert is presented."""
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/events",
                json={
                    "stage": "disk_partition_start",
                    "message": "Partitioning /dev/sda",
                },
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_post_event_not_found(self, nodes_app):
        with patch(
            "app.security.credentials.validate_service_svid",
            return_value=_valid_service_svid_principal(),
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    "/api/v1/nodes/99999/events",
                    json={"stage": "boot", "message": "msg"},
                    headers=_SVID_TEST_HEADERS,
                )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_post_event_missing_required_fields(self, nodes_app, seed_node):
        with patch(
            "app.security.credentials.validate_service_svid",
            return_value=_valid_service_svid_principal(),
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/events",
                    json={"stage": "boot"},  # missing message
                    headers=_SVID_TEST_HEADERS,
                )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_post_event_no_body(self, nodes_app, seed_node):
        with patch(
            "app.security.credentials.validate_service_svid",
            return_value=_valid_service_svid_principal(),
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/events",
                    headers=_SVID_TEST_HEADERS,
                )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_post_event_persisted_in_db(self, nodes_app, seed_node, dal):
        with (
            patch("app.api.nodes._nats_publish_safe", new_callable=AsyncMock),
            patch(
                "app.security.credentials.validate_service_svid",
                return_value=_valid_service_svid_principal(),
            ),
        ):
            async with nodes_app.test_client() as client:
                await client.post(
                    f"/api/v1/nodes/{seed_node}/events",
                    json={"stage": "configure_network", "message": "setting up eth0"},
                    headers=_SVID_TEST_HEADERS,
                )
        events = dal(dal.node_events.node_id == seed_node).select()
        assert len(events) == 1
        assert events[0].stage == "configure_network"

    @pytest.mark.asyncio
    async def test_post_event_nats_failure_non_fatal(self, nodes_app, seed_node, dal):
        """NATS publish failure should not cause the request to fail."""
        with (
            patch(
                "app.api.nodes._nats_publish_safe",
                new_callable=AsyncMock,
                side_effect=Exception("NATS down"),
            ),
            patch(
                "app.security.credentials.validate_service_svid",
                return_value=_valid_service_svid_principal(),
            ),
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/events",
                    json={"stage": "test_stage", "message": "nats fail test"},
                    headers=_SVID_TEST_HEADERS,
                )
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# DELETE /api/v1/nodes/{id}
# ---------------------------------------------------------------------------


class TestDecommissionNode:
    @pytest.mark.asyncio
    async def test_decommission_success(self, nodes_app, seed_node, dal):
        async with nodes_app.test_client() as client:
            resp = await client.delete(
                f"/api/v1/nodes/{seed_node}",
                json={"reason": "End of life"},
            )
        assert resp.status_code == 200
        assert dal(dal.nodes.id == seed_node).select()[0].state == "decommissioned"

    @pytest.mark.asyncio
    async def test_decommission_missing_reason(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.delete(
                f"/api/v1/nodes/{seed_node}",
                json={},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_decommission_no_body(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.delete(f"/api/v1/nodes/{seed_node}")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_decommission_not_found(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.delete(
                "/api/v1/nodes/99999",
                json={"reason": "x"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_decommission_already_decommissioned(self, nodes_app, dal, seed_node):
        dal(dal.nodes.id == seed_node).update(state="decommissioned")
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.delete(
                f"/api/v1/nodes/{seed_node}",
                json={"reason": "double"},
            )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# GET /api/v1/nodes/{id}/tags
# ---------------------------------------------------------------------------


class TestGetNodeTags:
    @pytest.mark.asyncio
    async def test_get_tags_returns_auto_tags(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{seed_node}/tags")
        assert resp.status_code == 200
        data = (await _json(resp))["data"]
        assert isinstance(data["tags"], list)
        auto = [t for t in data["tags"] if t["provenance"] == "auto"]
        assert len(auto) >= 1

    @pytest.mark.asyncio
    async def test_get_tags_not_found(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/99999/tags")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_tags_includes_operator_tags(self, nodes_app, dal, seed_node):
        now = datetime.now(timezone.utc)
        dal.node_tags_operator.insert(
            node_id=seed_node,
            tenant_id="acme",
            tag_key="env",
            tag_value="prod",
            provenance="operator",
            set_by_actor_sub="tester",
            set_at=now,
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{seed_node}/tags")
        tags = (await _json(resp))["data"]["tags"]
        operator_tags = [t for t in tags if t["provenance"] == "operator"]
        assert any(t["tag_key"] == "env" for t in operator_tags)


# ---------------------------------------------------------------------------
# PATCH /api/v1/nodes/{id}/tags
# ---------------------------------------------------------------------------


class TestPatchNodeTags:
    @pytest.mark.asyncio
    async def test_add_operator_tag(self, nodes_app, seed_node, dal):
        async with nodes_app.test_client() as client:
            resp = await client.patch(
                f"/api/v1/nodes/{seed_node}/tags",
                json={"add": [{"tag_key": "region", "tag_value": "us-east"}], "remove": []},
            )
        assert resp.status_code == 200
        rows = dal(dal.node_tags_operator.node_id == seed_node).select()
        assert any(r.tag_key == "region" and r.tag_value == "us-east" for r in rows)

    @pytest.mark.asyncio
    async def test_remove_operator_tag(self, nodes_app, dal, seed_node):
        now = datetime.now(timezone.utc)
        dal.node_tags_operator.insert(
            node_id=seed_node, tenant_id="acme",
            tag_key="remove-me", tag_value="yes",
            provenance="operator", set_by_actor_sub="tester", set_at=now,
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.patch(
                f"/api/v1/nodes/{seed_node}/tags",
                json={"add": [], "remove": [{"tag_key": "remove-me", "tag_value": "yes"}]},
            )
        assert resp.status_code == 200
        rows = dal(
            (dal.node_tags_operator.node_id == seed_node)
            & (dal.node_tags_operator.tag_key == "remove-me")
        ).select()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_patch_tags_not_found(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.patch(
                "/api/v1/nodes/99999/tags",
                json={"add": [], "remove": []},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_tags_no_body(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.patch(f"/api/v1/nodes/{seed_node}/tags")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_tags_invalid_body(self, nodes_app, seed_node):
        """422 when add entry is missing required fields."""
        async with nodes_app.test_client() as client:
            resp = await client.patch(
                f"/api/v1/nodes/{seed_node}/tags",
                json={"add": [{"only_key": "oops"}], "remove": []},
            )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Legacy Sprint-2 biome-assignment routes
# ---------------------------------------------------------------------------


class TestLegacyEggAssignment:
    @pytest.mark.asyncio
    async def test_assign_egg(self, nodes_app, seed_node, seed_egg, dal):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/biomes",
                json={"egg_id": seed_egg, "phase": "post_deploy"},
            )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_assign_egg_not_found_node(self, nodes_app, seed_egg):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                "/api/v1/nodes/99999/biomes",
                json={"egg_id": seed_egg},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_assign_egg_not_found_egg(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/biomes",
                json={"egg_id": 99999},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_assign_egg_conflict(self, nodes_app, seed_node, seed_egg, dal):
        """409 when the biome is already assigned and active."""
        now = datetime.now(timezone.utc)
        dal.node_egg_assignments.insert(
            node_id=seed_node, egg_id=seed_egg, tenant_id="acme",
            phase="post_deploy", status="pending", assigned_at=now,
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/biomes",
                json={"egg_id": seed_egg},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_list_eggs_for_node(self, nodes_app, seed_node, seed_egg, dal):
        now = datetime.now(timezone.utc)
        dal.node_egg_assignments.insert(
            node_id=seed_node, egg_id=seed_egg, tenant_id="acme",
            phase="post_deploy", status="pending", assigned_at=now,
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{seed_node}/biomes")
        assert resp.status_code == 200
        assert (await _json(resp))["data"]["total"] == 1

    @pytest.mark.asyncio
    async def test_list_eggs_node_not_found(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/99999/biomes")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_unassign_egg(self, nodes_app, seed_node, seed_egg, dal):
        now = datetime.now(timezone.utc)
        dal.node_egg_assignments.insert(
            node_id=seed_node, egg_id=seed_egg, tenant_id="acme",
            phase="post_deploy", status="ready", assigned_at=now,
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.delete(f"/api/v1/nodes/{seed_node}/biomes/{seed_egg}")
        assert resp.status_code == 202
        row = dal(
            (dal.node_egg_assignments.node_id == seed_node)
            & (dal.node_egg_assignments.egg_id == seed_egg)
        ).select().first()
        assert row.status == "draining"

    @pytest.mark.asyncio
    async def test_unassign_egg_not_found(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.delete(f"/api/v1/nodes/{seed_node}/biomes/99999")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Additional coverage tests — targeting uncovered lines
# ---------------------------------------------------------------------------


class TestDiscoverEdgeCases:
    """Additional discover endpoint paths for coverage."""

    @pytest.mark.asyncio
    async def test_discover_general_token_validation_error(self, nodes_app):
        """500 when token validation raises an unexpected exception."""
        with patch(
            "app.security.credentials.validate_one_time_bootstrap_token",
            side_effect=RuntimeError("unexpected"),
        ):
            async with nodes_app.test_client() as client:
                resp = await _post_discover(client, _discover_payload())
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_discover_mac_changed_same_dmi(self, nodes_app, dal):
        """Identity conflict when MAC changes but DMI UUID is the same."""
        from unittest.mock import patch as mpatch

        now = datetime.now(timezone.utc)
        # Insert existing node with same dmi_uuid but different MAC
        dal.nodes.insert(
            tenant_id="__default__",
            name="existing-node",
            state="probed",
            dmi_uuid="same-dmi-uuid",
            primary_nic_mac="11:22:33:44:55:66",
            created_at=now,
            updated_at=now,
        )
        dal.commit()

        mock_principal = MagicMock()
        mock_principal.sub = "bootstrap:new-mac"

        with mpatch(
            "app.security.credentials.validate_one_time_bootstrap_token",
            return_value=mock_principal,
        ), mpatch("app.clients.spire.SpireClient.register_workload", return_value="e-789"):
            async with nodes_app.test_client() as client:
                resp = await _post_discover(
                    client,
                    _discover_payload(
                        dmi_uuid="same-dmi-uuid",
                        primary_nic_mac="aa:bb:cc:dd:ee:ff",
                    ),
                )

        assert resp.status_code == 201
        data = await _json(resp)
        node = dal(dal.nodes.id == data["data"]["node_id"]).select()[0]
        assert node.posture == "identity_conflict"


class TestListNodesCursorPagination:
    """Cursor decode edge cases for list_nodes."""

    @pytest.mark.asyncio
    async def test_list_with_valid_cursor_filters_results(self, nodes_app, dal):
        """Valid cursor is accepted and applied."""
        import base64

        now = datetime.now(timezone.utc)
        for i in range(5):
            dal.nodes.insert(
                tenant_id="acme", name=f"cursor-node-{i}", state="probed",
                created_at=now, updated_at=now,
            )
        dal.commit()

        # Get first page
        async with nodes_app.test_client() as client:
            first_resp = await client.get("/api/v1/nodes/?page_size=2")
        assert first_resp.status_code == 200
        meta = (await _json(first_resp))["meta"]
        cursor = meta.get("next_cursor")
        assert cursor is not None

        # Use cursor for next page
        async with nodes_app.test_client() as client:
            second_resp = await client.get(f"/api/v1/nodes/?page_size=2&cursor={cursor}")
        assert second_resp.status_code == 200


class TestEvacuateEdgeCases:
    """Additional evacuate paths."""

    @pytest.mark.asyncio
    async def test_evacuate_with_force_flag(self, nodes_app, seed_node):
        """force=True should succeed even without safety check."""
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/evacuate",
                json={"reason": "forced maintenance", "force": True},
            )
        assert resp.status_code == 202
        data = (await _json(resp))["data"]
        assert data["force"] is True

    @pytest.mark.asyncio
    async def test_evacuate_empty_body_accepted(self, nodes_app, seed_node):
        """Empty JSON body is valid (all fields optional); safety check mocked to pass.

        force defaults to False, so a passing safety envelope (mocked, since
        evaluate_safety is an unimplemented Phase-3 stub that always fails
        closed) is required to reach 202.
        """
        with patch(
            "app.workers.migration_engine.evaluate_safety",
            new_callable=AsyncMock,
            return_value={"safe": True, "note": "ok"},
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/evacuate",
                    json={},
                )
        assert resp.status_code == 202


class TestNodeEventsTimestamp:
    """Node events with timestamp parsing."""

    @pytest.mark.asyncio
    async def test_post_event_with_explicit_timestamp(self, nodes_app, seed_node, dal):
        """Custom timestamp in ISO format is accepted and persisted."""
        with (
            patch("app.api.nodes._nats_publish_safe", new_callable=AsyncMock),
            patch(
                "app.security.credentials.validate_service_svid",
                return_value=_valid_service_svid_principal(),
            ),
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/events",
                    json={
                        "stage": "boot",
                        "message": "Booting",
                        "timestamp": "2025-01-01T10:00:00+00:00",
                    },
                    headers=_SVID_TEST_HEADERS,
                )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_post_event_with_invalid_timestamp_uses_now(self, nodes_app, seed_node, dal):
        """Invalid timestamp string falls back to current time."""
        with (
            patch("app.api.nodes._nats_publish_safe", new_callable=AsyncMock),
            patch(
                "app.security.credentials.validate_service_svid",
                return_value=_valid_service_svid_principal(),
            ),
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/events",
                    json={
                        "stage": "boot",
                        "message": "Booting",
                        "timestamp": "not-a-date",
                    },
                    headers=_SVID_TEST_HEADERS,
                )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_post_event_with_naive_timestamp(self, nodes_app, seed_node, dal):
        """Naive (no timezone) ISO timestamp gets UTC attached."""
        with (
            patch("app.api.nodes._nats_publish_safe", new_callable=AsyncMock),
            patch(
                "app.security.credentials.validate_service_svid",
                return_value=_valid_service_svid_principal(),
            ),
        ):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/events",
                    json={
                        "stage": "install",
                        "message": "Installing OS",
                        "timestamp": "2025-01-01T12:00:00",  # no timezone
                    },
                    headers=_SVID_TEST_HEADERS,
                )
        assert resp.status_code == 201


class TestDecommissionEdgeCases:
    """Additional decommission paths."""

    @pytest.mark.asyncio
    async def test_decommission_wrong_tenant(self, nodes_app, dal):
        """404 when node belongs to different tenant."""
        now = datetime.now(timezone.utc)
        other_id = int(
            dal.nodes.insert(
                tenant_id="other-tenant", name="other-node", state="probed",
                created_at=now, updated_at=now,
            )
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.delete(
                f"/api/v1/nodes/{other_id}",
                json={"reason": "cross-tenant attempt"},
            )
        assert resp.status_code == 404


class TestGetNodeTagsEdgeCases:
    """Additional tags endpoint paths."""

    @pytest.mark.asyncio
    async def test_get_tags_wrong_tenant(self, nodes_app, dal):
        """404 when node belongs to a different tenant (isolation)."""
        now = datetime.now(timezone.utc)
        other_id = int(
            dal.nodes.insert(
                tenant_id="wrong-tenant", name="other-tags-node", state="probed",
                created_at=now, updated_at=now,
            )
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{other_id}/tags")
        assert resp.status_code == 404


class TestPatchNodeTagsEdgeCases:
    """Additional patch tags paths."""

    @pytest.mark.asyncio
    async def test_patch_tags_wrong_tenant(self, nodes_app, dal):
        """404 when node belongs to a different tenant."""
        now = datetime.now(timezone.utc)
        other_id = int(
            dal.nodes.insert(
                tenant_id="wrong-tenant", name="other-patch-node", state="probed",
                created_at=now, updated_at=now,
            )
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.patch(
                f"/api/v1/nodes/{other_id}/tags",
                json={"add": [], "remove": []},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_tags_empty_add_remove(self, nodes_app, seed_node):
        """Empty add/remove returns current tag set."""
        async with nodes_app.test_client() as client:
            resp = await client.patch(
                f"/api/v1/nodes/{seed_node}/tags",
                json={"add": [], "remove": []},
            )
        assert resp.status_code == 200
        data = (await _json(resp))["data"]
        assert "tags" in data


class TestCloudInit:
    """GET /api/v1/nodes/{id}/cloud-init endpoint."""

    @pytest.fixture()
    def cloud_init_app(self, dal, monkeypatch):
        """App with cloud_init_templates table and a default template."""
        # Extend dal with cloud_init_templates table
        from penguin_dal import Field
        dal.define_table(
            "cloud_init_templates",
            Field("is_default", "boolean", default=False),
            Field("template_content", "text"),
            migrate=True,
        )
        dal.cloud_init_templates.insert(
            is_default=True,
            template_content="#cloud-config\nhostname: $hostname\n",
        )
        dal.commit()

        import app.middleware as mw_mod
        import app.security.scope_enforcement as scope_mod

        monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
        monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

        import app.api.nodes as nodes_mod

        nodes_mod = importlib.reload(nodes_mod)
        monkeypatch.setattr(nodes_mod, "get_db", lambda: dal)

        from quart import Quart, g
        from types import SimpleNamespace

        application = Quart(__name__)
        application.register_blueprint(nodes_mod.nodes_bp)

        @application.before_request
        async def _inject_identity():
            g.current_user = {
                "id": 1,
                "username": "tester",
                "_jwt_payload": {
                    "sub": "tester",
                    "tenant": "acme",
                    "scope": "gough.nodes.read",
                },
            }
            g.tenant_context = SimpleNamespace(tenant_id="acme", cross_tenant=False)

        return application

    @pytest.mark.asyncio
    async def test_cloud_init_success(self, cloud_init_app, seed_node):
        async with cloud_init_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{seed_node}/cloud-init")
        assert resp.status_code == 200
        body = (await resp.get_data(as_text=True))
        assert "hostname" in body

    @pytest.mark.asyncio
    async def test_cloud_init_invalid_baseline(self, cloud_init_app, seed_node):
        async with cloud_init_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{seed_node}/cloud-init?baseline=invalid")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_cloud_init_node_not_found(self, cloud_init_app):
        async with cloud_init_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/99999/cloud-init")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cloud_init_no_template_configured(self, nodes_app, seed_node):
        """404 when no default cloud-init template is configured."""
        # nodes_app fixture doesn't define cloud_init_templates table
        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{seed_node}/cloud-init")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cloud_init_hybrid_baseline(self, cloud_init_app, seed_node):
        async with cloud_init_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{seed_node}/cloud-init?baseline=hybrid")
        assert resp.status_code == 200


class TestLxdJoin:
    """POST /api/v1/nodes/{id}/lxd/join endpoint."""

    @pytest.mark.asyncio
    async def test_lxd_join_missing_cluster_id(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/lxd/join",
                json={"api_url": "https://10.0.0.1:8443", "member_name": "node1"},
            )
        assert resp.status_code == 400
        data = await _json(resp)
        assert "cluster_id" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_lxd_join_missing_api_url(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/lxd/join",
                json={"cluster_id": "c1", "member_name": "node1"},
            )
        assert resp.status_code == 400
        data = await _json(resp)
        assert "api_url" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_lxd_join_missing_member_name(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/lxd/join",
                json={"cluster_id": "c1", "api_url": "https://10.0.0.1:8443"},
            )
        assert resp.status_code == 400
        data = await _json(resp)
        assert "member_name" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_lxd_join_no_body(self, nodes_app, seed_node):
        async with nodes_app.test_client() as client:
            resp = await client.post(f"/api/v1/nodes/{seed_node}/lxd/join")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_lxd_join_node_not_found(self, nodes_app):
        async with nodes_app.test_client() as client:
            resp = await client.post(
                "/api/v1/nodes/99999/lxd/join",
                json={
                    "cluster_id": "c1",
                    "api_url": "https://10.0.0.1:8443",
                    "member_name": "node1",
                },
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_lxd_join_lxd_error_returns_502(self, nodes_app, seed_node):
        """LXD client errors → 502."""
        with patch("app.clients.lxd_extra.mint_join_token") as mock_mint:
            from app.clients import lxd_extra
            mock_mint.side_effect = lxd_extra.LXDError("LXD not reachable")
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/lxd/join",
                    json={
                        "cluster_id": "c1",
                        "api_url": "https://10.0.0.1:8443",
                        "member_name": "node1",
                    },
                )
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_lxd_join_success(self, nodes_app, seed_node, dal):
        """Successful LXD join updates node state to ready.

        The lxd_cluster_members table is patched via hasattr to avoid
        requiring the optional table in the test fixture schema.
        """
        mock_token = MagicMock()
        mock_token.token = "lxd-join-token-abc"

        original_hasattr = hasattr

        def _patched_hasattr(obj, name):
            if name == "lxd_cluster_members":
                return False  # skip insert, use node state update path only
            return original_hasattr(obj, name)

        with patch("app.clients.lxd_extra.mint_join_token", return_value=mock_token), \
             patch("app.clients.lxd_extra.cluster_join"), \
             patch("builtins.hasattr", side_effect=_patched_hasattr):
            async with nodes_app.test_client() as client:
                resp = await client.post(
                    f"/api/v1/nodes/{seed_node}/lxd/join",
                    json={
                        "cluster_id": "my-cluster",
                        "api_url": "https://10.0.0.1:8443",
                        "member_name": "test-node-1",
                    },
                )
        assert resp.status_code == 201
        data = (await _json(resp))["data"]
        assert data["node_id"] == seed_node
        assert data["cluster_id"] == "my-cluster"
        # Verify node state updated to ready
        node = dal(dal.nodes.id == seed_node).select()[0]
        assert node.state == "ready"


class TestRejectNodeEdgeCases:
    """Additional reject paths for coverage."""

    @pytest.mark.asyncio
    async def test_reject_new_state_node(self, nodes_app, dal, seed_node):
        """Node in 'new' state can be rejected."""
        dal(dal.nodes.id == seed_node).update(state="new")
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.post(
                f"/api/v1/nodes/{seed_node}/reject",
                json={"reason": "rejected from new"},
            )
        assert resp.status_code == 200


class TestListNodesBiomesFiltered:
    """List biomes with status filter."""

    @pytest.mark.asyncio
    async def test_list_biomes_with_status_filter(self, nodes_app, seed_node, seed_egg, dal):
        now = datetime.now(timezone.utc)
        dal.node_egg_assignments.insert(
            node_id=seed_node, egg_id=seed_egg, tenant_id="acme",
            phase="post_deploy", status="ready", assigned_at=now,
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{seed_node}/biomes?status=ready")
        assert resp.status_code == 200
        data = (await _json(resp))["data"]
        assert data["total"] == 1
        assert data["assignments"][0]["status"] == "ready"

    @pytest.mark.asyncio
    async def test_list_biomes_status_filter_excludes_others(self, nodes_app, seed_node, seed_egg, dal):
        now = datetime.now(timezone.utc)
        dal.node_egg_assignments.insert(
            node_id=seed_node, egg_id=seed_egg, tenant_id="acme",
            phase="post_deploy", status="pending", assigned_at=now,
        )
        dal.commit()

        # Filter for 'ready' — should return 0 rows
        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{seed_node}/biomes?status=ready")
        assert resp.status_code == 200
        data = (await _json(resp))["data"]
        assert data["total"] == 0


# ---------------------------------------------------------------------------
# Regression Tests: GH-31 data-serialization bugs
# ---------------------------------------------------------------------------


class TestRegressionGH31FirmwareType:
    """Regression: gh-31. firmware_type serialization fixes."""

    @pytest.mark.asyncio
    async def test_firmware_type_serialized_from_hardware_json_in_list_response(
        self, nodes_app, dal
    ):
        """Regression: gh-31. firmware_type must be serialized from hardware_json in list responses."""
        # Seed a node with firmware_type in hardware_json
        now = datetime.now(timezone.utc)
        node_id = dal.nodes.insert(
            tenant_id="acme",
            name="uefi-node",
            state="probed",
            dmi_uuid="dmi-with-uefi",
            primary_nic_mac="aa:bb:cc:dd:ee:ff",
            hardware_json={
                "firmware_type": "uefi",
                "lshw": {},
                "lsblk": {},
                "nics": [],
            },
            discovered_at=now,
            created_at=now,
            updated_at=now,
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.get("/api/v1/nodes/")
        assert resp.status_code == 200
        data = await _json(resp)
        nodes = data.get("data", {}).get("nodes", [])
        assert len(nodes) >= 1
        node = next((n for n in nodes if n["id"] == node_id), None)
        assert node is not None, f"Node {node_id} not found in list response"
        # firmware_type must be non-null and equal to the hardware_json value
        assert node["firmware_type"] == "uefi", (
            f"firmware_type should be 'uefi' from hardware_json, got {node['firmware_type']}"
        )

    @pytest.mark.asyncio
    async def test_firmware_type_serialized_from_hardware_json_in_detail_response(
        self, nodes_app, dal
    ):
        """Regression: gh-31. firmware_type must be serialized from hardware_json in detail responses."""
        # Seed a node with firmware_type in hardware_json
        now = datetime.now(timezone.utc)
        node_id = dal.nodes.insert(
            tenant_id="acme",
            name="legacy-node",
            state="probed",
            dmi_uuid="dmi-with-legacy",
            primary_nic_mac="aa:bb:cc:dd:ee:01",
            hardware_json={
                "firmware_type": "legacy",
                "lshw": {},
                "lsblk": {},
                "nics": [],
            },
            discovered_at=now,
            created_at=now,
            updated_at=now,
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{node_id}")
        assert resp.status_code == 200
        data = await _json(resp)
        node = data.get("data", {}).get("node", {})
        assert node.get("id") == node_id
        # firmware_type must be non-null and equal to the hardware_json value
        assert node["firmware_type"] == "legacy", (
            f"firmware_type should be 'legacy' from hardware_json, got {node['firmware_type']}"
        )
        # hardware_json should be included in detail response
        assert "hardware_json" in node
        assert node["hardware_json"]["firmware_type"] == "legacy"

    @pytest.mark.asyncio
    async def test_firmware_type_null_when_hardware_json_absent(self, nodes_app, dal):
        """Regression: gh-31. firmware_type must be null when hardware_json is not set."""
        now = datetime.now(timezone.utc)
        node_id = dal.nodes.insert(
            tenant_id="acme",
            name="no-hw-node",
            state="new",
            dmi_uuid="dmi-no-hardware",
            primary_nic_mac="aa:bb:cc:dd:ee:02",
            hardware_json=None,
            discovered_at=None,
            created_at=now,
            updated_at=now,
        )
        dal.commit()

        async with nodes_app.test_client() as client:
            resp = await client.get(f"/api/v1/nodes/{node_id}")
        assert resp.status_code == 200
        data = await _json(resp)
        node = data.get("data", {}).get("node", {})
        assert node.get("firmware_type") is None, (
            "firmware_type should be null when hardware_json is not set"
        )
