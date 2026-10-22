# Runbook: Quorum Loss (Cluster Consensus Failure)

**Alert Name:** `gough.cluster.quorum_loss`  
**Severity:** CRITICAL  
**Component:** Cluster consensus (etcd/Raft)  
**SLO Impact:** Cluster cannot schedule Biomes; complete unavailability  

---

## Symptoms

- Prometheus alert: `gough.cluster.quorum_loss{cluster_id="..."}`
- `gough cluster status` shows `healthy=false`, `quorum_status=lost`
- New Biomes cannot be deployed
- Existing running Biomes continue but cannot be migrated
- Logs: "Lost quorum; cannot reach majority of control plane nodes"

---

## Detection

**Metrics:**
```bash
gough.cluster.quorum_loss{cluster_id="..."} == 1
gough.cluster.consensus_members_available < (majority_count)
```

**CLI verification:**
```bash
# Check cluster quorum status
gough cluster status --json | jq '{healthy, quorum_status, available_members, required_members}'

# List control plane nodes
gough node list --filter="role=control-plane" --json | jq '.[] | {id, state, last_heartbeat}'

# Check consensus (etcd or Raft) status
kubectl -n gough get lease --sort-by=.metadata.managedFields[0].time | head -10

# Check for network partitions
gough node network-status --json | jq '.[] | {node_id, connectivity}'
```

---

## Severity Assessment

**CRITICAL.** Quorum loss blocks all cluster state changes. Existing Biomes continue running but cluster cannot handle any new deployments, migrations, or configuration changes. This is a complete control plane failure.

---

## First 30 Seconds

1. **Do NOT restart control plane nodes** — this may worsen quorum loss.
2. Check network connectivity between control plane nodes.
3. Determine how many control plane nodes are missing.
4. Check if this is a network partition (N nodes down) or a cascading failure.

---

## Diagnosis

**Three plausible causes:**

### Cause 1: Network Partition (50% likelihood)
**Symptoms:** Control plane nodes are running but cannot reach each other.

**Check:**
```bash
# Check network connectivity between control plane nodes
for NODE in $(gough node list --filter="role=control-plane" --json | jq -r '.[].id'); do
  echo "Node $NODE:"
  gough node network-status --node-id=$NODE | jq '.reachable_peers'
done

# Check inter-node latency
gough node network-latency --format=json | jq '.[] | select(.source_role=="control-plane" and .dest_role=="control-plane")'

# Check firewall rules
gough node firewall-status --node-id=<cp-node> | grep -E "2379|2380|50051"
# (etcd ports 2379, 2380; Raft consensus port 50051)
```

**If connectivity is broken:**
- Network partition is blocking consensus.
- Check upstream switch/router for port blocking.
- Check SecurityGroup / firewall rules.

---

### Cause 2: Control Plane Nodes Down (40% likelihood)
**Symptoms:** Multiple control plane nodes are NotReady or missing.

**Check:**
```bash
# List all control plane pods
kubectl -n gough get pods -l component=api-server

# Check node status
kubectl get nodes -l node-role.kubernetes.io/control-plane= -o wide

# Count available control plane members
gough cluster status --json | jq '.available_members'

# Check for pod restart loops
kubectl -n gough get pods -l component=api-server -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}'
```

**If nodes are down:**
- Check why (disk full, OOM, kernel panic).
- Review node logs: `dmesg` or `journalctl -u kubelet`.

---

### Cause 3: Consensus Database Corruption (10% likelihood)
**Symptoms:** Nodes are running but etcd/Raft database is corrupted.

**Check:**
```bash
# Check etcd health
kubectl -n gough exec -it <etcd-pod> -- etcdctl member list

# Check for corruption in Raft log
gough cluster consensus-status --format=json | jq '.corruption_detected'

# Inspect database size
kubectl -n gough exec -it <etcd-pod> -- ls -lh /var/lib/etcd/member/snap/db
```

**If corruption detected:**
- Database may need to be restored from backup.

---

## Resolution

**Step 1: Determine which cause applies.**

**For Cause 1 (Network partition):**
```bash
# Step 1: Restore network connectivity
# Check and fix firewall rules
gough node firewall-rule-add --source=<cp-node> --dest=<other-cp-node> --port=2379,2380,50051 --protocol=tcp

# Step 2: Wait for nodes to reconnect (up to 60 seconds)
kubectl -n gough wait --for=condition=Ready pod -l component=api-server --timeout=60s

# Step 3: Verify quorum is restored
gough cluster status | jq '.quorum_status'
# Should show: healthy

# Step 4: Confirm alert clears
curl -s 'http://localhost:9090/api/v1/query?query=gough.cluster.quorum_loss' | jq '.data.result | length'
# Should be 0
```

**For Cause 2 (Control plane nodes down):**
```bash
# Step 1: Check why nodes are down
journalctl -u kubelet --since=10m | grep -i error | head -10

# Step 2: Restart problematic nodes if safe
kubectl -n gough restart pod <api-server-pod>

# Step 3: Wait for pod to recover
kubectl -n gough wait --for=condition=Ready pod <api-server-pod> --timeout=60s

# Step 4: Monitor cluster recovery
watch -n 5 'gough cluster status | jq "{quorum_status, available_members}"'

# Step 5: Once quorum is restored, verify
gough cluster status --json | jq '.healthy'
# Should be true
```

**For Cause 3 (Database corruption):**
```bash
# Step 1: Backup current database
kubectl -n gough exec <etcd-pod> -- tar -czf /tmp/etcd-backup.tar.gz /var/lib/etcd/

# Step 2: Restore from backup
gough cluster restore-from-backup --backup=<latest-backup>

# Step 3: Restart etcd pods
kubectl -n gough delete pod -l app=etcd

# Step 4: Wait for cluster to reform
kubectl -n gough wait --for=condition=Ready pod -l app=etcd --timeout=120s

# Step 5: Verify quorum
gough cluster status | jq '.quorum_status'
```

---

## Verification

**Success criteria:**
1. `gough cluster status` shows `quorum_status=healthy`.
2. Alert `gough.cluster.quorum_loss` is no longer firing.
3. All control plane pods are Ready.
4. Biomes can be deployed successfully.

---

## Prevention

1. **Monitor consensus member health continuously:**
   ```bash
   # Alert if any member is unreachable
   gough alert set gough.cluster.consensus_member_unreachable \
     --threshold=1 \
     --duration=30s \
     --severity=warning
   ```

2. **Test failover procedures monthly:**
   - Simulate control plane node failure.
   - Verify quorum is maintained with N-1 nodes.

3. **Secure inter-node networking:**
   - Verify firewall rules allow consensus ports (2379, 2380, 50051).
   - Test latency between control plane nodes.

4. **Backup cluster state regularly:**
   ```bash
   # Daily backup
   0 1 * * * /usr/local/bin/gough cluster backup
   ```

---

## Escalation

**Escalate to:** Platform SRE, Network Team  
**When:**
- Cannot restore network connectivity.
- Multiple control plane nodes are permanently down.
- Database corruption cannot be fixed.

**Escalation procedure:**
```bash
gough alert escalate gough.cluster.quorum_loss \
  --team=platform-sre \
  --reason="Quorum loss; network partition suspected" \
  --ticket=<jira-ticket-id> \
  --page=true
```

---

## Related Runbooks

- [migration-rollback-fail.md](migration-rollback-fail.md) — Cluster state rollback during recovery
- [dr-drill-failed.md](dr-drill-failed.md) — Disaster recovery procedures

---

## Playbook Summary

| Step | Action | Time |
|------|--------|------|
| 1 | Determine cause (network, nodes down, or corruption) | 2-3 min |
| 2 | Restore network or restart nodes | 2-10 min |
| 3 | Wait for quorum to reform | 1-2 min |
| 4 | Verify cluster is healthy | 1 min |
| **Total MTTR** | | **6-16 min** |
