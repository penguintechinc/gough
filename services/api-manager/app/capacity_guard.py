"""Capacity guard: fail-closed host fit checks, snapshot ingest, placement reservations.

The access agent reports a ``gough.host_resources.v1`` snapshot in each heartbeat.
This module persists the latest snapshot per node, decides whether a placement
request fits on a node's allocatable capacity, and holds reservations so that
concurrent placements cannot both claim the same headroom.

Every refusal path fails closed: a missing or stale snapshot is never treated as
"plenty of room". The only fail-open path is the explicit core-mechanism kill
switch ``gough.disable-capacity-guard`` (see ``guard_disabled``).
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from prometheus_client import Gauge
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from .licensing import _feature_enabled_sync
from .models_m1 import (
    RESERVATION_ACTIVE,
    RESERVATION_EXPIRED,
    RESERVATION_RELEASED,
    CapacityReservation,
    NodeResourceSnapshot,
)

log = logging.getLogger(__name__)

#: Wire schema accepted from access agents. Any other value is refused with 400.
SCHEMA_VERSION = "gough.host_resources.v1"
#: Kill-switch flag key (core mechanism; unseen/OFF = guard ON).
KILL_SWITCH_FLAG = "gough.disable-capacity-guard"
GIB = 1024**3
_DIALECTS_WITH_ROW_LOCK = frozenset({"postgresql", "mysql", "mariadb"})

# Gauges labelled by node_id only (bounded by fleet size; never per-tenant or per-instance).
node_cpu_committed_ratio: Gauge = Gauge(
    "gough_node_cpu_committed_ratio",
    "Committed CPU cores divided by logical cores, per node (from latest snapshot).",
    ["node_id"],
)
node_memory_available_bytes: Gauge = Gauge(
    "gough_node_memory_available_bytes",
    "MemAvailable reported by the node's latest resource snapshot.",
    ["node_id"],
)
node_pressure_memory_some_avg60: Gauge = Gauge(
    "gough_node_pressure_memory_some_avg60",
    "PSI memory some avg60 per node; -1 when PSI is unavailable on the host.",
    ["node_id"],
)
node_snapshot_age_seconds: Gauge = Gauge(
    "gough_node_snapshot_age_seconds",
    "Seconds since the node's latest snapshot was received by the hub.",
    ["node_id"],
)
node_pressured: Gauge = Gauge(
    "gough_node_pressured",
    "1 when the node's latest snapshot trips a live pressure gate, else 0.",
    ["node_id"],
)
capacity_guard_disabled: Gauge = Gauge(
    "gough_capacity_guard_disabled",
    "1 when the gough.disable-capacity-guard kill switch is ON (legacy fail-open).",
)

_node_locks: dict[int, threading.Lock] = {}
_node_locks_guard = threading.Lock()


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to ``default`` when unset or malformed."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


@dataclass(slots=True, frozen=True)
class CapacityPolicy:
    """Tunable guard thresholds. Defaults match the documented spec values."""

    snapshot_max_age_s: float = 60.0  # 2x the agent's 30s heartbeat interval
    reserve_cpu_cores_min: float = 2.0
    reserve_cpu_pct: float = 0.10
    reserve_memory_min_bytes: int = 4 * GIB
    reserve_memory_pct: float = 0.10
    reserve_storage_pct: float = 0.15
    cpu_overcommit: float = 4.0
    memory_overcommit: float = 1.0
    storage_overcommit: float = 1.0
    pressure_memory_some_avg60_max: float = 10.0
    pressure_io_full_avg60_max: float = 10.0
    pressure_cpu_some_avg60_max: float = 50.0
    unaccounted_instances_fail: bool = False
    reservation_ttl_s: float = 900.0
    default_pool: str = "default"


def policy_from_env() -> CapacityPolicy:
    """Build the guard policy from ``GOUGH_*`` env vars (non-secret tuning only)."""
    base = CapacityPolicy()
    return CapacityPolicy(
        snapshot_max_age_s=_env_float("GOUGH_SNAPSHOT_MAX_AGE_S", base.snapshot_max_age_s),
        reserve_cpu_cores_min=_env_float("GOUGH_RESERVE_CPU_CORES", base.reserve_cpu_cores_min),
        reserve_cpu_pct=_env_float("GOUGH_RESERVE_CPU_PCT", base.reserve_cpu_pct),
        reserve_memory_min_bytes=int(
            _env_float("GOUGH_RESERVE_MEMORY_MIN_GIB", 4.0) * GIB
        ),
        reserve_memory_pct=_env_float("GOUGH_RESERVE_MEMORY_PCT", base.reserve_memory_pct),
        reserve_storage_pct=_env_float("GOUGH_RESERVE_STORAGE_PCT", base.reserve_storage_pct),
        cpu_overcommit=_env_float("GOUGH_CPU_OVERCOMMIT", base.cpu_overcommit),
        memory_overcommit=_env_float("GOUGH_MEMORY_OVERCOMMIT", base.memory_overcommit),
        storage_overcommit=_env_float("GOUGH_STORAGE_OVERCOMMIT", base.storage_overcommit),
        pressure_memory_some_avg60_max=_env_float(
            "GOUGH_PRESSURE_MEMORY_SOME_AVG60", base.pressure_memory_some_avg60_max
        ),
        pressure_io_full_avg60_max=_env_float(
            "GOUGH_PRESSURE_IO_FULL_AVG60", base.pressure_io_full_avg60_max
        ),
        pressure_cpu_some_avg60_max=_env_float(
            "GOUGH_PRESSURE_CPU_SOME_AVG60", base.pressure_cpu_some_avg60_max
        ),
        unaccounted_instances_fail=os.environ.get("GOUGH_CAPACITY_UNACCOUNTED_FAIL", "")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        reservation_ttl_s=_env_float("GOUGH_RESERVATION_TTL_S", base.reservation_ttl_s),
        default_pool=os.environ.get("GOUGH_CAPACITY_DEFAULT_POOL", base.default_pool),
    )


@dataclass(slots=True, frozen=True)
class FitRequest:
    """Resources a placement wants on one node. ``pool`` names the storage pool for disk."""

    cpu_cores: float
    memory_bytes: int
    disk_bytes: int
    pool: str


@dataclass(slots=True, frozen=True)
class Load:
    """Resource totals: committed on the host, or pending in reservations."""

    cpu_cores: float = 0.0
    memory_bytes: int = 0
    disk_bytes: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class HostCapacity:
    """Capacity facts extracted from one snapshot, the input to ``evaluate_fit``."""

    age_s: float
    logical_cores: int
    memory_total_bytes: int
    memory_available_bytes: int
    pools_total: dict[str, int]
    committed: Load
    instances_without_limits: int
    psi_memory_some_avg60: float | None
    psi_io_full_avg60: float | None
    psi_cpu_some_avg60: float | None
    section_errors: tuple[str, ...] = ()


def section_failure_reasons(errors: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Map agent ``errors`` keys to ``snapshot_section_failed:<section>`` reasons.

    A failed collector section is reported by the agent as zeros plus an error
    key, so zeros can never be trusted. Mapping (agent vocabulary, host_resources.py):

    - ``host_timeout|host_unavailable|host_error`` -> ``host`` (cpu, memory, root fs)
    - ``psi_*`` -> ``psi``
    - ``lxd_*`` -> ``lxd`` (storage pools and committed)
    - ``limit_unparsed:*`` / ``device_unparsed:*`` -> ``committed`` (under-counted)
    - any unrecognised key -> ``unknown`` (fail closed)
    """
    sections: set[str] = set()
    for err in errors:
        if err.startswith("host_"):
            sections.add("host")
        elif err.startswith("psi_"):
            sections.add("psi")
        elif err.startswith("lxd_"):
            sections.add("lxd")
        elif err.startswith(("limit_unparsed:", "device_unparsed:")):
            sections.add("committed")
        else:
            sections.add("unknown")
    return tuple(f"snapshot_section_failed:{s}" for s in sorted(sections))


