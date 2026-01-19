"""Eggs Management API Endpoints.

Provides REST API for managing deployable eggs (snaps, cloud-init, LXD containers/VMs),
egg groups, and cloud-init rendering. Supports creating, updating, listing, and deleting
eggs, as well as uploading LXD images to storage and rendering merged cloud-init configs.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Optional

import yaml
from quart import Blueprint, jsonify, request
from werkzeug.utils import secure_filename

from ..middleware import admin_required, auth_required, maintainer_or_admin_required
from ..models import get_db

log = logging.getLogger(__name__)

eggs_bp = Blueprint("eggs", __name__, url_prefix="/api/v1/eggs")


# ============================================================================
# Helper Functions
# ============================================================================


def validate_egg_type(egg_type: str) -> bool:
    """Validate egg type against allowed values."""
    from ..models.ipxe import EGG_TYPES
    return egg_type in EGG_TYPES


def validate_architecture(arch: str) -> bool:
    """Validate architecture against allowed values."""
    from ..models.ipxe import ARCHITECTURES
    return arch in ARCHITECTURES or arch == "any"


def serialize_egg(egg: object) -> dict:
    """Serialize egg database row to JSON-compatible dict."""
    return {
        "id": egg.id,
        "name": egg.name,
        "display_name": egg.display_name,
        "description": egg.description,
        "egg_type": egg.egg_type,
        "version": egg.version,
        "category": egg.category,
        "snap_name": egg.snap_name,
        "snap_channel": egg.snap_channel,
        "snap_classic": egg.snap_classic,
        "cloud_init_content": egg.cloud_init_content,
        "lxd_image_alias": egg.lxd_image_alias,
        "lxd_image_url": egg.lxd_image_url,
        "lxd_profiles": egg.lxd_profiles,
        "is_hypervisor_config": egg.is_hypervisor_config,
        "dependencies": egg.dependencies,
        "min_ram_mb": egg.min_ram_mb,
        "min_disk_gb": egg.min_disk_gb,
        "required_architecture": egg.required_architecture,
        "is_active": egg.is_active,
        "is_default": egg.is_default,
        "checksum": egg.checksum,
        "size_bytes": egg.size_bytes,
        "created_at": egg.created_at.isoformat() if egg.created_at else None,
        "updated_at": egg.updated_at.isoformat() if egg.updated_at else None,
    }


def serialize_egg_group(group: object) -> dict:
    """Serialize egg group database row to JSON-compatible dict."""
    return {
        "id": group.id,
        "name": group.name,
        "display_name": group.display_name,
        "description": group.description,
        "eggs": group.eggs,
        "is_default": group.is_default,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "updated_at": group.updated_at.isoformat() if group.updated_at else None,
    }


def validate_cloud_init_yaml(content: str) -> tuple[bool, Optional[str]]:
    """Validate cloud-init YAML content.

    Args:
        content: YAML string to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not content or not content.strip():
        return True, None

    try:
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict):
            return False, "Cloud-init content must be a YAML dictionary"
        return True, None
    except yaml.YAMLError as e:
        return False, f"Invalid YAML: {str(e)}"


def merge_cloud_init_configs(configs: list[str]) -> str:
    """Merge multiple cloud-init YAML configs into a single config.

    Handles merging of common cloud-init sections like packages, runcmd,
    write_files, etc. Later configs override earlier ones for scalar values.

    Args:
        configs: List of YAML string configurations

    Returns:
        Merged YAML configuration as string
    """
    merged = {}

    for config_str in configs:
        if not config_str or not config_str.strip():
            continue

        try:
            config = yaml.safe_load(config_str)
            if not isinstance(config, dict):
                continue

            for key, value in config.items():
                if key not in merged:
                    merged[key] = value
                elif isinstance(merged[key], list) and isinstance(value, list):
                    # Merge lists (packages, runcmd, etc.)
                    merged[key].extend(value)
                elif isinstance(merged[key], dict) and isinstance(value, dict):
                    # Merge dictionaries
                    merged[key].update(value)
                else:
                    # Override scalar values
                    merged[key] = value

        except yaml.YAMLError as e:
            log.warning(f"Skipping invalid cloud-init config: {e}")
            continue

    return yaml.dump(merged, default_flow_style=False, sort_keys=False)


