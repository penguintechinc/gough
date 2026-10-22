# Plan 5 — API Services & Management Interfaces

## Goal
Provide operator and consumer-facing interfaces: REST API (OpenAPI 3.1), WebUI tabs (Machines, Biomes, Deployments, Storage), CLI (`gough` command), and event streaming (NATS for real-time state changes). Mara can manage cluster from CLI or WebUI; Priya can view quota and audit logs.

## Scope
- [x] API Manager (Quart REST + gRPC endpoints on port 8080 / 50051)
- [x] WebUI tabs: Machines, Biomes, Deployments, Storage
- [x] CLI (`gough` command: node, biome, cluster, audit, auth subcommands)
- [x] Prometheus metrics (request rates, latencies, queue depths)
- [x] NATS event streaming (machine state changes, deployments, audit events)
- [x] License enforcement (WaddleAI integration for capacity forecast)
- [x] RBAC (operator, maintainer, viewer roles)

Out of scope (M2+): Multi-tenant RBAC (team-scoped in M3), GraphQL, advanced dashboarding (Grafana templates provided).

## API Surface
**REST endpoints** (non-exhaustive):
- `GET /api/v1/machines`, `GET /api/v1/machines/{id}`, `POST /api/v1/machines/{id}/probe`
- `GET /api/v1/biomes`, `GET /api/v1/biomes/{id}`, `POST /api/v1/biomes/{id}/deploy`
- `GET /api/v1/deployments`, `GET /api/v1/deployments/{id}/logs`
- `GET /api/v1/cluster/status`, `POST /api/v1/cluster/drain-node`
- `GET /api/v1/storage/quotas`, `POST /api/v1/storage/quota-request`
- `GET /api/v1/audit/log`, `POST /api/v1/audit/export`
- `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout`
- `GET /health`, `GET /ready`, `GET /api/v1/openapi.yaml`

**WebUI components:**
- Machines tab: list, detail, probe, tag override
- Biomes tab: catalog, custom biomes, publish, signature verification
- Deployments tab: status, logs, retry, rollback (M2)
- Storage tab: quota request, Longhorn/Ceph admin (if applicable)

**CLI** (gough command):
- `gough node list/get/probe/tag`
- `gough biome list/get/verify/deploy`
- `gough deployment list/logs/cancel`
- `gough cluster status/drain-node`
- `gough audit list/verify/export`
- `gough auth login/refresh/logout`
- `gough storage quota-request`

**NATS topics:**
- `gough.machines.{id}.state-changed` — published when node state changes
- `gough.deployments.{id}.status-changed` — deployment progressed to new phase
- `gough.audit.entry-written` — audit log entry appended

## Metrics & Alerts
- Request latency histograms (p50, p95, p99)
- API error rate counter (by endpoint, status code)
- Goroutine count gauge
- Longhorn/Ceph storage utilization gauge
- Queue depth for pending provisioning/deployments
- NATS message lag (audit sync)

## Acceptance Criteria
1. OpenAPI spec validates: `spectral lint api/v1/openapi.yaml`; SDK can be generated and used
2. Mara uses CLI: `gough node list` shows all nodes; `gough deployment logs <id>` streams live logs
3. WebUI renders without JS errors; Machines tab shows CPU/RAM/disk/tags; Biomes tab shows available catalog + custom biomes
4. RBAC enforced: maintainer cannot delete machines; viewer cannot trigger deployments; license tokens validated on WaddleAI capacity calls
5. NATS event stream carries all machine/deployment state changes; consumer can build real-time dashboard

## Dependencies
Plan 1-4 complete: API queries PostgreSQL tables, enforces auth/authz from Plan 4, uses gRPC clients to internal services (Vault, SPIRE, Longhorn). NATS broker deployed as cluster dependency.
