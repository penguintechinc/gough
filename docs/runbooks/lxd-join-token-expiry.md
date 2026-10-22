# Runbook: LXD Join Token Expiry

**Alert Name:** `gough.lxd.join_token_expiry`  
**Severity:** WARNING  
**Component:** LXD cluster join tokens  

**Symptoms:**
- Prometheus alert fires when token < 24h from expiry
- New LXD nodes cannot join cluster

**Detection:**
```bash
gough lxd join-token status --json | jq '.expires_at, .time_remaining_seconds'
```

**Resolution:**
```bash
# Generate new token
gough lxd join-token refresh --duration=168h

# Provide to operators
gough lxd join-token get
```

---
