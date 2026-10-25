"""Behaviour tests for the agent entry point: CLI flags, startup and shutdown orchestration."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from agent import __version__
from agent import main as agent_main
from agent.config import AgentConfig
from agent.enrollment import EnrollmentError


class FakeHeartbeat:
    """Heartbeat double recording lifecycle and session-count calls."""

    instances: list["FakeHeartbeat"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.session_counts: list[int] = []
        FakeHeartbeat.instances.append(self)

    async def start(self) -> None:
        """Record that the heartbeat was started."""
        self.started = True

    async def stop(self) -> None:
        """Record that the heartbeat was stopped."""
        self.stopped = True

    def set_active_sessions(self, count: int) -> None:
        """Record the reported active session count."""
        self.session_counts.append(count)


class FakeRSSH:
    """RSSH server double; never binds a socket."""

    instances: list["FakeRSSH"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.active = 0
        FakeRSSH.instances.append(self)

    def start(self) -> None:
        """Record that the server was started."""
        self.started = True

    def stop(self) -> None:
        """Record that the server was stopped."""
        self.stopped = True

    def get_active_session_count(self) -> int:
        """Return the configured fake session count."""
        return self.active


@pytest.fixture(autouse=True)
def doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap network- and socket-bound collaborators for in-process doubles."""
    FakeHeartbeat.instances = []
    FakeRSSH.instances = []
    monkeypatch.setattr(agent_main, "HeartbeatService", FakeHeartbeat)
    monkeypatch.setattr(agent_main, "RSSHServer", FakeRSSH)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every on-disk path the agent touches into tmp_path via env."""
    for key in list(os.environ):
        if key.startswith("GOUGH_"):
            monkeypatch.delenv(key, raising=False)
    layout = {
        "token": tmp_path / "state" / "tokens.json",
        "ca": tmp_path / "state" / "ca.pub",
        "hostkey": tmp_path / "state" / "host_key",
        "log": tmp_path / "log" / "agent.log",
    }
    monkeypatch.setenv("GOUGH_TOKEN_FILE", str(layout["token"]))
    return layout


def make_agent(paths: dict[str, Path], **overrides: Any) -> agent_main.GoughAgent:
    """Build a GoughAgent whose on-disk paths all live under tmp_path."""
    values: dict[str, Any] = {
        "token_file": str(paths["token"]),
        "ca_public_key_file": str(paths["ca"]),
        "host_key_file": str(paths["hostkey"]),
        "log_file": str(paths["log"]),
        "heartbeat_interval": 1,
    }
    values.update(overrides)
    return agent_main.GoughAgent(AgentConfig(**values))


def write_enrolled_tokens(paths: dict[str, Path], ca_public_key: str | None) -> None:
    """Write a token file that makes the agent look already enrolled."""
    paths["token"].parent.mkdir(parents=True, exist_ok=True)
    paths["token"].write_text(
        json.dumps(
            {
                "agent_id": "agent-1",
                "access_token": "acc",
                "refresh_token": "ref",
                "ca_public_key": ca_public_key,
            }
        )
    )


def test_help_prints_usage_and_does_not_start_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--help prints usage and never runs the async entry point."""

    def _forbidden() -> None:
        raise AssertionError("agent must not start for --help")

    monkeypatch.setattr(agent_main, "main", _forbidden)
    monkeypatch.setattr("sys.argv", ["gough-agent", "--help"])
    agent_main.run()
    out = capsys.readouterr().out
    assert "usage: gough-agent" in out
    assert "GOUGH_" in out


def test_short_help_flag_is_equivalent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """-h behaves like --help."""
    monkeypatch.setattr(
        agent_main, "main", lambda: (_ for _ in ()).throw(AssertionError)
    )
    monkeypatch.setattr("sys.argv", ["gough-agent", "-h"])
    agent_main.run()
    assert "usage: gough-agent" in capsys.readouterr().out


def test_version_prints_package_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--version prints exactly the package version and exits without starting."""
    monkeypatch.setattr(
        agent_main, "main", lambda: (_ for _ in ()).throw(AssertionError)
    )
    monkeypatch.setattr("sys.argv", ["gough-agent", "--version"])
    agent_main.run()
    assert capsys.readouterr().out.strip() == __version__


def test_no_flags_runs_main_under_asyncio(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no CLI flags run() drives the async main() to completion."""
    ran: list[bool] = []

    async def fake_main() -> None:
        ran.append(True)

    monkeypatch.setattr(agent_main, "main", fake_main)
    monkeypatch.setattr("sys.argv", ["gough-agent"])
    agent_main.run()
    assert ran == [True]


