# Production Deployment Checklist

This comprehensive checklist ensures successful and secure deployment of the Gough hypervisor automation system to production environments.

## Table of Contents

1. [Pre-Deployment Planning](#pre-deployment-planning)
2. [Infrastructure Preparation](#infrastructure-preparation)
3. [Security Configuration](#security-configuration)
4. [System Installation](#system-installation)
5. [Service Configuration](#service-configuration)
6. [Testing and Validation](#testing-and-validation)
7. [Go-Live Procedures](#go-live-procedures)
8. [Post-Deployment Verification](#post-deployment-verification)
9. [Rollback Procedures](#rollback-procedures)

---

## Pre-Deployment Planning

### 1. Requirements Validation

#### Hardware Requirements
- [ ] **Compute Resources**:
  - Minimum: 8 CPU cores, 32GB RAM, 500GB SSD per node
  - Recommended: 16 CPU cores, 64GB RAM, 1TB NVMe SSD per node
  - Network: 1Gbps minimum, 10Gbps recommended
- [ ] **Storage Requirements**:
  - Database storage: 200GB minimum, SSD recommended
  - Log storage: 100GB minimum with rotation
  - Backup storage: 3x database size minimum
- [ ] **Network Requirements**:
  - Management network: 10.0.10.0/24
  - Provisioning network: 192.168.1.0/24  
  - Production network: 10.0.100.0/24
  - DMZ network: 10.0.1.0/24

#### Software Requirements
- [ ] **Operating System**: Ubuntu 24.04 LTS Server
- [ ] **Container Runtime**: Docker 24.0+ with Docker Compose 2.0+
- [ ] **Database**: PostgreSQL 13+ and MySQL 8.0+
- [ ] **Load Balancer**: Nginx 1.20+ or HAProxy 2.4+
- [ ] **Monitoring**: Prometheus 2.40+, Grafana 9.0+
- [ ] **Security**: Fail2ban, UFW/iptables, ClamAV

#### Compliance Requirements
- [ ] **Data Protection**: GDPR/CCPA compliance if applicable
- [ ] **Security Standards**: SOC 2, ISO 27001 compliance
- [ ] **Audit Requirements**: Log retention, access auditing
- [ ] **Backup Requirements**: RTO/RPO specifications met

### 2. Environment Planning

#### Network Architecture
```
DMZ Zone (10.0.1.0/24)
├── Load Balancer (10.0.1.10)
├── Jump Host (10.0.1.20)
└── VPN Gateway (10.0.1.30)

Management Zone (10.0.10.0/24)
├── Gough Management Primary (10.0.10.10)
├── Gough Management Secondary (10.0.10.11)
├── MaaS Server (10.0.10.20)
├── FleetDM Server (10.0.10.30)
├── Database Primary (10.0.10.40)
├── Database Secondary (10.0.10.41)
├── Monitoring Server (10.0.10.50)
└── Log Server (10.0.10.60)

Provisioning Zone (192.168.1.0/24)
├── DHCP Range (192.168.1.100-200)
└── Physical Servers

Production Zone (10.0.100.0/24)
└── Deployed Servers (10.0.100.10+)
```

- [ ] **Network Segmentation**: VLANs configured and tested
- [ ] **Firewall Rules**: Security policies implemented
- [ ] **DNS Configuration**: Internal and external DNS setup
- [ ] **NTP Configuration**: Time synchronization configured
- [ ] **Certificate Authority**: SSL/TLS certificates prepared

#### Capacity Planning
- [ ] **User Capacity**: Concurrent user calculations
- [ ] **Server Capacity**: Maximum servers to manage
- [ ] **Performance Targets**: Response time requirements
- [ ] **Scalability Plan**: Horizontal scaling strategy
- [ ] **Resource Monitoring**: Capacity alerts configured

---

## Infrastructure Preparation

### 3. Host System Setup

#### Ubuntu Server Installation
- [ ] **Base Installation**:
  ```bash
  # Update system packages
  apt update && apt upgrade -y
  
  # Install essential packages
  apt install -y curl wget git vim htop tree unzip
  apt install -y software-properties-common apt-transport-https
  
  # Configure time zone
  timedatectl set-timezone UTC
  
  # Configure hostname
  hostnamectl set-hostname gough-prod-01
  ```

- [ ] **User Management**:
  ```bash
  # Create gough system user
  useradd -r -m -s /bin/bash gough
  usermod -aG docker gough
  
  # Configure SSH keys
  mkdir -p /home/gough/.ssh
  chmod 700 /home/gough/.ssh
  # Copy public keys
  chown -R gough:gough /home/gough/.ssh
  ```

- [ ] **System Hardening**:
  ```bash
  # Configure SSH
  sed -i 's/#PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
  sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
  sed -i 's/#Port 22/Port 2222/' /etc/ssh/sshd_config
  systemctl restart sshd
  
  # Configure firewall
  ufw --force reset
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow 2222/tcp
  ufw allow 8443/tcp
  ufw allow 5240/tcp
  ufw --force enable
  
  # Install and configure fail2ban
  apt install -y fail2ban
  systemctl enable fail2ban
  systemctl start fail2ban
  ```

#### Docker Installation
- [ ] **Docker Engine**:
  ```bash
  # Add Docker repository
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
  
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
  
  # Install Docker
  apt update
  apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  
  # Configure Docker daemon
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json << EOF
  {
    "log-driver": "json-file",
    "log-opts": {
      "max-size": "100m",
      "max-file": "5"
    },
    "storage-driver": "overlay2",
    "userland-proxy": false,
    "live-restore": true
  }
  EOF
  
  systemctl enable docker
  systemctl start docker
  ```

- [ ] **Docker Compose**:
  ```bash
  # Verify installation
  docker --version
  docker compose version
  
  # Test Docker functionality
  docker run hello-world
  ```

### 4. Storage Configuration

#### Database Storage
- [ ] **PostgreSQL Storage**:
  ```bash
  # Create encrypted volume for PostgreSQL
  mkdir -p /var/lib/postgresql-data
  chown -R 999:999 /var/lib/postgresql-data
  chmod 700 /var/lib/postgresql-data
  ```

- [ ] **MySQL Storage**:
  ```bash
  # Create encrypted volume for MySQL
  mkdir -p /var/lib/mysql-data
  chown -R 999:999 /var/lib/mysql-data
  chmod 700 /var/lib/mysql-data
  ```

#### Application Storage
- [ ] **Application Data**:
  ```bash
  # Create application directories
  mkdir -p /opt/gough/{config,data,logs,backups}
  chown -R gough:gough /opt/gough
  chmod 750 /opt/gough
  ```

- [ ] **Log Storage**:
  ```bash
  # Configure log rotation
  cat > /etc/logrotate.d/gough << EOF
  /opt/gough/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 644 gough gough
    postrotate
      docker kill -s USR1 \$(docker ps -q --filter ancestor=gough/management-server) 2>/dev/null || true
    endscript
  }
  EOF
  ```

### 5. Network Configuration

#### Firewall Configuration
- [ ] **iptables Rules**:
  ```bash
  # Create firewall script
  cat > /opt/gough/scripts/configure-firewall.sh << 'EOF'
  #!/bin/bash
  
  # Flush existing rules
  iptables -F
  iptables -X
  
  # Set default policies
  iptables -P INPUT DROP
  iptables -P FORWARD DROP
  iptables -P OUTPUT ACCEPT
  
  # Allow loopback
  iptables -A INPUT -i lo -j ACCEPT
  
  # Allow established connections
  iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
  
  # SSH access (from management network only)
  iptables -A INPUT -p tcp --dport 2222 -s 10.0.1.0/24 -j ACCEPT
  
  # Management portal (HTTPS)
  iptables -A INPUT -p tcp --dport 8443 -j ACCEPT
  
  # MaaS web interface
  iptables -A INPUT -p tcp --dport 5240 -j ACCEPT
  
  # FleetDM interface
  iptables -A INPUT -p tcp --dport 8444 -j ACCEPT
  
  # DNS and DHCP (provisioning network)
  iptables -A INPUT -p udp --dport 53 -s 192.168.1.0/24 -j ACCEPT
  iptables -A INPUT -p udp --dport 67 -s 192.168.1.0/24 -j ACCEPT
  iptables -A INPUT -p udp --dport 69 -s 192.168.1.0/24 -j ACCEPT
  
  # Save rules
  iptables-save > /etc/iptables/rules.v4
  EOF
  
  chmod +x /opt/gough/scripts/configure-firewall.sh
  /opt/gough/scripts/configure-firewall.sh
  ```

- [ ] **SSL Certificates**:
  ```bash
  # Create certificate directory
  mkdir -p /opt/gough/certs
  chmod 700 /opt/gough/certs
  
  # Generate self-signed certificates (replace with proper CA certs)
  openssl req -x509 -newkey rsa:4096 -keyout /opt/gough/certs/gough-key.pem -out /opt/gough/certs/gough-cert.pem -days 365 -nodes -subj "/C=US/ST=State/L=City/O=Organization/CN=gough.local"
  
  chown -R gough:gough /opt/gough/certs
  ```

---

## Security Configuration

### 6. Authentication and Authorization

#### SSL/TLS Configuration
- [ ] **Certificate Installation**:
  ```bash
  # Install certificates from CA or Let's Encrypt
  # Example for Let's Encrypt:
  apt install -y certbot
  certbot certonly --standalone -d gough.company.com
  
  # Copy certificates to application directory
  cp /etc/letsencrypt/live/gough.company.com/fullchain.pem /opt/gough/certs/
  cp /etc/letsencrypt/live/gough.company.com/privkey.pem /opt/gough/certs/
  chown gough:gough /opt/gough/certs/*
  ```

#### User Account Setup
- [ ] **Initial Admin Account**:
  ```bash
  # These credentials will be set during first run
  # Document secure password generation method
  ADMIN_PASSWORD=$(openssl rand -base64 32)
  echo "Admin password: $ADMIN_PASSWORD" > /opt/gough/config/initial-admin-password.txt
  chmod 600 /opt/gough/config/initial-admin-password.txt
  ```

### 7. Database Security

#### PostgreSQL Configuration
- [ ] **Security Settings**:
  ```bash
  # Create PostgreSQL configuration
  mkdir -p /opt/gough/config/postgresql
  
  cat > /opt/gough/config/postgresql/postgresql.conf << EOF
  # Connection settings
  listen_addresses = 'localhost'
  port = 5432
  max_connections = 100
  
  # Security settings
  ssl = on
  ssl_cert_file = '/var/lib/postgresql/server.crt'
  ssl_key_file = '/var/lib/postgresql/server.key'
  ssl_ca_file = '/var/lib/postgresql/ca.crt'
  
  # Logging settings
  logging_collector = on
  log_directory = 'log'
  log_filename = 'postgresql-%Y-%m-%d_%H%M%S.log'
  log_statement = 'all'
  log_connections = on
  log_disconnections = on
  EOF
  ```

#### MySQL Configuration
- [ ] **FleetDM Database**:
  ```bash
  # Create MySQL configuration
  mkdir -p /opt/gough/config/mysql
  
  cat > /opt/gough/config/mysql/my.cnf << EOF
  [mysqld]
  bind-address = 127.0.0.1
  port = 3306
  
  # Security settings
  local_infile = 0
  skip-show-database
  safe-user-create = 1
  
  # SSL settings
  ssl-cert = /var/lib/mysql/server.crt
  ssl-key = /var/lib/mysql/server.key
  ssl-ca = /var/lib/mysql/ca.crt
  
  # Performance settings
  innodb_buffer_pool_size = 1G
  innodb_log_file_size = 256M
  EOF
  ```

---

## System Installation

### 8. Gough Application Deployment

#### Environment Configuration
- [ ] **Environment Variables**:
  ```bash
  # Create production environment file
  cat > /opt/gough/config/.env.production << EOF
  # Application settings
  FLASK_ENV=production
  SECRET_KEY=$(openssl rand -hex 32)
  
  # Database connections
  DATABASE_URL=postgresql://gough:$(openssl rand -hex 16)@gough-postgresql:5432/gough
  MYSQL_URL=mysql://fleetdm:$(openssl rand -hex 16)@gough-mysql:3306/fleetdm
  REDIS_URL=redis://gough-redis:6379/0
  
  # External service URLs
  MAAS_URL=http://gough-maas:5240/MAAS
  MAAS_API_KEY=generate_from_maas_admin
  FLEET_URL=https://gough-fleetdm:8444
  FLEET_API_TOKEN=generate_from_fleet_admin
  
  # Security settings
  SESSION_TIMEOUT=28800
  JWT_EXPIRATION=3600
  BCRYPT_ROUNDS=12
  
  # Monitoring settings
  PROMETHEUS_METRICS=true
  LOG_LEVEL=INFO
  EOF
  
  chmod 600 /opt/gough/config/.env.production
  chown gough:gough /opt/gough/config/.env.production
  ```

#### Docker Compose Configuration
- [ ] **Production Compose File**:
  ```bash
  # Create production docker-compose.yml
  cat > /opt/gough/docker-compose.production.yml << 'EOF'
  version: '3.8'
  
  networks:
    gough-network:
      driver: bridge
      ipam:
        config:
          - subnet: 172.20.0.0/16
  
  volumes:
    postgresql-data:
      driver: local
      driver_opts:
        type: none
        o: bind
        device: /var/lib/postgresql-data
    mysql-data:
      driver: local
      driver_opts:
        type: none
        o: bind
        device: /var/lib/mysql-data
    gough-config:
      driver: local
      driver_opts:
        type: none
        o: bind
        device: /opt/gough/config
    gough-logs:
      driver: local
      driver_opts:
        type: none
        o: bind
        device: /opt/gough/logs
  
  services:
    postgresql:
      image: postgres:15
      container_name: gough-postgresql
      environment:
        POSTGRES_DB: gough
        POSTGRES_USER: gough
        POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
      volumes:
        - postgresql-data:/var/lib/postgresql/data
        - gough-config:/config:ro
      networks:
        - gough-network
      secrets:
        - postgres_password
      restart: unless-stopped
      healthcheck:
        test: ["CMD-SHELL", "pg_isready -U gough"]
        interval: 30s
        timeout: 10s
        retries: 3
  
    mysql:
      image: mysql:8.0
      container_name: gough-mysql
      environment:
        MYSQL_ROOT_PASSWORD_FILE: /run/secrets/mysql_root_password
        MYSQL_DATABASE: fleetdm
        MYSQL_USER: fleetdm
        MYSQL_PASSWORD_FILE: /run/secrets/mysql_password
      volumes:
        - mysql-data:/var/lib/mysql
      networks:
        - gough-network
      secrets:
        - mysql_root_password
        - mysql_password
      restart: unless-stopped
      healthcheck:
        test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
        interval: 30s
        timeout: 10s
        retries: 3
  
    redis:
      image: redis:7-alpine
      container_name: gough-redis
      command: redis-server --requirepass $(cat /run/secrets/redis_password)
      volumes:
        - /opt/gough/data/redis:/data
      networks:
        - gough-network
      secrets:
        - redis_password
      restart: unless-stopped
      healthcheck:
        test: ["CMD", "redis-cli", "ping"]
        interval: 30s
        timeout: 10s
        retries: 3
  
    management-server:
      image: gough/management-server:latest
      container_name: gough-management-server
      env_file:
        - /opt/gough/config/.env.production
      volumes:
        - gough-config:/app/config:ro
        - gough-logs:/app/logs
        - /opt/gough/certs:/app/certs:ro
      ports:
        - "8443:8443"
      networks:
        - gough-network
      depends_on:
        - postgresql
        - redis
      restart: unless-stopped
      healthcheck:
        test: ["CMD", "curl", "-f", "https://localhost:8443/health"]
        interval: 30s
        timeout: 10s
        retries: 3
        start_period: 60s
  
    maas-server:
      image: gough/maas-server:latest
      container_name: gough-maas-server
      privileged: true
      ports:
        - "5240:5240"
        - "53:53/udp"
        - "67:67/udp"  
        - "69:69/udp"
      volumes:
        - gough-config:/etc/maas:ro
        - /opt/gough/data/maas:/var/lib/maas
      networks:
        - gough-network
      restart: unless-stopped
      healthcheck:
        test: ["CMD", "curl", "-f", "http://localhost:5240/MAAS/"]
        interval: 60s
        timeout: 30s
        retries: 3
        start_period: 120s
  
    fleetdm-server:
      image: gough/fleetdm-server:latest
      container_name: gough-fleetdm-server
      environment:
        FLEET_MYSQL_ADDRESS: gough-mysql:3306
        FLEET_MYSQL_DATABASE: fleetdm
        FLEET_MYSQL_USERNAME: fleetdm
        FLEET_MYSQL_PASSWORD_FILE: /run/secrets/mysql_password
        FLEET_SERVER_CERT: /app/certs/gough-cert.pem
        FLEET_SERVER_KEY: /app/certs/gough-key.pem
      ports:
        - "8444:8444"
      volumes:
        - /opt/gough/certs:/app/certs:ro
        - gough-config:/app/config:ro
      networks:
        - gough-network
      depends_on:
        - mysql
      secrets:
        - mysql_password
      restart: unless-stopped
      healthcheck:
        test: ["CMD", "curl", "-f", "https://localhost:8444/api/v1/fleet/version"]
        interval: 60s
        timeout: 30s
        retries: 3
        start_period: 120s
  
    nginx-proxy:
      image: nginx:alpine
      container_name: gough-nginx-proxy
      ports:
        - "80:80"
        - "443:443"
      volumes:
        - /opt/gough/config/nginx.conf:/etc/nginx/nginx.conf:ro
        - /opt/gough/certs:/etc/nginx/certs:ro
      networks:
        - gough-network
      depends_on:
        - management-server
      restart: unless-stopped
      healthcheck:
        test: ["CMD", "curl", "-f", "http://localhost/health"]
        interval: 30s
        timeout: 10s
        retries: 3
  
  secrets:
    postgres_password:
      file: /opt/gough/config/secrets/postgres_password
    mysql_root_password:
      file: /opt/gough/config/secrets/mysql_root_password
    mysql_password:
      file: /opt/gough/config/secrets/mysql_password
    redis_password:
      file: /opt/gough/config/secrets/redis_password
  EOF
  ```

#### Secret Management
- [ ] **Generate Secrets**:
  ```bash
  # Create secrets directory
  mkdir -p /opt/gough/config/secrets
  chmod 700 /opt/gough/config/secrets
  
  # Generate database passwords
  openssl rand -hex 32 > /opt/gough/config/secrets/postgres_password
  openssl rand -hex 32 > /opt/gough/config/secrets/mysql_root_password
  openssl rand -hex 32 > /opt/gough/config/secrets/mysql_password
  openssl rand -hex 32 > /opt/gough/config/secrets/redis_password
  
  # Set proper permissions
  chmod 600 /opt/gough/config/secrets/*
  chown gough:gough /opt/gough/config/secrets/*
  ```

---

## Service Configuration

### 9. Load Balancer Configuration

#### Nginx Configuration
- [ ] **Nginx Reverse Proxy**:
  ```bash
  # Create nginx configuration
  cat > /opt/gough/config/nginx.conf << 'EOF'
  user nginx;
  worker_processes auto;
  error_log /var/log/nginx/error.log warn;
  pid /var/run/nginx.pid;
  
  events {
      worker_connections 1024;
      use epoll;
      multi_accept on;
  }
  
  http {
      include /etc/nginx/mime.types;
      default_type application/octet-stream;
      
      # Security headers
      add_header X-Frame-Options DENY always;
      add_header X-Content-Type-Options nosniff always;
      add_header X-XSS-Protection "1; mode=block" always;
      add_header Strict-Transport-Security "max-age=31536000; includeSubdomains; preload" always;
      
      # SSL configuration
      ssl_protocols TLSv1.2 TLSv1.3;
      ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
      ssl_prefer_server_ciphers off;
      ssl_session_cache shared:SSL:10m;
      ssl_session_timeout 10m;
      
      # Gzip compression
      gzip on;
      gzip_vary on;
      gzip_min_length 1024;
      gzip_types text/plain text/css text/xml text/javascript application/javascript application/json;
      
      # Rate limiting
      limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;
      limit_req_zone $binary_remote_addr zone=login:10m rate=1r/s;
      
      # Upstream servers
      upstream gough_management {
          server gough-management-server:8443 max_fails=3 fail_timeout=30s;
      }
      
      upstream gough_maas {
          server gough-maas-server:5240 max_fails=3 fail_timeout=30s;
      }
      
      upstream gough_fleetdm {
          server gough-fleetdm-server:8444 max_fails=3 fail_timeout=30s;
      }
      
      # HTTP to HTTPS redirect
      server {
          listen 80;
          server_name _;
          return 301 https://$host$request_uri;
      }
      
      # Main Gough Management Interface
      server {
          listen 443 ssl http2;
          server_name gough.company.com;
          
          ssl_certificate /etc/nginx/certs/gough-cert.pem;
          ssl_certificate_key /etc/nginx/certs/gough-key.pem;
          
          # Rate limiting
          limit_req zone=api burst=20 nodelay;
          
          location /api/auth/login {
              limit_req zone=login burst=5 nodelay;
              proxy_pass https://gough_management;
              proxy_set_header Host $host;
              proxy_set_header X-Real-IP $remote_addr;
              proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
              proxy_set_header X-Forwarded-Proto $scheme;
          }
          
          location / {
              proxy_pass https://gough_management;
              proxy_set_header Host $host;
              proxy_set_header X-Real-IP $remote_addr;
              proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
              proxy_set_header X-Forwarded-Proto $scheme;
              
              # WebSocket support
              proxy_http_version 1.1;
              proxy_set_header Upgrade $http_upgrade;
              proxy_set_header Connection "upgrade";
          }
      }
      
      # MaaS Interface
      server {
          listen 443 ssl http2;
          server_name maas.company.com;
          
          ssl_certificate /etc/nginx/certs/gough-cert.pem;
          ssl_certificate_key /etc/nginx/certs/gough-key.pem;
          
          location / {
              proxy_pass http://gough_maas;
              proxy_set_header Host $host;
              proxy_set_header X-Real-IP $remote_addr;
              proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
              proxy_set_header X-Forwarded-Proto $scheme;
          }
      }
      
      # FleetDM Interface
      server {
          listen 443 ssl http2;
          server_name fleetdm.company.com;
          
          ssl_certificate /etc/nginx/certs/gough-cert.pem;
          ssl_certificate_key /etc/nginx/certs/gough-key.pem;
          
          location / {
              proxy_pass https://gough_fleetdm;
              proxy_set_header Host $host;
              proxy_set_header X-Real-IP $remote_addr;
              proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
              proxy_set_header X-Forwarded-Proto $scheme;
              proxy_ssl_verify off;
          }
      }
  }
  EOF
  ```

### 10. System Integration

#### SystemD Service Configuration
- [ ] **Gough SystemD Service**:
  ```bash
  # Create systemd service file
  cat > /etc/systemd/system/gough.service << EOF
  [Unit]
  Description=Gough Hypervisor Automation System
  Requires=docker.service
  After=docker.service
  
  [Service]
  Type=oneshot
  RemainAfterExit=yes
  WorkingDirectory=/opt/gough
  ExecStart=/usr/bin/docker compose -f docker-compose.production.yml up -d
  ExecStop=/usr/bin/docker compose -f docker-compose.production.yml down
  TimeoutStartSec=0
  User=gough
  Group=gough
  
  [Install]
  WantedBy=multi-user.target
  EOF
  
  # Enable and start service
  systemctl daemon-reload
  systemctl enable gough
  ```

#### Monitoring Integration
- [ ] **Health Check Scripts**:
  ```bash
  # Create health check script
  cat > /opt/gough/scripts/health-check.sh << 'EOF'
  #!/bin/bash
  
  set -e
  
  echo "=== Gough Health Check $(date) ==="
  
  # Check container status
  echo "Checking container status..."
  docker compose -f /opt/gough/docker-compose.production.yml ps
  
  # Check service endpoints
  echo "Checking service endpoints..."
  curl -f -k https://localhost:8443/health || exit 1
  curl -f http://localhost:5240/MAAS/ || exit 1
  curl -f -k https://localhost:8444/api/v1/fleet/version || exit 1
  
  # Check database connectivity
  echo "Checking database connectivity..."
  docker exec gough-postgresql pg_isready -U gough || exit 1
  docker exec gough-mysql mysqladmin ping -h localhost || exit 1
  
  echo "✓ All health checks passed"
  EOF
  
  chmod +x /opt/gough/scripts/health-check.sh
  ```

---

## Testing and Validation

### 11. Component Testing

#### Database Testing
- [ ] **PostgreSQL Connection Test**:
  ```bash
  # Start services
  systemctl start gough
  sleep 60  # Allow time for startup
  
  # Test PostgreSQL
  docker exec gough-postgresql psql -U gough -d gough -c "SELECT version();"
  ```

- [ ] **MySQL Connection Test**:
  ```bash
  # Test MySQL
  docker exec gough-mysql mysql -u fleetdm -p$(cat /opt/gough/config/secrets/mysql_password) -e "SELECT VERSION();"
  ```

#### Application Testing
- [ ] **Management Server API Test**:
  ```bash
  # Test API endpoints
  curl -k https://localhost:8443/api/status
  curl -k https://localhost:8443/health
  ```

- [ ] **MaaS Integration Test**:
  ```bash
  # Test MaaS API
  curl http://localhost:5240/MAAS/api/2.0/version/
  ```

- [ ] **FleetDM Integration Test**:
  ```bash
  # Test FleetDM API
  curl -k https://localhost:8444/api/v1/fleet/version
  ```

#### Load Testing
- [ ] **Performance Baseline**:
  ```bash
  # Install Apache Bench
  apt install -y apache2-utils
  
  # Test management server performance
  ab -n 1000 -c 10 -k https://localhost:8443/api/status
  
  # Test with authentication
  # First get JWT token, then test authenticated endpoints
  TOKEN=$(curl -k -X POST https://localhost:8443/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@gough.local","password":"admin"}' | jq -r '.access_token')
  
  ab -n 500 -c 5 -H "Authorization: Bearer $TOKEN" https://localhost:8443/api/servers
  ```

### 12. Security Testing

#### SSL/TLS Testing
- [ ] **Certificate Validation**:
  ```bash
  # Test SSL configuration
  openssl s_client -connect localhost:8443 -servername gough.company.com
  
  # Check certificate expiration
  echo | openssl s_client -connect localhost:8443 2>/dev/null | openssl x509 -noout -dates
  
  # Test SSL Labs rating (if publicly accessible)
  # curl -s "https://api.ssllabs.com/api/v3/analyze?host=gough.company.com"
  ```

#### Security Scanning
- [ ] **Vulnerability Assessment**:
  ```bash
  # Install security tools
  apt install -y nmap nikto
  
  # Port scan
  nmap -sS -O localhost
  
  # Web application scan (if applicable)
  nikto -h https://localhost:8443
  ```

#### Authentication Testing
- [ ] **Login Security Test**:
  ```bash
  # Test rate limiting
  for i in {1..10}; do
    curl -k -X POST https://localhost:8443/api/auth/login \
      -H "Content-Type: application/json" \
      -d '{"email":"admin@gough.local","password":"wrong"}' &
  done
  wait
  
  # Should show rate limiting after several attempts
  ```

### 13. Integration Testing

#### End-to-End Testing
- [ ] **Complete Workflow Test**:
  ```bash
  # Create test script
  cat > /opt/gough/scripts/e2e-test.sh << 'EOF'
  #!/bin/bash
  
  set -e
  
  echo "=== Gough End-to-End Test ==="
  
  # Get authentication token
  TOKEN=$(curl -k -X POST https://localhost:8443/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@gough.local","password":"'$ADMIN_PASSWORD'"}' \
    | jq -r '.access_token')
  
  echo "✓ Authentication successful"
  
  # Test system status
  STATUS=$(curl -k -H "Authorization: Bearer $TOKEN" \
    https://localhost:8443/api/status | jq -r '.status')
  
  if [ "$STATUS" = "success" ]; then
    echo "✓ System status check passed"
  else
    echo "✗ System status check failed"
    exit 1
  fi
  
  # Test server listing
  curl -k -H "Authorization: Bearer $TOKEN" \
    https://localhost:8443/api/servers > /dev/null
  echo "✓ Server listing API working"
  
  # Test template listing
  curl -k -H "Authorization: Bearer $TOKEN" \
    https://localhost:8443/api/templates > /dev/null
  echo "✓ Template listing API working"
  
  echo "✓ All E2E tests passed"
  EOF
  
  chmod +x /opt/gough/scripts/e2e-test.sh
  ```

---

## Go-Live Procedures

### 14. Pre-Go-Live Checklist

#### Final Configuration Review
- [ ] **Configuration Audit**:
  ```bash
  # Review all configuration files
  find /opt/gough/config -name "*.conf" -o -name "*.yml" -o -name "*.yaml" | xargs ls -la
  
  # Check file permissions
  find /opt/gough -type f -perm /022 | grep -v logs || echo "No world-writable files found"
  
  # Verify secret files are secure
  find /opt/gough/config/secrets -type f ! -perm 600 | wc -l  # Should be 0
  ```

#### Service Verification
- [ ] **All Services Running**:
  ```bash
  # Check all containers are running
  docker compose -f /opt/gough/docker-compose.production.yml ps
  
  # Run comprehensive health check
  /opt/gough/scripts/health-check.sh
  
  # Check systemd service status
  systemctl status gough
  ```

#### Final Security Review
- [ ] **Security Posture Check**:
  ```bash
  # Verify firewall rules
  iptables -L -n
  
  # Check for unnecessary services
  systemctl list-unit-files --state=enabled | grep -v docker | grep -v gough
  
  # Verify SSL/TLS configuration
  nmap --script ssl-enum-ciphers -p 443,8443,8444 localhost
  ```

### 15. Go-Live Execution

#### Coordinated Startup
- [ ] **Service Startup Sequence**:
  ```bash
  # Start infrastructure services first
  docker compose -f /opt/gough/docker-compose.production.yml up -d postgresql mysql redis
  sleep 30
  
  # Start application services
  docker compose -f /opt/gough/docker-compose.production.yml up -d management-server maas-server fleetdm-server
  sleep 60
  
  # Start proxy services
  docker compose -f /opt/gough/docker-compose.production.yml up -d nginx-proxy
  sleep 10
  
  # Verify all services
  /opt/gough/scripts/health-check.sh
  ```

#### Initial Configuration
- [ ] **First-Time Setup**:
  ```bash
  # Access management interface
  echo "Access Gough at: https://$(hostname):8443"
  echo "Initial admin credentials in: /opt/gough/config/initial-admin-password.txt"
  
  # Document MaaS setup URL
  echo "Configure MaaS at: http://$(hostname):5240/MAAS"
  
  # Document FleetDM setup URL  
  echo "Configure FleetDM at: https://$(hostname):8444"
  ```

---

## Post-Deployment Verification

### 16. System Validation

#### Monitoring Setup
- [ ] **Enable System Monitoring**:
  ```bash
  # Install monitoring tools
  apt install -y prometheus-node-exporter
  systemctl enable prometheus-node-exporter
  systemctl start prometheus-node-exporter
  
  # Configure log monitoring
  apt install -y rsyslog-elasticsearch
  ```

#### Backup Verification
- [ ] **Test Backup Systems**:
  ```bash
  # Run initial backup
  /opt/gough/scripts/backup.sh
  
  # Verify backup files
  ls -la /opt/gough/backups/
  
  # Test restore procedure (on test database)
  # /opt/gough/scripts/restore.sh test
  ```

#### Performance Baseline
- [ ] **Establish Performance Metrics**:
  ```bash
  # Document resource usage
  docker stats --no-stream
  
  # Document response times
  /opt/gough/scripts/performance-test.sh > /opt/gough/logs/initial-performance.log
  ```

### 17. Documentation and Handover

#### Operational Documentation
- [ ] **Create Runbooks**:
  ```bash
  # Create operational procedures directory
  mkdir -p /opt/gough/docs/operations
  
  # Copy standard operating procedures
  cp /opt/gough/scripts/*.sh /opt/gough/docs/operations/
  
  # Document service URLs and credentials
  cat > /opt/gough/docs/operations/service-urls.txt << EOF
  Gough Management: https://$(hostname):8443
  MaaS Interface: http://$(hostname):5240/MAAS
  FleetDM Interface: https://$(hostname):8444
  
  Admin credentials: See /opt/gough/config/initial-admin-password.txt
  Database passwords: See /opt/gough/config/secrets/
  EOF
  ```

- [ ] **System Documentation**:
  - [ ] Network topology diagram
  - [ ] Service dependency map
  - [ ] Troubleshooting procedures
  - [ ] Emergency contact information
  - [ ] Escalation procedures

#### Training and Knowledge Transfer
- [ ] **Operator Training**:
  - [ ] System overview presentation
  - [ ] Hands-on training session
  - [ ] Emergency procedure walkthrough
  - [ ] Documentation review
  - [ ] Support contact information

---

## Rollback Procedures

### 18. Rollback Planning

#### Pre-Deployment Snapshot
- [ ] **System State Backup**:
  ```bash
  # Create system snapshot before deployment
  mkdir -p /backup/pre-deployment-$(date +%Y%m%d)
  
  # Backup current configuration
  tar -czf /backup/pre-deployment-$(date +%Y%m%d)/system-config.tar.gz /etc
  
  # Backup data directories
  tar -czf /backup/pre-deployment-$(date +%Y%m%d)/gough-data.tar.gz /opt/gough /var/lib/docker
  
  # Document current Docker images
  docker images > /backup/pre-deployment-$(date +%Y%m%d)/docker-images.txt
  ```

#### Emergency Rollback Procedure
- [ ] **Rapid Rollback Script**:
  ```bash
  # Create emergency rollback script
  cat > /opt/gough/scripts/emergency-rollback.sh << 'EOF'
  #!/bin/bash
  
  echo "=== EMERGENCY ROLLBACK PROCEDURE ==="
  echo "This will restore the system to pre-deployment state"
  read -p "Continue? (yes/no): " confirm
  
  if [ "$confirm" != "yes" ]; then
    echo "Rollback cancelled"
    exit 1
  fi
  
  # Stop current services
  systemctl stop gough
  docker compose -f /opt/gough/docker-compose.production.yml down -v
  
  # Restore from backup
  BACKUP_DATE=$(ls -1 /backup/ | grep pre-deployment | sort -r | head -1)
  echo "Restoring from backup: $BACKUP_DATE"
  
  # Restore configuration
  cd /
  tar -xzf /backup/$BACKUP_DATE/system-config.tar.gz
  tar -xzf /backup/$BACKUP_DATE/gough-data.tar.gz
  
  # Restart services
  systemctl start docker
  systemctl start gough
  
  echo "Rollback completed. Please verify system functionality."
  EOF
  
  chmod +x /opt/gough/scripts/emergency-rollback.sh
  ```

#### Rollback Verification
- [ ] **Post-Rollback Checks**:
  ```bash
  # Create rollback verification script
  cat > /opt/gough/scripts/verify-rollback.sh << 'EOF'
  #!/bin/bash
  
  echo "=== Rollback Verification ==="
  
  # Check system services
  systemctl status docker
  systemctl status gough
  
  # Check application health
  sleep 60  # Allow services to start
  /opt/gough/scripts/health-check.sh
  
  # Verify data integrity
  docker exec gough-postgresql pg_dump -U gough gough | wc -l
  docker exec gough-mysql mysqldump -u fleetdm -p fleetdm | wc -l
  
  echo "Rollback verification completed"
  EOF
  
  chmod +x /opt/gough/scripts/verify-rollback.sh
  ```

---

## Deployment Completion

### 19. Final Verification

#### Complete System Test
- [ ] **Full Integration Test**:
  ```bash
  # Run complete test suite
  /opt/gough/scripts/e2e-test.sh
  
  # Run performance test
  /opt/gough/scripts/performance-test.sh
  
  # Run security validation
  /opt/gough/scripts/security-test.sh
  ```

#### Monitoring and Alerting
- [ ] **Setup Monitoring Alerts**:
  - [ ] Service availability alerts
  - [ ] Performance threshold alerts
  - [ ] Security event alerts
  - [ ] Capacity utilization alerts
  - [ ] Backup failure alerts

### 20. Go-Live Sign-off

#### Stakeholder Approval
- [ ] **Technical Sign-off**:
  - [ ] System Administrator approval
  - [ ] Security team approval
  - [ ] Network team approval
  - [ ] Database team approval

- [ ] **Business Sign-off**:
  - [ ] Business owner approval
  - [ ] Operations manager approval
  - [ ] Compliance officer approval

#### Documentation Handover
- [ ] **Final Documentation Package**:
  - [ ] System architecture documentation
  - [ ] Operational procedures
  - [ ] Troubleshooting guides
  - [ ] Emergency procedures
  - [ ] Contact information
  - [ ] Support escalation procedures

---

## Post-Go-Live Activities

### 21. Ongoing Operations

#### Daily Operations
- [ ] **Daily Tasks**:
  ```bash
  # Create daily operations script
  cat > /opt/gough/scripts/daily-ops.sh << 'EOF'
  #!/bin/bash
  
  echo "=== Daily Operations Check $(date) ==="
  
  # System health check
  /opt/gough/scripts/health-check.sh
  
  # Check disk space
  df -h | grep -E "/(opt|var)"
  
  # Check service logs for errors
  docker compose -f /opt/gough/docker-compose.production.yml logs --tail=100 | grep -i error
  
  # Check system resources
  docker stats --no-stream
  
  # Verify backups
  ls -la /opt/gough/backups/ | tail -5
  
  echo "Daily operations check completed"
  EOF
  
  chmod +x /opt/gough/scripts/daily-ops.sh
  
  # Schedule daily operations
  echo "0 8 * * * gough /opt/gough/scripts/daily-ops.sh >> /opt/gough/logs/daily-ops.log 2>&1" | crontab -u gough -
  ```

#### Weekly Maintenance
- [ ] **Weekly Tasks**:
  - [ ] Security updates review and installation
  - [ ] Performance metrics review
  - [ ] Capacity planning assessment
  - [ ] Backup integrity verification
  - [ ] Log rotation and cleanup

#### Monthly Reviews
- [ ] **Monthly Tasks**:
  - [ ] Comprehensive security audit
  - [ ] Performance trend analysis
  - [ ] Capacity planning updates
  - [ ] Disaster recovery testing
  - [ ] Documentation updates

This production deployment checklist ensures a comprehensive, secure, and reliable deployment of the Gough hypervisor automation system in enterprise environments.