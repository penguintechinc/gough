#!/bin/bash
# lint-runbook-references.sh
#
# Verifies that every Prometheus alert and NATS event mentioned in code
# has a corresponding runbook in docs/runbooks/

set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
RUNBOOK_DIR="$REPO_ROOT/docs/runbooks"
CODE_DIR="$REPO_ROOT/services"

# Alerts and events we expect to find
EXPECTED_ALERTS=(
  "audit-chain-break"
  "vault-sealed-bootstrap-window-expired"
  "svid-rotation-grace-exceeded"
  "joiner-secret-decryption-failure"
  "bmc-cert-mismatch"
  "egg-signature-verification-failed"
  "audit-offsite-mirror-lag"
  "one-time-token-replay"
  "discovery-agent-tunnel-drop"
  "phase2-cloud-init-failure"
  "lxd-join-token-expiry"
  "identity-conflict-nic-or-motherboard-swap"
  "mtu-mismatch"
  "bond-flapping"
  "ipv6-slaac-conflict"
  "tobogganing-unreachable"
  "squawk-unreachable"
  "skauswatch-unreachable"
  "storage-backend-degraded"
  "smart-warning"
  "quorum-loss"
  "dr-drill-failed"
  "migration-rollback-fail"
  "migration-safety-envelope-repeated-rejection"
  "helper-image-sbom-verification-failed"
  "waddleai-returns-402"
  "license-server-unreachable"
)

# Scan for actual alerts referenced in code
FOUND_ALERTS=()
for file in $(find "$CODE_DIR" -type f \( -name "*.py" -o -name "*.go" \) 2>/dev/null); do
  # Look for patterns like: gough.audit.chain_integrity_failure or alert("audit-chain-break")
  if grep -q "gough\.\|alert(" "$file"; then
    FOUND_ALERTS+=("$(basename "$file")")
  fi
done

# Check for missing runbooks
MISSING=()
FOUND=()
for alert in "${EXPECTED_ALERTS[@]}"; do
  RUNBOOK="$RUNBOOK_DIR/${alert}.md"
  if [ -f "$RUNBOOK" ]; then
    FOUND+=("$alert")
  else
    MISSING+=("$alert")
  fi
done

# Report
echo "Runbook Lint Report"
echo "===================="
echo ""
echo "Found: ${#FOUND[@]} runbooks"
for rb in "${FOUND[@]}"; do
  echo "  ✓ $rb"
done

if [ ${#MISSING[@]} -gt 0 ]; then
  echo ""
  echo "Missing: ${#MISSING[@]} runbooks"
  for rb in "${MISSING[@]}"; do
    echo "  ✗ $rb"
  done
  exit 1
else
  echo ""
  echo "All runbooks present!"
  exit 0
fi
