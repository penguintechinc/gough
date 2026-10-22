"""Test suite for app/catalog.py (builtin biome seeding).

Coverage targets:
- BUILTIN_BIOMES constant
- seed_builtin_biomes() function with various scenarios
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, MagicMock, patch

from app.catalog import BUILTIN_BIOMES, seed_builtin_biomes


class TestBuiltinBiomesConstant:
    """Test BUILTIN_BIOMES constant definition."""

    def test_builtin_biomes_count(self):
        """Test BUILTIN_BIOMES contains exactly 4 biomes."""
        assert len(BUILTIN_BIOMES) == 4

    def test_builtin_biomes_names(self):
        """Test BUILTIN_BIOMES contains expected names."""
        names = [b["name"] for b in BUILTIN_BIOMES]
        assert "k8s-primary" in names
        assert "k8s-worker" in names
        assert "nest-agent" in names
        assert "longhorn-agent" in names

    def test_k8s_primary_properties(self):
        """Test k8s-primary biome configuration."""
        k8s_primary = next(b for b in BUILTIN_BIOMES if b["name"] == "k8s-primary")
        assert k8s_primary["biome_kind"] == "infrastructure"
        assert k8s_primary["phase"] == "post_deploy"
        assert k8s_primary["lock_to_host"] is True
        assert k8s_primary["emits_joiner_secrets"] is True

    def test_k8s_worker_properties(self):
        """Test k8s-worker biome configuration."""
        k8s_worker = next(b for b in BUILTIN_BIOMES if b["name"] == "k8s-worker")
        assert k8s_worker["biome_kind"] == "infrastructure"
        assert k8s_worker["phase"] == "post_deploy"
        assert k8s_worker["lock_to_host"] is False
        assert k8s_worker["emits_joiner_secrets"] is False

    def test_nest_agent_properties(self):
        """Test nest-agent biome configuration."""
        nest_agent = next(b for b in BUILTIN_BIOMES if b["name"] == "nest-agent")
        assert nest_agent["biome_kind"] == "infrastructure"
        assert nest_agent["phase"] == "always_on"
        assert nest_agent["lock_to_host"] is False
        assert nest_agent["emits_joiner_secrets"] is False

    def test_longhorn_agent_properties(self):
        """Test longhorn-agent biome configuration."""
        longhorn_agent = next(b for b in BUILTIN_BIOMES if b["name"] == "longhorn-agent")
        assert longhorn_agent["biome_kind"] == "application"
        assert longhorn_agent["phase"] == "always_on"
        assert longhorn_agent["lock_to_host"] is False
        assert longhorn_agent["emits_joiner_secrets"] is False


class TestSeedBuiltinBiomes:
    """Test seed_builtin_biomes() function."""

    def test_seed_builtin_biomes_all_new(self):
        """Test seed_builtin_biomes() inserts all biomes when none exist."""
        mock_db = MagicMock()
        # All queries return None (no existing biomes)
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_query.select.return_value.first.return_value = None

        seed_builtin_biomes(mock_db)

        # Should call biomes.insert() 4 times
        assert mock_db.biomes.insert.call_count == 4
        mock_db.commit.assert_called_once()

    def test_seed_builtin_biomes_all_exist(self):
        """Test seed_builtin_biomes() skips existing biomes (idempotent)."""
        mock_db = MagicMock()
        # All queries return existing biome
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_query.select.return_value.first.return_value = Mock(id=1)

        seed_builtin_biomes(mock_db)

        # Should not insert any biomes
        assert mock_db.biomes.insert.call_count == 0
        mock_db.commit.assert_called_once()

    def test_seed_builtin_biomes_partial_exist(self):
        """Test seed_builtin_biomes() inserts only missing biomes."""
        mock_db = MagicMock()
        # First call returns None, rest return existing
        existing = Mock(id=1)
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_query.select.return_value.first.side_effect = [
            None,  # k8s-primary doesn't exist
            existing,  # k8s-worker exists
            None,  # nest-agent doesn't exist
            existing,  # longhorn-agent exists
        ]

        seed_builtin_biomes(mock_db)

        # Should insert only k8s-primary and nest-agent
        assert mock_db.biomes.insert.call_count == 2
        mock_db.commit.assert_called_once()

    def test_seed_builtin_biomes_insert_parameters_k8s_primary(self):
        """Test seed_builtin_biomes() passes correct parameters for k8s-primary."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_query.select.return_value.first.return_value = None

        seed_builtin_biomes(mock_db)

        # Find the call that inserted k8s-primary
        insert_calls = mock_db.biomes.insert.call_args_list
        k8s_primary_call = next(
            (call for call in insert_calls if call[1].get("name") == "k8s-primary"),
            None
        )
        assert k8s_primary_call is not None
        assert k8s_primary_call[1]["signature_verified"] is True
        assert k8s_primary_call[1]["published_at"] is not None

    def test_seed_builtin_biomes_signature_verified_true(self):
        """Test all inserted biomes have signature_verified=True."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_query.select.return_value.first.return_value = None

        seed_builtin_biomes(mock_db)

        # Check all insert calls have signature_verified=True
        for call in mock_db.biomes.insert.call_args_list:
            assert call[1]["signature_verified"] is True

    def test_seed_builtin_biomes_published_at_set(self):
        """Test all inserted biomes have published_at set."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_query.select.return_value.first.return_value = None

        seed_builtin_biomes(mock_db)

        # Check all insert calls have published_at
        for call in mock_db.biomes.insert.call_args_list:
            assert call[1]["published_at"] is not None
            assert isinstance(call[1]["published_at"], datetime)

    def test_seed_builtin_biomes_commit_called(self):
        """Test seed_builtin_biomes() calls db.commit()."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_query.select.return_value.first.return_value = Mock()

        seed_builtin_biomes(mock_db)

        mock_db.commit.assert_called_once()


class TestBuiltinBiomesIntegration:
    """Integration tests with real database."""

    def test_seed_builtin_biomes_real_dal(self, tmp_path):
        """Test seed_builtin_biomes() with penguin-dal."""
        pytest.importorskip("penguin_dal")
        from penguin_dal import DB, Field

        # Create file-based DB (in-memory SQLite not supported by penguin_dal)
        db_path = tmp_path / "test_catalog.db"
        db = DB(f"sqlite:///{db_path}", pool_size=1, reflect=False, migrate=True)
        db.define_table(
            "biomes",
            Field("name", "string", unique=True),
            Field("biome_kind", "string"),
            Field("phase", "string"),
            Field("lock_to_host", "boolean"),
            Field("emits_joiner_secrets", "boolean"),
            Field("signature_verified", "boolean"),
            Field("published_at", "datetime"),
            migrate=True,
        )

        # Seed biomes
        seed_builtin_biomes(db)

        # Verify all 4 biomes exist
        count = db(db.biomes.id > 0).count()
        assert count == 4

        # Verify k8s-primary properties
        k8s_primary = db(db.biomes.name == "k8s-primary").select().first()
        assert k8s_primary is not None
        assert k8s_primary.biome_kind == "infrastructure"
        assert k8s_primary.lock_to_host is True

        # Seeding again should be idempotent
        seed_builtin_biomes(db)
        count = db(db.biomes.id > 0).count()
        assert count == 4  # Still 4, not 8

        db.close()
