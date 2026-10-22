"""Targeted coverage tests for biomes.py uncovered lines (Part 3).

Focuses on:
- Scope validation (_user_has_scope) with edge cases (list, string, null scopes)
- Resource constraint validation paths (lines 89, 116-129)
- Complex branching in eligibility evaluation
- Error handling in POST/PATCH operations
- Filter validation and edge cases
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _json(response) -> dict:
    """Helper to extract JSON from Quart response."""
    return json.loads(await response.get_data(as_text=True))


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    """Fresh in-memory SQLite penguin-dal DB for biomes tests."""
    from penguin_dal import DB, Field

    db = DB(
        f"sqlite:///{tmp_path}/test-biomes-cov3-{threading.get_ident()}.db",
        pool_size=1,
        reflect=False,
        migrate=True,
    )

    db.define_table(
        "biomes",
        Field("tenant_id", "string", default="__default__"),
        Field("name", "string"),
        Field("display_name", "string"),
        Field("description", "string"),
        Field("biome_type", "string", default="lxd_container"),
        Field("version", "string"),
        Field("egg_kind", "string", default="custom"),
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
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
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
        Field("hardware_json", "json"),
        Field("hardware_tags", "json"),
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

    from app.db import database as db_mod

    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    import app.api.biomes as biomes_mod

    monkeypatch.setattr(biomes_mod, "get_db", lambda: db)

    yield db
    try:
        db.close()
    except Exception:
        pass


class TestUserHasScopeEdgeCases:
    """Test _user_has_scope() with various scope claim formats."""

    def test_scope_as_list_of_strings(self, monkeypatch):
        """Test scope detection when claim is a list."""
        import importlib

        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "scope": ["read", "write", "admin"],
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_scope("write") is True
        assert biomes._user_has_scope("delete") is False

    def test_scope_as_space_separated_string(self, monkeypatch):
        """Test scope detection when claim is space-separated string."""
        import importlib

        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "scope": "read write admin",
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_scope("write") is True
        assert biomes._user_has_scope("delete") is False

    def test_scope_as_empty_string(self, monkeypatch):
        """Test scope detection with empty string."""
        import importlib

        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "scope": "",
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_scope("any") is False

    def test_scope_as_list_with_non_string(self, monkeypatch):
        """Test scope detection when list contains non-strings."""
        import importlib

        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "scope": ["read", 123, "write"],
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_scope("read") is True
        assert biomes._user_has_scope("123") is False

    def test_scope_missing_defaults_to_empty(self, monkeypatch):
        """Test scope detection when scope claim is missing."""
        import importlib

        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "sub": "user123",
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_scope("any") is False

    def test_scope_as_integer(self, monkeypatch):
        """Test scope detection when scope is an unexpected type."""
        import importlib

        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "scope": 123,
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_scope("any") is False


class TestResourceConstraintValidation:
    """Test eligibility evaluation with resource constraints (lines 106-142)."""

    @pytest.mark.asyncio
    async def test_min_ram_violation(self, dal):
        """Test eligibility when node RAM is below biome requirement."""
        now = datetime.now(timezone.utc)

        # Create biome requiring 4GB RAM
        biome_id = dal.biomes.insert(
            tenant_id="__default__",
            name="high_mem_biome",
            display_name="High Memory Biome",
            min_ram_mb=4096,
            created_at=now,
            updated_at=now,
        )

        # Create node with 2GB RAM
        node_id = dal.nodes.insert(
            tenant_id="__default__",
            name="low_mem_node",
            hardware_json={"memory_mb": 2048},
            created_at=now,
            updated_at=now,
        )

        biome = dal(dal.biomes.id == biome_id).select().first()
        node = dal(dal.nodes.id == node_id).select().first()

        from app.api.biomes import _eval_node_eligibility

        result = _eval_node_eligibility(dal, biome, node)

        assert result.eligible is False
        assert any(v["resource"] == "memory_mb" for v in result.resource_violations)

    @pytest.mark.asyncio
    async def test_min_disk_violation(self, dal):
        """Test eligibility when node disk is below biome requirement."""
        now = datetime.now(timezone.utc)

        biome_id = dal.biomes.insert(
            tenant_id="__default__",
            name="large_disk_biome",
            display_name="Large Disk Biome",
            min_disk_gb=500,
            created_at=now,
            updated_at=now,
        )

        node_id = dal.nodes.insert(
            tenant_id="__default__",
            name="small_disk_node",
            hardware_json={"disk_total_gb": 100},
            created_at=now,
            updated_at=now,
        )

        biome = dal(dal.biomes.id == biome_id).select().first()
        node = dal(dal.nodes.id == node_id).select().first()

        from app.api.biomes import _eval_node_eligibility

        result = _eval_node_eligibility(dal, biome, node)

        assert result.eligible is False
        assert any(v["resource"] == "disk_total_gb" for v in result.resource_violations)

    @pytest.mark.asyncio
    async def test_architecture_mismatch(self, dal):
        """Test eligibility when node architecture doesn't match."""
        now = datetime.now(timezone.utc)

        biome_id = dal.biomes.insert(
            tenant_id="__default__",
            name="arm_biome",
            display_name="ARM Biome",
            required_architecture="arm64",
            created_at=now,
            updated_at=now,
        )

        node_id = dal.nodes.insert(
            tenant_id="__default__",
            name="x86_node",
            hardware_json={"architecture": "x86_64"},
            created_at=now,
            updated_at=now,
        )

        biome = dal(dal.biomes.id == biome_id).select().first()
        node = dal(dal.nodes.id == node_id).select().first()

        from app.api.biomes import _eval_node_eligibility

        result = _eval_node_eligibility(dal, biome, node)

        assert result.eligible is False
        assert any(v["resource"] == "architecture" for v in result.resource_violations)

    @pytest.mark.asyncio
    async def test_architecture_any_matches_all(self, dal):
        """Test eligibility when biome requires 'any' architecture."""
        now = datetime.now(timezone.utc)

        biome_id = dal.biomes.insert(
            tenant_id="__default__",
            name="universal_biome",
            display_name="Universal Biome",
            required_architecture="any",
            created_at=now,
            updated_at=now,
        )

        node_id = dal.nodes.insert(
            tenant_id="__default__",
            name="any_node",
            hardware_json={"architecture": "arm64"},
            created_at=now,
            updated_at=now,
        )

        biome = dal(dal.biomes.id == biome_id).select().first()
        node = dal(dal.nodes.id == node_id).select().first()

        from app.api.biomes import _eval_node_eligibility

        result = _eval_node_eligibility(dal, biome, node)

        # Should not have architecture violation
        assert not any(v["resource"] == "architecture" for v in result.resource_violations)

    @pytest.mark.asyncio
    async def test_resource_constraint_with_invalid_values(self, dal):
        """Test eligibility when hardware values are unparseable."""
        now = datetime.now(timezone.utc)

        biome_id = dal.biomes.insert(
            tenant_id="__default__",
            name="test_biome",
            display_name="Test Biome",
            min_ram_mb=2048,
            created_at=now,
            updated_at=now,
        )

        # Node with invalid RAM value (non-numeric string)
        node_id = dal.nodes.insert(
            tenant_id="__default__",
            name="bad_hw_node",
            hardware_json={"memory_mb": "invalid"},
            created_at=now,
            updated_at=now,
        )

        biome = dal(dal.biomes.id == biome_id).select().first()
        node = dal(dal.nodes.id == node_id).select().first()

        from app.api.biomes import _eval_node_eligibility

        # Should not crash, just skip validation
        result = _eval_node_eligibility(dal, biome, node)

        # Invalid values are silently skipped
        assert not any(v["resource"] == "memory_mb" for v in result.resource_violations)

    @pytest.mark.asyncio
    async def test_resource_constraint_no_hardware_json(self, dal):
        """Test eligibility when node has no hardware_json."""
        now = datetime.now(timezone.utc)

        biome_id = dal.biomes.insert(
            tenant_id="__default__",
            name="test_biome",
            display_name="Test Biome",
            min_ram_mb=2048,
            created_at=now,
            updated_at=now,
        )

        # Node without hardware_json
        node_id = dal.nodes.insert(
            tenant_id="__default__",
            name="no_hw_node",
            created_at=now,
            updated_at=now,
        )

        biome = dal(dal.biomes.id == biome_id).select().first()
        node = dal(dal.nodes.id == node_id).select().first()

        from app.api.biomes import _eval_node_eligibility

        # Should handle missing hardware_json gracefully
        result = _eval_node_eligibility(dal, biome, node)

        assert not any(v["resource"] == "memory_mb" for v in result.resource_violations)

    @pytest.mark.asyncio
    async def test_all_resource_constraints_satisfied(self, dal):
        """Test eligibility when all resource constraints are met."""
        now = datetime.now(timezone.utc)

        biome_id = dal.biomes.insert(
            tenant_id="__default__",
            name="test_biome",
            display_name="Test Biome",
            min_ram_mb=2048,
            min_disk_gb=100,
            required_architecture="x86_64",
            created_at=now,
            updated_at=now,
        )

        node_id = dal.nodes.insert(
            tenant_id="__default__",
            name="good_node",
            hardware_json={
                "memory_mb": 8192,
                "disk_total_gb": 500,
                "architecture": "x86_64",
            },
            created_at=now,
            updated_at=now,
        )

        biome = dal(dal.biomes.id == biome_id).select().first()
        node = dal(dal.nodes.id == node_id).select().first()

        from app.api.biomes import _eval_node_eligibility

        result = _eval_node_eligibility(dal, biome, node)

        # No resource violations should exist
        assert not result.resource_violations


