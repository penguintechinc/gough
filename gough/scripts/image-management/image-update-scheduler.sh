#!/bin/bash

# Image Update Scheduler for Gough Hypervisor
# Cron job manager and automated scheduling for Ubuntu image updates
# Supports flexible scheduling with maintenance windows

set -euo pipefail

# =================================
# CONFIGURATION
# =================================

SCRIPT_NAME="image-update-scheduler"
LOG_FILE="/var/log/gough/${SCRIPT_NAME}.log"
CONFIG_FILE="/etc/gough/image-update-schedule.conf"
UBUNTU_UPDATER_SCRIPT="/opt/gough/scripts/image-management/ubuntu-image-updater.sh"

# Default scheduling configuration
DEFAULT_SCHEDULE="0 2 * * SUN"  # Every Sunday at 2 AM
SECURITY_SCHEDULE="0 3 * * TUE"  # Every Tuesday at 3 AM (Patch Tuesday + 1 day)
LTS_SCHEDULE="0 4 * * 1"         # Every Monday at 4 AM

# Maintenance window configuration
MAINTENANCE_START_HOUR=${MAINTENANCE_START_HOUR:-2}
MAINTENANCE_END_HOUR=${MAINTENANCE_END_HOUR:-6}
MAINTENANCE_DAY=${MAINTENANCE_DAY:-0}  # Sunday = 0

# =================================
# LOGGING FUNCTIONS
# =================================

log_info() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] INFO: $1" | tee -a "$LOG_FILE"
}

log_warn() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] WARN: $1" | tee -a "$LOG_FILE"
}

log_error() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $1" | tee -a "$LOG_FILE"
}

# =================================
# CONFIGURATION MANAGEMENT
# =================================

create_default_config() {
    local config_dir=$(dirname "$CONFIG_FILE")
    mkdir -p "$config_dir"
    
    cat > "$CONFIG_FILE" <<EOF
# Gough Image Update Schedule Configuration
# Configure automatic Ubuntu image updates

# Global settings
ENABLE_AUTOMATIC_UPDATES=true
MAINTENANCE_WINDOW_START=02:00
MAINTENANCE_WINDOW_END=06:00
MAINTENANCE_DAY=sunday
TIMEZONE=UTC

# Update policies
AUTO_IMPORT_SECURITY_UPDATES=true
AUTO_IMPORT_LTS_UPDATES=true
AUTO_IMPORT_NON_LTS_UPDATES=false

# Release-specific schedules (cron format)
# Format: RELEASE=SCHEDULE|ENABLED|DESCRIPTION
SCHEDULE_24_04="0 2 * * 0|true|Ubuntu 24.04 LTS weekly update"
SCHEDULE_23_10="0 3 * * 0|false|Ubuntu 23.10 weekly update (disabled)"
SCHEDULE_23_04="0 4 * * 0|false|Ubuntu 23.04 weekly update (disabled)"

# Security updates (separate schedule)
SECURITY_UPDATES_SCHEDULE="0 3 * * 2|true|Security updates - Patch Tuesday + 1"
SECURITY_RELEASES="24.04,22.04"

# Pre and post update hooks
PRE_UPDATE_HOOK="/opt/gough/hooks/pre-image-update.sh"
POST_UPDATE_HOOK="/opt/gough/hooks/post-image-update.sh"

# Notification settings
SLACK_WEBHOOK=""
EMAIL_RECIPIENTS="ops@gough.local"
NOTIFY_ON_SUCCESS=true
NOTIFY_ON_FAILURE=true

# Testing configuration
ENABLE_IMAGE_TESTING=true
TEST_TIMEOUT=1800
TEST_NODE_TAG="image-test"

# Cleanup settings
RETENTION_DAYS=90
MAX_VERSIONS_PER_RELEASE=5
CLEANUP_ENABLED=true
EOF
    
    log_info "Created default configuration file: $CONFIG_FILE"
}

