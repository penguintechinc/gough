"""iPXE Provisioning API Endpoints.

Provides REST API for managing iPXE/MAAS-like provisioning operations:
- iPXE/DHCP configuration management
- Machine discovery, commissioning, deployment
- Boot images and configurations
- Egg deployment (snaps, cloud-init, LXD)
- Power management integration
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime
from typing import Any, Optional

from quart import Blueprint, current_app, jsonify, request

from ..middleware import auth_required, admin_required, maintainer_or_admin_required
from ..models import get_db

log = logging.getLogger(__name__)

ipxe_bp = Blueprint("ipxe", __name__)


# =============================================================================
# Helper Functions
# =============================================================================

def _validate_required_fields(data: dict, required: list[str]) -> Optional[tuple]:
    """Validate required fields in request data.

    Args:
        data: Request data dictionary
        required: List of required field names

    Returns:
        None if valid, or (error_response, status_code) tuple
    """
    missing = [field for field in required if not data.get(field)]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400
    return None


def _get_machine_by_id(machine_id: str) -> Optional[dict]:
    """Get machine by ID or system_id.

    Args:
        machine_id: Database ID or system_id

    Returns:
        Machine record dict or None
    """
    db = get_db()

    # Try as integer ID first
    try:
        mid = int(machine_id)
        machine = db(db.ipxe_machines.id == mid).select().first()
        if machine:
            return machine.as_dict()
    except ValueError:
        pass

    # Try as system_id
    machine = db(db.ipxe_machines.system_id == machine_id).select().first()
    if machine:
        return machine.as_dict()

    return None


def _get_image_by_id(image_id: int) -> Optional[dict]:
    """Get boot image by ID.

    Args:
        image_id: Database ID

    Returns:
        Image record dict or None
    """
    db = get_db()
    image = db(db.ipxe_images.id == image_id).select().first()
    return image.as_dict() if image else None


def _get_boot_config_by_id(config_id: int) -> Optional[dict]:
    """Get boot configuration by ID.

    Args:
        config_id: Database ID

    Returns:
        Boot config record dict or None
    """
    db = get_db()
    config = db(db.ipxe_boot_configs.id == config_id).select().first()
    return config.as_dict() if config else None


def _create_deployment_job(
    machine_id: int,
    image_id: int,
    boot_config_id: Optional[int],
    eggs_to_deploy: list[int],
    user_id: int
) -> str:
    """Create a new deployment job.

    Args:
        machine_id: Target machine ID
        image_id: Boot image ID
        boot_config_id: Boot configuration ID (optional)
        eggs_to_deploy: List of egg IDs to deploy
        user_id: User initiating deployment

    Returns:
        Job ID string
    """
    db = get_db()
    job_id = f"deploy-{uuid.uuid4().hex[:12]}"

    db.deployment_jobs.insert(
        job_id=job_id,
        machine_id=machine_id,
        image_id=image_id,
        boot_config_id=boot_config_id,
        eggs_to_deploy=eggs_to_deploy,
        status="pending",
        progress_percent=0,
        created_by=user_id,
        started_at=datetime.utcnow()
    )
    db.commit()

    return job_id


def _log_boot_event(
    machine_id: Optional[int],
    mac_address: str,
    event_type: str,
    details: Optional[dict] = None,
    status: Optional[str] = None,
    ip_address: Optional[str] = None
) -> None:
    """Log a boot/deployment event.

    Args:
        machine_id: Machine ID (optional for discovery events)
        mac_address: Machine MAC address
        event_type: Type of event
        details: Event-specific details
        status: Event status
        ip_address: Client IP address
    """
    db = get_db()
    db.boot_events.insert(
        machine_id=machine_id,
        mac_address=mac_address,
        ip_address=ip_address,
        event_type=event_type,
        details=details or {},
        status=status
    )
    db.commit()


# =============================================================================
# iPXE Configuration Endpoints
# =============================================================================

@ipxe_bp.route("/config", methods=["GET"])
@auth_required
async def get_ipxe_config():
    """Get current iPXE/DHCP configuration.

    Returns:
        200: Current configuration
        404: No configuration found
    """
    db = get_db()

    # Get active configuration
    config = db(db.ipxe_config.is_active).select().first()

    if not config:
        return jsonify({"error": "No active iPXE configuration found"}), 404

    return jsonify(config.as_dict()), 200


@ipxe_bp.route("/config", methods=["PUT"])
@admin_required
@auth_required
async def update_ipxe_config():
    """Update iPXE/DHCP configuration.

    Request Body:
        name: Configuration name (required)
        dhcp_mode: DHCP mode (full/proxy/disabled)
        dhcp_interface: Network interface
        dhcp_subnet: Subnet CIDR
        dhcp_range_start: DHCP range start IP
        dhcp_range_end: DHCP range end IP
        dhcp_gateway: Gateway IP
        dns_servers: List of DNS server IPs
        tftp_enabled: Enable TFTP boot
        http_boot_url: HTTP boot URL
        minio_bucket: MinIO bucket name
        default_boot_script: Default iPXE boot script
        chain_url: Chain loading URL

    Returns:
        200: Configuration updated
        201: Configuration created
        400: Invalid request
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    validation_error = _validate_required_fields(data, ["name"])
    if validation_error:
        return validation_error

    db = get_db()

    # Check if configuration exists
    existing = db(db.ipxe_config.name == data["name"]).select().first()

    update_fields = {
        "dhcp_mode": data.get("dhcp_mode", "proxy"),
        "dhcp_interface": data.get("dhcp_interface"),
        "dhcp_subnet": data.get("dhcp_subnet"),
        "dhcp_range_start": data.get("dhcp_range_start"),
        "dhcp_range_end": data.get("dhcp_range_end"),
        "dhcp_gateway": data.get("dhcp_gateway"),
        "dns_servers": data.get("dns_servers", []),
        "tftp_enabled": data.get("tftp_enabled", True),
        "http_boot_url": data.get("http_boot_url"),
        "minio_bucket": data.get("minio_bucket"),
        "default_boot_script": data.get("default_boot_script"),
        "chain_url": data.get("chain_url"),
        "is_active": data.get("is_active", True),
        "updated_at": datetime.utcnow()
    }

    if existing:
        # Update existing configuration
        db(db.ipxe_config.id == existing.id).update(**update_fields)
        db.commit()

        updated = db(db.ipxe_config.id == existing.id).select().first()
        return jsonify(updated.as_dict()), 200
    else:
        # Create new configuration
        update_fields["name"] = data["name"]
        config_id = db.ipxe_config.insert(**update_fields)
        db.commit()

        created = db(db.ipxe_config.id == config_id).select().first()
        return jsonify(created.as_dict()), 201


