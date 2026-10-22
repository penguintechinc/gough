"""Tests for app.clients.lxd_extra (cluster admin LXD operations).

Uses ``requests_mock`` to stub the LXD HTTPS API and ``unittest.mock`` to
stub VaultClient interactions. Covers happy path + error path for each
public function. Targets >=90% coverage of the module.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
import requests_mock as rm_module

from app.clients import lxd_extra
from app.clients.lxd_extra import (
    ClusterMember,
    ClusterStatus,
    JoinToken,
    LXDClusterError,
    LXDConfigError,
    LXDMigrationError,
    LXDOperationTimeout,
    MigrationResult,
    _mask_password,
    cluster_join,
    get_cluster_status,
    live_migrate,
    mint_join_token,
    rotate_trust_password,
    set_trust_password,
)
from app.clients.vault import VaultError, VaultKvReadResponse


CLUSTER_ID = "test-cluster"
API_URL = "https://localhost:8443"


@pytest.fixture
def fake_vault() -> MagicMock:
    """Return a MagicMock VaultClient that returns a fake server cert."""
    vault = MagicMock()
    vault.kv_read.return_value = VaultKvReadResponse(
        data={"certificate": "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"},
        metadata={"v": 1},
    )
    vault.kv_write.return_value = None
    return vault


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LXD_API_URL", API_URL)
    monkeypatch.setenv("LXD_CLUSTER_ID", CLUSTER_ID)
    monkeypatch.delenv("LXD_CLIENT_CERT", raising=False)
    monkeypatch.delenv("LXD_CLIENT_KEY", raising=False)
    yield


@pytest.fixture
def requests_mock_fixture() -> Iterator[rm_module.Mocker]:
    with rm_module.Mocker() as m:
        yield m


# ----- helpers --------------------------------------------------------------


class TestMaskPassword:
    def test_short_password(self) -> None:
        assert _mask_password("") == "****"
        assert _mask_password("short") == "****"

    def test_long_password(self) -> None:
        masked = _mask_password("abcdEFGHijklMNOP")
        assert masked == "abcd...MNOP"


# ----- mint_join_token ------------------------------------------------------


class TestMintJoinToken:
    def test_happy_path(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.post(
            f"{API_URL}/1.0/cluster/members",
            json={
                "metadata": {
                    "server_name": "node-2",
                    "fingerprint": "abc123",
                    "addresses": ["10.0.0.1:8443"],
                    "secret": "supersecret",
                }
            },
            status_code=200,
        )
        token = mint_join_token(CLUSTER_ID, ttl_seconds=600, vault=fake_vault)
        assert isinstance(token, JoinToken)
        assert token.fingerprint == "abc123"
        decoded = json.loads(base64.b64decode(token.token).decode())
        assert decoded["secret"] == "supersecret"
        assert decoded["server_name"] == "node-2"
        assert token.expires_at > datetime.now(timezone.utc)

    def test_invalid_cluster_id(self, fake_vault: MagicMock) -> None:
        with pytest.raises(LXDConfigError):
            mint_join_token("", vault=fake_vault)

    def test_invalid_ttl(self, fake_vault: MagicMock) -> None:
        with pytest.raises(LXDConfigError):
            mint_join_token(CLUSTER_ID, ttl_seconds=0, vault=fake_vault)

    def test_lxd_returns_500(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.post(
            f"{API_URL}/1.0/cluster/members",
            json={"error": "boom"},
            status_code=500,
        )
        with pytest.raises(LXDClusterError):
            mint_join_token(CLUSTER_ID, vault=fake_vault)

    def test_malformed_response(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.post(
            f"{API_URL}/1.0/cluster/members",
            json={"metadata": {"addresses": ["x"], "fingerprint": "fp"}},
            status_code=200,
        )
        with pytest.raises(LXDClusterError):
            mint_join_token(CLUSTER_ID, vault=fake_vault)

    def test_non_json_body(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.post(
            f"{API_URL}/1.0/cluster/members",
            text="not json",
            status_code=200,
        )
        with pytest.raises(LXDClusterError):
            mint_join_token(CLUSTER_ID, vault=fake_vault)

    def test_vault_error_propagates_as_config_error(
        self, requests_mock_fixture: rm_module.Mocker
    ) -> None:
        bad_vault = MagicMock()
        bad_vault.kv_read.side_effect = VaultError("vault is sealed")
        with pytest.raises(LXDConfigError):
            mint_join_token(CLUSTER_ID, vault=bad_vault)

    def test_missing_cert_in_vault(
        self, requests_mock_fixture: rm_module.Mocker
    ) -> None:
        empty_vault = MagicMock()
        empty_vault.kv_read.return_value = VaultKvReadResponse(data={})
        with pytest.raises(LXDConfigError):
            mint_join_token(CLUSTER_ID, vault=empty_vault)

    def test_no_vault_token_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        with pytest.raises(LXDConfigError):
            mint_join_token(CLUSTER_ID)


# ----- cluster_join ---------------------------------------------------------


class TestClusterJoin:
    def test_happy_path_with_operation(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        op_url = "/1.0/operations/op-1"
        requests_mock_fixture.put(
            f"{API_URL}/1.0/cluster",
            json={"operation": op_url},
            status_code=202,
        )
        requests_mock_fixture.get(
            f"{API_URL}{op_url}/wait",
            json={"metadata": {"status": "Success", "status_code": 200}},
            status_code=200,
        )
        requests_mock_fixture.get(
            f"{API_URL}/1.0/cluster/members/node-3",
            json={
                "metadata": {
                    "server_name": "node-3",
                    "url": "https://10.0.0.3:8443",
                    "roles": ["database"],
                    "status": "Online",
                }
            },
            status_code=200,
        )
        member = cluster_join(
            "node-3:8443",
            "tokenblob",
            "10.0.0.1:8443",
            cluster_id=CLUSTER_ID,
            vault=fake_vault,
        )
        assert isinstance(member, ClusterMember)
        assert member.name == "node-3"
        assert member.role == "database"
        assert member.status == "Online"

    def test_happy_path_no_operation(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.put(
            f"{API_URL}/1.0/cluster",
            json={"metadata": {}},
            status_code=200,
        )
        requests_mock_fixture.get(
            f"{API_URL}/1.0/cluster/members/node-4",
            json={
                "metadata": {
                    "server_name": "node-4",
                    "url": "https://10.0.0.4:8443",
                    "roles": [],
                    "status": "Online",
                }
            },
            status_code=200,
        )
        member = cluster_join(
            "node-4:8443",
            "tokenblob",
            "10.0.0.1:8443",
            cluster_id=CLUSTER_ID,
            vault=fake_vault,
        )
        assert member.role == ""

    def test_missing_args(self, fake_vault: MagicMock) -> None:
        with pytest.raises(LXDConfigError):
            cluster_join("", "tok", "addr", cluster_id=CLUSTER_ID, vault=fake_vault)
        with pytest.raises(LXDConfigError):
            cluster_join("a", "", "addr", cluster_id=CLUSTER_ID, vault=fake_vault)
        with pytest.raises(LXDConfigError):
            cluster_join("a", "tok", "", cluster_id=CLUSTER_ID, vault=fake_vault)

    def test_missing_cluster_id(
        self, monkeypatch: pytest.MonkeyPatch, fake_vault: MagicMock
    ) -> None:
        monkeypatch.delenv("LXD_CLUSTER_ID", raising=False)
        with pytest.raises(LXDConfigError):
            cluster_join("a:1", "tok", "b:1", vault=fake_vault)

    def test_join_failure(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.put(
            f"{API_URL}/1.0/cluster",
            json={"error": "bad token"},
            status_code=403,
        )
        with pytest.raises(LXDClusterError):
            cluster_join(
                "n:1",
                "tok",
                "s:1",
                cluster_id=CLUSTER_ID,
                vault=fake_vault,
            )


# ----- live_migrate ---------------------------------------------------------


class TestLiveMigrate:
    def test_happy_path(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        op_url = "/1.0/operations/migrate-1"
        requests_mock_fixture.post(
            f"{API_URL}/1.0/instances/web1",
            json={"operation": op_url},
            status_code=202,
        )
        requests_mock_fixture.get(
            f"{API_URL}{op_url}/wait",
            json={"metadata": {"status": "Success", "status_code": 200}},
            status_code=200,
        )
        result = live_migrate(
            "web1",
            "node-2",
            cluster_id=CLUSTER_ID,
            vault=fake_vault,
        )
        assert isinstance(result, MigrationResult)
        assert result.operation_id == "migrate-1"
        assert result.status == "Success"
        assert result.duration_seconds >= 0

    def test_missing_args(self, fake_vault: MagicMock) -> None:
        with pytest.raises(LXDConfigError):
            live_migrate("", "n", cluster_id=CLUSTER_ID, vault=fake_vault)
        with pytest.raises(LXDConfigError):
            live_migrate("i", "", cluster_id=CLUSTER_ID, vault=fake_vault)

    def test_post_failure(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.post(
            f"{API_URL}/1.0/instances/web1",
            json={"error": "instance busy"},
            status_code=409,
        )
        with pytest.raises(LXDMigrationError):
            live_migrate("web1", "n2", cluster_id=CLUSTER_ID, vault=fake_vault)

    def test_missing_operation(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.post(
            f"{API_URL}/1.0/instances/web1",
            json={"metadata": {}},
            status_code=200,
        )
        with pytest.raises(LXDMigrationError):
            live_migrate("web1", "n2", cluster_id=CLUSTER_ID, vault=fake_vault)

    def test_failed_operation(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        op_url = "/1.0/operations/migrate-2"
        requests_mock_fixture.post(
            f"{API_URL}/1.0/instances/web1",
            json={"operation": op_url},
            status_code=202,
        )
        requests_mock_fixture.get(
            f"{API_URL}{op_url}/wait",
            json={"metadata": {"status": "Failure", "status_code": 400}},
            status_code=200,
        )
        with pytest.raises(LXDMigrationError):
            live_migrate("web1", "n2", cluster_id=CLUSTER_ID, vault=fake_vault)

    def test_timeout(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        op_url = "/1.0/operations/migrate-3"
        requests_mock_fixture.post(
            f"{API_URL}/1.0/instances/web1",
            json={"operation": op_url},
            status_code=202,
        )
        requests_mock_fixture.get(
            f"{API_URL}{op_url}/wait",
            json={"metadata": {"status": "Running", "status_code": 103}},
            status_code=200,
        )
        with pytest.raises(LXDOperationTimeout):
            live_migrate(
                "web1",
                "n2",
                cluster_id=CLUSTER_ID,
                vault=fake_vault,
                timeout=0.05,
            )

    def test_missing_cluster_id(
        self, monkeypatch: pytest.MonkeyPatch, fake_vault: MagicMock
    ) -> None:
        monkeypatch.delenv("LXD_CLUSTER_ID", raising=False)
        with pytest.raises(LXDConfigError):
            live_migrate("i", "n", vault=fake_vault)


# ----- set_trust_password ---------------------------------------------------


class TestSetTrustPassword:
    def test_happy_path(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.patch(
            f"{API_URL}/1.0",
            json={"metadata": {}},
            status_code=200,
        )
        set_trust_password("a" * 32, cluster_id=CLUSTER_ID, vault=fake_vault)

    def test_too_short(self, fake_vault: MagicMock) -> None:
        with pytest.raises(LXDConfigError):
            set_trust_password("short", cluster_id=CLUSTER_ID, vault=fake_vault)

    def test_lxd_failure_raises_config_error(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.patch(
            f"{API_URL}/1.0",
            json={"error": "denied"},
            status_code=403,
        )
        with pytest.raises(LXDConfigError):
            set_trust_password("a" * 32, cluster_id=CLUSTER_ID, vault=fake_vault)

    def test_missing_cluster_id(
        self, monkeypatch: pytest.MonkeyPatch, fake_vault: MagicMock
    ) -> None:
        monkeypatch.delenv("LXD_CLUSTER_ID", raising=False)
        with pytest.raises(LXDConfigError):
            set_trust_password("a" * 32, vault=fake_vault)


# ----- rotate_trust_password ------------------------------------------------


class TestRotateTrustPassword:
    def test_happy_path(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.patch(
            f"{API_URL}/1.0",
            json={"metadata": {}},
            status_code=200,
        )
        new_pw = rotate_trust_password(cluster_id=CLUSTER_ID, vault=fake_vault)
        assert isinstance(new_pw, str)
        assert len(new_pw) == 128
        fake_vault.kv_write.assert_called_once()
        path_arg, data_arg = fake_vault.kv_write.call_args.args
        assert "cluster-trust-password" in path_arg
        assert data_arg["password"] == new_pw

    def test_vault_write_failure(
        self, requests_mock_fixture: rm_module.Mocker
    ) -> None:
        bad_vault = MagicMock()
        bad_vault.kv_read.return_value = VaultKvReadResponse(
            data={"certificate": "PEM"}, metadata={}
        )
        bad_vault.kv_write.side_effect = VaultError("write denied")
        with pytest.raises(LXDConfigError):
            rotate_trust_password(cluster_id=CLUSTER_ID, vault=bad_vault)

    def test_missing_cluster_id(self, monkeypatch: pytest.MonkeyPatch, fake_vault: MagicMock) -> None:
        monkeypatch.delenv("LXD_CLUSTER_ID", raising=False)
        with pytest.raises(LXDConfigError):
            rotate_trust_password(vault=fake_vault)


# ----- get_cluster_status ---------------------------------------------------


class TestGetClusterStatus:
    def test_healthy(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.get(
            f"{API_URL}/1.0/cluster/members?recursion=2",
            json={
                "metadata": [
                    {
                        "server_name": "n1",
                        "url": "https://10.0.0.1:8443",
                        "roles": ["database-leader"],
                        "status": "Online",
                    },
                    {
                        "server_name": "n2",
                        "url": "https://10.0.0.2:8443",
                        "roles": ["database"],
                        "status": "Online",
                    },
                    {
                        "server_name": "n3",
                        "url": "https://10.0.0.3:8443",
                        "roles": [],
                        "status": "Online",
                    },
                ]
            },
            status_code=200,
        )
        status = get_cluster_status(cluster_id=CLUSTER_ID, vault=fake_vault)
        assert isinstance(status, ClusterStatus)
        assert status.quorum_status == "healthy"
        assert len(status.members) == 3
        assert status.members[0].role == "database-leader"

    def test_degraded_when_db_member_offline(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.get(
            f"{API_URL}/1.0/cluster/members?recursion=2",
            json={
                "metadata": [
                    {
                        "server_name": "n1",
                        "url": "https://10.0.0.1:8443",
                        "roles": ["database-leader"],
                        "status": "Online",
                    },
                    {
                        "server_name": "n2",
                        "url": "https://10.0.0.2:8443",
                        "roles": ["database"],
                        "status": "Offline",
                    },
                ]
            },
            status_code=200,
        )
        status = get_cluster_status(cluster_id=CLUSTER_ID, vault=fake_vault)
        assert status.quorum_status == "degraded"

    def test_degraded_when_no_db_members(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        requests_mock_fixture.get(
            f"{API_URL}/1.0/cluster/members?recursion=2",
            json={"metadata": []},
            status_code=200,
        )
        status = get_cluster_status(cluster_id=CLUSTER_ID, vault=fake_vault)
        assert status.quorum_status == "degraded"
        assert status.members == []

    def test_missing_cluster_id(
        self, monkeypatch: pytest.MonkeyPatch, fake_vault: MagicMock
    ) -> None:
        monkeypatch.delenv("LXD_CLUSTER_ID", raising=False)
        with pytest.raises(LXDConfigError):
            get_cluster_status(vault=fake_vault)


# ----- transport / cert pinning ---------------------------------------------


class TestTransport:
    def test_request_exception_raises_cluster_error(
        self, requests_mock_fixture: rm_module.Mocker, fake_vault: MagicMock
    ) -> None:
        import requests

        requests_mock_fixture.get(
            f"{API_URL}/1.0/cluster/members?recursion=2",
            exc=requests.exceptions.ConnectTimeout("timeout"),
        )
        with pytest.raises(LXDClusterError):
            get_cluster_status(cluster_id=CLUSTER_ID, vault=fake_vault)

    def test_client_cert_paths_must_exist(
        self,
        requests_mock_fixture: rm_module.Mocker,
        fake_vault: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LXD_CLIENT_CERT", "/nonexistent/cert.pem")
        monkeypatch.setenv("LXD_CLIENT_KEY", "/nonexistent/key.pem")
        with pytest.raises(LXDConfigError):
            get_cluster_status(cluster_id=CLUSTER_ID, vault=fake_vault)

    def test_client_cert_paths_existing(
        self,
        requests_mock_fixture: rm_module.Mocker,
        fake_vault: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        cert_path.write_text("cert")
        key_path.write_text("key")
        monkeypatch.setenv("LXD_CLIENT_CERT", str(cert_path))
        monkeypatch.setenv("LXD_CLIENT_KEY", str(key_path))
        requests_mock_fixture.get(
            f"{API_URL}/1.0/cluster/members?recursion=2",
            json={"metadata": []},
            status_code=200,
        )
        status = get_cluster_status(cluster_id=CLUSTER_ID, vault=fake_vault)
        assert status.quorum_status == "degraded"
