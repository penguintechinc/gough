# Production Deployment Guide

This guide covers deploying Gough in a production environment with security, scalability, and reliability considerations.

## Pre-Production Planning

### Infrastructure Requirements

**Minimum Hardware Requirements:**
- **CPU**: 8 cores (16 vCPU recommended)
- **Memory**: 32GB RAM (64GB recommended)
- **Storage**: 500GB SSD (1TB recommended)
- **Network**: 1Gbps network interface
- **Additional**: IPMI/BMC access for target servers

**Recommended Production Hardware:**
- **CPU**: 16+ cores with virtualization support
- **Memory**: 64GB+ RAM
- **Storage**: 1TB+ NVMe SSD with RAID 1/10
- **Network**: Multiple 1Gbps interfaces (management + provisioning)
- **Backup**: Dedicated backup storage/network

### Network Design

```
┌─────────────────────────────────────────────────────────────────┐
│                     Production Network Design                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Internet ─── Firewall ─── Management VLAN (10.0.1.0/24)      │
│                    │              │                            │
│                    │              └─ Gough Management Server   │
│                    │                                           │
│                    └─────── Provisioning VLAN (10.0.2.0/24)   │
│                                   │                            │
│                                   ├─ MaaS DHCP/PXE Server      │
│                                   ├─ Target Server 1           │
│                                   ├─ Target Server 2           │
│                                   └─ Target Server N           │
│                                                                 │
│  Monitoring VLAN (10.0.3.0/24) ─── FleetDM ─── OSQuery Agents │
└─────────────────────────────────────────────────────────────────┘
```

## Step 1: System Preparation

### Update and Secure the Host System

```bash
# Update system packages
sudo apt update && sudo apt upgrade -y

# Install essential packages
sudo apt install -y \
    curl wget git vim htop \
    ufw fail2ban \
    ca-certificates gnupg lsb-release \
    backup-manager \
    logwatch logrotate

# Configure firewall
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow 80,443/tcp
sudo ufw allow 5240:5249/tcp
sudo ufw allow 8000/tcp
sudo ufw allow 8443/tcp
sudo ufw enable

# Configure fail2ban
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

### Install Docker with Production Settings

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Configure Docker daemon for production
sudo tee /etc/docker/daemon.json << EOF
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  },
  "storage-driver": "overlay2",
  "userland-proxy": false,
  "experimental": false,
  "live-restore": true,
  "metrics-addr": "127.0.0.1:9323",
  "dns": ["8.8.8.8", "8.8.4.4"]
}
EOF

# Restart Docker with new configuration
sudo systemctl restart docker
sudo systemctl enable docker

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/download/v2.20.2/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```

## Step 2: SSL Certificate Setup

### Option A: Self-Signed Certificates (Internal Use)

```bash
# Create certificate directory
sudo mkdir -p /opt/gough/ssl

# Generate CA certificate
openssl req -new -x509 -days 3650 -nodes \
    -out /opt/gough/ssl/ca.crt \
    -keyout /opt/gough/ssl/ca.key \
    -subj "/C=US/ST=State/L=City/O=Organization/CN=Gough-CA"

# Generate server certificate
openssl req -new -nodes \
    -out /opt/gough/ssl/server.csr \
    -keyout /opt/gough/ssl/server.key \
    -subj "/C=US/ST=State/L=City/O=Organization/CN=gough.yourdomain.com"

# Sign server certificate
openssl x509 -req -days 365 \
    -in /opt/gough/ssl/server.csr \
    -CA /opt/gough/ssl/ca.crt \
    -CAkey /opt/gough/ssl/ca.key \
    -CAcreateserial \
    -out /opt/gough/ssl/server.crt

# Set appropriate permissions
sudo chown -R root:docker /opt/gough/ssl
sudo chmod 640 /opt/gough/ssl/*.key
sudo chmod 644 /opt/gough/ssl/*.crt
```

### Option B: Let's Encrypt Certificates (Public Access)

```bash
# Install certbot
sudo apt install -y certbot

# Generate certificate (requires domain and public access)
sudo certbot certonly --standalone \
    -d gough.yourdomain.com \
    --email admin@yourdomain.com \
    --agree-tos

# Copy certificates to Gough directory
sudo cp /etc/letsencrypt/live/gough.yourdomain.com/fullchain.pem /opt/gough/ssl/server.crt
sudo cp /etc/letsencrypt/live/gough.yourdomain.com/privkey.pem /opt/gough/ssl/server.key

# Set up automatic renewal
sudo crontab -e
# Add: 0 2 * * * certbot renew --quiet && systemctl reload docker
```

## Step 3: Production Configuration

### Clone and Configure Repository

```bash
# Clone repository
sudo git clone https://github.com/penguintechinc/gough.git /opt/gough
cd /opt/gough/gough

# Set ownership
sudo chown -R $(whoami):docker /opt/gough
```

