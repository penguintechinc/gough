#!/bin/bash

# Ubuntu Image Update Manager for Gough Hypervisor
# Automated Ubuntu image management with version control and testing
# Supports 24.04 LTS and rolling updates for enterprise deployments

set -euo pipefail

# =================================
# CONFIGURATION VARIABLES
# =================================

# Script configuration
SCRIPT_NAME="ubuntu-image-updater"
VERSION="1.0.0"
LOG_FILE="/var/log/gough/${SCRIPT_NAME}.log"
LOCK_FILE="/var/lock/${SCRIPT_NAME}.lock"

# Ubuntu release configuration
UBUNTU_RELEASES=("24.04" "23.10" "23.04")
DEFAULT_RELEASE="24.04"
LTS_RELEASES=("24.04" "22.04" "20.04")

# MaaS configuration
MAAS_URL="${MAAS_URL:-http://localhost:5240/MAAS}"
MAAS_API_KEY="${MAAS_API_KEY:-}"
MAAS_API_VERSION="${MAAS_API_VERSION:-2.0}"

# Storage configuration
IMAGE_STORE_PATH="${IMAGE_STORE_PATH:-/var/lib/maas/images}"
BACKUP_PATH="${BACKUP_PATH:-/var/lib/maas/image-backups}"
TEMP_PATH="${TEMP_PATH:-/tmp/maas-image-updates}"

# Notification configuration
SLACK_WEBHOOK="${SLACK_WEBHOOK:-}"
EMAIL_RECIPIENTS="${EMAIL_RECIPIENTS:-ops@gough.local}"

# Update policy configuration
AUTO_IMPORT_SECURITY_UPDATES=${AUTO_IMPORT_SECURITY_UPDATES:-true}
AUTO_IMPORT_LTS_UPDATES=${AUTO_IMPORT_LTS_UPDATES:-true}
AUTO_IMPORT_NON_LTS_UPDATES=${AUTO_IMPORT_NON_LTS_UPDATES:-false}
RETENTION_DAYS=${RETENTION_DAYS:-90}
MAX_VERSIONS_PER_RELEASE=${MAX_VERSIONS_PER_RELEASE:-5}

# Testing configuration
ENABLE_IMAGE_TESTING=${ENABLE_IMAGE_TESTING:-true}
TEST_TIMEOUT=${TEST_TIMEOUT:-1800}  # 30 minutes
TEST_NODE_TAG=${TEST_NODE_TAG:-image-test}

# =================================
# LOGGING AND UTILITIES
# =================================

# Setup logging
setup_logging() {
    mkdir -p "$(dirname "$LOG_FILE")"
    exec 1> >(tee -a "$LOG_FILE")
    exec 2>&1
}

# Logging functions
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

# Progress indicator
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
# LOCK AND CLEANUP MANAGEMENT
# =================================

acquire_lock() {
    if ! (set -C; echo $$ > "$LOCK_FILE") 2>/dev/null; then
        local pid
        pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")
        log_fatal "Script is already running (PID: $pid). Lock file: $LOCK_FILE"
    fi
    trap cleanup_and_exit EXIT
}

