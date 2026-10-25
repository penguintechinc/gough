"""Default behaviour of ``app.clouds.base.BaseCloud`` shared by every provider.

Covers the concrete defaults providers inherit (reboot = stop then start,
empty catalogue listings, console output) and ``wait_for_state`` polling: it
returns the moment the target is reached, fails fast on ERROR, and times out
with a CloudError. ``time.sleep`` is patched so polling runs instantly.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.clouds.base import BaseCloud, CloudError, Machine, MachineState


class _FakeCloud(BaseCloud):
    """Scripted provider: ``get_machine`` pops successive states from ``states``."""

    provider_type = "fake"

    def __init__(self, states: list[MachineState]) -> None:
        super().__init__({})
        self._states = list(states)
        self.calls: list[str] = []

    def authenticate(self) -> bool:
        return True

    def list_machines(self, filters: dict | None = None) -> list[Machine]:
        return []

    def get_machine(self, machine_id: str) -> Machine:
        self.calls.append(machine_id)
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return Machine(
            id=machine_id, name="m", state=state, provider="fake", provider_id="1"
        )

    def create_machine(self, spec: Any) -> Machine:
        raise NotImplementedError

    def destroy_machine(self, machine_id: str) -> bool:
        return True

    def start_machine(self, machine_id: str) -> bool:
        self.calls.append(f"start:{machine_id}")
        return True

    def stop_machine(self, machine_id: str) -> bool:
        self.calls.append(f"stop:{machine_id}")
        return True


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the module's sleep with a recorder so polling never blocks."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    return slept


def test_reboot_is_stop_then_start_in_order() -> None:
    """Default reboot stops the machine first and returns the result of the start."""
    cloud = _FakeCloud([MachineState.RUNNING])

    assert cloud.reboot_machine("vm-1") is True
    assert cloud.calls == ["stop:vm-1", "start:vm-1"]


def test_reboot_reports_start_failure() -> None:
    """If the start step reports failure, reboot reports failure."""
    cloud = _FakeCloud([MachineState.RUNNING])
    cloud.start_machine = lambda machine_id: False  # type: ignore[method-assign]

    assert cloud.reboot_machine("vm-1") is False


def test_cloud_init_support_follows_class_flag() -> None:
    """get_cloud_init_support mirrors the supports_cloud_init class attribute."""
    cloud = _FakeCloud([MachineState.RUNNING])
    assert cloud.get_cloud_init_support() is True

    cloud.supports_cloud_init = False
    assert cloud.get_cloud_init_support() is False


@pytest.mark.parametrize(
    "method",
    ["list_images", "list_sizes"],
)
def test_catalogue_listings_default_to_empty(method: str) -> None:
    """Providers that do not override image/size listing return an empty list."""
    cloud = _FakeCloud([MachineState.RUNNING])

    assert getattr(cloud, method)({"arch": "amd64"}) == []


def test_regions_and_console_default_empty() -> None:
    """Default region list is empty and console output is an empty string."""
    cloud = _FakeCloud([MachineState.RUNNING])

    assert cloud.list_regions() == []
    assert cloud.get_console_output("vm-1") == ""


def test_wait_for_state_returns_once_target_reached(no_sleep: list[float]) -> None:
    """Polling returns the machine as soon as it reaches the target, sleeping between polls."""
    cloud = _FakeCloud(
        [MachineState.PENDING, MachineState.PENDING, MachineState.RUNNING]
    )

    machine = cloud.wait_for_state("vm-1", MachineState.RUNNING, timeout=60, interval=5)

    assert machine.state is MachineState.RUNNING
    assert len(cloud.calls) == 3
    assert no_sleep == [5, 5]


def test_wait_for_state_fails_fast_on_error_state(no_sleep: list[float]) -> None:
    """Entering ERROR while waiting for another state raises immediately, without further polling."""
    cloud = _FakeCloud([MachineState.PENDING, MachineState.ERROR, MachineState.RUNNING])

    with pytest.raises(CloudError, match="entered error state"):
        cloud.wait_for_state("vm-9", MachineState.RUNNING)

    assert len(cloud.calls) == 2


def test_wait_for_state_times_out(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    """A machine that never reaches the target raises a timeout CloudError naming the target state."""
    clock = iter([0.0, 1.0, 2.0, 400.0])
    monkeypatch.setattr("time.time", lambda: next(clock))
    cloud = _FakeCloud([MachineState.PENDING])

    with pytest.raises(
        CloudError, match="Timeout waiting for machine vm-2 to reach stopped"
    ):
        cloud.wait_for_state("vm-2", MachineState.STOPPED, timeout=300, interval=10)
