# Runbook: License Server Unreachable

**Alert Name:** `gough.licensing.server_unreachable`  
**Severity:** WARNING  
**Component:** License validation  

**Symptoms:**
- Prometheus alert: `gough.licensing.server_unreachable`
- License server cannot be reached
- Cluster continues with cached license

**Detection:**
```bash
# Test license server connectivity
curl -s https://license.penguintech.io/health

# Check cache status
gough license cache-status --json | jq '.cache_valid, .cache_expires_at'
```

**Resolution:**
1. Check network connectivity to license server
2. If unreachable, cluster uses cached license
3. Escalate to infrastructure team if persistent

---
