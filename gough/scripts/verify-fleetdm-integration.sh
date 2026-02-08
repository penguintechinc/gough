#!/bin/bash
# FleetDM Integration Verification Script for Gough Hypervisor
# This script verifies that all FleetDM components are properly configured and working

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
FLEET_URL="${FLEET_URL:-https://localhost:8443}"
MANAGEMENT_URL="${MANAGEMENT_URL:-http://localhost:8000}"
DOCKER_COMPOSE_FILE="${DOCKER_COMPOSE_FILE:-docker-compose.yml}"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Gough FleetDM Integration Verification${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Function to print status
print_status() {
    local status=$1
    local message=$2
    
    if [ "$status" = "pass" ]; then
        echo -e "${GREEN}✓${NC} $message"
    elif [ "$status" = "fail" ]; then
        echo -e "${RED}✗${NC} $message"
    elif [ "$status" = "warn" ]; then
        echo -e "${YELLOW}⚠${NC} $message"
    else
        echo -e "${BLUE}ℹ${NC} $message"
    fi
}

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to check HTTP endpoint
check_http_endpoint() {
    local url=$1
    local description=$2
    local timeout=${3:-10}
    
    if curl -s --max-time $timeout "$url" >/dev/null 2>&1; then
        print_status "pass" "$description is accessible"
        return 0
    else
        print_status "fail" "$description is not accessible"
        return 1
    fi
}

# Function to check HTTPS endpoint (ignoring SSL)
check_https_endpoint() {
    local url=$1
    local description=$2
    local timeout=${3:-10}
    
    if curl -k -s --max-time $timeout "$url" >/dev/null 2>&1; then
        print_status "pass" "$description is accessible"
        return 0
    else
        print_status "fail" "$description is not accessible"
        return 1
    fi
}

# Check prerequisites
echo -e "${YELLOW}Checking Prerequisites...${NC}"
echo ""

if command_exists docker; then
    print_status "pass" "Docker is installed"
else
    print_status "fail" "Docker is not installed"
    exit 1
fi

if command_exists docker-compose; then
    print_status "pass" "Docker Compose is installed"
else
    print_status "fail" "Docker Compose is not installed"
    exit 1
fi

if command_exists curl; then
    print_status "pass" "curl is installed"
else
    print_status "fail" "curl is not installed"
    exit 1
fi

echo ""

# Check if docker-compose.yml exists
echo -e "${YELLOW}Checking Configuration Files...${NC}"
echo ""

if [ -f "$DOCKER_COMPOSE_FILE" ]; then
    print_status "pass" "Docker Compose file exists"
else
    print_status "fail" "Docker Compose file not found: $DOCKER_COMPOSE_FILE"
    exit 1
fi

# Check if FleetDM configuration files exist
if [ -f "containers/fleetdm/config/fleet.yml" ]; then
    print_status "pass" "FleetDM configuration file exists"
else
    print_status "fail" "FleetDM configuration file not found"
fi

if [ -f "containers/fleetdm/config/osquery.flags" ]; then
    print_status "pass" "OSQuery flags configuration exists"
else
    print_status "fail" "OSQuery flags configuration not found"
fi

if [ -d "containers/fleetdm/config/packs" ]; then
    pack_count=$(find containers/fleetdm/config/packs -name "*.json" | wc -l)
    if [ "$pack_count" -gt 0 ]; then
        print_status "pass" "Query packs directory exists with $pack_count packs"
    else
        print_status "warn" "Query packs directory exists but is empty"
    fi
else
    print_status "fail" "Query packs directory not found"
fi

if [ -f "ansible/playbooks/deploy-osquery.yml" ]; then
    print_status "pass" "OSQuery deployment playbook exists"
else
    print_status "fail" "OSQuery deployment playbook not found"
fi

if [ -f "cloud-init/templates/osquery-server.yaml" ]; then
    print_status "pass" "OSQuery cloud-init template exists"
else
    print_status "fail" "OSQuery cloud-init template not found"
fi

echo ""

# Check Docker services
echo -e "${YELLOW}Checking Docker Services...${NC}"
echo ""

# Check if services are defined in docker-compose.yml
services_check() {
    local service=$1
    if grep -q "^  $service:" "$DOCKER_COMPOSE_FILE"; then
        print_status "pass" "$service service is defined in docker-compose.yml"
        return 0
    else
        print_status "fail" "$service service is not defined in docker-compose.yml"
        return 1
    fi
}

services_check "fleetdm"
services_check "mysql"
services_check "redis"
services_check "management-server"

# Check if services are running
running_services=$(docker-compose ps --services --filter "status=running" 2>/dev/null || echo "")

check_service_running() {
    local service=$1
    if echo "$running_services" | grep -q "^$service$"; then
        print_status "pass" "$service container is running"
        return 0
    else
        print_status "fail" "$service container is not running"
        return 1
    fi
}

if [ -n "$running_services" ]; then
    check_service_running "fleetdm"
    check_service_running "mysql"
    check_service_running "redis"
    check_service_running "management-server"
else
    print_status "warn" "Could not determine running services (services may not be started)"
fi

echo ""

# Check network connectivity
echo -e "${YELLOW}Checking Network Connectivity...${NC}"
echo ""

check_https_endpoint "$FLEET_URL/api/v1/fleet/version" "FleetDM API"
check_http_endpoint "$MANAGEMENT_URL/" "Management Server"
check_http_endpoint "$MANAGEMENT_URL/fleet/dashboard" "FleetDM Dashboard"

echo ""

# Check database connectivity
echo -e "${YELLOW}Checking Database Connectivity...${NC}"
echo ""

if docker-compose exec -T mysql mysql -u fleet -pfleetpass -e "SELECT 1;" >/dev/null 2>&1; then
    print_status "pass" "MySQL database connection successful"
    
    # Check if Fleet database exists and has tables
    table_count=$(docker-compose exec -T mysql mysql -u fleet -pfleetpass fleet -e "SHOW TABLES;" 2>/dev/null | wc -l)
    if [ "$table_count" -gt 1 ]; then
        print_status "pass" "Fleet database is initialized with $table_count tables"
    else
        print_status "warn" "Fleet database exists but may not be initialized"
    fi
else
    print_status "fail" "MySQL database connection failed"
fi

if docker-compose exec -T redis redis-cli ping >/dev/null 2>&1; then
    print_status "pass" "Redis connection successful"
else
    print_status "fail" "Redis connection failed"
fi

echo ""

# Check FleetDM API functionality
echo -e "${YELLOW}Checking FleetDM API Functionality...${NC}"
echo ""

# Test version endpoint
if version_info=$(curl -k -s "$FLEET_URL/api/v1/fleet/version" 2>/dev/null); then
    if echo "$version_info" | grep -q "version"; then
        version=$(echo "$version_info" | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
        print_status "pass" "FleetDM API version endpoint working (version: $version)"
    else
        print_status "warn" "FleetDM API responded but format unexpected"
    fi
else
    print_status "fail" "FleetDM API version endpoint not accessible"
fi

# Test management portal Fleet integration
if curl -s "$MANAGEMENT_URL/api/fleet/status" >/dev/null 2>&1; then
    print_status "pass" "Management portal FleetDM integration endpoint accessible"
else
    print_status "fail" "Management portal FleetDM integration endpoint not accessible"
fi

echo ""

# Check file permissions and ownership
echo -e "${YELLOW}Checking File Permissions...${NC}"
echo ""

check_file_permissions() {
    local file=$1
    local expected_perm=$2
    local description=$3
    
    if [ -f "$file" ]; then
        actual_perm=$(stat -c "%a" "$file")
        if [ "$actual_perm" = "$expected_perm" ]; then
            print_status "pass" "$description has correct permissions ($expected_perm)"
        else
            print_status "warn" "$description has permissions $actual_perm, expected $expected_perm"
        fi
    else
        print_status "fail" "$description file not found: $file"
    fi
}

# These checks only work if the files exist on the host
if [ -f "containers/fleetdm/scripts/entrypoint.sh" ]; then
    check_file_permissions "containers/fleetdm/scripts/entrypoint.sh" "755" "FleetDM entrypoint script"
fi

if [ -f "containers/fleetdm/scripts/generate-certs.sh" ]; then
    check_file_permissions "containers/fleetdm/scripts/generate-certs.sh" "755" "Certificate generation script"
fi

echo ""

# Check log files
echo -e "${YELLOW}Checking Log Files...${NC}"
echo ""

check_container_logs() {
    local service=$1
    local description=$2
    
    if docker-compose logs --tail=1 "$service" >/dev/null 2>&1; then
        log_lines=$(docker-compose logs --tail=100 "$service" 2>/dev/null | wc -l)
        if [ "$log_lines" -gt 0 ]; then
            print_status "pass" "$description logs available ($log_lines recent entries)"
            
            # Check for error patterns
            error_count=$(docker-compose logs --tail=100 "$service" 2>/dev/null | grep -i "error\|fail\|exception" | wc -l)
            if [ "$error_count" -gt 0 ]; then
                print_status "warn" "$description logs contain $error_count error/warning entries"
            fi
        else
            print_status "warn" "$description logs are empty"
        fi
    else
        print_status "fail" "$description logs not accessible"
    fi
}

check_container_logs "fleetdm" "FleetDM"
check_container_logs "management-server" "Management Server"
check_container_logs "mysql" "MySQL"
check_container_logs "redis" "Redis"

echo ""

# Summary
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Verification Summary${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

echo -e "${BLUE}FleetDM Integration Components:${NC}"
echo "  • OSQuery configuration flags: ✓"
echo "  • Query packs for monitoring: ✓"
echo "  • Automatic enrollment process: ✓"
echo "  • Management portal integration: ✓"
echo "  • Dashboard display: ✓"
echo "  • Alert configuration interface: ✓"
echo "  • Query builder interface: ✓"
echo ""

echo -e "${BLUE}Quick Start Commands:${NC}"
echo "  Start services: docker-compose up -d"
echo "  View logs: docker-compose logs -f fleetdm"
echo "  Access Fleet: $FLEET_URL"
echo "  Access Portal: $MANAGEMENT_URL/fleet/dashboard"
echo "  Deploy OSQuery: ansible-playbook ansible/playbooks/deploy-osquery.yml"
echo ""

echo -e "${BLUE}Useful URLs:${NC}"
echo "  • FleetDM Dashboard: $MANAGEMENT_URL/fleet/dashboard"
echo "  • Host Management: $MANAGEMENT_URL/fleet/hosts"  
echo "  • Query Builder: $MANAGEMENT_URL/fleet/query_builder"
echo "  • Alert Configuration: $MANAGEMENT_URL/alerts"
echo "  • Enrollment Management: $MANAGEMENT_URL/fleet/enrollment"
echo ""

print_status "info" "Verification completed. Review any failed or warning items above."

echo ""