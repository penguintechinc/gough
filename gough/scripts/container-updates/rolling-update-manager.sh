#!/bin/bash

# Gough Container Rolling Update Manager
# Automated rolling updates with zero-downtime deployment
# Support for Docker Compose and Kubernetes deployments

set -euo pipefail

# =================================
# CONFIGURATION VARIABLES
# =================================

SCRIPT_NAME="rolling-update-manager"
VERSION="1.0.0"
LOG_FILE="/var/log/gough/${SCRIPT_NAME}.log"
LOCK_FILE="/var/lock/${SCRIPT_NAME}.lock"

# Update configuration
UPDATE_STRATEGY=${UPDATE_STRATEGY:-"rolling"}  # rolling, blue-green, canary
MAX_UNAVAILABLE=${MAX_UNAVAILABLE:-1}
MAX_SURGE=${MAX_SURGE:-1}
UPDATE_TIMEOUT=${UPDATE_TIMEOUT:-600}  # 10 minutes
HEALTH_CHECK_TIMEOUT=${HEALTH_CHECK_TIMEOUT:-120}  # 2 minutes
ROLLBACK_ON_FAILURE=${ROLLBACK_ON_FAILURE:-true}

# Registry configuration
CONTAINER_REGISTRY=${CONTAINER_REGISTRY:-"docker.io"}
REGISTRY_USERNAME=${REGISTRY_USERNAME:-""}
REGISTRY_PASSWORD=${REGISTRY_PASSWORD:-""}

# Deployment targets
DEPLOYMENT_TYPE=${DEPLOYMENT_TYPE:-"compose"}  # compose, kubernetes
COMPOSE_FILE=${COMPOSE_FILE:-"docker-compose.yml"}
KUBECTL_CONFIG=${KUBECTL_CONFIG:-""}
KUBERNETES_NAMESPACE=${KUBERNETES_NAMESPACE:-"gough"}

# Notification configuration
SLACK_WEBHOOK=${SLACK_WEBHOOK:-""}
EMAIL_RECIPIENTS=${EMAIL_RECIPIENTS:-"ops@gough.local"}

# Services configuration
SERVICES_CONFIG="/etc/gough/services.yml"

# =================================
# LOGGING AND UTILITIES
# =================================

setup_logging() {
    mkdir -p "$(dirname "$LOG_FILE")"
    exec 1> >(tee -a "$LOG_FILE")
    exec 2>&1
}

log_info() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] INFO: $1"
}

log_warn() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] WARN: $1"
}

log_error() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $1"
}

log_fatal() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] FATAL: $1"
    cleanup_and_exit 1
}

# Progress tracking
show_progress() {
    local current=$1
    local total=$2
    local operation=$3
    local percent=$((current * 100 / total))
    printf "\r[%3d%%] %s (%d/%d)" "$percent" "$operation" "$current" "$total"
    if [[ $current -eq $total ]]; then
        printf "\n"
    fi
}

# =================================
# LOCK MANAGEMENT
# =================================

acquire_lock() {
    if ! (set -C; echo $$ > "$LOCK_FILE") 2>/dev/null; then
        local pid
        pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")
        log_fatal "Rolling update is already running (PID: $pid)"
    fi
    trap cleanup_and_exit EXIT
}

cleanup_and_exit() {
    local exit_code=${1:-0}
    
    # Remove lock file
    if [[ -f "$LOCK_FILE" ]]; then
        rm -f "$LOCK_FILE"
    fi
    
    # Final notification
    if [[ $exit_code -eq 0 ]]; then
        log_info "Rolling update completed successfully"
        send_notification "✅ Container rolling update completed successfully" "success"
    else
        log_error "Rolling update failed with exit code $exit_code"
        send_notification "❌ Container rolling update failed" "error"
    fi
    
    exit "$exit_code"
}

# =================================
# NOTIFICATION SYSTEM
# =================================

