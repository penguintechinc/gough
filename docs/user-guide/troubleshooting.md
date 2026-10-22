# Troubleshooting guide

Diagnostic and recovery procedures for common gough CLI issues.

---

## Authentication and login issues

### "no stored credentials" when running gough commands

**Cause:** CLI config doesn't have a token or context set.

**Check:**
```bash
gough config list
# Check if cluster.url and current_context are set

gough config current-context
# Should show a context name
```

**Fix:**
```bash
# Option 1: Set via environment variables (CI/CD recommended)
export GOUGH_TOKEN=tok_abc123...
export GOUGH_CLUSTER_URL=https://gough.prod.example.com
gough cluster status

# Option 2: Create and use a context (local development)
gough config set-context production \
  --cluster https://gough.prod.example.com

gough config use-context production

gough login --token tok_abc123...
# Or login interactively
gough login
```

**Check stored credentials:**
```bash
gough config get cluster.url
gough config get current_context
```

### "token expired" error

**Cause:** Stored token is no longer valid (lifetime exceeded).

**Check:**
```bash
gough config list
# Token appears to be set, but API rejects it
```

**Fix:**
```bash
# Generate new token
gough token create --name=my-token

# Update stored credentials
gough config set token <new-token>

# Or re-login
gough login
```

### "insufficient permissions" or "401 unauthorized"

**Cause:** Token doesn't have required scope for the operation.

**Check:**
```bash
# Test a read-only operation
gough cluster status  # should work with read scope

# If this fails, token is invalid; if it works, scope is limited
```

**Fix:**
```bash
# Create a token with broader scope
gough token create --name=admin --scope=*:*

# Or request specific scopes
gough token create --name=provisioner --scope=node:read,node:write,biome:deploy
```

### Keychain/credential storage issues (macOS/Linux)

**Cause:** Credentials are stored in system keychain but access is blocked or expired.

**Check:**
```bash
gough config list
# If token is missing but you think it's stored, keychain may be locked
```

**Fix (macOS):**
```bash
# Unlock Keychain via System Preferences or:
security unlock-keychain

# Retry login
gough login
```

**Fix (Linux):**
```bash
# If using pass or secretservice backend
pass show gough/token  # for pass
secret-tool lookup service gough  # for secretservice

# Re-add token
gough login
```

**Override with environment variables:**
```bash
# Bypass keychain entirely
export GOUGH_TOKEN=tok_abc123...
export GOUGH_CLUSTER_URL=https://gough.prod.example.com
gough cluster status
```

---

## Configuration and context errors

### "context not found" when using gough config use-context

**Cause:** Context name doesn't exist in config.

**Check:**
```bash
gough config get-contexts
# Lists all available contexts

gough config current-context
# Shows current active context
```

**Fix:**
```bash
# Create the context
gough config set-context production \
  --cluster https://gough.prod.example.com \
  --tenant tenant-123

# Use it
gough config use-context production
```

**List contexts:**
```bash
gough config get-contexts
# Output:
#   NAME         CLUSTER URL                    TENANT ID  ACTIVE
#   production   https://gough.prod.example.com tenant-123 ✓
#   staging      https://gough.staging.example.com
```

### "no cluster URL configured"

**Cause:** Neither `GOUGH_CLUSTER_URL` env var nor config context has a URL.

**Check:**
```bash
echo $GOUGH_CLUSTER_URL
# If empty, env var is not set

gough config get cluster.url
# If empty, config is not set
```

**Fix:**
```bash
# Option 1: Set env var (for CI/CD)
export GOUGH_CLUSTER_URL=https://gough.prod.example.com
gough cluster status

# Option 2: Set in config (for local development)
gough config set-context default --cluster https://gough.prod.example.com
gough config use-context default
```

---

## API errors by exit code

| Exit Code | HTTP Status | Meaning | Action |
|-----------|-------------|---------|--------|
| 0 | 200 | Success | N/A |
| 3 | 401 | Unauthorized (invalid/expired token) | Re-login: `gough login` |
| 4 | 401 | Tenant mismatch | Check token tenant matches cluster context |
| 5 | 429 | Rate limited | Backoff 30-60s, retry |
| 6 | 422 | Validation error (bad input) | Check command syntax and arguments |
| 9 | 503 | Cluster unhealthy | Wait 1-5 min, check `gough cluster status` |
| 10 | (special) | DR drill failed | Investigate drill result, don't auto-retry |

