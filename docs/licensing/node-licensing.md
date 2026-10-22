# Gough Node Licensing

How gough meters nodes, which requests are refused, and what an operator sees.

Implementation: `services/api-manager/app/licensing.py`.
For the license server API itself see [license-server-integration.md](license-server-integration.md).

## The model: activation is metered, inventory is free

Gough charges for nodes it **activates**, not for nodes it can **see**.

| Action | Metered? |
|---|---|
| Bare-metal node discovered over PXE / iPXE / gRPC enrolment | ❌ free |
| Pre-existing cloud VM synced in from your AWS/GCP/Azure/Vultr account | ❌ free |
| `POST /api/v1/nodes/{id}/deploy` | ✅ consumes a slot |
| `POST /api/v1/clouds/{id}/machines` | ✅ consumes a slot |
| Re-deploying a node that is already active | ❌ free (already counted) |

This means you can inventory an entire datacenter on the free tier and see every
machine you own. You pay only for what you actually run. A machine that PXE-boots
is never silently dropped because you hit a cap.

## Allowances

| Tier | Nodes |
|---|---|
| `community` (unlicensed, or no `LICENSE_KEY`) | **3**, any mix of physical / virtual / cloud |
| `professional` | `limits.max_servers` from the license |
| `enterprise` | `limits.max_servers` from the license |
| PenguinTech-controlled domain | unlimited (enforcement bypassed) |

Both paid tiers are metered per node — tier controls *features*, never the node
count. `max_servers: -1` means unlimited.

Bypass is **domain-based only**, never an env var or config flag:
`*.penguincloud.io`, `*.penguintech.cloud`, `*.localhost.local`.

## What counts as an active node

Two registries are summed; they do not mirror each other, so neither alone is a
complete picture.

- **`nodes`** — bare metal, counted when `state` is one of
  `deploying`, `configuring`, `ready`, `upgrading`, `quarantined`, `draining`.
  Not counted: `new`, `probed`, `planned` (inventory) or
  `decommissioned`, `rejected` (gone).
- **`cloud_machines`** — counted only when the machine carries gough's
  `gough-managed=true` tag, and its status is not `terminated`/`failed`/`broken`.

That tag is stamped provider-side at creation, so it survives an inventory sync
overwriting the local row. It is what separates *"gough provisioned this"* from
*"gough found this in your account"* — without it, connecting an AWS account
holding 200 instances would instantly exhaust a 3-node allowance.

A stopped cloud VM still counts: it still exists and still bills.

## Being refused

Both enforcement points return **402** with the counts that produced the refusal:

```json
{
  "status": "error",
  "error": {
    "code": "license_required",
    "message": "Node allowance reached (3/3). Deploying another node requires additional licensed nodes.",
    "details": { "active_nodes": 3, "allowed_nodes": 3, "node_id": 42 }
  }
}
```

Nodes already active above the allowance are **never** retroactively removed or
stopped. The cap only refuses new activations.

## Multi-cloud is flag-gated, not licence-gated

`/api/v1/clouds/*` sits behind the PostHog flag **`gough.multi-cloud`**. This is
a kill-switch and staged-rollout control, *not* a paid feature — all six
providers (`maas`, `lxd`, `aws`, `gcp`, `azure`, `vultr`) are available on every
tier. The node count is what is metered, not which provider you use.

> ⚠️ **The flag defaults OFF.** Per the "new flags default OFF until validated"
> rule, a deployment that has never configured `POSTHOG_KEY` — or one where the
> flag has not been switched on — gets **404 `feature_disabled`** on every
> `/api/v1/clouds/*` route. Set `POSTHOG_KEY` and enable the flag before
> expecting the cloud surface to answer.

## Failure behaviour

Licensing must never take the control plane down with it.

| Situation | Behaviour |
|---|---|
| License server unreachable, value cached | Last-known allowance is reused |
| License server unreachable, nothing cached | Falls back to 3 (community) |
| Malformed / unparseable entitlement response | Falls back to 3 |
| Invalid licence | Falls back to 3 |
| PostHog unreachable, flag cached | Last-known flag value |
| PostHog unreachable or unconfigured, nothing cached | Flag reads OFF |
| Node counting query fails | Counted as 0 — never blocks deployment |

Lookups are cached for 5 minutes. A failed refresh keeps the stale value rather
than demoting a paying customer mid-outage.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LICENSE_KEY` | *(unset → community)* | `PENG-XXXX-XXXX-XXXX-XXXX-ABCD` |
| `LICENSE_SERVER_URL` | `https://license.penguintech.io` | Entitlement API |
| `PRODUCT_NAME` | `gough` | Sent on validate |
| `POSTHOG_KEY` | *(unset → flags OFF)* | PostHog project API key |
| `POSTHOG_HOST` | `https://license.penguintech.io` | Flag endpoint |
| `GOUGH_CLUSTER_ID` | `gough` | Flag `distinct_id` |

The license key is sent in an `Authorization: Bearer` header, never in a request
body or query string, so it stays out of access logs and proxy traces.
