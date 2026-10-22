# Networking guide: management, internal, external

Gough clusters have three distinct network baselines: management (out-of-band), internal (cluster-to-cluster), and external (client-facing). Configure each independently based on your infrastructure and requirements.

---

## The three network baselines

| Baseline | Purpose | Use Case |
|----------|---------|----------|
| **Management** | Out-of-band access (iLO, IPMI, BMC, SSH) | Small, stable network (~10.0.0.0/24) |
| **Internal** | Pod-to-pod, inter-node gRPC (50051), Kubernetes | Dynamic, high-traffic (10.1.0.0/16 or larger) |
| **External** | Client-facing API, ingress, public services | Scalable, isolated from internal (10.2.0.0/24 or public) |

---

## Management baseline

**Purpose:** Out-of-band management separate from workload traffic. Used for hardware BMC/iLO access, SSH to nodes, Prometheus scrape, etc.

**Configuration:**
```bash
gough cluster network-baseline status
# Shows current mgmt baseline config

gough cluster network-baseline configure mgmt \
  --opt cidr=10.0.0.0/24 \
  --opt vlan=10 \
  --opt mtu=1500
```

**Typical configuration:**
- **CIDR:** 10.0.0.0/24 (room for 254 nodes)
- **VLAN:** Isolated VLAN (10-50, varies by site)
- **MTU:** 1500 (standard)
- **Gateway:** Dedicated mgmt network gateway
- **DNS:** Squawk (recommended) or built-in dnsmasq

**When to use Squawk for mgmt DHCP/DNS:**
```bash
gough cluster network-baseline migrate mgmt --to squawk
```

**Benefits:**
- Scalable to hundreds of nodes
- Integrated with Squawk cluster-wide
- Automatic DNS updates for new nodes
- High availability (redundant Squawk instances)

**Built-in dnsmasq fallback:**
- Used during initial bootstrap only
- Single point of failure
- Deprecated for production; migrate to Squawk immediately

---

## Internal baseline

**Purpose:** High-throughput cluster-internal traffic (pod-to-pod, inter-node gRPC, etcd, Vault).

**Configuration:**
```bash
gough cluster network-baseline status

gough cluster network-baseline configure internal \
  --opt cidr=10.1.0.0/16 \
  --opt vlan=100 \
  --opt provider=cilium \
  --opt mtu=9000
```

**Typical configuration:**
- **CIDR:** 10.1.0.0/16 (65k IPs; 256 /24 subnets for pods)
- **VLAN:** Dedicated VLAN, isolated from mgmt and external (100-199)
- **MTU:** 9000 if jumbo frames supported (else 1500)
- **Provider:** Cilium (XDP/eBPF for performance)
- **DNS:** Squawk for service discovery

**Scaling considerations:**
- /24 per node (256 pods per node): 10.1.X.0/24 where X = node index
- If you exceed 256 nodes, expand CIDR to 10.1.0.0/15 or custom

---

## External baseline

**Purpose:** Client-facing API, ingress, public services. Isolated from internal workload traffic.

**Configuration:**
```bash
gough cluster network-baseline configure external \
  --opt cidr=10.2.0.0/24 \
  --opt vlan=200 \
  --opt mtu=1500
```

**Two deployment modes:**

### Mode 1: Private external (internal datacenter)
- CIDR: 10.2.0.0/24 (private)
- Clients reach via internal LB (dal2.penguintech.cloud for beta)
- Ingress controller routes to internal pods

### Mode 2: Public external
- CIDR: Custom public IPs (assigned by ISP/cloud provider)
- DNS: Public domain (e.g., api.gough.example.com)
- Ingress controller terminates TLS, routes to pods

**Squawk integration for external DNS:**
```bash
gough cluster network-baseline migrate external --to squawk
# Squawk manages DNS for public domain
# Automatic failover to secondary API endpoints
```

---

## Control plane frontend

The Kubernetes API server needs a stable frontend address so that control plane nodes and workers can reach it reliably. Gough supports three modes for managing the control plane frontend.

### Frontend modes