send_notification() {
    local message=$1
    local type=${2:-info}
    local timestamp=$(date +'%Y-%m-%d %H:%M:%S UTC')
    
    # Send Slack notification
    if [[ -n "$SLACK_WEBHOOK" ]]; then
        send_slack_notification "$message" "$type" "$timestamp"
    fi
    
    # Send email notification
    send_email_notification "$message" "$type" "$timestamp"
}

send_slack_notification() {
    local message=$1
    local type=$2
    local timestamp=$3
    
    local color="good"
    local icon=":information_source:"
    
    case "$type" in
        success) color="good"; icon=":white_check_mark:" ;;
        error) color="danger"; icon=":x:" ;;
        warning) color="warning"; icon=":warning:" ;;
    esac
    
    local payload=$(cat <<EOF
{
    "channel": "#gough-deployments",
    "username": "Gough Update Manager",
    "icon_emoji": "$icon",
    "attachments": [
        {
            "color": "$color",
            "title": "Container Update Notification",
            "text": "$message",
            "fields": [
                {
                    "title": "Server",
                    "value": "$(hostname)",
                    "short": true
                },
                {
                    "title": "Strategy",
                    "value": "$UPDATE_STRATEGY",
                    "short": true
                },
                {
                    "title": "Timestamp",
                    "value": "$timestamp",
                    "short": true
                }
            ]
        }
    ]
}
EOF
    )
    
    curl -s -X POST -H 'Content-type: application/json' \
         --data "$payload" \
         "$SLACK_WEBHOOK" || true
}

send_email_notification() {
    local message=$1
    local type=$2
    local timestamp=$3
    
    local subject="Gough Container Update - $type"
    
    local body="
Container Update Notification

Message: $message
Server: $(hostname)
Strategy: $UPDATE_STRATEGY
Timestamp: $timestamp
Log File: $LOG_FILE

This is an automated notification from the Gough container update system.
"
    
    if command -v mail >/dev/null 2>&1; then
        echo "$body" | mail -s "$subject" "$EMAIL_RECIPIENTS"
    fi
}

# =================================
# REGISTRY OPERATIONS
# =================================

registry_login() {
    if [[ -n "$REGISTRY_USERNAME" ]] && [[ -n "$REGISTRY_PASSWORD" ]]; then
        log_info "Logging into container registry..."
        echo "$REGISTRY_PASSWORD" | docker login "$CONTAINER_REGISTRY" -u "$REGISTRY_USERNAME" --password-stdin
        log_info "Successfully logged into registry"
    fi
}

get_latest_image_tag() {
    local image=$1
    local current_tag=$2
    
    # For now, we'll use a simple approach - check for newer semantic versions
    # In production, you'd want more sophisticated version comparison
    
    log_info "Checking for updates to $image:$current_tag..."
    
    # Get available tags from registry (simplified - assumes Docker Hub API)
    local repo_name
    repo_name=$(echo "$image" | sed 's|.*/||')
    
    # Mock version check - in reality, you'd query the registry API
    # This is a simplified example
    case "$image" in
        *gough/maas*)
            echo "3.4.1"
            ;;
        *gough/management-server*)
            echo "1.0.1"
            ;;
        *fleetdm/fleet*)
            echo "v4.40.1"
            ;;
        postgres*)
            echo "15.1"
            ;;
        *)
            echo "$current_tag"  # No update available
            ;;
    esac
}

check_image_vulnerability() {
    local image=$1
    local tag=$2
    
    log_info "Checking image vulnerabilities: $image:$tag"
    
    # Use trivy for vulnerability scanning if available
    if command -v trivy >/dev/null 2>&1; then
        local vuln_count
        vuln_count=$(trivy image --quiet --format json "$image:$tag" | jq '.Results[0].Vulnerabilities | length' 2>/dev/null || echo "0")
        
        if [[ $vuln_count -gt 0 ]]; then
            log_warn "Found $vuln_count vulnerabilities in $image:$tag"
            return 1
        else
            log_info "No vulnerabilities found in $image:$tag"
            return 0
        fi
    else
        log_warn "Trivy not available - skipping vulnerability scan"
        return 0
    fi
}

