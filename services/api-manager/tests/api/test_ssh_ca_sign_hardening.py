"""POST /api/v1/ssh-ca/sign hardening (security review of 385dd0f).

Covers the validity cap, the principal allowlist (root always refused, empty
allowlist refuses everything), the resource-type allowlist, and host binding
(node must be in the caller's tenant; issued principals are node-scoped).
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from quart import Quart, g


def _passthrough(*dargs: Any, **dkwargs: Any) -> Any:
    """Stand-in for @auth_required / @roles_required(...) decorators."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


@pytest.fixture
def sign_app(monkeypatch: pytest.MonkeyPatch) -> tuple[Quart, Any, dict[str, Any]]:
    """SSH CA blueprint with auth stubbed, a tenant-``acme`` caller and spies."""
    import app.middleware as mw_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough)
    import app.api.ssh_ca as ssh_ca_mod

    ssh_ca_mod = importlib.reload(ssh_ca_mod)

    app = Quart(__name__)
    app.register_blueprint(ssh_ca_mod.ssh_ca_bp, url_prefix="/api/v1/ssh-ca")

    @app.before_request
    async def _identity() -> None:
        g.current_user = {"id": 1, "_jwt_payload": {"sub": "1", "tenant": "acme"}}
        g.tenant_context = SimpleNamespace(tenant_id="acme", cross_tenant=False)

    spies: dict[str, Any] = {
        "access_calls": [],
        "node_tenants": {7: "acme", 8: "other"},
    }

    def _access(user_id: int, rtype: str, rid: int) -> bool:
        spies["access_calls"].append((user_id, rtype, rid))
        return True

    ca = MagicMock()
    ca.sign_public_key.return_value = ("ssh-rsa-cert-v01@openssh.com AAAA", "kid-1")
    spies["ca"] = ca
    db = MagicMock()
    db.ssh_ca_config.id = 1  # handler evaluates ``id > 0``
    db.return_value.select.return_value.first.return_value = (
        MagicMock()
    )  # CA configured

    monkeypatch.setattr(ssh_ca_mod, "get_current_user", lambda: g.current_user)
    monkeypatch.setattr(ssh_ca_mod, "get_audit_logger", lambda: None)
    monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db)
    monkeypatch.setattr(ssh_ca_mod, "check_shell_access", _access)
    monkeypatch.setattr(ssh_ca_mod, "SSHCertificateAuthority", lambda *a, **k: ca)
    monkeypatch.setattr(
        ssh_ca_mod, "_load_node_tenant", lambda nid: spies["node_tenants"].get(nid)
    )
    monkeypatch.setenv(ssh_ca_mod.SSH_PRINCIPAL_ALLOWLIST_ENV, "ubuntu,deploy")
    return app, ssh_ca_mod, spies


def _body(**overrides: Any) -> dict[str, Any]:
    """A valid sign request, with per-test overrides."""
    body: dict[str, Any] = {
        "public_key": "ssh-ed25519 AAAAC3Nza test",
        "resource_type": "nodes",
        "resource_id": "7",
        "principals": ["ubuntu"],
        "validity_seconds": 3600,
    }
    body.update(overrides)
    return body


async def _post(app: Quart, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST the sign request and return (status, json)."""
    resp = await app.test_client().post("/api/v1/ssh-ca/sign", json=body)
    return resp.status_code, (await resp.get_json()) or {}


class TestValidityCap:
    """Certificates never outlive the 3600 s ceiling."""

    async def test_max_validity_accepted(
        self, sign_app: tuple[Quart, Any, dict[str, Any]]
    ) -> None:
        """Exactly the cap is allowed."""
        app, _, spies = sign_app
        status, _ = await _post(app, _body(validity_seconds=3600))
        assert status == 200
        assert spies["ca"].sign_public_key.call_args.kwargs["valid_seconds"] == 3600

    @pytest.mark.parametrize("validity", [3601, 28800, 10**9])
    async def test_above_cap_rejected(
        self, sign_app: tuple[Quart, Any, dict[str, Any]], validity: int
    ) -> None:
        """Anything above the cap is a 400 and nothing is signed."""
        app, _, spies = sign_app
        status, body = await _post(app, _body(validity_seconds=validity))
        assert status == 400
        assert "3600" in body["error"]
        spies["ca"].sign_public_key.assert_not_called()

    @pytest.mark.parametrize("validity", [0, -1, True, "60", 1.5, None])
    async def test_non_positive_or_non_int_rejected(
        self, sign_app: tuple[Quart, Any, dict[str, Any]], validity: Any
    ) -> None:
        """Zero, negatives, bools, strings, floats and null are rejected."""
        app, _, spies = sign_app
        status, _ = await _post(app, _body(validity_seconds=validity))
        assert status == 400
        spies["ca"].sign_public_key.assert_not_called()


class TestPrincipals:
    """Only allowlisted, well-formed, non-root principals are signed."""

    @pytest.mark.parametrize("principals", [["root"], ["ubuntu", "root"]])
    async def test_root_always_rejected(
        self,
        sign_app: tuple[Quart, Any, dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        principals: list[str],
    ) -> None:
        """root is refused even when an operator puts it in the allowlist."""
        app, mod, spies = sign_app
        monkeypatch.setenv(mod.SSH_PRINCIPAL_ALLOWLIST_ENV, "root,ubuntu")
        status, body = await _post(app, _body(principals=principals))
        assert status == 400
        assert "root" in body["error"]
        spies["ca"].sign_public_key.assert_not_called()

    async def test_unlisted_principal_rejected(
        self, sign_app: tuple[Quart, Any, dict[str, Any]]
    ) -> None:
        """A principal outside the allowlist is refused."""
        app, _, spies = sign_app
        status, body = await _post(app, _body(principals=["admin"]))
        assert status == 400
        assert "allowlist" in body["error"]
        spies["ca"].sign_public_key.assert_not_called()

    async def test_empty_allowlist_rejects_everything(
        self,
        sign_app: tuple[Quart, Any, dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default (unset) allowlist fails closed."""
        app, mod, spies = sign_app
        monkeypatch.delenv(mod.SSH_PRINCIPAL_ALLOWLIST_ENV, raising=False)
        status, _ = await _post(app, _body(principals=["ubuntu"]))
        assert status == 400
        spies["ca"].sign_public_key.assert_not_called()

    @pytest.mark.parametrize(
        "principals", [["ubuntu,root"], ["Ubuntu"], [""], [7], "ubuntu", []]
    )
    async def test_malformed_principals_rejected(
        self, sign_app: tuple[Quart, Any, dict[str, Any]], principals: Any
    ) -> None:
        """Separator injection, bad case, empties, non-strings and non-lists are refused."""
        app, _, spies = sign_app
        status, _ = await _post(app, _body(principals=principals))
        assert status == 400
        spies["ca"].sign_public_key.assert_not_called()


class TestHostBinding:
    """The certificate is bound to one node the caller's tenant owns."""

    async def test_success_issues_node_bound_principals(
        self, sign_app: tuple[Quart, Any, dict[str, Any]]
    ) -> None:
        """Issued principals carry the node id; the access check sees the int id."""
        app, _, spies = sign_app
        status, body = await _post(app, _body(principals=["ubuntu", "deploy"]))
        assert status == 200
        assert body["principals"] == ["ubuntu@node-7", "deploy@node-7"]
        assert spies["ca"].sign_public_key.call_args.kwargs["principals"] == [
            "ubuntu@node-7",
            "deploy@node-7",
        ]
        assert spies["access_calls"] == [(1, "nodes", 7)]

    async def test_other_tenant_node_is_404(
        self, sign_app: tuple[Quart, Any, dict[str, Any]]
    ) -> None:
        """A node in another tenant is reported missing and never access-checked."""
        app, _, spies = sign_app
        status, _ = await _post(app, _body(resource_id="8"))
        assert status == 404
        assert spies["access_calls"] == []
        spies["ca"].sign_public_key.assert_not_called()

    async def test_missing_node_is_404(
        self, sign_app: tuple[Quart, Any, dict[str, Any]]
    ) -> None:
        """A node id that does not exist is a 404."""
        app, _, _ = sign_app
        status, _ = await _post(app, _body(resource_id="99"))
        assert status == 404

    @pytest.mark.parametrize(
        "rtype", ["enrollment_keys", "resource_teams", "vm", "auth_user"]
    )
    async def test_non_node_resource_types_rejected(
        self, sign_app: tuple[Quart, Any, dict[str, Any]], rtype: str
    ) -> None:
        """Arbitrary table names can no longer be used to satisfy the access check."""
        app, _, spies = sign_app
        status, _ = await _post(app, _body(resource_type=rtype))
        assert status == 400
        assert spies["access_calls"] == []

    @pytest.mark.parametrize("rid", ["0", "-1", "7; drop", "abc", "1" * 19])
    async def test_malformed_resource_id_rejected(
        self, sign_app: tuple[Quart, Any, dict[str, Any]], rid: str
    ) -> None:
        """Only a positive integer node id is accepted."""
        app, _, _ = sign_app
        status, _ = await _post(app, _body(resource_id=rid))
        assert status == 400

    async def test_access_denied_is_refused(
        self,
        sign_app: tuple[Quart, Any, dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An in-tenant node the caller has no shell permission on is refused."""
        app, mod, spies = sign_app
        monkeypatch.setattr(mod, "check_shell_access", lambda *a: False)
        status, _ = await _post(app, _body())
        assert status == 400
        spies["ca"].sign_public_key.assert_not_called()