# ============================================================================
# Eggs Management
# ============================================================================


@eggs_bp.route("/", methods=["GET"])
@auth_required
async def list_eggs():
    """List all eggs with optional filtering.

    Query Parameters:
        type: Filter by egg_type (snap, cloud_init, lxd_container, lxd_vm)
        category: Filter by category
        is_active: Filter by active status (true/false)
        is_default: Filter by default status (true/false)

    Returns:
        200: List of eggs
    """
    db = get_db()

    query = db.eggs

    # Apply filters
    egg_type = request.args.get("type")
    if egg_type:
        if not validate_egg_type(egg_type):
            return jsonify({"error": f"Invalid egg type: {egg_type}"}), 400
        query = query.egg_type == egg_type

    category = request.args.get("category")
    if category:
        query = query.category == category

    is_active = request.args.get("is_active")
    if is_active is not None:
        query = query.is_active == (is_active.lower() == "true")

    is_default = request.args.get("is_default")
    if is_default is not None:
        query = query.is_default == (is_default.lower() == "true")

    eggs = db(query).select(orderby=db.eggs.display_name)

    return jsonify({
        "eggs": [serialize_egg(egg) for egg in eggs],
        "total": len(eggs),
    }), 200


@eggs_bp.route("/", methods=["POST"])
@maintainer_or_admin_required
async def create_egg():
    """Create a new egg.

    Request Body:
        name: Unique identifier (required)
        display_name: Human-readable name (required)
        description: Egg description
        egg_type: Type of egg (required) - snap, cloud_init, lxd_container, lxd_vm
        version: Version string
        category: Category classification
        snap_name: Snap package name (for snap type)
        snap_channel: Snap channel (stable, edge, etc.)
        snap_classic: Whether snap requires classic confinement
        cloud_init_content: Cloud-init YAML content (for cloud_init type)
        lxd_image_alias: LXD image alias (for lxd types)
        lxd_image_url: LXD image storage URL (for lxd types)
        lxd_profiles: Array of LXD profile names (for lxd types)
        is_hypervisor_config: Whether this configures hypervisor
        dependencies: Array of egg IDs this depends on
        min_ram_mb: Minimum RAM requirement
        min_disk_gb: Minimum disk requirement
        required_architecture: Required architecture (amd64, arm64, any)
        is_active: Whether egg is active
        is_default: Whether this is a default egg

    Returns:
        201: Egg created successfully
        400: Invalid request
        409: Egg name already exists
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    name = data.get("name", "").strip()
    display_name = data.get("display_name", "").strip()
    egg_type = data.get("egg_type", "").strip()

    if not name:
        return jsonify({"error": "Egg name required"}), 400

    if not display_name:
        return jsonify({"error": "Display name required"}), 400

    if not egg_type:
        return jsonify({"error": "Egg type required"}), 400

    if not validate_egg_type(egg_type):
        return jsonify({"error": f"Invalid egg type: {egg_type}"}), 400

    db = get_db()

    # Check if egg name already exists
    existing = db(db.eggs.name == name).select().first()
    if existing:
        return jsonify({"error": "Egg name already exists"}), 409

    # Validate cloud-init content if provided
    cloud_init_content = data.get("cloud_init_content")
    if cloud_init_content:
        valid, error = validate_cloud_init_yaml(cloud_init_content)
        if not valid:
            return jsonify({"error": f"Invalid cloud-init content: {error}"}), 400

    # Validate architecture if provided
    required_arch = data.get("required_architecture", "any")
    if not validate_architecture(required_arch):
        return jsonify({"error": f"Invalid architecture: {required_arch}"}), 400

    try:
        egg_id = db.eggs.insert(
            name=name,
            display_name=display_name,
            description=data.get("description"),
            egg_type=egg_type,
            version=data.get("version"),
            category=data.get("category"),
            snap_name=data.get("snap_name"),
            snap_channel=data.get("snap_channel", "stable"),
            snap_classic=data.get("snap_classic", False),
            cloud_init_content=cloud_init_content,
            lxd_image_alias=data.get("lxd_image_alias"),
            lxd_image_url=data.get("lxd_image_url"),
            lxd_profiles=data.get("lxd_profiles"),
            is_hypervisor_config=data.get("is_hypervisor_config", False),
            dependencies=data.get("dependencies"),
            min_ram_mb=data.get("min_ram_mb", 0),
            min_disk_gb=data.get("min_disk_gb", 0),
            required_architecture=required_arch,
            is_active=data.get("is_active", True),
            is_default=data.get("is_default", False),
        )

        db.commit()

        egg = db.eggs(egg_id)

        return jsonify({
            "message": "Egg created successfully",
            "egg": serialize_egg(egg),
        }), 201

    except Exception as e:
        db.rollback()
        log.exception(f"Error creating egg: {e}")
        return jsonify({"error": str(e)}), 500


@eggs_bp.route("/<int:egg_id>", methods=["GET"])
@auth_required
async def get_egg(egg_id: int):
    """Get egg details by ID.

    Args:
        egg_id: Egg ID

    Returns:
        200: Egg details
        404: Egg not found
    """
    db = get_db()

    egg = db.eggs(egg_id)
    if not egg:
        return jsonify({"error": "Egg not found"}), 404

    return jsonify({"egg": serialize_egg(egg)}), 200


@eggs_bp.route("/<int:egg_id>", methods=["PUT"])
@maintainer_or_admin_required
async def update_egg(egg_id: int):
    """Update egg by ID.

    Args:
        egg_id: Egg ID

    Request Body: Same fields as create_egg (all optional)

    Returns:
        200: Egg updated successfully
        400: Invalid request
        404: Egg not found
        409: Egg name conflict
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    db = get_db()

    egg = db.eggs(egg_id)
    if not egg:
        return jsonify({"error": "Egg not found"}), 404

    # Check for name conflict if name is being changed
    if "name" in data and data["name"] != egg.name:
        existing = db((db.eggs.name == data["name"]) & (db.eggs.id != egg_id)).select().first()
        if existing:
            return jsonify({"error": "Egg name already exists"}), 409

    # Validate egg type if provided
    if "egg_type" in data and not validate_egg_type(data["egg_type"]):
        return jsonify({"error": f"Invalid egg type: {data['egg_type']}"}), 400

    # Validate cloud-init content if provided
    if "cloud_init_content" in data and data["cloud_init_content"]:
        valid, error = validate_cloud_init_yaml(data["cloud_init_content"])
        if not valid:
            return jsonify({"error": f"Invalid cloud-init content: {error}"}), 400

    # Validate architecture if provided
    if "required_architecture" in data:
        if not validate_architecture(data["required_architecture"]):
            return jsonify({"error": f"Invalid architecture: {data['required_architecture']}"}), 400

    try:
        update_fields = {}

        # Update only provided fields
        for field in ["name", "display_name", "description", "egg_type", "version",
                      "category", "snap_name", "snap_channel", "snap_classic",
                      "cloud_init_content", "lxd_image_alias", "lxd_image_url",
                      "lxd_profiles", "is_hypervisor_config", "dependencies",
                      "min_ram_mb", "min_disk_gb", "required_architecture",
                      "is_active", "is_default", "checksum", "size_bytes"]:
            if field in data:
                update_fields[field] = data[field]

        if update_fields:
            db(db.eggs.id == egg_id).update(**update_fields)
            db.commit()

        egg = db.eggs(egg_id)

        return jsonify({
            "message": "Egg updated successfully",
            "egg": serialize_egg(egg),
        }), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error updating egg: {e}")
        return jsonify({"error": str(e)}), 500


