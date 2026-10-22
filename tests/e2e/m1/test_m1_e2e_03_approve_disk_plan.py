"""M1 E2E Test 03: Operator approves disk plan + k8s-primary biome via API."""

import pytest
import time


def test_m1_e2e_03_approve_disk_plan(primary_cluster, sim_node_factory, api_client):
    """
    Test: Operator approves disk plan + `k8s-primary` biome via API
    Pass criteria:
      - Phase-2 deploy starts
      - `nodes.state = deploying`
    Failure isolation: Plan compilation < 5s
    """
    print("\n[m1-e2e-03] Starting disk plan approval test")

    import uuid
    dmi_uuid = str(uuid.uuid4())

    # First get a node into probed state
    sim_node = sim_node_factory(firmware="bios", smart_pattern="healthy", dmi_uuid=dmi_uuid)

    # Wait for node to be probed (reuse logic from test 02)
    max_retries = 300
    for i in range(max_retries):
        resp = api_client.get("/api/v1/nodes", timeout=5)
        if resp.status_code == 200:
            nodes = resp.json().get("nodes", [])
            probed_node = next((n for n in nodes if n.get("dmi_uuid") == dmi_uuid), None)

            if probed_node and probed_node.get("state") == "probed":
                node_id = probed_node.get("id")
                print(f"[m1-e2e-03] Node {node_id} is probed")
                break

        time.sleep(1)
    else:
        raise TimeoutError("Node did not reach probed state in time")

    # Post a disk plan for this node
    print(f"[m1-e2e-03] Submitting disk plan for node {node_id}")

    disk_plan = {
        "node_id": node_id,
        "disks": [
            {
                "serial": "DISK-SYSTEM-001",
                "action": "format_and_deploy",
                "partition_scheme": "gpt",
                "filesystem": "ext4"
            }
        ]
    }

    plan_start = time.time()
    resp = api_client.post("/api/v1/plans", data=disk_plan, timeout=10)

    plan_compile_time = time.time() - plan_start
    print(f"[m1-e2e-03] Plan compiled in {plan_compile_time:.2f}s")

    assert plan_compile_time < 5, \
        f"Plan compilation took {plan_compile_time:.2f}s, expected < 5s"

    assert resp.status_code == 201, \
        f"Plan POST failed: {resp.status_code} {resp.text}"

    plan = resp.json()
    plan_id = plan.get("id")
    print(f"[m1-e2e-03] Plan created: {plan_id}")

    # Approve the plan
    print(f"[m1-e2e-03] Approving plan {plan_id}")

    approve_resp = api_client.post(
        f"/api/v1/plans/{plan_id}/approve",
        data={"force": False},
        timeout=10
    )

    assert approve_resp.status_code == 200, \
        f"Plan approval failed: {approve_resp.status_code} {approve_resp.text}"

    print(f"[m1-e2e-03] Plan approved")

    # Verify node state is now `deploying`
    resp = api_client.get(f"/api/v1/nodes/{node_id}", timeout=5)
    assert resp.status_code == 200

    node = resp.json()
    assert node.get("state") == "deploying", \
        f"Node state is {node.get('state')}, expected 'deploying'"

    print(f"[m1-e2e-03] Node state transitioned to 'deploying'")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
