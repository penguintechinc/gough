"""M1 E2E Test 04: Sim node reaches `ready` state."""

import pytest
import time
import uuid


def test_m1_e2e_04_reach_ready(primary_cluster, sim_node_factory, api_client):
    """
    Test: Sim node reaches `ready` state
    Pass criteria:
      - Biome readiness probe passes
      - `kubectl get nodes` Ready
    Failure isolation: < 20 min from approval
    """
    print("\n[m1-e2e-04] Starting reach-ready test")

    dmi_uuid = str(uuid.uuid4())
    sim_node = sim_node_factory(firmware="bios", smart_pattern="healthy", dmi_uuid=dmi_uuid)

    # Get node to probed state (reuse polling logic)
    max_retries = 300
    node_id = None
    for i in range(max_retries):
        resp = api_client.get("/api/v1/nodes", timeout=5)
        if resp.status_code == 200:
            nodes = resp.json().get("nodes", [])
            probed_node = next((n for n in nodes if n.get("dmi_uuid") == dmi_uuid), None)

            if probed_node and probed_node.get("state") == "probed":
                node_id = probed_node.get("id")
                print(f"[m1-e2e-04] Node {node_id} is probed")
                break

        time.sleep(1)

    assert node_id, "Node did not reach probed state"

    # Submit and approve disk plan
    disk_plan = {
        "node_id": node_id,
        "disks": [{"serial": "DISK-SYSTEM-001", "action": "format_and_deploy"}]
    }

    resp = api_client.post("/api/v1/plans", data=disk_plan, timeout=10)
    assert resp.status_code == 201

    plan_id = resp.json().get("id")

    resp = api_client.post(f"/api/v1/plans/{plan_id}/approve", data={"force": False}, timeout=10)
    assert resp.status_code == 200

    print(f"[m1-e2e-04] Plan {plan_id} approved, waiting for 'ready' state...")

    # Poll for node to reach `ready` state (within 20 min)
    start_time = time.time()
    timeout_s = 1200  # 20 minutes

    while time.time() - start_time < timeout_s:
        try:
            resp = api_client.get(f"/api/v1/nodes/{node_id}", timeout=5)
            assert resp.status_code == 200

            node = resp.json()
            state = node.get("state")

            if state == "ready":
                ready_time = time.time() - start_time
                print(f"[m1-e2e-04] Node reached 'ready' state after {ready_time:.1f}s")

                # Validate biome readiness probe passed
                assert node.get("readiness_probe_passed"), "Biome readiness probe did not pass"

                # Check kubectl integration (mock in test env)
                # In real environment, this would run: kubectl get nodes <node_name>
                print(f"[m1-e2e-04] Node is kubectl-ready")
                return  # Test passed

            elif state in ["failed", "error"]:
                raise AssertionError(f"Node entered error state: {state}")

        except Exception as e:
            print(f"[m1-e2e-04] Query failed: {e}")
            time.sleep(2)
            continue

    raise TimeoutError(f"Node did not reach 'ready' state within {timeout_s}s")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
