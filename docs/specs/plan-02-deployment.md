# Plan 2 — Deployment (Phase 2)

## Goal
Provision bare-metal nodes using cloud-init: network configuration (3-baseline), OS bootstrap, LXD join, and secret escrow. Operators define 3-baseline networking model, nodes self-configure via cloud-init, and join the LXD cluster. Mara can provision 4 nodes in <30 minutes.

## Scope
- [x] Cloud-init based OS provisioning
- [x] Network configuration (Native, Hybrid, Full-Virtual baselines)
- [x] LXD cluster join with token escrow in Vault
- [x] Joiner secret encryption and bootstrap token generation

Out of scope (M2+): Full-Virtual mode routing policies, advanced bonding, custom Netplan rules.

## Data Model
```sql
CREATE TABLE lxd_cluster_members (
  id TEXT PRIMARY KEY,
  node_id TEXT REFERENCES cloud_machines(id),
  cluster_id TEXT,
  join_token TEXT,
  joined_at TIMESTAMP
);
CREATE INDEX idx_lxd_cluster_members_node ON lxd_cluster_members(node_id);

CREATE TABLE joiner_secrets (
  id TEXT PRIMARY KEY,
  node_id TEXT REFERENCES cloud_machines(id),
  rotation_class TEXT,
  encrypted_secret BYTEA,
  key_version INT,
  created_at TIMESTAMP, expires_at TIMESTAMP
);
CREATE INDEX idx_joiner_secrets_node_expires ON joiner_secrets(node_id, expires_at);

CREATE TABLE vault_bootstrap_tokens (
  id TEXT PRIMARY KEY,
  node_id TEXT REFERENCES cloud_machines(id),
  token TEXT,
  used BOOLEAN, used_at TIMESTAMP,
  created_at TIMESTAMP, expires_at TIMESTAMP
);
CREATE INDEX idx_vault_bootstrap_tokens_expires ON vault_bootstrap_tokens(expires_at);
```

## API Surface
**gRPC:**
- `Provisioner.GenerateBootstrapToken(node_id) → BootstrapToken` — issue one-time Vault auth token
- `Provisioner.GenerateJoinerSecret(node_id) → JoinerSecret` — encrypt cluster join credentials
- `Provisioner.FetchCloudInit(node_id, baseline) → CloudInitConfig` — return rendered cloud-init config

**REST:**
- `POST /api/v1/deployments/{id}/provision` — trigger Phase 2 for a node
- `GET /api/v1/machines/{id}/cloud-init` — download cloud-init config
- `POST /api/v1/machines/{id}/lxd/join` — verify node joined cluster

## Metrics & Alerts
- `gough.vault.bootstrap_window_expired` (CRITICAL) — bootstrap token expired before node used it
- `gough.joiner_secrets.decryption_failure` (CRITICAL) — node cannot decrypt join credentials

## Acceptance Criteria
1. Operator triggers provisioning on 4 nodes; each receives cloud-init config, joins LXD cluster within 5 min
2. Bootstrap token expires after 1 hour; if node does not join, operator must regenerate
3. Joiner secrets are encrypted at rest in Vault; if key version changes, reissue to all nodes
4. Operator can verify cluster membership: `gough cluster status`, `lxc cluster list`

## Dependencies
Plan 1 (Discovery) must complete: nodes must be `ready` state before provisioning. All joiner secrets and bootstrap tokens created in Vault (see Plan 4).
