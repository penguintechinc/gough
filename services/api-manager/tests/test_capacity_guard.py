"""Tests for the capacity guard: fit math, fail-closed refusals, ingest, reservations.

Fit-math tests are pure (no DB). Reservation, ingest and read-model tests run
against a real SQLite file engine, not mocks, so the SQL path is exercised.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app import capacity_guard as cg
from app.models_m1 import RESERVATION_ACTIVE, RESERVATION_EXPIRED, RESERVATION_RELEASED
from app.models_sqlalchemy import Base
from pydantic import ValidationError
from sqlalchemy import create_engine, select

GIB = 1024**3
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


def _policy(**overrides: Any) -> cg.CapacityPolicy:
    """Return a policy with spec defaults, optionally overridden."""
    return cg.CapacityPolicy(**overrides)


def _cap(**overrides: Any) -> cg.HostCapacity:
    """Build a healthy 16-core / 64 GiB host with one 1000 GiB pool and no PSI trips."""
    base: dict[str, Any] = dict(
        age_s=5.0,
        logical_cores=16,
        memory_total_bytes=64 * GIB,
        memory_available_bytes=40 * GIB,
        pools_total={"default": 1000 * GIB},
        committed=cg.Load(),
        instances_without_limits=0,
        psi_memory_some_avg60=1.0,
        psi_io_full_avg60=0.5,
        psi_cpu_some_avg60=5.0,
        section_errors=(),
    )
    base.update(overrides)
    return cg.HostCapacity(**base)


def _req(
    cpu: float = 0.0, mem_gib: float = 0.0, disk_gib: float = 0.0, pool: str = "default"
) -> cg.FitRequest:
    """Build a FitRequest with GiB-sized memory and disk."""
    return cg.FitRequest(
        cpu_cores=cpu,
        memory_bytes=int(mem_gib * GIB),
        disk_bytes=int(disk_gib * GIB),
        pool=pool,
    )


def _snapshot_dict(collected: datetime, **overrides: Any) -> dict[str, Any]:
    """Return a valid gough.host_resources.v1 body, with optional top-level overrides."""
    body: dict[str, Any] = {
        "schema": "gough.host_resources.v1",
        "collected_at": collected.isoformat(),
        "cpu": {
            "logical_cores": 16,
            "usage_pct": 10.0,
            "load1": 1.0,
            "load5": 1.0,
            "load15": 1.0,
        },
        "memory": {
            "total_bytes": 64 * GIB,
            "available_bytes": 40 * GIB,
            "swap_total_bytes": 0,
            "swap_used_bytes": 0,
        },
        "pressure": {
            "cpu": {"some_avg10": 1.0, "some_avg60": 1.0},
            "memory": {"some_avg10": 0.0, "some_avg60": 1.0, "full_avg60": 0.0},
            "io": {"some_avg60": 0.0, "full_avg60": 0.5},
        },
        "storage_pools": [
            {
                "name": "default",
                "driver": "zfs",
                "total_bytes": 1000 * GIB,
                "used_bytes": 100 * GIB,
            }
        ],
        "host_root_fs": {"total_bytes": 100 * GIB, "free_bytes": 60 * GIB},
        "committed": {
            "cpu_cores": 0.0,
            "memory_bytes": 0,
            "disk_bytes": {"default": 0},
            "instances": 0,
            "instances_without_limits": 0,
        },
        "errors": [],
    }
    body.update(overrides)
    return body


@pytest.fixture()
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Real SQLite engine with the nodes and capacity tables; kill switch OFF."""
    monkeypatch.setattr(cg, "_feature_enabled_sync", lambda *_a, **_k: False)
    eng = create_engine(f"sqlite:///{tmp_path}/guard.db")
    tables = [
        Base.metadata.tables["nodes"],
        Base.metadata.tables["access_agents"],
        cg.NodeResourceSnapshot.__table__,
        cg.CapacityReservation.__table__,
    ]
    Base.metadata.create_all(eng, tables=tables)
    with eng.begin() as conn:
        conn.execute(
            Base.metadata.tables["nodes"]
            .insert()
            .values(id=1, name="host-a", tenant_id="__default__", state="ready")
        )
    return eng


# ---------------------------------------------------------------------------
# Pure fit math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_", "committed", "expected_reason"),
    [
        (_req(cpu=56.0), cg.Load(), None),  # (16-2)*4 = 56 exactly fits
        (_req(cpu=56.5), cg.Load(), "insufficient_cpu"),
        (_req(cpu=6.0), cg.Load(cpu_cores=50.0), None),
        (_req(cpu=7.0), cg.Load(cpu_cores=50.0), "insufficient_cpu"),
        (_req(mem_gib=57.6), cg.Load(), None),  # 64 - 6.4 reserve
        (_req(mem_gib=58.0), cg.Load(), "insufficient_memory"),
        (_req(mem_gib=10.0), cg.Load(memory_bytes=48 * GIB), "insufficient_memory"),
        (_req(disk_gib=850.0), cg.Load(), None),  # 1000 - 15% reserve
        (_req(disk_gib=851.0), cg.Load(), "insufficient_disk:default"),
        (_req(disk_gib=10.0), cg.Load(disk_bytes={"default": 840 * GIB}), None),
        (
            _req(disk_gib=11.0),
            cg.Load(disk_bytes={"default": 840 * GIB}),
            "insufficient_disk:default",
        ),
        (_req(pool="fast"), cg.Load(), "unknown_pool:fast"),
    ],
)
def test_fit_dimensions_table(
    request_: cg.FitRequest, committed: cg.Load, expected_reason: str | None
) -> None:
    """Each dimension admits exactly up to allocatable and refuses one step past it."""
    result = cg.evaluate_fit(_cap(committed=committed), request_, cg.Load(), _policy())
    if expected_reason is None:
        assert result.ok, result.reasons
        assert result.reasons == ()
    else:
        assert not result.ok
        assert expected_reason in result.reasons


@pytest.mark.parametrize(
    ("logical_cores", "expected_cpu_alloc"),
    [
        (8, (8 - 2) * 4.0),  # min(2) dominates: max(2, 0.8) = 2
        (64, (64 - 6.4) * 4.0),  # pct dominates: max(2, 6.4) = 6.4
    ],
)
def test_cpu_reserve_min_vs_pct(logical_cores: int, expected_cpu_alloc: float) -> None:
    """CPU reserve is max(min cores, pct of cores)."""
    result = cg.evaluate_fit(
        _cap(logical_cores=logical_cores), _req(), cg.Load(), _policy()
    )
    assert result.allocatable["cpu_cores"] == pytest.approx(expected_cpu_alloc)


@pytest.mark.parametrize(
    ("total_gib", "expected_reserve_gib"),
    [
        (8, 4.0),  # min 4 GiB dominates 10% (0.8 GiB)
        (64, 6.4),  # 10% dominates
    ],
)
def test_memory_reserve_min_vs_pct(total_gib: int, expected_reserve_gib: float) -> None:
    """Memory reserve is max(4 GiB, 10% of total)."""
    result = cg.evaluate_fit(
        _cap(
            memory_total_bytes=total_gib * GIB, memory_available_bytes=total_gib * GIB
        ),
        _req(),
        cg.Load(),
        _policy(),
    )
    assert result.allocatable["memory_bytes"] == pytest.approx(
        (total_gib - expected_reserve_gib) * GIB
    )


def test_overcommit_ratio_applies_to_cpu_and_memory() -> None:
    """Overcommit ratios scale allocatable; cpu defaults to 4.0 and memory to 1.0."""
    result = cg.evaluate_fit(
        _cap(), _req(), cg.Load(), _policy(cpu_overcommit=1.0, memory_overcommit=2.0)
    )
    assert result.allocatable["cpu_cores"] == pytest.approx(14.0)
    assert result.allocatable["memory_bytes"] == pytest.approx(2 * (64 - 6.4) * GIB)


def test_pending_reservations_count_against_allocatable() -> None:
    """Pending load from active reservations is added to committed before the check."""
    result = cg.evaluate_fit(_cap(), _req(cpu=20.0), cg.Load(cpu_cores=40.0), _policy())
    assert not result.ok
    assert "insufficient_cpu" in result.reasons


