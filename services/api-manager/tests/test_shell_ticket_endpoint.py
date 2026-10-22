"""Tests for POST /api/v1/shell/sessions/<id>/ticket."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from quart import Quart, g

import app.api.shell as shell
import app.middleware as mw
from app.shell_tickets import ShellTicketStore, set_ticket_store


@pytest.fixture()
def ctx(monkeypatch):
    """Quart app with shell blueprint; DB/authz stubbed via ``state``."""
    state = {"sess": SimpleNamespace(user_id=1, ended_at=None, resource_type="vm",
                                     resource_id="r"),
             "admin": False, "access": True}
    store = ShellTicketStore(None)
    set_ticket_store(store)

    async def _inline(fn):
        return fn()

    class _DB:
        shell_sessions = SimpleNamespace(session_id=1)

        def __call__(self, *_a, **_k):
            return SimpleNamespace(select=lambda: SimpleNamespace(first=lambda: state["sess"]))

    monkeypatch.setattr(shell, "run_db", _inline)
    monkeypatch.setattr(shell, "get_db", lambda: _DB())
    monkeypatch.setattr(shell, "get_current_user", lambda: {"id": 1})
    monkeypatch.setattr(mw, "get_current_user", lambda: {"id": 1})
    monkeypatch.setattr(shell, "user_is_admin", lambda uid: state["admin"])
    monkeypatch.setattr(shell, "check_shell_access",
                        lambda *a: (state["access"], None if state["access"] else "no"))
    monkeypatch.setattr(shell, "get_audit_logger", lambda: None)
    app = Quart(__name__)
    app.register_blueprint(shell.shell_bp)

    @app.before_request
    async def _t():
        g.tenant_context = SimpleNamespace(tenant_id="t1")

    yield app.test_client(), state, store
    set_ticket_store(None)


@pytest.mark.asyncio
async def test_owner_gets_ticket_bound_to_user_session_tenant(ctx):
    c, state, store = ctx
    r = await c.post("/api/v1/shell/sessions/s1/ticket")
    assert r.status_code == 200
    body = await r.get_json()
    assert set(body) == {"ticket", "expires_in", "websocket_url"}
    claims = await store.consume(body["ticket"])
    assert (claims.user_id, claims.session_id, claims.tenant) == (1, "s1", "t1")
    assert await store.consume(body["ticket"]) is None


@pytest.mark.asyncio
async def test_non_owner_non_admin_denied(ctx):
    c, state, _ = ctx
    state["sess"] = SimpleNamespace(user_id=2, ended_at=None, resource_type="vm",
                                    resource_id="r")
    assert (await c.post("/api/v1/shell/sessions/s1/ticket")).status_code == 403
    state["admin"] = True
    assert (await c.post("/api/v1/shell/sessions/s1/ticket")).status_code == 200


@pytest.mark.asyncio
async def test_no_resource_access_denied(ctx):
    c, state, _ = ctx
    state["access"] = False
    assert (await c.post("/api/v1/shell/sessions/s1/ticket")).status_code == 403


@pytest.mark.asyncio
async def test_missing_and_ended_session(ctx):
    c, state, _ = ctx
    state["sess"] = SimpleNamespace(user_id=1, ended_at=1, resource_type="vm", resource_id="r")
    assert (await c.post("/api/v1/shell/sessions/s1/ticket")).status_code == 409
    state["sess"] = None
    assert (await c.post("/api/v1/shell/sessions/s1/ticket")).status_code == 404
