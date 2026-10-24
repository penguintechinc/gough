"""Option-injection, stub-path and host-cert resolution regressions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from quart import Quart

from app.safe_names import validate_rfc1123_name, validate_spiffe_id


@pytest.mark.parametrize("bad", ["--target=x", "-rf", "a b", "a/b", "A", "", "a;b", "x" * 300])
def test_rfc1123_rejects_injection(bad):
    """Flag-like / path-like / oversize names are rejected."""
    with pytest.raises(ValueError):
        validate_rfc1123_name(bad)


def test_rfc1123_accepts_valid():
    """A normal DNS label passes through unchanged."""
    assert validate_rfc1123_name("node-1.example") == "node-1.example"


@pytest.mark.parametrize("bad", ["-x", "spiffe://", "http://a/b", "spiffe://a b/c"])
def test_spiffe_rejects(bad):
    """Malformed SPIFFE IDs are rejected."""
    with pytest.raises(ValueError):
        validate_spiffe_id(bad)


def test_live_migrate_rejects_flag_names():
    """LXD live-migrate refuses option-like instance/member names."""
    from app.clients.lxd_extra import LXDConfigError, live_migrate

    with pytest.raises(LXDConfigError):
        live_migrate("--force", "node1", cluster_id="c")
    with pytest.raises(LXDConfigError):
        live_migrate("web-1", "../x", cluster_id="c")


def test_spire_register_rejects_bad_ids():
    """spire-server entry create never receives a flag-like SPIFFE ID."""
    from app.clients.spire import (
        SpireClient,
        SpireRegistrationEntry,
        SpireRegistrationFailed,
    )

    client = SpireClient.__new__(SpireClient)
    client.server_address = "h:8081"
    with pytest.raises(SpireRegistrationFailed):
        client.register_workload(
            SpireRegistrationEntry(spiffe_id="-spiffeID", parent_id="spiffe://a/b")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flag,code,status",
    [
        (False, "feature_disabled", 404),
        (True, "not_implemented_control_plane_replace", 501),
    ],
)
async def test_control_plane_ops_never_pretend(monkeypatch, flag, code, status):
    """Replace/force-recover return 404 (flag off) or 501, never success."""
    import app.api.primary as primary

    async def _flag(*_a, **_k):
        return flag

    monkeypatch.setattr(primary, "feature_enabled", _flag)
    app = Quart(__name__)
    async with app.app_context():
        resp, st = await primary._control_plane_gate()
        assert st == status
        assert (await resp.get_json())["error"]["code"] == code
    src = open(primary.__file__).read()
    assert "kubectl\", \"cordon" not in src and "etcdctl\", " not in src


def test_biomes_no_subprocess_shellout():
    """Upgrade path no longer shells out to kubectl/lxc."""
    src = open(__import__("app.api.biomes", fromlist=["x"]).__file__).read()
    assert "subprocess.run" not in src and '"rollout"' not in src


class _Rows(list):
    def first(self):
        return self[0] if self else None


class _Col:
    def __eq__(self, o):
        return _Cond()


class _Cond:
    def __and__(self, o):
        return _Cond()


def _install(monkeypatch, node, agents):
    import app.api.ssh_ca as m

    class FakeDB:
        nodes = SimpleNamespace(id=_Col())
        access_agents = SimpleNamespace(status=_Col(), node_id=_Col(), hostname=_Col())
        _n = 0

        def __call__(self, q):
            self._n += 1
            if self._n == 1:
                return SimpleNamespace(select=lambda: _Rows([node] if node else []))
            return SimpleNamespace(select=lambda: _Rows(agents))

    monkeypatch.setattr(m, "get_db", lambda: FakeDB())
    return m


def test_non_node_resource_denied(monkeypatch):
    """Only nodes may get host-scoped certs."""
    m = _install(monkeypatch, SimpleNamespace(tenant_id="t1"), [SimpleNamespace(agent_id="a")])
    with pytest.raises(m.AgentResolutionError) as e:
        m.resolve_agent_id("vm", "1", "t1")
    assert e.value.status == 403


def test_hostname_spoof_does_not_bind(monkeypatch):
    """A hostname-only match (no node link) never binds an agent."""
    m = _install(monkeypatch, None, [SimpleNamespace(agent_id="evil")])
    with pytest.raises(m.AgentResolutionError) as e:
        m.resolve_agent_id("node", "victim-host", "t1")
    assert e.value.status == 404


def test_cross_tenant_node_denied(monkeypatch):
    """Other tenant's node is denied; super-admin may cross."""
    m = _install(monkeypatch, SimpleNamespace(tenant_id="t2"), [SimpleNamespace(agent_id="a")])
    with pytest.raises(m.AgentResolutionError) as e:
        m.resolve_agent_id("node", "1", "t1")
    assert e.value.status == 404
    m = _install(monkeypatch, SimpleNamespace(tenant_id="t2"), [SimpleNamespace(agent_id="a")])
    assert m.resolve_agent_id("node", "1", "t1", True) == "a"


def test_two_agents_denied_409(monkeypatch):
    """Ambiguous node->agent link fails closed with 409."""
    m = _install(
        monkeypatch,
        SimpleNamespace(tenant_id="t1"),
        [SimpleNamespace(agent_id="a"), SimpleNamespace(agent_id="b")],
    )
    with pytest.raises(m.AgentResolutionError) as e:
        m.resolve_agent_id("node", "1", "t1")
    assert e.value.status == 409


def test_single_agent_ok(monkeypatch):
    """Exactly one linked active agent resolves."""
    m = _install(monkeypatch, SimpleNamespace(tenant_id="t1"), [SimpleNamespace(agent_id="a")])
    assert m.resolve_agent_id("node", "1", "t1") == "a"
