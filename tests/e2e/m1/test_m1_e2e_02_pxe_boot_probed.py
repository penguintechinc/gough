"""M1 E2E Test 02: Sim node PXE-boots, hits `probed` state."""

import pytest
import time
import uuid


def test_m1_e2e_02_pxe_boot_to_probed(primary_cluster, sim_node_factory, api_client):
    """
    Test: Sim node PXE-boots, hits `probed` state
    Pass criteria:
      - `nodes` row exists with full inventory
      - SMART data captured
      - SPIRE SVID issued to discovery agent
    Failure isolation: Boot completes within 10min
    """
    print("\n[m1-e2e-02] Starting PXE boot test")

    # Launch sim node with default BIOS, healthy SMART
    dmi_uuid = str(uuid.uuid4())
    sim_node = sim_node_factory(
        firmware="bios",
        smart_pattern="healthy",
        dmi_uuid=dmi_uuid
    )

    print(f"[m1-e2e-02] Launched sim node PID {sim_node['pid']}, DMI UUID {dmi_uuid}")

    # Poll for node to reach `probed` state (within 10 min)
    start_time = time.time()
    timeout_s = 600  # 10 minutes

    while time.time() - start_time < timeout_s:
        try:
            # Query nodes endpoint
            resp = api_client.get("/api/v1/nodes", timeout=5)
            assert resp.status_code == 200, f"Failed to list nodes: {resp.text}"

            nodes = resp.json().get("nodes", [])

            # Find the node by DMI UUID
            probed_node = None
            for node in nodes:
                if node.get("dmi_uuid") == dmi_uuid:
                    probed_node = node
                    break

            if probed_node and probed_node.get("state") == "probed":
                print(f"[m1-e2e-02] Node reached 'probed' state after {time.time() - start_time:.1f}s")

                # Validate full inventory
                assert probed_node.get("inventory"), "No inventory captured"
                inv = probed_node["inventory"]

                assert inv.get("cpu_count", 0) > 0, "CPU count not captured"
                assert inv.get("memory_gb", 0) > 0, "Memory not captured"
                assert len(inv.get("disks", [])) >= 3, "Expected 3+ disks (2 dark + 1 system)"

                # Validate SMART data
                disks = inv.get("disks", [])
                for disk in disks:
                    assert disk.get("smart_status"), f"Disk {disk.get('name')} has no SMART status"
                    assert disk["smart_status"] in ["healthy", "warning", "failing"], \
                        f"Invalid SMART status: {disk['smart_status']}"

                # Validate SPIRE SVID issued
                assert probed_node.get("spire_svid_issued"), \
                    "SPIRE has not issued SVID to discovery agent"

                print(f"[m1-e2e-02] Inventory: CPUs={inv.get('cpu_count')}, " \
                      f"RAM={inv.get('memory_gb')}GB, Disks={len(disks)}")
                return  # Test passed

        except Exception as e:
            print(f"[m1-e2e-02] Query failed: {e}")
            time.sleep(2)
            continue

    raise TimeoutError(f"Node did not reach 'probed' state within {timeout_s}s")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
