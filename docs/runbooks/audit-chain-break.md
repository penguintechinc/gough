# Runbook: Audit Chain Break

**Alert Name:** `gough.audit.chain_integrity_failure`  
**Severity:** CRITICAL  
**Component:** Audit subsystem  
**SLO Impact:** RPO/RTO for compliance audit trails  

---

## Symptoms

- Prometheus alert fires: `gough.audit.chain_integrity_failure`
- Audit chain integrity check returns non-zero exit code
- `gough audit verify` CLI returns mismatched chain hash
- Offsite mirror shows divergence from primary audit log
- Operator receives automated email: "Audit chain integrity failure detected"

---

## Detection

**Primary metric:**
```
gough.audit.chain_integrity_failure{cluster_id="...",severity="critical"} == 1
```

**CLI verification (first 30 seconds):**
```bash
# Verify chain integrity immediately
gough audit verify --since=-1h

# Check offsite mirror sync lag
gough audit offsite-status --json | jq '.lag_seconds, .last_sync'

# Review last 10 entries for corruption patterns
gough audit log --limit=10 --format=json | jq '.[]|{id,entry_hash,prev_hash}'
```

**Expected output on healthy system:**
- `verify` returns exit code 0
- `offsite-status.lag_seconds` < 300
- Each entry's `prev_hash` matches previous entry's `entry_hash`

---

## Severity Assessment

**Critical.** Audit chain breaks indicate tamper, corruption, or operator error. Do not assume this is routine. Audit integrity is non-negotiable compliance boundary.

---

## First 30 Seconds

1. **Do NOT restart any services** — you risk data loss.
2. Run verification commands above; capture full output to a file.
3. Check if this correlates with recent cluster operations (e.g., `gough biome rollback`, multi-cluster migration).
4. If offsite mirror is behind >300s: suspect network issue (see `audit-offsite-mirror-lag` runbook).
5. If `verify` shows hash mismatch: proceed to Diagnosis.

---

## Diagnosis

**Three plausible causes:**

### Cause 1: Offsite Mirror Network Lag (40% likelihood)
**Symptoms:** Local audit log is correct; offsite mirror is behind.

**Check:**
```bash
# Local chain integrity
gough audit verify --local-only

# Mirror reachability
curl -v https://<audit-offsite-url>/health

# Network diagnostics
ping -c 5 <audit-offsite-host>
mtr -r -c 10 <audit-offsite-host>
```

**If local is OK but mirror is behind:**
- Network is recovering or mirror is temporarily unreachable.
- Monitor metric `gough.audit.offsite_mirror_lag_seconds` — should drop within 10 minutes.
- If lag persists >30 min: escalate to network/infrastructure team (see Escalation).

**Resolution:** Wait 10 minutes for mirror to catch up. If metric remains elevated, see `audit-offsite-mirror-lag` runbook.

---

### Cause 2: Database Corruption or Write Failure (35% likelihood)
**Symptoms:** Verify fails on primary audit log; recent entries have inconsistent hashes.

**Check:**
```bash
# Inspect database directly
psql -U postgres <audit_db> -c "SELECT id, entry_hash, prev_hash FROM audit_log ORDER BY id DESC LIMIT 20;"

# Check for NULL or empty hashes
psql -U postgres <audit_db> -c "SELECT COUNT(*) FROM audit_log WHERE entry_hash IS NULL OR prev_hash IS NULL;"

# Check database transaction log for errors
journalctl -u postgresql -n 100 | grep -i error
```

**If hashes are NULL or inconsistent:**
- Database write may have failed mid-transaction.
- Check disk space: `df -h`
- Check PostgreSQL logs: `tail -100 /var/log/postgresql/postgresql.log | grep ERROR`

**Resolution (if disk space OK):**
```bash
# Stop audit writer
systemctl stop gough-audit-writer

# Attempt repair (requires careful review; do NOT run blindly)
gough audit repair --dry-run

# Review repair plan; if safe, execute
gough audit repair --confirm

# Verify chain after repair
gough audit verify --since=-1h

# Restart audit writer
systemctl start gough-audit-writer
```

**If disk full:**
1. Free up space on audit partition.
2. Restart PostgreSQL: `systemctl restart postgresql`
3. Re-run `gough audit verify`.

---

### Cause 3: Operator Applied Incompatible Patch or Rollback (25% likelihood)
**Symptoms:** Alert fires after `gough biome rollback` or cluster version downgrade.

**Check:**
```bash
# Review recent audit entries for patch/rollback markers
gough audit log --limit=20 --format=json | jq '.[]|select(.action=="rollback" or .action=="patch") | {id,timestamp,action,biome_id}'

# Check cluster schema version
gough cluster version

# Check if this is expected (e.g., planned rollback)
gough audit log --limit=5 --format=json | jq '.[0] | {timestamp,action,operator}'
```

