#!/bin/bash

# Gough Log Rotation Setup Script
# Configures and manages automated log rotation for all Gough services
# Enterprise-grade log management with monitoring and alerting

set -euo pipefail

# =================================
# CONFIGURATION
# =================================

SCRIPT_NAME="setup-logrotate"
VERSION="1.0.0"
LOG_FILE="/var/log/gough/${SCRIPT_NAME}.log"

# Paths
GOUGH_LOGROTATE_CONFIG="/home/penguin/code/Gough/gough/config/logrotate/gough-logrotate.conf"
SYSTEM_LOGROTATE_DIR="/etc/logrotate.d"
GOUGH_LOGROTATE_FILE="$SYSTEM_LOGROTATE_DIR/gough"

# User and group configuration
GOUGH_USER="${GOUGH_USER:-gough}"
GOUGH_GROUP="${GOUGH_GROUP:-gough}"

# Notification configuration
SLACK_WEBHOOK="${SLACK_WEBHOOK:-}"
EMAIL_RECIPIENTS="${EMAIL_RECIPIENTS:-ops@gough.local}"

# =================================
# LOGGING
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
    exit 1
}

# =================================
# SYSTEM REQUIREMENTS
# =================================

check_requirements() {
    log_info "Checking system requirements..."
    
    # Check if running as root
    if [[ $EUID -ne 0 ]]; then
        log_fatal "This script must be run as root"
    fi
    
    # Check for logrotate
    if ! command -v logrotate >/dev/null 2>&1; then
        log_info "Installing logrotate..."
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update && apt-get install -y logrotate
        elif command -v yum >/dev/null 2>&1; then
            yum install -y logrotate
        else
            log_fatal "Cannot install logrotate - unsupported package manager"
        fi
    fi
    
    # Check logrotate version
    local logrotate_version
    logrotate_version=$(logrotate --version | head -1 | awk '{print $2}')
    log_info "Logrotate version: $logrotate_version"
    
    # Check for required directories
    if [[ ! -d "$SYSTEM_LOGROTATE_DIR" ]]; then
        log_fatal "System logrotate directory not found: $SYSTEM_LOGROTATE_DIR"
    fi
    
    # Check source configuration file
    if [[ ! -f "$GOUGH_LOGROTATE_CONFIG" ]]; then
        log_fatal "Gough logrotate configuration not found: $GOUGH_LOGROTATE_CONFIG"
    fi
    
    log_info "System requirements check completed"
}

# =================================
# USER AND DIRECTORY SETUP
# =================================

create_gough_user() {
    log_info "Setting up Gough system user..."
    
    # Create gough group if it doesn't exist
    if ! getent group "$GOUGH_GROUP" >/dev/null 2>&1; then
        log_info "Creating group: $GOUGH_GROUP"
        groupadd --system "$GOUGH_GROUP"
    fi
    
    # Create gough user if it doesn't exist
    if ! getent passwd "$GOUGH_USER" >/dev/null 2>&1; then
        log_info "Creating user: $GOUGH_USER"
        useradd --system --group "$GOUGH_GROUP" --home-dir /var/lib/gough --shell /bin/false "$GOUGH_USER"
    fi
    
    log_info "Gough user setup completed"
}

create_log_directories() {
    log_info "Creating log directories..."
    
    # Main Gough log directory
    mkdir -p /var/log/gough
    chown "$GOUGH_USER:$GOUGH_GROUP" /var/log/gough
    chmod 755 /var/log/gough
    
    # Service-specific log directories
    local log_dirs=(
        "/var/log/gough/management-server"
        "/var/log/gough/agents"
        "/var/log/gough/maintenance"
        "/var/log/gough/backups"
        "/var/log/gough/updates"
        "/var/log/haproxy"
        "/var/log/prometheus"
        "/var/log/alertmanager"
        "/var/log/grafana"
        "/var/log/elasticsearch"
        "/var/log/logstash"
        "/var/log/kibana"
        "/var/log/filebeat"
    )
    
    for dir in "${log_dirs[@]}"; do
        if [[ ! -d "$dir" ]]; then
            log_info "Creating directory: $dir"
            mkdir -p "$dir"
            chown "$GOUGH_USER:$GOUGH_GROUP" "$dir"
            chmod 755 "$dir"
        fi
    done
    
    log_info "Log directories created"
}

