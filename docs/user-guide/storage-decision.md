# Storage Decision Guide

---

## Quick Decision Tree

```
Local workload only?
├─ Yes → local-nvme (fast, no replication)
└─ No → need replication?
    ├─ Need HA (replication)?
    │   ├─ 3-5 nodes → Nest (built-in, simple)
    │   ├─ 5-20 nodes → Longhorn (user-friendly)
    │   ├─ 20+ nodes → Ceph (performance)
    │   └─ External → iSCSI/NFS (enterprise)
    └─ Need shared storage?
        └─ Yes → NFS (for legacy apps)
```

---

## Backends & Comparison

| Backend | Nodes | Replicas | Latency | Setup | Cost |
|---------|-------|----------|---------|-------|------|
| local-nvme | 1+ | 0 | <1ms | Easy | Low |
| Nest | 3-5 | 3 | 2-5ms | Easy | Low |
| Longhorn | 5-20 | 2-3 | 5-10ms | Medium | Low |
| Ceph | 20+ | 3+ | 10-20ms | Hard | High |
| iSCSI | 1-∞ | 0 | 5-15ms | Hard | High |
| NFS | 1-∞ | 0 | 20-50ms | Easy | Medium |

---

## Fleet-Size Guidance

**Small (3-5 nodes):** Nest (built-in, production-grade)  
**Medium (5-20 nodes):** Longhorn (balance of simplicity and performance)  
**Large (20+ nodes):** Ceph (horizontal scale, enterprise support)

---