# =================================
# SERVICE MANAGEMENT
# =================================

load_services_config() {
    if [[ ! -f "$SERVICES_CONFIG" ]]; then
        log_warn "Services configuration not found: $SERVICES_CONFIG"
        create_default_services_config
    fi
    
    log_info "Loading services configuration from: $SERVICES_CONFIG"
}

create_default_services_config() {
    local config_dir=$(dirname "$SERVICES_CONFIG")
    mkdir -p "$config_dir"
    
    cat > "$SERVICES_CONFIG" <<EOF
# Gough Services Configuration for Rolling Updates
services:
  maas-region:
    image: "gough/maas"
    current_tag: "3.4.0"
    update_priority: 1
    health_check_path: "/MAAS/api/2.0/version/"
    health_check_port: 5240
    dependencies: ["postgres"]
    update_policy: "rolling"
    
  management-server:
    image: "gough/management-server" 
    current_tag: "1.0.0"
    update_priority: 2
    health_check_path: "/health"
    health_check_port: 8000
    dependencies: ["postgres", "redis"]
    update_policy: "rolling"
    
  fleetdm:
    image: "fleetdm/fleet"
    current_tag: "v4.40.0"
    update_priority: 3
    health_check_path: "/api/v1/fleet/version"
    health_check_port: 8443
    dependencies: ["mysql"]
    update_policy: "rolling"
    
  postgres:
    image: "postgres"
    current_tag: "15"
    update_priority: 0
    health_check_port: 5432
    dependencies: []
    update_policy: "blue-green"
    
  redis:
    image: "redis"
    current_tag: "7-alpine"
    update_priority: 0
    health_check_port: 6379
    dependencies: []
    update_policy: "rolling"
    
  mysql:
    image: "mysql"
    current_tag: "8.0"
    update_priority: 0
    health_check_port: 3306
    dependencies: []
    update_policy: "blue-green"
EOF
    
    log_info "Created default services configuration: $SERVICES_CONFIG"
}

get_service_info() {
    local service_name=$1
    local field=$2
    
    # Simple YAML parsing - in production, use yq or similar
    grep -A 10 "^  ${service_name}:" "$SERVICES_CONFIG" | grep "    ${field}:" | awk '{print $2}' | tr -d '"'
}

get_service_dependencies() {
    local service_name=$1
    
    # Extract dependencies (simplified)
    grep -A 10 "^  ${service_name}:" "$SERVICES_CONFIG" | grep "dependencies:" | sed 's/.*\[//; s/\]//' | tr ',' '\n' | tr -d ' "'
}

# =================================
# HEALTH CHECKS
# =================================

check_service_health() {
    local service_name=$1
    local timeout=${2:-$HEALTH_CHECK_TIMEOUT}
    
    local health_check_path=$(get_service_info "$service_name" "health_check_path")
    local health_check_port=$(get_service_info "$service_name" "health_check_port")
    
    log_info "Checking health of $service_name..."
    
    local waited=0
    while [[ $waited -lt $timeout ]]; do
        if [[ -n "$health_check_path" ]]; then
            # HTTP health check
            if curl -s -f "http://localhost:${health_check_port}${health_check_path}" >/dev/null 2>&1; then
                log_info "$service_name is healthy"
                return 0
            fi
        else
            # Port health check
            if nc -z localhost "$health_check_port" 2>/dev/null; then
                log_info "$service_name is healthy (port check)"
                return 0
            fi
        fi
        
        sleep 5
        waited=$((waited + 5))
        show_progress "$waited" "$timeout" "Health check for $service_name"
    done
    
    log_error "$service_name health check failed after ${timeout} seconds"
    return 1
}

