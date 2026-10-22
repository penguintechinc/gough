# Day One: First 30 Minutes to Cluster Ready

**Goal:** Bootstrap a single-primary cluster on a fresh Debian 12 VM, PXE-boot one sim node, and deploy the `k8s-primary` biome to a working Kubernetes cluster.

**Prerequisite Knowledge:**
- Basic Linux command line
- SSH access to a Debian 12 VM or physical hardware
- At least 4 CPU cores, 16 GB RAM, 100 GB disk for primary
- One additional machine (physical or VM) for the first worker node

**Timeline:** 20-30 minutes from blank Debian to `kubectl get nodes` showing Ready

---

## Step 1: Prepare the Primary Node (5 minutes)

### 1.1 SSH into Debian 12 VM/Hardware
```bash
ssh root@<primary_ip>

# Verify OS
cat /etc/os-release | grep PRETTY_NAME
# Should show: Debian GNU/Linux 12
```

### 1.2 Install Gough
```bash
# Download and install gough CLI
curl -fsSL https://releases.penguintech.io/gough/install.sh | bash

# Verify installation
gough version
# Should show: gough version X.Y.Z
```

### 1.3 Initialize Cluster
```bash
# Create a new cluster named "lab"
gough cluster create --name=lab --cluster-size=3 --primary-ip=<your_vm_ip>

# Expected output:
# Cluster created: lab (ID: cluster-abc123)
# Primary node IP: <your_vm_ip>
# Bootstrap token: (saved securely)

# Watch initialization progress
gough cluster init-progress
# Wait for "Vault unsealed" and "SPIRE identity issued"
# (This takes ~2-3 minutes)

# Verify cluster is healthy
gough cluster status
# Should show:
#   Status: initializing → ready
#   Quorum: healthy
#   Vault: unsealed
```

---

## Step 2: Provision First Worker Node via PXE (10 minutes)

### 2.1 Power On Second Machine
```bash
# Power on the machine you've designated as the first worker
# It should PXE-boot automatically

# In Gough WebUI, go to Machines tab
# You should see a new row appear within 60 seconds (state: "new")

# Via CLI:
gough node list
# Should show:
#   node-1 (primary) - state: ready
#   node-2 (new worker) - state: new

# If not appearing, check:
# - PXE is enabled in BIOS
# - Machine is on same network as primary
# - iPXE handler is running: `gough ipxe status`
```

### 2.2 Provision Node via WebUI
```bash
# Open WebUI: http://<primary_ip>:3000
# Navigate to Machines tab

# Click on node-2 (the "new" node)
# Click "Plan Disks" button

# In the disk editor:
#   - See all available disks (SATA, NVMe, etc.)
#   - Mark OS disk(s): reserve for root (usually 50 GB)
#   - Leave other disks as "dark drives" (reserved for distributed storage)

# Continue to "Select Biomes" step
#   - Select: k8s-worker (mandatory for Kubernetes cluster)
#   - Select: nest-agent (optional; provides local storage)

# Review "Deploy Preview"
#   - Confirms biome compatibility with hardware
#   - Shows network configuration

# Click "Approve & Deploy"
#   - Phase-1 (hardware inventory) starts immediately
#   - Phase-2 (cloud-init network setup) starts after Phase-1
#   - Phase-3 (biome deployment, k8s join) starts after Phase-2
#   - Total time: ~8-12 minutes
```

### 2.3 Monitor Deployment
```bash
# Watch progress via CLI
gough node provision-watch --node-id=node-2

# Or check status:
gough node list --format=table
# State progression: new → discovering → probed → provisioning → ready

# Check pod status once node transitions to provisioning:
kubectl -n gough get pods -o wide | grep node-2
# Should see pods like cloud-init-node-2, lxd-join-node-2

# Once "ready", verify node is in Kubernetes cluster:
kubectl get nodes
# Should show:
#   NAME    STATUS   ROLES   AGE
#   node-1  Ready    master  X minutes
#   node-2  Ready    <none>  X minutes
```

---

## Step 3: Deploy the k8s-primary Biome (5 minutes)

### 3.1 Check Available Biomes
```bash
# List built-in biomes
gough biome list
# Should show:
#   k8s-primary (bootstrap k8s control plane)
#   k8s-worker (join k8s cluster)
#   nest-agent (distributed storage)
#   (others...)

# Check biome details
gough biome inspect --biome-id=k8s-primary
# Shows requirements, dependencies, Phase-2 duration estimate
```

