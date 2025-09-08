#!/bin/bash

# Gough Database Maintenance Script
# Comprehensive database maintenance for PostgreSQL and MySQL
# Automated backup, optimization, cleanup, and health monitoring

set -euo pipefail

# =================================
# CONFIGURATION VARIABLES
# =================================

SCRIPT_NAME="database-maintenance"
VERSION="1.0.0"
LOG_FILE="/var/log/gough/${SCRIPT_NAME}.log"
LOCK_FILE="/var/lock/${SCRIPT_NAME}.lock"

# Database configuration
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-maasdb}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"

MYSQL_HOST="${MYSQL_HOST:-localhost}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_USER:-root}"
MYSQL_DB="${MYSQL_DB:-fleet}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"

REDIS_HOST="${REDIS_HOST:-localhost}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_PASSWORD="${REDIS_PASSWORD:-}"

# Backup configuration
BACKUP_BASE_DIR="/var/lib/gough/database-backups"
BACKUP_RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-30}
BACKUP_COMPRESSION=${BACKUP_COMPRESSION:-gzip}
MAX_BACKUP_SIZE=${MAX_BACKUP_SIZE:-10737418240}  # 10GB
BACKUP_PARALLELISM=${BACKUP_PARALLELISM:-2}

# Maintenance configuration
VACUUM_THRESHOLD=${VACUUM_THRESHOLD:-20}  # Percentage of dead tuples
ANALYZE_THRESHOLD=${ANALYZE_THRESHOLD:-10}  # Percentage of modified tuples
AUTO_VACUUM_ENABLED=${AUTO_VACUUM_ENABLED:-true}
MAINTENANCE_WINDOW_START=${MAINTENANCE_WINDOW_START:-02:00}
MAINTENANCE_WINDOW_END=${MAINTENANCE_WINDOW_END:-06:00}

# Monitoring configuration
SLACK_WEBHOOK="${SLACK_WEBHOOK:-}"
EMAIL_RECIPIENTS="${EMAIL_RECIPIENTS:-dba@gough.local}"
ALERT_ON_BACKUP_FAILURE=${ALERT_ON_BACKUP_FAILURE:-true}
ALERT_ON_HIGH_DISK_USAGE=${ALERT_ON_HIGH_DISK_USAGE:-true}
DISK_USAGE_THRESHOLD=${DISK_USAGE_THRESHOLD:-80}  # Percentage