load_config() {
    if [[ ! -f "$CONFIG_FILE" ]]; then
        log_warn "Configuration file not found, creating default: $CONFIG_FILE"
        create_default_config
    fi
    
    # Source the configuration file
    source "$CONFIG_FILE"
    
    log_info "Loaded configuration from: $CONFIG_FILE"
}

# =================================
# SCHEDULE MANAGEMENT
# =================================

install_cron_jobs() {
    log_info "Installing cron jobs for image updates..."
    
    # Create temporary cron file
    local temp_cron="/tmp/gough-image-updates.cron"
    
    # Add header
    cat > "$temp_cron" <<EOF
# Gough Hypervisor Image Update Schedule
# Automatically generated - do not edit manually
# Managed by: $SCRIPT_NAME

SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
MAILTO=${EMAIL_RECIPIENTS:-}

EOF
    
    # Add update schedules for each release
    local schedules=(
        "24.04:$SCHEDULE_24_04"
        "23.10:$SCHEDULE_23_10" 
        "23.04:$SCHEDULE_23_04"
    )
    
    for schedule_def in "${schedules[@]}"; do
        local release="${schedule_def%%:*}"
        local schedule_info="${schedule_def#*:}"
        
        # Parse schedule info
        IFS='|' read -r schedule enabled description <<< "$schedule_info"
        
        if [[ "$enabled" == "true" ]]; then
            cat >> "$temp_cron" <<EOF
# $description
$schedule root $UBUNTU_UPDATER_SCRIPT --release $release 2>&1 | logger -t gough-image-update

EOF
            log_info "Added cron job for Ubuntu $release: $schedule"
        else
            log_info "Skipping disabled schedule for Ubuntu $release"
        fi
    done
    
    # Add security update schedule if enabled
    if [[ "${AUTO_IMPORT_SECURITY_UPDATES:-false}" == "true" ]]; then
        IFS='|' read -r sec_schedule sec_enabled sec_description <<< "$SECURITY_UPDATES_SCHEDULE"
        
        if [[ "$sec_enabled" == "true" ]]; then
            local releases_param=""
            IFS=',' read -ra sec_releases <<< "$SECURITY_RELEASES"
            for rel in "${sec_releases[@]}"; do
                releases_param="$releases_param --release $rel"
            done
            
            cat >> "$temp_cron" <<EOF
# $sec_description
$sec_schedule root $UBUNTU_UPDATER_SCRIPT $releases_param --force 2>&1 | logger -t gough-security-update

EOF
            log_info "Added security update schedule: $sec_schedule"
        fi
    fi
    
    # Add cleanup job
    if [[ "${CLEANUP_ENABLED:-true}" == "true" ]]; then
        cat >> "$temp_cron" <<EOF
# Weekly cleanup of old images
0 1 * * 1 root $UBUNTU_UPDATER_SCRIPT --cleanup 2>&1 | logger -t gough-image-cleanup

EOF
        log_info "Added weekly cleanup schedule"
    fi
    
    # Install the cron file
    cp "$temp_cron" /etc/cron.d/gough-image-updates
    chmod 644 /etc/cron.d/gough-image-updates
    
    # Cleanup
    rm -f "$temp_cron"
    
    # Restart cron service to pick up changes
    if systemctl is-active --quiet cron; then
        systemctl reload cron
        log_info "Reloaded cron service"
    elif systemctl is-active --quiet crond; then
        systemctl reload crond
        log_info "Reloaded crond service"
    fi
    
    log_info "Cron jobs installed successfully"
}

remove_cron_jobs() {
    log_info "Removing Gough image update cron jobs..."
    
    if [[ -f /etc/cron.d/gough-image-updates ]]; then
        rm -f /etc/cron.d/gough-image-updates
        log_info "Removed cron file: /etc/cron.d/gough-image-updates"
        
        # Restart cron service
        if systemctl is-active --quiet cron; then
            systemctl reload cron
        elif systemctl is-active --quiet crond; then
            systemctl reload crond
        fi
    else
        log_info "No existing cron jobs found"
    fi
}

