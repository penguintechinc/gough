#!/bin/bash
set -e

# Gough Testing Framework - Main Test Runner
# Comprehensive test execution script with reporting and CI/CD integration

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TEST_DIR="$PROJECT_ROOT/tests"
REPORTS_DIR="$TEST_DIR/reports"
VENV_DIR="$PROJECT_ROOT/venv"

# Default values
TEST_TYPE="all"
ENVIRONMENT="local"
PARALLEL_JOBS=4
COVERAGE_THRESHOLD=80
PERFORMANCE_ENABLED=false
INTEGRATION_ENABLED=false
DOCKER_ENABLED=false

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Help function
show_help() {
    cat << EOF
Gough Testing Framework - Test Runner

Usage: $0 [OPTIONS]

OPTIONS:
    -t, --test-type TYPE        Test type to run (unit|integration|performance|e2e|all) [default: all]
    -e, --environment ENV       Test environment (local|docker|ci) [default: local]
    -j, --parallel-jobs NUM     Number of parallel test jobs [default: 4]
    -c, --coverage-threshold    Minimum coverage percentage [default: 80]
    -p, --performance          Enable performance tests [default: false]
    -i, --integration          Enable integration tests [default: false]
    -d, --docker               Run tests in Docker containers [default: false]
    --markers MARKERS          Pytest markers to run (e.g., "unit and not slow")
    --reports-only             Generate reports from existing test data
    --clean                    Clean previous test artifacts
    --setup-env                Setup test environment and dependencies
    --validate-env             Validate test environment setup
    -h, --help                 Show this help message

EXAMPLES:
    $0                                          # Run all tests with default settings
    $0 -t unit -j 8                           # Run unit tests with 8 parallel jobs
    $0 -t integration -i -e docker            # Run integration tests in Docker
    $0 -t performance -p --markers "not slow"  # Run fast performance tests
    $0 --clean --setup-env                     # Clean and setup environment

ENVIRONMENT VARIABLES:
    GOUGH_TEST_DB_URL          Database URL for testing
    GOUGH_TEST_REDIS_URL       Redis URL for testing
    GOUGH_TEST_MAAS_URL        MaaS URL for testing
    GOUGH_TEST_FLEET_URL       FleetDM URL for testing
    CI                         Set to 'true' for CI/CD environments
EOF
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            -t|--test-type)
                TEST_TYPE="$2"
                shift 2
                ;;
            -e|--environment)
                ENVIRONMENT="$2"
                shift 2
                ;;
            -j|--parallel-jobs)
                PARALLEL_JOBS="$2"
                shift 2
                ;;
            -c|--coverage-threshold)
                COVERAGE_THRESHOLD="$2"
                shift 2
                ;;
            -p|--performance)
                PERFORMANCE_ENABLED=true
                shift
                ;;
            -i|--integration)
                INTEGRATION_ENABLED=true
                shift
                ;;
            -d|--docker)
                DOCKER_ENABLED=true
                shift
                ;;
            --markers)
                PYTEST_MARKERS="$2"
                shift 2
                ;;
            --reports-only)
                REPORTS_ONLY=true
                shift
                ;;
            --clean)
                CLEAN_ARTIFACTS=true
                shift
                ;;
            --setup-env)
                SETUP_ENV=true
                shift
                ;;
            --validate-env)
                VALIDATE_ENV=true
                shift
                ;;
            -h|--help)
                show_help
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                show_help
                exit 1
                ;;
        esac
    done
}

# Environment setup
setup_environment() {
    log_info "Setting up test environment..."
    
    # Create virtual environment if it doesn't exist
    if [[ ! -d "$VENV_DIR" ]]; then
        log_info "Creating Python virtual environment..."
        python3 -m venv "$VENV_DIR"
    fi
    
    # Activate virtual environment
    source "$VENV_DIR/bin/activate"
    
    # Upgrade pip
    pip install --upgrade pip
    
    # Install test dependencies
    log_info "Installing test dependencies..."
    pip install -r "$TEST_DIR/requirements.txt"
    
    # Install project dependencies
    if [[ -f "$PROJECT_ROOT/requirements.txt" ]]; then
        pip install -r "$PROJECT_ROOT/requirements.txt"
    fi
    
    # Create reports directory
    mkdir -p "$REPORTS_DIR"
    
    # Setup test databases and services if needed
    if [[ "$ENVIRONMENT" == "local" ]]; then
        setup_local_services
    elif [[ "$ENVIRONMENT" == "docker" ]]; then
        setup_docker_services
    fi
    
    log_success "Environment setup completed"
}

