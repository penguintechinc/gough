# Runbook: Vault Sealed + Bootstrap Window Expired

**Alert Name:** `gough.vault.bootstrap_window_expired`  
**Severity:** CRITICAL  
**Component:** Vault initialization / PKI  
**SLO Impact:** Cluster cannot bootstrap new nodes; certificate renewal blocked  

---

## Symptoms

- Prometheus alert: `gough.vault.bootstrap_window_expired{severity="critical"}`
- `gough vault status` shows `sealed=true` and `bootstrap_available=false`
- New nodes cannot join cluster (cloud-init fails with "vault unsealing failed")
- Certificate rotation pipeline stalls (intermediate CA cannot be renewed)
- Operator logs show "Bootstrap window: expired"

---

## Detection

**Primary metrics:**
```
gough.vault.sealed{cluster_id="..."} == 1
gough.vault.bootstrap_window_remaining_seconds{...} <= 0
```

**CLI verification (first 30 seconds):**
```bash
# Check Vault status
gough vault status --json | jq '{sealed,bootstrap_available,bootstrap_window_expires_at}'

# Check if bootstrap tokens exist in database
psql -U postgres -d gough -c "SELECT id, created_at, expires_at, used FROM vault_bootstrap_tokens ORDER BY expires_at DESC LIMIT 5;"

# Check for nodes waiting to join
kubectl -n gough get nodes | grep NotReady
gough node list --filter="state=joining"

# Inspect Vault pod logs
kubectl -n gough logs -l app=vault --tail=50 | grep -i "sealed\|bootstrap"
```

**Expected on healthy system:**
- `sealed=false`
- `bootstrap_available=true`
- `bootstrap_window_expires_at` is > 24h in future
- All nodes show Ready state

---

## Severity Assessment

**CRITICAL.** Bootstrap window expiry blocks cluster expansion and certificate renewal. This must be resolved before any new nodes can join. No new Biomes can be deployed until Vault is unsealed and bootstrap window is refreshed.

---

## First 30 Seconds

1. **Do NOT attempt to unseal Vault yet** — bootstrap window may be recoverable without full unseal.
2. Run verification commands above.
3. Check if Vault pod is running: `kubectl -n gough get pods -l app=vault`
4. If Vault pod is down: restart it (see Cause 2 below).
5. If Vault is up but sealed: proceed to Diagnosis.

---

## Diagnosis

**Three plausible causes:**

### Cause 1: Bootstrap Window Legitimately Expired (50% likelihood)
**Symptoms:** Vault is unsealed; bootstrap tokens table shows all entries with `expires_at < now()`.

**Check:**
```bash
# Verify Vault is actually unsealed
gough vault status | grep sealed
# Should show: sealed=false

# Check bootstrap tokens
psql -U postgres -d gough -c "SELECT COUNT(*) as total, \
  COUNT(CASE WHEN expires_at > NOW() THEN 1 END) as valid, \
  COUNT(CASE WHEN expires_at <= NOW() THEN 1 END) as expired \
  FROM vault_bootstrap_tokens;"

# Check when bootstrap window was created
psql -U postgres -d gough -c "SELECT MIN(created_at), MAX(expires_at) FROM vault_bootstrap_tokens;"
```

**If Vault unsealed, all tokens expired, but cluster running normally:**
- This is expected behavior. Bootstrap window expires after successful cluster onboarding.
- If no new nodes need to join: this alert is false-positive (see Prevention section).

**If Vault unsealed and you need to add new nodes:**
- Bootstrap window must be refreshed (see Resolution).

---

### Cause 2: Vault Pod Crashed or Restarted (30% likelihood)
**Symptoms:** Vault pod in CrashLoopBackOff or recently restarted; `sealed=true` (sealed after restart).

**Check:**
```bash
# Check Vault pod status
kubectl -n gough get pods -l app=vault -o wide

# Check pod restart count
kubectl -n gough get pods -l app=vault -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}'

# Check recent pod events
kubectl -n gough describe pod -l app=vault | grep -A 10 "Events:"

# Check Vault logs
kubectl -n gough logs -l app=vault --tail=100 | tail -50
```

