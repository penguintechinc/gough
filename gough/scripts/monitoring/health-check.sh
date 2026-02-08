#!/bin/bash
# Gough Hypervisor - Health Check Script
# Comprehensive health monitoring for all services

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Health check results
TOTAL_CHECKS=0
PASSED_CHECKS=0
FAILED_CHECKS=0

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[PASS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[FAIL]${NC} $1"
}

# Function to check service health
check_service() {
    local service_name="$1"
    local health_url="$2"
    local expected_status="${3:-200}"
    
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    if curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 "$health_url" | grep -q "$expected_status"; then
        print_success "$service_name is healthy"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        return 0
    else
        print_error "$service_name is not healthy (URL: $health_url)"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

# Function to check container status
check_container() {
    local container_name="$1"
    
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    if docker ps --format "table {{.Names}}\t{{.Status}}" | grep -q "$container_name.*Up"; then
        print_success "Container $container_name is running"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        return 0
    else
        print_error "Container $container_name is not running"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

# Function to check disk space
check_disk_space() {
    local path="$1"
    local threshold="${2:-85}"
    
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    local usage=$(df "$path" | tail -1 | awk '{print $5}' | sed 's/%//')
    
    if [[ "$usage" -lt "$threshold" ]]; then
        print_success "Disk space OK: ${usage}% used on $path"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        return 0
    else
        print_error "Disk space critical: ${usage}% used on $path (threshold: ${threshold}%)"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

# Function to check memory usage
check_memory_usage() {
    local threshold="${1:-90}"
    
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    local usage=$(free | grep '^Mem:' | awk '{printf "%.0f", $3/$2 * 100.0}')
    
    if [[ "$usage" -lt "$threshold" ]]; then
        print_success "Memory usage OK: ${usage}%"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        return 0
    else
        print_error "Memory usage high: ${usage}% (threshold: ${threshold}%)"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

# Function to check Elasticsearch cluster health
check_elasticsearch_cluster() {
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    local cluster_health=$(curl -s "http://localhost:9200/_cluster/health" 2>/dev/null)
    
    if [[ -n "$cluster_health" ]]; then
        local status=$(echo "$cluster_health" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
        
        case "$status" in
            "green")
                print_success "Elasticsearch cluster health: $status"
                PASSED_CHECKS=$((PASSED_CHECKS + 1))
                return 0
                ;;
            "yellow")
                print_warning "Elasticsearch cluster health: $status (degraded but functional)"
                PASSED_CHECKS=$((PASSED_CHECKS + 1))
                return 0
                ;;
            "red")
                print_error "Elasticsearch cluster health: $status (critical)"
                FAILED_CHECKS=$((FAILED_CHECKS + 1))
                return 1
                ;;
            *)
                print_error "Elasticsearch cluster health: unknown status"
                FAILED_CHECKS=$((FAILED_CHECKS + 1))
                return 1
                ;;
        esac
    else
        print_error "Elasticsearch cluster health: unable to connect"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

# Function to check Prometheus targets
check_prometheus_targets() {
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    local targets=$(curl -s "http://localhost:9090/api/v1/targets" 2>/dev/null)
    
    if [[ -n "$targets" ]]; then
        local active_targets=$(echo "$targets" | grep -o '"health":"up"' | wc -l)
        local total_targets=$(echo "$targets" | grep -o '"health":"[^"]*"' | wc -l)
        
        if [[ "$active_targets" -eq "$total_targets" && "$total_targets" -gt 0 ]]; then
            print_success "Prometheus targets: $active_targets/$total_targets active"
            PASSED_CHECKS=$((PASSED_CHECKS + 1))
            return 0
        else
            print_warning "Prometheus targets: $active_targets/$total_targets active"
            PASSED_CHECKS=$((PASSED_CHECKS + 1))
            return 0
        fi
    else
        print_error "Prometheus targets: unable to retrieve target status"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

# Function to check log shipping
check_log_shipping() {
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    # Check if logs are being indexed in Elasticsearch
    local log_count=$(curl -s "http://localhost:9200/gough-logs-*/_count" 2>/dev/null | grep -o '"count":[0-9]*' | cut -d':' -f2)
    
    if [[ -n "$log_count" && "$log_count" -gt 0 ]]; then
        print_success "Log shipping: $log_count logs indexed"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        return 0
    else
        print_warning "Log shipping: no logs found or Elasticsearch unavailable"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

# Function to generate summary report
generate_summary() {
    echo
    print_status "==================== HEALTH CHECK SUMMARY ===================="
    echo -e "Total Checks: $TOTAL_CHECKS"
    echo -e "Passed: ${GREEN}$PASSED_CHECKS${NC}"
    echo -e "Failed: ${RED}$FAILED_CHECKS${NC}"
    
    if [[ "$FAILED_CHECKS" -eq 0 ]]; then
        print_success "All health checks passed!"
        return 0
    else
        print_error "$FAILED_CHECKS health check(s) failed"
        return 1
    fi
}

# Function to show detailed status
show_detailed_status() {
    print_status "==================== DETAILED STATUS ===================="
    
    # Docker containers
    print_status "Docker Containers:"
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "(prometheus|grafana|elasticsearch|kibana|logstash|filebeat|alertmanager)"
    
    echo
    
    # System resources
    print_status "System Resources:"
    echo "Memory Usage:"
    free -h
    echo
    echo "Disk Usage:"
    df -h | head -1
    df -h | grep -E "/$|/var|/opt" || true
    
    echo
    
    # Service URLs
    print_status "Service Access URLs:"
    echo "  - Grafana: http://localhost:3000"
    echo "  - Prometheus: http://localhost:9090" 
    echo "  - AlertManager: http://localhost:9093"
    echo "  - Kibana: http://localhost:5601"
    echo "  - Elasticsearch: http://localhost:9200"
}

# Main execution
print_status "Starting Gough Hypervisor health check..."
print_status "Timestamp: $(date)"

echo
print_status "==================== CONTAINER CHECKS ===================="

# Check core application containers
check_container "postgres"
check_container "redis"
check_container "management-server"

# Check monitoring containers
check_container "prometheus" || true
check_container "grafana" || true
check_container "alertmanager" || true
check_container "node-exporter" || true

# Check logging containers
check_container "elasticsearch" || true
check_container "logstash" || true
check_container "kibana" || true
check_container "filebeat" || true

echo
print_status "==================== SERVICE HEALTH CHECKS ===================="

# Core application services
check_service "Management Server" "http://localhost:8000" || true
check_service "PostgreSQL" "http://localhost:9187/metrics" || true
check_service "Redis" "http://localhost:9121/metrics" || true

# Monitoring services
check_service "Prometheus" "http://localhost:9090/-/healthy" || true
check_service "Grafana" "http://localhost:3000/api/health" || true
check_service "AlertManager" "http://localhost:9093/-/healthy" || true
check_service "Node Exporter" "http://localhost:9100/metrics" || true

# Logging services
check_service "Elasticsearch" "http://localhost:9200/_cluster/health" || true
check_service "Kibana" "http://localhost:5601/api/status" || true
check_service "Logstash" "http://localhost:9600" || true

echo
print_status "==================== SYSTEM RESOURCE CHECKS ===================="

# System resource checks
check_disk_space "/" 85
check_disk_space "/var" 85 || true
check_memory_usage 90

echo
print_status "==================== SPECIALIZED CHECKS ===================="

# Specialized health checks
check_elasticsearch_cluster || true
check_prometheus_targets || true
check_log_shipping || true

# Generate summary
generate_summary
exit_code=$?

# Show detailed status if requested or if there are failures
if [[ "$1" == "--detailed" || "$exit_code" -ne 0 ]]; then
    echo
    show_detailed_status
fi

exit $exit_code