# Plan 3 — Orchestration (Phase 3)

## Goal
Deploy biomes (workload templates) onto provisioned nodes. Built-in catalog (k8s-primary, k8s-worker, nest-agent, longhorn-agent) can be deployed immediately; custom biomes authoring is supported with cosign signature verification. Aki can author and sign a new biome in <30 minutes; Mara can deploy k8s primary + 3 workers in <20 minutes.

## Scope
- [x] Kubernetes control plane bootstrap (k8s-primary biome)
- [x] Kubernetes worker join (k8s-worker biome)
- [x] Built-in biome catalog (4 biomes: k8s-primary, k8s-worker, nest-agent, longhorn-agent)
- [x] Custom biome authoring and publishing (git repos; registry in M2)
- [x] cosign signature verification on biome image references

Out of scope (M2+): Biome migration (WaddleAI integration available for capacity forecast; manual trigger only), persistent biome registry, SBOM attestation enforcement.

## Data Model
```sql
CREATE TABLE biomes (
  id TEXT PRIMARY KEY,
  name TEXT, version TEXT,
  biome_kind TEXT,
  phase TEXT,
  lock_to_host BOOLEAN,
  requires_hardware_tags JSONB,
  emits_joiner_secrets BOOLEAN,
  image_ref TEXT, image_digest TEXT,
  signature_verified BOOLEAN,
  published_at TIMESTAMP
);
CREATE INDEX idx_eggs_kind_phase ON biomes(biome_kind, phase);
CREATE INDEX idx_eggs_published ON biomes(published_at);

CREATE TABLE deployments (
  id TEXT PRIMARY KEY,
  biome_id TEXT REFERENCES biomes(id),
  node_id TEXT REFERENCES cloud_machines(id),
  phase INT,
  status TEXT,
  logs_url TEXT,
  created_at TIMESTAMP, updated_at TIMESTAMP
);
CREATE INDEX idx_deployments_status ON deployments(status);
CREATE INDEX idx_deployments_egg_node ON deployments(biome_id, node_id);
```

## API Surface
**gRPC:**
- `Orchestrator.DeployEgg(biome_id, node_id, config) → Deployment` — schedule biome on node
- `Orchestrator.GetEgg(biome_id) → Biome` — fetch biome metadata + signature status
- `Orchestrator.VerifySignature(biome_id, pubkey) → VerifyResult`

**REST:**
- `GET /api/v1/biomes` — list built-in + published custom biomes
- `GET /api/v1/biomes/{id}` — detail: version, phase, hardware requirements, signature status
- `POST /api/v1/biomes/{id}/deploy` — trigger deployment on node(s)
- `GET /api/v1/deployments` — list all deployments by status
- `GET /api/v1/deployments/{id}` — deployment detail: logs, phase progress

## Metrics & Alerts
- `gough.biomes.signature_verification_failed` (CRITICAL prod / WARNING staging) — biome image signature invalid or missing
- `gough.cluster.quorum_loss` (CRITICAL) — k8s control plane lost majority (during primary deployment)

## Acceptance Criteria
1. Mara deploys k8s-primary to node-1, k8s-worker to nodes 2-4; cluster is operational within 20 min
2. Aki authors custom biome (e.g., monitoring agent), signs with cosign, publishes to git repo
3. Operator verifies biome signature: `gough biome verify myegg:v1.0.0`; deployment blocked if signature invalid in production
4. Operator can view deployment progress: `gough deployment list`, `gough deployment logs <id>`

## Dependencies
Plan 2 (Deployment) must complete: all nodes must be LXD cluster members. Plan 4 (Security) provides cosign verification and SPIRE identity for biomes emitting joiner secrets.
