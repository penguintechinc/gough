# Gough Platform Specification (M1)

**Status:** M1 (Minimum Viable Product)  
**Version:** 1.1.0  
**Last Updated:** 2026-05-09

This is the canonical specification for Gough. For operational guidance, see user guides and runbooks in `/docs/user-guide/` and `/docs/runbooks/`.

---

## Overview

Gough is a bare-metal provisioning platform for Kubernetes clusters running on LXD. It provides:

- **Phase-1 (Discovery):** PXE-boot hardware inventory, SMART checks, BMC validation
- **Phase-2 (Deployment):** Cloud-init networking, LXD cluster join
- **Phase-3 (Orchestration):** Biome deployment (k8s control plane, workers, storage agents)
- **Management Plane:** Vault (PKI), SPIRE (identity), PostgreSQL (OLTP), Prometheus (metrics), Audit chain (compliance)
- **Operator Interface:** WebUI + CLI (`gough` command)

---

## Seven Personas & Acceptance Tests

See `/docs/specs/platform-spec.md` sections "Personas" (full text in plan file) for detailed day-in-the-life scenarios.

**For M1, acceptance criteria per persona:**

1. **Infra Operator (Mara):** Can rack 4-node expansion, discover hardware, plan disks, deploy biomes in <45 min
2. **SRE (Devon):** Can review capacity forecast, adjust migration policy, run DR drill with <30 min RTO
3. **Platform Dev (Aki):** Can author new biomes, publish with signatures, test in staging CI
4. **Tenant Consumer (Priya):** Can view cluster health, request storage quota, see audit logs for her namespace (M3)
5. **Sales Engineer:** Can provision single-primary lab cluster in <1 hour for demo
6. **Compliance Officer:** Can audit trail verify, export compliance reports, monitor offsite mirror
7. **Enterprise Support:** Can diagnose node failures via CLI, recommend recovery steps

---

## M1 Feature Set

### Phase-1 (Discovery)
- [x] PXE boot via iPXE
- [x] Hardware inventory (CPU, RAM, disks, NICs, TPM, SMART)
- [x] SMART health pre-flight
- [x] BMC certificate validation + warning on mismatch
- [x] Auto-tagging by hardware attributes (e.g., `gpu:nvidia`, `cpu:vendor:intel`)

### Phase-2 (Deployment)
- [x] Cloud-init based provisioning
- [x] Network configuration (3-baseline support: Native, Hybrid, Full-Virtual)
- [x] LXD cluster join (with token escrow in Vault)
- [x] Joiner secret encryption/decryption

### Phase-3 (Orchestration)
- [x] Kubernetes control plane bootstrap (k8s-primary biome)
- [x] Kubernetes worker join (k8s-worker biome)
- [x] Built-in biome catalog (k8s-primary, k8s-worker, nest-agent, longhorn-agent)
- [x] Custom biome authoring support

### Security & Identity
- [x] Vault PKI (CA, intermediate, leaf certs)
- [x] SPIRE SVID issuance + mTLS between services
- [x] JWT OIDC auth for CLI + API
- [x] Joiner secret generation + rotation
- [x] Bootstrap token one-time use enforcement

### Operations
- [x] Audit chain (append-only, offsite mirror)
- [x] Prometheus metrics + Grafana dashboards
- [x] NATS event streaming for cluster state changes
- [x] License enforcement (WaddleAI integration)
- [x] Health checks (liveness + readiness)

### Management
- [x] API Manager (Quart REST + gRPC endpoints)
- [x] WebUI (Machines, Biomes, Deployments, Storage tabs)
- [x] CLI (`gough` command: node, biome, cluster, audit, etc.)
- [x] RBAC (operator, maintainer, viewer roles)

### M1.1 additions (Phase 6)
- [x] HA control-plane frontend (kube-vip default + external LB mode)
- [x] Control-plane node replacement endpoint (`replace_primary`)
- [x] Quorum recovery break-glass endpoint (`force_recover_quorum`)
- [x] Control-plane frontend mode switching (`switch_frontend`)
- [x] iPXE CA rotation endpoint (`rotate_ipxe_ca`)
- [x] Full cluster/migration/biome-upgrade orchestration (no stubs)
- [x] Real Prometheus integration + capacity forecasting
- [x] X.509 chain validation + SPIRE SVID verification
- [x] Integration health probes (WaddleAI, Waddlebot)
- [x] iPXE Phase-2 deploy script rendering
- [x] LUKS sealing tiers (TPM2, TPM2+PIN, network-bound)

### Limitations (M2+)
- [ ] Live Biome migration (WaddleAI capacity forecast available; manual trigger only)
- [ ] Multi-tenant (single operator-scoped cluster in M1)
- [ ] BGP-mode anycast control-plane frontend (M3+; returns 501 Not Implemented)
- [ ] Custom biomes distribution (git repos only; registry forthcoming in M2)
- [ ] Advanced networking (no Cilium Gateway API HTTPRoute; Ingress only)

---

## Data Model (PostgreSQL)

