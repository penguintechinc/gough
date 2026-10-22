# Disaster recovery runbook

---

## RTO & RPO by cluster configuration

| Cluster Size | RTO Target | RPO Target | Method |
|---|---|---|---|
| 1 primary, no secondary | 4 hours | 1 hour | Manual restore from S3 backup |
| 3-5 control plane | 1.5 hours | 15 min | Promote secondary, DNS update |
| 10+ control plane | 30 min | 5 min | Automatic failover + DNS cutover |

---

## DR drill

**Purpose:** Test full recovery process without impacting production. Run quarterly at minimum.

**Command:**
```bash
gough dr drill
# or
gough dr drill --target=<site-id>
```

**What it tests:**
- Connectivity to secondary site
- Backup accessibility and integrity
- Database replication lag
- Vault state replication
- SPIRE federation status
- Biome catalog mirror availability
- DNS update capability

**Expected output:**
```
✓ Connectivity: OK (latency 45ms)
✓ Backup accessible: s3://backups/gough-prod-2026-05-02T11:30:00Z
✓ Database lag: 2.3s
✓ Vault replicated: yes (performance standby ready)
✓ SPIRE state: synced
✓ OCI mirror: available
✓ DNS validation: OK

Drill completed successfully. Next recovery would take ~20 minutes.
```

**Verification after drill:**
```bash
# Check drill result
gough dr drill
# Exit code 0 = pass, 10 = fail

# View detailed drill report
gough cluster status
# Should show "dr_drill_ok: true" and timestamp
```

**Troubleshooting drill failures:**
- `DNS validation failed` — Contact network team, check Squawk status
- `Backup accessible: FAILED` — S3 credentials or path issue; verify backup location with `gough config get backup.s3_url`
- `Database lag: X > 30s` — Replication is degraded; investigate network between sites
- `Vault replicated: no` — Vault hasn't replicated to secondary; manual intervention needed

---

## DR promote: primary is unreachable

**When to use:** Primary site is completely unavailable (network down, datacenter issue, all nodes down).

**Pre-conditions:**
- Confirmed primary is truly unreachable (5+ ping failures, no SSH, no API response)
- Gathered approval from ops lead or on-call manager
- Verified secondary is healthy: `gough dr drill --target=<secondary-site>`
- Notified customers/stakeholders of potential brief outage

**Command:**
```bash
gough dr promote <site-id> \
  --reason="Primary datacenter network failure, confirmed unreachable" \
  --source-unreachable
```

**What it does (timeline: ~15-20 minutes):**
1. **Verifies secondary readiness** (~1m)
   - Checks database replication lag
   - Validates Vault state sync
   - Confirms SPIRE federation
   
2. **Promotes secondary Postgres to primary** (~2m)
   - Stops replication from old primary
   - Converts standby to read-write
   
3. **Promotes Vault performance standby to leader** (~1m)
   - Unseals Vault on secondary
   - Initializes high-availability cluster
   
4. **Re-anchors SPIRE federation** (~3m)
   - Updates SPIRE root CA pointers
   - Issues new intermediate certificates
   
5. **Verifies OCI mirror** (~2m)
   - Confirms biome catalog is accessible
   - Tests image pull capability
   
6. **Updates DNS** (~5m)
   - If Squawk is configured: automatic cutover
   - If manual DNS: sends DNS update instructions

**Expected output:**
```
Promoting site secondary-dal2 to primary...

✓ Secondary readiness check: OK (db lag: 1.2s)
✓ Postgres promotion: OK
✓ Vault promotion: OK
✓ SPIRE re-anchoring: OK
✓ OCI mirror validation: OK
✓ DNS update: OK (TTL 60s, cutover ~2 min)

Promotion complete. Clients should reconnect within 2 minutes.
```

**After promotion:**
```bash
# Verify new primary is healthy
gough cluster status
# State should show "primary" with all components healthy

# Monitor client reconnection
gough cluster status
# Wait for "ready nodes" to stabilize

# Check audit log
gough audit log --limit=5
# Should show "site_promoted" event
```

---

## DR promote: primary is reachable but degraded

**When to use:** Primary is partially responsive but too unstable to trust (high latency, frequent timeouts, disk full, database corruption warnings).

**Pre-conditions:**
- Primary is reachable but unstable
- No `--source-unreachable` flag used (default behavior)
- Secondary has passed recent DR drill

**Command:**
```bash
gough dr promote <site-id> \
  --reason="Primary experiencing degraded performance and database corruption warnings"
```

**Difference from unreachable scenario:**
- Promotion is slower (~25-30 min vs 15-20 min)
- System attempts graceful shutdown of primary services
- Full audit trail is captured from old primary before cutover
- Old primary can be analyzed post-promotion for root cause
- Risk of data loss is lower (replication lag typically < 5s)

**Verification steps:**
```bash
# Confirm promotion with health check
gough cluster status

# Check if old primary is still accessible (for forensics)
gough node list
# Old primary should show "degraded" or "offline" state

# Schedule maintenance window to restore/rebuild old primary
```

