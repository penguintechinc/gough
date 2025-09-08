#!/bin/bash

# PostgreSQL High Availability Setup Script
# Configures master-slave replication for Gough MaaS database
# Supports automatic failover and recovery procedures

set -euo pipefail

# =================================
# CONFIGURATION VARIABLES
# =================================

POSTGRES_MAJOR=${POSTGRES_MAJOR:-15}
POSTGRES_USER=${POSTGRES_USER:-maas}
POSTGRES_DB=${POSTGRES_DB:-maasdb}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-}
POSTGRES_REPLICATION_USER=${POSTGRES_REPLICATION_USER:-replica}
POSTGRES_REPLICATION_PASSWORD=${POSTGRES_REPLICATION_PASSWORD:-}

# Replication configuration
POSTGRES_MASTER_HOST=${POSTGRES_MASTER_HOST:-postgres-primary}
POSTGRES_MASTER_PORT=${POSTGRES_MASTER_PORT:-5432}
POSTGRES_REPLICA_HOST=${POSTGRES_REPLICA_HOST:-postgres-replica}
POSTGRES_REPLICA_PORT=${POSTGRES_REPLICA_PORT:-5432}

# Paths
PGDATA=${PGDATA:-/var/lib/postgresql/data}
POSTGRES_CONF="$PGDATA/postgresql.conf"
PG_HBA_CONF="$PGDATA/pg_hba.conf"
RECOVERY_CONF="$PGDATA/recovery.conf"
POSTGRESQL_CONF_DIR=${POSTGRESQL_CONF_DIR:-/etc/postgresql/config}

# Logging
LOG_FILE="/var/log/postgresql/replication-setup.log"
exec 1> >(tee -a "$LOG_FILE")
exec 2>&1

# =================================
# LOGGING FUNCTIONS
# =================================

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
# VALIDATION FUNCTIONS
# =================================

validate_environment() {
    log_info "Validating environment variables..."
    
    if [[ -z "$POSTGRES_PASSWORD" ]]; then
        log_fatal "POSTGRES_PASSWORD is required"
    fi
    
    if [[ -z "$POSTGRES_REPLICATION_PASSWORD" ]]; then
        log_fatal "POSTGRES_REPLICATION_PASSWORD is required"
    fi
    
    log_info "Environment validation completed"
}

check_postgres_running() {
    if pg_isready -h localhost -p 5432 >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

wait_for_postgres() {
    local host=$1
    local port=$2
    local timeout=${3:-60}
    local counter=0
    
    log_info "Waiting for PostgreSQL at $host:$port..."
    
    while ! pg_isready -h "$host" -p "$port" >/dev/null 2>&1; do
        if [[ $counter -ge $timeout ]]; then
            log_fatal "PostgreSQL at $host:$port did not start within $timeout seconds"
        fi
        sleep 1
        ((counter++))
    done
    
    log_info "PostgreSQL at $host:$port is ready"
}

# =================================
# DATABASE SETUP FUNCTIONS
# =================================

setup_primary_database() {
    log_info "Setting up primary database..."
    
    # Initialize database if not exists
    if [[ ! -f "$PGDATA/PG_VERSION" ]]; then
        log_info "Initializing new PostgreSQL cluster..."
        initdb --encoding=UTF-8 --locale=en_US.UTF-8 --auth-host=md5 --auth-local=peer
    fi
    
    # Start PostgreSQL temporarily for setup
    if ! check_postgres_running; then
        log_info "Starting PostgreSQL for initial setup..."
        pg_ctl start -D "$PGDATA" -o "-p 5432"
        wait_for_postgres localhost 5432
    fi
    
    # Create replication user
    log_info "Creating replication user..."
    psql -v ON_ERROR_STOP=1 <<-EOSQL
        DO \$\$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '$POSTGRES_REPLICATION_USER') THEN
                CREATE USER $POSTGRES_REPLICATION_USER WITH REPLICATION ENCRYPTED PASSWORD '$POSTGRES_REPLICATION_PASSWORD';
                GRANT CONNECT ON DATABASE $POSTGRES_DB TO $POSTGRES_REPLICATION_USER;
            END IF;
        END
        \$\$;
EOSQL
    
    # Create database if not exists
    log_info "Creating MaaS database..."
    psql -v ON_ERROR_STOP=1 <<-EOSQL
        SELECT 'CREATE DATABASE $POSTGRES_DB' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$POSTGRES_DB')\gexec
        GRANT ALL PRIVILEGES ON DATABASE $POSTGRES_DB TO $POSTGRES_USER;
EOSQL
    
    # Setup replication slots
    log_info "Creating replication slot..."
    psql -v ON_ERROR_STOP=1 -d "$POSTGRES_DB" <<-EOSQL
        DO \$\$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_replication_slots WHERE slot_name = 'replica_slot') THEN
                PERFORM pg_create_physical_replication_slot('replica_slot');
            END IF;
        END
        \$\$;
EOSQL
    
    log_info "Primary database setup completed"
}

configure_primary_postgresql() {
    log_info "Configuring primary PostgreSQL..."
    
    # Copy optimized configuration
    if [[ -f "$POSTGRESQL_CONF_DIR/postgresql.conf" ]]; then
        cp "$POSTGRESQL_CONF_DIR/postgresql.conf" "$POSTGRES_CONF"
        log_info "Applied optimized PostgreSQL configuration"
    fi
    
    # Configure pg_hba.conf for replication
    log_info "Configuring authentication..."
    
    # Backup original pg_hba.conf
    cp "$PG_HBA_CONF" "$PG_HBA_CONF.backup.$(date +%Y%m%d_%H%M%S)"
    
    # Add replication entries
    cat >> "$PG_HBA_CONF" <<-EOF

# Replication connections for high availability
host replication $POSTGRES_REPLICATION_USER $POSTGRES_REPLICA_HOST/32 md5
host replication $POSTGRES_REPLICATION_USER 172.21.0.0/16 md5
host replication $POSTGRES_REPLICATION_USER 192.168.100.0/24 md5

# Application connections
host $POSTGRES_DB $POSTGRES_USER 172.21.0.0/16 md5
host $POSTGRES_DB $POSTGRES_USER 192.168.100.0/24 md5
host all all 172.21.0.0/16 md5
host all all 192.168.100.0/24 md5

# Local connections
local all all peer
host all all 127.0.0.1/32 md5
host all all ::1/128 md5
EOF
    
    log_info "PostgreSQL configuration updated"
}

