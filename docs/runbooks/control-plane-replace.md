# Runbook: Replace a Control-Plane Node

**Endpoint:** `POST /api/v1/primary/replace`  
**Severity:** HIGH  
**Component:** Kubernetes control plane (etcd cluster member)  
**SLO Impact:** Temporary degradation (cluster operates at reduced quorum) until replacement joins  

---

## Symptoms

- Control-plane node hardware failure (disk, power, network)
- Node stuck in CrashLoopBackOff and unrecoverable
- Need to decommission a control-plane host for maintenance
- Operator decision to replace a node in the control plane

---

## Prerequisites

**Before starting:**

1. **Cluster must be healthy.** Check quorum:
   ```bash
   gough cluster status --json | jq '{healthy,quorum_status,available_members,required_members}'
   # Example output: healthy=true, quorum_status=healthy, available_members=3, required_members=2
   ```

2. **Replacement node must exist** and be provisioned in the `ready` state:
   ```bash
   gough node list --filter="state=ready" --json | jq '.[] | {id,state}'
   ```

3. **Replacement node must be biome-eligible** for `k8s-primary`:
   ```bash
   gough node describe <new-node-id> | jq '.hardware_tags'
   # Must match k8s-primary requirements (e.g., memory >= 8GB, disk >= 50GB)
   ```

4. **Target node to replace must be identifiable:**
   ```bash
   gough node list --filter="role=control-plane" --json | jq '.[] | {id,state,role}'
   ```

---

## When to Replace

- **Hardware failure:** Node is down or unstable (kernel panics, disk errors, OOM)
- **Maintenance:** Node needs to be removed for physical upgrades, OS refresh, or decommissioning
- **Capacity rebalancing:** Moving control-plane from underpowered node to larger host

**Do NOT replace without cause.** Each replacement briefly reduces quorum headroom.

---

## Step-by-Step Procedure

### Step 1: Initiate Replacement Request

```bash
# Call the replacement endpoint
curl -X POST https://gough.internal.example.com/api/v1/primary/replace \
  -H "Authorization: Bearer $(gough auth get-token)" \
  -H "Content-Type: application/json" \
  -d '{
    "old_node_id": "node-01",
    "new_node_id": "node-04",
    "drain_timeout_seconds": 300
  }'

# Expected response (202 Accepted):
# {
#   "operation_id": "replace-node-01-uuid-123",
#   "status": "in_progress",
#   "phase": "draining",
#   "estimated_duration_seconds": 420
# }
```

**Parameters:**
- `old_node_id`: Control-plane node to remove (e.g., `node-01`)
- `new_node_id`: Replacement node to join (e.g., `node-04`)
- `drain_timeout_seconds`: Grace period for pod eviction (default 300)

### Step 2: Monitor the Replacement

The replacement goes through these phases:

1. **Drain phase** (1–2 min): Gough cordons the old node and evicts all workloads
2. **Reset phase** (30–60 sec): SSH into old node, run `kubeadm reset` to clean up
3. **Join phase** (1–2 min): New node runs `kubeadm join --control-plane` with stored secrets
4. **Etcd health check** (30–60 sec): Verify new member is healthy in etcd

**Watch the operation:**

```bash
# Check operation status
curl https://gough.internal.example.com/api/v1/primary/replace/<operation-id> \
  -H "Authorization: Bearer $(gough auth get-token)"

# Expected response:
# {
#   "operation_id": "...",
#   "status": "in_progress",
#   "phase": "joining",
#   "old_node_id": "node-01",
#   "new_node_id": "node-04",
#   "progress_percent": 65
# }
```

**Or use CLI shorthand:**

```bash
# Check node status in real-time
gough node list --filter="role=control-plane" --json | jq '.[] | {id,state,role}'

# Check etcd member health
kubectl -n gough exec -it <any-etcd-pod> -- etcdctl member list

# Monitor API server pod readiness
kubectl -n gough get pods -l component=api-server -w
```

### Step 3: Verify Replacement Complete

When the operation finishes, you should see:

```bash
# Check operation final status
curl https://gough.internal.example.com/api/v1/primary/replace/<operation-id> \
  -H "Authorization: Bearer $(gough auth get-token)"
# Response: status="completed"

# Verify new node is in the cluster
gough node list --filter="role=control-plane" --json | jq '.[] | select(.id=="node-04")'
# Expected: state=ready, role=control-plane

# Verify etcd membership
kubectl -n gough exec -it <etcd-pod> -- etcdctl member list | grep node-04
# Expected: new member appears in list with healthy status

# Verify API server is ready on new node
kubectl get nodes -l node-role.kubernetes.io/control-plane=,kubernetes.io/hostname=node-04 -o wide
# Expected: STATUS=Ready, ROLES=control-plane
```