# Local services setup
setup_local_services() {
    log_info "Setting up local test services..."
    
    # Check for required services
    if command -v docker &> /dev/null; then
        log_info "Starting test containers..."
        
        # Start PostgreSQL test database
        docker run -d --name gough-test-db \
            -e POSTGRES_DB=gough_test \
            -e POSTGRES_USER=test_user \
            -e POSTGRES_PASSWORD=test_pass \
            -p 5433:5432 \
            postgres:13 || log_warning "Test database container already exists"
        
        # Start Redis for caching tests
        docker run -d --name gough-test-redis \
            -p 6380:6379 \
            redis:7 || log_warning "Test Redis container already exists"
        
        # Wait for services to be ready
        sleep 5
        
        # Set environment variables for tests
        export DATABASE_URL="postgresql://test_user:test_pass@localhost:5433/gough_test"
        export REDIS_URL="redis://localhost:6380/0"
    else
        log_warning "Docker not available, some integration tests may fail"
    fi
}

# Docker services setup
setup_docker_services() {
    log_info "Setting up Docker test environment..."
    
    # Use docker-compose for test services
    if [[ -f "$PROJECT_ROOT/docker-compose.test.yml" ]]; then
        docker-compose -f "$PROJECT_ROOT/docker-compose.test.yml" up -d
        sleep 10  # Wait for services to be ready
    else
        log_warning "docker-compose.test.yml not found, creating minimal setup..."
        setup_local_services
    fi
}

# Environment validation
validate_environment() {
    log_info "Validating test environment..."
    
    local validation_failed=false
    
    # Check Python version
    python_version=$(python3 --version | cut -d' ' -f2)
    if [[ ! "$python_version" =~ ^3\.[8-9]|^3\.1[0-9] ]]; then
        log_error "Python 3.8+ required, found: $python_version"
        validation_failed=true
    fi
    
    # Check pytest installation
    if ! pip show pytest &> /dev/null; then
        log_error "pytest not installed"
        validation_failed=true
    fi
    
    # Check database connectivity
    if [[ -n "$DATABASE_URL" ]]; then
        log_info "Testing database connectivity..."
        python3 -c "
import psycopg2
import os
try:
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    conn.close()
    print('Database connection successful')
except Exception as e:
    print(f'Database connection failed: {e}')
    exit(1)
" || validation_failed=true
    fi
    
    # Check Redis connectivity
    if [[ -n "$REDIS_URL" ]]; then
        log_info "Testing Redis connectivity..."
        python3 -c "
import redis
import os
try:
    r = redis.from_url(os.environ['REDIS_URL'])
    r.ping()
    print('Redis connection successful')
except Exception as e:
    print(f'Redis connection failed: {e}')
    exit(1)
" || validation_failed=true
    fi
    
    if [[ "$validation_failed" == true ]]; then
        log_error "Environment validation failed"
        exit 1
    else
        log_success "Environment validation passed"
    fi
}

