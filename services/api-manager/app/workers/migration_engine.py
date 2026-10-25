"""Migration safety-envelope evaluator (pure functions).

Per spec "Capacity Prediction & Live Migration → Migration Safety Envelope":
every candidate migration goes through the same envelope check before any
``lxc move --live`` is issued. This module hosts the pure-function evaluator
so it can be unit-tested in isolation and reused by the API blueprint, the
auto-migration worker (M2), and the evacuate-node endpoint.

The evaluator never silently skips a rejection; every rejection is reported
in ``SafetyResult.violations`` with a structured reason and offending node id.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Self

log = logging.getLogger(__name__)

#: PostHog flag gating the destructive cross-node execution path
#: (``execute_migration``) -- defaulted OFF per critical-rules.md Feature
#: Flags & License Tiers. Defined here (not app/licensing.py) because the
#: flag is specific to this module's feature surface; app.licensing only
#: hosts the generic ``feature_enabled()`` evaluator + flags that gate its
#: own concerns (e.g. FLAG_MULTI_CLOUD).
#:
#: Deliberately NOT applied to ``evaluate_safety``: that function has no
#: side effects (it is the same read-only envelope check ``evaluate()``
#: already performs, just loaded from live DB state), so gating it would
#: reintroduce the fail-closed-forever stub this module replaces. Only the
#: actual cross-node move is flag-gated.
FLAG_LIVE_MIGRATION: str = "gough.live-migration"


# =============================================================================
# Data structures
# =============================================================================


@dataclass(slots=True, frozen=True)
class NodeSnapshot:
    """Snapshot of a node's current capacity & forecast at safety-check time."""

    node_id: int
    state: str  # "ready" | "draining" | "failed" | "maintenance" | ...
    cpu_free_pct: float
    mem_free_pct: float
    disk_free_pct: float
    forecast_cpu_free_pct: float
    forecast_mem_free_pct: float
    forecast_disk_free_pct: float
    hardware_tags: frozenset[str] = field(default_factory=frozenset)
    composite_load_score: float = 0.0
    # False when no fresh host snapshot backs the free-percentages. Headroom
    # checks fail closed on False; they never assume free capacity.
    telemetry_fresh: bool = False


@dataclass(slots=True, frozen=True)
class BiomeSnapshot:
    """Snapshot of the biome being migrated."""

    biome_instance_id: int
    biome_id: int
    biome_kind: str
    src_node_id: int
    lock_to_host: bool
    required_hardware_tags: frozenset[str] = field(default_factory=frozenset)
    expected_cpu_load_pct: float = 0.0
    expected_mem_load_pct: float = 0.0
    expected_disk_load_pct: float = 0.0


@dataclass(slots=True, frozen=True)
class ClusterState:
    """Snapshot of the cluster at safety-check time."""

    cluster_id: str
    nodes: tuple[NodeSnapshot, ...]
    in_flight_migrations: int
    network_partition_active: bool = False
    maintenance_window_active: bool = False


@dataclass(slots=True, frozen=True)
class MigrationPolicy:
    """In-memory image of the ``migration_policy`` row."""

    enabled: bool
    evaluation_interval_seconds: int
    min_healthy_nodes: int
    max_concurrent_migrations: int
    require_target_capacity_headroom_cpu_pct: int
    require_target_capacity_headroom_mem_pct: int
    require_target_capacity_headroom_disk_pct: int
    rollback_on_destination_failure: bool
    rollback_window_seconds: int
    forbid_migration_during_partition: bool
    forbid_migration_during_maintenance: bool
    waddleai_risk_threshold: float
    capacity_forecast_horizon_days: int


@dataclass(slots=True, frozen=True)
class MigrationPlan:
    """The candidate migration we are evaluating."""

    biome: BiomeSnapshot
    requested_target_node_id: Optional[int]  # None → engine picks
    ignore_lock: bool
    reason: str


@dataclass(slots=True, frozen=True)
class Violation:
    """A single rejection reason. Every rejection produces ≥ 1 violation."""

    code: str
    message: str
    node_id: Optional[int] = None
    field: Optional[str] = None
    actual: Optional[float] = None
    required: Optional[float] = None


@dataclass(slots=True, frozen=True)
class SafetyResult:
    """Result of evaluating a migration plan against the safety envelope."""

    verdict: str  # "pass" | "rejected"
    reason: Optional[str]  # short rejection reason code; None on pass
    candidates_considered: int
    chosen_target_node_id: Optional[int]
    violations: tuple[Violation, ...]


# =============================================================================
# Validation of policy field values (used by PATCH /migration/policy)
# =============================================================================


# (field_name, low_inclusive, high_inclusive) — matches spec table at L4738.
_INT_RANGES: dict[str, tuple[int, int]] = {
    "evaluation_interval_seconds": (60, 3600),
    "min_healthy_nodes": (2, 10_000),
    "max_concurrent_migrations": (1, 10),
    "require_target_capacity_headroom_cpu_pct": (0, 50),
    "require_target_capacity_headroom_mem_pct": (0, 50),
    "require_target_capacity_headroom_disk_pct": (0, 50),
    "rollback_window_seconds": (60, 1800),
}

_BOOL_FIELDS: frozenset[str] = frozenset(
    {
        "enabled",
        "rollback_on_destination_failure",
        "forbid_migration_during_partition",
        "forbid_migration_during_maintenance",
    }
)