```sql
-- Core inventory
CREATE TABLE cloud_machines (
  id TEXT PRIMARY KEY,                          -- node-1, node-2, ...
  state TEXT,                                   -- new, discovering, probed, provisioning, ready
  phase INT,                                    -- 0 (initial), 1, 2, 3
  cpu_count INT, memory_mb INT, disk_total_gb BIGINT,
  bmc_ip TEXT, bmc_cert_thumbprint TEXT,
  nic_macs TEXT[], nic_mtu INT,
  smart_status TEXT,                            -- passed, warning, failed
  hardware_tags JSONB,                          -- {gpu: "none", cpu: {vendor: "intel"}, ...}
  created_at TIMESTAMP, updated_at TIMESTAMP
);

-- Biomes (workload templates)
CREATE TABLE biomes (
  id TEXT PRIMARY KEY,                          -- k8s-primary, vault-leader, ...
  name TEXT, version TEXT,
  biome_kind TEXT,                                -- infrastructure, application
  phase TEXT,                                   -- post_deploy, always_on, ...
  lock_to_host BOOLEAN,                         -- if true, no live migration
  requires_hardware_tags JSONB,                 -- {tpm: "2.0", ...}
  emits_joiner_secrets BOOLEAN,
  image_ref TEXT, image_digest TEXT,
  signature_verified BOOLEAN,
  published_at TIMESTAMP
);

-- Deployments (Biome → Node assignments)
CREATE TABLE deployments (
  id TEXT PRIMARY KEY,
  biome_id TEXT REFERENCES biomes(id),
  node_id TEXT REFERENCES cloud_machines(id),
  phase INT,                                    -- 1, 2, 3
  status TEXT,                                  -- pending, running, completed, failed
  logs_url TEXT,
  created_at TIMESTAMP, updated_at TIMESTAMP
);

-- LXD cluster membership
CREATE TABLE lxd_cluster_members (
  id TEXT PRIMARY KEY,
  node_id TEXT REFERENCES cloud_machines(id),
  cluster_id TEXT,
  join_token TEXT,                              -- one-time use, expires in 1h
  joined_at TIMESTAMP
);

-- Joiner secrets (encrypted bootstrap secrets for new nodes)
CREATE TABLE joiner_secrets (
  id TEXT PRIMARY KEY,
  node_id TEXT REFERENCES cloud_machines(id),
  rotation_class TEXT,                          -- joiner, intermediate-ca, ...
  encrypted_secret BYTEA,
  key_version INT,
  created_at TIMESTAMP, expires_at TIMESTAMP
);

-- Vault bootstrap tokens
CREATE TABLE vault_bootstrap_tokens (
  id TEXT PRIMARY KEY,
  node_id TEXT REFERENCES cloud_machines(id),
  token TEXT,                                   -- one-time use
  used BOOLEAN, used_at TIMESTAMP,
  created_at TIMESTAMP, expires_at TIMESTAMP
);

-- Audit log (immutable, append-only)
CREATE TABLE audit_log (
  id BIGINT PRIMARY KEY,
  entry_hash TEXT,
  prev_hash TEXT,                               -- chain integrity
  timestamp TIMESTAMP,
  actor TEXT,                                   -- user or service
  action TEXT,                                  -- node_provision, egg_deploy, ...
  resource_type TEXT, resource_id TEXT,
  details JSONB,
  severity TEXT,                                -- info, warning, critical
  created_at TIMESTAMP
);

CREATE TABLE alert_rules (
  id TEXT PRIMARY KEY,
  name TEXT,                                    -- gough.audit.chain_integrity_failure
  severity TEXT,                                -- critical, warning, info
  threshold NUMERIC,
  duration_seconds INT,
  action TEXT                                   -- alert, escalate, page
);
```

---

## API Design

**gRPC (internal, service-to-service):**
- Port 50051
- Methods: Provision, GetStatus, DeployEgg, MigrateEgg, etc.
- mTLS (SPIRE SVID enforced)

**REST (external, client-facing):**
- Port 8080 (Quart)
- OpenAPI 3.1 spec at `/api/v1/openapi.yaml`
- JWT auth (OIDC `sub`, `aud`, `iss` claims)
- Versioning: `/api/v{major}/endpoint`
- Response format: `{ status, data, meta: { version, timestamp } }`

---

## Metrics (Prometheus)

**Alerts emitted (referenced in `/docs/runbooks/`):**

- `gough.audit.chain_integrity_failure` (CRITICAL)
- `gough.vault.bootstrap_window_expired` (CRITICAL)
- `gough.spiffe.svid_rotation_grace_exceeded` (CRITICAL)
- `gough.joiner_secrets.decryption_failure` (CRITICAL)
- `gough.bmc.certificate_mismatch_detected` (WARNING)
- `gough.biomes.signature_verification_failed` (CRITICAL prod / WARNING staging)
- `gough.cluster.quorum_loss` (CRITICAL)
- `gough.audit.offsite_mirror_lag` (WARNING)
- `gough.security.otp_replay_detected` (WARNING)
- `gough.discovery.agent_tunnel_drop` (WARNING)
- (... and 15 more; see `/docs/runbooks/` directory)

---

## Deployment Architecture

- **Primary:** Kubernetes namespace `gough`
- **HA:** 3-5 control plane nodes (etcd consensus)
- **Storage:** Longhorn or Ceph (or Nest distributed storage)
- **Networking:** 3-baseline support (Native, Hybrid, Full-Virtual)
- **Backup:** Daily snapshots to offsite mirror (RPO 5 min, RTO 30 min target)

---

## Security

- **AuthN:** JWT OIDC (Vault-issued or external provider)
- **AuthZ:** RBAC (operator, maintainer, viewer roles; team-scoped in M3)
- **Transport:** TLS 1.2+ (SPIRE mTLS between services)
- **Secrets:** Vault (PKI, joiner secrets, bootstrap tokens)
- **Audit:** Append-only chain with offsite mirror + NATS event stream
- **Supply Chain:** cosign signatures on all biomes; SBOM generated
- **License:** Domain-based enforcement for enterprise features

---

## See Also

- `/docs/user-guide/day-one.md` — First 30 minutes
- `/docs/user-guide/day-two.md` — Weekly/monthly operations
- `/docs/runbooks/` — 28 failure-mode runbooks (CRITICAL, WARNING, INFO)
- `/docs/architecture/overview.md` — System architecture
