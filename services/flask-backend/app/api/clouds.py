"""Cloud Provider API Endpoints.

Provides REST API for managing cloud providers and machines.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request
from flask_security import auth_required, roles_required, roles_accepted

from ..clouds import (
    CLOUD_REGISTRY,
    get_cloud_provider,
    list_available_providers,
    CloudError,
    CloudAuthError,
    CloudNotFoundError,
    CloudQuotaError,
    MachineSpec,
)
from ..models import get_db

log = logging.getLogger(__name__)

clouds_bp = Blueprint("clouds", __name__)


# ============================================================================
# Cloud Provider Management
# ============================================================================


@clouds_bp.route("/", methods=["GET"])
@auth_required()
@roles_accepted("admin", "maintainer", "viewer")
def list_providers():
    """List configured cloud providers.

    Returns:
        200: List of configured providers
    """
    db = get_db()

    providers = db(db.cloud_providers).select().as_list()

    # Don't expose sensitive config data to non-admins
    for provider in providers:
        if "config" in provider:
            del provider["config"]

    return jsonify({
        "providers": providers,
        "count": len(providers),
        "available_types": list_available_providers(),
    }), 200


@clouds_bp.route("/", methods=["POST"])
@auth_required()
@roles_required("admin")
def add_provider():
    """Add a new cloud provider.

    Request Body:
        name: Display name for the provider
        provider_type: Type (maas, lxd, aws, gcp, azure, vultr)
        config: Provider-specific configuration
        enabled: Whether provider is enabled (default: true)

    Returns:
        201: Provider created
        400: Invalid request
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    name = data.get("name", "").strip()
    provider_type = data.get("provider_type", "").lower()
    config = data.get("config", {})
    enabled = data.get("enabled", True)

    if not name:
        return jsonify({"error": "Provider name required"}), 400

    if not provider_type:
        return jsonify({"error": "Provider type required"}), 400

    if provider_type not in CLOUD_REGISTRY:
        return jsonify({
            "error": f"Unknown provider type: {provider_type}",
            "available": list(CLOUD_REGISTRY.keys()),
        }), 400

    # Validate by attempting to authenticate
    try:
        provider = get_cloud_provider(provider_type, config)
        provider.authenticate()
    except CloudAuthError as e:
        return jsonify({
            "error": f"Authentication failed: {e}",
            "status": "auth_error",
        }), 400
    except CloudError as e:
        return jsonify({
            "error": f"Provider configuration error: {e}",
            "status": "config_error",
        }), 400

    # Store in database
    db = get_db()

    # Check for duplicate name
    existing = db(db.cloud_providers.name == name).count()
    if existing > 0:
        return jsonify({"error": f"Provider with name '{name}' already exists"}), 409

    provider_id = db.cloud_providers.insert(
        name=name,
        provider_type=provider_type,
        config=config,
        status="connected",
        enabled=enabled,
    )
    db.commit()

    log.info(f"Cloud provider created: {name} ({provider_type})")

    return jsonify({
        "message": "Provider created successfully",
        "provider": {
            "id": provider_id,
            "name": name,
            "provider_type": provider_type,
            "status": "connected",
            "enabled": enabled,
        },
    }), 201


