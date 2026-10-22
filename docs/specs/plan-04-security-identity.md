# Plan 4 — Security & Identity

## Goal
Establish PKI (Vault CA/intermediate/leaf certs), SPIRE SVID issuance, JWT OIDC auth, and audit chain (append-only, offsite mirror). Services authenticate via mTLS; operators/consumers via JWT. All actions logged with chain integrity verification and offsite mirror for compliance.

## Scope
- [x] Vault PKI (CA, intermediate, leaf certificates)
- [x] SPIRE SVID issuance + mTLS between services
- [x] JWT OIDC auth for CLI + API
- [x] Joiner secret generation, rotation, one-time use enforcement
- [x] Audit chain (append-only, hash-chained, offsite mirror via NATS)

Out of scope (M2+): Multi-tenant audit isolation, HA Vault, FIPS certification.

## Data Model
```sql
CREATE TABLE joiner_secrets (
  id TEXT PRIMARY KEY,
  node_id TEXT REFERENCES cloud_machines(id),
  rotation_class TEXT,
  encrypted_secret BYTEA,
  key_version INT,
  created_at TIMESTAMP, expires_at TIMESTAMP
);

CREATE TABLE vault_bootstrap_tokens (
  id TEXT PRIMARY KEY,
  node_id TEXT REFERENCES cloud_machines(id),
  token TEXT,
  used BOOLEAN, used_at TIMESTAMP,
  created_at TIMESTAMP, expires_at TIMESTAMP
);

CREATE TABLE audit_log (
  id BIGINT PRIMARY KEY,
  entry_hash TEXT,
  prev_hash TEXT,
  timestamp TIMESTAMP,
  actor TEXT,
  action TEXT,
  resource_type TEXT, resource_id TEXT,
  details JSONB,
  severity TEXT,
  created_at TIMESTAMP
);
CREATE INDEX idx_audit_log_actor_timestamp ON audit_log(actor, timestamp);
CREATE INDEX idx_audit_log_resource ON audit_log(resource_type, resource_id);
CREATE INDEX idx_audit_log_severity ON audit_log(severity);

CREATE TABLE alert_rules (
  id TEXT PRIMARY KEY,
  name TEXT,
  severity TEXT,
  threshold NUMERIC,
  duration_seconds INT,
  action TEXT
);
```

## API Surface
**gRPC:**
- `Identity.IssueSVID(service_name, ttl) → SVID` — request mTLS certificate
- `Identity.VerifyOTPN(node_id, otp_token, rotation_class) → DecryptedSecret` — one-time-use verification
- `Audit.WriteEntry(actor, action, resource, details) → AuditID` — append to chain

**REST:**
- `POST /api/v1/auth/login` — JWT issue (OIDC flows)
- `GET /api/v1/audit/log` — list audit entries (filtered by actor, resource, date range)
- `GET /api/v1/audit/verify` — verify chain integrity for date range
- `POST /api/v1/vault/rotate-keys` — operator-triggered key rotation (breaks old joiner secrets; reissue required)

## Metrics & Alerts
- `gough.audit.chain_integrity_failure` (CRITICAL) — hash chain broken; offsite mirror stale/unavailable
- `gough.spiffe.svid_rotation_grace_exceeded` (CRITICAL) — SVID expiration imminent; service cannot renew
- `gough.security.otp_replay_detected` (WARNING) — joiner secret OTP used twice (should not happen; token consumed after first use)
- `gough.audit.offsite_mirror_lag` (WARNING) — offsite mirror >5 min behind primary

## Acceptance Criteria
1. Services authenticate via SPIRE mTLS; curl with `--cacert` to any gRPC service succeeds; connection denied without valid SVID
2. Operator logs in via CLI: `gough auth login`; receives JWT valid for 1 hour; can refresh via `gough auth refresh`
3. Audit log shows all provisioning/deployment actions; operator can verify chain: `gough audit verify 2026-04-20 2026-04-30` returns PASS
4. Offsite mirror lag monitored; missing mirror triggers WARNING; audit trail exportable: `gough audit export > audit-backup.jsonl`

## Dependencies
Plan 2 (Deployment) generates bootstrap tokens and joiner secrets; Plan 4 enforces encryption and storage in Vault. Plan 3 (Orchestration) biomes use SPIRE SVID for mTLS. All plans depend on audit logging.
