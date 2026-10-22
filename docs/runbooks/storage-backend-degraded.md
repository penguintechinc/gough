# Runbook: Storage Backend Degraded

**Alert Name:** `gough.storage.backend_degraded`  
**Severity:** WARNING  
**Component:** Storage (Nest/Longhorn/Ceph)  

**Symptoms:**
- Prometheus alert: `gough.storage.backend_degraded{backend="...",replica_count=...}`
- Storage replication factor < target
- Pod may be pending storage

**Detection:**
```bash
# Check storage status
gough storage status --json | jq '.backends[] | {name, replicas, healthy, degraded}'

# Check specific backend
gough storage backend-status --backend=ceph
```

**Resolution:**
```bash
# For Longhorn degradation:
kubectl -n gough get pvc
# Check PVC status and add replicas if needed

# For Ceph:
# Check OSDs: `ceph osd tree`
# Rebalance: `ceph balancer on`
```

---
