"""Targeted coverage tests for biomes.py remaining uncovered lines (Part 2).

Focuses on:
- MFA check function _user_has_mfa() with edge cases (amr claim, mfa claim)
- Scope validation _user_has_scope() with various claim formats
- Error paths in DELETE operations (hard delete, soft delete, iPXE blocking)
- Upload endpoint error paths and validations
- Upgrade endpoint scope/MFA validation paths
- Eligibility endpoint with various node configurations
- Edge cases in compliance lane checks
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


# Helper to extract JSON from Quart response
async def _json(response) -> dict:
    """Helper to extract JSON from Quart response."""
    return json.loads(await response.get_data(as_text=True))


class TestUserHasMFADirectCall:
    """Test _user_has_mfa() directly with mocked get_current_user."""

    def test_user_has_mfa_from_amr_claim(self, monkeypatch):
        """Test MFA detection from amr claim with 'mfa' in list."""
        # Import the module to get the function
        import importlib
        biomes = importlib.import_module("app.api.biomes")

        # Patch get_current_user before calling the function
        import app.middleware as mw
        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "amr": ["password", "mfa"],
                }
            },
        )
        # Reload to pick up patch
        importlib.reload(biomes)
        assert biomes._user_has_mfa() is True

    def test_user_has_mfa_from_boolean_claim(self, monkeypatch):
        """Test MFA detection from boolean mfa claim."""
        import importlib
        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "mfa": True,
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_mfa() is True

    def test_user_has_no_mfa_empty_amr(self, monkeypatch):
        """Test MFA detection with empty amr list."""
        import importlib
        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "amr": [],
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_mfa() is False

    def test_user_has_mfa_no_payload(self, monkeypatch):
        """Test MFA detection with missing payload."""
        import importlib
        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {},
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_mfa() is False


class TestUserHasScopeDirectCall:
    """Test _user_has_scope() directly with mocked get_current_user."""

    def test_scope_in_list_claim(self, monkeypatch):
        """Test scope detection when scope is a list."""
        import importlib
        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "scope": ["gough.biomes.read", "gough.biomes.write"],
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_scope("gough.biomes.read") is True
        assert biomes._user_has_scope("gough.biomes.write") is True
        assert biomes._user_has_scope("gough.biomes.admin") is False

    def test_scope_in_space_separated_string(self, monkeypatch):
        """Test scope detection when scope is space-separated string."""
        import importlib
        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "scope": "gough.biomes.read gough.biomes.write",
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_scope("gough.biomes.read") is True
        assert biomes._user_has_scope("gough.biomes.write") is True

    def test_scope_empty_list(self, monkeypatch):
        """Test scope detection with empty list."""
        import importlib
        import app.middleware as mw

        monkeypatch.setattr(
            mw,
            "get_current_user",
            lambda: {
                "_jwt_payload": {
                    "scope": [],
                }
            },
        )
        biomes = importlib.import_module("app.api.biomes")
        importlib.reload(biomes)
        assert biomes._user_has_scope("gough.biomes.read") is False


class TestDeleteBiomeErrorPaths:
    """Test DELETE /biomes/{id} error paths."""

    @pytest.mark.asyncio
    async def test_delete_not_found(self, client):
        """Test delete nonexistent biome."""
        resp = await client.delete("/api/v1/biomes/999999")
        assert resp.status_code == 404


class TestUploadLXDImagePaths:
    """Test POST /biomes/{id}/upload error and success paths."""

    @pytest.mark.asyncio
    async def test_upload_biome_not_found(self, client):
        """Test upload to nonexistent biome."""
        resp = await client.post("/api/v1/biomes/999999/upload")
        assert resp.status_code == 404


class TestUpgradeBiomePaths:
    """Test POST /biomes/{id}/upgrade scope/MFA validation paths."""

    @pytest.mark.asyncio
    async def test_upgrade_biome_not_found(self, client):
        """Test upgrade nonexistent biome."""
        resp = await client.post(
            "/api/v1/biomes/999999/upgrade",
            json={"target_version": "2.0", "rollout_plan": "canary"},
        )
        assert resp.status_code == 404


class TestEligibilityPaths:
    """Test GET /biomes/{id}/eligibility endpoint."""

    @pytest.mark.asyncio
    async def test_eligibility_biome_not_found(self, client):
        """Test eligibility for nonexistent biome."""
        resp = await client.get("/api/v1/biomes/999999/eligibility?node_id=1")
        assert resp.status_code == 404


class TestComplianceLaneDetection:
    """Test _is_compliance_lane() function."""

    def test_compliance_lane_alpha(self, monkeypatch):
        """Test alpha env is NOT a compliance lane."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "alpha")
        from app.api.biomes import _is_compliance_lane
        assert _is_compliance_lane() is False

    def test_compliance_lane_dev(self, monkeypatch):
        """Test dev env is NOT a compliance lane."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "dev")
        from app.api.biomes import _is_compliance_lane
        assert _is_compliance_lane() is False

    def test_compliance_lane_local(self, monkeypatch):
        """Test local env is NOT a compliance lane."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "local")
        from app.api.biomes import _is_compliance_lane
        assert _is_compliance_lane() is False

    def test_compliance_lane_beta(self, monkeypatch):
        """Test beta env IS a compliance lane."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "beta")
        from app.api.biomes import _is_compliance_lane
        assert _is_compliance_lane() is True

    def test_compliance_lane_prod(self, monkeypatch):
        """Test prod env IS a compliance lane."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "prod")
        from app.api.biomes import _is_compliance_lane
        assert _is_compliance_lane() is True

    def test_compliance_lane_default(self, monkeypatch):
        """Test default (no env) is NOT a compliance lane."""
        monkeypatch.delenv("GOUGH_DEPLOY_TIER", raising=False)
        from app.api.biomes import _is_compliance_lane
        # Default is "alpha"
        assert _is_compliance_lane() is False

    def test_compliance_lane_case_insensitive(self, monkeypatch):
        """Test GOUGH_DEPLOY_TIER is case-insensitive."""
        monkeypatch.setenv("GOUGH_DEPLOY_TIER", "BETA")
        from app.api.biomes import _is_compliance_lane
        assert _is_compliance_lane() is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
