# Runbook: SVID Rotation Grace Period Exceeded

**Alert Name:** `gough.spiffe.svid_rotation_grace_exceeded`  
**Severity:** CRITICAL  
**Component:** SPIFFE/SPIRE identity  
**SLO Impact:** Service-to-service mTLS broken; new pod identities cannot be issued  

---

## Symptoms

- Prometheus alert: `gough.spiffe.svid_rotation_grace_exceeded{cluster_id="..."}`
- `gough spiffe status` shows `svid_expired=true` or `grace_period_remaining_seconds <= 0`
- New pods fail to start (SPIRE agent cannot issue SVID)
- Service-to-service mTLS connections drop
- Logs: "SVID expired; unable to renew within grace period"

---

## Detection

**Primary metrics:**
```bash
gough.spiffe.svid_rotation_grace_exceeded{...} == 1
gough.spiffe.svid_validity_seconds{...} < 0
gough.spiffe.grace_period_remaining_seconds{...} <= 0
```

**CLI verification (first 30 seconds):**
```bash
# Check SPIRE agent status
gough spiffe status --json | jq '{svid_expired,grace_period_remaining_seconds,next_rotation}'

# Check SPIRE server health
kubectl -n gough get pods -l app=spire-server -o wide

# Inspect workload SVIDs
gough spiffe workload-list --format=json | jq '.[] | {workload_id, svid_expiry, expires_in_seconds}'

# Check for failing pods
kubectl -n gough get pods | grep -E "CrashLoopBackOff|ImagePullBackOff|Pending"

# Inspect agent logs for rotation failures
kubectl -n gough logs -l app=spire-agent --tail=50 | grep -i "rotation\|svid\|expired"
```

---

## Severity Assessment

**CRITICAL.** SVIDs are the foundation of service-to-service mTLS. Expired SVIDs block all inter-pod communication. New workloads cannot be issued identities. This cascades to complete cluster dysfunction.

---

## First 30 Seconds

1. **Do NOT restart SPIRE components yet** — this may worsen the situation if rotation is already in-progress.
2. Run verification commands above.
3. Check SPIRE server pod logs for rotation errors.
4. Determine if grace period has truly expired or if rotation is stalled.
5. If any pods are in CrashLoopBackOff, they're likely waiting for SVID refresh.

---

## Diagnosis

**Three plausible causes:**

### Cause 1: SPIRE Server Cannot Reach Root CA (50% likelihood)
**Symptoms:** SPIRE agent logs show "cannot renew SVID: root CA unreachable" or similar.

**Check:**
```bash
# Check SPIRE server reachability
kubectl -n gough get svc -l app=spire-server

# Test SPIRE API availability
kubectl -n gough exec -it <spire-server-pod> -- spire-server api fetch

# Check upstream CA connectivity (if using external CA)
gough spiffe ca-status --json | jq '{ca_type,is_reachable,last_sync,sync_error}'

# Check network policies
kubectl -n gough get networkpolicy | grep spire

# Check firewall/DNS
nslookup <spire-server.gough.svc.cluster.local>
kubectl -n gough exec -it <spire-agent-pod> -- curl -v https://spire-server:8081/api/version
```

**If SPIRE server is unreachable:**
- Check if pod is running and ready.
- Verify network policy allows agent→server on port 8081 (mTLS).
- Verify DNS resolution works.

---

### Cause 2: SPIRE Intermediate CA Expired (35% likelihood)
**Symptoms:** SPIRE server logs show "intermediate CA expired; cannot issue new SVIDs".

**Check:**
```bash
# Check SPIRE CA certificates
kubectl -n gough exec -it <spire-server-pod> -- spire-server api ca inspect

# Get expiry dates
kubectl -n gough exec -it <spire-server-pod> -- \
  openssl x509 -in /var/lib/spire/server/ca.crt -noout -enddate -startdate

# Check if intermediate CA needs rotation
gough spiffe ca-status --json | jq '.intermediate_ca | {issued_at, expires_at, remaining_seconds}'

# Check if upstream root CA is healthy
gough spiffe ca-status --json | jq '.root_ca | {issued_at, expires_at, remaining_seconds}'
```