### Create Production Environment File

```bash
# Create production environment
cat > .env << EOF
# Production Environment Configuration

# Network Configuration
DHCP_SUBNET=10.0.2.0/24
DHCP_RANGE_START=10.0.2.100
DHCP_RANGE_END=10.0.2.200
DHCP_GATEWAY=10.0.2.1
DHCP_DNS_SERVERS=8.8.8.8,8.8.4.4
MANAGEMENT_SUBNET=10.0.1.0/24

# Domain and SSL
DOMAIN=yourdomain.com
SSL_CERT_PATH=/opt/gough/ssl/server.crt
SSL_KEY_PATH=/opt/gough/ssl/server.key

# Database Configuration
POSTGRES_DB=management
POSTGRES_USER=gough_user
POSTGRES_PASSWORD=$(openssl rand -base64 32)
MYSQL_ROOT_PASSWORD=$(openssl rand -base64 32)
MYSQL_PASSWORD=$(openssl rand -base64 32)

# Security
SECRET_KEY=$(openssl rand -base64 64)
MAAS_USER_PASSWORD=$(openssl rand -base64 16)
FLEET_ADMIN_PASSWORD=$(openssl rand -base64 16)
JWT_SECRET_KEY=$(openssl rand -base64 32)

# SSH Keys (Replace with your actual keys)
SSH_PUBLIC_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5... admin@yourdomain.com"
ANSIBLE_SSH_PUBLIC_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5... ansible@yourdomain.com"

# Backup Configuration
BACKUP_ENABLED=true
BACKUP_RETENTION_DAYS=30
BACKUP_S3_BUCKET=gough-backups
BACKUP_S3_REGION=us-west-2

# Monitoring
PROMETHEUS_ENABLED=true
GRAFANA_ENABLED=true
LOG_LEVEL=INFO

# External Services
SMTP_SERVER=smtp.yourdomain.com
SMTP_PORT=587
SMTP_USER=gough@yourdomain.com
SMTP_PASSWORD=your-smtp-password
EOF
```

### Create Production Docker Compose Override

```bash
# Create production override file
cat > docker-compose.prod.yml << EOF
version: '3.8'

services:
  nginx:
    image: nginx:alpine
    container_name: nginx-proxy
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./config/nginx/nginx.prod.conf:/etc/nginx/nginx.conf:ro
      - ./ssl:/etc/nginx/ssl:ro
      - nginx_logs:/var/log/nginx
    depends_on:
      - management-server
      - maas
      - fleetdm
    networks:
      - maas-network
    restart: unless-stopped
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  postgres:
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./backups/postgres:/backups
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  mysql:
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
      MYSQL_PASSWORD: ${MYSQL_PASSWORD}
    volumes:
      - mysql_data:/var/lib/mysql
      - ./backups/mysql:/backups
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  management-server:
    environment:
      SECRET_KEY: ${SECRET_KEY}
      DEBUG: false
      LOG_LEVEL: ${LOG_LEVEL}
      SMTP_SERVER: ${SMTP_SERVER}
      SMTP_PORT: ${SMTP_PORT}
      SMTP_USER: ${SMTP_USER}
      SMTP_PASSWORD: ${SMTP_PASSWORD}
    volumes:
      - management_data:/opt/py4web-apps/maas_portal/databases
      - management_logs:/var/log/py4web
      - ./backups/management:/backups
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  fleetdm:
    environment:
      FLEET_ADMIN_PASSWORD: ${FLEET_ADMIN_PASSWORD}
      FLEET_TLS_CERT: /etc/ssl/server.crt
      FLEET_TLS_KEY: /etc/ssl/server.key
    volumes:
      - fleet_data:/var/lib/fleet
      - fleet_logs:/var/log/fleet
      - ./ssl:/etc/ssl:ro
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  prometheus:
    image: prom/prometheus:latest
    container_name: prometheus
    ports:
      - "9090:9090"
    volumes:
      - ./config/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--web.console.libraries=/etc/prometheus/console_libraries'
      - '--web.console.templates=/etc/prometheus/consoles'
      - '--storage.tsdb.retention.time=30d'
      - '--web.enable-lifecycle'
    networks:
      - maas-network
    restart: unless-stopped
    profiles:
      - monitoring

  grafana:
    image: grafana/grafana:latest
    container_name: grafana
    ports:
      - "3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
      - ./config/grafana:/etc/grafana/provisioning:ro
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${SECRET_KEY}
    networks:
      - maas-network
    restart: unless-stopped
    profiles:
      - monitoring

volumes:
  nginx_logs:
    driver: local
  prometheus_data:
    driver: local
  grafana_data:
    driver: local
EOF
```

## Step 4: Backup Strategy

### Automated Backup Script