# ---------------------------------------------------------------------------
# Fail-closed: missing and stale snapshots
# ---------------------------------------------------------------------------


def test_missing_snapshot_refuses() -> None:
    """No snapshot at all is a refusal, never a pass."""
    result = cg.evaluate_fit(None, _req(), cg.Load(), _policy())
    assert not result.ok
    assert result.reasons == ("stale_or_missing_snapshot",)


def test_stale_snapshot_refuses() -> None:
    """A snapshot older than the max age refuses with the stale reason."""
    result = cg.evaluate_fit(
        _cap(age_s=61.0), _req(), cg.Load(), _policy(snapshot_max_age_s=60.0)
    )
    assert result.reasons == ("stale_or_missing_snapshot",)


def test_fresh_boundary_passes() -> None:
    """Exactly at the max age is still fresh."""
    result = cg.evaluate_fit(
        _cap(age_s=60.0), _req(), cg.Load(), _policy(snapshot_max_age_s=60.0)
    )
    assert result.ok


# ---------------------------------------------------------------------------
# Section failures: zeros reported with an error key are never trusted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("errors", "expected_reason"),
    [
        (("lxd_unavailable",), "snapshot_section_failed:lxd"),
        (("lxd_timeout",), "snapshot_section_failed:lxd"),
        (("lxd_error",), "snapshot_section_failed:lxd"),
        (("host_timeout",), "snapshot_section_failed:host"),
        (("host_unavailable",), "snapshot_section_failed:host"),
        (("host_error",), "snapshot_section_failed:host"),
        (("psi_error",), "snapshot_section_failed:psi"),
        (("limit_unparsed:web-1:limits.cpu",), "snapshot_section_failed:committed"),
        (("device_unparsed:web-1:size",), "snapshot_section_failed:committed"),
        (("something_new",), "snapshot_section_failed:unknown"),
    ],
)
def test_failed_section_refuses(errors: tuple[str, ...], expected_reason: str) -> None:
    """Every agent error key invalidates its section and refuses the fit."""
    result = cg.evaluate_fit(_cap(section_errors=errors), _req(), cg.Load(), _policy())
    assert not result.ok
    assert expected_reason in result.reasons


def test_lxd_unavailable_refuses_even_with_zero_committed() -> None:
    """lxd_unavailable reports committed=0; that zero must not pass as real headroom."""
    cap = _cap(committed=cg.Load(), section_errors=("lxd_unavailable",))
    result = cg.evaluate_fit(
        cap, _req(cpu=1.0, mem_gib=1.0, disk_gib=1.0), cg.Load(), _policy()
    )
    assert not result.ok
    assert "snapshot_section_failed:lxd" in result.reasons


def test_section_failure_is_reported_alongside_capacity_reasons() -> None:
    """Capacity figures stay visible when a section failed, so operators see both causes."""
    result = cg.evaluate_fit(
        _cap(section_errors=("host_error",)), _req(cpu=100.0), cg.Load(), _policy()
    )
    assert "snapshot_section_failed:host" in result.reasons
    assert "insufficient_cpu" in result.reasons


# ---------------------------------------------------------------------------
# Pressure gates and PSI availability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "signal"),
    [
        ({"psi_memory_some_avg60": 10.5}, "memory_some_avg60"),
        ({"psi_io_full_avg60": 10.5}, "io_full_avg60"),
        ({"psi_cpu_some_avg60": 50.5}, "cpu_some_avg60"),
        ({"memory_available_bytes": 3 * GIB}, "memory_available_below_reserve"),
    ],
)
def test_pressure_gate_trips(overrides: dict[str, Any], signal: str) -> None:
    """Each live pressure gate refuses with node_pressured and names the signal."""
    result = cg.evaluate_fit(_cap(**overrides), _req(), cg.Load(), _policy())
    assert not result.ok
    assert "node_pressured" in result.reasons
    assert signal in result.pressure_signals


def test_pressure_at_threshold_does_not_trip() -> None:
    """Gates are strictly greater-than; exactly at the threshold passes."""
    result = cg.evaluate_fit(
        _cap(psi_memory_some_avg60=10.0), _req(), cg.Load(), _policy()
    )
    assert result.ok


