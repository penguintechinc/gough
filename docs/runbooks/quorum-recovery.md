# Runbook: Quorum Recovery (Break-Glass)

**Endpoint:** `POST /api/v1/primary/force-recover`  
**Severity:** CRITICAL  
**Component:** Kubernetes etcd consensus  
**SLO Impact:** Complete cluster unavailability until recovery succeeds  

---

## Symptoms

- Alert: `gough.cluster.quorum_loss{cluster_id="..."}`
- CLI output: `gough cluster status` shows `quorum_status=lost`
- Multiple control-plane nodes unreachable (> half of cluster)
- Etcd error logs: "fatal: lost quorum"
- API server pods unable to start or in CrashLoopBackOff
- No new Biome deployments possible; existing Biomes continue but are frozen

---

## When to Use This Runbook

**Break-glass recovery** is used only when:

1. **Etcd quorum is permanently lost** — more than half of control-plane nodes are dead/offline with no recovery in sight
2. **Standard restart procedures fail** — restarting nodes does not restore quorum within 10 minutes
3. **Network partition cannot be healed** — you've confirmed firewalls/network is the cause and cannot be fixed quickly

**Do NOT use this runbook for:**
- Temporary node reboots (wait 2–3 minutes for etcd to recover)
- Network partition that might heal itself (wait 10 minutes first)
- Single node failure in a 3+ node cluster (quorum still exists)

---

## Prerequisites (Critical)

**You must have:**

1. **At least one control-plane node alive** (even if not part of quorum):
   ```bash
   gough node list --filter="role=control-plane" --json | jq '.[] | select(.state=="ready")'
   # Must have at least 1 ready node; if all are NotReady, cluster is unrecoverable
   ```

2. **SSH access to a surviving control-plane node**:
   ```bash
   ssh -i <your-key> ubuntu@<surviving-cp-node-ip>
   ```

3. **Vault is unsealed** (credentials are accessible):
   ```bash
   gough vault status | jq '.sealed'
   # Must be false; if true, unseal Vault first (see vault-sealed-bootstrap-window-expired.md)
   ```

4. **Database connectivity** (PostgreSQL is running):
   ```bash
   psql -U postgres -d gough -c "SELECT COUNT(*) FROM cloud_machines;" 2>/dev/null | grep -q "[0-9]"
   ```

5. **Cluster name must be known** (you'll type it to confirm):
   ```bash
   gough cluster list --json | jq '.[] | {id,name}'
   ```

---

## Understanding the Recovery

Quorum recovery uses the latest available etcd snapshot. The procedure:

1. **Identify a surviving node** with the freshest etcd snapshot
2. **Snapshot the database** (snapshot state at time of loss)
3. **Restore that snapshot** to a temporary directory
4. **Reconstitute etcd** from the snapshot (not from corrupted member state)
5. **Re-add other control-plane nodes** as they come online

**Data loss:** You lose transactions after the snapshot was taken (typically < 1 min old).

---

## Step-by-Step Procedure

### Step 1: Confirm Quorum is Actually Lost

```bash
# Verify the problem exists
gough cluster status --json | jq '{healthy,quorum_status,available_members,required_members}'

# Check which control-plane nodes are reachable
gough node list --filter="role=control-plane" --json | jq '.[] | {id,state,last_heartbeat}'

# Expected for quorum loss:
# - healthy=false
# - quorum_status=lost
# - available_members < required_members (e.g., 1 out of 2 required)
# - Multiple nodes show state=NotReady or last_heartbeat > 5 minutes ago
```

**If quorum is NOT lost, do NOT proceed.** See `/docs/runbooks/quorum-loss.md` instead.

### Step 2: Identify the Surviving Node

Choose a control-plane node that is currently running and can access etcd:

```bash
# List all control-plane nodes and their status
gough node list --filter="role=control-plane" --json | jq '.[] | {id,state}'

# SSH to the most recently healthy node (check last_heartbeat)
SURVIVING_CP_NODE="node-02"  # Example: choose the one with most recent heartbeat
ssh -i <your-key> ubuntu@$SURVIVING_CP_NODE
```

### Step 3: Verify Etcd Access on Surviving Node

Once SSH'd into the surviving node:

```bash
# Check if etcd pod exists
sudo docker ps | grep etcd
# If no etcd container, the node cannot be the recovery source; try another node

# Check etcd status
sudo docker exec <etcd-container-id> etcdctl member list
# May show "no permission" or "unhealthy" — this is expected in quorum loss

# List etcd snapshots (created periodically by snapshot-agent)
ls -la /var/lib/etcd-snapshots/ 2>/dev/null | head -10
# Example files: etcd-snapshot-2026-05-09T14:30:00Z.db, etcd-snapshot-2026-05-09T14:25:00Z.db
```

### Step 4: Initiate Break-Glass Recovery (API)

From your local machine with API access:

```bash
# Call force-recover endpoint
# REQUIRED: Provide cluster name to confirm (prevents accidental recovery of wrong cluster)
# REQUIRED: Your request must carry superadmin scope + MFA

curl -X POST https://gough.internal.example.com/api/v1/primary/force-recover \
  -H "Authorization: Bearer $(gough auth get-token --scope=superadmin)" \
  -H "X-MFA-Token: $(gough auth get-mfa-token)" \
  -H "Content-Type: application/json" \
  -d '{
    "cluster_id": "gough-dal2-prod",
    "cluster_name_confirmation": "gough-dal2-prod",
    "surviving_node_id": "node-02",
    "snapshot_timestamp": "2026-05-09T14:30:00Z"
  }'

# Expected response (202 Accepted):
# {
#   "operation_id": "force-recover-uuid-456",
#   "status": "in_progress",
#   "phase": "snapshot_verify",
#   "message": "Verifying snapshot integrity on surviving node..."
# }
```

**Parameters:**
- `cluster_id`: Cluster identifier (e.g., `gough-dal2-prod`)
- `cluster_name_confirmation`: **Must exactly match cluster name** (prevents accidental cluster recovery)
- `surviving_node_id`: The control-plane node to recover from
- `snapshot_timestamp`: (optional) Explicit snapshot to restore; if omitted, uses latest

### Step 5: Monitor Recovery Progress

```bash
# Check operation status in real-time
curl https://gough.internal.example.com/api/v1/primary/force-recover/<operation-id> \
  -H "Authorization: Bearer $(gough auth get-token --scope=superadmin)"

# Expected output (in progress):
# {
#   "operation_id": "force-recover-uuid-456",
#   "status": "in_progress",
#   "phase": "restoring_snapshot",
#   "progress_percent": 35,
#   "message": "Restoring snapshot to temporary directory..."
# }
```

**Expected phases (in order):**

1. **snapshot_verify** (30 sec): Verify snapshot integrity
2. **restoring_snapshot** (1–2 min): Restore to temp dir
3. **etcd_reconstitute** (1–2 min): Rebuild etcd from snapshot
4. **member_readd** (1–5 min): Re-add other CP nodes as they rejoin
5. **quorum_verify** (30 sec): Confirm quorum is restored
6. **completed**: Recovery succeeded

**Total expected time: 5–10 minutes.**

### Step 6: Verify Recovery Succeeded

```bash
# Check final operation status
curl https://gough.internal.example.com/api/v1/primary/force-recover/<operation-id> \
  -H "Authorization: Bearer $(gough auth get-token --scope=superadmin)"
# Expected: status="completed"

# Verify cluster is healthy
gough cluster status --json | jq '{healthy,quorum_status,available_members}'
# Expected: healthy=true, quorum_status=healthy, available_members=3

# Verify all control-plane nodes are back in cluster
gough node list --filter="role=control-plane" --json | jq '.[] | {id,state}'
# Expected: all nodes show state=ready

# Check etcd membership
kubectl -n gough exec -it <etcd-pod> -- etcdctl member list
# Expected: all 3 members present, healthy=true

# Check API server pods
kubectl -n gough get pods -l component=api-server
# Expected: all pods Ready and Running
```

### Step 7: Check for Data Loss

The recovery restores etcd to the state of the last snapshot. You may have lost transactions after that snapshot.

```bash
# Check audit chain for any gaps
gough audit list --limit=50 | jq '.[] | {timestamp,action}'
# Look for gaps in timestamps; recovery time should appear as a gap

# Compare with your backup audit log (if offsite mirror enabled)
gough audit list --source=offsite --limit=50 | jq '.[] | {timestamp,action}'
# If there are entries here after recovery, they are NOT in the cluster

# Document what was lost (if anything)
# Example: "Deployment request from 2026-05-09T14:35:00Z was not applied"
```

---

## If Recovery Fails

### Failure: "Snapshot verification failed"

**Cause:** Snapshot is corrupted or incompatible.

**Fix:**

```bash
# Retry with an earlier snapshot
curl -X POST https://.../api/v1/primary/force-recover \
  -H "Authorization: Bearer $(gough auth get-token --scope=superadmin)" \
  -H "X-MFA-Token: $(gough auth get-mfa-token)" \
  -H "Content-Type: application/json" \
  -d '{
    "cluster_id": "gough-dal2-prod",
    "cluster_name_confirmation": "gough-dal2-prod",
    "surviving_node_id": "node-02",
    "snapshot_timestamp": "2026-05-09T14:25:00Z"
  }'
```

### Failure: "No surviving nodes available"

**Cause:** All control-plane nodes are down.

**Fix:** Cluster is unrecoverable. You must redeploy from scratch. Contact PenguinTech support.

### Failure: "Member re-add timeout (30s)"

**Cause:** Other control-plane nodes are not coming online after snapshot restore.

**Fix:**

```bash
# Manually restart the offline control-plane nodes
gough node reboot node-01 node-03
# Wait 3–5 minutes for nodes to rejoin

# Check if they rejoined quorum
kubectl -n gough exec -it <etcd-pod> -- etcdctl member list

# If still offline, try recovery again:
curl -X POST https://.../api/v1/primary/force-recover/<operation-id>/retry \
  -H "Authorization: Bearer $(gough auth get-token --scope=superadmin)"
```

### Failure: "Database restore failed"

**Cause:** Etcd database is corrupted beyond repair, or restore procedure encountered an error.

**Fix:** Cluster is unrecoverable. Contact PenguinTech support for manual intervention.

---

## Escalation Path

If recovery fails after two attempts:

1. **Stop further recovery attempts** — repeated snapshots may worsen the situation
2. **Collect diagnostics:**
   ```bash
   gough audit list --limit=100 > /tmp/audit-dump.json
   gough cluster status --json > /tmp/cluster-status.json
   kubectl -n gough logs -l component=api-server > /tmp/api-server-logs.txt
   ```
3. **Contact PenguinTech support** with:
   - Cluster ID
   - Operation ID (`force-recover-...`)
   - All diagnostic files above
   - Timeline of what failed

---

## Prevention

**To avoid quorum loss:**

1. **Deploy with 3+ control-plane nodes** (minimum for HA)
2. **Monitor etcd health continuously:**
   ```bash
   gough cluster status --json | jq '.quorum_status' # Watch for "lost"
   ```
3. **Set up alerts** for:
   - `gough.cluster.quorum_loss` (CRITICAL)
   - `gough.cluster.consensus_members_available < required_members` (WARNING)
4. **Maintain recent snapshots** — Gough takes snapshots every 5 minutes by default
5. **Test recovery procedures monthly** in non-production clusters

---

## Related Runbooks

- `/docs/runbooks/quorum-loss.md` — Diagnose and recover from temporary quorum loss
- `/docs/runbooks/control-plane-replace.md` — Replace a failed control-plane node (without quorum loss)
- `/docs/runbooks/vault-sealed-bootstrap-window-expired.md` — Unseal Vault if needed

