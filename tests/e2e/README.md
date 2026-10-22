# M1 E2E Test Suite

Comprehensive end-to-end test catalog for Gough M1 milestone (single-primary, single-tenant bootstrap).

## Architecture

### Test Environment

- **Primary**: LXD container on Debian 12 (or GitHub Actions ubuntu-latest)
- **Sim Nodes**: QEMU VMs launched via `dev-tools/node-sim/launch-sim.sh`
- **Network**: Linux bridge (`gough-sim-bridge`) for inter-VM communication

### Test Framework

- **pytest** with fixtures for cluster management
- **playwright** for UI E2E (future)
- **requests** for HTTP API testing
- Timeout: 300s per test (aggregated suite timeout 60 min in CI)

## Test Catalog (16 tests)

### Core Bootstrap & Deployment

| ID | Name | Duration | Real HW | Status |
|---|---|---|---|---|
| m1-e2e-01 | Bootstrap primary (Shamir tier) | 5 min | No | ✓ |
| m1-e2e-02 | PXE boot → probed state | 10 min | Yes* | ✓ |
| m1-e2e-03 | Approve disk plan + biome | <5s | No | ✓ |
| m1-e2e-04 | Reach ready state | 20 min | Yes* | ✓ |

### Error Handling & Validation

| ID | Name | Duration | Real HW | Status |
|---|---|---|---|---|
| m1-e2e-05 | Biome dependency cycle rejection | <1s | No | ✓ |
| m1-e2e-06 | Disk plan collision rejection | <1s | No | ✓ |
| m1-e2e-07 | One-time token replay rejection | <1s | No | ✓ |

### Advanced Features

| ID | Name | Duration | Real HW | Status |
|---|---|---|---|---|
| m1-e2e-08 | Identity conflict detection | 5 min | Yes* | ✓ |
| m1-e2e-09 | SMART warning surfacing | 15 min | Yes* | ✓ |
| m1-e2e-10 | Dark drive toggle | 15 min | No | ✓ |
| m1-e2e-11 | LUKS rekey | 5 min | Yes | ⚠️ |
| m1-e2e-12 | Audit chain integrity | 5 min | No | ✓ |
| m1-e2e-13 | UEFI boot compatibility | 10 min | Yes* | ✓ |
| m1-e2e-14 | IPv6 SLAAC boot | 10 min | Yes* | ✓ |
| m1-e2e-15 | Joiner secret emit | <1s | No | ✓ |
| m1-e2e-16 | Cross-tenant isolation | <1s | No | ✓ |

*Yes* = can run on sim with QEMU; *Yes\** = can run on sim but real HW provides better coverage

## Running Tests

### Local (sim + LXD primary)

```bash
# Requires LXD initialized: lxd init --auto
make test-e2e-sim
```

### CI (GitHub Actions)

```yaml
on:
  pull_request:
  push:
  schedule: '0 2 * * *'  # Nightly
```

Every PR runs the full M1 suite. Nightly runs include lab-cluster tests (if available).

### Filtering Tests

```bash
# Run one test
pytest tests/e2e/m1/test_m1_e2e_01_bootstrap_primary.py -v

# Run by pattern
pytest tests/e2e/m1/ -k "smart" -v

# Skip real-hardware tests on CI
pytest tests/e2e/ -m "not real_hw" -v
```

## Fixtures (conftest.py)

### primary_cluster
- Bootstraps a fresh Gough cluster in LXD
- Yields: `cluster_id`, `api_url`, `admin_token`, config dir
- Scope: session (reused across all tests)

### sim_node_factory
- Factory fixture for launching QEMU VMs
- Params: `firmware` (bios/uefi), `ipv6` (disabled/slaac-only/dhcpv6/dual-stack), `secure_boot`, `dmi_uuid`, `smart_pattern`, `with_bmc`
- Returns: metadata dict with PID, DMI UUID, etc.
- Cleanup: automatic teardown after test

### gough_cli_token
- Returns authenticated API token for admin requests

### api_client
- Pre-configured HTTP client with auth headers
- Methods: `get()`, `post()`, `put()`, `delete()`

## Node-Sim Tools (dev-tools/node-sim/)

### launch-sim.sh
```bash
./launch-sim.sh \
  --firmware bios \
  --ipv6 disabled \
  --dmi-uuid <uuid> \
  --smart-pattern healthy \
  [--with-bmc] \
  [--secure-boot]
```

Outputs: QEMU PID, SMART fixture path, Redfish endpoint (if --with-bmc)

### seed-smart.py
Generates synthetic smartmontools JSON per pattern (healthy/warning/failing).

### mock-redfish.py
Lightweight Redfish HTTP server for QEMU BMC simulation.

### cleanup.sh
Tears down all QEMU VMs and storage in `/tmp/gough-qemu`.

## Pass Criteria

All 16 tests must pass:
- No test timeout
- No assertion failures
- All API responses correct (status, data structure)
- Audit chain verified (no breaks)
- No cross-tenant leaks

## Failure Scenarios

Tests are marked with `@pytest.mark.xfail` if they:
- Require unimplemented APIs (empty stub endpoints)
- Depend on real BMC/IPMI hardware
- Need multi-node quorum (M2+)

## Reports

Test results uploaded to GitHub Actions artifacts:
- `/tmp/e2e-results.log` — full pytest output
- `/tmp/gough-qemu/` — QEMU artifacts (cleaned up post-test)

Nightly lab runs produce:
- Grafana dashboard `E2E Results`
- Per-test timing histogram
- Failure root-cause summary

## Maintenance

### Adding a Test

1. Create `test_m1_e2e_NN_<name>.py` in `tests/e2e/m1/`
2. Use pytest fixture pattern (see existing tests)
3. Follow pass criteria from spec
4. Cleanup any spawned resources (QEMU VMs, files)
5. Document in table above

### Updating Fixtures

Modify `tests/e2e/conftest.py`. Changes to `primary_cluster` rebuild LXD instance; changes to `sim_node_factory` affect all node-spawning tests.

## CI/CD Integration

- **PR gate**: Full M1 suite must pass (`test-e2e-sim`)
- **Merge**: Passing tests required before main merge
- **Nightly**: Extended suite on lab cluster (M2+ tests, chaos scenarios)
- **Schedule**: Run at 2 AM UTC daily

## Known Limitations

- `test_m1_e2e_11_luks_rekey` — requires filesystem encryption (M1.5+)
- `test_m1_e2e_16_cross_tenant_denied` — single-tenant in M1; full multi-tenant in M3
- BMC tests (SMART, Redfish) mock hardware; QEMU TPM not available for Vault unseal tests
- IPv6 tests require IPv6-enabled network in CI (currently IPv4-only on ubuntu-latest)

## See Also

- Spec: `/Users/penguinz/.claude/plans/let-s-create-a-spec-snoopy-nest.md` (section "M1 E2E Test Catalog")
- Node-sim: `dev-tools/node-sim/README.md` (TBD)
- Deployment: `docs/deployment/e2e-setup.md` (TBD)
