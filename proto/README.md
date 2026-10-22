# Gough Protocol Buffers (gRPC Definitions)

This directory contains all Protocol Buffer schemas for Gough's internal gRPC communication plane.

## Layout

- `v1/` — Protocol Buffer modules (current version)
  - `gough/` — Service definitions (11 services, one per file)
- `buf.yaml` — Buf linting and breaking change detection config
- `buf.gen.yaml` — Code generator plugin configuration

## Services

| Service | File | Methods |
|---------|------|---------|
| Nodes | `nodes.proto` | Discover, List, Get, Patch, Deploy, Reject, Rekey, Evacuate, Decommission, EventStream, AddManual |
| Biomes | `biomes.proto` | List, Get, Create, Update, Delete, Sign, Upgrade, EligibilityCheck, Validate, Publish |
| Disks | `disks.proto` | List, Plan, Patch, SmartRecheck |
| Migration | `migration.proto` | PolicyGet, PolicyPatch, Trigger, EventStream, SafetyEnvelope |
| Capacity | `capacity.proto` | Forecast, Risks |
| Audit | `audit.proto` | AppendEvent, Stream, Verify, ExportRange |
| JoinerSecrets | `joiner.proto` | Emit, Consume, Rotate, Revoke, List |
| Discovery | `discovery.proto` | Probe, EventStream (bidi), ExecCommand, ExtractSecret |
| IPXE | `ipxe.proto` | ResolveBootScript, BindMac, MintBootstrapToken |
| Primary | `primary.proto` | Status, Replace, ForceRecover, FrontendSwitch, RotateCA |
| Network | `network.proto` | BaselineConfigure, ProviderMigrate, FallbackControl, Status |

## Adding a New Service

1. Create `v1/gough/{service}.proto`
2. Define package `gough.v1`, service name, and rpc methods
3. Run `make proto` to regenerate stubs

## Regenerating Stubs

```bash
cd proto && buf generate
```

Generated code appears in:
- `../services/api-manager/app/grpc/` (Python)
- `../services/discovery-agent/internal/grpcgen/` (Go)

Or use the Makefile target:
```bash
make proto
```