class TestComplianceLaneDetection:
    """Test compliance lane detection based on deployment tier."""

    def test_is_compliance_lane_alpha(self, monkeypatch):
        """Test that alpha tier is not a compliance lane."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "alpha")

        import importlib

        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)

        assert biomes._is_compliance_lane() is False

    def test_is_compliance_lane_dev(self, monkeypatch):
        """Test that dev tier is not a compliance lane."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "dev")

        import importlib

        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)

        assert biomes._is_compliance_lane() is False

    def test_is_compliance_lane_local(self, monkeypatch):
        """Test that local tier is not a compliance lane."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "local")

        import importlib

        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)

        assert biomes._is_compliance_lane() is False

    def test_is_compliance_lane_beta(self, monkeypatch):
        """Test that beta tier is a compliance lane."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "beta")

        import importlib

        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)

        assert biomes._is_compliance_lane() is True

    def test_is_compliance_lane_prod(self, monkeypatch):
        """Test that prod tier is a compliance lane."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "prod")

        import importlib

        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)

        assert biomes._is_compliance_lane() is True

    def test_is_compliance_lane_case_insensitive(self, monkeypatch):
        """Test that tier check is case-insensitive."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "ALPHA")

        import importlib

        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)

        assert biomes._is_compliance_lane() is False

    def test_is_compliance_lane_default(self, monkeypatch):
        """Test that missing tier defaults to alpha (not compliance)."""
        monkeypatch.delenv("GOUGH_DEPLOY_TIER", raising=False)

        import importlib

        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)

        assert biomes._is_compliance_lane() is False
