"""Wiring tests: alembic head, LXD mandatory limits, migration headroom fail-closed.

Covers the call sites that consume the capacity guard, not the guard math itself
(see tests/test_capacity_guard.py for that).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.clouds.base import CloudError, MachineSpec
from app.clouds.lxd import LXDCloud, ResourceLimitsRequiredError
from app.workers.migration_engine import BiomeSnapshot, NodeSnapshot, _violates_headroom

API_ROOT = Path(__file__).resolve().parent.parent


def test_alembic_single_head() -> None:
    """The migration graph has exactly one head, chained on the agent-link migration."""
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["20261022_1700_access_agents_tenant"]


def _lxd() -> LXDCloud:
    """LXD cloud with authentication stubbed out so create_machine reaches config build."""
    cloud = LXDCloud({"endpoint": "https://lxd.invalid:8443"})
    cloud._authenticated = True
    cloud._client = object()
    return cloud


@pytest.mark.parametrize(
    ("size", "storage_gb"),
    [
        (None, 10),  # no size at all
        ("", 10),
        ("2", 10),  # cpu only, no memory
        ("/4GiB", 10),  # memory missing cpu
        ("2/", 10),  # memory empty
        ("2/4GiB", 0),  # no root disk size
    ],
)
def test_lxd_create_requires_limits(size: Any, storage_gb: int) -> None:
    """Missing cpu, memory, or root disk size is refused before any LXD call."""
    spec = MachineSpec(
        name="web-1", image="ubuntu-22.04", size=size, storage_gb=storage_gb
    )
    with pytest.raises(ResourceLimitsRequiredError, match="resource limits required"):
        _lxd().create_machine(spec)


def test_lxd_limits_built_from_size_with_hard_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With full limits, config carries limits.cpu, limits.memory and enforce=hard."""
    captured: dict[str, Any] = {}
    cloud = _lxd()

    class _Instances:
        def create(
            self, body: dict[str, Any], wait: bool, target: str | None = None
        ) -> Any:
            captured.update(body)
            captured["_target"] = target
            raise RuntimeError("stop after config build")

    cloud._client = type("C", (), {"instances": _Instances()})()
    monkeypatch.setattr(
        cloud, "_parse_image_source", lambda image: {"type": "image", "alias": image}
    )
    spec = MachineSpec(
        name="web-1",
        image="ubuntu-22.04",
        size="2/4GiB",
        storage_gb=20,
        extra={"capacity_guarded": True, "target": "member-b"},
    )
    with pytest.raises(RuntimeError, match="stop after config build"):
        cloud.create_machine(spec)
    assert captured["_target"] == "member-b"
    config = captured["config"]
    assert config["limits.cpu"] == "2"
    assert config["limits.memory"] == "4GiB"
    assert config["limits.memory.enforce"] == "hard"
    assert captured["devices"]["root"]["size"] == "20GiB"


def test_lxd_create_refused_without_guarded_placement() -> None:
    """Direct create_machine without the guard's marker is refused before any LXD call."""
    spec = MachineSpec(name="web-1", image="ubuntu-22.04", size="2/4GiB", storage_gb=20)
    with pytest.raises(CloudError, match="capacity-guarded placement"):
        _lxd().create_machine(spec)


def _biome() -> BiomeSnapshot:
    """A biome with no tag requirements and zero expected load."""
    return BiomeSnapshot(
        biome_instance_id=1,
        biome_id=1,
        biome_kind="lxd_container",
        src_node_id=99,
        lock_to_host=False,
    )


def _policy() -> Any:
    """Policy requiring 10% headroom on each dimension (only the fields the gate reads)."""
    return SimpleNamespace(
        require_target_capacity_headroom_cpu_pct=10,
        require_target_capacity_headroom_mem_pct=10,
        require_target_capacity_headroom_disk_pct=10,
    )


def test_headroom_fails_closed_without_telemetry() -> None:
    """A node with no fresh snapshot is a violation, not 100% free."""
    node = NodeSnapshot(
        node_id=7,
        state="ready",
        cpu_free_pct=0.0,
        mem_free_pct=0.0,
        disk_free_pct=0.0,
        forecast_cpu_free_pct=0.0,
        forecast_mem_free_pct=0.0,
        forecast_disk_free_pct=0.0,
        telemetry_fresh=False,
    )
    violation = _violates_headroom(node, _biome(), _policy())
    assert violation is not None
    assert violation.code == "no_fresh_host_telemetry"
    assert violation.node_id == 7


def test_headroom_passes_with_fresh_telemetry() -> None:
    """Fresh telemetry with ample free capacity passes the headroom gate."""
    node = NodeSnapshot(
        node_id=8,
        state="ready",
        cpu_free_pct=80.0,
        mem_free_pct=80.0,
        disk_free_pct=80.0,
        forecast_cpu_free_pct=80.0,
        forecast_mem_free_pct=80.0,
        forecast_disk_free_pct=80.0,
        telemetry_fresh=True,
    )
    assert _violates_headroom(node, _biome(), _policy()) is None
