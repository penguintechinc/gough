# Day Two: Operations & Monitoring (First 30 Days)

**Focus:** Establish operational routines, verify backups, test DR, monitor SLOs.

---

## Weekly Tasks

### Week 1: Backup Verification
```bash
# Verify daily snapshots are being created
gough backup list --since=-7d | wc -l
# Should show at least 7 entries

# Test restore (in staging)
gough backup restore --backup-id=<latest> --target=staging-cluster --dry-run

# Verify audit log backup
gough audit backup list | jq '.[] | {backup_id, age_days}'
```

### Week 1: Capacity Review
```bash
# Check current utilization
gough capacity status --json | jq '.nodes[] | {node_id, cpu_usage_pct, mem_usage_pct, disk_usage_pct}'

# Forecast next 7 days
gough capacity forecast --horizon-days=7

# If trending above 80%: prepare for expansion or migration
```

### Week 1: Certificate Rotation Check
```bash
# List certificates and expiry
gough cert list --json | jq '.[] | {name, expires_at, days_remaining}'

# If any cert expires < 30 days: rotate immediately
gough cert rotate --cert-id=<id>
```

### Week 2: DR Drill
```bash
# Run DR drill (simulates failover to backup)
gough dr drill-run

# Monitor drill progress
watch -n 10 'gough dr drill-status --json | jq ".progress_pct"'

# Review results
gough dr drill-report --latest --json | jq '{rto_observed, rpo_observed, errors}'

# RTO SLO: < 30 min, RPO SLO: < 5 min
# If either is exceeded: investigate and update DR runbook
```

### Week 4: Compliance Audit
```bash
# Verify audit chain integrity
gough audit verify --since=-30d

# Check for any suspicious entries
gough audit log --filter="severity=critical" --since=-30d | wc -l

# Generate compliance report
gough audit report --from=<month_start> --to=<month_end> > /backup/audit_report_$(date +%Y%m).txt
```

---

## Daily Operations

### Automated Daily Tasks
```bash
# These should run automatically (cron)
0 0 * * * /usr/local/bin/gough cluster backup
0 1 * * * /usr/local/bin/gough audit backup
0 2 * * * /usr/local/bin/gough metrics export --output=/backup/metrics_$(date +\%Y\%m\%d).json
```

### Morning Monitoring (5 minutes)
```bash
# Check cluster health
gough cluster status
# Should show: healthy=true

# Check for alerts overnight
curl -s 'http://localhost:9090/api/v1/query?query=ALERTS' | jq '.data.result | length'
# Should be 0; if > 0, review via runbooks

# Check node status
gough node list | grep -v "ready"
# Should be empty

# Check for failed pods
kubectl get pods -A | grep -E "CrashLoop|Pending|Failed"
# Should be empty or accounted for
```

### Periodic Checks (Weekly)
```bash
# Verify license server reachability
curl -s https://license.penguintech.io/health | jq '.status'

# Check integration service status
gough integration status --json | jq '.[] | {service, status}'

# Verify WaddleAI can make capacity predictions
gough waddleai forecast --horizon-days=7 | jq '.forecast[0]'
```

---

## Troubleshooting Common Issues

### Node becomes unhealthy
```bash
# Diagnosis
gough node status --node-id=<id> --detailed

# Common causes:
# - Disk full: check disk usage
# - Memory pressure: check memory usage
# - Network flapping: check bond-flapping runbook

# Recovery
gough node drain --node-id=<id>
# (Migrate Biomes off node)

gough node reboot --node-id=<id>
# (Reboot for system cleanup)

gough node undrain --node-id=<id>
# (Re-enable scheduling after recovery)
```

### Pod OOMKilled
```bash
# Identify pod
kubectl logs <pod> --previous | grep -i "oom\|memory"

# Increase resource limits
kubectl set resources deployment/<deploy> --limits=memory=2Gi

# Or trigger migration to node with more memory
gough biome migrate --biome-id=<id> --target-node=<node_with_more_memory>
```

### Storage replication lag
```bash
# Check storage status
gough storage status

# If replication factor < 2:
# Add replicas (additional nodes) or
# Reduce data size temporarily

# Monitor recovery
watch -n 10 'gough storage status | jq ".replication_factor"'
```

---

## SLO Monitoring

| SLO | Check Command | Target | Action if Breached |
|-----|---------------|--------|-------------------|
| Availability 99.9% | `gough metrics export --query='cluster.availability' --from=-30d` | >= 99.9% | Investigate errors; increase redundancy |
| Pod startup < 5min | `kubectl get events --all-namespaces --sort-by='.lastTimestamp'` | p95 < 5min | Profile slow pods; optimize image |
| Backup success rate | `gough backup list --since=-7d \| grep successful` | 100% | Fix backup mechanism |
| DR RTO < 30min | `gough dr drill-report --latest` | <= 30 min | Test and optimize failover |
| Audit integrity | `gough audit verify` | Exit 0 | See audit-chain-break runbook |

---

## Scaling Decisions

### Add Nodes When
- Cluster utilization > 80% (CPU or memory)
- Storage capacity < 20% free
- Pod scheduling delays exceed 1 minute

```bash
# Add worker node
gough node provision-phase-1 --add-worker

# Monitor node join
gough node provision-watch --node-id=<new_node>

# Trigger workload rebalancing
gough workload rebalance --strategy=spread
```

### Remove Nodes When
- Consolidating after temporary spike
- Retiring hardware
- Planned maintenance

```bash
# Drain node (migrate Biomes off)
gough node drain --node-id=<id>

# Verify no running Biomes
kubectl get pods --all-namespaces --field-selector=spec.nodeName=<id>

# Power off node
gough node poweroff --node-id=<id>
```

---

## Documentation Updates

Keep operational runbooks updated:
```bash
# After each major incident
git commit -m "chore: update runbook for [issue] based on [incident-id]"

# After SLO changes
git commit -m "docs: update SLO targets for [component]"

# After DR drill
git commit -m "docs: DR recovery time adjusted to [RTO] min based on drill results"
```

---
