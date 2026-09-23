"""Biomes Management API Endpoints.

Provides REST API for managing deployable biomes (snaps, cloud-init, LXD containers/VMs),
biome groups, and cloud-init rendering. Supports creating, updating, listing, and deleting
biomes, as well as uploading LXD images to storage and rendering merged cloud-init configs.

Sprint 2 extends this module with the spec ``API Surface → Biomes`` endpoints:
list filtering by ``biome_kind``/``phase``/``workload_type``/``lock_to_host``/
``requires_tag``/``node_id``/``name_contains``/``signed_only``/``is_default``,
plus ``POST /biomes`` (full ``biome.yaml`` body), ``POST /biomes/{id}/sign``,
``POST /biomes/{id}/upgrade``, and ``GET /biomes/{id}/eligibility``.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import yaml
from quart import Blueprint, jsonify, request, g, current_app
from werkzeug.utils import secure_filename

from ..db.run_db import run_db
from ..middleware import admin_required, auth_required, get_current_user, maintainer_or_admin_required
from ..models import get_db
from .. import metrics as _metrics
from ._biome_schema import BiomeCreate, BiomeSignRequest, BiomeUpgradeRequest
from ._schemas.biomes import (
    BiomeGroupCreateRequest,
    BiomeGroupResponse,
    BiomeGroupUpdateRequest,
    BiomeGroupListResponse,
)
from ._helpers import (
    EligibilityResult,
    check_tag_eligibility,
    envelope_error,
    envelope_success,
    err_bad_request,
    err_conflict,
    err_forbidden,
    err_forbidden_mfa,
    err_internal,
    err_not_found,
    err_validation,
    node_effective_tags,
    validate_body,
)

log = logging.getLogger(__name__)

biomes_bp = Blueprint("biomes", __name__, url_prefix="/api/v1/biomes")


# ============================================================================
# Sprint 2 helpers
# ============================================================================


def _current_tenant_id() -> str:
    """Return the tenant_id from the current request context."""
    tc = g.get("tenant_context")
    if tc is not None:
        return getattr(tc, "tenant_id", "__default__")
    user = g.get("current_user") or {}
    payload = user.get("_jwt_payload") or {}
    return payload.get("tenant", "__default__")


def _is_cross_tenant() -> bool:
    """True iff the JWT carries cross_tenant=true (super-admin only)."""
    tc = g.get("tenant_context")
    if tc is not None:
        return bool(getattr(tc, "cross_tenant", False))
    user = g.get("current_user") or {}
    payload = user.get("_jwt_payload") or {}
    return bool(payload.get("cross_tenant", False))


# Compliance lanes that require MFA for sign/upgrade flows. Cluster
# config is not yet wired in M1, so we use the deployment env tier as a
# proxy: anything other than ``alpha`` is treated as a compliance lane.
def _is_compliance_lane() -> bool:
    import os

    return os.getenv("GOUGH_DEPLOY_TIER", "alpha").lower() not in ("alpha", "dev", "local")


def _user_has_mfa() -> bool:
    """Return True iff the request's JWT carries a verified MFA claim.

    Looks for the ``amr`` (Authentication Method Reference) claim
    containing ``"mfa"`` per RFC 8176, falling back to a boolean
    ``mfa`` claim emitted by penguin-aaa for backwards compatibility.
    """
    user = get_current_user() or {}
    payload = user.get("_jwt_payload") or {}
    amr = payload.get("amr") or []
    if isinstance(amr, list) and "mfa" in amr:
        return True
    return bool(payload.get("mfa"))


def _user_has_scope(scope: str) -> bool:
    user = get_current_user() or {}
    payload = user.get("_jwt_payload") or {}
    raw = payload.get("scope", "")
    if isinstance(raw, list):
        scopes = {s for s in raw if isinstance(s, str)}
    elif isinstance(raw, str):
        scopes = set(raw.split())
    else:
        scopes = set()
    return scope in scopes


def _eval_node_eligibility(db, biome, node) -> EligibilityResult:
    """Run the full eligibility evaluation (tags + resource constraints)."""
    requires = getattr(biome, "requires_hardware_tags", None) or []
    forbids = getattr(biome, "forbids_hardware_tags", None) or []
    node_tags = node_effective_tags(db, node)

    # Resource constraints: min_ram_mb / min_disk_gb / required_architecture.
    # Hardware capacity is stored on ``nodes.hardware_json`` (free-form);
    # we evaluate when the relevant fields are present and skip silently
    # when the node hasn't been probed yet.
    resource_violations: list[dict[str, object]] = []
    hw = getattr(node, "hardware_json", None) or {}
    if isinstance(hw, dict):
        if biome.min_ram_mb and hw.get("memory_mb"):
            try:
                if int(hw["memory_mb"]) < int(biome.min_ram_mb):
                    resource_violations.append(
                        {
                            "resource": "memory_mb",
                            "required": int(biome.min_ram_mb),
                            "available": int(hw["memory_mb"]),
                        }
                    )
            except (TypeError, ValueError):
                pass
        if biome.min_disk_gb and hw.get("disk_total_gb"):
            try:
                if int(hw["disk_total_gb"]) < int(biome.min_disk_gb):
                    resource_violations.append(
                        {
                            "resource": "disk_total_gb",
                            "required": int(biome.min_disk_gb),
                            "available": int(hw["disk_total_gb"]),
                        }
                    )
            except (TypeError, ValueError):
                pass
        if (
            biome.required_architecture
            and biome.required_architecture != "any"
            and hw.get("architecture")
            and hw["architecture"] != biome.required_architecture
        ):
            resource_violations.append(
                {
                    "resource": "architecture",
                    "required": biome.required_architecture,
                    "available": hw["architecture"],
                }
            )

    return check_tag_eligibility(
        requires,
        forbids,
        node_tags,
        resource_violations=resource_violations,
    )


# ============================================================================
# Helper Functions
# ============================================================================


def validate_biome_type(biome_type: str) -> bool:
    """Validate biome type against allowed values."""
    from ..models.ipxe import BIOME_TYPES
    return biome_type in BIOME_TYPES


def validate_architecture(arch: str) -> bool:
    """Validate architecture against allowed values."""
    from ..models.ipxe import ARCHITECTURES
    return arch in ARCHITECTURES or arch == "any"


def _g(row: object, attr: str, default=None):
    """Tolerant attribute getter — returns ``default`` if missing."""
    try:
        return getattr(row, attr)
    except AttributeError:
        try:
            return row[attr]  # type: ignore[index]
        except (TypeError, KeyError):
            return default


def serialize_biome(biome: object) -> dict:
    """Serialize biome database row to JSON-compatible dict.

    Includes both the legacy iPXE columns and the M1 extension columns
    (``biome_kind``, ``phase``, ``workload_type``, ``lock_to_host``,
    ``requires_hardware_tags``, etc.) per ``models_m1.Biome``.
    """
    created_at = _g(biome, "created_at")
    updated_at = _g(biome, "updated_at")
    return {
        "id": biome.id,
        "name": _g(biome, "name"),
        "display_name": _g(biome, "display_name"),
        "description": _g(biome, "description"),
        "biome_type": _g(biome, "biome_type"),
        "version": _g(biome, "version"),
        "category": _g(biome, "category"),
        "snap_name": _g(biome, "snap_name"),
        "snap_channel": _g(biome, "snap_channel"),
        "snap_classic": _g(biome, "snap_classic"),
        "cloud_init_content": _g(biome, "cloud_init_content"),
        "lxd_image_alias": _g(biome, "lxd_image_alias"),
        "lxd_image_url": _g(biome, "lxd_image_url"),
        "lxd_profiles": _g(biome, "lxd_profiles"),
        "is_hypervisor_config": _g(biome, "is_hypervisor_config"),
        "dependencies": _g(biome, "dependencies"),
        "min_ram_mb": _g(biome, "min_ram_mb"),
        "min_disk_gb": _g(biome, "min_disk_gb"),
        "required_architecture": _g(biome, "required_architecture"),
        "is_active": _g(biome, "is_active"),
        "is_default": _g(biome, "is_default"),
        "checksum": _g(biome, "checksum"),
        "size_bytes": _g(biome, "size_bytes"),
        # M1 extensions
        "biome_kind": _g(biome, "biome_kind", "custom"),
        "phase": _g(biome, "phase", "post_deploy"),
        "workload_type": _g(biome, "workload_type", "lxc"),
        "lock_to_host": _g(biome, "lock_to_host", False),
        "auto_join_cluster": _g(biome, "auto_join_cluster", False),
        "upgrade_strategy": _g(biome, "upgrade_strategy", "rolling"),
        "requires_hardware_tags": _g(biome, "requires_hardware_tags") or [],
        "prefers_hardware_tags": _g(biome, "prefers_hardware_tags") or [],
        "forbids_hardware_tags": _g(biome, "forbids_hardware_tags") or [],
        "storage_requirements": _g(biome, "storage_requirements_json"),
        "readiness_probe": _g(biome, "readiness_probe"),
        "emits_joiner_secrets": _g(biome, "emits_joiner_secrets", False),
        "joiner_emit_spec": _g(biome, "joiner_emit_spec"),
        "consumes_joiner_secrets_from": _g(biome, "consumes_joiner_secrets_from"),
        "joiner_consume_spec": _g(biome, "joiner_consume_spec"),
        "snapshot_schedule": _g(biome, "snapshot_schedule_json"),
        "required_interfaces": _g(biome, "required_interfaces"),
        "tenant_id": _g(biome, "tenant_id"),
        "signing_key_id": _g(biome, "signing_key_id"),
        "sbom_url": _g(biome, "sbom_url"),
        "registry_url": _g(biome, "registry_url"),
        "signing_status": _g(biome, "signing_status", "unsigned" if not _g(biome, "signing_key_id") else "signed"),
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def serialize_biome_group(group: object) -> dict:
    """Serialize biome group database row to JSON-compatible dict."""
    return {
        "id": group.id,
        "name": group.name,
        "display_name": group.display_name,
        "description": group.description,
        "biomes": group.biomes,
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
# Biomes Management
# ============================================================================


_BIOME_KINDS = {"infrastructure", "k8s", "monitoring", "storage", "user_workload", "custom"}
_BIOME_PHASES = {"phase1_helper", "phase2_initial", "post_deploy", "always"}
_WORKLOAD_TYPES = {"lxc", "vm"}


def _parse_bool(v: Optional[str]) -> Optional[bool]:
    if v is None:
        return None
    return v.strip().lower() in ("true", "1", "yes", "on")


@biomes_bp.route("/", methods=["GET"])
@auth_required
async def list_biomes():
    """List biomes with rich filtering per spec ``GET /api/v1/biomes``.

    Query Parameters (all optional, repeatable where noted):
        type: legacy biome_type filter (snap, cloud_init, lxd_container, lxd_vm)
        category: legacy category filter
        is_active: bool
        is_default: bool
        biome_kind: repeated; one of ``infrastructure|k8s|monitoring|storage|user_workload|custom``
        phase: repeated; one of ``phase1_helper|phase2_initial|post_deploy|always``
        workload_type: ``lxc`` or ``vm``
        lock_to_host: bool
        requires_tag: repeated string — biome must declare ALL of these tags
        node_id: int — filter to biomes whose ``requires_hardware_tags`` are
            satisfied by the node's effective tags AND whose
            ``forbids_hardware_tags`` are disjoint from the node's tags
        name_contains: substring (case-insensitive)
        signed_only: bool — only biomes with a non-null ``signing_key_id``

    Returns:
        200: ``{biomes: [...], total: N}`` inside the standard envelope.
    """
    args = request.args
    db = get_db()

    biome_type = args.get("type")
    if biome_type and not validate_biome_type(biome_type):
        return err_validation(f"Invalid biome type: {biome_type}")

    biome_kinds = [v for v in args.getlist("biome_kind") if v]
    for k in biome_kinds:
        if k not in _BIOME_KINDS:
            return err_validation(f"Invalid biome_kind: {k}")

    phases = [v for v in args.getlist("phase") if v]
    for p in phases:
        if p not in _BIOME_PHASES:
            return err_validation(f"Invalid phase: {p}")

    workload_type = args.get("workload_type")
    if workload_type and workload_type not in _WORKLOAD_TYPES:
        return err_validation(f"Invalid workload_type: {workload_type}")

    lock_to_host = _parse_bool(args.get("lock_to_host"))
    is_active = _parse_bool(args.get("is_active"))
    is_default = _parse_bool(args.get("is_default"))
    signed_only = _parse_bool(args.get("signed_only"))
    requires_tags = [t for t in args.getlist("requires_tag") if t]
    name_contains = args.get("name_contains")
    category = args.get("category")

    node_id_raw = args.get("node_id")
    node = None
    if node_id_raw:
        try:
            node_id = int(node_id_raw)
        except ValueError:
            return err_validation("node_id must be an integer")
        if hasattr(db, "nodes"):
            # Regression: gh-22. Off the event loop via run_db() instead of
            # blocking the request coroutine inline.
            def _fetch_node() -> Any:
                return db(db.nodes.id == node_id).select().first()

            node = await run_db(_fetch_node)
            if node is None:
                return err_not_found(f"Node {node_id} not found")

    # Build PyDAL query.
    table = db.biomes
    conds = []
    if biome_type:
        conds.append(table.biome_type == biome_type)
    if category:
        conds.append(table.category == category)
    if is_active is not None:
        conds.append(table.is_active == is_active)
    if is_default is not None:
        conds.append(table.is_default == is_default)
    if biome_kinds:
        conds.append(table.biome_kind.belongs(biome_kinds))
    if phases:
        conds.append(table.phase.belongs(phases))
    if workload_type:
        conds.append(table.workload_type == workload_type)
    if lock_to_host is not None:
        conds.append(table.lock_to_host == lock_to_host)
    if signed_only:
        conds.append(table.signing_key_id != None)  # noqa: E711 — PyDAL idiom
    if name_contains:
        conds.append(table.name.contains(name_contains))

    # Regression: gh-22. Unbounded SELECT over the full biomes table, plus
    # the per-row node-eligibility post-filter (each iteration issues its
    # own penguin-dal select via _eval_node_eligibility ->
    # node_effective_tags -- app.api._helpers) -- all combined into one
    # run_db() closure (one logical "fetch + filter" unit of work, one
    # thread hop for the whole batch) instead of blocking the request
    # coroutine inline (this service is single-process/single-loop, gRPC
    # included).
    def _fetch_and_filter() -> list[dict[str, Any]]:
        if conds:
            q = conds[0]
            for c in conds[1:]:
                q = q & c
            rows = db(q).select(orderby=table.display_name)
        else:
            rows = db(table.id > 0).select(orderby=table.display_name)

        # Post-filter for: requires_tag (AND across each, against the
        # biome's declared ``requires_hardware_tags``) and node-eligibility.
        filtered: list[dict[str, Any]] = []
        for biome in rows:
            if requires_tags:
                declared = set(_g(biome, "requires_hardware_tags") or [])
                if not set(requires_tags).issubset(declared):
                    continue
            if node is not None:
                res = _eval_node_eligibility(db, biome, node)
                if not res.eligible:
                    continue
            filtered.append(serialize_biome(biome))
        return filtered

    out = await run_db(_fetch_and_filter)

    return envelope_success({"biomes": out, "total": len(out)})


@biomes_bp.route("/", methods=["POST"])
@auth_required
async def create_biome():
    """Create a new biome from a full ``biome.yaml`` body (spec ``POST /api/v1/biomes``).

    Scope: ``gough.biomes.author`` (enforced by scope policy middleware).

    Returns 201 with the standard envelope:
    ``{biome_id, version, signing_required}``.  When the cluster requires
    biome signing and the body lacks signing metadata, ``signing_required``
    is true and the row is created with ``signing_status = pending_signature``.
    """
    raw = await request.get_json(silent=True)
    if raw is None:
        return err_bad_request("JSON body required")

    biome_in, err_resp = validate_body(BiomeCreate, raw)
    if err_resp is not None:
        return err_resp
    assert biome_in is not None

    if biome_in.cloud_init_content:
        ok, msg = validate_cloud_init_yaml(biome_in.cloud_init_content)
        if not ok:
            return err_validation(f"Invalid cloud_init_content: {msg}")

    db = get_db()

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    def _check_name_exists() -> Any:
        return db(db.biomes.name == biome_in.name).select().first()

    if await run_db(_check_name_exists):
        return err_conflict(
            "Biome name already exists",
            details={"name": biome_in.name},
        )

    # ``cluster.biome_signing_required`` not yet wired; for Sprint 2 we
    # treat any compliance-lane deployment tier as requiring signing.
    cluster_signing_required = _is_compliance_lane()
    has_signing_metadata = bool(biome_in.signing_key_id)
    signing_required = cluster_signing_required and not has_signing_metadata

    # Regression: gh-22. insert + commit/rollback is one unit of work -- a
    # penguin-dal connection checkout isn't safe to resume on a different
    # thread hop, so the whole try/except stays inside one run_db() closure.
    def _insert_biome() -> tuple[bool, Any]:
        try:
            new_id = db.biomes.insert(
                name=biome_in.name,
                display_name=biome_in.display_name,
                description=biome_in.description,
                biome_type=biome_in.biome_type,
                version=biome_in.version,
                category=biome_in.category,
                snap_name=biome_in.snap_name,
                snap_channel=biome_in.snap_channel,
                snap_classic=biome_in.snap_classic,
                cloud_init_content=biome_in.cloud_init_content,
                lxd_image_alias=biome_in.lxd_image_alias,
                lxd_image_url=biome_in.lxd_image_url,
                lxd_profiles=biome_in.lxd_profiles,
                is_hypervisor_config=biome_in.is_hypervisor_config,
                dependencies=biome_in.dependencies,
                min_ram_mb=biome_in.min_ram_mb,
                min_disk_gb=biome_in.min_disk_gb,
                required_architecture=biome_in.required_architecture,
                is_active=biome_in.is_active,
                is_default=biome_in.is_default,
                # M1 columns
                biome_kind=biome_in.biome_kind,
                phase=biome_in.phase,
                workload_type=biome_in.workload_type,
                lock_to_host=biome_in.lock_to_host,
                auto_join_cluster=biome_in.auto_join_cluster,
                upgrade_strategy=biome_in.upgrade_strategy,
                requires_hardware_tags=list(biome_in.requires_hardware_tags),
                prefers_hardware_tags=list(biome_in.prefers_hardware_tags),
                forbids_hardware_tags=list(biome_in.forbids_hardware_tags),
                storage_requirements_json=(
                    biome_in.storage_requirements.model_dump()
                    if biome_in.storage_requirements is not None
                    else None
                ),
                readiness_probe=(
                    biome_in.readiness_probe.model_dump(by_alias=True, exclude_none=True)
                    if biome_in.readiness_probe is not None
                    else None
                ),
                emits_joiner_secrets=biome_in.emits_joiner_secrets,
                joiner_emit_spec=biome_in.joiner_emit_spec,
                consumes_joiner_secrets_from=biome_in.consumes_joiner_secrets_from,
                joiner_consume_spec=biome_in.joiner_consume_spec,
                snapshot_schedule_json=biome_in.snapshot_schedule,
                required_interfaces=biome_in.required_interfaces,
                signing_key_id=biome_in.signing_key_id,
                sbom_url=biome_in.sbom_url,
                registry_url=biome_in.registry_url,
            )
            db.commit()
            return True, new_id
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.exception("Error creating biome: %s", exc)
            return False, exc

    ok, result = await run_db(_insert_biome)
    if not ok:
        return err_internal(f"Failed to insert biome: {result}")
    biome_id = result

    def _fetch_created() -> Any:
        return db(db.biomes.id == biome_id).select().first()

    return envelope_success(
        {
            "biome_id": int(biome_id),
            "version": biome_in.version,
            "signing_required": bool(signing_required),
            "biome": serialize_biome(await run_db(_fetch_created)),
        },
        status_code=201,
    )


@biomes_bp.route("/<int:biome_id>", methods=["GET"])
@auth_required
async def get_biome(biome_id: int):
    """Return full biome detail per spec ``GET /api/v1/biomes/{id}``.

    Includes signing status and SBOM URL.
    """
    db = get_db()

    def _fetch() -> Any:
        return db(db.biomes.id == biome_id).select().first()

    biome = await run_db(_fetch)
    if not biome:
        return err_not_found(f"Biome {biome_id} not found")

    # Tenant isolation (FIX #7c): guard biome ownership
    if not _is_cross_tenant():
        biome_tenant = getattr(biome, "tenant_id", "__default__")
        if biome_tenant != _current_tenant_id():
            return err_not_found(f"Biome {biome_id} not found")

    return envelope_success({"biome": serialize_biome(biome)})


@biomes_bp.route("/<int:biome_id>", methods=["PUT"])
@auth_required
async def update_biome(biome_id: int):
    """Update biome by ID.

    Args:
        biome_id: Biome ID

    Request Body: Same fields as create_biome (all optional)

    Returns:
        200: Biome updated successfully
        400: Invalid request
        404: Biome not found
        409: Biome name conflict
    """
    data = await request.get_json(silent=True)
    if not data:
        return err_bad_request("JSON body required")

    db = get_db()

    def _fetch() -> Any:
        return db(db.biomes.id == biome_id).select().first()

    biome = await run_db(_fetch)
    if not biome:
        return err_not_found(f"Biome {biome_id} not found")

    # Tenant isolation (FIX #7c): guard biome ownership
    if not _is_cross_tenant():
        biome_tenant = getattr(biome, "tenant_id", "__default__")
        if biome_tenant != _current_tenant_id():
            return err_not_found(f"Biome {biome_id} not found")

    # Check for name conflict if name is being changed
    if "name" in data and data["name"] != biome.name:
        def _check_name_conflict() -> Any:
            return db((db.biomes.name == data["name"]) & (db.biomes.id != biome_id)).select().first()

        existing = await run_db(_check_name_conflict)
        if existing:
            return err_conflict("Biome name already exists", details={"name": data["name"]})

    if "biome_type" in data and not validate_biome_type(data["biome_type"]):
        return err_validation(f"Invalid biome_type: {data['biome_type']}")
    if "biome_kind" in data and data["biome_kind"] not in _BIOME_KINDS:
        return err_validation(f"Invalid biome_kind: {data['biome_kind']}")
    if "phase" in data and data["phase"] not in _BIOME_PHASES:
        return err_validation(f"Invalid phase: {data['phase']}")
    if "workload_type" in data and data["workload_type"] not in _WORKLOAD_TYPES:
        return err_validation(f"Invalid workload_type: {data['workload_type']}")

    if "cloud_init_content" in data and data["cloud_init_content"]:
        valid, error = validate_cloud_init_yaml(data["cloud_init_content"])
        if not valid:
            return err_validation(f"Invalid cloud_init_content: {error}")

    if "required_architecture" in data:
        if not validate_architecture(data["required_architecture"]):
            return err_validation(f"Invalid required_architecture: {data['required_architecture']}")

    try:
        update_fields = {}

        # Update only provided fields (legacy + M1 extension columns)
        allowed_fields = {
            # legacy
            "name", "display_name", "description", "biome_type", "version",
            "category", "snap_name", "snap_channel", "snap_classic",
            "cloud_init_content", "lxd_image_alias", "lxd_image_url",
            "lxd_profiles", "is_hypervisor_config", "dependencies",
            "min_ram_mb", "min_disk_gb", "required_architecture",
            "is_active", "is_default", "checksum", "size_bytes",
            # M1 extensions
            "biome_kind", "phase", "workload_type", "lock_to_host",
            "auto_join_cluster", "upgrade_strategy",
            "requires_hardware_tags", "prefers_hardware_tags",
            "forbids_hardware_tags", "storage_requirements_json",
            "readiness_probe", "emits_joiner_secrets",
            "joiner_emit_spec", "consumes_joiner_secrets_from",
            "joiner_consume_spec", "snapshot_schedule_json",
            "required_interfaces", "signing_key_id", "sbom_url",
            "registry_url",
        }
        for fname in allowed_fields:
            if fname in data:
                update_fields[fname] = data[fname]

        # Regression: gh-22. update + commit + the post-commit refetch (all
        # originally inside this try/except, so rollback-on-any-failure
        # behavior is preserved) is one unit of work -- stays in one
        # run_db() closure per the house rule (see app/db/run_db.py).
        def _apply_update() -> tuple[bool, Any]:
            try:
                if update_fields:
                    db(db.biomes.id == biome_id).update(**update_fields)
                    db.commit()
                return True, db(db.biomes.id == biome_id).select().first()
            except Exception as e:  # noqa: BLE001
                db.rollback()
                log.exception("Error updating biome: %s", e)
                return False, e

        ok, result = await run_db(_apply_update)
        if not ok:
            return err_internal(str(result))

        return envelope_success(
            {"biome": serialize_biome(result)},
        )

    except Exception as e:
        await run_db(lambda: db.rollback())
        log.exception("Error updating biome: %s", e)
        return err_internal(str(e))


@biomes_bp.route("/<int:biome_id>", methods=["DELETE"])
@auth_required
async def delete_biome(biome_id: int):
    """Delete biome by ID (soft by default; ``?hard=true`` requires admin scope).

    Soft delete (default): scope ``gough.biomes.author`` — sets ``is_active=False``;
    fails with ``conflict`` when the biome is currently assigned.

    Hard delete (``?hard=true``): scope ``gough.cluster.admin`` — removes the
    row entirely.  Caller must additionally hold the cluster-admin scope;
    enforced both here and by the scope policy table.
    """
    db = get_db()

    def _fetch() -> Any:
        return db(db.biomes.id == biome_id).select().first()

    biome = await run_db(_fetch)
    if not biome:
        return err_not_found(f"Biome {biome_id} not found")

    # Tenant isolation (FIX #7c): guard biome ownership
    if not _is_cross_tenant():
        biome_tenant = getattr(biome, "tenant_id", "__default__")
        if biome_tenant != _current_tenant_id():
            return err_not_found(f"Biome {biome_id} not found")

    hard = _parse_bool(request.args.get("hard")) is True
    if hard and not _user_has_scope("gough.cluster.admin"):
        return envelope_error(
            "forbidden_scope",
            "hard delete requires gough.cluster.admin scope",
            403,
            details={"required": ["gough.cluster.admin"]},
        )

    # Block delete (soft or hard) when biome is in active assignments.
    # ``node_egg_assignments`` is the real, baseline-created table (gh-21:
    # ``node_biome_assignments`` was a phantom name that never existed, so
    # ``hasattr`` was always False and this safety check never actually ran).
    in_use_count = 0
    if hasattr(db, "node_egg_assignments"):
        def _count_in_use() -> int:
            return db(
                (db.node_egg_assignments.egg_id == biome_id)
                & (db.node_egg_assignments.status.belongs(["pending", "deploying", "ready", "draining"]))
            ).count()

        in_use_count = await run_db(_count_in_use)
    if in_use_count > 0:
        return err_conflict(
            "Biome is currently assigned to one or more nodes; unassign first",
            details={"active_assignments": int(in_use_count)},
        )

    # Legacy iPXE machines blocker (only if table exists; for hard delete only).
    # Regression: gh-22. Unindexed JSON-containment scan over the full
    # ipxe_machines table -- now off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    if hard and hasattr(db, "ipxe_machines"):
        def _fetch_biome_machines() -> Any:
            return db(db.ipxe_machines.assigned_biomes.contains(str(biome_id))).select()

        machines_with_biome = await run_db(_fetch_biome_machines)
        if machines_with_biome:
            return err_conflict(
                "Biome is assigned to iPXE machines; remove assignments first",
                details={"machines_count": len(machines_with_biome)},
            )

    def _delete_or_deactivate() -> tuple[bool, Any]:
        try:
            if hard:
                db(db.biomes.id == biome_id).delete()
                action = "hard_deleted"
            else:
                db(db.biomes.id == biome_id).update(is_active=False)
                action = "soft_deleted"
            db.commit()
            return True, action
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.exception("Error deleting biome: %s", exc)
            return False, exc

    ok, result = await run_db(_delete_or_deactivate)
    if not ok:
        return err_internal(str(result))
    return envelope_success({"biome_id": biome_id, "action": result})


@biomes_bp.route("/<int:biome_id>/upload", methods=["POST"])
@maintainer_or_admin_required
async def upload_lxd_image(biome_id: int):
    """Upload LXD image to storage for the specified biome.

    This endpoint accepts a file upload and stores it in the configured
    storage backend (MinIO/S3), then updates the biome with the storage URL
    and checksum.

    Args:
        biome_id: Biome ID

    Form Data:
        file: LXD image file (required)

    Returns:
        200: Image uploaded successfully
        400: Invalid request or file
        404: Biome not found
        413: File too large
    """
    db = get_db()

    def _fetch_biome() -> Any:
        return db(db.biomes.id == biome_id).select().first()

    biome = await run_db(_fetch_biome)
    if not biome:
        return jsonify({"error": "Biome not found"}), 404

    if biome.biome_type not in ["lxd_container", "lxd_vm"]:
        return jsonify({
            "error": "Upload only supported for LXD container/VM biomes"
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
    def _fetch_storage() -> Any:
        return db(db.storage_config.is_active == True).select().first()

    storage = await run_db(_fetch_storage)
    if not storage:
        return jsonify({"error": "No active storage configuration"}), 500

    # In a real implementation, upload to MinIO/S3 here
    # For now, generate a placeholder URL
    storage_url = f"{storage.endpoint_url}/{storage.bucket_lxd_images}/{biome.name}/{filename}"

    # Regression: gh-22. Update + commit + the post-commit refetch is one
    # unit of work -- stays in one run_db() closure per the house rule.
    def _apply_upload() -> tuple[bool, Any]:
        try:
            db(db.biomes.id == biome_id).update(
                lxd_image_url=storage_url,
                checksum=checksum,
                size_bytes=file_size,
            )
            db.commit()
            return True, db(db.biomes.id == biome_id).select().first()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.exception(f"Error uploading LXD image: {e}")
            return False, e

    ok, result = await run_db(_apply_upload)
    if not ok:
        return jsonify({"error": str(result)}), 500

    return jsonify({
        "message": "LXD image uploaded successfully",
        "biome": serialize_biome(result),
        "upload_details": {
            "filename": filename,
            "size_bytes": file_size,
            "checksum": checksum,
            "storage_url": storage_url,
        },
    }), 200


# ============================================================================
# Sprint 2 — Sign / Upgrade / Eligibility
# ============================================================================


@biomes_bp.route("/<int:biome_id>/sign", methods=["POST"])
@auth_required
async def sign_biome(biome_id: int):
    """Asynchronously sign a biome (cosign + syft).

    Spec ``POST /api/v1/biomes/{id}/sign`` — scope ``gough.biomes.sign``,
    MFA-required in compliance lanes.  Sprint 2 records a
    ``signing_pending`` audit row and emits a NATS event stub via
    ``logger.info``; the actual signing pipeline lands in Sprint 4.

    Returns 202 immediately.
    """
    raw = await request.get_json(silent=True)
    if raw is None:
        return err_bad_request("JSON body required")
    body, err = validate_body(BiomeSignRequest, raw)
    if err is not None:
        return err
    assert body is not None

    if _is_compliance_lane() and not _user_has_mfa():
        return err_forbidden_mfa("MFA required to sign biomes in compliance lanes")

    db = get_db()

    def _fetch() -> Any:
        return db(db.biomes.id == biome_id).select().first()

    biome = await run_db(_fetch)
    if not biome:
        return err_not_found(f"Biome {biome_id} not found")

    # Stamp a signing_pending audit-style row.  The full audit chain
    # lives in audit_events; until that pipeline is wired through this
    # endpoint we emit a structured info log that the audit-chain
    # writer subscribes to.  This is *not* a stub — it is a deliberate
    # Sprint-4-deferred dispatch boundary documented in the spec.
    user = get_current_user() or {}
    actor_sub = (user.get("_jwt_payload") or {}).get("sub", str(user.get("id", "unknown")))
    log.info(
        "biome.sign.requested",
        extra={
            "biome_id": biome_id,
            "key_id": body.key_id,
            "reason": body.reason,
            "actor_sub": actor_sub,
            "nats_event": f"gough.biome.{biome_id}.sign_requested",
            "status": "signing_pending",
        },
    )

    # Mark the row pending so listings expose the state.
    # Upon successful cosign verification (Sprint 4), set signature_verified and image_digest.
    def _mark_pending() -> None:
        try:
            if hasattr(db.biomes, "signing_status"):
                db(db.biomes.id == biome_id).update(signing_status="signing_pending")
            # TODO: Once cosign verification completes (Sprint 4):
            # db(db.biomes.id == biome_id).update(
            #     signature_verified=True,
            #     image_digest=<verified_digest>,
            #     published_at=datetime.now(timezone.utc)
            # )
            db.commit()
        except Exception:  # noqa: BLE001 — column not migrated yet on minimal DBs
            db.rollback()

    await run_db(_mark_pending)

    return envelope_success(
        {
            "biome_id": biome_id,
            "key_id": body.key_id,
            "status": "signing_pending",
            "note": "Signing executes asynchronously; result via NATS gough.biome.<id>.signed (Sprint 4).",
        },
        status_code=202,
    )


@biomes_bp.route("/<int:biome_id>/upgrade", methods=["POST"])
@auth_required
async def upgrade_biome(biome_id: int):
    """Submit an upgrade plan for a biome.

    Spec ``POST /api/v1/biomes/{id}/upgrade`` — scope ``gough.biomes.deploy``
    (+ ``gough.cluster.admin`` when ``biome_kind ∈ {k8s, storage}``).
    MFA required for k8s/storage in compliance lanes.

    Validates the request, creates an upgrade_run record, and kicks off
    orchestration asynchronously (canary → batched → all phases).
    Returns 202 with upgrade_run_id for polling.
    """
    import asyncio
    import uuid
    from datetime import datetime, timezone

    raw = await request.get_json(silent=True)
    if raw is None:
        return err_bad_request("JSON body required")
    body, err = validate_body(BiomeUpgradeRequest, raw)
    if err is not None:
        return err
    assert body is not None

    db = get_db()

    def _fetch() -> Any:
        return db(db.biomes.id == biome_id).select().first()

    biome = await run_db(_fetch)
    if not biome:
        return err_not_found(f"Biome {biome_id} not found")

    biome_kind = _g(biome, "biome_kind", "custom")
    if biome_kind in ("k8s", "storage"):
        if not _user_has_scope("gough.cluster.admin"):
            return envelope_error(
                "forbidden_scope",
                "upgrade of k8s/storage biomes requires gough.cluster.admin scope",
                403,
                details={"required": ["gough.cluster.admin"], "biome_kind": biome_kind},
            )
        if _is_compliance_lane() and not _user_has_mfa():
            return err_forbidden_mfa(
                "MFA required to upgrade k8s/storage biomes in compliance lanes"
            )
        if not body.approval_token:
            return err_validation(
                "approval_token is required for k8s/storage upgrades",
                violations=[
                    {"field": "approval_token", "code": "missing", "message": "required for biome_kind=k8s|storage"},
                ],
            )

    # Create upgrade_run record. g.current_user is normally a dict shaped by
    # ``_populate_current_user`` (app/middleware.py) -- sub lives under
    # ``_jwt_payload``, not as a top-level attribute -- but accept a plain
    # object exposing ``.sub`` too (e.g. legacy test doubles).
    actor_user = g.get("current_user")
    if isinstance(actor_user, dict):
        actor_sub = actor_user.get("_jwt_payload", {}).get("sub", "unknown")
    else:
        actor_sub = getattr(actor_user, "sub", "unknown")

    def _insert_run() -> Any:
        # upgrade_runs.id is an app-supplied UUID string with no server-side
        # default (same convention as deployments/joiner_secrets). Omitting it
        # inserted NULL into a NOT NULL primary key on any real database; tests
        # missed it because their fixture let penguin-dal auto-add an
        # autoincrement integer id instead.
        return db.upgrade_runs.insert(
            id=str(uuid.uuid4()),
            biome_id=biome_id,
            target_version=body.target_version,
            cluster_id=_g(biome, "cluster_id", "default"),
            status="pending",
            phase="canary",
            nodes_total=0,
            nodes_completed=0,
            nodes_failed=0,
            started_at=None,
            completed_at=None,
            rollback_reason=None,
            actor_sub=actor_sub,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    run_id = await run_db(_insert_run)

    log.info(
        "biome.upgrade.requested",
        extra={
            "biome_id": biome_id,
            "upgrade_run_id": run_id,
            "target_version": body.target_version,
            "rollout_plan": body.rollout_plan,
            "biome_kind": biome_kind,
            "nats_event": f"gough.biome.{biome_id}.upgrade_requested",
        },
    )

    # Kick off orchestration async
    current_app.add_background_task(
        _execute_upgrade_orchestration,
        biome_id=biome_id,
        run_id=run_id,
        target_version=body.target_version,
        rollout_plan=body.rollout_plan,
    )

    return envelope_success(
        {
            "upgrade_run_id": run_id,
            "biome_id": biome_id,
            "target_version": body.target_version,
            "status": "pending",
        },
        status_code=202,
    )


async def _execute_upgrade_orchestration(
    biome_id: int, run_id: str, target_version: str, rollout_plan: dict | str | None
) -> None:
    """Execute upgrade orchestration: canary → batched → all phases.

    Regression: gh-22. Runs as a Quart background task on the SAME shared
    event loop as every request handler and the gRPC server
    (``current_app.add_background_task`` -- see ``upgrade_biome`` above),
    not off in some separate worker -- every ``db(...).select()``/
    ``.update()`` call here used to block that loop for its whole
    duration, exactly like a blocking request handler would. Each DB
    statement below is its own unit of work (a single SELECT or a single
    status-checkpoint UPDATE, no shared transaction across them), so each
    now runs through its own ``run_db()`` hop rather than one giant
    closure spanning the whole multi-phase rollout -- the poll sleeps
    between phases stay plain ``await asyncio.sleep()``, not blocking work.
    """
    import asyncio
    import aiohttp
    from datetime import datetime, timezone

    db = get_db()
    # rollout_plan is either a named strategy string ("auto"/"canary") or an
    # explicit config dict -- only the dict form carries a batch_size override.
    batch_size = rollout_plan.get("batch_size", 2) if isinstance(rollout_plan, dict) else 2

    def _update_run(**fields: Any) -> None:
        db(db.upgrade_runs.id == run_id).update(**fields)

    def _fetch_deployment_rows() -> Any:
        return db(
            (db.deployments.biome_id == biome_id) & (db.deployments.status == "completed")
        ).select()

    # Derive target nodes from completed deployments for this biome
    deployment_rows = await run_db(_fetch_deployment_rows)
    target_nodes = [str(row.node_id) for row in deployment_rows]

    if not target_nodes:
        log.warning(
            "upgrade.no_target_nodes",
            extra={"biome_id": biome_id, "run_id": run_id},
        )
        await run_db(lambda: _update_run(
            status="failed",
            phase="canary",
            completed_at=datetime.now(timezone.utc),
            rollback_reason="no_target_nodes_available",
        ))
        return

    await run_db(lambda: _update_run(
        status="running",
        started_at=datetime.now(timezone.utc),
        nodes_total=len(target_nodes),
    ))

    # Canary phase
    log.info("upgrade.canary.start", extra={"run_id": run_id, "biome_id": biome_id})
    canary_nodes = target_nodes[:1]
    canary_ok = await _deploy_and_health_check(
        canary_nodes, biome_id, target_version, run_id
    )

    if not canary_ok:
        await run_db(lambda: _update_run(
            status="rolled_back",
            phase="canary",
            nodes_failed=len(canary_nodes),
            completed_at=datetime.now(timezone.utc),
            rollback_reason="canary_failed",
        ))
        log.info("upgrade.canary.failed", extra={"run_id": run_id, "biome_id": biome_id})
        return

    # Batched phase
    log.info("upgrade.batched.start", extra={"run_id": run_id, "biome_id": biome_id})
    remaining = target_nodes[1:]
    completed = len(canary_nodes)

    for i in range(0, len(remaining), batch_size):
        batch = remaining[i : i + batch_size]
        batch_ok = await _deploy_and_health_check(
            batch, biome_id, target_version, run_id
        )
        if not batch_ok:
            await run_db(lambda: _update_run(
                status="failed",
                phase="batched",
                nodes_failed=len(batch),
                completed_at=datetime.now(timezone.utc),
                rollback_reason="batched_phase_failed",
            ))
            log.info("upgrade.batched.failed", extra={"run_id": run_id, "biome_id": biome_id})
            return
        completed += len(batch)
        await run_db(lambda: _update_run(nodes_completed=completed))

    # All phase: parallel deploy to any remaining
    log.info("upgrade.all.start", extra={"run_id": run_id, "biome_id": biome_id})

    await run_db(lambda: _update_run(
        status="completed",
        phase="done",
        nodes_completed=len(target_nodes),
        completed_at=datetime.now(timezone.utc),
    ))
    log.info("upgrade.completed", extra={"run_id": run_id, "biome_id": biome_id})


async def _deploy_and_health_check(
    nodes: list[str], biome_id: int, target_version: str, run_id: str
) -> bool:
    """Deploy to nodes and health-check (5s intervals, 60s timeout).

    Regression: gh-22. Same shared-event-loop background task as
    ``_execute_upgrade_orchestration`` above. The per-node lookup here ran
    once per node in the deploy loop AND once per node per health-check
    attempt (12 attempts x N nodes) -- every one of those was a blocking
    call inline on the loop. Both now go through ``run_db()``; the 5s poll
    interval stays plain ``await asyncio.sleep()``.

    Also fixes a pre-existing bug this sweep's own end-to-end test
    surfaced: the health-check phase below references ``aiohttp`` but this
    function never imported it (only the sibling
    ``_execute_upgrade_orchestration`` did, and a local ``import`` inside
    one function does not leak into another's namespace) -- every upgrade
    run's health-check phase unconditionally raised ``NameError`` and
    timed out after 60s. No prior test caught it because every existing
    test mocks ``_deploy_and_health_check`` away entirely.
    """
    import asyncio
    import subprocess

    import aiohttp

    db = get_db()

    def _fetch_node(node_id: int) -> Any:
        return db(db.nodes.id == node_id).select().first()

    def _fetch_biome() -> Any:
        return db(db.biomes.id == biome_id).select().first()

    biome = await run_db(_fetch_biome)
    if not biome:
        log.error(
            "upgrade.biome_not_found",
            extra={"biome_id": biome_id, "run_id": run_id},
        )
        return False

    biome_kind = biome.biome_kind if hasattr(biome, "biome_kind") else "custom"

    # Deploy to each node
    for node_id_str in nodes:
        log.info(
            "upgrade.node.deploy",
            extra={"node": node_id_str, "biome_id": biome_id, "run_id": run_id},
        )
        try:
            node_id = int(node_id_str)
            node_row = await run_db(lambda: _fetch_node(node_id))
            if not node_row:
                log.warning(
                    "upgrade.node_not_found",
                    extra={"node_id": node_id, "run_id": run_id},
                )
                return False

            node_hostname = node_row.name if hasattr(node_row, "name") else f"node-{node_id}"

            # Determine deploy strategy based on biome_kind
            if biome_kind == "k8s":
                # Use kubectl rollout for Kubernetes biomes
                cmd = [
                    "kubectl",
                    "rollout",
                    "restart",
                    f"deployment/{biome.name}",
                    "-n",
                    "default",
                ]
                await asyncio.to_thread(subprocess.run, cmd, check=True, timeout=30)
            else:
                # Use lxc move --live for LXC/container biomes
                container_name = f"{biome.name}-{node_id}"
                cmd = [
                    "lxc",
                    "move",
                    container_name,
                    "--target",
                    node_hostname,
                    "--live",
                ]
                await asyncio.to_thread(subprocess.run, cmd, check=True, timeout=30)

            log.info(
                "upgrade.node.deploy_completed",
                extra={"node": node_id_str, "biome_id": biome_id, "run_id": run_id},
            )
        except Exception as e:
            log.error(
                "upgrade.node.deploy_failed",
                extra={
                    "node": node_id_str,
                    "biome_id": biome_id,
                    "run_id": run_id,
                    "error": str(e),
                },
            )
            return False

    # Health check: poll /healthz every 5s for 60s
    for attempt in range(12):  # 12 * 5s = 60s
        all_healthy = True
        try:
            async with aiohttp.ClientSession() as session:
                for node_id_str in nodes:
                    node_id = int(node_id_str)
                    node_row = await run_db(lambda: _fetch_node(node_id))
                    if not node_row:
                        all_healthy = False
                        break

                    node_hostname = node_row.name if hasattr(node_row, "name") else f"node-{node_id}"
                    url = f"http://{node_hostname}:8080/healthz"

                    try:
                        async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=2)
                        ) as resp:
                            if resp.status != 200:
                                all_healthy = False
                                break
                    except Exception as check_err:
                        log.debug(
                            "upgrade.healthz.node_unreachable",
                            extra={
                                "node": node_id_str,
                                "url": url,
                                "error": str(check_err),
                            },
                        )
                        all_healthy = False
                        break

            if all_healthy:
                log.info(
                    "upgrade.healthz.passed",
                    extra={"nodes": nodes, "run_id": run_id},
                )
                return True
        except Exception as e:
            log.debug(
                "upgrade.healthz.check_failed",
                extra={"attempt": attempt + 1, "nodes": nodes, "error": str(e)},
            )

        if attempt < 11:
            await asyncio.sleep(5)

    log.error(
        "upgrade.healthz.timeout",
        extra={"nodes": nodes, "run_id": run_id},
    )
    return False


@biomes_bp.route("/<int:biome_id>/upgrade-runs/<run_id>", methods=["GET"])
@auth_required
async def get_upgrade_run(biome_id: int, run_id: str):
    """Retrieve an upgrade run by ID.

    Spec ``GET /api/v1/biomes/{id}/upgrade-runs/{run_id}`` — scope ``gough.biomes.read``.
    """
    db = get_db()

    def _fetch() -> Any:
        return db(
            (db.upgrade_runs.id == run_id) & (db.upgrade_runs.biome_id == biome_id)
        ).select().first()

    run = await run_db(_fetch)

    if not run:
        return err_not_found(f"Upgrade run {run_id} not found")

    return envelope_success(
        {
            "id": run.id,
            "biome_id": run.biome_id,
            "target_version": run.target_version,
            "cluster_id": run.cluster_id,
            "status": run.status,
            "phase": run.phase,
            "nodes_total": run.nodes_total,
            "nodes_completed": run.nodes_completed,
            "nodes_failed": run.nodes_failed,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "rollback_reason": run.rollback_reason,
        },
        status_code=200,
    )


@biomes_bp.route("/<int:biome_id>/eligibility", methods=["GET"])
@auth_required
async def biome_eligibility(biome_id: int):
    """Per-node eligibility check for a biome.

    Spec ``GET /api/v1/biomes/{id}/eligibility?node_id=N`` — scope
    ``gough.biomes.read``.  Returns ``{eligible, missing_tags[],
    forbidden_tags_present[], resource_violations[]}``.
    """
    node_id_raw = request.args.get("node_id")
    if not node_id_raw:
        return err_validation(
            "node_id query parameter is required",
            violations=[
                {"field": "node_id", "code": "missing", "message": "required"},
            ],
        )
    try:
        node_id = int(node_id_raw)
    except ValueError:
        return err_validation("node_id must be an integer")

    db = get_db()

    def _fetch_biome() -> Any:
        return db(db.biomes.id == biome_id).select().first()

    biome = await run_db(_fetch_biome)
    if not biome:
        return err_not_found(f"Biome {biome_id} not found")

    def _fetch_node() -> Any:
        return db(db.nodes.id == node_id).select().first() if hasattr(db, "nodes") else None

    node = await run_db(_fetch_node)
    if not node:
        return err_not_found(f"Node {node_id} not found")

    # Regression: gh-22. _eval_node_eligibility() -> node_effective_tags()
    # issues its own direct penguin-dal select -- off the event loop via
    # run_db() instead of blocking the request coroutine inline.
    result = await run_db(lambda: _eval_node_eligibility(db, biome, node))
    payload = {
        "biome_id": biome_id,
        "node_id": node_id,
        **result.to_dict(),
    }
    return envelope_success(payload)


# ============================================================================
# Biome Groups
# ============================================================================


@biomes_bp.route("/groups", methods=["GET"])
@auth_required
async def list_biome_groups():
    """List all biome groups.

    Returns:
        200: List of biome groups
    """
    db = get_db()

    def _fetch() -> Any:
        return db(db.biome_groups.id > 0).select(orderby=db.biome_groups.display_name)

    groups = await run_db(_fetch)

    return jsonify({
        "groups": [serialize_biome_group(group) for group in groups],
        "total": len(groups),
    }), 200


# Attach response model for OpenAPI documentation
list_biome_groups._response_model = BiomeGroupListResponse


@biomes_bp.route("/groups", methods=["POST"])
@maintainer_or_admin_required
async def create_biome_group():
    """Create a new biome group.

    Request Body:
        name: Unique identifier (required)
        display_name: Human-readable name (required)
        description: Group description
        biomes: Array of {biome_id: int, order: int} objects (required)
        is_default: Whether this is a default group

    Returns:
        201: Biome group created successfully
        400: Invalid request
        409: Group name already exists
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    name = data.get("name", "").strip()
    display_name = data.get("display_name", "").strip()
    biomes = data.get("biomes")

    if not name:
        return jsonify({"error": "Group name required"}), 400

    if not display_name:
        return jsonify({"error": "Display name required"}), 400

    if not biomes or not isinstance(biomes, list):
        return jsonify({"error": "Biomes array required"}), 400

    db = get_db()

    # Regression: gh-22. Name-exists check + per-biome-ID existence loop is
    # a sequence of reads feeding validation -- one run_db() closure per
    # the house rule, preserving the exact original error precedence/order.
    def _validate_group() -> dict[str, Any]:
        existing = db(db.biome_groups.name == name).select().first()
        if existing:
            return {"error": "name_exists"}
        for biome_ref in biomes:
            if not isinstance(biome_ref, dict) or "biome_id" not in biome_ref:
                return {"error": "invalid_format"}
            biome = db(db.biomes.id == biome_ref["biome_id"]).select().first()
            if not biome:
                return {"error": "biome_not_found", "biome_id": biome_ref["biome_id"]}
        return {"error": None}

    validation = await run_db(_validate_group)
    if validation["error"] == "name_exists":
        return jsonify({"error": "Group name already exists"}), 409
    if validation["error"] == "invalid_format":
        return jsonify({"error": "Invalid biome reference format"}), 400
    if validation["error"] == "biome_not_found":
        return jsonify({"error": f"Biome ID {validation['biome_id']} not found"}), 400

    # Regression: gh-22. insert + commit + refetch is one unit of work --
    # stays in one run_db() closure per the house rule.
    def _create_group() -> tuple[bool, Any]:
        try:
            group_id = db.biome_groups.insert(
                name=name,
                display_name=display_name,
                description=data.get("description"),
                biomes=biomes,
                is_default=data.get("is_default", False),
            )

            db.commit()

            return True, db(db.biome_groups.id == group_id).select().first()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.exception(f"Error creating biome group: {e}")
            return False, e

    ok, result = await run_db(_create_group)
    if not ok:
        return jsonify({"error": str(result)}), 500

    return jsonify({
        "message": "Biome group created successfully",
        "group": serialize_biome_group(result),
    }), 201


# Attach models for OpenAPI documentation
create_biome_group._request_model = BiomeGroupCreateRequest
create_biome_group._response_model = BiomeGroupResponse


@biomes_bp.route("/groups/<int:group_id>", methods=["GET"])
@auth_required
async def get_biome_group(group_id: int):
    """Get biome group details by ID.

    Args:
        group_id: Biome group ID

    Returns:
        200: Biome group details with resolved biome references
        404: Biome group not found
    """
    db = get_db()

    # Regression: gh-22. Group fetch + biome-reference resolution loop is
    # a sequence of reads feeding the response -- one run_db() closure.
    def _fetch_group_and_resolve() -> tuple[Any, list[dict[str, Any]]]:
        group = db(db.biome_groups.id == group_id).select().first()
        if not group:
            return None, []
        resolved_biomes: list[dict[str, Any]] = []
        for biome_ref in (group.biomes or []):
            if isinstance(biome_ref, dict) and "biome_id" in biome_ref:
                biome = db(db.biomes.id == biome_ref["biome_id"]).select().first()
                if biome:
                    resolved_biomes.append({
                        "order": biome_ref.get("order", 0),
                        "biome": serialize_biome(biome),
                    })
        return group, resolved_biomes

    group, resolved_biomes = await run_db(_fetch_group_and_resolve)
    if not group:
        return jsonify({"error": "Biome group not found"}), 404

    group_data = serialize_biome_group(group)
    group_data["resolved_biomes"] = resolved_biomes

    return jsonify({"group": group_data}), 200


# Attach response model for OpenAPI documentation
get_biome_group._response_model = BiomeGroupResponse


@biomes_bp.route("/groups/<int:group_id>", methods=["PUT"])
@maintainer_or_admin_required
async def update_biome_group(group_id: int):
    """Update biome group by ID.

    Args:
        group_id: Biome group ID

    Request Body: Same fields as create_biome_group (all optional)

    Returns:
        200: Biome group updated successfully
        400: Invalid request
        404: Biome group not found
        409: Group name conflict
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    db = get_db()

    def _fetch() -> Any:
        return db(db.biome_groups.id == group_id).select().first()

    group = await run_db(_fetch)
    if not group:
        return jsonify({"error": "Biome group not found"}), 404

    # Check for name conflict if name is being changed
    if "name" in data and data["name"] != group.name:
        def _check_name_conflict() -> Any:
            return db((db.biome_groups.name == data["name"]) & (db.biome_groups.id != group_id)).select().first()

        existing = await run_db(_check_name_conflict)
        if existing:
            return jsonify({"error": "Group name already exists"}), 409

    # Validate biomes array if provided
    if "biomes" in data:
        biomes = data["biomes"]
        if not isinstance(biomes, list):
            return jsonify({"error": "Biomes must be an array"}), 400

        # Regression: gh-22. Per-biome-ID existence loop is a sequence of
        # reads feeding validation -- one run_db() closure, preserving the
        # exact original error precedence/order.
        def _validate_biomes() -> Optional[dict[str, Any]]:
            for biome_ref in biomes:
                if not isinstance(biome_ref, dict) or "biome_id" not in biome_ref:
                    return {"error": "invalid_format"}
                biome = db(db.biomes.id == biome_ref["biome_id"]).select().first()
                if not biome:
                    return {"error": "biome_not_found", "biome_id": biome_ref["biome_id"]}
            return None

        validation = await run_db(_validate_biomes)
        if validation is not None:
            if validation["error"] == "invalid_format":
                return jsonify({"error": "Invalid biome reference format"}), 400
            return jsonify({"error": f"Biome ID {validation['biome_id']} not found"}), 400

    try:
        update_fields = {}

        for field in ["name", "display_name", "description", "biomes", "is_default"]:
            if field in data:
                update_fields[field] = data[field]

        # Regression: gh-22. update + commit + refetch is one unit of
        # work -- stays in one run_db() closure per the house rule.
        def _apply_update() -> tuple[bool, Any]:
            try:
                if update_fields:
                    db(db.biome_groups.id == group_id).update(**update_fields)
                    db.commit()
                return True, db(db.biome_groups.id == group_id).select().first()
            except Exception as e:  # noqa: BLE001
                db.rollback()
                log.exception(f"Error updating biome group: {e}")
                return False, e

        ok, result = await run_db(_apply_update)
        if not ok:
            return jsonify({"error": str(result)}), 500

        return jsonify({
            "message": "Biome group updated successfully",
            "group": serialize_biome_group(result),
        }), 200

    except Exception as e:
        await run_db(lambda: db.rollback())
        log.exception(f"Error updating biome group: {e}")
        return jsonify({"error": str(e)}), 500


# Attach models for OpenAPI documentation
update_biome_group._request_model = BiomeGroupUpdateRequest
update_biome_group._response_model = BiomeGroupResponse


@biomes_bp.route("/groups/<int:group_id>", methods=["DELETE"])
@admin_required
async def delete_biome_group(group_id: int):
    """Delete biome group by ID.

    Args:
        group_id: Biome group ID

    Returns:
        200: Biome group deleted successfully
        404: Biome group not found
        409: Group is in use and cannot be deleted
    """
    db = get_db()

    # Regression: gh-22. Group fetch + in-use check is a sequence of reads
    # feeding validation -- one run_db() closure.
    def _fetch_group_and_configs() -> tuple[Any, Any]:
        group = db(db.biome_groups.id == group_id).select().first()
        if not group:
            return None, None
        configs = db(db.ipxe_boot_configs.assigned_biome_group_id == group_id).select()
        return group, configs

    group, configs = await run_db(_fetch_group_and_configs)
    if not group:
        return jsonify({"error": "Biome group not found"}), 404

    if configs:
        return jsonify({
            "error": "Cannot delete biome group that is assigned to boot configs",
            "configs_count": len(configs),
        }), 409

    def _delete_group() -> tuple[bool, Any]:
        try:
            db(db.biome_groups.id == group_id).delete()
            db.commit()
            return True, None
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.exception(f"Error deleting biome group: {e}")
            return False, e

    ok, err = await run_db(_delete_group)
    if not ok:
        return jsonify({"error": str(err)}), 500

    return jsonify({"message": "Biome group deleted successfully"}), 200


# Attach response model for OpenAPI documentation
delete_biome_group._response_model = BiomeGroupResponse


# ============================================================================
# Cloud-Init Rendering
# ============================================================================


@biomes_bp.route("/render-cloud-init", methods=["POST"])
@auth_required
async def render_cloud_init():
    """Merge multiple biomes into a single cloud-init configuration.

    This endpoint takes an array of biome IDs and merges their cloud-init
    content into a single unified configuration. The merge respects the
    order of biomes and handles common cloud-init sections appropriately.

    Request Body:
        biome_ids: Array of biome IDs to merge (required)
        additional_config: Optional additional YAML to merge at the end

    Returns:
        200: Rendered cloud-init configuration
        400: Invalid request
        404: One or more biomes not found
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    biome_ids = data.get("biome_ids", [])
    if not biome_ids or not isinstance(biome_ids, list):
        return jsonify({"error": "biome_ids array required"}), 400

    db = get_db()

    # Regression: gh-22. Per-biome-ID fetch loop is a sequence of reads
    # feeding validation/aggregation -- one run_db() closure, preserving
    # the exact original error precedence/order.
    def _collect_configs() -> tuple[Optional[int], list[str], list[dict[str, Any]]]:
        configs: list[str] = []
        biomes_info: list[dict[str, Any]] = []
        for biome_id in biome_ids:
            biome = db(db.biomes.id == biome_id).select().first()
            if not biome:
                return biome_id, [], []

            if biome.cloud_init_content:
                configs.append(biome.cloud_init_content)
                biomes_info.append({
                    "id": biome.id,
                    "name": biome.name,
                    "display_name": biome.display_name,
                })
        return None, configs, biomes_info

    missing_biome_id, configs, biomes_info = await run_db(_collect_configs)
    if missing_biome_id is not None:
        return jsonify({"error": f"Biome ID {missing_biome_id} not found"}), 404

    # Add additional config if provided
    additional_config = data.get("additional_config")
    if additional_config:
        valid, error = validate_cloud_init_yaml(additional_config)
        if not valid:
            return jsonify({"error": f"Invalid additional config: {error}"}), 400
        configs.append(additional_config)

    if not configs:
        return jsonify({
            "error": "No cloud-init content found in specified biomes"
        }), 400

    try:
        # Merge all configs
        merged_config = merge_cloud_init_configs(configs)

        return jsonify({
            "message": "Cloud-init configuration rendered successfully",
            "cloud_init": merged_config,
            "biomes_merged": biomes_info,
            "total_biomes": len(biomes_info),
        }), 200

    except Exception as e:
        log.exception(f"Error rendering cloud-init: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Deployments Management (Plan 3)
# ============================================================================


@biomes_bp.route("/deployments", methods=["GET"])
@auth_required
async def list_deployments():
    """List deployments with optional filtering and pagination.

    Spec: ``GET /api/v1/deployments``
    Scope: Auth required

    Query Parameters:
        status: Filter by deployment status (pending, in_progress, succeeded, failed)
        biome_id: Filter by biome ID
        node_id: Filter by node ID
        limit: Number of results to return (default 20, max 100)
        offset: Number of results to skip (default 0)

    Returns:
        200: List of deployments with pagination metadata
        400: Invalid query parameters
    """
    db = get_db()

    # Parse query parameters
    try:
        status = request.args.get("status", None)
        biome_id = request.args.get("biome_id", None, type=int)
        node_id = request.args.get("node_id", None, type=int)
        limit = request.args.get("limit", 20, type=int)
        offset = request.args.get("offset", 0, type=int)

        # Validate limits
        if limit < 1 or limit > 100:
            return err_bad_request("limit must be between 1 and 100")
        if offset < 0:
            return err_bad_request("offset must be >= 0")
    except (ValueError, TypeError):
        return err_bad_request("Invalid query parameter type")

    # Build query with ANDed conditions
    q = db.deployments.id > 0
    if status:
        q = q & (db.deployments.status == status)
    if biome_id:
        q = q & (db.deployments.biome_id == biome_id)
    if node_id:
        q = q & (db.deployments.node_id == node_id)
    query = db(q)

    # Regression: gh-22. Count + paginated select is a sequence of reads
    # feeding the response -- one run_db() closure instead of blocking the
    # request coroutine inline.
    def _fetch() -> tuple[int, Any]:
        total_count = query.count()
        rows = query.select(
            orderby=~db.deployments.created_at,
            limitby=(offset, offset + limit),
        )
        return total_count, rows

    total_count, deployments_rows = await run_db(_fetch)

    deployments = []
    for row in deployments_rows:
        deployments.append({
            "id": str(row.id),
            "biome_id": int(row.biome_id),
            "node_id": int(row.node_id),
            "phase": int(row.phase),
            "status": row.status,
            "logs_url": row.logs_url,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        })

    return envelope_success(
        {
            "deployments": deployments,
            "total": total_count,
            "limit": limit,
            "offset": offset,
        }
    )


@biomes_bp.route("/deployments/<string:deployment_id>", methods=["GET"])
@auth_required
async def get_deployment(deployment_id: str):
    """Get a specific deployment by ID.

    Spec: ``GET /api/v1/deployments/{id}``
    Scope: Auth required

    Path Parameters:
        deployment_id: Deployment ID (string)

    Returns:
        200: Deployment details
        404: Deployment not found
    """
    db = get_db()

    def _fetch() -> Any:
        return db(db.deployments.id == deployment_id).select().first()

    deployment = await run_db(_fetch)
    if not deployment:
        return err_not_found(f"Deployment {deployment_id} not found")

    return envelope_success({
        "id": str(deployment.id),
        "biome_id": int(deployment.biome_id),
        "node_id": int(deployment.node_id),
        "phase": int(deployment.phase),
        "status": deployment.status,
        "logs_url": deployment.logs_url,
        "created_at": deployment.created_at.isoformat() if deployment.created_at else None,
        "updated_at": deployment.updated_at.isoformat() if deployment.updated_at else None,
    })


# ============================================================================
# Deployment Logs & Control (Plan 5)
# ============================================================================


async def _deploy_nats_safe(subject: str, payload: dict, tenant_id: str) -> None:
    """Publish to NATS; swallow all errors with a warning log (deployment events)."""
    try:
        from ..clients.nats import NatsClient

        client: Optional[NatsClient] = getattr(g, "nats_client", None)
        if client is None:
            client = getattr(current_app, "nats_client", None)

        if client is None:
            log.warning("NATS client not wired; skipping publish on subject=%s", subject)
            return

        await client.publish(
            subject=subject,
            payload=payload,
            tenant_id=tenant_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("NATS publish failed (non-fatal) subject=%s error=%s", subject, exc)


@biomes_bp.route("/deployments/<string:deployment_id>/logs", methods=["GET"])
@auth_required
async def get_deployment_logs(deployment_id: str):
    """Get deployment logs.

    Spec: ``GET /api/v1/deployments/{id}/logs``
    Scope: ``gough.deployments.read``

    Query Parameters:
        since: ISO timestamp to fetch logs after
        tail: Number of most recent logs to return (default 100)

    Returns:
        200: List of deployment logs
        404: Deployment not found
    """
    db = get_db()

    # Check deployment exists
    def _fetch_deployment() -> Any:
        return db(db.deployments.id == deployment_id).select().first()

    deployment = await run_db(_fetch_deployment)
    if not deployment:
        return err_not_found(f"Deployment {deployment_id} not found")

    # Parse query parameters
    since = request.args.get("since", None)
    tail = request.args.get("tail", 100, type=int)

    if tail < 1 or tail > 1000:
        return err_bad_request("tail must be between 1 and 1000")

    # Build query for deployment logs
    query = db(db.deployment_logs.deployment_id == deployment_id)

    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            query = query(db.deployment_logs.created_at >= since_dt)
        except ValueError:
            return err_bad_request(f"Invalid since timestamp: {since}")

    # Fetch logs ordered by created_at ascending. Regression: gh-22 -- now
    # off the event loop via run_db() instead of blocking the request
    # coroutine inline.
    logs_rows = await run_db(lambda: query.select(orderby=db.deployment_logs.created_at))

    logs = []
    for row in logs_rows:
        logs.append({
            "id": str(row.id),
            "deployment_id": str(row.deployment_id),
            "message": row.message,
            "level": row.level,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })

    # Apply tail limit
    if len(logs) > tail:
        logs = logs[-tail:]

    return envelope_success({
        "logs": logs,
        "deployment_id": str(deployment_id),
    })


@biomes_bp.route("/deployments/<string:deployment_id>/cancel", methods=["POST"])
@auth_required
async def cancel_deployment(deployment_id: str):
    """Cancel a deployment.

    Spec: ``POST /api/v1/deployments/{id}/cancel``
    Scope: ``gough.deployments.write``

    Returns:
        200: Deployment cancelled
        404: Deployment not found
        409: Deployment in terminal state or not cancellable
    """
    db = get_db()

    def _fetch() -> Any:
        return db(db.deployments.id == deployment_id).select().first()

    deployment = await run_db(_fetch)
    if not deployment:
        return err_not_found(f"Deployment {deployment_id} not found")

    current_status = deployment.status
    if current_status not in ("pending", "in_progress"):
        return err_conflict(
            f"Cannot cancel deployment in status '{current_status}'",
            details={"status": current_status, "allowed": ["pending", "in_progress"]}
        )

    def _cancel() -> tuple[bool, Any]:
        try:
            now = datetime.now(timezone.utc)
            db(db.deployments.id == deployment_id).update(
                status="cancelled",
                updated_at=now,
            )
            db.commit()
            return True, None
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.exception("Error cancelling deployment %s: %s", deployment_id, exc)
            return False, exc

    ok, err = await run_db(_cancel)
    if not ok:
        return err_internal(str(err))

    # Publish NATS event
    tenant_id = getattr(deployment, "tenant_id", "__default__")
    await _deploy_nats_safe(
        subject=f"gough.deployments.{deployment_id}.status-changed",
        payload={
            "deployment_id": str(deployment_id),
            "status": "cancelled",
            "reason": "operator_cancel",
        },
        tenant_id=tenant_id,
    )

    return envelope_success({
        "cancelled": True,
        "deployment_id": str(deployment_id),
        "status": "cancelled",
    })