**If this was a planned rollback:**
- Audit chain integrity may be expected to reset.
- Verify with operator who initiated the rollback.
- If intentional, acknowledge the alert and add a suppression rule.

**If unplanned:**
- Escalate to platform team (see Escalation).

---

## Resolution

**Step 1: Confirm which cause applies (from Diagnosis above).**

**Step 2: Apply targeted fix:**

**For Cause 1 (mirror lag):**
```bash
# Monitor progress
watch -n 10 'gough audit offsite-status --json | jq ".lag_seconds"'

# If lag doesn't decrease, check mirror logs
ssh <audit-mirror-host> "journalctl -u gough-audit-mirror -n 50"

# Trigger manual sync
gough audit offsite-sync --force
```

**For Cause 2 (DB corruption):**
```bash
# Backup audit DB before repair
pg_dump <audit_db> > /backup/audit_db_backup_$(date +%s).sql

# Repair
gough audit repair --confirm

# Verify
gough audit verify --since=-24h

# If verify still fails, restore from backup and escalate
psql -U postgres < /backup/audit_db_backup_*.sql
```

**For Cause 3 (rollback-induced):**
```bash
# If rollback was intentional, suppress alert
gough alert silence gough.audit.chain_integrity_failure --duration 1h --reason "Planned rollback"

# If unplanned, rollback the rollback
gough biome rollback --biome-id=<biome_id> --to-version=<previous_good_version>
```

**Step 3: Verify fix:**
```bash
gough audit verify --since=-1h
# Should exit 0

# Confirm alert clears in Prometheus after 1 min
curl -s 'http://localhost:9090/api/v1/query?query=gough.audit.chain_integrity_failure' | jq '.data.result'
# Should return empty array
```

---

## Verification

**Success criteria:**
1. `gough audit verify --since=-1h` exits 0.
2. Alert `gough.audit.chain_integrity_failure` is no longer firing (check Prometheus after 1 min).
3. Offsite mirror lag is < 300 seconds.
4. No new audit chain breaks occur within 24 hours.

**Long-term validation:**
```bash
# Schedule hourly integrity checks (cron)
# 0 * * * * /usr/local/bin/gough audit verify --since=-2h >> /var/log/gough-audit-verify.log 2>&1

# Monitor metric trend over 7 days
gough metrics export --query 'gough.audit.chain_integrity_failure' --from=-7d | jq '.data | group_by(.timestamp) | .[] | {timestamp: .[0].timestamp, count: length}'
```

---

## Prevention

1. **Daily integrity checks:**
   ```bash
   # Add to crontab
   0 0 * * * /usr/local/bin/gough audit verify --since=-24h
   ```

2. **Monitor offsite mirror lag continuously:**
   - Alert threshold: lag > 600s (10 min)
   - Escalate to network team if > 1800s (30 min)

3. **Backup audit DB regularly:**
   ```bash
   0 1 * * * pg_dump gough_audit | gzip > /backup/audit_$(date +\%Y\%m\%d\%H\%M\%S).sql.gz
   ```

4. **Document all rollbacks:**
   - Before running `gough biome rollback`, notify SRE team with reason.
   - Log the reason in a ticket for audit trail.

5. **Test repair procedure monthly:**
   - Run `gough audit repair --dry-run` in staging.
   - Verify chain integrity after repair.

---

## Escalation

**Escalate to:** Platform SRE, Compliance Officer  
**When:**
- Database repair fails or chain cannot be verified.
- Offsite mirror is unreachable (network team needed).
- Unknown cause after diagnosis steps.

**Escalation procedure:**
```bash
gough alert escalate gough.audit.chain_integrity_failure \
  --team=platform-sre \
  --reason="Database corruption; repair failed" \
  --ticket=<jira-ticket-id>
```

**PagerDuty integration:** Alert should auto-trigger on-call SRE page if not resolved within 5 minutes.

---

## Related Runbooks

- [audit-offsite-mirror-lag.md](audit-offsite-mirror-lag.md) — Offsite mirror falling behind
- [quorum-loss.md](quorum-loss.md) — Cluster consensus failure (may impact audit writes)
- [dr-drill-failed.md](dr-drill-failed.md) — DR audit recovery procedures

---

## Playbook Summary

| Step | Action | Time |
|------|--------|------|
| 1 | Run `gough audit verify --since=-1h` | 30s |
| 2 | Check offsite lag with `gough audit offsite-status` | 30s |
| 3 | Diagnose cause (DB, network, or rollback) | 2-5 min |
| 4 | Apply targeted resolution (mirror sync, DB repair, or escalate) | 5-30 min |
| 5 | Verify `gough audit verify` passes | 1 min |
| 6 | Confirm alert clears in Prometheus | 1 min |
| **Total MTTR** | | **10-35 min** |

