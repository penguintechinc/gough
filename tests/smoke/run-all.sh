#!/bin/bash
#
# Master Smoke Test Runner
# Runs all smoke tests and reports combined results
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
TIMEOUT=120 # Total timeout for all tests
VERBOSE="${VERBOSE:-false}"

# Counters
TESTS_PASSED=0
TESTS_FAILED=0
TOTAL_TESTS=4

# Test files
TESTS=(
    "test_containers.sh"
    "test_api_health.sh"
    "test_ipxe_services.sh"
    "test_webui.sh"
)

# Function to run a single test
run_test() {
    local test_file=$1
    local test_name=${test_file%.sh}
    local test_path="$SCRIPT_DIR/$test_file"

    if [ ! -f "$test_path" ]; then
        echo -e "${RED}✗ Test file not found: $test_path${NC}"
        return 1
    fi

    if [ ! -x "$test_path" ]; then
        chmod +x "$test_path"
    fi

    echo ""
    echo -e "${BLUE}════════════════════════════════════════${NC}"
    echo -e "${BLUE}Running: $test_name${NC}"
    echo -e "${BLUE}════════════════════════════════════════${NC}"

    # Run test and capture result
    local output
    local exit_code=0

    if output=$("$test_path" 2>&1); then
        echo "$output"
        echo -e "${GREEN}✓ PASSED${NC}: $test_name"
        return 0
    else
        exit_code=$?
        echo "$output"
        echo -e "${RED}✗ FAILED${NC}: $test_name (exit code: $exit_code)"
        return 1
    fi
}

# Main execution
main() {
    local start_time=$(date +%s)

    echo -e "${BLUE}"
    echo "╔════════════════════════════════════════╗"
    echo "║   Gough Smoke Test Suite               ║"
    echo "║   $(date '+%Y-%m-%d %H:%M:%S')        ║"
    echo "╚════════════════════════════════════════╝"
    echo -e "${NC}"

    # Check if Docker is running
    if ! docker ps > /dev/null 2>&1; then
        echo -e "${RED}ERROR: Docker is not running${NC}"
        echo "Please start Docker and try again."
        exit 1
    fi

    # Check if docker-compose is available
    if ! command -v docker-compose &> /dev/null && ! command -v docker compose &> /dev/null; then
        echo -e "${RED}ERROR: docker-compose is not available${NC}"
        exit 1
    fi

    # Check if containers are running
    echo -e "${YELLOW}Checking container status...${NC}"
    local running_containers=$(docker ps --format "{{.Names}}" | wc -l)
    echo "Found $running_containers running containers"

    if [ $running_containers -eq 0 ]; then
        echo -e "${YELLOW}WARNING: No containers appear to be running${NC}"
        echo "You may need to start containers with: docker-compose up -d"
        echo "Continuing with tests anyway..."
    fi

    # Run each test
    for test_file in "${TESTS[@]}"; do
        if run_test "$test_file"; then
            ((TESTS_PASSED++))
        else
            ((TESTS_FAILED++))
        fi
    done

    # Print summary
    local end_time=$(date +%s)
    local duration=$((end_time - start_time))

    echo ""
    echo -e "${BLUE}════════════════════════════════════════${NC}"
    echo -e "${BLUE}Test Summary${NC}"
    echo -e "${BLUE}════════════════════════════════════════${NC}"
    echo "Total Tests: $TOTAL_TESTS"
    echo -e "Passed: ${GREEN}$TESTS_PASSED${NC}"
    echo -e "Failed: ${RED}$TESTS_FAILED${NC}"
    echo "Duration: ${duration}s"
    echo ""

    # Final result
    if [ $TESTS_FAILED -eq 0 ]; then
        echo -e "${GREEN}════════════════════════════════════════${NC}"
        echo -e "${GREEN}✓ ALL SMOKE TESTS PASSED${NC}"
        echo -e "${GREEN}════════════════════════════════════════${NC}"
        echo ""
        echo "All critical services are functional."
        echo "Ready for development or deployment."
        return 0
    else
        echo -e "${RED}════════════════════════════════════════${NC}"
        echo -e "${RED}✗ SOME TESTS FAILED${NC}"
        echo -e "${RED}════════════════════════════════════════${NC}"
        echo ""
        echo "Check the output above for details."
        echo "Services may still be initializing - retry in a moment."
        return 1
    fi
}

# Print usage
usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Runs all Gough smoke tests to verify basic functionality.

OPTIONS:
    -h, --help          Show this help message
    -v, --verbose       Enable verbose output
    -t, --test TEST     Run only specific test (container, api, ipxe, webui)

EXAMPLES:
    # Run all tests
    $0

    # Run only container health test
    $0 --test container

    # Enable verbose output
    $0 --verbose

EOF
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            usage
            exit 0
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        -t|--test)
            if [ -z "$2" ]; then
                echo "Error: --test requires a value"
                usage
                exit 1
            fi
            case "$2" in
                container)
                    TESTS=("test_containers.sh")
                    ;;
                api)
                    TESTS=("test_api_health.sh")
                    ;;
                ipxe)
                    TESTS=("test_ipxe_services.sh")
                    ;;
                webui)
                    TESTS=("test_webui.sh")
                    ;;
                *)
                    echo "Error: Unknown test '$2'"
                    echo "Valid options: container, api, ipxe, webui"
                    exit 1
                    ;;
            esac
            TOTAL_TESTS=${#TESTS[@]}
            shift 2
            ;;
        *)
            echo "Error: Unknown option '$1'"
            usage
            exit 1
            ;;
    esac
done

# Run main function
if main; then
    exit 0
else
    exit 1
fi
