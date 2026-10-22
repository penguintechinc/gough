# Runbook: Network Bond Flapping

**Alert Name:** `gough.network.bond_flapping`  
**Severity:** WARNING  
**Component:** Network bonding / LAG  

**Symptoms:**
- Prometheus alert: `gough.network.bond_flapping{node_id="..."}`
- Network interface repeatedly goes down/up
- Log spam from bonding driver

**Detection:**
```bash
# Check bond status
gough node bond-status --node-id=<id> --json | jq '.bonds[] | {name, state, members}'

# Check interface logs
dmesg | grep -i "bond\|down\|up" | tail -20
```

**Resolution:**
1. Check cable connections
2. Verify switch port configuration
3. Check for duplex/speed mismatches
4. Run `gough doctor network`

---
