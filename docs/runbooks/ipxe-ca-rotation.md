# Runbook: Rotate iPXE CA (Intermediate Certificate)

**Endpoint:** `POST /api/v1/primary/rotate-ca`  
**Severity:** MEDIUM  
**Component:** iPXE bootstrap, Vault PKI (intermediate CA)  
**SLO Impact:** No impact if scheduled during cluster idle; brief PXE boot disruption if done during provisioning  

---

## Symptoms (Why You Need This)

- Intermediate CA certificate approaching expiration (< 30 days remaining)
- Intermediate CA already expired (new nodes cannot boot)
- Suspected CA compromise (emergency rotation)
- Scheduled maintenance (routine CA refresh per security policy)
- CA revocation detected (force immediate rotation)

---

## When to Rotate

**Scheduled rotation (recommended):**
- Every 12 months (typical)
- During maintenance window (off-hours)
- When cluster is idle (no biome deployments running)

**Emergency rotation:**
- CA nearing expiry (< 7 days)
- CA already expired
- Suspected key compromise
- Regulatory/compliance requirement

---

## Prerequisites

**Before starting rotation:**

1. **Vault is unsealed** (PKI issuing capacity required):
   ```bash
   gough vault status | jq '.sealed'
   # Must be false
   ```

2. **Vault PKI is responsive:**
   ```bash
   gough vault status --json | jq '{sealed,pki_available}'
   # Both must be true/unsealed
   ```

3. **Cluster is healthy** (no quorum issues):
   ```bash
   gough cluster status --json | jq '.{healthy,quorum_status}'
   # Both must be true/healthy
   ```

4. **TFTP root is writable** (helper image storage):
   ```bash
   gough tftp status | jq '.{writable,available_gb}'
   # Must be writable with space available
   ```

5. **No active biome deployments** (no new nodes provisioning):
   ```bash
   gough deployment list --filter="status=in_progress" --json | jq '. | length'
   # Should be 0 (or very few if unavoidable)
   ```

6. **Current CA details known:**
   ```bash
   gough vault cert-info --cert=ipxe-ca | jq '{issuer,subject,expires_at}'
   ```

---

## Expected Behavior During Rotation

### Timeline

**Before rotation:** Existing helper images signed with old CA; new PXE boots trust old CA in cloud-init.

**During rotation (5–15 minutes):**

1. Vault issues new intermediate CA (30 sec)
2. Helper image is re-signed with new CA (1–2 min)
3. Helper artifact is published to TFTP (30 sec)
4. Trust anchor in bootstrap cloud-init is swapped atomically (10 sec)
5. Existing nodes continue with old trust anchor (no impact)
6. New nodes PXE-boot and fetch new helper (trust new CA)

**After rotation:** All new PXE boots trust the new CA; existing nodes unaffected.

### No Disruption Expected

- Existing nodes continue running (no restart)
- Existing nodes' cloud-init bootstrap no longer runs (already completed)
- New nodes seamlessly trust new CA (automatic)
- Zero downtime

---

## Step-by-Step Procedure

### Step 1: Verify Prerequisites

```bash
# Quick pre-flight check
gough vault status
# sealed=false, pki_available=true

gough cluster status
# healthy=true, quorum_status=healthy

gough deployment list --json | jq '.[] | select(.status=="in_progress")'
# Should return nothing (or very few)

# Check current CA expiration
gough vault cert-info --cert=ipxe-ca | jq '.{expires_at,days_remaining}'
# Example output: expires_at="2026-08-15T00:00:00Z", days_remaining=98
```

**If any check fails, resolve the issue before proceeding.** See "Troubleshooting" below.

### Step 2: Schedule a Maintenance Window (If Scheduled Rotation)

```bash
# Announce to the team (e.g., in Slack)
# "CA rotation scheduled for 2026-05-09 02:00 UTC. Expect no impact; new PXE boots will use new CA."

# Wait for acknowledgment or until off-hours
```

### Step 3: Initiate CA Rotation

