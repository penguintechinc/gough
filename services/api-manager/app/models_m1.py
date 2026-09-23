"""SQLAlchemy ORM classes for M1 tables created by Alembic migrations 20260427_0930
through 20260427_1930. Importing this module registers all M1 classes on Base.metadata
so Alembic autogenerate sees them and Base.metadata.create_all(checkfirst=True)
creates them on fresh databases.
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import relationship, synonym

from .models_sqlalchemy import Base


class UUID(TypeDecorator):
    """Platform-agnostic UUID type that stores as STRING(36) and handles both UUID and string objects."""
    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value) if isinstance(value, str) else value


# =============================================================================
# Node & Infrastructure Tables
# =============================================================================


class Node(Base):
    """Bare-metal node inventory with state machine and hardware discovery."""

    __tablename__ = "nodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    name = Column(String(255), nullable=False)
    state = Column(String(32), nullable=False, server_default="new")
    dmi_uuid = Column(String(64), nullable=True)
    primary_nic_mac = Column(String(17), nullable=True)
    ipv4 = Column(String(15), nullable=True)
    ipv6 = Column(String(45), nullable=True)
    boot_config_id = Column(Integer, nullable=True)
    hardware_json = Column(JSON, nullable=True)
    hardware_tags = Column(JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True)
    posture = Column(String(32), nullable=False, server_default="compliant")
    preferred_addr_family = Column(String(16), nullable=False, server_default="auto")
    attestation_method = Column(String(32), nullable=False, server_default="discovery_agent")
    discovered_at = Column(DateTime(timezone=True), nullable=True)
    deployed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_nodes_tenant_name"),
        UniqueConstraint("tenant_id", "dmi_uuid", name="uq_nodes_tenant_dmi_uuid"),
        Index("ix_nodes_state", "state"),
        Index("ix_nodes_tenant_id", "tenant_id"),
        Index("ix_nodes_primary_nic_mac", "primary_nic_mac"),
        # postgresql_using is a dialect-specific kwarg: honored (GIN) when
        # compiled for Postgres, ignored (falls back to a standard index) on
        # every other dialect -- matches the migration's Postgres-only guard.
        Index("ix_nodes_hardware_tags", "hardware_tags", postgresql_using="gin"),
        {"extend_existing": True},
    )

    # Relationships
    disks = relationship("Disk", back_populates="node", cascade="all, delete-orphan")
    bmc = relationship("NodeBmc", back_populates="node", uselist=False)
    firmware = relationship("HardwareFirmware", back_populates="node", cascade="all, delete-orphan")
    tags_operator = relationship("NodeTagOperator", back_populates="node", cascade="all, delete-orphan")
    biome_assignments = relationship("NodeBiomeAssignment", back_populates="node", cascade="all, delete-orphan")


class Disk(Base):
    """Disk inventory with SMART status and storage backend assignment."""

    __tablename__ = "disks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    device_path = Column(String(255), nullable=False)
    serial = Column(String(64), nullable=True)
    capacity_bytes = Column(BigInteger, nullable=False)
    rotational = Column(Boolean, nullable=False, server_default='false')
    smart_status = Column(String(16), nullable=False, server_default="unknown")
    smart_attributes_json = Column(JSON, nullable=True)
    reserved_for_storage = Column(Boolean, nullable=False, server_default='false')
    storage_backend = Column(String(32), nullable=True)
    tier = Column(String(16), nullable=False, server_default="bulk")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("node_id", "device_path", name="uq_disks_node_device_path"),
        Index("ix_disks_node_id", "node_id"),
        Index("ix_disks_tenant_id", "tenant_id"),
        Index("ix_disks_serial", "serial"),
        Index("ix_disks_storage_backend", "storage_backend"),
        {"extend_existing": True},
    )

    # Relationships
    node = relationship("Node", back_populates="disks")
    disk_plans = relationship("DiskPlan", back_populates="disk", cascade="all, delete-orphan")


class DiskPlan(Base):
    """Disk partitioning and filesystem planning."""

    __tablename__ = "disk_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    disk_id = Column(Integer, ForeignKey("disks.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    mount_point = Column(String(255), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    fs_type = Column(String(16), nullable=False)
    partition_index = Column(Integer, nullable=False)
    raid_level = Column(String(8), nullable=True)
    encryption = Column(String(8), nullable=False, server_default="none")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("node_id", "mount_point", name="uq_disk_plans_node_mount_point"),
        Index("ix_disk_plans_node_id", "node_id"),
        Index("ix_disk_plans_disk_id", "disk_id"),
        Index("ix_disk_plans_tenant_id", "tenant_id"),
        {"extend_existing": True},
    )

    # Relationships
    disk = relationship("Disk", back_populates="disk_plans")


class StorageBackend(Base):
    """Distributed storage backend configuration and status."""

    __tablename__ = "storage_backends"

    id = Column(UUID(), primary_key=True)
    cluster_id = Column(UUID(), nullable=False)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    kind = Column(String(16), nullable=False)
    name = Column(String(255), nullable=False)
    is_default = Column(Boolean, nullable=False, server_default='false')
    config_json = Column(JSON, nullable=True)
    credentials_ref = Column(String(255), nullable=True)
    status = Column(String(16), nullable=False, server_default="initializing")
    capacity_total_bytes = Column(BigInteger, nullable=True)
    capacity_used_bytes = Column(BigInteger, nullable=True)
    health_check_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("cluster_id", "name", name="uq_storage_backends_cluster_name"),
        Index("ix_storage_backends_cluster_id", "cluster_id"),
        Index("ix_storage_backends_kind", "kind"),
        Index("ix_storage_backends_tenant_id", "tenant_id"),
        Index("ix_storage_backends_is_default", "is_default"),
        {"extend_existing": True},
    )


# =============================================================================
# Biome & Workload Tables
# =============================================================================


class Biome(Base):
    """Biome definitions with deployment, packaging, and workload orchestration."""

    __tablename__ = "biomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Pre-M1 baseline columns
    name = Column(String(255), nullable=False, server_default="")
    display_name = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    version = Column(String(64), nullable=True)
    category = Column(String(64), nullable=True)
    egg_kind = Column(String(64), nullable=True)
    snap_name = Column(String(255), nullable=True)
    snap_channel = Column(String(64), nullable=True, server_default="stable")
    snap_classic = Column(Boolean, nullable=False, server_default='false')
    cloud_init_content = Column(Text, nullable=True)
    lxd_image_alias = Column(String(255), nullable=True)
    # Written by app/api/biomes.py:create_biome alongside lxd_image_alias --
    # an alias names an image in a remote, a URL points at one directly, so
    # both are carried. Absent until now, which made every POST /api/v1/biomes
    # fail with CompileError("Unconsumed column names").
    lxd_image_url = Column(String(1024), nullable=True)
    # Operator-facing classification, distinct from biome_kind/workload_type
    # (which describe runtime shape). Also written by create_biome.
    biome_type = Column(String(64), nullable=True)
    lxd_profiles = Column(JSON, nullable=True)
    is_hypervisor_config = Column(Boolean, nullable=False, server_default='false')
    dependencies = Column(JSON, nullable=True)
    min_ram_mb = Column(Integer, nullable=True)
    min_disk_gb = Column(Integer, nullable=True)
    required_architecture = Column(String(32), nullable=True, server_default="any")
    is_active = Column(Boolean, nullable=False, server_default='true')
    is_default = Column(Boolean, nullable=False, server_default='false')
    checksum = Column(String(255), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    signing_status = Column(String(32), nullable=True, server_default="unsigned")
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    # M1 Extensions
    biome_kind = Column(String(32), nullable=False, server_default="custom")
    phase = Column(String(32), nullable=False, server_default="post_deploy")
    workload_type = Column(String(8), nullable=False, server_default="lxc")
    lock_to_host = Column(Boolean, nullable=False, server_default='false')
    auto_join_cluster = Column(Boolean, nullable=False, server_default='false')
    upgrade_strategy = Column(String(16), nullable=False, server_default="rolling")
    storage_requirements_json = Column(JSON, nullable=True)
    readiness_probe = Column(JSON, nullable=True)
    signing_key_id = Column(String(255), nullable=True)
    image_digest = Column(String(255), nullable=True)
    signature_verified = Column(Boolean, nullable=False, server_default='false')
    published_at = Column(DateTime(timezone=True), nullable=True)
    sbom_url = Column(String(1024), nullable=True)
    registry_url = Column(String(1024), nullable=True)
    requires_hardware_tags = Column(JSON, nullable=True)
    prefers_hardware_tags = Column(JSON, nullable=True)
    forbids_hardware_tags = Column(JSON, nullable=True)
    emits_joiner_secrets = Column(Boolean, nullable=False, server_default='false')
    joiner_emit_spec = Column(JSON, nullable=True)
    consumes_joiner_secrets_from = Column(JSON, nullable=True)
    joiner_consume_spec = Column(JSON, nullable=True)
    snapshot_schedule_json = Column(JSON, nullable=True)
    required_interfaces = Column(JSON, nullable=True)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")

    __table_args__ = (
        Index("ix_biomes_biome_kind", "biome_kind"),
        Index("ix_biomes_phase", "phase"),
        Index("ix_biomes_workload_type", "workload_type"),
        Index("ix_biomes_tenant_id", "tenant_id"),
        Index("ix_biomes_lock_to_host", "lock_to_host"),
        {"extend_existing": True},
    )

    # Relationships
    assignments = relationship("NodeBiomeAssignment", back_populates="biome")


class NodeBiomeAssignment(Base):
    """Assignment of biome instances to nodes with dependency tracking."""

    __tablename__ = "node_egg_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    egg_id = Column(Integer, ForeignKey("biomes.id", ondelete="RESTRICT"), nullable=False)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    phase = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, server_default="pending")
    depends_on_egg_instance_id = Column(
        Integer,
        ForeignKey("node_egg_assignments.id", ondelete="SET NULL"),
        nullable=True,
    )
    readiness_probe_state = Column(String(32), nullable=False, server_default="not_started")
    last_event_at = Column(DateTime(timezone=True), nullable=True)
    assigned_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    deployed_at = Column(DateTime(timezone=True), nullable=True)
    removed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("node_id", "egg_id", name="uq_node_egg_assignments_node_egg"),
        Index("ix_node_egg_assignments_node_id", "node_id"),
        Index("ix_node_egg_assignments_egg_id", "egg_id"),
        Index("ix_node_egg_assignments_status", "status"),
        Index("ix_node_egg_assignments_tenant_id", "tenant_id"),
        Index("ix_node_egg_assignments_phase", "phase"),
        {"extend_existing": True},
    )

    # Relationships
    node = relationship("Node", back_populates="biome_assignments")
    biome = relationship("Biome", back_populates="assignments")

    # Backward compatibility alias: biome_id -> egg_id
    biome_id = synonym("egg_id")


class Deployment(Base):
    """Orchestration status and lifecycle for biome deployments to nodes."""

    __tablename__ = "deployments"

    id = Column(String(64), primary_key=True)
    biome_id = Column(Integer, ForeignKey("biomes.id", ondelete="RESTRICT"), nullable=False)
    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    phase = Column(Integer, nullable=False, server_default="1")
    status = Column(String(32), nullable=False, server_default="pending")
    logs_url = Column(String(1024), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_deployments_status", "status"),
        Index("ix_deployments_egg_node", "biome_id", "node_id"),
        {"extend_existing": True},
    )


class UpgradeRun(Base):
    """One biome upgrade rollout, tracked across its canary/rollout phases.

    Backs ``POST /api/v1/biomes/{id}/upgrade`` (``app.api.biomes.upgrade_biome``
    inserts, ``_execute_upgrade_orchestration`` updates as phases progress).

    The table previously had no model: the baseline migration created it with
    raw DDL, carried over from a pre-baseline migration, and left "giving them
    proper models" as an explicit follow-up. That split the two schema-creation
    paths -- alembic produced the table, while ``app.models.init_db``'s
    ``create_all_tables()`` (which builds from ORM metadata) did not, so a
    deployment initialised that way had no ``upgrade_runs`` at all. This model
    is that follow-up; the column set matches the baseline's DDL exactly, and
    the duplicate raw DDL has been removed from the baseline so the ORM is the
    single source of truth.

    ``id`` is an app-supplied UUID string, not an autoincrement integer -- the
    same convention as ``deployments``/``joiner_secrets``. Callers must pass it.
    """

    __tablename__ = "upgrade_runs"

    id = Column(String(36), primary_key=True)
    biome_id = Column(Integer, ForeignKey("biomes.id"), nullable=False)
    target_version = Column(String(50), nullable=False)
    cluster_id = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False, server_default="pending")
    phase = Column(String(50), nullable=False, server_default="canary")
    nodes_total = Column(Integer, nullable=False, server_default="0")
    nodes_completed = Column(Integer, nullable=False, server_default="0")
    nodes_failed = Column(Integer, nullable=False, server_default="0")
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    rollback_reason = Column(Text, nullable=True)
    #: OIDC ``sub`` of whoever requested the upgrade -- audit only.
    actor_sub = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_upgrade_runs_biome_id", "biome_id"),
        Index("ix_upgrade_runs_status", "status"),
        Index("ix_upgrade_runs_cluster_id", "cluster_id"),
        {"extend_existing": True},
    )


# =============================================================================
# Hardware & Management Tables
# =============================================================================


class NodeBmc(Base):
    """Baseboard management controller configuration for out-of-band access."""

    __tablename__ = "node_bmc"

    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    protocol = Column(String(16), nullable=False)
    endpoint = Column(String(255), nullable=False)
    username_ref = Column(String(255), nullable=False)
    password_ref = Column(String(255), nullable=False)
    cert_fingerprint = Column(String(95), nullable=True)
    session_ttl_sec = Column(Integer, nullable=False, server_default='1800')
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    capabilities = Column(JSON, nullable=True)
    firmware_summary = Column(JSON, nullable=True)
    factory_creds_detected = Column(Boolean, nullable=False, server_default='false')
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_node_bmc_tenant_id", "tenant_id"),
        Index("ix_node_bmc_protocol", "protocol"),
        {"extend_existing": True},
    )

    # Relationships
    node = relationship("Node", back_populates="bmc")


class HardwareFirmware(Base):
    """Hardware firmware tracking with CVE monitoring."""

    __tablename__ = "hardware_firmware"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    component = Column(String(32), nullable=False)
    component_id = Column(String(255), nullable=False)
    current_version = Column(String(255), nullable=False)
    available_version = Column(String(255), nullable=True)
    cve_list = Column(JSON, nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    last_updated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("node_id", "component", "component_id", name="uq_hw_fw_node_comp_id"),
        Index("ix_hardware_firmware_node_id", "node_id"),
        Index("ix_hardware_firmware_component", "component"),
        Index("ix_hardware_firmware_tenant_id", "tenant_id"),
        {"extend_existing": True},
    )

    # Relationships
    node = relationship("Node", back_populates="firmware")


class NodeTagOperator(Base):
    """Operator-defined node tags for workload affinity and placement."""

    __tablename__ = "node_tags_operator"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    tag_key = Column(String(255), nullable=False)
    tag_value = Column(String(255), nullable=False)
    provenance = Column(String(32), nullable=False, server_default="operator")
    set_by_actor_sub = Column(String(255), nullable=True)
    set_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("node_id", "tag_key", "tag_value", name="uq_node_tags_op_node_key_val"),
        Index("ix_node_tags_operator_node_id", "node_id"),
        Index("ix_node_tags_operator_tag_key", "tag_key"),
        Index("ix_node_tags_operator_tenant_id", "tenant_id"),
        {"extend_existing": True},
    )

    # Relationships
    node = relationship("Node", back_populates="tags_operator")


# =============================================================================
# Security & Trust Tables
# =============================================================================


class SpiffeTrustEntry(Base):
    """SPIFFE trust fabric for workload identity and mTLS."""

    __tablename__ = "spiffe_trust_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    spiffe_id = Column(String(512), nullable=False, unique=True)
    workload_class = Column(String(32), nullable=False)
    trust_domain = Column(String(255), nullable=False)
    parent_spiffe_id = Column(String(512), nullable=True)
    selectors = Column(JSON, nullable=True)
    ttl_seconds = Column(Integer, nullable=False, server_default='3600')
    active = Column(Boolean, nullable=False, server_default='true')
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_spiffe_trust_entries_workload_class", "workload_class"),
        Index("ix_spiffe_trust_entries_trust_domain", "trust_domain"),
        Index("ix_spiffe_trust_entries_active", "active"),
        {"extend_existing": True},
    )


class JoinerSecret(Base):
    """Encrypted joiner secrets for cross-biome initialization and data sharing."""

    __tablename__ = "joiner_secrets"

    id = Column(UUID(), primary_key=True)
    cluster_id = Column(UUID(), nullable=False)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    biome_kind = Column(String(64), nullable=False)
    egg_kind = synonym("biome_kind")
    emitter_biome_id = Column(Integer, ForeignKey("biomes.id", ondelete="RESTRICT"), nullable=False)
    emitter_node_id = Column(Integer, ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    extractor_name = Column(String(255), nullable=False)
    scope = Column(String(16), nullable=False)
    ciphertext = Column(LargeBinary, nullable=False)
    iv = Column(LargeBinary(length=12), nullable=False)
    auth_tag = Column(LargeBinary(length=16), nullable=False)
    dek_wrapped = Column(LargeBinary, nullable=False)
    vault_kek_name = Column(String(255), nullable=False)
    ttl_seconds = Column(Integer, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    rotation_class = Column(String(64), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    rotated_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    audit_event_id = Column(UUID(), ForeignKey("audit_events.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        # postgresql_where is honored (partial index) on Postgres and ignored
        # (falls back to a full index) on every other dialect -- matches the
        # migration this table was originally created by.
        Index(
            "ix_joiner_secrets_cluster_egg_extractor",
            "cluster_id", "biome_kind", "extractor_name",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_joiner_secrets_expires_at",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_joiner_secrets_tenant_id", "tenant_id"),
        {"extend_existing": True},
    )


# =============================================================================
# Audit & Observability Tables
# =============================================================================


class AuditEvent(Base):
    """Audit log with hash-chain integrity for compliance."""

    __tablename__ = "audit_events"

    id = Column(UUID(), primary_key=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    cluster_id = Column(String(255), nullable=False)
    tenant_id = Column(String(255), nullable=True)
    actor_sub = Column(String(512), nullable=False)
    actor_scope = Column(JSON, nullable=True)
    action = Column(String(255), nullable=False)
    resource_kind = Column(String(64), nullable=False)
    resource_id = Column(String(255), nullable=True)
    before_json = Column(JSON, nullable=True)
    after_json = Column(JSON, nullable=True)
    request_id = Column(String(255), nullable=True)
    source_ip = Column(String(45), nullable=True)
    user_agent = Column(String(512), nullable=True)
    prev_hash = Column(LargeBinary(length=32), nullable=False)
    hash = Column(LargeBinary(length=32), nullable=False)
    signature = Column(LargeBinary, nullable=True)

    __table_args__ = (
        Index("ix_audit_events_ts", "ts"),
        Index("ix_audit_events_cluster_id_ts", "cluster_id", "ts"),
        Index("ix_audit_events_tenant_id_ts", "tenant_id", "ts"),
        Index("ix_audit_events_actor_sub", "actor_sub"),
        Index("ix_audit_events_action", "action"),
        Index("ix_audit_events_request_id", "request_id"),
        {"extend_existing": True},
    )


class MigrationEvent(Base):
    """Biome migration tracking with safety checks and outcomes."""

    __tablename__ = "migration_events"

    id = Column(UUID(), primary_key=True)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    biome_instance_id = Column(Integer, ForeignKey("node_egg_assignments.id", ondelete="SET NULL"), nullable=True)
    biome_id = Column(Integer, nullable=True)
    biome_kind = Column(String(64), nullable=True)
    src_node_id = Column(Integer, ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    dst_node_id = Column(Integer, ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    reason = Column(String(512), nullable=True)
    result = Column(String(32), nullable=False)
    rejection_reason = Column(String(255), nullable=True)
    safety_check_details_json = Column(JSON, nullable=True)
    started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_migration_events_biome_instance_id", "biome_instance_id"),
        Index("ix_migration_events_src_node_id", "src_node_id"),
        Index("ix_migration_events_dst_node_id", "dst_node_id"),
        Index("ix_migration_events_tenant_id", "tenant_id"),
        Index("ix_migration_events_started_at", "started_at"),
        Index("ix_migration_events_result", "result"),
        {"extend_existing": True},
    )


class MigrationPolicy(Base):
    """Cluster-level migration policy for automated workload balancing."""

    __tablename__ = "migration_policy"

    id = Column(UUID(), primary_key=True)
    cluster_id = Column(UUID(), nullable=False, unique=True)
    enabled = Column(Boolean, nullable=False, server_default='false')
    evaluation_interval_seconds = Column(Integer, nullable=False, server_default='300')
    min_healthy_nodes = Column(Integer, nullable=False, server_default='3')
    max_concurrent_migrations = Column(Integer, nullable=False, server_default='1')
    require_target_capacity_headroom_cpu_pct = Column(Integer, nullable=False, server_default='20')
    require_target_capacity_headroom_mem_pct = Column(Integer, nullable=False, server_default='20')
    require_target_capacity_headroom_disk_pct = Column(Integer, nullable=False, server_default='15')
    rollback_on_destination_failure = Column(Boolean, nullable=False, server_default='true')
    rollback_window_seconds = Column(Integer, nullable=False, server_default='300')
    forbid_migration_during_partition = Column(Boolean, nullable=False, server_default='true')
    forbid_migration_during_maintenance = Column(Boolean, nullable=False, server_default='true')
    waddleai_risk_threshold = Column(Float, nullable=False, server_default='0.75')
    capacity_forecast_horizon_days = Column(Integer, nullable=False, server_default='7')
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_migration_policy_cluster_id", "cluster_id"),
        {"extend_existing": True},
    )


class LeaderLease(Base):
    """Distributed leader election lease for HA control plane."""

    __tablename__ = "leader_leases"

    lease_name = Column(String(255), primary_key=True)
    holder_id = Column(String(255), nullable=True)
    acquired_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    version = Column(Integer, nullable=False, server_default='0')

    __table_args__ = (
        Index("ix_leader_leases_expires_at", "expires_at"),
        {"extend_existing": True},
    )


class DrDrill(Base):
    """Disaster recovery drill tracking with RTO/RPO measurement."""

    __tablename__ = "dr_drills"

    id = Column(UUID(), primary_key=True)
    cluster_id = Column(UUID(), nullable=False)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(16), nullable=False)
    target = Column(String(64), nullable=False, server_default="staging-clone")
    rpo_observed_seconds = Column(Integer, nullable=True)
    rto_observed_seconds = Column(Integer, nullable=True)
    error_message = Column(String(2048), nullable=True)
    audit_ref = Column(UUID(), ForeignKey("audit_events.id", ondelete="SET NULL"), nullable=True)
    triggered_by = Column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_dr_drills_cluster_started", "cluster_id", "started_at"),
        Index("ix_dr_drills_status", "status"),
        Index("ix_dr_drills_tenant", "tenant_id"),
        {"extend_existing": True},
    )


class SloDefinition(Base):
    """Service-level objective definitions for SLO tracking and alerting."""

    __tablename__ = "slo_definitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cluster_id = Column(UUID(), nullable=False)
    tenant_id = Column(String(255), nullable=True)
    slo_name = Column(String(128), nullable=False)
    domain = Column(String(32), nullable=False)
    target_value = Column(Float, nullable=False)
    target_unit = Column(String(16), nullable=False)
    window_seconds = Column(Integer, nullable=False, server_default='2592000')
    error_budget_seconds = Column(Integer, nullable=True)
    alert_threshold_burn_rate = Column(Float, nullable=False, server_default='2.0')
    runbook_url = Column(String(1024), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("cluster_id", "tenant_id", "slo_name", name="uq_slo_cluster_tenant_name"),
        Index("ix_slo_cluster", "cluster_id"),
        Index("ix_slo_name", "slo_name"),
        {"extend_existing": True},
    )


# =============================================================================
# Node Progress Events Table
# =============================================================================


class NodeEvent(Base):
    """Structured progress event from discovery agent or cloud-init runcmd.

    Written by ``POST /api/v1/nodes/{id}/events`` and republished to the
    NATS subject ``gough.node.{id}.events``.  The ``sequence_id`` field is
    set by the agent to detect replay / gaps on tunnel resume.
    """

    __tablename__ = "node_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    ts = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    stage = Column(String(255), nullable=False)
    message = Column(String(4096), nullable=False)
    progress_pct = Column(Integer, nullable=True)
    sequence_id = Column(BigInteger, nullable=True)
    raw_json = Column(JSON, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_node_events_node_id", "node_id"),
        Index("ix_node_events_tenant_id", "tenant_id"),
        Index("ix_node_events_ts", "ts"),
        Index("ix_node_events_stage", "stage"),
        {"extend_existing": True},
    )


# =============================================================================
# Webhook Endpoints Table
# =============================================================================


class WebhookEndpoint(Base):
    """Tenant-registered webhook subscriber endpoint.

    Read/written via raw SQL in ``app/api/webhooks.py`` and
    ``app/workers/webhook_dispatcher.py``; this class registers the table on
    ``Base.metadata`` for Alembic autogenerate/``create_all`` parity, matching
    every other M1 table. HMAC/asymmetric signing material is never stored
    here -- it lives in Vault only (``WebhookKeyManager``).
    """

    __tablename__ = "webhook_endpoints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    url = Column(String(2048), nullable=False)
    signing_mode = Column(String(32), nullable=False, server_default="ed25519")
    active = Column(Boolean, nullable=False, server_default="true")
    event_filter = Column(JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True)
    retry_policy = Column(JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_webhook_endpoints_tenant_id", "tenant_id"),
        Index("ix_webhook_endpoints_active", "active"),
        {"extend_existing": True},
    )


# =============================================================================
# Orphan Table Schemas (gh-21)
# =============================================================================
# The following twelve tables are queried at runtime throughout app/api/ (and
# app/permissions.py) but had no SQLAlchemy model or migration anywhere in
# the codebase -- see .superpowers/sdd/followups/orphan-schemas-brief.md for
# the file:line usage recon each profile below is derived from. Declaring
# them here registers them on the shared Base the same way every other M1
# table is, so Base.metadata.create_all() (driven by the baseline migration)
# creates them on a fresh database. Grants + RLS enablement live in the
# baseline migration itself (SQLAlchemy metadata can't express GRANT/RLS).
#
# created_at/updated_at use ``server_default=func.now()`` (a genuine
# database-side DEFAULT) rather than this file's more common Python-side
# ``default=lambda: datetime.now(timezone.utc)`` pattern, for several of
# these tables specifically because their INSERT call sites never pass
# those columns (verified: ``app.api.ipxe.create_image``,
# ``create_boot_config``, ``update_ipxe_config``'s create branch,
# ``app.api.biomes.create_biome_group``) -- a Python-side-only default is
# invisible to penguin-dal's reflected-table insert (the exact class of bug
# already fixed for ``boot_events`` in the approved brief, and documented in
# ``tests/test_rls_isolation.py``'s node/node_events seed-helper
# docstrings). Applied uniformly across all twelve tables' created_at/
# updated_at columns here rather than table-by-table, since "does today's
# caller happen to pass it" is a fragile thing to keep in sync by hand.


class Cluster(Base):
    """Cluster registry -- the tenant-ownership anchor for every
    ``/api/v1/clusters/<cluster_id>/*`` route.

    SECURITY-CRITICAL (gh-21): missing this table was a fail-open
    tenant-IDOR gate. ``app.api.clusters._require_cluster_tenant`` guards
    every cluster-scoped route with ``if hasattr(db, "clusters"): ...`` --
    with no ``clusters`` table at all, that check was always False, so the
    tenant-ownership lookup was skipped entirely and cross-tenant requests
    for someone else's cluster were never rejected. Creating this table
    makes ``hasattr(db, "clusters")`` True, which is what turns the guard
    on.
    """

    __tablename__ = "clusters"

    id = Column(String(255), primary_key=True)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, server_default="ready")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_clusters_tenant_id", "tenant_id"),
        {"extend_existing": True},
    )


class ClusterConfig(Base):
    """Schemaless key/value config document store scoped to a single cluster.

    Backs the network-pools / baseline-topology / identity-plane / generic
    config documents surfaced under ``/api/v1/clusters/<cluster_id>/*``
    (``app.api.clusters._load_cluster_doc`` / ``_save_cluster_doc``)
    without proliferating a dedicated table per document type. Cluster-
    scoped, not tenant-scoped -- no RLS, same as ``migration_policy``.
    """

    __tablename__ = "cluster_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cluster_id = Column(String(255), ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False)
    key = Column(String(128), nullable=False)
    value_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("cluster_id", "key", name="uq_cluster_config_cluster_key"),
        Index("ix_cluster_config_cluster_id", "cluster_id"),
        {"extend_existing": True},
    )


class StorageQuota(Base):
    """Per-tenant storage resource quota (limit/used) -- RLS-enforced.

    SECURITY (gh-21): ``app.api.storage.list_storage_quotas`` filters by a
    USER-SUPPLIED ``tenant_id`` query parameter with no cross-check against
    the caller's own tenant -- RLS on this table is the actual enforcement
    boundary, not the app-level filter.
    """

    __tablename__ = "storage_quotas"

    id = Column(UUID(), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    resource_type = Column(String(64), nullable=False)
    limit_value = Column(Numeric, nullable=True)
    used_value = Column(Numeric, nullable=True)
    unit = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "resource_type", name="uq_storage_quotas_tenant_resource"),
        Index("ix_storage_quotas_tenant_id", "tenant_id"),
        Index("ix_storage_quotas_created_at", "created_at"),
        {"extend_existing": True},
    )


class StorageQuotaRequest(Base):
    """Tenant-submitted storage quota increase request (INSERT-only from the API).

    SECURITY (gh-21): ``tenant_id`` comes directly from the request body --
    RLS WITH CHECK (the generic ``tenant_isolation`` policy's USING clause
    doubles as WITH CHECK when no separate WITH CHECK is specified) is the
    actual enforcement that a caller can't write a request under a
    tenant_id other than their own token's tenant; the app layer does not
    cross-check it today.
    """

    __tablename__ = "storage_quota_requests"

    id = Column(UUID(), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    resource_type = Column(String(64), nullable=False)
    requested_value = Column(Numeric, nullable=False)
    unit = Column(String(16), nullable=False)
    justification = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, server_default="pending")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_storage_quota_requests_tenant_id", "tenant_id"),
        Index("ix_storage_quota_requests_status", "status"),
        {"extend_existing": True},
    )


class DeploymentLog(Base):
    """Append-only per-deployment log stream (``GET /deployments/<id>/logs``).

    SELECT-only in current code (``app.api.biomes.get_deployment_logs``); no
    writer exists yet -- INSERT is granted for a future writer, matching the
    approved profile.
    """

    __tablename__ = "deployment_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    deployment_id = Column(String(64), ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False)
    message = Column(Text, nullable=False)
    level = Column(String(16), nullable=False, server_default="info")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_deployment_logs_deployment_id", "deployment_id"),
        Index("ix_deployment_logs_created_at", "created_at"),
        {"extend_existing": True},
    )


class ResourcePermission(Base):
    """Per-user, per-resource permission grant (comma-separated permission list).

    Backs ``app.permissions.check_resource_permission`` -- ``permission`` is
    intentionally a plain comma-separated String, not a JSON/array column:
    the reader does ``permission in (perms.permission or "").split(",")``.
    """

    __tablename__ = "resource_permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("auth_user.id", ondelete="CASCADE"), nullable=False)
    resource_type = Column(String(64), nullable=False)
    resource_id = Column(Integer, nullable=False)
    permission = Column(String(255), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "resource_type", "resource_id",
            name="uq_resource_permissions_user_type_id",
        ),
        Index("ix_resource_permissions_user_id", "user_id"),
        {"extend_existing": True},
    )


class BiomeGroup(Base):
    """Named, ordered collection of biomes assigned to an iPXE boot config.

    ``tenant_id`` is a NEW column (approved -- biomes is tenant-scoped);
    current handlers never set it on create, so every row lands on the
    ``server_default`` ('__default__') until a follow-up threads tenant
    through ``app.api.biomes``'s group handlers -- with that default, RLS
    still makes every row visible to the default tenant in the meantime,
    which is the accepted interim behavior per the approved profile.
    """

    __tablename__ = "biome_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(255), nullable=False, server_default="__default__")
    name = Column(String(255), nullable=False, unique=True)
    display_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    biomes = Column(JSON, nullable=False)
    is_default = Column(Boolean, nullable=False, server_default='false')
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_biome_groups_tenant_id", "tenant_id"),
        {"extend_existing": True},
    )


class IpxeMachine(Base):
    """Legacy MAAS-style bare-metal machine inventory for iPXE provisioning.

    Distinct from ``nodes`` (the canonical M1 inventory table) --
    ``app.api.ipxe._find_node_or_machine_by_mac`` checks ``nodes`` first and
    falls back to this table, so both must exist for MAC resolution to keep
    working across the transition period. No code path inserts new rows
    today (status/assignment updates only) -- INSERT is granted for
    out-of-band/future writer parity, matching the approved profile.
    """

    __tablename__ = "ipxe_machines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    system_id = Column(String(255), nullable=False, unique=True)
    mac_address = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)
    zone = Column(String(255), nullable=True)
    pool = Column(String(255), nullable=True)
    boot_config_id = Column(Integer, ForeignKey("ipxe_boot_configs.id", ondelete="SET NULL"), nullable=True)
    assigned_biomes = Column(JSON, nullable=True)
    dmi_uuid = Column(String(64), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    deployed_at = Column(DateTime(timezone=True), nullable=True)
    elder_synced_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_ipxe_machines_mac_address", "mac_address"),
        Index("ix_ipxe_machines_status", "status"),
        Index("ix_ipxe_machines_boot_config_id", "boot_config_id"),
        {"extend_existing": True},
    )


class IpxeImage(Base):
    """Boot image catalog (kernel/initrd/squashfs) for iPXE deployment."""

    __tablename__ = "ipxe_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    display_name = Column(String(255), nullable=False)
    os_name = Column(String(64), nullable=False, server_default="ubuntu")
    os_version = Column(String(64), nullable=False)
    architecture = Column(String(32), nullable=False)
    kernel_path = Column(String(1024), nullable=False)
    initrd_path = Column(String(1024), nullable=False)
    squashfs_path = Column(String(1024), nullable=True)
    kernel_params = Column(Text, nullable=True)
    image_type = Column(String(32), nullable=False, server_default="minimal")
    minio_bucket = Column(String(255), nullable=True)
    checksum = Column(String(255), nullable=True)
    is_default = Column(Boolean, nullable=False, server_default='false')
    is_active = Column(Boolean, nullable=False, server_default='true')
    size_bytes = Column(BigInteger, nullable=False, server_default='0')
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_ipxe_images_architecture", "architecture"),
        {"extend_existing": True},
    )


class IpxeBootConfig(Base):
    """Named iPXE boot configuration (script, boot order, default image/biome-group)."""

    __tablename__ = "ipxe_boot_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    ipxe_script = Column(Text, nullable=True)
    kernel_params = Column(Text, nullable=True)
    boot_order = Column(JSON, nullable=False)
    timeout_seconds = Column(Integer, nullable=False, server_default='30')
    default_image_id = Column(Integer, ForeignKey("ipxe_images.id", ondelete="SET NULL"), nullable=True)
    assigned_biome_group_id = Column(
        Integer, ForeignKey("biome_groups.id", ondelete="SET NULL"), nullable=True
    )
    is_default = Column(Boolean, nullable=False, server_default='false')
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_ipxe_boot_configs_default_image_id", "default_image_id"),
        Index("ix_ipxe_boot_configs_assigned_biome_group_id", "assigned_biome_group_id"),
        {"extend_existing": True},
    )


class IpxeConfig(Base):
    """iPXE/DHCP/TFTP boot service configuration profile."""

    __tablename__ = "ipxe_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    dhcp_mode = Column(String(32), nullable=False, server_default="proxy")
    dhcp_interface = Column(String(255), nullable=True)
    dhcp_subnet = Column(String(64), nullable=True)
    dhcp_range_start = Column(String(64), nullable=True)
    dhcp_range_end = Column(String(64), nullable=True)
    dhcp_gateway = Column(String(64), nullable=True)
    dns_servers = Column(JSON, nullable=True)
    tftp_enabled = Column(Boolean, nullable=False, server_default='true')
    http_boot_url = Column(String(1024), nullable=True)
    minio_bucket = Column(String(255), nullable=True)
    chain_url = Column(String(1024), nullable=True)
    default_boot_script = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default='true')
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_ipxe_config_is_active", "is_active"),
        {"extend_existing": True},
    )


class BootEvent(Base):
    """Append-only iPXE/PXE boot event log (discovery, TFTP, boot-start, deploy-complete).

    INSERT-only from ``app.api.ipxe._log_boot_event``. ``machine_id`` is
    nullable -- discovery events fire before a machine is registered.
    """

    __tablename__ = "boot_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    machine_id = Column(Integer, ForeignKey("ipxe_machines.id", ondelete="SET NULL"), nullable=True)
    mac_address = Column(String(32), nullable=False)
    ip_address = Column(String(64), nullable=True)
    event_type = Column(String(32), nullable=False)
    details = Column(JSON, nullable=False)
    status = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_boot_events_machine_id", "machine_id"),
        Index("ix_boot_events_mac_address", "mac_address"),
        Index("ix_boot_events_event_type", "event_type"),
        Index("ix_boot_events_created_at", "created_at"),
        {"extend_existing": True},
    )


# =============================================================================
# Backward-compat aliases
# =============================================================================

NodeEggAssignment = NodeBiomeAssignment