@dataclass(slots=True, frozen=True)
class FitResult:
    """Outcome of a fit check. ``ok`` is False whenever ``reasons`` is non-empty."""

    ok: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    allocatable: dict[str, float]
    committed: dict[str, float]
    pending: dict[str, float]
    pressure_signals: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class ReserveResult:
    """Result of ``reserve``: the fit decision plus the reservation id when granted."""

    fit: FitResult
    reservation_id: str | None


def _refused(reason: str) -> FitResult:
    """Build a fail-closed refusal with no capacity figures."""
    return FitResult(
        ok=False,
        reasons=(reason,),
        warnings=(),
        allocatable={},
        committed={},
        pending={},
    )


def _reserve_for(policy: CapacityPolicy, total: int) -> int:
    """Memory reserve in bytes: max(configured minimum, pct of total)."""
    return max(policy.reserve_memory_min_bytes, int(policy.reserve_memory_pct * total))


def pressure_signals(cap: HostCapacity, policy: CapacityPolicy) -> tuple[str, ...]:
    """Return the names of live pressure gates currently tripped on this host.

    Memory headroom is checked whether or not PSI is available. PSI gates are
    skipped when PSI is unavailable (``None``), which evaluate_fit reports as a warning.
    """
    signals: list[str] = []
    if cap.memory_available_bytes < _reserve_for(policy, cap.memory_total_bytes):
        signals.append("memory_available_below_reserve")
    if cap.psi_memory_some_avg60 is not None and (
        cap.psi_memory_some_avg60 > policy.pressure_memory_some_avg60_max
    ):
        signals.append("memory_some_avg60")
    if cap.psi_io_full_avg60 is not None and (
        cap.psi_io_full_avg60 > policy.pressure_io_full_avg60_max
    ):
        signals.append("io_full_avg60")
    if cap.psi_cpu_some_avg60 is not None and (
        cap.psi_cpu_some_avg60 > policy.pressure_cpu_some_avg60_max
    ):
        signals.append("cpu_some_avg60")
    return tuple(signals)