_ALLOWED_HORIZON_DAYS: frozenset[int] = frozenset({1, 7, 30})

POLICY_FIELDS: frozenset[str] = (
    frozenset(_INT_RANGES.keys())
    | _BOOL_FIELDS
    | frozenset({"waddleai_risk_threshold", "capacity_forecast_horizon_days"})
)


def validate_policy_patch(patch: dict) -> list[Violation]:
    """Validate a JSON Merge Patch body against the policy field constraints.

    Returns a list of violations. Empty list → patch is valid.
    """
    violations: list[Violation] = []

    for key, value in patch.items():
        if key not in POLICY_FIELDS:
            violations.append(
                Violation(
                    code="unknown_field",
                    message=f"Unknown policy field: {key}",
                    field=key,
                )
            )
            continue

        if key in _BOOL_FIELDS:
            if not isinstance(value, bool):
                violations.append(
                    Violation(
                        code="type_error",
                        message=f"Field {key} must be boolean",
                        field=key,
                    )
                )
            continue

        if key in _INT_RANGES:
            low, high = _INT_RANGES[key]
            if not isinstance(value, int) or isinstance(value, bool):
                violations.append(
                    Violation(
                        code="type_error",
                        message=f"Field {key} must be integer",
                        field=key,
                    )
                )
                continue
            if not (low <= value <= high):
                violations.append(
                    Violation(
                        code="out_of_range",
                        message=f"Field {key} must be in [{low}, {high}]",
                        field=key,
                        actual=float(value),
                        required=float(low),
                    )
                )
            continue

        if key == "waddleai_risk_threshold":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                violations.append(
                    Violation(
                        code="type_error",
                        message="waddleai_risk_threshold must be a number",
                        field=key,
                    )
                )
                continue
            if not (0.0 <= float(value) <= 1.0):
                violations.append(
                    Violation(
                        code="out_of_range",
                        message="waddleai_risk_threshold must be in [0.0, 1.0]",
                        field=key,
                        actual=float(value),
                    )
                )
            continue

        if key == "capacity_forecast_horizon_days":
            # Explicitly reject bool (True == 1 in Python)
            if isinstance(value, bool) or not isinstance(value, int):
                violations.append(
                    Violation(
                        code="type_error",
                        message="capacity_forecast_horizon_days must be integer",
                        field=key,
                    )
                )
                continue
            if value not in _ALLOWED_HORIZON_DAYS:
                violations.append(
                    Violation(
                        code="out_of_range",
                        message="capacity_forecast_horizon_days must be one of {1, 7, 30}",
                        field=key,
                        actual=float(value),
                    )
                )

    return violations


def validate_combination(
    merged: dict, *, cluster_size: Optional[int] = None
) -> list[Violation]:
    """Cross-field combination checks (e.g., ``min_healthy_nodes`` vs cluster).

    The single-field range checks live in ``validate_policy_patch``. Combination
    rules live here so they can be applied to the *merged* policy (existing row
    + incoming patch), not just the patch in isolation.
    """
    violations: list[Violation] = []
    if cluster_size is not None:
        mhn = merged.get("min_healthy_nodes")
        if isinstance(mhn, int) and not isinstance(mhn, bool):
            if mhn > cluster_size:
                violations.append(
                    Violation(
                        code="impossible_floor",
                        message=(
                            f"min_healthy_nodes ({mhn}) exceeds cluster size "
                            f"({cluster_size})"
                        ),
                        field="min_healthy_nodes",
                        actual=float(mhn),
                        required=float(cluster_size),
                    )
                )
    return violations


# =============================================================================
# Safety envelope evaluation
# =============================================================================


def _ready_nodes(state: ClusterState) -> tuple[NodeSnapshot, ...]:
    return tuple(n for n in state.nodes if n.state == "ready")


def _candidate_nodes(
    state: ClusterState, plan: MigrationPlan
) -> tuple[NodeSnapshot, ...]:
    """Filter nodes that could host the biome (excludes source, non-ready, tags)."""
    return tuple(
        n
        for n in state.nodes
        if n.state == "ready"
        and n.node_id != plan.biome.src_node_id
        and plan.biome.required_hardware_tags.issubset(n.hardware_tags)
    )


def _violates_headroom(
    node: NodeSnapshot, biome: BiomeSnapshot, policy: MigrationPolicy
) -> Optional[Violation]:
    """Return a violation if simulating the biome's load on the node breaches the
    headroom requirements; else ``None``."""
    if not node.telemetry_fresh:
        return Violation(
            code="no_fresh_host_telemetry",
            message=(
                f"Target node {node.node_id} has no fresh host resource snapshot; "
                "headroom cannot be verified"
            ),
            node_id=node.node_id,
            field="host_resource_snapshot",
            actual=0.0,
            required=1.0,
        )
    cpu_after = node.forecast_cpu_free_pct - biome.expected_cpu_load_pct
    mem_after = node.forecast_mem_free_pct - biome.expected_mem_load_pct
    disk_after = node.forecast_disk_free_pct - biome.expected_disk_load_pct

    if cpu_after < policy.require_target_capacity_headroom_cpu_pct:
        return Violation(
            code="insufficient_cpu_headroom",
            message=(
                f"Target node {node.node_id} CPU free post-migration "
                f"{cpu_after:.1f}% < required "
                f"{policy.require_target_capacity_headroom_cpu_pct}%"
            ),
            node_id=node.node_id,
            field="require_target_capacity_headroom_cpu_pct",
            actual=cpu_after,
            required=float(policy.require_target_capacity_headroom_cpu_pct),
        )
    if mem_after < policy.require_target_capacity_headroom_mem_pct:
        return Violation(
            code="insufficient_mem_headroom",
            message=(
                f"Target node {node.node_id} memory free post-migration "
                f"{mem_after:.1f}% < required "
                f"{policy.require_target_capacity_headroom_mem_pct}%"
            ),
            node_id=node.node_id,
            field="require_target_capacity_headroom_mem_pct",
            actual=mem_after,
            required=float(policy.require_target_capacity_headroom_mem_pct),
        )
    if disk_after < policy.require_target_capacity_headroom_disk_pct:
        return Violation(
            code="insufficient_disk_headroom",
            message=(
                f"Target node {node.node_id} disk free post-migration "
                f"{disk_after:.1f}% < required "
                f"{policy.require_target_capacity_headroom_disk_pct}%"
            ),
            node_id=node.node_id,
            field="require_target_capacity_headroom_disk_pct",
            actual=disk_after,
            required=float(policy.require_target_capacity_headroom_disk_pct),
        )
    return None


def evaluate(
    plan: MigrationPlan,
    cluster_state: ClusterState,
    policy: MigrationPolicy,
) -> SafetyResult:
    """Evaluate a migration plan against the safety envelope.

    Steps follow spec "Pre-Migration Safety Check" (lines 4756-4770):

    1. Cluster health: ready nodes ≥ ``min_healthy_nodes``
    2. Concurrency: in-flight migrations < ``max_concurrent_migrations``
    3. Partition / maintenance gates
    4. Biome lock: ``lock_to_host`` always rejects unless ``ignore_lock``
    5. Target selection: filter by ``required_hardware_tags``
    6. Forecast headroom: simulate biome's load against each candidate
    7. Pick the lowest composite-load-score survivor

    Every rejection is recorded with a structured ``Violation`` so the
    operator dashboard can surface it (never silently skipped, per spec).
    """
    violations: list[Violation] = []

    # Step 1: cluster health ---------------------------------------------------
    ready = _ready_nodes(cluster_state)
    if len(ready) < policy.min_healthy_nodes:
        violations.append(
            Violation(
                code="cluster_unhealthy",
                message=(
                    f"Cluster has {len(ready)} ready nodes; policy requires "
                    f"≥ {policy.min_healthy_nodes}"
                ),
                field="min_healthy_nodes",
                actual=float(len(ready)),
                required=float(policy.min_healthy_nodes),
            )
        )
        return SafetyResult(
            verdict="rejected",
            reason="cluster_unhealthy",
            candidates_considered=0,
            chosen_target_node_id=None,
            violations=tuple(violations),
        )

    # Step 2: concurrency ------------------------------------------------------
    if cluster_state.in_flight_migrations >= policy.max_concurrent_migrations:
        violations.append(
            Violation(
                code="too_many_concurrent_migrations",
                message=(
                    f"{cluster_state.in_flight_migrations} migrations in flight; "
                    f"policy max {policy.max_concurrent_migrations}"
                ),
                field="max_concurrent_migrations",
                actual=float(cluster_state.in_flight_migrations),
                required=float(policy.max_concurrent_migrations),
            )
        )
        return SafetyResult(
            verdict="rejected",
            reason="too_many_concurrent_migrations",
            candidates_considered=0,
            chosen_target_node_id=None,
            violations=tuple(violations),
        )

    # Step 3: partition/maintenance gates --------------------------------------
    if (
        policy.forbid_migration_during_partition
        and cluster_state.network_partition_active
    ):
        violations.append(
            Violation(
                code="network_partition_active",
                message="Network partition active; migrations paused by policy",
                field="forbid_migration_during_partition",
            )
        )
        return SafetyResult(
            verdict="rejected",
            reason="network_partition_active",
            candidates_considered=0,
            chosen_target_node_id=None,
            violations=tuple(violations),
        )
    if (
        policy.forbid_migration_during_maintenance
        and cluster_state.maintenance_window_active
    ):
        violations.append(
            Violation(
                code="maintenance_window_active",
                message="Tenant maintenance window active; migrations paused",
                field="forbid_migration_during_maintenance",
            )
        )
        return SafetyResult(
            verdict="rejected",
            reason="maintenance_window_active",
            candidates_considered=0,
            chosen_target_node_id=None,
            violations=tuple(violations),
        )

    # Step 4: biome lock ---------------------------------------------------------
    if plan.biome.lock_to_host and not plan.ignore_lock:
        violations.append(
            Violation(
                code="biome_locked_to_host",
                message=(
                    f"Biome {plan.biome.biome_instance_id} is locked to host; "
                    f"set ignore_lock=true with override scope to migrate"
                ),
                field="lock_to_host",
            )
        )
        return SafetyResult(
            verdict="rejected",
            reason="biome_locked_to_host",
            candidates_considered=0,
            chosen_target_node_id=None,
            violations=tuple(violations),
        )

    # Step 5: candidate filter -------------------------------------------------
    candidates = _candidate_nodes(cluster_state, plan)
    if plan.requested_target_node_id is not None:
        candidates = tuple(
            n for n in candidates if n.node_id == plan.requested_target_node_id
        )
        if not candidates:
            violations.append(
                Violation(
                    code="target_unavailable",
                    message=(
                        f"Requested target node {plan.requested_target_node_id} is "
                        f"not a viable candidate (state, source-equality, or "
                        f"hardware-tag constraints)"
                    ),
                    node_id=plan.requested_target_node_id,
                    field="target_node_id",
                )
            )
            return SafetyResult(
                verdict="rejected",
                reason="target_unavailable",
                candidates_considered=0,
                chosen_target_node_id=None,
                violations=tuple(violations),
            )

    if not candidates:
        violations.append(
            Violation(
                code="no_viable_candidates",
                message=(
                    "No nodes satisfy hardware-tag, source-equality, and ready-state "
                    "constraints"
                ),
            )
        )
        return SafetyResult(
            verdict="rejected",
            reason="no_viable_candidates",
            candidates_considered=0,
            chosen_target_node_id=None,
            violations=tuple(violations),
        )

    # Step 6: headroom check per candidate -------------------------------------
    survivors: list[NodeSnapshot] = []
    for node in candidates:
        viol = _violates_headroom(node, plan.biome, policy)
        if viol is None:
            survivors.append(node)
        else:
            violations.append(viol)

    if not survivors:
        return SafetyResult(
            verdict="rejected",
            reason="insufficient_headroom",
            candidates_considered=len(candidates),
            chosen_target_node_id=None,
            violations=tuple(violations),
        )

    # Step 7: pick lowest composite load score ---------------------------------
    chosen = min(survivors, key=lambda n: n.composite_load_score)

    return SafetyResult(
        verdict="pass",
        reason=None,
        candidates_considered=len(candidates),
        chosen_target_node_id=chosen.node_id,
        violations=tuple(violations),  # advisory: per-candidate rejections
    )


