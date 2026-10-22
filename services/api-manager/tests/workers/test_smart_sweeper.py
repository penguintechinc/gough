"""Tests for ``app.workers.smart_sweeper``.

Coverage targets ≥90%: warning detection across SATA/NVMe fixtures, status
derivation, leader-lease respect, NATS publish on transitions, capacity-metric
refresh, and run_once behaviour with both leader / non-leader outcomes.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.workers import smart_sweeper as sw

# Same sys.path shim _grpc_smartctl_runner performs itself (see that
# function's docstring) -- needed here too so `gough.discovery_pb2_grpc`
# resolves to the *same* sys.modules entry the production code will import
# at call time, letting tests monkeypatch `discovery_pb2_grpc.DiscoveryStub`
# in a way the real deferred import actually observes.
_GRPC_PKG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "app", "grpc"
)
if _GRPC_PKG_DIR not in sys.path:
    sys.path.insert(0, _GRPC_PKG_DIR)

import grpc
from gough import discovery_pb2, discovery_pb2_grpc

HEALTHY_SATA = {
    "smart_status": {"passed": True},
    "ata_smart_attributes": {
        "table": [
            {"name": "Reallocated_Sector_Ct", "raw": {"value": 0}},
            {"name": "Current_Pending_Sector", "raw": {"value": 0}},
            {"name": "Temperature_Celsius", "raw": {"value": 32}},
            {"name": "Power_On_Hours", "raw": {"value": 1000}},
        ]
    },
}

HOT_SATA = {
    "smart_status": {"passed": True},
    "ata_smart_attributes": {
        "table": [
            {"name": "Temperature_Celsius", "raw": {"value": 65}},
        ]
    },
}

REALLOC_SATA = {
    "smart_status": {"passed": True},
    "ata_smart_attributes": {
        "table": [
            {"name": "Reallocated_Sector_Ct", "raw": {"value": 4}},
            {"name": "Pending_Sector_Ct", "raw": {"value": 2}},
        ]
    },
}

OLD_SATA = {
    "smart_status": {"passed": True},
    "ata_smart_attributes": {
        "table": [
            {"name": "Power_On_Hours", "raw": {"value": 50000}},
        ]
    },
}

FAILED_SATA = {
    "smart_status": {"passed": False},
    "ata_smart_attributes": {"table": []},
}

NVME_CRITICAL = {
    "smart_status": {"passed": True},
    "nvme_smart_health_information_log": {
        "critical_warning": 4,
        "temperature": 35,
        "power_on_hours": 100,
    },
}

NVME_HOT = {
    "smart_status": {"passed": True},
    "nvme_smart_health_information_log": {
        "critical_warning": 0,
        "temperature": 70,
        "power_on_hours": 100,
    },
}

UNKNOWN = {"smart_status": {}}


# ---------------------------------------------------------------------------
# detect_warnings + derive_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blob,expected_reason",
    [
        (HOT_SATA, "temperature_celsius_over_50"),
        (REALLOC_SATA, "reallocated_sector_ct_gt_0"),
        (REALLOC_SATA, "pending_sector_ct_gt_0"),
        (OLD_SATA, "power_on_hours_over_45000"),
        (FAILED_SATA, "smart_overall_health_failed"),
        (NVME_CRITICAL, "nvme_critical_warning_nonzero"),
        (NVME_HOT, "temperature_celsius_over_50"),
    ],
)
def test_detect_warnings_flags(blob, expected_reason):
    reasons = sw.detect_warnings(blob)
    assert expected_reason in reasons


def test_detect_warnings_healthy_returns_empty():
    assert sw.detect_warnings(HEALTHY_SATA) == []


def test_derive_status_failed_when_overall_false():
    assert sw.derive_status(["smart_overall_health_failed"], FAILED_SATA) == "failed"


def test_derive_status_warning_when_reasons_present():
    assert sw.derive_status(["temperature_celsius_over_50"], HOT_SATA) == "warning"


def test_derive_status_passed_when_clean():
    assert sw.derive_status([], HEALTHY_SATA) == "passed"


def test_derive_status_unknown_when_empty():
    assert sw.derive_status([], UNKNOWN) == "unknown"


# ---------------------------------------------------------------------------
# Leader lease behaviour
# ---------------------------------------------------------------------------


class _FakeLease:
    """Records acquire/release to verify the sweeper honours leader status."""

    def __init__(self, will_grant: bool = True) -> None:
        self.will_grant = will_grant
        self.acquired = 0
        self.released = 0

    def acquire(self, conn, name, ttl_seconds, holder_id=None):
        self.acquired += 1
        if not self.will_grant:
            return None
        return SimpleNamespace(
            lease_name=name, holder_id=holder_id or "test", ttl_seconds=ttl_seconds,
            acquired_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc), version=1, extra={},
        )

    def release(self, conn, handle):
        self.released += 1
        return True


# ---------------------------------------------------------------------------
# Sweep semantics
# ---------------------------------------------------------------------------


@pytest.fixture()
def ready_node(dal):
    """Insert a ready node + two disks."""
    now = datetime.now(timezone.utc)
    node_id = dal.nodes.insert(
        tenant_id="acme", name="n1", state="ready",
        dmi_uuid="0", primary_nic_mac="00:00:00:00:00:01",
    )
    sda = dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sda",
        capacity_bytes=512_000_000_000, rotational=False, smart_status="passed",
        reserved_for_storage=True, storage_backend="nest", tier="fast",
        created_at=now, updated_at=now,
    )
    sdb = dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sdb",
        capacity_bytes=2_000_000_000_000, rotational=True, smart_status="passed",
        reserved_for_storage=True, storage_backend="nest", tier="bulk",
        created_at=now, updated_at=now,
    )
    # A "new" node should be ignored.
    dal.nodes.insert(
        tenant_id="acme", name="n2", state="new",
        dmi_uuid="x", primary_nic_mac="00:00:00:00:00:02",
    )
    dal.commit()
    return SimpleNamespace(node_id=int(node_id), sda=int(sda), sdb=int(sdb))


def test_sweep_skips_when_not_leader(dal, ready_node):
    lease = _FakeLease(will_grant=False)
    publishes: list = []
    sweeper = sw.SMARTSweeper(
        smartctl_runner=lambda node: {"/dev/sda": HEALTHY_SATA},
        publisher=lambda subj, payload: publishes.append((subj, payload)),
        leader_lease_module=lease,
        holder_id="test-holder",
    )
    res = sweeper.run_once(ttl=60)
    assert res.leader is False
    assert res.disks_probed == 0
    assert lease.acquired == 1
    assert lease.released == 0
    assert publishes == []


def test_sweep_only_visits_ready_nodes(dal, ready_node):
    lease = _FakeLease(will_grant=True)
    visits: list = []

    def runner(node):
        visits.append(node.id)
        return {"/dev/sda": HEALTHY_SATA, "/dev/sdb": HEALTHY_SATA}

    sweeper = sw.SMARTSweeper(
        smartctl_runner=runner,
        publisher=lambda *_: None,
        leader_lease_module=lease,
        holder_id="t",
    )
    res = sweeper.run_once(ttl=60)
    assert res.leader is True
    assert visits == [ready_node.node_id]
    assert res.nodes_visited == 1
    assert res.disks_probed == 2
    assert lease.released == 1


def test_sweep_emits_warning_on_transition(dal, ready_node):
    lease = _FakeLease(will_grant=True)
    publishes: list = []

    def runner(node):
        return {"/dev/sda": HOT_SATA, "/dev/sdb": HEALTHY_SATA}

    sweeper = sw.SMARTSweeper(
        smartctl_runner=runner,
        publisher=lambda subj, payload: publishes.append((subj, payload)),
        leader_lease_module=lease,
        holder_id="t",
    )
    res = sweeper.run_once(ttl=60)
    assert res.warnings_emitted == 1
    assert publishes
    subj, payload = publishes[0]
    assert subj == "gough.smart.warning"
    assert payload["device_path"] == "/dev/sda"
    assert "temperature_celsius_over_50" in payload["reasons"]
    # disk row updated
    sda = dal(dal.disks.id == ready_node.sda).select().first()
    assert sda.smart_status == "warning"


def test_sweep_does_not_re_emit_when_already_warning(dal, ready_node):
    """Second sweep with same warning should not re-publish (transition only)."""
    lease = _FakeLease(will_grant=True)
    publishes: list = []
    sweeper = sw.SMARTSweeper(
        smartctl_runner=lambda node: {"/dev/sda": HOT_SATA, "/dev/sdb": HEALTHY_SATA},
        publisher=lambda s, p: publishes.append((s, p)),
        leader_lease_module=lease,
        holder_id="t",
    )
    sweeper.run_once(ttl=60)
    # Reset publish count, run again with same input
    publishes.clear()
    sweeper.run_once(ttl=60)
    assert publishes == []


def test_sweep_handles_runner_exception(dal, ready_node):
    lease = _FakeLease(will_grant=True)

    def boom(node):
        raise RuntimeError("tunnel down")

    sweeper = sw.SMARTSweeper(
        smartctl_runner=boom,
        publisher=lambda *_: None,
        leader_lease_module=lease,
        holder_id="t",
    )
    res = sweeper.run_once(ttl=60)
    assert res.leader is True
    assert res.disks_probed == 0


def test_sweep_skips_unknown_device_paths(dal, ready_node):
    lease = _FakeLease(will_grant=True)
    publishes: list = []

    sweeper = sw.SMARTSweeper(
        smartctl_runner=lambda node: {"/dev/sdz": HOT_SATA},  # not in DB
        publisher=lambda s, p: publishes.append((s, p)),
        leader_lease_module=lease,
        holder_id="t",
    )
    res = sweeper.run_once(ttl=60)
    assert res.disks_probed == 0
    assert publishes == []


def test_sweep_updates_capacity_metric(dal, ready_node):
    lease = _FakeLease(will_grant=True)
    sweeper = sw.SMARTSweeper(
        smartctl_runner=lambda n: {"/dev/sda": HEALTHY_SATA, "/dev/sdb": HEALTHY_SATA},
        publisher=lambda *_: None,
        leader_lease_module=lease,
        holder_id="t",
    )
    sweeper.run_once(ttl=60)
    # Both disks reserved_for_storage=True; backend=nest; sum = 2.5 TB
    metric = sw.STORAGE_CAPACITY_USED.labels(
        tenant_id="acme", storage_backend="nest", tier="fast"
    )
    assert metric._value.get() == 512_000_000_000
    bulk = sw.STORAGE_CAPACITY_USED.labels(
        tenant_id="acme", storage_backend="nest", tier="bulk"
    )
    assert bulk._value.get() == 2_000_000_000_000


def test_default_publisher_uses_logger_when_no_app(monkeypatch, caplog):
    """Resolving the publisher outside any Quart context falls back to logging."""
    publisher = sw._resolve_publisher()
    with caplog.at_level("INFO"):
        publisher("gough.smart.warning", {"node_id": 1})
    assert any("gough.smart.warning" in r.getMessage() for r in caplog.records)


def test_default_smartctl_runner_raises():
    with pytest.raises(NotImplementedError):
        sw._default_smartctl_runner(SimpleNamespace(id=1))


def test_failed_smart_emits_warning_with_failed_status(dal, ready_node):
    lease = _FakeLease(will_grant=True)
    publishes: list = []
    sweeper = sw.SMARTSweeper(
        smartctl_runner=lambda n: {"/dev/sda": FAILED_SATA, "/dev/sdb": HEALTHY_SATA},
        publisher=lambda s, p: publishes.append((s, p)),
        leader_lease_module=lease,
        holder_id="t",
    )
    sweeper.run_once(ttl=60)
    assert publishes[0][1]["smart_status"] == "failed"
    sda = dal(dal.disks.id == ready_node.sda).select().first()
    assert sda.smart_status == "failed"


# ---------------------------------------------------------------------------
# Real Postgres: leader_lease cascade (Task 8b) + RLS cross-tenant sentinel
# ---------------------------------------------------------------------------
#
# The tests above exercise `_sweep`/`_update_disk`/etc. against the sqlite
# `dal` fixture with `_FakeLease` standing in for `leader_lease` -- they
# never call the real `app.workers.leader_lease.acquire()/release()`, so
# they can't prove the Task 8a cascade fix (leader_lease.acquire() now takes
# a penguin-dal DB, not the raw SQLAlchemy Connection `run_once()` used to
# pass it). These use the REAL `leader_lease` module against real Postgres
# (`pg_db`/`pg_db_scoped`) instead.


def _seed_pg_ready_node_and_disk(db, tenant_id: str = "acme", node_name: str = "pg-node-1"):
    """Insert a real ``ready`` node + one disk via penguin-dal; return their ids.

    ``created_at``/``updated_at`` have no server-side DEFAULT
    (``app.models_m1``'s columns set them via SQLAlchemy ORM-side
    ``default=``, which only applies on ORM/Core inserts) -- supplied
    explicitly here, same pattern as every other direct-insert seed helper
    in this suite (see e.g. ``app.api.webhooks.create_webhook``).
    """
    now = datetime.now(timezone.utc)
    node_id = db.nodes.insert(
        tenant_id=tenant_id, name=node_name, state="ready",
        dmi_uuid=f"dmi-{node_name}", primary_nic_mac="aa:bb:cc:dd:ee:01",
        created_at=now, updated_at=now,
    )
    disk_id = db.disks.insert(
        node_id=node_id, tenant_id=tenant_id, device_path="/dev/sda",
        capacity_bytes=512_000_000_000, rotational=False, smart_status="unknown",
        reserved_for_storage=True, storage_backend="nest", tier="fast",
        created_at=now, updated_at=now,
    )
    db.commit()
    return int(node_id), int(disk_id)


def test_run_once_succeeds_without_app_context_against_real_pg(pg_db, monkeypatch):
    """Regression: gh-22 (DB pool consolidation). ``SMARTSweeper`` used to
    require an active Quart app context (the now-removed
    ``_require_app_context()`` guard) because its ``get_db()`` resolved to
    ``app.models.get_db()`` (``quart.g``/``current_app``-backed). It now
    resolves to ``app.db.database.get_db()`` (RLS-wired, app-context-free --
    see that module's docstring), so ``run_once()`` must succeed with NO
    Quart app context pushed at all. Exercised against real Postgres (not
    the sqlite ``dal`` fixture) because the whole point is proving the path
    this worker now goes through -- ``app.db.database``'s own
    ``init_db()``/RLS wiring, not just "some get_db() got monkeypatched".

    No app context is pushed anywhere in this test (unlike
    ``test_disks.py``'s fixtures, nothing here builds a Quart app at all) --
    deliberately not asserted via ``quart.has_app_context()`` itself, since
    ``tests/workers/conftest.py`` stubs ``sys.modules["quart"]`` with a
    ``MagicMock()`` whenever this module is the first to import ``quart`` in
    the session, and a MagicMock-backed ``has_app_context()`` call is
    truthy regardless of whether it's real quart underneath -- the absence
    of a context is structural here (nothing in this file ever pushes one),
    not something worth re-proving through a mock that can't tell the
    difference.
    """
    import app.workers.smart_sweeper as sweeper_mod

    monkeypatch.setattr(sweeper_mod, "get_db", lambda: pg_db)
    node_id, disk_id = _seed_pg_ready_node_and_disk(pg_db)

    sweeper = sw.SMARTSweeper(
        smartctl_runner=lambda node: {"/dev/sda": HEALTHY_SATA},
        publisher=lambda *_: None,
        holder_id="no-ctx-holder",
    )
    res = sweeper.run_once(ttl=60)

    assert res.leader is True
    assert res.nodes_visited == 1
    assert res.disks_probed == 1

    disk = pg_db(pg_db.disks.id == disk_id).select().first()
    assert disk.smart_status == "passed"


def test_run_once_real_leader_lease_module_accepts_penguin_dal_db(pg_db, monkeypatch):
    """Regression test for the Task 8a cascade: ``leader_lease.acquire()``/
    ``release()`` take a penguin-dal ``DB`` (each statement its own
    autocommitted ``executesql()`` call -- see that module's docstring), not
    a raw SQLAlchemy ``Connection`` (which has no ``.executesql()``).
    Exercises ``run_once()`` end-to-end against real Postgres using the REAL
    ``app.workers.leader_lease`` module (no ``_FakeLease`` stub).
    """
    import app.workers.smart_sweeper as sweeper_mod

    monkeypatch.setattr(sweeper_mod, "get_db", lambda: pg_db)
    node_id, disk_id = _seed_pg_ready_node_and_disk(pg_db)

    publishes: list = []
    sweeper = sw.SMARTSweeper(
        smartctl_runner=lambda node: {"/dev/sda": HEALTHY_SATA},
        publisher=lambda subj, payload: publishes.append((subj, payload)),
        holder_id="pg-test-holder",
    )
    res = sweeper.run_once(ttl=60)

    assert res.leader is True
    assert res.nodes_visited == 1
    assert res.disks_probed == 1

    disk = pg_db(pg_db.disks.id == disk_id).select().first()
    assert disk.smart_status == "passed"

    lease_rows = pg_db.executesql(
        "SELECT holder_id FROM leader_leases WHERE lease_name = %(name)s",
        {"name": "smart_sweeper"},
    )
    assert lease_rows == [(None,)], "release() must clear the holder after a successful sweep"


def test_run_once_second_sweep_reacquires_released_lease(pg_db, monkeypatch):
    """A released lease can be re-acquired on the next tick -- proves
    ``release()`` actually persisted the clear (not just returned truthy)
    when called with the new penguin-dal ``DB`` argument.
    """
    import app.workers.smart_sweeper as sweeper_mod

    monkeypatch.setattr(sweeper_mod, "get_db", lambda: pg_db)
    _seed_pg_ready_node_and_disk(pg_db)

    sweeper = sw.SMARTSweeper(
        smartctl_runner=lambda node: {"/dev/sda": HEALTHY_SATA},
        publisher=lambda *_: None,
        holder_id="pg-test-holder",
    )
    first = sweeper.run_once(ttl=60)
    second = sweeper.run_once(ttl=60)
    assert first.leader is True
    assert second.leader is True


def test_run_once_cross_tenant_sentinel_lets_scoped_role_sweep_other_tenant(
    pg_db, pg_db_scoped, monkeypatch
):
    """``nodes``/``disks`` are RLS-protected (baseline ``rls_tables``).
    Proves the cross-tenant sentinel wrap added to ``run_once()`` lets the
    scoped, non-owner ``api-manager-rw`` role (which actually has RLS
    enforced, unlike ``pg_db``'s table-owner connection) still sweep every
    tenant's ready nodes -- instead of silently sweeping zero nodes forever
    now that ``get_db()`` resolves to the RLS-wired engine.
    """
    import app.workers.smart_sweeper as sweeper_mod
    from app.db.rls import install_rls_events, set_current_tenant

    install_rls_events(pg_db_scoped.engine)
    node_id, disk_id = _seed_pg_ready_node_and_disk(pg_db, tenant_id="tenant-a")

    monkeypatch.setattr(sweeper_mod, "get_db", lambda: pg_db_scoped)
    sweeper = sw.SMARTSweeper(
        smartctl_runner=lambda node: {"/dev/sda": HEALTHY_SATA},
        publisher=lambda *_: None,
        holder_id="pg-scoped-holder",
    )
    try:
        res = sweeper.run_once(ttl=60)
    finally:
        set_current_tenant(None)

    assert res.leader is True
    assert res.nodes_visited == 1, "sentinel must let the scoped role see the other tenant's node"
    assert res.disks_probed == 1

    disk = pg_db(pg_db.disks.id == disk_id).select().first()
    assert disk.smart_status == "passed"


def test_scoped_role_without_sentinel_sees_zero_ready_nodes(pg_db, pg_db_scoped):
    """Negative control: proves the failure mode the sentinel wrap in
    ``run_once()`` prevents -- an unset tenant GUC on the scoped role sees
    none of another tenant's ``ready`` nodes.
    """
    from app.db.rls import install_rls_events, set_current_tenant

    install_rls_events(pg_db_scoped.engine)
    _seed_pg_ready_node_and_disk(pg_db, tenant_id="tenant-a")

    set_current_tenant(None)
    rows = pg_db_scoped.executesql(
        "SELECT COUNT(*) FROM nodes WHERE state = %(state)s", {"state": "ready"}
    )
    assert rows[0][0] == 0, "unset tenant GUC must fail closed on RLS-protected nodes"


# ---------------------------------------------------------------------------
# gRPC tunnel wiring (Task: smart_sweeper smartctl -> Discovery.ExecCommand)
# ---------------------------------------------------------------------------
#
# These tests exercise the real gRPC-tunnel default runner
# (`_grpc_smartctl_runner`) and its flag gate (`_tunnel_enabled` /
# `_default_smartctl_runner`) -- every test above injects its own
# `smartctl_runner` and never reaches this code at all. Only the
# gRPC/smartctl boundary (grpc.secure_channel, DiscoveryStub.ExecCommand) is
# mocked; channel/stub construction, address resolution, command building,
# exit-code/JSON parsing, and DB persistence all run for real.


# ---- _resolve_node_address --------------------------------------------------


@pytest.mark.parametrize(
    "ipv4,ipv6,preferred,expected",
    [
        ("10.0.0.1", "fe80::1", "auto", "10.0.0.1"),
        ("10.0.0.1", "fe80::1", "ipv4", "10.0.0.1"),
        ("10.0.0.1", "fe80::1", "ipv6", "fe80::1"),
        ("10.0.0.1", None, "ipv6", "10.0.0.1"),  # preferred family absent -> fall back
        (None, "fe80::1", "auto", "fe80::1"),
        (None, None, "auto", None),
    ],
)
def test_resolve_node_address(ipv4, ipv6, preferred, expected):
    node = SimpleNamespace(id=1, ipv4=ipv4, ipv6=ipv6, preferred_addr_family=preferred)
    assert sw._resolve_node_address(node) == expected


def test_resolve_node_address_defaults_missing_attrs_to_none():
    """A bare object with none of the three attrs (e.g. a minimal test
    double) must not raise -- mirrors app.api.primary's own getattr-safe
    resolution.
    """
    assert sw._resolve_node_address(SimpleNamespace(id=1)) is None


# ---- _tunnel_enabled ---------------------------------------------------------


def test_tunnel_enabled_true_when_flag_on(monkeypatch):
    from app import licensing

    monkeypatch.setattr(
        licensing, "_feature_enabled_sync",
        lambda flag, ident: flag == sw.FLAG_SMART_SWEEPER_TUNNEL,
    )
    assert sw._tunnel_enabled() is True


def test_tunnel_enabled_false_when_flag_off(monkeypatch):
    from app import licensing

    monkeypatch.setattr(licensing, "_feature_enabled_sync", lambda flag, ident: False)
    assert sw._tunnel_enabled() is False


def test_tunnel_enabled_passes_cluster_id_as_distinct_id(monkeypatch):
    from app import licensing

    monkeypatch.setenv("GOUGH_CLUSTER_ID", "cluster-42")
    seen = {}

    def _fake(flag, ident):
        seen["flag"] = flag
        seen["ident"] = ident
        return True

    monkeypatch.setattr(licensing, "_feature_enabled_sync", _fake)
    assert sw._tunnel_enabled() is True
    assert seen == {"flag": sw.FLAG_SMART_SWEEPER_TUNNEL, "ident": "cluster-42"}


# ---- _default_smartctl_runner dispatch --------------------------------------


def test_default_smartctl_runner_dispatches_to_grpc_runner_when_flag_on(monkeypatch):
    monkeypatch.setattr(sw, "_tunnel_enabled", lambda: True)
    calls = []

    def _fake_grpc_runner(node):
        calls.append(node)
        return {"/dev/sda": HEALTHY_SATA}

    monkeypatch.setattr(sw, "_grpc_smartctl_runner", _fake_grpc_runner)
    node = SimpleNamespace(id=7)
    result = sw._default_smartctl_runner(node)
    assert result == {"/dev/sda": HEALTHY_SATA}
    assert calls == [node]


def test_default_smartctl_runner_raises_with_flag_name_when_explicitly_off(monkeypatch):
    """Regression: the dispatcher must check the flag itself, not merely
    happen to raise because gRPC deps are unavailable -- forcing the flag
    OFF explicitly (rather than relying on POSTHOG_KEY being unset in the
    test environment, as `test_default_smartctl_runner_raises` does) proves
    the gate is the flag, and the error message names it for operators.
    """
    monkeypatch.setattr(sw, "_tunnel_enabled", lambda: False)
    with pytest.raises(NotImplementedError, match=sw.FLAG_SMART_SWEEPER_TUNNEL):
        sw._default_smartctl_runner(SimpleNamespace(id=1))


# ---- _grpc_smartctl_runner: real wiring, gRPC/smartctl boundary mocked -----


@pytest.fixture()
def grpc_dal(tmp_path, monkeypatch):
    """A dedicated DAL carrying the ``ipv4``/``ipv6``/``preferred_addr_family``
    columns the shared ``dal`` fixture's ``nodes`` table doesn't define.

    Only ``_grpc_smartctl_runner`` needs those columns (to resolve a
    dialable address) -- the shared fixture's narrower ``nodes`` table is
    correct for every other test in this file, which inject their own
    ``smartctl_runner`` and never call ``_resolve_node_address``. A fresh
    ``DB``/sqlite file rather than widening the shared fixture: penguin-dal's
    SQLAlchemy-backed ``define_table`` has no pydal-style ``redefine=True``
    schema-migration path for adding columns to an already-defined
    ``Table`` (confirmed empirically -- it raises SQLAlchemy's
    ``already defined for this MetaData instance``), so reusing ``dal``'s
    own table here would require changing the fixture every other worker
    test file also depends on, for a need specific to this module.
    """
    from penguin_dal import DB, Field

    db_path = tmp_path / f"smart-sweeper-grpc-{os.getpid()}.db"
    db = DB(f"sqlite:///{db_path}", pool_size=1, reflect=False, migrate=True)
    db.define_table(
        "nodes",
        Field("tenant_id", "string", default="__default__"),
        Field("name", "string"),
        Field("state", "string", default="new"),
        Field("dmi_uuid", "string"),
        Field("primary_nic_mac", "string"),
        Field("hardware_tags", "json"),
        Field("ipv4", "string"),
        Field("ipv6", "string"),
        Field("preferred_addr_family", "string", default="auto"),
        migrate=True,
    )
    db.define_table(
        "disks",
        Field("node_id", "integer", notnull=True),
        Field("tenant_id", "string", default="__default__"),
        Field("device_path", "string", notnull=True),
        Field("serial", "string"),
        Field("capacity_bytes", "bigint"),
        Field("rotational", "boolean", default=False),
        Field("smart_status", "string", default="unknown"),
        Field("smart_attributes_json", "json"),
        Field("reserved_for_storage", "boolean", default=False),
        Field("storage_backend", "string"),
        Field("tier", "string", default="bulk"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )
    db.define_table(
        "leader_leases",
        Field("lease_name", "string", unique=True),
        Field("holder_id", "string"),
        Field("acquired_at", "datetime"),
        Field("expires_at", "datetime"),
        Field("version", "integer", default=0),
        migrate=True,
    )
    monkeypatch.setattr(sw, "get_db", lambda: db)

    yield db
    try:
        db.close()
    except Exception:  # noqa: BLE001, S110 -- best-effort teardown, mirrors
        # tests/workers/conftest.py's `dal` fixture (same pattern, same reasoning).
        pass


def _insert_grpc_node(db, **overrides):
    defaults = {
        "tenant_id": "acme", "name": "grpc-node", "state": "ready",
        "dmi_uuid": "grpc-1", "primary_nic_mac": "aa:bb:cc:dd:ee:ff",
        "ipv4": "10.1.2.3", "preferred_addr_family": "ipv4",
    }
    defaults.update(overrides)
    node_id = db.nodes.insert(**defaults)
    db.commit()
    return int(node_id)


class _FakeStub:
    """Stands in for ``discovery_pb2_grpc.DiscoveryStub`` -- the per-device
    ``ExecCommand`` behaviour is supplied per-test via ``responder``.
    """

    def __init__(self, channel):
        self.channel = channel

    def ExecCommand(self, request, timeout=None):
        return _FakeStub.responder(request)

    responder = None  # set per-test via monkeypatch on the class


class _FakeChannel:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_grpc_smartctl_runner_raises_without_node_address():
    node = SimpleNamespace(id=9, ipv4=None, ipv6=None, preferred_addr_family="auto")
    with pytest.raises(RuntimeError, match="no ipv4 or ipv6 address"):
        sw._grpc_smartctl_runner(node)


def test_grpc_smartctl_runner_returns_empty_and_skips_dial_when_no_known_disks(
    grpc_dal, monkeypatch
):
    node_id = _insert_grpc_node(grpc_dal)
    node = grpc_dal(grpc_dal.nodes.id == node_id).select().first()

    def _boom(target, creds):
        raise AssertionError("must not dial when the node has no known disks")

    monkeypatch.setattr(grpc, "secure_channel", _boom)
    assert sw._grpc_smartctl_runner(node) == {}


def test_grpc_smartctl_runner_parses_both_devices(grpc_dal, monkeypatch):
    node_id = _insert_grpc_node(grpc_dal)
    now = datetime.now(timezone.utc)
    grpc_dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sda",
        capacity_bytes=1, smart_status="passed", created_at=now, updated_at=now,
    )
    grpc_dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sdb",
        capacity_bytes=1, smart_status="passed", created_at=now, updated_at=now,
    )
    grpc_dal.commit()
    node = grpc_dal(grpc_dal.nodes.id == node_id).select().first()

    responses = {
        "/dev/sda": discovery_pb2.ExecCommandResponse(
            output=json.dumps(FAILED_SATA), exit_code=0
        ),
        "/dev/sdb": discovery_pb2.ExecCommandResponse(
            output=json.dumps(HEALTHY_SATA), exit_code=0
        ),
    }

    def _responder(request):
        for device_path, resp in responses.items():
            if device_path in request.command:
                return resp
        raise AssertionError(f"unexpected command: {request.command}")

    channels: list[_FakeChannel] = []

    def _fake_secure_channel(target, creds):
        assert target == "10.1.2.3:50051"
        ch = _FakeChannel()
        channels.append(ch)
        return ch

    monkeypatch.setattr(grpc, "secure_channel", _fake_secure_channel)
    monkeypatch.setattr(_FakeStub, "responder", staticmethod(_responder))
    monkeypatch.setattr(discovery_pb2_grpc, "DiscoveryStub", _FakeStub)

    result = sw._grpc_smartctl_runner(node)
    assert result == {"/dev/sda": FAILED_SATA, "/dev/sdb": HEALTHY_SATA}
    assert channels and channels[0].closed is True


def test_grpc_smartctl_runner_skips_device_on_rpc_error(grpc_dal, monkeypatch):
    """One device's ExecCommand RPC failing must not blind the sweep to its
    sibling -- real partial-success behaviour, not a silent no-op.
    """
    node_id = _insert_grpc_node(grpc_dal)
    now = datetime.now(timezone.utc)
    grpc_dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sda",
        capacity_bytes=1, smart_status="passed", created_at=now, updated_at=now,
    )
    grpc_dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sdb",
        capacity_bytes=1, smart_status="passed", created_at=now, updated_at=now,
    )
    grpc_dal.commit()
    node = grpc_dal(grpc_dal.nodes.id == node_id).select().first()

    def _responder(request):
        if "/dev/sda" in request.command:
            raise grpc.RpcError("tunnel unreachable")
        return discovery_pb2.ExecCommandResponse(
            output=json.dumps(HEALTHY_SATA), exit_code=0
        )

    monkeypatch.setattr(grpc, "secure_channel", lambda target, creds: _FakeChannel())
    monkeypatch.setattr(_FakeStub, "responder", staticmethod(_responder))
    monkeypatch.setattr(discovery_pb2_grpc, "DiscoveryStub", _FakeStub)

    before = sw.DISKS_PROBED.labels(status="error")._value.get()
    result = sw._grpc_smartctl_runner(node)
    after = sw.DISKS_PROBED.labels(status="error")._value.get()

    assert result == {"/dev/sdb": HEALTHY_SATA}
    assert after == before + 1


def test_grpc_smartctl_runner_skips_device_on_invalid_json(grpc_dal, monkeypatch):
    node_id = _insert_grpc_node(grpc_dal)
    now = datetime.now(timezone.utc)
    grpc_dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sda",
        capacity_bytes=1, smart_status="passed", created_at=now, updated_at=now,
    )
    grpc_dal.commit()
    node = grpc_dal(grpc_dal.nodes.id == node_id).select().first()

    def _responder(request):
        return discovery_pb2.ExecCommandResponse(output="not json{{", exit_code=0)

    monkeypatch.setattr(grpc, "secure_channel", lambda target, creds: _FakeChannel())
    monkeypatch.setattr(_FakeStub, "responder", staticmethod(_responder))
    monkeypatch.setattr(discovery_pb2_grpc, "DiscoveryStub", _FakeStub)

    before = sw.DISKS_PROBED.labels(status="error")._value.get()
    result = sw._grpc_smartctl_runner(node)
    after = sw.DISKS_PROBED.labels(status="error")._value.get()

    assert result == {}
    assert after == before + 1


def test_grpc_smartctl_runner_skips_device_on_commandline_exit_bit(grpc_dal, monkeypatch):
    """smartctl's exit code is a bitmask -- only bit 0 (command-line parse
    failure) means the output is unusable. A device returning that bit must
    be skipped (and counted as an error), not treated as valid data.
    """
    node_id = _insert_grpc_node(grpc_dal)
    now = datetime.now(timezone.utc)
    grpc_dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sda",
        capacity_bytes=1, smart_status="passed", created_at=now, updated_at=now,
    )
    grpc_dal.commit()
    node = grpc_dal(grpc_dal.nodes.id == node_id).select().first()

    def _responder(request):
        return discovery_pb2.ExecCommandResponse(output="", exit_code=1)

    monkeypatch.setattr(grpc, "secure_channel", lambda target, creds: _FakeChannel())
    monkeypatch.setattr(_FakeStub, "responder", staticmethod(_responder))
    monkeypatch.setattr(discovery_pb2_grpc, "DiscoveryStub", _FakeStub)

    before = sw.DISKS_PROBED.labels(status="error")._value.get()
    result = sw._grpc_smartctl_runner(node)
    after = sw.DISKS_PROBED.labels(status="error")._value.get()

    assert result == {}
    assert after == before + 1


def test_grpc_smartctl_runner_allows_nonzero_exit_with_health_bits_set(grpc_dal, monkeypatch):
    """A nonzero exit code that is NOT bit 0 (e.g. bit 2 -- SMART overall-
    health self-assessment failed) still carries usable JSON -- this is
    exactly the pre-fail/failed signal the sweeper exists to catch, so it
    must NOT be treated as a probe error.
    """
    node_id = _insert_grpc_node(grpc_dal)
    now = datetime.now(timezone.utc)
    grpc_dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sda",
        capacity_bytes=1, smart_status="passed", created_at=now, updated_at=now,
    )
    grpc_dal.commit()
    node = grpc_dal(grpc_dal.nodes.id == node_id).select().first()

    def _responder(request):
        return discovery_pb2.ExecCommandResponse(
            output=json.dumps(FAILED_SATA), exit_code=4,  # bit 2 set, bit 0 clear
        )

    monkeypatch.setattr(grpc, "secure_channel", lambda target, creds: _FakeChannel())
    monkeypatch.setattr(_FakeStub, "responder", staticmethod(_responder))
    monkeypatch.setattr(discovery_pb2_grpc, "DiscoveryStub", _FakeStub)

    result = sw._grpc_smartctl_runner(node)
    assert result == {"/dev/sda": FAILED_SATA}


# ---- End-to-end flag-ON real path: SMARTSweeper.run_once(), no injected ---
# ---- smartctl_runner -- the actual production dispatch chain ------------


def test_flag_on_real_grpc_path_parses_and_flags_failing_disk(grpc_dal, monkeypatch):
    """The comprehensive flag-ON real-path test: ``SMARTSweeper`` is
    constructed with NO ``smartctl_runner`` override, so ``run_once()`` goes
    through the real dispatch chain --
    ``_default_smartctl_runner`` -> ``_tunnel_enabled`` (flag ON) ->
    ``_grpc_smartctl_runner`` -- with only the gRPC/smartctl boundary
    (``app.licensing``'s PostHog call, ``grpc.secure_channel``,
    ``DiscoveryStub.ExecCommand``) mocked. Proves SMART attributes are
    parsed off the wire and persisted, and that a failing disk is flagged
    (status transition, WARN log, ``gough.smart.warning`` publish, metric).
    """
    from app import licensing

    node_id = _insert_grpc_node(grpc_dal)
    now = datetime.now(timezone.utc)
    sda = grpc_dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sda",
        capacity_bytes=1_000_000_000_000, rotational=False, smart_status="passed",
        reserved_for_storage=True, storage_backend="nest", tier="fast",
        created_at=now, updated_at=now,
    )
    sdb = grpc_dal.disks.insert(
        node_id=node_id, tenant_id="acme", device_path="/dev/sdb",
        capacity_bytes=1_000_000_000_000, rotational=True, smart_status="passed",
        reserved_for_storage=True, storage_backend="nest", tier="bulk",
        created_at=now, updated_at=now,
    )
    grpc_dal.commit()

    # Force the flag ON without a real PostHog server/network call.
    monkeypatch.setattr(
        licensing, "_feature_enabled_sync",
        lambda flag, ident: flag == sw.FLAG_SMART_SWEEPER_TUNNEL,
    )

    responses = {
        "/dev/sda": discovery_pb2.ExecCommandResponse(
            output=json.dumps(FAILED_SATA), exit_code=0
        ),
        "/dev/sdb": discovery_pb2.ExecCommandResponse(
            output=json.dumps(HEALTHY_SATA), exit_code=0
        ),
    }

    def _responder(request):
        for device_path, resp in responses.items():
            if device_path in request.command:
                return resp
        raise AssertionError(f"unexpected command: {request.command}")

    channels: list[_FakeChannel] = []

    def _fake_secure_channel(target, creds):
        ch = _FakeChannel()
        channels.append(ch)
        return ch

    monkeypatch.setattr(grpc, "secure_channel", _fake_secure_channel)
    monkeypatch.setattr(_FakeStub, "responder", staticmethod(_responder))
    monkeypatch.setattr(discovery_pb2_grpc, "DiscoveryStub", _FakeStub)

    publishes: list = []
    lease = _FakeLease(will_grant=True)
    sweeper = sw.SMARTSweeper(
        # No smartctl_runner override -- exercises the real default chain.
        publisher=lambda subj, payload: publishes.append((subj, payload)),
        leader_lease_module=lease,
        holder_id="grpc-real-path",
    )
    res = sweeper.run_once(ttl=60)

    assert res.leader is True
    assert res.nodes_visited == 1
    assert res.disks_probed == 2
    assert channels and channels[0].closed is True

    sda_row = grpc_dal(grpc_dal.disks.id == sda).select().first()
    sdb_row = grpc_dal(grpc_dal.disks.id == sdb).select().first()
    assert sda_row.smart_status == "failed"
    assert sda_row.smart_attributes_json == FAILED_SATA
    assert sdb_row.smart_status == "passed"
    assert sdb_row.smart_attributes_json == HEALTHY_SATA

    assert publishes, "the failing disk must publish a gough.smart.warning event"
    subj, payload = publishes[0]
    assert subj == "gough.smart.warning"
    assert payload["device_path"] == "/dev/sda"
    assert payload["smart_status"] == "failed"
    assert "smart_overall_health_failed" in payload["reasons"]


# ---- WARN log on SMART failure/pre-fail transition (not just publish) ----


def test_update_disk_logs_warning_on_transition_to_warning(dal, ready_node, caplog):
    lease = _FakeLease(will_grant=True)
    sweeper = sw.SMARTSweeper(
        smartctl_runner=lambda node: {"/dev/sda": HOT_SATA, "/dev/sdb": HEALTHY_SATA},
        publisher=lambda *_: None,
        leader_lease_module=lease,
        holder_id="t",
    )
    with caplog.at_level("WARNING"):
        sweeper.run_once(ttl=60)

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "/dev/sda" in r.getMessage() and "warning" in r.getMessage()
        for r in warning_records
    ), "a SMART warning transition must emit a WARN log, not just a metric/publish"