list_scheduled_jobs() {
    log_info "Current Gough image update schedule:"
    
    if [[ -f /etc/cron.d/gough-image-updates ]]; then
        echo "=== Scheduled Jobs ==="
        grep -v '^#' /etc/cron.d/gough-image-updates | grep -v '^$' | while read -r line; do
            echo "  $line"
        done
        echo "======================"
    else
        echo "No scheduled jobs found"
    fi
    
    # Show next run times
    echo ""
    echo "=== Next Run Times ==="
    if command -v cronitor >/dev/null 2>&1; then
        cronitor list --format table
    else
        echo "Install 'cronitor' package for detailed scheduling information"
    fi
    echo "======================"
}

# =================================
# MAINTENANCE WINDOW MANAGEMENT
# =================================

check_maintenance_window() {
    local current_hour=$(date +'%H')
    local current_day=$(date +'%w')  # 0 = Sunday
    local current_time=$(date +'%H:%M')
    
    # Convert maintenance window times to 24-hour format
    local maintenance_start=$(printf "%02d:00" "$MAINTENANCE_START_HOUR")
    local maintenance_end=$(printf "%02d:00" "$MAINTENANCE_END_HOUR")
    
    # Check if we're in the maintenance day and time window
    if [[ $current_day -eq $MAINTENANCE_DAY ]] && \
       [[ "$current_time" > "$maintenance_start" ]] && \
       [[ "$current_time" < "$maintenance_end" ]]; then
        return 0  # In maintenance window
    else
        return 1  # Outside maintenance window
    fi
}

wait_for_maintenance_window() {
    if check_maintenance_window; then
        log_info "Currently in maintenance window"
        return 0
    fi
    
    log_info "Waiting for next maintenance window..."
    
    # Calculate next maintenance window
    local next_maintenance
    local current_day=$(date +'%w')
    local days_until_maintenance
    
    if [[ $current_day -le $MAINTENANCE_DAY ]]; then
        days_until_maintenance=$((MAINTENANCE_DAY - current_day))
    else
        days_until_maintenance=$((7 - current_day + MAINTENANCE_DAY))
    fi
    
    next_maintenance=$(date -d "+${days_until_maintenance} days ${MAINTENANCE_START_HOUR}:00" +'%Y-%m-%d %H:%M:%S')
    log_info "Next maintenance window: $next_maintenance"
    
    # Wait until maintenance window
    local target_time=$(date -d "$next_maintenance" +%s)
    local current_time=$(date +%s)
    local wait_time=$((target_time - current_time))
    
    if [[ $wait_time -gt 0 ]]; then
        log_info "Waiting ${wait_time} seconds for maintenance window"
        sleep "$wait_time"
    fi
}

# =================================
# HOOK MANAGEMENT
# =================================

run_pre_update_hook() {
    if [[ -n "${PRE_UPDATE_HOOK:-}" ]] && [[ -x "$PRE_UPDATE_HOOK" ]]; then
        log_info "Running pre-update hook: $PRE_UPDATE_HOOK"
        
        if "$PRE_UPDATE_HOOK"; then
            log_info "Pre-update hook completed successfully"
            return 0
        else
            log_error "Pre-update hook failed"
            return 1
        fi
    else
        log_info "No pre-update hook configured or executable"
        return 0
    fi
}

run_post_update_hook() {
    local update_result=$1
    
    if [[ -n "${POST_UPDATE_HOOK:-}" ]] && [[ -x "$POST_UPDATE_HOOK" ]]; then
        log_info "Running post-update hook: $POST_UPDATE_HOOK"
        
        # Pass update result as environment variable
        export GOUGH_UPDATE_RESULT=$update_result
        
        if "$POST_UPDATE_HOOK"; then
            log_info "Post-update hook completed successfully"
            return 0
        else
            log_error "Post-update hook failed"
            return 1
        fi
    else
        log_info "No post-update hook configured or executable"
        return 0
    fi
}

# =================================
# MAIN FUNCTIONS
# =================================

