# Plan 6 — Data Model & Persistence

## Goal
Define all PostgreSQL tables, indexes, constraints, and per-table access patterns. Alembic migration strategy ensures schema init and evolution under operator control (no auto-migration on startup). Per-service DB accounts grant scoped permissions (read-only, read-write, scoped RLS, admin).

## Scope
- [x] Complete PostgreSQL schema (all 7 core tables)
- [x] Primary + secondary indexes optimized for access patterns
- [x] Per-service database accounts (API Manager, provisioning agents, audit service)
- [x] Alembic baseline + migration examples
- [x] Constraint enforcement (FK, NOT NULL, UNIQUE, CHECK)

Out of scope (M2+): Partitioning by date, read replicas, multi-database federation.

## Data Model (Complete Schema)
```sql
CREATE TABLE cloud_machines (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL DEFAULT 'new',
  phase INT NOT NULL DEFAULT 0,
  cpu_count INT, memory_mb INT, disk_total_gb BIGINT,
  bmc_ip TEXT, bmc_cert_thumbprint TEXT,
  nic_macs TEXT[], nic_mtu INT,
  smart_status TEXT DEFAULT 'unknown',
  hardware_tags JSONB DEFAULT '{}',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_machines_state ON cloud_machines(state);
CREATE INDEX idx_machines_tags ON cloud_machines USING GIN(hardware_tags);
CREATE INDEX idx_machines_updated ON cloud_machines(updated_at);

CREATE TABLE biomes (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL, version TEXT NOT NULL,
  biome_kind TEXT NOT NULL,
  phase TEXT NOT NULL,
  lock_to_host BOOLEAN DEFAULT FALSE,
  requires_hardware_tags JSONB DEFAULT '{}',
  emits_joiner_secrets BOOLEAN DEFAULT FALSE,
  image_ref TEXT NOT NULL, image_digest TEXT,
  signature_verified BOOLEAN DEFAULT FALSE,
  published_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX idx_eggs_name_version ON biomes(name, version);
CREATE INDEX idx_eggs_kind_phase ON biomes(biome_kind, phase);

CREATE TABLE deployments (
  id TEXT PRIMARY KEY,
  biome_id TEXT NOT NULL REFERENCES biomes(id),
  node_id TEXT NOT NULL REFERENCES cloud_machines(id),
  phase INT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  logs_url TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_deployments_status ON deployments(status);
CREATE INDEX idx_deployments_egg ON deployments(biome_id);
CREATE INDEX idx_deployments_node ON deployments(node_id);

CREATE TABLE lxd_cluster_members (
  id TEXT PRIMARY KEY,
  node_id TEXT NOT NULL UNIQUE REFERENCES cloud_machines(id),
  cluster_id TEXT NOT NULL,
  join_token TEXT,
  joined_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_lxd_cluster_node ON lxd_cluster_members(node_id);

CREATE TABLE joiner_secrets (
  id TEXT PRIMARY KEY,
  node_id TEXT NOT NULL REFERENCES cloud_machines(id),
  rotation_class TEXT NOT NULL,
  encrypted_secret BYTEA NOT NULL,
  key_version INT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_joiner_node_expires ON joiner_secrets(node_id, expires_at);
CREATE INDEX idx_joiner_rotation_class ON joiner_secrets(rotation_class);

CREATE TABLE vault_bootstrap_tokens (
  id TEXT PRIMARY KEY,
  node_id TEXT NOT NULL REFERENCES cloud_machines(id),
  token TEXT NOT NULL,
  used BOOLEAN DEFAULT FALSE, used_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_bootstrap_expires ON vault_bootstrap_tokens(expires_at);
CREATE INDEX idx_bootstrap_node_used ON vault_bootstrap_tokens(node_id, used);

CREATE TABLE audit_log (
  id BIGINT PRIMARY KEY,
  entry_hash TEXT NOT NULL,
  prev_hash TEXT,
  timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  resource_type TEXT, resource_id TEXT,
  details JSONB DEFAULT '{}',
  severity TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_audit_actor_time ON audit_log(actor, timestamp DESC);
CREATE INDEX idx_audit_resource ON audit_log(resource_type, resource_id);
CREATE INDEX idx_audit_severity ON audit_log(severity);
```

## Per-Service Database Accounts

**api-manager (read-write):** SELECT, INSERT, UPDATE, DELETE on all tables (except audit_log: INSERT only).
**provisioning-agent (read-write):** SELECT, UPDATE on cloud_machines, deployments; INSERT on joiner_secrets, vault_bootstrap_tokens.
**audit-service (write-only):** INSERT on audit_log only.
**analytics-service (read-only):** SELECT on cloud_machines, deployments, audit_log.

## Alembic Strategy

- **Baseline:** `alembic revision --autogenerate -m "Initial schema"` (one-time, checked in)
- **Migrations:** Each schema change: `alembic revision --autogenerate -m "description"` → committed to git
- **Deployment:** Manual operator action or K8s Job (never auto-run in app startup)
- **Rollback:** `alembic downgrade <revision>` — test in staging first

## Acceptance Criteria
1. Schema initializes via `alembic upgrade head` (or `create_all()` for greenfield); all tables present with correct column types/constraints
2. Indexes present for foreign keys and frequently filtered columns (state, timestamp, resource_id)
3. Per-service accounts created with minimal grants; audit logs show account usage
4. Alembic baseline checked in; new migrations can be applied/rolled back without data loss
5. Operator can verify schema: `psql -c "\\dt" gough_db` shows all 7 tables

## Dependencies
Alembic initialized as part of project bootstrap. Per-service accounts created as part of K8s Helm deployment (Plan 5 API setup). All previous plans (1-5) depend on schema completion.
