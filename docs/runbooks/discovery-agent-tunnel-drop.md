# Runbook: Discovery Agent Tunnel Drop

**Alert Name:** `gough.discovery.agent_tunnel_drop`  
**Severity:** WARNING  
**Component:** Discovery agent (node inventory)  
**SLO Impact:** Node state may be stale; discovery delays  

---

## Symptoms

- Prometheus alert: `gough.discovery.agent_tunnel_drop{node_id="..."}`
- `gough node discovery-status --node-id=<id>` shows `tunnel_connected=false`
- Discovery agent cannot reach primary cluster
- Logs: "Discovery agent tunnel dropped; reconnecting"

---

## Detection & Resolution

**Primary metric:**
```bash
gough.discovery.agent_tunnel_drop{...} == 1
gough.discovery.agent_tunnel_reconnect_attempts_total > 0
```

**Check:**
```bash
# Verify tunnel status
gough node discovery-status --node-id=<id> --json | jq '.tunnel_connected, .last_heartbeat'

# Restart discovery agent
gough node discovery-restart --node-id=<id>

# Monitor reconnection
watch -n 5 'gough node discovery-status --node-id=<id> | jq ".tunnel_connected"'
```

**Resolution:**
1. Restart discovery agent (usually auto-recovers).
2. Check network connectivity to primary cluster.
3. If persistent, check agent pod logs.

---