```bash
# Create backup directory and script
sudo mkdir -p /opt/gough/backups/{postgres,mysql,management}

cat > /opt/gough/scripts/backup.sh << 'EOF'
#!/bin/bash
# Gough Production Backup Script

BACKUP_DIR="/opt/gough/backups"
RETENTION_DAYS=30
DATE=$(date +%Y%m%d_%H%M%S)

# Function to log messages
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" | tee -a /var/log/gough-backup.log
}

# PostgreSQL backup
log "Starting PostgreSQL backup..."
docker-compose exec -T postgres pg_dump -U postgres management > "$BACKUP_DIR/postgres/management_$DATE.sql"
if [ $? -eq 0 ]; then
    log "PostgreSQL backup completed successfully"
    gzip "$BACKUP_DIR/postgres/management_$DATE.sql"
else
    log "ERROR: PostgreSQL backup failed"
fi

# MySQL backup
log "Starting MySQL backup..."
docker-compose exec -T mysql mysqldump -u root -p${MYSQL_ROOT_PASSWORD} fleet > "$BACKUP_DIR/mysql/fleet_$DATE.sql"
if [ $? -eq 0 ]; then
    log "MySQL backup completed successfully"
    gzip "$BACKUP_DIR/mysql/fleet_$DATE.sql"
else
    log "ERROR: MySQL backup failed"
fi

# Configuration backup
log "Starting configuration backup..."
tar -czf "$BACKUP_DIR/management/config_$DATE.tar.gz" \
    --exclude='*.log' \
    --exclude='*.sock' \
    /opt/gough/gough/.env \
    /opt/gough/gough/config \
    /opt/gough/ssl
log "Configuration backup completed"

# Cleanup old backups
log "Cleaning up old backups..."
find "$BACKUP_DIR" -name "*.gz" -mtime +$RETENTION_DAYS -delete
find "$BACKUP_DIR" -name "*.sql" -mtime +$RETENTION_DAYS -delete

# Optional: Upload to S3 if configured
if [ "$BACKUP_S3_BUCKET" != "" ]; then
    log "Uploading to S3..."
    aws s3 sync $BACKUP_DIR s3://$BACKUP_S3_BUCKET/gough-backups/$(date +%Y-%m-%d)/
fi

log "Backup process completed"
EOF

chmod +x /opt/gough/scripts/backup.sh
```

### Schedule Automated Backups

```bash
# Add backup cron job
(crontab -l 2>/dev/null; echo "0 2 * * * /opt/gough/scripts/backup.sh") | crontab -
```

## Step 5: Monitoring Setup

### Create Monitoring Configuration

```bash
# Create Prometheus configuration
mkdir -p /opt/gough/gough/config/prometheus

cat > /opt/gough/gough/config/prometheus/prometheus.yml << EOF
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "alert.rules"

alerting:
  alertmanagers:
    - static_configs:
        - targets:
          - alertmanager:9093

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  - job_name: 'docker'
    static_configs:
      - targets: ['host.docker.internal:9323']

  - job_name: 'postgres'
    static_configs:
      - targets: ['postgres_exporter:9187']

  - job_name: 'mysql'
    static_configs:
      - targets: ['mysql_exporter:9104']

  - job_name: 'nginx'
    static_configs:
      - targets: ['nginx:80']
EOF

# Create Grafana provisioning
mkdir -p /opt/gough/gough/config/grafana/dashboards
mkdir -p /opt/gough/gough/config/grafana/datasources

cat > /opt/gough/gough/config/grafana/datasources/prometheus.yml << EOF
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    url: http://prometheus:9090
    isDefault: true
EOF
```

## Step 6: Production Deployment

### Deploy with Production Profile

```bash
cd /opt/gough/gough

# Start core services
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# Start monitoring services
docker-compose -f docker-compose.yml -f docker-compose.prod.yml --profile monitoring up -d

# Verify all services are running
docker-compose ps
```

### Initial Configuration

```bash
# Wait for services to be ready
sleep 60

# Check service health
curl -f http://localhost:8000/api/status
curl -f http://localhost:5240/MAAS/
curl -k -f https://localhost:8443/api/v1/fleet/version

# Access web interfaces
echo "Access points:"
echo "- Management Portal: https://yourdomain.com/"
echo "- MaaS: https://yourdomain.com:5240/MAAS/"
echo "- FleetDM: https://yourdomain.com:8443/"
echo "- Prometheus: https://yourdomain.com:9090/"
echo "- Grafana: https://yourdomain.com:3000/"
```

## Step 7: Production Hardening

### Security Hardening Checklist

