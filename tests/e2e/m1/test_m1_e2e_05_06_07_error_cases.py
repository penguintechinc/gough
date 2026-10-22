"""M1 E2E Tests 05-07: Error case validation."""

import pytest
import time
import uuid


def test_m1_e2e_05_egg_dependency_cycle(primary_cluster, api_client):
    """
    Test 05: Biome dependency cycle rejected at plan-compile
    Pass criteria:
      - Plan POST returns 422 with `cycle` field
    Failure isolation: Zero changes made on target
    """
    print("\n[m1-e2e-05] Starting biome dependency cycle test")

    # Create a node first
    node_id = "test-node-05"

    # Submit a plan with cyclic biome dependencies
    # (biome A depends on B, B depends on C, C depends on A)
    cyclic_plan = {
        "node_id": node_id,
        "biomes": [
            {"name": "biome-a", "depends_on": ["biome-b"]},
            {"name": "biome-b", "depends_on": ["biome-c"]},
            {"name": "biome-c", "depends_on": ["biome-a"]}
        ]
    }

    resp = api_client.post("/api/v1/plans", data=cyclic_plan, timeout=10)

    assert resp.status_code == 422, \
        f"Expected 422, got {resp.status_code}"

    error = resp.json()
    assert error.get("error") == "dependency_cycle" or "cycle" in error.get("details", {}), \
        f"Error does not indicate cycle: {error}"

    print(f"[m1-e2e-05] Correctly rejected cyclic plan: {error}")


def test_m1_e2e_06_disk_plan_collision(primary_cluster, api_client):
    """
    Test 06: Disk plan `write_files` collision rejected
    Pass criteria:
      - Plan POST returns 422 with conflicting path
    Failure isolation: Zero changes made on target
    """
    print("\n[m1-e2e-06] Starting disk plan collision test")

    node_id = "test-node-06"

    # Submit a plan with conflicting write_files paths
    collision_plan = {
        "node_id": node_id,
        "write_files": [
            {"path": "/etc/gough/config.yaml", "content": "config1"},
            {"path": "/etc/gough/config.yaml", "content": "config2"}  # Collision!
        ]
    }

    resp = api_client.post("/api/v1/plans", data=collision_plan, timeout=10)

    assert resp.status_code == 422, \
        f"Expected 422, got {resp.status_code}"

    error = resp.json()
    assert "collision" in error.get("error", "").lower() or \
           "collision" in str(error.get("details", "")), \
        f"Error does not indicate collision: {error}"

    print(f"[m1-e2e-06] Correctly rejected collision plan: {error}")


def test_m1_e2e_07_one_time_token_replay(primary_cluster, api_client):
    """
    Test 07: One-time bootstrap token replay rejected
    Pass criteria:
      - Second use returns 409
      - Nonce reject counter increments
    Failure isolation: First use succeeds
    """
    print("\n[m1-e2e-07] Starting one-time token replay test")

    # Create a one-time bootstrap token
    token_resp = api_client.post("/api/v1/bootstrap/tokens", data={}, timeout=10)
    assert token_resp.status_code == 201

    token_data = token_resp.json()
    token = token_data.get("token")
    nonce_id = token_data.get("nonce_id")

    print(f"[m1-e2e-07] Created token with nonce {nonce_id}")

    # First use should succeed
    first_use = api_client.post(
        "/api/v1/bootstrap/exchange",
        data={"token": token, "node_id": "test-node-07"},
        timeout=10
    )

    assert first_use.status_code == 200, \
        f"First use failed: {first_use.status_code}"

    print(f"[m1-e2e-07] Token first use succeeded")

    # Second use should fail with 409
    second_use = api_client.post(
        "/api/v1/bootstrap/exchange",
        data={"token": token, "node_id": "test-node-07-2"},
        timeout=10
    )

    assert second_use.status_code == 409, \
        f"Expected 409 on replay, got {second_use.status_code}"

    error = second_use.json()
    assert error.get("error") == "token_already_used", \
        f"Expected token_already_used error: {error}"

    # Verify reject counter incremented
    nonce_status = api_client.get(f"/api/v1/bootstrap/nonces/{nonce_id}", timeout=5)
    assert nonce_status.status_code == 200

    nonce = nonce_status.json()
    assert nonce.get("reject_count", 0) >= 1, "Reject counter did not increment"

    print(f"[m1-e2e-07] Token replay correctly rejected, reject_count={nonce.get('reject_count')}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