# =============================================================================
# Machine Management Endpoints
# =============================================================================

@ipxe_bp.route("/machines", methods=["GET"])
@auth_required
async def list_machines():
    """List all machines with optional filters.

    Query Parameters:
        status: Filter by status (unknown/discovered/commissioning/ready/deploying/deployed/failed)
        zone: Filter by zone
        pool: Filter by pool

    Returns:
        200: List of machines
    """
    db = get_db()

    # Build query
    query = db.ipxe_machines.id > 0

    # Apply filters
    args = await request.args
    if args.get("status"):
        query &= (db.ipxe_machines.status == args["status"])
    if args.get("zone"):
        query &= (db.ipxe_machines.zone == args["zone"])
    if args.get("pool"):
        query &= (db.ipxe_machines.pool == args["pool"])

    machines = db(query).select(orderby=~db.ipxe_machines.last_seen_at)

    return jsonify({
        "machines": [m.as_dict() for m in machines],
        "count": len(machines)
    }), 200


@ipxe_bp.route("/machines/<string:machine_id>", methods=["GET"])
@auth_required
async def get_machine(machine_id: str):
    """Get machine details by ID or system_id.

    Args:
        machine_id: Machine database ID or system_id

    Returns:
        200: Machine details
        404: Machine not found
    """
    machine = _get_machine_by_id(machine_id)

    if not machine:
        return jsonify({"error": f"Machine not found: {machine_id}"}), 404

    return jsonify(machine), 200


