# Runbook: DR Drill Failed

**Alert Name:** `gough.dr.drill_failed`  
**Severity:** CRITICAL  
**Component:** Disaster recovery testing  

**Symptoms:**
- Prometheus alert: `gough.dr.drill_failed{drill_id="..."}`
- DR drill did not complete successfully
- RTO/RPO SLOs may not be achievable

**Detection:**
```bash
# Check latest drill result
gough dr drill-status --latest --json | jq '{drill_id, status, rto_observed, rpo_observed, errors}'

# Check for blocking errors
gough dr drill-log --latest --filter="severity=error"
```

**Resolution:**
```bash
# Investigate error in detail
gough dr drill-log --drill-id=<id> --detailed

# Fix underlying issues (backup, restore, snapshot issues)

# Retry drill
gough dr drill-run --force

# Document findings in runbook updates
```

---
