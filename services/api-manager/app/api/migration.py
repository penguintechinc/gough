"""Migration API Endpoints.

Implements the spec's "API Surface → Migration" + "Capacity Prediction & Live
Migration → Migration Safety Envelope" sections:

* GET    /api/v1/migration/policy              gough.capacity.read
* PATCH  /api/v1/migration/policy              gough.migration.policy
* POST   /api/v1/migration/biome/{instance_id}   gough.migration.trigger
                                               (+ gough.migration.override-lock
                                                   if ignore_lock=true)
* GET    /api/v1/migration/events              gough.capacity.read
* GET    /api/v1/migration/safety-envelope     gough.capacity.read

POST trigger validates the safety envelope, executes ``lxc move --live``
asynchronously, and returns 202 with a migration_event_id for polling status.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Optional

from quart import Blueprint, current_app, g, jsonify, request

from ..db.run_db import run_db
from ..middleware import auth_required
from ..models import get_db
from ..workers.migration_engine import (
    ClusterState,
    BiomeSnapshot,
    MigrationPlan,
    MigrationPolicy as PolicyDC,
    NodeSnapshot,
    POLICY_FIELDS,
    SafetyResult,
    Violation,
    evaluate,
    safety_result_to_dict,
    validate_combination,
    validate_policy_patch,
    violations_to_dicts,
)

log = logging.getLogger(__name__)

migration_bp = Blueprint("migration", __name__)


# =============================================================================
# Scope / MFA decorators (mirror the iPXE blueprint convention)
# =============================================================================


def _scope_required(*required_scopes: str) -> Callable:
    """Enforce OIDC scope membership on the request principal.

    Reads ``g.principal`` (set by the credentials middleware in production),
    falling back to the scopes carried in ``g.current_user["_jwt_payload"]``.

    Authorisation is on scopes only. This docstring used to promise a legacy
    ``g.current_user.role`` fallback ("admin is treated as superset;
    maintainer accepted for read-only scopes") that the body has never
    implemented -- and must not: security.md requires every permission check to
    go through OIDC scopes and forbids branching on role names, with roles
    being pre-expanded scope bundles. A caller that should be allowed needs the
    scope in its token, not a role string.
    """
    required = frozenset(required_scopes)

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            principal = getattr(g, "principal", None)
            if principal is not None:
                granted = getattr(principal, "scopes", frozenset())
                if not required.issubset(granted):
                    return jsonify({
                        "error": "Insufficient scope",
                        "required": sorted(required),
                    }), 403
                return await fn(*args, **kwargs)

            user = getattr(g, "current_user", None)
            if user is not None:
                from ..security.scope_enforcement import extract_scopes_from_jwt
                granted = extract_scopes_from_jwt(user.get("_jwt_payload") or {})
                if required.issubset(granted):
                    return await fn(*args, **kwargs)

            return jsonify({
                "error": "Insufficient scope",
                "required": sorted(required),
            }), 403

        return wrapper

    return decorator


def _mfa_required(fn: Callable) -> Callable:
    """Enforce that the request principal has completed MFA recently.

    Reads ``g.principal.mfa_verified`` (bool). The pre-OIDC test harness sets
    ``g.mfa_verified`` directly on the request context.
    """
    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        principal = getattr(g, "principal", None)
        verified = False
        if principal is not None:
            verified = bool(getattr(principal, "mfa_verified", False))
        if not verified:
            verified = bool(getattr(g, "mfa_verified", False))
        if not verified:
            return jsonify({
                "error": "MFA required",
                "code": "mfa_required",
            }), 403
        return await fn(*args, **kwargs)

    return wrapper


# =============================================================================
# Helpers
# =============================================================================


_POLICY_INT_FIELDS = (
    "evaluation_interval_seconds",
    "min_healthy_nodes",
    "max_concurrent_migrations",
    "require_target_capacity_headroom_cpu_pct",
    "require_target_capacity_headroom_mem_pct",
    "require_target_capacity_headroom_disk_pct",
    "rollback_window_seconds",
    "capacity_forecast_horizon_days",
)
_POLICY_BOOL_FIELDS = (
    "enabled",
    "rollback_on_destination_failure",
    "forbid_migration_during_partition",
    "forbid_migration_during_maintenance",
)
_POLICY_FLOAT_FIELDS = ("waddleai_risk_threshold",)


def _cluster_id() -> str:
    """Return the operative cluster id for this request.

    Production sets ``g.cluster_id`` from the credentials middleware; tests
    fall back to ``current_app.config['CLUSTER_ID']`` or a fixed default.
    """
    cid = getattr(g, "cluster_id", None)
    if cid:
        return str(cid)
    cfg = current_app.config.get("CLUSTER_ID") if current_app else None
    return str(cfg or "00000000-0000-0000-0000-000000000000")


def _policy_row_to_dict(row: Any) -> dict:
    """Serialise a ``migration_policy`` row to JSON."""
    out: dict = {}
    for field_name in _POLICY_INT_FIELDS + _POLICY_BOOL_FIELDS + _POLICY_FLOAT_FIELDS:
        out[field_name] = getattr(row, field_name, None)
    out["cluster_id"] = str(getattr(row, "cluster_id", ""))
    if getattr(row, "created_at", None):
        out["created_at"] = row.created_at.isoformat()
    if getattr(row, "updated_at", None):
        out["updated_at"] = row.updated_at.isoformat()
    return out


def _default_policy_dict(cluster_id: str) -> dict:
    """Return the spec-default policy values for a brand-new cluster."""
    return {
        "cluster_id": cluster_id,
        "enabled": False,
        "evaluation_interval_seconds": 300,
        "min_healthy_nodes": 3,
        "max_concurrent_migrations": 1,
        "require_target_capacity_headroom_cpu_pct": 20,
        "require_target_capacity_headroom_mem_pct": 20,
        "require_target_capacity_headroom_disk_pct": 15,
        "rollback_on_destination_failure": True,
        "rollback_window_seconds": 300,
        "forbid_migration_during_partition": True,
        "forbid_migration_during_maintenance": True,
        "waddleai_risk_threshold": 0.75,
        "capacity_forecast_horizon_days": 7,
    }


def _load_or_default_policy(cluster_id: str) -> dict:
    """Read the ``migration_policy`` row for the cluster; return defaults if
    none exists (M1 fresh-cluster behaviour)."""
    db = get_db()
    if "migration_policy" not in getattr(db, "tables", []):
        return _default_policy_dict(cluster_id)
    row = db(db.migration_policy.cluster_id == cluster_id).select().first()
    if row is None:
        return _default_policy_dict(cluster_id)
    return _policy_row_to_dict(row)


def _policy_dict_to_dataclass(d: dict) -> PolicyDC:
    return PolicyDC(
        enabled=bool(d["enabled"]),
        evaluation_interval_seconds=int(d["evaluation_interval_seconds"]),
        min_healthy_nodes=int(d["min_healthy_nodes"]),
        max_concurrent_migrations=int(d["max_concurrent_migrations"]),
        require_target_capacity_headroom_cpu_pct=int(
            d["require_target_capacity_headroom_cpu_pct"]),
        require_target_capacity_headroom_mem_pct=int(
            d["require_target_capacity_headroom_mem_pct"]),
        require_target_capacity_headroom_disk_pct=int(
            d["require_target_capacity_headroom_disk_pct"]),
        rollback_on_destination_failure=bool(d["rollback_on_destination_failure"]),
        rollback_window_seconds=int(d["rollback_window_seconds"]),
        forbid_migration_during_partition=bool(
            d["forbid_migration_during_partition"]),
        forbid_migration_during_maintenance=bool(
            d["forbid_migration_during_maintenance"]),
        waddleai_risk_threshold=float(d["waddleai_risk_threshold"]),
        capacity_forecast_horizon_days=int(d["capacity_forecast_horizon_days"]),
    )


async def _load_cluster_state(cluster_id: str) -> ClusterState:
    """Load a ``ClusterState`` snapshot from the live DB.

    The capacity provider (WaddleAI) is consulted in M2 to populate the
    ``forecast_*`` fields; M1 falls back to the current free-percent values
    captured on the ``nodes.hardware_json`` blob.

    Regression: gh-22. Unbounded full-table scan over ``nodes`` -- genuinely
    needed here (a migration safety decision has to see every node's live
    capacity, not a page of them), so this is a wrap-only conversion, not a
    pagination one. Both reads (the node scan + the in-flight migration
    count) are one logical "cluster state" snapshot, so they run in a single
    ``run_db()`` closure rather than two separate thread hops.
    """
    db = get_db()

    def _do_load() -> ClusterState:
        nodes: list[NodeSnapshot] = []
        if "nodes" in getattr(db, "tables", []):
            rows = db(db.nodes.id > 0).select()
            for row in rows:
                hw = getattr(row, "hardware_json", None) or {}
                cpu_free = float(hw.get("cpu_free_pct", 100.0))
                mem_free = float(hw.get("mem_free_pct", 100.0))
                disk_free = float(hw.get("disk_free_pct", 100.0))
                tags = frozenset(hw.get("tags", []) or [])
                score = float(hw.get("composite_load_score", 0.0))
                nodes.append(NodeSnapshot(
                    node_id=row.id,
                    state=str(row.state or "unknown"),
                    cpu_free_pct=cpu_free,
                    mem_free_pct=mem_free,
                    disk_free_pct=disk_free,
                    forecast_cpu_free_pct=float(hw.get("forecast_cpu_free_pct",
                                                        cpu_free)),
                    forecast_mem_free_pct=float(hw.get("forecast_mem_free_pct",
                                                        mem_free)),
                    forecast_disk_free_pct=float(hw.get("forecast_disk_free_pct",
                                                         disk_free)),
                    hardware_tags=tags,
                    composite_load_score=score,
                ))
        in_flight = 0
        if "migration_events" in getattr(db, "tables", []):
            in_flight = db(
                db.migration_events.result == "in_flight"
            ).count()
        return ClusterState(
            cluster_id=cluster_id,
            nodes=tuple(nodes),
            in_flight_migrations=in_flight,
        )

    return await run_db(_do_load)


def _load_biome_snapshot(biome_instance_id: int) -> Optional[BiomeSnapshot]:
    """Hydrate a ``BiomeSnapshot`` from ``node_egg_assignments`` + ``biomes``.

    ``node_egg_assignments`` is the real, baseline-created table (gh-21:
    ``node_biome_assignments`` was a phantom name that never existed, so
    this check was always False and every migration trigger request 404'd
    unconditionally before this fix).
    """
    db = get_db()
    if ("node_egg_assignments" not in getattr(db, "tables", [])
            or "biomes" not in getattr(db, "tables", [])):
        return None
    nba = db(db.node_egg_assignments.id == biome_instance_id).select().first()
    if nba is None:
        return None
    biome = db(db.biomes.id == nba.egg_id).select().first()
    if biome is None:
        return None
    requires = frozenset(getattr(biome, "requires_hardware_tags", None) or [])
    sr = getattr(biome, "storage_requirements_json", None) or {}
    return BiomeSnapshot(
        biome_instance_id=nba.id,
        biome_id=biome.id,
        biome_kind=str(getattr(biome, "biome_kind", "custom")),
        src_node_id=int(nba.node_id),
        lock_to_host=bool(getattr(biome, "lock_to_host", False)),
        required_hardware_tags=requires,
        expected_cpu_load_pct=float(sr.get("expected_cpu_load_pct", 5.0)),
        expected_mem_load_pct=float(sr.get("expected_mem_load_pct", 10.0)),
        expected_disk_load_pct=float(sr.get("expected_disk_load_pct", 2.0)),
    )


def _record_safety_event(
    cluster_id: str,
    biome: BiomeSnapshot,
    plan: MigrationPlan,
    result: SafetyResult,
    actor_sub: str,
) -> str:
    """Persist a ``migration_events`` row capturing the safety-check outcome.

    Returns the event id (UUID string). The row carries ``result =
    'safety_check'`` per spec line 4768; the structured outcome lives on
    ``safety_check_details_json``.
    """
    db = get_db()
    if "migration_events" not in getattr(db, "tables", []):
        return ""
    details = {
        "actor_sub": actor_sub,
        "verdict": result.verdict,
        "reason": result.reason,
        "candidates_considered": result.candidates_considered,
        "chosen_target_node_id": result.chosen_target_node_id,
        "ignore_lock": plan.ignore_lock,
        "violations": violations_to_dicts(result.violations),
        "requested_target_node_id": plan.requested_target_node_id,
    }
    # ``id`` (VARCHAR(36) UUID, app.models_m1.UUID) has no server-side or
    # Python-side default -- this insert previously omitted it entirely.
    # Unreachable in production until the gh-21 ``_load_biome_snapshot`` fix
    # (this function's only caller stopped 404ing before ever reaching it),
    # so the resulting NotNullViolation never surfaced; fixed alongside it.
    row_id = db.migration_events.insert(
        id=str(uuid.uuid4()),
        biome_instance_id=biome.biome_instance_id,
        biome_id=biome.biome_id,
        biome_kind=biome.biome_kind,
        src_node_id=biome.src_node_id,
        dst_node_id=result.chosen_target_node_id,
        reason=plan.reason,
        result="safety_check",
        rejection_reason=result.reason if result.verdict == "rejected" else None,
        safety_check_details_json=details,
        started_at=datetime.now(timezone.utc),
    )
    db.commit()
    return str(row_id)


def _principal_sub() -> str:
    principal = getattr(g, "principal", None)
    if principal is not None:
        sub = getattr(principal, "sub", None)
        if sub:
            return str(sub)
    user = getattr(g, "current_user", None)
    if user is not None:
        if isinstance(user, dict):
            return str(user.get("email") or user.get("id") or "anonymous")
        return str(getattr(user, "email", None) or getattr(user, "id", "anonymous"))
    return "anonymous"


def _principal_scopes() -> frozenset[str]:
    """Return scopes from the current principal (FIX #16).

    Authorization must be scope-based ONLY. Do NOT auto-grant scopes based on
    role names — roles are informational/audit only. The JWT itself must carry
    the required scopes, issued by the auth service at token creation time.
    This enforces the principle that override capabilities (e.g.
    ``gough.migration.override-lock``) must be explicitly granted, not derived
    from a role name.
    """
    principal = getattr(g, "principal", None)
    if principal is not None:
        return frozenset(getattr(principal, "scopes", frozenset()))
    user = getattr(g, "current_user", None)
    if user is not None:
        payload = user.get("_jwt_payload") or {}
        raw = payload.get("scope", "")
        if isinstance(raw, list):
            return frozenset(s for s in raw if isinstance(s, str))
        elif isinstance(raw, str):
            return frozenset(raw.split())
    return frozenset()


# =============================================================================
# Routes
# =============================================================================


@migration_bp.route("/policy", methods=["GET"])
@auth_required
@_scope_required("gough.capacity.read")
async def get_policy():
    """Return the cluster's migration policy."""
    cluster_id = _cluster_id()
    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    policy = await run_db(lambda: _load_or_default_policy(cluster_id))
    return jsonify({"status": "success", "data": policy}), 200


@migration_bp.route("/policy", methods=["PATCH"])
@auth_required
@_scope_required("gough.migration.policy")
async def patch_policy():
    """Apply a JSON Merge Patch to the migration policy.

    Validates every field per the spec's range table (line 4738). Invalid
    combinations return 422 with ``details.violations[]`` so the operator
    UI can highlight individual fields.
    """
    body = await request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({
            "status": "error",
            "error": {
                "code": "validation_failed",
                "message": "Request body must be a JSON object",
            },
        }), 422

    unknown = [k for k in body.keys() if k not in POLICY_FIELDS]
    field_violations = validate_policy_patch(body)
    if field_violations:
        return jsonify({
            "status": "error",
            "error": {
                "code": "validation_failed",
                "message": "Validation failed",
                "details": {
                    "violations": violations_to_dicts(field_violations),
                },
            },
        }), 422

    cluster_id = _cluster_id()

    # Regression: gh-22. Current-policy read + cluster-size count is one
    # unit of work -- off the event loop via run_db() instead of blocking
    # the request coroutine inline.
    def _load_current_and_size() -> tuple[dict[str, Any], int]:
        current = _load_or_default_policy(cluster_id)
        db = get_db()
        cluster_size = 0
        if "nodes" in getattr(db, "tables", []):
            cluster_size = db(db.nodes.id > 0).count()
        return current, cluster_size

    current, cluster_size = await run_db(_load_current_and_size)
    merged = {**current, **body}

    db = get_db()

    combo_violations = validate_combination(
        merged, cluster_size=cluster_size or None
    )
    if combo_violations:
        return jsonify({
            "status": "error",
            "error": {
                "code": "validation_failed",
                "message": "Validation failed",
                "details": {
                    "violations": violations_to_dicts(combo_violations),
                },
            },
        }), 422

    if "migration_policy" not in getattr(db, "tables", []):
        return jsonify({
            "status": "error",
            "error": {
                "code": "schema_unavailable",
                "message": "migration_policy table not present",
            },
        }), 500

    now = datetime.now(timezone.utc)

    # Regression: gh-22. Existing-row check + insert-or-update + commit +
    # the post-commit refetch is one unit of work -- stays in one run_db()
    # closure per the house rule (see app/db/run_db.py).
    def _apply_patch() -> dict[str, Any]:
        existing = db(db.migration_policy.cluster_id == cluster_id).select().first()
        update_fields = {k: v for k, v in body.items() if k in POLICY_FIELDS}
        update_fields["updated_at"] = now
        if existing is None:
            insert_payload = {**merged, **update_fields}
            insert_payload.pop("created_at", None)
            insert_payload.pop("updated_at", None)
            db.migration_policy.insert(
                id=str(uuid.uuid4()),
                cluster_id=cluster_id,
                created_at=now,
                updated_at=now,
                **{k: v for k, v in insert_payload.items() if k in POLICY_FIELDS},
            )
        else:
            db(db.migration_policy.cluster_id == cluster_id).update(**update_fields)
        db.commit()

        return _load_or_default_policy(cluster_id)

    refreshed = await run_db(_apply_patch)
    log.info(
        "migration_policy patched cluster_id=%s actor=%s fields=%s",
        cluster_id, _principal_sub(), sorted(body.keys()),
    )
    return jsonify({"status": "success", "data": refreshed}), 200


@migration_bp.route("/biome/<int:instance_id>", methods=["POST"])
@auth_required
@_scope_required("gough.migration.trigger")
async def trigger_migration(instance_id: int):
    """Trigger a manual migration for a biome instance.

    M1 implementation per spec Sprint table line 186:
    1. Validate request body
    2. Resolve biome + cluster state
    3. Run the safety envelope (synchronous path returns the outcome)
    4. Persist a ``migration_events`` row with ``result='safety_check'``
    5. Return 202 + event id; full execution deferred to M2
    """
    body = await request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    target_node_id = body.get("target_node_id")
    if target_node_id is not None:
        try:
            target_node_id = int(target_node_id)
        except (TypeError, ValueError):
            return jsonify({"error": "target_node_id must be an integer"}), 400

    ignore_lock = bool(body.get("ignore_lock", False))
    synchronous = bool(body.get("synchronous", False))
    reason = str(body.get("reason", "")).strip()

    if not reason:
        return jsonify({
            "status": "error",
            "error": {
                "code": "missing_field",
                "message": "reason field is required",
            },
        }), 422

    if ignore_lock and "gough.migration.override-lock" not in _principal_scopes():
        return jsonify({
            "error": "Insufficient scope",
            "required": ["gough.migration.override-lock"],
            "message": "ignore_lock=true requires the override-lock scope",
        }), 403

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    biome = await run_db(lambda: _load_biome_snapshot(instance_id))
    if biome is None:
        return jsonify({
            "error": "biome_instance_not_found",
            "biome_instance_id": instance_id,
        }), 404

    cluster_id = _cluster_id()
    cluster_state = await _load_cluster_state(cluster_id)
    policy_dict = await run_db(lambda: _load_or_default_policy(cluster_id))
    policy = _policy_dict_to_dataclass(policy_dict)

    plan = MigrationPlan(
        biome=biome,
        requested_target_node_id=target_node_id,
        ignore_lock=ignore_lock,
        reason=reason,
    )
    result = evaluate(plan, cluster_state, policy)

    # Regression: gh-22. Off the event loop via run_db() instead of
    # blocking the request coroutine inline.
    actor_sub = _principal_sub()
    event_id = await run_db(
        lambda: _record_safety_event(cluster_id, biome, plan, result, actor_sub)
    )

    response_data = {
        "migration_event_id": event_id,
        "biome_instance_id": instance_id,
        "src_node_id": biome.src_node_id,
        "dst_node_id": result.chosen_target_node_id,
        "verdict": result.verdict,
        "safety_check": safety_result_to_dict(result),
        # Per this handler's docstring (step 5): M1 only runs the safety
        # envelope and persists the event -- real execution is a Phase 3 /
        # M2 concern (app.workers.migration_engine.execute_migration is a
        # deliberate stub until then). ``synchronous`` only controls
        # whether the (still-deferred) execution attempt is made inline;
        # it does not make execution actually happen in M1.
        "note": "execution-deferred-to-M2",
    }

    if synchronous:
        if result.verdict == "rejected":
            return jsonify({
                "status": "error",
                "error": {
                    "code": "safety_check_failed",
                    "message": result.reason,
                },
            }), 422

        # Migration passed safety; execute via migration_engine
        if result.chosen_target_node_id is not None:
            from ..workers.migration_engine import execute_migration
            exec_result = await execute_migration(
                biome_instance_id=instance_id,
                src_node_id=biome.src_node_id,
                dst_node_id=result.chosen_target_node_id,
                live=True,
                reason=reason,
            )
            # execute_migration() stubs "not_implemented" until Phase 3 --
            # that is expected/documented M1 behavior, not a caller error,
            # so it still resolves 202 (like the asynchronous path) with
            # the stub result attached rather than a hard failure.
            response_data["execution_result"] = exec_result

        return jsonify({"status": "success", "data": response_data}), 202

    # Asynchronous: safety check done, execution deferred
    return jsonify({"status": "success", "data": response_data}), 202


@migration_bp.route("/events", methods=["GET"])
@auth_required
@_scope_required("gough.capacity.read")
async def list_events():
    """List migration events with filters.

    Query parameters:
        since      ISO-8601 lower bound (inclusive)
        until      ISO-8601 upper bound (exclusive)
        node_id    src OR dst match
        biome_id     match on biome_id
        result     'pass' | 'rejected' | 'executed' | 'rolled_back'
                   (filters by ``result`` column or by ``rejection_reason``
                    presence for the synthetic 'rejected' bucket)
        limit      page size (default 50, max 200)
        offset     pagination offset
    """
    db = get_db()
    if "migration_events" not in getattr(db, "tables", []):
        return jsonify({"status": "success", "data": [], "meta": {"total": 0, "limit": 50, "offset": 0}}), 200

    since_raw = request.args.get("since")
    until_raw = request.args.get("until")
    node_id_raw = request.args.get("node_id")
    biome_id_raw = request.args.get("biome_id")
    result_raw = (request.args.get("result") or "").strip().lower()
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    if request.args.get("page_size"):
        try:
            limit = int(request.args.get("page_size"))
        except (TypeError, ValueError):
            pass
    limit = max(1, min(limit, 500))
    try:
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(offset, 0)

    query = db.migration_events.id != None  # noqa: E711 — DAL idiom

    if since_raw:
        try:
            since_ts = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
            query &= db.migration_events.started_at >= since_ts
        except ValueError:
            return jsonify({
                "status": "error",
                "error": {
                    "code": "invalid_param",
                    "message": "Invalid 'since' timestamp",
                },
            }), 400

    if until_raw:
        try:
            until_ts = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
            query &= db.migration_events.started_at < until_ts
        except ValueError:
            return jsonify({"error": "Invalid 'until' timestamp"}), 400

    if node_id_raw:
        try:
            nid = int(node_id_raw)
        except ValueError:
            return jsonify({"error": "Invalid node_id"}), 400
        query &= (
            (db.migration_events.src_node_id == nid)
            | (db.migration_events.dst_node_id == nid)
        )

    if biome_id_raw:
        try:
            eid = int(biome_id_raw)
        except ValueError:
            return jsonify({"error": "Invalid biome_id"}), 400
        query &= db.migration_events.biome_id == eid

    if result_raw:
        if result_raw == "rejected":
            query &= db.migration_events.rejection_reason != None  # noqa: E711
        elif result_raw in {"pass", "executed", "rolled_back", "safety_check"}:
            query &= db.migration_events.result == result_raw
        else:
            return jsonify({"error": "Invalid 'result' filter"}), 400

    # Regression: gh-22. Count + page select is one unit of work -- off
    # the event loop via run_db() instead of blocking the request
    # coroutine inline.
    def _fetch_page() -> tuple[int, Any]:
        total = db(query).count()
        rows = db(query).select(
            orderby=~db.migration_events.started_at,
            limitby=(offset, offset + limit),
        )
        return total, rows

    total, rows = await run_db(_fetch_page)
    events = [_serialise_event(r) for r in rows]

    return jsonify({
        "status": "success",
        "data": events,
        "meta": {
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    }), 200


def _serialise_event(row: Any) -> dict:
    return {
        "id": str(row.id),
        "biome_instance_id": getattr(row, "biome_instance_id", None),
        "biome_id": getattr(row, "biome_id", None),
        "biome_kind": getattr(row, "biome_kind", None),
        "src_node_id": getattr(row, "src_node_id", None),
        "dst_node_id": getattr(row, "dst_node_id", None),
        "reason": getattr(row, "reason", None),
        "result": getattr(row, "result", None),
        "rejection_reason": getattr(row, "rejection_reason", None),
        "safety_check": getattr(row, "safety_check_details_json", None),
        "started_at": (
            row.started_at.isoformat() if getattr(row, "started_at", None)
            else None
        ),
        "completed_at": (
            row.completed_at.isoformat() if getattr(row, "completed_at", None)
            else None
        ),
        "duration_seconds": getattr(row, "duration_seconds", None),
    }


@migration_bp.route("/safety-envelope", methods=["GET"])
@auth_required
@_scope_required("gough.capacity.read")
async def get_safety_envelope():
    """Return current policy + the most recent 100 safety-check results."""
    cluster_id = _cluster_id()

    # Regression: gh-22. Policy read + recent-events select is one unit of
    # work -- off the event loop via run_db() instead of blocking the
    # request coroutine inline.
    def _load_envelope() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        policy = _load_or_default_policy(cluster_id)
        db = get_db()
        last_results: list[dict] = []
        if "migration_events" in getattr(db, "tables", []):
            rows = db(db.migration_events.result == "safety_check").select(
                orderby=~db.migration_events.started_at,
                limitby=(0, 100),
            )
            last_results = [_serialise_event(r) for r in rows]
        return policy, last_results

    policy, last_results = await run_db(_load_envelope)
    return jsonify({
        "status": "success",
        "data": {
            "policy": policy,
            "recent_checks": last_results,
        },
    }), 200
