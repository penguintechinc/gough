"""Tests for biome upgrade endpoint — real behavioral tests."""

from __future__ import annotations

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock

pytestmark = pytest.mark.asyncio


class TestUpgradeBiome:
    """Tests for upgrade_biome endpoint — behavioral tests with real DB."""

    @pytest.mark.asyncio
    async def test_request_returns_202_with_run_id(self, authed_client, db, monkeypatch):
        """POST /api/v1/biomes/{id}/upgrade returns 202 with upgrade_run_id."""
        biome_id = db.biomes.insert(
            name="test-lxc-biome",
            biome_kind="lxc",
            workload_type="container",
            phase="post_deploy",
            registry_url="ghcr.io/penguintechinc/gough/test:v1",
            tenant_id="__default__",
        )
        db.commit()

        for i in range(1, 4):
            node_id = db.nodes.insert(
                name=f"node-{i}",
                state="ready",
                dmi_uuid=f"00000000-0000-0000-0000-00000000000{i}",
                primary_nic_mac=f"aa:bb:cc:dd:ee:0{i}",
                tenant_id="__default__",
            )
            db.commit()
            db.deployments.insert(
                biome_id=biome_id,
                node_id=node_id,
                status="completed",
                phase=1,
            )
            db.commit()

        body = {
            "target_version": "v1.1.0",
            "rollout_plan": {"batch_size": 2},
        }

        # Mock background task to prevent it from running
        captured = {}
        def fake_add_background_task(fn, *args, **kwargs):
            captured["fn"] = fn
            captured["args"] = args
            captured["kwargs"] = kwargs

        import app.api.biomes as biomes_mod
        monkeypatch.setattr(biomes_mod, "current_app", MagicMock(
            add_background_task=fake_add_background_task
        ))

        response = await authed_client.post(
            f"/api/v1/biomes/{biome_id}/upgrade",
            json=body,
        )

        assert response.status_code == 202  # 1
        data = await response.get_json()
        assert data["data"]["upgrade_run_id"]  # 2
        assert data["data"]["biome_id"] == biome_id  # 3
        assert data["data"]["target_version"] == "v1.1.0"  # 4
        assert data["data"]["status"] == "pending"  # 5

        run_id = data["data"]["upgrade_run_id"]
        run = db(db.upgrade_runs.id == run_id).select().first()
        assert run is not None  # 6
        assert run.biome_id == biome_id  # 7
        assert run.status == "pending"  # 8
        assert run.phase == "canary"  # 9

    @pytest.mark.asyncio
    async def test_k8s_biome_requires_approval_token(self, authed_client, db):
        """POST k8s/storage upgrade without approval_token → 422."""
        biome_id = db.biomes.insert(
            name="test-k8s-biome",
            biome_kind="k8s",
            workload_type="kubernetes",
            phase="post_deploy",
            registry_url="ghcr.io/penguintechinc/gough/k8s:v1",
            tenant_id="__default__",
        )
        db.commit()

        body = {
            "target_version": "v1.1.0",
            "rollout_plan": {"batch_size": 2},
        }

        with patch("app.api.biomes._user_has_scope", return_value=True):
            response = await authed_client.post(
                f"/api/v1/biomes/{biome_id}/upgrade",
                json=body,
            )

        assert response.status_code == 422  # 1
        data = await response.get_json()
        assert data["error"]["code"] == "validation_failed"  # 2

    @pytest.mark.asyncio
    async def test_k8s_biome_requires_admin_scope(self, authed_viewer_client, db):
        """POST k8s upgrade with non-admin token → 403."""
        biome_id = db.biomes.insert(
            name="test-k8s-biome",
            biome_kind="k8s",
            workload_type="kubernetes",
            phase="post_deploy",
            registry_url="ghcr.io/penguintechinc/gough/k8s:v1",
            tenant_id="__default__",
        )
        db.commit()

        body = {
            "target_version": "v1.1.0",
            "rollout_plan": {"batch_size": 2},
            "approval_token": "test-token-abc123",
        }

        with patch("app.api.biomes._user_has_scope", return_value=False):
            response = await authed_viewer_client.post(
                f"/api/v1/biomes/{biome_id}/upgrade",
                json=body,
            )

        assert response.status_code == 403  # 1
        data = await response.get_json()
        assert data["error"]["code"] == "forbidden_scope"  # 2

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — Quart background-task patching + DB schema mirror needed")
    @pytest.mark.asyncio
    async def test_canary_success_progresses_to_batched(self, db):
        """Canary success → status=completed, phase=done."""
        biome_id = db.biomes.insert(
            name="test-lxc",
            biome_kind="lxc",
            workload_type="container",
            phase="post_deploy",
            registry_url="ghcr.io/penguintechinc/gough/test:v1",
            tenant_id="__default__",
        )
        db.commit()

        for i in range(1, 4):
            node_id = db.nodes.insert(
                name=f"node-{i}",
                state="ready",
                dmi_uuid=f"00000000-0000-0000-0000-00000000000{i}",
                primary_nic_mac=f"aa:bb:cc:dd:ee:0{i}",
                tenant_id="__default__",
            )
            db.commit()
            db.deployments.insert(biome_id=biome_id, node_id=node_id, status="completed")
            db.commit()

        run_id = "test-run-123"
        db.upgrade_runs.insert(
            id=run_id,
            biome_id=biome_id,
            target_version="v1.1.0",
            cluster_id="default",
            status="pending",
            phase="canary",
            nodes_total=0,
            nodes_completed=0,
            nodes_failed=0,
            actor_sub="test-user",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        with patch("app.api.biomes._deploy_and_health_check", new_callable=AsyncMock) as mock_deploy:
            mock_deploy.return_value = True
            with patch("app.api.biomes.get_db", return_value=db):
                from app.api.biomes import _execute_upgrade_orchestration
                import asyncio
                await _execute_upgrade_orchestration(
                    biome_id=biome_id,
                    run_id=run_id,
                    target_version="v1.1.0",
                    rollout_plan={"batch_size": 2},
                )

        run = db(db.upgrade_runs.id == run_id).select().first()
        assert run.status == "completed"  # 1
        assert run.phase == "done"  # 2
        assert run.nodes_completed == 3  # 3

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — Quart background-task patching + DB schema mirror needed")
    @pytest.mark.asyncio
    async def test_canary_failure_rolls_back(self, db):
        """Canary failure → status=rolled_back, phase=canary."""
        biome_id = db.biomes.insert(
            name="test-lxc",
            biome_kind="lxc",
            workload_type="container",
            phase="post_deploy",
            registry_url="ghcr.io/penguintechinc/gough/test:v1",
            tenant_id="__default__",
        )
        db.commit()

        for i in range(1, 4):
            node_id = db.nodes.insert(
                name=f"node-{i}",
                state="ready",
                dmi_uuid=f"00000000-0000-0000-0000-00000000000{i}",
                primary_nic_mac=f"aa:bb:cc:dd:ee:0{i}",
                tenant_id="__default__",
            )
            db.commit()
            db.deployments.insert(biome_id=biome_id, node_id=node_id, status="completed")
            db.commit()

        run_id = "test-run-fail-canary"
        db.upgrade_runs.insert(
            id=run_id,
            biome_id=biome_id,
            target_version="v1.1.0",
            cluster_id="default",
            status="pending",
            phase="canary",
            nodes_total=0,
            nodes_completed=0,
            nodes_failed=0,
            actor_sub="test-user",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        with patch("app.api.biomes._deploy_and_health_check", new_callable=AsyncMock) as mock_deploy:
            mock_deploy.return_value = False
            with patch("app.api.biomes.get_db", return_value=db):
                from app.api.biomes import _execute_upgrade_orchestration
                await _execute_upgrade_orchestration(
                    biome_id=biome_id,
                    run_id=run_id,
                    target_version="v1.1.0",
                    rollout_plan={"batch_size": 2},
                )

        run = db(db.upgrade_runs.id == run_id).select().first()
        assert run.status == "rolled_back"  # 1
        assert run.phase == "canary"  # 2
        assert run.rollback_reason == "canary_failed"  # 3

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — Quart background-task patching + DB schema mirror needed")
    @pytest.mark.asyncio
    async def test_batched_failure_marks_failed(self, db):
        """Batched phase failure → status=failed, phase=batched."""
        biome_id = db.biomes.insert(
            name="test-lxc",
            biome_kind="lxc",
            workload_type="container",
            phase="post_deploy",
            registry_url="ghcr.io/penguintechinc/gough/test:v1",
            tenant_id="__default__",
        )
        db.commit()

        for i in range(1, 4):
            node_id = db.nodes.insert(
                name=f"node-{i}",
                state="ready",
                dmi_uuid=f"00000000-0000-0000-0000-00000000000{i}",
                primary_nic_mac=f"aa:bb:cc:dd:ee:0{i}",
                tenant_id="__default__",
            )
            db.commit()
            db.deployments.insert(biome_id=biome_id, node_id=node_id, status="completed")
            db.commit()

        run_id = "test-run-fail-batch"
        db.upgrade_runs.insert(
            id=run_id,
            biome_id=biome_id,
            target_version="v1.1.0",
            cluster_id="default",
            status="pending",
            phase="canary",
            nodes_total=0,
            nodes_completed=0,
            nodes_failed=0,
            actor_sub="test-user",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        call_count = [0]

        async def side_effect_deploy(*args, **kwargs):
            call_count[0] += 1
            return call_count[0] == 1

        with patch("app.api.biomes._deploy_and_health_check", side_effect=side_effect_deploy):
            with patch("app.api.biomes.get_db", return_value=db):
                from app.api.biomes import _execute_upgrade_orchestration
                await _execute_upgrade_orchestration(
                    biome_id=biome_id,
                    run_id=run_id,
                    target_version="v1.1.0",
                    rollout_plan={"batch_size": 2},
                )

        run = db(db.upgrade_runs.id == run_id).select().first()
        assert run.status == "failed"  # 1
        assert run.phase == "batched"  # 2
        assert run.rollback_reason == "batched_phase_failed"  # 3

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — Quart background-task patching + DB schema mirror needed")
    @pytest.mark.asyncio
    async def test_batch_size_honored(self, db):
        """Batch size from rollout_plan is respected."""
        biome_id = db.biomes.insert(
            name="test-lxc",
            biome_kind="lxc",
            workload_type="container",
            phase="post_deploy",
            registry_url="ghcr.io/penguintechinc/gough/test:v1",
            tenant_id="__default__",
        )
        db.commit()

        for i in range(1, 6):
            node_id = db.nodes.insert(
                name=f"node-{i}",
                state="ready",
                dmi_uuid=f"00000000-0000-0000-0000-00000000000{i}",
                primary_nic_mac=f"aa:bb:cc:dd:ee:0{i}",
                tenant_id="__default__",
            )
            db.commit()
            db.deployments.insert(biome_id=biome_id, node_id=node_id, status="completed")
            db.commit()

        run_id = "test-run-batch-size"
        db.upgrade_runs.insert(
            id=run_id,
            biome_id=biome_id,
            target_version="v1.1.0",
            cluster_id="default",
            status="pending",
            phase="canary",
            nodes_total=0,
            nodes_completed=0,
            nodes_failed=0,
            actor_sub="test-user",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        node_batches = []

        async def capture_deploy(nodes, *args, **kwargs):
            node_batches.append(nodes)
            return True

        with patch("app.api.biomes._deploy_and_health_check", side_effect=capture_deploy):
            with patch("app.api.biomes.get_db", return_value=db):
                from app.api.biomes import _execute_upgrade_orchestration
                await _execute_upgrade_orchestration(
                    biome_id=biome_id,
                    run_id=run_id,
                    target_version="v1.1.0",
                    rollout_plan={"batch_size": 2},
                )

        assert len(node_batches) == 3  # 1
        assert len(node_batches[0]) == 1  # 2 - canary
        assert len(node_batches[1]) == 2  # 3 - batch
        assert len(node_batches[2]) == 2  # 4 - batch

    @pytest.mark.skip(reason="gh-16: Phase 3 test harness rework — Quart background-task patching + DB schema mirror needed")
    @pytest.mark.asyncio
    async def test_get_upgrade_run_returns_row(self, authed_client, db):
        """GET /api/v1/biomes/{id}/upgrade-runs/{run_id} returns 200."""
        biome_id = db.biomes.insert(
            name="test-lxc",
            biome_kind="lxc",
            workload_type="container",
            phase="post_deploy",
            registry_url="ghcr.io/penguintechinc/gough/test:v1",
            tenant_id="__default__",
        )
        db.commit()

        run_id = "test-run-get"
        db.upgrade_runs.insert(
            id=run_id,
            biome_id=biome_id,
            target_version="v1.1.0",
            cluster_id="default",
            status="completed",
            phase="done",
            nodes_total=3,
            nodes_completed=3,
            nodes_failed=0,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            actor_sub="test-user",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.commit()

        with patch("app.api.biomes._user_has_scope", return_value=True):
            response = await authed_client.get(
                f"/api/v1/biomes/{biome_id}/upgrade-runs/{run_id}",
            )

        assert response.status_code == 200  # 1
        data = await response.get_json()
        assert data["data"]["id"] == run_id  # 2
        assert data["data"]["status"] == "completed"  # 3
        assert data["data"]["phase"] == "done"  # 4

    @pytest.mark.asyncio
    async def test_get_upgrade_run_404_unknown(self, authed_client, db):
        """GET unknown upgrade_run → 404."""
        biome_id = db.biomes.insert(
            name="test-lxc",
            biome_kind="lxc",
            workload_type="container",
            phase="post_deploy",
            registry_url="ghcr.io/penguintechinc/gough/test:v1",
            tenant_id="__default__",
        )
        db.commit()

        with patch("app.api.biomes._user_has_scope", return_value=True):
            response = await authed_client.get(
                f"/api/v1/biomes/{biome_id}/upgrade-runs/bogus-run-id",
            )

        assert response.status_code == 404  # 1
        data = await response.get_json()
        assert data is not None  # basic validation


class _FakeHealthzResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> "_FakeHealthzResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeClientSession:
    """Stand-in for ``aiohttp.ClientSession`` -- no real network calls."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._status = 200

    async def __aenter__(self) -> "_FakeClientSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def get(self, url: str, timeout: int = 2) -> _FakeHealthzResponse:
        return _FakeHealthzResponse(self._status)


class TestDeployAndHealthCheck:
    """``_deploy_and_health_check`` (gh-22): per-node DB lookups in the deploy
    loop AND the 12x-attempt health-poll loop now run via ``run_db()``.
    Exercises those lookups against a real (sqlite) ``db`` fixture --
    subprocess/aiohttp are mocked (no real lxc/kubectl/network calls), but
    the node/biome reads are real DB round-trips through the thread hop.
    """

    @pytest.mark.asyncio
    async def test_deploy_and_healthcheck_succeeds_with_real_node_lookup(
        self, test_client, db, monkeypatch
    ) -> None:
        """# regression: gh-22

        Seeds a real biome + node, deploys, and health-checks -- proves
        both the deploy-loop node lookup and the health-check-loop node
        lookup (previously two separate blocking-inline DB reads) still
        resolve correctly through run_db()/asyncio.to_thread(). Depends on
        ``test_client`` (unused directly) purely to get the ``biomes``
        table defined on the shared ``dal`` instance -- see that fixture.
        """
        import app.api.biomes as biomes_mod

        biome_id = db.biomes.insert(
            name="test-lxc-biome",
            biome_kind="lxc",
            workload_type="container",
            phase="post_deploy",
            tenant_id="__default__",
        )
        node_id = db.nodes.insert(
            name="node-1",
            state="ready",
            dmi_uuid="00000000-0000-0000-0000-000000000001",
            primary_nic_mac="aa:bb:cc:dd:ee:01",
            tenant_id="__default__",
        )
        db.commit()

        monkeypatch.setattr(biomes_mod, "get_db", lambda: db)
        monkeypatch.setattr("subprocess.run", MagicMock())
        monkeypatch.setattr("aiohttp.ClientSession", _FakeClientSession)

        result = await biomes_mod._deploy_and_health_check(
            [str(node_id)], biome_id, "v1.1.0", "test-run-healthcheck"
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_biome_not_found(
        self, test_client, db, monkeypatch
    ) -> None:
        import app.api.biomes as biomes_mod

        monkeypatch.setattr(biomes_mod, "get_db", lambda: db)

        result = await biomes_mod._deploy_and_health_check(
            ["1"], 999999, "v1.1.0", "test-run-missing-biome"
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_node_not_found(
        self, test_client, db, monkeypatch
    ) -> None:
        import app.api.biomes as biomes_mod

        biome_id = db.biomes.insert(
            name="test-lxc-biome-2",
            biome_kind="lxc",
            workload_type="container",
            phase="post_deploy",
            tenant_id="__default__",
        )
        db.commit()

        monkeypatch.setattr(biomes_mod, "get_db", lambda: db)

        result = await biomes_mod._deploy_and_health_check(
            ["999999"], biome_id, "v1.1.0", "test-run-missing-node"
        )

        assert result is False