def evaluate_fit(
    cap: HostCapacity | None,
    request: FitRequest,
    pending: Load,
    policy: CapacityPolicy,
) -> FitResult:
    """Decide whether ``request`` fits on a host. Pure function, no I/O.

    Allocatable per dimension is ``(capacity - reserve) * overcommit``; the fit
    holds iff ``committed + pending + request <= allocatable`` for every
    dimension. ``cap=None`` (no snapshot ever received) always refuses.

    Args:
        cap: Capacity facts from the latest snapshot, or None when missing.
        request: The placement being evaluated.
        pending: Sum of active, unexpired reservations on the node.
        policy: Thresholds and overcommit ratios.

    Returns:
        FitResult with ``ok`` plus reasons (blocking) and warnings (advisory).
    """
    if cap is None or cap.age_s > policy.snapshot_max_age_s:
        return _refused("stale_or_missing_snapshot")

    # Any failed collector section makes the whole snapshot untrusted for fit.
    reasons: list[str] = list(section_failure_reasons(cap.section_errors))
    warnings: list[str] = []

    cpu_reserve = max(policy.reserve_cpu_cores_min, policy.reserve_cpu_pct * cap.logical_cores)
    alloc_cpu = max(0.0, cap.logical_cores - cpu_reserve) * policy.cpu_overcommit
    mem_reserve = _reserve_for(policy, cap.memory_total_bytes)
    alloc_mem = max(0.0, cap.memory_total_bytes - mem_reserve) * policy.memory_overcommit
    alloc: dict[str, float] = {"cpu_cores": alloc_cpu, "memory_bytes": alloc_mem}

    committed: dict[str, float] = {
        "cpu_cores": cap.committed.cpu_cores,
        "memory_bytes": float(cap.committed.memory_bytes),
    }
    pend: dict[str, float] = {
        "cpu_cores": pending.cpu_cores,
        "memory_bytes": float(pending.memory_bytes),
    }

    if cap.committed.cpu_cores + pending.cpu_cores + request.cpu_cores > alloc_cpu:
        reasons.append("insufficient_cpu")
    if cap.committed.memory_bytes + pending.memory_bytes + request.memory_bytes > alloc_mem:
        reasons.append("insufficient_memory")

    total = cap.pools_total.get(request.pool)
    if total is None:
        # Unknown pool is refused even for a zero-disk request: fail closed.
        reasons.append(f"unknown_pool:{request.pool}")
    else:
        pool_alloc = max(0.0, total - policy.reserve_storage_pct * total) * (
            policy.storage_overcommit
        )
        committed_disk = cap.committed.disk_bytes.get(request.pool, 0)
        pending_disk = pending.disk_bytes.get(request.pool, 0)
        alloc[f"disk_bytes:{request.pool}"] = pool_alloc
        committed[f"disk_bytes:{request.pool}"] = float(committed_disk)
        pend[f"disk_bytes:{request.pool}"] = float(pending_disk)
        if committed_disk + pending_disk + request.disk_bytes > pool_alloc:
            reasons.append(f"insufficient_disk:{request.pool}")

    signals = pressure_signals(cap, policy)
    if signals:
        reasons.append("node_pressured")
    if cap.psi_memory_some_avg60 is None and cap.psi_io_full_avg60 is None:
        warnings.append("psi_unavailable")

    if cap.instances_without_limits > 0:
        if policy.unaccounted_instances_fail:
            reasons.append("unaccounted_instances")
        else:
            warnings.append("unaccounted_instances")

    return FitResult(
        ok=not reasons,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        allocatable=alloc,
        committed=committed,
        pending=pend,
        pressure_signals=signals,
    )