@ipxe_bp.route("/machines/<string:machine_id>/commission", methods=["POST"])
@maintainer_or_admin_required
@auth_required
async def commission_machine(machine_id: str):
    """Commission a discovered machine.

    Commissioning gathers hardware details and prepares machine for deployment.

    Args:
        machine_id: Machine database ID or system_id

    Returns:
        200: Commission started
        400: Invalid machine state
        404: Machine not found
    """
    machine = _get_machine_by_id(machine_id)

    if not machine:
        return jsonify({"error": f"Machine not found: {machine_id}"}), 404

    # Verify machine is in discoverable state
    if machine["status"] not in ["unknown", "discovered", "failed"]:
        return jsonify({
            "error": f"Cannot commission machine in '{machine['status']}' state",
            "allowed_states": ["unknown", "discovered", "failed"]
        }), 400

    db = get_db()

    # Update machine status
    db(db.ipxe_machines.id == machine["id"]).update(
        status="commissioning",
        updated_at=datetime.utcnow()
    )
    db.commit()

    # Log event
    _log_boot_event(
        machine_id=machine["id"],
        mac_address=machine["mac_address"],
        event_type="boot_start",
        details={"action": "commission"},
        status="started"
    )

    log.info(f"Machine {machine['system_id']} commissioning started")

    return jsonify({
        "message": "Commissioning started",
        "machine_id": machine["system_id"],
        "status": "commissioning"
    }), 200


@ipxe_bp.route("/machines/<string:machine_id>/deploy", methods=["POST"])
@maintainer_or_admin_required
@auth_required
async def deploy_machine(machine_id: str):
    """Deploy OS and eggs to a machine.

    Request Body:
        image_id: Boot image ID (required)
        boot_config_id: Boot configuration ID (optional)
        eggs: List of egg IDs to deploy (optional)

    Args:
        machine_id: Machine database ID or system_id

    Returns:
        200: Deployment started
        400: Invalid request or machine state
        404: Machine not found
    """
    machine = _get_machine_by_id(machine_id)

    if not machine:
        return jsonify({"error": f"Machine not found: {machine_id}"}), 404

    # Verify machine is ready for deployment
    if machine["status"] not in ["ready", "deployed", "failed"]:
        return jsonify({
            "error": f"Cannot deploy machine in '{machine['status']}' state",
            "allowed_states": ["ready", "deployed", "failed"]
        }), 400

    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    validation_error = _validate_required_fields(data, ["image_id"])
    if validation_error:
        return validation_error

    image_id = data["image_id"]
    boot_config_id = data.get("boot_config_id")
    eggs = data.get("eggs", [])

    # Validate image exists
    if not _get_image_by_id(image_id):
        return jsonify({"error": f"Boot image not found: {image_id}"}), 404

    # Validate boot config if provided
    if boot_config_id and not _get_boot_config_by_id(boot_config_id):
        return jsonify({"error": f"Boot config not found: {boot_config_id}"}), 404

    db = get_db()

    # Get current user from context
    from quart import g
    user = getattr(g, "current_user", None)
    user_id = user["id"] if user else None

    # Create deployment job
    job_id = _create_deployment_job(
        machine_id=machine["id"],
        image_id=image_id,
        boot_config_id=boot_config_id,
        eggs_to_deploy=eggs,
        user_id=user_id
    )

    # Update machine status and eggs
    db(db.ipxe_machines.id == machine["id"]).update(
        status="deploying",
        boot_config_id=boot_config_id,
        assigned_eggs=eggs,
        updated_at=datetime.utcnow()
    )
    db.commit()

    # Log event
    _log_boot_event(
        machine_id=machine["id"],
        mac_address=machine["mac_address"],
        event_type="boot_start",
        details={
            "action": "deploy",
            "job_id": job_id,
            "image_id": image_id,
            "eggs": eggs
        },
        status="started"
    )

    log.info(f"Machine {machine['system_id']} deployment started (job: {job_id})")

    return jsonify({
        "message": "Deployment started",
        "machine_id": machine["system_id"],
        "job_id": job_id,
        "status": "deploying"
    }), 200


@ipxe_bp.route("/machines/<string:machine_id>/release", methods=["POST"])
@maintainer_or_admin_required
@auth_required
async def release_machine(machine_id: str):
    """Release a deployed machine back to ready pool.

    Args:
        machine_id: Machine database ID or system_id

    Returns:
        200: Machine released
        400: Invalid machine state
        404: Machine not found
    """
    machine = _get_machine_by_id(machine_id)

    if not machine:
        return jsonify({"error": f"Machine not found: {machine_id}"}), 404

    # Verify machine is deployed
    if machine["status"] not in ["deployed", "failed"]:
        return jsonify({
            "error": f"Cannot release machine in '{machine['status']}' state",
            "allowed_states": ["deployed", "failed"]
        }), 400

    db = get_db()

    # Update machine status
    db(db.ipxe_machines.id == machine["id"]).update(
        status="ready",
        assigned_eggs=[],
        deployed_at=None,
        updated_at=datetime.utcnow()
    )
    db.commit()

    # Log event
    _log_boot_event(
        machine_id=machine["id"],
        mac_address=machine["mac_address"],
        event_type="deployment_complete",
        details={"action": "release"},
        status="released"
    )

    log.info(f"Machine {machine['system_id']} released to ready pool")

    return jsonify({
        "message": "Machine released",
        "machine_id": machine["system_id"],
        "status": "ready"
    }), 200


@ipxe_bp.route("/machines/<string:machine_id>/power/<string:action>", methods=["POST"])
@maintainer_or_admin_required
@auth_required
async def power_control(machine_id: str, action: str):
    """Control machine power state.

    Args:
        machine_id: Machine database ID or system_id
        action: Power action (on/off/cycle/reset)

    Returns:
        200: Power action initiated
        400: Invalid action or power type
        404: Machine not found
    """
    machine = _get_machine_by_id(machine_id)

    if not machine:
        return jsonify({"error": f"Machine not found: {machine_id}"}), 404

    # Validate action
    valid_actions = ["on", "off", "cycle", "reset"]
    if action not in valid_actions:
        return jsonify({
            "error": f"Invalid power action: {action}",
            "valid_actions": valid_actions
        }), 400

    # Check if power control is available
    power_type = machine.get("power_type", "manual")
    if power_type == "manual":
        return jsonify({
            "error": "Manual power control - no automated power management available",
            "machine_id": machine["system_id"]
        }), 400

    # Log the power action
    _log_boot_event(
        machine_id=machine["id"],
        mac_address=machine["mac_address"],
        event_type="boot_start",
        details={
            "action": f"power_{action}",
            "power_type": power_type,
            "bmc_address": machine.get("bmc_address")
        },
        status="initiated"
    )

    log.info(f"Power {action} initiated for machine {machine['system_id']} via {power_type}")

    return jsonify({
        "message": f"Power {action} initiated",
        "machine_id": machine["system_id"],
        "power_type": power_type,
        "action": action
    }), 200


@ipxe_bp.route("/machines/<string:machine_id>/eggs", methods=["PUT"])
@maintainer_or_admin_required
@auth_required
async def update_machine_eggs(machine_id: str):
    """Update eggs assigned to a machine.

    Request Body:
        eggs: List of egg IDs

    Args:
        machine_id: Machine database ID or system_id

    Returns:
        200: Eggs updated
        400: Invalid request
        404: Machine not found
    """
    machine = _get_machine_by_id(machine_id)

    if not machine:
        return jsonify({"error": f"Machine not found: {machine_id}"}), 404

    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    eggs = data.get("eggs", [])

    if not isinstance(eggs, list):
        return jsonify({"error": "Eggs must be a list"}), 400

    db = get_db()

    # Update assigned eggs
    db(db.ipxe_machines.id == machine["id"]).update(
        assigned_eggs=eggs,
        updated_at=datetime.utcnow()
    )
    db.commit()

    log.info(f"Machine {machine['system_id']} eggs updated: {eggs}")

    return jsonify({
        "message": "Eggs updated",
        "machine_id": machine["system_id"],
        "eggs": eggs
    }), 200


@ipxe_bp.route("/machines/<string:machine_id>", methods=["DELETE"])
@admin_required
@auth_required
async def delete_machine(machine_id: str):
    """Delete a machine from inventory.

    Args:
        machine_id: Machine database ID or system_id

    Returns:
        200: Machine deleted
        400: Machine is deployed (must release first)
        404: Machine not found
    """
    machine = _get_machine_by_id(machine_id)

    if not machine:
        return jsonify({"error": f"Machine not found: {machine_id}"}), 404

    # Prevent deletion of deployed machines
    if machine["status"] == "deployed":
        return jsonify({
            "error": "Cannot delete deployed machine - release it first",
            "machine_id": machine["system_id"]
        }), 400

    db = get_db()

    # Delete machine
    db(db.ipxe_machines.id == machine["id"]).delete()
    db.commit()

    log.info(f"Machine {machine['system_id']} deleted from inventory")

    return jsonify({
        "message": "Machine deleted",
        "machine_id": machine["system_id"]
    }), 200


# =============================================================================
# Boot Images Endpoints
# =============================================================================

@ipxe_bp.route("/images", methods=["GET"])
@auth_required
async def list_images():
    """List all boot images.

    Query Parameters:
        architecture: Filter by architecture (amd64/arm64)
        os_version: Filter by OS version

    Returns:
        200: List of boot images
    """
    db = get_db()

    # Build query
    query = db.ipxe_images.id > 0

    # Apply filters
    args = await request.args
    if args.get("architecture"):
        query &= (db.ipxe_images.architecture == args["architecture"])
    if args.get("os_version"):
        query &= (db.ipxe_images.os_version == args["os_version"])

    images = db(query).select(orderby=~db.ipxe_images.created_at)

    return jsonify({
        "images": [img.as_dict() for img in images],
        "count": len(images)
    }), 200


@ipxe_bp.route("/images", methods=["POST"])
@admin_required
@auth_required
async def create_image():
    """Create a new boot image.

    Request Body:
        name: Unique image name (required)
        display_name: Display name (required)
        os_name: OS name (default: ubuntu)
        os_version: OS version (required)
        architecture: Architecture (required)
        kernel_path: MinIO path to kernel (required)
        initrd_path: MinIO path to initrd (required)
        squashfs_path: MinIO path to squashfs
        kernel_params: Kernel boot parameters
        image_type: Image type (live/install/minimal)
        minio_bucket: MinIO bucket name
        is_default: Set as default image
        is_active: Image is active

    Returns:
        201: Image created
        400: Invalid request
        409: Image name already exists
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    validation_error = _validate_required_fields(
        data,
        ["name", "display_name", "os_version", "architecture", "kernel_path", "initrd_path"]
    )
    if validation_error:
        return validation_error

    db = get_db()

    # Check for duplicate name
    existing = db(db.ipxe_images.name == data["name"]).select().first()
    if existing:
        return jsonify({"error": f"Image name already exists: {data['name']}"}), 409

    # Create image
    image_id = db.ipxe_images.insert(
        name=data["name"],
        display_name=data["display_name"],
        os_name=data.get("os_name", "ubuntu"),
        os_version=data["os_version"],
        architecture=data["architecture"],
        kernel_path=data["kernel_path"],
        initrd_path=data["initrd_path"],
        squashfs_path=data.get("squashfs_path"),
        kernel_params=data.get("kernel_params"),
        image_type=data.get("image_type", "minimal"),
        minio_bucket=data.get("minio_bucket"),
        is_default=data.get("is_default", False),
        is_active=data.get("is_active", True),
        checksum=data.get("checksum"),
        size_bytes=data.get("size_bytes", 0)
    )
    db.commit()

    created = db(db.ipxe_images.id == image_id).select().first()

    log.info(f"Boot image created: {data['name']}")

    return jsonify(created.as_dict()), 201


@ipxe_bp.route("/images/<int:image_id>", methods=["GET"])
@auth_required
async def get_image(image_id: int):
    """Get boot image details.

    Args:
        image_id: Image database ID

    Returns:
        200: Image details
        404: Image not found
    """
    image = _get_image_by_id(image_id)

    if not image:
        return jsonify({"error": f"Boot image not found: {image_id}"}), 404

    return jsonify(image), 200


@ipxe_bp.route("/images/<int:image_id>", methods=["PUT"])
@admin_required
@auth_required
async def update_image(image_id: int):
    """Update boot image.

    Request Body:
        display_name: Display name
        os_version: OS version
        kernel_path: MinIO path to kernel
        initrd_path: MinIO path to initrd
        squashfs_path: MinIO path to squashfs
        kernel_params: Kernel boot parameters
        image_type: Image type
        is_default: Set as default image
        is_active: Image is active

    Args:
        image_id: Image database ID

    Returns:
        200: Image updated
        400: Invalid request
        404: Image not found
    """
    image = _get_image_by_id(image_id)

    if not image:
        return jsonify({"error": f"Boot image not found: {image_id}"}), 404

    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    db = get_db()

    # Build update fields
    update_fields = {"updated_at": datetime.utcnow()}

    allowed_updates = [
        "display_name", "os_version", "kernel_path", "initrd_path",
        "squashfs_path", "kernel_params", "image_type", "minio_bucket",
        "is_default", "is_active", "checksum", "size_bytes"
    ]

    for field in allowed_updates:
        if field in data:
            update_fields[field] = data[field]

    # Update image
    db(db.ipxe_images.id == image_id).update(**update_fields)
    db.commit()

    updated = db(db.ipxe_images.id == image_id).select().first()

    log.info(f"Boot image updated: {image['name']}")

    return jsonify(updated.as_dict()), 200


@ipxe_bp.route("/images/<int:image_id>", methods=["DELETE"])
@admin_required
@auth_required
async def delete_image(image_id: int):
    """Delete boot image.

    Args:
        image_id: Image database ID

    Returns:
        200: Image deleted
        400: Image is in use
        404: Image not found
    """
    image = _get_image_by_id(image_id)

    if not image:
        return jsonify({"error": f"Boot image not found: {image_id}"}), 404

    db = get_db()

    # Check if image is in use
    machines_using = db(db.ipxe_machines.status == "deploying").select()
    for machine in machines_using:
        job = db(
            (db.deployment_jobs.machine_id == machine.id) &
            (db.deployment_jobs.image_id == image_id) &
            (db.deployment_jobs.status.belongs(["pending", "power_on", "pxe_boot", "os_install"]))
        ).select().first()
        if job:
            return jsonify({
                "error": "Image is in use by active deployments",
                "image_id": image_id
            }), 400

    # Delete image
    db(db.ipxe_images.id == image_id).delete()
    db.commit()

    log.info(f"Boot image deleted: {image['name']}")

    return jsonify({
        "message": "Boot image deleted",
        "image_id": image_id
    }), 200


# =============================================================================
# Boot Configurations Endpoints
# =============================================================================

@ipxe_bp.route("/boot-configs", methods=["GET"])
@auth_required
async def list_boot_configs():
    """List all boot configurations.

    Returns:
        200: List of boot configurations
    """
    db = get_db()

    configs = db(db.ipxe_boot_configs).select(orderby=~db.ipxe_boot_configs.created_at)

    return jsonify({
        "boot_configs": [cfg.as_dict() for cfg in configs],
        "count": len(configs)
    }), 200


@ipxe_bp.route("/boot-configs", methods=["POST"])
@admin_required
@auth_required
async def create_boot_config():
    """Create a new boot configuration.

    Request Body:
        name: Unique config name (required)
        description: Description
        ipxe_script: Custom iPXE script
        kernel_params: Kernel parameters
        boot_order: Boot device order list
        timeout_seconds: Boot timeout
        default_image_id: Default boot image ID
        assigned_egg_group_id: Assigned egg group ID
        is_default: Set as default config

    Returns:
        201: Boot config created
        400: Invalid request
        409: Config name already exists
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    validation_error = _validate_required_fields(data, ["name"])
    if validation_error:
        return validation_error

    db = get_db()

    # Check for duplicate name
    existing = db(db.ipxe_boot_configs.name == data["name"]).select().first()
    if existing:
        return jsonify({"error": f"Boot config name already exists: {data['name']}"}), 409

    # Create boot config
    config_id = db.ipxe_boot_configs.insert(
        name=data["name"],
        description=data.get("description"),
        ipxe_script=data.get("ipxe_script"),
        kernel_params=data.get("kernel_params"),
        boot_order=data.get("boot_order", []),
        timeout_seconds=data.get("timeout_seconds", 30),
        default_image_id=data.get("default_image_id"),
        assigned_egg_group_id=data.get("assigned_egg_group_id"),
        is_default=data.get("is_default", False)
    )
    db.commit()

    created = db(db.ipxe_boot_configs.id == config_id).select().first()

    log.info(f"Boot config created: {data['name']}")

    return jsonify(created.as_dict()), 201


@ipxe_bp.route("/boot-configs/<int:config_id>", methods=["GET"])
@auth_required
async def get_boot_config(config_id: int):
    """Get boot configuration details.

    Args:
        config_id: Config database ID

    Returns:
        200: Boot config details
        404: Config not found
    """
    config = _get_boot_config_by_id(config_id)

    if not config:
        return jsonify({"error": f"Boot config not found: {config_id}"}), 404

    return jsonify(config), 200


@ipxe_bp.route("/boot-configs/<int:config_id>", methods=["PUT"])
@admin_required
@auth_required
async def update_boot_config(config_id: int):
    """Update boot configuration.

    Request Body:
        description: Description
        ipxe_script: Custom iPXE script
        kernel_params: Kernel parameters
        boot_order: Boot device order list
        timeout_seconds: Boot timeout
        default_image_id: Default boot image ID
        assigned_egg_group_id: Assigned egg group ID
        is_default: Set as default config

    Args:
        config_id: Config database ID

    Returns:
        200: Boot config updated
        400: Invalid request
        404: Config not found
    """
    config = _get_boot_config_by_id(config_id)

    if not config:
        return jsonify({"error": f"Boot config not found: {config_id}"}), 404

    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    db = get_db()

    # Build update fields
    update_fields = {"updated_at": datetime.utcnow()}

    allowed_updates = [
        "description", "ipxe_script", "kernel_params", "boot_order",
        "timeout_seconds", "default_image_id", "assigned_egg_group_id", "is_default"
    ]

    for field in allowed_updates:
        if field in data:
            update_fields[field] = data[field]

    # Update boot config
    db(db.ipxe_boot_configs.id == config_id).update(**update_fields)
    db.commit()

    updated = db(db.ipxe_boot_configs.id == config_id).select().first()

    log.info(f"Boot config updated: {config['name']}")

    return jsonify(updated.as_dict()), 200


@ipxe_bp.route("/boot-configs/<int:config_id>", methods=["DELETE"])
@admin_required
@auth_required
async def delete_boot_config(config_id: int):
    """Delete boot configuration.

    Args:
        config_id: Config database ID

    Returns:
        200: Boot config deleted
        400: Config is in use
        404: Config not found
    """
    config = _get_boot_config_by_id(config_id)

    if not config:
        return jsonify({"error": f"Boot config not found: {config_id}"}), 404

    db = get_db()

    # Check if config is in use
    machines_using = db(db.ipxe_machines.boot_config_id == config_id).count()
    if machines_using > 0:
        return jsonify({
            "error": f"Boot config is in use by {machines_using} machines",
            "config_id": config_id
        }), 400

    # Delete boot config
    db(db.ipxe_boot_configs.id == config_id).delete()
    db.commit()

    log.info(f"Boot config deleted: {config['name']}")

    return jsonify({
        "message": "Boot config deleted",
        "config_id": config_id
    }), 200


@ipxe_bp.route("/boot-configs/<int:config_id>/preview", methods=["GET"])
@auth_required
async def preview_boot_config(config_id: int):
    """Preview rendered iPXE boot script for configuration.

    Args:
        config_id: Config database ID

    Returns:
        200: Rendered iPXE script
        404: Config not found
    """
    config = _get_boot_config_by_id(config_id)

    if not config:
        return jsonify({"error": f"Boot config not found: {config_id}"}), 404

    # Get default image if set
    image = None
    if config.get("default_image_id"):
        image = _get_image_by_id(config["default_image_id"])

    # Build preview script
    script_lines = ["#!ipxe", ""]

    if config.get("ipxe_script"):
        # Use custom script if provided
        script_lines.append(config["ipxe_script"])
    elif image:
        # Generate default script from image
        script_lines.extend([
            f"# Boot configuration: {config['name']}",
            f"# Image: {image['display_name']} ({image['os_name']} {image['os_version']})",
            "",
            f"set timeout {config.get('timeout_seconds', 30)}",
            "",
            f"kernel {image['kernel_path']} {image.get('kernel_params', '')} {config.get('kernel_params', '')}",
            f"initrd {image['initrd_path']}",
            "boot"
        ])
    else:
        script_lines.append("# No custom script or default image configured")

    preview_script = "\n".join(script_lines)

    return jsonify({
        "config_id": config_id,
        "config_name": config["name"],
        "preview": preview_script,
        "image": image["name"] if image else None
    }), 200


# =============================================================================
# Elder Integration Endpoints
# =============================================================================

@ipxe_bp.route("/machines/<string:machine_id>/sync-elder", methods=["POST"])
@maintainer_or_admin_required
@auth_required
async def sync_machine_to_elder(machine_id: str):
    """Sync machine data to Elder service.

    Synchronizes a machine's current state with Elder's infrastructure
    registry for unified resource discovery and management.

    Args:
        machine_id: Machine database ID or system_id

    Returns:
        200: Machine synced successfully
        400: Sync failed or invalid machine state
        404: Machine not found
        503: Elder service unavailable
    """
    from ..integrations import get_elder_client

    machine = _get_machine_by_id(machine_id)

    if not machine:
        return jsonify({"error": f"Machine not found: {machine_id}"}), 404

    db = get_db()

    try:
        # Get Elder client
        elder_client = await get_elder_client(db)

        if not elder_client:
            return jsonify({
                "error": "Elder service not configured",
                "message": "Elder integration is not configured"
            }), 503

        # Sync machine
        async with elder_client:
            result = await elder_client.sync_machine(machine)

        # Update last sync timestamp
        db(db.ipxe_machines.id == machine["id"]).update(
            elder_synced_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.commit()

        log.info(f"Machine synced to Elder: {machine['system_id']}")

        return jsonify({
            "message": "Machine synced to Elder",
            "machine_id": machine["system_id"],
            "elder_status": result
        }), 200

    except Exception as e:
        log.error(f"Machine sync to Elder failed: {str(e)}")
        return jsonify({
            "error": "Elder sync failed",
            "details": str(e)
        }), 400


@ipxe_bp.route("/elder/status", methods=["GET"])
@auth_required
async def get_elder_status():
    """Get Elder service status and configuration.

    Returns:
        200: Elder status information
        503: Elder service unavailable or not configured
    """
    from ..integrations import get_elder_client, ElderConnectionError

    db = get_db()

    try:
        # Get Elder client
        elder_client = await get_elder_client(db)

        if not elder_client:
            return jsonify({
                "configured": False,
                "message": "Elder service not configured"
            }), 503

        # Check health
        async with elder_client:
            is_healthy = await elder_client.health_check()

        return jsonify({
            "configured": True,
            "healthy": is_healthy,
            "url": elder_client.elder_url,
            "status": "healthy" if is_healthy else "unhealthy"
        }), 200

    except ElderConnectionError as e:
        log.warning(f"Elder health check failed: {str(e)}")
        return jsonify({
            "configured": True,
            "healthy": False,
            "error": str(e),
            "status": "unreachable"
        }), 503

    except Exception as e:
        log.error(f"Elder status check failed: {str(e)}")
        return jsonify({
            "error": "Status check failed",
            "details": str(e)
        }), 400


@ipxe_bp.route("/elder/config", methods=["PUT"])
@admin_required
@auth_required
async def update_elder_config():
    """Update Elder service configuration.

    Request Body:
        elder_url: Elder service URL (required)
        api_key: API key for authentication (required)
        timeout: Request timeout in seconds (optional, default: 10)
        max_retries: Maximum retry attempts (optional, default: 3)
        is_active: Enable/disable Elder integration (optional, default: true)

    Returns:
        200: Configuration updated
        201: Configuration created
        400: Invalid request
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    validation_error = _validate_required_fields(
        data,
        ["elder_url", "api_key"]
    )
    if validation_error:
        return validation_error

    db = get_db()

    # Create elder_config table if it doesn't exist
    if "elder_config" not in db.tables:
        log.error("elder_config table does not exist")
        return jsonify({
            "error": "Elder configuration table not available",
            "message": "Database schema may not be properly initialized"
        }), 400

    # Get or create default configuration
    existing = db(db.elder_config.name == "default").select().first()

    update_fields = {
        "elder_url": data["elder_url"],
        "api_key": data["api_key"],
        "timeout": data.get("timeout", 10),
        "max_retries": data.get("max_retries", 3),
        "is_active": data.get("is_active", True),
        "updated_at": datetime.utcnow()
    }

    if existing:
        # Update existing configuration
        db(db.elder_config.id == existing.id).update(**update_fields)
        db.commit()

        updated = db(db.elder_config.id == existing.id).select().first()
        log.info("Elder configuration updated")

        return jsonify({
            "message": "Configuration updated",
            "config": updated.as_dict()
        }), 200

    else:
        # Create new configuration
        update_fields["name"] = "default"
        config_id = db.elder_config.insert(**update_fields)
        db.commit()

        created = db(db.elder_config.id == config_id).select().first()
        log.info("Elder configuration created")

        return jsonify({
            "message": "Configuration created",
            "config": created.as_dict()
        }), 201
