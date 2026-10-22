# Runbook: Squawk Unreachable

**Alert Name:** `gough.integration.squawk_unreachable`  
**Severity:** WARNING  
**Component:** Squawk integration (DNS/DHCP)  

**Symptoms:**
- Prometheus alert: `gough.integration.squawk_unreachable`
- DNS resolution may fail
- Fallback to local DNS cache

**Detection:**
```bash
# Test Squawk
curl -s https://<squawk>/health

# Check cluster config
gough config get dns-provider
```

**Resolution:**
1. Verify Squawk is running and healthy
2. Check network connectivity
3. If down, escalate to Squawk team
4. Fallback to local resolver if persistent

---