**If restart count > 5:**
- Vault is in crash loop. Check logs for error.
- Common causes: PVC not mounted, corrupted data, permission denied.

**Resolution (if pod restarted):**
```bash
# Step 1: Verify PVC is mounted
kubectl -n gough get pvc -l app=vault

# Step 2: Check PVC status
kubectl -n gough describe pvc vault-data | grep -E "Status|Message"

# Step 3: If PVC is pending, check PV
kubectl get pv | grep vault

# Step 4: Unseal Vault
# Use Shamir shares OR Seal Wrap key (see Cause 3)
gough vault unseal --shamir-share=<share1> --shamir-share=<share2> --shamir-share=<share3>

# Step 5: Verify unseal worked
gough vault status | grep sealed
# Should show: sealed=false

# Step 6: Refresh bootstrap window
gough vault refresh-bootstrap-window --duration=168h
```

---

### Cause 3: Vault Sealed (True Unseal Required) (20% likelihood)
**Symptoms:** Vault pod is running; `sealed=true`; restart count is low or 0.

**Check:**
```bash
# Confirm Vault is actually sealed
gough vault status

# Check if Seal Wrap key is available
ls -la /etc/gough/vault/seal-wrap-key
# Should exist if auto-unseal is configured

# Check for Shamir shares in secure storage
gough vault shamir-shares --list
# Should show available shares and threshold
```

**Sealed scenarios:**

**Scenario 3a: Seal Wrap key available (auto-unseal configured):**
```bash
# Vault should auto-unseal. If not, manually trigger
gough vault unseal --use-seal-wrap-key

# Verify
gough vault status | grep sealed
# Should show: sealed=false
```

**Scenario 3b: Only Shamir shares available:**
```bash
# Requires Shamir threshold shares (typically 3 of 5)
# Retrieve shares from secure storage (e.g., Sealed Secrets ConfigMap, KMS)
gough vault unseal --shamir-share=<share1> --shamir-share=<share2> --shamir-share=<share3>

# If shares are in Sealed Secrets:
kubectl -n gough get sealedsecret vault-shamir-shares -o jsonpath='{.spec.encryptedData}' | \
  kubeseal -d --raw | jq -r '.shares[]'
```

**Scenario 3c: Cannot find Shamir shares or Seal Wrap key:**
- Escalate to SRE (see Escalation).
- This indicates a serious operational failure or backup loss.

---

## Resolution

**Step 1: Determine which cause applies (from Diagnosis above).**

**Step 2: Apply targeted fix:**

**For Cause 1 (Bootstrap window legitimately expired, no new nodes needed):**
```bash
# Suppress alert if this is expected behavior
gough alert silence gough.vault.bootstrap_window_expired --duration 720h --reason "Bootstrap window expired; no cluster expansion planned"

# If NEW nodes need to join later, explicitly refresh
gough vault refresh-bootstrap-window --duration=168h --reason "Preparing for node expansion"
```

**For Cause 2 (Vault pod restarted):**
```bash
# Unseal Vault (use Seal Wrap key or Shamir shares)
gough vault unseal --use-seal-wrap-key

# Verify
kubectl -n gough logs -l app=vault --tail=10 | grep -i "unsealed\|ready"

# Check Vault API is responding
kubectl -n gough exec -it <vault-pod> -- vault status

# Refresh bootstrap window
gough vault refresh-bootstrap-window --duration=168h
```

**For Cause 3 (True unseal required):**
```bash
# Unseal with Shamir shares
gough vault unseal --shamir-share=<share1> --shamir-share=<share2> --shamir-share=<share3>

# Verify unsealed
gough vault status | jq '{sealed,initialized,supported_seal_types}'

# Refresh bootstrap window
gough vault refresh-bootstrap-window --duration=168h

# Notify operator to store shares securely
gough vault shamir-shares --verify
```

**Step 3: Verify bootstrap window is refreshed:**
```bash
# Check bootstrap window
gough vault status --json | jq '.bootstrap_window_expires_at'
# Should show future timestamp (e.g., 7 days from now)

# Verify alert clears in Prometheus
curl -s 'http://localhost:9090/api/v1/query?query=gough.vault.bootstrap_window_expired' | jq '.data.result'
# Should return empty array
```

**Step 4: Test bootstrap on new node (optional, if nodes are waiting):**
```bash
# Trigger Phase-1 on a waiting node to verify it can use bootstrap token
gough node provision-phase-2 --node-id=<id>

# Monitor Phase-2 progress
kubectl -n gough logs -f -l app=cloud-init,node-id=<id>

# Verify node transitions to Ready
kubectl get nodes <node-name> -w
```

---

## Verification

**Success criteria:**
1. `gough vault status | grep sealed` shows `sealed=false`.
2. `gough vault status | jq .bootstrap_window_expires_at` shows future timestamp.
3. Alert `gough.vault.bootstrap_window_expired` is no longer firing.
4. If nodes were waiting to join: they successfully transition to Ready state.
5. Certificate renewal pipeline is unblocked (check `gough cert status`).

**Persistent validation:**
```bash
# Monitor bootstrap window expiry
watch -n 60 'gough vault status --json | jq ".bootstrap_window_expires_at, .sealed"'

# Verify no new Vault seal events
kubectl -n gough logs -l app=vault --since=1h | grep -i "sealed" | tail -5

# Check that bootstrap tokens are being used (for new node joins)
psql -U postgres -d gough -c "SELECT COUNT(CASE WHEN used=true THEN 1 END) as used, \
  COUNT(CASE WHEN used=false THEN 1 END) as unused FROM vault_bootstrap_tokens WHERE expires_at > NOW();"
```

---

## Prevention

1. **Automate bootstrap window refresh before expiry:**
   ```bash
   # Cron job to refresh weekly (7 days before expiry)
   0 0 * * 0 /usr/local/bin/gough vault refresh-bootstrap-window --duration=168h
   ```

2. **Monitor bootstrap window proactively:**
   - Alert threshold: `bootstrap_window_remaining_seconds < 604800` (7 days)
   - Alert escalates as window approaches expiry

3. **Store Shamir shares securely:**
   - Use Sealed Secrets or Vault's own encryption
   - Document retrieval procedure for on-call SRE
   - Test unsealing quarterly in staging

4. **Document Vault unseal procedure:**
   - Create runbook for share retrieval
   - Train all SREs on manual unseal steps
   - Test quarterly during DR drills

5. **Monitor Vault pod restarts:**
   - Alert on pod restart count > 3 in 1h
   - Investigate storage issues immediately

---

## Escalation

**Escalate to:** Platform SRE, Vault Operator  
**When:**
- Shamir shares or Seal Wrap key cannot be found.
- Vault refuses to unseal even with correct shares.
- PVC is corrupted or inaccessible.
- Multiple unsealing attempts fail.

**Escalation procedure:**
```bash
gough alert escalate gough.vault.bootstrap_window_expired \
  --team=platform-sre \
  --reason="Cannot unseal Vault; shares not found" \
  --ticket=<jira-ticket-id> \
  --page=true
```

**PagerDuty:** Alert should page on-call SRE immediately if not resolved within 5 minutes.

---

## Related Runbooks

- [quorum-loss.md](quorum-loss.md) — Cluster consensus loss (may seal Vault)
- [joiner-secret-decryption-failure.md](joiner-secret-decryption-failure.md) — Bootstrap token decryption errors
- [dr-drill-failed.md](dr-drill-failed.md) — DR recovery from sealed Vault

---

## Playbook Summary

| Step | Action | Time |
|------|--------|------|
| 1 | Check `gough vault status` | 30s |
| 2 | Verify Vault pod is running | 30s |
| 3 | Diagnose cause (expired window, pod restart, or sealed) | 2-5 min |
| 4 | Unseal Vault (if needed) with Shamir shares or Seal Wrap | 2-10 min |
| 5 | Refresh bootstrap window | 1 min |
| 6 | Verify alert clears and bootstrap tokens are valid | 1 min |
| **Total MTTR** | | **6-16 min** |

