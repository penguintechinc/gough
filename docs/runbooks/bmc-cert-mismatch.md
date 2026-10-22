# Runbook: BMC Certificate Mismatch

**Alert Name:** `gough.bmc.certificate_mismatch_detected`  
**Severity:** WARNING  
**Component:** BMC certificate validation  
**SLO Impact:** Node provisioning warnings; may block PXE boot  

---

## Symptoms

- Prometheus alert: `gough.bmc.certificate_mismatch_detected{node_id="...",bmc_model="..."}`
- `gough node bmc-verify --node-id=<id>` shows certificate mismatch
- WebUI shows yellow warning on node row: "BMC cert mismatch"
- Node provisioning pauses pending operator review
- Logs: "BMC certificate thumbprint changed; manual approval required"

---

## Detection

**Metrics:**
```bash
gough.bmc.certificate_mismatch_detected{...} == 1
gough.bmc.cert_thumbprint_changed{...} == 1
```

**CLI verification:**
```bash
# Check BMC cert mismatch details
gough node bmc-verify --node-id=<id> --json | jq '{mismatch, current_thumbprint, expected_thumbprint, node_model}'

# List all nodes with cert mismatches
gough node list --filter="bmc_cert_mismatch=true" --json | jq '.[] | {id,bmc_model,last_cert_update}'

# Inspect BMC entry in database
psql -U postgres -d gough -c "SELECT id, bmc_model, bmc_cert_thumbprint, cert_verified_at FROM cloud_machines WHERE id = '<node_id>';"
```

---

## Severity Assessment

**WARNING.** BMC certificate mismatch is expected behavior when BMC firmware is updated or replaced. This is a procedural gate, not a security failure. Operator review required before proceeding.

---

## First 30 Seconds

1. Determine if this is expected (e.g., BMC firmware upgrade, motherboard swap).
2. If unexpected, check BMC logs for intrusions or unauthorized access.
3. If expected, operator can approve to proceed.

---

## Diagnosis

**Two plausible causes:**

### Cause 1: Expected BMC Change (80% likelihood)
**Symptoms:** Node was recently serviced (BMC firmware updated, motherboard replaced, or BMC reset performed).

**Check:**
```bash
# Get change history
gough node audit-log --node-id=<id> --limit=10 | grep -i "firmware\|motherboard\|bmc\|certificate"

# Check when cert last changed
psql -U postgres -d gough -c "SELECT created_at, action, details FROM audit_log WHERE node_id = '<id>' AND action LIKE '%cert%' ORDER BY created_at DESC LIMIT 5;"

# Inspect BMC event log
ipmitool -I lanplus -H <bmc_ip> -U <user> -P <pass> sel list | head -20
```

**If this was a known change:**
- Operator should review the change and approve.

---

### Cause 2: Unexpected BMC Change (Security Concern) (20% likelihood)
**Symptoms:** No known BMC changes; certificate changed without authorization.

**Check:**
```bash
# Check BMC access logs for suspicious activity
ipmitool -I lanplus -H <bmc_ip> -U <user> -P <pass> user list

# Check for recent configuration changes
ipmitool -I lanplus -H <bmc_ip> -U <user> -P <pass> channel info 1

# Inspect BMC firmware version
ipmitool -I lanplus -H <bmc_ip> -U <user> -P <pass> mc info | grep -i "firmware\|version"

# Check for failed authentication attempts
ipmitool -I lanplus -H <bmc_ip> -U <user> -P <pass> session info | head -20
```

**If unauthorized changes detected:**
- Escalate to security team immediately (see Escalation).
- Do NOT approve provisioning until cleared.

---

## Resolution

**For Cause 1 (Expected change):**
```bash
# Option A: Approve cert mismatch via WebUI
# Navigate to node row → click "Approve BMC Cert" button

# Option B: Approve via CLI
gough node bmc-approve --node-id=<id> --reason="Firmware upgraded on $(date)"

# Verify approval
gough node bmc-verify --node-id=<id>
# Should show: mismatch=false

# Update expected thumbprint in database
psql -U postgres -d gough -c "UPDATE cloud_machines SET bmc_cert_thumbprint = '<new_thumbprint>', cert_verified_at = NOW() WHERE id = '<id>';"

# Resume provisioning
gough node provision-phase-1 --node-id=<id>

# Monitor progress
kubectl -n gough logs -l app=ipxe-handler,node-id=<id> --follow
```

**For Cause 2 (Unexpected change):**
```bash
# Step 1: Do NOT approve
# Alert security team

# Step 2: Quarantine node
gough node quarantine --node-id=<id> --reason="Unauthorized BMC cert change; security investigation underway"

# Step 3: Preserve evidence
gough node bmc-forensics --node-id=<id> > /backup/bmc_forensics_<id>_$(date +%s).log

# Step 4: Power off node (isolate)
ipmitool -I lanplus -H <bmc_ip> -U <user> -P <pass> power off

# Step 5: Escalate to security team
gough alert escalate gough.bmc.certificate_mismatch_detected \
  --team=security-team \
  --reason="Unauthorized BMC cert change; node $id quarantined" \
  --ticket=<jira-ticket-id> \
  --page=true
```

---

## Verification

**Success criteria (for expected change):**
1. BMC cert mismatch is approved.
2. Node provisioning resumes successfully.
3. Node transitions to `ready` state.
4. Alert clears in Prometheus.

**Success criteria (for unexpected change):**
1. Node is quarantined.
2. Security team has been notified.
3. Evidence is preserved.

---

## Prevention

1. **Document BMC maintenance procedures:**
   - Create runbook for firmware updates, motherboard swaps.
   - Always notify ops team before BMC changes.
   - Update BMC cert thumbprint immediately after change.

2. **Monitor BMC cert changes:**
   ```bash
   # Alert for unexpected BMC changes
   gough alert set gough.bmc.certificate_change \
     --threshold=1 \
     --duration=1m \
     --severity=warning
   ```

3. **Secure BMC access:**
   - Use strong credentials (stored in Vault).
   - Limit BMC network access (firewall rules).
   - Monitor BMC for unauthorized login attempts.

4. **Test BMC cert verification:**
   - Monthly: verify all node BMC certs match expected thumbprints.
   - `gough node bmc-verify --all`

---

## Escalation

**Escalate to:** Security Team  
**When:**
- BMC cert change is unauthorized or unexplained.
- Multiple nodes show unexpected cert changes.
- BMC logs show intrusion attempts or unauthorized access.

---

## Related Runbooks

- [identity-conflict-nic-or-motherboard-swap.md](identity-conflict-nic-or-motherboard-swap.md) — Motherboard swap impacts

---

## Playbook Summary

| Step | Action | Time |
|------|--------|------|
| 1 | Determine if cert change is expected | 1-2 min |
| 2 | Review change history and BMC logs | 2-3 min |
| 3 | Approve (if expected) or escalate (if unexpected) | 1 min |
| 4 | Resume provisioning or quarantine node | 1 min |
| **Total MTTR** | | **5-7 min** |

