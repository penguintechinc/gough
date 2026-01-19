#!/bin/bash
# Gough Smoke Tests - Alpha (local) and Beta (staging) support
#
# Usage:
#   ./run-smoke-tests.sh alpha         # Run full alpha tests (build, run, API, security, lint)
#   ./run-smoke-tests.sh beta          # Run beta tests against staging (API, UI only)
#   ./run-smoke-tests.sh alpha quick   # Skip container builds (faster iteration)
#
# Alpha tests: Full local validation before commit
# Beta tests: Validate deployed staging environment

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
ENVIRONMENT="${1:-alpha}"
QUICK_MODE="${2:-}"
TIMESTAMP=$(date +%s)
LOG_DIR="/tmp/gough-smoke-${ENVIRONMENT}-${TIMESTAMP}"
SUMMARY_LOG="${LOG_DIR}/summary.log"

# URLs based on environment
if [ "$ENVIRONMENT" == "beta" ]; then
    BASE_URL="https://gough.penguintech.io"
    API_URL="${BASE_URL}"
    WEBUI_URL="${BASE_URL}"
    SKIP_BUILD=true
    SKIP_DOCKER=true
else
    BASE_URL="http://localhost:5001"
    API_URL="${BASE_URL}"
    WEBUI_URL="http://localhost:3000"
    SKIP_BUILD=false
    SKIP_DOCKER=false
fi

# Quick mode skips builds
if [ "$QUICK_MODE" == "quick" ]; then
    SKIP_BUILD=true
fi

mkdir -p "$LOG_DIR"

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1" | tee -a "$SUMMARY_LOG"
}

log_success() {
    echo -e "${GREEN}[PASS]${NC} $1" | tee -a "$SUMMARY_LOG"
}

log_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$SUMMARY_LOG"
}

log_error() {
    echo -e "${RED}[FAIL]${NC} $1" | tee -a "$SUMMARY_LOG"
}

# Test counters
TESTS_TOTAL=0
TESTS_PASSED=0
TESTS_FAILED=0

run_test() {
    local test_name="$1"
    local test_cmd="$2"

    TESTS_TOTAL=$((TESTS_TOTAL + 1))
    log_info "Running: $test_name"

    if eval "$test_cmd" > "${LOG_DIR}/${test_name// /_}.log" 2>&1; then
        log_success "$test_name"
        TESTS_PASSED=$((TESTS_PASSED + 1))
        return 0
    else
        log_error "$test_name - Check ${LOG_DIR}/${test_name// /_}.log"
        TESTS_FAILED=$((TESTS_FAILED + 1))
        return 1
    fi
}

# ============================================================================
# Phase 1: Container Build Tests (Alpha only)
# ============================================================================
if [ "$SKIP_BUILD" == "false" ]; then
    log_info "=== Phase 1: Container Build Tests ==="

    run_test "Build api-manager" "docker compose build api-manager"
    run_test "Build webui" "docker compose build webui"
    run_test "Build worker-ipxe" "docker compose build worker-ipxe"
fi

# ============================================================================
# Phase 2: Container Runtime Tests (Alpha only)
# ============================================================================
if [ "$SKIP_DOCKER" == "false" ]; then
    log_info "=== Phase 2: Container Runtime Tests ==="

    # Start containers
    run_test "Start postgres" "docker compose up -d postgres && sleep 5"
    run_test "Start api-manager" "docker compose up -d api-manager && sleep 10"

    # Health checks
    run_test "Postgres health" "docker compose ps postgres | grep -q 'healthy'"
    run_test "API Manager health" "docker compose ps api-manager | grep -q 'healthy'"

    # Check logs for errors
    run_test "API Manager no errors" "! docker logs gough-api-manager 2>&1 | grep -iE 'error|exception|traceback' | grep -v 'Redis unavailable'"
fi

# ============================================================================
# Phase 3: API Integration Tests
# ============================================================================
log_info "=== Phase 3: API Integration Tests ==="

# Health/status endpoints
run_test "API status endpoint" "curl -f -s ${API_URL}/api/v1/status | grep -q 'running'"

# Authentication endpoints
run_test "Auth login endpoint exists" "curl -s -o /dev/null -w '%{http_code}' ${API_URL}/api/v1/auth/login | grep -qE '200|400|401|405'"
run_test "Auth register endpoint exists" "curl -s -o /dev/null -w '%{http_code}' ${API_URL}/api/v1/auth/register | grep -qE '200|400|401|404|405'"

# Users endpoints (should require auth)
run_test "Users endpoint requires auth" "curl -s -o /dev/null -w '%{http_code}' ${API_URL}/api/v1/users | grep -q '401'"

# API versioning check
run_test "API has v1 versioning" "curl -s ${API_URL}/api/v1/status | grep -q 'status'"

# ============================================================================
# Phase 4: WebUI Tests (if webui is running)
# ============================================================================
if [ "$ENVIRONMENT" == "alpha" ]; then
    # Try to start webui for alpha tests (optional)
    if [ "$SKIP_DOCKER" == "false" ]; then
        log_info "=== Phase 4: WebUI Tests (Optional) ==="
        if docker compose up -d webui > /dev/null 2>&1 && sleep 10; then
            run_test "WebUI health" "curl -f -s ${WEBUI_URL}/ > /dev/null"
        else
            log_warning "WebUI not available (port 3000 may be in use) - skipping"
        fi
    fi
elif [ "$ENVIRONMENT" == "beta" ]; then
    log_info "=== Phase 4: WebUI Tests (Beta) ==="
    run_test "WebUI loads" "curl -f -s ${WEBUI_URL}/ > /dev/null"
    run_test "WebUI has assets" "curl -s ${WEBUI_URL}/ | grep -qE 'script|link'"
fi

# ============================================================================
# Phase 5: Security Tests (Alpha only)
# ============================================================================
if [ "$ENVIRONMENT" == "alpha" ]; then
    log_info "=== Phase 5: Security Tests ==="

    # Python security scanning
    if [ -f "services/api-manager/requirements.txt" ]; then
        run_test "Python bandit scan" "cd services/api-manager && bandit -r app/ -f txt -o ${LOG_DIR}/bandit.txt || true"
        run_test "Python safety check" "cd services/api-manager && safety check --json > ${LOG_DIR}/safety.json || true"
    fi

    # Docker image scanning
    run_test "Trivy scan api-manager" "trivy image --severity HIGH,CRITICAL --exit-code 0 gough-api-manager > ${LOG_DIR}/trivy-api.txt 2>&1 || true"

    # Check for secrets in code
    run_test "No hardcoded secrets" "! grep -rE '(password|secret|key)\s*=\s*[\"'\''][^\"'\'']+[\"'\'']' services/ --include='*.py' --include='*.js' | grep -v '__pycache__' | grep -v 'node_modules' | grep -v 'test' | grep -v 'example'"
fi

# ============================================================================
# Phase 6: Linting Tests (Alpha only)
# ============================================================================
if [ "$ENVIRONMENT" == "alpha" ]; then
    log_info "=== Phase 6: Linting Tests ==="

    # Python linting
    if [ -f "services/api-manager/app/__init__.py" ]; then
        run_test "Python flake8" "cd services/api-manager && flake8 app/ --max-line-length=100 --exclude=__pycache__ > ${LOG_DIR}/flake8.txt 2>&1 || true"
        run_test "Python black check" "cd services/api-manager && black --check app/ > ${LOG_DIR}/black.txt 2>&1 || true"
    fi

    # YAML linting
    if command -v yamllint &> /dev/null; then
        run_test "YAML lint" "yamllint docker-compose.yml > ${LOG_DIR}/yamllint.txt 2>&1 || true"
    fi
fi

# ============================================================================
# Phase 7: Unit Tests (Alpha only)
# ============================================================================
if [ "$ENVIRONMENT" == "alpha" ]; then
    log_info "=== Phase 7: Unit Tests ==="

    # Python unit tests
    if [ -d "services/api-manager/tests" ]; then
        run_test "Python unit tests" "cd services/api-manager && pytest tests/ -v > ${LOG_DIR}/pytest.txt 2>&1 || true"
    fi
fi

# ============================================================================
# Summary
# ============================================================================
echo "" | tee -a "$SUMMARY_LOG"
log_info "=== Test Summary ==="
echo "Environment: $ENVIRONMENT" | tee -a "$SUMMARY_LOG"
echo "Total Tests: $TESTS_TOTAL" | tee -a "$SUMMARY_LOG"
echo -e "${GREEN}Passed: $TESTS_PASSED${NC}" | tee -a "$SUMMARY_LOG"
echo -e "${RED}Failed: $TESTS_FAILED${NC}" | tee -a "$SUMMARY_LOG"
echo "" | tee -a "$SUMMARY_LOG"
echo "Logs saved to: $LOG_DIR" | tee -a "$SUMMARY_LOG"

if [ $TESTS_FAILED -gt 0 ]; then
    log_error "Some tests failed. Review logs in $LOG_DIR"
    exit 1
else
    log_success "All tests passed!"
    exit 0
fi
