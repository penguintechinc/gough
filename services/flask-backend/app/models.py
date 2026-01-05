"""PyDAL Database Models for Gough Hypervisor Orchestration Platform.

This module defines all database tables using PyDAL, consolidating:
- Flask-Security-Too authentication tables
- Legacy py4web management-server tables
- New cloud provider and orchestration tables

All tables use PyDAL for runtime operations as required by CLAUDE.md.
"""

from datetime import datetime

from flask import Flask, g
from pydal import DAL, Field
from pydal.validators import (
    IS_EMAIL,
    IS_IN_SET,
    IS_NOT_IN_DB,
)

from .config import Config

# Valid roles for RBAC
VALID_ROLES = ["admin", "maintainer", "viewer"]

# Cloud provider types
CLOUD_PROVIDER_TYPES = ["maas", "lxd", "aws", "gcp", "azure", "vultr"]

# Secrets backend types
SECRETS_BACKEND_TYPES = ["encrypted_db", "vault", "infisical", "aws", "gcp", "azure"]

# Job status values
JOB_STATUSES = ["pending", "running", "completed", "failed", "cancelled"]

# Machine status values
MACHINE_STATUSES = [
    "new", "commissioning", "ready", "allocated", "deploying",
    "deployed", "releasing", "disk_erasing", "failed", "broken",
    "running", "stopped", "terminated"
]


def init_db(app: Flask) -> DAL:
    """Initialize database connection and define all tables."""
    db_uri = Config.get_db_uri()

    db = DAL(
        db_uri,
        pool_size=Config.DB_POOL_SIZE,
        migrate=True,
        check_reserved=["all"],
        lazy_tables=False,
    )

    # =========================================================================
    # Flask-Security-Too Authentication Tables
    # =========================================================================

    # Roles table (Flask-Security-Too required)
    db.define_table(
        "auth_role",
        Field("name", "string", length=80, unique=True, notnull=True),
        Field("description", "string", length=255),
        Field("permissions", "text"),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # Users table (Flask-Security-Too compatible)
    db.define_table(
        "auth_user",
        Field("email", "string", length=255, unique=True, notnull=True,
              requires=[IS_EMAIL(), IS_NOT_IN_DB(db, "auth_user.email")]),
        Field("password", "password", length=255),
        Field("active", "boolean", default=True),
        Field("fs_uniquifier", "string", length=64, unique=True, notnull=True),
        Field("confirmed_at", "datetime"),
        Field("last_login_at", "datetime"),
        Field("current_login_at", "datetime"),
        Field("last_login_ip", "string", length=64),
        Field("current_login_ip", "string", length=64),
        Field("login_count", "integer", default=0),
        Field("tf_totp_secret", "string", length=255),
        Field("tf_primary_method", "string", length=64),
        Field("full_name", "string", length=255),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # User-Role association table
    db.define_table(
        "auth_user_roles",
        Field("user_id", "reference auth_user", notnull=True),
        Field("role_id", "reference auth_role", notnull=True),
    )

    # Refresh tokens for JWT compatibility
    db.define_table(
        "auth_refresh_tokens",
        Field("user_id", "reference auth_user", notnull=True),
        Field("token_hash", "string", length=255, unique=True),
        Field("expires_at", "datetime"),
        Field("revoked", "boolean", default=False),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # =========================================================================
    # Secrets Management Tables
    # =========================================================================

    # Secrets backend configuration
    db.define_table(
        "secrets_config",
        Field("name", "string", length=100, unique=True, notnull=True),
        Field("backend_type", "string", length=50, notnull=True,
              requires=IS_IN_SET(SECRETS_BACKEND_TYPES)),
        Field("is_default", "boolean", default=False),
        Field("config_data", "text"),  # JSON encrypted configuration
        Field("is_active", "boolean", default=True),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # Encrypted secrets storage (for encrypted_db backend)
    db.define_table(
        "encrypted_secrets",
        Field("path", "string", length=500, unique=True, notnull=True),
        Field("encrypted_data", "text", notnull=True),
        Field("created_by", "reference auth_user"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # Cloud Provider Tables
    # =========================================================================

    # Cloud provider configurations
    db.define_table(
        "cloud_providers",
        Field("name", "string", length=100, unique=True, notnull=True),
        Field("provider_type", "string", length=50, notnull=True,
              requires=IS_IN_SET(CLOUD_PROVIDER_TYPES)),
        Field("description", "text"),
        Field("region", "string", length=100),
        Field("credentials_path", "string", length=500),  # Path in secrets manager
        Field("config_data", "text"),  # JSON additional configuration
        Field("is_active", "boolean", default=True),
        Field("last_sync_at", "datetime"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # Unified machine inventory across all cloud providers
    db.define_table(
        "cloud_machines",
        Field("provider_id", "reference cloud_providers", notnull=True),
        Field("external_id", "string", length=255, notnull=True),
        Field("hostname", "string", length=255),
        Field("ip_address", "string", length=64),
        Field("private_ip", "string", length=64),
        Field("status", "string", length=50, default="new",
              requires=IS_IN_SET(MACHINE_STATUSES)),
        Field("machine_type", "string", length=100),  # Instance type/size
        Field("architecture", "string", length=50, default="amd64"),
        Field("cpu_count", "integer"),
        Field("memory_mb", "integer"),
        Field("storage_gb", "integer"),
        Field("os_image", "string", length=255),
        Field("zone", "string", length=100),
        Field("tags", "text"),  # JSON array
        Field("metadata", "text"),  # JSON provider-specific metadata
        Field("lxd_cluster_id", "reference lxd_clusters"),
        Field("fleet_host_id", "integer"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # MaaS Integration Tables (from py4web)
    # =========================================================================

    # MaaS Configuration
    db.define_table(
        "maas_config",
        Field("name", "string", length=100, unique=True, notnull=True),
        Field("maas_url", "string", length=500, notnull=True),
        Field("api_key", "password", length=500, notnull=True),
        Field("username", "string", length=100, notnull=True),
        Field("is_active", "boolean", default=True),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # Legacy servers table (for MaaS bare metal inventory)
    db.define_table(
        "servers",
        Field("hostname", "string", length=255, unique=True, notnull=True),
        Field("maas_system_id", "string", length=100, unique=True),
        Field("mac_address", "string", length=17, notnull=True),
        Field("ip_address", "string", length=64),
        Field("status", "string", length=50, default="New"),
        Field("architecture", "string", length=50, default="amd64"),
        Field("memory_mb", "integer"),
        Field("cpu_count", "integer"),
        Field("storage_gb", "integer"),
        Field("power_type", "string", length=50),
        Field("power_parameters", "text"),  # JSON
        Field("zone", "string", length=100, default="default"),
        Field("pool", "string", length=100, default="default"),
        Field("tags", "text"),  # JSON array
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # LXD Cluster Tables
    # =========================================================================

    # LXD Clusters
    db.define_table(
        "lxd_clusters",
        Field("name", "string", length=100, unique=True, notnull=True),
        Field("description", "text"),
        Field("cluster_key_path", "string", length=500),  # Path in secrets manager
        Field("admin_cert_path", "string", length=500),  # Path in secrets manager
        Field("api_endpoint", "string", length=500),
        Field("api_port", "integer", default=8443),
        Field("status", "string", length=50, default="initializing"),
        Field("member_count", "integer", default=0),
        Field("storage_pool", "string", length=100, default="default"),
        Field("network_bridge", "string", length=100, default="lxdbr0"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # LXD Cluster Members
    db.define_table(
        "lxd_cluster_members",
        Field("cluster_id", "reference lxd_clusters", notnull=True),
        Field("machine_id", "reference cloud_machines"),
        Field("member_name", "string", length=255, notnull=True),
        Field("api_url", "string", length=500),
        Field("status", "string", length=50, default="pending"),
        Field("is_leader", "boolean", default=False),
        Field("joined_at", "datetime"),
        Field("last_seen_at", "datetime"),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # =========================================================================
    # Cloud-Init Templates
    # =========================================================================

    db.define_table(
        "cloud_init_templates",
        Field("name", "string", length=100, unique=True, notnull=True),
        Field("description", "text"),
        Field("template_content", "text", notnull=True),
        Field("template_type", "string", length=50, default="user-data",
              requires=IS_IN_SET(["user-data", "meta-data", "network-config"])),
        Field("is_default", "boolean", default=False),
        Field("version", "string", length=20, default="1.0"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # Package configurations
    db.define_table(
        "package_configs",
        Field("name", "string", length=100, unique=True, notnull=True),
        Field("description", "text"),
        Field("packages", "text", notnull=True),  # JSON array
        Field("repositories", "text"),  # JSON array
        Field("pre_install_scripts", "text"),
        Field("post_install_scripts", "text"),
        Field("is_default", "boolean", default=False),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # Deployment Jobs
    # =========================================================================

    db.define_table(
        "deployment_jobs",
        Field("job_id", "string", length=64, unique=True, notnull=True),
        Field("machine_id", "reference cloud_machines"),
        Field("server_id", "reference servers"),  # Legacy MaaS reference
        Field("cloud_init_template_id", "reference cloud_init_templates"),
        Field("package_config_id", "reference package_configs"),
        Field("lxd_cluster_id", "reference lxd_clusters"),
        Field("status", "string", length=50, default="pending",
              requires=IS_IN_SET(JOB_STATUSES)),
        Field("job_type", "string", length=50, default="provision"),
        Field("ansible_playbook", "string", length=255),
        Field("log_output", "text"),
        Field("error_message", "text"),
        Field("progress_percent", "integer", default=0),
        Field("started_at", "datetime"),
        Field("completed_at", "datetime"),
        Field("created_by", "reference auth_user"),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # =========================================================================
    # FleetDM Integration Tables
    # =========================================================================

    # FleetDM Configuration
    db.define_table(
        "fleetdm_config",
        Field("name", "string", length=100, unique=True, notnull=True),
        Field("fleet_url", "string", length=500, notnull=True),
        Field("api_token_path", "string", length=500),  # Path in secrets manager
        Field("is_active", "boolean", default=True),
        Field("osquery_version", "string", length=20, default="5.10.2"),
        Field("enroll_secret_path", "string", length=500),  # Path in secrets manager
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # FleetDM Hosts
    db.define_table(
        "fleet_hosts",
        Field("fleet_host_id", "integer", unique=True, notnull=True),
        Field("hostname", "string", length=255, notnull=True),
        Field("uuid", "string", length=64, unique=True),
        Field("machine_id", "reference cloud_machines"),
        Field("server_id", "reference servers"),  # Legacy reference
        Field("last_seen_at", "datetime"),
        Field("status", "string", length=50, default="offline"),
        Field("platform", "string", length=50),
        Field("osquery_version", "string", length=20),
        Field("enrolled_at", "datetime"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # FleetDM Queries
    db.define_table(
        "fleet_queries",
        Field("name", "string", length=100, unique=True, notnull=True),
        Field("description", "text"),
        Field("query", "text", notnull=True),
        Field("fleet_query_id", "integer"),
        Field("category", "string", length=50, default="custom"),
        Field("is_scheduled", "boolean", default=False),
        Field("interval_seconds", "integer", default=3600),
        Field("created_by", "reference auth_user"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # Query Executions
    db.define_table(
        "query_executions",
        Field("query_id", "reference fleet_queries"),
        Field("campaign_id", "integer"),
        Field("executed_by", "reference auth_user"),
        Field("target_hosts", "text"),  # JSON array
        Field("status", "string", length=50, default="pending"),
        Field("results_count", "integer", default=0),
        Field("execution_time_ms", "integer"),
        Field("error_message", "text"),
        Field("executed_at", "datetime", default=datetime.utcnow),
        Field("completed_at", "datetime"),
    )

    # FleetDM Alerts
    db.define_table(
        "fleet_alerts",
        Field("name", "string", length=100, unique=True, notnull=True),
        Field("description", "text"),
        Field("query_id", "reference fleet_queries"),
        Field("alert_condition", "string", length=100, notnull=True),
        Field("condition_parameters", "text"),  # JSON
        Field("notification_channels", "text"),  # JSON array
        Field("is_active", "boolean", default=True),
        Field("severity", "string", length=20, default="medium",
              requires=IS_IN_SET(["low", "medium", "high", "critical"])),
        Field("cooldown_minutes", "integer", default=60),
        Field("last_triggered_at", "datetime"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # Alert History
    db.define_table(
        "alert_history",
        Field("alert_id", "reference fleet_alerts"),
        Field("triggered_by", "string", length=100),
        Field("trigger_data", "text"),  # JSON
        Field("notification_sent", "boolean", default=False),
        Field("notification_channels_used", "text"),  # JSON array
        Field("resolved", "boolean", default=False),
        Field("resolved_by", "reference auth_user"),
        Field("resolution_notes", "text"),
        Field("triggered_at", "datetime", default=datetime.utcnow),
        Field("resolved_at", "datetime"),
    )

    # OSQuery Results Cache
    db.define_table(
        "osquery_results",
        Field("host_id", "reference fleet_hosts"),
        Field("query_name", "string", length=100, notnull=True),
        Field("result_data", "text"),  # JSON
        Field("result_hash", "string", length=64),
        Field("execution_time", "datetime"),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # =========================================================================
    # Resource Teams & Shell Access Tables
    # =========================================================================

    # Resource Teams
    db.define_table(
        "resource_teams",
        Field("name", "string", length=255, unique=True, notnull=True),
        Field("description", "text"),
        Field("created_by", "reference auth_user", notnull=True),
        Field("is_active", "boolean", default=True),
        Field("metadata", "text"),  # JSON
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # Team Members
    db.define_table(
        "team_members",
        Field("team_id", "reference resource_teams", notnull=True,
              ondelete="CASCADE"),
        Field("user_id", "reference auth_user", notnull=True,
              ondelete="CASCADE"),
        Field("role", "string", length=50, notnull=True,
              requires=IS_IN_SET(["owner", "admin", "member", "viewer"])),
        Field("added_by", "reference auth_user", notnull=True),
        Field("added_at", "datetime", default=datetime.utcnow),
        Field("expires_at", "datetime"),
    )

    # Resource Assignments
    db.define_table(
        "resource_assignments",
        Field("team_id", "reference resource_teams", notnull=True),
        Field("resource_type", "string", length=100, notnull=True),
        Field("resource_id", "string", length=255, notnull=True),
        Field("permissions", "text", notnull=True),  # JSON array
        Field("assigned_by", "reference auth_user", notnull=True),
        Field("assigned_at", "datetime", default=datetime.utcnow),
    )

    # Access Agents
    db.define_table(
        "access_agents",
        Field("agent_id", "string", length=255, unique=True, notnull=True),
        Field("hostname", "string", length=255, notnull=True),
        Field("ip_address", "string", length=64),
        Field("enrollment_key_hash", "string", length=255),
        Field("enrollment_completed", "boolean", default=False),
        Field("jwt_token_id", "reference auth_refresh_tokens"),
        Field("last_heartbeat", "datetime"),
        Field("status", "string", length=50, default="pending", notnull=True,
              requires=IS_IN_SET(["pending", "enrolled", "active", "suspended"])),
        Field("capabilities", "text"),  # JSON
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # Enrollment Keys
    db.define_table(
        "enrollment_keys",
        Field("key_hash", "string", length=255, unique=True, notnull=True),
        Field("created_by", "reference auth_user", notnull=True),
        Field("expires_at", "datetime", notnull=True),
        Field("is_used", "boolean", default=False),
        Field("used_by_agent", "reference access_agents"),
        Field("metadata", "text"),  # JSON
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # SSH CA Configuration
    db.define_table(
        "ssh_ca_config",
        Field("ca_name", "string", length=255, unique=True, notnull=True),
        Field("ca_type", "string", length=50, notnull=True,
              requires=IS_IN_SET(["user", "host"])),
        Field("public_key", "text", notnull=True),
        Field("private_key_vault_path", "string", length=500),
        Field("cert_validity_seconds", "integer", default=3600),
        Field("max_validity_seconds", "integer"),
        Field("principals_allowed", "text"),  # JSON array
        Field("is_active", "boolean", default=True),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # Shell Sessions
    db.define_table(
        "shell_sessions",
        Field("session_id", "string", length=255, unique=True, notnull=True),
        Field("user_id", "reference auth_user", notnull=True),
        Field("team_id", "reference resource_teams"),
        Field("resource_type", "string", length=100, notnull=True),
        Field("resource_id", "string", length=255, notnull=True),
        Field("agent_id", "reference access_agents", notnull=True),
        Field("session_type", "string", length=50, notnull=True,
              requires=IS_IN_SET(["ssh", "kubectl", "docker", "cloud_cli"])),
        Field("started_at", "datetime", default=datetime.utcnow),
        Field("ended_at", "datetime"),
        Field("client_ip", "string", length=64),
        Field("audit_log_path", "string", length=500),
    )

    # =========================================================================
    # System Logs
    # =========================================================================

    db.define_table(
        "system_logs",
        Field("level", "string", length=20, notnull=True,
              requires=IS_IN_SET(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])),
        Field("component", "string", length=50, notnull=True),
        Field("message", "text", notnull=True),
        Field("details", "text"),  # JSON
        Field("machine_id", "reference cloud_machines"),
        Field("server_id", "reference servers"),
        Field("job_id", "reference deployment_jobs"),
        Field("user_id", "reference auth_user"),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # Commit table definitions
    db.commit()

    # Store db instance in app
    app.config["db"] = db

    # Create default roles if they don't exist
    _create_default_roles(db)

    return db


def _create_default_roles(db: DAL) -> None:
    """Create default roles if they don't exist."""
    default_roles = [
        {"name": "admin", "description": "Full system access"},
        {"name": "maintainer", "description": "Read/write access, no user management"},
        {"name": "viewer", "description": "Read-only access"},
    ]

    for role_data in default_roles:
        existing = db(db.auth_role.name == role_data["name"]).select().first()
        if not existing:
            db.auth_role.insert(**role_data)

    db.commit()


def get_db() -> DAL:
    """Get database connection for current request context."""
    from flask import current_app

    if "db" not in g:
        g.db = current_app.config.get("db")
    return g.db
