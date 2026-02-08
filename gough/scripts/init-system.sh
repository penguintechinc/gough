#!/bin/bash
set -euo pipefail

# MaaS Infrastructure Automation - System Initialization Script
# This script initializes the complete MaaS infrastructure automation system

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_FILE="${PROJECT_ROOT}/logs/init-system.log"

# Ensure logs directory exists
mkdir -p "${PROJECT_ROOT}/logs"

# Logging function
log() {
    local level=$1
    shift
    local message="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    echo -e "${timestamp} [${level}] ${message}" | tee -a "$LOG_FILE"
    
    case $level in
        "ERROR")   echo -e "${RED}${message}${NC}" ;;
        "WARNING") echo -e "${YELLOW}${message}${NC}" ;;
        "SUCCESS") echo -e "${GREEN}${message}${NC}" ;;
        "INFO")    echo -e "${BLUE}${message}${NC}" ;;
    esac
}

# Error handling
error_exit() {
    log "ERROR" "Script failed: $1"
    exit 1
}

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Print banner
print_banner() {
    echo -e "${BLUE}"
    echo "============================================================"
    echo "    MaaS Infrastructure Automation System Initialization"
    echo "============================================================"
    echo -e "${NC}"
    echo "Project: MaaS Infrastructure Automation"
    echo "Version: 1.0.0"
    echo "Date: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Location: $PROJECT_ROOT"
    echo ""
}