# =================================
# LOGROTATE CONFIGURATION
# =================================

install_logrotate_config() {
    log_info "Installing Gough logrotate configuration..."
    
    # Create backup of existing configuration if it exists
    if [[ -f "$GOUGH_LOGROTATE_FILE" ]]; then
        local backup_file="${GOUGH_LOGROTATE_FILE}.backup.$(date +%Y%m%d_%H%M%S)"
        log_info "Backing up existing configuration to: $backup_file"
        cp "$GOUGH_LOGROTATE_FILE" "$backup_file"
    fi
    
    # Copy new configuration
    log_info "Installing new logrotate configuration..."
    cp "$GOUGH_LOGROTATE_CONFIG" "$GOUGH_LOGROTATE_FILE"
    
    # Set proper permissions
    chmod 644 "$GOUGH_LOGROTATE_FILE"
    chown root:root "$GOUGH_LOGROTATE_FILE"
    
    # Validate configuration
    log_info "Validating logrotate configuration..."
    if logrotate -d "$GOUGH_LOGROTATE_FILE" >/dev/null 2>&1; then
        log_info "Logrotate configuration is valid"
    else
        log_error "Logrotate configuration validation failed"
        logrotate -d "$GOUGH_LOGROTATE_FILE"
        return 1
    fi
    
    log_info "Logrotate configuration installed successfully"
}

configure_logrotate_status() {
    log_info "Configuring logrotate status file..."
    
    # Ensure logrotate status directory exists
    local status_dir="/var/lib/logrotate"
    mkdir -p "$status_dir"
    
    # Create Gough-specific status file
    local gough_status_file="$status_dir/gough-status"
    touch "$gough_status_file"
    chown root:root "$gough_status_file"
    chmod 644 "$gough_status_file"
    
    log_info "Logrotate status configuration completed"
}

# =================================
# CRON INTEGRATION
# =================================

setup_logrotate_cron() {
    log_info "Setting up logrotate cron job..."
    
    # Check if logrotate cron already exists
    local cron_file="/etc/cron.d/gough-logrotate"
    
    cat > "$cron_file" <<EOF
# Gough Logrotate Cron Job
# Runs logrotate more frequently for high-volume production environment

SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
MAILTO=${EMAIL_RECIPIENTS:-}

# Run Gough logrotate every 4 hours (high volume environment)
0 */4 * * * root /usr/sbin/logrotate -s /var/lib/logrotate/gough-status /etc/logrotate.d/gough 2>&1 | logger -t gough-logrotate

# Daily full logrotate run
25 6 * * * root /usr/sbin/logrotate /etc/logrotate.conf 2>&1 | logger -t logrotate

# Weekly cleanup of very old compressed logs
0 3 * * 0 root find /var/log -name "*.gz" -mtime +90 -delete 2>&1 | logger -t gough-cleanup

# Emergency cleanup check (runs every hour)
15 * * * * root /opt/gough/scripts/log-management/emergency-cleanup.sh 2>&1 | logger -t gough-emergency
EOF
    
    # Set proper permissions
    chmod 644 "$cron_file"
    chown root:root "$cron_file"
    
    # Reload cron
    if systemctl is-active --quiet cron; then
        systemctl reload cron
        log_info "Reloaded cron service"
    elif systemctl is-active --quiet crond; then
        systemctl reload crond
        log_info "Reloaded crond service"
    fi
    
    log_info "Logrotate cron job configured"
}

# =================================
# EMERGENCY CLEANUP SCRIPT
# =================================

