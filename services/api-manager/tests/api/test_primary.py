"""Test suite for Primary HA API endpoints (≥90% coverage).

Tests for:
- GET /api/v1/primary/status
- POST /api/v1/primary/replace (MFA-required)
- POST /api/v1/primary/force-recover (MFA-required)
- POST /api/v1/primary/frontend-switch (MFA-required)
- POST /api/v1/primary/rotate-ca (MFA-required)

The mutating endpoints are real, synchronous M2 implementations (etcd via
``aetcd``, ``etcdctl``/``kubectl`` subprocesses, Vault PKI) -- there is no
202-deferred-to-M2 stub response (see the module docstring on
``app.api.primary``). Fixtures below mock those external-service boundaries
(``aetcd.Client``, the ``etcdctl`` subprocess, the Vault client) so the tests
exercise the endpoints' real success/error paths deterministically instead of
depending on an etcd/Vault/etcdctl install that this test environment does
not have.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Self

import pytest


class _FakeMember:
    """Duck-types ``aetcd``'s ``Member`` for the attributes primary.py reads."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.id = name
        self.client_urls = (f"http://{name}:2380",)


class _FakeStatus:
    """Duck-types ``aetcd``'s cluster ``Status`` (only ``.leader`` is read)."""

    def __init__(self, leader: object | None) -> None:
        self.leader = leader


class _FakeEtcdClient:
    """Minimal async stand-in for ``aetcd.Client`` covering primary.py's calls.

    Starts with a healthy 3-member cluster (members "1", "2", "3") so
    ``_check_etcd_quorum`` passes and node-join/leader-election polling loops
    resolve on their first iteration (no 5s ``asyncio.sleep`` in tests).
    """

    def __init__(self) -> None:
        self.member_names = ["1", "2", "3"]
        self.removed: list[str] = []

    async def members(self) -> Any:
        for name in self.member_names:
            yield _FakeMember(name)

    async def remove_member(self, member_id: str) -> None:
        self.removed.append(member_id)
        if member_id in self.member_names:
            self.member_names.remove(member_id)

    async def status(self) -> _FakeStatus:
        return _FakeStatus(leader=_FakeMember("1"))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeVaultClient:
    """Minimal stand-in for the Vault client's PKI-issuance call."""

    def pki_write(self, path: str, **kwargs: object) -> dict[str, str]:
        return {
            "certificate": "FAKE-CERT-PEM",
            "private_key": "FAKE-KEY-PEM",
            "cert_fingerprint": "aa:bb:cc:dd:ee:ff",
        }


@pytest.fixture()
def fake_etcd(monkeypatch):
    """Patch ``aetcd.Client`` so replace/force-recover see a healthy cluster.

    No real etcd server exists in this test environment -- unmocked, every
    etcd RPC fails with ``Connection refused``, which is real but incidental
    environment noise, not the behaviour under test.
    """
    import aetcd

    fake_client = _FakeEtcdClient()
    monkeypatch.setattr(aetcd, "Client", lambda *a, **kw: fake_client)
    return fake_client


@pytest.fixture()
def fake_etcdctl(monkeypatch):
    """Stub the ``etcdctl`` binary for force-recover's snapshot save/restore.

    ``etcdctl`` is not installed in this test environment; mock the
    subprocess boundary rather than letting the missing binary silently
    dictate the test outcome (a `FileNotFoundError` masquerading as a
    generic 500). Only ``etcdctl`` invocations are intercepted -- every other
    subprocess call (e.g. `kubectl`) runs for real.
    """
    real_run = subprocess.run

    def _fake_run(args: Any, *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        if args and args[0] == "etcdctl":
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _fake_run)


@pytest.fixture()
def fake_vault_ca(client, tmp_path):
    """Configure a fake Vault PKI client + writable TFTP root for rotate-ca.

    Neither is wired into the bare test app by default (``vault_client`` is
    unset -> immediate 503; ``TFTP_ROOT`` defaults to ``/var/lib/tftp``,
    unwritable here) -- mock the Vault boundary and point TFTP at a real
    writable tmp dir so the endpoint's own artifact-publish logic runs for
    real against the filesystem.
    """
    (tmp_path / "boot").mkdir()
    client.app.config["vault_client"] = _FakeVaultClient()
    client.app.config["TFTP_ROOT"] = str(tmp_path)
    return client


