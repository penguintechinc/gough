"""Behaviour tests for DHCPFullServer: generated dnsmasq config, process lifecycle and cleanup.

dnsmasq is replaced by a fake asyncio subprocess so tests verify the exact arguments and
stop/kill sequencing without needing root or a real network interface.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from worker.config import WorkerConfig
from worker.services.dhcp_server import DHCPFullServer


def _cfg(**overrides: object) -> WorkerConfig:
    """Full-mode config with a fixed boot URL and a DHCP range."""
    base: dict[str, object] = {
        "api_manager_url": "http://api.test",
        "worker_api_key": "k",
        "worker_id": "w",
        "dhcp_mode": "full",
        "dhcp_interface": "eth1",
        "dhcp_range_start": "10.0.0.100",
        "dhcp_range_end": "10.0.0.200",
        "dhcp_gateway": "10.0.0.1",
        "dhcp_dns_servers": "8.8.8.8,1.1.1.1",
        "tftp_root": "/srv/tftp",
        "http_boot_url": "http://10.1.1.5:8080",
    }
    base.update(overrides)
    return WorkerConfig(**base)  # type: ignore[arg-type]


class FakeStderr:
    """Async iterator over canned dnsmasq stderr lines."""

    def __init__(self, lines: list[bytes]) -> None:
        """Hold the lines to emit."""
        self._lines = list(lines)

    def __aiter__(self) -> "FakeStderr":
        """Iterate over self."""
        return self

    async def __anext__(self) -> bytes:
        """Yield the next line or stop."""
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class FakeProcess:
    """Stand-in for asyncio.subprocess.Process with scripted exit behaviour."""

    def __init__(self, returncode: int = 0, *, hang_until_killed: bool = False,
                 stderr: list[bytes] | None = None) -> None:
        """Configure exit code, whether terminate() is ignored, and stderr content."""
        self.pid = 4242
        self.returncode = returncode
        self.stderr = FakeStderr(stderr or [])
        self.hang_until_killed = hang_until_killed
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        """Record SIGTERM delivery."""
        self.terminated = True

    def kill(self) -> None:
        """Record SIGKILL delivery and let wait() return."""
        self.killed = True

    async def wait(self) -> int:
        """Return the exit code; when hanging and not killed, signal the wait_for timeout path."""
        if self.hang_until_killed and self.terminated and not self.killed:
            # Equivalent to wait_for() expiring on a process that ignores SIGTERM.
            raise asyncio.TimeoutError
        return self.returncode


def _install_subprocess(monkeypatch: pytest.MonkeyPatch, proc: FakeProcess | None,
                        calls: list[tuple[Any, ...]], error: Exception | None = None) -> None:
    """Replace create_subprocess_exec, recording argv and returning the fake process or raising."""

    async def fake_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        """Record the argv and either raise or return the scripted process."""
        calls.append(args)
        if error is not None:
            raise error
        assert proc is not None
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def test_generated_config_contains_range_gateway_dns_and_boot_options() -> None:
    """The dnsmasq config reflects interface, DHCP range, gateway, DNS list and PXE boot selection."""
    text = DHCPFullServer(_cfg())._generate_dnsmasq_config()

    assert "interface=eth1" in text
    assert "bind-interfaces" in text
    assert "dhcp-range=10.0.0.100,10.0.0.200,12h" in text
    assert "dhcp-option=3,10.0.0.1" in text
    assert "dhcp-option=6,8.8.8.8,1.1.1.1" in text
    assert "tftp-root=/srv/tftp" in text
    assert "dhcp-boot=tag:bios,undionly.kpxe" in text
    assert "dhcp-boot=tag:efi-x86_64,ipxe.efi" in text
    assert 'dhcp-option=tag:!bios,tag:!efi-x86_64,67,"http://10.1.1.5:8080/boot.ipxe"' in text
    assert "port=0" in text  # DNS disabled: DHCP/TFTP only


def test_config_matches_both_bios_and_efi_arch_codes() -> None:
    """Client arch 0 is tagged BIOS; arch 7 and 9 are tagged EFI x86_64."""
    text = DHCPFullServer(_cfg())._generate_dnsmasq_config()

    assert "dhcp-match=set:bios,option:client-arch,0" in text
    assert "dhcp-match=set:efi-x86_64,option:client-arch,7" in text
    assert "dhcp-match=set:efi-x86_64,option:client-arch,9" in text


@pytest.mark.asyncio
async def test_start_writes_config_and_launches_dnsmasq(monkeypatch: pytest.MonkeyPatch) -> None:
    """start() writes the rendered config to a temp file and execs dnsmasq pointing at it."""
    calls: list[tuple[Any, ...]] = []
    proc = FakeProcess(returncode=0)
    _install_subprocess(monkeypatch, proc, calls)
    server = DHCPFullServer(_cfg())

    await server.start()

    assert server.config_file is not None
    argv = calls[0]
    assert argv[0] == "dnsmasq"
    assert argv[1:3] == ("--conf-file", str(server.config_file))
    assert "--no-daemon" in argv
    assert server.running is True  # process exited, but running flag only drops on stop()

    written = server.config_file.read_text()
    assert written == server._generate_dnsmasq_config()
    await server.stop()


@pytest.mark.asyncio
async def test_unexpected_exit_is_logged_and_start_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """If dnsmasq exits on its own, start() returns (so the supervisor can react) rather than hanging."""
    calls: list[tuple[Any, ...]] = []
    proc = FakeProcess(returncode=2, stderr=[b"dnsmasq: failed to bind\n", b"   \n", b"\xff\n"])
    _install_subprocess(monkeypatch, proc, calls)
    server = DHCPFullServer(_cfg())

    await server.start()
    await asyncio.sleep(0)  # let the stderr monitor drain

    assert proc.returncode == 2
    assert server.running is True
    await server.stop()


@pytest.mark.asyncio
async def test_missing_dnsmasq_binary_is_reraised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing dnsmasq executable surfaces as FileNotFoundError to the caller."""
    _install_subprocess(monkeypatch, None, [], error=FileNotFoundError("dnsmasq"))
    server = DHCPFullServer(_cfg())

    with pytest.raises(FileNotFoundError):
        await server.start()