run_scheduled_update() {
    log_info "Starting scheduled image update process..."
    
    # Load configuration
    load_config
    
    # Check if automatic updates are enabled
    if [[ "${ENABLE_AUTOMATIC_UPDATES:-false}" != "true" ]]; then
        log_info "Automatic updates are disabled"
        return 0
    fi
    
    # Wait for maintenance window if required
    if [[ "${REQUIRE_MAINTENANCE_WINDOW:-false}" == "true" ]]; then
        wait_for_maintenance_window
    fi
    
    # Run pre-update hook
    if ! run_pre_update_hook; then
        log_error "Pre-update hook failed - aborting update"
        return 1
    fi
    
    # Run the actual update
    local update_result=0
    if [[ -x "$UBUNTU_UPDATER_SCRIPT" ]]; then
        log_info "Running Ubuntu image updater..."
        if "$UBUNTU_UPDATER_SCRIPT"; then
            log_info "Image update completed successfully"
            update_result=0
        else
            log_error "Image update failed"
            update_result=1
        fi
    else
        log_error "Ubuntu updater script not found or not executable: $UBUNTU_UPDATER_SCRIPT"
        update_result=1
    fi
    
    # Run post-update hook
    run_post_update_hook $update_result
    
    return $update_result
}

show_status() {
    echo "=== Gough Image Update Scheduler Status ==="
    echo ""
    
    # Configuration status
    echo "Configuration:"
    echo "  Config file: $CONFIG_FILE"
    echo "  Updater script: $UBUNTU_UPDATER_SCRIPT"
    echo "  Log file: $LOG_FILE"
    echo ""
    
    # Load and show configuration
    if [[ -f "$CONFIG_FILE" ]]; then
        load_config
        echo "  Automatic updates: ${ENABLE_AUTOMATIC_UPDATES:-false}"
        echo "  Maintenance window: ${MAINTENANCE_WINDOW_START:-} - ${MAINTENANCE_WINDOW_END:-} on ${MAINTENANCE_DAY:-sunday}"
        echo "  Security updates: ${AUTO_IMPORT_SECURITY_UPDATES:-false}"
        echo "  LTS updates: ${AUTO_IMPORT_LTS_UPDATES:-false}"
    else
        echo "  Configuration not found"
    fi
    echo ""
    
    # Current time and maintenance window status
    echo "Current Status:"
    echo "  Current time: $(date)"
    echo "  Timezone: $(date +%Z)"
    if check_maintenance_window; then
        echo "  Maintenance window: ACTIVE"
    else
        echo "  Maintenance window: inactive"
    fi
    echo ""
    
    # Scheduled jobs
    list_scheduled_jobs
}

show_usage() {
    cat <<EOF
Gough Image Update Scheduler v1.0.0

Usage: $0 [COMMAND] [OPTIONS]

Commands:
    install         Install cron jobs for automatic updates
    remove          Remove all scheduled update jobs
    run             Run scheduled update process now
    status          Show scheduler status and configuration
    list            List all scheduled jobs
    test-config     Test configuration file validity

Options:
    --config FILE   Use specific configuration file
    --dry-run       Show what would be done without making changes
    --help          Show this help message

Configuration:
    Default config: $CONFIG_FILE
    Log file:      $LOG_FILE

Examples:
    $0 install              # Install automatic update schedule
    $0 run                  # Run update process immediately
    $0 status               # Show current status
    $0 remove               # Remove all scheduled jobs

For more information, see the Gough documentation.
EOF
}

# =================================
# MAIN EXECUTION
# =================================

main() {
    # Create log directory
    mkdir -p "$(dirname "$LOG_FILE")"
    
    local command=${1:-help}
    shift || true
    
    case "$command" in
        install)
            load_config
            install_cron_jobs
            ;;
        remove)
            remove_cron_jobs
            ;;
        run)
            run_scheduled_update
            ;;
        status)
            show_status
            ;;
        list)
            list_scheduled_jobs
            ;;
        test-config)
            if load_config; then
                echo "Configuration file is valid"
                exit 0
            else
                echo "Configuration file has errors"
                exit 1
            fi
            ;;
        help|--help|-h)
            show_usage
            ;;
        *)
            echo "Unknown command: $command"
            show_usage
            exit 1
            ;;
    esac
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi