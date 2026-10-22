# Runbook: Joiner Secret Decryption Failure

**Alert Name:** `gough.joiner_secrets.decryption_failure`  
**Severity:** CRITICAL  
**Component:** Joiner secrets (bootstrapping new nodes)  
**SLO Impact:** New nodes cannot bootstrap; cluster expansion blocked  

---

## Symptoms

- Prometheus alert: `gough.joiner_secrets.decryption_failure{node_id="..."}`
- Node logs show "failed to decrypt joiner secret" during Phase-2
- New node transitions to `failed` state
- Cloud-init Phase-2 exits with error code 1
- `gough node list --state=failed | head -10`

---

## Detection

**Primary metric:**
```bash
gough.joiner_secrets.decryption_failure{...} == 1
gough.joiner_secrets.decryption_attempts_failed_total >= 1
```

**CLI verification (first 30 seconds):**
```bash
# Check failed nodes
gough node list --state=failed --json | jq '.[] | {id,phase,error_message,created_at}'

# Inspect joiner secret in database
psql -U postgres -d gough -c "SELECT id, rotation_class, encrypted_secret, key_version FROM joiner_secrets WHERE created_at > NOW() - INTERVAL '1 hour' LIMIT 5;"

# Check decryption key versions
gough secret-key list --json | jq '.[] | {rotation_class,current_version,previous_version}'

# Check node Phase-2 logs
kubectl -n gough logs -l app=cloud-init --since=10m | grep -i "decrypt\|joiner\|failed"
```

---

## Severity Assessment

**CRITICAL.** Joiner secrets are required for new nodes to bootstrap into the cluster. Decryption failure blocks cluster expansion completely. This may indicate key rotation issues or key loss.

---

## First 30 Seconds

1. **Do NOT retry provisioning failed nodes yet** — this may cause key version mismatches.
2. Check decryption key versions and ensure latest version is accessible.
3. Verify Vault is unsealed and accessible.
4. Determine if this is a single-node issue or cluster-wide.

---

## Diagnosis

**Three plausible causes:**

### Cause 1: Decryption Key Version Mismatch (55% likelihood)
**Symptoms:** Node received joiner secret encrypted with key version N, but only versions N-1 and earlier are available.

**Check:**
```bash
# Get the failed node's joiner secret key version
FAILED_NODE_ID="<node_id>"
psql -U postgres -d gough -c "SELECT key_version FROM joiner_secrets WHERE node_id = '$FAILED_NODE_ID' LIMIT 1;"

# Check available decryption key versions
gough secret-key list --rotation-class=joiner --json | jq '.versions | keys'

# Check current rotation class version
gough secret-key current-version --rotation-class=joiner

# Check for pending key rotations
gough secret-key rotation-status --json | jq '.[] | select(.rotation_class=="joiner")'
```

**If key version is missing:**
- Key rotation may have occurred, and old versions were purged prematurely.
- Or key rotation is in-progress and not all nodes have updated yet.

---

### Cause 2: Vault Inaccessible During Secret Encryption (30% likelihood)
**Symptoms:** Secret-rotation service logs show "vault unreachable" around the time node provisioning started.

**Check:**
```bash
# Check if Vault is sealed
gough vault status | jq '{sealed,initialized}'

# Check Vault reachability from secret-rotation service
kubectl -n gough exec -it <secret-rotation-pod> -- \
  curl -v https://vault.gough.svc.cluster.local:8200/v1/sys/health

# Check secret-rotation service logs
kubectl -n gough logs -l app=secret-rotation --since=30m | grep -i "vault\|unreachable\|error"

# Check if secret-rotation pod restarted
kubectl -n gough get pods -l app=secret-rotation -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}'
```

**If Vault was unreachable:**
- Joiner secrets may not have been encrypted with the latest key version.
- Or encryption was skipped entirely.

---

### Cause 3: Corrupted Joiner Secret in Database (15% likelihood)
**Symptoms:** Joiner secret exists but is malformed (NULL, truncated, or non-base64).

**Check:**
```bash
# Inspect the specific joiner secret
FAILED_NODE_ID="<node_id>"
psql -U postgres -d gough -c "SELECT id, encrypted_secret, key_version, created_at FROM joiner_secrets WHERE node_id = '$FAILED_NODE_ID' LIMIT 1;"

# Try to base64-decode the secret
psql -U postgres -d gough -c "SELECT encrypted_secret FROM joiner_secrets WHERE node_id = '$FAILED_NODE_ID'" | \
  base64 --decode > /tmp/decoded_secret 2>&1

# Check if it's valid
file /tmp/decoded_secret
# Should show: data (binary) or similar

# Check if secret is truncated (should be > 256 bytes)
psql -U postgres -d gough -c "SELECT LENGTH(encrypted_secret) FROM joiner_secrets WHERE node_id = '$FAILED_NODE_ID';"
```

**If secret is malformed:**
- Database write was interrupted or corrupted.
- Or encryption process failed partway through.

---

## Resolution

**Step 1: Determine which cause applies.**

**Step 2: Apply targeted fix:**

**For Cause 1 (Key version mismatch):**
```bash
# Step 1: Check if old key version can be recovered
gough secret-key restore --rotation-class=joiner --version=<N> --from-backup

# Step 2: If old key not available, re-encrypt secrets with current key
gough secret-key rotate --rotation-class=joiner --re-encrypt-all

# Step 3: Wait for rotation to complete
kubectl -n gough logs -l app=secret-rotation --follow | grep -i "rotation complete"

# Step 4: Retry failed node provisioning
FAILED_NODE_ID="<node_id>"
gough node provision-phase-2 --node-id=$FAILED_NODE_ID

# Step 5: Monitor provisioning progress
kubectl -n gough logs -l app=cloud-init,node-id=$FAILED_NODE_ID --follow
```

**For Cause 2 (Vault inaccessible during encryption):**
```bash
# Step 1: Verify Vault is now accessible
gough vault status | jq '.sealed'
# Should show: false

# Step 2: Re-generate joiner secrets for all failed nodes
for NODE_ID in $(gough node list --state=failed --json | jq -r '.[].id'); do
  gough secret create-joiner --node-id=$NODE_ID --force
done

# Step 3: Retry failed node provisioning
gough node list --state=failed --json | jq -r '.[].id' | while read NODE_ID; do
  gough node provision-phase-2 --node-id=$NODE_ID
done

# Step 4: Monitor progress
watch -n 10 'gough node list --state=failed | wc -l'
```

**For Cause 3 (Corrupted secret):**
```bash
# Step 1: Backup corrupted secret for forensics
FAILED_NODE_ID="<node_id>"
psql -U postgres -d gough -c "SELECT * FROM joiner_secrets WHERE node_id = '$FAILED_NODE_ID'" > /backup/joiner_secret_corrupted_$FAILED_NODE_ID.sql

# Step 2: Delete corrupted secret
psql -U postgres -d gough -c "DELETE FROM joiner_secrets WHERE node_id = '$FAILED_NODE_ID';"

# Step 3: Create new joiner secret
gough secret create-joiner --node-id=$FAILED_NODE_ID

# Step 4: Retry provisioning
gough node provision-phase-2 --node-id=$FAILED_NODE_ID

# Step 5: Verify success
kubectl -n gough logs -l app=cloud-init,node-id=$FAILED_NODE_ID | grep -i "bootstrap.*success"
```

**Step 3: Verify joiner secrets can be decrypted:**
```bash
# Check that no new decryption failures occur
gough node list --state=failed | wc -l
# Should be 0

# Verify alert clears
curl -s 'http://localhost:9090/api/v1/query?query=gough.joiner_secrets.decryption_failure' | jq '.data.result | length'
# Should be 0

# Test new node provisioning
gough node simulate --phase=1,2,3 --node-count=1 --monitor
# Should complete without decryption errors
```

---

## Verification

**Success criteria:**
1. All failed nodes transition to `ready` state.
2. Alert `gough.joiner_secrets.decryption_failure` is no longer firing.
3. New nodes can be provisioned without decryption errors.
4. Joiner secrets table has no NULL or corrupted entries.

**Persistent validation:**
```bash
# Monitor for new decryption failures
gough metrics export --query 'gough.joiner_secrets.decryption_failure' --from=-24h | jq '.data | length'
# Should be 0

# Verify key rotation is current
gough secret-key list --json | jq '.current_version'

# Test joiner secret creation and decryption
gough secret test-create-decrypt --rotation-class=joiner --count=5
# Should succeed with 5 successful decryptions
```

---

## Prevention

1. **Monitor key version availability:**
   ```bash
   # Alert if any old key versions are missing
   gough alert set gough.secret_key.version_missing \
     --threshold=1 \
     --duration=5m \
     --severity=critical
   ```

2. **Test secret encryption/decryption regularly:**
   ```bash
   # Weekly test
   0 2 * * 0 /usr/local/bin/gough secret test-create-decrypt --rotation-class=joiner --count=10
   ```

3. **Monitor Vault accessibility from secret-rotation service:**
   ```bash
   # Alert if Vault is unreachable
   gough alert set gough.secret_rotation.vault_unreachable \
     --threshold=1 \
     --duration=1m \
     --severity=critical
   ```

4. **Backup joiner secrets regularly:**
   ```bash
   # Daily backup
   0 1 * * * pg_dump gough -t joiner_secrets | gzip > /backup/joiner_secrets_$(date +\%Y\%m\%d).sql.gz
   ```

5. **Document key rotation procedure:**
   - Create runbook for manual key rotation.
   - Test rotation in staging before production.
   - Keep history of old key versions for forensics.

---

## Escalation

**Escalate to:** Platform SRE, Security Team  
**When:**
- Key rotation fails or old keys cannot be recovered.
- Multiple nodes fail with different key version mismatches.
- Corrupted secrets are found in production.

**Escalation procedure:**
```bash
gough alert escalate gough.joiner_secrets.decryption_failure \
  --team=platform-sre \
  --reason="Key version mismatch; possible rotation failure" \
  --ticket=<jira-ticket-id> \
  --page=true
```

---

## Related Runbooks

- [vault-sealed-bootstrap-window-expired.md](vault-sealed-bootstrap-window-expired.md) — Vault unsealing affects secret encryption
- [phase2-cloud-init-failure.md](phase2-cloud-init-failure.md) — Broader Phase-2 failure diagnosis
- [lxd-join-token-expiry.md](lxd-join-token-expiry.md) — LXD join token issues related to joiner secrets

---

## Playbook Summary

| Step | Action | Time |
|------|--------|------|
| 1 | Identify failed nodes and check joiner secret key versions | 1 min |
| 2 | Diagnose cause (key mismatch, Vault error, or corrupted secret) | 3-5 min |
| 3 | Re-generate or re-encrypt joiner secrets | 3-10 min |
| 4 | Retry failed node provisioning | 5-20 min |
| 5 | Verify all nodes reach ready state | 1 min |
| **Total MTTR** | | **13-37 min** |