| Mode | Recommended For | What Gough does | What you do | VIP address |
|------|---|---|---|---|
| **kube-vip** (default) | Small to medium clusters (≤ ~7 control-plane nodes) | Deploys static pod on every control-plane node; L2 ARP-based VIP failover; automatic node coordination | Configure VIP endpoint; Gough handles election and ARP announce | operator-assigned within `external` baseline |
| **external** | Large clusters (> ~7 CP nodes) or when using existing LB infrastructure | Plumbs your external LB endpoint into kubeadm config; does NOT manage the VIP or LB | Deploy and manage F5, AWS NLB, GCP TCP LB, HAProxy, or keepalived separately; point Gough to the endpoint | your-lb.internal.example.com:6443 |
| **none** | Single-node demo/lab only | First control-plane node runs `kubeadm init` with no VIP; no HA | Cluster cannot scale horizontally without redeploy; suitable for temporary labs only | none |

### Why kube-vip is the default

Kube-vip eliminates the need to stand up external LB infrastructure before deploying Kubernetes. The L2 ARP-based VIP is highly available — if any one control-plane node dies, the remaining instances renegotiate ARP ownership and the VIP stays answering. This makes the default suitable for small on-prem clusters where external LB is not available.

At scale (7+ control-plane nodes), dedicated LB hardware outperforms a kubelet static pod. Switch to `external` mode and bring your own load balancer.

### Sizing recommendation

- **≤ 7 control-plane nodes:** Use kube-vip (default). Configure your network switch to permit VIP ARP announces on the external baseline.
- **> 7 control-plane nodes or multi-site:** Use external mode. Deploy an LB (F5, NLB, HAProxy, etc.) and point Gough to it.
- **Single-node demo only:** Use none mode; accept that the cluster cannot scale later without redeploy.

### Frontend mode selection at deploy time

When deploying the `k8s-primary` biome:

```bash
# kube-vip mode (default)
gough biome deploy k8s-primary --node node-01 \
  --frontend-mode kube-vip \
  --frontend-endpoint 10.2.0.10:6443 \
  --frontend-interface ens3 \
  --frontend-baseline external

# external mode
gough biome deploy k8s-primary --node node-01 \
  --frontend-mode external \
  --frontend-endpoint nlb.internal.example.com:6443 \
  --frontend-baseline external

# none mode (single-node lab)
gough biome deploy k8s-primary --node node-01 \
  --frontend-mode none
```

### VIP failover behavior (kube-vip mode)

When a control-plane node running kube-vip fails:

1. Remaining kube-vip instances detect the loss via multicast/unicast heartbeat.
2. ARP negotiation occurs among remaining nodes (~2–5 seconds).
3. One remaining node becomes the new ARP responder for the VIP.
4. Client connections to the VIP are rerouted to the new responder.
5. Etcd consensus continues with the remaining quorum.

**Network requirement:** External baseline VLAN must permit ARP on the VIP address. Most switches allow this by default; verify with network team if ARP is restricted.

### BGP-mode anycast (M3+)

Anycast frontend mode (BGP route advertisement per control-plane node) requires router peering configuration that Gough cannot own. This is deferred to M3. For now, use kube-vip or external.

---

## Network baseline status and operations

**Check current configuration:**
```bash
gough cluster network-baseline status
```

**Example output:**
```
Management baseline:
  CIDR: 10.0.0.0/24
  VLAN: 10
  Provider: squawk (DHCP), squawk (DNS)
  Status: healthy

Internal baseline:
  CIDR: 10.1.0.0/16
  VLAN: 100
  Provider: cilium (CNI)
  Status: healthy
  MTU: 9000 (jumbo frames enabled)

External baseline:
  CIDR: 10.2.0.0/24
  VLAN: 200
  Status: healthy
  Ingress: running (2 replicas)
```

**Detailed network status:**
```bash
gough node list
# Shows network assignment per node

gough cluster status
# Includes network health summary
```

---

## Migrating a baseline: built-in to Squawk

**When to migrate:**
- Bootstrapping with built-in dnsmasq initially (acceptable)
- Scaling beyond ~10 nodes (Squawk recommended)
- Need HA DHCP/DNS (production requirement)

