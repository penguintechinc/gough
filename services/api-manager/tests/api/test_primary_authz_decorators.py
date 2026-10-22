"""Authorization decorators guarding the primary (control-plane) endpoints.

``_scope_required`` must deny when the OIDC principal lacks any required scope,
fall back to the legacy ``g.current_user`` JWT payload, and fail closed when
neither identity is present. ``_mfa_required`` must deny unless MFA was verified
on the principal or the request. Each case is asserted through a real Quart
request so status codes and the error envelope are checked.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from quart import Quart, g

import app.api.primary as primary


def _app(route_decorators: list[Any]) -> Quart:
    """Quart app with one POST route wrapped by ``route_decorators`` (outermost first)."""
    app = Quart(__name__)

    async def _handler() -> Any:
        return {"ok": True}, 200

    fn: Any = _handler
    for deco in reversed(route_decorators):
        fn = deco(fn)
    app.add_url_rule("/guarded", "guarded", fn, methods=["POST"])
    return app


def _with_identity(principal: Any = None, user: Any = None) -> Any:
    """Return a before_request-style setter for g.principal / g.current_user."""

    def _set(app: Quart) -> None:
        @app.before_request
        async def _inject() -> None:
            if principal is not None:
                g.principal = principal
            if user is not None:
                g.current_user = user

    return _set


async def test_scope_denied_for_principal_missing_scope() -> None:
    """A principal lacking a required scope gets 403 forbidden_scope listing what is needed."""
    app = _app([primary._scope_required("gough.cluster.admin")])
    _with_identity(principal=SimpleNamespace(scopes=frozenset({"gough.cluster.read"})))(app)

    resp = await app.test_client().post("/guarded")

    assert resp.status_code == 403
    body = await resp.get_json()
    assert body["error"]["code"] == "forbidden_scope"
    assert body["error"]["details"] == {"required": ["gough.cluster.admin"]}


async def test_scope_allowed_for_principal_with_all_scopes() -> None:
    """A principal holding every required scope reaches the handler."""
    app = _app([primary._scope_required("a:read", "a:write")])
    _with_identity(principal=SimpleNamespace(scopes=frozenset({"a:read", "a:write", "x"})))(app)

    resp = await app.test_client().post("/guarded")

    assert resp.status_code == 200
    assert (await resp.get_json()) == {"ok": True}


async def test_scope_falls_back_to_legacy_jwt_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a principal, scopes are read from the current_user JWT payload."""
    import app.security.scope_enforcement as se

    monkeypatch.setattr(se, "extract_scopes_from_jwt", lambda payload: frozenset(payload["scope"].split()))
    app = _app([primary._scope_required("gough.cluster.read")])
    _with_identity(user={"id": 1, "_jwt_payload": {"scope": "gough.cluster.read other"}})(app)

    resp = await app.test_client().post("/guarded")

    assert resp.status_code == 200


async def test_scope_legacy_user_missing_scope_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy identity lacking the scope is denied with 403."""
    import app.security.scope_enforcement as se

    monkeypatch.setattr(se, "extract_scopes_from_jwt", lambda payload: frozenset())
    app = _app([primary._scope_required("gough.cluster.read")])
    _with_identity(user={"id": 1})(app)

    resp = await app.test_client().post("/guarded")

    assert resp.status_code == 403


async def test_scope_no_identity_fails_closed() -> None:
    """No principal and no current_user -> 403, never the handler."""
    app = _app([primary._scope_required("gough.cluster.read")])

    resp = await app.test_client().post("/guarded")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"]["code"] == "forbidden_scope"


async def test_mfa_denied_without_verification() -> None:
    """A principal without mfa_verified is refused with forbidden_mfa_required."""
    app = _app([primary._mfa_required])
    _with_identity(principal=SimpleNamespace(mfa_verified=False))(app)

    resp = await app.test_client().post("/guarded")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"]["code"] == "forbidden_mfa_required"


async def test_mfa_allowed_when_principal_verified() -> None:
    """A principal with mfa_verified=True reaches the handler."""
    app = _app([primary._mfa_required])
    _with_identity(principal=SimpleNamespace(mfa_verified=True))(app)

    resp = await app.test_client().post("/guarded")

    assert resp.status_code == 200


async def test_mfa_allowed_via_request_flag_when_principal_unverified() -> None:
    """A request-level mfa_verified flag satisfies the check even if the principal lacks it."""
    app = _app([primary._mfa_required])

    @app.before_request
    async def _flag() -> None:
        g.principal = SimpleNamespace(mfa_verified=False)
        g.mfa_verified = True

    resp = await app.test_client().post("/guarded")

    assert resp.status_code == 200


def test_grpc_target_brackets_only_ipv6() -> None:
    """IPv4 hosts keep the bare host:port form; IPv6 literals are bracketed."""
    assert primary._grpc_target("10.0.0.5") == "10.0.0.5:50051"
    assert primary._grpc_target("fd00::5") == "[fd00::5]:50051"
    assert primary._grpc_target("fd00::5", port=6443) == "[fd00::5]:6443"
