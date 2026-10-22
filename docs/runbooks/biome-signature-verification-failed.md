# Runbook: Biome Signature Verification Failed

**Alert Name:** `gough.biomes.signature_verification_failed`  
**Severity:** CRITICAL (prod) / WARNING (staging)  
**Component:** Biome verification (cosign/SBOM)  
**SLO Impact:** Cannot deploy Biomes; possible tamper or supply chain compromise  

---

## Symptoms

- Prometheus alert: `gough.biomes.signature_verification_failed{biome_id="...",environment="prod"}`
- `gough biome deploy <biome_id>` fails with "signature verification failed"
- Biome SBOM cannot be validated
- Operator sees error: "Biome image signature does not match; abort deployment"
- Logs: "cosign verification failed: invalid signature"

---

## Detection

**Metrics:**
```bash
gough.biomes.signature_verification_failed{...} == 1
gough.biomes.sbom_validation_failed{...} == 1
```

**CLI verification:**
```bash
# Verify signature of a specific biome
gough biome verify --biome-id=<id> --verify-signature=true

# Check cosign public key availability
gough crypto keys list --key-type=cosign-public | jq '.[] | {key_id, algorithm, public_key_thumbprint}'

# Inspect biome image in registry
gough registry inspect-image --image=<egg_image>:<tag> --json | jq '{digest, signatures, sbom_present}'
```

---

## Severity Assessment

**CRITICAL (production).** Biome signature verification is the final defense against supply chain compromise. Failures in prod must be treated as potential security incidents.

**WARNING (staging).** In staging, signature failures are expected during development. Allow signature override for testing.

---

## First 30 Seconds

1. **Determine environment (prod vs staging):**
   - Prod: do NOT override; escalate immediately.
   - Staging: may override if signature is temporarily unavailable.
2. Check if this biome was recently published.
3. Verify cosign public key is current and accessible.

---

## Diagnosis

**Three plausible causes:**

### Cause 1: Biome Image Not Signed (55% likelihood, staging)
**Symptoms:** Biome image exists but has no cosign signature.

**Check:**
```bash
# Verify biome has signature
gough biome verify --biome-id=<id> --json | jq '.signature_present'

# Check registry for signature artifacts
gough registry inspect-image --image=<egg_image>:<tag> --show-signatures

# Check biome publish process logs
gough biome log --biome-id=<id> --filter="action=publish" --json | jq '.[] | {timestamp,status,error}'
```

**If signature is missing:**
- Biome publish may have failed mid-way.
- Or signature was not included in build pipeline.

---

### Cause 2: Cosign Public Key Mismatch or Rotation (30% likelihood)
**Symptoms:** Biome was signed with key version N, but only versions N-1 or N+1 are available.

**Check:**
```bash
# Get biome signature key version
gough biome verify --biome-id=<id> --json | jq '.signing_key_version'

# Check available cosign keys
gough crypto keys list --key-type=cosign-public --json | jq '.[] | {key_version, algorithm, created_at, is_active}'

# Check for pending key rotations
gough crypto rotation-status --key-type=cosign | jq '.[] | {status, old_version, new_version}'
```

**If key versions mismatch:**
- Key rotation may have occurred without updating all signatures.
- Or biome was built with an old/future key version.

---

### Cause 3: Biome Image Tampered or Supply Chain Issue (15% likelihood)
**Symptoms:** Signature exists but verification fails; image digest has changed.

**Check:**
```bash
# Get image digest from registry
DIGEST=$(gough registry inspect-image --image=<egg_image>:<tag> --json | jq -r '.digest')

# Get digest from biome manifest in database
psql -U postgres -d gough -c "SELECT image_digest, image_ref FROM biomes WHERE id = '<biome_id>';"

# Compare digests
echo "Registry: $DIGEST"
psql -U postgres -d gough -c "SELECT image_digest FROM biomes WHERE id = '<biome_id>';" | tail -1

# Check image build logs for tampering signs
gough biome build-log --biome-id=<id> --format=json | jq '.[] | {timestamp, action, status}' | tail -20
```

**If digests mismatch:**
- Image was modified after signing (supply chain compromise).
- Or database record is stale.

---

## Resolution

**For Cause 1 (Missing signature, staging only):**
```bash
# Step 1: Republish biome with signature
gough biome publish --biome-id=<id> --force-resign

# Step 2: Monitor republish progress
kubectl -n gough logs -l app=biome-publisher --since=1m | grep <biome_id>

# Step 3: Verify signature
gough biome verify --biome-id=<id> --verify-signature=true

# Step 4: Try deployment again
gough biome deploy --biome-id=<id>
```

**For Cause 2 (Key version mismatch):**
```bash
# Step 1: Update cosign key to latest version
gough crypto update-public-key --key-type=cosign --version=latest

# Step 2: Wait for key cache to refresh
sleep 30

# Step 3: Retry verification
gough biome verify --biome-id=<id> --verify-signature=true

# Step 4: If still failing, re-sign biome with current key
gough biome resign --biome-id=<id> --key-version=latest

# Step 5: Retry deployment
gough biome deploy --biome-id=<id>
```

**For Cause 3 (Image tampered, prod only):**
```bash
# Step 1: Do NOT deploy
# Step 2: Escalate to security team immediately
gough alert escalate gough.biomes.signature_verification_failed \
  --team=security-team \
  --reason="Biome image signature mismatch; possible tampering" \
  --ticket=<jira-ticket-id> \
  --page=true

# Step 3: Preserve evidence
gough biome forensics --biome-id=<id> > /backup/egg_forensics_<id>_$(date +%s).log

# Step 4: Quarantine biome
gough biome quarantine --biome-id=<id> --reason="Signature verification failed; security investigation"
```

---

## Verification

**Success criteria:**
1. `gough biome verify --biome-id=<id>` succeeds with signature verification.
2. Biome can be deployed without signature warnings.
3. Alert clears in Prometheus.

---

## Prevention

1. **Automate biome signature verification in CI:**
   - All biomes must be signed before publication.
   - Verification is mandatory before tagging as latest.

2. **Monitor cosign key rotation:**
   ```bash
   # Alert for key rotation events
   gough alert set gough.crypto.cosign_key_rotated \
     --threshold=1 \
     --duration=5m \
     --severity=warning
   ```

3. **Test signature verification regularly:**
   ```bash
   # Weekly test
   0 2 * * 0 /usr/local/bin/gough biome verify --all --verify-signature=true
   ```

4. **Document signature verification procedure:**
   - Runbook for manual biome signing.
   - Key escrow procedure (Vault storage).
   - SBOM generation and validation.

---

## Escalation

**Escalate to:** Security Team  
**When:**
- Image digest mismatch (possible tampering).
- Multiple biomes show signature failures.
- Cosign key is compromised or unavailable.

---

## Related Runbooks

- [helper-image-sbom-verification-failed.md](helper-image-sbom-verification-failed.md) — SBOM validation failures

---

## Playbook Summary

| Step | Action | Time |
|------|--------|------|
| 1 | Check if signature is present on biome image | 1 min |
| 2 | Verify cosign public key version matches | 1 min |
| 3 | Re-sign biome or update key | 2-5 min |
| 4 | Retry deployment or escalate to security | 1 min |
| **Total MTTR** | | **5-8 min** |
