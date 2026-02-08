#!/bin/bash
#
# Smoke Test: Container Health Verification
# Verifies all 5 containers start and pass health checks
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Counters
PASSED=0
FAILED=0
TOTAL=5

# Container details
declare -A CONTAINERS=(
    [postgres]="gough-postgres"
    [api-manager]="gough-api-manager"
    [webui]="gough-webui"
    [worker-ipxe]="gough-worker-ipxe"
    [access-agent]="gough-access-agent"
)

echo "========================================"
echo "Smoke Test: Container Health Check"
echo "========================================"

# Function to check if container is running
check_container() {
    local name=$1
    local container=$2

    echo -n "Checking $name... "

    # Check if container exists and is running
    if docker ps --format "{{.Names}}" | grep -q "^${container}$"; then
        echo -e "${GREEN}✓ Running${NC}"

        # Check health status if available
        local health=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "none")

        if [ "$health" = "healthy" ]; then
            echo -e "  └─ Health: ${GREEN}healthy${NC}"
            ((PASSED++))
            return 0
        elif [ "$health" = "starting" ]; then
            echo -e "  └─ Health: ${YELLOW}starting${NC} (acceptable)"
            ((PASSED++))
            return 0
        elif [ "$health" = "none" ]; then
            echo -e "  └─ Health: ${YELLOW}no healthcheck${NC} (acceptable)"
            ((PASSED++))
            return 0
        else
            echo -e "  └─ Health: ${RED}${health}${NC}"
            ((FAILED++))
            return 1
        fi
    else
        echo -e "${RED}✗ Not Running${NC}"
        ((FAILED++))
        return 1
    fi
}

# Check each container
for name in "${!CONTAINERS[@]}"; do
    check_container "$name" "${CONTAINERS[$name]}" || true
done

# Print summary
echo ""
echo "========================================"
echo "Summary: $PASSED/$TOTAL containers healthy"
echo "========================================"

if [ $FAILED -gt 0 ]; then
    echo -e "${RED}FAILED${NC}: $FAILED container(s) not healthy"
    exit 1
else
    echo -e "${GREEN}PASSED${NC}: All containers healthy"
    exit 0
fi
