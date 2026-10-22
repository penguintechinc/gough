# Runbook: IPv6 SLAAC Conflict

**Alert Name:** `gough.network.ipv6_slaac_conflict`  
**Severity:** WARNING  
**Component:** IPv6 SLAAC address assignment  

**Symptoms:**
- Prometheus alert: `gough.network.ipv6_slaac_conflict{node_id="..."}`
- Multiple nodes assigned same IPv6 address
- Network routing issues

**Detection:**
```bash
# Check for duplicate IPv6 addresses
gough node list --json | jq '.[] | {node_id, ipv6_addrs}' | sort | uniq -d

# Check SLAAC status
gough node network-slaac-status --json | jq '.[] | {node_id, ipv6_addr, conflict}'
```

**Resolution:**
```bash
# Manually assign unique IPv6 addresses
gough node network-ipv6-set --node-id=<id> --address=<unique-ipv6>

# Regenerate SLAAC
gough node network-slaac-regenerate --node-id=<id>
```

---