cleanup_and_exit() {
    local exit_code=${1:-0}
    
    # Remove lock file
    if [[ -f "$LOCK_FILE" ]]; then
        rm -f "$LOCK_FILE"
    fi
    
    # Cleanup temporary files
    if [[ -d "$TEMP_PATH" ]]; then
        rm -rf "$TEMP_PATH"
    fi
    
    # Final log message
    if [[ $exit_code -eq 0 ]]; then
        log_info "Ubuntu image update process completed successfully"
        send_notification "✅ Ubuntu image update completed successfully" "success"
    else
        log_error "Ubuntu image update process failed with exit code $exit_code"
        send_notification "❌ Ubuntu image update failed" "error"
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
    "channel": "#gough-infrastructure",
    "username": "Gough Image Updater",
    "icon_emoji": "$icon",
    "attachments": [
        {
            "color": "$color",
            "title": "Ubuntu Image Update Notification",
            "text": "$message",
            "fields": [
                {
                    "title": "Server",
                    "value": "$(hostname)",
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
    
    local subject="Gough Ubuntu Image Update - $type"
    
    local body="
Ubuntu Image Update Notification

Message: $message
Server: $(hostname)
Timestamp: $timestamp
Log File: $LOG_FILE

This is an automated notification from the Gough Hypervisor image management system.
"
    
    # Send email using mail command if available
    if command -v mail >/dev/null 2>&1; then
        echo "$body" | mail -s "$subject" "$EMAIL_RECIPIENTS"
    fi
}

# =================================
# MAAS API INTERACTION
# =================================

maas_api_call() {
    local method=$1
    local endpoint=$2
    local data=${3:-}
    
    local url="${MAAS_URL}/api/${MAAS_API_VERSION}/${endpoint}"
    local auth_header="Authorization: OAuth oauth_consumer_key=\"$(echo "$MAAS_API_KEY" | cut -d: -f1)\", oauth_token=\"$(echo "$MAAS_API_KEY" | cut -d: -f2)\", oauth_signature_method=\"PLAINTEXT\", oauth_signature=\"%26$(echo "$MAAS_API_KEY" | cut -d: -f3)\""
    
    if [[ -n "$data" ]]; then
        curl -s -X "$method" \
             -H "$auth_header" \
             -H "Content-Type: application/x-www-form-urlencoded" \
             -d "$data" \
             "$url"
    else
        curl -s -X "$method" \
             -H "$auth_header" \
             "$url"
    fi
}

get_available_images() {
    local release=$1
    maas_api_call "GET" "boot-sources/" | jq -r '.[] | select(.url | contains("images.maas.io")) | .id'
}

get_imported_images() {
    maas_api_call "GET" "boot-resources/" | jq -r '.[] | select(.rtype == "synced") | "\(.name)|\(.architecture)|\(.subarches)"'
}

import_image() {
    local release=$1
    local architecture=${2:-amd64}
    local subarchitecture=${3:-generic}
    
    log_info "Importing Ubuntu $release/$architecture/$subarchitecture image..."
    
    local data="name=ubuntu%2F$release&architecture=$architecture&subarchitecture=$subarchitecture"
    local result=$(maas_api_call "POST" "boot-resources/" "$data")
    
    if echo "$result" | jq -e '.id' >/dev/null; then
        log_info "Successfully initiated import for Ubuntu $release/$architecture"
        return 0
    else
        log_error "Failed to import Ubuntu $release/$architecture: $result"
        return 1
    fi
}

delete_old_image() {
    local image_id=$1
    local image_name=$2
    
    log_info "Deleting old image: $image_name (ID: $image_id)"
    
    local result=$(maas_api_call "DELETE" "boot-resources/$image_id/")
    
    if [[ $? -eq 0 ]]; then
        log_info "Successfully deleted image $image_name"
        return 0
    else
        log_error "Failed to delete image $image_name"
        return 1
    fi
}

wait_for_import_completion() {
    local max_wait=${1:-3600}  # 1 hour default
    local check_interval=30
    local waited=0
    
    log_info "Waiting for image import to complete (max wait: ${max_wait}s)..."
    
    while [[ $waited -lt $max_wait ]]; do
        local syncing_count=$(maas_api_call "GET" "boot-resources/" | jq '[.[] | select(.rtype == "syncing")] | length')
        
        if [[ "$syncing_count" == "0" ]]; then
            log_info "All image imports completed"
            return 0
        fi
        
        show_progress "$waited" "$max_wait" "Waiting for import completion"
        sleep "$check_interval"
        waited=$((waited + check_interval))
    done
    
    log_warn "Image import did not complete within ${max_wait} seconds"
    return 1
}

# =================================
# IMAGE MANAGEMENT FUNCTIONS
# =================================

check_for_updates() {
    local release=$1
    
    log_info "Checking for updates for Ubuntu $release..."
    
    # Create temporary directory for update checking
    mkdir -p "$TEMP_PATH"
    
    # Download current release information
    local release_info_url="http://cloud-images.ubuntu.com/$release/current/unpacked/"
    local local_info_file="$TEMP_PATH/${release}-current.info"
    
    # Get current build information
    if ! curl -s "$release_info_url" -o "$local_info_file"; then
        log_warn "Could not retrieve update information for Ubuntu $release"
        return 1
    fi
    
    # Extract build date and version information
    local remote_build_date
    remote_build_date=$(grep -o '[0-9]\{8\}' "$local_info_file" | head -1 || echo "unknown")
    
    # Get currently imported version
    local current_images
    current_images=$(get_imported_images | grep "ubuntu/$release")
    
    if [[ -z "$current_images" ]]; then
        log_info "No current images found for Ubuntu $release - will import latest"
        return 0  # Trigger import
    fi
    
    # Compare versions (simplified - in production, you'd want more sophisticated version comparison)
    local needs_update=false
    
    # Check if this is a security update day (simplified logic)
    local today=$(date +%u)  # Day of week (1=Monday, 7=Sunday)
    if [[ "$today" == "2" ]] && [[ "$AUTO_IMPORT_SECURITY_UPDATES" == "true" ]]; then
        log_info "Security update day detected - checking for security updates"
        needs_update=true
    fi
    
    # Check if this is an LTS release and LTS updates are enabled
    if printf '%s\n' "${LTS_RELEASES[@]}" | grep -q "^$release$"; then
        if [[ "$AUTO_IMPORT_LTS_UPDATES" == "true" ]]; then
            log_info "LTS release detected - checking for updates"
            needs_update=true
        fi
    else
        if [[ "$AUTO_IMPORT_NON_LTS_UPDATES" == "true" ]]; then
            log_info "Non-LTS release detected - checking for updates"
            needs_update=true
        fi
    fi
    
    if [[ "$needs_update" == "true" ]]; then
        log_info "Updates available for Ubuntu $release"
        return 0
    else
        log_info "No updates needed for Ubuntu $release"
        return 1
    fi
}

backup_current_images() {
    local release=$1
    
    log_info "Creating backup of current images for Ubuntu $release..."
    
    # Create backup directory
    mkdir -p "$BACKUP_PATH/$release"
    
    # Create timestamp for backup
    local backup_timestamp=$(date +'%Y%m%d_%H%M%S')
    local backup_dir="$BACKUP_PATH/$release/$backup_timestamp"
    
    mkdir -p "$backup_dir"
    
    # Export current image metadata
    get_imported_images | grep "ubuntu/$release" > "$backup_dir/image_list.txt"
    
    # Create backup manifest
    cat > "$backup_dir/backup_manifest.json" <<EOF
{
    "timestamp": "$(date -Iseconds)",
    "release": "$release",
    "backup_type": "image_metadata",
    "created_by": "$SCRIPT_NAME",
    "version": "$VERSION",
    "hostname": "$(hostname)"
}
EOF
    
    log_info "Backup created: $backup_dir"
}

test_image() {
    local release=$1
    local architecture=${2:-amd64}
    
    if [[ "$ENABLE_IMAGE_TESTING" != "true" ]]; then
        log_info "Image testing disabled - skipping test"
        return 0
    fi
    
    log_info "Testing Ubuntu $release/$architecture image..."
    
    # Find test node with appropriate tag
    local test_nodes
    test_nodes=$(maas_api_call "GET" "machines/" | jq -r ".[] | select(.tag_names[] == \"$TEST_NODE_TAG\") | .system_id")
    
    if [[ -z "$test_nodes" ]]; then
        log_warn "No test nodes available with tag '$TEST_NODE_TAG' - skipping image test"
        return 0
    fi
    
    # Get first available test node
    local test_node
    test_node=$(echo "$test_nodes" | head -1)
    
    log_info "Using test node: $test_node"
    
    # Deploy image to test node
    local deploy_data="distro_series=$release&hwe_kernel=ga-${release}"
    local deploy_result
    deploy_result=$(maas_api_call "POST" "machines/$test_node/" "op=deploy&$deploy_data")
    
    if ! echo "$deploy_result" | jq -e '.status_name == "Deploying"' >/dev/null; then
        log_error "Failed to start deployment on test node"
        return 1
    fi
    
    # Wait for deployment completion
    local waited=0
    while [[ $waited -lt $TEST_TIMEOUT ]]; do
        local node_status
        node_status=$(maas_api_call "GET" "machines/$test_node/" | jq -r '.status_name')
        
        case "$node_status" in
            "Deployed")
                log_info "Image test deployment successful"
                # Release the test node
                maas_api_call "POST" "machines/$test_node/" "op=release" >/dev/null
                return 0
                ;;
            "Failed deployment")
                log_error "Image test deployment failed"
                # Release the test node
                maas_api_call "POST" "machines/$test_node/" "op=release" >/dev/null
                return 1
                ;;
            "Deploying")
                show_progress "$waited" "$TEST_TIMEOUT" "Testing image deployment"
                sleep 30
                waited=$((waited + 30))
                ;;
            *)
                log_warn "Unexpected node status during test: $node_status"
                sleep 30
                waited=$((waited + 30))
                ;;
        esac
    done
    
    log_error "Image test timed out after ${TEST_TIMEOUT} seconds"
    # Try to release the test node
    maas_api_call "POST" "machines/$test_node/" "op=release" >/dev/null || true
    return 1
}

