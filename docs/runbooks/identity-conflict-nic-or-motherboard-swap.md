# Runbook: Identity Conflict (NIC or Motherboard Swap)

**Alert Name:** `gough.identity.conflict_detected`  
**Severity:** WARNING  
**Component:** Node identity / hardware fingerprint  

**Symptoms:**
- Prometheus alert: `gough.identity.conflict_detected{node_id="..."}`
- Node hardware fingerprint changed (NIC MAC, motherboard serial)
- Cloud-init Phase-1 detects identity mismatch

**Detection:**
```bash
gough node identity-status --node-id=<id> --json | jq '{fingerprint, hardware_hash, matches_db}'

# Check for NIC changes
gough node hardware-changes --node-id=<id> --json | jq '.changes[] | {component, old_value, new_value}'
```

**Resolution:**
1. Verify hardware change is intentional
2. Approve new identity: `gough node identity-approve --node-id=<id>`
3. Update database fingerprint

---