create_emergency_cleanup_script() {
    log_info "Creating emergency cleanup script..."
    
    local script_dir="/opt/gough/scripts/log-management"
    mkdir -p "$script_dir"
    
    local emergency_script="$script_dir/emergency-cleanup.sh"
    
    cat > "$emergency_script" <<'EOF'
#!/bin/bash

# Gough Emergency Log Cleanup Script
# Runs when disk space is critically low

set -euo pipefail

# Configuration
DISK_USAGE_CRITICAL=95
DISK_USAGE_WARNING=90
LOG_DIR="/var/log"
SLACK_WEBHOOK="${SLACK_WEBHOOK:-}"

# Get disk usage percentage
get_disk_usage() {
    df "$LOG_DIR" | awk 'NR==2 {print $5}' | sed 's/%//'
}

# Send notification
send_notification() {
    local message="$1"
    local level="$2"
    
    # Log to system log
    logger -t gough-emergency "$message"
    
    # Send Slack notification
    if [[ -n "$SLACK_WEBHOOK" ]]; then
        local color="warning"
        if [[ "$level" == "critical" ]]; then
            color="danger"
        fi
        
        curl -s -X POST "$SLACK_WEBHOOK" \
             -d '{"channel":"#gough-critical","text":"'"$message"'","color":"'"$color"'"}' \
             -H 'Content-type: application/json' || true
    fi
}

# Main cleanup function
emergency_cleanup() {
    local disk_usage=$(get_disk_usage)
    
    if [[ $disk_usage -lt $DISK_USAGE_WARNING ]]; then
        # Disk usage is normal, no action needed
        exit 0
    fi
    
    if [[ $disk_usage -ge $DISK_USAGE_WARNING ]] && [[ $disk_usage -lt $DISK_USAGE_CRITICAL ]]; then
        send_notification "⚠️ Disk usage warning: ${disk_usage}% on $(hostname)" "warning"
        
        # Compress large log files
        find "$LOG_DIR" -name "*.log" -size +50M -exec gzip {} \; 2>/dev/null || true
        
        # Remove old compressed logs (older than 30 days)
        find "$LOG_DIR" -name "*.gz" -mtime +30 -delete 2>/dev/null || true
        
    elif [[ $disk_usage -ge $DISK_USAGE_CRITICAL ]]; then
        send_notification "🚨 CRITICAL: Disk usage ${disk_usage}% on $(hostname) - emergency cleanup initiated!" "critical"
        
        # Aggressive cleanup
        # 1. Compress all log files immediately
        find "$LOG_DIR" -name "*.log" -size +10M -exec gzip {} \; 2>/dev/null || true
        
        # 2. Remove old compressed logs (older than 7 days)
        find "$LOG_DIR" -name "*.gz" -mtime +7 -delete 2>/dev/null || true
        
        # 3. Remove old rotated logs
        find "$LOG_DIR" -name "*.log.*" -mtime +3 -delete 2>/dev/null || true
        
        # 4. Truncate very large active log files (keep last 10MB)
        find "$LOG_DIR" -name "*.log" -size +100M -exec tail -c 10M {} \; -exec cp /dev/null {} \; 2>/dev/null || true
        
        # Check disk usage after cleanup
        local new_disk_usage=$(get_disk_usage)
        local cleaned_space=$((disk_usage - new_disk_usage))
        
        send_notification "✅ Emergency cleanup completed on $(hostname). Freed ${cleaned_space}% disk space. Usage now: ${new_disk_usage}%" "warning"
    fi
}

# Run cleanup
emergency_cleanup
EOF
    
    chmod +x "$emergency_script"
    chown root:root "$emergency_script"
    
    log_info "Emergency cleanup script created: $emergency_script"
}

# =================================
# MONITORING AND VALIDATION
# =================================