cleanup_old_images() {
    local release=$1
    
    log_info "Cleaning up old images for Ubuntu $release..."
    
    # Get all images for this release, sorted by creation date
    local images
    images=$(maas_api_call "GET" "boot-resources/" | \
             jq -r ".[] | select(.name | startswith(\"ubuntu/$release\")) | \"\(.id)|\(.name)|\(.created)\"" | \
             sort -t'|' -k3,3r)
    
    local image_count=0
    while IFS='|' read -r image_id image_name created_date; do
        image_count=$((image_count + 1))
        
        # Keep the most recent images up to MAX_VERSIONS_PER_RELEASE
        if [[ $image_count -gt $MAX_VERSIONS_PER_RELEASE ]]; then
            delete_old_image "$image_id" "$image_name"
        fi
    done <<< "$images"
    
    # Also clean up based on retention policy
    local retention_date
    retention_date=$(date -d "$RETENTION_DAYS days ago" +'%Y-%m-%d')
    
    while IFS='|' read -r image_id image_name created_date; do
        local image_date
        image_date=$(echo "$created_date" | cut -d'T' -f1)
        
        if [[ "$image_date" < "$retention_date" ]]; then
            delete_old_image "$image_id" "$image_name"
        fi
    done <<< "$images"
}

# =================================
# MAIN UPDATE PROCESS
# =================================

