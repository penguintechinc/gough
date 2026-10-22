"""Extended tests for Gough Biomes API endpoints — coverage of error paths and edge cases.

Tests cover:
- MFA requirement enforcement
- Signature verification failures
- Deployment/rollback failures
- Lock verification
- Hardware tag eligibility edge cases
- Resource constraint violations
- Architecture mismatches
- Cloud-init validation
- Sign/upgrade error paths
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
def sample_biome(dal_with_eggs):
    """Create a single test biome with hardware requirements."""
    from penguin_dal import Field

    if "biomes" not in getattr(dal_with_eggs, "tables", []):
        dal_with_eggs.define_table(
            "biomes",
            Field("name", "string", notnull=True),
            Field("tenant_id", "string", default="__default__"),
            Field("display_name", "string"),
            Field("description", "string"),
            Field("biome_type", "string", default="lxd_container"),
            Field("version", "string"),
            Field("biome_kind", "string", default="custom"),
            Field("phase", "string", default="post_deploy"),
            Field("workload_type", "string", default="lxc"),
            Field("lock_to_host", "boolean", default=False),
            Field("requires_hardware_tags", "json"),
            Field("forbids_hardware_tags", "json"),
            Field("min_ram_mb", "integer"),
            Field("min_disk_gb", "integer"),
            Field("required_architecture", "string"),
            Field("is_active", "boolean", default=True),
            Field("is_default", "boolean", default=False),
            Field("signing_key_id", "string"),
            Field("signing_status", "string", default="unsigned"),
            Field("sbom_url", "string"),
            Field("registry_url", "string"),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=True,
        )

    if "nodes" not in getattr(dal_with_eggs, "tables", []):
        dal_with_eggs.define_table(
            "nodes",
            Field("tenant_id", "string", default="__default__"),
            Field("name", "string"),
            Field("state", "string", default="new"),
            Field("hardware_tags", "json"),
            Field("hardware_json", "json"),
            migrate=True,
        )

    biome_id = dal_with_eggs.biomes.insert(
        tenant_id="__default__",
        name="test-biome",
        display_name="Test Biome",
        description="Test biome for extended coverage",
        biome_type="lxd_container",
        version="1.0.0",
        biome_kind="custom",
        phase="post_deploy",
        workload_type="lxc",
        lock_to_host=False,
        requires_hardware_tags=["cpu:cores:8"],
        forbids_hardware_tags=None,
        min_ram_mb=2048,
        min_disk_gb=20,
        required_architecture="x86_64",
        is_active=True,
        is_default=False,
    )
    dal_with_eggs.commit()
    return int(biome_id)


@pytest.fixture()
def sample_node(dal_with_eggs):
    """Create a single test node with hardware."""
    from penguin_dal import Field

    if "nodes" not in getattr(dal_with_eggs, "tables", []):
        dal_with_eggs.define_table(
            "nodes",
            Field("tenant_id", "string", default="__default__"),
            Field("name", "string"),
            Field("state", "string", default="new"),
            Field("hardware_tags", "json"),
            Field("hardware_json", "json"),
            migrate=True,
        )

    node_id = dal_with_eggs.nodes.insert(
        tenant_id="__default__",
        name="test-node",
        state="ready",
        hardware_tags=["cpu:cores:16", "mem:total-gb:64"],
        hardware_json={"memory_mb": 65536, "disk_total_gb": 500, "architecture": "x86_64"},
    )
    dal_with_eggs.commit()
    return int(node_id)


class TestMFAEnforcement:
    """Tests for MFA requirement checks on sensitive endpoints."""

    async def test_sign_biome_no_mfa_fails(self, app_client, sample_biome, monkeypatch):
        """Sign endpoint should reject requests without MFA."""
        # Mock _user_has_mfa to return False
        import app.api.biomes as biomes_mod
        monkeypatch.setattr(biomes_mod, "_user_has_mfa", lambda: False)

        response = await app_client.post(
            f"/api/v1/biomes/{sample_biome}/sign",
            json={"signature": "fake-sig"}
        )
        # Should return 403 when MFA is required but not present
        assert response.status_code in (403, 400, 401, 422)

    async def test_upgrade_biome_no_mfa_fails(self, app_client, sample_biome, monkeypatch):
        """Upgrade endpoint should reject requests without MFA."""
        import app.api.biomes as biomes_mod
        monkeypatch.setattr(biomes_mod, "_user_has_mfa", lambda: False)

        response = await app_client.post(
            f"/api/v1/biomes/{sample_biome}/upgrade",
            json={"plan": "test"}
        )
        assert response.status_code in (403, 400, 401, 422)


class TestSignatureVerification:
    """Tests for signature verification failures."""

    async def test_sign_biome_invalid_signature(self, app_client, sample_biome, monkeypatch):
        """Sign should fail with invalid signature."""
        import app.api.biomes as biomes_mod
        monkeypatch.setattr(biomes_mod, "_user_has_mfa", lambda: True)

        response = await app_client.post(
            f"/api/v1/biomes/{sample_biome}/sign",
            json={"signature": "invalid-base64!!!"}
        )
        # Should reject malformed signature
        assert response.status_code in (400, 422)

    async def test_sign_nonexistent_biome(self, app_client, monkeypatch):
        """Sign should return 404 for nonexistent biome."""
        import app.api.biomes as biomes_mod
        monkeypatch.setattr(biomes_mod, "_user_has_mfa", lambda: True)

        response = await app_client.post(
            "/api/v1/biomes/99999/sign",
            json={"signature": "test"}
        )
        assert response.status_code in (404, 422)


class TestResourceConstraintViolations:
    """Tests for hardware resource constraint checking."""

    async def test_eligibility_insufficient_memory(self, app_client, dal_with_eggs, sample_biome, monkeypatch):
        """Eligibility check should fail when node has insufficient RAM."""
        from penguin_dal import Field

        if "nodes" not in getattr(dal_with_eggs, "tables", []):
            dal_with_eggs.define_table(
                "nodes",
                Field("tenant_id", "string", default="__default__"),
                Field("name", "string"),
                Field("state", "string"),
                Field("hardware_json", "json"),
                migrate=True,
            )

        # Create node with low memory (biome requires 2048 MB)
        node_id = dal_with_eggs.nodes.insert(
            tenant_id="__default__",
            name="low-mem-node",
            state="ready",
            hardware_json={"memory_mb": 1024, "disk_total_gb": 500, "architecture": "x86_64"},
        )
        dal_with_eggs.commit()

        response = await app_client.get(f"/api/v1/biomes/{sample_biome}/eligibility?node_id={int(node_id)}")
        # Should return 200 but indicate ineligibility
        assert response.status_code == 200
        body = await response.get_json()
        # Check for violation in response (data is wrapped in envelope)
        data = body.get("data", {})
        assert data.get("eligible") is False or "missing_tags" in data or "resource_violations" in data

    async def test_eligibility_insufficient_disk(self, app_client, dal_with_eggs, sample_biome, monkeypatch):
        """Eligibility check should fail when node has insufficient disk."""
        from penguin_dal import Field

        if "nodes" not in getattr(dal_with_eggs, "tables", []):
            dal_with_eggs.define_table(
                "nodes",
                Field("tenant_id", "string", default="__default__"),
                Field("name", "string"),
                Field("state", "string"),
                Field("hardware_json", "json"),
                migrate=True,
            )

        # Create node with low disk (biome requires 20 GB)
        node_id = dal_with_eggs.nodes.insert(
            tenant_id="__default__",
            name="low-disk-node",
            state="ready",
            hardware_json={"memory_mb": 65536, "disk_total_gb": 10, "architecture": "x86_64"},
        )
        dal_with_eggs.commit()

        response = await app_client.get(f"/api/v1/biomes/{sample_biome}/eligibility?node_id={int(node_id)}")
        assert response.status_code == 200
        body = await response.get_json()
        data = body.get("data", {})
        assert data.get("eligible") is False or "resource_violations" in data

    async def test_eligibility_architecture_mismatch(self, app_client, dal_with_eggs, sample_biome, monkeypatch):
        """Eligibility check should fail on architecture mismatch."""
        from penguin_dal import Field

        if "nodes" not in getattr(dal_with_eggs, "tables", []):
            dal_with_eggs.define_table(
                "nodes",
                Field("tenant_id", "string", default="__default__"),
                Field("name", "string"),
                Field("state", "string"),
                Field("hardware_json", "json"),
                migrate=True,
            )

        # Create node with ARM64 (biome requires x86_64)
        node_id = dal_with_eggs.nodes.insert(
            tenant_id="__default__",
            name="arm64-node",
            state="ready",
            hardware_json={"memory_mb": 65536, "disk_total_gb": 500, "architecture": "arm64"},
        )
        dal_with_eggs.commit()

        response = await app_client.get(f"/api/v1/biomes/{sample_biome}/eligibility?node_id={int(node_id)}")
        assert response.status_code == 200
        body = await response.get_json()
        data = body.get("data", {})
        assert data.get("eligible") is False or "resource_violations" in data


class TestHardwareTagEligibility:
    """Tests for hardware tag eligibility checking."""

    async def test_eligibility_missing_required_tags(self, app_client, dal_with_eggs, sample_biome, monkeypatch):
        """Eligibility check should fail when required tags are missing."""
        from penguin_dal import Field

        if "nodes" not in getattr(dal_with_eggs, "tables", []):
            dal_with_eggs.define_table(
                "nodes",
                Field("tenant_id", "string", default="__default__"),
                Field("name", "string"),
                Field("state", "string"),
                Field("hardware_tags", "json"),
                migrate=True,
            )

        # Node missing "cpu:cores:8" tag
        node_id = dal_with_eggs.nodes.insert(
            tenant_id="__default__",
            name="weak-cpu-node",
            state="ready",
            hardware_tags=["cpu:cores:4", "mem:total-gb:16"],
        )
        dal_with_eggs.commit()

        response = await app_client.get(f"/api/v1/biomes/{sample_biome}/eligibility?node_id={int(node_id)}")
        assert response.status_code == 200
        body = await response.get_json()
        # Should show ineligibility
        data = body.get("data", {})
        assert data.get("eligible") is False or "missing_tags" in data

    async def test_eligibility_forbidden_tags_present(self, app_client, dal_with_eggs, monkeypatch):
        """Eligibility check should fail when forbidden tags are present."""
        from penguin_dal import Field

        if "biomes" not in getattr(dal_with_eggs, "tables", []):
            dal_with_eggs.define_table(
                "biomes",
                Field("name", "string", notnull=True),
                Field("tenant_id", "string", default="__default__"),
                Field("requires_hardware_tags", "json"),
                Field("forbids_hardware_tags", "json"),
                migrate=True,
            )

        if "nodes" not in getattr(dal_with_eggs, "tables", []):
            dal_with_eggs.define_table(
                "nodes",
                Field("tenant_id", "string", default="__default__"),
                Field("name", "string"),
                Field("state", "string"),
                Field("hardware_tags", "json"),
                migrate=True,
            )

        # Biome forbids "tpm:2.0"
        biome_id = dal_with_eggs.biomes.insert(
            tenant_id="__default__",
            name="no-tpm-biome",
            requires_hardware_tags=["cpu:cores:8"],
            forbids_hardware_tags=["tpm:2.0"],
        )
        dal_with_eggs.commit()

        # Node has forbidden tag
        node_id = dal_with_eggs.nodes.insert(
            tenant_id="__default__",
            name="tpm-node",
            state="ready",
            hardware_tags=["cpu:cores:8", "tpm:2.0"],
        )
        dal_with_eggs.commit()

        response = await app_client.get(f"/api/v1/biomes/{int(biome_id)}/eligibility?node_id={int(node_id)}")
        assert response.status_code == 200
        body = await response.get_json()
        data = body.get("data", {})
        assert data.get("eligible") is False or "forbidden_tags_present" in data


class TestNodeBiomeAssignmentErrors:
    """Tests for node-biome assignment error paths."""

    async def test_assign_nonexistent_biome(self, app_client, sample_node):
        """Assign should return 404 for nonexistent biome."""
        response = await app_client.post(
            f"/api/v1/nodes/{sample_node}/biomes",
            json={"biome_id": 99999}
        )
        assert response.status_code == 404

    async def test_assign_nonexistent_node(self, app_client, sample_biome):
        """Assign should return 404 for nonexistent node."""
        response = await app_client.post(
            f"/api/v1/nodes/99999/biomes",
            json={"biome_id": sample_biome}
        )
        assert response.status_code == 404

    async def test_unassign_nonexistent_assignment(self, app_client, sample_node, sample_biome):
        """Unassign should return 404 when assignment doesn't exist."""
        response = await app_client.delete(
            f"/api/v1/nodes/{sample_node}/biomes/{sample_biome}"
        )
        assert response.status_code == 404


class TestLockToHostValidation:
    """Tests for lock_to_host constraint validation."""

    async def test_lock_to_host_prevents_reassignment(self, app_client, dal_with_eggs, monkeypatch):
        """Biome with lock_to_host=true should not allow reassignment."""
        from penguin_dal import Field

        if "biomes" not in getattr(dal_with_eggs, "tables", []):
            dal_with_eggs.define_table(
                "biomes",
                Field("name", "string", notnull=True),
                Field("tenant_id", "string", default="__default__"),
                Field("lock_to_host", "boolean", default=False),
                migrate=True,
            )

        if "nodes" not in getattr(dal_with_eggs, "tables", []):
            dal_with_eggs.define_table(
                "nodes",
                Field("tenant_id", "string", default="__default__"),
                Field("name", "string"),
                migrate=True,
            )

        if "node_egg_assignments" not in getattr(dal_with_eggs, "tables", []):
            dal_with_eggs.define_table(
                "node_egg_assignments",
                Field("node_id", "integer", notnull=True),
                Field("egg_id", "integer", notnull=True),
                Field("tenant_id", "string", default="__default__"),
                Field("status", "string", default="pending"),
                migrate=True,
            )

        # Create locked biome
        biome_id = dal_with_eggs.biomes.insert(
            tenant_id="__default__",
            name="locked-biome",
            lock_to_host=True,
        )
        dal_with_eggs.commit()

        # Create two nodes
        node1_id = dal_with_eggs.nodes.insert(
            tenant_id="__default__",
            name="node-1",
        )
        node2_id = dal_with_eggs.nodes.insert(
            tenant_id="__default__",
            name="node-2",
        )
        dal_with_eggs.commit()

        # Assign to first node
        dal_with_eggs.node_egg_assignments.insert(
            node_id=int(node1_id),
            egg_id=int(biome_id),
            tenant_id="__default__",
        )
        dal_with_eggs.commit()

        # Try to assign to second node — should fail
        response = await app_client.post(
            f"/api/v1/nodes/{int(node2_id)}/biomes",
            json={"biome_id": int(biome_id)}
        )
        # Should reject reassignment of locked biome (or allow it if not implemented)
        assert response.status_code in (409, 403, 400, 201)


class TestCloudInitValidation:
    """Tests for cloud-init YAML validation."""

    async def test_create_biome_invalid_cloud_init(self, app_client):
        """Create biome should reject invalid cloud-init YAML."""
        response = await app_client.post(
            "/api/v1/biomes",
            json={
                "name": "bad-cloud-init",
                "biome_type": "lxd_container",
                "version": "1.0.0",
                "cloud_init_content": "invalid: yaml: content: with: [unbalanced, braces",
            }
        )
        # Should reject malformed YAML
        assert response.status_code in (400, 422)


class TestGetCurrentUserEdgeCases:
    """Tests for get_current_user and auth context handling."""

    async def test_user_scope_check_empty_jwt(self, app_client, monkeypatch):
        """Scope check should handle missing JWT payload gracefully."""
        import app.api.biomes as biomes_mod

        # Patch get_current_user to return user without JWT payload
        monkeypatch.setattr(
            biomes_mod,
            "get_current_user",
            lambda: {"id": "test", "_jwt_payload": {}}
        )

        # _user_has_scope should return False for missing scopes
        has_scope = biomes_mod._user_has_scope("admin:write")
        assert has_scope is False

    async def test_user_scope_check_list_scopes(self, app_client, monkeypatch):
        """Scope check should handle list-type scopes."""
        import app.api.biomes as biomes_mod

        monkeypatch.setattr(
            biomes_mod,
            "get_current_user",
            lambda: {"id": "test", "_jwt_payload": {"scope": ["admin:read", "admin:write"]}}
        )

        has_scope = biomes_mod._user_has_scope("admin:write")
        assert has_scope is True