def test_psi_null_is_warning_not_failure() -> None:
    """PSI unsupported (all null, no error) skips PSI gates and warns only."""
    cap = _cap(
        psi_memory_some_avg60=None, psi_io_full_avg60=None, psi_cpu_some_avg60=None
    )
    result = cg.evaluate_fit(cap, _req(cpu=1.0), cg.Load(), _policy())
    assert result.ok
    assert "psi_unavailable" in result.warnings


def test_memory_headroom_still_gated_when_psi_null() -> None:
    """With PSI null, the memory-below-reserve gate still applies."""
    cap = _cap(
        psi_memory_some_avg60=None,
        psi_io_full_avg60=None,
        psi_cpu_some_avg60=None,
        memory_available_bytes=1 * GIB,
    )
    result = cg.evaluate_fit(cap, _req(), cg.Load(), _policy())
    assert "node_pressured" in result.reasons


@pytest.mark.parametrize(
    ("fail", "expected_in"), [(False, "warnings"), (True, "reasons")]
)
def test_unaccounted_instances_warn_or_fail(fail: bool, expected_in: str) -> None:
    """Instances without limits warn by default and hard-fail when configured."""
    result = cg.evaluate_fit(
        _cap(instances_without_limits=2),
        _req(),
        cg.Load(),
        _policy(unaccounted_instances_fail=fail),
    )
    target = result.reasons if expected_in == "reasons" else result.warnings
    assert "unaccounted_instances" in target
    assert result.ok is (not fail)


# ---------------------------------------------------------------------------
# Snapshot ingest validation
# ---------------------------------------------------------------------------


def test_valid_snapshot_parses() -> None:
    """A well-formed v1 body validates and keeps its errors list."""
    snap = cg.parse_snapshot(_snapshot_dict(NOW, errors=["psi_error"]))
    assert snap.errors == ["psi_error"]
    assert snap.collected_at.tzinfo is not None


def test_unknown_schema_version_rejected() -> None:
    """A schema other than gough.host_resources.v1 is refused."""
    with pytest.raises(ValidationError):
        cg.parse_snapshot(_snapshot_dict(NOW, schema="gough.host_resources.v2"))


@pytest.mark.parametrize(
    "mutation",
    [
        {"cpu": {"logical_cores": 0}},
        {"memory": {"total_bytes": -1, "available_bytes": 0}},
        {
            "committed": {
                "cpu_cores": 1.0,
                "memory_bytes": 0,
                "disk_bytes": {},
                "instances": 0,
                "instances_without_limits": -1,
            }
        },
        {"collected_at": "not-a-date"},
    ],
)
def test_malformed_snapshot_rejected(mutation: dict[str, Any]) -> None:
    """Out-of-range or malformed fields fail validation."""
    body = _snapshot_dict(NOW)
    body.update(mutation)
    with pytest.raises(ValidationError):
        cg.parse_snapshot(body)


def test_null_pressure_accepted() -> None:
    """pressure: null (PSI unsupported) is a valid snapshot."""
    snap = cg.parse_snapshot(_snapshot_dict(NOW, pressure=None))
    assert snap.pressure is None