**Example: handling exit codes in scripts**
```bash
gough node provision --node-id=node-42
exit_code=$?

case $exit_code in
  0)
    echo "✓ Provisioning started"
    ;;
  3)
    echo "✗ Auth error: token expired or invalid"
    gough login
    exit 1
    ;;
  5)
    echo "⚠ Rate limited, waiting 60s..."
    sleep 60
    gough node provision --node-id=node-42
    ;;
  6)
    echo "✗ Validation error: check node-id"
    exit 1
    ;;
  9)
    echo "⚠ Cluster unhealthy, waiting 30s..."
    sleep 30
    gough cluster status
    exit 1
    ;;
  10)
    echo "✗ DR drill failed: investigate"
    exit 1
    ;;
  *)
    echo "✗ Unknown error: $exit_code"
    exit $exit_code
    ;;
esac
```

---

## Verbose flag for debugging

**Enable verbose output to see HTTP requests and responses:**

```bash
gough node list --verbose

# Output includes:
#   GET /api/v1/nodes HTTP/1.1
#   Host: gough.prod.example.com
#   Authorization: Bearer tok_...
#   Accept: application/json
#   
#   200 OK
#   Content-Length: 1234
#   Content-Type: application/json
#   
#   [{"id":"node-1","state":"ready",...}]
#   Latency: 240ms
```

**Common verbose output patterns:**

```bash
# Request rejected before reaching API
Error: no cluster URL configured
(verbose has no effect here; config error)

# API request succeeds but operation fails
GET /api/v1/nodes HTTP/1.1
200 OK
[{"id":"node-1",...}]
Latency: 120ms
# Check exit code and error message

# Network or TLS error
Error: connection refused at gough.prod.example.com:443
# Check cluster URL and network connectivity
```

**Enable verbose in CI/CD:**
```bash
export GOUGH_VERBOSE=1
gough node provision --node-id=node-42
```

---

## Custom PKI and CA certificates

**If your cluster uses self-signed or internal CA certificates:**

```bash
# Download CA certificate from your cluster
openssl s_client -connect gough.prod.example.com:443 \
  -showcerts </dev/null 2>/dev/null \
  | openssl x509 -out /tmp/gough-ca.crt

# Tell gough CLI to use it
gough config set ca_cert_path /tmp/gough-ca.crt

# Verify connection
gough cluster status

# Or use environment variable
export GOUGH_CA_FILE=/tmp/gough-ca.crt
gough cluster status
```

**Or use `--ca-file` flag on individual commands:**
```bash
gough cluster status --ca-file=/path/to/ca.crt
```

---

## Insecure mode for development/testing only

**NEVER use in production.** Disables TLS certificate validation.

```bash
# Skip TLS verification (dev/test only)
gough cluster status --insecure

# Or via environment variable
export GOUGH_INSECURE=1
gough cluster status
```

**When this is appropriate:**
- Local testing with self-signed certs
- Debugging TLS issues in dev environment
- Temporary troubleshooting (immediately revert)

**When this is NOT appropriate:**
- Production environments
- Any environment with sensitive data
- CI/CD pipelines (use proper CA certs instead)

---

## Rate limiting (exit code 5)

**Cause:** Too many API requests to the cluster in a short time.

**Typical rate limits:**
- List operations: 10 requests/minute per token
- Write operations: 5 requests/minute per token
- Admin operations: 1 request/minute per token

**Check if rate limited:**
```bash
gough node list
# Exit code 5 = rate limited

# Or use verbose mode
gough node list --verbose
# Should show HTTP 429 Too Many Requests
```

**Retry strategy:**
```bash
# Exponential backoff (recommended)
gough node list || sleep 30 && gough node list || sleep 60 && gough node list

# Or in a loop
for attempt in {1..5}; do
  gough node list && break
  if [ $? -eq 5 ]; then
    delay=$((2 ** attempt))
    echo "Rate limited, waiting ${delay}s..."
    sleep $delay
  fi
done
```

**Reduce request rate:**
```bash
# Batch operations instead of individual requests
gough node list  # 1 request instead of 10

# Avoid polling in tight loops
gough node provision-watch --node-id=node-42  # polls at reasonable interval
# Don't do: while true; do gough node list; done
```

---

## Cluster unhealthy (exit code 9)

**Cause:** One or more cluster components are down or unhealthy.

**Check component status:**
```bash
gough cluster status

# Output shows component health:
#   vault_sealed: false (healthy)
#   spire_healthy: true
#   db_healthy: true
#   k8s_healthy: true
#   nats_healthy: true

# If any show false or unhealthy, that's the issue
```

**Troubleshoot by component:**