async def test_main_exits_1_and_still_stops_when_start_fails(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """A startup failure exits with status 1 and the finally block still stops the agent."""
    stopped: list[bool] = []

    class BrokenAgent:
        def __init__(self, config: AgentConfig) -> None:
            self.config = config

        async def start(self) -> None:
            raise RuntimeError("bind failed")

        async def stop(self) -> None:
            stopped.append(True)

    monkeypatch.setattr(agent_main, "GoughAgent", BrokenAgent)
    with pytest.raises(SystemExit) as exc:
        await agent_main.main()
    assert exc.value.code == 1
    assert stopped == [True]


async def test_start_without_enrollment_key_exits_1(paths: dict[str, Path]) -> None:
    """An unenrolled agent with no enrollment key cannot enroll and exits 1."""
    agent = make_agent(paths, enrollment_key=None)
    with pytest.raises(SystemExit) as exc:
        await agent.start()
    assert exc.value.code == 1
    assert agent.heartbeat is None


async def test_start_enrolled_with_ca_wires_validator_rssh_and_heartbeat(
    paths: dict[str, Path],
) -> None:
    """With tokens and a CA key the agent builds validator, rssh server and heartbeat, then runs."""
    write_enrolled_tokens(paths, ca_public_key="ssh-ed25519 AAAAC3 ca")
    agent = make_agent(paths)
    task = asyncio.create_task(agent.start())
    for _ in range(200):
        if agent._running:
            break
        await asyncio.sleep(0.01)
    assert agent.cert_validator is not None
    assert agent.rssh_server is not None
    assert FakeRSSH.instances[0].started is True
    assert FakeHeartbeat.instances[0].started is True

    await agent.stop()
    await asyncio.wait_for(task, timeout=5)
    assert FakeRSSH.instances[0].stopped is True
    assert FakeHeartbeat.instances[0].stopped is True
    assert agent.cert_validator is not None
    assert agent.cert_validator._ca_key_file is None  # cleaned up on stop


async def test_start_without_ca_runs_heartbeat_only(paths: dict[str, Path]) -> None:
    """Without a CA public key no validator or rssh server is created; heartbeat still runs."""
    write_enrolled_tokens(paths, ca_public_key=None)
    agent = make_agent(paths)
    task = asyncio.create_task(agent.start())
    for _ in range(200):
        if agent._running:
            break
        await asyncio.sleep(0.01)
    assert agent.cert_validator is None
    assert agent.rssh_server is None
    assert FakeRSSH.instances == []
    assert FakeHeartbeat.instances[0].started is True
    await agent.stop()
    await asyncio.wait_for(task, timeout=5)


async def test_start_enrolls_when_not_enrolled(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """An unenrolled agent with a key calls enroll() and then proceeds."""
    calls: list[str] = []

    def fake_enroll(self: Any) -> None:
        calls.append("enroll")
        write_enrolled_tokens(paths, ca_public_key=None)
        self.config.agent_id = "agent-new"

    monkeypatch.setattr(agent_main.AgentEnrollment, "enroll", fake_enroll)
    agent = make_agent(paths, enrollment_key="ek-1")
    task = asyncio.create_task(agent.start())
    for _ in range(200):
        if agent._running:
            break
        await asyncio.sleep(0.01)
    assert calls == ["enroll"]
    await agent.stop()
    await asyncio.wait_for(task, timeout=5)


async def test_enrollment_error_during_start_exits_1(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """An EnrollmentError from enroll() is logged as critical and exits 1."""

    def failing_enroll(self: Any) -> None:
        raise EnrollmentError("key revoked")

    monkeypatch.setattr(agent_main.AgentEnrollment, "enroll", failing_enroll)
    agent = make_agent(paths, enrollment_key="ek-1")
    with pytest.raises(SystemExit) as exc:
        await agent.start()
    assert exc.value.code == 1


async def test_stop_before_start_is_safe(paths: dict[str, Path]) -> None:
    """Stopping an agent that never started touches no collaborators and does not raise."""
    agent = make_agent(paths)
    await agent.stop()
    assert agent._running is False


def test_shutdown_command_clears_running_flag(paths: dict[str, Path]) -> None:
    """A shutdown command from the server flips the running flag."""
    agent = make_agent(paths)
    agent._running = True
    agent._handle_server_command({"type": "shutdown"})
    assert agent._running is False


@pytest.mark.parametrize(
    "cmd",
    [{"type": "reload_config"}, {"type": "terminate_session", "session_id": "s1"}],
)
def test_known_commands_are_acknowledged_without_side_effects(
    paths: dict[str, Path], cmd: dict[str, Any]
) -> None:
    """reload_config and terminate_session are accepted and do not stop the agent."""
    agent = make_agent(paths)
    agent._running = True
    agent._handle_server_command(cmd)
    assert agent._running is True


def test_unknown_command_is_tolerated(paths: dict[str, Path]) -> None:
    """Unknown command types are logged and ignored."""
    agent = make_agent(paths)
    agent._running = True
    agent._handle_server_command({"type": "make-coffee"})
    assert agent._running is True


def test_session_callbacks_push_active_count_to_heartbeat(
    paths: dict[str, Path],
) -> None:
    """Session start and end both refresh the heartbeat's active-session count."""
    agent = make_agent(paths)
    agent.rssh_server = FakeRSSH()
    agent.heartbeat = FakeHeartbeat()
    agent.rssh_server.active = 2
    agent._on_session_start("s-1")
    agent.rssh_server.active = 1
    agent._on_session_end("s-1")
    assert agent.heartbeat.session_counts == [2, 1]


def test_session_callbacks_noop_without_heartbeat(paths: dict[str, Path]) -> None:
    """Session callbacks before startup do not dereference a missing heartbeat or rssh server."""
    agent = make_agent(paths)
    agent._on_session_start("s-1")
    agent._on_session_end("s-1")
    assert agent.heartbeat is None
