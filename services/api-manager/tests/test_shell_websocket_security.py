"""Security tests for the hub shell WebSocket (no local shell, authn/authz, ticket)."""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

import pytest
from quart import Quart
from quart.testing.connections import WebsocketDisconnectError, WebsocketResponseError

import app.websocket as ws_mod
from app.shell_tickets import ShellTicketStore, TicketClaims, set_ticket_store
from app.websocket import MalformedMessage, init_websocket, parse_client_message

SID = "sess-1"
MODULE = pathlib.Path(ws_mod.__file__)


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    """Any pty/Popen/os.system during these tests is a hard failure."""
    import os
    import pty
    import subprocess

    def boom(*a, **k):
        raise AssertionError("local process/pty spawn attempted")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(pty, "openpty", boom)
    monkeypatch.setattr(os, "system", boom)


@pytest.fixture()
def store():
    """Fresh in-memory ticket store."""
    s = ShellTicketStore(None)
    set_ticket_store(s)
    yield s
    set_ticket_store(None)


@pytest.fixture()
def client(store, monkeypatch):
    """Quart app with /ws/shell; DB/flag/authz stubbed to allow by default."""

    async def _flag(*_a, **_k):
        return False

    async def _inline(fn):
        return fn()

    monkeypatch.setattr(ws_mod, "feature_enabled", _flag)
    monkeypatch.setattr(ws_mod, "run_db", _inline)
    monkeypatch.setattr(ws_mod, "get_audit_logger", lambda: None)
    sess = SimpleNamespace(user_id=1, ended_at=None, resource_type="vm", resource_id="x")
    state = {"session": sess, "deny": ""}

    def _authorize(session_id, user_id):
        if state["deny"]:
            return None, state["deny"]
        return state["session"], ""

    monkeypatch.setattr(ws_mod, "_authorize", _authorize)
    app = Quart(__name__)
    init_websocket(app)
    c = app.test_client()
    c.state = state
    return c


async def _ticket(store, user=1, sid=SID, tenant="t1"):
    return await store.mint(TicketClaims(user, sid, tenant))


async def _expect_reject(client, qs):
    with pytest.raises((WebsocketResponseError, WebsocketDisconnectError)):
        async with client.websocket(f"/ws/shell?{qs}") as ws:
            await ws.receive()


@pytest.mark.asyncio
async def test_no_ticket_rejected_before_accept(client):
    await _expect_reject(client, f"session_id={SID}")


@pytest.mark.asyncio
async def test_bogus_ticket_rejected(client):
    await _expect_reject(client, f"session_id={SID}&ticket=nope")


@pytest.mark.asyncio
async def test_ticket_for_other_session_rejected(client, store):
    t = await _ticket(store, sid="other")
    await _expect_reject(client, f"session_id={SID}&ticket={t}")


@pytest.mark.asyncio
async def test_wrong_users_session_denied(client, store):
    client.state["deny"] = "not session owner"
    t = await _ticket(store, user=2)
    await _expect_reject(client, f"session_id={SID}&ticket={t}")


@pytest.mark.asyncio
async def test_authorized_gets_4501_never_local_shell(client, store):
    t = await _ticket(store)
    async with client.websocket(f"/ws/shell?session_id={SID}&ticket={t}") as ws:
        assert "not yet available" in await ws.receive()


@pytest.mark.asyncio
async def test_replayed_ticket_denied(client, store):
    t = await _ticket(store)
    async with client.websocket(f"/ws/shell?session_id={SID}&ticket={t}") as ws:
        await ws.receive()
    await _expect_reject(client, f"session_id={SID}&ticket={t}")


@pytest.mark.asyncio
async def test_second_attach_denied(client, store):
    assert await store.try_attach(SID)
    t = await _ticket(store)
    await _expect_reject(client, f"session_id={SID}&ticket={t}")
    await store.release(SID)
    assert await store.try_attach(SID)


@pytest.mark.asyncio
async def test_kill_switch_denies(client, store, monkeypatch):
    async def _on(*_a, **_k):
        return True

    monkeypatch.setattr(ws_mod, "feature_enabled", _on)
    t = await _ticket(store)
    await _expect_reject(client, f"session_id={SID}&ticket={t}")


@pytest.mark.asyncio
async def test_ticket_expiry_and_hashing(store):
    t = await _ticket(store)
    assert t not in store._mem  # only the hash is stored
    key = next(iter(store._mem))
    store._mem[key] = (0.0, store._mem[key][1])
    assert await store.consume(t) is None


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"type":"exec"}',
        '{"type":"input","input":5}',
        '{"type":"resize","rows":0,"cols":80}',
        '{"type":"resize","rows":true,"cols":1}',
        "x" * 70000,
    ],
)
def test_malformed_messages_rejected(raw):
    with pytest.raises(MalformedMessage):
        parse_client_message(raw)


def test_valid_messages():
    assert parse_client_message('{"type":"input","input":"ls"}').data == "ls"
    m = parse_client_message('{"type":"resize","rows":30,"cols":100}')
    assert (m.rows, m.cols) == (30, 100)


def test_module_never_spawns_processes():
    src = MODULE.read_text()
    forbidden = (
        r"(import pty|import subprocess|from subprocess|Popen|os\.system"
        r"|os\.popen|openpty|forkpty|create_subprocess)"
    )
    assert not re.search(forbidden, src)
    assert "/bin/bash" not in src
