"""Handler-level gate on the platform-wide iPXE / Elder write routes.

The route policy (``SCOPE_POLICY``) maps these writes to
``gough.cluster.superadmin``; the handlers' own decorator must agree, or a
superadmin token is refused by a stale ``admin_required`` (tenant-admin)
check and a tenant admin is let through whenever the policy middleware is
bypassed. Regression: security review of PR #52 (HIGH over-broad grant).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from quart import Quart, g

import app.api.ipxe as ipxe_mod
from app.security.scope_policy import ROLE_TO_SCOPE_BUNDLE

WRITES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("PUT", "/api/v1/ipxe/config", {"name": "lab"}),
    (
        "PUT",
        "/api/v1/ipxe/elder/config",
        {"elder_url": "https://93.184.216.34", "api_key": "k"},
    ),
    ("POST", "/api/v1/ipxe/boot-configs", {"name": "bc"}),
    ("PUT", "/api/v1/ipxe/boot-configs/1", {"name": "bc"}),
    ("DELETE", "/api/v1/ipxe/boot-configs/1", None),
    ("POST", "/api/v1/ipxe/images", {"name": "img"}),
    ("PUT", "/api/v1/ipxe/images/1", {"name": "img"}),
    ("DELETE", "/api/v1/ipxe/images/1", None),
]

#: Broadest tenant-level token: the admin bundle without the superadmin scope
#: (same model as tests/security/test_scope_policy_reviewed.py).
TENANT_ADMIN = {
    "id": 7,
    "role": "admin",
    "_jwt_payload": {
        "scope": " ".join(
            sorted(ROLE_TO_SCOPE_BUNDLE["admin"] - {"gough.cluster.superadmin"})
        )
    },
}
SUPERADMIN = {
    "id": 1,
    "role": "viewer",  # proves the gate is scope-based, not role-name based
    "_jwt_payload": {"scope": "gough.cluster.superadmin"},
}


def _client(principal: dict[str, Any]) -> Any:
    """iPXE blueprint with ``principal`` injected as the current user."""
    app = Quart(__name__)
    app.register_blueprint(ipxe_mod.ipxe_bp, url_prefix="/api/v1/ipxe")

    @app.before_request
    async def _inject() -> None:
        g.current_user = principal

    return app.test_client()


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
async def test_tenant_admin_refused_by_handler(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """A tenant admin (full admin bundle, role=admin) gets 403 and never touches the DB."""
    get_db = MagicMock()
    monkeypatch.setattr(ipxe_mod, "get_db", get_db)
    send = getattr(_client(TENANT_ADMIN), method.lower())
    resp = await send(path, json=body) if body is not None else await send(path)
    assert resp.status_code == 403
    assert (await resp.get_json())["required"] == ["gough.cluster.superadmin"]
    get_db.assert_not_called()


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
async def test_superadmin_passes_handler_gate(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """A superadmin-scoped token passes the handler gate (whatever the handler then returns)."""
    monkeypatch.setattr(ipxe_mod, "get_db", MagicMock)
    send = getattr(_client(SUPERADMIN), method.lower())
    resp = await send(path, json=body) if body is not None else await send(path)
    assert resp.status_code != 403
    payload = await resp.get_json(silent=True) or {}
    assert payload.get("error") not in {
        "Insufficient scopes",
        "Insufficient permissions",
    }