def test_record_snapshot_ignores_older_and_upserts_newer(engine: Any) -> None:
    """Latest collected_at wins; an out-of-order older snapshot is refused."""
    newer = cg.parse_snapshot(_snapshot_dict(NOW))
    older = cg.parse_snapshot(
        _snapshot_dict(
            NOW - timedelta(seconds=30),
            cpu={
                "logical_cores": 4,
                "usage_pct": 1.0,
                "load1": 0.0,
                "load5": 0.0,
                "load15": 0.0,
            },
        )
    )
    with engine.begin() as conn:
        assert cg.record_snapshot(conn, 1, newer, NOW) is cg.SnapshotOutcome.STORED
    with engine.begin() as conn:
        outcome = cg.record_snapshot(conn, 1, older, NOW + timedelta(seconds=5))
        assert outcome is cg.SnapshotOutcome.OUT_OF_ORDER
    with engine.connect() as conn:
        rows = conn.execute(select(cg.NodeResourceSnapshot.__table__)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["logical_cores"] == 16


# ---------------------------------------------------------------------------
# Reservations: real DB path
# ---------------------------------------------------------------------------


def _seed_snapshot(engine: Any, collected: datetime = NOW, **overrides: Any) -> None:
    """Write a fresh snapshot for node 1 at ``collected``."""
    snap = cg.parse_snapshot(_snapshot_dict(collected, **overrides))
    with engine.begin() as conn:
        cg.record_snapshot(conn, 1, snap, NOW)


def test_reserve_grants_then_refuses_when_full(engine: Any) -> None:
    """A reservation that consumes the allocatable makes the next one refuse."""
    _seed_snapshot(engine)
    first = cg.reserve(engine, 1, _req(cpu=40.0), now=NOW, policy=_policy())
    second = cg.reserve(engine, 1, _req(cpu=40.0), now=NOW, policy=_policy())
    assert first.fit.ok and first.reservation_id is not None
    assert not second.fit.ok and second.reservation_id is None
    assert "insufficient_cpu" in second.fit.reasons


def test_concurrent_reserve_exactly_one_wins(engine: Any) -> None:
    """Two threads racing for the same headroom: exactly one reservation is granted."""
    _seed_snapshot(engine)
    barrier = threading.Barrier(2)
    outcomes: list[cg.ReserveResult] = []
    lock = threading.Lock()

    def _attempt() -> None:
        barrier.wait()
        res = cg.reserve(engine, 1, _req(cpu=40.0), now=NOW, policy=_policy())
        with lock:
            outcomes.append(res)

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(outcomes) == 2
    assert sum(1 for r in outcomes if r.fit.ok) == 1
    with engine.connect() as conn:
        active = conn.execute(
            select(cg.CapacityReservation.__table__).where(
                cg.CapacityReservation.__table__.c.status == RESERVATION_ACTIVE
            )
        ).all()
    assert len(active) == 1


def test_reserve_refuses_without_snapshot(engine: Any) -> None:
    """No snapshot means no reservation: fail closed."""
    result = cg.reserve(engine, 1, _req(cpu=1.0), now=NOW, policy=_policy())
    assert not result.fit.ok
    assert result.fit.reasons == ("stale_or_missing_snapshot",)
    assert result.reservation_id is None


def test_release_frees_headroom(engine: Any) -> None:
    """Releasing a failed placement's reservation returns its headroom."""
    _seed_snapshot(engine)
    held = cg.reserve(engine, 1, _req(cpu=40.0), now=NOW, policy=_policy())
    assert held.reservation_id is not None
    assert cg.release(engine, held.reservation_id, now=NOW) is True
    again = cg.reserve(engine, 1, _req(cpu=40.0), now=NOW, policy=_policy())
    assert again.fit.ok
    assert cg.release(engine, "00000000-0000-0000-0000-000000000000", now=NOW) is False


def test_reservation_expires_after_ttl(engine: Any) -> None:
    """Expired reservations stop counting against capacity and are marked expired."""
    _seed_snapshot(engine)
    # Age limit raised so the original snapshot stays fresh at the later clock.
    policy = _policy(reservation_ttl_s=900.0, snapshot_max_age_s=10_000.0)
    held = cg.reserve(engine, 1, _req(cpu=40.0), now=NOW, policy=policy)
    assert held.fit.ok
    later = NOW + timedelta(seconds=901)
    result = cg.reserve(engine, 1, _req(cpu=40.0), now=later, policy=policy)
    assert result.fit.ok
    with engine.connect() as conn:
        table = cg.CapacityReservation.__table__
        status = conn.execute(
            select(table.c.status).where(table.c.public_id == held.reservation_id)
        ).scalar_one()
    assert status == RESERVATION_EXPIRED


def test_snapshot_absorbs_confirmed_reservation_when_committed_grew(
    engine: Any,
) -> None:
    """A confirmed reservation is released by a later snapshot whose committed grew by its size."""
    _seed_snapshot(engine, collected=NOW - timedelta(seconds=30))
    held = cg.reserve(engine, 1, _req(cpu=4.0, mem_gib=8.0), now=NOW, policy=_policy())
    assert held.reservation_id is not None
    assert cg.confirm(engine, held.reservation_id, now=NOW, policy=_policy())
    grown = _snapshot_dict(
        NOW + timedelta(seconds=10),
        committed={
            "cpu_cores": 4.0,
            "memory_bytes": 8 * GIB,
            "disk_bytes": {"default": 0},
            "instances": 1,
            "instances_without_limits": 0,
        },
    )
    with engine.begin() as conn:
        out = cg.record_snapshot(
            conn, 1, cg.parse_snapshot(grown), NOW + timedelta(seconds=10)
        )
    assert out is cg.SnapshotOutcome.STORED
    with engine.connect() as conn:
        table = cg.CapacityReservation.__table__
        status = conn.execute(
            select(table.c.status).where(table.c.public_id == held.reservation_id)
        ).scalar_one()
    assert status == RESERVATION_RELEASED


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_off_by_default_keeps_guard_on(engine: Any) -> None:
    """Unseen or OFF flag keeps the guard on: missing snapshot still refuses."""
    result = cg.check_fit_sync(engine, 1, _req(cpu=1.0), now=NOW, policy=_policy())
    assert not result.ok


def test_kill_switch_on_restores_legacy_and_warns(
    engine: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Flag ON: legacy pass-through, loud WARNING, gauge set to 1."""
    monkeypatch.setattr(
        cg, "_feature_enabled_sync", lambda flag, _d="": flag == cg.KILL_SWITCH_FLAG
    )
    with caplog.at_level("WARNING"):
        result = cg.check_fit_sync(
            engine, 1, _req(cpu=999.0), now=NOW, policy=_policy()
        )
        granted = cg.reserve(engine, 1, _req(cpu=999.0), now=NOW, policy=_policy())
    assert result.ok and "guard_disabled" in result.warnings
    assert granted.fit.ok and granted.reservation_id is None
    assert any("DISABLED" in rec.message for rec in caplog.records)
    assert cg.capacity_guard_disabled._value.get() == 1


# ---------------------------------------------------------------------------
# Read model and migration freshness
# ---------------------------------------------------------------------------


def test_capacity_view_fresh_snapshot(engine: Any) -> None:
    """Read model reports allocatable and freshness for a healthy node."""
    _seed_snapshot(engine)
    view = cg.get_capacity_view(engine, 1, now=NOW, policy=_policy())
    body = view.to_dict()
    assert body["has_snapshot"] is True and body["fresh"] is True
    assert body["allocatable"]["cpu_cores"] == pytest.approx(56.0)


def test_capacity_view_missing_snapshot(engine: Any) -> None:
    """Missing snapshot yields has_snapshot=False and a warning, not zeros."""
    view = cg.get_capacity_view(engine, 1, now=NOW, policy=_policy())
    assert view.to_dict()["has_snapshot"] is False
    assert "missing_snapshot" in view.warnings


def test_free_percentages_fail_closed_on_section_error(engine: Any) -> None:
    """A snapshot with any section error is not fresh for migration headroom."""
    _seed_snapshot(engine, errors=["lxd_unavailable"])
    free = cg.free_percentages(engine, 1, now=NOW, policy=_policy())
    assert free.fresh is False


def test_free_percentages_missing_snapshot(engine: Any) -> None:
    """No snapshot returns zero free and fresh=False."""
    free = cg.free_percentages(engine, 1, now=NOW, policy=_policy())
    assert (free.cpu_free_pct, free.mem_free_pct, free.disk_free_pct, free.fresh) == (
        0.0,
        0.0,
        0.0,
        False,
    )


def test_free_percentages_healthy(engine: Any) -> None:
    """Healthy snapshot yields real free percentages."""
    _seed_snapshot(engine)
    free = cg.free_percentages(engine, 1, now=NOW, policy=_policy())
    assert free.fresh is True
    assert free.cpu_free_pct == pytest.approx(90.0)
    assert free.mem_free_pct == pytest.approx(62.5)
    assert free.disk_free_pct == pytest.approx(60.0)  # min(pool 90%, root fs 60%)