```bash
# Change default passwords
echo "Please change these default credentials:"
echo "- Management Portal admin password"
echo "- MaaS admin password" 
echo "- FleetDM admin password"
echo "- Database passwords (already randomized)"

# Disable unused services
sudo systemctl disable bluetooth
sudo systemctl disable cups
sudo systemctl disable avahi-daemon

# Configure log rotation
sudo tee /etc/logrotate.d/gough << EOF
/opt/gough/gough/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 644 root root
    postrotate
        docker-compose -f /opt/gough/gough/docker-compose.yml \
                      -f /opt/gough/gough/docker-compose.prod.yml \
                      restart management-server
    endscript
}
EOF

# Set up log monitoring
sudo tee /etc/logwatch/conf/services/gough.conf << EOF
Title = "Gough System"
LogFile = /opt/gough/gough/logs/*.log
*OnlyService = gough
*RemoveHeaders
EOF
```

### Network Security

```bash
# Configure advanced firewall rules
sudo ufw insert 1 deny from any to any port 22 proto tcp
sudo ufw insert 2 allow from 10.0.1.0/24 to any port 22 proto tcp
sudo ufw insert 3 allow from YOUR_MANAGEMENT_IP to any port 22 proto tcp

# Disable ICMP ping (optional)
sudo ufw insert 1 deny in on any to any port 7,9,13,17,19,23,135,137,138,139,445,1433,1434,3306,5432,5984

# Configure fail2ban for Docker services
sudo tee /etc/fail2ban/jail.d/gough.conf << EOF
[gough-management]
enabled = true
port = 8000
filter = gough-management
logpath = /opt/gough/gough/logs/management-server.log
maxretry = 5
bantime = 3600

[gough-maas]
enabled = true
port = 5240
filter = gough-maas
logpath = /opt/gough/gough/logs/maas.log
maxretry = 3
bantime = 3600
EOF
```

## Step 8: Maintenance Procedures

### Regular Maintenance Script

```bash
cat > /opt/gough/scripts/maintenance.sh << 'EOF'
#!/bin/bash
# Gough Production Maintenance Script

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" | tee -a /var/log/gough-maintenance.log
}

cd /opt/gough/gough

log "Starting maintenance..."

# Update system packages
log "Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Clean up Docker
log "Cleaning up Docker..."
docker system prune -f
docker volume prune -f
docker image prune -a -f

# Rotate logs
log "Rotating logs..."
sudo logrotate -f /etc/logrotate.d/gough

# Check service health
log "Checking service health..."
docker-compose ps
curl -f http://localhost:8000/api/status || log "WARNING: Management server health check failed"

# Update container images
log "Updating container images..."
docker-compose pull
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d

log "Maintenance completed"
EOF

chmod +x /opt/gough/scripts/maintenance.sh

# Schedule monthly maintenance
(crontab -l 2>/dev/null; echo "0 3 1 * * /opt/gough/scripts/maintenance.sh") | crontab -
```

## Disaster Recovery

### Recovery Procedures

1. **Full System Recovery:**
```bash
# Restore from backup
cd /opt/gough/gough
docker-compose down

# Restore databases
zcat /path/to/backup/postgres/management_YYYYMMDD_HHMMSS.sql.gz | \
    docker-compose exec -T postgres psql -U postgres management

zcat /path/to/backup/mysql/fleet_YYYYMMDD_HHMMSS.sql.gz | \
    docker-compose exec -T mysql mysql -u root -p${MYSQL_ROOT_PASSWORD} fleet

# Restore configuration
tar -xzf /path/to/backup/management/config_YYYYMMDD_HHMMSS.tar.gz -C /

# Restart services
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

2. **Data Recovery:**
```bash
# Individual service recovery
docker-compose restart management-server
docker-compose restart maas-server
docker-compose restart fleetdm
```

## Production Checklist

- [ ] All passwords changed from defaults
- [ ] SSL certificates installed and valid
- [ ] Firewall configured and enabled
- [ ] Backup system operational and tested
- [ ] Monitoring systems configured
- [ ] Log rotation configured
- [ ] Maintenance scripts scheduled
- [ ] Documentation updated with environment specifics
- [ ] Team trained on operational procedures
- [ ] Disaster recovery tested
- [ ] Performance baselines established
- [ ] Security audit completed

## Performance Optimization

### Database Optimization

```bash
# PostgreSQL tuning
docker-compose exec postgres bash -c "
cat >> /var/lib/postgresql/data/postgresql.conf << EOF
shared_buffers = 256MB
effective_cache_size = 1GB
maintenance_work_mem = 64MB
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100
random_page_cost = 1.1
effective_io_concurrency = 200
work_mem = 4MB
min_wal_size = 1GB
max_wal_size = 4GB
max_worker_processes = 8
max_parallel_workers_per_gather = 4
max_parallel_workers = 8
max_parallel_maintenance_workers = 4
EOF
"

# Restart PostgreSQL
docker-compose restart postgres
```

Your production Gough deployment is now complete and hardened for enterprise use. Regular monitoring, maintenance, and security updates will ensure optimal performance and security.