setup_logrotate_monitoring() {
    log_info "Setting up logrotate monitoring..."
    
    # Create monitoring script
    local monitor_script="/opt/gough/scripts/log-management/monitor-logrotate.sh"
    mkdir -p "$(dirname "$monitor_script")"
    
    cat > "$monitor_script" <<'EOF'
#!/bin/bash

# Gough Logrotate Monitoring Script
# Checks logrotate status and reports issues

set -euo pipefail

LOGROTATE_STATUS="/var/lib/logrotate/gough-status"
SLACK_WEBHOOK="${SLACK_WEBHOOK:-}"
LOG_DIR="/var/log"
MAX_LOG_AGE_HOURS=48

# Check if logrotate is working properly
check_logrotate_status() {
    if [[ ! -f "$LOGROTATE_STATUS" ]]; then
        echo "ERROR: Logrotate status file not found"
        return 1
    fi
    
    local status_age_hours
    status_age_hours=$((($(date +%s) - $(stat -c %Y "$LOGROTATE_STATUS")) / 3600))
    
    if [[ $status_age_hours -gt 25 ]]; then
        echo "WARNING: Logrotate status file is ${status_age_hours} hours old"
        return 1
    fi
    
    return 0
}

# Check for very old log files
check_log_ages() {
    local old_logs
    old_logs=$(find "$LOG_DIR" -name "*.log" -mtime +2 -size +100M 2>/dev/null || true)
    
    if [[ -n "$old_logs" ]]; then
        echo "WARNING: Found old large log files:"
        echo "$old_logs"
        return 1
    fi
    
    return 0
}

# Send notification
send_alert() {
    local message="$1"
    
    logger -t gough-logrotate-monitor "$message"
    
    if [[ -n "$SLACK_WEBHOOK" ]]; then
        curl -s -X POST "$SLACK_WEBHOOK" \
             -d '{"channel":"#gough-operations","text":"📄 Logrotate Monitor: '"$message"'"}' \
             -H 'Content-type: application/json' || true
    fi
}

# Main monitoring function
main() {
    local issues=()
    
    if ! check_logrotate_status; then
        issues+=("Logrotate status check failed")
    fi
    
    if ! check_log_ages; then
        issues+=("Old log files detected")
    fi
    
    if [[ ${#issues[@]} -gt 0 ]]; then
        local message="Logrotate issues detected on $(hostname): ${issues[*]}"
        send_alert "$message"
        exit 1
    else
        echo "Logrotate monitoring: All checks passed"
        exit 0
    fi
}

main "$@"
EOF
    
    chmod +x "$monitor_script"
    chown root:root "$monitor_script"
    
    # Add monitoring to cron
    local monitor_cron="/etc/cron.d/gough-logrotate-monitor"
    
    cat > "$monitor_cron" <<EOF
# Gough Logrotate Monitoring
# Checks logrotate health every 6 hours

0 */6 * * * root $monitor_script 2>&1 | logger -t gough-logrotate-monitor
EOF
    
    chmod 644 "$monitor_cron"
    chown root:root "$monitor_cron"
    
    log_info "Logrotate monitoring configured"
}

# =================================
# TESTING AND VALIDATION
# =================================

test_logrotate_config() {
    log_info "Testing logrotate configuration..."
    
    # Test dry run
    log_info "Running logrotate dry run..."
    if logrotate -d "$GOUGH_LOGROTATE_FILE"; then
        log_info "Dry run successful"
    else
        log_error "Dry run failed"
        return 1
    fi
    
    # Test force rotation on a small test file
    log_info "Testing actual rotation with test file..."
    local test_log="/var/log/gough/test-rotation.log"
    echo "Test log entry $(date)" > "$test_log"
    chown "$GOUGH_USER:$GOUGH_GROUP" "$test_log"
    
    # Create test-specific logrotate config
    local test_config="/tmp/test-logrotate.conf"
    cat > "$test_config" <<EOF
$test_log {
    daily
    rotate 1
    compress
    missingok
    notifempty
    create 644 $GOUGH_USER $GOUGH_GROUP
    copytruncate
}
EOF
    
    # Run test rotation
    if logrotate -f "$test_config"; then
        log_info "Test rotation successful"
        # Cleanup
        rm -f "$test_log"* "$test_config"
    else
        log_error "Test rotation failed"
        rm -f "$test_config"
        return 1
    fi
    
    log_info "Logrotate configuration testing completed successfully"
}

# =================================
# STATUS AND REPORTING
# =================================

show_status() {
    echo "=== Gough Log Rotation Status ==="
    echo ""
    
    echo "Configuration:"
    echo "  Config file: $GOUGH_LOGROTATE_FILE"
    echo "  Status file: /var/lib/logrotate/gough-status"
    echo "  Log directory: /var/log/gough"
    echo "  User/Group: $GOUGH_USER:$GOUGH_GROUP"
    echo ""
    
    echo "Service Status:"
    if [[ -f "$GOUGH_LOGROTATE_FILE" ]]; then
        echo "  ✅ Configuration installed"
    else
        echo "  ❌ Configuration not found"
    fi
    
    if systemctl is-active --quiet cron || systemctl is-active --quiet crond; then
        echo "  ✅ Cron service active"
    else
        echo "  ❌ Cron service not active"
    fi
    
    if [[ -f "/etc/cron.d/gough-logrotate" ]]; then
        echo "  ✅ Cron job configured"
    else
        echo "  ❌ Cron job not found"
    fi
    echo ""
    
    echo "Recent Activity:"
    if [[ -f "/var/lib/logrotate/gough-status" ]]; then
        echo "  Last run: $(date -r /var/lib/logrotate/gough-status)"
    else
        echo "  No rotation history found"
    fi
    echo ""
    
    echo "Log Directory Usage:"
    du -sh /var/log/gough/* 2>/dev/null | head -10 || echo "  No logs found"
    echo ""
    
    echo "Disk Usage:"
    df -h /var/log | grep -v Filesystem
}

# =================================
# MAIN EXECUTION
# =================================

show_usage() {
    cat <<EOF
Gough Log Rotation Setup Script v$VERSION

Usage: $0 [COMMAND] [OPTIONS]

Commands:
    install         Install complete logrotate configuration
    test            Test logrotate configuration
    uninstall       Remove Gough logrotate configuration
    status          Show current status
    rotate-now      Force log rotation now

Options:
    --user USER     Set Gough system user (default: gough)
    --group GROUP   Set Gough system group (default: gough)
    --help          Show this help message

Environment Variables:
    SLACK_WEBHOOK       Slack webhook URL for notifications
    EMAIL_RECIPIENTS    Email addresses for notifications

Examples:
    $0 install          # Install complete log rotation setup
    $0 test             # Test configuration
    $0 status           # Show current status
    $0 rotate-now       # Force immediate rotation

Files:
    Source config: $GOUGH_LOGROTATE_CONFIG
    System config: $GOUGH_LOGROTATE_FILE
    Log file: $LOG_FILE
EOF
}

main() {
    setup_logging
    
    local command=${1:-install}
    
    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --user)
                GOUGH_USER="$2"
                shift 2
                ;;
            --group)
                GOUGH_GROUP="$2"
                shift 2
                ;;
            --help)
                show_usage
                exit 0
                ;;
            install|test|uninstall|status|rotate-now)
                command="$1"
                shift
                ;;
            *)
                shift
                ;;
        esac
    done
    
    log_info "Starting Gough logrotate setup - command: $command"
    
    # Execute command
    case "$command" in
        install)
            check_requirements
            create_gough_user
            create_log_directories
            install_logrotate_config
            configure_logrotate_status
            setup_logrotate_cron
            create_emergency_cleanup_script
            setup_logrotate_monitoring
            test_logrotate_config
            log_info "Gough logrotate installation completed successfully"
            ;;
        test)
            check_requirements
            test_logrotate_config
            ;;
        uninstall)
            log_info "Removing Gough logrotate configuration..."
            rm -f "$GOUGH_LOGROTATE_FILE"
            rm -f "/etc/cron.d/gough-logrotate"
            rm -f "/etc/cron.d/gough-logrotate-monitor"
            log_info "Uninstallation completed"
            ;;
        status)
            show_status
            ;;
        rotate-now)
            log_info "Forcing log rotation now..."
            logrotate -f "$GOUGH_LOGROTATE_FILE"
            log_info "Forced rotation completed"
            ;;
        *)
            log_error "Unknown command: $command"
            show_usage
            exit 1
            ;;
    esac
}

# Run if executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi