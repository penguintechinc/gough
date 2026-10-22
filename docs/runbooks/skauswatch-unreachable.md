# Runbook: Skauswatch Unreachable

**Alert Name:** `gough.integration.skauswatch_unreachable`  
**Severity:** WARNING  
**Component:** Skauswatch integration (SPIFFE/SPIRE)  

**Symptoms:**
- Prometheus alert: `gough.integration.skauswatch_unreachable`
- New SPIFFE identities cannot be issued
- Fallback to built-in SPIRE (minimal)

**Detection:**
```bash
# Test Skauswatch
curl -s https://<skauswatch>/health

# Check cluster config
gough config get spiffe-provider

# Monitor fallback usage
gough metrics export --query 'gough.identity.spire_fallback_used'
```

**Resolution:**
1. Verify Skauswatch is running
2. Check network connectivity
3. If down, escalate to Skauswatch team
4. Fall back to built-in SPIRE if needed

---