**If intermediate CA is expired:**
- Intermediate CA must be renewed immediately.
- This requires access to root CA signing key (typically in Vault or Sealed Secrets).

---

### Cause 3: SPIRE Agent Cannot Reach Server (Networking Issue) (15% likelihood)
**Symptoms:** Agent logs show "connection refused" or "dial timeout" when contacting SPIRE server.

**Check:**
```bash
# Check agent-server connectivity
kubectl -n gough exec -it <spire-agent-pod> -- \
  telnet spire-server.gough.svc.cluster.local 8081

# Check mTLS certificates on agent
ls -la /var/lib/spire/agent/svid/

# Check SPIRE agent pod logs
kubectl -n gough logs -l app=spire-agent --tail=100 | grep -E "dial|connection|timeout|tls"

# Check for network policy blocking
kubectl -n gough describe networkpolicy spire-agent-to-server

# Test DNS resolution from agent pod
kubectl -n gough exec -it <spire-agent-pod> -- \
  nslookup spire-server.gough.svc.cluster.local
```

**If connectivity is broken:**
- Check if network policy is blocking traffic.
- Check if SPIRE server service is properly exposed.
- Restart SPIRE agent to re-establish connection.

---

## Resolution

**Step 1: Determine which cause applies.**

**Step 2: Apply targeted fix:**

**For Cause 1 (SPIRE server unreachable):**
```bash
# Step 1: Verify SPIRE server pod
kubectl -n gough get pods -l app=spire-server
# If NotReady, check PVC status
kubectl -n gough describe pvc spire-server-data

# Step 2: Restart SPIRE server if needed
kubectl -n gough delete pod -l app=spire-server
# Wait for pod to start
kubectl -n gough wait --for=condition=Ready pod -l app=spire-server --timeout=60s

# Step 3: Wait for SPIRE server to recover
sleep 30

# Step 4: Verify server is responsive
kubectl -n gough exec -it <spire-server-pod> -- spire-server api ca inspect

# Step 5: Trigger SVID rotation on agents
gough spiffe trigger-rotation

# Step 6: Monitor rotation progress
watch -n 10 'kubectl -n gough logs -l app=spire-agent --tail=5 | grep -i rotation'
```

**For Cause 2 (Intermediate CA expired):**
```bash
# Step 1: Check intermediate CA details
kubectl -n gough exec -it <spire-server-pod> -- \
  openssl x509 -in /var/lib/spire/server/ca.crt -noout -text | grep -E "Issuer|Subject|Not After"

# Step 2: Rotate intermediate CA
# This requires root CA key (typically in Vault)
gough spiffe rotate-intermediate-ca --root-key-secret=<vault-secret-path>

# Step 3: Verify rotation
kubectl -n gough exec -it <spire-server-pod> -- spire-server api ca inspect

# Step 4: Trigger SVID rotation on all agents
gough spiffe trigger-rotation

# Step 5: Wait for SVIDs to be renewed
kubectl -n gough logs -l app=spire-agent --since=1m | grep -i "svid.*renewed"
```

**For Cause 3 (Network connectivity):**
```bash
# Step 1: Restart SPIRE agents to re-establish connection
kubectl -n gough rollout restart daemonset/spire-agent

# Step 2: Wait for agents to reconnect
kubectl -n gough wait --for=condition=Ready pod -l app=spire-agent --timeout=120s

# Step 3: Verify connectivity
kubectl -n gough logs -l app=spire-agent --tail=10 | grep -i "connected\|authenticated"

# Step 4: Trigger SVID rotation
gough spiffe trigger-rotation

# Step 5: Monitor rotation
watch -n 5 'gough spiffe status --json | jq ".grace_period_remaining_seconds"'
```

**Step 3: Verify SVIDs are renewed:**
```bash
# Check grace period is cleared
gough spiffe status --json | jq '.grace_period_remaining_seconds'
# Should show positive value (> 3600)

# Check active SVIDs
gough spiffe workload-list --json | jq '.[] | {workload_id, expires_in_seconds}' | head -10

# Verify alert clears
curl -s 'http://localhost:9090/api/v1/query?query=gough.spiffe.svid_rotation_grace_exceeded' | jq '.data.result'
# Should return empty array

# Test pod startup
kubectl -n gough run test-pod --image=busybox -- sleep 3600
# Pod should start successfully
```

---

## Verification

**Success criteria:**
1. `gough spiffe status | jq .grace_period_remaining_seconds` shows positive value.
2. Alert `gough.spiffe.svid_rotation_grace_exceeded` is no longer firing.
3. All SPIRE agent pods show "ready" state.
4. New pods can be created without SVID errors.
5. Service-to-service mTLS connections are working.

**Long-term validation:**
```bash
# Monitor SVID expiry and grace period
watch -n 60 'gough spiffe status --json | jq "{svid_expired, grace_remaining_seconds, next_rotation}"'

# Check for recurring rotation issues
kubectl -n gough logs -l app=spire-agent --since=24h | grep -i "rotation\|failed" | wc -l
# Should be 0

# Validate SPIRE server health metrics
gough metrics export --query 'spire.server.*' --from=-1h | jq '.data | map(select(.value != "0")) | length'
# Should be low (healthy)
```

---

## Prevention

1. **Automate SVID rotation monitoring:**
   ```bash
   # Alert when grace period drops below 24h
   gough alert set gough.spiffe.svid_rotation_grace_exceeded \
     --threshold=86400 \
     --duration=1m \
     --severity=warning
   ```

2. **Test rotation regularly:**
   ```bash
   # Monthly rotation test
   0 2 1 * * /usr/local/bin/gough spiffe trigger-rotation --dry-run
   ```

3. **Monitor intermediate CA expiry:**
   ```bash
   # Alert 30 days before intermediate CA expires
   gough alert set gough.spiffe.intermediate_ca_expires_soon \
     --threshold=2592000 \
     --severity=warning
   ```

4. **Document rotation procedure:**
   - Create runbook for intermediate CA renewal.
   - Store root CA key securely (Vault, HSM).
   - Train SREs on SPIFFE troubleshooting.

5. **Test DR recovery:**
   - Quarterly: simulate SPIRE server failure and recovery.
   - Verify SVID rotation works after failover.

---

## Escalation

**Escalate to:** Platform SRE, Security Team  
**When:**
- Cannot reach SPIRE server after restart.
- Intermediate CA renewal fails.
- Root CA key is inaccessible.
- Multiple rotation attempts fail.

**Escalation procedure:**
```bash
gough alert escalate gough.spiffe.svid_rotation_grace_exceeded \
  --team=platform-sre \
  --reason="SVID rotation failed; intermediate CA may be compromised" \
  --ticket=<jira-ticket-id> \
  --page=true
```

---

## Related Runbooks

- [identity-conflict-nic-or-motherboard-swap.md](identity-conflict-nic-or-motherboard-swap.md) — SPIFFE identity conflicts
- [dr-drill-failed.md](dr-drill-failed.md) — DR recovery with SPIRE state

---

## Playbook Summary

| Step | Action | Time |
|------|--------|------|
| 1 | Check SPIRE server and agent status | 1 min |
| 2 | Diagnose cause (unreachable server, expired CA, or network) | 3-5 min |
| 3 | Apply targeted fix (restart server, rotate CA, or reset agents) | 5-15 min |
| 4 | Trigger SVID rotation | 1 min |
| 5 | Verify SVIDs renewed and grace period cleared | 1 min |
| **Total MTTR** | | **10-22 min** |