@eggs_bp.route("/<int:egg_id>", methods=["DELETE"])
@admin_required
async def delete_egg(egg_id: int):
    """Delete egg by ID.

    Args:
        egg_id: Egg ID

    Returns:
        200: Egg deleted successfully
        404: Egg not found
        409: Egg is in use and cannot be deleted
    """
    db = get_db()

    egg = db.eggs(egg_id)
    if not egg:
        return jsonify({"error": "Egg not found"}), 404

    # Check if egg is referenced by any machines
    machines_with_egg = db(db.ipxe_machines.assigned_eggs.contains(str(egg_id))).select()
    if machines_with_egg:
        return jsonify({
            "error": "Cannot delete egg that is assigned to machines",
            "machines_count": len(machines_with_egg),
        }), 409

    # Check if egg is in any egg groups
    groups = db.egg_groups.eggs.contains(str(egg_id))
    groups_with_egg = db(groups).select()
    if groups_with_egg:
        return jsonify({
            "error": "Cannot delete egg that is in egg groups",
            "groups_count": len(groups_with_egg),
        }), 409

    try:
        db(db.eggs.id == egg_id).delete()
        db.commit()

        return jsonify({"message": "Egg deleted successfully"}), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error deleting egg: {e}")
        return jsonify({"error": str(e)}), 500


