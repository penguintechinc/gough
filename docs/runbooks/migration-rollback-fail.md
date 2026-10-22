# Runbook: Biome Migration Rollback Failed

**Alert Name:** `gough.migration.rollback_failed`  
**Severity:** CRITICAL  
**Component:** Biome migration safety  

**Symptoms:**
- Prometheus alert: `gough.migration.rollback_failed{biome_id="..."}`
- Migration safety check failed; cannot rollback
- Biome is in inconsistent state

**Detection:**
```bash
# Check migration status
gough migration status --biome-id=<id> --json | jq '{state, rollback_available, error}'
```

**Resolution:**
1. Escalate to SRE (data consistency issue)
2. Manual rollback may be required
3. Investigate why rollback failed

---