### 3.2 Deploy via WebUI
```bash
# WebUI: Deployments tab

# Click "New Deployment"
#   - Select biome: k8s-primary
#   - Select nodes: node-1 (primary)
#   - Confirm resource allocation (CPU, memory, disk)

# Or via CLI:
gough biome deploy --biome-id=k8s-primary --node-id=node-1

# Monitor deployment
kubectl -n gough get pods -l biome=k8s-primary -w
# Should transition from pending → init → running

# Check deployment logs
gough biome deploy-log --deployment-id=<id> --follow
```

### 3.3 Verify Kubernetes Cluster
```bash
# Once deployment is ready (should take ~2-3 minutes)
kubectl cluster-info
# Should show:
#   Kubernetes master is running at https://...

# Verify all nodes are Ready
kubectl get nodes
# All nodes should show "Ready" under STATUS

# Verify system pods
kubectl get pods -n kube-system
# Should show running coredns, kube-proxy, etc.

# Quick health check
kubectl get all -A | head -20
# Verify no pods in CrashLoopBackOff or Pending state
```

---

## Step 4: Verify Cluster Health (5 minutes)

### 4.1 Audit Trail
```bash
# Verify audit chain is intact
gough audit verify --since=-1h
# Should exit 0 with no errors

# View recent audit entries
gough audit log --limit=5
# Should show initialization and deployment events
```

### 4.2 Monitor & Metrics
```bash
# Check Prometheus
curl -s http://localhost:9090/api/v1/query?query=up | jq '.data.result | length'
# Should show multiple targets (Prometheus itself, kubelet, etc.)

# Check Grafana
# Open http://localhost:3000/d/cluster-overview
# Should show healthy cluster dashboard
```

### 4.3 Storage Status (if nest-agent was deployed)
```bash
# Check storage backend
gough storage status
# Should show:
#   Nest (distributed storage): healthy
#   Replicas: 3 (one per node)

# Verify storage class
kubectl get storageclasses
# Should show: nest-replicated (or similar)
```

---

## Next Steps (Day Two Onward)

1. **Add More Nodes:** Repeat Step 2 for additional worker nodes
2. **Monitor & Alerting:** Set up on-call escalations for Prometheus alerts
3. **Backup:** Configure daily cluster snapshots (see Day Two guide)
4. **Deploy Workloads:** Deploy your first application biome to the k8s cluster

---

## Troubleshooting

### Node doesn't appear in Machines list
```bash
# Check iPXE handler status
gough ipxe status

# Verify PXE is working
# Reboot machine with "Boot to Network" option

# Check network reachability
ping <node_ip>
```

### Phase-2 or Phase-3 fails
```bash
# Check cloud-init logs on the node
ssh <node_ip>
journalctl -u cloud-init -n 100

# Check for network issues
ip route
ip addr

# Retry provisioning (safe to do multiple times)
gough node provision-phase-2 --node-id=<id>
```

### Kubernetes node doesn't join cluster
```bash
# Check kubelet logs
journalctl -u kubelet -n 100

# Check if k8s-worker biome is running
gough biome deploy-status --biome-id=k8s-worker --node-id=<id>

# Check joiner secret is available
gough secret joiner-secret-status --node-id=<id>
```

### Cluster health check failed
```bash
# Run diagnostics
gough doctor cluster

# Check audit chain
gough audit verify

# Check quorum status
gough cluster status --detailed
```

---

## Key Commands Reference

| Task | Command |
|------|---------|
| Cluster status | `gough cluster status` |
| List nodes | `gough node list` |
| List biomes | `gough biome list` |
| Deploy biome | `gough biome deploy --biome-id=<biome> --node-id=<node>` |
| Monitor node | `gough node provision-watch --node-id=<node>` |
| Kubernetes access | `kubectl get nodes` |
| Audit logs | `gough audit log --limit=10` |
| Troubleshoot | `gough doctor <component>` |

---

## Success Checklist

- [ ] Gough cluster initialized and primary node ready
- [ ] First worker node PXE-booted and provisioned
- [ ] Worker node shows Ready in `kubectl get nodes`
- [ ] k8s-primary biome deployed successfully
- [ ] Kubernetes API is responsive (`kubectl cluster-info`)
- [ ] System pods (coredns, kube-proxy) are running
- [ ] Audit trail verified intact
- [ ] No critical alerts in Prometheus

**You're ready for Day Two operations!**

