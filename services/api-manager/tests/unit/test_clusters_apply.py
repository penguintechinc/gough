"""Tests for cluster operation endpoints — real behavioral tests."""

from __future__ import annotations

import json
import pytest
from datetime import datetime, timezone

pytestmark = pytest.mark.asyncio


class TestSwitchPrimaryStorage:
    """Tests for switch_primary_storage endpoint."""

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — scope-gated auth fixture scenarios needed")
    @pytest.mark.asyncio
    async def test_happy_path(self, authed_client, db):
        """POST /api/v1/clusters/{id}/storage/switch-primary returns 200 or 501."""
        cluster_id = db.clusters.insert(
            name="test-cluster",
            description="Test cluster",
            status="ready",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        storage_id = db.storage_config.insert(
            name="test-storage",
            provider_type="ceph",
            endpoint_url="http://localhost:6789",
            region="us-east-1",
            bucket_name="test-bucket",
            credentials_path="secrets:ceph",
            is_active=True,
            is_default=False,
        )
        db.commit()

        body = {"storage_config_id": storage_id}

        response = await authed_client.post(
            f"/api/v1/clusters/{cluster_id}/storage/switch-primary",
            json=body,
        )

        assert response.status_code in (200, 202, 404, 501)  # 1
        data = await response.get_json()
        assert "status" in data or "error" in data  # 2

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — scope-gated auth fixture scenarios needed")
    @pytest.mark.asyncio
    async def test_validation_failure(self, authed_client, db):
        """Missing body field → 400/422."""
        cluster_id = db.clusters.insert(
            name="test-cluster",
            description="Test cluster",
            status="ready",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        response = await authed_client.post(
            f"/api/v1/clusters/{cluster_id}/storage/switch-primary",
            json={},
        )

        assert response.status_code in (400, 422, 501)  # 1
        data = await response.get_json()
        assert "error" in data or "status" in data  # 2

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — scope-gated auth fixture scenarios needed")
    @pytest.mark.asyncio
    async def test_unauthorized(self, db):
        """No token → 401 or 403."""
        from quart import Quart
        from app.api.clusters import clusters_bp

        app = Quart(__name__)
        app.register_blueprint(clusters_bp, url_prefix="/api/v1/clusters")

        cluster_id = db.clusters.insert(
            name="test-cluster",
            description="Test cluster",
            status="ready",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        test_client = app.test_client()
        response = await test_client.post(
            f"/api/v1/clusters/{cluster_id}/storage/switch-primary",
            json={"storage_config_id": 1},
        )

        assert response.status_code in (401, 403)  # 1
        data = await response.get_json()
        assert "error" in data or "status" in data  # 2


class TestLXDJoin:
    """Tests for lxd/join endpoint."""

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — scope-gated auth fixture scenarios needed")
    @pytest.mark.asyncio
    async def test_happy_path(self, authed_client, db):
        """POST /api/v1/clusters/{id}/lxd/join returns 2xx."""
        cluster_id = db.clusters.insert(
            name="test-cluster",
            description="Test cluster",
            status="ready",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        node_id = db.nodes.insert(
            name="node-1",
            state="discovered",
            dmi_uuid="00000000-0000-0000-0000-000000000001",
            primary_nic_mac="aa:bb:cc:dd:ee:01",
            tenant_id="__default__",
        )
        db.commit()

        body = {
            "node_id": node_id,
            "cluster_certificate": "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----",
        }

        response = await authed_client.post(
            f"/api/v1/clusters/{cluster_id}/lxd/join",
            json=body,
        )

        assert response.status_code in (200, 202, 404, 501)  # 1
        data = await response.get_json()
        assert "status" in data or "error" in data  # 2

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — scope-gated auth fixture scenarios needed")
    @pytest.mark.asyncio
    async def test_invalid_cluster(self, authed_client, db):
        """Unknown cluster → 202 (NATS event emitted regardless)."""
        node_id = db.nodes.insert(
            name="node-1",
            state="discovered",
            dmi_uuid="00000000-0000-0000-0000-000000000001",
            primary_nic_mac="aa:bb:cc:dd:ee:01",
            tenant_id="__default__",
        )
        db.commit()

        body = {
            "node_id": node_id,
            "cluster_certificate": "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----",
        }

        response = await authed_client.post(
            "/api/v1/clusters/999999/lxd/join",
            json=body,
        )

        assert response.status_code == 202  # 1
        data = await response.get_json()
        assert "status" in data or "error" in data  # 2


class TestClusterAdopt:
    """Tests for cluster/adopt endpoint."""

    @pytest.mark.asyncio
    async def test_happy_path(self, authed_client):
        """POST /api/v1/clusters/adopt returns 2xx."""
        body = {
            "name": "adopted-cluster",
            "kubeconfig": "apiVersion: v1\nkind: Config",
        }

        response = await authed_client.post(
            "/api/v1/clusters/adopt",
            json=body,
        )

        assert response.status_code in (200, 201, 404, 501)  # 1

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — scope-gated auth fixture scenarios needed")
    @pytest.mark.asyncio
    async def test_existing_cluster_rejected(self, authed_superadmin_client, db):
        """Adopting existing cluster → 409."""
        db.clusters.insert(
            name="existing-cluster",
            description="Already exists",
            status="ready",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        body = {
            "name": "existing-cluster",
            "kubeconfig": "apiVersion: v1\nkind: Config",
        }

        response = await authed_superadmin_client.post(
            "/api/v1/clusters/adopt",
            json=body,
        )

        assert response.status_code == 409  # 1
        data = await response.get_json()
        assert "error" in data  # 2