@clouds_bp.route("/<int:provider_id>", methods=["GET"])
@auth_required()
@roles_accepted("admin", "maintainer", "viewer")
def get_provider(provider_id: int):
    """Get provider details.

    Args:
        provider_id: Provider ID

    Returns:
        200: Provider details
        404: Provider not found
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    result = provider.as_dict()

    # Don't expose config to non-admins
    if "config" in result:
        del result["config"]

    return jsonify(result), 200


@clouds_bp.route("/<int:provider_id>", methods=["PUT"])
@auth_required()
@roles_required("admin")
def update_provider(provider_id: int):
    """Update provider configuration.

    Args:
        provider_id: Provider ID

    Request Body:
        name: New display name (optional)
        config: New configuration (optional)
        enabled: Enable/disable (optional)

    Returns:
        200: Provider updated
        404: Provider not found
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    data = request.get_json() or {}

    updates = {}

    if "name" in data:
        updates["name"] = data["name"].strip()

    if "enabled" in data:
        updates["enabled"] = bool(data["enabled"])

    if "config" in data:
        # Validate new config
        new_config = data["config"]
        try:
            cloud = get_cloud_provider(provider.provider_type, new_config)
            cloud.authenticate()
            updates["config"] = new_config
            updates["status"] = "connected"
        except CloudError as e:
            return jsonify({"error": f"Invalid configuration: {e}"}), 400

    if updates:
        db(db.cloud_providers.id == provider_id).update(**updates)
        db.commit()

    return jsonify({"message": "Provider updated successfully"}), 200


@clouds_bp.route("/<int:provider_id>", methods=["DELETE"])
@auth_required()
@roles_required("admin")
def delete_provider(provider_id: int):
    """Delete a cloud provider.

    Args:
        provider_id: Provider ID

    Returns:
        200: Provider deleted
        404: Provider not found
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    # Check for existing machines
    machine_count = db(db.cloud_machines.provider_id == provider_id).count()
    if machine_count > 0:
        return jsonify({
            "error": f"Cannot delete provider with {machine_count} machines",
            "hint": "Delete or migrate machines first",
        }), 409

    db(db.cloud_providers.id == provider_id).delete()
    db.commit()

    log.info(f"Cloud provider deleted: {provider.name}")

    return jsonify({"message": "Provider deleted successfully"}), 200


@clouds_bp.route("/<int:provider_id>/test", methods=["POST"])
@auth_required()
@roles_required("admin")
def test_provider(provider_id: int):
    """Test provider connectivity.

    Args:
        provider_id: Provider ID

    Returns:
        200: Connection successful
        404: Provider not found
        500: Connection failed
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config)
        cloud.authenticate()

        # Update status
        db(db.cloud_providers.id == provider_id).update(status="connected")
        db.commit()

        return jsonify({
            "status": "connected",
            "message": "Connection successful",
        }), 200

    except CloudAuthError as e:
        db(db.cloud_providers.id == provider_id).update(status="auth_error")
        db.commit()
        return jsonify({"status": "auth_error", "error": str(e)}), 401

    except CloudError as e:
        db(db.cloud_providers.id == provider_id).update(status="error")
        db.commit()
        return jsonify({"status": "error", "error": str(e)}), 500


# ============================================================================
# Machine Management
# ============================================================================


@clouds_bp.route("/<int:provider_id>/machines", methods=["GET"])
@auth_required()
@roles_accepted("admin", "maintainer", "viewer")
def list_machines(provider_id: int):
    """List machines for a provider.

    Args:
        provider_id: Provider ID

    Query Parameters:
        refresh: If true, refresh from cloud API

    Returns:
        200: List of machines
        404: Provider not found
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    refresh = request.args.get("refresh", "").lower() == "true"

    if refresh:
        # Fetch from cloud API
        try:
            cloud = get_cloud_provider(provider.provider_type, provider.config)
            cloud.authenticate()
            machines = cloud.list_machines()

            # Sync to database
            _sync_machines_to_db(db, provider_id, machines)

            return jsonify({
                "machines": [m.to_dict() for m in machines],
                "count": len(machines),
                "source": "cloud_api",
            }), 200

        except CloudError as e:
            log.error(f"Error listing machines from cloud: {e}")
            return jsonify({"error": str(e)}), 500

    # Return from database
    machines = db(db.cloud_machines.provider_id == provider_id).select().as_list()

    return jsonify({
        "machines": machines,
        "count": len(machines),
        "source": "database",
    }), 200


@clouds_bp.route("/<int:provider_id>/machines", methods=["POST"])
@auth_required()
@roles_accepted("admin", "maintainer")
def create_machine(provider_id: int):
    """Create a new machine.

    Args:
        provider_id: Provider ID

    Request Body:
        name: Machine name
        image: OS image
        size: Instance size
        region: Region/zone (optional)
        cloud_init: Cloud-init user data (optional)
        ssh_keys: List of SSH public keys (optional)
        tags: Tags/labels (optional)

    Returns:
        201: Machine created
        404: Provider not found
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    if not provider.enabled:
        return jsonify({"error": "Provider is disabled"}), 400

    data = request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    # Build machine spec
    try:
        spec = MachineSpec(
            name=data.get("name", ""),
            image=data.get("image", ""),
            size=data.get("size", ""),
            region=data.get("region", ""),
            cloud_init=data.get("cloud_init", ""),
            ssh_keys=data.get("ssh_keys", []),
            tags=data.get("tags", {}),
            extra=data.get("extra", {}),
        )
    except Exception as e:
        return jsonify({"error": f"Invalid machine spec: {e}"}), 400

    if not spec.name:
        return jsonify({"error": "Machine name required"}), 400

    if not spec.image:
        return jsonify({"error": "Image required"}), 400

    if not spec.size:
        return jsonify({"error": "Size required"}), 400

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config)
        cloud.authenticate()
        machine = cloud.create_machine(spec)

        # Store in database
        machine_id = db.cloud_machines.insert(
            provider_id=provider_id,
            cloud_id=machine.id,
            name=machine.name,
            state=machine.state.value,
            region=machine.region,
            image=machine.image,
            size=machine.size,
            public_ips=machine.public_ips,
            private_ips=machine.private_ips,
            tags=machine.tags,
            extra=machine.extra,
        )
        db.commit()

        log.info(f"Machine created: {machine.name} on {provider.name}")

        result = machine.to_dict()
        result["db_id"] = machine_id

        return jsonify(result), 201

    except CloudQuotaError as e:
        return jsonify({"error": f"Quota exceeded: {e}"}), 429

    except CloudError as e:
        log.error(f"Error creating machine: {e}")
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/machines/<machine_id>", methods=["GET"])
@auth_required()
@roles_accepted("admin", "maintainer", "viewer")
def get_machine(provider_id: int, machine_id: str):
    """Get machine details.

    Args:
        provider_id: Provider ID
        machine_id: Machine ID (cloud provider ID)

    Returns:
        200: Machine details
        404: Machine not found
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config)
        cloud.authenticate()
        machine = cloud.get_machine(machine_id)

        return jsonify(machine.to_dict()), 200

    except CloudNotFoundError:
        return jsonify({"error": "Machine not found"}), 404

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/machines/<machine_id>", methods=["DELETE"])
@auth_required()
@roles_accepted("admin", "maintainer")
def destroy_machine(provider_id: int, machine_id: str):
    """Destroy a machine.

    Args:
        provider_id: Provider ID
        machine_id: Machine ID

    Returns:
        200: Machine destroyed
        404: Machine not found
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config)
        cloud.authenticate()
        cloud.destroy_machine(machine_id)

        # Remove from database
        db(
            (db.cloud_machines.provider_id == provider_id) & (
                db.cloud_machines.cloud_id == machine_id)
        ).delete()
        db.commit()

        log.info(f"Machine destroyed: {machine_id} on {provider.name}")

        return jsonify({"message": "Machine destroyed successfully"}), 200

    except CloudNotFoundError:
        return jsonify({"error": "Machine not found"}), 404

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/machines/<machine_id>/start", methods=["POST"])
@auth_required()
@roles_accepted("admin", "maintainer")
def start_machine(provider_id: int, machine_id: str):
    """Start a stopped machine.

    Args:
        provider_id: Provider ID
        machine_id: Machine ID

    Returns:
        200: Machine started
        404: Machine not found
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config)
        cloud.authenticate()
        cloud.start_machine(machine_id)

        # Update database
        db(
            (db.cloud_machines.provider_id == provider_id) & (
                db.cloud_machines.cloud_id == machine_id)
        ).update(state="running")
        db.commit()

        return jsonify({"message": "Machine started"}), 200

    except CloudNotFoundError:
        return jsonify({"error": "Machine not found"}), 404

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/machines/<machine_id>/stop", methods=["POST"])
@auth_required()
@roles_accepted("admin", "maintainer")
def stop_machine(provider_id: int, machine_id: str):
    """Stop a running machine.

    Args:
        provider_id: Provider ID
        machine_id: Machine ID

    Returns:
        200: Machine stopped
        404: Machine not found
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config)
        cloud.authenticate()
        cloud.stop_machine(machine_id)

        # Update database
        db(
            (db.cloud_machines.provider_id == provider_id) & (
                db.cloud_machines.cloud_id == machine_id)
        ).update(state="stopped")
        db.commit()

        return jsonify({"message": "Machine stopped"}), 200

    except CloudNotFoundError:
        return jsonify({"error": "Machine not found"}), 404

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/machines/<machine_id>/reboot", methods=["POST"])
@auth_required()
@roles_accepted("admin", "maintainer")
def reboot_machine(provider_id: int, machine_id: str):
    """Reboot a machine.

    Args:
        provider_id: Provider ID
        machine_id: Machine ID

    Returns:
        200: Machine rebooted
        404: Machine not found
    """
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config)
        cloud.authenticate()
        cloud.reboot_machine(machine_id)

        return jsonify({"message": "Machine rebooted"}), 200

    except CloudNotFoundError:
        return jsonify({"error": "Machine not found"}), 404

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Provider Resources (Images, Sizes, Regions)
# ============================================================================


@clouds_bp.route("/<int:provider_id>/images", methods=["GET"])
@auth_required()
@roles_accepted("admin", "maintainer", "viewer")
def list_images(provider_id: int):
    """List available images for a provider."""
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config)
        cloud.authenticate()
        images = cloud.list_images()

        return jsonify({"images": images}), 200

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/sizes", methods=["GET"])
@auth_required()
@roles_accepted("admin", "maintainer", "viewer")
def list_sizes(provider_id: int):
    """List available machine sizes for a provider."""
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config)
        cloud.authenticate()
        sizes = cloud.list_sizes()

        return jsonify({"sizes": sizes}), 200

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/regions", methods=["GET"])
@auth_required()
@roles_accepted("admin", "maintainer", "viewer")
def list_regions(provider_id: int):
    """List available regions for a provider."""
    db = get_db()

    provider = db(db.cloud_providers.id == provider_id).select().first()

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config)
        cloud.authenticate()
        regions = cloud.list_regions()

        return jsonify({"regions": regions}), 200

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Helper Functions
# ============================================================================


def _sync_machines_to_db(db, provider_id: int, machines: list) -> None:
    """Sync machines from cloud API to database."""
    # Get existing machines
    existing = {
        m.cloud_id: m.id
        for m in db(db.cloud_machines.provider_id == provider_id).select()
    }

    cloud_ids = set()

    for machine in machines:
        cloud_ids.add(machine.id)

        if machine.id in existing:
            # Update existing
            db(db.cloud_machines.id == existing[machine.id]).update(
                name=machine.name,
                state=machine.state.value,
                region=machine.region,
                public_ips=machine.public_ips,
                private_ips=machine.private_ips,
                tags=machine.tags,
            )
        else:
            # Insert new
            db.cloud_machines.insert(
                provider_id=provider_id,
                cloud_id=machine.id,
                name=machine.name,
                state=machine.state.value,
                region=machine.region,
                image=machine.image,
                size=machine.size,
                public_ips=machine.public_ips,
                private_ips=machine.private_ips,
                tags=machine.tags,
                extra=machine.extra,
            )

    # Remove machines that no longer exist in cloud
    for cloud_id in existing:
        if cloud_id not in cloud_ids:
            db(
                (db.cloud_machines.provider_id == provider_id) & (
                    db.cloud_machines.cloud_id == cloud_id)
            ).delete()

    db.commit()
