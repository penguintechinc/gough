# Runbook: Migration Safety Envelope Repeated Rejection

**Alert Name:** `gough.migration.safety_envelope_rejected`  
**Severity:** WARNING  
**Component:** Migration safety policy  

**Symptoms:**
- Prometheus alert fires repeatedly
- Migration safety envelope threshold exceeded
- Capacity policy may need adjustment

**Detection:**
```bash
# Check rejection reasons
gough migration safety-check --biome-id=<id> --json | jq '.rejection_reasons[]'
```

**Resolution:**
```bash
# Review and adjust migration policy
gough migration policy show

# Tighten or loosen thresholds as needed
gough migration policy set require_target_capacity_headroom_mem_pct 25
```

---
