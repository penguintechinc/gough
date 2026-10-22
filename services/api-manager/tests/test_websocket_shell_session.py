"""Behavioural tests for the ``/ws/shell`` WebSocket handler in app/websocket.py.

Drives the real Quart WebSocket route through Quart's test client. The DB
handle and the PTY manager are replaced with in-memory fakes so each test
asserts the close codes, emitted frames, DB side effects and audit calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from quart import Quart
from quart.testing.connections import WebsocketDisconnectError

import app.websocket as ws_mod


class _FakeQuery:
    """Query stub returning the single session row (or None) it was built with."""

    def __init__(self, db: "_FakeDB") -> None:
        self._db = db

    def select(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
        return self

    def first(self) -> Any:
        return self._db.row

    def update(self, **values: Any) -> None:
        self._db.updates.append(values)
        for key, value in values.items():
            setattr(self._db.row, key, value)


class _FakeDB:
    """In-memory penguin-dal stand-in exposing ``shell_sessions`` rows."""

    def __init__(self, row: Any) -> None:
        self.row = row
        self.updates: list[dict[str, Any]] = []
        self.commits = 0
        self.shell_sessions = SimpleNamespace(session_id="session_id")

    def __call__(self, *_args: Any, **_kwargs: Any) -> _FakeQuery:
        return _FakeQuery(self)

    def commit(self) -> None:
        self.commits += 1


class _FakePty:
    """Replaces ShellSessionManager, recording every call the handler makes."""

    instances: list["_FakePty"] = []

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.started: dict[str, Any] | None = None
        self.inputs: list[str] = []
        self.resizes: list[tuple[Any, Any]] = []
        self.cleaned = False
        _FakePty.instances.append(self)

    async def start(self, command: str, rows: int, cols: int) -> None:
        self.started = {"command": command, "rows": rows, "cols": cols}

    async def write_input(self, data: str) -> None:
        self.inputs.append(data)

    async def resize(self, rows: Any, cols: Any) -> None:
        self.resizes.append((rows, cols))

    async def cleanup(self) -> None:
        self.cleaned = True


@pytest.fixture()
def shell_app(monkeypatch: pytest.MonkeyPatch) -> Quart:
    """Quart app with the shell websocket mounted and PTY manager faked."""
    _FakePty.instances.clear()
    app = Quart(__name__)
    ws_mod.init_websocket(app)
    monkeypatch.setattr(ws_mod, "ShellSessionManager", _FakePty)
    monkeypatch.setattr(ws_mod, "_get_shell_command", lambda _t: "/bin/sh")
    monkeypatch.setattr(ws_mod, "get_audit_logger", lambda: None)
    return app


def _install_db(monkeypatch: pytest.MonkeyPatch, row: Any) -> _FakeDB:
    """Patch get_db to return a fake DB seeded with ``row``."""
    db = _FakeDB(row)
    monkeypatch.setattr(ws_mod, "get_db", lambda: db)
    return db


def _session(**overrides: Any) -> SimpleNamespace:
    """Return a live (not ended) shell session row with optional overrides."""
    base: dict[str, Any] = {
        "session_id": "sess-1",
        "session_type": "bash",
        "started_at": datetime.utcnow() - timedelta(seconds=30),
        "ended_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_missing_session_id_is_closed_1008(shell_app: Quart, monkeypatch: pytest.MonkeyPatch) -> None:
    """Connecting without session_id is rejected with policy-violation close 1008."""
    _install_db(monkeypatch, None)

    async with shell_app.test_client().websocket("/ws/shell") as conn:
        with pytest.raises(WebsocketDisconnectError) as exc_info:
            await conn.receive()

    assert exc_info.value.args[0] == 1008
    assert _FakePty.instances == []


async def test_unknown_session_is_closed_1008_without_starting_pty(
    shell_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session_id that does not exist closes 1008 and never spawns a PTY."""
    _install_db(monkeypatch, None)

    async with shell_app.test_client().websocket("/ws/shell?session_id=ghost") as conn:
        with pytest.raises(WebsocketDisconnectError) as exc_info:
            await conn.receive()

    assert exc_info.value.args[0] == 1008

    assert _FakePty.instances == []


async def test_already_ended_session_reports_error_then_closes(
    shell_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ended session sends an error frame and closes 1000; no PTY is started."""
    _install_db(monkeypatch, _session(ended_at=datetime.utcnow()))

    async with shell_app.test_client().websocket("/ws/shell?session_id=sess-1") as conn:
        frame = json.loads(await conn.receive())

    assert frame == {"type": "error", "message": "Session already terminated"}
    assert _FakePty.instances == []


async def test_live_session_starts_pty_with_requested_geometry(
    shell_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live session starts the PTY with the requested rows/cols and announces readiness."""
    _install_db(monkeypatch, _session())

    async with shell_app.test_client().websocket(
        "/ws/shell?session_id=sess-1&rows=50&cols=132"
    ) as conn:
        ready = json.loads(await conn.receive())
        await conn.close(1000)

    assert ready == {"type": "connected", "session_id": "sess-1", "message": "Shell ready"}
    (pty,) = _FakePty.instances
    assert pty.started == {"command": "/bin/sh", "rows": 50, "cols": 132}


async def test_input_and_resize_messages_are_forwarded_and_session_finalised(
    shell_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input/resize frames reach the PTY; disconnect cleans up and stamps ended_at once."""
    db = _install_db(monkeypatch, _session())
    audit = MagicMock()
    monkeypatch.setattr(ws_mod, "get_audit_logger", lambda: audit)

    async with shell_app.test_client().websocket("/ws/shell?session_id=sess-1") as conn:
        await conn.receive()  # connected frame
        await conn.send(json.dumps({"type": "input", "input": "ls\n"}))
        await conn.send(json.dumps({"type": "resize", "rows": 40, "cols": 100}))
        await conn.send(json.dumps({"type": "unknown"}))  # ignored, must not raise

    (pty,) = _FakePty.instances
    assert pty.inputs == ["ls\n"]
    assert pty.resizes == [(40, 100)]
    assert pty.cleaned is True
    assert len(db.updates) == 1 and db.updates[0]["ended_at"] is not None
    assert db.commits == 1
    audit.log_shell_session_terminate.assert_called_once()
    kwargs = audit.log_shell_session_terminate.call_args.kwargs
    assert kwargs["session_id"] == "sess-1"
    assert kwargs["reason"] == "client_disconnect"
    assert isinstance(kwargs["duration_seconds"], int) and kwargs["duration_seconds"] >= 30


async def test_bad_geometry_reports_start_failure(
    shell_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-integer rows/cols fail PTY startup: an error frame is sent and the socket closed 1011."""
    _install_db(monkeypatch, _session())

    async with shell_app.test_client().websocket(
        "/ws/shell?session_id=sess-1&rows=tall"
    ) as conn:
        frame = json.loads(await conn.receive())

    assert frame["type"] == "error"
    assert frame["message"].startswith("Failed to start shell:")
    assert _FakePty.instances == []
