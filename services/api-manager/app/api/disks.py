"""Disks Management API Endpoints.

Implements the M1 spec "API Surface → Disks" contract:

- ``GET    /api/v1/nodes/{node_id}/disks``               — list disks (SMART, tier, reservation)
- ``POST   /api/v1/nodes/{node_id}/disks/{disk_id}/plan`` — create/replace partitioning plan
- ``PATCH  /api/v1/nodes/{node_id}/disks/{disk_id}``      — dark-drive toggle, tier classification
- ``POST   /api/v1/nodes/{node_id}/disks/{disk_id}/smart-recheck`` — enqueue SMART recheck

All routes are tenant-scoped via the JWT ``tenant`` claim and protected by the
scope-enforcement decorator from :mod:`app.security.scope_enforcement`.
``write_files`` path collisions are detected during plan compilation, in line
with the spec's ``Phase 2`` plan-compilation contract.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from quart import Blueprint, current_app, g, jsonify, request

from ..db.run_db import run_db
from ..models import get_db
from ..middleware import auth_required
from ..security.scope_enforcement import require_scopes

log = logging.getLogger(__name__)

disks_bp = Blueprint("disks", __name__)


# =============================================================================
# Pydantic v2 Models
# =============================================================================


_VALID_FS_TYPES = frozenset(
    {"ext4", "xfs", "btrfs", "zfs", "vfat", "swap", "none"}
)
_VALID_ENCRYPTION = frozenset({"none", "luks", "luks2"})
_VALID_TIERS = frozenset({"fast", "bulk"})


class PartitionSpec(BaseModel):
    """A single partition entry in a disk plan."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(..., ge=1, le=128, description="GPT partition index (1-based).")
    mount_point: str = Field(..., min_length=1, max_length=255)
    size_mb: int = Field(..., ge=1, description="Partition size in MiB.")
    fs_type: str = Field(..., description="Filesystem type.")
    encryption: str = Field(default="none", description="Encryption mode.")

    @field_validator("fs_type")
    @classmethod
    def _validate_fs(cls, v: str) -> str:
        if v not in _VALID_FS_TYPES:
            raise ValueError(
                f"fs_type must be one of {sorted(_VALID_FS_TYPES)}, got {v!r}"
            )
        return v

    @field_validator("encryption")
    @classmethod
    def _validate_encryption(cls, v: str) -> str:
        if v not in _VALID_ENCRYPTION:
            raise ValueError(
                f"encryption must be one of {sorted(_VALID_ENCRYPTION)}, got {v!r}"
            )
        return v

    @field_validator("mount_point")
    @classmethod
    def _validate_mount_point(cls, v: str) -> str:
        v = v.strip()
        if v in {"swap", "none"}:
            return v
        if not v.startswith("/"):
            raise ValueError("mount_point must be an absolute path or 'swap'/'none'")
        return v


class DiskPlanRequest(BaseModel):
    """Request body for ``POST /disks/{disk_id}/plan``."""

    model_config = ConfigDict(extra="forbid")

    partitions: list[PartitionSpec] = Field(..., min_length=1)


