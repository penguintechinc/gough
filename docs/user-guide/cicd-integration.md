# CI/CD integration guide

Integrate `gough` CLI into CI/CD pipelines for automated cluster operations, node provisioning, and infrastructure deployment.

---

## Machine authentication via GOUGH_TOKEN

**Option 1: Environment variables (recommended)**

Set `GOUGH_TOKEN` and `GOUGH_CLUSTER_URL` in your CI environment:

```bash
export GOUGH_TOKEN="tok_abc123..."
export GOUGH_CLUSTER_URL="https://gough.prod.example.com"

gough node list
# Authenticates as the token holder without prompting
```

**Option 2: Config file (for local testing)**

```bash
gough config set-context production --cluster https://gough.prod.example.com
gough login --token tok_abc123...

gough node list
# Uses stored credentials from config
```

**Generating machine tokens:**

1. Log in with user credentials (one-time)
   ```bash
   gough login --cluster https://gough.prod.example.com
   ```

2. Create a machine token
   ```bash
   gough token create --name=ci-deployer --scope=node:read,node:write,biome:deploy
   # Output: tok_xyz789...
   ```

3. Store token in CI/CD secrets manager:
   - GitHub Actions: Settings → Secrets → `GOUGH_TOKEN`
   - GitLab CI: Settings → CI/CD → Variables → `GOUGH_TOKEN`
   - Jenkins: Credentials → Secret text → `GOUGH_TOKEN`

4. Reference in pipelines
   ```yaml
   # GitHub Actions
   - name: Deploy node
     env:
       GOUGH_TOKEN: ${{ secrets.GOUGH_TOKEN }}
       GOUGH_CLUSTER_URL: https://gough.prod.example.com
     run: gough node provision --node-id=node-42
   ```

**Token scopes (examples):**
- `node:read` — list, inspect, get status
- `node:write` — provision, tag, drain
- `biome:read` — list, inspect biomes
- `biome:deploy` — deploy, undeploy biomes
- `cluster:read` — cluster status, audit log
- `cluster:admin` — all operations (use sparingly)

---

## Exit code reference

All `gough` commands follow standard exit codes. Script accordingly:

| Exit Code | Meaning | When to Retry |
|-----------|---------|---------------|
| 0 | Success | N/A |
| 1 | General error (invalid args, I/O error) | No |
| 3 | Auth error (invalid token, expired, insufficient scope) | No, re-authenticate |
| 4 | Tenant mismatch (token tenant != request tenant) | No, check token and cluster |
| 5 | Rate limited (too many requests, wait and retry) | Yes, exponential backoff |
| 6 | Validation error (bad input, missing required field) | No, fix input |
| 9 | Cluster unhealthy (components down, quorum lost) | Yes, wait and retry |
| 10 | DR drill failed | Investigate, don't auto-retry |

**Scripting example:**
```bash
gough node provision --node-id=node-42
exit_code=$?

case $exit_code in
  0)
    echo "Provisioning started"
    ;;
  5)
    echo "Rate limited, retrying in 30s..."
    sleep 30
    gough node provision --node-id=node-42
    ;;
  *)
    echo "Error: $exit_code"
    exit $exit_code
    ;;
esac
```

---

## GitHub Actions example

**Login-free workflow using GOUGH_TOKEN:**

```yaml
name: Provision node

on:
  workflow_dispatch:
    inputs:
      node_id:
        description: Node ID to provision
        required: true
        type: string

jobs:
  provision:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@<full-sha>

      - name: Install gough CLI
        run: |
          curl -fsSL https://releases.penguintech.io/gough/install.sh | bash

      - name: Provision node
        env:
          GOUGH_TOKEN: ${{ secrets.GOUGH_TOKEN }}
          GOUGH_CLUSTER_URL: https://gough.prod.example.com
        run: |
          gough node provision --node-id=${{ inputs.node_id }}
          gough node provision-watch --node-id=${{ inputs.node_id }} --timeout=20m

      - name: Verify provisioning
        run: |
          gough node list --node-id=${{ inputs.node_id }}
          # Exit 0 if node is "ready"

      - name: Notify Slack on success
        if: success()
        run: |
          curl -X POST ${{ secrets.SLACK_WEBHOOK }} \
            -d '{"text":"Node ${{ inputs.node_id }} provisioned successfully"}'

      - name: Notify Slack on failure
        if: failure()
        run: |
          curl -X POST ${{ secrets.SLACK_WEBHOOK }} \
            -d '{"text":"Node ${{ inputs.node_id }} provisioning FAILED"}'
```

---

## GitLab CI example

**Automated node provisioning on merge:**

```yaml
stages:
  - provision
  - verify

variables:
  GOUGH_CLUSTER_URL: https://gough.prod.example.com

provision-node:
  stage: provision
  image: ubuntu:24.04
  script:
    - curl -fsSL https://releases.penguintech.io/gough/install.sh | bash
    - gough node provision --node-id=$NODE_ID --biomes=k8s-worker,nest-agent
    - gough node provision-watch --node-id=$NODE_ID --timeout=25m
  environment:
    name: production
  only:
    - main
  retry:
    max: 2
    when:
      - runner_system_failure
      - stuck_or_timeout_failure

verify-provisioning:
  stage: verify
  image: ubuntu:24.04
  script:
    - curl -fsSL https://releases.penguintech.io/gough/install.sh | bash
    - |
      status=$(gough node list --node-id=$NODE_ID --output json | jq -r '.state')
      if [ "$status" != "ready" ]; then
        echo "Node not ready: $status"
        exit 1
      fi
    - kubectl get nodes $NODE_ID
  environment:
    name: production
  only:
    - main
  dependencies:
    - provision-node
```

---

## Non-interactive flags for scripting

**JSON output for parsing:**
```bash
# Output as JSON for jq/Python/etc.
gough node list --output=json | jq '.[] | select(.state=="ready")'

# Example: get node IP addresses
gough node list --output=json | jq -r '.[] | "\(.id): \(.ip)"'
```

**Quiet mode (suppress output):**
```bash
# Exit code only, no stdout
gough node provision --node-id=node-42 --quiet
echo "Provision returned: $?"
```

**Verbose debug output:**
```bash
# Print HTTP requests, responses, timing
gough node list --verbose
# Output includes:
#   GET /api/v1/nodes HTTP/1.1
#   Host: gough.prod.example.com
#   Authorization: Bearer tok_...
#   <response body>
#   Latency: 120ms
```

---

## Scripting patterns

### Automated node provisioning

```bash
#!/bin/bash
set -e  # exit on error

CLUSTER_URL=${GOUGH_CLUSTER_URL:-https://gough.prod.example.com}
NODES=${@:-node-{1..5}}

for node_id in $NODES; do
  echo "Provisioning $node_id..."
  
  gough node provision --node-id=$node_id --biomes=k8s-worker --quiet
  
  # Watch progress with timeout
  if ! gough node provision-watch --node-id=$node_id --timeout=20m --quiet; then
    echo "ERROR: $node_id provisioning failed or timed out"
    exit 1
  fi
  
  # Verify node is ready
  status=$(gough node list --node-id=$node_id --output=json | jq -r '.state')
  if [ "$status" != "ready" ]; then
    echo "ERROR: $node_id is in state $status (expected ready)"
    exit 1
  fi
  
  echo "✓ $node_id ready"
done

echo "All nodes provisioned successfully"
```

### Parse JSON output with jq

```bash
#!/bin/bash

# Get all nodes in "ready" state
gough node list --output=json | jq -r '.[] | select(.state=="ready") | .id'

# Count ready nodes
gough node list --output=json | jq '[.[] | select(.state=="ready")] | length'

# Get node details as CSV
gough node list --output=json | jq -r '.[] | [.id, .ip, .state, .role] | @csv'
```

### Biome deployment with rollback

```bash
#!/bin/bash
set -e

BIOME=my-app
TARGET_NODES=$(gough node list --output=json | jq -r '.[] | select(.state=="ready") | .id')
BACKUP_DIR=/tmp/gough-backup

# Backup current deployment
mkdir -p $BACKUP_DIR
gough biome list --output=json > $BACKUP_DIR/biomes-before.json

# Deploy biome
for node_id in $TARGET_NODES; do
  echo "Deploying $BIOME to $node_id..."
  if ! gough biome deploy --biome-id=$BIOME --node-id=$node_id; then
    echo "Deployment to $node_id failed, rolling back..."
    # Restore from backup (implementation depends on your backup strategy)
    exit 1
  fi
done

echo "✓ Deployment successful"
```

### Exit code handling with retry

```bash
#!/bin/bash

retry_with_backoff() {
  local max_attempts=5
  local attempt=1
  local delay=2
  
  while [ $attempt -le $max_attempts ]; do
    echo "Attempt $attempt/$max_attempts..."
    
    "$@"
    exit_code=$?
    
    case $exit_code in
      0)
        return 0  # Success
        ;;
      5)
        echo "Rate limited, waiting ${delay}s..."
        sleep $delay
        delay=$((delay * 2))  # Exponential backoff
        attempt=$((attempt + 1))
        ;;
      9)
        echo "Cluster unhealthy, waiting ${delay}s..."
        sleep $delay
        attempt=$((attempt + 1))
        ;;
      *)
        echo "Fatal error: $exit_code"
        return $exit_code
        ;;
    esac
  done
  
  echo "Max attempts reached"
  return 1
}

retry_with_backoff gough node list
```

---

## Troubleshooting CI/CD failures

**"no cluster URL configured"**
```bash
# Set via environment variable
export GOUGH_CLUSTER_URL=https://gough.prod.example.com
gough cluster status

# Or set in CI/CD secrets
```

**"invalid token"**
```bash
# Token is expired or malformed
gough token create --name=ci-deployer --scope=...
# Update CI/CD secret with new token
```

**"tenant mismatch"**
```bash
# Token and cluster are from different tenants
gough config get-contexts
# Verify context cluster and token tenant match
```

**"cluster unhealthy" (exit 9)**
```bash
# One or more components are down
gough cluster status

# Wait and retry (exit 9 suggests transient failure)
sleep 30 && gough cluster status
```

**Enable verbose logging:**
```bash
gough node list --verbose
# Shows HTTP requests, responses, timing, and errors
```

---

## Example: Multi-site node provisioning

```bash
#!/bin/bash

SITES=("dal2-primary" "dal2-secondary")
NODES_PER_SITE=3

for site in "${SITES[@]}"; do
  echo "=== Provisioning site: $site ==="
  
  export GOUGH_CLUSTER_URL="https://$site.penguintech.cloud"
  
  for i in $(seq 1 $NODES_PER_SITE); do
    node_id="node-$site-$i"
    
    echo "Provisioning $node_id..."
    gough node provision --node-id=$node_id --biomes=k8s-worker --quiet
    
    # Don't wait synchronously; kick off all in parallel
  done
  
  # Now wait for all nodes in this site
  echo "Waiting for all nodes in $site to be ready..."
  for i in $(seq 1 $NODES_PER_SITE); do
    node_id="node-$site-$i"
    if ! gough node provision-watch --node-id=$node_id --timeout=20m --quiet; then
      echo "ERROR: $node_id failed"
      exit 1
    fi
    echo "✓ $node_id ready"
  done
done

echo "All sites provisioned successfully"
```

---
