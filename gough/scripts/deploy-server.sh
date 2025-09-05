#!/bin/bash
set -euo pipefail

# MaaS Infrastructure - Server Deployment Script
# Deploy and configure servers using MaaS and Ansible

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_FILE="${PROJECT_ROOT}/logs/deploy-$(date +%Y%m%d-%H%M%S).log"

# Default values
DEFAULT_TEMPLATE="base-server"
DEFAULT_ARCHITECTURE="amd64/generic"
DEFAULT_DISTRO="jammy"
DEFAULT_POWER_TYPE="manual"

# Ensure logs directory exists
mkdir -p "${PROJECT_ROOT}/logs"

# Logging function
log() {
    local level=$1
    shift
    local message="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    echo "${timestamp} [${level}] ${message}" >> "$LOG_FILE"
    
    case $level in
        "ERROR")   echo -e "${RED}[ERROR] ${message}${NC}" ;;
        "WARNING") echo -e "${YELLOW}[WARNING] ${message}${NC}" ;;
        "SUCCESS") echo -e "${GREEN}[SUCCESS] ${message}${NC}" ;;
        "INFO")    echo -e "${BLUE}[INFO] ${message}${NC}" ;;
    esac
}

# Error handling
error_exit() {
    log "ERROR" "Deployment failed: $1"
    echo ""
    echo "Check the log file for details: $LOG_FILE"
    exit 1
}

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Print usage
usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Deploy a server through MaaS with automated configuration"
    echo ""
    echo "Required Options:"
    echo "  -n, --hostname HOSTNAME     Target hostname for the server"
    echo "  -m, --mac MAC_ADDRESS       MAC address of the server's boot interface"
    echo ""
    echo "Optional Options:"
    echo "  -t, --template TEMPLATE     Cloud-init template to use (default: $DEFAULT_TEMPLATE)"
    echo "  -a, --architecture ARCH     Server architecture (default: $DEFAULT_ARCHITECTURE)"
    echo "  -d, --distro DISTRO         Ubuntu distribution series (default: $DEFAULT_DISTRO)"
    echo "  -p, --power-type TYPE       Power management type (default: $DEFAULT_POWER_TYPE)"
    echo "  -g, --groups GROUPS         Ansible groups (comma-separated)"
    echo "  -s, --system-id ID          Existing MaaS system ID (skip creation)"
    echo "  -c, --skip-commission       Skip commissioning (machine already commissioned)"
    echo "  -w, --wait-only             Only wait for existing deployment"
    echo "      --dry-run               Show what would be done without executing"
    echo "  -h, --help                  Show this help message"
    echo ""
    echo "Examples:"
    echo "  # Deploy a basic server"
    echo "  $0 -n web01 -m 00:11:22:33:44:55"
    echo ""
    echo "  # Deploy a Docker host with specific template"
    echo "  $0 -n docker01 -m 00:11:22:33:44:56 -t docker-host -g docker_hosts"
    echo ""
    echo "  # Deploy using existing commissioned machine"
    echo "  $0 -n k8s01 -s abc123 -c -t kubernetes-node"
    echo ""
    echo "Available templates:"
    for template in "$PROJECT_ROOT"/cloud-init/templates/*.yaml; do
        if [[ -f "$template" ]]; then
            basename "$template" .yaml | sed 's/^/  - /'
        fi
    done
}

# Parse command line arguments
parse_args() {
    HOSTNAME=""
    MAC_ADDRESS=""
    TEMPLATE="$DEFAULT_TEMPLATE"
    ARCHITECTURE="$DEFAULT_ARCHITECTURE"
    DISTRO="$DEFAULT_DISTRO"
    POWER_TYPE="$DEFAULT_POWER_TYPE"
    GROUPS=""
    SYSTEM_ID=""
    SKIP_COMMISSION=false
    WAIT_ONLY=false
    DRY_RUN=false
    
    while [[ $# -gt 0 ]]; do
        case $1 in
            -n|--hostname)
                HOSTNAME="$2"
                shift 2
                ;;
            -m|--mac)
                MAC_ADDRESS="$2"
                shift 2
                ;;
            -t|--template)
                TEMPLATE="$2"
                shift 2
                ;;
            -a|--architecture)
                ARCHITECTURE="$2"
                shift 2
                ;;
            -d|--distro)
                DISTRO="$2"
                shift 2
                ;;
            -p|--power-type)
                POWER_TYPE="$2"
                shift 2
                ;;
            -g|--groups)
                GROUPS="$2"
                shift 2
                ;;
            -s|--system-id)
                SYSTEM_ID="$2"
                shift 2
                ;;
            -c|--skip-commission)
                SKIP_COMMISSION=true
                shift
                ;;
            -w|--wait-only)
                WAIT_ONLY=true
                shift
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                error_exit "Unknown option: $1"
                ;;
        esac
    done
    
    # Validate required arguments
    if [[ -z "$HOSTNAME" ]]; then
        error_exit "Hostname is required (-n/--hostname)"
    fi
    
    if [[ -z "$MAC_ADDRESS" && -z "$SYSTEM_ID" && ! "$WAIT_ONLY" == true ]]; then
        error_exit "MAC address is required (-m/--mac) unless using existing system ID"
    fi
}

# Load environment configuration
load_config() {
    log "INFO" "Loading configuration..."
    
    cd "$PROJECT_ROOT"
    
    # Load environment variables
    if [[ -f .env ]]; then
        set -a
        source .env
        set +a
    else
        error_exit "Environment file (.env) not found. Run init-system.sh first."
    fi
    
    # Set MaaS configuration
    MAAS_URL="${MAAS_URL:-http://localhost:5240/MAAS/}"
    MAAS_API_KEY="${MAAS_API_KEY:-}"
    MANAGEMENT_URL="${MANAGEMENT_SERVER_URL:-http://localhost:8000}"
    MANAGEMENT_API_KEY="${MANAGEMENT_API_KEY:-}"
    
    if [[ -z "$MAAS_API_KEY" ]]; then
        error_exit "MAAS_API_KEY not configured in .env file"
    fi
    
    log "SUCCESS" "Configuration loaded successfully"
}

# Validate template
validate_template() {
    local template_file="$PROJECT_ROOT/cloud-init/templates/${TEMPLATE}.yaml"
    
    if [[ ! -f "$template_file" ]]; then
        error_exit "Template file not found: $template_file"
    fi
    
    log "INFO" "Using cloud-init template: $TEMPLATE"
}

# Check prerequisites
check_prerequisites() {
    log "INFO" "Checking prerequisites..."
    
    # Check required commands
    local required_commands=("curl" "jq" "ansible-playbook")
    for cmd in "${required_commands[@]}"; do
        if ! command_exists "$cmd"; then
            error_exit "Required command not found: $cmd"
        fi
    done
    
    # Check MaaS connectivity
    if ! curl -sSf "${MAAS_URL}api/2.0/version/" -H "Authorization: OAuth $MAAS_API_KEY" >/dev/null 2>&1; then
        error_exit "Cannot connect to MaaS server at $MAAS_URL"
    fi
    
    # Check if hostname is already in use
    local existing_machine
    existing_machine=$(curl -sS "${MAAS_URL}api/2.0/machines/" \
        -H "Authorization: OAuth $MAAS_API_KEY" \
        | jq -r ".[] | select(.hostname == \"$HOSTNAME\") | .system_id // empty")
    
    if [[ -n "$existing_machine" && -z "$SYSTEM_ID" ]]; then
        log "WARNING" "Machine with hostname '$HOSTNAME' already exists (system_id: $existing_machine)"
        read -p "Do you want to use the existing machine? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            SYSTEM_ID="$existing_machine"
            SKIP_COMMISSION=true
        else
            error_exit "Deployment cancelled"
        fi
    fi
    
    log "SUCCESS" "Prerequisites check passed"
}

# Generate cloud-init data
generate_cloud_init() {
    log "INFO" "Generating cloud-init configuration..."
    
    local template_file="$PROJECT_ROOT/cloud-init/templates/${TEMPLATE}.yaml"
    local temp_file="/tmp/cloud-init-${HOSTNAME}-$(date +%s).yaml"
    
    # Read SSH public keys
    local ssh_public_key=""
    local ansible_ssh_key=""
    
    if [[ -f "$PROJECT_ROOT/config/ssh/maas_key.pub" ]]; then
        ssh_public_key=$(cat "$PROJECT_ROOT/config/ssh/maas_key.pub")
    fi
    
    if [[ -f "$PROJECT_ROOT/config/ssh/ansible_key.pub" ]]; then
        ansible_ssh_key=$(cat "$PROJECT_ROOT/config/ssh/ansible_key.pub")
    fi
    
    # Substitute variables in template
    sed -e "s/\${HOSTNAME}/$HOSTNAME/g" \
        -e "s/\${DOMAIN:-local}/local/g" \
        -e "s/\${TIMEZONE:-UTC}/UTC/g" \
        -e "s|\${SSH_PUBLIC_KEY}|$ssh_public_key|g" \
        -e "s|\${ANSIBLE_SSH_PUBLIC_KEY}|$ansible_ssh_key|g" \
        -e "s|\${MANAGEMENT_SERVER_URL:-http://172.20.0.2:8000}|$MANAGEMENT_URL|g" \
        -e "s/\${MANAGEMENT_SUBNET:-172.20.0.0\/16}/172.20.0.0\/16/g" \
        -e "s/\${PRIMARY_INTERFACE:-ens3}/ens3/g" \
        "$template_file" > "$temp_file"
    
    CLOUD_INIT_DATA=$(cat "$temp_file")
    rm -f "$temp_file"
    
    log "SUCCESS" "Cloud-init configuration generated"
}

# Deploy server via Ansible
deploy_server() {
    log "INFO" "Starting server deployment..."
    
    if [[ "$DRY_RUN" == true ]]; then
        log "INFO" "DRY RUN - Would deploy server with:"
        log "INFO" "  Hostname: $HOSTNAME"
        log "INFO" "  MAC: $MAC_ADDRESS"
        log "INFO" "  Template: $TEMPLATE"
        log "INFO" "  Architecture: $ARCHITECTURE"
        log "INFO" "  Distribution: $DISTRO"
        log "INFO" "  System ID: $SYSTEM_ID"
        return 0
    fi
    
    # Prepare Ansible variables
    local ansible_vars=""
    ansible_vars+="-e target_hostname=$HOSTNAME "
    ansible_vars+="-e deployment_template=$TEMPLATE "
    ansible_vars+="-e target_architecture=$ARCHITECTURE "
    ansible_vars+="-e distro_series=$DISTRO "
    ansible_vars+="-e power_type=$POWER_TYPE "
    ansible_vars+="-e maas_url=$MAAS_URL "
    ansible_vars+="-e maas_api_key='$MAAS_API_KEY' "
    ansible_vars+="-e management_server_url=$MANAGEMENT_URL "
    ansible_vars+="-e management_api_key='$MANAGEMENT_API_KEY' "
    ansible_vars+="-e cloud_init_data='$CLOUD_INIT_DATA' "
    
    if [[ -n "$MAC_ADDRESS" ]]; then
        ansible_vars+="-e server_mac_address=$MAC_ADDRESS "
    fi
    
    if [[ -n "$SYSTEM_ID" ]]; then
        ansible_vars+="-e machine_system_id=$SYSTEM_ID "
    fi
    
    if [[ "$SKIP_COMMISSION" == true ]]; then
        ansible_vars+="-e skip_commission=true "
    fi
    
    if [[ -n "$GROUPS" ]]; then
        IFS=',' read -ra GROUP_ARRAY <<< "$GROUPS"
        local groups_json=$(printf '%s\n' "${GROUP_ARRAY[@]}" | jq -R . | jq -s .)
        ansible_vars+="-e target_groups='$groups_json' "
    fi
    
    # Run Ansible playbook
    log "INFO" "Executing Ansible deployment playbook..."
    
    cd "$PROJECT_ROOT/ansible"
    
    if ansible-playbook playbooks/deploy-server.yml $ansible_vars -v; then
        log "SUCCESS" "Server deployment completed successfully!"
        
        # Get deployment information
        local deployed_ip
        deployed_ip=$(grep "machine_ip:" "$LOG_FILE" | tail -1 | cut -d: -f2- | tr -d ' ')
        
        if [[ -n "$deployed_ip" ]]; then
            log "INFO" "Server deployed at IP: $deployed_ip"
        fi
        
        return 0
    else
        error_exit "Ansible deployment failed"
    fi
}

# Wait for existing deployment
wait_for_deployment() {
    log "INFO" "Waiting for existing deployment to complete..."
    
    if [[ -z "$SYSTEM_ID" ]]; then
        error_exit "System ID required for wait-only mode"
    fi
    
    local status=""
    local retries=0
    local max_retries=60  # 30 minutes maximum
    
    while [[ $retries -lt $max_retries ]]; do
        status=$(curl -sS "${MAAS_URL}api/2.0/machines/${SYSTEM_ID}/" \
            -H "Authorization: OAuth $MAAS_API_KEY" \
            | jq -r '.status_name')
        
        log "INFO" "Current status: $status (check $((retries + 1))/$max_retries)"
        
        case "$status" in
            "Deployed")
                log "SUCCESS" "Deployment completed successfully!"
                return 0
                ;;
            "Failed deployment"|"Failed commissioning")
                error_exit "Deployment failed with status: $status"
                ;;
            "Deploying"|"Commissioning")
                log "INFO" "Deployment in progress..."
                ;;
        esac
        
        sleep 30
        ((retries++))
    done
    
    error_exit "Deployment timed out after 30 minutes"
}

# Print deployment summary
print_summary() {
    echo ""
    echo -e "${GREEN}============================================================${NC}"
    echo -e "${GREEN}    Server Deployment Summary${NC}"
    echo -e "${GREEN}============================================================${NC}"
    echo ""
    echo "Server Details:"
    echo "  • Hostname:      $HOSTNAME"
    echo "  • Template:      $TEMPLATE"
    echo "  • Architecture:  $ARCHITECTURE"
    echo "  • Distribution:  $DISTRO"
    
    if [[ -n "$SYSTEM_ID" ]]; then
        echo "  • System ID:     $SYSTEM_ID"
    fi
    
    if [[ -n "$MAC_ADDRESS" ]]; then
        echo "  • MAC Address:   $MAC_ADDRESS"
    fi
    
    echo ""
    echo "Next Steps:"
    echo "  • Monitor deployment in MaaS UI: ${MAAS_URL}"
    echo "  • Check server status: ssh maas@[server-ip]"
    echo "  • View deployment logs: $LOG_FILE"
    echo ""
}

# Main execution
main() {
    log "INFO" "Starting server deployment process"
    
    parse_args "$@"
    load_config
    validate_template
    check_prerequisites
    
    if [[ "$WAIT_ONLY" == true ]]; then
        wait_for_deployment
    else
        generate_cloud_init
        deploy_server
    fi
    
    print_summary
    log "SUCCESS" "Server deployment process completed"
}

# Run main function with all arguments
main "$@"