@pytest.mark.asyncio
async def test_other_spawn_error_is_reraised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any other spawn failure is logged and re-raised, never swallowed."""
    _install_subprocess(monkeypatch, None, [], error=PermissionError("denied"))
    server = DHCPFullServer(_cfg())

    with pytest.raises(PermissionError, match="denied"):
        await server.start()


@pytest.mark.asyncio
async def test_monitor_output_logs_nonblank_lines_and_tolerates_bad_bytes() -> None:
    """Stderr lines are decoded (invalid bytes dropped) and blank lines are skipped."""
    server = DHCPFullServer(_cfg())
    server.process = FakeProcess(stderr=[b"one\n", b"  \n", b"\xff\xfetwo\n"])  # type: ignore[assignment]

    await server._monitor_output()

    assert server.process is not None
    assert list(server.process.stderr._lines) == []  # fully drained


@pytest.mark.asyncio
async def test_monitor_output_without_process_is_noop() -> None:
    """With no process attached the monitor returns immediately."""
    server = DHCPFullServer(_cfg())

    await server._monitor_output()

    assert server.process is None


@pytest.mark.asyncio
async def test_monitor_output_stream_error_is_logged() -> None:
    """An exception while reading stderr ends the monitor instead of crashing the worker."""

    class BrokenStderr:
        """Stderr whose iteration fails immediately."""

        def __aiter__(self) -> "BrokenStderr":
            """Return self."""
            return self

        async def __anext__(self) -> bytes:
            """Fail on the first read."""
            raise OSError("pipe closed")

    server = DHCPFullServer(_cfg())
    proc = FakeProcess()
    proc.stderr = BrokenStderr()  # type: ignore[assignment]
    server.process = proc  # type: ignore[assignment]

    await server._monitor_output()  # must not raise


@pytest.mark.asyncio
async def test_stop_terminates_cleanly_and_removes_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() sends SIGTERM, waits for exit, and deletes the generated config file."""
    calls: list[tuple[Any, ...]] = []
    proc = FakeProcess(returncode=0)
    _install_subprocess(monkeypatch, proc, calls)
    server = DHCPFullServer(_cfg())
    await server.start()
    config_path = server.config_file
    assert config_path is not None and config_path.exists()

    await server.stop()

    assert proc.terminated is True
    assert proc.killed is False
    assert server.running is False
    assert not config_path.exists()


@pytest.mark.asyncio
async def test_stop_escalates_to_kill_when_terminate_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dnsmasq that ignores SIGTERM is SIGKILLed after the grace timeout and reaped."""
    calls: list[tuple[Any, ...]] = []
    proc = FakeProcess(returncode=-9, hang_until_killed=True)
    _install_subprocess(monkeypatch, proc, calls)
    server = DHCPFullServer(_cfg())
    await server.start()

    await server.stop()

    assert proc.terminated is True
    assert proc.killed is True
    assert server.config_file is not None and not server.config_file.exists()


@pytest.mark.asyncio
async def test_stop_swallows_unexpected_stop_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure during termination is logged and stop() still completes and cleans up."""
    calls: list[tuple[Any, ...]] = []
    proc = FakeProcess(returncode=0)

    def boom() -> None:
        """Simulate the process handle having gone away."""
        raise ProcessLookupError("no such process")

    proc.terminate = boom  # type: ignore[method-assign]
    _install_subprocess(monkeypatch, proc, calls)
    server = DHCPFullServer(_cfg())
    await server.start()
    config_path = server.config_file

    await server.stop()

    assert server.running is False
    assert config_path is not None and not config_path.exists()


@pytest.mark.asyncio
async def test_stop_without_process_only_cleans_config(tmp_path: Path) -> None:
    """stop() with no process just removes a leftover config file and does not fail."""
    server = DHCPFullServer(_cfg())
    leftover = tmp_path / "gough-dhcp-leftover.conf"
    leftover.write_text("x")
    server.config_file = leftover

    await server.stop()

    assert not leftover.exists()


@pytest.mark.asyncio
async def test_stop_reports_cleanup_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the config file cannot be unlinked, stop() logs a warning and completes."""
    server = DHCPFullServer(_cfg())
    leftover = tmp_path / "gough-dhcp-locked.conf"
    leftover.write_text("x")
    server.config_file = leftover

    def refuse(self: Path, missing_ok: bool = False) -> None:
        """Simulate an unlink failure such as a permission error."""
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", refuse)

    await server.stop()  # must not raise

    assert leftover.exists()
