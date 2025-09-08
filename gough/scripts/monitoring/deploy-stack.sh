#!/bin/bash
# Gough Hypervisor - Monitoring Stack Deployment Script
# Automates the deployment of monitoring and logging infrastructure

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
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"

# Default settings
DEPLOY_MONITORING=false
DEPLOY_LOGGING=false
DEPLOY_ALL=false
SKIP_CHECKS=false
DRY_RUN=false

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to show usage
show_usage() {
    cat << EOF
Gough Hypervisor - Monitoring Stack Deployment

Usage: $0 [OPTIONS]

OPTIONS:
    -m, --monitoring     Deploy monitoring stack (Prometheus, Grafana, AlertManager)
    -l, --logging        Deploy logging stack (ELK + Filebeat)
    -a, --all           Deploy both monitoring and logging stacks
    -s, --skip-checks   Skip pre-deployment checks
    -d, --dry-run       Show what would be deployed without executing
    -h, --help          Show this help message

EXAMPLES:
    $0 --monitoring                  # Deploy only monitoring stack
    $0 --logging                     # Deploy only logging stack
    $0 --all                        # Deploy both stacks
    $0 --all --skip-checks          # Deploy all with minimal checks

EOF
}

# Function to check prerequisites
check_prerequisites() {
    if [[ "$SKIP_CHECKS" == "true" ]]; then
        print_warning "Skipping prerequisite checks"
        return 0
    fi

    print_status "Checking prerequisites..."

    # Check Docker
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed or not in PATH"
        exit 1
    fi

    # Check Docker Compose
    if ! command -v docker-compose &> /dev/null; then
        print_error "Docker Compose is not installed or not in PATH"
        exit 1
    fi

    # Check if Docker is running
    if ! docker info &> /dev/null; then
        print_error "Docker daemon is not running"
        exit 1
    fi

    # Check available disk space (minimum 10GB)
    available_space=$(df "$PROJECT_DIR" | tail -1 | awk '{print $4}')
    min_space=$((10 * 1024 * 1024)) # 10GB in KB
    if [[ "$available_space" -lt "$min_space" ]]; then
        print_error "Insufficient disk space. Minimum 10GB required, $(($available_space / 1024 / 1024))GB available"
        exit 1
    fi

    # Check available RAM (minimum 4GB)
    available_ram=$(free -k | grep '^Mem:' | awk '{print $2}')
    min_ram=$((4 * 1024 * 1024)) # 4GB in KB
    if [[ "$available_ram" -lt "$min_ram" ]]; then
        print_warning "Low RAM detected. Minimum 4GB recommended, $(($available_ram / 1024 / 1024))GB available"
    fi

    print_success "Prerequisites check passed"
}

# Function to prepare configuration
prepare_config() {
    print_status "Preparing configuration..."

    # Create config directories if they don't exist
    mkdir -p "$PROJECT_DIR/config"/{prometheus,grafana,alertmanager,elasticsearch,logstash,kibana,filebeat,blackbox}

    # Copy environment file if it doesn't exist
    if [[ ! -f "$PROJECT_DIR/.env.monitoring.local" ]]; then
        if [[ -f "$PROJECT_DIR/.env.monitoring" ]]; then
            cp "$PROJECT_DIR/.env.monitoring" "$PROJECT_DIR/.env.monitoring.local"
            print_status "Created .env.monitoring.local from template"
        else
            print_warning "No environment file found. Using defaults."
        fi
    fi

    print_success "Configuration prepared"
}

# Function to deploy monitoring stack
deploy_monitoring() {
    print_status "Deploying monitoring stack..."

    if [[ "$DRY_RUN" == "true" ]]; then
        print_status "DRY RUN: Would execute: docker-compose --profile monitoring up -d"
        return 0
    fi

    cd "$PROJECT_DIR"

    # Pull images
    print_status "Pulling monitoring images..."
    docker-compose --profile monitoring pull

    # Start services
    print_status "Starting monitoring services..."
    docker-compose --profile monitoring up -d

    # Wait for services to be healthy
    print_status "Waiting for services to be ready..."
    sleep 30

    # Check service status
    if docker-compose ps | grep -q "monitoring"; then
        print_success "Monitoring stack deployed successfully"
        
        print_status "Access URLs:"
        echo "  - Grafana: http://localhost:3000 (admin/admin)"
        echo "  - Prometheus: http://localhost:9090"
        echo "  - AlertManager: http://localhost:9093"
    else
        print_error "Failed to deploy monitoring stack"
        exit 1
    fi
}