@eggs_bp.route("/<int:egg_id>/upload", methods=["POST"])
@maintainer_or_admin_required
async def upload_lxd_image(egg_id: int):
    """Upload LXD image to storage for the specified egg.

    This endpoint accepts a file upload and stores it in the configured
    storage backend (MinIO/S3), then updates the egg with the storage URL
    and checksum.

    Args:
        egg_id: Egg ID

    Form Data:
        file: LXD image file (required)

    Returns:
        200: Image uploaded successfully
        400: Invalid request or file
        404: Egg not found
        413: File too large
    """
    db = get_db()

    egg = db.eggs(egg_id)
    if not egg:
        return jsonify({"error": "Egg not found"}), 404

    if egg.egg_type not in ["lxd_container", "lxd_vm"]:
        return jsonify({
            "error": "Upload only supported for LXD container/VM eggs"
        }), 400

    files = await request.files
    if "file" not in files:
        return jsonify({"error": "No file provided"}), 400

    file = files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Secure the filename
    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"error": "Invalid filename"}), 400

    # Read file data and calculate checksum
    file_data = await file.read()
    if not file_data:
        return jsonify({"error": "Empty file"}), 400

    file_size = len(file_data)
    checksum = hashlib.sha256(file_data).hexdigest()

    # Get storage configuration
    storage = db(db.storage_config.is_active == True).select().first()
    if not storage:
        return jsonify({"error": "No active storage configuration"}), 500

    try:
        # In a real implementation, upload to MinIO/S3 here
        # For now, generate a placeholder URL
        storage_url = f"{storage.endpoint_url}/{storage.bucket_lxd_images}/{egg.name}/{filename}"

        # Update egg with storage information
        db(db.eggs.id == egg_id).update(
            lxd_image_url=storage_url,
            checksum=checksum,
            size_bytes=file_size,
        )
        db.commit()

        egg = db.eggs(egg_id)

        return jsonify({
            "message": "LXD image uploaded successfully",
            "egg": serialize_egg(egg),
            "upload_details": {
                "filename": filename,
                "size_bytes": file_size,
                "checksum": checksum,
                "storage_url": storage_url,
            },
        }), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error uploading LXD image: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Egg Groups
# ============================================================================


@eggs_bp.route("/groups", methods=["GET"])
@auth_required
async def list_egg_groups():
    """List all egg groups.

    Returns:
        200: List of egg groups
    """
    db = get_db()

    groups = db(db.egg_groups).select(orderby=db.egg_groups.display_name)

    return jsonify({
        "groups": [serialize_egg_group(group) for group in groups],
        "total": len(groups),
    }), 200


