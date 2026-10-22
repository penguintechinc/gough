# Runbook: MTU Mismatch

**Alert Name:** `gough.network.mtu_mismatch`  
**Severity:** WARNING  
**Component:** Network MTU configuration  

**Symptoms:**
- Prometheus alert: `gough.network.mtu_mismatch{node_id="..."}`
- Network performance degraded (packet fragmentation)
- Logs: "MTU mismatch detected between nodes"

**Detection:**
```bash
# Check MTU across cluster
gough node network-status --json | jq '.[] | {node_id, mtu}'

# Identify mismatches
gough node network-status --json | jq '[.[] | .mtu] | unique'
# Should be single value
```

**Resolution:**
```bash
# Standardize to 1500 or 9000 (jumbo frames)
gough node network-mtu-set --value=1500 --all

# Verify
gough node network-status --json | jq '.[] | .mtu' | sort -u
```

---