**Vault is sealed:**
```bash
gough cluster status
# vault_sealed: true

# Unseal Vault (requires unseal keys)
gough cluster vault-unseal

# Or contact ops team
```

**Database is down:**
```bash
gough cluster status
# db_healthy: false

# Check which database is configured
gough config get db.type  # postgresql, mysql, etc.

# Check database logs (depends on your deployment)
# If using Kubernetes: kubectl logs -n gough deployment/postgres

# Retry after service recovers (wait 2-5 min)
sleep 120 && gough cluster status
```

**Kubernetes is down:**
```bash
gough cluster status
# k8s_healthy: false

# Check kubectl access
kubectl cluster-info

# If kubectl fails, Kubernetes API is down
# Contact infrastructure team
```

**Retry with backoff:**
```bash
# Cluster unhealthy often resolves in 1-5 minutes
for attempt in {1..10}; do
  gough cluster status && break
  echo "Cluster unhealthy, waiting 30s..."
  sleep 30
done
```

**If it persists:**
```bash
# Run full diagnostics
gough doctor cluster

# Check recent events
gough audit log --limit=20

# Escalate to ops team with diagnostic output
```

---

## Node provisioning timeouts

**Issue:** Node provision-watch times out before node reaches "ready" state.

**Check timeout setting:**
```bash
gough node provision-watch --node-id=node-42 --timeout=20m
# Default timeout is 20 minutes; increase if needed

gough node provision-watch --node-id=node-42 --timeout=30m
```

**Check node state:**
```bash
gough node list --node-id=node-42
# State progression: new → discovering → probed → provisioning → ready

# Check logs
gough node provision-log --node-id=node-42 --follow
```

**Common causes:**

**Stuck in "discovering" state (>5 min):**
- Node is not PXE-booting. Check:
  - BIOS PXE settings enabled
  - Network connectivity
  - DHCP server reachable

**Stuck in "probing" state (>10 min):**
- Hardware inventory scan is slow. Normal for large disk arrays.
- Check logs: `gough node provision-log --node-id=node-42`

**Stuck in "provisioning" state (>15 min):**
- Cloud-init is executing slowly
- Biome deployment is taking time
- Check logs: `ssh <node-ip> journalctl -u cloud-init -n 100`

**Fix:**
```bash
# Restart provisioning
gough node provision-phase-2 --node-id=node-42

# Or start over
gough node reprovision --node-id=node-42
```

---

## Network connectivity issues

**"connection refused" or "connection timed out"**

```bash
gough cluster status
# Error: connection refused at gough.prod.example.com:443

# Check:
1. Cluster URL is correct
gough config get cluster.url

2. Network connectivity to cluster
ping gough.prod.example.com
curl -v https://gough.prod.example.com/healthz

3. If using internal LB (beta):
ping dal2.penguintech.cloud
# Internal LB requires Host header
curl -H "Host: gough.penguintech.cloud" https://dal2.penguintech.cloud/healthz
```

**Slow API responses (>5s latency)**

```bash
gough cluster status --verbose
# Check "Latency: XXXms" in output

# If > 5000ms:
1. Check network: ping <cluster> (should be <100ms)
2. Check cluster health: gough cluster status (might be under load)
3. Check DNS resolution: nslookup gough.prod.example.com
4. Wait and retry (might be temporary spike)
```

---

## Biome deployment issues

**"biome not found"**

```bash
gough biome list
# Verify biome exists

gough biome deploy --biome-id=nonexistent --node-id=node-1
# Error: biome not found

# Use correct biome ID from list
```

**"node not compatible with biome"**

```bash
gough biome deploy --biome-id=gpu-workload --node-id=node-1
# Error: node-1 incompatible: GPU required

# Check node specs
gough node list --node-id=node-1

# Deploy to compatible node
gough node list | grep gpu  # find GPU node
gough biome deploy --biome-id=gpu-workload --node-id=<gpu-node>
```

---

## General debugging workflow

1. **Check basics:**
   ```bash
   gough config list
   gough config current-context
   gough cluster status
   ```

2. **Enable verbose mode:**
   ```bash
   gough <command> --verbose
   ```

3. **Check cluster health:**
   ```bash
   gough cluster status
   gough audit log --limit=5
   ```

4. **Check exit codes:**
   ```bash
   gough <command>
   echo "Exit code: $?"
   ```

5. **Escalate with diagnostic output:**
   ```bash
   gough cluster status --verbose > /tmp/cluster-status.txt
   gough audit log --limit=50 --output=json > /tmp/audit.json
   gough doctor cluster > /tmp/doctor-output.txt
   
   # Share logs with ops team
   ```

---