class DiskPatchRequest(BaseModel):
    """Request body for ``PATCH /disks/{disk_id}`` — partial update."""

    model_config = ConfigDict(extra="forbid")

    reserved_for_storage: Optional[bool] = None
    tier: Optional[str] = None
    storage_backend: Optional[str] = None

    @field_validator("tier")
    @classmethod
    def _validate_tier(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in _VALID_TIERS:
            raise ValueError(
                f"tier must be one of {sorted(_VALID_TIERS)}, got {v!r}"
            )
        return v


# =============================================================================
# Helpers
# =============================================================================


def _tenant_id() -> str:
    """Return the JWT-bound tenant id, falling back to '__default__'."""
    ctx = getattr(g, "tenant_context", None)
    if ctx is not None:
        return getattr(ctx, "tenant_id", "__default__")
    user = getattr(g, "current_user", None) or {}
    payload = user.get("_jwt_payload", {}) if isinstance(user, dict) else {}
    return payload.get("tenant", "__default__")


def _disk_to_json(row: Any) -> dict[str, Any]:
    """Serialize a disk row (penguin-dal Row or dict) to the API shape."""
    d = row.as_dict() if hasattr(row, "as_dict") else dict(row)

    smart_attrs = d.get("smart_attributes_json")
    if isinstance(smart_attrs, str) and smart_attrs:
        try:
            smart_attrs = json.loads(smart_attrs)
        except json.JSONDecodeError:
            log.warning("disk %s: invalid smart_attributes_json", d.get("id"))
            smart_attrs = None

    def _iso(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return value.isoformat()

    return {
        "id": d.get("id"),
        "node_id": d.get("node_id"),
        "tenant_id": d.get("tenant_id"),
        "device_path": d.get("device_path"),
        "serial": d.get("serial"),
        "capacity_bytes": d.get("capacity_bytes"),
        "rotational": bool(d.get("rotational")) if d.get("rotational") is not None else False,
        "smart_status": d.get("smart_status") or "unknown",
        "smart_attributes": smart_attrs,
        "reserved_for_storage": bool(d.get("reserved_for_storage"))
        if d.get("reserved_for_storage") is not None
        else False,
        "storage_backend": d.get("storage_backend"),
        "tier": d.get("tier") or "bulk",
        "created_at": _iso(d.get("created_at")),
        "updated_at": _iso(d.get("updated_at")),
    }


def _plan_to_json(row: Any) -> dict[str, Any]:
    d = row.as_dict() if hasattr(row, "as_dict") else dict(row)
    return {
        "id": d.get("id"),
        "disk_id": d.get("disk_id"),
        "node_id": d.get("node_id"),
        "tenant_id": d.get("tenant_id"),
        "partition_index": d.get("partition_index"),
        "mount_point": d.get("mount_point"),
        "size_bytes": d.get("size_bytes"),
        "fs_type": d.get("fs_type"),
        "encryption": d.get("encryption") or "none",
    }


def _load_disk(db: Any, node_id: int, disk_id: int, tenant_id: str) -> Optional[Any]:
    """Fetch disk by (node_id, disk_id), tenant-scoped."""
    return db(
        (db.disks.id == disk_id)
        & (db.disks.node_id == node_id)
        & (db.disks.tenant_id == tenant_id)
    ).select().first()


def _load_node(db: Any, node_id: int, tenant_id: str) -> Optional[Any]:
    """Fetch node row, tenant-scoped."""
    return db(
        (db.nodes.id == node_id) & (db.nodes.tenant_id == tenant_id)
    ).select().first()


def render_sgdisk_script(disk: Any, partitions: list[PartitionSpec]) -> str:
    """Render an idempotent ``sgdisk`` script for the given plan.

    Output is intentionally deterministic so callers can diff successive
    dry-runs. Only the parts required for the spec are emitted; the live
    apply path will additionally invoke mkfs/mkswap once approved.
    """
    device = disk.device_path if hasattr(disk, "device_path") else disk.get("device_path")
    lines = [
        "#!/bin/sh",
        "set -eu",
        f"# Gough disk plan for {device}",
        f"sgdisk --zap-all {device}",
    ]
    for part in sorted(partitions, key=lambda p: p.index):
        type_code = "8200" if part.fs_type == "swap" else "8300"
        label = part.mount_point.replace("/", "_").strip("_") or "root"
        lines.append(
            f"sgdisk --new={part.index}:0:+{part.size_mb}M "
            f"--typecode={part.index}:{type_code} "
            f"--change-name={part.index}:{label} {device}"
        )
        if part.encryption in {"luks", "luks2"}:
            partdev = f"{device}{part.index}"
            lines.append(
                f"# encryption={part.encryption} for {partdev} (key applied at deploy time)"
            )
    lines.append(f"partprobe {device}")
    return "\n".join(lines) + "\n"


def detect_write_files_collision(
    db: Any, node_id: int, disk_id: int, mount_points: list[str], tenant_id: str
) -> Optional[str]:
    """Return the colliding mount_point on the *node* (excluding the same disk),

    matching the spec's ``write_files`` path-collision rule for plan compilation.
    """
    seen: set[str] = set()
    for mp in mount_points:
        if mp in {"swap", "none"}:
            continue
        if mp in seen:
            return mp
        seen.add(mp)

    rows = db(
        (db.disk_plans.node_id == node_id)
        & (db.disk_plans.disk_id != disk_id)
        & (db.disk_plans.tenant_id == tenant_id)
    ).select(db.disk_plans.mount_point)
    other = {r.mount_point for r in rows}
    for mp in mount_points:
        if mp in other:
            return mp
    return None


def _publish_smart_recheck(node_id: int, disk_id: int) -> str:
    """Publish a SMART-recheck request.

    Returns the dispatch mode actually used:
    - ``"nats"`` if a NATS client is attached at ``current_app.nats_client``
    - ``"queue"`` otherwise (a row is written to ``smart_recheck_queue`` so the
      sweeper picks it up on its next tick).
    """
    nats_client = getattr(current_app, "nats_client", None)
    payload = {
        "node_id": node_id,
        "disk_id": disk_id,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    if nats_client is not None and hasattr(nats_client, "publish"):
        try:
            nats_client.publish(
                "gough.smart.recheck", json.dumps(payload).encode("utf-8")
            )
            return "nats"
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "smart-recheck nats publish failed for node=%s disk=%s: %s",
                node_id,
                disk_id,
                exc,
            )

    db = get_db()
    if "smart_recheck_queue" not in db.tables:
        from penguin_dal import Field as DalField  # local import — avoid hard dep at import time

        db.define_table(
            "smart_recheck_queue",
            DalField("node_id", "integer", notnull=True),
            DalField("disk_id", "integer", notnull=True),
            DalField("requested_at", "datetime"),
            DalField("status", "string", default="pending"),
            migrate=True,
        )
    db.smart_recheck_queue.insert(
        node_id=node_id,
        disk_id=disk_id,
        requested_at=datetime.now(timezone.utc),
        status="pending",
    )
    db.commit()
    return "queue"


# =============================================================================
# Routes
# =============================================================================


@disks_bp.route("/<int:node_id>/disks", methods=["GET"])
@auth_required
@require_scopes("gough.disks.read")
async def list_node_disks(node_id: int):
    """List all disks attached to the given node, tenant-scoped."""
    tenant_id = _tenant_id()
    db = get_db()

    # Regression: gh-22. Node check + disks select is one unit of work --
    # off the event loop via run_db() instead of blocking the request
    # coroutine inline.
    def _fetch() -> tuple[Any, Any]:
        node = _load_node(db, node_id, tenant_id)
        if node is None:
            return None, None
        rows = (
            db((db.disks.node_id == node_id) & (db.disks.tenant_id == tenant_id))
            .select(orderby=db.disks.device_path)
        )
        return node, rows

    node, rows = await run_db(_fetch)
    if node is None:
        return jsonify({"error": "Node not found"}), 404

    return (
        jsonify(
            {
                "status": "success",
                "data": {
                    "node_id": node_id,
                    "disks": [_disk_to_json(r) for r in rows],
                },
                "meta": {
                    "version": 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "count": len(rows),
                },
            }
        ),
        200,
    )


@disks_bp.route("/<int:node_id>/disks/<int:disk_id>/plan", methods=["POST"])
@auth_required
@require_scopes("gough.disks.plan")
async def create_disk_plan(node_id: int, disk_id: int):
    """Create or replace the partitioning plan for a disk.

    Query params:
        dry-run: ``true`` returns the rendered ``sgdisk`` script without writing
        any rows. Anything else (including absence) commits the plan.
    """
    tenant_id = _tenant_id()
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    disk = await run_db(lambda: _load_disk(db, node_id, disk_id, tenant_id))
    if disk is None:
        return jsonify({"error": "Disk not found"}), 404

    raw = await request.get_json(silent=True)
    if raw is None:
        return jsonify({"error": "Request body required"}), 400

    try:
        plan_req = DiskPlanRequest.model_validate(raw)
    except ValidationError as exc:
        # Serialize validation errors to JSON-safe format (Pydantic errors may contain non-serializable context)
        errors = [
            {
                "type": e.get("type", ""),
                "loc": e.get("loc", ()),
                "msg": e.get("msg", ""),
            }
            for e in exc.errors()
        ]
        return jsonify({"error": "validation_failed", "details": errors}), 422

    # Validate partition indexes are unique
    indexes = [p.index for p in plan_req.partitions]
    if len(set(indexes)) != len(indexes):
        return (
            jsonify(
                {
                    "error": "validation_failed",
                    "details": "Partition indexes must be unique within a plan.",
                }
            ),
            422,
        )

    # Total size sanity check vs disk capacity
    total_bytes = sum(p.size_mb for p in plan_req.partitions) * 1024 * 1024
    capacity = int(disk.capacity_bytes or 0)
    if capacity and total_bytes > capacity:
        return (
            jsonify(
                {
                    "error": "validation_failed",
                    "details": (
                        f"Plan total {total_bytes} bytes exceeds disk capacity "
                        f"{capacity} bytes."
                    ),
                }
            ),
            422,
        )

    mount_points = [p.mount_point for p in plan_req.partitions]
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    collision = await run_db(
        lambda: detect_write_files_collision(
            db, node_id, disk_id, mount_points, tenant_id
        )
    )
    if collision is not None:
        return (
            jsonify(
                {
                    "error": "write_files_path_collision",
                    "details": (
                        f"Mount point {collision!r} already claimed by another "
                        f"disk plan on node {node_id}."
                    ),
                    "path": collision,
                }
            ),
            422,
        )

    sgdisk_script = render_sgdisk_script(disk, plan_req.partitions)

    dry_run = (request.args.get("dry-run", "false").lower() == "true")
    if dry_run:
        return (
            jsonify(
                {
                    "status": "success",
                    "data": {
                        "dry_run": True,
                        "sgdisk_script": sgdisk_script,
                        "partitions": [p.model_dump() for p in plan_req.partitions],
                    },
                    "meta": {
                        "version": 1,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                }
            ),
            200,
        )

    # Regression: gh-22. Delete-existing + insert-new + commit + refetch is
    # one unit of work -- stays in one run_db() closure per the house rule
    # (see app/db/run_db.py).
    def _persist_plan() -> Any:
        # Replace any existing plans for this disk (idempotent re-plan)
        db(
            (db.disk_plans.disk_id == disk_id)
            & (db.disk_plans.tenant_id == tenant_id)
        ).delete()

        created_ids: list[int] = []
        for part in plan_req.partitions:
            new_id = db.disk_plans.insert(
                node_id=node_id,
                disk_id=disk_id,
                tenant_id=tenant_id,
                partition_index=part.index,
                mount_point=part.mount_point,
                size_bytes=part.size_mb * 1024 * 1024,
                fs_type=part.fs_type,
                encryption=part.encryption,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            created_ids.append(int(new_id))
        db.commit()

        return db(db.disk_plans.id.belongs(created_ids)).select() if created_ids else []

    rows = await run_db(_persist_plan)
    return (
        jsonify(
            {
                "status": "success",
                "data": {
                    "dry_run": False,
                    "sgdisk_script": sgdisk_script,
                    "plans": [_plan_to_json(r) for r in rows],
                },
                "meta": {
                    "version": 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
        ),
        201,
    )


@disks_bp.route("/<int:node_id>/disks/<int:disk_id>", methods=["PATCH"])
@auth_required
@require_scopes("gough.disks.plan")
async def patch_disk(node_id: int, disk_id: int):
    """Partial update: dark-drive toggle (``reserved_for_storage``) + tier."""
    tenant_id = _tenant_id()
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    disk = await run_db(lambda: _load_disk(db, node_id, disk_id, tenant_id))
    if disk is None:
        return jsonify({"error": "Disk not found"}), 404

    raw = await request.get_json(silent=True)
    if raw is None:
        return jsonify({"error": "Request body required"}), 400

    try:
        patch = DiskPatchRequest.model_validate(raw)
    except ValidationError as exc:
        # Serialize validation errors to JSON-safe format (Pydantic errors may contain non-serializable context)
        errors = [
            {
                "type": e.get("type", ""),
                "loc": e.get("loc", ()),
                "msg": e.get("msg", ""),
            }
            for e in exc.errors()
        ]
        return jsonify({"error": "validation_failed", "details": errors}), 422

    update_fields: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    touched = False
    if patch.reserved_for_storage is not None:
        update_fields["reserved_for_storage"] = bool(patch.reserved_for_storage)
        touched = True
    if patch.tier is not None:
        update_fields["tier"] = patch.tier
        touched = True
    if patch.storage_backend is not None:
        update_fields["storage_backend"] = patch.storage_backend or None
        touched = True

    if not touched:
        return (
            jsonify(
                {
                    "error": "validation_failed",
                    "details": "Provide at least one of: reserved_for_storage, tier, storage_backend.",
                }
            ),
            422,
        )

    # Regression: gh-22. Update + commit + the post-commit refetch is one
    # unit of work -- stays in one run_db() closure per the house rule
    # (see app/db/run_db.py).
    def _apply_patch() -> Any:
        db(
            (db.disks.id == disk_id)
            & (db.disks.node_id == node_id)
            & (db.disks.tenant_id == tenant_id)
        ).update(**update_fields)
        db.commit()

        return _load_disk(db, node_id, disk_id, tenant_id)

    refreshed = await run_db(_apply_patch)
    return (
        jsonify(
            {
                "status": "success",
                "data": _disk_to_json(refreshed),
                "meta": {
                    "version": 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
        ),
        200,
    )


@disks_bp.route(
    "/<int:node_id>/disks/<int:disk_id>/smart-recheck", methods=["POST"]
)
@auth_required
@require_scopes("gough.disks.plan")
async def smart_recheck(node_id: int, disk_id: int):
    """Enqueue a fresh SMART probe for the smart_sweeper worker."""
    tenant_id = _tenant_id()
    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    disk = await run_db(lambda: _load_disk(db, node_id, disk_id, tenant_id))
    if disk is None:
        return jsonify({"error": "Disk not found"}), 404

    # _publish_smart_recheck() falls back to a direct penguin-dal
    # insert+commit when no NATS client is attached -- off the event loop
    # via run_db() instead of blocking the request coroutine inline.
    mode = await run_db(lambda: _publish_smart_recheck(node_id, disk_id))
    return (
        jsonify(
            {
                "status": "success",
                "data": {
                    "node_id": node_id,
                    "disk_id": disk_id,
                    "dispatch": mode,
                    "subject": "gough.smart.recheck",
                },
                "meta": {
                    "version": 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
        ),
        202,
    )


__all__ = [
    "disks_bp",
    "DiskPlanRequest",
    "DiskPatchRequest",
    "PartitionSpec",
    "render_sgdisk_script",
    "detect_write_files_collision",
]
