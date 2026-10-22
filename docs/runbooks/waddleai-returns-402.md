# Runbook: WaddleAI Returns 402 Payment Required

**Alert Name:** `gough.waddleai.returns_402`  
**Severity:** WARNING / INFO  
**Component:** WaddleAI integration (license)  

**Symptoms:**
- Prometheus alert: `gough.waddleai.http_402_payment_required`
- WaddleAI capacity forecast unavailable
- Cluster still functions; forecast features disabled

**Detection:**
```bash
# Check WaddleAI status
gough waddleai status --json | jq '.license_valid, .features_available'

# Check cluster config
gough config get license-key
```

**Resolution:**
1. Verify license is valid
2. Check license server connectivity
3. If license expired: renew or escalate
4. Cluster continues with limited predictive features

---