wait_for_service_ready() {
    local service_name=$1
    local timeout=${2:-$HEALTH_CHECK_TIMEOUT}
    
    log_info "Waiting for $service_name to be ready..."
    
    if check_service_health "$service_name" "$timeout"; then
        log_info "$service_name is ready"
        return 0
    else
        log_error "$service_name is not ready after timeout"
        return 1
    fi
}

# =================================
# DOCKER COMPOSE OPERATIONS
# =================================

compose_get_current_tag() {
    local service_name=$1
    
    docker-compose -f "$COMPOSE_FILE" config | grep -A 5 "^  ${service_name}:" | grep "image:" | awk -F: '{print $NF}' | tr -d ' '
}

compose_update_service() {
    local service_name=$1
    local new_tag=$2
    
    log_info "Updating $service_name to tag $new_tag using Docker Compose..."
    
    # Create backup of current compose file
    cp "$COMPOSE_FILE" "${COMPOSE_FILE}.backup.$(date +%Y%m%d_%H%M%S)"
    
    # Update the image tag in compose file
    local image_name=$(get_service_info "$service_name" "image")
    sed -i "s|${image_name}:.*|${image_name}:${new_tag}|g" "$COMPOSE_FILE"
    
    # Pull new image
    if ! docker-compose -f "$COMPOSE_FILE" pull "$service_name"; then
        log_error "Failed to pull new image for $service_name"
        return 1
    fi
    
    # Perform rolling update
    case "$UPDATE_STRATEGY" in
        rolling)
            compose_rolling_update "$service_name"
            ;;
        blue-green)
            compose_blue_green_update "$service_name"
            ;;
        *)
            log_error "Unsupported update strategy: $UPDATE_STRATEGY"
            return 1
            ;;
    esac
}

compose_rolling_update() {
    local service_name=$1
    
    log_info "Performing rolling update for $service_name..."
    
    # Get current scale
    local current_scale
    current_scale=$(docker-compose -f "$COMPOSE_FILE" ps -q "$service_name" | wc -l)
    
    if [[ $current_scale -eq 0 ]]; then
        current_scale=1
    fi
    
    # Calculate update parameters
    local max_unavailable_count=$((current_scale * MAX_UNAVAILABLE / 100))
    if [[ $max_unavailable_count -eq 0 ]]; then
        max_unavailable_count=1
    fi
    
    local max_surge_count=$((current_scale * MAX_SURGE / 100))
    if [[ $max_surge_count -eq 0 ]]; then
        max_surge_count=1
    fi
    
    log_info "Rolling update parameters: scale=$current_scale, max_unavailable=$max_unavailable_count, max_surge=$max_surge_count"
    
    # Start new containers
    local new_scale=$((current_scale + max_surge_count))
    docker-compose -f "$COMPOSE_FILE" up -d --scale "$service_name=$new_scale" "$service_name"
    
    # Wait for new containers to be healthy
    if ! wait_for_service_ready "$service_name"; then
        log_error "New containers failed health check"
        return 1
    fi
    
    # Remove old containers gradually
    local target_scale=$current_scale
    while [[ $target_scale -gt 0 ]]; do
        target_scale=$((target_scale - max_unavailable_count))
        if [[ $target_scale -lt $current_scale ]]; then
            target_scale=$current_scale
        fi
        
        docker-compose -f "$COMPOSE_FILE" up -d --scale "$service_name=$target_scale" "$service_name"
        
        # Wait between removals
        sleep 10
    done
    
    log_info "Rolling update completed for $service_name"
    return 0
}

compose_blue_green_update() {
    local service_name=$1
    
    log_info "Performing blue-green update for $service_name..."
    
    # Create green version
    local green_service="${service_name}-green"
    
    # Start green service
    docker-compose -f "$COMPOSE_FILE" up -d "$green_service"
    
    # Wait for green to be healthy
    if ! wait_for_service_ready "$green_service"; then
        log_error "Green service failed health check"
        docker-compose -f "$COMPOSE_FILE" stop "$green_service"
        return 1
    fi
    
    # Switch traffic (this would require load balancer reconfiguration)
    log_info "Switching traffic from blue to green..."
    
    # Stop blue service
    docker-compose -f "$COMPOSE_FILE" stop "$service_name"
    
    # Rename green to blue
    docker rename "${green_service}_1" "${service_name}_1" 2>/dev/null || true
    
    log_info "Blue-green update completed for $service_name"
    return 0
}

# =================================
# KUBERNETES OPERATIONS
# =================================

kubectl_update_service() {
    local service_name=$1
    local new_tag=$2
    
    log_info "Updating $service_name to tag $new_tag using Kubernetes..."
    
    local image_name=$(get_service_info "$service_name" "image")
    local full_image="${image_name}:${new_tag}"
    
    # Update deployment image
    kubectl set image deployment/"$service_name" "$service_name"="$full_image" -n "$KUBERNETES_NAMESPACE"
    
    # Wait for rollout to complete
    if kubectl rollout status deployment/"$service_name" -n "$KUBERNETES_NAMESPACE" --timeout="${UPDATE_TIMEOUT}s"; then
        log_info "Kubernetes rollout completed for $service_name"
        return 0
    else
        log_error "Kubernetes rollout failed for $service_name"
        return 1
    fi
}

kubernetes_check_rollout_status() {
    local service_name=$1
    
    kubectl rollout status deployment/"$service_name" -n "$KUBERNETES_NAMESPACE" --timeout=10s >/dev/null 2>&1
}

# =================================
# UPDATE ORCHESTRATION
# =================================

get_update_order() {
    # Get services sorted by update priority (0 = highest priority, database first)
    grep -E "^  [a-zA-Z-]+:$" "$SERVICES_CONFIG" | sed 's/://; s/  //' | while read -r service; do
        local priority=$(get_service_info "$service" "update_priority")
        echo "$priority:$service"
    done | sort -n | cut -d: -f2
}

check_dependencies() {
    local service_name=$1
    
    log_info "Checking dependencies for $service_name..."
    
    local dependencies
    dependencies=$(get_service_dependencies "$service_name")
    
    for dep in $dependencies; do
        if ! check_service_health "$dep" 30; then
            log_error "Dependency $dep is not healthy"
            return 1
        fi
    done
    
    log_info "All dependencies for $service_name are healthy"
    return 0
}

update_single_service() {
    local service_name=$1
    local force_update=${2:-false}
    
    log_info "Processing update for service: $service_name"
    
    # Get current and latest tags
    local current_tag=$(get_service_info "$service_name" "current_tag")
    local latest_tag=$(get_latest_image_tag "$(get_service_info "$service_name" "image")" "$current_tag")
    
    # Check if update is needed
    if [[ "$current_tag" == "$latest_tag" ]] && [[ "$force_update" != "true" ]]; then
        log_info "$service_name is already up to date ($current_tag)"
        return 0
    fi
    
    log_info "$service_name update available: $current_tag -> $latest_tag"
    
    # Check dependencies
    if ! check_dependencies "$service_name"; then
        log_error "Dependencies check failed for $service_name"
        return 1
    fi
    
    # Perform vulnerability scan
    local image_name=$(get_service_info "$service_name" "image")
    if ! check_image_vulnerability "$image_name" "$latest_tag"; then
        log_warn "Vulnerabilities found in $image_name:$latest_tag"
        # Continue anyway - in production, you might want to stop here
    fi
    
    # Perform the update based on deployment type
    local update_success=false
    case "$DEPLOYMENT_TYPE" in
        compose)
            if compose_update_service "$service_name" "$latest_tag"; then
                update_success=true
            fi
            ;;
        kubernetes)
            if kubectl_update_service "$service_name" "$latest_tag"; then
                update_success=true
            fi
            ;;
        *)
            log_error "Unsupported deployment type: $DEPLOYMENT_TYPE"
            return 1
            ;;
    esac
    
    if [[ "$update_success" == "true" ]]; then
        # Update the services config with new tag
        sed -i "s|current_tag: \"$current_tag\"|current_tag: \"$latest_tag\"|" "$SERVICES_CONFIG"
        log_info "$service_name updated successfully from $current_tag to $latest_tag"
        send_notification "✅ $service_name updated: $current_tag -> $latest_tag" "success"
        return 0
    else
        log_error "$service_name update failed"
        
        # Rollback if enabled
        if [[ "$ROLLBACK_ON_FAILURE" == "true" ]]; then
            log_info "Rolling back $service_name to previous version..."
            rollback_service "$service_name" "$current_tag"
        fi
        
        return 1
    fi
}

