"""Cloud Provider API Endpoints.

Provides REST API for managing cloud providers and machines.
"""

from __future__ import annotations

import logging
from typing import Any

from quart import Blueprint, jsonify, request

from ..clouds import (
    CLOUD_REGISTRY,
    CloudAuthError,
    CloudError,
    CloudNotFoundError,
    CloudQuotaError,
    MachineSpec,
    get_cloud_provider,
    list_available_providers,
)
from ..db.run_db import run_db
from ..licensing import (
    FLAG_MULTI_CLOUD,
    count_active_nodes,
    feature_enabled,
    node_allowance,
    stamp_managed_tag,
)
from ..middleware import auth_required, roles_accepted, roles_required
from ..models import get_db
from ._helpers import err_feature_disabled, err_license_required

log = logging.getLogger(__name__)

clouds_bp = Blueprint("clouds", __name__)


@clouds_bp.before_request
async def _gate_multi_cloud():
    """Switch the whole cloud-provider surface off behind its feature flag.

    Blueprint-wide rather than per-route so a route added later cannot forget
    the gate. Returning None lets the request proceed; returning a response
    short-circuits it.

    Defaults OFF when PostHog is unconfigured or unreachable with nothing
    cached, per the "new flags default OFF until validated" rule -- so a
    deployment that has never configured POSTHOG_KEY sees /api/v1/clouds/*
    as 404 until the flag is switched on.
    """
    if await feature_enabled(FLAG_MULTI_CLOUD):
        return None
    return err_feature_disabled(
        "Cloud provider management is not enabled for this deployment.",
        details={"flag": FLAG_MULTI_CLOUD},
    )


# ============================================================================
# Cloud Provider Management
# ============================================================================


#: Provider fields safe to return over the API. An explicit allow-list rather
#: than "the row minus a few keys": the previous redaction deleted a key named
#: "config", which the reflected row does not have (the column is
#: ``config_data``), so it removed nothing and every response carried the
#: provider's credentials. A projection cannot fail that way -- a column added
#: later is excluded until someone lists it here. Both ``config_data`` (the
#: credentials themselves) and ``credentials_path`` (where they live on disk)
#: are deliberately absent. See security.md "Output Validation (Response Shape)".
_PROVIDER_PUBLIC_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "provider_type",
    "description",
    "region",
    "status",
    "is_active",
    "last_sync_at",
    "created_at",
    "updated_at",
)


def _provider_public(row: Any) -> dict[str, Any]:
    """Project a ``cloud_providers`` row onto its API-safe fields."""
    data = row if isinstance(row, dict) else row.as_dict()
    return {k: data[k] for k in _PROVIDER_PUBLIC_FIELDS if k in data}


@clouds_bp.route("/", methods=["GET"])
@auth_required
@roles_accepted("admin", "maintainer", "viewer")
async def list_providers():
    """List configured cloud providers.

    Returns:
        200: List of configured providers
    """
    db = get_db()

    def _fetch_providers() -> Any:
        # ``db(table)`` is not a penguin-dal query -- it reaches for
        # ``.clause`` on the table and raises. ``id > 0`` is the house
        # select-all idiom (see app/api/ssh_ca.py, biomes.py).
        return db(db.cloud_providers.id > 0).select().as_list()

    providers = await run_db(_fetch_providers)

    # Project onto API-safe fields -- never return the raw row.
    providers = [_provider_public(p) for p in providers]

    return jsonify({
        "providers": providers,
        "count": len(providers),
        "available_types": list_available_providers(),
    }), 200


@clouds_bp.route("/", methods=["POST"])
@auth_required
@roles_required("admin")
async def add_provider():
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
    data = await request.get_json()

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

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _check_name_exists() -> int:
        return db(db.cloud_providers.name == name).count()

    existing = await run_db(_check_name_exists)
    if existing > 0:
        return jsonify({"error": f"Provider with name '{name}' already exists"}), 409

    # Regression: gh-22. insert + commit is one unit of work -- a
    # penguin-dal connection checkout isn't safe to resume on a different
    # thread hop, so both stay inside one run_db() closure.
    def _insert_provider() -> Any:
        new_id = db.cloud_providers.insert(
            name=name,
            provider_type=provider_type,
            config_data=config,
            status="connected",
            is_active=enabled,
        )
        db.commit()
        return new_id

    provider_id = await run_db(_insert_provider)

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
@auth_required
@roles_accepted("admin", "maintainer", "viewer")
async def get_provider(provider_id: int):
    """Get provider details.

    Args:
        provider_id: Provider ID

    Returns:
        200: Provider details
        404: Provider not found
    """
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    return jsonify(_provider_public(provider)), 200


@clouds_bp.route("/<int:provider_id>", methods=["PUT"])
@auth_required
@roles_required("admin")
async def update_provider(provider_id: int):
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

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    data = await request.get_json() or {}

    updates = {}

    if "name" in data:
        updates["name"] = data["name"].strip()

    if "enabled" in data:
        updates["is_active"] = bool(data["enabled"])

    if "config" in data:
        # Validate new config
        new_config = data["config"]
        try:
            cloud = get_cloud_provider(provider.provider_type, new_config)
            cloud.authenticate()
            updates["config_data"] = new_config
            updates["status"] = "connected"
        except CloudError as e:
            return jsonify({"error": f"Invalid configuration: {e}"}), 400

    if updates:
        # Regression: gh-22. update + commit is one unit of work -- stays
        # in one run_db() closure per the house rule (see app/db/run_db.py).
        def _apply_update() -> None:
            db(db.cloud_providers.id == provider_id).update(**updates)
            db.commit()

        await run_db(_apply_update)

    return jsonify({"message": "Provider updated successfully"}), 200


@clouds_bp.route("/<int:provider_id>", methods=["DELETE"])
@auth_required
@roles_required("admin")
async def delete_provider(provider_id: int):
    """Delete a cloud provider.

    Args:
        provider_id: Provider ID

    Returns:
        200: Provider deleted
        404: Provider not found
    """
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    # Check for existing machines
    def _count_machines() -> int:
        return db(db.cloud_machines.provider_id == provider_id).count()

    machine_count = await run_db(_count_machines)
    if machine_count > 0:
        return jsonify({
            "error": f"Cannot delete provider with {machine_count} machines",
            "hint": "Delete or migrate machines first",
        }), 409

    # Regression: gh-22. delete + commit is one unit of work -- stays in
    # one run_db() closure per the house rule.
    def _delete_provider() -> None:
        db(db.cloud_providers.id == provider_id).delete()
        db.commit()

    await run_db(_delete_provider)

    log.info(f"Cloud provider deleted: {provider.name}")

    return jsonify({"message": "Provider deleted successfully"}), 200


@clouds_bp.route("/<int:provider_id>/test", methods=["POST"])
@auth_required
@roles_required("admin")
async def test_provider(provider_id: int):
    """Test provider connectivity.

    Args:
        provider_id: Provider ID

    Returns:
        200: Connection successful
        404: Provider not found
        500: Connection failed
    """
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    def _set_status(new_status: str) -> None:
        db(db.cloud_providers.id == provider_id).update(status=new_status)
        db.commit()

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config_data)
        cloud.authenticate()

        # Update status
        await run_db(lambda: _set_status("connected"))

        return jsonify({
            "status": "connected",
            "message": "Connection successful",
        }), 200

    except CloudAuthError as e:
        await run_db(lambda: _set_status("auth_error"))
        return jsonify({"status": "auth_error", "error": str(e)}), 401

    except CloudError as e:
        await run_db(lambda: _set_status("error"))
        return jsonify({"status": "error", "error": str(e)}), 500


# ============================================================================
# Machine Management
# ============================================================================


@clouds_bp.route("/<int:provider_id>/machines", methods=["GET"])
@auth_required
@roles_accepted("admin", "maintainer", "viewer")
async def list_machines(provider_id: int):
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

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    refresh = request.args.get("refresh", "").lower() == "true"

    if refresh:
        # Fetch from cloud API
        try:
            cloud = get_cloud_provider(provider.provider_type, provider.config_data)
            cloud.authenticate()
            machines = cloud.list_machines()

            # Sync to database -- one run_db() closure for the whole sync
            # (existing-machines scan + per-machine insert/update/delete),
            # not left blocking the event loop inline (gh-22).
            await run_db(lambda: _sync_machines_to_db(db, provider_id, machines))

            return jsonify({
                "machines": [m.to_dict() for m in machines],
                "count": len(machines),
                "source": "cloud_api",
            }), 200

        except CloudError as e:
            log.error(f"Error listing machines from cloud: {e}")
            return jsonify({"error": str(e)}), 500

    # Return from database
    def _fetch_machines() -> Any:
        return db(db.cloud_machines.provider_id == provider_id).select().as_list()

    machines = await run_db(_fetch_machines)

    return jsonify({
        "machines": machines,
        "count": len(machines),
        "source": "database",
    }), 200


@clouds_bp.route("/<int:provider_id>/machines", methods=["POST"])
@auth_required
@roles_accepted("admin", "maintainer")
async def create_machine(provider_id: int):
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

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    if not provider.is_active:
        return jsonify({"error": "Provider is disabled"}), 400

    data = await request.get_json()

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

    # Provisioning a cloud VM is an activation, so it is metered. Syncing an
    # operator's pre-existing fleet in via list_machines is not -- those rows
    # carry no gough-managed tag and never reach this path.
    allowance = await node_allowance(request.host)
    active = await run_db(lambda: count_active_nodes(db))
    if active >= allowance:
        return err_license_required(
            f"Node allowance reached ({active}/{allowance}). Creating another "
            f"machine requires additional licensed nodes.",
            details={
                "active_nodes": active,
                "allowed_nodes": allowance if allowance != float("inf") else "unlimited",
            },
        )

    # Stamp gough's marker so this machine stays attributable across the
    # inventory syncs that later overwrite its local row.
    spec.tags = stamp_managed_tag(spec.tags)

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config_data)
        cloud.authenticate()
        machine = cloud.create_machine(spec)

        # Store in database.  Regression: gh-22. insert + commit is one
        # unit of work -- stays in one run_db() closure per the house rule
        # (see app/db/run_db.py).
        def _store_machine() -> Any:
            new_id = db.cloud_machines.insert(
                provider_id=provider_id,
                external_id=machine.id,
                hostname=machine.name,
                status=machine.state.value,
                zone=machine.region,
                os_image=machine.image,
                machine_type=machine.size,
                public_ips=machine.public_ips,
                private_ips=machine.private_ips,
                ip_address=_primary_ip(machine.public_ips),
                private_ip=_primary_ip(machine.private_ips),
                tags=machine.tags,
                metadata=machine.extra,
            )
            db.commit()
            return new_id

        machine_id = await run_db(_store_machine)

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
@auth_required
@roles_accepted("admin", "maintainer", "viewer")
async def get_machine(provider_id: int, machine_id: str):
    """Get machine details.

    Args:
        provider_id: Provider ID
        machine_id: Machine ID (cloud provider ID)

    Returns:
        200: Machine details
        404: Machine not found
    """
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config_data)
        cloud.authenticate()
        machine = cloud.get_machine(machine_id)

        return jsonify(machine.to_dict()), 200

    except CloudNotFoundError:
        return jsonify({"error": "Machine not found"}), 404

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/machines/<machine_id>", methods=["DELETE"])
@auth_required
@roles_accepted("admin", "maintainer")
async def destroy_machine(provider_id: int, machine_id: str):
    """Destroy a machine.

    Args:
        provider_id: Provider ID
        machine_id: Machine ID

    Returns:
        200: Machine destroyed
        404: Machine not found
    """
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config_data)
        cloud.authenticate()
        cloud.destroy_machine(machine_id)

        # Remove from database. Regression: gh-22. delete + commit is one
        # unit of work -- stays in one run_db() closure.
        def _delete_machine() -> None:
            db(
                (db.cloud_machines.provider_id == provider_id) & (
                    db.cloud_machines.external_id == machine_id)
            ).delete()
            db.commit()

        await run_db(_delete_machine)

        log.info(f"Machine destroyed: {machine_id} on {provider.name}")

        return jsonify({"message": "Machine destroyed successfully"}), 200

    except CloudNotFoundError:
        return jsonify({"error": "Machine not found"}), 404

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/machines/<machine_id>/start", methods=["POST"])
@auth_required
@roles_accepted("admin", "maintainer")
async def start_machine(provider_id: int, machine_id: str):
    """Start a stopped machine.

    Args:
        provider_id: Provider ID
        machine_id: Machine ID

    Returns:
        200: Machine started
        404: Machine not found
    """
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config_data)
        cloud.authenticate()
        cloud.start_machine(machine_id)

        # Update database. Regression: gh-22. update + commit is one unit
        # of work -- stays in one run_db() closure.
        def _mark_running() -> None:
            db(
                (db.cloud_machines.provider_id == provider_id) & (
                    db.cloud_machines.external_id == machine_id)
            ).update(status="running")
            db.commit()

        await run_db(_mark_running)

        return jsonify({"message": "Machine started"}), 200

    except CloudNotFoundError:
        return jsonify({"error": "Machine not found"}), 404

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/machines/<machine_id>/stop", methods=["POST"])
@auth_required
@roles_accepted("admin", "maintainer")
async def stop_machine(provider_id: int, machine_id: str):
    """Stop a running machine.

    Args:
        provider_id: Provider ID
        machine_id: Machine ID

    Returns:
        200: Machine stopped
        404: Machine not found
    """
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config_data)
        cloud.authenticate()
        cloud.stop_machine(machine_id)

        # Update database. Regression: gh-22. update + commit is one unit
        # of work -- stays in one run_db() closure.
        def _mark_stopped() -> None:
            db(
                (db.cloud_machines.provider_id == provider_id) & (
                    db.cloud_machines.external_id == machine_id)
            ).update(status="stopped")
            db.commit()

        await run_db(_mark_stopped)

        return jsonify({"message": "Machine stopped"}), 200

    except CloudNotFoundError:
        return jsonify({"error": "Machine not found"}), 404

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/machines/<machine_id>/reboot", methods=["POST"])
@auth_required
@roles_accepted("admin", "maintainer")
async def reboot_machine(provider_id: int, machine_id: str):
    """Reboot a machine.

    Args:
        provider_id: Provider ID
        machine_id: Machine ID

    Returns:
        200: Machine rebooted
        404: Machine not found
    """
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config_data)
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
@auth_required
@roles_accepted("admin", "maintainer", "viewer")
async def list_images(provider_id: int):
    """List available images for a provider."""
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config_data)
        cloud.authenticate()
        images = cloud.list_images()

        return jsonify({"images": images}), 200

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/sizes", methods=["GET"])
@auth_required
@roles_accepted("admin", "maintainer", "viewer")
async def list_sizes(provider_id: int):
    """List available machine sizes for a provider."""
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config_data)
        cloud.authenticate()
        sizes = cloud.list_sizes()

        return jsonify({"sizes": sizes}), 200

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


@clouds_bp.route("/<int:provider_id>/regions", methods=["GET"])
@auth_required
@roles_accepted("admin", "maintainer", "viewer")
async def list_regions(provider_id: int):
    """List available regions for a provider."""
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch_provider() -> Any:
        return db(db.cloud_providers.id == provider_id).select().first()

    provider = await run_db(_fetch_provider)

    if not provider:
        return jsonify({"error": "Provider not found"}), 404

    try:
        cloud = get_cloud_provider(provider.provider_type, provider.config_data)
        cloud.authenticate()
        regions = cloud.list_regions()

        return jsonify({"regions": regions}), 200

    except CloudError as e:
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Helper Functions
# ============================================================================


def _primary_ip(addresses: list[str] | None) -> str | None:
    """First address from a provider-reported list, or None.

    ``cloud_machines`` keeps both the full list and a single denormalised
    primary: the list is what the provider actually reports, the scalar is
    what callers wanting one address read without unpacking JSON.
    """
    if not addresses:
        return None
    return addresses[0]


def _sync_machines_to_db(db, provider_id: int, machines: list) -> None:
    """Sync machines from cloud API to database.

    Regression: gh-22. This used to run its unbounded existing-machines
    SELECT plus a per-machine insert/update/delete loop synchronously,
    inline in the request coroutine -- callers now run this whole function
    as a single ``run_db()`` closure (see ``list_machines`` above) instead
    of leaving it blocking the event loop. New-machine inserts and
    stale-machine deletes are batched into one statement each (trivially
    batchable: same field set / a single ``IN`` predicate); per-machine
    updates are NOT batched -- each row's field values differ, and
    penguin-dal has no bulk-UPDATE-with-per-row-values primitive, so
    batching those would mean hand-rolling a ``CASE WHEN`` statement, which
    is out of scope for this sweep.
    """
    # Get existing machines
    existing = {
        m.external_id: m.id
        for m in db(db.cloud_machines.provider_id == provider_id).select()
    }

    cloud_ids = set()
    new_rows: list[dict[str, Any]] = []

    for machine in machines:
        cloud_ids.add(machine.id)

        if machine.id in existing:
            # Update existing
            db(db.cloud_machines.id == existing[machine.id]).update(
                hostname=machine.name,
                status=machine.state.value,
                zone=machine.region,
                public_ips=machine.public_ips,
                private_ips=machine.private_ips,
                ip_address=_primary_ip(machine.public_ips),
                private_ip=_primary_ip(machine.private_ips),
                tags=machine.tags,
            )
        else:
            new_rows.append({
                "provider_id": provider_id,
                "external_id": machine.id,
                "hostname": machine.name,
                "status": machine.state.value,
                "zone": machine.region,
                "os_image": machine.image,
                "machine_type": machine.size,
                "public_ips": machine.public_ips,
                "private_ips": machine.private_ips,
                "ip_address": _primary_ip(machine.public_ips),
                "private_ip": _primary_ip(machine.private_ips),
                "tags": machine.tags,
                "metadata": machine.extra,
            })

    if new_rows:
        db.cloud_machines.bulk_insert(new_rows)

    # Remove machines that no longer exist in cloud -- one batched DELETE
    # instead of one per stale machine.
    stale_cloud_ids = [cid for cid in existing if cid not in cloud_ids]
    if stale_cloud_ids:
        db(
            (db.cloud_machines.provider_id == provider_id)
            & (db.cloud_machines.external_id.belongs(stale_cloud_ids))
        ).delete()

    db.commit()
