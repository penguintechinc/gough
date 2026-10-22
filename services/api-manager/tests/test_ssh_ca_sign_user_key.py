"""Real ssh-keygen tests for SSHCertificateAuthority.sign_user_key + /sign policy."""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

import pytest
from quart import Quart

import app.ssh_ca as ssh_ca_mod
from app.ssh_ca import SSHCertificateAuthority, validate_principal_syntax

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """CA key + user pubkey on disk; run_db inlined; returns (ca, pubkey, app)."""
    ca_key = tmp_path / "ca"
    user_key = tmp_path / "user"
    _keygen(ca_key)
    _keygen(user_key)

    async def _inline(fn):
        return fn()

    monkeypatch.setattr(ssh_ca_mod, "run_db", _inline)
    ca = SSHCertificateAuthority()
    monkeypatch.setattr(ca, "_get_private_key_path", lambda: ca_key)
    return ca, (tmp_path / "user.pub").read_text(), Quart(__name__)


def _describe(cert: str, tmp_path) -> str:
    f = tmp_path / "c-cert.pub"
    f.write_text(cert + "\n")
    return subprocess.run(
        ["ssh-keygen", "-L", "-f", str(f)], capture_output=True, text=True, check=True
    ).stdout


@pytest.mark.asyncio
async def test_signed_cert_exact_principals_options_validity(env, tmp_path):
    ca, pub, app = env
    async with app.app_context():
        cert = await ca.sign_user_key(
            pub, ["ubuntu"], 300, "u@r-1", host_ids=["agent-1"],
            source_address="10.0.0.5", force_command="/bin/true",
        )
    out = _describe(cert, tmp_path)
    assert "Key ID: \"u@r-1\"" in out
    principals = out.split("Principals:")[1].split("Critical Options:")[0].split()
    assert principals == ["ubuntu", "host:agent-1"]
    assert "source-address 10.0.0.5" in out and "force-command /bin/true" in out
    assert "allow-root" not in principals
    valid = out.split("Valid:")[1].splitlines()[0]
    assert "from" in valid and "to" in valid


@pytest.mark.asyncio
async def test_allow_root_only_via_keyword(env, tmp_path):
    ca, pub, app = env
    async with app.app_context():
        cert = await ca.sign_user_key(
            pub, ["root"], 300, "k", host_ids=["a"], allow_root=True
        )
    out = _describe(cert, tmp_path)
    assert out.split("Principals:")[1].split("Critical")[0].split() == [
        "root", "host:a", "allow-root"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["host:evil", "allow-root", "a,b", "a b", "a\nb", "a\x00", "", "../x", "x" * 80],
)
async def test_requested_reserved_or_injected_principals_rejected(env, bad):
    ca, pub, app = env
    async with app.app_context():
        with pytest.raises(ValueError):
            await ca.sign_user_key(pub, ["ubuntu", bad], 300, "k", host_ids=["a"])


@pytest.mark.asyncio
async def test_validity_cap(env):
    ca, pub, app = env
    async with app.app_context():
        with pytest.raises(ValueError, match="exceeds maximum"):
            await ca.sign_user_key(pub, ["ubuntu"], 10**6, "k", host_ids=["a"])


def test_validate_principal_syntax_accepts_logins():
    for ok in ("ubuntu", "deploy.user", "a-b_c", "u@d"):
        validate_principal_syntax(ok)


@pytest.fixture()
def sign_client(monkeypatch):
    """Quart app with the /sign route and every lookup stubbed."""
    import app.api.ssh_ca as api

    state = {"admin": False, "scopes": [], "allowed": ["ubuntu"], "agent": "agent-7"}
    calls: list[dict] = []

    async def _inline(fn):
        return fn()

    class FakeCA:
        async def sign_user_key(self, **kw):
            calls.append(kw)
            return "CERT"

    monkeypatch.setattr(api, "run_db", _inline)
    monkeypatch.setattr(api, "check_shell_access", lambda *a: True)
    def _resolve(*_a):
        if state["agent"] is None:
            raise api.AgentResolutionError(404, "No active access agent for this resource")
        return state["agent"]

    monkeypatch.setattr(api, "resolve_agent_id", _resolve)
    monkeypatch.setattr(api, "allowed_login_principals", lambda *a: state["allowed"])
    monkeypatch.setattr(api, "user_is_admin", lambda uid: state["admin"])
    monkeypatch.setattr(api, "get_audit_logger", lambda: None)
    monkeypatch.setattr(api, "SSHCertificateAuthority", FakeCA)
    class _DB:
        ssh_ca_config = SimpleNamespace(id=1)

        def __call__(self, *_a, **_k):
            return SimpleNamespace(select=lambda: SimpleNamespace(first=lambda: 1))

    monkeypatch.setattr(api, "get_db", lambda: _DB())
    monkeypatch.setattr(api, "get_current_user", lambda: {
        "id": 1, "email": "u@x", "_jwt_payload": {"scope": state["scopes"]}})
    import app.middleware as mw

    monkeypatch.setattr(mw, "get_current_user", lambda: {"id": 1})
    app = Quart(__name__)
    app.register_blueprint(api.ssh_ca_bp)
    return app.test_client(), state, calls


def _body(**kw):
    base = {"public_key": "k", "resource_type": "vm", "resource_id": "r",
            "principals": ["ubuntu"], "validity_seconds": 99999}
    base.update(kw)
    return base


async def _post(c, body):
    return await c.post("/api/v1/ssh-ca/sign", json=body)


@pytest.mark.asyncio
async def test_api_host_principal_comes_from_authorized_agent(sign_client):
    c, state, calls = sign_client
    r = await _post(c, _body())
    assert r.status_code == 200, await r.get_data()
    assert calls[0]["host_ids"] == ["agent-7"]
    assert calls[0]["validity_seconds"] <= 900
    assert calls[0]["allow_root"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["host:agent-9", "allow-root", "a,b", "a b", "a\nb"])
async def test_api_rejects_reserved_and_injection(sign_client, bad):
    c, state, calls = sign_client
    state["allowed"] = ["ubuntu", bad]
    r = await _post(c, _body(principals=["ubuntu", bad]))
    assert r.status_code == 400 and not calls


@pytest.mark.asyncio
async def test_api_principal_outside_allowlist_denied(sign_client):
    c, state, calls = sign_client
    r = await _post(c, _body(principals=["mallory"]))
    assert r.status_code == 403 and not calls


@pytest.mark.asyncio
async def test_api_non_admin_cannot_get_root(sign_client):
    c, state, calls = sign_client
    state["allowed"] = ["root"]
    state["scopes"] = ["gough.shell.root"]
    r = await _post(c, _body(principals=["root"]))
    assert r.status_code == 403 and not calls


@pytest.mark.asyncio
async def test_api_admin_without_root_scope_denied_with_scope_allowed(sign_client):
    c, state, calls = sign_client
    state.update(admin=True, allowed=["root"])
    assert (await _post(c, _body(principals=["root"]))).status_code == 403
    state["scopes"] = ["gough.shell.root"]
    assert (await _post(c, _body(principals=["root"]))).status_code == 200
    assert calls[-1]["allow_root"] is True


@pytest.mark.asyncio
async def test_api_no_agent_fails_closed(sign_client):
    c, state, calls = sign_client
    state["agent"] = None
    r = await _post(c, _body())
    assert r.status_code == 404 and not calls