def guard_disabled() -> bool:
    """Return True when the ``gough.disable-capacity-guard`` kill switch is ON.

    Unseen or OFF flags keep the guard ON. Flag lookup failures default OFF
    through the licensing client, so the guard fails closed.
    """
    disabled = _feature_enabled_sync(KILL_SWITCH_FLAG, "")
    capacity_guard_disabled.set(1 if disabled else 0)
    if disabled:
        log.warning("capacity guard DISABLED by %s; legacy fail-open behaviour active", KILL_SWITCH_FLAG)
    return disabled


def _aware(value: datetime) -> datetime:
    """Return ``value`` as timezone-aware UTC (SQLite returns naive datetimes)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _node_lock(node_id: int) -> threading.Lock:
    """Return the in-process lock guarding one node's reservation check+insert."""
    with _node_locks_guard:
        lock = _node_locks.get(node_id)
        if lock is None:
            lock = threading.Lock()
            _node_locks[node_id] = lock
        return lock


def _lock_node_row(conn: Connection, node_id: int) -> None:
    """Take a row lock on the node so concurrent placements serialise.

    Postgres/MySQL/MariaDB use ``SELECT ... FOR UPDATE``. SQLite has no row
    locks; callers there rely on the in-process per-node lock plus SQLite's
    database-level write lock.
    """
    from .models_sqlalchemy import Base

    nodes = Base.metadata.tables["nodes"]
    stmt = select(nodes.c.id).where(nodes.c.id == node_id)
    if conn.dialect.name in _DIALECTS_WITH_ROW_LOCK:
        stmt = stmt.with_for_update()
    conn.execute(stmt).first()


def _load_capacity(conn: Connection, node_id: int, now: datetime) -> HostCapacity | None:
    """Load the node's latest snapshot as a HostCapacity, or None when absent."""
    table = NodeResourceSnapshot.__table__
    row = conn.execute(select(table).where(table.c.node_id == node_id)).mappings().first()
    if row is None:
        return None
    payload: dict[str, Any] = row["payload"]
    pressure = payload.get("pressure") or {}
    psi_mem = pressure.get("memory") or {}
    psi_io = pressure.get("io") or {}
    psi_cpu = pressure.get("cpu") or {}
    pools = {p["name"]: int(p["total_bytes"]) for p in payload.get("storage_pools", [])}
    committed_raw = payload.get("committed", {})
    disk_committed = {k: int(v) for k, v in (committed_raw.get("disk_bytes") or {}).items()}
    age = (now - _aware(row["received_at"])).total_seconds()
    return HostCapacity(
        age_s=age,
        logical_cores=int(row["logical_cores"]),
        memory_total_bytes=int(row["memory_total_bytes"]),
        memory_available_bytes=int(row["memory_available_bytes"]),
        pools_total=pools,
        committed=Load(
            cpu_cores=float(row["committed_cpu_cores"]),
            memory_bytes=int(row["committed_memory_bytes"]),
            disk_bytes=disk_committed,
        ),
        instances_without_limits=int(row["instances_without_limits"] or 0),
        psi_memory_some_avg60=_opt_float(psi_mem.get("some_avg60")),
        psi_io_full_avg60=_opt_float(psi_io.get("full_avg60")),
        psi_cpu_some_avg60=_opt_float(psi_cpu.get("some_avg60")),
        section_errors=tuple(str(e) for e in (payload.get("errors") or [])),
    )


def _opt_float(value: Any) -> float | None:
    """Coerce an optional numeric JSON value to float, preserving None."""
    return None if value is None else float(value)


