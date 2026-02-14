"""SQLAlchemy Database Models for Gough Hypervisor Orchestration Platform.

This module defines all database tables using SQLAlchemy for schema creation
and migrations. PyDAL is used for runtime operations as required by CLAUDE.md.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


# =============================================================================
# Flask-Security-Too Authentication Tables
# =============================================================================

class AuthRole(Base):
    """Roles table for RBAC."""

    __tablename__ = "auth_role"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), unique=True, nullable=False)
    description = Column(String(255))
    permissions = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    users = relationship("AuthUserRole", back_populates="role")


class AuthUser(Base):
    """Users table compatible with Flask-Security-Too."""

    __tablename__ = "auth_user"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False)
    password = Column(String(255))
    active = Column(Boolean, default=True)
    fs_uniquifier = Column(String(64), unique=True, nullable=False)
    confirmed_at = Column(DateTime)
    last_login_at = Column(DateTime)
    current_login_at = Column(DateTime)
    last_login_ip = Column(String(64))
    current_login_ip = Column(String(64))
    login_count = Column(Integer, default=0)
    tf_totp_secret = Column(String(255))
    tf_primary_method = Column(String(64))
    full_name = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    roles = relationship("AuthUserRole", back_populates="user")
    refresh_tokens = relationship("AuthRefreshToken", back_populates="user")
    password_resets = relationship("AuthPasswordReset", back_populates="user")


class AuthUserRole(Base):
    """User-Role association table."""

    __tablename__ = "auth_user_roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("auth_user.id"), nullable=False)
    role_id = Column(Integer, ForeignKey("auth_role.id"), nullable=False)

    # Relationships
    user = relationship("AuthUser", back_populates="roles")
    role = relationship("AuthRole", back_populates="users")


class AuthRefreshToken(Base):
    """Refresh tokens for JWT compatibility."""

    __tablename__ = "auth_refresh_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("auth_user.id"), nullable=False)
    token_hash = Column(String(255), unique=True)
    expires_at = Column(DateTime)
    revoked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("AuthUser", back_populates="refresh_tokens")


class AuthPasswordReset(Base):
    """Password reset tokens."""

    __tablename__ = "auth_password_resets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("auth_user.id"), nullable=False)
    token_hash = Column(String(255), unique=True)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("AuthUser", back_populates="password_resets")


# =============================================================================
# Secrets Management Tables
# =============================================================================

class SecretsConfig(Base):
    """Secrets backend configuration."""

    __tablename__ = "secrets_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    backend_type = Column(String(50), nullable=False)
    is_default = Column(Boolean, default=False)
    config_data = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EncryptedSecret(Base):
    """Encrypted secrets storage (for encrypted_db backend)."""

    __tablename__ = "encrypted_secrets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    path = Column(String(500), unique=True, nullable=False)
    encrypted_data = Column(Text, nullable=False)
    created_by = Column(Integer, ForeignKey("auth_user.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =============================================================================
# Storage Configuration Tables
# =============================================================================

class StorageConfig(Base):
    """S3-compatible storage configurations."""

    __tablename__ = "storage_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    provider_type = Column(String(50), nullable=False)
    endpoint_url = Column(String(500))
    region = Column(String(100))
    bucket_name = Column(String(255))
    credentials_path = Column(String(500))
    is_default = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    use_ssl = Column(Boolean, default=True)
    config_data = Column(Text)
    created_by = Column(Integer, ForeignKey("auth_user.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =============================================================================
# Cloud Provider Tables
# =============================================================================

class CloudProvider(Base):
    """Cloud provider configurations."""

    __tablename__ = "cloud_providers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    provider_type = Column(String(50), nullable=False)
    description = Column(Text)
    region = Column(String(100))
    credentials_path = Column(String(500))
    config_data = Column(Text)
    is_active = Column(Boolean, default=True)
    last_sync_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    machines = relationship("CloudMachine", back_populates="provider")


class CloudMachine(Base):
    """Unified machine inventory across all cloud providers."""

    __tablename__ = "cloud_machines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("cloud_providers.id"), nullable=False)
    external_id = Column(String(255), nullable=False)
    hostname = Column(String(255))
    ip_address = Column(String(64))
    private_ip = Column(String(64))
    status = Column(String(50), default="new")
    machine_type = Column(String(100))
    architecture = Column(String(50), default="amd64")
    cpu_count = Column(Integer)
    memory_mb = Column(Integer)
    storage_gb = Column(Integer)
    os_image = Column(String(255))
    zone = Column(String(100))
    tags = Column(Text)
    metadata_json = Column('metadata', Text)
    lxd_cluster_id = Column(Integer, ForeignKey("lxd_clusters.id"))
    fleet_host_id = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    provider = relationship("CloudProvider", back_populates="machines")
    lxd_cluster = relationship("LXDCluster", back_populates="machines")


# =============================================================================
# MaaS Integration Tables
# =============================================================================

class MaaSConfig(Base):
    """MaaS Configuration."""

    __tablename__ = "maas_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    maas_url = Column(String(500), nullable=False)
    api_key = Column(String(500), nullable=False)
    username = Column(String(100), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Server(Base):
    """Legacy servers table (for MaaS bare metal inventory)."""

    __tablename__ = "servers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hostname = Column(String(255), unique=True, nullable=False)
    maas_system_id = Column(String(100), unique=True)
    mac_address = Column(String(17), nullable=False)
    ip_address = Column(String(64))
    status = Column(String(50), default="New")
    architecture = Column(String(50), default="amd64")
    memory_mb = Column(Integer)
    cpu_count = Column(Integer)
    storage_gb = Column(Integer)
    power_type = Column(String(50))
    power_parameters = Column(Text)
    zone = Column(String(100), default="default")
    pool = Column(String(100), default="default")
    tags = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =============================================================================
# LXD Cluster Tables
# =============================================================================

class LXDCluster(Base):
    """LXD Clusters."""

    __tablename__ = "lxd_clusters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)
    cluster_key_path = Column(String(500))
    admin_cert_path = Column(String(500))
    api_endpoint = Column(String(500))
    api_port = Column(Integer, default=8443)
    status = Column(String(50), default="initializing")
    member_count = Column(Integer, default=0)
    storage_pool = Column(String(100), default="default")
    network_bridge = Column(String(100), default="lxdbr0")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    machines = relationship("CloudMachine", back_populates="lxd_cluster")
    members = relationship("LXDClusterMember", back_populates="cluster")


class LXDClusterMember(Base):
    """LXD Cluster Members."""

    __tablename__ = "lxd_cluster_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cluster_id = Column(Integer, ForeignKey("lxd_clusters.id"), nullable=False)
    machine_id = Column(Integer, ForeignKey("cloud_machines.id"))
    member_name = Column(String(255), nullable=False)
    api_url = Column(String(500))
    status = Column(String(50), default="pending")
    is_leader = Column(Boolean, default=False)
    joined_at = Column(DateTime)
    last_seen_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    cluster = relationship("LXDCluster", back_populates="members")


# =============================================================================
# Cloud-Init Templates
# =============================================================================

class CloudInitTemplate(Base):
    """Cloud-init templates."""

    __tablename__ = "cloud_init_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)
    template_content = Column(Text, nullable=False)
    template_type = Column(String(50), default="user-data")
    is_default = Column(Boolean, default=False)
    version = Column(String(20), default="1.0")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PackageConfig(Base):
    """Package configurations."""

    __tablename__ = "package_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)
    packages = Column(Text, nullable=False)
    repositories = Column(Text)
    pre_install_scripts = Column(Text)
    post_install_scripts = Column(Text)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =============================================================================
# Deployment Jobs
# =============================================================================

class DeploymentJob(Base):
    """Deployment jobs."""

    __tablename__ = "deployment_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), unique=True, nullable=False)
    machine_id = Column(Integer, ForeignKey("cloud_machines.id"))
    server_id = Column(Integer, ForeignKey("servers.id"))
    cloud_init_template_id = Column(Integer, ForeignKey("cloud_init_templates.id"))
    package_config_id = Column(Integer, ForeignKey("package_configs.id"))
    lxd_cluster_id = Column(Integer, ForeignKey("lxd_clusters.id"))
    status = Column(String(50), default="pending")
    job_type = Column(String(50), default="provision")
    ansible_playbook = Column(String(255))
    log_output = Column(Text)
    error_message = Column(Text)
    progress_percent = Column(Integer, default=0)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_by = Column(Integer, ForeignKey("auth_user.id"))
    created_at = Column(DateTime, default=datetime.utcnow)


# =============================================================================
# FleetDM Integration Tables
# =============================================================================

class FleetDMConfig(Base):
    """FleetDM Configuration."""

    __tablename__ = "fleetdm_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    fleet_url = Column(String(500), nullable=False)
    api_token_path = Column(String(500))
    is_active = Column(Boolean, default=True)
    osquery_version = Column(String(20), default="5.10.2")
    enroll_secret_path = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FleetHost(Base):
    """FleetDM Hosts."""

    __tablename__ = "fleet_hosts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fleet_host_id = Column(Integer, unique=True, nullable=False)
    hostname = Column(String(255), nullable=False)
    uuid = Column(String(64), unique=True)
    machine_id = Column(Integer, ForeignKey("cloud_machines.id"))
    server_id = Column(Integer, ForeignKey("servers.id"))
    last_seen_at = Column(DateTime)
    status = Column(String(50), default="offline")
    platform = Column(String(50))
    osquery_version = Column(String(20))
    enrolled_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FleetQuery(Base):
    """FleetDM Queries."""

    __tablename__ = "fleet_queries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)
    query = Column(Text, nullable=False)
    fleet_query_id = Column(Integer)
    category = Column(String(50), default="custom")
    is_scheduled = Column(Boolean, default=False)
    interval_seconds = Column(Integer, default=3600)
    created_by = Column(Integer, ForeignKey("auth_user.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class QueryExecution(Base):
    """Query Executions."""

    __tablename__ = "query_executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    query_id = Column(Integer, ForeignKey("fleet_queries.id"))
    campaign_id = Column(Integer)
    executed_by = Column(Integer, ForeignKey("auth_user.id"))
    target_hosts = Column(Text)
    status = Column(String(50), default="pending")
    results_count = Column(Integer, default=0)
    execution_time_ms = Column(Integer)
    error_message = Column(Text)
    executed_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)


class FleetAlert(Base):
    """FleetDM Alerts."""

    __tablename__ = "fleet_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)
    query_id = Column(Integer, ForeignKey("fleet_queries.id"))
    alert_condition = Column(String(100), nullable=False)
    condition_parameters = Column(Text)
    notification_channels = Column(Text)
    is_active = Column(Boolean, default=True)
    severity = Column(String(20), default="medium")
    cooldown_minutes = Column(Integer, default=60)
    last_triggered_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AlertHistory(Base):
    """Alert History."""

    __tablename__ = "alert_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_id = Column(Integer, ForeignKey("fleet_alerts.id"))
    triggered_by = Column(String(100))
    trigger_data = Column(Text)
    notification_sent = Column(Boolean, default=False)
    notification_channels_used = Column(Text)
    resolved = Column(Boolean, default=False)
    resolved_by = Column(Integer, ForeignKey("auth_user.id"))
    resolution_notes = Column(Text)
    triggered_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime)


class OSQueryResult(Base):
    """OSQuery Results Cache."""

    __tablename__ = "osquery_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    host_id = Column(Integer, ForeignKey("fleet_hosts.id"))
    query_name = Column(String(100), nullable=False)
    result_data = Column(Text)
    result_hash = Column(String(64))
    execution_time = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


# =============================================================================
# Elder Integration Tables
# =============================================================================

class ElderConfig(Base):
    """Elder Configuration."""

    __tablename__ = "elder_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    elder_url = Column(String(500), nullable=False)
    api_key = Column(String(500), nullable=False)
    timeout = Column(Integer, default=10)
    max_retries = Column(Integer, default=3)
    is_active = Column(Boolean, default=True)
    last_sync_at = Column(DateTime)
    last_error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ElderHost(Base):
    """Elder Host Registrations."""

    __tablename__ = "elder_hosts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hostname = Column(String(255), unique=True, nullable=False)
    ip_address = Column(String(64), nullable=False)
    fqdn = Column(String(255), nullable=False)
    machine_id = Column(Integer, ForeignKey("cloud_machines.id"))
    server_id = Column(Integer, ForeignKey("servers.id"))
    apps = Column(Text)
    zone = Column(String(100), default="default")
    pool = Column(String(100), default="default")
    tags = Column(Text)
    metadata_json = Column('metadata', Text)
    elder_registered = Column(Boolean, default=False)
    last_sync_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ElderApp(Base):
    """Elder Application Registrations."""

    __tablename__ = "elder_apps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    app_name = Column(String(255), unique=True, nullable=False)
    hosts = Column(Text, nullable=False)
    port = Column(Integer, nullable=False)
    protocol = Column(String(20), default="http")
    path = Column(String(255), default="/")
    health_check_url = Column(String(500))
    priority = Column(Integer, default=100)
    elder_registered = Column(Boolean, default=False)
    last_sync_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =============================================================================
# Resource Teams & Shell Access Tables
# =============================================================================

class ResourceTeam(Base):
    """Resource Teams."""

    __tablename__ = "resource_teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text)
    created_by = Column(Integer, ForeignKey("auth_user.id"), nullable=False)
    is_active = Column(Boolean, default=True)
    metadata_json = Column('metadata', Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    members = relationship("TeamMember", back_populates="team")


class TeamMember(Base):
    """Team Members."""

    __tablename__ = "team_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("resource_teams.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("auth_user.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(50), nullable=False)
    added_by = Column(Integer, ForeignKey("auth_user.id"), nullable=False)
    added_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime)

    # Relationships
    team = relationship("ResourceTeam", back_populates="members")


class ResourceAssignment(Base):
    """Resource Assignments."""

    __tablename__ = "resource_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("resource_teams.id"), nullable=False)
    resource_type = Column(String(100), nullable=False)
    resource_id = Column(String(255), nullable=False)
    permissions = Column(Text, nullable=False)
    assigned_by = Column(Integer, ForeignKey("auth_user.id"), nullable=False)
    assigned_at = Column(DateTime, default=datetime.utcnow)


class AccessAgent(Base):
    """Access Agents."""

    __tablename__ = "access_agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(String(255), unique=True, nullable=False)
    hostname = Column(String(255), nullable=False)
    ip_address = Column(String(64))
    enrollment_key_hash = Column(String(255))
    enrollment_completed = Column(Boolean, default=False)
    jwt_token_id = Column(Integer, ForeignKey("auth_refresh_tokens.id"))
    last_heartbeat = Column(DateTime)
    status = Column(String(50), default="pending", nullable=False)
    capabilities = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EnrollmentKey(Base):
    """Enrollment Keys."""

    __tablename__ = "enrollment_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_hash = Column(String(255), unique=True, nullable=False)
    created_by = Column(Integer, ForeignKey("auth_user.id"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    is_used = Column(Boolean, default=False)
    used_by_agent = Column(Integer, ForeignKey("access_agents.id"))
    metadata_json = Column('metadata', Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class SSHCAConfig(Base):
    """SSH CA Configuration."""

    __tablename__ = "ssh_ca_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ca_name = Column(String(255), unique=True, nullable=False)
    ca_type = Column(String(50), nullable=False)
    public_key = Column(Text, nullable=False)
    private_key_vault_path = Column(String(500))
    cert_validity_seconds = Column(Integer, default=3600)
    max_validity_seconds = Column(Integer)
    principals_allowed = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ShellSession(Base):
    """Shell Sessions."""

    __tablename__ = "shell_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(255), unique=True, nullable=False)
    user_id = Column(Integer, ForeignKey("auth_user.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("resource_teams.id"))
    resource_type = Column(String(100), nullable=False)
    resource_id = Column(String(255), nullable=False)
    agent_id = Column(Integer, ForeignKey("access_agents.id"), nullable=False)
    session_type = Column(String(50), nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime)
    client_ip = Column(String(64))
    audit_log_path = Column(String(500))


# =============================================================================
# System Logs
# =============================================================================

class SystemLog(Base):
    """System logs."""

    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(20), nullable=False)
    component = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    details = Column(Text)
    machine_id = Column(Integer, ForeignKey("cloud_machines.id"))
    server_id = Column(Integer, ForeignKey("servers.id"))
    job_id = Column(Integer, ForeignKey("deployment_jobs.id"))
    user_id = Column(Integer, ForeignKey("auth_user.id"))
    created_at = Column(DateTime, default=datetime.utcnow)


def get_sqlalchemy_engine(db_uri: str):
    """Create SQLAlchemy engine from database URI."""
    # Convert PyDAL URI to SQLAlchemy URI format
    if db_uri.startswith("postgres://"):
        # SQLAlchemy requires postgresql:// not postgres://
        db_uri = db_uri.replace("postgres://", "postgresql://", 1)
    elif db_uri.startswith("sqlite:"):
        # SQLAlchemy uses sqlite:/// for files
        if "memory" not in db_uri:
            db_uri = db_uri.replace("sqlite://", "sqlite:///", 1)

    return create_engine(db_uri, echo=False)


def create_all_tables(db_uri: str):
    """Create all tables using SQLAlchemy and seed default data."""
    from sqlalchemy.orm import sessionmaker
    import uuid
    import bcrypt

    engine = get_sqlalchemy_engine(db_uri)
    Base.metadata.create_all(engine)

    # Create session for seeding default data
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Create default roles if they don't exist
        default_roles = [
            {"name": "admin", "description": "Full system access"},
            {"name": "maintainer", "description": "Read/write access, no user management"},
            {"name": "viewer", "description": "Read-only access"},
        ]

        for role_data in default_roles:
            existing = session.query(AuthRole).filter_by(name=role_data["name"]).first()
            if not existing:
                role = AuthRole(**role_data)
                session.add(role)
                print(f"Created role: {role_data['name']}")

        session.commit()

        # Create default admin user if doesn't exist
        admin_email = "admin@gough.local"
        existing_admin = session.query(AuthUser).filter_by(email=admin_email).first()

        if not existing_admin:
            # Hash default password 'admin'
            password_hash = bcrypt.hashpw("admin".encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

            admin_user = AuthUser(
                email=admin_email,
                password=password_hash,
                full_name="Administrator",
                active=True,
                fs_uniquifier=str(uuid.uuid4()),
            )
            session.add(admin_user)
            session.commit()

            # Assign admin role
            admin_role = session.query(AuthRole).filter_by(name="admin").first()
            if admin_role:
                user_role = AuthUserRole(user_id=admin_user.id, role_id=admin_role.id)
                session.add(user_role)
                session.commit()

            print(f"Created default admin user: {admin_email} with password 'admin'")
            print("IMPORTANT: Change the default admin password immediately!")

        session.close()

    except Exception as e:
        session.rollback()
        session.close()
        print(f"Error seeding default data: {e}")
        raise

    return engine
