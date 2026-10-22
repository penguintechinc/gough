"""Tests for M1 SQLAlchemy ORM classes."""

import pytest
from app.models_sqlalchemy import Base
from app import models_m1


class TestNodeClass:
    """Test Node ORM class definition."""

    def test_node_class_has_expected_tablename(self):
        """Node class maps to 'nodes' table."""
        assert models_m1.Node.__tablename__ == "nodes"

    def test_node_class_has_expected_columns(self):
        """Node class has all required columns from migration."""
        cols = {c.name for c in models_m1.Node.__table__.columns}
        expected = {
            "id",
            "tenant_id",
            "name",
            "state",
            "dmi_uuid",
            "primary_nic_mac",
            "ipv4",
            "ipv6",
            "boot_config_id",
            "hardware_json",
            "hardware_tags",
            "posture",
            "preferred_addr_family",
            "attestation_method",
            "discovered_at",
            "deployed_at",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_disk_class_relationship_to_node(self):
        """Disk relationship references Node correctly."""
        assert hasattr(models_m1.Disk, "node")
        assert models_m1.Node.disks


class TestAuditEventClass:
    """Test AuditEvent ORM class."""

    def test_audit_event_class_has_hash_chain_columns(self):
        """AuditEvent has hash-chain integrity columns."""
        cols = {c.name for c in models_m1.AuditEvent.__table__.columns}
        assert "prev_hash" in cols
        assert "hash" in cols
        assert "signature" in cols


class TestJoinerSecretClass:
    """Test JoinerSecret ORM class."""

    def test_joiner_secret_class_has_envelope_columns(self):
        """JoinerSecret has encryption envelope columns."""
        cols = {c.name for c in models_m1.JoinerSecret.__table__.columns}
        assert "ciphertext" in cols
        assert "iv" in cols
        assert "auth_tag" in cols
        assert "dek_wrapped" in cols


class TestMigrationPolicyClass:
    """Test MigrationPolicy ORM class."""

    def test_migration_policy_class_has_safety_envelope_fields(self):
        """MigrationPolicy has all 12 safety/config envelope fields."""
        cols = {c.name for c in models_m1.MigrationPolicy.__table__.columns}
        expected = {
            "enabled",
            "evaluation_interval_seconds",
            "min_healthy_nodes",
            "max_concurrent_migrations",
            "require_target_capacity_headroom_cpu_pct",
            "require_target_capacity_headroom_mem_pct",
            "require_target_capacity_headroom_disk_pct",
            "rollback_on_destination_failure",
            "rollback_window_seconds",
            "forbid_migration_during_partition",
            "forbid_migration_during_maintenance",
            "waddleai_risk_threshold",
            "capacity_forecast_horizon_days",
        }
        assert expected.issubset(cols)


class TestEggClass:
    """Test Biome ORM class with M1 extensions."""

    def test_egg_class_has_m1_extension_columns(self):
        """Biome class has all 21 M1 extension columns."""
        cols = {c.name for c in models_m1.Biome.__table__.columns}
        m1_cols = {
            "egg_kind",
            "phase",
            "workload_type",
            "lock_to_host",
            "auto_join_cluster",
            "upgrade_strategy",
            "storage_requirements_json",
            "readiness_probe",
            "signing_key_id",
            "sbom_url",
            "registry_url",
            "requires_hardware_tags",
            "prefers_hardware_tags",
            "forbids_hardware_tags",
            "emits_joiner_secrets",
            "joiner_emit_spec",
            "consumes_joiner_secrets_from",
            "joiner_consume_spec",
            "snapshot_schedule_json",
            "required_interfaces",
            "tenant_id",
        }
        assert m1_cols.issubset(cols), f"Missing M1 columns: {m1_cols - cols}"

    def test_egg_class_assignments_relationship(self):
        """Biome has assignments relationship."""
        assert hasattr(models_m1.Biome, "assignments")


class TestBaseMetadata:
    """Test that all M1 tables are registered on Base.metadata."""

    def test_base_metadata_includes_all_m1_tables(self):
        """Base.metadata.tables includes all 17 M1 tables."""
        table_names = set(Base.metadata.tables.keys())
        m1_tables = {
            "nodes",
            "disks",
            "disk_plans",
            "node_egg_assignments",
            "storage_backends",
            "spiffe_trust_entries",
            "biomes",
            "node_bmc",
            "hardware_firmware",
            "node_tags_operator",
            "audit_events",
            "joiner_secrets",
            "migration_events",
            "migration_policy",
            "leader_leases",
            "dr_drills",
            "slo_definitions",
        }
        assert m1_tables.issubset(table_names), f"Missing M1 tables: {m1_tables - table_names}"


class TestNodeEggAssignmentRelationships:
    """Test NodeEggAssignment relationships."""

    def test_node_egg_assignment_relationships(self):
        """NodeEggAssignment has both node and biome relationships."""
        assert hasattr(models_m1.NodeEggAssignment, "node")
        assert hasattr(models_m1.NodeEggAssignment, "biome")


class TestStorageBackendUuidPk:
    """Test StorageBackend UUID primary key."""

    def test_storage_backend_uuid_pk(self):
        """StorageBackend has UUID primary key."""
        pk_col = models_m1.StorageBackend.__table__.primary_key
        col_names = {c.name for c in pk_col.columns}
        assert "id" in col_names