update_ubuntu_images() {
    local release=$1
    
    log_info "Starting Ubuntu $release image update process..."
    
    # Check if updates are available
    if ! check_for_updates "$release"; then
        log_info "No updates available for Ubuntu $release"
        return 0
    fi
    
    # Backup current images
    backup_current_images "$release"
    
    # Import new image
    if ! import_image "$release"; then
        log_error "Failed to import new image for Ubuntu $release"
        return 1
    fi
    
    # Wait for import completion
    if ! wait_for_import_completion; then
        log_error "Image import did not complete successfully"
        return 1
    fi
    
    # Test new image
    if ! test_image "$release"; then
        log_error "New image failed testing - consider rolling back"
        send_notification "⚠️ Ubuntu $release image failed testing" "warning"
        return 1
    fi
    
    # Cleanup old images
    cleanup_old_images "$release"
    
    log_info "Successfully updated Ubuntu $release images"
    send_notification "✅ Ubuntu $release image updated successfully" "success"
}

# =================================
# VALIDATION AND PREREQUISITES
# =================================

validate_environment() {
    log_info "Validating environment..."
    
    # Check required commands
    local required_commands=("curl" "jq" "date" "grep" "sort")
    for cmd in "${required_commands[@]}"; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            log_fatal "Required command not found: $cmd"
        fi
    done
    
    # Check MaaS API configuration
    if [[ -z "$MAAS_API_KEY" ]]; then
        log_fatal "MAAS_API_KEY environment variable is required"
    fi
    
    # Test MaaS API connectivity
    if ! maas_api_call "GET" "version/" >/dev/null; then
        log_fatal "Cannot connect to MaaS API at $MAAS_URL"
    fi
    
    # Create required directories
    mkdir -p "$IMAGE_STORE_PATH" "$BACKUP_PATH" "$TEMP_PATH"
    
    # Check disk space
    local available_space
    available_space=$(df "$IMAGE_STORE_PATH" | awk 'NR==2 {print $4}')
    local required_space=$((10 * 1024 * 1024))  # 10GB in KB
    
    if [[ $available_space -lt $required_space ]]; then
        log_warn "Low disk space available: ${available_space}KB (recommended: ${required_space}KB)"
    fi
    
    log_info "Environment validation completed"
}

