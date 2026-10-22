# Plan 1 — Discovery (Phase 1)

## Goal
Implement PXE-based hardware discovery and inventory validation. Operators boot new nodes, Gough discovers CPU/RAM/disk/NIC/TPM/SMART status, validates BMC certificates, applies auto-tags, and marks nodes `ready` for Phase 2 provisioning. Mara can discover and tag 4 nodes in <15 minutes.

## Scope
- [x] PXE boot via iPXE
- [x] Hardware inventory collection (CPU, RAM, disks, NICs, TPM, SMART)
- [x] SMART health pre-flight checks
- [x] BMC certificate validation + warning on mismatch
- [x] Auto-tagging by hardware attributes (e.g., `gpu:nvidia`, `cpu:vendor:intel`)

Out of scope (M2+): GPU detection beyond basic tags, persistent SMART trend analysis, multi-product discovery federation.

## Data Model
```sql
CREATE TABLE cloud_machines (
  id TEXT PRIMARY KEY,
  state TEXT,
  phase INT,
  cpu_count INT, memory_mb INT, disk_total_gb BIGINT,
  bmc_ip TEXT, bmc_cert_thumbprint TEXT,
  nic_macs TEXT[], nic_mtu INT,
  smart_status TEXT,
  hardware_tags JSONB,
  created_at TIMESTAMP, updated_at TIMESTAMP
);
CREATE INDEX idx_cloud_machines_state ON cloud_machines(state);
CREATE INDEX idx_cloud_machines_hardware_tags ON cloud_machines USING GIN(hardware_tags);
```

## API Surface
**gRPC (internal):**
- `DiscoveryAgent.Probe(node_id) → InventorySnapshot` — collect hardware, verify SMART, return tags
- `DiscoveryAgent.ValidateBMC(node_id, cert_thumbprint) → ValidationResult`
- `DiscoveryAgent.ApplyTags(node_id, tag_map) → TagsApplied`

**REST (operator-facing):**
- `GET /api/v1/machines` — list all discovered nodes with state/tags
- `GET /api/v1/machines/{id}` — detail view: CPU, RAM, disk, BMC, SMART status
- `POST /api/v1/machines/{id}/probe` — trigger discovery on existing node
- `POST /api/v1/machines/{id}/tags` — manually override auto-tags

## Metrics & Alerts
- `gough.discovery.agent_tunnel_drop` (WARNING) — iPXE tunnel to discovery service lost
- `gough.bmc.certificate_mismatch_detected` (WARNING) — BMC cert thumbprint changed
- `gough.discovery.smart_failure_detected` (CRITICAL) — disk reports SMART failure

## Acceptance Criteria
1. Mara boots 4 new nodes via PXE; each is inventoried and tagged (e.g., `cpu:vendor:intel`, `gpu:none`, `tpm:2.0`) within 2 min per node
2. SMART health preflight blocks provisioning if disk status is `failed`
3. BMC cert mismatch triggers WARNING alert; operator must acknowledge before proceeding
4. Operator can view machine list and detail via WebUI or CLI: `gough node list`, `gough node get node-1`

## Dependencies
None — Discovery is Phase 1 and has no upstream dependencies. All subsequent phases depend on this.
