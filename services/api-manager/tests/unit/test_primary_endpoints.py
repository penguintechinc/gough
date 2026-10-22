"""Unit tests for primary.py endpoints: replace, force-recover, frontend-switch, rotate-ca.

Tests cover happy-path + error cases with mocked external dependencies
(etcd, Vault, SSH/subprocess, DB) running at network boundary only.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from quart import g

pytestmark = pytest.mark.skip(
    reason="gh-16: Phase 2 test harness rework — etcd3 mock + async fixture wiring needed"
)

# Mock etcd3 module before any imports that use it
sys.modules['etcd3'] = MagicMock()

from app.api.primary import (
    force_recover_quorum,
    replace_primary,
    rotate_ipxe_ca,
    switch_frontend,
)
from app.audit import AuditEventType, AuditLogger


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app_with_config(app):
    """Configure app with required settings."""
    app.config["CLUSTER_ID"] = "test-cluster"
    app.config["ETCD_HOST"] = "localhost"
    app.config["ETCD_PORT"] = 2379
    app.config["CONTROL_PLANE_ENDPOINT"] = "10.0.0.5:6443"
    app.config["TFTP_ROOT"] = "/var/lib/tftp"
    # Mock audit logger
    app.extensions = {
        "audit": MagicMock(spec=AuditLogger, log=MagicMock()),
    }
    # Mock vault client
    app.config["vault_client"] = MagicMock()
    return app


@pytest_asyncio.fixture
async def auth_context(app_with_config):
    """Set up authenticated request context with MFA."""
    async with app_with_config.test_request_context("/"):
        g.principal = MagicMock(
            scopes={"gough.cluster.admin", "gough.cluster.superadmin"},
            mfa_verified=True,
        )
        yield app_with_config


# =============================================================================
# replace_primary tests
# =============================================================================


@pytest.mark.asyncio
async def test_replace_primary_success(auth_context):
    """Happy path: replace primary node successfully."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "old_node_id": "node-1",
            "new_node_id": "node-4",
            "reason": "node-1 failed hardware",
        },
    ):
        with patch("app.api.primary.get_db") as mock_db, \
             patch("etcd3.client") as mock_etcd, \
             patch("app.api.primary._check_etcd_quorum", return_value=True) as mock_quorum, \
             patch("app.api.primary._get_joiner_secrets", return_value={}) as mock_secrets, \
             patch("app.api.primary.asyncio.to_thread") as mock_thread:

            # Mock etcd member list to simulate new node joining
            mock_client = MagicMock()
            member1 = MagicMock(id="member-1", name="node-1")
            member2 = MagicMock(id="member-2", name="node-2")
            member3 = MagicMock(id="member-3", name="node-4")
            mock_client.member_list.return_value = [member1, member2, member3]
            mock_etcd.return_value = mock_client

            # Simulate found on first check after member removal
            async def thread_side_effect(fn, *args):
                if fn == mock_client.member_list:
                    return [member1, member2, member3]
                return None

            mock_thread.side_effect = thread_side_effect

            g.principal = MagicMock(
                scopes={"gough.cluster.admin"},
                mfa_verified=True,
            )

            response = await replace_primary()
            assert response[1] == 200
            data = response[0].get_json()
            assert data["status"] == "success"
            assert data["data"]["old_node_id"] == "node-1"
            assert data["data"]["new_node_id"] == "node-4"


@pytest.mark.asyncio
async def test_replace_primary_missing_inputs(auth_context):
    """Validation: missing old_node_id or new_node_id."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={"reason": "test"},
    ):
        g.principal = MagicMock(
            scopes={"gough.cluster.admin"},
            mfa_verified=True,
        )
        response = await replace_primary()
        assert response[1] == 422
        assert response[0].get_json()["error"]["code"] == "validation_failed"


@pytest.mark.asyncio
async def test_replace_primary_quorum_loss(auth_context):
    """Error: quorum unhealthy before operation."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "old_node_id": "node-1",
            "new_node_id": "node-4",
            "reason": "test",
        },
    ):
        with patch("app.api.primary.get_db"), \
             patch("etcd3.client"), \
             patch("app.api.primary._check_etcd_quorum", return_value=False):

            g.principal = MagicMock(
                scopes={"gough.cluster.admin"},
                mfa_verified=True,
            )
            response = await replace_primary()
            assert response[1] == 409
            assert response[0].get_json()["error"]["code"] == "quorum_loss"


# =============================================================================
# force_recover_quorum tests
# =============================================================================