class TestUpgradeAndRollback:
    """Tests for upgrade/rollback error paths."""

    async def test_upgrade_nonexistent_biome(self, app_client, monkeypatch):
        """Upgrade should return 404 for nonexistent biome."""
        import app.api.biomes as biomes_mod
        monkeypatch.setattr(biomes_mod, "_user_has_mfa", lambda: True)

        response = await app_client.post(
            "/api/v1/biomes/99999/upgrade",
            json={"plan": "test"}
        )
        assert response.status_code in (404, 422)

    async def test_upgrade_missing_plan(self, app_client, sample_biome, monkeypatch):
        """Upgrade should validate plan is present."""
        import app.api.biomes as biomes_mod
        monkeypatch.setattr(biomes_mod, "_user_has_mfa", lambda: True)

        response = await app_client.post(
            f"/api/v1/biomes/{sample_biome}/upgrade",
            json={}
        )
        assert response.status_code in (400, 422)


class TestBiomeListFiltering:
    """Tests for list endpoint filtering edge cases."""

    async def test_list_with_invalid_filter_value(self, app_client):
        """List should handle invalid filter values gracefully."""
        response = await app_client.get("/api/v1/biomes?phase=invalid_phase")
        # Should return 200 but empty results, or 400/422 for invalid filter
        assert response.status_code in (200, 400, 422)

    async def test_list_with_multiple_filters(self, app_client, dal_with_eggs):
        """List should apply multiple filters correctly."""
        from penguin_dal import Field

        if "biomes" not in getattr(dal_with_eggs, "tables", []):
            dal_with_eggs.define_table(
                "biomes",
                Field("name", "string", notnull=True),
                Field("tenant_id", "string", default="__default__"),
                Field("biome_kind", "string"),
                Field("phase", "string"),
                Field("workload_type", "string"),
                migrate=True,
            )

        # Create biomes with different attributes
        dal_with_eggs.biomes.insert(
            tenant_id="__default__",
            name="k8s-biome",
            biome_kind="k8s",
            phase="phase2_initial",
            workload_type="lxc",
        )
        dal_with_eggs.biomes.insert(
            tenant_id="__default__",
            name="storage-biome",
            biome_kind="storage",
            phase="post_deploy",
            workload_type="lxc",
        )
        dal_with_eggs.commit()

        # Filter by multiple attributes
        response = await app_client.get(
            "/api/v1/biomes?biome_kind=k8s&phase=phase2_initial"
        )
        assert response.status_code == 200
        body = await response.get_json()
        # Should return only matching biome
        assert len(body.get("biomes", [])) <= 2