def _expire_and_load_pending(conn: Connection, node_id: int, now: datetime) -> Load:
    """Expire overdue reservations for the node, then total the still-active ones."""
    table = CapacityReservation.__table__
    rows = conn.execute(
        select(table).where(
            table.c.node_id == node_id, table.c.status == RESERVATION_ACTIVE
        )
    ).mappings().all()
    cpu = 0.0
    mem = 0
    disk: dict[str, int] = {}
    for row in rows:
        if _aware(row["expires_at"]) <= now:
            conn.execute(
                update(table)
                .where(table.c.id == row["id"])
                .values(status=RESERVATION_EXPIRED, released_at=now)
            )
            continue
        cpu += float(row["cpu_cores"])
        mem += int(row["memory_bytes"])
        disk[row["pool"]] = disk.get(row["pool"], 0) + int(row["disk_bytes"])
    return Load(cpu_cores=cpu, memory_bytes=mem, disk_bytes=disk)


def check_fit_sync(
    engine: Engine,
    node_id: int,
    request: FitRequest,
    *,
    now: datetime | None = None,
    policy: CapacityPolicy | None = None,
) -> FitResult:
    """Read-only fit check for a node. Use ``reserve`` to actually place work.

    Args:
        engine: SQLAlchemy engine (``get_db().engine``).
        node_id: Internal node primary key.
        request: Resources requested.
        now: Clock override for tests; defaults to current UTC.
        policy: Policy override; defaults to ``policy_from_env()``.

    Returns:
        FitResult. Legacy pass-through (``ok=True``, warning ``guard_disabled``)
        only when the kill switch is ON.
    """
    pol = policy or policy_from_env()
    current = now or datetime.now(UTC)
    if guard_disabled():
        return FitResult(True, (), ("guard_disabled",), {}, {}, {})
    with engine.connect() as conn:
        cap = _load_capacity(conn, node_id, current)
        pending = _expire_and_load_pending(conn, node_id, current)
        conn.commit()
    result = evaluate_fit(cap, request, pending, pol)
    _publish_node_gauges(node_id, cap, pol)
    return result


def check_fit_for_db(db: Any, node_id: int, request: FitRequest) -> FitResult:
    """Fit check from a penguin-dal handle. Fails closed when no engine is available.

    Args:
        db: penguin-dal ``DB`` handle whose ``engine`` attribute backs the check.
        node_id: Internal node primary key.
        request: Resources requested.

    Returns:
        FitResult; ``no_database_engine`` refusal when the handle has no engine.
    """
    engine = getattr(db, "engine", None)
    if engine is None:
        log.error("capacity guard: no database engine on DB handle; refusing node_id=%s", node_id)
        return _refused("no_database_engine")
    return check_fit_sync(engine, node_id, request)


def reserve(
    engine: Engine,
    node_id: int,
    request: FitRequest,
    *,
    now: datetime | None = None,
    policy: CapacityPolicy | None = None,
) -> ReserveResult:
    """Atomically check fit and insert a reservation for a placement.

    The check and insert run in one transaction under a per-node lock (plus a
    row lock on Postgres/MySQL), so two concurrent placements cannot both claim
    the same headroom. Callers must call ``release`` if the placement fails.

    Args:
        engine: SQLAlchemy engine.
        node_id: Internal node primary key.
        request: Resources requested.
        now: Clock override for tests.
        policy: Policy override.

    Returns:
        ReserveResult; ``reservation_id`` is set only when the fit is granted.
    """
    pol = policy or policy_from_env()
    current = now or datetime.now(UTC)
    if guard_disabled():
        return ReserveResult(FitResult(True, (), ("guard_disabled",), {}, {}, {}), None)
    table = CapacityReservation.__table__
    with _node_lock(node_id):
        with engine.begin() as conn:
            _lock_node_row(conn, node_id)
            cap = _load_capacity(conn, node_id, current)
            pending = _expire_and_load_pending(conn, node_id, current)
            fit = evaluate_fit(cap, request, pending, pol)
            reservation_id: str | None = None
            if fit.ok:
                reservation_id = str(uuid.uuid4())
                conn.execute(
                    insert(table).values(
                        public_id=reservation_id,
                        node_id=node_id,
                        cpu_cores=request.cpu_cores,
                        memory_bytes=request.memory_bytes,
                        disk_bytes=request.disk_bytes,
                        pool=request.pool,
                        status=RESERVATION_ACTIVE,
                        created_at=current,
                        expires_at=current + timedelta(seconds=pol.reservation_ttl_s),
                    )
                )
    _publish_node_gauges(node_id, cap, pol)
    if not fit.ok:
        log.info("capacity refusal node_id=%s reasons=%s", node_id, ",".join(fit.reasons))
    return ReserveResult(fit, reservation_id)


def release(engine: Engine, reservation_id: str, *, now: datetime | None = None) -> bool:
    """Release a reservation after a failed placement. Returns True when a row changed."""
    current = now or datetime.now(UTC)
    table = CapacityReservation.__table__
    with engine.begin() as conn:
        result = conn.execute(
            update(table)
            .where(table.c.public_id == reservation_id, table.c.status == RESERVATION_ACTIVE)
            .values(status=RESERVATION_RELEASED, released_at=current)
        )
    return bool(result.rowcount)


class SnapshotCpu(BaseModel):
    """CPU section of the host resource snapshot."""

    model_config = ConfigDict(extra="ignore")
    logical_cores: int = Field(ge=1)
    usage_pct: float | None = Field(default=None, ge=0)
    load1: float | None = Field(default=None, ge=0)
    load5: float | None = Field(default=None, ge=0)
    load15: float | None = Field(default=None, ge=0)


class SnapshotMemory(BaseModel):
    """Memory section of the host resource snapshot."""

    model_config = ConfigDict(extra="ignore")
    total_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    swap_total_bytes: int = Field(default=0, ge=0)
    swap_used_bytes: int = Field(default=0, ge=0)


class SnapshotPsiSome(BaseModel):
    """PSI some-averages for CPU."""

    model_config = ConfigDict(extra="ignore")
    some_avg10: float = Field(ge=0)
    some_avg60: float = Field(ge=0)


class SnapshotPsiMemory(BaseModel):
    """PSI some/full averages for memory."""

    model_config = ConfigDict(extra="ignore")
    some_avg10: float = Field(ge=0)
    some_avg60: float = Field(ge=0)
    full_avg60: float = Field(ge=0)


class SnapshotPsiIo(BaseModel):
    """PSI some/full averages for IO."""

    model_config = ConfigDict(extra="ignore")
    some_avg60: float = Field(ge=0)
    full_avg60: float = Field(ge=0)


class SnapshotPressure(BaseModel):
    """PSI pressure block; the whole block is null when the host has no PSI."""

    model_config = ConfigDict(extra="ignore")
    cpu: SnapshotPsiSome
    memory: SnapshotPsiMemory
    io: SnapshotPsiIo


class SnapshotStoragePool(BaseModel):
    """One storage pool on the host."""

    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=255)
    driver: str = Field(min_length=1, max_length=64)
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)


class SnapshotRootFs(BaseModel):
    """Host root filesystem capacity."""

    model_config = ConfigDict(extra="ignore")
    total_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)


class SnapshotCommitted(BaseModel):
    """Resources committed to instances on the host, as the agent sees them."""

    model_config = ConfigDict(extra="ignore")
    cpu_cores: float = Field(ge=0)
    memory_bytes: int = Field(ge=0)
    disk_bytes: dict[str, int] = Field(default_factory=dict)
    instances: int = Field(ge=0)
    instances_without_limits: int = Field(ge=0)


