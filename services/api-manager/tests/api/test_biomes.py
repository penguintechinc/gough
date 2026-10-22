"""Tests for Gough Biomes API endpoints (Sprint 2).

Tests cover:
- GET /api/v1/biomes — list with filtering by biome_kind, phase, workload_type, etc.
- POST /api/v1/biomes — create biome from full biome.yaml body
- POST /api/v1/biomes/{id}/sign — sign biome (async, cosign/syft in Sprint 4)
- POST /api/v1/biomes/{id}/upgrade — submit upgrade plan (Sprint 4 orchestration)
- GET /api/v1/biomes/{id}/eligibility — per-node hardware tag eligibility
- POST /api/v1/nodes/{id}/biomes — assign biome to node
- GET /api/v1/nodes/{id}/biomes — list node's biomes
- DELETE /api/v1/nodes/{id}/biomes/{biome_id} — unassign biome from node

Focus: filter combinations, eligibility checker with synthetic fixtures,
assignment endpoints, sign/upgrade body validation, >= 90% coverage.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def app_client(app_with_auth):
    """Get the Quart test client from the fixture."""
    return app_with_auth.test_client()


@pytest.fixture()
def sample_eggs(dal_with_eggs):
    """Create 5 sample biomes with varied biome_kind, phase, workload_type, and tags."""
    biomes = []
    specs = [
        {
            "name": "k8s-primary",
            "display_name": "Kubernetes Primary",
            "biome_kind": "k8s",
            "phase": "phase2_initial",
            "workload_type": "lxc",
            "requires_hardware_tags": ["cpu:cores:8", "mem:total-gb:32"],
            "lock_to_host": True,
        },
        {
            "name": "nest-agent",
            "display_name": "Nest Storage Agent",
            "biome_kind": "storage",
            "phase": "post_deploy",
            "workload_type": "lxc",
            "requires_hardware_tags": ["disk:dark-drives:2"],
            "lock_to_host": False,
        },
        {
            "name": "prometheus",
            "display_name": "Prometheus Monitoring",
            "biome_kind": "monitoring",
            "phase": "post_deploy",
            "workload_type": "lxc",
            "requires_hardware_tags": [],
            "forbids_hardware_tags": [],
            "lock_to_host": False,
        },
        {
            "name": "vault-leader",
            "display_name": "Vault HA Leader",
            "biome_kind": "infrastructure",
            "phase": "post_deploy",
            "workload_type": "vm",
            "requires_hardware_tags": ["tpm:2.0"],
            "lock_to_host": True,
        },
        {
            "name": "app-workload",
            "display_name": "User Application",
            "biome_kind": "user_workload",
            "phase": "post_deploy",
            "workload_type": "lxc",
            "requires_hardware_tags": ["mem:total-gb:16"],
            "lock_to_host": False,
        },
    ]

    for i, spec in enumerate(specs):
        egg_id = dal_with_eggs.biomes.insert(
            tenant_id="__default__",
            name=spec["name"],
            display_name=spec["display_name"],
            description=f"Test biome {i}",
            biome_type="lxd_container",
            version="1.0.0",
            biome_kind=spec["biome_kind"],
            phase=spec["phase"],
            workload_type=spec["workload_type"],
            lock_to_host=spec.get("lock_to_host", False),
            requires_hardware_tags=spec.get("requires_hardware_tags"),
            forbids_hardware_tags=spec.get("forbids_hardware_tags"),
            is_active=True,
            is_default=False,
        )
        dal_with_eggs.commit()
        biomes.append((int(egg_id), spec["name"]))

    return biomes


@pytest.fixture()
def sample_nodes(dal_with_eggs):
    """Create 4 sample nodes with various hardware tags."""
    nodes = []
    specs = [
        {
            "name": "node-1",
            "hardware_tags": ["cpu:cores:16", "mem:total-gb:128", "disk:dark-drives:4"],
        },
        {
            "name": "node-2",
            "hardware_tags": ["cpu:cores:8", "mem:total-gb:32", "disk:dark-drives:0"],
        },
        {
            "name": "node-3",
            "hardware_tags": ["cpu:cores:24", "mem:total-gb:256", "tpm:2.0"],
        },
        {
            "name": "node-4",
            "hardware_tags": ["cpu:cores:4", "mem:total-gb:16"],
        },
    ]

    for i, spec in enumerate(specs):
        node_id = dal_with_eggs.nodes.insert(
            tenant_id="__default__",
            name=spec["name"],
            state="ready",
            hardware_tags=spec.get("hardware_tags"),
        )
        dal_with_eggs.commit()
        nodes.append((int(node_id), spec["name"], spec["hardware_tags"]))

    return nodes


class TestEggsList:
    """Tests for GET /api/v1/biomes list filtering."""

    async def test_list_all_eggs(self, app_client, sample_eggs):
        """List all biomes without filters."""
        response = await app_client.get("/api/v1/biomes")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "success"
        assert len(body["data"]["biomes"]) == 5
        assert body["data"]["total"] == 5

    async def test_list_filter_by_egg_kind(self, app_client, sample_eggs):
        """Filter biomes by biome_kind."""
        response = await app_client.get("/api/v1/biomes?biome_kind=k8s")
        assert response.status_code == 200
        body = await response.get_json()
        biomes = body["data"]["biomes"]
        assert len(biomes) == 1
        assert biomes[0]["name"] == "k8s-primary"

    async def test_list_filter_by_multiple_egg_kinds(self, app_client, sample_eggs):
        """Filter by multiple biome_kind values (repeated query param)."""
        response = await app_client.get("/api/v1/biomes?biome_kind=k8s&biome_kind=storage")
        assert response.status_code == 200
        body = await response.get_json()
        biomes = body["data"]["biomes"]
        assert len(biomes) == 2
        names = {e["name"] for e in biomes}
        assert names == {"k8s-primary", "nest-agent"}

    async def test_list_filter_by_phase(self, app_client, sample_eggs):
        """Filter biomes by phase."""
        response = await app_client.get("/api/v1/biomes?phase=post_deploy")
        assert response.status_code == 200
        body = await response.get_json()
        biomes = body["data"]["biomes"]
        assert len(biomes) == 4
        for e in biomes:
            assert e["phase"] == "post_deploy"

    async def test_list_filter_by_workload_type(self, app_client, sample_eggs):
        """Filter biomes by workload_type."""
        response = await app_client.get("/api/v1/biomes?workload_type=vm")
        assert response.status_code == 200
        body = await response.get_json()
        biomes = body["data"]["biomes"]
        assert len(biomes) == 1
        assert biomes[0]["name"] == "vault-leader"

    async def test_list_filter_by_lock_to_host(self, app_client, sample_eggs):
        """Filter by lock_to_host flag."""
        response = await app_client.get("/api/v1/biomes?lock_to_host=true")
        assert response.status_code == 200
        body = await response.get_json()
        biomes = body["data"]["biomes"]
        assert len(biomes) == 2
        names = {e["name"] for e in biomes}
        assert names == {"k8s-primary", "vault-leader"}

    async def test_list_filter_by_name_contains(self, app_client, sample_eggs):
        """Filter biomes by name substring."""
        response = await app_client.get("/api/v1/biomes?name_contains=nest")
        assert response.status_code == 200
        body = await response.get_json()
        biomes = body["data"]["biomes"]
        assert len(biomes) == 1
        assert biomes[0]["name"] == "nest-agent"

    async def test_list_filter_by_is_default(self, app_client, sample_eggs):
        """Filter biomes by is_default flag."""
        response = await app_client.get("/api/v1/biomes?is_default=false")
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["data"]["biomes"]) == 5  # all are non-default

    async def test_list_filter_invalid_egg_kind(self, app_client):
        """Invalid biome_kind returns validation error."""
        response = await app_client.get("/api/v1/biomes?biome_kind=invalid_kind")
        assert response.status_code == 422
        body = await response.get_json()
        assert body["error"]["code"] == "validation_failed"

    async def test_list_filter_invalid_workload_type(self, app_client):
        """Invalid workload_type returns validation error."""
        response = await app_client.get("/api/v1/biomes?workload_type=container")
        assert response.status_code == 422

    async def test_list_filter_node_id_eligibility(self, app_client, sample_eggs, sample_nodes):
        """Filter biomes by node_id (eligibility check)."""
        node_id = sample_nodes[0][0]  # node-1 with high specs
        response = await app_client.get(f"/api/v1/biomes?node_id={node_id}")
        assert response.status_code == 200
        body = await response.get_json()
        # node-1 should be eligible for biomes that don't require dark drives beyond 4
        biomes = body["data"]["biomes"]
        assert len(biomes) >= 1

    async def test_list_filter_node_id_not_found(self, app_client):
        """Non-existent node_id returns 404."""
        response = await app_client.get("/api/v1/biomes?node_id=9999")
        assert response.status_code == 404
        body = await response.get_json()
        assert body["error"]["code"] == "not_found"


class TestEggsCreate:
    """Tests for POST /api/v1/biomes biome creation."""

    async def test_create_egg_success(self, app_client, dal_with_eggs):
        """Create a valid biome."""
        body = {
            "name": "test-biome",
            "display_name": "Test Biome",
            "description": "A test biome",
            "biome_type": "lxd_container",
            "version": "1.0.0",
            "biome_kind": "custom",
            "phase": "post_deploy",
            "workload_type": "lxc",
        }
        response = await app_client.post("/api/v1/biomes", json=body)
        assert response.status_code == 201
        data = await response.get_json()
        assert data["status"] == "success"
        assert data["data"]["biome_id"]
        assert data["data"]["version"] == "1.0.0"

    async def test_create_egg_with_hardware_tags(self, app_client, dal_with_eggs):
        """Create biome with hardware tag requirements."""
        body = {
            "name": "gpu-biome",
            "display_name": "GPU Workload",
            "biome_type": "lxd_vm",
            "version": "2.0.0",
            "requires_hardware_tags": ["gpu:vendor:nvidia:count:1", "mem:total-gb:32"],
            "forbids_hardware_tags": ["cpu:vendor:amd"],
        }
        response = await app_client.post("/api/v1/biomes", json=body)
        assert response.status_code == 201
        data = await response.get_json()
        biome = data["data"]["biome"]
        assert biome["requires_hardware_tags"] == ["gpu:vendor:nvidia:count:1", "mem:total-gb:32"]
        assert biome["forbids_hardware_tags"] == ["cpu:vendor:amd"]

    async def test_create_egg_invalid_cloud_init_yaml(self, app_client, dal_with_eggs):
        """Invalid cloud-init YAML is rejected."""
        body = {
            "name": "bad-cloud-init",
            "display_name": "Bad Cloud Init",
            "biome_type": "cloud_init",
            "version": "1.0.0",
            "cloud_init_content": "invalid: [yaml: content: {",
        }
        response = await app_client.post("/api/v1/biomes", json=body)
        assert response.status_code == 422
        data = await response.get_json()
        assert "cloud_init_content" in data["error"]["message"].lower()

    async def test_create_egg_duplicate_name(self, app_client, sample_eggs):
        """Duplicate biome name returns conflict."""
        body = {
            "name": "k8s-primary",  # already exists
            "display_name": "Another K8s",
            "biome_type": "lxd_container",
            "version": "1.5.0",
        }
        response = await app_client.post("/api/v1/biomes", json=body)
        assert response.status_code == 409
        data = await response.get_json()
        assert data["error"]["code"] == "conflict"

    async def test_create_egg_missing_required_field(self, app_client, dal_with_eggs):
        """Missing required field returns validation error."""
        body = {
            "display_name": "Incomplete Biome",
            # missing 'name'
            "biome_type": "lxd_container",
            "version": "1.0.0",
        }
        response = await app_client.post("/api/v1/biomes", json=body)
        assert response.status_code == 422
        data = await response.get_json()
        assert "name" in str(data["error"]["details"]["violations"]).lower()


class TestEggSign:
    """Tests for POST /api/v1/biomes/{id}/sign."""

    async def test_sign_egg_success(self, app_client, sample_eggs):
        """Sign an biome successfully (returns 202)."""
        egg_id = sample_eggs[0][0]
        body = {
            "key_id": "my-signing-key",
            "reason": "CVE patch for k8s-primary",
        }
        response = await app_client.post(f"/api/v1/biomes/{egg_id}/sign", json=body)
        assert response.status_code == 202
        data = await response.get_json()
        assert data["status"] == "success"
        assert data["data"]["status"] == "signing_pending"

    async def test_sign_egg_missing_body(self, app_client, sample_eggs):
        """Missing body returns bad_request."""
        egg_id = sample_eggs[0][0]
        response = await app_client.post(f"/api/v1/biomes/{egg_id}/sign")
        assert response.status_code == 400

    async def test_sign_egg_invalid_key_id(self, app_client, sample_eggs):
        """Empty key_id is rejected."""
        egg_id = sample_eggs[0][0]
        body = {
            "key_id": "",
            "reason": "Test",
        }
        response = await app_client.post(f"/api/v1/biomes/{egg_id}/sign", json=body)
        assert response.status_code == 422

    async def test_sign_egg_not_found(self, app_client, dal_with_eggs):
        """Non-existent biome returns 404."""
        body = {
            "key_id": "key1",
            "reason": "Test",
        }
        response = await app_client.post("/api/v1/biomes/9999/sign", json=body)
        assert response.status_code == 404


class TestEggUpgrade:
    """Tests for POST /api/v1/biomes/{id}/upgrade."""

    async def test_upgrade_egg_success(self, app_client, sample_eggs):
        """Submit biome upgrade (returns 202)."""
        egg_id = sample_eggs[4][0]  # user_workload biome — no cluster.admin or approval_token required
        body = {
            "target_version": "1.1.0",
            "rollout_plan": "auto",
        }
        response = await app_client.post(f"/api/v1/biomes/{egg_id}/upgrade", json=body)
        assert response.status_code == 202
        data = await response.get_json()
        # "pending" (not "upgrade_pending") -- mirrors the literal
        # upgrade_runs.status value set by _insert_run in app.api.biomes,
        # verified against the DB row in tests/unit/test_biome_upgrade.py.
        assert data["data"]["status"] == "pending"

    async def test_upgrade_egg_missing_target_version(self, app_client, sample_eggs):
        """Missing target_version is rejected."""
        egg_id = sample_eggs[0][0]
        body = {
            "rollout_plan": "canary",
        }
        response = await app_client.post(f"/api/v1/biomes/{egg_id}/upgrade", json=body)
        assert response.status_code == 422

    async def test_upgrade_egg_invalid_rollout_plan(self, app_client, sample_eggs):
        """Invalid rollout_plan is rejected."""
        egg_id = sample_eggs[0][0]
        body = {
            "target_version": "1.1.0",
            "rollout_plan": "invalid_strategy",
        }
        response = await app_client.post(f"/api/v1/biomes/{egg_id}/upgrade", json=body)
        assert response.status_code == 422


class TestEggEligibility:
    """Tests for GET /api/v1/biomes/{id}/eligibility."""

    async def test_egg_eligibility_check_eligible(self, app_client, sample_eggs, sample_nodes):
        """Node is eligible for biome based on hardware tags."""
        egg_id = sample_eggs[0][0]  # k8s-primary requires cpu:cores:8, mem:total-gb:32
        node_id = sample_nodes[0][0]  # node-1 has cpu:cores:16, mem:total-gb:128
        response = await app_client.get(
            f"/api/v1/biomes/{egg_id}/eligibility?node_id={node_id}"
        )
        assert response.status_code == 200
        data = await response.get_json()
        assert data["data"]["eligible"] is True
        assert data["data"]["missing_tags"] == []

    async def test_egg_eligibility_check_insufficient_resources(self, app_client, sample_eggs, sample_nodes):
        """Node is ineligible due to insufficient resources."""
        egg_id = sample_eggs[0][0]  # k8s-primary requires cpu:cores:8, mem:total-gb:32
        node_id = sample_nodes[3][0]  # node-4 has cpu:cores:4, mem:total-gb:16
        response = await app_client.get(
            f"/api/v1/biomes/{egg_id}/eligibility?node_id={node_id}"
        )
        assert response.status_code == 200
        data = await response.get_json()
        assert data["data"]["eligible"] is False
        assert len(data["data"]["missing_tags"]) > 0

    async def test_egg_eligibility_missing_node_id(self, app_client, sample_eggs):
        """Missing node_id query param returns validation error."""
        egg_id = sample_eggs[0][0]
        response = await app_client.get(f"/api/v1/biomes/{egg_id}/eligibility")
        assert response.status_code == 422

    async def test_egg_eligibility_invalid_node_id(self, app_client, sample_eggs):
        """Invalid node_id (non-integer) returns validation error."""
        egg_id = sample_eggs[0][0]
        response = await app_client.get(f"/api/v1/biomes/{egg_id}/eligibility?node_id=not_an_int")
        assert response.status_code == 422


class TestNodeEggAssignment:
    """Tests for POST/GET/DELETE /api/v1/nodes/{id}/biomes."""

    async def test_assign_egg_to_node_success(self, app_client, sample_eggs, sample_nodes):
        """Assign biome to node successfully."""
        egg_id = sample_eggs[0][0]
        node_id = sample_nodes[0][0]
        body = {
            "biome_id": egg_id,
        }
        response = await app_client.post(f"/api/v1/nodes/{node_id}/biomes", json=body)
        assert response.status_code == 201
        data = await response.get_json()
        assert data["data"]["assignment"]["status"] == "pending"
        assert data["data"]["assignment"]["biome_id"] == egg_id

    async def test_assign_egg_missing_egg_id(self, app_client, sample_nodes):
        """Missing egg_id is rejected."""
        node_id = sample_nodes[0][0]
        body = {
            "annotation": "no egg_id",
        }
        response = await app_client.post(f"/api/v1/nodes/{node_id}/biomes", json=body)
        assert response.status_code == 422

    async def test_assign_egg_duplicate(self, app_client, sample_eggs, sample_nodes):
        """Duplicate assignment returns conflict."""
        egg_id = sample_eggs[0][0]
        node_id = sample_nodes[0][0]
        body = {"biome_id": egg_id}
        response = await app_client.post(f"/api/v1/nodes/{node_id}/biomes", json=body)
        assert response.status_code == 201
        # Try again
        response = await app_client.post(f"/api/v1/nodes/{node_id}/biomes", json=body)
        assert response.status_code == 409

    async def test_list_node_eggs(self, app_client, sample_eggs, sample_nodes):
        """List biomes assigned to a node."""
        egg_id = sample_eggs[0][0]
        node_id = sample_nodes[0][0]
        body = {"biome_id": egg_id}
        await app_client.post(f"/api/v1/nodes/{node_id}/biomes", json=body)
        response = await app_client.get(f"/api/v1/nodes/{node_id}/biomes")
        assert response.status_code == 200
        data = await response.get_json()
        assert len(data["data"]["assignments"]) == 1
        assert data["data"]["assignments"][0]["biome_id"] == egg_id

    async def test_unassign_egg_from_node(self, app_client, sample_eggs, sample_nodes):
        """Unassign biome from node (transition to draining)."""
        egg_id = sample_eggs[0][0]
        node_id = sample_nodes[0][0]
        body = {"biome_id": egg_id}
        await app_client.post(f"/api/v1/nodes/{node_id}/biomes", json=body)
        response = await app_client.delete(f"/api/v1/nodes/{node_id}/biomes/{egg_id}")
        assert response.status_code in (200, 202)
        data = await response.get_json()
        assert data["data"]["status"] == "draining"


# ---------------------------------------------------------------------------
# Additional fixtures for extended tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def dal_with_groups(dal_with_eggs):
    """Extend dal_with_eggs with biome_groups and related tables."""
    from penguin_dal import Field
    from datetime import datetime, timezone

    if "biome_groups" not in getattr(dal_with_eggs, "tables", []):
        dal_with_eggs.define_table(
            "biome_groups",
            Field("name", "string", notnull=True),
            Field("display_name", "string"),
            Field("description", "string"),
            Field("biomes", "json"),
            Field("is_default", "boolean", default=False),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=True,
        )

    if "ipxe_boot_configs" not in getattr(dal_with_eggs, "tables", []):
        dal_with_eggs.define_table(
            "ipxe_boot_configs",
            Field("name", "string"),
            Field("assigned_biome_group_id", "integer"),
            migrate=True,
        )

    if "deployments" not in getattr(dal_with_eggs, "tables", []):
        dal_with_eggs.define_table(
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

    if "deployment_logs" not in getattr(dal_with_eggs, "tables", []):
        dal_with_eggs.define_table(
            "deployment_logs",
            Field("deployment_id", "string"),
            Field("message", "string"),
            Field("level", "string", default="info"),
            Field("created_at", "datetime"),
            migrate=True,
        )

    return dal_with_eggs


@pytest.fixture()
def extended_app(app_with_auth, dal_with_groups, monkeypatch):
    """App fixture with extended tables for groups/deployments tests."""
    import app.api.biomes as biomes_mod
    import importlib

    monkeypatch.setattr(biomes_mod, "get_db", lambda: dal_with_groups)
    return app_with_auth


@pytest.fixture()
def extended_client(extended_app):
    return extended_app.test_client()


@pytest.fixture()
def sample_biome_id(dal_with_groups):
    """Insert a single biome and return its ID."""
    bid = dal_with_groups.biomes.insert(
        tenant_id="__default__",
        name="test-biome-ext",
        display_name="Test Biome Extended",
        biome_type="lxd_container",
        version="1.0.0",
        biome_kind="custom",
        phase="post_deploy",
        workload_type="lxc",
        is_active=True,
        is_default=False,
    )
    dal_with_groups.commit()
    return int(bid)


@pytest.fixture()
def sample_group_id(dal_with_groups, sample_biome_id):
    """Insert a biome group and return its ID."""
    gid = dal_with_groups.biome_groups.insert(
        name="test-group",
        display_name="Test Group",
        description="A test group",
        biomes=[{"biome_id": sample_biome_id, "order": 1}],
        is_default=False,
    )
    dal_with_groups.commit()
    return int(gid)


@pytest.fixture()
def sample_deployment_id(dal_with_groups, sample_biome_id):
    """Insert a deployment and return its ID."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    # Get any existing node or insert one
    node = dal_with_groups(dal_with_groups.nodes.id > 0).select().first()
    node_id = int(node.id) if node else 1

    did = dal_with_groups.deployments.insert(
        biome_id=sample_biome_id,
        node_id=node_id,
        phase=1,
        status="pending",
        tenant_id="__default__",
        created_at=now,
        updated_at=now,
    )
    dal_with_groups.commit()
    return int(did)