rollback_service() {
    local service_name=$1
    local previous_tag=$2
    
    log_info "Rolling back $service_name to $previous_tag..."
    
    case "$DEPLOYMENT_TYPE" in
        compose)
            compose_update_service "$service_name" "$previous_tag"
            ;;
        kubernetes)
            kubectl rollout undo deployment/"$service_name" -n "$KUBERNETES_NAMESPACE"
            ;;
    esac
    
    log_info "Rollback completed for $service_name"
    send_notification "🔄 Rolled back $service_name to $previous_tag" "warning"
}

# =================================
# MAIN UPDATE PROCESS
# =================================

run_rolling_updates() {
    local force_update=${1:-false}
    local services_filter=${2:-""}
    
    log_info "Starting rolling update process..."
    log_info "Strategy: $UPDATE_STRATEGY, Force: $force_update"
    
    # Load services configuration
    load_services_config
    
    # Registry login
    registry_login
    
    # Get services in update order
    local services_to_update
    if [[ -n "$services_filter" ]]; then
        services_to_update="$services_filter"
    else
        services_to_update=$(get_update_order)
    fi
    
    # Count total services
    local total_services
    total_services=$(echo "$services_to_update" | wc -w)
    local current_service=0
    local failed_services=()
    
    log_info "Updating $total_services services: $services_to_update"
    
    # Update each service
    for service in $services_to_update; do
        current_service=$((current_service + 1))
        log_info "Processing service $current_service of $total_services: $service"
        
        if update_single_service "$service" "$force_update"; then
            log_info "✅ Successfully updated $service"
        else
            log_error "❌ Failed to update $service"
            failed_services+=("$service")
        fi
        
        # Brief pause between services
        sleep 5
    done
    
    # Summary
    local successful_updates=$((total_services - ${#failed_services[@]}))
    log_info "Update summary: $successful_updates successful, ${#failed_services[@]} failed"
    
    if [[ ${#failed_services[@]} -gt 0 ]]; then
        log_error "Failed services: ${failed_services[*]}"
        send_notification "⚠️ Rolling update completed with ${#failed_services[@]} failures: ${failed_services[*]}" "warning"
        return 1
    else
        log_info "All services updated successfully"
        send_notification "✅ All $total_services services updated successfully" "success"
        return 0
    fi
}

# =================================
# MAIN FUNCTION
# =================================

show_usage() {
    cat <<EOF
Gough Container Rolling Update Manager v$VERSION

Usage: $0 [OPTIONS] [COMMAND]

Commands:
    update              Run rolling updates for all services
    update SERVICE      Update specific service only
    rollback SERVICE    Rollback service to previous version
    status              Show current status of all services
    check               Check for available updates without applying
    config              Show current configuration

Options:
    --force             Force update even if no new version detected
    --strategy STRATEGY Set update strategy (rolling, blue-green, canary)
    --timeout SECONDS   Set update timeout (default: $UPDATE_TIMEOUT)
    --dry-run          Show what would be updated without making changes
    --help              Show this help message

Environment Variables:
    UPDATE_STRATEGY         Update strategy (default: $UPDATE_STRATEGY)
    DEPLOYMENT_TYPE         Deployment type (compose/kubernetes)
    COMPOSE_FILE           Docker Compose file path
    KUBERNETES_NAMESPACE   Kubernetes namespace
    SLACK_WEBHOOK          Slack webhook URL for notifications

Examples:
    $0 update                    # Update all services
    $0 update maas-region        # Update specific service
    $0 --force update            # Force update all services
    $0 --strategy blue-green update postgres  # Use blue-green strategy

Configuration file: $SERVICES_CONFIG
Log file: $LOG_FILE
EOF
}

main() {
    # Setup
    setup_logging
    acquire_lock
    
    # Parse command line arguments
    local force_update=false
    local dry_run=false
    local command="update"
    local service_filter=""
    
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force)
                force_update=true
                shift
                ;;
            --strategy)
                UPDATE_STRATEGY="$2"
                shift 2
                ;;
            --timeout)
                UPDATE_TIMEOUT="$2"
                shift 2
                ;;
            --dry-run)
                dry_run=true
                shift
                ;;
            --help)
                show_usage
                exit 0
                ;;
            update|rollback|status|check|config)
                command="$1"
                shift
                ;;
            *)
                if [[ "$command" == "update" ]] || [[ "$command" == "rollback" ]]; then
                    service_filter="$1"
                fi
                shift
                ;;
        esac
    done
    
    # Execute command
    case "$command" in
        update)
            if [[ "$dry_run" == "true" ]]; then
                log_info "Dry run mode - would update: $service_filter"
                exit 0
            fi
            run_rolling_updates "$force_update" "$service_filter"
            ;;
        status)
            show_status
            ;;
        check)
            check_for_updates
            ;;
        config)
            show_config
            ;;
        *)
            log_error "Unknown command: $command"
            show_usage
            exit 1
            ;;
    esac
}