# Clean artifacts
clean_artifacts() {
    log_info "Cleaning test artifacts..."
    
    # Remove previous reports
    rm -rf "$REPORTS_DIR"/*
    
    # Remove Python cache
    find "$PROJECT_ROOT" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "$PROJECT_ROOT" -name "*.pyc" -delete 2>/dev/null || true
    
    # Remove coverage files
    rm -f "$PROJECT_ROOT/.coverage"
    rm -rf "$PROJECT_ROOT/.coverage.*"
    
    # Remove pytest cache
    rm -rf "$PROJECT_ROOT/.pytest_cache"
    
    log_success "Artifacts cleaned"
}

# Run tests based on type
run_tests() {
    log_info "Running $TEST_TYPE tests..."
    
    # Activate virtual environment
    source "$VENV_DIR/bin/activate"
    
    # Base pytest command
    local pytest_cmd="python -m pytest"
    local pytest_args=""
    
    # Add parallel execution
    if [[ "$PARALLEL_JOBS" -gt 1 ]]; then
        pytest_args="$pytest_args -n $PARALLEL_JOBS"
    fi
    
    # Add coverage
    pytest_args="$pytest_args --cov=gough"
    pytest_args="$pytest_args --cov-fail-under=$COVERAGE_THRESHOLD"
    
    # Add test markers based on type
    case $TEST_TYPE in
        unit)
            pytest_args="$pytest_args -m 'unit'"
            pytest_args="$pytest_args tests/unit/"
            ;;
        integration)
            pytest_args="$pytest_args -m 'integration'"
            pytest_args="$pytest_args tests/integration/"
            ;;
        performance)
            pytest_args="$pytest_args -m 'performance'"
            pytest_args="$pytest_args tests/performance/"
            ;;
        e2e)
            pytest_args="$pytest_args -m 'e2e'"
            pytest_args="$pytest_args tests/integration/test_end_to_end_provisioning.py"
            ;;
        all)
            if [[ "$PERFORMANCE_ENABLED" == false ]]; then
                pytest_args="$pytest_args -m 'not performance'"
            fi
            if [[ "$INTEGRATION_ENABLED" == false ]]; then
                pytest_args="$pytest_args -m 'not integration'"
            fi
            pytest_args="$pytest_args tests/"
            ;;
    esac
    
    # Add custom markers
    if [[ -n "$PYTEST_MARKERS" ]]; then
        pytest_args="$pytest_args -m '$PYTEST_MARKERS'"
    fi
    
    # Environment-specific settings
    if [[ "$ENVIRONMENT" == "ci" ]] || [[ "$CI" == "true" ]]; then
        pytest_args="$pytest_args --tb=short"
        pytest_args="$pytest_args --maxfail=10"
    fi
    
    # Execute tests
    log_info "Executing: $pytest_cmd $pytest_args"
    
    if [[ "$DOCKER_ENABLED" == true ]]; then
        run_tests_in_docker "$pytest_cmd $pytest_args"
    else
        $pytest_cmd $pytest_args
    fi
    
    local test_exit_code=$?
    
    if [[ $test_exit_code -eq 0 ]]; then
        log_success "Tests completed successfully"
    else
        log_error "Tests failed with exit code: $test_exit_code"
        return $test_exit_code
    fi
}

# Run tests in Docker
run_tests_in_docker() {
    local pytest_cmd="$1"
    
    log_info "Running tests in Docker container..."
    
    docker run --rm \
        -v "$PROJECT_ROOT:/app" \
        -w /app \
        -e DATABASE_URL="${DATABASE_URL:-postgresql://test:test@localhost:5432/gough_test}" \
        -e REDIS_URL="${REDIS_URL:-redis://localhost:6379/1}" \
        -e TESTING=1 \
        --network host \
        python:3.9-slim \
        bash -c "
            apt-get update && apt-get install -y gcc libpq-dev
            pip install -r tests/requirements.txt
            $pytest_cmd
        "
}

# Generate reports
generate_reports() {
    log_info "Generating test reports..."
    
    # Ensure reports directory exists
    mkdir -p "$REPORTS_DIR"
    
    # Generate coverage badge
    if command -v coverage-badge &> /dev/null; then
        coverage-badge -o "$REPORTS_DIR/coverage-badge.svg"
    fi
    
    # Generate JUnit XML for CI/CD
    if [[ -f "$REPORTS_DIR/pytest_report.json" ]]; then
        python3 -c "
import json
import xml.etree.ElementTree as ET
from datetime import datetime

with open('$REPORTS_DIR/pytest_report.json', 'r') as f:
    data = json.load(f)

# Create JUnit XML format
root = ET.Element('testsuites')
root.set('name', 'Gough Tests')
root.set('tests', str(data['summary']['total']))
root.set('failures', str(data['summary'].get('failed', 0)))
root.set('errors', str(data['summary'].get('error', 0)))
root.set('time', str(data['summary'].get('duration', 0)))
root.set('timestamp', datetime.now().isoformat())

testsuite = ET.SubElement(root, 'testsuite')
testsuite.set('name', 'All Tests')

for test in data['tests']:
    testcase = ET.SubElement(testsuite, 'testcase')
    testcase.set('name', test['nodeid'])
    testcase.set('classname', test.get('filename', ''))
    testcase.set('time', str(test.get('duration', 0)))
    
    if test['outcome'] == 'failed':
        failure = ET.SubElement(testcase, 'failure')
        failure.set('message', test.get('call', {}).get('longrepr', 'Test failed'))

tree = ET.ElementTree(root)
tree.write('$REPORTS_DIR/junit.xml', encoding='utf-8', xml_declaration=True)
print('JUnit XML report generated')
"
    fi
    
    # Generate summary report
    cat > "$REPORTS_DIR/test_summary.md" << EOF
# Gough Test Summary

**Generated:** $(date)
**Environment:** $ENVIRONMENT
**Test Type:** $TEST_TYPE

## Test Results

$(if [[ -f "$REPORTS_DIR/pytest_report.json" ]]; then
    python3 -c "
import json
with open('$REPORTS_DIR/pytest_report.json', 'r') as f:
    data = json.load(f)
summary = data['summary']
print(f\"- **Total Tests:** {summary['total']}\")
print(f\"- **Passed:** {summary.get('passed', 0)}\")
print(f\"- **Failed:** {summary.get('failed', 0)}\")
print(f\"- **Skipped:** {summary.get('skipped', 0)}\")
print(f\"- **Duration:** {summary.get('duration', 0):.2f}s\")
"
else
    echo "Test results not available"
fi)

## Coverage Report

$(if [[ -f "$REPORTS_DIR/coverage.json" ]]; then
    python3 -c "
import json
with open('$REPORTS_DIR/coverage.json', 'r') as f:
    data = json.load(f)
totals = data['totals']
print(f\"- **Coverage:** {totals['percent_covered']:.1f}%\")
print(f\"- **Lines Covered:** {totals['covered_lines']}/{totals['num_statements']}\")
print(f\"- **Missing Lines:** {totals['missing_lines']}\")
"
else
    echo "Coverage report not available"
fi)

## Links

- [HTML Coverage Report](coverage_html/index.html)
- [Detailed Test Report](pytest_report.html)
EOF
    
    log_success "Reports generated in $REPORTS_DIR"
}

# Main execution
main() {
    log_info "Starting Gough Testing Framework"
    log_info "Project: $PROJECT_ROOT"
    log_info "Test Directory: $TEST_DIR"
    
    # Parse arguments
    parse_args "$@"
    
    # Handle special operations
    if [[ "$CLEAN_ARTIFACTS" == true ]]; then
        clean_artifacts
    fi
    
    if [[ "$SETUP_ENV" == true ]]; then
        setup_environment
    fi
    
    if [[ "$VALIDATE_ENV" == true ]]; then
        validate_environment
    fi
    
    # Skip tests if only generating reports
    if [[ "$REPORTS_ONLY" == true ]]; then
        generate_reports
        exit 0
    fi
    
    # Setup environment if not exists
    if [[ ! -d "$VENV_DIR" ]]; then
        setup_environment
    fi
    
    # Validate environment
    validate_environment
    
    # Run tests
    local start_time=$(date +%s)
    
    if run_tests; then
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        
        log_success "All tests completed successfully in ${duration}s"
        
        # Generate reports
        generate_reports
        
        exit 0
    else
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        
        log_error "Tests failed after ${duration}s"
        
        # Still generate reports for failure analysis
        generate_reports
        
        exit 1
    fi
}

# Execute main function
main "$@"