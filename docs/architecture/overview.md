# Gough Platform Architecture Overview

**Canonical reference:** See `/Users/penguinz/code/gough/docs/specs/platform-spec.md` for complete detailed specification.

---

## System at a Glance

Gough is a two-phase LXD + Kubernetes provisioning platform that:

1. **Phase 1 (Discovery):** PXE-boots new hardware, inventories CPU/RAM/disks/NICs, detects SMART warnings, validates BMC certificates, collects hardware tags
2. **Phase 2 (Deployment):** Cloud-init configures networking, installs LXD, joins existing cluster, deploys user-selected Biomes (cloud-init-based workloads)
3. **Orchestration:** Manages Biomes (k8s-primary, k8s-worker, nest-agent, vault-leader, etc.) across LXD containers + Kubernetes, with live migration, capacity forecasting, and multi-cluster DR

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Operator (Human)                        │
│              WebUI (Vite/React) + CLI (Gough)               │
└──────────────────────┬──────────────────────────────────────┘
                       │
        ┌──────────────┴──────────────┐
        │                             │
   ┌────▼────────────────────┐   ┌───▼──────────────────────┐
   │  API Manager (Python)    │   │ iPXE Handler (Python)   │
   │ - CRUD: Machines, Biomes   │   │ - PXE boot handler      │
   │ - RBAC, audit logging    │   │ - Phase-1 inventory     │
   │ - gRPC + REST endpoints  │   │ - cloud-init templating │
   │ - License enforcement    │   │ - Bootstrap token issue  │
   └──────┬────────────────────┘   └───┬──────────────────────┘
          │                            │
    ┌─────▼─────────────────────────────▼─────────┐
    │   Core Services (Kubernetes namespace)      │
    ├────────────────────────────────────────────┤
    │ • Vault (PKI, secrets, bootstrap tokens)    │
    │ • SPIRE (SVID identity plane, mTLS)         │
    │ • PostgreSQL (OLTP: machines, biomes, audit)  │
    │ • Prometheus (metrics, alerting)            │
    │ • Grafana (dashboards)                      │
    │ • NATS (event streaming for cluster events) │
    │ • Audit chain (append-only log, offsite)    │
    │ • Longhorn/Ceph (persistent storage)        │
    └────────────────────────────────────────────┘
          │
    ┌─────▼──────────────────────────────────┐
    │   LXD Hosts + Kubernetes Control Plane │
    │                                         │
    │  ┌─────────────────────────────────┐   │
    │  │ Host Node (LXD + kubelet)       │   │
    │  │ ├─ Container: k8s-primary       │   │
    │  │ ├─ Container: k8s-worker        │   │
    │  │ ├─ Container: nest-agent        │   │
    │  │ └─ (user-defined biomes)          │   │
    │  └─────────────────────────────────┘   │
    │             (Repeated per node)         │
    └─────────────────────────────────────────┘
```

---

## Core Components

| Component | Role | Technology | Port |
|-----------|------|-----------|------|
| **API Manager** | API + WebUI backend, RBAC, audit | Quart (Python) | 8080 |
| **iPXE Handler** | PXE boot, Phase-1 inventory | Python | 69 (TFTP) |
| **WebUI** | Operator dashboard, workflow | React/Vite | 3000 |
| **Vault** | PKI, secret management | HashiCorp Vault | 8200 |
| **SPIRE** | SVID identity issuance, mTLS | CNCF SPIRE | 8081 |
| **PostgreSQL** | OLTP database | PostgreSQL 16 | 5432 |
| **Prometheus** | Metrics, alerting | Prometheus | 9090 |
| **NATS** | Event streaming | NATS | 4222 |
| **Kubernetes** | Workload orchestration | Kubernetes 1.31+ | 6443 |
| **LXD** | Container/VM host | Canonical LXD | 8443 |

---

## Kubernetes Control Plane Frontend

The control plane exposes its API server on a stable, highly-available frontend address. Gough supports three modes:

### Frontend modes at a glance

```
┌─────────────────────────────────────────────────────────────┐
│             Control Plane Frontend Modes                    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  kube-vip (default, ≤ 7 CP nodes)                           │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ VIP (10.2.0.10:6443)                                │   │
│  │  ├── CP Node 1 (kube-vip static pod, ARP owner)    │   │
│  │  ├── CP Node 2 (kube-vip static pod, standby)      │   │
│  │  ├── CP Node 3 (kube-vip static pod, standby)      │   │
│  │  └── Workers + external clients → VIP             │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  external (> 7 CP nodes, bring-your-own LB)                │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ External LB (nlb.internal.example.com:6443)          │   │
│  │  ├── CP Node 1 (kubeadm join <external-vip>)       │   │
│  │  ├── CP Node 2 (kubeadm join <external-vip>)       │   │
│  │  ├── CP Node N (kubeadm join <external-vip>)       │   │
│  │  └── You manage: F5, NLB, HAProxy, keepalived       │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  none (single-node lab demo only)                           │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Direct node IP (10.2.0.1:6443)                       │   │
│  │ └─ No HA; no clustering possible; demo only         │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

**kube-vip L2 ARP failover:** When a control-plane node fails, the remaining nodes renegotiate ARP ownership of the VIP. Clients automatically reroute to the new owner within 2–5 seconds.

**Sizing:** Use kube-vip for small clusters (default), external LB for large clusters (7+ nodes), none for single-node labs only.

For detailed configuration and sizing recommendations, see `/docs/user-guide/networking.md` → "Control plane frontend".

---

## See Also

- `/docs/user-guide/networking.md` — Control plane frontend configuration and sizing
- `/docs/specs/platform-spec.md` — Complete specification
- `/docs/runbooks/control-plane-replace.md` — Replace a control-plane node
- `/docs/runbooks/quorum-recovery.md` — Break-glass quorum recovery
- `/docs/runbooks/ipxe-ca-rotation.md` — Rotate iPXE CA
- `/docs/runbooks/` — Troubleshooting procedures

