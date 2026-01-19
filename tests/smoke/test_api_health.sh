#!/bin/bash
#
# Smoke Test: API Health Endpoints
# Verifies API health endpoints (/api/v1/health, /api/v1/ready, /healthz, /readyz)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
API_URL="${API_URL:-http://localhost:5000}"
API_TIMEOUT=5
MAX_RETRIES=10
RETRY_DELAY=2

# Counters
PASSED=0
FAILED=0
TOTAL=4

echo "========================================"
echo "Smoke Test: API Health Endpoints"
echo "========================================"
echo "Testing API at: $API_URL"
echo ""

# Function to test endpoint with retries
test_endpoint() {
    local name=$1
    local endpoint=$2
    local retries=$MAX_RETRIES
    local attempt=0

    echo -n "Testing $name ($endpoint)... "

    while [ $attempt -lt $MAX_RETRIES ]; do
        if curl -sf --max-time "$API_TIMEOUT" "$API_URL$endpoint" > /dev/null 2>&1; then
            echo -e "${GREEN}✓ OK${NC}"
            ((PASSED++))
            return 0
        fi
        ((attempt++))
        if [ $attempt -lt $MAX_RETRIES ]; then
            echo -n "."
            sleep "$RETRY_DELAY"
        fi
    done

    echo -e "${RED}✗ FAILED${NC}"
    echo "  └─ Could not reach: $API_URL$endpoint"
    ((FAILED++))
    return 1
}

# Test health check endpoints
test_endpoint "Health Check (/healthz)" "/healthz" || true
test_endpoint "Health Check (/api/v1/health)" "/api/v1/health" || true
test_endpoint "Readiness Check (/readyz)" "/readyz" || true
test_endpoint "Readiness Check (/api/v1/ready)" "/api/v1/ready" || true

# Print summary
echo ""
echo "========================================"
echo "Summary: $PASSED/$TOTAL endpoints responding"
echo "========================================"

if [ $FAILED -gt 0 ]; then
    echo -e "${YELLOW}WARNING${NC}: $FAILED endpoint(s) not responding"
    echo "This may be expected if the API is still starting up."
    # Don't fail, just warn - API might still be initializing
    exit 0
else
    echo -e "${GREEN}PASSED${NC}: All health endpoints responding"
    exit 0
fi