class HostResourceSnapshot(BaseModel):
    """Validated ``gough.host_resources.v1`` payload sent in the heartbeat body."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    schema_version: Literal["gough.host_resources.v1"] = Field(alias="schema")
    collected_at: datetime
    cpu: SnapshotCpu
    memory: SnapshotMemory
    pressure: SnapshotPressure | None = None
    storage_pools: list[SnapshotStoragePool] = Field(default_factory=list)
    host_root_fs: SnapshotRootFs | None = None
    committed: SnapshotCommitted
    errors: list[str] = Field(default_factory=list)


def parse_snapshot(raw: object) -> HostResourceSnapshot:
    """Validate an untrusted ``resource_snapshot`` body.

    Raises:
        pydantic.ValidationError: On unknown schema version or any malformed field.
    """
    return HostResourceSnapshot.model_validate(raw)


def record_snapshot(
    conn: Connection,
    node_id: int,
    snap: HostResourceSnapshot,
    received_at: datetime,
) -> bool:
    """Upsert the node's latest snapshot and release reservations it has absorbed.

    Out-of-order snapshots (older ``collected_at`` than the stored row) are ignored.

    Returns:
        True when the stored snapshot was replaced; False when skipped as older.
    """
    collected = _aware(snap.collected_at)
    table = NodeResourceSnapshot.__table__
    existing = conn.execute(
        select(table.c.id, table.c.collected_at).where(table.c.node_id == node_id)
    ).mappings().first()
    if existing is not None and _aware(existing["collected_at"]) > collected:
        return False

    values: dict[str, Any] = {
        "node_id": node_id,
        "received_at": received_at,
        "collected_at": collected,
        "schema_version": snap.schema_version,
        "payload": snap.model_dump(mode="json", by_alias=True),
        "logical_cores": snap.cpu.logical_cores,
        "cpu_usage_pct": snap.cpu.usage_pct,
        "memory_total_bytes": snap.memory.total_bytes,
        "memory_available_bytes": snap.memory.available_bytes,
        "committed_cpu_cores": snap.committed.cpu_cores,
        "committed_memory_bytes": snap.committed.memory_bytes,
        "instances_without_limits": snap.committed.instances_without_limits,
        "psi_available": snap.pressure is not None,
    }
    if existing is None:
        conn.execute(insert(table).values(**values))
    else:
        conn.execute(update(table).where(table.c.id == existing["id"]).values(**values))

    res = CapacityReservation.__table__
    conn.execute(
        update(res)
        .where(
            res.c.node_id == node_id,
            res.c.status == RESERVATION_ACTIVE,
            res.c.created_at < collected,
        )
        .values(status=RESERVATION_RELEASED, released_at=received_at)
    )
    return True


def resolve_node_id(conn: Connection, hostname: str) -> int | None:
    """Map an access agent's hostname to its node row (``nodes.name``), or None."""
    from .models_sqlalchemy import Base

    nodes = Base.metadata.tables["nodes"]
    row = conn.execute(select(nodes.c.id).where(nodes.c.name == hostname)).first()
    return None if row is None else int(row[0])