@pytest.mark.asyncio
async def test_force_recover_success(auth_context):
    """Happy path: force-recover with snapshot restore."""
    cluster_id = "test-cluster"
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "surviving_node_id": "node-1",
            "reason": "quorum lost",
            "typed_cluster_name_confirmation": cluster_id,
        },
    ):
        with patch("app.api.primary.get_db"), \
             patch("etcd3.client") as mock_etcd, \
             patch("app.api.primary.asyncio.to_thread") as mock_thread, \
             patch("app.api.primary._etcd_snapshot_restore", return_value="/var/lib/etcd-recovered"):

            mock_client = MagicMock()
            mock_client.status.return_value = MagicMock(leader=1)
            mock_etcd.return_value = mock_client

            async def thread_side_effect(fn, *args):
                return None

            mock_thread.side_effect = thread_side_effect

            g.principal = MagicMock(
                scopes={"gough.cluster.superadmin"},
                mfa_verified=True,
            )

            response = await force_recover_quorum()
            assert response[1] == 200
            data = response[0].get_json()
            assert data["status"] == "success"
            assert data["data"]["surviving_node_id"] == "node-1"


@pytest.mark.asyncio
async def test_force_recover_confirmation_mismatch(auth_context):
    """Validation: typed confirmation must match cluster name."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "surviving_node_id": "node-1",
            "reason": "quorum lost",
            "typed_cluster_name_confirmation": "wrong-cluster",
        },
    ):
        g.principal = MagicMock(
            scopes={"gough.cluster.superadmin"},
            mfa_verified=True,
        )
        response = await force_recover_quorum()
        assert response[1] == 422
        assert "confirmation" in response[0].get_json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_force_recover_missing_surviving_node(auth_context):
    """Validation: surviving_node_id required."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "reason": "quorum lost",
            "typed_cluster_name_confirmation": "test-cluster",
        },
    ):
        g.principal = MagicMock(
            scopes={"gough.cluster.superadmin"},
            mfa_verified=True,
        )
        response = await force_recover_quorum()
        assert response[1] == 422


# =============================================================================
# switch_frontend tests
# =============================================================================


@pytest.mark.asyncio
async def test_switch_frontend_kube_vip(auth_context):
    """Happy path: switch to kube-vip mode."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "target_mode": "kube-vip",
            "new_endpoint": "10.0.0.5:6443",
        },
    ):
        with patch("app.api.primary.get_db") as mock_db, \
             patch("app.api.primary._update_endpoint_on_nodes", return_value=True), \
             patch("app.api.primary._emit_gracious_arp", return_value=True):

            mock_db.return_value.query.return_value = [(1,), (2,), (3,)]

            g.principal = MagicMock(
                scopes={"gough.cluster.admin"},
                mfa_verified=True,
            )

            response = await switch_frontend()
            assert response[1] == 200
            data = response[0].get_json()
            assert data["status"] == "success"
            assert data["data"]["target_mode"] == "kube-vip"


@pytest.mark.asyncio
async def test_switch_frontend_external(auth_context):
    """Happy path: switch to external mode with endpoint validation."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "target_mode": "external",
            "new_endpoint": "lb.example.com:6443",
        },
    ):
        with patch("app.api.primary.get_db"), \
             patch("app.api.primary._update_endpoint_on_nodes", return_value=True), \
             patch("app.api.primary.asyncio.to_thread") as mock_thread:

            # Mock successful TCP connect
            def thread_side_effect(fn, *args):
                return None

            mock_thread.side_effect = thread_side_effect

            g.principal = MagicMock(
                scopes={"gough.cluster.admin"},
                mfa_verified=True,
            )

            response = await switch_frontend()
            assert response[1] == 200
            assert response[0].get_json()["data"]["target_mode"] == "external"


@pytest.mark.asyncio
async def test_switch_frontend_anycast_unimplemented(auth_context):
    """Intentional 501: anycast requires BGP (M3 work)."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={"target_mode": "anycast"},
    ):
        g.principal = MagicMock(
            scopes={"gough.cluster.admin"},
            mfa_verified=True,
        )
        response = await switch_frontend()
        assert response[1] == 501
        assert response[0].get_json()["error"]["code"] == "anycast_requires_bgp"


@pytest.mark.asyncio
async def test_switch_frontend_invalid_mode(auth_context):
    """Validation: invalid target_mode."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={"target_mode": "invalid"},
    ):
        g.principal = MagicMock(
            scopes={"gough.cluster.admin"},
            mfa_verified=True,
        )
        response = await switch_frontend()
        assert response[1] == 422