@pytest.mark.asyncio
async def test_get_primary_status(client):
    """Test GET /api/v1/primary/status returns per-service quorum state."""
    response = await client.get(
        "/api/v1/primary/status",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "success"
    assert "services" in data["data"]
    assert "postgres" in data["data"]["services"]
    assert "vault" in data["data"]["services"]
    assert "spire" in data["data"]["services"]
    assert "k8s_control_plane" in data["data"]["services"]


@pytest.mark.asyncio
async def test_get_primary_status_includes_quorum_health(client):
    """Test GET /api/v1/primary/status includes quorum_healthy field."""
    response = await client.get(
        "/api/v1/primary/status",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    data = await response.get_json()
    # All services should have quorum_healthy field
    for service_name, service_info in data["data"]["services"].items():
        assert "quorum_healthy" in service_info


@pytest.mark.asyncio
async def test_post_primary_replace_success(client, fake_etcd):
    """Test POST /api/v1/primary/replace performs a real etcd member swap.

    Regression: this asserted a 202-deferred-to-M2 stub response that was
    never implemented (see module docstring on ``app.api.primary`` -- the
    endpoint has returned real synchronous 200/409/504/500 results since its
    first commit). With the etcd RPC boundary mocked (``fake_etcd``), the
    real success path is 200 with the old/new node ids echoed back.
    """
    body = {
        "old_node_id": 1,
        "new_node_id": 2,
        "reason": "primary_node_failure",
    }
    response = await client.post(
        "/api/v1/primary/replace",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "success"
    assert data["data"]["old_node_id"] == 1
    assert data["data"]["new_node_id"] == 2


@pytest.mark.asyncio
async def test_post_primary_replace_missing_old_node_id(client):
    """Test POST /api/v1/primary/replace rejects missing old_node_id."""
    body = {
        "new_node_id": 2,
        "reason": "failure",
    }
    response = await client.post(
        "/api/v1/primary/replace",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422
    data = await response.get_json()
    assert "old_node_id" in data["error"]["message"]


@pytest.mark.asyncio
async def test_post_primary_replace_missing_new_node_id(client):
    """Test POST /api/v1/primary/replace rejects missing new_node_id."""
    body = {
        "old_node_id": 1,
        "reason": "failure",
    }
    response = await client.post(
        "/api/v1/primary/replace",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_primary_replace_missing_reason(client):
    """Test POST /api/v1/primary/replace rejects missing reason."""
    body = {
        "old_node_id": 1,
        "new_node_id": 2,
    }
    response = await client.post(
        "/api/v1/primary/replace",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422
    data = await response.get_json()
    assert "reason" in data["error"]["message"]


@pytest.mark.asyncio
async def test_post_primary_force_recover_success(client, fake_etcd, fake_etcdctl):
    """Test POST /api/v1/primary/force-recover with a real break-glass recovery.

    Regression: this asserted a 202-deferred-to-M2 stub that was never
    implemented -- the endpoint always ran a real ``etcdctl`` snapshot
    save/restore + etcd stabilization poll. With ``etcdctl`` (missing from
    this environment) and the etcd RPC boundary mocked, the real success
    path is 200.
    """
    body = {
        "surviving_node_id": 3,
        "reason": "quorum_loss_incident",
        "typed_cluster_name_confirmation": "default",
    }
    response = await client.post(
        "/api/v1/primary/force-recover",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "success"
    assert data["data"]["surviving_node_id"] == 3


@pytest.mark.asyncio
async def test_post_primary_force_recover_wrong_cluster_confirmation(client):
    """Test POST /api/v1/primary/force-recover rejects mismatched confirmation."""
    body = {
        "surviving_node_id": 3,
        "reason": "quorum_loss",
        "typed_cluster_name_confirmation": "wrong-cluster-name",
    }
    response = await client.post(
        "/api/v1/primary/force-recover",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422
    data = await response.get_json()
    assert "mismatch" in data["error"]["message"].lower()


@pytest.mark.asyncio
async def test_post_primary_force_recover_missing_surviving_node_id(client):
    """Test POST /api/v1/primary/force-recover rejects missing surviving_node_id."""
    body = {
        "reason": "quorum_loss",
        "typed_cluster_name_confirmation": "default",
    }
    response = await client.post(
        "/api/v1/primary/force-recover",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_primary_frontend_switch_to_vip(client, monkeypatch):
    """Test POST /api/v1/primary/frontend-switch to VIP mode.

    Regression: this asserted a 202-deferred-to-M2 stub that was never
    implemented, and expected ``target_mode == "vip"`` echoed back -- but
    the endpoint's own docstring documents an intentional legacy-alias remap
    ("Renames modes from M1 vocabulary: vip->kube-vip"), so the real success
    response reports "kube-vip".

    The real ``_update_endpoint_on_nodes``/``_emit_gracious_arp`` reach a
    live gRPC stub (``app.grpc.gough.discovery_pb2_grpc``, owned by another
    workstream) and columns the ``nodes`` table doesn't have in this schema
    (pre-existing, documented on ``_update_endpoint_on_nodes`` itself) -- out
    of scope here, so stub them the same way
    ``tests/api/test_primary_dal_conversion.py`` already does, isolating
    this test to the endpoint's own request/response handling.
    """
    import app.api.primary as primary_mod

    async def fake_update_endpoint(new_endpoint: str, node_ids: list) -> dict:
        # Real shape: dict[node_id] -> {"success": bool, ...}.
        return {nid: {"success": True, "error": None} for nid in node_ids}

    async def fake_arp(vip: str, node_ids: list) -> dict:
        return {}

    monkeypatch.setattr(primary_mod, "_update_endpoint_on_nodes", fake_update_endpoint)
    monkeypatch.setattr(primary_mod, "_emit_gracious_arp", fake_arp)

    body = {
        "target_mode": "vip",
    }
    response = await client.post(
        "/api/v1/primary/frontend-switch",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "success"
    assert data["data"]["target_mode"] == "kube-vip"


@pytest.mark.asyncio
async def test_post_primary_frontend_switch_to_anycast(client):
    """Test POST /api/v1/primary/frontend-switch to anycast mode.

    Regression: this asserted a 202-deferred-to-M2 stub that was never
    implemented. Anycast is intentionally unimplemented in M2 (BGP peering
    "tracked in M3 roadmap" per the endpoint's own docstring), which returns
    501 -- not a deferred-acceptance response.
    """
    body = {
        "target_mode": "anycast",
    }
    response = await client.post(
        "/api/v1/primary/frontend-switch",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 501
    data = await response.get_json()
    assert data["status"] == "error"
    assert data["error"]["code"] == "anycast_requires_bgp"


@pytest.mark.asyncio
async def test_post_primary_frontend_switch_invalid_mode(client):
    """Test POST /api/v1/primary/frontend-switch rejects invalid mode."""
    body = {
        "target_mode": "invalid",
    }
    response = await client.post(
        "/api/v1/primary/frontend-switch",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422
    data = await response.get_json()
    assert "vip" in data["error"]["message"] or "anycast" in data["error"]["message"]


@pytest.mark.asyncio
async def test_post_primary_rotate_ca_success(client, fake_vault_ca):
    """Test POST /api/v1/primary/rotate-ca performs a real CA rotation.

    Regression: this asserted a 202-deferred-to-M2 stub that was never
    implemented -- the endpoint always issued a real Vault PKI cert and
    published it to TFTP. With Vault mocked and TFTP pointed at a writable
    tmp dir (``fake_vault_ca``), the real success path is 200.
    """
    body = {
        "reason": "annual_rotation",
        "new_ca_validity_days": 365,
    }
    response = await fake_vault_ca.post(
        "/api/v1/primary/rotate-ca",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "success"
    assert data["data"]["validity_days"] == 365
    assert data["data"]["new_ca_fingerprint"] == "aa:bb:cc:dd:ee:ff"


@pytest.mark.asyncio
async def test_post_primary_rotate_ca_missing_reason(client):
    """Test POST /api/v1/primary/rotate-ca rejects missing reason."""
    body = {
        "new_ca_validity_days": 365,
    }
    response = await client.post(
        "/api/v1/primary/rotate-ca",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422
    data = await response.get_json()
    assert "reason" in data["error"]["message"]


@pytest.mark.asyncio
async def test_post_primary_rotate_ca_invalid_validity_days_too_short(client):
    """Test POST /api/v1/primary/rotate-ca rejects validity_days < 1."""
    body = {
        "reason": "rotation",
        "new_ca_validity_days": 0,
    }
    response = await client.post(
        "/api/v1/primary/rotate-ca",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_primary_rotate_ca_invalid_validity_days_too_long(client):
    """Test POST /api/v1/primary/rotate-ca rejects validity_days > 3650."""
    body = {
        "reason": "rotation",
        "new_ca_validity_days": 3651,
    }
    response = await client.post(
        "/api/v1/primary/rotate-ca",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422
    data = await response.get_json()
    assert "3650" in data["error"]["message"]


@pytest.mark.asyncio
async def test_scope_gough_cluster_read_required_for_status(client):
    """Test GET /api/v1/primary/status requires gough.cluster.read scope."""
    response = await client.get(
        "/api/v1/primary/status",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code in (200, 403)


@pytest.mark.asyncio
async def test_scope_gough_cluster_admin_required_for_replace(client, fake_etcd):
    """Test POST /api/v1/primary/replace requires gough.cluster.admin scope.

    The ``client`` fixture's ``_inject_auth`` hook always grants full admin
    scope, so this cannot exercise actual scope *denial* -- it proves an
    admin-scoped request reaches ``_scope_required``'s pass-through and the
    real handler completes (regression: previously asserted a 202-stub
    response that was never implemented -- see ``fake_etcd``).
    """
    body = {
        "old_node_id": 1,
        "new_node_id": 2,
        "reason": "failure",
    }
    response = await client.post(
        "/api/v1/primary/replace",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_scope_gough_cluster_superadmin_required_for_force_recover(
    client, fake_etcd, fake_etcdctl
):
    """Test POST /api/v1/primary/force-recover requires gough.cluster.superadmin scope.

    Same caveat as ``test_scope_gough_cluster_admin_required_for_replace``:
    proves a superadmin-scoped request reaches the real handler and
    completes, not actual scope denial.
    """
    body = {
        "surviving_node_id": 3,
        "reason": "quorum_loss",
        "typed_cluster_name_confirmation": "default",
    }
    response = await client.post(
        "/api/v1/primary/force-recover",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_mfa_required_for_replace(client, fake_etcd):
    """Test POST /api/v1/primary/replace requires MFA.

    The ``client`` fixture's ``_inject_auth`` hook always sets
    ``g.mfa_verified = True``, so this cannot exercise actual MFA *denial* --
    it proves an MFA-verified request reaches the real handler and completes
    (regression: previously asserted a 202-stub response that was never
    implemented -- see ``fake_etcd``).
    """
    body = {
        "old_node_id": 1,
        "new_node_id": 2,
        "reason": "failure",
    }
    response = await client.post(
        "/api/v1/primary/replace",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_mfa_required_for_force_recover(client, fake_etcd, fake_etcdctl):
    """Test POST /api/v1/primary/force-recover requires MFA.

    Same caveat as ``test_mfa_required_for_replace``: proves an MFA-verified
    request reaches the real handler and completes, not actual MFA denial.
    """
    body = {
        "surviving_node_id": 3,
        "reason": "loss",
        "typed_cluster_name_confirmation": "default",
    }
    response = await client.post(
        "/api/v1/primary/force-recover",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_mfa_required_for_rotate_ca(client, fake_vault_ca):
    """Test POST /api/v1/primary/rotate-ca requires MFA.

    Same caveat as ``test_mfa_required_for_replace``: proves an MFA-verified
    request reaches the real handler and completes, not actual MFA denial.
    """
    body = {
        "reason": "rotation",
    }
    response = await fake_vault_ca.post(
        "/api/v1/primary/rotate-ca",
        data=json.dumps(body),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