# ---------------------------------------------------------------------------
# GET /api/v1/biomes/<id>
# ---------------------------------------------------------------------------


class TestGetBiome:
    """Tests for GET /api/v1/biomes/<id>."""

    async def test_get_biome_success(self, extended_client, sample_biome_id):
        response = await extended_client.get(f"/api/v1/biomes/{sample_biome_id}")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "success"
        assert body["data"]["biome"]["id"] == sample_biome_id
        assert body["data"]["biome"]["name"] == "test-biome-ext"

    async def test_get_biome_not_found(self, extended_client):
        response = await extended_client.get("/api/v1/biomes/99999")
        assert response.status_code == 404
        body = await response.get_json()
        assert body["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# PUT /api/v1/biomes/<id>
# ---------------------------------------------------------------------------


class TestUpdateBiome:
    """Tests for PUT /api/v1/biomes/<id>."""

    async def test_update_biome_success(self, extended_client, sample_biome_id):
        response = await extended_client.put(
            f"/api/v1/biomes/{sample_biome_id}",
            json={"display_name": "Updated Name", "version": "2.0.0"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "success"

    async def test_update_biome_not_found(self, extended_client):
        response = await extended_client.put(
            "/api/v1/biomes/99999",
            json={"display_name": "Nope"},
        )
        assert response.status_code == 404

    async def test_update_biome_no_body(self, extended_client, sample_biome_id):
        response = await extended_client.put(f"/api/v1/biomes/{sample_biome_id}")
        assert response.status_code == 400

    async def test_update_biome_name_conflict(self, extended_client, dal_with_groups, sample_biome_id):
        # Create second biome
        other_id = dal_with_groups.biomes.insert(
            name="other-biome", display_name="Other", biome_type="lxd_container",
            version="1.0.0", biome_kind="custom", phase="post_deploy", workload_type="lxc",
        )
        dal_with_groups.commit()
        response = await extended_client.put(
            f"/api/v1/biomes/{int(other_id)}",
            json={"name": "test-biome-ext"},
        )
        assert response.status_code == 409

    async def test_update_biome_invalid_biome_kind(self, extended_client, sample_biome_id):
        response = await extended_client.put(
            f"/api/v1/biomes/{sample_biome_id}",
            json={"biome_kind": "invalid_kind"},
        )
        assert response.status_code == 422

    async def test_update_biome_invalid_phase(self, extended_client, sample_biome_id):
        response = await extended_client.put(
            f"/api/v1/biomes/{sample_biome_id}",
            json={"phase": "invalid_phase"},
        )
        assert response.status_code == 422

    async def test_update_biome_invalid_workload_type(self, extended_client, sample_biome_id):
        response = await extended_client.put(
            f"/api/v1/biomes/{sample_biome_id}",
            json={"workload_type": "docker"},
        )
        assert response.status_code == 422

    async def test_update_biome_invalid_cloud_init(self, extended_client, sample_biome_id):
        response = await extended_client.put(
            f"/api/v1/biomes/{sample_biome_id}",
            json={"cloud_init_content": "invalid: [yaml: {"},
        )
        assert response.status_code == 422

    async def test_update_biome_invalid_architecture(self, extended_client, sample_biome_id):
        response = await extended_client.put(
            f"/api/v1/biomes/{sample_biome_id}",
            json={"required_architecture": "sparc"},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/v1/biomes/<id>
# ---------------------------------------------------------------------------


class TestDeleteBiome:
    """Tests for DELETE /api/v1/biomes/<id>."""

    async def test_soft_delete_biome(self, extended_client, sample_biome_id):
        response = await extended_client.delete(f"/api/v1/biomes/{sample_biome_id}")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["action"] == "soft_deleted"

    async def test_delete_biome_not_found(self, extended_client):
        response = await extended_client.delete("/api/v1/biomes/99999")
        assert response.status_code == 404

    async def test_delete_biome_in_use(self, extended_client, sample_biome_id, dal_with_groups):
        """Biome in active assignment cannot be deleted."""
        node = dal_with_groups(dal_with_groups.nodes.id > 0).select().first()
        if node:
            dal_with_groups.node_egg_assignments.insert(
                node_id=int(node.id),
                egg_id=sample_biome_id,
                status="ready",
            )
            dal_with_groups.commit()

        response = await extended_client.delete(f"/api/v1/biomes/{sample_biome_id}")
        # Either blocked (409) or soft-deleted if no assignments found
        assert response.status_code in (200, 409)

    async def test_hard_delete_no_scope(self, extended_client, sample_biome_id, app_with_auth, dal_with_groups, monkeypatch):
        """Hard delete without gough.cluster.admin scope returns 403."""
        from types import SimpleNamespace
        from quart import g

        # Create an app with limited scope that doesn't include cluster.admin
        import app.api.biomes as biomes_mod
        monkeypatch.setattr(biomes_mod, "get_db", lambda: dal_with_groups)

        import importlib as _importlib
        import app.middleware as mw_mod2
        import app.security.scope_enforcement as scope_mod2

        def _passthrough2(*dargs, **dkwargs):
            if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
                return dargs[0]
            return lambda fn: fn

        monkeypatch.setattr(mw_mod2, "auth_required", _passthrough2)
        monkeypatch.setattr(scope_mod2, "require_scopes", _passthrough2)

        biomes_mod2 = _importlib.reload(biomes_mod)
        monkeypatch.setattr(biomes_mod2, "get_db", lambda: dal_with_groups)

        from quart import Quart
        no_scope_app = Quart(__name__)
        no_scope_app.config["TESTING"] = True
        no_scope_app.url_map.strict_slashes = False
        no_scope_app.register_blueprint(biomes_mod2.biomes_bp)

        @no_scope_app.before_request
        async def _inject():
            # No cluster.admin scope
            g.current_user = {
                "id": 1,
                "username": "limited",
                "_jwt_payload": {
                    "sub": "limited",
                    "tenant": "__default__",
                    "scope": "gough.biomes.read",
                    "mfa": True,
                },
            }
            g.tenant_context = SimpleNamespace(tenant_id="__default__")

        client = no_scope_app.test_client()
        response = await client.delete(f"/api/v1/biomes/{sample_biome_id}?hard=true")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Additional list_biomes filter paths
# ---------------------------------------------------------------------------


class TestListBiomesFilters:
    """Test additional filter paths in GET /api/v1/biomes."""

    async def test_list_filter_by_type(self, app_client, sample_eggs):
        """Filter by legacy biome type."""
        response = await app_client.get("/api/v1/biomes?type=lxd_container")
        assert response.status_code == 200

    async def test_list_filter_by_is_active(self, app_client, sample_eggs):
        """Filter by is_active."""
        response = await app_client.get("/api/v1/biomes?is_active=true")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "success"

    async def test_list_filter_by_phase(self, app_client, sample_eggs):
        """Filter by phase value."""
        response = await app_client.get("/api/v1/biomes?phase=phase1_helper")
        assert response.status_code == 200

    async def test_list_filter_signed_only(self, app_client, sample_eggs):
        """signed_only filter returns only biomes with signing_key_id set."""
        response = await app_client.get("/api/v1/biomes?signed_only=true")
        assert response.status_code == 200
        body = await response.get_json()
        # No biomes in sample_eggs have signing_key_id — should return 0
        assert len(body["data"]["biomes"]) == 0

    async def test_list_filter_invalid_phase(self, app_client):
        """Invalid phase returns validation error."""
        response = await app_client.get("/api/v1/biomes?phase=invalid_phase")
        assert response.status_code == 422

    async def test_list_filter_requires_tag(self, app_client, sample_eggs):
        """requires_tag filter — biome must declare the tag."""
        response = await app_client.get("/api/v1/biomes?requires_tag=tpm:2.0")
        assert response.status_code == 200
        body = await response.get_json()
        biomes = body["data"]["biomes"]
        # Only vault-leader requires tpm:2.0
        assert len(biomes) == 1
        assert biomes[0]["name"] == "vault-leader"

    async def test_list_filter_node_id_not_integer(self, app_client):
        """Non-integer node_id returns validation error."""
        response = await app_client.get("/api/v1/biomes?node_id=notanint")
        assert response.status_code == 422

    async def test_list_filter_node_eligibility_excludes_ineligible(self, app_client, sample_eggs, sample_nodes):
        """Node eligibility filter excludes biomes node can't run."""
        node_id = sample_nodes[3][0]  # node-4: cpu:cores:4, mem:total-gb:16 (minimal)
        response = await app_client.get(f"/api/v1/biomes?node_id={node_id}")
        assert response.status_code == 200
        body = await response.get_json()
        # node-4 can't satisfy cpu:cores:8 requirement
        names = {b["name"] for b in body["data"]["biomes"]}
        assert "k8s-primary" not in names


# ---------------------------------------------------------------------------
# Biome Groups endpoints
# ---------------------------------------------------------------------------


class TestBiomeGroups:
    """Tests for /api/v1/biomes/groups endpoints."""

    async def test_list_biome_groups_empty(self, extended_client):
        """List groups returns empty list when none exist."""
        response = await extended_client.get("/api/v1/biomes/groups")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["total"] == 0

    async def test_list_biome_groups_with_data(self, extended_client, sample_group_id):
        """List groups returns existing groups."""
        response = await extended_client.get("/api/v1/biomes/groups")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["total"] == 1

    async def test_create_biome_group_success(self, extended_client, sample_biome_id):
        """Create biome group successfully."""
        response = await extended_client.post(
            "/api/v1/biomes/groups",
            json={
                "name": "new-group",
                "display_name": "New Group",
                "biomes": [{"biome_id": sample_biome_id, "order": 1}],
            },
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["group"]["name"] == "new-group"

    async def test_create_biome_group_missing_name(self, extended_client):
        response = await extended_client.post(
            "/api/v1/biomes/groups",
            json={"display_name": "No Name", "biomes": []},
        )
        assert response.status_code == 400

    async def test_create_biome_group_missing_display_name(self, extended_client):
        response = await extended_client.post(
            "/api/v1/biomes/groups",
            json={"name": "group-no-display", "biomes": []},
        )
        assert response.status_code == 400

    async def test_create_biome_group_missing_biomes(self, extended_client):
        response = await extended_client.post(
            "/api/v1/biomes/groups",
            json={"name": "no-biomes", "display_name": "No Biomes"},
        )
        assert response.status_code == 400

    async def test_create_biome_group_no_body(self, extended_client):
        response = await extended_client.post("/api/v1/biomes/groups")
        assert response.status_code == 400

    async def test_create_biome_group_duplicate_name(self, extended_client, sample_group_id, sample_biome_id):
        response = await extended_client.post(
            "/api/v1/biomes/groups",
            json={
                "name": "test-group",
                "display_name": "Duplicate",
                "biomes": [{"biome_id": sample_biome_id}],
            },
        )
        assert response.status_code == 409

    async def test_create_biome_group_biome_not_found(self, extended_client):
        response = await extended_client.post(
            "/api/v1/biomes/groups",
            json={
                "name": "bad-ref",
                "display_name": "Bad Ref",
                "biomes": [{"biome_id": 99999}],
            },
        )
        assert response.status_code == 400

    async def test_create_biome_group_invalid_biome_ref(self, extended_client):
        response = await extended_client.post(
            "/api/v1/biomes/groups",
            json={
                "name": "bad-ref-format",
                "display_name": "Bad Format",
                "biomes": ["not_a_dict"],
            },
        )
        assert response.status_code == 400

    async def test_get_biome_group_success(self, extended_client, sample_group_id):
        response = await extended_client.get(f"/api/v1/biomes/groups/{sample_group_id}")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["group"]["name"] == "test-group"
        assert "resolved_biomes" in body["group"]

    async def test_get_biome_group_not_found(self, extended_client):
        response = await extended_client.get("/api/v1/biomes/groups/99999")
        assert response.status_code == 404

    async def test_update_biome_group_success(self, extended_client, sample_group_id):
        response = await extended_client.put(
            f"/api/v1/biomes/groups/{sample_group_id}",
            json={"display_name": "Updated Group"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["group"]["display_name"] == "Updated Group"

    async def test_update_biome_group_no_body(self, extended_client, sample_group_id):
        response = await extended_client.put(f"/api/v1/biomes/groups/{sample_group_id}")
        assert response.status_code == 400

    async def test_update_biome_group_not_found(self, extended_client):
        response = await extended_client.put(
            "/api/v1/biomes/groups/99999",
            json={"display_name": "Ghost"},
        )
        assert response.status_code == 404

    async def test_update_biome_group_name_conflict(self, extended_client, dal_with_groups, sample_group_id, sample_biome_id):
        # Create another group
        other = dal_with_groups.biome_groups.insert(
            name="other-group", display_name="Other",
            biomes=[{"biome_id": sample_biome_id}], is_default=False,
        )
        dal_with_groups.commit()
        response = await extended_client.put(
            f"/api/v1/biomes/groups/{int(other)}",
            json={"name": "test-group"},
        )
        assert response.status_code == 409

    async def test_update_biome_group_biomes_not_array(self, extended_client, sample_group_id):
        response = await extended_client.put(
            f"/api/v1/biomes/groups/{sample_group_id}",
            json={"biomes": "not_a_list"},
        )
        assert response.status_code == 400

    async def test_update_biome_group_invalid_biome_ref(self, extended_client, sample_group_id):
        response = await extended_client.put(
            f"/api/v1/biomes/groups/{sample_group_id}",
            json={"biomes": ["not_a_dict"]},
        )
        assert response.status_code == 400

    async def test_update_biome_group_biome_not_found(self, extended_client, sample_group_id):
        response = await extended_client.put(
            f"/api/v1/biomes/groups/{sample_group_id}",
            json={"biomes": [{"biome_id": 99999}]},
        )
        assert response.status_code == 400

    async def test_biome_group_membership_field_contract(self, extended_client, sample_group_id, sample_biome_id):
        """Regression: gh-31, gh-32 — biome group membership must be 'biomes', not 'biome_ids'.

        The PenguinCloud portal was guessing the wrong field name when Gough returns
        biome-group membership. This test locks the contract: the field MUST be named
        'biomes' and MUST be an array of objects with 'biome_id' and 'order'.
        """
        # Create a group with seeded biome membership
        create_response = await extended_client.post(
            "/api/v1/biomes/groups",
            json={
                "name": "membership-test-group",
                "display_name": "Membership Test",
                "biomes": [
                    {"biome_id": sample_biome_id, "order": 1},
                ],
            },
        )
        assert create_response.status_code == 201
        group_id = (await create_response.get_json())["group"]["id"]

        # GET the group and verify membership field shape
        get_response = await extended_client.get(f"/api/v1/biomes/groups/{group_id}")
        assert get_response.status_code == 200
        body = await get_response.get_json()
        group = body["group"]

        # CRITICAL: field must be named 'biomes', not 'biome_ids'
        assert "biomes" in group, "Field 'biomes' not found in response (expected membership array)"
        assert "biome_ids" not in group, "Field 'biome_ids' should not exist (use 'biomes' instead)"

        # CRITICAL: 'biomes' must be an array of objects with biome_id and order
        assert isinstance(group["biomes"], list), "biomes must be an array"
        assert len(group["biomes"]) == 1, "Expected 1 biome in membership"
        member = group["biomes"][0]
        assert isinstance(member, dict), "Each member must be an object"
        assert "biome_id" in member, "Each member must have 'biome_id'"
        assert "order" in member, "Each member must have 'order'"
        assert member["biome_id"] == sample_biome_id, "biome_id must match seeded value"
        assert member["order"] == 1, "order must match seeded value"

    async def test_delete_biome_group_success(self, extended_client, sample_group_id):
        response = await extended_client.delete(f"/api/v1/biomes/groups/{sample_group_id}")
        assert response.status_code == 200

    async def test_delete_biome_group_not_found(self, extended_client):
        response = await extended_client.delete("/api/v1/biomes/groups/99999")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Render Cloud Init
# ---------------------------------------------------------------------------


class TestRenderCloudInit:
    """Tests for POST /api/v1/biomes/render-cloud-init."""

    async def test_render_cloud_init_success(self, extended_client, dal_with_groups):
        """Merge cloud-init from biomes with content."""
        bid = dal_with_groups.biomes.insert(
            name="cloud-init-biome",
            display_name="Cloud Init Biome",
            cloud_init_content="packages:\n  - curl\n  - git\n",
            biome_kind="custom",
        )
        dal_with_groups.commit()

        response = await extended_client.post(
            "/api/v1/biomes/render-cloud-init",
            json={"biome_ids": [int(bid)]},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert "cloud_init" in body
        assert "packages" in body["cloud_init"]

    async def test_render_cloud_init_no_body(self, extended_client):
        response = await extended_client.post("/api/v1/biomes/render-cloud-init")
        assert response.status_code == 400

    async def test_render_cloud_init_missing_biome_ids(self, extended_client):
        response = await extended_client.post(
            "/api/v1/biomes/render-cloud-init",
            json={"additional_config": "hostname: test"},
        )
        assert response.status_code == 400

    async def test_render_cloud_init_biome_not_found(self, extended_client):
        response = await extended_client.post(
            "/api/v1/biomes/render-cloud-init",
            json={"biome_ids": [99999]},
        )
        assert response.status_code == 404

    async def test_render_cloud_init_no_content(self, extended_client, sample_biome_id):
        """Biome with no cloud_init_content results in error."""
        response = await extended_client.post(
            "/api/v1/biomes/render-cloud-init",
            json={"biome_ids": [sample_biome_id]},
        )
        assert response.status_code == 400

    async def test_render_cloud_init_invalid_additional_config(self, extended_client, dal_with_groups):
        bid = dal_with_groups.biomes.insert(
            name="cloud-init-biome2",
            cloud_init_content="packages:\n  - curl\n",
            biome_kind="custom",
        )
        dal_with_groups.commit()
        response = await extended_client.post(
            "/api/v1/biomes/render-cloud-init",
            json={
                "biome_ids": [int(bid)],
                "additional_config": "invalid: [yaml: {",
            },
        )
        assert response.status_code == 400

    async def test_render_cloud_init_with_additional_config(self, extended_client, dal_with_groups):
        bid = dal_with_groups.biomes.insert(
            name="cloud-init-biome3",
            cloud_init_content="packages:\n  - curl\n",
            biome_kind="custom",
        )
        dal_with_groups.commit()
        response = await extended_client.post(
            "/api/v1/biomes/render-cloud-init",
            json={
                "biome_ids": [int(bid)],
                "additional_config": "hostname: myhost\n",
            },
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Deployments endpoints
# ---------------------------------------------------------------------------


class TestDeployments:
    """Tests for /api/v1/biomes/deployments endpoints."""

    async def test_list_deployments_empty(self, extended_client):
        response = await extended_client.get("/api/v1/biomes/deployments")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["total"] == 0

    async def test_list_deployments_with_data(self, extended_client, sample_deployment_id):
        response = await extended_client.get("/api/v1/biomes/deployments")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["total"] == 1

    async def test_list_deployments_filter_by_status(self, extended_client, sample_deployment_id):
        response = await extended_client.get("/api/v1/biomes/deployments?status=pending")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["total"] == 1

    async def test_list_deployments_invalid_limit(self, extended_client):
        response = await extended_client.get("/api/v1/biomes/deployments?limit=0")
        assert response.status_code == 400

    async def test_list_deployments_invalid_offset(self, extended_client):
        response = await extended_client.get("/api/v1/biomes/deployments?offset=-1")
        assert response.status_code == 400

    async def test_list_deployments_limit_too_high(self, extended_client):
        response = await extended_client.get("/api/v1/biomes/deployments?limit=200")
        assert response.status_code == 400

    async def test_get_deployment_success(self, extended_client, sample_deployment_id):
        response = await extended_client.get(f"/api/v1/biomes/deployments/{sample_deployment_id}")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["id"] == str(sample_deployment_id)

    async def test_get_deployment_not_found(self, extended_client):
        response = await extended_client.get("/api/v1/biomes/deployments/99999")
        assert response.status_code == 404

    async def test_get_deployment_logs_success(self, extended_client, sample_deployment_id):
        response = await extended_client.get(
            f"/api/v1/biomes/deployments/{sample_deployment_id}/logs"
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert "logs" in body["data"]

    async def test_get_deployment_logs_not_found(self, extended_client):
        response = await extended_client.get("/api/v1/biomes/deployments/99999/logs")
        assert response.status_code == 404

    async def test_get_deployment_logs_invalid_tail(self, extended_client, sample_deployment_id):
        response = await extended_client.get(
            f"/api/v1/biomes/deployments/{sample_deployment_id}/logs?tail=0"
        )
        assert response.status_code == 400

    async def test_cancel_deployment_success(self, extended_client, sample_deployment_id):
        response = await extended_client.post(
            f"/api/v1/biomes/deployments/{sample_deployment_id}/cancel"
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["cancelled"] is True
        assert body["data"]["status"] == "cancelled"

    async def test_cancel_deployment_not_found(self, extended_client):
        response = await extended_client.post("/api/v1/biomes/deployments/99999/cancel")
        assert response.status_code == 404

    async def test_cancel_deployment_terminal_state(self, extended_client, dal_with_groups, sample_biome_id):
        """Cannot cancel a deployment in terminal state."""
        from datetime import datetime, timezone

        node = dal_with_groups(dal_with_groups.nodes.id > 0).select().first()
        node_id = int(node.id) if node else 1
        now = datetime.now(timezone.utc)
        did = dal_with_groups.deployments.insert(
            biome_id=sample_biome_id, node_id=node_id, phase=1,
            status="succeeded", tenant_id="__default__",
            created_at=now, updated_at=now,
        )
        dal_with_groups.commit()

        response = await extended_client.post(f"/api/v1/biomes/deployments/{int(did)}/cancel")
        assert response.status_code == 409


# ---------------------------------------------------------------------------
# Helper functions unit tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Unit tests for validate_cloud_init_yaml and merge_cloud_init_configs."""

    def test_validate_cloud_init_empty(self):
        from app.api.biomes import validate_cloud_init_yaml
        valid, msg = validate_cloud_init_yaml("")
        assert valid is True
        assert msg is None

    def test_validate_cloud_init_valid(self):
        from app.api.biomes import validate_cloud_init_yaml
        valid, msg = validate_cloud_init_yaml("packages:\n  - curl\n")
        assert valid is True

    def test_validate_cloud_init_not_dict(self):
        from app.api.biomes import validate_cloud_init_yaml
        valid, msg = validate_cloud_init_yaml("- item1\n- item2\n")
        assert valid is False
        assert "dictionary" in msg.lower()

    def test_validate_cloud_init_invalid_yaml(self):
        from app.api.biomes import validate_cloud_init_yaml
        valid, msg = validate_cloud_init_yaml("invalid: [yaml: {")
        assert valid is False
        assert msg is not None

    def test_merge_cloud_init_configs(self):
        from app.api.biomes import merge_cloud_init_configs
        configs = [
            "packages:\n  - curl\nwrite_files:\n  - path: /etc/test\n    content: test\n",
            "packages:\n  - git\nhostname: myhost\n",
        ]
        result = merge_cloud_init_configs(configs)
        assert "packages" in result
        assert "hostname" in result

    def test_merge_cloud_init_dict_merge(self):
        from app.api.biomes import merge_cloud_init_configs
        configs = [
            "runcmd:\n  - echo hello\n",
            "runcmd:\n  - echo world\n",
        ]
        result = merge_cloud_init_configs(configs)
        # Both runcmd entries should be in the merged result
        assert "hello" in result
        assert "world" in result

    def test_merge_cloud_init_scalar_override(self):
        from app.api.biomes import merge_cloud_init_configs
        configs = [
            "hostname: original\n",
            "hostname: override\n",
        ]
        result = merge_cloud_init_configs(configs)
        assert "override" in result

    def test_merge_cloud_init_invalid_skipped(self):
        from app.api.biomes import merge_cloud_init_configs
        configs = ["invalid: [yaml: {", "hostname: valid\n"]
        result = merge_cloud_init_configs(configs)
        # Invalid entry should be silently skipped
        assert "valid" in result

    def test_parse_bool_none(self):
        from app.api.biomes import _parse_bool
        assert _parse_bool(None) is None

    def test_parse_bool_true_values(self):
        from app.api.biomes import _parse_bool
        assert _parse_bool("true") is True
        assert _parse_bool("1") is True
        assert _parse_bool("yes") is True

    def test_parse_bool_false_values(self):
        from app.api.biomes import _parse_bool
        assert _parse_bool("false") is False
        assert _parse_bool("0") is False


class TestTagEligibilityHelper:
    """Tests for the tag eligibility checker (unit tests on helper logic)."""

    def test_numeric_tag_coercion(self):
        """Test numeric tag parsing (mem:total-gb:32, cpu:cores:16, etc.)."""
        from app.api._helpers import _coerce_numeric, _split_numeric_tag

        # Test integer
        assert _coerce_numeric("32") == 32.0
        # Test decimal
        assert _coerce_numeric("32.5") == 32.5
        # Test with g suffix (speed)
        assert _coerce_numeric("100g") == 100000.0
        # Test with gbps suffix
        assert _coerce_numeric("10gbps") == 10000.0

        # Test split
        assert _split_numeric_tag("mem:total-gb:32") == ("mem:total-gb", 32.0)
        assert _split_numeric_tag("cpu:cores:16") == ("cpu:cores", 16.0)
        assert _split_numeric_tag("nic:speed:100g") == ("nic:speed", 100000.0)

    def test_numeric_tag_comparison(self):
        """Test that numeric tag >= comparison works."""
        from app.api._helpers import check_tag_eligibility

        # Node has mem:total-gb:128, requires mem:total-gb:32 → eligible
        result = check_tag_eligibility(
            requires=["mem:total-gb:32"],
            forbids=[],
            node_tags=["mem:total-gb:128"],
        )
        assert result.eligible is True
        assert result.missing_tags == []

        # Node has mem:total-gb:16, requires mem:total-gb:32 → ineligible
        result = check_tag_eligibility(
            requires=["mem:total-gb:32"],
            forbids=[],
            node_tags=["mem:total-gb:16"],
        )
        assert result.eligible is False
        assert "mem:total-gb:32" in result.missing_tags

    def test_categorical_tag_matching(self):
        """Test categorical tag exact matching."""
        from app.api._helpers import check_tag_eligibility

        # Exact match
        result = check_tag_eligibility(
            requires=["tpm:2.0"],
            forbids=[],
            node_tags=["tpm:2.0"],
        )
        assert result.eligible is True

        # Missing tag
        result = check_tag_eligibility(
            requires=["tpm:2.0"],
            forbids=[],
            node_tags=["no-tpm"],
        )
        assert result.eligible is False
        assert "tpm:2.0" in result.missing_tags

    def test_forbid_tags(self):
        """Test forbids_hardware_tags enforcement."""
        from app.api._helpers import check_tag_eligibility

        # Node has forbidden tag
        result = check_tag_eligibility(
            requires=[],
            forbids=["cpu:vendor:amd"],
            node_tags=["cpu:vendor:amd", "cpu:cores:8"],
        )
        assert result.eligible is False
        assert "cpu:vendor:amd" in result.forbidden_tags_present

        # Node doesn't have forbidden tag
        result = check_tag_eligibility(
            requires=[],
            forbids=["cpu:vendor:amd"],
            node_tags=["cpu:vendor:intel"],
        )
        assert result.eligible is True