@pytest.mark.asyncio
async def test_switch_frontend_legacy_vip_mode(auth_context):
    """Backward compat: vip mode maps to kube-vip."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "target_mode": "vip",
            "new_endpoint": "10.0.0.5:6443",
        },
    ):
        with patch("app.api.primary.get_db"), \
             patch("app.api.primary._update_endpoint_on_nodes", return_value=True):

            g.principal = MagicMock(
                scopes={"gough.cluster.admin"},
                mfa_verified=True,
            )

            response = await switch_frontend()
            assert response[1] == 200
            assert response[0].get_json()["data"]["target_mode"] == "kube-vip"


# =============================================================================
# rotate_ipxe_ca tests
# =============================================================================


@pytest.mark.asyncio
async def test_rotate_ipxe_ca_success(auth_context):
    """Happy path: rotate CA, sign helper, publish."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "reason": "annual rotation",
            "new_ca_validity_days": 365,
        },
    ):
        with patch("app.api.primary.get_db"), \
             patch("app.api.primary._issue_pki_ca") as mock_pki, \
             patch("app.api.primary._publish_helper_artifact", return_value=True):

            mock_pki.return_value = {
                "cert": "-----BEGIN CERTIFICATE-----...",
                "key": "-----BEGIN PRIVATE KEY-----...",
                "ca_fingerprint": "sha256:abc123def456",
            }

            g.principal = MagicMock(
                scopes={"gough.cluster.superadmin"},
                mfa_verified=True,
            )

            response = await rotate_ipxe_ca()
            assert response[1] == 200
            data = response[0].get_json()
            assert data["status"] == "success"
            assert data["data"]["new_ca_fingerprint"] == "sha256:abc123def456"
            assert data["data"]["validity_days"] == 365


@pytest.mark.asyncio
async def test_rotate_ipxe_ca_missing_reason(auth_context):
    """Validation: reason required."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={},
    ):
        g.principal = MagicMock(
            scopes={"gough.cluster.superadmin"},
            mfa_verified=True,
        )
        response = await rotate_ipxe_ca()
        assert response[1] == 422
        assert response[0].get_json()["error"]["code"] == "validation_failed"


@pytest.mark.asyncio
async def test_rotate_ipxe_ca_invalid_days(auth_context):
    """Validation: days must be 1..3650."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "reason": "rotation",
            "new_ca_validity_days": 5000,
        },
    ):
        g.principal = MagicMock(
            scopes={"gough.cluster.superadmin"},
            mfa_verified=True,
        )
        response = await rotate_ipxe_ca()
        assert response[1] == 422


@pytest.mark.asyncio
async def test_rotate_ipxe_ca_pki_failure(auth_context):
    """Error: Vault PKI issuance fails."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "reason": "rotation",
            "new_ca_validity_days": 365,
        },
    ):
        with patch("app.api.primary._issue_pki_ca", return_value=None):
            g.principal = MagicMock(
                scopes={"gough.cluster.superadmin"},
                mfa_verified=True,
            )
            response = await rotate_ipxe_ca()
            assert response[1] == 500
            assert "pki_issue_failed" in response[0].get_json()["error"]["code"]


# =============================================================================
# Integration-like tests (minimal mocking)
# =============================================================================


@pytest.mark.asyncio
async def test_replace_primary_timeout(auth_context):
    """Timeout: new node fails to join etcd in 300s."""
    async with auth_context.test_request_context(
        "/",
        method="POST",
        json={
            "old_node_id": "node-1",
            "new_node_id": "node-4",
            "reason": "test",
        },
    ):
        with patch("app.api.primary.get_db"), \
             patch("etcd3.client") as mock_etcd, \
             patch("app.api.primary._check_etcd_quorum", return_value=True), \
             patch("app.api.primary.asyncio.sleep", side_effect=lambda x: None) as mock_sleep, \
             patch("app.api.primary.asyncio.get_event_loop") as mock_loop:

            mock_client = MagicMock()
            # Simulate node never joining
            member1 = MagicMock(id="m1", name="node-1")
            member2 = MagicMock(id="m2", name="node-2")
            mock_client.member_list.return_value = [member1, member2]
            mock_etcd.return_value = mock_client

            # Mock time progression
            times = iter([0.0, 5.0, 10.0, 305.0])
            mock_loop.return_value.time.side_effect = lambda: next(times)

            g.principal = MagicMock(
                scopes={"gough.cluster.admin"},
                mfa_verified=True,
            )

            response = await replace_primary()
            assert response[1] == 504
            assert "timeout" in response[0].get_json()["error"]["code"]