# Function to deploy logging stack
deploy_logging() {
    print_status "Deploying logging stack..."

    if [[ "$DRY_RUN" == "true" ]]; then
        print_status "DRY RUN: Would execute: docker-compose --profile logging up -d"
        return 0
    fi

    cd "$PROJECT_DIR"

    # Pull images
    print_status "Pulling logging images..."
    docker-compose --profile logging pull

    # Start services
    print_status "Starting logging services..."
    docker-compose --profile logging up -d

    # Wait for Elasticsearch to be ready
    print_status "Waiting for Elasticsearch to be ready..."
    timeout=120
    counter=0
    while [[ $counter -lt $timeout ]]; do
        if curl -s -f "http://localhost:9200/_cluster/health" > /dev/null 2>&1; then
            print_success "Elasticsearch is ready"
            break
        fi
        sleep 5
        counter=$((counter + 5))
        print_status "Waiting for Elasticsearch... ($counter/$timeout seconds)"
    done

    if [[ $counter -ge $timeout ]]; then
        print_error "Elasticsearch failed to start within $timeout seconds"
        exit 1
    fi

    # Check service status
    if docker-compose ps | grep -q "logging"; then
        print_success "Logging stack deployed successfully"
        
        print_status "Access URLs:"
        echo "  - Kibana: http://localhost:5601"
        echo "  - Elasticsearch: http://localhost:9200"
    else
        print_error "Failed to deploy logging stack"
        exit 1
    fi
}

# Function to show deployment status
show_status() {
    print_status "Current deployment status:"
    
    cd "$PROJECT_DIR"
    docker-compose ps

    print_status "\nService Health:"
    
    # Check Prometheus
    if curl -s -f "http://localhost:9090/-/healthy" > /dev/null 2>&1; then
        print_success "Prometheus: Healthy"
    else
        print_warning "Prometheus: Not accessible"
    fi

    # Check Grafana
    if curl -s -f "http://localhost:3000/api/health" > /dev/null 2>&1; then
        print_success "Grafana: Healthy"
    else
        print_warning "Grafana: Not accessible"
    fi

    # Check Elasticsearch
    if curl -s -f "http://localhost:9200/_cluster/health" > /dev/null 2>&1; then
        print_success "Elasticsearch: Healthy"
    else
        print_warning "Elasticsearch: Not accessible"
    fi

    # Check Kibana
    if curl -s -f "http://localhost:5601/api/status" > /dev/null 2>&1; then
        print_success "Kibana: Healthy"
    else
        print_warning "Kibana: Not accessible"
    fi
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -m|--monitoring)
            DEPLOY_MONITORING=true
            shift
            ;;
        -l|--logging)
            DEPLOY_LOGGING=true
            shift
            ;;
        -a|--all)
            DEPLOY_ALL=true
            shift
            ;;
        -s|--skip-checks)
            SKIP_CHECKS=true
            shift
            ;;
        -d|--dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

# Validate arguments
if [[ "$DEPLOY_ALL" == "false" && "$DEPLOY_MONITORING" == "false" && "$DEPLOY_LOGGING" == "false" ]]; then
    print_error "No deployment option specified"
    show_usage
    exit 1
fi

# Set deployment flags for --all
if [[ "$DEPLOY_ALL" == "true" ]]; then
    DEPLOY_MONITORING=true
    DEPLOY_LOGGING=true
fi

# Main execution
print_status "Starting Gough Hypervisor monitoring stack deployment"
print_status "Project directory: $PROJECT_DIR"

# Run checks
check_prerequisites

# Prepare configuration
prepare_config

# Deploy services
if [[ "$DEPLOY_MONITORING" == "true" ]]; then
    deploy_monitoring
fi

if [[ "$DEPLOY_LOGGING" == "true" ]]; then
    deploy_logging
fi

# Show final status
if [[ "$DRY_RUN" == "false" ]]; then
    echo
    show_status
fi

print_success "Deployment completed successfully!"
print_status "Check MONITORING_SETUP.md for detailed configuration and usage instructions"