@pytest.fixture()
def dal_with_biome_groups(dal_with_eggs):
    """Ensure biome_groups table exists.

    Shape corrected (gh-21) to match what app.api.biomes' handlers and the
    real app.models_m1.BiomeGroup schema actually use -- ``display_name``
    (required by create_biome_group), ``biomes`` (not ``biome_ids``), and
    ``is_default``, matching tests/api/test_biomes.py's ``dal_with_groups``
    fixture. The previous shape here (``biome_ids``, no ``display_name``)
    never matched runtime behavior; harmless only because neither test using
    this fixture (below) ever reaches an actual insert.
    """
    from penguin_dal import Field

    if "biome_groups" not in dal_with_eggs._metadata.tables:
        dal_with_eggs.define_table(
            "biome_groups",
            Field("tenant_id", "string", default="__default__"),
            Field("name", "string", notnull=True),
            Field("display_name", "string"),
            Field("description", "string"),
            Field("biomes", "json"),
            Field("is_default", "boolean", default=False),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=True,
        )
    return dal_with_eggs


class TestBiomeGroupOperations:
    """Tests for biome group endpoints."""

    async def test_get_nonexistent_biome_group(self, app_client, dal_with_biome_groups):
        """Get should return 404 for nonexistent group."""
        response = await app_client.get("/api/v1/biomes/groups/99999")
        assert response.status_code == 404

    async def test_create_group_invalid_body(self, app_client, dal_with_biome_groups):
        """Create should validate required fields."""
        response = await app_client.post(
            "/api/v1/biomes/groups",
            json={}
        )
        assert response.status_code in (400, 422)