---

## DR failback: return to original primary

**When to use:** After promotion, primary datacenter is restored and you want to move back.

**Pre-conditions:**
- Old primary site is rebuilt and healthy
- Passed DR drill on old primary: `gough dr drill --target=<original-site>`
- Window of 1-2 hours with acceptable downtime (optional: can do zero-downtime with care)
- Notification sent to team and customers

**Command:**
```bash
gough dr failback <site-id>
```

**What it does (timeline: ~20-30 minutes):**
1. **Snapshots current secondary data** (~5m)
   - Creates point-in-time backup of secondary
   
2. **Replicates data to restored primary** (~10m)
   - Begins continuous replication of secondary → primary
   - Primary catches up to secondary
   
3. **Performs health check on primary** (~2m)
   - Verifies database integrity
   - Checks Vault unsealing
   
4. **Switches back to original primary** (~2m)
   - Promotes primary to leader
   - Demotes secondary to standby
   
5. **Updates DNS** (~5m)
   - Routes traffic back to original primary
   - Squawk or manual DNS update

**Expected output:**
```
Failing back from secondary-dal2 to primary-dal1...

✓ Data snapshot: OK
✓ Replication: OK (0.8s lag)
✓ Primary health check: OK
✓ Primary promotion: OK
✓ DNS update: OK (TTL 60s, cutover ~2 min)

Failback complete. Original primary is now active.
```

**Verification:**
```bash
gough cluster status
# Should show primary-dal1 as active

gough audit log --limit=10
# Should show "failback_initiated" and "failback_complete" events
```

---

## Restore from backup

**When to use:**
- Both primary and secondary are lost (rare multi-site failure)
- Need to recover to a point-in-time before an incident
- Disaster recovery but no secondary available

**Pre-conditions:**
- Identify the backup timestamp to restore to: `gough config get backup.s3_url`
- New target cluster is provisioned and initialized
- Target cluster is empty (no data you want to preserve)

**Command:**
```bash
gough dr restore \
  --from=s3://backups/gough-prod-2026-05-02T11:30:00Z \
  --cluster-id=<target-cluster-id>
```

**What it does (timeline: depends on data size, typically 1-4 hours):**
1. Verifies backup accessibility
2. Downloads and validates backup integrity
3. Restores database to target cluster
4. Restores Vault state and unseals
5. Re-issues SPIRE certificates
6. Verifies all components online

**Expected output:**
```
Restoring from s3://backups/gough-prod-2026-05-02T11:30:00Z...

✓ Backup found: 2026-05-02T11:30:00Z (size: 127 GB)
✓ Backup integrity: verified
✓ Database restore: OK (time elapsed: 45m)
✓ Vault unsealing: OK
✓ SPIRE issuance: OK
✓ Component verification: OK

Restore complete. Cluster is ready for service.
```

**After restore:**
```bash
# Verify restored cluster is healthy
gough cluster status

# Check timestamp of data
gough audit log --limit=1 | grep timestamp
# Should show "2026-05-02T11:30:00Z" as latest event

# Manually verify critical data is present
# (Business continuity team should validate)
```

---

## Decision tree: which command to use

```
Is primary reachable?
  ├─ NO (down, no ping, no SSH)
  │   └─ gough dr promote <site> --source-unreachable
  │
  ├─ YES but degraded (slow, timeouts, warnings)
  │   └─ gough dr promote <site> (without --source-unreachable)
  │
  └─ YES and healthy
      ├─ Want to return after temporary failover?
      │   └─ gough dr failback <site>
      │
      └─ Just testing?
          └─ gough dr drill [--target=<site>]

Lost BOTH primary and secondary?
  └─ gough dr restore --from=s3://... --cluster-id=...
```

---

## General verification steps

**After any DR operation (drill, promote, failback, restore):**

```bash
# 1. Cluster health
gough cluster status
# All components should show healthy: vault, spire, db, k8s, nats

# 2. Audit trail integrity
gough audit verify --since=-24h
# Should exit 0 with no errors

# 3. Node status
gough node list
# All nodes should show "ready" state

# 4. Recent events
gough audit log --limit=10
# Should show the DR operation event with timestamp and operator

# 5. Data consistency (if DB-specific)
# Run application-level health checks
curl https://<api-url>/healthz
# Should respond 200 OK
```

---

## Troubleshooting

| Problem | Check | Fix |
|---------|-------|-----|
| Drill reports "Database lag > 30s" | Network latency between sites | Verify network links, check replication queue size |
| Promote fails with "Vault locked" | Secondary Vault is sealed | Manually unseal secondary: `gough cluster vault-unseal --site=<secondary>` |
| Promote fails with "DNS update failed" | Squawk is not configured | Configure Squawk first: `gough cluster network-baseline configure mgmt --opt squawk_zone=<zone>` |
| Failback hangs on "Replication" | Primary is catching up from large lag | Wait, or check network: `ping <primary-ip>` |
| Restore reports "Backup not found" | S3 path is wrong | Check configured backup location: `gough config get backup.s3_url` |

---
