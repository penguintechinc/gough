"""iPXE Provisioning Database Models for Gough.

This module defines all iPXE/provisioning-related database constants using penguin-dal:
- iPXE/DHCP configuration
- Machine inventory and discovery
- Deployable biomes (snaps, cloud-init, LXD)
- Boot images and configurations
- Deployment tracking and logging
- Storage and Elder integration

Schema is managed by SQLAlchemy/Alembic (see models_sqlalchemy.py).
Runtime queries use penguin-dal (auto-reflects the schema).
"""


# Enum-like constants
DHCP_MODES = ["full", "proxy", "disabled"]
MACHINE_STATUSES = [
    "unknown", "discovered", "commissioning", "ready",
    "deploying", "deployed", "failed"
]
BOOT_MODES = ["bios", "uefi", "uefi_http"]
ARCHITECTURES = ["amd64", "arm64"]
POWER_TYPES = ["ipmi", "redfish", "amt", "wol", "manual"]
BIOME_TYPES = ["snap", "cloud_init", "lxd_container", "lxd_vm"]
IMAGE_TYPES = ["live", "install", "minimal"]
DEPLOYMENT_STATUSES = [
    "pending", "power_on", "pxe_boot", "os_install",
    "biome_deploy", "complete", "failed"
]
BOOT_EVENT_TYPES = [
    "dhcp_request", "tftp_request", "boot_start",
    "os_installed", "biome_started", "biome_complete",
    "deployment_complete", "error"
]
STORAGE_PROVIDERS = [
    "minio", "aws_s3", "gcs", "do_spaces", "wasabi",
    "backblaze", "custom"
]