@dataclass(slots=True, frozen=True)
class CapacityView:
    """Read model for ``GET /api/v1/nodes/{id}/capacity``."""

    node_id: int
    has_snapshot: bool
    fresh: bool
    snapshot_age_s: float | None
    capacity: dict[str, Any]
    reserve: dict[str, float]
    committed: dict[str, float]
    pending: dict[str, float]
    allocatable: dict[str, float]
    pressure: dict[str, Any]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the response body shape (explicit keys only)."""
        return {
            "node_id": self.node_id,
            "has_snapshot": self.has_snapshot,
            "fresh": self.fresh,
            "snapshot_age_s": self.snapshot_age_s,
            "capacity": self.capacity,
            "reserve": self.reserve,
            "committed": self.committed,
            "pending": self.pending,
            "allocatable": self.allocatable,
            "pressure": self.pressure,
            "warnings": list(self.warnings),
        }


def get_capacity_view(
    engine: Engine,
    node_id: int,
    *,
    now: datetime | None = None,
    policy: CapacityPolicy | None = None,
) -> CapacityView:
    """Build the capacity read model for one node from its latest snapshot."""
    pol = policy or policy_from_env()
    current = now or datetime.now(UTC)
    with engine.connect() as conn:
        cap = _load_capacity(conn, node_id, current)
        pending = _expire_and_load_pending(conn, node_id, current)
        conn.commit()
    if cap is None:
        return CapacityView(
            node_id=node_id, has_snapshot=False, fresh=False, snapshot_age_s=None,
            capacity={}, reserve={}, committed={}, pending={}, allocatable={},
            pressure={}, warnings=("missing_snapshot",),
        )
    cpu_reserve = max(pol.reserve_cpu_cores_min, pol.reserve_cpu_pct * cap.logical_cores)
    mem_reserve = _reserve_for(pol, cap.memory_total_bytes)
    fit = evaluate_fit(cap, FitRequest(0.0, 0, 0, pol.default_pool), pending, pol)
    _publish_node_gauges(node_id, cap, pol)
    return CapacityView(
        node_id=node_id,
        has_snapshot=True,
        fresh=cap.age_s <= pol.snapshot_max_age_s,
        snapshot_age_s=round(cap.age_s, 3),
        capacity={
            "logical_cores": cap.logical_cores,
            "memory_total_bytes": cap.memory_total_bytes,
            "memory_available_bytes": cap.memory_available_bytes,
            "storage_pools_total_bytes": cap.pools_total,
        },
        reserve={
            "cpu_cores": cpu_reserve,
            "memory_bytes": float(mem_reserve),
            "storage_pct": pol.reserve_storage_pct,
        },
        committed=fit.committed,
        pending=fit.pending,
        allocatable=fit.allocatable,
        pressure={
            "psi_available": cap.psi_memory_some_avg60 is not None,
            "memory_some_avg60": cap.psi_memory_some_avg60,
            "io_full_avg60": cap.psi_io_full_avg60,
            "cpu_some_avg60": cap.psi_cpu_some_avg60,
            "tripped": list(pressure_signals(cap, pol)),
        },
        warnings=fit.warnings,
    )


@dataclass(slots=True, frozen=True)
class FreePercentages:
    """Free-capacity percentages derived from a snapshot, for migration headroom checks."""

    cpu_free_pct: float
    mem_free_pct: float
    disk_free_pct: float
    fresh: bool


def free_percentages(
    engine: Engine,
    node_id: int,
    *,
    now: datetime | None = None,
    policy: CapacityPolicy | None = None,
) -> FreePercentages:
    """Derive free CPU/memory/disk percentages from the node's latest snapshot.

    Missing snapshot returns zeros with ``fresh=False``; callers must fail closed.
    Disk is the minimum free ratio across storage pools and the host root fs.
    """
    pol = policy or policy_from_env()
    current = now or datetime.now(UTC)
    table = NodeResourceSnapshot.__table__
    with engine.connect() as conn:
        row = conn.execute(
            select(
                table.c.payload,
                table.c.received_at,
                table.c.memory_total_bytes,
                table.c.memory_available_bytes,
            ).where(table.c.node_id == node_id)
        ).mappings().first()
    if row is None:
        return FreePercentages(0.0, 0.0, 0.0, False)
    payload: dict[str, Any] = row["payload"]
    fresh = (current - _aware(row["received_at"])).total_seconds() <= pol.snapshot_max_age_s
    # A snapshot with any failed section is not trustworthy for headroom either.
    fresh = fresh and not payload.get("errors")
    usage = (payload.get("cpu") or {}).get("usage_pct")
    cpu_free = 0.0 if usage is None else max(0.0, 100.0 - float(usage))
    total_mem = int(row["memory_total_bytes"])
    mem_free = (int(row["memory_available_bytes"]) / total_mem * 100.0) if total_mem else 0.0
    ratios: list[float] = []
    for pool in payload.get("storage_pools", []):
        total = int(pool["total_bytes"])
        if total > 0:
            ratios.append((total - int(pool["used_bytes"])) / total * 100.0)
    root = payload.get("host_root_fs") or {}
    if int(root.get("total_bytes", 0) or 0) > 0:
        ratios.append(int(root["free_bytes"]) / int(root["total_bytes"]) * 100.0)
    disk_free = min(ratios) if ratios else 0.0
    return FreePercentages(cpu_free, mem_free, disk_free, fresh)


def _publish_node_gauges(node_id: int, cap: HostCapacity | None, policy: CapacityPolicy) -> None:
    """Refresh the per-node gauges from a snapshot view. No-op when no snapshot."""
    label = str(node_id)
    if cap is None:
        node_snapshot_age_seconds.labels(label).set(float("inf"))
        node_pressured.labels(label).set(0)
        return
    node_snapshot_age_seconds.labels(label).set(cap.age_s)
    node_cpu_committed_ratio.labels(label).set(
        cap.committed.cpu_cores / cap.logical_cores if cap.logical_cores else 0.0
    )
    node_memory_available_bytes.labels(label).set(cap.memory_available_bytes)
    node_pressure_memory_some_avg60.labels(label).set(
        -1.0 if cap.psi_memory_some_avg60 is None else cap.psi_memory_some_avg60
    )
    node_pressured.labels(label).set(1 if pressure_signals(cap, policy) else 0)


__all__ = [
    "KILL_SWITCH_FLAG",
    "SCHEMA_VERSION",
    "CapacityPolicy",
    "CapacityView",
    "FitRequest",
    "FitResult",
    "HostCapacity",
    "HostResourceSnapshot",
    "Load",
    "ReserveResult",
    "check_fit_sync",
    "evaluate_fit",
    "get_capacity_view",
    "guard_disabled",
    "parse_snapshot",
    "policy_from_env",
    "pressure_signals",
    "record_snapshot",
    "release",
    "reserve",
    "resolve_node_id",
]
