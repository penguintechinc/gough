"""iPXE Provisioning API Endpoints.

Provides REST API for managing iPXE/MAAS-like provisioning operations:
- iPXE/DHCP configuration management
- Machine discovery, commissioning, deployment
- Boot images and configurations
- Biome deployment (snaps, cloud-init, LXD)
- Power management integration
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Literal, Optional

import jwt
from quart import Blueprint, Response, current_app, g, jsonify, request

from ..db.run_db import run_db
from ..middleware import auth_required, admin_required, maintainer_or_admin_required
from ..models import get_db
from .. import metrics as _metrics
from ._dto import serialize_elder_config

log = logging.getLogger(__name__)

ipxe_bp = Blueprint("ipxe", __name__)


# =============================================================================
# Helper Functions
# =============================================================================

# Regression: gh-22. Pagination defaults for list_machines/list_images --
# page_size default is deliberately generous so existing callers that never
# pass page_size/cursor see the same result set as before the sweep, unless
# their table has actually grown past the default.
_DEFAULT_PAGE_SIZE = 500
_MAX_PAGE_SIZE = 2000


def _encode_ipxe_cursor(item_id: int, ts: Optional[datetime]) -> str:
    """Encode (id, ts) into a base64 opaque pagination cursor.

    Mirrors ``app.api.nodes``'s cursor pattern (gh-22). ``ts`` may be
    ``None`` (e.g. a machine that has never reported ``last_seen_at``).
    """
    payload = json.dumps({"id": item_id, "ts": ts.isoformat() if ts else None})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_ipxe_cursor(cursor: str) -> tuple[int, Optional[datetime]]:
    """Decode an opaque pagination cursor back to (id, ts).

    Raises ValueError on bad/tampered input.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        item_id = int(payload["id"])
        ts_raw = payload.get("ts")
        ts: Optional[datetime] = None
        if ts_raw:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        return item_id, ts
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc


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
    biomes_to_deploy: list[int],
    user_id: int
) -> str:
    """Create a new deployment job.

    Args:
        machine_id: Target machine ID
        image_id: Boot image ID
        boot_config_id: Boot configuration ID (optional)
        biomes_to_deploy: List of biome IDs to deploy
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
        biomes_to_deploy=biomes_to_deploy,
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

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch() -> Any:
        return db(db.ipxe_config.is_active).select().first()

    config = await run_db(_fetch)

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
    def _fetch_existing() -> Any:
        return db(db.ipxe_config.name == data["name"]).select().first()

    existing = await run_db(_fetch_existing)

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
        # Regression: gh-22. update + commit + refetch is one unit of
        # work -- one run_db() closure per the house rule.
        def _apply_update() -> Any:
            db(db.ipxe_config.id == existing.id).update(**update_fields)
            db.commit()
            return db(db.ipxe_config.id == existing.id).select().first()

        updated = await run_db(_apply_update)
        return jsonify(updated.as_dict()), 200
    else:
        # Create new configuration
        update_fields["name"] = data["name"]

        def _apply_insert() -> Any:
            config_id = db.ipxe_config.insert(**update_fields)
            db.commit()
            return db(db.ipxe_config.id == config_id).select().first()

        created = await run_db(_apply_insert)
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
        page_size: Max rows to return (default 500, max 2000)
        cursor: Opaque pagination cursor from a previous response's next_cursor

    Returns:
        200: List of machines

    Regression: gh-22. This was an unbounded ``SELECT *`` over the entire
    ``ipxe_machines`` table run synchronously on the shared event loop --
    now the SELECT runs off-loop via ``run_db()`` and is bounded by
    ``page_size`` (default generous enough that existing callers see the
    same response shape/content unless their table has actually grown past
    it). Cursor filtering mirrors ``app.api.nodes.list_nodes``: the DB
    fetch always pulls the first ``page_size + 1`` rows of the ordered set,
    then the cursor is applied client-side over that fixed window (same
    known limitation nodes.py has -- not a true DB-side seek beyond the
    first window; out of scope for this sweep to redesign).
    """
    db = get_db()

    # Build query
    query = db.ipxe_machines.id > 0

    # Apply filters
    # Regression: gh-22. `request.args` is a sync `cached_property`
    # (`ImmutableMultiDict`), not a coroutine -- `await`ing it raised
    # `TypeError: object ImmutableMultiDict can't be used in 'await'
    # expression` on every real call, a pre-existing bug this sweep's own
    # end-to-end pagination tests surfaced (prior tests all mocked `db` and
    # asserted a permissive `status_code in (200, 401, 500)`, so the 500
    # this raised went unnoticed).
    args = request.args
    if args.get("status"):
        query &= (db.ipxe_machines.status == args["status"])
    if args.get("zone"):
        query &= (db.ipxe_machines.zone == args["zone"])
    if args.get("pool"):
        query &= (db.ipxe_machines.pool == args["pool"])

    try:
        page_size = min(int(args.get("page_size", _DEFAULT_PAGE_SIZE)), _MAX_PAGE_SIZE)
        if page_size < 1:
            page_size = _DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        return jsonify({"error": "page_size must be an integer"}), 400

    cursor_raw = args.get("cursor")
    cursor_after_id: Optional[int] = None
    cursor_after_ts: Optional[datetime] = None
    if cursor_raw:
        try:
            cursor_after_id, cursor_after_ts = _decode_ipxe_cursor(cursor_raw)
        except ValueError:
            return jsonify({"error": "cursor is invalid or tampered"}), 400

    def _fetch() -> Any:
        return db(query).select(
            orderby=~db.ipxe_machines.last_seen_at, limitby=(0, page_size + 1)
        )

    machines = await run_db(_fetch)

    if cursor_after_id is not None:
        floor_ts = datetime.min.replace(tzinfo=timezone.utc)
        cursor_key = (cursor_after_ts or floor_ts, cursor_after_id)
        filtered = []
        for m in machines:
            row_ts = m.last_seen_at
            if row_ts is not None and row_ts.tzinfo is None:
                row_ts = row_ts.replace(tzinfo=timezone.utc)
            # Descending order (~last_seen_at): the next page is strictly
            # "less than" the cursor's position.
            if (row_ts or floor_ts, m.id) < cursor_key:
                filtered.append(m)
        machines = filtered

    next_cursor: Optional[str] = None
    out_machines = list(machines)
    if len(out_machines) > page_size:
        out_machines = out_machines[:page_size]
        last = out_machines[-1]
        next_cursor = _encode_ipxe_cursor(last.id, last.last_seen_at)

    return jsonify({
        "machines": [m.as_dict() for m in out_machines],
        "count": len(out_machines),
        "next_cursor": next_cursor,
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
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    machine = await run_db(lambda: _get_machine_by_id(machine_id))

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
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    machine = await run_db(lambda: _get_machine_by_id(machine_id))

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
    def _update_status() -> None:
        db(db.ipxe_machines.id == machine["id"]).update(
            status="commissioning",
            updated_at=datetime.utcnow()
        )
        db.commit()

    await run_db(_update_status)

    # Log event
    await run_db(lambda: _log_boot_event(
        machine_id=machine["id"],
        mac_address=machine["mac_address"],
        event_type="boot_start",
        details={"action": "commission"},
        status="started"
    ))

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
    """Deploy OS and biomes to a machine.

    Request Body:
        image_id: Boot image ID (required)
        boot_config_id: Boot configuration ID (optional)
        biomes: List of biome IDs to deploy (optional)

    Args:
        machine_id: Machine database ID or system_id

    Returns:
        200: Deployment started
        400: Invalid request or machine state
        404: Machine not found
    """
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    machine = await run_db(lambda: _get_machine_by_id(machine_id))

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
    biomes = data.get("biomes", [])

    # Validate image + boot config existence -- both reads feeding
    # validation, one run_db() closure.
    def _validate_refs() -> Optional[str]:
        if not _get_image_by_id(image_id):
            return "image_not_found"
        if boot_config_id and not _get_boot_config_by_id(boot_config_id):
            return "boot_config_not_found"
        return None

    validation_failure = await run_db(_validate_refs)
    if validation_failure == "image_not_found":
        return jsonify({"error": f"Boot image not found: {image_id}"}), 404
    if validation_failure == "boot_config_not_found":
        return jsonify({"error": f"Boot config not found: {boot_config_id}"}), 404

    db = get_db()

    # Get current user from context
    from quart import g
    user = getattr(g, "current_user", None)
    user_id = user["id"] if user else None

    # Create deployment job
    job_id = await run_db(lambda: _create_deployment_job(
        machine_id=machine["id"],
        image_id=image_id,
        boot_config_id=boot_config_id,
        biomes_to_deploy=biomes,
        user_id=user_id
    ))

    # Update machine status and biomes
    def _update_status() -> None:
        db(db.ipxe_machines.id == machine["id"]).update(
            status="deploying",
            boot_config_id=boot_config_id,
            assigned_biomes=biomes,
            updated_at=datetime.utcnow()
        )
        db.commit()

    await run_db(_update_status)

    # Log event
    await run_db(lambda: _log_boot_event(
        machine_id=machine["id"],
        mac_address=machine["mac_address"],
        event_type="boot_start",
        details={
            "action": "deploy",
            "job_id": job_id,
            "image_id": image_id,
            "biomes": biomes
        },
        status="started"
    ))

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
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    machine = await run_db(lambda: _get_machine_by_id(machine_id))

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
    def _update_status() -> None:
        db(db.ipxe_machines.id == machine["id"]).update(
            status="ready",
            assigned_biomes=[],
            deployed_at=None,
            updated_at=datetime.utcnow()
        )
        db.commit()

    await run_db(_update_status)

    # Log event
    await run_db(lambda: _log_boot_event(
        machine_id=machine["id"],
        mac_address=machine["mac_address"],
        event_type="deployment_complete",
        details={"action": "release"},
        status="released"
    ))

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
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    machine = await run_db(lambda: _get_machine_by_id(machine_id))

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
    await run_db(lambda: _log_boot_event(
        machine_id=machine["id"],
        mac_address=machine["mac_address"],
        event_type="boot_start",
        details={
            "action": f"power_{action}",
            "power_type": power_type,
            "bmc_address": machine.get("bmc_address")
        },
        status="initiated"
    ))

    log.info(f"Power {action} initiated for machine {machine['system_id']} via {power_type}")

    return jsonify({
        "message": f"Power {action} initiated",
        "machine_id": machine["system_id"],
        "power_type": power_type,
        "action": action
    }), 200


@ipxe_bp.route("/machines/<string:machine_id>/biomes", methods=["PUT"])
@maintainer_or_admin_required
@auth_required
async def update_machine_biomes(machine_id: str):
    """Update biomes assigned to a machine.

    Request Body:
        biomes: List of biome IDs

    Args:
        machine_id: Machine database ID or system_id

    Returns:
        200: Biomes updated
        400: Invalid request
        404: Machine not found
    """
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    machine = await run_db(lambda: _get_machine_by_id(machine_id))

    if not machine:
        return jsonify({"error": f"Machine not found: {machine_id}"}), 404

    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    biomes = data.get("biomes", [])

    if not isinstance(biomes, list):
        return jsonify({"error": "Biomes must be a list"}), 400

    db = get_db()

    # Update assigned biomes
    def _update_biomes() -> None:
        db(db.ipxe_machines.id == machine["id"]).update(
            assigned_biomes=biomes,
            updated_at=datetime.utcnow()
        )
        db.commit()

    await run_db(_update_biomes)

    log.info(f"Machine {machine['system_id']} biomes updated: {biomes}")

    return jsonify({
        "message": "Biomes updated",
        "machine_id": machine["system_id"],
        "biomes": biomes
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
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    machine = await run_db(lambda: _get_machine_by_id(machine_id))

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
    def _delete_machine() -> None:
        db(db.ipxe_machines.id == machine["id"]).delete()
        db.commit()

    await run_db(_delete_machine)

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
        page_size: Max rows to return (default 500, max 2000)
        cursor: Opaque pagination cursor from a previous response's next_cursor

    Returns:
        200: List of boot images

    Regression: gh-22. Same conversion as ``list_machines`` above -- the
    SELECT now runs off-loop via ``run_db()`` and is bounded by
    ``page_size``; see that handler's docstring for the pagination/cursor
    semantics (mirrors ``app.api.nodes.list_nodes``).
    """
    db = get_db()

    # Build query
    query = db.ipxe_images.id > 0

    # Apply filters
    # Regression: gh-22. `request.args` is a sync `cached_property`
    # (`ImmutableMultiDict`), not a coroutine -- `await`ing it raised
    # `TypeError: object ImmutableMultiDict can't be used in 'await'
    # expression` on every real call, a pre-existing bug this sweep's own
    # end-to-end pagination tests surfaced (prior tests all mocked `db` and
    # asserted a permissive `status_code in (200, 401, 500)`, so the 500
    # this raised went unnoticed).
    args = request.args
    if args.get("architecture"):
        query &= (db.ipxe_images.architecture == args["architecture"])
    if args.get("os_version"):
        query &= (db.ipxe_images.os_version == args["os_version"])

    try:
        page_size = min(int(args.get("page_size", _DEFAULT_PAGE_SIZE)), _MAX_PAGE_SIZE)
        if page_size < 1:
            page_size = _DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        return jsonify({"error": "page_size must be an integer"}), 400

    cursor_raw = args.get("cursor")
    cursor_after_id: Optional[int] = None
    cursor_after_ts: Optional[datetime] = None
    if cursor_raw:
        try:
            cursor_after_id, cursor_after_ts = _decode_ipxe_cursor(cursor_raw)
        except ValueError:
            return jsonify({"error": "cursor is invalid or tampered"}), 400

    def _fetch() -> Any:
        return db(query).select(
            orderby=~db.ipxe_images.created_at, limitby=(0, page_size + 1)
        )

    images = await run_db(_fetch)

    if cursor_after_id is not None:
        floor_ts = datetime.min.replace(tzinfo=timezone.utc)
        cursor_key = (cursor_after_ts or floor_ts, cursor_after_id)
        filtered = []
        for img in images:
            row_ts = img.created_at
            if row_ts is not None and row_ts.tzinfo is None:
                row_ts = row_ts.replace(tzinfo=timezone.utc)
            # Descending order (~created_at): the next page is strictly
            # "less than" the cursor's position.
            if (row_ts or floor_ts, img.id) < cursor_key:
                filtered.append(img)
        images = filtered

    next_cursor: Optional[str] = None
    out_images = list(images)
    if len(out_images) > page_size:
        out_images = out_images[:page_size]
        last = out_images[-1]
        next_cursor = _encode_ipxe_cursor(last.id, last.created_at)

    return jsonify({
        "images": [img.as_dict() for img in out_images],
        "count": len(out_images),
        "next_cursor": next_cursor,
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
    def _check_name_exists() -> Any:
        return db(db.ipxe_images.name == data["name"]).select().first()

    existing = await run_db(_check_name_exists)
    if existing:
        return jsonify({"error": f"Image name already exists: {data['name']}"}), 409

    # Create image. Regression: gh-22. insert + commit + refetch is one
    # unit of work -- one run_db() closure per the house rule.
    def _create_image() -> Any:
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
        return db(db.ipxe_images.id == image_id).select().first()

    created = await run_db(_create_image)

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
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    image = await run_db(lambda: _get_image_by_id(image_id))

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
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    image = await run_db(lambda: _get_image_by_id(image_id))

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

    # Update image. Regression: gh-22. update + commit + refetch is one
    # unit of work -- one run_db() closure per the house rule.
    def _apply_update() -> Any:
        db(db.ipxe_images.id == image_id).update(**update_fields)
        db.commit()
        return db(db.ipxe_images.id == image_id).select().first()

    updated = await run_db(_apply_update)

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
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    image = await run_db(lambda: _get_image_by_id(image_id))

    if not image:
        return jsonify({"error": f"Boot image not found: {image_id}"}), 404

    db = get_db()

    # Check if image is in use -- deploying-machines scan + per-machine
    # job lookup is a sequence of reads feeding validation, one closure.
    def _check_in_use() -> bool:
        machines_using = db(db.ipxe_machines.status == "deploying").select()
        for machine in machines_using:
            job = db(
                (db.deployment_jobs.machine_id == machine.id) &
                (db.deployment_jobs.image_id == image_id) &
                (db.deployment_jobs.status.belongs(["pending", "power_on", "pxe_boot", "os_install"]))
            ).select().first()
            if job:
                return True
        return False

    if await run_db(_check_in_use):
        return jsonify({
            "error": "Image is in use by active deployments",
            "image_id": image_id
        }), 400

    # Delete image
    def _delete_image() -> None:
        db(db.ipxe_images.id == image_id).delete()
        db.commit()

    await run_db(_delete_image)

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

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _fetch() -> Any:
        return db(db.ipxe_boot_configs).select(orderby=~db.ipxe_boot_configs.created_at)

    configs = await run_db(_fetch)

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
        assigned_biome_group_id: Assigned biome group ID
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
    def _check_name_exists() -> Any:
        return db(db.ipxe_boot_configs.name == data["name"]).select().first()

    existing = await run_db(_check_name_exists)
    if existing:
        return jsonify({"error": f"Boot config name already exists: {data['name']}"}), 409

    # Create boot config. Regression: gh-22. insert + commit + refetch is
    # one unit of work -- one run_db() closure per the house rule.
    def _create_config() -> Any:
        config_id = db.ipxe_boot_configs.insert(
            name=data["name"],
            description=data.get("description"),
            ipxe_script=data.get("ipxe_script"),
            kernel_params=data.get("kernel_params"),
            boot_order=data.get("boot_order", []),
            timeout_seconds=data.get("timeout_seconds", 30),
            default_image_id=data.get("default_image_id"),
            assigned_biome_group_id=data.get("assigned_biome_group_id"),
            is_default=data.get("is_default", False)
        )
        db.commit()
        return db(db.ipxe_boot_configs.id == config_id).select().first()

    created = await run_db(_create_config)

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
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    config = await run_db(lambda: _get_boot_config_by_id(config_id))

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
        assigned_biome_group_id: Assigned biome group ID
        is_default: Set as default config

    Args:
        config_id: Config database ID

    Returns:
        200: Boot config updated
        400: Invalid request
        404: Config not found
    """
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    config = await run_db(lambda: _get_boot_config_by_id(config_id))

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
        "timeout_seconds", "default_image_id", "assigned_biome_group_id", "is_default"
    ]

    for field in allowed_updates:
        if field in data:
            update_fields[field] = data[field]

    # Update boot config. Regression: gh-22. update + commit + refetch is
    # one unit of work -- one run_db() closure per the house rule.
    def _apply_update() -> Any:
        db(db.ipxe_boot_configs.id == config_id).update(**update_fields)
        db.commit()
        return db(db.ipxe_boot_configs.id == config_id).select().first()

    updated = await run_db(_apply_update)

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
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    config = await run_db(lambda: _get_boot_config_by_id(config_id))

    if not config:
        return jsonify({"error": f"Boot config not found: {config_id}"}), 404

    db = get_db()

    # Check if config is in use
    def _count_in_use() -> int:
        return db(db.ipxe_machines.boot_config_id == config_id).count()

    machines_using = await run_db(_count_in_use)
    if machines_using > 0:
        return jsonify({
            "error": f"Boot config is in use by {machines_using} machines",
            "config_id": config_id
        }), 400

    # Delete boot config
    def _delete_config() -> None:
        db(db.ipxe_boot_configs.id == config_id).delete()
        db.commit()

    await run_db(_delete_config)

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
    # Regression: gh-22. Config fetch + default-image fetch is a sequence
    # of reads feeding the response -- one run_db() closure.
    def _fetch_config_and_image() -> tuple[Any, Any]:
        config = _get_boot_config_by_id(config_id)
        if not config:
            return None, None
        image = None
        if config.get("default_image_id"):
            image = _get_image_by_id(config["default_image_id"])
        return config, image

    config, image = await run_db(_fetch_config_and_image)

    if not config:
        return jsonify({"error": f"Boot config not found: {config_id}"}), 404

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

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    machine = await run_db(lambda: _get_machine_by_id(machine_id))

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
        def _update_sync_timestamp() -> None:
            db(db.ipxe_machines.id == machine["id"]).update(
                elder_synced_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            db.commit()

        await run_db(_update_sync_timestamp)

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


# =============================================================================
# Sprint 2: Bootstrap Token / iPXE Chain Endpoints
# =============================================================================
#
# Per spec "Phase 1 → iPXE Chain" (let-s-create-a-spec-snoopy-nest.md):
#   * GET /ipxe/helper/{mac}      anonymous, MAC+nonce-bound iPXE script
#   * GET /ipxe/deploy/{mac}      anonymous, MAC+nonce-bound iPXE script (Sprint 4 expands)
#   * POST /api/v1/ipxe/bind-mac          scope: gough.nodes.provision
#   * POST /api/v1/ipxe/mint-bootstrap-token  scope: gough.nodes.provision
#
# JWT payload: {mac, dmi_uuid_hint, nonce, iat, exp, phase: "helper"|"deploy"}
# TTL: 600s. Nonces tracked Redis-first, DB (bootstrap_nonces) as fallback.

_BOOTSTRAP_JWT_TTL_SECONDS = 300  # 5 minutes, must be ≤600s per validator
_BOOTSTRAP_VAULT_KEY = "gough-bootstrap-jwt"
_BOOTSTRAP_NONCE_REDIS_PREFIX = "bootstrap:nonce:"


# -----------------------------------------------------------------------------
# In-memory IP-based rate limiter (no external dep). Thread-safe.
# -----------------------------------------------------------------------------

class _RateLimiter:
    """Thread-safe sliding-window per-key rate limiter (in-memory).

    100 requests / 60s by default. Designed for single-process deployments;
    distributed enforcement deferred to Sprint 3 ingress (Cilium/Envoy).
    """

    def __init__(self, max_requests: int = 100, window_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, [])
            cutoff = now - self.window_seconds
            # Drop stale entries (cheap because bucket is small at limit ceiling).
            i = 0
            for i, ts in enumerate(bucket):
                if ts >= cutoff:
                    break
            else:
                i = len(bucket)
            del bucket[:i]
            if len(bucket) >= self.max_requests:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_ipxe_script_rate_limiter = _RateLimiter(max_requests=100, window_seconds=60.0)


def _client_source_ip() -> str:
    """Best-effort source IP for rate-limiting key."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",", 1)[0].strip()
    return request.remote_addr or "unknown"


def _rate_limit_ipxe_script(f: Callable) -> Callable:
    """Decorator: 100 req/min/IP rate limit for anonymous iPXE script endpoints."""
    @wraps(f)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        ip = _client_source_ip()
        if not _ipxe_script_rate_limiter.allow(ip):
            log.warning(f"iPXE script rate limit exceeded for source_ip={ip}")
            return jsonify({"error": "Rate limit exceeded"}), 429
        return await f(*args, **kwargs)
    return wrapper


# -----------------------------------------------------------------------------
# Node lookup by MAC
# -----------------------------------------------------------------------------

def _normalize_mac(mac: str) -> str:
    """Normalize MAC to lowercase colon-separated form (aa:bb:cc:dd:ee:ff).

    Accepts colon, dash, or dotless inputs. Returns empty string on invalid.
    """
    if not mac:
        return ""
    cleaned = mac.strip().lower().replace("-", ":").replace(".", "")
    if ":" not in cleaned and len(cleaned) == 12:
        cleaned = ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))
    parts = cleaned.split(":")
    if len(parts) != 6 or not all(len(p) == 2 and all(c in "0123456789abcdef" for c in p) for p in parts):
        return ""
    return cleaned


def _find_node_or_machine_by_mac(mac: str) -> Optional[dict]:
    """Resolve a MAC against either nodes.primary_nic_mac or ipxe_machines.mac_address.

    Returns a dict with at least: id, mac, dmi_uuid (or None), source.
    Source is 'nodes' or 'ipxe_machines'. Returns None if not found.
    """
    normalized = _normalize_mac(mac)
    if not normalized:
        return None

    db = get_db()

    # Prefer the canonical nodes table.
    if "nodes" in db.tables:
        node = db(db.nodes.primary_nic_mac == normalized).select().first()
        if node:
            d = node.as_dict()
            return {
                "id": d["id"],
                "mac": normalized,
                "dmi_uuid": d.get("dmi_uuid"),
                "source": "nodes",
                "raw": d,
            }

    # Fall back to legacy ipxe_machines.
    if "ipxe_machines" in db.tables:
        machine = db(db.ipxe_machines.mac_address == normalized).select().first()
        if machine:
            d = machine.as_dict()
            return {
                "id": d["id"],
                "mac": normalized,
                "dmi_uuid": d.get("dmi_uuid"),
                "source": "ipxe_machines",
                "raw": d,
            }

    return None


# -----------------------------------------------------------------------------
# Nonce registration (Redis preferred, DB fallback)
# -----------------------------------------------------------------------------

def _register_bootstrap_nonce(nonce: str, mac: str, phase: str, ttl_seconds: int) -> None:
    """Register a freshly-minted bootstrap nonce so it can be one-time-validated.

    Tries Redis (current_app.redis_client) first; falls back to DB
    bootstrap_nonces table created by migration 20260428_0900_bootstrap_nonces.

    Raises RuntimeError if neither store is available.
    """
    redis_client = getattr(current_app, "redis_client", None)
    if redis_client is not None:
        key = f"{_BOOTSTRAP_NONCE_REDIS_PREFIX}{nonce}"
        # Match credentials.validate_one_time_bootstrap_token's SET NX semantics.
        added = redis_client.set(key, "issued", ex=ttl_seconds, nx=True)
        if not added:
            raise RuntimeError(f"Bootstrap nonce collision: {nonce}")
        return

    # DB fallback.
    db = get_db()
    if "bootstrap_nonces" not in db.tables:
        raise RuntimeError(
            "No nonce store available: Redis client missing and "
            "bootstrap_nonces table not present"
        )
    now_ts = datetime.now(timezone.utc).timestamp()
    expires_at = now_ts + ttl_seconds
    if ttl_seconds <= 0:
        _metrics.bootstrap_window_expired.inc()
        raise RuntimeError("Bootstrap token TTL already expired at registration time")
    db.bootstrap_nonces.insert(
        nonce=nonce,
        mac=mac,
        phase=phase,
        used=False,
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc),
    )
    db.commit()


# -----------------------------------------------------------------------------
# JWT minting
# -----------------------------------------------------------------------------

def _mint_bootstrap_jwt(
    mac: str,
    *,
    phase: Literal["helper", "deploy"],
    dmi_uuid_hint: Optional[str] = None,
    ttl_seconds: int = _BOOTSTRAP_JWT_TTL_SECONDS,
) -> tuple[str, str]:
    """Mint a one-time bootstrap JWT bound to (mac, nonce).

    Signing path:
        1. Vault transit signing if app has a vault_client and the transit key exists.
        2. HS256 fallback using current_app.config['JWT_SECRET_KEY'] for non-prod /
           pre-Vault bootstrap.

    Returns (jwt_token, nonce). The nonce is registered before this function
    returns so a replay of the same nonce will be rejected on validation.
    """
    nonce = secrets.token_urlsafe(24)
    now = int(datetime.now(timezone.utc).timestamp())
    payload: dict[str, Any] = {
        "mac": mac,
        "dmi_uuid_hint": dmi_uuid_hint,
        "nonce": nonce,
        "iat": now,
        "exp": now + ttl_seconds,
        "phase": phase,
    }

    vault_client = getattr(current_app, "vault_client", None)
    token: Optional[str] = None
    if vault_client is not None:
        try:
            # Build a JWS by signing the canonical header.payload via Vault transit.
            # Header advertises Vault-issued opaque signature; verifiers must
            # call Vault transit_verify_signature to validate.
            import base64
            import json as _json
            header = {"alg": "vault-transit", "typ": "JWT", "kid": _BOOTSTRAP_VAULT_KEY}
            h_b64 = base64.urlsafe_b64encode(
                _json.dumps(header, separators=(",", ":")).encode()
            ).rstrip(b"=").decode()
            p_b64 = base64.urlsafe_b64encode(
                _json.dumps(payload, separators=(",", ":")).encode()
            ).rstrip(b"=").decode()
            signing_input = f"{h_b64}.{p_b64}".encode()
            signature = vault_client.transit_sign(_BOOTSTRAP_VAULT_KEY, signing_input)
            sig_b64 = base64.urlsafe_b64encode(signature.encode()).rstrip(b"=").decode()
            token = f"{h_b64}.{p_b64}.{sig_b64}"
            log.info(f"Bootstrap JWT minted via Vault transit for mac={mac} phase={phase}")
        except Exception as e:  # noqa: BLE001 - intentional fallback
            log.warning(
                f"Vault transit_sign unavailable ({e}); falling back to HS256 for mac={mac}"
            )
            token = None

    if token is None:
        secret = current_app.config.get("JWT_SECRET_KEY") or current_app.config.get(
            "BOOTSTRAP_JWT_SECRET"
        )
        if not secret:
            raise RuntimeError(
                "Bootstrap JWT secret not configured: set JWT_SECRET_KEY or BOOTSTRAP_JWT_SECRET"
            )
        token = jwt.encode(payload, secret, algorithm="HS256")
        log.info(f"Bootstrap JWT minted via HS256 fallback for mac={mac} phase={phase}")

    # Register nonce so validate_one_time_bootstrap_token's NX check is meaningful.
    _register_bootstrap_nonce(nonce, mac, phase, ttl_seconds)

    return token, nonce


# -----------------------------------------------------------------------------
# Script renderers
# -----------------------------------------------------------------------------

def _render_helper_ipxe_script(
    mac: str,
    jwt_token: str,
    primary_url: str,
    *,
    firmware: Literal["bios", "uefi"],
) -> str:
    """Render the Phase-1 helper iPXE script.

    The script:
      1. Pulls kernel + initrd from the primary over HTTPS.
      2. Boots the helper image with kernel cmdline:
           gough_token=<jwt> gough_primary=<url> gough_mac=<mac>
      3. Branches BIOS vs UEFI via iPXE ${platform} macro at runtime.

    `firmware` selects the default kernel/initrd artifact name; the script
    still uses the iPXE platform macro so a single script renders correctly
    on either firmware family.
    """
    base = primary_url.rstrip("/")
    helper_kernel = "helper-bios" if firmware == "bios" else "helper-efi"
    helper_initrd = "helper.initrd"
    cmdline = f"gough_token={jwt_token} gough_primary={base} gough_mac={mac}"
    script = (
        "#!ipxe\n"
        f"# Gough helper boot script (phase=helper, mac={mac}, firmware={firmware})\n"
        "set retries:int32 5\n"
        "set delay_ms:int32 1000\n"
        "echo Gough: starting helper boot for ${mac}\n"
        "iseq ${platform} efi && goto uefi || goto bios\n"
        "\n"
        ":bios\n"
        f"set kernel_url {base}/ipxe/kernel/helper-bios\n"
        f"set initrd_url {base}/ipxe/initrd/{helper_initrd}\n"
        "goto fetch\n"
        "\n"
        ":uefi\n"
        f"set kernel_url {base}/ipxe/kernel/helper-efi\n"
        f"set initrd_url {base}/ipxe/initrd/{helper_initrd}\n"
        "goto fetch\n"
        "\n"
        ":fetch\n"
        "echo Gough: fetching kernel ${kernel_url}\n"
        "kernel ${kernel_url} " + cmdline + "\n"
        "echo Gough: fetching initrd ${initrd_url}\n"
        "initrd ${initrd_url}\n"
        "boot || goto failed\n"
        "\n"
        ":failed\n"
        "echo Gough: helper boot failed; rebooting in 30s\n"
        "sleep 30\n"
        "reboot\n"
    )
    # Helper kernel name kept alongside cmdline for downstream debugging tools.
    _ = helper_kernel
    return script


def _render_deploy_ipxe_script(mac: str, jwt_token: str, primary_url: str) -> str:
    """Render the Phase-2 deploy iPXE script.

    Renders a complete iPXE script for the deployment phase that:
    1. Pulls kernel + initrd from primary over HTTPS
    2. Boots the installer with bootstrap token embedded in kernel cmdline
    3. Includes fallback boot logic for retry on failure

    Args:
        mac: Target machine MAC address (normalized)
        jwt_token: One-time bootstrap JWT token
        primary_url: Base URL for kernel/initrd artifacts

    Returns:
        Valid iPXE script with #!ipxe header and complete boot sequence
    """
    base = primary_url.rstrip("/")
    deploy_kernel = "deploy-kernel"
    deploy_initrd = "deploy.initrd"
    cmdline = f"gough.bootstrap_token={jwt_token} gough.mac={mac} gough.phase=deploy"
    script = (
        "#!ipxe\n"
        f"# Gough deploy boot script (phase=deploy, mac={mac})\n"
        "set retries:int32 3\n"
        "set delay_ms:int32 1000\n"
        "echo Gough: starting deployment for ${mac}\n"
        "\n"
        ":fetch\n"
        f"set kernel_url {base}/ipxe/kernel/{deploy_kernel}\n"
        f"set initrd_url {base}/ipxe/initrd/{deploy_initrd}\n"
        "echo Gough: fetching deploy kernel ${kernel_url}\n"
        "kernel ${kernel_url} " + cmdline + "\n"
        "echo Gough: fetching deploy initrd ${initrd_url}\n"
        "initrd ${initrd_url}\n"
        "boot || goto fallback\n"
        "\n"
        ":fallback\n"
        "echo Gough: deploy boot failed; retrying\n"
        "sleep ${delay_ms}\n"
        "goto fetch\n"
    )
    return script


# -----------------------------------------------------------------------------
# Anonymous (MAC+nonce) script endpoints
# -----------------------------------------------------------------------------

def _detect_firmware_from_query() -> Literal["bios", "uefi"]:
    """Best-effort firmware detection.

    iPXE's chain step propagates ${platform} as a query param when configured
    (e.g. ?fw=efi). Default to 'uefi' when ambiguous since modern targets are
    UEFI-first. Both branches still ship the platform-aware iPXE script body.
    """
    fw = (request.args.get("fw") or request.args.get("firmware") or "").lower()
    if fw in ("bios", "pcbios", "legacy"):
        return "bios"
    return "uefi"


def _primary_base_url() -> str:
    """Resolve the primary HTTPS URL for kernel/initrd artifact pulls."""
    cfg = current_app.config.get("PRIMARY_BASE_URL")
    if cfg:
        return str(cfg)
    # Use the request host as fallback so dev/test deployments work without
    # explicit configuration. iPXE's trust anchor is the embedded internal CA.
    scheme = "https"
    return f"{scheme}://{request.host}"


@ipxe_bp.route("/helper/<string:mac>", methods=["GET"])
@_rate_limit_ipxe_script
async def get_helper_ipxe_script(mac: str):
    """Anonymous helper iPXE script for a known MAC.

    Authentication: MAC+nonce binding (no JWT in request). The script body
    contains a freshly-minted one-time bootstrap JWT that the discovery agent
    then presents to /api/v1/nodes/discover.

    Returns:
        200 text/plain: iPXE script
        404 application/json: MAC not found
    """
    normalized = _normalize_mac(mac)
    if not normalized:
        return jsonify({"error": f"Invalid MAC address: {mac}"}), 400

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    record = await run_db(lambda: _find_node_or_machine_by_mac(normalized))
    if not record:
        log.warning(f"iPXE helper requested for unknown MAC {normalized}")
        return jsonify({"error": f"MAC not found: {normalized}"}), 404

    firmware = _detect_firmware_from_query()
    primary_url = _primary_base_url()

    token, nonce = await run_db(lambda: _mint_bootstrap_jwt(
        normalized,
        phase="helper",
        dmi_uuid_hint=record.get("dmi_uuid"),
    ))

    script = _render_helper_ipxe_script(
        normalized, token, primary_url, firmware=firmware
    )

    log.info(
        f"iPXE helper script issued mac={normalized} firmware={firmware} "
        f"nonce={nonce[:8]}... source={record['source']}"
    )

    return Response(script, status=200, content_type="text/plain; charset=utf-8")


@ipxe_bp.route("/deploy/<string:mac>", methods=["GET"])
@_rate_limit_ipxe_script
async def get_deploy_ipxe_script(mac: str):
    """Anonymous deploy iPXE script for a known MAC.

    Authentication: MAC+nonce binding via bootstrap JWT. The script body
    contains a freshly-minted one-time bootstrap JWT that the deployment
    agent then presents during the provisioning workflow.

    Returns:
        200 text/plain: iPXE script
        404 application/json: MAC not found
    """
    normalized = _normalize_mac(mac)
    if not normalized:
        return jsonify({"error": f"Invalid MAC address: {mac}"}), 400

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    record = await run_db(lambda: _find_node_or_machine_by_mac(normalized))
    if not record:
        log.warning(f"iPXE deploy requested for unknown MAC {normalized}")
        return jsonify({"error": f"MAC not found: {normalized}"}), 404

    primary_url = _primary_base_url()
    token, nonce = await run_db(lambda: _mint_bootstrap_jwt(
        normalized,
        phase="deploy",
        dmi_uuid_hint=record.get("dmi_uuid"),
    ))

    script = _render_deploy_ipxe_script(normalized, token, primary_url)

    log.info(
        f"iPXE deploy script issued mac={normalized} "
        f"nonce={nonce[:8]}... source={record['source']}"
    )

    return Response(script, status=200, content_type="text/plain; charset=utf-8")


# -----------------------------------------------------------------------------
# Authenticated control endpoints (gough.nodes.provision)
# -----------------------------------------------------------------------------

def _scope_required(*required_scopes: str) -> Callable:
    """Decorator enforcing OIDC scope membership on the request principal.

    Reads g.principal (set by app.security.credentials.credentials_middleware).
    Falls back to legacy g.current_user.role check when running in the
    pre-OIDC test harness (admin/maintainer accepted as superset).
    """
    required = frozenset(required_scopes)

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            principal = getattr(g, "principal", None)
            if principal is not None:
                granted = getattr(principal, "scopes", frozenset())
                if not required.issubset(granted):
                    return jsonify({
                        "error": "Insufficient scope",
                        "required": sorted(required),
                    }), 403
                return await f(*args, **kwargs)

            # Authorization is scope-only: read scopes from the validated JWT.
            user = getattr(g, "current_user", None)
            if user is not None:
                from ..security.scope_enforcement import extract_scopes_from_jwt
                granted = extract_scopes_from_jwt(user.get("_jwt_payload") or {})
                if required.issubset(granted):
                    return await f(*args, **kwargs)

            return jsonify({"error": "Insufficient scope"}), 403

        return wrapper

    return decorator


@ipxe_bp.route("/bind-mac", methods=["POST"])
@auth_required
@_scope_required("gough.nodes.provision")
async def bind_mac():
    """Pre-bind a MAC address for a known DMI UUID.

    Request body:
        mac: MAC address (required)
        dmi_uuid: DMI UUID hint (required)
        node_id: existing node id to bind onto (optional)

    Returns:
        201: Binding created
        200: Existing binding refreshed
        400: Invalid request
    """
    data = await request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    err = _validate_required_fields(data, ["mac", "dmi_uuid"])
    if err:
        return err

    normalized = _normalize_mac(data["mac"])
    if not normalized:
        return jsonify({"error": f"Invalid MAC address: {data['mac']}"}), 400

    dmi_uuid = str(data["dmi_uuid"]).strip()
    if not dmi_uuid:
        return jsonify({"error": "dmi_uuid must not be empty"}), 400

    db = get_db()
    requested_node_id = data.get("node_id")

    if "nodes" in db.tables:
        # Regression: gh-22. Off the event loop via run_db() instead of
        # blocking the request coroutine inline.
        def _fetch_node() -> Any:
            if requested_node_id:
                return db(db.nodes.id == int(requested_node_id)).select().first()
            return db(db.nodes.dmi_uuid == dmi_uuid).select().first()

        node = await run_db(_fetch_node)
        if requested_node_id and not node:
            return jsonify({"error": f"Node not found: {requested_node_id}"}), 404

        if node:
            def _update_node() -> None:
                db(db.nodes.id == node.id).update(
                    primary_nic_mac=normalized,
                    dmi_uuid=dmi_uuid,
                    updated_at=datetime.now(timezone.utc),
                )
                db.commit()

            await run_db(_update_node)
            log.info(f"MAC bound to existing node id={node.id} mac={normalized} dmi={dmi_uuid}")
            return jsonify({
                "node_id": node.id,
                "mac": normalized,
                "dmi_uuid": dmi_uuid,
                "status": "updated",
            }), 200

        # Create a new node row in 'new' state for the binding.
        def _create_node() -> Any:
            new_id = db.nodes.insert(
                tenant_id="__default__",
                name=f"node-{normalized.replace(':', '')}",
                state="new",
                dmi_uuid=dmi_uuid,
                primary_nic_mac=normalized,
            )
            db.commit()
            return new_id

        new_id = await run_db(_create_node)
        log.info(f"MAC bound; new node id={new_id} mac={normalized} dmi={dmi_uuid}")
        return jsonify({
            "node_id": new_id,
            "mac": normalized,
            "dmi_uuid": dmi_uuid,
            "status": "created",
        }), 201

    return jsonify({"error": "nodes table not available"}), 500


@ipxe_bp.route("/mint-bootstrap-token", methods=["POST"])
@auth_required
@_scope_required("gough.nodes.provision")
async def mint_bootstrap_token():
    """Mint a fresh one-time bootstrap JWT for a MAC (operator-driven flow).

    Request body:
        mac: MAC address (required)
        phase: "helper" or "deploy" (default: "helper")

    Returns:
        200: JWT minted
        400: Invalid request
        404: MAC not found
    """
    data = await request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    err = _validate_required_fields(data, ["mac"])
    if err:
        return err

    normalized = _normalize_mac(data["mac"])
    if not normalized:
        return jsonify({"error": f"Invalid MAC address: {data['mac']}"}), 400

    phase: Literal["helper", "deploy"] = data.get("phase", "helper")
    if phase not in ("helper", "deploy"):
        return jsonify({"error": "phase must be 'helper' or 'deploy'"}), 400

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    record = await run_db(lambda: _find_node_or_machine_by_mac(normalized))
    if not record:
        return jsonify({"error": f"MAC not found: {normalized}"}), 404

    token, nonce = await run_db(lambda: _mint_bootstrap_jwt(
        normalized,
        phase=phase,
        dmi_uuid_hint=record.get("dmi_uuid"),
    ))

    log.info(f"Operator minted bootstrap token mac={normalized} phase={phase}")

    return jsonify({
        "mac": normalized,
        "phase": phase,
        "token": token,
        "nonce": nonce,
        "ttl_seconds": _BOOTSTRAP_JWT_TTL_SECONDS,
    }), 200


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
    def _fetch_existing() -> Any:
        return db(db.elder_config.name == "default").select().first()

    existing = await run_db(_fetch_existing)

    update_fields = {
        "elder_url": data["elder_url"],
        "api_key": data["api_key"],
        "timeout": data.get("timeout", 10),
        "max_retries": data.get("max_retries", 3),
        "is_active": data.get("is_active", True),
        "updated_at": datetime.utcnow()
    }

    if existing:
        # Update existing configuration. Regression: gh-22. update +
        # commit + refetch is one unit of work -- one run_db() closure.
        def _apply_update() -> Any:
            db(db.elder_config.id == existing.id).update(**update_fields)
            db.commit()
            return db(db.elder_config.id == existing.id).select().first()

        updated = await run_db(_apply_update)
        log.info("Elder configuration updated")

        # Explicit allow-list projection -- never the raw row. Regression:
        # audit output-validation. This handler used to echo the just-stored
        # ``api_key`` straight back via ``.as_dict()``, the same raw-row-echo
        # shape as the fixed clouds.py provider leak. See
        # ``app.api._dto.ELDER_CONFIG_PUBLIC_FIELDS``.
        return jsonify({
            "message": "Configuration updated",
            "config": serialize_elder_config(updated),
        }), 200

    else:
        # Create new configuration. Regression: gh-22. insert + commit +
        # refetch is one unit of work -- one run_db() closure.
        update_fields["name"] = "default"

        def _create_config() -> Any:
            config_id = db.elder_config.insert(**update_fields)
            db.commit()
            return db(db.elder_config.id == config_id).select().first()

        created = await run_db(_create_config)
        log.info("Elder configuration created")

        # Explicit allow-list projection -- never the raw row. See the
        # matching comment in the update branch above.
        return jsonify({
            "message": "Configuration created",
            "config": serialize_elder_config(created),
        }), 201