**Migration for management baseline:**
```bash
# Verify Squawk is configured
gough config get squawk.zone

# Initiate migration
gough cluster network-baseline migrate mgmt --to squawk
# Exit code 0 = success, wait ~2-5 minutes for cutover

# Verify migration
gough cluster network-baseline status
# Should show "Provider: squawk" for mgmt
```

**Migration for internal baseline:**
```bash
gough cluster network-baseline migrate internal --to squawk
```

**Migration for external baseline:**
```bash
gough cluster network-baseline migrate external --to squawk
```

**What happens during migration:**
1. Squawk zone is validated
2. DHCP lease holders are notified
3. DNS is updated to point to Squawk nameservers
4. Existing leases continue until expiration (no disruption)
5. New DHCP requests go to Squawk
6. Built-in dnsmasq is disabled

---

## Configuring Squawk integration

**Prerequisites:**
- Squawk cluster deployed and healthy
- Network connectivity from Gough nodes to Squawk DHCP/DNS servers
- Squawk zone ID (e.g., "dal2-mgmt", "dal2-internal")

**Setup:**
```bash
# Set Squawk zone for management baseline
gough cluster network-baseline configure mgmt --opt squawk_zone=dal2-mgmt

# Migrate to Squawk
gough cluster network-baseline migrate mgmt --to squawk
```

**Verify Squawk is providing DHCP/DNS:**
```bash
# On any node, check DHCP server
systemctl status systemd-networkd
# Should show DHCP configured via Squawk

# Verify DNS resolution
nslookup node-1.gough.local
# Should resolve via Squawk (not local dnsmasq)

# Check Squawk status from Gough CLI
gough cluster status | grep -i squawk
```

---

## Troubleshooting network issues

| Problem | Check | Fix |
|---------|-------|-----|
| New node doesn't get DHCP lease | Check mgmt baseline DHCP provider | If using Squawk, verify reachability; if using dnsmasq, check service: `systemctl status dnsmasq` |
| Pod-to-pod communication fails | Check internal VLAN config | Verify switch allows internal VLAN traffic; check MTU: `gough cluster network-baseline status` |
| DNS resolution slow | High query latency | Check DNS provider (Squawk vs built-in); verify Squawk zone is responsive |
| Ingress not accessible from outside | Check external baseline config | Verify external VLAN is routed to clients; check ingress controller: `kubectl -n ingress get pods` |
| Pod IP conflicts on different nodes | Check pod CIDR assignment | Expand CIDR if /16 is exhausted; verify Cilium state: `kubectl -n kube-system logs -l k8s-app=cilium` |

**Enable verbose networking diagnostics:**
```bash
# Check inter-node latency
gough node list
# Latency column shows ms between nodes

# Ping specific node
ping <node-ip>

# Check network interface MTU
ssh <node-ip> ip link show
# All interfaces should have consistent MTU
```

---

## Common baseline configurations

### Small cluster (1-10 nodes, single datacenter)
```bash
# Management
gough cluster network-baseline configure mgmt --opt cidr=10.0.0.0/24 --opt squawk_zone=local-mgmt

# Internal
gough cluster network-baseline configure internal --opt cidr=10.1.0.0/24 --opt vlan=100

# External
gough cluster network-baseline configure external --opt cidr=10.2.0.0/24 --opt vlan=200
```

### Medium cluster (10-50 nodes, single site)
```bash
# Management
gough cluster network-baseline configure mgmt --opt cidr=10.0.0.0/24 --opt squawk_zone=dal2-mgmt

# Internal (multiple /24 subnets)
gough cluster network-baseline configure internal --opt cidr=10.1.0.0/16 --opt vlan=100 --opt provider=cilium --opt mtu=9000

# External
gough cluster network-baseline configure external --opt cidr=10.2.0.0/24 --opt vlan=200
```

### Large cluster (50+ nodes, multi-site)
```bash
# Management (separate for each site)
gough cluster network-baseline configure mgmt --opt cidr=10.0.0.0/24 --opt squawk_zone=dal2-mgmt

# Internal (expanded for multiple sites)
gough cluster network-baseline configure internal --opt cidr=10.1.0.0/15 --opt vlan=100 --opt provider=cilium --opt mtu=9000

# External (public or private)
gough cluster network-baseline configure external --opt cidr=203.0.113.0/24 --opt vlan=200
```

---