### Step 4: Check Quorum Health

```bash
# Verify cluster quorum is still healthy
gough cluster status --json | jq '{healthy,quorum_status,available_members,required_members}'
# Expected: healthy=true, quorum_status=healthy, available_members=3, required_members=2

# Check Prometheus alert (if any)
curl -s 'http://prometheus.internal.example.com:9090/api/v1/query?query=gough.cluster.quorum_loss' | jq '.data.result | length'
# Expected: 0 (no quorum loss alerts)
```

### Step 5: Clean Up (If Replace Failed)

If the replacement fails, the old node remains in the cluster (in a degraded state). You must decide:

**Option A: Retry replacement** (recommended for transient failures)
```bash
# Call replace again, same parameters
curl -X POST https://gough.internal.example.com/api/v1/primary/replace \
  -H "Authorization: Bearer $(gough auth get-token)" \
  -H "Content-Type: application/json" \
  -d '{"old_node_id": "node-01", "new_node_id": "node-04", "drain_timeout_seconds": 300}'
```

**Option B: Remove old node manually** (if it's permanently dead)
```bash
# SSH into any working control-plane node
ssh -i <key> ubuntu@<cp-node>

# Remove the old member from etcd
sudo etcdctl member list | grep node-01
# Copy the member ID (e.g., "abc123def456")

sudo etcdctl member remove abc123def456

# Verify removal
sudo etcdctl member list
# node-01 should no longer appear
```

---

## Expected Behavior

### Quorum During Replacement

When replacing a 3-node cluster:
- **Before:** 3 members, quorum requires 2
- **During replace:** Briefly 4 members (old + new), then old is removed, back to 3
- **Quorum never lost** if the other 2 nodes remain healthy

### Pod Eviction

The drain phase evicts all non-system pods from the old node with a 5-minute grace period. System pods (kubelet, etcd) are forcibly terminated. Reschedulable pods automatically restart on remaining nodes.

### API Server Availability

The API server remains available during replacement because other control-plane nodes are unaffected. Clients may see brief latency spikes during the drain phase but should not lose connectivity.

---

## Troubleshooting

### Replacement stuck in "draining" phase (5+ minutes)

**Cause:** A pod is refusing to evict (e.g., missing PodDisruptionBudget).

**Fix:**
```bash
# Check eviction errors
kubectl logs -n gough -l component=api-server --tail=20 | grep -i evict

# Force evict the problematic pod manually
kubectl delete pod <pod-name> -n <namespace> --grace-period=0 --force

# Resume replacement
curl -X POST https://.../api/v1/primary/replace/<operation-id>/resume \
  -H "Authorization: Bearer $(gough auth get-token)"
```

### Replacement stuck in "join" phase (5+ minutes)

**Cause:** New node is not reaching the control plane or secret retrieval failed.

**Fix:**
```bash
# Check cloud-init logs on new node
ssh -i <key> ubuntu@<new-node-id> tail -50 /var/log/cloud-init-output.log | grep -i join

# Check kubeadm join error on new node
ssh -i <key> ubuntu@<new-node-id> sudo journalctl -u kubelet --since=5m | grep -i error

# If network is broken, restart new node and retry:
gough node reboot <new-node-id>
# Wait for node to come back up
sleep 30
curl -X POST https://.../api/v1/primary/replace/<operation-id>/resume \
  -H "Authorization: Bearer $(gough auth get-token)"
```

### Quorum lost during replacement

**Cause:** A second control-plane node also failed during the replacement window.

**Fix:** See `/docs/runbooks/quorum-recovery.md` for break-glass recovery procedures.

---

## Audit Trail

All replacement operations are logged to the audit chain:

```bash
# View audit entries for this replacement
gough audit list --filter="action=primary_node_replace" --limit=10

# Example audit entry:
# {
#   "timestamp": "2026-05-09T14:32:10Z",
#   "actor": "operator@example.com",
#   "action": "primary_node_replace",
#   "resource_type": "control_plane",
#   "resource_id": "node-01",
#   "details": {
#     "old_node": "node-01",
#     "new_node": "node-04",
#     "operation_id": "replace-...",
#     "status": "completed",
#     "drain_duration_seconds": 120,
#     "join_duration_seconds": 95
#   }
# }
```

---

## Related Runbooks

- `/docs/runbooks/quorum-recovery.md` — Break-glass recovery if quorum is lost
- `/docs/runbooks/control-plane-replace.md` — This document
- `/docs/user-guide/networking.md` — Control plane frontend modes (kube-vip/external/none)