# =============================================================================
# Helpers for serialising results to the API response
# =============================================================================


def violations_to_dicts(violations: Iterable[Violation]) -> list[dict]:
    """Serialise violations for inclusion in a JSON response body."""
    out: list[dict] = []
    for v in violations:
        d: dict = {"code": v.code, "message": v.message}
        if v.node_id is not None:
            d["node_id"] = v.node_id
        if v.field is not None:
            d["field"] = v.field
        if v.actual is not None:
            d["actual"] = v.actual
        if v.required is not None:
            d["required"] = v.required
        out.append(d)
    return out


def safety_result_to_dict(result: SafetyResult) -> dict:
    """Serialise a SafetyResult for inclusion in a JSON response body."""
    return {
        "verdict": result.verdict,
        "reason": result.reason,
        "candidates_considered": result.candidates_considered,
        "chosen_target_node_id": result.chosen_target_node_id,
        "violations": violations_to_dicts(result.violations),
    }


# =============================================================================
# OpenTelemetry instrumentation (migration lifecycle: traces + metrics)
# =============================================================================
#
# Scoped to this module deliberately: gough has no OTel SDK wiring anywhere
# else yet (a pre-existing, repo-wide gap flagged in this change's report,
# not silently fixed everywhere as a side effect of this feature). Reads
# only the standard OTEL_EXPORTER_OTLP_* / OTEL_SERVICE_NAME env vars
# (critical-rules.md Observability) -- never a hardcoded vendor endpoint.
# Setup and every emission call are wrapped so a dead/unreachable collector
# can never fail a migration: telemetry failure is never a request failure.

_otel_lock = threading.Lock()
_otel_configured = False
_tracer: Any = None
_migration_duration_histogram: Any = None
_migration_outcome_counter: Any = None
_migration_rollback_counter: Any = None


def _configure_otel() -> None:
    """Idempotently configure the migration-lifecycle tracer + meter.

    Falls back to the OTel API's own no-op implementations (safe to call
    unconditionally) if the SDK/exporter packages are unavailable or the
    collector can't be reached at setup time.
    """
    global _otel_configured, _tracer
    global _migration_duration_histogram, _migration_outcome_counter
    global _migration_rollback_counter

    with _otel_lock:
        if _otel_configured:
            return
        _otel_configured = True

        # No destination configured yet (dev/test default) -- skip real SDK
        # wiring entirely rather than let every span attempt (and fail) a
        # network connect to the OTel default of localhost:4317. Setting
        # OTEL_EXPORTER_OTLP_ENDPOINT turns this on; nothing here is ever
        # hardcoded to a specific backend.
        if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            log.info(
                "OTEL_EXPORTER_OTLP_ENDPOINT unset; migration lifecycle "
                "spans/metrics use the no-op tracer/meter (not exported)."
            )
            _tracer = _NoOpTracer()
            return

        try:
            from opentelemetry import metrics, trace
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            resource = Resource.create(
                {
                    "service.name": os.environ.get(
                        "OTEL_SERVICE_NAME", "gough-api-manager"
                    ),
                }
            )

            tracer_provider = TracerProvider(resource=resource)
            tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(tracer_provider)

            meter_provider = MeterProvider(
                resource=resource,
                metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
            )
            metrics.set_meter_provider(meter_provider)

            _tracer = trace.get_tracer("gough.migration")
            meter = metrics.get_meter("gough.migration")
            _migration_duration_histogram = meter.create_histogram(
                "gough_migration_duration_seconds",
                description="Duration of biome migration executions",
                unit="s",
            )
            _migration_outcome_counter = meter.create_counter(
                "gough_migration_outcome_total",
                description="Migration executions by terminal outcome",
            )
            _migration_rollback_counter = meter.create_counter(
                "gough_migration_rollback_total",
                description="Migration rollbacks triggered after destination failure",
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 -- telemetry must never break migrations
            log.warning(
                "OTel setup for migration lifecycle failed; continuing "
                "without export: %s",
                exc,
            )
            try:
                from opentelemetry import trace as _trace_noop

                _tracer = _trace_noop.get_tracer("gough.migration")
            except Exception:  # noqa: BLE001
                _tracer = _NoOpTracer()


class _NoOpSpan:
    def set_attribute(self, *_a: Any, **_k: Any) -> None:
        pass

    def record_exception(self, *_a: Any, **_k: Any) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_a: object) -> None:
        pass


