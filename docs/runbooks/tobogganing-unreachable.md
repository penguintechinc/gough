# Runbook: Tobogganing Unreachable

**Alert Name:** `gough.integration.tobogganing_unreachable`  
**Severity:** CRITICAL / WARNING  
**Component:** Tobogganing integration (DHCP/DNS)  

**Symptoms:**
- Prometheus alert: `gough.integration.tobogganing_unreachable`
- DHCP/DNS bootstrap fails for new nodes
- Fallback to built-in dnsmasq (dev-mode only)

**Detection:**
```bash
# Check Tobogganing reachability
curl -s https://<tobogganing>/health | jq '.status'

# Check cluster config
gough config get dhcp-provider

# Monitor fallback usage
gough metrics export --query 'gough.provisioning.bootstrap_dns_fallback_used'
```

**Resolution:**
```bash
# If Tobogganing is down: escalate to Tobogganing team
# Fallback to built-in: `gough config set dhcp-provider=builtin`
# Restore: `gough config set dhcp-provider=tobogganing` when service is back

# Verify connectivity restored
gough integration tobogganing-test
```

---