# Check system requirements
check_requirements() {
    log "INFO" "Checking system requirements..."
    
    # Check if running as root/sudo
    if [[ $EUID -eq 0 ]]; then
        error_exit "This script should not be run as root. Please run as a regular user with sudo access."
    fi
    
    # Check for required commands
    local required_commands=("docker" "docker-compose" "git" "curl" "jq" "openssl")
    local missing_commands=()
    
    for cmd in "${required_commands[@]}"; do
        if ! command_exists "$cmd"; then
            missing_commands+=("$cmd")
        fi
    done
    
    if [[ ${#missing_commands[@]} -gt 0 ]]; then
        log "ERROR" "Missing required commands: ${missing_commands[*]}"
        log "INFO" "Please install the missing commands and run this script again."
        
        # Provide installation instructions based on OS
        if command_exists "apt-get"; then
            log "INFO" "On Ubuntu/Debian, run:"
            log "INFO" "sudo apt-get update && sudo apt-get install -y docker.io docker-compose git curl jq openssl"
        elif command_exists "yum"; then
            log "INFO" "On CentOS/RHEL, run:"
            log "INFO" "sudo yum install -y docker docker-compose git curl jq openssl"
        fi
        
        exit 1
    fi
    
    # Check Docker daemon
    if ! docker info >/dev/null 2>&1; then
        error_exit "Docker daemon is not running. Please start Docker and try again."
    fi
    
    # Check Docker Compose version
    local compose_version
    if command_exists "docker-compose"; then
        compose_version=$(docker-compose --version | cut -d' ' -f3 | cut -d',' -f1)
        log "INFO" "Docker Compose version: $compose_version"
    fi
    
    log "SUCCESS" "All system requirements met!"
}

# Setup environment
setup_environment() {
    log "INFO" "Setting up environment..."
    
    cd "$PROJECT_ROOT"
    
    # Create .env file if it doesn't exist
    if [[ ! -f .env ]]; then
        log "INFO" "Creating .env file from template..."
        cp .env.example .env
        log "WARNING" "Please edit .env file with your specific configuration before continuing!"
        log "INFO" "Key settings to change:"
        log "INFO" "- MAAS_ADMIN_PASSWORD"
        log "INFO" "- SECRET_KEY"
        log "INFO" "- Database passwords"
        log "INFO" "- SSH public keys"
        
        read -p "Press Enter after updating .env file, or Ctrl+C to abort..."
    fi
    
    # Create secrets file if it doesn't exist
    if [[ ! -f config/secrets.yml ]]; then
        log "INFO" "Creating secrets configuration..."
        cp config/secrets.example.yml config/secrets.yml
        chmod 600 config/secrets.yml
        log "WARNING" "Please edit config/secrets.yml with your actual secrets!"
    fi
    
    # Create necessary directories
    log "INFO" "Creating required directories..."
    mkdir -p {logs,backups,data/{postgres,mysql,redis,maas,fleet}}
    
    # Set permissions
    chmod 755 scripts/*.sh
    chmod 600 config/secrets.yml
    
    log "SUCCESS" "Environment setup completed!"
}

# Generate SSL certificates
generate_ssl_certificates() {
    log "INFO" "Generating SSL certificates..."
    
    local ssl_dir="$PROJECT_ROOT/config/ssl"
    mkdir -p "$ssl_dir"
    
    if [[ ! -f "$ssl_dir/server.crt" ]]; then
        log "INFO" "Generating self-signed SSL certificate..."
        
        openssl req -x509 -newkey rsa:4096 \
            -keyout "$ssl_dir/server.key" \
            -out "$ssl_dir/server.crt" \
            -days 365 -nodes \
            -subj "/C=US/ST=CA/L=San Francisco/O=MaaS Infrastructure/CN=maas.local" \
            -addext "subjectAltName=DNS:maas.local,DNS:localhost,DNS:maas,DNS:management-server,DNS:fleetdm,IP:127.0.0.1,IP:172.20.0.2,IP:172.20.0.3,IP:172.20.0.10"
        
        chmod 600 "$ssl_dir/server.key"
        chmod 644 "$ssl_dir/server.crt"
        
        log "SUCCESS" "SSL certificates generated!"
    else
        log "INFO" "SSL certificates already exist, skipping generation."
    fi
}

# Generate SSH keys
generate_ssh_keys() {
    log "INFO" "Setting up SSH keys..."
    
    local ssh_dir="$PROJECT_ROOT/config/ssh"
    mkdir -p "$ssh_dir"
    
    # Generate SSH key for Ansible if it doesn't exist
    if [[ ! -f "$ssh_dir/ansible_key" ]]; then
        log "INFO" "Generating SSH key for Ansible automation..."
        
        ssh-keygen -t ed25519 -f "$ssh_dir/ansible_key" \
            -C "ansible@maas-automation" -N ""
        
        chmod 600 "$ssh_dir/ansible_key"
        chmod 644 "$ssh_dir/ansible_key.pub"
        
        log "SUCCESS" "Ansible SSH key generated!"
        log "INFO" "Public key: $(cat "$ssh_dir/ansible_key.pub")"
    else
        log "INFO" "Ansible SSH key already exists."
    fi
    
    # Generate SSH key for MaaS user if it doesn't exist
    if [[ ! -f "$ssh_dir/maas_key" ]]; then
        log "INFO" "Generating SSH key for MaaS user..."
        
        ssh-keygen -t ed25519 -f "$ssh_dir/maas_key" \
            -C "maas@maas-automation" -N ""
        
        chmod 600 "$ssh_dir/maas_key"
        chmod 644 "$ssh_dir/maas_key.pub"
        
        log "SUCCESS" "MaaS SSH key generated!"
        log "INFO" "Public key: $(cat "$ssh_dir/maas_key.pub")"
    else
        log "INFO" "MaaS SSH key already exists."
    fi
}

# Initialize databases
init_databases() {
    log "INFO" "Initializing databases..."
    
    # Create database initialization SQL
    cat > "$PROJECT_ROOT/scripts/init-postgres.sql" << 'EOF'
-- PostgreSQL initialization script for MaaS management server

-- Create management database if it doesn't exist
CREATE DATABASE IF NOT EXISTS management;

-- Create user for management application
CREATE USER IF NOT EXISTS maas_management WITH PASSWORD 'management_pass';
GRANT ALL PRIVILEGES ON DATABASE management TO maas_management;

-- Set up extensions
\c management;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Create basic tables will be created by py4web application
EOF
    
    log "SUCCESS" "Database initialization scripts created!"
}

# Build and start services
start_services() {
    log "INFO" "Building and starting services..."
    
    cd "$PROJECT_ROOT"
    
    # Build all images
    log "INFO" "Building Docker images (this may take a while)..."
    docker-compose build --no-cache
    
    # Start core services first
    log "INFO" "Starting database services..."
    docker-compose up -d postgres mysql redis
    
    # Wait for databases to be ready
    log "INFO" "Waiting for databases to be ready..."
    sleep 30
    
    # Check database connectivity
    local retries=0
    while [[ $retries -lt 12 ]]; do
        if docker-compose exec -T postgres pg_isready -U postgres >/dev/null 2>&1; then
            log "SUCCESS" "PostgreSQL is ready!"
            break
        fi
        
        log "INFO" "Waiting for PostgreSQL... (attempt $((retries + 1))/12)"
        sleep 5
        ((retries++))
    done
    
    if [[ $retries -eq 12 ]]; then
        error_exit "PostgreSQL failed to start after 60 seconds"
    fi
    
    # Start remaining services
    log "INFO" "Starting all services..."
    docker-compose up -d
    
    # Wait for services to be healthy
    log "INFO" "Waiting for services to become healthy..."
    sleep 60
    
    log "SUCCESS" "All services started!"
}

# Verify installation
verify_installation() {
    log "INFO" "Verifying installation..."
    
    # Check service status
    log "INFO" "Checking service health..."
    
    local services=("postgres" "mysql" "redis" "maas" "management-server" "fleetdm")
    local failed_services=()
    
    for service in "${services[@]}"; do
        if docker-compose ps | grep -q "$service.*healthy\|Up"; then
            log "SUCCESS" "✓ $service is running"
        else
            log "ERROR" "✗ $service is not healthy"
            failed_services+=("$service")
        fi
    done
    
    if [[ ${#failed_services[@]} -gt 0 ]]; then
        log "WARNING" "Some services failed to start: ${failed_services[*]}"
        log "INFO" "Check logs with: docker-compose logs [service-name]"
    fi
    
    # Test API endpoints
    log "INFO" "Testing API endpoints..."
    
    # Test MaaS API
    if curl -sSf "http://localhost:5240/MAAS/" >/dev/null 2>&1; then
        log "SUCCESS" "✓ MaaS web interface is accessible"
    else
        log "WARNING" "✗ MaaS web interface is not accessible"
    fi
    
    # Test Management Server
    if curl -sSf "http://localhost:8000/" >/dev/null 2>&1; then
        log "SUCCESS" "✓ Management server is accessible"
    else
        log "WARNING" "✗ Management server is not accessible"
    fi
    
    # Test FleetDM (with SSL skip for self-signed cert)
    if curl -sSfk "https://localhost:8443/api/v1/fleet/version" >/dev/null 2>&1; then
        log "SUCCESS" "✓ FleetDM server is accessible"
    else
        log "WARNING" "✗ FleetDM server is not accessible"
    fi
    
    log "SUCCESS" "Installation verification completed!"
}

# Print access information
print_access_info() {
    log "INFO" "System initialization completed!"
    echo ""
    echo -e "${GREEN}============================================================${NC}"
    echo -e "${GREEN}    MaaS Infrastructure Automation System Ready!${NC}"
    echo -e "${GREEN}============================================================${NC}"
    echo ""
    echo "🌐 Web Interfaces:"
    echo "   • MaaS Server:        http://localhost:5240/MAAS/"
    echo "   • Management Portal:  http://localhost:8000/"
    echo "   • FleetDM Security:   https://localhost:8443/"
    echo ""
    echo "🔐 Default Credentials:"
    echo "   • MaaS Admin:         admin / admin"
    echo "   • Fleet Admin:        admin@fleet.local / admin123"
    echo ""
    echo "📂 Important Directories:"
    echo "   • Project Root:       $PROJECT_ROOT"
    echo "   • Configuration:      $PROJECT_ROOT/config/"
    echo "   • Logs:              $PROJECT_ROOT/logs/"
    echo "   • Cloud-Init:        $PROJECT_ROOT/cloud-init/templates/"
    echo ""
    echo "🔧 Useful Commands:"
    echo "   • View logs:         docker-compose logs [service]"
    echo "   • Restart services:  docker-compose restart"
    echo "   • Stop all:          docker-compose down"
    echo "   • Deploy server:     ./scripts/deploy-server.sh"
    echo ""
    echo "📚 Next Steps:"
    echo "   1. Configure MaaS server with your network settings"
    echo "   2. Add physical machines to MaaS"
    echo "   3. Create cloud-init templates for your use cases"
    echo "   4. Deploy your first server using the management portal"
    echo ""
    echo -e "${YELLOW}⚠️  Remember to change default passwords in production!${NC}"
    echo ""
}

# Cleanup function
cleanup() {
    log "INFO" "Cleaning up temporary files..."
    # Add cleanup tasks if needed
}

# Main execution
main() {
    # Set trap for cleanup on exit
    trap cleanup EXIT
    
    print_banner
    check_requirements
    setup_environment
    generate_ssl_certificates
    generate_ssh_keys
    init_databases
    start_services
    verify_installation
    print_access_info
    
    log "SUCCESS" "System initialization completed successfully!"
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Initialization completed" >> "$LOG_FILE"
}

# Handle command line arguments
case "${1:-}" in
    --help|-h)
        echo "Usage: $0 [options]"
        echo ""
        echo "Options:"
        echo "  --help, -h     Show this help message"
        echo "  --check        Only check requirements"
        echo "  --env-only     Only setup environment files"
        echo ""
        exit 0
        ;;
    --check)
        print_banner
        check_requirements
        exit 0
        ;;
    --env-only)
        print_banner
        setup_environment
        exit 0
        ;;
    "")
        main
        ;;
    *)
        error_exit "Unknown option: $1. Use --help for usage information."
        ;;
esac