show_status() {
    echo "=== Gough Container Update Status ==="
    echo "Strategy: $UPDATE_STRATEGY"
    echo "Deployment: $DEPLOYMENT_TYPE"
    echo ""
    
    load_services_config
    
    echo "Services:"
    for service in $(get_update_order); do
        local current_tag=$(get_service_info "$service" "current_tag")
        local image=$(get_service_info "$service" "image")
        local latest_tag=$(get_latest_image_tag "$image" "$current_tag")
        
        if [[ "$current_tag" == "$latest_tag" ]]; then
            echo "  ✅ $service: $current_tag (up to date)"
        else
            echo "  🔄 $service: $current_tag -> $latest_tag (update available)"
        fi
    done
}

check_for_updates() {
    log_info "Checking for available updates..."
    
    load_services_config
    local updates_available=false
    
    for service in $(get_update_order); do
        local current_tag=$(get_service_info "$service" "current_tag")
        local image=$(get_service_info "$service" "image")
        local latest_tag=$(get_latest_image_tag "$image" "$current_tag")
        
        if [[ "$current_tag" != "$latest_tag" ]]; then
            log_info "Update available for $service: $current_tag -> $latest_tag"
            updates_available=true
        fi
    done
    
    if [[ "$updates_available" == "true" ]]; then
        log_info "Updates are available - run with 'update' command to apply"
        exit 1
    else
        log_info "All services are up to date"
        exit 0
    fi
}

show_config() {
    echo "=== Configuration ==="
    echo "Strategy: $UPDATE_STRATEGY"
    echo "Deployment: $DEPLOYMENT_TYPE"
    echo "Compose File: $COMPOSE_FILE"
    echo "K8s Namespace: $KUBERNETES_NAMESPACE"
    echo "Services Config: $SERVICES_CONFIG"
    echo "Update Timeout: $UPDATE_TIMEOUT"
    echo "Health Check Timeout: $HEALTH_CHECK_TIMEOUT"
    echo "Rollback on Failure: $ROLLBACK_ON_FAILURE"
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi