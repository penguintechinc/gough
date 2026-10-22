# Runbook: Audit Offsite Mirror Lag

**Alert Name:** `gough.audit.offsite_mirror_lag`  
**Severity:** WARNING  
**Component:** Audit offsite replication  
**SLO Impact:** RPO degrades; audit log recovery window increases  

---

## Symptoms

- Prometheus alert: `gough.audit.offsite_mirror_lag_seconds > 600`
- `gough audit offsite-status` shows lag > 10 minutes
- Offsite mirror is stale; latest audit entries are not yet replicated
- Network latency or mirror unavailability

---

## Detection

**Metrics:**
```bash
gough.audit.offsite_mirror_lag_seconds > 600
gough.audit.offsite_mirror_reachable == 0
```

**CLI verification:**
```bash
# Check mirror lag
gough audit offsite-status --json | jq '.lag_seconds, .last_sync, .status'

# Test mirror connectivity
curl -v https://<audit-offsite-url>/health

# Check replication queue
psql -U postgres -d gough -c "SELECT COUNT(*) FROM audit_log_replication_queue;"
```

---

## Diagnosis

**Two plausible causes:**

### Cause 1: Network Latency (70% likelihood)
- Network between primary and mirror is slow or congested.
- Check network MTU, latency, packet loss.

### Cause 2: Mirror Service Down (30% likelihood)
- Mirror service is unreachable or overloaded.
- Check mirror pod status, disk space, CPU.

---

## Resolution

**For network latency:**
```bash
# Check network path
mtr -r -c 10 <audit-offsite-host>

# Fix: restart mirror sync process
gough audit offsite-sync --force

# Monitor progress
watch -n 5 'gough audit offsite-status --json | jq ".lag_seconds"'
```

**For mirror service down:**
```bash
# Restart mirror
kubectl -n gough restart pod -l app=audit-mirror

# Wait for pod to be ready
kubectl -n gough wait --for=condition=Ready pod -l app=audit-mirror --timeout=60s

# Trigger sync
gough audit offsite-sync --force
```

---

## Verification

**Success criteria:**
1. `gough audit offsite-status | jq .lag_seconds` < 300 (5 minutes).
2. Alert clears in Prometheus.

---

## Prevention

1. Monitor offsite connectivity continuously.
2. Alert threshold: lag > 600s (10 min); escalate at 1800s (30 min).
3. Test mirror failover quarterly.

---