# =================================
# MAIN FUNCTION
# =================================

main() {
    log_info "Starting Ubuntu Image Updater v$VERSION"
    
    # Setup
    setup_logging
    acquire_lock
    validate_environment
    
    # Process command line arguments
    local releases_to_update=()
    local force_update=false
    
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --release)
                releases_to_update+=("$2")
                shift 2
                ;;
            --force)
                force_update=true
                shift
                ;;
            --help)
                show_usage
                exit 0
                ;;
            *)
                log_error "Unknown argument: $1"
                show_usage
                exit 1
                ;;
        esac
    done
    
    # Default to all configured releases if none specified
    if [[ ${#releases_to_update[@]} -eq 0 ]]; then
        releases_to_update=("${UBUNTU_RELEASES[@]}")
    fi
    
    # Update images for each specified release
    local failed_updates=0
    local total_releases=${#releases_to_update[@]}
    local current_release=0
    
    for release in "${releases_to_update[@]}"; do
        current_release=$((current_release + 1))
        log_info "Processing release $current_release of $total_releases: Ubuntu $release"
        
        if ! update_ubuntu_images "$release"; then
            log_error "Failed to update Ubuntu $release"
            failed_updates=$((failed_updates + 1))
        fi
    done
    
    # Summary
    local successful_updates=$((total_releases - failed_updates))
    log_info "Update summary: $successful_updates successful, $failed_updates failed"
    
    if [[ $failed_updates -gt 0 ]]; then
        send_notification "⚠️ Ubuntu image updates completed with $failed_updates failures" "warning"
        exit 1
    else
        send_notification "✅ All Ubuntu image updates completed successfully" "success"
        exit 0
    fi
}

show_usage() {
    cat <<EOF
Ubuntu Image Updater for Gough Hypervisor v$VERSION

Usage: $0 [OPTIONS]

Options:
    --release RELEASE    Update specific Ubuntu release (e.g., 24.04)
                        Can be specified multiple times
    --force             Force update even if no updates detected
    --help              Show this help message

Environment Variables:
    MAAS_URL            MaaS server URL (default: http://localhost:5240/MAAS)
    MAAS_API_KEY        MaaS API key (required)
    SLACK_WEBHOOK       Slack webhook URL for notifications
    EMAIL_RECIPIENTS    Email addresses for notifications

Examples:
    $0                           # Update all configured releases
    $0 --release 24.04           # Update only Ubuntu 24.04
    $0 --release 24.04 --force   # Force update Ubuntu 24.04
    
Configuration files:
    - Log file: $LOG_FILE
    - Backup directory: $BACKUP_PATH
    - Image store: $IMAGE_STORE_PATH

For more information, see the Gough documentation.
EOF
}

# =================================
# SCRIPT EXECUTION
# =================================

# Only run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi