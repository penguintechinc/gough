"""iPXE Provisioning Database Models for Gough.

This module defines all iPXE/provisioning-related database tables using PyDAL:
- iPXE/DHCP configuration
- Machine inventory and discovery
- Deployable eggs (snaps, cloud-init, LXD)
- Boot images and configurations
- Deployment tracking and logging
- Storage and Elder integration

All tables use PyDAL for runtime operations as required by CLAUDE.md.
"""

from datetime import datetime

from pydal import DAL, Field
from pydal.validators import IS_IN_SET, IS_NOT_IN_DB, IS_NOT_EMPTY, IS_JSON


# Enum-like constants
DHCP_MODES = ["full", "proxy", "disabled"]
MACHINE_STATUSES = [
    "unknown", "discovered", "commissioning", "ready",
    "deploying", "deployed", "failed"
]
BOOT_MODES = ["bios", "uefi", "uefi_http"]
ARCHITECTURES = ["amd64", "arm64"]
POWER_TYPES = ["ipmi", "redfish", "amt", "wol", "manual"]
EGG_TYPES = ["snap", "cloud_init", "lxd_container", "lxd_vm"]
IMAGE_TYPES = ["live", "install", "minimal"]
DEPLOYMENT_STATUSES = [
    "pending", "power_on", "pxe_boot", "os_install",
    "egg_deploy", "complete", "failed"
]
BOOT_EVENT_TYPES = [
    "dhcp_request", "tftp_request", "boot_start",
    "os_installed", "egg_started", "egg_complete",
    "deployment_complete", "error"
]
STORAGE_PROVIDERS = [
    "minio", "aws_s3", "gcs", "do_spaces", "wasabi",
    "backblaze", "custom"
]


def define_ipxe_tables(db: DAL) -> None:
    """Define all iPXE provisioning tables in the database.

    Args:
        db: PyDAL database instance

    Tables created:
        - ipxe_config: Global iPXE/DHCP configuration
        - ipxe_machines: Discovered/managed nodes
        - eggs: Deployable packages (snap, cloud_init, lxd_container, lxd_vm)
        - egg_groups: Logical groupings of eggs
        - ipxe_images: Boot images (Ubuntu LTS)
        - ipxe_boot_configs: Machine boot configurations
        - deployment_jobs: Deployment tracking
        - boot_events: Boot/deployment event log
        - storage_config: S3 storage configuration
        - elder_config: Elder integration configuration
    """

    # =========================================================================
    # 1. ipxe_config - Global iPXE/DHCP Configuration
    # =========================================================================
    db.define_table(
        "ipxe_config",
        Field("name", "string", length=255, unique=True, notnull=True,
              requires=[IS_NOT_EMPTY(), IS_NOT_IN_DB(db, "ipxe_config.name")]),
        Field("dhcp_mode", "string", length=20, notnull=True, default="proxy",
              requires=IS_IN_SET(DHCP_MODES)),
        Field("dhcp_interface", "string", length=64),
        Field("dhcp_subnet", "string", length=64),
        Field("dhcp_range_start", "string", length=64),
        Field("dhcp_range_end", "string", length=64),
        Field("dhcp_gateway", "string", length=64),
        Field("dns_servers", "json"),  # Array of DNS server IPs
        Field("tftp_enabled", "boolean", default=True),
        Field("http_boot_url", "string", length=512),
        Field("minio_bucket", "string", length=255),
        Field("default_boot_script", "text"),
        Field("chain_url", "string", length=512),
        Field("is_active", "boolean", default=True),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # 2. ipxe_machines - Discovered/Managed Nodes
    # =========================================================================
    db.define_table(
        "ipxe_machines",
        Field("system_id", "string", length=64, unique=True, notnull=True,
              requires=[IS_NOT_EMPTY(), IS_NOT_IN_DB(db, "ipxe_machines.system_id")]),
        Field("hostname", "string", length=255),
        Field("mac_address", "string", length=64, unique=True, notnull=True,
              requires=[IS_NOT_EMPTY(), IS_NOT_IN_DB(db, "ipxe_machines.mac_address")]),
        Field("ip_address", "string", length=64),
        Field("status", "string", length=64, notnull=True, default="unknown",
              requires=IS_IN_SET(MACHINE_STATUSES)),
        Field("boot_mode", "string", length=20, default="uefi",
              requires=IS_IN_SET(BOOT_MODES)),
        Field("architecture", "string", length=20, default="amd64",
              requires=IS_IN_SET(ARCHITECTURES)),
        Field("cpu_count", "integer", default=0),
        Field("memory_mb", "integer", default=0),
        Field("storage_gb", "integer", default=0),
        Field("bmc_address", "string", length=255),
        Field("power_type", "string", length=64, default="manual",
              requires=IS_IN_SET(POWER_TYPES)),
        Field("zone", "string", length=255),
        Field("pool", "string", length=255),
        Field("tags", "json"),  # Array of tags
        Field("hardware_info", "json"),  # Full lshw output
        Field("boot_config_id", "reference ipxe_boot_configs"),
        Field("assigned_eggs", "json"),  # Array of egg IDs
        Field("last_boot_at", "datetime"),
        Field("last_seen_at", "datetime"),
        Field("deployed_at", "datetime"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # 3. eggs - Deployable Packages
    # =========================================================================
    db.define_table(
        "eggs",
        Field("name", "string", length=255, unique=True, notnull=True,
              requires=[IS_NOT_EMPTY(), IS_NOT_IN_DB(db, "eggs.name")]),
        Field("display_name", "string", length=255, notnull=True),
        Field("description", "text"),
        Field("egg_type", "string", length=64, notnull=True,
              requires=IS_IN_SET(EGG_TYPES)),
        Field("version", "string", length=64),
        Field("category", "string", length=255),  # networking, storage, monitoring, etc.
        # Snap-specific fields
        Field("snap_name", "string", length=255),
        Field("snap_channel", "string", length=64),
        Field("snap_classic", "boolean", default=False),
        # Cloud-init-specific fields
        Field("cloud_init_content", "text"),  # YAML content
        # LXD-specific fields
        Field("lxd_image_alias", "string", length=255),
        Field("lxd_image_url", "string", length=512),  # MinIO path or URL
        Field("lxd_profiles", "json"),  # Array of LXD profile names
        # Deployment metadata
        Field("is_hypervisor_config", "boolean", default=False),
        Field("dependencies", "json"),  # Array of egg IDs
        Field("min_ram_mb", "integer", default=0),
        Field("min_disk_gb", "integer", default=0),
        Field("required_architecture", "string", length=20,
              requires=IS_IN_SET(ARCHITECTURES + ["any"])),
        Field("is_active", "boolean", default=True),
        Field("is_default", "boolean", default=False),
        Field("checksum", "string", length=128),
        Field("size_bytes", "bigint", default=0),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # 4. egg_groups - Logical Groupings of Eggs
    # =========================================================================
    db.define_table(
        "egg_groups",
        Field("name", "string", length=255, unique=True, notnull=True,
              requires=[IS_NOT_EMPTY(), IS_NOT_IN_DB(db, "egg_groups.name")]),
        Field("display_name", "string", length=255, notnull=True),
        Field("description", "text"),
        Field("eggs", "json", notnull=True),  # Array of {egg_id, order}
        Field("is_default", "boolean", default=False),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # 5. ipxe_images - Boot Images (Ubuntu LTS)
    # =========================================================================
    db.define_table(
        "ipxe_images",
        Field("name", "string", length=255, unique=True, notnull=True,
              requires=[IS_NOT_EMPTY(), IS_NOT_IN_DB(db, "ipxe_images.name")]),
        Field("display_name", "string", length=255, notnull=True),
        Field("os_name", "string", length=64, notnull=True, default="ubuntu"),
        Field("os_version", "string", length=64, notnull=True, default="24.04"),
        Field("architecture", "string", length=20, notnull=True, default="amd64",
              requires=IS_IN_SET(ARCHITECTURES)),
        Field("kernel_path", "string", length=512),  # MinIO path
        Field("initrd_path", "string", length=512),  # MinIO path
        Field("squashfs_path", "string", length=512),  # MinIO path
        Field("kernel_params", "text"),
        Field("image_type", "string", length=64, default="minimal",
              requires=IS_IN_SET(IMAGE_TYPES)),
        Field("minio_bucket", "string", length=255),
        Field("is_default", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("checksum", "string", length=128),
        Field("size_bytes", "bigint", default=0),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # 6. ipxe_boot_configs - Machine Boot Configurations
    # =========================================================================
    db.define_table(
        "ipxe_boot_configs",
        Field("name", "string", length=255, unique=True, notnull=True,
              requires=[IS_NOT_EMPTY(), IS_NOT_IN_DB(db, "ipxe_boot_configs.name")]),
        Field("description", "text"),
        Field("ipxe_script", "text"),  # Custom iPXE script
        Field("kernel_params", "text"),
        Field("boot_order", "json"),  # Array of boot device priorities
        Field("timeout_seconds", "integer", default=30),
        Field("default_image_id", "reference ipxe_images"),
        Field("assigned_egg_group_id", "reference egg_groups"),
        Field("is_default", "boolean", default=False),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # 7. deployment_jobs - Deployment Tracking
    # =========================================================================
    db.define_table(
        "deployment_jobs",
        Field("job_id", "string", length=64, unique=True, notnull=True,
              requires=[IS_NOT_EMPTY(), IS_NOT_IN_DB(db, "deployment_jobs.job_id")]),
        Field("machine_id", "reference ipxe_machines", notnull=True),
        Field("image_id", "reference ipxe_images", notnull=True),
        Field("boot_config_id", "reference ipxe_boot_configs"),
        Field("eggs_to_deploy", "json"),  # Array of egg IDs
        Field("rendered_cloud_init", "text"),  # Merged cloud-init YAML
        Field("status", "string", length=64, notnull=True, default="pending",
              requires=IS_IN_SET(DEPLOYMENT_STATUSES)),
        Field("progress_percent", "integer", default=0),
        Field("current_phase", "string", length=255),
        Field("log_output", "text"),
        Field("error_message", "text"),
        Field("started_at", "datetime"),
        Field("completed_at", "datetime"),
        Field("created_by", "reference auth_user"),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # =========================================================================
    # 8. boot_events - Boot/Deployment Event Log
    # =========================================================================
    db.define_table(
        "boot_events",
        Field("machine_id", "reference ipxe_machines"),
        Field("mac_address", "string", length=64, notnull=True),
        Field("ip_address", "string", length=64),
        Field("event_type", "string", length=64, notnull=True,
              requires=IS_IN_SET(BOOT_EVENT_TYPES)),
        Field("details", "json"),  # Event-specific details
        Field("status", "string", length=64),
        Field("created_at", "datetime", default=datetime.utcnow),
    )

    # =========================================================================
    # 9. storage_config - S3 Storage Configuration
    # =========================================================================
    db.define_table(
        "storage_config",
        Field("name", "string", length=255, unique=True, notnull=True,
              requires=[IS_NOT_EMPTY(), IS_NOT_IN_DB(db, "storage_config.name")]),
        Field("provider_type", "string", length=64, notnull=True,
              requires=IS_IN_SET(STORAGE_PROVIDERS)),
        Field("endpoint_url", "string", length=512),
        Field("region", "string", length=64),
        Field("bucket_name", "string", length=255),
        Field("credentials_path", "string", length=512, notnull=True),
        Field("use_ssl", "boolean", default=True),
        Field("path_style", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("is_default", "boolean", default=False),
        Field("config_data", "json"),
        Field("created_by", "reference auth_user"),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # =========================================================================
    # 10. elder_config - Elder Integration Configuration
    # =========================================================================
    db.define_table(
        "elder_config",
        Field("elder_url", "string", length=512, notnull=True),
        Field("api_key", "password", length=512, notnull=True),  # Encrypted
        Field("sync_enabled", "boolean", default=True),
        Field("sync_interval_seconds", "integer", default=300),
        Field("last_sync_at", "datetime"),
        Field("last_sync_status", "string", length=64),
        Field("created_at", "datetime", default=datetime.utcnow),
        Field("updated_at", "datetime", default=datetime.utcnow,
              update=datetime.utcnow),
    )

    # Add indexes for performance
    db.ipxe_machines.mac_address.create_index()
    db.ipxe_machines.system_id.create_index()
    db.ipxe_machines.status.create_index()
    db.eggs.egg_type.create_index()
    db.eggs.category.create_index()
    db.deployment_jobs.job_id.create_index()
    db.deployment_jobs.status.create_index()
    db.boot_events.machine_id.create_index()
    db.boot_events.event_type.create_index()
    db.boot_events.created_at.create_index()