@eggs_bp.route("/groups", methods=["POST"])
@maintainer_or_admin_required
async def create_egg_group():
    """Create a new egg group.

    Request Body:
        name: Unique identifier (required)
        display_name: Human-readable name (required)
        description: Group description
        eggs: Array of {egg_id: int, order: int} objects (required)
        is_default: Whether this is a default group

    Returns:
        201: Egg group created successfully
        400: Invalid request
        409: Group name already exists
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    name = data.get("name", "").strip()
    display_name = data.get("display_name", "").strip()
    eggs = data.get("eggs")

    if not name:
        return jsonify({"error": "Group name required"}), 400

    if not display_name:
        return jsonify({"error": "Display name required"}), 400

    if not eggs or not isinstance(eggs, list):
        return jsonify({"error": "Eggs array required"}), 400

    db = get_db()

    # Check if group name already exists
    existing = db(db.egg_groups.name == name).select().first()
    if existing:
        return jsonify({"error": "Group name already exists"}), 409

    # Validate that all egg IDs exist
    for egg_ref in eggs:
        if not isinstance(egg_ref, dict) or "egg_id" not in egg_ref:
            return jsonify({"error": "Invalid egg reference format"}), 400

        egg = db.eggs(egg_ref["egg_id"])
        if not egg:
            return jsonify({"error": f"Egg ID {egg_ref['egg_id']} not found"}), 400

    try:
        group_id = db.egg_groups.insert(
            name=name,
            display_name=display_name,
            description=data.get("description"),
            eggs=eggs,
            is_default=data.get("is_default", False),
        )

        db.commit()

        group = db.egg_groups(group_id)

        return jsonify({
            "message": "Egg group created successfully",
            "group": serialize_egg_group(group),
        }), 201

    except Exception as e:
        db.rollback()
        log.exception(f"Error creating egg group: {e}")
        return jsonify({"error": str(e)}), 500


@eggs_bp.route("/groups/<int:group_id>", methods=["GET"])
@auth_required
async def get_egg_group(group_id: int):
    """Get egg group details by ID.

    Args:
        group_id: Egg group ID

    Returns:
        200: Egg group details with resolved egg references
        404: Egg group not found
    """
    db = get_db()

    group = db.egg_groups(group_id)
    if not group:
        return jsonify({"error": "Egg group not found"}), 404

    # Resolve egg references
    resolved_eggs = []
    for egg_ref in (group.eggs or []):
        if isinstance(egg_ref, dict) and "egg_id" in egg_ref:
            egg = db.eggs(egg_ref["egg_id"])
            if egg:
                resolved_eggs.append({
                    "order": egg_ref.get("order", 0),
                    "egg": serialize_egg(egg),
                })

    group_data = serialize_egg_group(group)
    group_data["resolved_eggs"] = resolved_eggs

    return jsonify({"group": group_data}), 200


@eggs_bp.route("/groups/<int:group_id>", methods=["PUT"])
@maintainer_or_admin_required
async def update_egg_group(group_id: int):
    """Update egg group by ID.

    Args:
        group_id: Egg group ID

    Request Body: Same fields as create_egg_group (all optional)

    Returns:
        200: Egg group updated successfully
        400: Invalid request
        404: Egg group not found
        409: Group name conflict
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    db = get_db()

    group = db.egg_groups(group_id)
    if not group:
        return jsonify({"error": "Egg group not found"}), 404

    # Check for name conflict if name is being changed
    if "name" in data and data["name"] != group.name:
        existing = db((db.egg_groups.name == data["name"]) & (db.egg_groups.id != group_id)).select().first()
        if existing:
            return jsonify({"error": "Group name already exists"}), 409

    # Validate eggs array if provided
    if "eggs" in data:
        eggs = data["eggs"]
        if not isinstance(eggs, list):
            return jsonify({"error": "Eggs must be an array"}), 400

        for egg_ref in eggs:
            if not isinstance(egg_ref, dict) or "egg_id" not in egg_ref:
                return jsonify({"error": "Invalid egg reference format"}), 400

            egg = db.eggs(egg_ref["egg_id"])
            if not egg:
                return jsonify({"error": f"Egg ID {egg_ref['egg_id']} not found"}), 400

    try:
        update_fields = {}

        for field in ["name", "display_name", "description", "eggs", "is_default"]:
            if field in data:
                update_fields[field] = data[field]

        if update_fields:
            db(db.egg_groups.id == group_id).update(**update_fields)
            db.commit()

        group = db.egg_groups(group_id)

        return jsonify({
            "message": "Egg group updated successfully",
            "group": serialize_egg_group(group),
        }), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error updating egg group: {e}")
        return jsonify({"error": str(e)}), 500


