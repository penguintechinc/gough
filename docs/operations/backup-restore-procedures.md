# Backup and Restore Procedures

This document provides comprehensive backup and restore procedures for all components of the Gough hypervisor automation system.

## Table of Contents

1. [Overview](#overview)
2. [Backup Strategy](#backup-strategy)
3. [Database Backup Procedures](#database-backup-procedures)
4. [Application Data Backup](#application-data-backup)
5. [Configuration Backup](#configuration-backup)
6. [Container Image Backup](#container-image-backup)
7. [Automated Backup Scripts](#automated-backup-scripts)
8. [Restore Procedures](#restore-procedures)
9. [Disaster Recovery](#disaster-recovery)
10. [Backup Monitoring and Verification](#backup-monitoring-and-verification)

---

## Overview

### Backup Objectives

**Recovery Time Objective (RTO)**: Maximum acceptable downtime
- **Critical**: 1 hour
- **Important**: 4 hours  
- **Standard**: 24 hours

**Recovery Point Objective (RPO)**: Maximum acceptable data loss
- **Database**: 15 minutes
- **Configuration**: 1 hour
- **Logs**: 24 hours

### Backup Types

1. **Full Backup**: Complete system backup (weekly)
2. **Incremental Backup**: Changed data only (daily)
3. **Differential Backup**: Changes since last full backup (daily)
4. **Transaction Log Backup**: Database logs (every 15 minutes)

### Storage Locations

```
Primary Storage: /backup/local (local disk)
Secondary Storage: /backup/remote (network attached storage)
Offsite Storage: s3://gough-backups/prod (cloud storage)
```

---

## Backup Strategy

### Backup Schedule

| Component | Frequency | Type | Retention | Location |
|-----------|-----------|------|-----------|----------|
| PostgreSQL | 15 min | Transaction Log | 7 days | Local |
| PostgreSQL | Daily | Full | 30 days | Remote |
| PostgreSQL | Weekly | Full | 90 days | Offsite |
| MySQL | Daily | Full | 30 days | Remote |
| MySQL | Weekly | Full | 90 days | Offsite |
| Configuration | Daily | Full | 30 days | Remote |
| Application Data | Daily | Incremental | 30 days | Remote |
| Container Images | Weekly | Full | 90 days | Offsite |
| System State | Weekly | Full | 30 days | Remote |

### Backup Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Gough System  │    │  Local Backup   │    │ Remote Backup   │
│                 │───▶│   Storage       │───▶│    Storage      │
│ • PostgreSQL    │    │                 │    │                 │
│ • MySQL         │    │ /backup/local   │    │ /backup/remote  │
│ • Config Files  │    │                 │    │                 │
│ • Docker Images │    │ • Hot backups   │    │ • Cold backups  │
└─────────────────┘    │ • Quick access  │    │ • Long retention│
                       └─────────────────┘    └─────────────────┘
                                │
                                ▼
                       ┌─────────────────┐
                       │  Offsite Backup │
                       │     Storage     │
                       │                 │
                       │ Cloud Storage   │
                       │ • S3/GCS/Azure  │
                       │ • Long retention│
                       │ • Disaster DR   │
                       └─────────────────┘
```

---

## Database Backup Procedures

### PostgreSQL Backup

#### Full Database Backup

**Manual Backup**:
```bash
#!/bin/bash
# postgresql-backup.sh

set -euo pipefail

BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backup/local/postgresql"
REMOTE_DIR="/backup/remote/postgresql"
DB_NAME="gough"
DB_USER="gough"
DB_HOST="gough-postgresql"

# Create backup directories
mkdir -p "$BACKUP_DIR" "$REMOTE_DIR"

# Perform database backup
echo "Starting PostgreSQL backup at $(date)"

# Full database dump
docker exec gough-postgresql pg_dump \
  --host=localhost \
  --port=5432 \
  --username="$DB_USER" \
  --dbname="$DB_NAME" \
  --format=custom \
  --compress=9 \
  --verbose \
  --file="/backup/gough_full_$BACKUP_DATE.backup"

# Copy backup from container
docker cp gough-postgresql:/backup/gough_full_$BACKUP_DATE.backup \
  "$BACKUP_DIR/gough_full_$BACKUP_DATE.backup"

# Remove backup from container
docker exec gough-postgresql rm /backup/gough_full_$BACKUP_DATE.backup

# Verify backup integrity
pg_restore --list "$BACKUP_DIR/gough_full_$BACKUP_DATE.backup" > /dev/null
if [ $? -eq 0 ]; then
  echo "✓ Backup verification successful"
else
  echo "✗ Backup verification failed"
  exit 1
fi

# Compress backup
gzip "$BACKUP_DIR/gough_full_$BACKUP_DATE.backup"

# Copy to remote storage
cp "$BACKUP_DIR/gough_full_$BACKUP_DATE.backup.gz" "$REMOTE_DIR/"

# Upload to cloud storage (if configured)
if [ -n "${AWS_S3_BUCKET:-}" ]; then
  aws s3 cp "$BACKUP_DIR/gough_full_$BACKUP_DATE.backup.gz" \
    "s3://$AWS_S3_BUCKET/postgresql/gough_full_$BACKUP_DATE.backup.gz"
fi

# Cleanup old local backups (keep 7 days)
find "$BACKUP_DIR" -name "gough_full_*.backup.gz" -mtime +7 -delete

# Cleanup old remote backups (keep 30 days)
find "$REMOTE_DIR" -name "gough_full_*.backup.gz" -mtime +30 -delete

echo "PostgreSQL backup completed at $(date)"
```

#### Transaction Log Backup

**Continuous WAL Archiving**:
```bash
#!/bin/bash
# postgresql-wal-backup.sh

WAL_ARCHIVE_DIR="/backup/local/postgresql/wal"
REMOTE_WAL_DIR="/backup/remote/postgresql/wal"

# Create WAL archive directories
mkdir -p "$WAL_ARCHIVE_DIR" "$REMOTE_WAL_DIR"

# Configure PostgreSQL for WAL archiving
docker exec gough-postgresql psql -U gough -d gough -c "
  ALTER SYSTEM SET wal_level = replica;
  ALTER SYSTEM SET archive_mode = on;
  ALTER SYSTEM SET archive_command = 'test ! -f $WAL_ARCHIVE_DIR/%f && cp %p $WAL_ARCHIVE_DIR/%f';
  SELECT pg_reload_conf();
"

# Sync WAL files to remote storage
rsync -av --delete "$WAL_ARCHIVE_DIR/" "$REMOTE_WAL_DIR/"

# Cleanup old WAL files (keep 7 days)
find "$WAL_ARCHIVE_DIR" -name "*.wal" -mtime +7 -delete
find "$REMOTE_WAL_DIR" -name "*.wal" -mtime +30 -delete
```

### MySQL Backup

#### FleetDM Database Backup

**Full MySQL Backup**:
```bash
#!/bin/bash
# mysql-backup.sh

set -euo pipefail

BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backup/local/mysql"
REMOTE_DIR="/backup/remote/mysql"
DB_NAME="fleetdm"
DB_USER="fleetdm"
DB_HOST="gough-mysql"

# Create backup directories
mkdir -p "$BACKUP_DIR" "$REMOTE_DIR"

echo "Starting MySQL backup at $(date)"

# Get MySQL password from secret
MYSQL_PASSWORD=$(cat /opt/gough/config/secrets/mysql_password)

# Full database dump
docker exec gough-mysql mysqldump \
  --host=localhost \
  --user="$DB_USER" \
  --password="$MYSQL_PASSWORD" \
  --single-transaction \
  --routines \
  --triggers \
  --events \
  --add-drop-database \
  --add-drop-table \
  --add-locks \
  --disable-keys \
  --extended-insert \
  --lock-tables=false \
  --quick \
  --set-gtid-purged=OFF \
  "$DB_NAME" > "/backup/fleetdm_full_$BACKUP_DATE.sql"

# Copy backup from container
docker cp gough-mysql:/backup/fleetdm_full_$BACKUP_DATE.sql \
  "$BACKUP_DIR/fleetdm_full_$BACKUP_DATE.sql"

# Remove backup from container
docker exec gough-mysql rm /backup/fleetdm_full_$BACKUP_DATE.sql

# Verify backup integrity
if grep -q "Dump completed" "$BACKUP_DIR/fleetdm_full_$BACKUP_DATE.sql"; then
  echo "✓ MySQL backup verification successful"
else
  echo "✗ MySQL backup verification failed"
  exit 1
fi

# Compress backup
gzip "$BACKUP_DIR/fleetdm_full_$BACKUP_DATE.sql"

# Copy to remote storage
cp "$BACKUP_DIR/fleetdm_full_$BACKUP_DATE.sql.gz" "$REMOTE_DIR/"

# Upload to cloud storage
if [ -n "${AWS_S3_BUCKET:-}" ]; then
  aws s3 cp "$BACKUP_DIR/fleetdm_full_$BACKUP_DATE.sql.gz" \
    "s3://$AWS_S3_BUCKET/mysql/fleetdm_full_$BACKUP_DATE.sql.gz"
fi

# Cleanup old backups
find "$BACKUP_DIR" -name "fleetdm_full_*.sql.gz" -mtime +7 -delete
find "$REMOTE_DIR" -name "fleetdm_full_*.sql.gz" -mtime +30 -delete

echo "MySQL backup completed at $(date)"
```

---

## Application Data Backup

### Configuration Files Backup

**System Configuration Backup**:
```bash
#!/bin/bash
# config-backup.sh

set -euo pipefail

BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backup/local/config"
REMOTE_DIR="/backup/remote/config"

# Create backup directories
mkdir -p "$BACKUP_DIR" "$REMOTE_DIR"

echo "Starting configuration backup at $(date)"

# Create configuration archive
tar -czf "$BACKUP_DIR/gough_config_$BACKUP_DATE.tar.gz" \
  --exclude="*.log" \
  --exclude="*.tmp" \
  --exclude="secrets/*" \
  /opt/gough/config \
  /etc/docker \
  /etc/nginx \
  /etc/systemd/system/gough.service

# Backup secrets separately (encrypted)
if [ -d "/opt/gough/config/secrets" ]; then
  # Encrypt secrets with GPG
  tar -czf - /opt/gough/config/secrets | \
    gpg --symmetric --cipher-algo AES256 \
        --compress-algo 2 \
        --s2k-digest-algo SHA512 \
        --output "$BACKUP_DIR/gough_secrets_$BACKUP_DATE.tar.gz.gpg"
fi

# Backup Docker Compose files
cp /opt/gough/docker-compose.production.yml \
   "$BACKUP_DIR/docker-compose_$BACKUP_DATE.yml"

# Copy to remote storage
cp "$BACKUP_DIR"/gough_config_$BACKUP_DATE.* "$REMOTE_DIR/"
cp "$BACKUP_DIR"/gough_secrets_$BACKUP_DATE.* "$REMOTE_DIR/" 2>/dev/null || true

# Verify backup
if tar -tzf "$BACKUP_DIR/gough_config_$BACKUP_DATE.tar.gz" > /dev/null 2>&1; then
  echo "✓ Configuration backup verification successful"
else
  echo "✗ Configuration backup verification failed"
  exit 1
fi

# Upload to cloud storage
if [ -n "${AWS_S3_BUCKET:-}" ]; then
  aws s3 cp "$BACKUP_DIR/gough_config_$BACKUP_DATE.tar.gz" \
    "s3://$AWS_S3_BUCKET/config/"
  aws s3 cp "$BACKUP_DIR/gough_secrets_$BACKUP_DATE.tar.gz.gpg" \
    "s3://$AWS_S3_BUCKET/config/" 2>/dev/null || true
fi

# Cleanup old backups
find "$BACKUP_DIR" -name "gough_config_*.tar.gz" -mtime +7 -delete
find "$BACKUP_DIR" -name "gough_secrets_*.tar.gz.gpg" -mtime +7 -delete
find "$REMOTE_DIR" -name "gough_config_*.tar.gz" -mtime +30 -delete
find "$REMOTE_DIR" -name "gough_secrets_*.tar.gz.gpg" -mtime +30 -delete

echo "Configuration backup completed at $(date)"
```

### Application Data Backup

**Persistent Volume Backup**:
```bash
#!/bin/bash
# data-backup.sh

set -euo pipefail

BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backup/local/data"
REMOTE_DIR="/backup/remote/data"

# Create backup directories
mkdir -p "$BACKUP_DIR" "$REMOTE_DIR"

echo "Starting data backup at $(date)"

# Backup Docker volumes
docker run --rm \
  -v gough_postgresql-data:/data/postgresql:ro \
  -v gough_mysql-data:/data/mysql:ro \
  -v gough_gough-config:/data/config:ro \
  -v gough_gough-logs:/data/logs:ro \
  -v "$BACKUP_DIR":/backup \
  alpine:latest \
  tar -czf "/backup/gough_volumes_$BACKUP_DATE.tar.gz" /data

# Backup MaaS data
if [ -d "/opt/gough/data/maas" ]; then
  tar -czf "$BACKUP_DIR/gough_maas_data_$BACKUP_DATE.tar.gz" \
    /opt/gough/data/maas
fi

# Copy to remote storage
cp "$BACKUP_DIR"/gough_*_$BACKUP_DATE.tar.gz "$REMOTE_DIR/"

# Verify backups
for backup_file in "$BACKUP_DIR"/gough_*_$BACKUP_DATE.tar.gz; do
  if tar -tzf "$backup_file" > /dev/null 2>&1; then
    echo "✓ Data backup verification successful: $(basename "$backup_file")"
  else
    echo "✗ Data backup verification failed: $(basename "$backup_file")"
    exit 1
  fi
done

# Upload to cloud storage
if [ -n "${AWS_S3_BUCKET:-}" ]; then
  aws s3 sync "$BACKUP_DIR"/ "s3://$AWS_S3_BUCKET/data/" \
    --exclude "*" --include "gough_*_$BACKUP_DATE.tar.gz"
fi

# Cleanup old backups
find "$BACKUP_DIR" -name "gough_*_*.tar.gz" -mtime +7 -delete
find "$REMOTE_DIR" -name "gough_*_*.tar.gz" -mtime +30 -delete

echo "Data backup completed at $(date)"
```

---

## Container Image Backup

### Docker Image Backup

**Container Image Backup**:
```bash
#!/bin/bash
# image-backup.sh

set -euo pipefail

BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backup/local/images"
REMOTE_DIR="/backup/remote/images"

# Create backup directories
mkdir -p "$BACKUP_DIR" "$REMOTE_DIR"

echo "Starting Docker image backup at $(date)"

# Get list of Gough-related images
GOUGH_IMAGES=$(docker images --format "{{.Repository}}:{{.Tag}}" | grep -E "(gough|postgres|mysql|redis|nginx)" | grep -v "<none>")

# Save each image
for image in $GOUGH_IMAGES; do
  safe_name=$(echo "$image" | tr '/:' '_')
  echo "Backing up image: $image"
  
  docker save "$image" | gzip > "$BACKUP_DIR/${safe_name}_$BACKUP_DATE.tar.gz"
  
  # Verify image backup
  if [ -f "$BACKUP_DIR/${safe_name}_$BACKUP_DATE.tar.gz" ] && 
     [ $(stat -f%z "$BACKUP_DIR/${safe_name}_$BACKUP_DATE.tar.gz" 2>/dev/null || stat -c%s "$BACKUP_DIR/${safe_name}_$BACKUP_DATE.tar.gz") -gt 1000000 ]; then
    echo "✓ Image backup successful: $image"
  else
    echo "✗ Image backup failed: $image"
    exit 1
  fi
done

# Create image manifest
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}\t{{.Size}}" | \
  grep -E "(gough|postgres|mysql|redis|nginx)" > "$BACKUP_DIR/image_manifest_$BACKUP_DATE.txt"

# Copy to remote storage
cp "$BACKUP_DIR"/*_$BACKUP_DATE.* "$REMOTE_DIR/"

# Upload to cloud storage (for selected images only)
if [ -n "${AWS_S3_BUCKET:-}" ]; then
  # Only upload Gough-specific images to save bandwidth
  aws s3 sync "$BACKUP_DIR"/ "s3://$AWS_S3_BUCKET/images/" \
    --exclude "*" --include "gough*_$BACKUP_DATE.tar.gz"
  aws s3 cp "$BACKUP_DIR/image_manifest_$BACKUP_DATE.txt" \
    "s3://$AWS_S3_BUCKET/images/"
fi

# Cleanup old backups (images are large, keep fewer)
find "$BACKUP_DIR" -name "*_*.tar.gz" -mtime +3 -delete
find "$BACKUP_DIR" -name "image_manifest_*.txt" -mtime +7 -delete
find "$REMOTE_DIR" -name "*_*.tar.gz" -mtime +14 -delete
find "$REMOTE_DIR" -name "image_manifest_*.txt" -mtime +30 -delete

echo "Docker image backup completed at $(date)"
```

---

## Automated Backup Scripts

### Master Backup Script

**Comprehensive Backup Orchestration**:
```bash
#!/bin/bash
# master-backup.sh

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/opt/gough/logs/backup-$(date +%Y%m%d).log"
BACKUP_TYPE="${1:-daily}"  # daily, weekly, monthly

# Logging function
log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

# Error handling
handle_error() {
  log "ERROR: Backup failed at step: $1"
  # Send notification
  if command -v mail >/dev/null 2>&1; then
    echo "Gough backup failed: $1" | mail -s "Backup Failure" admin@company.com
  fi
  exit 1
}

# Pre-backup checks
pre_backup_checks() {
  log "Starting pre-backup checks"
  
  # Check available disk space
  AVAILABLE_SPACE=$(df /backup/local | tail -1 | awk '{print $4}')
  REQUIRED_SPACE=5242880  # 5GB in KB
  
  if [ "$AVAILABLE_SPACE" -lt "$REQUIRED_SPACE" ]; then
    handle_error "Insufficient disk space for backup"
  fi
  
  # Check if all services are running
  if ! docker compose -f /opt/gough/docker-compose.production.yml ps | grep -q "Up"; then
    handle_error "Some services are not running"
  fi
  
  log "Pre-backup checks passed"
}

# Main backup function
run_backup() {
  log "Starting $BACKUP_TYPE backup"
  
  case "$BACKUP_TYPE" in
    daily)
      # Daily backups
      "$SCRIPT_DIR/postgresql-backup.sh" || handle_error "PostgreSQL backup"
      "$SCRIPT_DIR/mysql-backup.sh" || handle_error "MySQL backup"
      "$SCRIPT_DIR/config-backup.sh" || handle_error "Configuration backup"
      "$SCRIPT_DIR/data-backup.sh" || handle_error "Data backup"
      ;;
    weekly)
      # Weekly backups (includes images)
      "$SCRIPT_DIR/postgresql-backup.sh" || handle_error "PostgreSQL backup"
      "$SCRIPT_DIR/mysql-backup.sh" || handle_error "MySQL backup"
      "$SCRIPT_DIR/config-backup.sh" || handle_error "Configuration backup"
      "$SCRIPT_DIR/data-backup.sh" || handle_error "Data backup"
      "$SCRIPT_DIR/image-backup.sh" || handle_error "Image backup"
      "$SCRIPT_DIR/system-backup.sh" || handle_error "System backup"
      ;;
    monthly)
      # Monthly backups (comprehensive)
      "$SCRIPT_DIR/postgresql-backup.sh" || handle_error "PostgreSQL backup"
      "$SCRIPT_DIR/mysql-backup.sh" || handle_error "MySQL backup"
      "$SCRIPT_DIR/config-backup.sh" || handle_error "Configuration backup"
      "$SCRIPT_DIR/data-backup.sh" || handle_error "Data backup"
      "$SCRIPT_DIR/image-backup.sh" || handle_error "Image backup"
      "$SCRIPT_DIR/system-backup.sh" || handle_error "System backup"
      "$SCRIPT_DIR/archive-backup.sh" || handle_error "Archive backup"
      ;;
  esac
}

# Post-backup verification
post_backup_verification() {
  log "Starting post-backup verification"
  
  # Verify backup files exist and are not empty
  BACKUP_COUNT=$(find /backup/local -name "*$(date +%Y%m%d)*" -type f | wc -l)
  if [ "$BACKUP_COUNT" -eq 0 ]; then
    handle_error "No backup files created today"
  fi
  
  # Verify remote synchronization
  if ! rsync -n -av /backup/local/ /backup/remote/ | grep -q "sending incremental file list"; then
    handle_error "Remote backup synchronization failed"
  fi
  
  log "Post-backup verification passed"
}

# Generate backup report
generate_report() {
  local report_file="/opt/gough/logs/backup-report-$(date +%Y%m%d).txt"
  
  cat > "$report_file" << EOF
Gough Backup Report - $(date)
================================

Backup Type: $BACKUP_TYPE
Status: SUCCESS

Backup Files Created:
$(find /backup/local -name "*$(date +%Y%m%d)*" -type f -exec ls -lh {} \; | awk '{print $9 " - " $5}')

Disk Usage:
$(df -h /backup/local | tail -1)

Remote Storage:
$(df -h /backup/remote | tail -1)

Total Backup Size Today:
$(find /backup/local -name "*$(date +%Y%m%d)*" -type f -exec du -ch {} + | tail -1)

EOF

  log "Backup report generated: $report_file"
  
  # Email report if configured
  if command -v mail >/dev/null 2>&1; then
    mail -s "Gough Backup Report - $(date +%Y-%m-%d)" admin@company.com < "$report_file"
  fi
}

# Main execution
main() {
  log "Starting Gough backup process"
  
  pre_backup_checks
  run_backup
  post_backup_verification
  generate_report
  
  log "Backup process completed successfully"
}

# Execute main function
main "$@"
```

### Cron Configuration

**Automated Backup Schedule**:
```bash
# Install backup scripts
cp /opt/gough/scripts/backup/*.sh /opt/gough/scripts/
chmod +x /opt/gough/scripts/*.sh

# Configure cron for gough user
sudo -u gough crontab -l > /tmp/gough-cron 2>/dev/null || echo "" > /tmp/gough-cron

# Add backup schedules
cat >> /tmp/gough-cron << EOF
# PostgreSQL WAL backup (every 15 minutes)
*/15 * * * * /opt/gough/scripts/postgresql-wal-backup.sh >> /opt/gough/logs/wal-backup.log 2>&1

# Daily backups (2 AM)
0 2 * * * /opt/gough/scripts/master-backup.sh daily >> /opt/gough/logs/daily-backup.log 2>&1

# Weekly backups (2 AM Sunday)
0 2 * * 0 /opt/gough/scripts/master-backup.sh weekly >> /opt/gough/logs/weekly-backup.log 2>&1

# Monthly backups (2 AM 1st of month)
0 2 1 * * /opt/gough/scripts/master-backup.sh monthly >> /opt/gough/logs/monthly-backup.log 2>&1

# Backup cleanup (daily at 4 AM)
0 4 * * * /opt/gough/scripts/backup-cleanup.sh >> /opt/gough/logs/cleanup.log 2>&1

# Backup verification (daily at 6 AM)
0 6 * * * /opt/gough/scripts/backup-verification.sh >> /opt/gough/logs/verification.log 2>&1
EOF

# Install new crontab
sudo -u gough crontab /tmp/gough-cron
rm /tmp/gough-cron

echo "Backup automation configured successfully"
```

---

## Restore Procedures

### Database Restore

#### PostgreSQL Restore

**Full Database Restore**:
```bash
#!/bin/bash
# postgresql-restore.sh

set -euo pipefail

BACKUP_FILE="$1"
TARGET_DB="${2:-gough}"
RESTORE_TIMESTAMP=$(date +%Y%m%d_%H%M%S)

if [ -z "$BACKUP_FILE" ]; then
  echo "Usage: $0 <backup_file> [target_database]"
  exit 1
fi

echo "Starting PostgreSQL restore at $(date)"
echo "Backup file: $BACKUP_FILE"
echo "Target database: $TARGET_DB"

# Verify backup file exists
if [ ! -f "$BACKUP_FILE" ]; then
  echo "Error: Backup file not found: $BACKUP_FILE"
  exit 1
fi

# Stop application services to prevent connections
echo "Stopping application services..."
docker compose -f /opt/gough/docker-compose.production.yml stop management-server

# Create restore database
echo "Creating restore database..."
docker exec gough-postgresql createdb -U gough "${TARGET_DB}_restore_$RESTORE_TIMESTAMP" || true

# Decompress backup if needed
RESTORE_FILE="$BACKUP_FILE"
if [[ "$BACKUP_FILE" == *.gz ]]; then
  RESTORE_FILE="/tmp/$(basename "$BACKUP_FILE" .gz)"
  gunzip -c "$BACKUP_FILE" > "$RESTORE_FILE"
fi

# Copy backup file to container
docker cp "$RESTORE_FILE" gough-postgresql:/tmp/restore.backup

# Perform restore
echo "Restoring database..."
docker exec gough-postgresql pg_restore \
  --host=localhost \
  --port=5432 \
  --username=gough \
  --dbname="${TARGET_DB}_restore_$RESTORE_TIMESTAMP" \
  --verbose \
  --clean \
  --if-exists \
  /tmp/restore.backup

# Verify restore
echo "Verifying restore..."
RESTORED_TABLES=$(docker exec gough-postgresql psql -U gough -d "${TARGET_DB}_restore_$RESTORE_TIMESTAMP" -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")

if [ "$RESTORED_TABLES" -gt 0 ]; then
  echo "✓ Database restore verification successful ($RESTORED_TABLES tables restored)"
  
  # Optionally replace original database
  read -p "Replace original database with restored data? (y/N): " -n 1 -r
  echo
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    docker exec gough-postgresql psql -U gough -c "DROP DATABASE IF EXISTS ${TARGET_DB}_backup_$(date +%Y%m%d);"
    docker exec gough-postgresql psql -U gough -c "ALTER DATABASE $TARGET_DB RENAME TO ${TARGET_DB}_backup_$(date +%Y%m%d);"
    docker exec gough-postgresql psql -U gough -c "ALTER DATABASE ${TARGET_DB}_restore_$RESTORE_TIMESTAMP RENAME TO $TARGET_DB;"
    echo "✓ Database replacement completed"
  fi
else
  echo "✗ Database restore verification failed"
  exit 1
fi

# Cleanup
docker exec gough-postgresql rm /tmp/restore.backup
if [ "$RESTORE_FILE" != "$BACKUP_FILE" ]; then
  rm "$RESTORE_FILE"
fi

# Restart application services
echo "Restarting application services..."
docker compose -f /opt/gough/docker-compose.production.yml start management-server

echo "PostgreSQL restore completed at $(date)"
```

#### MySQL Restore

**FleetDM Database Restore**:
```bash
#!/bin/bash
# mysql-restore.sh

set -euo pipefail

BACKUP_FILE="$1"
TARGET_DB="${2:-fleetdm}"
RESTORE_TIMESTAMP=$(date +%Y%m%d_%H%M%S)

if [ -z "$BACKUP_FILE" ]; then
  echo "Usage: $0 <backup_file> [target_database]"
  exit 1
fi

echo "Starting MySQL restore at $(date)"
echo "Backup file: $BACKUP_FILE"
echo "Target database: $TARGET_DB"

# Get MySQL password
MYSQL_PASSWORD=$(cat /opt/gough/config/secrets/mysql_password)

# Stop FleetDM service
echo "Stopping FleetDM service..."
docker compose -f /opt/gough/docker-compose.production.yml stop fleetdm-server

# Create restore database
echo "Creating restore database..."
docker exec gough-mysql mysql -u root -p"$MYSQL_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS ${TARGET_DB}_restore_$RESTORE_TIMESTAMP;"

# Decompress backup if needed
RESTORE_FILE="$BACKUP_FILE"
if [[ "$BACKUP_FILE" == *.gz ]]; then
  RESTORE_FILE="/tmp/$(basename "$BACKUP_FILE" .gz)"
  gunzip -c "$BACKUP_FILE" > "$RESTORE_FILE"
fi

# Copy backup file to container
docker cp "$RESTORE_FILE" gough-mysql:/tmp/restore.sql

# Perform restore
echo "Restoring database..."
docker exec gough-mysql mysql -u root -p"$MYSQL_PASSWORD" "${TARGET_DB}_restore_$RESTORE_TIMESTAMP" < /tmp/restore.sql

# Verify restore
echo "Verifying restore..."
RESTORED_TABLES=$(docker exec gough-mysql mysql -u root -p"$MYSQL_PASSWORD" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${TARGET_DB}_restore_$RESTORE_TIMESTAMP';" | tail -1)

if [ "$RESTORED_TABLES" -gt 0 ]; then
  echo "✓ MySQL restore verification successful ($RESTORED_TABLES tables restored)"
  
  # Optionally replace original database
  read -p "Replace original database with restored data? (y/N): " -n 1 -r
  echo
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    docker exec gough-mysql mysql -u root -p"$MYSQL_PASSWORD" -e "DROP DATABASE IF EXISTS ${TARGET_DB}_backup_$(date +%Y%m%d);"
    docker exec gough-mysql mysql -u root -p"$MYSQL_PASSWORD" -e "RENAME TABLE $TARGET_DB TO ${TARGET_DB}_backup_$(date +%Y%m%d);"
    docker exec gough-mysql mysql -u root -p"$MYSQL_PASSWORD" -e "RENAME TABLE ${TARGET_DB}_restore_$RESTORE_TIMESTAMP TO $TARGET_DB;"
    echo "✓ Database replacement completed"
  fi
else
  echo "✗ MySQL restore verification failed"
  exit 1
fi

# Cleanup
docker exec gough-mysql rm /tmp/restore.sql
if [ "$RESTORE_FILE" != "$BACKUP_FILE" ]; then
  rm "$RESTORE_FILE"
fi

# Restart FleetDM service
echo "Restarting FleetDM service..."
docker compose -f /opt/gough/docker-compose.production.yml start fleetdm-server

echo "MySQL restore completed at $(date)"
```

### Configuration Restore

**System Configuration Restore**:
```bash
#!/bin/bash
# config-restore.sh

set -euo pipefail

BACKUP_FILE="$1"
RESTORE_TYPE="${2:-preview}"  # preview, apply

if [ -z "$BACKUP_FILE" ]; then
  echo "Usage: $0 <backup_file> [preview|apply]"
  exit 1
fi

echo "Starting configuration restore at $(date)"
echo "Backup file: $BACKUP_FILE"
echo "Restore type: $RESTORE_TYPE"

# Verify backup file
if [ ! -f "$BACKUP_FILE" ]; then
  echo "Error: Backup file not found: $BACKUP_FILE"
  exit 1
fi

# Create temporary restore directory
RESTORE_DIR="/tmp/gough-config-restore-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESTORE_DIR"

# Extract backup
echo "Extracting configuration backup..."
if [[ "$BACKUP_FILE" == *.gpg ]]; then
  # Encrypted backup
  gpg --decrypt "$BACKUP_FILE" | tar -xzf - -C "$RESTORE_DIR"
else
  # Regular backup
  tar -xzf "$BACKUP_FILE" -C "$RESTORE_DIR"
fi

if [ "$RESTORE_TYPE" = "preview" ]; then
  echo "Preview mode - showing files that would be restored:"
  find "$RESTORE_DIR" -type f | sed "s|$RESTORE_DIR||g"
  echo
  echo "To apply restore, run: $0 $BACKUP_FILE apply"
elif [ "$RESTORE_TYPE" = "apply" ]; then
  echo "Applying configuration restore..."
  
  # Backup current configuration
  CURRENT_BACKUP="/backup/local/config/current_config_backup_$(date +%Y%m%d_%H%M%S).tar.gz"
  tar -czf "$CURRENT_BACKUP" /opt/gough/config /etc/docker /etc/nginx 2>/dev/null || true
  echo "Current configuration backed up to: $CURRENT_BACKUP"
  
  # Stop services
  echo "Stopping Gough services..."
  systemctl stop gough
  
  # Restore configuration files
  echo "Restoring configuration files..."
  cp -r "$RESTORE_DIR"/opt/gough/config/* /opt/gough/config/
  cp -r "$RESTORE_DIR"/etc/docker/* /etc/docker/ 2>/dev/null || true
  cp -r "$RESTORE_DIR"/etc/nginx/* /etc/nginx/ 2>/dev/null || true
  
  # Set proper permissions
  chown -R gough:gough /opt/gough/config
  chmod 700 /opt/gough/config/secrets
  chmod 600 /opt/gough/config/secrets/*
  
  # Restart services
  echo "Restarting services..."
  systemctl daemon-reload
  systemctl start gough
  
  echo "✓ Configuration restore completed"
  
  # Verify services are running
  sleep 30
  if systemctl is-active --quiet gough; then
    echo "✓ Services started successfully"
  else
    echo "✗ Services failed to start - check logs"
    echo "To rollback: tar -xzf $CURRENT_BACKUP -C /"
    exit 1
  fi
fi

# Cleanup
rm -rf "$RESTORE_DIR"

echo "Configuration restore process completed"
```

### Complete System Restore

**Disaster Recovery Restore**:
```bash
#!/bin/bash
# complete-system-restore.sh

set -euo pipefail

BACKUP_DATE="$1"
RESTORE_MODE="${2:-staged}"  # staged, direct

if [ -z "$BACKUP_DATE" ]; then
  echo "Usage: $0 <backup_date> [staged|direct]"
  echo "Example: $0 20231207 staged"
  exit 1
fi

echo "=== GOUGH COMPLETE SYSTEM RESTORE ==="
echo "Backup date: $BACKUP_DATE"
echo "Restore mode: $RESTORE_MODE"
echo "WARNING: This will replace the current system!"
echo

read -p "Continue with system restore? (yes/no): " -n 3 -r
echo
if [[ ! $REPLY =~ ^yes$ ]]; then
  echo "System restore cancelled"
  exit 1
fi

# Locate backup files
BACKUP_BASE="/backup/remote"
if [ ! -d "$BACKUP_BASE" ]; then
  BACKUP_BASE="/backup/local"
fi

POSTGRES_BACKUP=$(find "$BACKUP_BASE/postgresql" -name "*${BACKUP_DATE}*.backup.gz" | head -1)
MYSQL_BACKUP=$(find "$BACKUP_BASE/mysql" -name "*${BACKUP_DATE}*.sql.gz" | head -1)
CONFIG_BACKUP=$(find "$BACKUP_BASE/config" -name "*${BACKUP_DATE}*.tar.gz" | head -1)
DATA_BACKUP=$(find "$BACKUP_BASE/data" -name "*${BACKUP_DATE}*.tar.gz" | head -1)

# Verify all backups exist
echo "Verifying backup files..."
for backup_file in "$POSTGRES_BACKUP" "$MYSQL_BACKUP" "$CONFIG_BACKUP" "$DATA_BACKUP"; do
  if [ -z "$backup_file" ] || [ ! -f "$backup_file" ]; then
    echo "Error: Missing backup file for $BACKUP_DATE"
    exit 1
  fi
  echo "✓ Found: $(basename "$backup_file")"
done

if [ "$RESTORE_MODE" = "staged" ]; then
  echo "Performing staged restore (services remain available)..."
  
  # Restore to temporary databases first
  echo "1. Restoring PostgreSQL to staging database..."
  /opt/gough/scripts/postgresql-restore.sh "$POSTGRES_BACKUP" "gough_staging"
  
  echo "2. Restoring MySQL to staging database..."
  /opt/gough/scripts/mysql-restore.sh "$MYSQL_BACKUP" "fleetdm_staging"
  
  echo "3. Extracting configuration to staging area..."
  STAGING_CONFIG="/tmp/gough-config-staging"
  mkdir -p "$STAGING_CONFIG"
  tar -xzf "$CONFIG_BACKUP" -C "$STAGING_CONFIG"
  
  echo "Staged restore completed. Review and apply with 'direct' mode if satisfied."
  
elif [ "$RESTORE_MODE" = "direct" ]; then
  echo "Performing direct restore (system will be unavailable)..."
  
  # Create complete system backup before restore
  echo "Creating emergency backup of current system..."
  EMERGENCY_BACKUP="/backup/local/emergency-backup-$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$EMERGENCY_BACKUP"
  
  # Backup current databases
  /opt/gough/scripts/postgresql-backup.sh
  /opt/gough/scripts/mysql-backup.sh
  /opt/gough/scripts/config-backup.sh
  
  cp /backup/local/postgresql/gough_full_$(date +%Y%m%d_*)* "$EMERGENCY_BACKUP/" 2>/dev/null || true
  cp /backup/local/mysql/fleetdm_full_$(date +%Y%m%d_*)* "$EMERGENCY_BACKUP/" 2>/dev/null || true
  cp /backup/local/config/gough_config_$(date +%Y%m%d_*)* "$EMERGENCY_BACKUP/" 2>/dev/null || true
  
  echo "Emergency backup created at: $EMERGENCY_BACKUP"
  
  # Stop all services
  echo "Stopping all services..."
  systemctl stop gough
  
  # Restore databases
  echo "Restoring PostgreSQL database..."
  /opt/gough/scripts/postgresql-restore.sh "$POSTGRES_BACKUP" gough
  
  echo "Restoring MySQL database..."
  /opt/gough/scripts/mysql-restore.sh "$MYSQL_BACKUP" fleetdm
  
  # Restore configuration
  echo "Restoring configuration..."
  /opt/gough/scripts/config-restore.sh "$CONFIG_BACKUP" apply
  
  # Restore data volumes
  echo "Restoring data volumes..."
  docker run --rm \
    -v gough_postgresql-data:/data/postgresql \
    -v gough_mysql-data:/data/mysql \
    -v gough_gough-config:/data/config \
    -v gough_gough-logs:/data/logs \
    -v "$DATA_BACKUP":/backup.tar.gz:ro \
    alpine:latest \
    sh -c "cd / && tar -xzf /backup.tar.gz"
  
  # Start services
  echo "Starting services..."
  systemctl start gough
  
  # Verify system health
  echo "Verifying system health..."
  sleep 60
  /opt/gough/scripts/health-check.sh
  
  echo "✓ Complete system restore completed successfully"
  echo "Emergency rollback available at: $EMERGENCY_BACKUP"
fi

echo "System restore process completed at $(date)"
```

---

## Disaster Recovery

### Disaster Recovery Plan

**Recovery Time Objectives (RTO) and Recovery Point Objectives (RPO)**:

| Component | RTO | RPO | Recovery Method |
|-----------|-----|-----|-----------------|
| Database | 1 hour | 15 minutes | Point-in-time recovery |
| Application | 30 minutes | 1 hour | Container restart |
| Configuration | 15 minutes | 1 hour | Config restore |
| Complete System | 4 hours | 1 hour | Full restore |

### Disaster Scenarios and Procedures

#### Scenario 1: Database Corruption

**Detection**:
- Database connection errors
- Data integrity check failures
- Application errors

**Recovery Steps**:
```bash
# 1. Identify the issue
/opt/gough/scripts/database-health-check.sh

# 2. Stop applications
docker compose -f /opt/gough/docker-compose.production.yml stop management-server

# 3. Restore from latest backup
LATEST_BACKUP=$(ls -1 /backup/remote/postgresql/gough_full_*.backup.gz | sort -r | head -1)
/opt/gough/scripts/postgresql-restore.sh "$LATEST_BACKUP"

# 4. Verify and restart
/opt/gough/scripts/health-check.sh
docker compose -f /opt/gough/docker-compose.production.yml start management-server
```

#### Scenario 2: Complete Server Failure

**Recovery Steps**:
```bash
# 1. Provision new server
# 2. Install base system
# 3. Restore from offsite backup
aws s3 sync s3://gough-backups/prod/latest/ /tmp/restore/

# 4. Complete system restore
/opt/gough/scripts/complete-system-restore.sh $(date +%Y%m%d) direct

# 5. Update DNS/network configuration
# 6. Verify all services
```

### Backup Monitoring and Verification

#### Backup Health Monitoring

**Backup Verification Script**:
```bash
#!/bin/bash
# backup-verification.sh

set -euo pipefail

LOG_FILE="/opt/gough/logs/backup-verification-$(date +%Y%m%d).log"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

# Check if backups were created today
check_daily_backups() {
  log "Checking daily backups..."
  
  local today=$(date +%Y%m%d)
  local backup_types=("postgresql" "mysql" "config" "data")
  local missing_backups=0
  
  for backup_type in "${backup_types[@]}"; do
    local backup_count=$(find "/backup/local/$backup_type" -name "*$today*" | wc -l)
    if [ "$backup_count" -eq 0 ]; then
      log "ERROR: No $backup_type backup found for today"
      missing_backups=$((missing_backups + 1))
    else
      log "✓ $backup_type backup found ($backup_count files)"
    fi
  done
  
  return $missing_backups
}

# Test backup integrity
test_backup_integrity() {
  log "Testing backup integrity..."
  
  # Test PostgreSQL backup
  local latest_pg_backup=$(ls -1 /backup/local/postgresql/gough_full_*.backup.gz 2>/dev/null | sort -r | head -1)
  if [ -n "$latest_pg_backup" ]; then
    if gunzip -t "$latest_pg_backup" 2>/dev/null; then
      log "✓ PostgreSQL backup integrity OK"
    else
      log "ERROR: PostgreSQL backup corruption detected"
      return 1
    fi
  fi
  
  # Test MySQL backup
  local latest_mysql_backup=$(ls -1 /backup/local/mysql/fleetdm_full_*.sql.gz 2>/dev/null | sort -r | head -1)
  if [ -n "$latest_mysql_backup" ]; then
    if gunzip -t "$latest_mysql_backup" 2>/dev/null; then
      log "✓ MySQL backup integrity OK"
    else
      log "ERROR: MySQL backup corruption detected"
      return 1
    fi
  fi
  
  return 0
}

# Check remote backup synchronization
check_remote_sync() {
  log "Checking remote backup synchronization..."
  
  local local_files=$(find /backup/local -name "$(date +%Y%m%d)*" -type f | wc -l)
  local remote_files=$(find /backup/remote -name "$(date +%Y%m%d)*" -type f | wc -l)
  
  if [ "$local_files" -eq "$remote_files" ] && [ "$local_files" -gt 0 ]; then
    log "✓ Remote synchronization OK ($local_files files)"
    return 0
  else
    log "ERROR: Remote synchronization mismatch (local: $local_files, remote: $remote_files)"
    return 1
  fi
}

# Check cloud backup status
check_cloud_backup() {
  log "Checking cloud backup status..."
  
  if [ -n "${AWS_S3_BUCKET:-}" ]; then
    local cloud_files=$(aws s3 ls "s3://$AWS_S3_BUCKET/" --recursive | grep "$(date +%Y%m%d)" | wc -l)
    if [ "$cloud_files" -gt 0 ]; then
      log "✓ Cloud backup OK ($cloud_files files)"
      return 0
    else
      log "WARNING: No cloud backups found for today"
      return 1
    fi
  else
    log "Cloud backup not configured"
    return 0
  fi
}

# Generate verification report
generate_report() {
  local status="$1"
  
  cat > "/opt/gough/logs/backup-verification-report-$(date +%Y%m%d).txt" << EOF
Gough Backup Verification Report
================================
Date: $(date)
Status: $status

Backup Files Summary:
$(find /backup/local -name "*$(date +%Y%m%d)*" -type f -exec ls -lh {} \; | awk '{print $9 " - " $5}')

Storage Usage:
Local: $(du -sh /backup/local | awk '{print $1}')
Remote: $(du -sh /backup/remote | awk '{print $1}')

Recent Backup Log:
$(tail -20 "$LOG_FILE")
EOF

  # Send notification if there are issues
  if [ "$status" != "SUCCESS" ]; then
    if command -v mail >/dev/null 2>&1; then
      mail -s "Backup Verification Issues - $(date +%Y-%m-%d)" admin@company.com < "/opt/gough/logs/backup-verification-report-$(date +%Y%m%d).txt"
    fi
  fi
}

# Main verification process
main() {
  log "Starting backup verification process"
  
  local errors=0
  
  check_daily_backups || errors=$((errors + 1))
  test_backup_integrity || errors=$((errors + 1))
  check_remote_sync || errors=$((errors + 1))
  check_cloud_backup || errors=$((errors + 1))
  
  if [ $errors -eq 0 ]; then
    log "✓ All backup verification checks passed"
    generate_report "SUCCESS"
  else
    log "✗ $errors backup verification checks failed"
    generate_report "FAILED"
    exit 1
  fi
  
  log "Backup verification completed"
}

main "$@"
```

This comprehensive backup and restore documentation provides enterprise-grade data protection for the Gough hypervisor automation system, ensuring business continuity and disaster recovery capabilities.