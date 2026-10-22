"""Tests for OIDC scope policy configuration."""

from __future__ import annotations

import pytest

from app.security.scope_policy import (
    ANONYMOUS_PATHS,
    KNOWN_SCOPES,
    SCOPE_POLICY,
    assert_policy_well_formed,
)


class TestKnownScopes:
    """KNOWN_SCOPES catalog tests."""

    def test_known_scopes_not_empty(self) -> None:
        """KNOWN_SCOPES is populated."""
        assert len(KNOWN_SCOPES) > 0

    def test_known_scopes_is_frozenset(self) -> None:
        """KNOWN_SCOPES is immutable."""
        assert isinstance(KNOWN_SCOPES, frozenset)

    def test_all_required_scopes_present(self) -> None:
        """Catalog includes all scopes from spec."""
        required = {
            "gough.cluster.read", "gough.nodes.read", "gough.disks.read",
            "gough.biomes.read", "gough.capacity.read", "gough.storage.read",
            "gough.audit.read", "gough.dr.read", "gough.bmc.read",
            "gough.nodes.provision", "gough.nodes.rekey",
            "gough.nodes.decommission", "gough.disks.plan",
            "gough.biomes.author", "gough.biomes.deploy", "gough.biomes.sign",
            "gough.storage.configure", "gough.migration.policy",
            "gough.migration.trigger", "gough.dr.drill",
            "gough.bmc.configure", "gough.joiner.read", "gough.joiner.rotate",
            "gough.cluster.admin", "gough.cluster.superadmin",
            "gough.dr.promote", "gough.biomes.unsafe-skip-signing",
            "gough.migration.override-lock",
        }
        assert required.issubset(KNOWN_SCOPES)


class TestScopePolicy:
    """SCOPE_POLICY mappings tests."""

    def test_scope_policy_not_empty(self) -> None:
        """SCOPE_POLICY has entries."""
        assert len(SCOPE_POLICY) > 0 and isinstance(SCOPE_POLICY, dict)

    def test_all_scopes_in_known_set(self) -> None:
        """All required scopes are in KNOWN_SCOPES."""
        for (method, path), scopes in SCOPE_POLICY.items():
            if scopes is not None:
                unknown = scopes - KNOWN_SCOPES
                assert not unknown, f"Unknown scopes at ({method}, {path})"

    def test_no_duplicate_path_method_entries(self) -> None:
        """No duplicate (method, path) entries."""
        entries = list(SCOPE_POLICY.keys())
        assert len(entries) == len(set(entries))

    def test_scope_policy_entry_count(self) -> None:
        """Verify scope policy has all required entries."""
        assert len(SCOPE_POLICY) >= 75, f"Expected 75+ entries, got {len(SCOPE_POLICY)}"

    def test_eggs_endpoints_mapped(self) -> None:
        """Verify biomes endpoints have scope mappings."""
        eggs_endpoints = [
            ("GET", "/api/v1/biomes"),
            ("POST", "/api/v1/biomes"),
            ("GET", "/api/v1/biomes/<int:biome_id>"),
            ("PUT", "/api/v1/biomes/<int:biome_id>"),
            ("DELETE", "/api/v1/biomes/<int:biome_id>"),
            ("POST", "/api/v1/biomes/<int:biome_id>/upload"),
            ("POST", "/api/v1/biomes/<int:biome_id>/sign"),
            ("POST", "/api/v1/biomes/<int:biome_id>/upgrade"),
            ("GET", "/api/v1/biomes/<int:biome_id>/eligibility"),
            ("POST", "/api/v1/biomes/render-cloud-init"),
            ("GET", "/api/v1/biomes/groups"),
            ("POST", "/api/v1/biomes/groups"),
        ]
        for method, path in eggs_endpoints:
            assert (method, path) in SCOPE_POLICY

    def test_no_todo_markers_remain(self) -> None:
        """Verify all TODO markers removed from scope_policy."""
        import inspect
        import re
        import app.security.scope_policy as sp_mod
        source = inspect.getsource(sp_mod)
        assert not re.search(r'#\s*(TODO|FIXME)', source), "TODO/FIXME markers remain in scope_policy"

    def test_anonymous_paths_includes_ipxe(self) -> None:
        """Verify new anonymous iPXE paths are included."""
        ipxe_paths = {
            ("GET", "/api/v1/ipxe/helper/<string:mac>"),
            ("GET", "/api/v1/ipxe/deploy/<string:mac>"),
            ("GET", "/api/v1/ipxe/kernel/<string:name>"),
            ("GET", "/api/v1/ipxe/initrd/<string:name>"),
            ("GET", "/api/v1/ipxe/helper-efi/<string:mac>"),
        }
        for path in ipxe_paths:
            assert path in ANONYMOUS_PATHS, f"Missing anonymous path: {path}"


class TestAnonymousPaths:
    """ANONYMOUS_PATHS configuration tests."""

    def test_anonymous_paths_not_empty_and_immutable(self) -> None:
        """ANONYMOUS_PATHS is immutable and populated."""
        assert isinstance(ANONYMOUS_PATHS, frozenset) and len(ANONYMOUS_PATHS) > 0

    def test_required_anonymous_paths_present(self) -> None:
        """Required anonymous endpoints defined."""
        required = {("GET", "/healthz"), ("GET", "/readyz"),
                    ("GET", "/api/v1/version")}
        assert required.issubset(ANONYMOUS_PATHS)

    def test_anonymous_paths_disjoint_from_policy(self) -> None:
        """Anonymous paths don't conflict with scope policy."""
        policy = set(SCOPE_POLICY.keys())
        for path in ANONYMOUS_PATHS:
            assert path not in policy


class TestPolicyValidation:
    """Policy well-formedness validation tests."""

    def test_assert_policy_well_formed_passes_at_import(self) -> None:
        """assert_policy_well_formed() passes without errors."""
        try:
            assert_policy_well_formed()
        except ValueError as e:
            pytest.fail(f"Policy validation failed: {e}")


class TestComplianceLanes:
    """Compliance lane scope requirements (Wave 2 preview)."""

    def test_compliance_lane_admin_scopes_in_known_scopes(self) -> None:
        """Admin compliance lane scopes are registered."""
        assert {"gough.cluster.admin",
                "gough.cluster.superadmin"}.issubset(KNOWN_SCOPES)

    def test_dangerous_override_scopes_registered(self) -> None:
        """Dangerous override scopes are in catalog."""
        assert {"gough.dr.promote", "gough.biomes.unsafe-skip-signing",
                "gough.migration.override-lock"}.issubset(KNOWN_SCOPES)