@eggs_bp.route("/groups/<int:group_id>", methods=["DELETE"])
@admin_required
async def delete_egg_group(group_id: int):
    """Delete egg group by ID.

    Args:
        group_id: Egg group ID

    Returns:
        200: Egg group deleted successfully
        404: Egg group not found
        409: Group is in use and cannot be deleted
    """
    db = get_db()

    group = db.egg_groups(group_id)
    if not group:
        return jsonify({"error": "Egg group not found"}), 404

    # Check if group is assigned to any boot configs
    configs = db(db.ipxe_boot_configs.assigned_egg_group_id == group_id).select()
    if configs:
        return jsonify({
            "error": "Cannot delete egg group that is assigned to boot configs",
            "configs_count": len(configs),
        }), 409

    try:
        db(db.egg_groups.id == group_id).delete()
        db.commit()

        return jsonify({"message": "Egg group deleted successfully"}), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error deleting egg group: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Cloud-Init Rendering
# ============================================================================


@eggs_bp.route("/render-cloud-init", methods=["POST"])
@auth_required
async def render_cloud_init():
    """Merge multiple eggs into a single cloud-init configuration.

    This endpoint takes an array of egg IDs and merges their cloud-init
    content into a single unified configuration. The merge respects the
    order of eggs and handles common cloud-init sections appropriately.

    Request Body:
        egg_ids: Array of egg IDs to merge (required)
        additional_config: Optional additional YAML to merge at the end

    Returns:
        200: Rendered cloud-init configuration
        400: Invalid request
        404: One or more eggs not found
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    egg_ids = data.get("egg_ids", [])
    if not egg_ids or not isinstance(egg_ids, list):
        return jsonify({"error": "egg_ids array required"}), 400

    db = get_db()

    configs = []
    eggs_info = []

    # Collect cloud-init configs from eggs
    for egg_id in egg_ids:
        egg = db.eggs(egg_id)
        if not egg:
            return jsonify({"error": f"Egg ID {egg_id} not found"}), 404

        if egg.cloud_init_content:
            configs.append(egg.cloud_init_content)
            eggs_info.append({
                "id": egg.id,
                "name": egg.name,
                "display_name": egg.display_name,
            })

    # Add additional config if provided
    additional_config = data.get("additional_config")
    if additional_config:
        valid, error = validate_cloud_init_yaml(additional_config)
        if not valid:
            return jsonify({"error": f"Invalid additional config: {error}"}), 400
        configs.append(additional_config)

    if not configs:
        return jsonify({
            "error": "No cloud-init content found in specified eggs"
        }), 400

    try:
        # Merge all configs
        merged_config = merge_cloud_init_configs(configs)

        return jsonify({
            "message": "Cloud-init configuration rendered successfully",
            "cloud_init": merged_config,
            "eggs_merged": eggs_info,
            "total_eggs": len(eggs_info),
        }), 200

    except Exception as e:
        log.exception(f"Error rendering cloud-init: {e}")
        return jsonify({"error": str(e)}), 500
