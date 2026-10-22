"""M1 E2E Test 01: Bootstrap fresh primary via gough init (Shamir tier)."""

import pytest
import time
import requests


def test_m1_e2e_01_bootstrap_primary(primary_cluster, api_client):
    """
    Test: Bootstrap fresh primary via `gough init` (Shamir tier)
    Pass criteria:
      - `gough cluster status` returns healthy
      - SPIRE issues SVIDs to all services
      - Vault unsealed
    Failure isolation: All bootstrap services started within 5min
    """
    print(f"\n[m1-e2e-01] Starting bootstrap test for cluster {primary_cluster['cluster_id']}")

    # Wait for cluster to be fully ready (all services healthy)
    start_time = time.time()
    timeout_s = 300  # 5 minutes

    while time.time() - start_time < timeout_s:
        try:
            # Check cluster status endpoint
            resp = api_client.get("/api/v1/cluster/status", timeout=5)
            if resp.status_code == 200:
                status = resp.json()

                # Validate cluster is healthy
                assert status.get("cluster_state") == "healthy", \
                    f"Cluster state is {status.get('cluster_state')}, expected 'healthy'"

                # Validate all required services are up
                required_services = [
                    "api-manager",
                    "discovery-agent",
                    "vault",
                    "spire-server"
                ]

                services_status = status.get("services", {})
                for svc in required_services:
                    assert svc in services_status, f"Service {svc} not found in status"
                    assert services_status[svc].get("healthy"), \
                        f"Service {svc} is not healthy: {services_status[svc]}"

                # Validate Vault is unsealed
                vault_status = services_status.get("vault", {})
                assert not vault_status.get("sealed"), "Vault is still sealed"

                # Validate SPIRE is issuing SVIDs
                spire_status = services_status.get("spire-server", {})
                assert spire_status.get("svid_issued_count", 0) > 0, \
                    "SPIRE has not issued any SVIDs yet"

                print(f"[m1-e2e-01] Cluster healthy after {time.time() - start_time:.1f}s")
                print(f"[m1-e2e-01] Services status: {services_status}")
                return  # Test passed

        except requests.RequestException as e:
            print(f"[m1-e2e-01] API request failed: {e}")
            time.sleep(1)
            continue

    raise TimeoutError(
        f"Primary cluster did not reach healthy state within {timeout_s}s"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