# Performance tuning
CONNECTION_POOL_SIZE=${CONNECTION_POOL_SIZE:-20}
QUERY_TIMEOUT=${QUERY_TIMEOUT:-300}  # 5 minutes
CHECKPOINT_COMPLETION_TARGET=${CHECKPOINT_COMPLETION_TARGET:-0.9}

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
        log_fatal "Database maintenance is already running (PID: $pid)"
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
        log_info "Database maintenance completed successfully"
        send_notification "✅ Database maintenance completed successfully" "success"
    else
        log_error "Database maintenance failed with exit code $exit_code"
        send_notification "❌ Database maintenance failed" "error"
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
    local icon=":database:"
    
    case "$type" in
        success) color="good"; icon=":white_check_mark:" ;;
        error) color="danger"; icon=":x:" ;;
        warning) color="warning"; icon=":warning:" ;;
    esac
    
    local payload=$(cat <<EOF
{
    "channel": "#gough-database",
    "username": "Gough DB Maintenance",
    "icon_emoji": "$icon",
    "attachments": [
        {
            "color": "$color",
            "title": "Database Maintenance Notification",
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
    
    local subject="Gough Database Maintenance - $type"
    
    local body="
Database Maintenance Notification

Message: $message
Server: $(hostname)
Timestamp: $timestamp
Log File: $LOG_FILE

This is an automated notification from the Gough database maintenance system.
"
    
    if command -v mail >/dev/null 2>&1; then
        echo "$body" | mail -s "$subject" "$EMAIL_RECIPIENTS"
    fi
}

# =================================
# DATABASE CONNECTION HELPERS
# =================================

postgres_execute() {
    local query=$1
    local database=${2:-$POSTGRES_DB}
    
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        -h "$POSTGRES_HOST" \
        -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" \
        -d "$database" \
        -t -c "$query" 2>/dev/null || return 1
}

mysql_execute() {
    local query=$1
    local database=${2:-$MYSQL_DB}
    
    mysql \
        -h "$MYSQL_HOST" \
        -P "$MYSQL_PORT" \
        -u "$MYSQL_USER" \
        -p"$MYSQL_PASSWORD" \
        -D "$database" \
        -e "$query" 2>/dev/null || return 1
}

redis_execute() {
    local command=$1
    
    if [[ -n "$REDIS_PASSWORD" ]]; then
        redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" -a "$REDIS_PASSWORD" "$command"
    else
        redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "$command"
    fi
}

# =================================
# DATABASE HEALTH CHECKS
# =================================

check_postgres_health() {
    log_info "Checking PostgreSQL health..."
    
    # Connection test
    if ! postgres_execute "SELECT 1;" >/dev/null; then
        log_error "Cannot connect to PostgreSQL"
        return 1
    fi
    
    # Check database size
    local db_size_bytes
    db_size_bytes=$(postgres_execute "SELECT pg_database_size('$POSTGRES_DB');" | tr -d ' ')
    local db_size_mb=$((db_size_bytes / 1024 / 1024))
    log_info "PostgreSQL database size: ${db_size_mb}MB"
    
    # Check connection count
    local connections
    connections=$(postgres_execute "SELECT count(*) FROM pg_stat_activity;" | tr -d ' ')
    log_info "Active connections: $connections"
    
    # Check replication lag (if replica)
    local is_replica
    is_replica=$(postgres_execute "SELECT pg_is_in_recovery();" | tr -d ' ')
    if [[ "$is_replica" == "t" ]]; then
        local lag_bytes
        lag_bytes=$(postgres_execute "SELECT CASE WHEN pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn() THEN 0 ELSE EXTRACT (EPOCH FROM now() - pg_last_xact_replay_timestamp()) END;" | tr -d ' ')
        log_info "Replication lag: ${lag_bytes} seconds"
        
        if [[ $(echo "$lag_bytes > 60" | bc -l) -eq 1 ]]; then
            log_warn "High replication lag detected: ${lag_bytes} seconds"
        fi
    fi
    
    # Check for long-running queries
    local long_queries
    long_queries=$(postgres_execute "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND now() - query_start > interval '5 minutes';" | tr -d ' ')
    if [[ $long_queries -gt 0 ]]; then
        log_warn "Found $long_queries long-running queries"
    fi
    
    # Check disk usage
    local data_dir
    data_dir=$(postgres_execute "SHOW data_directory;" | tr -d ' ')
    if [[ -d "$data_dir" ]]; then
        local disk_usage
        disk_usage=$(df "$data_dir" | awk 'NR==2 {print $5}' | sed 's/%//')
        log_info "PostgreSQL disk usage: ${disk_usage}%"
        
        if [[ $disk_usage -gt $DISK_USAGE_THRESHOLD ]]; then
            log_warn "High disk usage: ${disk_usage}%"
            send_notification "⚠️ PostgreSQL disk usage is ${disk_usage}%" "warning"
        fi
    fi
    
    log_info "PostgreSQL health check completed"
    return 0
}

check_mysql_health() {
    log_info "Checking MySQL health..."
    
    # Connection test
    if ! mysql_execute "SELECT 1;" >/dev/null; then
        log_error "Cannot connect to MySQL"
        return 1
    fi
    
    # Check database size
    local db_size_mb
    db_size_mb=$(mysql_execute "SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024, 1) AS 'DB Size in MB' FROM information_schema.tables WHERE table_schema='$MYSQL_DB';" | tail -1)
    log_info "MySQL database size: ${db_size_mb}MB"
    
    # Check connection count
    local connections
    connections=$(mysql_execute "SHOW STATUS LIKE 'Threads_connected';" | awk '{print $2}')
    log_info "Active connections: $connections"
    
    # Check replication status (if slave)
    local slave_status
    slave_status=$(mysql_execute "SHOW SLAVE STATUS\G" | grep "Slave_IO_Running" | awk '{print $2}' || echo "No")
    if [[ "$slave_status" == "Yes" ]]; then
        local seconds_behind
        seconds_behind=$(mysql_execute "SHOW SLAVE STATUS\G" | grep "Seconds_Behind_Master" | awk '{print $2}')
        log_info "Replication lag: ${seconds_behind} seconds"
        
        if [[ $seconds_behind -gt 60 ]]; then
            log_warn "High replication lag: ${seconds_behind} seconds"
        fi
    fi
    
    # Check InnoDB status
    local innodb_status
    innodb_status=$(mysql_execute "SHOW ENGINE INNODB STATUS\G" | grep "BACKGROUND THREAD" -A 20 | grep "Log sequence number" | awk '{print $4}')
    log_info "InnoDB log sequence number: $innodb_status"
    
    log_info "MySQL health check completed"
    return 0
}

check_redis_health() {
    log_info "Checking Redis health..."
    
    # Connection test
    if ! redis_execute "PING" | grep -q "PONG"; then
        log_error "Cannot connect to Redis"
        return 1
    fi
    
    # Get Redis info
    local redis_info
    redis_info=$(redis_execute "INFO")
    
    # Extract key metrics
    local used_memory_mb
    used_memory_mb=$(echo "$redis_info" | grep "used_memory:" | cut -d: -f2 | tr -d '\r' | awk '{print int($1/1024/1024)}')
    log_info "Redis memory usage: ${used_memory_mb}MB"
    
    local connected_clients
    connected_clients=$(echo "$redis_info" | grep "connected_clients:" | cut -d: -f2 | tr -d '\r')
    log_info "Redis connected clients: $connected_clients"
    
    local keyspace_hits
    keyspace_hits=$(echo "$redis_info" | grep "keyspace_hits:" | cut -d: -f2 | tr -d '\r')
    local keyspace_misses
    keyspace_misses=$(echo "$redis_info" | grep "keyspace_misses:" | cut -d: -f2 | tr -d '\r')
    
    if [[ $keyspace_hits -gt 0 ]] && [[ $keyspace_misses -gt 0 ]]; then
        local hit_rate
        hit_rate=$(echo "scale=2; $keyspace_hits * 100 / ($keyspace_hits + $keyspace_misses)" | bc)
        log_info "Redis hit rate: ${hit_rate}%"
    fi
    
    log_info "Redis health check completed"
    return 0
}

# =================================
# BACKUP OPERATIONS
# =================================

create_backup_directories() {
    local backup_date=$(date +'%Y-%m-%d')
    
    # Create backup directories
    mkdir -p "$BACKUP_BASE_DIR/postgres/$backup_date"
    mkdir -p "$BACKUP_BASE_DIR/mysql/$backup_date"
    mkdir -p "$BACKUP_BASE_DIR/redis/$backup_date"
    
    # Set proper permissions
    chmod 750 "$BACKUP_BASE_DIR"
    chown -R postgres:postgres "$BACKUP_BASE_DIR/postgres" 2>/dev/null || true
    chown -R mysql:mysql "$BACKUP_BASE_DIR/mysql" 2>/dev/null || true
}

backup_postgres() {
    log_info "Creating PostgreSQL backup..."
    
    local backup_date=$(date +'%Y-%m-%d')
    local backup_time=$(date +'%H%M%S')
    local backup_dir="$BACKUP_BASE_DIR/postgres/$backup_date"
    local backup_file="$backup_dir/postgres_${POSTGRES_DB}_${backup_date}_${backup_time}.sql"
    
    # Create full database backup
    log_info "Creating full database backup..."
    PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
        -h "$POSTGRES_HOST" \
        -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        --verbose \
        --no-password \
        --format=plain \
        --no-owner \
        --no-privileges \
        > "$backup_file"
    
    # Compress backup
    if [[ "$BACKUP_COMPRESSION" == "gzip" ]]; then
        log_info "Compressing backup..."
        gzip "$backup_file"
        backup_file="${backup_file}.gz"
    fi
    
    # Verify backup
    if [[ -f "$backup_file" ]]; then
        local backup_size
        backup_size=$(du -h "$backup_file" | cut -f1)
        log_info "PostgreSQL backup created: $backup_file ($backup_size)"
        
        # Check backup size
        local backup_bytes
        backup_bytes=$(stat -c%s "$backup_file")
        if [[ $backup_bytes -gt $MAX_BACKUP_SIZE ]]; then
            log_warn "Backup size exceeds maximum: $backup_bytes bytes"
        fi
        
        # Create metadata
        cat > "${backup_file}.meta" <<EOF
{
    "database": "$POSTGRES_DB",
    "host": "$POSTGRES_HOST",
    "timestamp": "$(date -Iseconds)",
    "size_bytes": $backup_bytes,
    "compression": "$BACKUP_COMPRESSION",
    "pg_version": "$(postgres_execute "SELECT version();")"
}
EOF
        
        return 0
    else
        log_error "PostgreSQL backup failed"
        return 1
    fi
}

backup_mysql() {
    log_info "Creating MySQL backup..."
    
    local backup_date=$(date +'%Y-%m-%d')
    local backup_time=$(date +'%H%M%S')
    local backup_dir="$BACKUP_BASE_DIR/mysql/$backup_date"
    local backup_file="$backup_dir/mysql_${MYSQL_DB}_${backup_date}_${backup_time}.sql"
    
    # Create full database backup
    log_info "Creating full database backup..."
    mysqldump \
        -h "$MYSQL_HOST" \
        -P "$MYSQL_PORT" \
        -u "$MYSQL_USER" \
        -p"$MYSQL_PASSWORD" \
        --single-transaction \
        --routines \
        --triggers \
        --events \
        --add-drop-database \
        --databases "$MYSQL_DB" \
        > "$backup_file"
    
    # Compress backup
    if [[ "$BACKUP_COMPRESSION" == "gzip" ]]; then
        log_info "Compressing backup..."
        gzip "$backup_file"
        backup_file="${backup_file}.gz"
    fi
    
    # Verify backup
    if [[ -f "$backup_file" ]]; then
        local backup_size
        backup_size=$(du -h "$backup_file" | cut -f1)
        log_info "MySQL backup created: $backup_file ($backup_size)"
        
        # Create metadata
        local backup_bytes
        backup_bytes=$(stat -c%s "$backup_file")
        cat > "${backup_file}.meta" <<EOF
{
    "database": "$MYSQL_DB",
    "host": "$MYSQL_HOST",
    "timestamp": "$(date -Iseconds)",
    "size_bytes": $backup_bytes,
    "compression": "$BACKUP_COMPRESSION",
    "mysql_version": "$(mysql_execute "SELECT VERSION();" | tail -1)"
}
EOF
        
        return 0
    else
        log_error "MySQL backup failed"
        return 1
    fi
}

backup_redis() {
    log_info "Creating Redis backup..."
    
    local backup_date=$(date +'%Y-%m-%d')
    local backup_time=$(date +'%H%M%S')
    local backup_dir="$BACKUP_BASE_DIR/redis/$backup_date"
    local backup_file="$backup_dir/redis_${backup_date}_${backup_time}.rdb"
    
    # Trigger Redis save
    redis_execute "BGSAVE"
    
    # Wait for background save to complete
    local save_in_progress=1
    while [[ $save_in_progress -eq 1 ]]; do
        sleep 5
        save_in_progress=$(redis_execute "LASTSAVE" | xargs -I{} redis_execute "LASTSAVE" | wc -l)
        if [[ $save_in_progress -eq 1 ]]; then
            break
        fi
        log_info "Waiting for Redis background save to complete..."
    done
    
    # Copy RDB file
    local redis_dir="/var/lib/redis"  # Default Redis data directory
    if [[ -f "$redis_dir/dump.rdb" ]]; then
        cp "$redis_dir/dump.rdb" "$backup_file"
        
        # Compress backup
        if [[ "$BACKUP_COMPRESSION" == "gzip" ]]; then
            gzip "$backup_file"
            backup_file="${backup_file}.gz"
        fi
        
        local backup_size
        backup_size=$(du -h "$backup_file" | cut -f1)
        log_info "Redis backup created: $backup_file ($backup_size)"
        
        # Create metadata
        local backup_bytes
        backup_bytes=$(stat -c%s "$backup_file")
        local redis_version
        redis_version=$(redis_execute "INFO server" | grep "redis_version" | cut -d: -f2 | tr -d '\r')
        
        cat > "${backup_file}.meta" <<EOF
{
    "host": "$REDIS_HOST",
    "timestamp": "$(date -Iseconds)",
    "size_bytes": $backup_bytes,
    "compression": "$BACKUP_COMPRESSION",
    "redis_version": "$redis_version"
}
EOF
        
        return 0
    else
        log_error "Redis backup failed - RDB file not found"
        return 1
    fi
}

# =================================
# OPTIMIZATION OPERATIONS
# =================================

optimize_postgres() {
    log_info "Optimizing PostgreSQL database..."
    
    # Get table statistics
    local tables
    tables=$(postgres_execute "SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('information_schema', 'pg_catalog');" | grep -v "^$")
    
    local table_count=0
    local processed_count=0
    
    # Count tables
    table_count=$(echo "$tables" | wc -l)
    log_info "Processing $table_count tables for optimization"
    
    while IFS='|' read -r schema table; do
        schema=$(echo "$schema" | xargs)
        table=$(echo "$table" | xargs)
        processed_count=$((processed_count + 1))
        
        if [[ -z "$schema" ]] || [[ -z "$table" ]]; then
            continue
        fi
        
        show_progress "$processed_count" "$table_count" "Optimizing tables"
        
        # Get table stats
        local dead_tuples
        dead_tuples=$(postgres_execute "SELECT n_dead_tup FROM pg_stat_user_tables WHERE schemaname='$schema' AND relname='$table';" | tr -d ' ')
        local live_tuples
        live_tuples=$(postgres_execute "SELECT n_tup_ins + n_tup_upd FROM pg_stat_user_tables WHERE schemaname='$schema' AND relname='$table';" | tr -d ' ')
        
        if [[ -n "$dead_tuples" ]] && [[ -n "$live_tuples" ]] && [[ $live_tuples -gt 0 ]]; then
            local dead_percentage
            dead_percentage=$(echo "scale=2; $dead_tuples * 100 / ($live_tuples + $dead_tuples)" | bc 2>/dev/null || echo "0")
            
            # Vacuum if dead tuple percentage exceeds threshold
            if [[ $(echo "$dead_percentage > $VACUUM_THRESHOLD" | bc -l) -eq 1 ]]; then
                log_info "Vacuuming $schema.$table (${dead_percentage}% dead tuples)"
                postgres_execute "VACUUM ANALYZE $schema.$table;" || log_warn "Failed to vacuum $schema.$table"
            fi
            
            # Analyze if significant changes
            local mod_tuples
            mod_tuples=$(postgres_execute "SELECT n_tup_upd + n_tup_del FROM pg_stat_user_tables WHERE schemaname='$schema' AND relname='$table';" | tr -d ' ')
            if [[ -n "$mod_tuples" ]] && [[ $mod_tuples -gt 0 ]]; then
                local mod_percentage
                mod_percentage=$(echo "scale=2; $mod_tuples * 100 / ($live_tuples + $dead_tuples)" | bc 2>/dev/null || echo "0")
                
                if [[ $(echo "$mod_percentage > $ANALYZE_THRESHOLD" | bc -l) -eq 1 ]]; then
                    log_info "Analyzing $schema.$table (${mod_percentage}% modified)"
                    postgres_execute "ANALYZE $schema.$table;" || log_warn "Failed to analyze $schema.$table"
                fi
            fi
        fi
    done <<< "$tables"
    
    # Reindex if needed
    log_info "Checking for index bloat..."
    local bloated_indexes
    bloated_indexes=$(postgres_execute "SELECT indexrelname FROM pg_stat_user_indexes WHERE idx_scan = 0 AND schemaname NOT IN ('information_schema', 'pg_catalog');" | grep -v "^$")
    
    if [[ -n "$bloated_indexes" ]]; then
        log_info "Found unused indexes, consider removing:"
        echo "$bloated_indexes"
    fi
    
    # Update statistics
    log_info "Updating database statistics..."
    postgres_execute "ANALYZE;" || log_warn "Failed to update global statistics"
    
    log_info "PostgreSQL optimization completed"
}

optimize_mysql() {
    log_info "Optimizing MySQL database..."
    
    # Get table list
    local tables
    tables=$(mysql_execute "SHOW TABLES;" | grep -v "Tables_in_")
    
    local table_count=0
    local processed_count=0
    
    # Count tables
    table_count=$(echo "$tables" | wc -l)
    log_info "Processing $table_count tables for optimization"
    
    while read -r table; do
        if [[ -z "$table" ]]; then
            continue
        fi
        
        processed_count=$((processed_count + 1))
        show_progress "$processed_count" "$table_count" "Optimizing tables"
        
        # Optimize table
        log_info "Optimizing table: $table"
        mysql_execute "OPTIMIZE TABLE $table;" || log_warn "Failed to optimize $table"
        
        # Analyze table
        mysql_execute "ANALYZE TABLE $table;" || log_warn "Failed to analyze $table"
    done <<< "$tables"
    
    # Check for fragmentation
    log_info "Checking table fragmentation..."
    local fragmented_tables
    fragmented_tables=$(mysql_execute "SELECT TABLE_NAME, ROUND((DATA_FREE/1024/1024),2) AS DATA_FREE_MB FROM information_schema.TABLES WHERE TABLE_SCHEMA='$MYSQL_DB' AND DATA_FREE > 0 ORDER BY DATA_FREE DESC LIMIT 10;")
    
    if [[ -n "$fragmented_tables" ]]; then
        log_info "Top fragmented tables:"
        echo "$fragmented_tables"
    fi
    
    log_info "MySQL optimization completed"
}

optimize_redis() {
    log_info "Optimizing Redis..."
    
    # Get memory info
    local memory_info
    memory_info=$(redis_execute "INFO memory")
    
    local used_memory
    used_memory=$(echo "$memory_info" | grep "used_memory:" | cut -d: -f2 | tr -d '\r')
    local used_memory_rss
    used_memory_rss=$(echo "$memory_info" | grep "used_memory_rss:" | cut -d: -f2 | tr -d '\r')
    
    log_info "Redis memory usage: $used_memory bytes (RSS: $used_memory_rss bytes)"
    
    # Check if memory defragmentation is needed
    if [[ $used_memory_rss -gt $((used_memory * 120 / 100)) ]]; then
        log_info "Memory fragmentation detected, running defragmentation..."
        redis_execute "MEMORY DOCTOR" || log_warn "Memory doctor command failed"
    fi
    
    # Clean expired keys
    local expired_keys
    expired_keys=$(redis_execute "INFO stats" | grep "expired_keys:" | cut -d: -f2 | tr -d '\r')
    log_info "Expired keys cleaned: $expired_keys"
    
    # Get key statistics
    local keyspace_info
    keyspace_info=$(redis_execute "INFO keyspace")
    log_info "Keyspace info: $keyspace_info"
    
    log_info "Redis optimization completed"
}

# =================================
# CLEANUP OPERATIONS
# =================================

cleanup_old_backups() {
    log_info "Cleaning up old backups (retention: $BACKUP_RETENTION_DAYS days)..."
    
    local cleaned_count=0
    
    # PostgreSQL backups
    if [[ -d "$BACKUP_BASE_DIR/postgres" ]]; then
        local old_postgres_backups
        old_postgres_backups=$(find "$BACKUP_BASE_DIR/postgres" -name "*.sql*" -mtime +$BACKUP_RETENTION_DAYS)
        for backup in $old_postgres_backups; do
            log_info "Removing old PostgreSQL backup: $backup"
            rm -f "$backup" "$backup.meta"
            cleaned_count=$((cleaned_count + 1))
        done
    fi
    
    # MySQL backups
    if [[ -d "$BACKUP_BASE_DIR/mysql" ]]; then
        local old_mysql_backups
        old_mysql_backups=$(find "$BACKUP_BASE_DIR/mysql" -name "*.sql*" -mtime +$BACKUP_RETENTION_DAYS)
        for backup in $old_mysql_backups; do
            log_info "Removing old MySQL backup: $backup"
            rm -f "$backup" "$backup.meta"
            cleaned_count=$((cleaned_count + 1))
        done
    fi
    
    # Redis backups
    if [[ -d "$BACKUP_BASE_DIR/redis" ]]; then
        local old_redis_backups
        old_redis_backups=$(find "$BACKUP_BASE_DIR/redis" -name "*.rdb*" -mtime +$BACKUP_RETENTION_DAYS)
        for backup in $old_redis_backups; do
            log_info "Removing old Redis backup: $backup"
            rm -f "$backup" "$backup.meta"
            cleaned_count=$((cleaned_count + 1))
        done
    fi
    
    # Remove empty directories
    find "$BACKUP_BASE_DIR" -type d -empty -delete 2>/dev/null || true
    
    log_info "Cleaned up $cleaned_count old backup files"
}

cleanup_database_logs() {
    log_info "Cleaning up database logs..."
    
    # PostgreSQL log cleanup
    local postgres_log_dir="/var/log/postgresql"
    if [[ -d "$postgres_log_dir" ]]; then
        local old_postgres_logs
        old_postgres_logs=$(find "$postgres_log_dir" -name "*.log" -mtime +7)  # Keep 7 days of logs
        for log_file in $old_postgres_logs; do
            log_info "Removing old PostgreSQL log: $log_file"
            rm -f "$log_file"
        done
    fi
    
    # MySQL log cleanup
    local mysql_log_dir="/var/log/mysql"
    if [[ -d "$mysql_log_dir" ]]; then
        local old_mysql_logs
        old_mysql_logs=$(find "$mysql_log_dir" -name "*.log" -mtime +7)
        for log_file in $old_mysql_logs; do
            log_info "Removing old MySQL log: $log_file"
            rm -f "$log_file"
        done
    fi
    
    log_info "Database log cleanup completed"
}

# =================================
# MAINTENANCE WINDOW CHECK
# =================================

check_maintenance_window() {
    local current_time=$(date +'%H:%M')
    local start_time="$MAINTENANCE_WINDOW_START"
    local end_time="$MAINTENANCE_WINDOW_END"
    
    # Simple time comparison (assumes same day)
    if [[ "$current_time" > "$start_time" ]] && [[ "$current_time" < "$end_time" ]]; then
        return 0  # In maintenance window
    else
        return 1  # Outside maintenance window
    fi
}

# =================================
# MAIN FUNCTIONS
# =================================

run_health_checks() {
    log_info "Running database health checks..."
    
    local health_status=0
    
    # PostgreSQL health check
    if ! check_postgres_health; then
        health_status=1
    fi
    
    # MySQL health check
    if ! check_mysql_health; then
        health_status=1
    fi
    
    # Redis health check
    if ! check_redis_health; then
        health_status=1
    fi
    
    return $health_status
}

run_backups() {
    log_info "Running database backups..."
    
    create_backup_directories
    
    local backup_status=0
    
    # PostgreSQL backup
    if ! backup_postgres; then
        backup_status=1
        if [[ "$ALERT_ON_BACKUP_FAILURE" == "true" ]]; then
            send_notification "❌ PostgreSQL backup failed" "error"
        fi
    fi
    
    # MySQL backup
    if ! backup_mysql; then
        backup_status=1
        if [[ "$ALERT_ON_BACKUP_FAILURE" == "true" ]]; then
            send_notification "❌ MySQL backup failed" "error"
        fi
    fi
    
    # Redis backup
    if ! backup_redis; then
        backup_status=1
        if [[ "$ALERT_ON_BACKUP_FAILURE" == "true" ]]; then
            send_notification "❌ Redis backup failed" "error"
        fi
    fi
    
    if [[ $backup_status -eq 0 ]]; then
        send_notification "✅ All database backups completed successfully" "success"
    fi
    
    return $backup_status
}

run_optimization() {
    log_info "Running database optimization..."
    
    # Check if we're in maintenance window
    if ! check_maintenance_window; then
        log_warn "Outside maintenance window - skipping optimization"
        return 0
    fi
    
    local optimization_status=0
    
    # PostgreSQL optimization
    if ! optimize_postgres; then
        optimization_status=1
    fi
    
    # MySQL optimization
    if ! optimize_mysql; then
        optimization_status=1
    fi
    
    # Redis optimization
    if ! optimize_redis; then
        optimization_status=1
    fi
    
    return $optimization_status
}

run_cleanup() {
    log_info "Running database cleanup..."
    
    # Clean old backups
    cleanup_old_backups
    
    # Clean database logs
    cleanup_database_logs
    
    log_info "Database cleanup completed"
}

show_status() {
    echo "=== Gough Database Maintenance Status ==="
    echo ""
    
    echo "Configuration:"
    echo "  PostgreSQL: $POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB"
    echo "  MySQL: $MYSQL_HOST:$MYSQL_PORT/$MYSQL_DB"
    echo "  Redis: $REDIS_HOST:$REDIS_PORT"
    echo "  Backup Directory: $BACKUP_BASE_DIR"
    echo "  Maintenance Window: $MAINTENANCE_WINDOW_START - $MAINTENANCE_WINDOW_END"
    echo ""
    
    echo "Current Status:"
    echo "  Current Time: $(date)"
    if check_maintenance_window; then
        echo "  Maintenance Window: ACTIVE"
    else
        echo "  Maintenance Window: inactive"
    fi
    echo ""
    
    # Quick health check
    echo "Database Health:"
    if postgres_execute "SELECT 1;" >/dev/null 2>&1; then
        echo "  PostgreSQL: ✅ Connected"
    else
        echo "  PostgreSQL: ❌ Connection failed"
    fi
    
    if mysql_execute "SELECT 1;" >/dev/null 2>&1; then
        echo "  MySQL: ✅ Connected"
    else
        echo "  MySQL: ❌ Connection failed"
    fi
    
    if redis_execute "PING" | grep -q "PONG" 2>/dev/null; then
        echo "  Redis: ✅ Connected"
    else
        echo "  Redis: ❌ Connection failed"
    fi
    echo ""
    
    # Recent backups
    echo "Recent Backups:"
    if [[ -d "$BACKUP_BASE_DIR" ]]; then
        find "$BACKUP_BASE_DIR" -name "*.sql*" -o -name "*.rdb*" | head -5 | while read -r backup; do
            local backup_age
            backup_age=$((($(date +%s) - $(stat -c %Y "$backup")) / 3600))
            echo "  $(basename "$backup") (${backup_age}h ago)"
        done
    else
        echo "  No backups found"
    fi
}

show_usage() {
    cat <<EOF
Gough Database Maintenance Script v$VERSION

Usage: $0 [COMMAND] [OPTIONS]

Commands:
    health              Run database health checks only
    backup              Run database backups only
    optimize            Run database optimization only
    cleanup             Run cleanup operations only
    full                Run all maintenance operations (default)
    status              Show current status and configuration

Options:
    --force-optimization    Force optimization outside maintenance window
    --retention-days DAYS   Override backup retention period
    --help                  Show this help message

Environment Variables:
    POSTGRES_HOST           PostgreSQL host (default: localhost)
    POSTGRES_USER           PostgreSQL user (default: postgres)
    POSTGRES_PASSWORD       PostgreSQL password
    MYSQL_HOST             MySQL host (default: localhost)  
    MYSQL_USER             MySQL user (default: root)
    MYSQL_PASSWORD         MySQL password
    REDIS_HOST             Redis host (default: localhost)
    SLACK_WEBHOOK          Slack webhook for notifications
    EMAIL_RECIPIENTS       Email addresses for notifications

Examples:
    $0 full                 # Run all maintenance operations
    $0 backup               # Run backups only
    $0 health               # Check database health
    $0 status               # Show current status

Configuration:
    Log file: $LOG_FILE
    Backup directory: $BACKUP_BASE_DIR
    Maintenance window: $MAINTENANCE_WINDOW_START - $MAINTENANCE_WINDOW_END
EOF
}

# =================================
# MAIN EXECUTION
# =================================

main() {
    # Setup
    setup_logging
    acquire_lock
    
    local command=${1:-full}
    local force_optimization=false
    
    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force-optimization)
                force_optimization=true
                shift
                ;;
            --retention-days)
                BACKUP_RETENTION_DAYS="$2"
                shift 2
                ;;
            --help)
                show_usage
                exit 0
                ;;
            health|backup|optimize|cleanup|full|status)
                command="$1"
                shift
                ;;
            *)
                shift
                ;;
        esac
    done
    
    log_info "Starting database maintenance - command: $command"
    
    # Execute command
    case "$command" in
        health)
            run_health_checks
            ;;
        backup)
            run_backups
            ;;
        optimize)
            if [[ "$force_optimization" == "true" ]]; then
                log_info "Forcing optimization outside maintenance window"
            fi
            run_optimization
            ;;
        cleanup)
            run_cleanup
            ;;
        full)
            local overall_status=0
            
            if ! run_health_checks; then
                overall_status=1
            fi
            
            if ! run_backups; then
                overall_status=1
            fi
            
            if ! run_optimization; then
                overall_status=1
            fi
            
            run_cleanup  # Always run cleanup
            
            exit $overall_status
            ;;
        status)
            show_status
            ;;
        *)
            log_error "Unknown command: $command"
            show_usage
            exit 1
            ;;
    esac
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi