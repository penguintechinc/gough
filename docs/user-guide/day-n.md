# Day N: Scaling & Advanced Operations

**Focus:** Multi-cluster DR, capacity scaling, brownfield adoption, compliance.

---

## Scaling the Cluster

### Horizontal Scaling (More Nodes)
```bash
# Add worker nodes
for i in {1..5}; do
  gough node provision-phase-1 --add-worker
  sleep 30  # Stagger provisioning
done

# Trigger migration to rebalance
gough workload rebalance --strategy=spread --max-concurrent=2

# Verify even distribution
gough node list | grep -E "cpu|mem" | sort -k2 -n
```

### Vertical Scaling (Larger Nodes)
```bash
# Evaluate current node specs
gough node list --json | jq '.[] | {id, cpu_count, memory_mb}'

# Provision larger nodes
gough node provision-phase-1 --add-worker --flavor=large

# Drain smaller nodes and retire
gough node drain --node-id=<small_node>
gough node poweroff --node-id=<small_node>
```

---

## Multi-Cluster DR

### Set Up Secondary Cluster
```bash
# Provision secondary cluster on different infrastructure
gough cluster create --name=secondary \
  --cluster-size=3 \
  --region=<dr_region> \
  --disaster-recovery-primary=<primary_cluster_id>

# Verify replication lag
gough dr status --json | jq '.replication_lag_seconds'
```

### DR Failover Procedure
```bash
# Declared secondary as primary (operational decision)
gough dr failover --confirm \
  --reason="Primary cluster lost; executing failover" \
  --ticket=<jira_incident_id>

# Verify applications are running on secondary
kubectl get pods -A

# Update DNS to point to secondary (external operation)
# Update WaddleAI integration endpoints (external operation)
```

---

## Brownfield Adoption (Legacy to Gough)

### Migrate Existing Workloads
```bash
# 1. Create "legacy" tenant in Gough
gough tenant create --name=legacy --quota-nodes=10

# 2. Establish network bridge between legacy infrastructure and Gough cluster
gough network bridge-create --bridge-name=legacy-bridge \
  --legacy-network=<legacy_cidr> \
  --gough-network=<gough_cidr>

# 3. Migrate application Biomes
gough biome import --app=<legacy_app> --source-type=docker-image

# 4. Test biome in staging
gough biome deploy --biome-id=<legacy_app_egg> --node-id=<staging_node> --dry-run

# 5. Deploy to production
gough biome deploy --biome-id=<legacy_app_egg> --node-id=<prod_node>

# 6. Validate data consistency
gough migration validate --biome-id=<legacy_app_egg>

# 7. Cutover DNS (external operation)
```

---

## Compliance Lane Adoption

### Enable Compliance Features
```bash
# Activate compliance mode (immutable audit trail, enhanced RBAC)
gough cluster set-compliance-mode --enabled \
  --audit-retention-days=2555 \  # 7 years
  --encryption-at-rest=true \
  --network-policy-default=deny

# Verify compliance posture
gough compliance report --json | jq '.[] | {control, status, remediation}'

# Generate compliance artifacts
gough compliance export --format=cis-kubernetes \
  --output=/compliance/gough-cis-report-$(date +%Y%m%d).pdf
```

---

## Custom Biome Authoring

### Create New Biome from Scratch
```bash
# Scaffold biome structure
gough biome new --name=my-app --biome_kind=application

# Edit biome manifest
cat k8s/biomes/seed/my-app/biome.yaml
# Define:
#   - phase: post_deploy
#   - workload_type: lxc or vm
#   - requires_hardware_tags: (e.g., gpu:nvidia, mem:>32gb)
#   - emits_joiner_secrets: (if joining cluster)

# Write cloud-init
nano k8s/biomes/seed/my-app/cloud-init/user-data.yaml

# Create LXD profile (if customizations needed)
nano k8s/biomes/seed/my-app/lxd-profile.yaml

# Validate syntax
gough biome validate --biome-id=my-app

# Test locally
gough dev seed --biomes my-app,k8s-primary

# Publish to registry
gough biome publish --biome-id=my-app
```

---

## Performance Tuning

### CPU & Memory Optimization
```bash
# Profile running Biomes
gough profiler start --biome-id=<id> --duration=5m

# Analyze resource usage
gough profiler report --biome-id=<id> --format=json | \
  jq '.metrics[] | select(.cpu_pct > 50 or .mem_pct > 50)'

# Adjust resource requests/limits
gough biome update --biome-id=<id> \
  --cpu-request=500m \
  --cpu-limit=1000m \
  --mem-request=512Mi \
  --mem-limit=2Gi
```

### Network Performance
```bash
# Test inter-node latency
gough node network-benchmark --pairs=all

# Identify bottlenecks
gough metrics export --query='network.latency_ms{percentile="p99"}' --from=-1h

# Optimize: adjust MTU, enable jumbo frames, or upgrade links
gough node network-mtu-set --value=9000 --all
```

### Storage Performance
```bash
# Benchmark storage
gough storage benchmark --backend=ceph --size=10G --duration=10m

# Monitor I/O
gough metrics export --query='storage.iops{...}' --from=-1h | jq '.'

# Optimize: add OSDs, tune replication, or switch backend
```

---

## Version Management & Rollouts

### Rolling Cluster Upgrade
```bash
# Plan upgrade
gough cluster upgrade --from-version=1.0.0 --to-version=1.1.0 --dry-run

# Execute upgrade (control plane first)
gough cluster upgrade --confirm --max-surge=1

# Monitor upgrade progress
watch -n 10 'gough cluster status --json | jq ".upgrade_progress"'

# Validate after upgrade
gough audit verify --since=-1h
gough cluster status
```

---

## Long-Term Operations

### Monthly Tasks
```bash
0 0 1 * * /usr/local/bin/gough cluster capacity-forecast --horizon-days=90 > /reports/forecast_$(date +\%Y\%m).txt
0 0 1 * * /usr/local/bin/gough compliance export --format=cis-kubernetes --output=/compliance/gough-cis-$(date +\%Y\%m\%d).pdf
```

### Quarterly Tasks
```bash
# Q1, Q2, Q3, Q4
0 0 1 1,4,7,10 * /usr/local/bin/gough dr drill-run --full-recovery-test

# Quarterly security audit
0 0 1 1,4,7,10 * /usr/local/bin/gough security audit --comprehensive
```

### Annual Tasks
```bash
# Anniversary of cluster creation
0 0 <DAY> <MONTH> * /usr/local/bin/gough cluster anniversary-health-check
```

---