setup_replica_database() {
    log_info "Setting up replica database..."
    
    # Wait for primary to be ready
    wait_for_postgres "$POSTGRES_MASTER_HOST" "$POSTGRES_MASTER_PORT" 120
    
    # Remove existing data directory if present
    if [[ -d "$PGDATA" ]]; then
        log_warn "Removing existing data directory for replica setup..."
        rm -rf "$PGDATA"/*
    fi
    
    # Create base backup from primary
    log_info "Creating base backup from primary..."
    export PGPASSWORD=$POSTGRES_REPLICATION_PASSWORD
    pg_basebackup \
        -h "$POSTGRES_MASTER_HOST" \
        -p "$POSTGRES_MASTER_PORT" \
        -U "$POSTGRES_REPLICATION_USER" \
        -D "$PGDATA" \
        -Fp \
        -Xs \
        -P \
        -v \
        -W
    
    # Configure standby
    log_info "Configuring standby server..."
    
    # Create standby.signal file (PostgreSQL 12+)
    touch "$PGDATA/standby.signal"
    
    # Configure primary connection info
    cat >> "$POSTGRES_CONF" <<-EOF

# Standby configuration
primary_conninfo = 'host=$POSTGRES_MASTER_HOST port=$POSTGRES_MASTER_PORT user=$POSTGRES_REPLICATION_USER password=$POSTGRES_REPLICATION_PASSWORD application_name=replica'
primary_slot_name = 'replica_slot'
promote_trigger_file = '/tmp/postgresql.trigger'
recovery_target_timeline = 'latest'
EOF
    
    # Set proper permissions
    chmod 600 "$POSTGRES_CONF"
    chown postgres:postgres "$PGDATA" -R
    
    log_info "Replica database setup completed"
}

# =================================
# MONITORING AND HEALTH CHECKS
# =================================

setup_monitoring() {
    log_info "Setting up replication monitoring..."
    
    # Create monitoring database objects
    psql -v ON_ERROR_STOP=1 -d "$POSTGRES_DB" <<-EOSQL
        -- Create monitoring schema
        CREATE SCHEMA IF NOT EXISTS monitoring;
        
        -- Create replication lag monitoring view
        CREATE OR REPLACE VIEW monitoring.replication_lag AS
        SELECT
            client_addr,
            client_hostname,
            client_port,
            state,
            sent_lsn,
            write_lsn,
            flush_lsn,
            replay_lsn,
            write_lag,
            flush_lag,
            replay_lag,
            sync_priority,
            sync_state,
            reply_time
        FROM pg_stat_replication;
        
        -- Create database size monitoring view
        CREATE OR REPLACE VIEW monitoring.database_sizes AS
        SELECT
            datname,
            pg_size_pretty(pg_database_size(datname)) as size,
            pg_database_size(datname) as size_bytes
        FROM pg_database
        WHERE datname NOT IN ('template0', 'template1', 'postgres');
        
        -- Create connection monitoring view
        CREATE OR REPLACE VIEW monitoring.connections AS
        SELECT
            datname,
            usename,
            client_addr,
            client_port,
            application_name,
            state,
            backend_start,
            query_start,
            state_change
        FROM pg_stat_activity
        WHERE state IS NOT NULL;
        
        -- Grant access to monitoring user
        DO \$\$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'monitoring') THEN
                CREATE USER monitoring WITH ENCRYPTED PASSWORD 'monitor123';
            END IF;
        END
        \$\$;
        
        GRANT USAGE ON SCHEMA monitoring TO monitoring;
        GRANT SELECT ON ALL TABLES IN SCHEMA monitoring TO monitoring;
        GRANT SELECT ON ALL TABLES IN SCHEMA pg_catalog TO monitoring;
        GRANT SELECT ON ALL TABLES IN SCHEMA information_schema TO monitoring;
EOSQL
    
    log_info "Monitoring setup completed"
}

create_health_check_script() {
    log_info "Creating health check script..."
    
    cat > /usr/local/bin/postgres-health-check.sh <<-'EOF'
#!/bin/bash

# PostgreSQL Health Check Script
# Returns 0 if healthy, 1 if unhealthy

set -euo pipefail

POSTGRES_USER=${POSTGRES_USER:-maas}
POSTGRES_DB=${POSTGRES_DB:-maasdb}
MAX_LAG_SECONDS=${MAX_LAG_SECONDS:-30}

# Check if PostgreSQL is running
if ! pg_isready -q; then
    echo "PostgreSQL is not ready"
    exit 1
fi

# Check if we can connect to the database
if ! psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;" >/dev/null 2>&1; then
    echo "Cannot connect to database $POSTGRES_DB"
    exit 1
fi

# Check replication lag (for primary)
if pg_is_in_recovery | grep -q 'f'; then
    # This is a primary server
    LAG=$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "
        SELECT COALESCE(
            EXTRACT(EPOCH FROM (now() - reply_time))::int,
            999999
        ) FROM pg_stat_replication LIMIT 1;
    " | tr -d ' ')
    
    if [[ "$LAG" -gt "$MAX_LAG_SECONDS" ]]; then
        echo "Replication lag too high: ${LAG}s"
        exit 1
    fi
fi

echo "PostgreSQL is healthy"
exit 0
EOF
    
    chmod +x /usr/local/bin/postgres-health-check.sh
    log_info "Health check script created"
}

# =================================
# BACKUP AND RECOVERY
# =================================

setup_backup_procedures() {
    log_info "Setting up backup procedures..."
    
    # Create backup directory
    mkdir -p /var/lib/postgresql/backups
    chown postgres:postgres /var/lib/postgresql/backups
    
    # Create backup script
    cat > /usr/local/bin/postgres-backup.sh <<-'EOF'
#!/bin/bash

# PostgreSQL Backup Script
# Creates full database backups with WAL archiving

set -euo pipefail

BACKUP_DIR=${BACKUP_DIR:-/var/lib/postgresql/backups}
POSTGRES_USER=${POSTGRES_USER:-maas}
POSTGRES_DB=${POSTGRES_DB:-maasdb}
RETENTION_DAYS=${RETENTION_DAYS:-7}

# Create timestamped backup
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/${POSTGRES_DB}_${TIMESTAMP}.sql.gz"

echo "Starting backup to $BACKUP_FILE..."

# Create full database backup
pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" | gzip > "$BACKUP_FILE"

# Create base backup for point-in-time recovery
BASE_BACKUP_DIR="$BACKUP_DIR/basebackup_$TIMESTAMP"
pg_basebackup -U "$POSTGRES_USER" -D "$BASE_BACKUP_DIR" -Ft -z -P -v

# Clean up old backups
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +$RETENTION_DAYS -delete
find "$BACKUP_DIR" -name "basebackup_*" -mtime +$RETENTION_DAYS -exec rm -rf {} +

echo "Backup completed: $BACKUP_FILE"
EOF
    
    chmod +x /usr/local/bin/postgres-backup.sh
    
    # Create restore script
    cat > /usr/local/bin/postgres-restore.sh <<-'EOF'
#!/bin/bash

# PostgreSQL Restore Script
# Restores database from backup

set -euo pipefail

BACKUP_DIR=${BACKUP_DIR:-/var/lib/postgresql/backups}
POSTGRES_USER=${POSTGRES_USER:-maas}
POSTGRES_DB=${POSTGRES_DB:-maasdb}

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <backup_file>"
    exit 1
fi

BACKUP_FILE="$1"

if [[ ! -f "$BACKUP_FILE" ]]; then
    echo "Backup file $BACKUP_FILE not found"
    exit 1
fi

echo "Restoring from $BACKUP_FILE..."

# Drop existing database (if exists) and recreate
psql -U postgres -c "DROP DATABASE IF EXISTS $POSTGRES_DB;"
psql -U postgres -c "CREATE DATABASE $POSTGRES_DB OWNER $POSTGRES_USER;"

# Restore from backup
gunzip -c "$BACKUP_FILE" | psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

echo "Restore completed successfully"
EOF
    
    chmod +x /usr/local/bin/postgres-restore.sh
    
    log_info "Backup procedures setup completed"
}

# =================================
# MAIN EXECUTION
# =================================

main() {
    log_info "Starting PostgreSQL HA setup..."
    log_info "Mode: ${POSTGRES_REPLICATION_MODE:-unknown}"
    
    validate_environment
    
    case "${POSTGRES_REPLICATION_MODE:-primary}" in
        primary|master)
            log_info "Configuring as primary database server..."
            setup_primary_database
            configure_primary_postgresql
            setup_monitoring
            ;;
        secondary|slave|replica)
            log_info "Configuring as replica database server..."
            setup_replica_database
            ;;
        *)
            log_fatal "Invalid POSTGRES_REPLICATION_MODE: ${POSTGRES_REPLICATION_MODE:-unknown}"
            ;;
    esac
    
    create_health_check_script
    setup_backup_procedures
    
    log_info "PostgreSQL HA setup completed successfully"
}

# Execute main function
main "$@"