```bash
# Call the rotation endpoint
curl -X POST https://gough.internal.example.com/api/v1/primary/rotate-ca \
  -H "Authorization: Bearer $(gough auth get-token)" \
  -H "Content-Type: application/json" \
  -d '{
    "rotation_reason": "scheduled_refresh",
    "force": false,
    "backup_old_ca": true
  }'

# Expected response (202 Accepted):
# {
#   "operation_id": "rotate-ca-uuid-789",
#   "status": "in_progress",
#   "phase": "issuing_new_ca",
#   "old_ca_thumbprint": "abcd1234...",
#   "new_ca_thumbprint": "ef567890...",
#   "estimated_duration_seconds": 420
# }
```

**Parameters:**
- `rotation_reason`: `scheduled_refresh`, `expiration_imminent`, `compromise_suspected`, or `compliance_requirement`
- `force`: `false` (default) waits until idle; `true` rotates immediately even if nodes are provisioning
- `backup_old_ca`: `true` (default) saves old CA to backup location for audit trail

### Step 4: Monitor Rotation Progress

```bash
# Check operation status in real-time
curl https://gough.internal.example.com/api/v1/primary/rotate-ca/<operation-id> \
  -H "Authorization: Bearer $(gough auth get-token)"

# Expected output (in progress):
# {
#   "operation_id": "rotate-ca-uuid-789",
#   "status": "in_progress",
#   "phase": "resigning_helper_image",
#   "progress_percent": 55,
#   "message": "Re-signing helper netboot image with new CA..."
# }
```

**Expected phases (in order):**

1. **issuing_new_ca** (30 sec): Vault issues new intermediate CA
2. **resigning_helper_image** (1–2 min): Helper image is re-signed with new CA
3. **publishing_to_tftp** (30 sec): New helper artifact uploaded to TFTP root
4. **swapping_trust_anchor** (10 sec): Atomic swap of CA in bootstrap cloud-init
5. **verifying_rotation** (30 sec): Spot-check new CA is in use
6. **completed**: Rotation succeeded

**Total time: 5–15 minutes.**

### Step 5: Verify Rotation Succeeded

```bash
# Check final operation status
curl https://gough.internal.example.com/api/v1/primary/rotate-ca/<operation-id> \
  -H "Authorization: Bearer $(gough auth get-token)"
# Expected: status="completed"

# Verify new CA is in place
gough vault cert-info --cert=ipxe-ca | jq '{issuer,subject,expires_at}'
# issuer should show "Gough iPXE CA" (same name, but new key)

# Compare with pre-rotation thumbprint
# Thumbprint should have changed
curl https://gough.internal.example.com/api/v1/primary/rotate-ca/<operation-id> \
  -H "Authorization: Bearer $(gough auth get-token)" | jq '.{old_ca_thumbprint,new_ca_thumbprint}'
# Should show two different values

# Check TFTP root contains new helper with new CA
gough tftp list | grep helper-image
# Listing should show recently updated helper artifact

# Test new PXE boot (optional, on test node)
gough node pxe-boot --test-node <test-node-id>
# Should fetch helper and boot successfully with new CA
```

### Step 6: Verify Existing Nodes Are Unaffected

```bash
# Spot-check a few existing nodes
gough node list --json | jq '.[] | select(.state=="ready") | {id,provisioned_at}' | head -3

# SSH into one
ssh -i <key> ubuntu@<existing-node-ip>

# Check what CA is in cloud-init (old CA should still be there)
sudo cat /var/lib/cloud/instance/boot-finished
# File exists = node completed boot already

# Check system continues running
systemctl status
# Should show all services healthy
```

---

## If Rotation Fails

### Failure: "Vault PKI not responsive"

**Cause:** Vault is sealed or unreachable.

**Fix:**

```bash
# Check Vault status
gough vault status | jq '.sealed'

# If sealed, unseal it (see vault-sealed-bootstrap-window-expired.md)
# If unreachable, check Vault pod
kubectl -n gough get pods -l app=vault

# Restart Vault pod if needed
kubectl -n gough delete pod -l app=vault
# Wait for pod to restart
sleep 30

# Retry rotation
curl -X POST https://.../api/v1/primary/rotate-ca \
  -H "Authorization: Bearer $(gough auth get-token)" \
  -H "Content-Type: application/json" \
  -d '{"rotation_reason": "scheduled_refresh", "force": false}'
```

### Failure: "Helper image re-signing failed"

**Cause:** Image artifact is missing or corrupted.

**Fix:**

```bash
# Check helper image location
gough tftp list | grep -i helper

# If missing, rebuild helper image
gough image build helper-netboot --output=/tmp/helper.iso

# Upload to TFTP
gough tftp upload /tmp/helper.iso

# Retry rotation
curl -X POST https://.../api/v1/primary/rotate-ca/<operation-id>/retry \
  -H "Authorization: Bearer $(gough auth get-token)"
```

### Failure: "TFTP root not writable"

**Cause:** TFTP storage is full or permissions issue.

**Fix:**

```bash
# Check TFTP status
gough tftp status | jq '.{writable,available_gb,usage_percent}'

# If full, clean up old artifacts
gough tftp cleanup --older-than=30d
# Removes TFTP artifacts older than 30 days

# If permissions, check TFTP owner
ls -la /var/lib/tftp/
# Should be owned by tftp:tftp (or gough:gough)

# Retry rotation
curl -X POST https://.../api/v1/primary/rotate-ca/<operation-id>/retry \
  -H "Authorization: Bearer $(gough auth get-token)"
```

### Failure: "New CA cannot be trusted in bootstrap"

**Cause:** New CA signature does not match expected root (rare).

**Fix:**

```bash
# Verify Vault root CA is intact
gough vault cert-info --cert=root-ca | jq '.subject'

# Compare root CA with backup
gough vault cert-list --filter="type=root" | jq '.[]' | head -1

# If root CA is compromised, contact PenguinTech support — do not proceed
# Otherwise, retry rotation
curl -X POST https://.../api/v1/primary/rotate-ca/<operation-id>/retry \
  -H "Authorization: Bearer $(gough auth get-token)"
```

---

## Rollback (Undo Rotation)

If rotation causes new PXE boots to fail, you can rollback to the old CA:

```bash
# List all CA rotation operations
gough vault cert-list --filter="type=intermediate,role=ipxe" --json | jq '.[] | {issued_at,expires_at}'

# Find the old CA (most recent before rotation)
OLD_CA_THUMBPRINT="abcd1234..."  # From pre-rotation output above

# Rollback to old CA
curl -X POST https://gough.internal.example.com/api/v1/primary/rotate-ca/rollback \
  -H "Authorization: Bearer $(gough auth get-token)" \
  -H "Content-Type: application/json" \
  -d '{"old_ca_thumbprint": "'"$OLD_CA_THUMBPRINT"'"}'

# Expected response:
# {
#   "operation_id": "rotate-ca-rollback-uuid",
#   "status": "completed",
#   "message": "Rolled back to previous CA; TFTP helper re-signed"
# }
```

**After rollback:**
- New PXE boots will again trust the old CA
- Next rotation can be scheduled for a different time

---

## Audit Trail

All CA rotations are logged:

```bash
# View CA rotation events
gough audit list --filter="action=ipxe_ca_rotate" --limit=10

# Example audit entry:
# {
#   "timestamp": "2026-05-09T02:15:30Z",
#   "actor": "sre@penguintech.io",
#   "action": "ipxe_ca_rotate",
#   "resource_type": "certificate",
#   "resource_id": "ipxe-intermediate-ca",
#   "details": {
#     "operation_id": "rotate-ca-uuid-789",
#     "reason": "scheduled_refresh",
#     "old_ca_thumbprint": "abcd1234...",
#     "new_ca_thumbprint": "ef567890...",
#     "status": "completed",
#     "duration_seconds": 523
#   }
# }
```

---

## Related Runbooks

- `/docs/runbooks/vault-sealed-bootstrap-window-expired.md` — Unseal Vault if needed
- `/docs/runbooks/phase2-cloud-init-failure.md` — Troubleshoot bootstrap failures (may be CA-related)