class _NoOpTracer:
    """Last-resort fallback if even the opentelemetry API package is absent."""

    def start_as_current_span(self, *_a: Any, **_k: Any) -> _NoOpSpan:
        return _NoOpSpan()


def _record_outcome_metric(outcome: str, duration_seconds: float) -> None:
    _configure_otel()
    try:
        if _migration_duration_histogram is not None:
            _migration_duration_histogram.record(duration_seconds, {"outcome": outcome})
        if _migration_outcome_counter is not None:
            _migration_outcome_counter.add(1, {"outcome": outcome})
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to record migration metric (outcome=%s): %s", outcome, exc)


def _record_rollback_metric() -> None:
    _configure_otel()
    try:
        if _migration_rollback_counter is not None:
            _migration_rollback_counter.add(1)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to record migration rollback metric: %s", exc)


# =============================================================================
# Async execution functions (for API endpoints)
# =============================================================================


def _resolve_cluster_id() -> str:
    """Resolve the operative cluster id outside a guaranteed request context.

    Mirrors ``app.api.migration._cluster_id()`` when a Quart app/request
    context is live (true for both call sites: a request handler and a
    ``current_app.add_background_task`` background task, which Quart runs
    inside the app context); falls back to ``GOUGH_CLUSTER_ID``/"default"
    for bare unit tests with no app context at all.
    """
    try:
        from quart import current_app, g

        cid = getattr(g, "cluster_id", None)
        if cid:
            return str(cid)
        cfg = current_app.config.get("CLUSTER_ID")
        if cfg:
            return str(cfg)
    except Exception as exc:  # noqa: BLE001 -- RuntimeError outside app/request context
        log.debug("_resolve_cluster_id: no Quart context (%s); using env default", exc)
    return os.environ.get("GOUGH_CLUSTER_ID", "default")


#: Assignment states that occupy a node and must be considered when
#: evacuating it or resolving a migration target -- mirrors
#: ``app.api.biomes._deploy_and_health_check``'s target-node derivation.
_ACTIVE_ASSIGNMENT_STATUSES: tuple[str, ...] = (
    "pending",
    "deploying",
    "ready",
    "draining",
)


def _load_node_biome_snapshots(node_id: int) -> list[BiomeSnapshot]:
    """Load a ``BiomeSnapshot`` for every active biome instance on ``node_id``.

    Column mapping mirrors ``app.api.migration._load_biome_snapshot``, but
    fetches every assignment currently occupying the node rather than a
    single instance -- this is what ``evaluate_safety`` needs to decide
    whether a *node* (not just one biome instance) can be safely evacuated.
    """
    from ..models import get_db

    db = get_db()
    if "node_egg_assignments" not in getattr(
        db, "tables", []
    ) or "biomes" not in getattr(db, "tables", []):
        return []

    rows = db(
        (db.node_egg_assignments.node_id == node_id)
        & (db.node_egg_assignments.status.belongs(_ACTIVE_ASSIGNMENT_STATUSES))
    ).select()

    out: list[BiomeSnapshot] = []
    for nba in rows:
        biome = db(db.biomes.id == nba.egg_id).select().first()
        if biome is None:
            continue
        requires = frozenset(getattr(biome, "requires_hardware_tags", None) or [])
        sr = getattr(biome, "storage_requirements_json", None) or {}
        out.append(
            BiomeSnapshot(
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
        )
    return out


async def evaluate_safety(node_id: int) -> dict[str, Any]:
    """Evaluate migration safety for a node evacuation.

    Called by ``nodes.py:evacuate_node`` to assess whether the node's
    workloads can be safely migrated off. Loads the real cluster state +
    migration policy + every active biome instance on the node, and runs
    each one through the same ``evaluate()`` safety envelope used by the
    manual-trigger API path. A node with nothing active on it is trivially
    safe to evacuate; a node whose every instance clears the envelope is
    safe; otherwise unsafe, with every rejection reported in ``violations``
    (never silently skipped, per this module's own docstring contract).

    Returns:
        {"safe": bool, "note": str, "violations": list[dict]}
    """
    # Deferred import: app.api.migration imports this module at its own
    # top level (``from ..workers.migration_engine import evaluate, ...``),
    # so importing it back at THIS module's top level would be circular.
    from ..api.migration import (
        _load_cluster_state,
        _load_or_default_policy,
        _policy_dict_to_dataclass,
    )
    from ..db.run_db import run_db

    _configure_otel()
    with _tracer.start_as_current_span("gough.migration.safety_evaluate") as span:
        span.set_attribute("gough.node_id", node_id)
        cluster_id = _resolve_cluster_id()

        try:
            cluster_state = await _load_cluster_state(cluster_id)
            policy_dict = await run_db(lambda: _load_or_default_policy(cluster_id))
            policy = _policy_dict_to_dataclass(policy_dict)
            biomes = await run_db(lambda: _load_node_biome_snapshots(node_id))
        except Exception as exc:  # noqa: BLE001 -- fail closed on any load error
            log.warning(
                "evaluate_safety: failed to load state for node=%s: %s",
                node_id,
                exc,
            )
            span.record_exception(exc)
            return {
                "safe": False,
                "note": f"Failed to load cluster/node state: {exc}",
                "violations": [],
            }

        if not biomes:
            span.set_attribute("gough.biome_count", 0)
            span.set_attribute("gough.safe", True)
            return {
                "safe": True,
                "note": "No active biome instances on this node; nothing to evacuate.",
                "violations": [],
            }

        span.set_attribute("gough.biome_count", len(biomes))
        all_violations: list[dict[str, Any]] = []
        unsafe_instance_ids: list[int] = []
        for biome in biomes:
            plan = MigrationPlan(
                biome=biome,
                requested_target_node_id=None,
                ignore_lock=False,
                reason="node_evacuation",
            )
            result = evaluate(plan, cluster_state, policy)
            if result.verdict != "pass":
                unsafe_instance_ids.append(biome.biome_instance_id)
                all_violations.extend(violations_to_dicts(result.violations))
                if result.reason:
                    all_violations.append(
                        {
                            "code": result.reason,
                            "message": (
                                f"biome_instance_id={biome.biome_instance_id}: "
                                f"{result.reason}"
                            ),
                            "biome_instance_id": biome.biome_instance_id,
                        }
                    )

        if unsafe_instance_ids:
            span.set_attribute("gough.safe", False)
            note = (
                f"{len(unsafe_instance_ids)} of {len(biomes)} biome "
                f"instance(s) failed the migration safety envelope: "
                f"{unsafe_instance_ids}"
            )
            return {"safe": False, "note": note, "violations": all_violations}

        span.set_attribute("gough.safe", True)
        return {
            "safe": True,
            "note": f"Safety envelope passed for {len(biomes)} biome instance(s).",
            "violations": [],
        }


def _set_assignment_fields(biome_instance_id: int, **fields: Any) -> None:
    """Update a ``node_egg_assignments`` row's lifecycle fields.

    No-op (never raises) if the table isn't present -- the same soft-schema
    convention ``app.api.migration`` uses throughout
    (``"table" not in getattr(db, "tables", [])``), so a stripped-down test
    DAL without this table doesn't turn a telemetry/lifecycle update into a
    hard failure.
    """
    from ..models import get_db

    db = get_db()
    if "node_egg_assignments" not in getattr(db, "tables", []):
        return
    fields.setdefault("updated_at", datetime.now(timezone.utc))
    db(db.node_egg_assignments.id == biome_instance_id).update(**fields)
    db.commit()


async def _update_migration_event(
    migration_event_id: str | None, **fields: Any
) -> None:
    """Update the ``migration_events`` row tracking this migration attempt.

    No-op if there is no event id to update against (e.g. a direct
    ``execute_migration()`` call outside the API trigger path) or the table
    isn't present in a stripped-down test DAL.
    """
    if not migration_event_id:
        return
    from ..db.run_db import run_db
    from ..models import get_db

    def _do_update() -> None:
        db = get_db()
        if "migration_events" not in getattr(db, "tables", []):
            return
        db(db.migration_events.id == migration_event_id).update(**fields)
        db.commit()

    await run_db(_do_update)


def _emit_migration_nats_event(
    cluster_id: str, event_name: str, payload: dict[str, Any]
) -> None:
    """Best-effort NATS publish for migration progress events.

    Mirrors ``app.api.clusters._emit_nats``'s convention (best-effort,
    no-op when no client is wired up) rather than depending on the real
    ``app.clients.nats.NatsClient`` directly, so a dead/unreachable NATS
    server never fails a migration.
    """
    try:
        import json as _json

        from quart import current_app

        nats_client = getattr(current_app, "nats_client", None)
        if nats_client is None:
            log.info(
                "NATS not configured; would emit subject=%s payload=%s",
                f"gough.cluster.{cluster_id}.{event_name}",
                payload,
            )
            return
        publish = getattr(nats_client, "publish", None)
        if publish is None:
            return
        publish(
            f"gough.cluster.{cluster_id}.{event_name}", _json.dumps(payload).encode()
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 -- telemetry/events must never break a migration
        log.warning("NATS publish failed event=%s err=%s", event_name, exc)


async def _attempt_rollback(
    *,
    instance_name: str,
    src_name: str,
    cluster_id: str,
) -> tuple[bool, str]:
    """Best-effort move of ``instance_name`` back to ``src_name``.

    Returns (rolled_back_ok, detail).
    """
    from ..clients import lxd_extra

    try:
        with _tracer.start_as_current_span("gough.migration.rollback"):
            await asyncio.to_thread(
                lxd_extra.live_migrate,
                instance_name,
                src_name,
                cluster_id=cluster_id,
            )
        return True, f"rolled back to {src_name}"
    except lxd_extra.LXDError as exc:
        return False, f"rollback to {src_name} also failed: {exc}"


async def execute_migration(
    biome_instance_id: int,
    src_node_id: int,
    dst_node_id: int,
    live: bool,
    reason: str,
    *,
    migration_event_id: str | None = None,
) -> dict[str, Any]:
    """Execute a cross-node biome migration end to end.

    Per the ``migration_policy`` rollback fields
    (``rollback_on_destination_failure``, ``rollback_window_seconds``):
    drains the assignment, issues the LXD cluster-member move
    (``app.clients.lxd_extra.live_migrate`` -- the same Vault-pinned-TLS
    HTTPS API path already used for cluster join/bootstrap), verifies the
    destination member came up, and rolls back to ``src_node_id`` on
    destination failure when the policy says to. Every transition updates
    the ``migration_events`` row (``migration_event_id``, when the caller is
    the API trigger path) and the ``node_egg_assignments`` row, so a poller
    of ``GET /migration/events`` sees the real lifecycle.

    Gated behind :data:`FLAG_LIVE_MIGRATION` (default OFF) -- see that
    constant's docstring for why ``evaluate_safety`` above is not gated the
    same way.

    Args:
        biome_instance_id: ``node_egg_assignments.id`` of the instance to move.
        src_node_id: source node id.
        dst_node_id: destination node id.
        live: if True, live-migrate (``lxc move --live`` semantics); if
            False, a cold move (same LXD API call -- ``live_migrate`` already
            issues the request with ``live`` best describing its moving
            behaviour either way; the distinction is carried through for API
            compatibility and emitted on telemetry, since LXD cluster moves
            of running instances are inherently the "live" code path and
            gough does not separately implement a stop/move/start sequence).
        reason: human-readable reason for the migration (audit/log only).
        migration_event_id: ``migration_events.id`` to update through the
            lifecycle, when called from the API trigger path.

    Returns:
        {"status": "executed"|"failed"|"rolled_back"|"disabled",
         "duration_ms": int, "verdict": str}
    """
    from ..clients import lxd_extra
    from ..db.run_db import run_db
    from ..licensing import feature_enabled
    from ..models import get_db

    _configure_otel()
    start = time.monotonic()
    cluster_id = _resolve_cluster_id()

    if not await feature_enabled(FLAG_LIVE_MIGRATION, cluster_id):
        log.info(
            "execute_migration: %s disabled for cluster=%s; not executing "
            "biome_instance_id=%s",
            FLAG_LIVE_MIGRATION,
            cluster_id,
            biome_instance_id,
        )
        return {
            "status": "disabled",
            "duration_ms": 0,
            "verdict": "feature_flag_disabled",
        }

    with _tracer.start_as_current_span("gough.migration.execute") as span:
        span.set_attribute("gough.biome_instance_id", biome_instance_id)
        span.set_attribute("gough.src_node_id", src_node_id)
        span.set_attribute("gough.dst_node_id", dst_node_id)
        span.set_attribute("gough.live", live)

        def _load_context() -> tuple[str | None, str | None, str | None]:
            db = get_db()
            if "node_egg_assignments" not in getattr(db, "tables", []):
                return None, None, None
            nba = db(db.node_egg_assignments.id == biome_instance_id).select().first()
            biome_name: str | None = None
            if nba is not None and "biomes" in getattr(db, "tables", []):
                biome = db(db.biomes.id == nba.egg_id).select().first()
                if biome is not None:
                    biome_name = getattr(biome, "name", None)
            dst_name: str | None = None
            src_name: str | None = None
            if "nodes" in getattr(db, "tables", []):
                dst_row = db(db.nodes.id == dst_node_id).select().first()
                src_row = db(db.nodes.id == src_node_id).select().first()
                dst_name = (
                    getattr(dst_row, "name", None) if dst_row is not None else None
                )
                src_name = (
                    getattr(src_row, "name", None) if src_row is not None else None
                )
            return biome_name, dst_name, src_name

        biome_name, dst_name, src_name = await run_db(_load_context)

        if not biome_name or not dst_name or not src_name:
            note = (
                "Could not resolve biome/node names for the LXD move "
                f"(biome_name={biome_name!r}, dst_name={dst_name!r}, "
                f"src_name={src_name!r})"
            )
            log.error(
                "execute_migration: %s (biome_instance_id=%s)",
                note,
                biome_instance_id,
            )
            await _update_migration_event(
                migration_event_id,
                result="failed",
                rejection_reason=note,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=time.monotonic() - start,
            )
            _record_outcome_metric("failed", time.monotonic() - start)
            span.set_attribute("gough.outcome", "failed")
            return {
                "status": "failed",
                "duration_ms": int((time.monotonic() - start) * 1000),
                "verdict": note,
            }

        instance_name = f"{biome_name}-{src_node_id}"

        _emit_migration_nats_event(
            cluster_id,
            "migration.started",
            {
                "migration_event_id": migration_event_id,
                "biome_instance_id": biome_instance_id,
                "src_node_id": src_node_id,
                "dst_node_id": dst_node_id,
                "reason": reason,
            },
        )

        # Drain: mark the assignment draining before the move so a
        # concurrent evacuate_node/safety check sees it mid-flight.
        await run_db(
            lambda: _set_assignment_fields(
                biome_instance_id,
                status="draining",
            )
        )
        await _update_migration_event(
            migration_event_id,
            result="executing",
        )

        try:
            with _tracer.start_as_current_span("gough.migration.lxd_move"):
                await asyncio.to_thread(
                    lxd_extra.live_migrate,
                    instance_name,
                    dst_name,
                    cluster_id=cluster_id,
                )
        except lxd_extra.LXDError as exc:
            # The move itself never completed -- LXD's operation model keeps
            # the instance on src on a failed/cancelled operation, so there
            # is nothing to roll back here (unlike the post-move verify
            # failure below).
            note = f"LXD move failed: {exc}"
            log.error(
                "execute_migration: %s instance=%s target=%s",
                note,
                instance_name,
                dst_name,
            )
            await run_db(
                lambda: _set_assignment_fields(
                    biome_instance_id,
                    status="ready",
                )
            )
            await _update_migration_event(
                migration_event_id,
                result="failed",
                rejection_reason=note,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=time.monotonic() - start,
            )
            _emit_migration_nats_event(
                cluster_id,
                "migration.failed",
                {
                    "migration_event_id": migration_event_id,
                    "biome_instance_id": biome_instance_id,
                    "error": note,
                },
            )
            _record_outcome_metric("failed", time.monotonic() - start)
            span.record_exception(exc)
            span.set_attribute("gough.outcome", "failed")
            return {
                "status": "failed",
                "duration_ms": int((time.monotonic() - start) * 1000),
                "verdict": note,
            }

        # Verify: destination cluster member must report Online post-move.
        verified = True
        try:
            with _tracer.start_as_current_span("gough.migration.verify"):
                status = await asyncio.to_thread(
                    lxd_extra.get_cluster_status,
                    cluster_id=cluster_id,
                )
            verified = any(
                m.name == dst_name and m.status == "Online" for m in status.members
            )
        except lxd_extra.LXDError as exc:
            log.warning(
                "execute_migration: post-move verify failed (treating as "
                "destination failure): %s",
                exc,
            )
            verified = False

        if verified:
            await run_db(
                lambda: _set_assignment_fields(
                    biome_instance_id,
                    node_id=dst_node_id,
                    status="ready",
                )
            )
            await _update_migration_event(
                migration_event_id,
                result="executed",
                completed_at=datetime.now(timezone.utc),
                duration_seconds=time.monotonic() - start,
            )
            _emit_migration_nats_event(
                cluster_id,
                "migration.completed",
                {
                    "migration_event_id": migration_event_id,
                    "biome_instance_id": biome_instance_id,
                    "dst_node_id": dst_node_id,
                },
            )
            duration = time.monotonic() - start
            _record_outcome_metric("executed", duration)
            span.set_attribute("gough.outcome", "executed")
            log.info(
                "execute_migration: completed biome_instance_id=%s "
                "src=%s dst=%s duration_s=%.2f",
                biome_instance_id,
                src_node_id,
                dst_node_id,
                duration,
            )
            return {
                "status": "executed",
                "duration_ms": int(duration * 1000),
                "verdict": "executed",
            }

        # Destination verification failed: this policy knob is the only
        # thing in the schema that governs this branch (spec silent on any
        # finer-grained retry/backoff semantics -- see this change's report).
        from ..api.migration import _load_or_default_policy

        policy_dict = await run_db(lambda: _load_or_default_policy(cluster_id))
        rollback_on_failure = bool(
            policy_dict.get("rollback_on_destination_failure", True)
        )

        if not rollback_on_failure:
            await run_db(
                lambda: _set_assignment_fields(
                    biome_instance_id,
                    node_id=dst_node_id,
                    status="failed",
                )
            )
            note = f"Destination node {dst_name} failed post-move verification"
            await _update_migration_event(
                migration_event_id,
                result="failed",
                rejection_reason=note,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=time.monotonic() - start,
            )
            _emit_migration_nats_event(
                cluster_id,
                "migration.failed",
                {
                    "migration_event_id": migration_event_id,
                    "biome_instance_id": biome_instance_id,
                    "error": note,
                },
            )
            _record_outcome_metric("failed", time.monotonic() - start)
            span.set_attribute("gough.outcome", "failed")
            return {
                "status": "failed",
                "duration_ms": int((time.monotonic() - start) * 1000),
                "verdict": note,
            }

        rolled_back_ok, rollback_detail = await _attempt_rollback(
            instance_name=instance_name,
            src_name=src_name,
            cluster_id=cluster_id,
        )
        _record_rollback_metric()
        final_status = "rolled_back" if rolled_back_ok else "failed"
        await run_db(
            lambda: _set_assignment_fields(
                biome_instance_id,
                status="ready" if rolled_back_ok else "failed",
            )
        )
        await _update_migration_event(
            migration_event_id,
            result=final_status,
            rejection_reason=rollback_detail,
            completed_at=datetime.now(timezone.utc),
            duration_seconds=time.monotonic() - start,
        )
        _emit_migration_nats_event(
            cluster_id,
            f"migration.{final_status}",
            {
                "migration_event_id": migration_event_id,
                "biome_instance_id": biome_instance_id,
                "detail": rollback_detail,
            },
        )
        _record_outcome_metric(final_status, time.monotonic() - start)
        span.set_attribute("gough.outcome", final_status)
        log.warning(
            "execute_migration: destination verify failed, rollback=%s "
            "biome_instance_id=%s detail=%s",
            rolled_back_ok,
            biome_instance_id,
            rollback_detail,
        )
        return {
            "status": final_status,
            "duration_ms": int((time.monotonic() - start) * 1000),
            "verdict": rollback_detail,
        }
