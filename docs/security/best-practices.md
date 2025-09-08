# Security Best Practices for Gough Enterprise Deployment

This document provides comprehensive security guidelines and best practices for deploying and operating the Gough hypervisor automation system in enterprise environments.

## Table of Contents

1. [Security Framework Overview](#security-framework-overview)
2. [Infrastructure Security](#infrastructure-security)
3. [Network Security](#network-security)
4. [Application Security](#application-security)
5. [Access Control and Authentication](#access-control-and-authentication)
6. [Data Protection](#data-protection)
7. [Container Security](#container-security)
8. [Monitoring and Incident Response](#monitoring-and-incident-response)
9. [Compliance and Governance](#compliance-and-governance)
10. [Security Hardening Checklist](#security-hardening-checklist)

---

## Security Framework Overview

### Defense in Depth Strategy

Gough implements a multi-layered security approach with the following principles:

```
┌─────────────────────────────────────────────────────────┐
│                 Perimeter Security                      │
│  ┌─────────────────────────────────────────────────┐    │
│  │              Network Security                   │    │
│  │  ┌─────────────────────────────────────────┐    │    │
│  │  │           Host Security                 │    │    │
│  │  │  ┌─────────────────────────────────┐    │    │    │
│  │  │  │      Application Security       │    │    │    │
│  │  │  │  ┌─────────────────────────┐    │    │    │    │
│  │  │  │  │    Data Security        │    │    │    │    │
│  │  │  │  │  ┌─────────────────┐    │    │    │    │    │
│  │  │  │  │  │ Identity &      │    │    │    │    │    │
│  │  │  │  │  │ Access Mgmt     │    │    │    │    │    │
│  │  │  │  │  └─────────────────┘    │    │    │    │    │
│  │  │  │  └─────────────────────────┘    │    │    │    │
│  │  │  └─────────────────────────────────┘    │    │    │
│  │  └─────────────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

### Security Principles

1. **Zero Trust Architecture**: Never trust, always verify
2. **Principle of Least Privilege**: Minimal necessary access rights
3. **Defense in Depth**: Multiple security layers
4. **Fail Secure**: System fails to secure state
5. **Security by Design**: Built-in security controls
6. **Continuous Monitoring**: Real-time threat detection

### Threat Model

**Primary Threats**:
- Unauthorized access to management systems
- Network intrusion and lateral movement  
- Data breaches and information disclosure
- Service disruption and availability attacks
- Supply chain and dependency vulnerabilities
- Insider threats and privilege escalation

**Risk Assessment Matrix**:

| Threat Category | Likelihood | Impact | Risk Level | Mitigation Priority |
|----------------|------------|---------|------------|-------------------|
| External Attack | High | Critical | High | Immediate |
| Insider Threat | Medium | High | Medium | Short-term |
| Data Breach | Medium | Critical | High | Immediate |
| Service Disruption | High | Medium | Medium | Short-term |
| Supply Chain | Low | High | Medium | Medium-term |

---

## Infrastructure Security

### Physical Security

**Data Center Requirements**:
- Restricted physical access with multi-factor authentication
- Environmental monitoring (temperature, humidity, power)
- Surveillance systems with recording capabilities
- Secure boot and hardware attestation for servers
- Hardware security modules (HSMs) for cryptographic operations

**Server Security Configuration**:

```bash
# Secure boot configuration
mokutil --sb-state  # Verify Secure Boot status
systemctl status fwupd  # Firmware update service

# Hardware monitoring
sensors  # Temperature and voltage monitoring
smartctl -a /dev/sda  # Storage device health

# Physical tamper detection
grep -i tamper /var/log/syslog
```

### Host Operating System Security

**Ubuntu 24.04 LTS Hardening**:

1. **System Updates**:
   ```bash
   # Automated security updates
   apt install unattended-upgrades
   dpkg-reconfigure -plow unattended-upgrades
   
   # Configure automatic updates
   cat > /etc/apt/apt.conf.d/20auto-upgrades << EOF
   APT::Periodic::Update-Package-Lists "1";
   APT::Periodic::Unattended-Upgrade "1";
   APT::Periodic::AutocleanInterval "7";
   EOF
   ```

2. **Kernel Security**:
   ```bash
   # Enable kernel hardening
   cat > /etc/sysctl.d/99-security.conf << EOF
   # Network security
   net.ipv4.ip_forward = 0
   net.ipv4.conf.all.send_redirects = 0
   net.ipv4.conf.default.send_redirects = 0
   net.ipv4.conf.all.accept_redirects = 0
   net.ipv4.conf.default.accept_redirects = 0
   net.ipv4.conf.all.secure_redirects = 0
   net.ipv4.conf.default.secure_redirects = 0
   
   # Memory protection
   kernel.dmesg_restrict = 1
   kernel.kptr_restrict = 2
   kernel.yama.ptrace_scope = 1
   
   # Process security
   fs.suid_dumpable = 0
   kernel.core_pattern = |/bin/false
   EOF
   
   sysctl --system
   ```

3. **AppArmor Configuration**:
   ```bash
   # Enable AppArmor
   systemctl enable apparmor
   systemctl start apparmor
   
   # Check AppArmor status
   aa-status
   
   # Create custom profiles for Gough services
   aa-genprof gough-management-server
   ```

4. **Audit System Configuration**:
   ```bash
   # Install auditd
   apt install auditd audispd-plugins
   
   # Configure audit rules
   cat > /etc/audit/rules.d/gough.rules << EOF
   # Monitor configuration changes
   -w /etc/gough/ -p wa -k gough_config
   -w /opt/gough/ -p wa -k gough_files
   
   # Monitor privileged commands
   -a always,exit -F arch=b64 -S execve -F euid=0 -k root_commands
   
   # Monitor network connections
   -a always,exit -F arch=b64 -S socket -F a0=2 -k network_connect
   EOF
   
   systemctl restart auditd
   ```

### Container Host Security

**Docker Security Configuration**:

```json
// /etc/docker/daemon.json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "5"
  },
  "storage-driver": "overlay2",
  "userland-proxy": false,
  "live-restore": true,
  "no-new-privileges": true,
  "seccomp-profile": "/etc/docker/seccomp-profile.json",
  "userns-remap": "default",
  "authorization-plugins": ["docker-authz-plugin"]
}
```

**User Namespace Configuration**:
```bash
# Configure user namespace remapping
echo 'dockremap:165536:65536' >> /etc/subuid
echo 'dockremap:165536:65536' >> /etc/subgid

# Restart Docker daemon
systemctl restart docker
```

---

## Network Security

### Network Segmentation

**Network Architecture**:

```
┌─────────────────────────────────────────────────────────┐
│                DMZ (Public Access)                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │   WAF/LB    │  │ Jump Host   │  │ VPN Gateway │     │
│  │ 10.0.1.0/24 │  │ 10.0.1.0/24 │  │ 10.0.1.0/24 │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────┐
│              Management Network                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │   Gough     │  │   MaaS      │  │  FleetDM    │     │
│  │ Management  │  │   Server    │  │   Server    │     │
│  │ 10.0.10.0/24│  │10.0.10.0/24 │  │10.0.10.0/24 │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────┐
│            Provisioning Network                        │
│  ┌─────────────────────────────────────────────────┐   │
│  │        Physical Servers (PXE Boot)              │   │
│  │              192.168.1.0/24                     │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────┐
│             Production Network                          │
│  ┌─────────────────────────────────────────────────┐   │
│  │          Deployed Servers                       │   │
│  │            10.0.100.0/24                        │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### Firewall Configuration

**iptables Rules** (`/etc/gough/firewall-rules.sh`):

```bash
#!/bin/bash
# Gough Firewall Configuration

# Flush existing rules
iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X

# Default policies
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT

# Allow loopback
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# Allow established and related connections
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# SSH access (restricted)
iptables -A INPUT -p tcp --dport 22 -s 10.0.1.0/24 -j ACCEPT

# Management portal (HTTPS only)
iptables -A INPUT -p tcp --dport 8443 -s 10.0.1.0/24 -j ACCEPT

# MaaS API access
iptables -A INPUT -p tcp --dport 5240 -s 10.0.10.0/24 -j ACCEPT

# FleetDM API access
iptables -A INPUT -p tcp --dport 8443 -s 10.0.10.0/24 -j ACCEPT

# DNS and DHCP for provisioning
iptables -A INPUT -p udp --dport 53 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p udp --dport 67 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p udp --dport 69 -s 192.168.1.0/24 -j ACCEPT

# Database access (internal only)
iptables -A INPUT -p tcp --dport 5432 -s 172.20.0.0/16 -j ACCEPT
iptables -A INPUT -p tcp --dport 3306 -s 172.20.0.0/16 -j ACCEPT

# Redis cache (internal only)
iptables -A INPUT -p tcp --dport 6379 -s 172.20.0.0/16 -j ACCEPT

# Drop everything else
iptables -A INPUT -j LOG --log-prefix "DROPPED: "
iptables -A INPUT -j DROP

# Save rules
iptables-save > /etc/iptables/rules.v4
```

### SSL/TLS Configuration

**Certificate Management**:

```bash
# Generate production certificates with Let's Encrypt
certbot certonly --standalone \
  -d gough.company.com \
  -d maas.company.com \
  -d fleetdm.company.com \
  --email admin@company.com \
  --agree-tos \
  --non-interactive

# Certificate renewal automation
cat > /etc/cron.d/certbot << EOF
0 2 * * * certbot renew --post-hook "docker-compose restart nginx-proxy"
EOF
```

**TLS Configuration** (`nginx.conf`):

```nginx
server {
    listen 443 ssl http2;
    server_name gough.company.com;
    
    # TLS Configuration
    ssl_certificate /etc/letsencrypt/live/gough.company.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/gough.company.com/privkey.pem;
    
    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    add_header Content-Security-Policy "default-src 'self'" always;
    
    # TLS settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;
    
    # OCSP stapling
    ssl_stapling on;
    ssl_stapling_verify on;
    ssl_trusted_certificate /etc/letsencrypt/live/gough.company.com/chain.pem;
    
    # Proxy to management server
    location / {
        proxy_pass http://gough-management-server:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Network Monitoring

**Intrusion Detection System** (`suricata.yaml`):

```yaml
vars:
  address-groups:
    HOME_NET: "[10.0.0.0/8,192.168.0.0/16,172.16.0.0/12]"
    EXTERNAL_NET: "!$HOME_NET"
    SERVERS: "[10.0.10.0/24]"
    DMZ: "[10.0.1.0/24]"

default-rule-path: /etc/suricata/rules

rule-files:
  - suricata.rules
  - gough-custom.rules

# Logging configuration
outputs:
  - eve-log:
      enabled: yes
      filetype: json
      filename: eve.json
      types:
        - alert:
            payload: yes
            packet: yes
            http-body: yes
        - http:
            extended: yes
        - dns:
            query: yes
            answer: yes
        - tls:
            extended: yes
        - files:
            force-magic: no
        - smtp:
        - ftp:
        - ssh:
        - stats:
            totals: yes
            threads: no
```

**Custom Suricata Rules** (`gough-custom.rules`):

```
# Gough-specific security rules
alert tcp any any -> $SERVERS 8000 (msg:"Gough Management API Access"; sid:1000001; rev:1;)
alert tcp any any -> $SERVERS 5240 (msg:"MaaS API Access"; sid:1000002; rev:1;)
alert tcp any any -> $SERVERS 8443 (msg:"FleetDM API Access"; sid:1000003; rev:1;)

# Suspicious activity
alert tcp any any -> $SERVERS any (msg:"Multiple Failed Login Attempts"; content:"401"; threshold:type both,track by_src,count 5,seconds 60; sid:1000004; rev:1;)
alert tcp any any -> $SERVERS any (msg:"SQL Injection Attempt"; content:"UNION"; content:"SELECT"; distance:0; within:100; sid:1000005; rev:1;)
alert tcp any any -> $SERVERS any (msg:"Command Injection Attempt"; pcre:"/[\|\&\;\`\$\(\)]/"; sid:1000006; rev:1;)

# Data exfiltration detection
alert tcp $SERVERS any -> $EXTERNAL_NET any (msg:"Large Data Transfer"; flow:established; dsize:>10000; threshold:type threshold,track by_src,count 100,seconds 60; sid:1000007; rev:1;)
```

---

## Application Security

### Web Application Security

**Security Headers Configuration**:

```python
# Flask security headers
from flask import Flask
from flask_talisman import Talisman

app = Flask(__name__)

# Content Security Policy
csp = {
    'default-src': "'self'",
    'script-src': "'self' 'unsafe-inline'",
    'style-src': "'self' 'unsafe-inline'",
    'img-src': "'self' data:",
    'font-src': "'self'",
    'connect-src': "'self'",
    'frame-ancestors': "'none'",
    'form-action': "'self'",
    'base-uri': "'self'"
}

Talisman(app, 
    force_https=True,
    strict_transport_security=True,
    content_security_policy=csp,
    referrer_policy='strict-origin-when-cross-origin',
    feature_policy={
        'geolocation': "'none'",
        'microphone': "'none'",
        'camera': "'none'"
    }
)
```

**Input Validation and Sanitization**:

```python
from marshmallow import Schema, fields, validate, ValidationError
import bleach

class ServerCreateSchema(Schema):
    hostname = fields.Str(
        required=True,
        validate=validate.Regexp(
            r'^[a-zA-Z0-9][a-zA-Z0-9\-]{0,61}[a-zA-Z0-9]$',
            error="Invalid hostname format"
        )
    )
    mac_address = fields.Str(
        required=True,
        validate=validate.Regexp(
            r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$',
            error="Invalid MAC address format"
        )
    )
    tags = fields.List(
        fields.Str(validate=validate.Length(min=1, max=50)),
        validate=validate.Length(max=10)
    )

def sanitize_input(data):
    """Sanitize user input to prevent XSS"""
    if isinstance(data, str):
        return bleach.clean(data, tags=[], strip=True)
    elif isinstance(data, dict):
        return {k: sanitize_input(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_input(item) for item in data]
    return data
```

### API Security

**JWT Token Security**:

```python
import jwt
import secrets
from datetime import datetime, timedelta
from functools import wraps

class JWTManager:
    def __init__(self, secret_key=None, algorithm='HS256'):
        self.secret_key = secret_key or secrets.token_urlsafe(32)
        self.algorithm = algorithm
        self.token_blacklist = set()
    
    def generate_token(self, user_id, roles, expires_delta=None):
        if expires_delta is None:
            expires_delta = timedelta(hours=8)
        
        expire = datetime.utcnow() + expires_delta
        payload = {
            'user_id': user_id,
            'roles': roles,
            'exp': expire,
            'iat': datetime.utcnow(),
            'iss': 'gough-management-server',
            'aud': 'gough-api'
        }
        
        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)
    
    def verify_token(self, token):
        try:
            if token in self.token_blacklist:
                raise jwt.InvalidTokenError("Token has been revoked")
            
            payload = jwt.decode(
                token, 
                self.secret_key, 
                algorithms=[self.algorithm],
                audience='gough-api',
                issuer='gough-management-server'
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise jwt.InvalidTokenError("Token has expired")
        except jwt.InvalidTokenError as e:
            raise e
    
    def revoke_token(self, token):
        self.token_blacklist.add(token)

# Rate limiting decorator
def rate_limit(max_requests=100, window_seconds=3600):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            client_ip = request.environ.get('HTTP_X_REAL_IP', request.remote_addr)
            key = f"rate_limit:{client_ip}:{f.__name__}"
            
            current_requests = redis_client.get(key)
            if current_requests is None:
                redis_client.setex(key, window_seconds, 1)
            else:
                if int(current_requests) >= max_requests:
                    return {"error": "Rate limit exceeded"}, 429
                redis_client.incr(key)
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator
```

### Database Security

**Database Connection Security**:

```python
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
import ssl

# Secure database connection
DATABASE_CONFIG = {
    'host': 'gough-postgresql',
    'port': 5432,
    'database': 'gough',
    'username': 'gough_app',
    'password': os.environ['DB_PASSWORD'],
    'sslmode': 'require',
    'sslcert': '/app/certs/client-cert.pem',
    'sslkey': '/app/certs/client-key.pem',
    'sslrootcert': '/app/certs/ca-cert.pem'
}

# Connection string with SSL
DATABASE_URL = (
    f"postgresql://{DATABASE_CONFIG['username']}:"
    f"{DATABASE_CONFIG['password']}@{DATABASE_CONFIG['host']}:"
    f"{DATABASE_CONFIG['port']}/{DATABASE_CONFIG['database']}"
    f"?sslmode={DATABASE_CONFIG['sslmode']}"
    f"&sslcert={DATABASE_CONFIG['sslcert']}"
    f"&sslkey={DATABASE_CONFIG['sslkey']}"
    f"&sslrootcert={DATABASE_CONFIG['sslrootcert']}"
)

engine = create_engine(
    DATABASE_URL,
    poolclass=NullPool,  # Prevent connection pooling for security
    echo=False,  # Don't log SQL queries
    pool_pre_ping=True,
    pool_recycle=3600
)
```

**SQL Injection Prevention**:

```python
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

Session = sessionmaker(bind=engine)

def get_server_by_id(server_id):
    """Secure database query using parameterized statements"""
    session = Session()
    try:
        # Use parameterized query to prevent SQL injection
        query = text("""
            SELECT id, hostname, status, created_at 
            FROM servers 
            WHERE id = :server_id AND deleted_at IS NULL
        """)
        
        result = session.execute(query, {'server_id': server_id})
        return result.fetchone()
    finally:
        session.close()

# ORM-based queries (also secure)
def search_servers(hostname_pattern):
    session = Session()
    try:
        servers = session.query(Server).filter(
            Server.hostname.like(f"%{hostname_pattern}%"),
            Server.deleted_at.is_(None)
        ).all()
        return servers
    finally:
        session.close()
```

---

## Access Control and Authentication

### Multi-Factor Authentication

**TOTP Implementation**:

```python
import pyotp
import qrcode
from io import BytesIO
import base64

class MFAManager:
    def __init__(self):
        self.issuer = "Gough Management System"
    
    def generate_secret(self, user_email):
        """Generate TOTP secret for user"""
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        
        # Generate QR code
        provisioning_uri = totp.provisioning_uri(
            name=user_email,
            issuer_name=self.issuer
        )
        
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(provisioning_uri)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        qr_code_data = base64.b64encode(buffer.getvalue()).decode()
        
        return secret, qr_code_data
    
    def verify_token(self, secret, token):
        """Verify TOTP token"""
        totp = pyotp.TOTP(secret)
        return totp.verify(token, valid_window=1)  # Allow 30-second window
```

### Role-Based Access Control (RBAC)

**Permission System**:

```python
from enum import Enum
from functools import wraps

class Permission(Enum):
    # System permissions
    SYSTEM_READ = "system:read"
    SYSTEM_WRITE = "system:write"
    SYSTEM_ADMIN = "system:admin"
    
    # Server permissions
    SERVER_READ = "server:read"
    SERVER_WRITE = "server:write"
    SERVER_DEPLOY = "server:deploy"
    SERVER_DELETE = "server:delete"
    
    # Template permissions
    TEMPLATE_READ = "template:read"
    TEMPLATE_WRITE = "template:write"
    
    # User permissions
    USER_READ = "user:read"
    USER_WRITE = "user:write"
    USER_ADMIN = "user:admin"

class Role:
    VIEWER = "viewer"
    OPERATOR = "operator"  
    ADMIN = "admin"

ROLE_PERMISSIONS = {
    Role.VIEWER: [
        Permission.SYSTEM_READ,
        Permission.SERVER_READ,
        Permission.TEMPLATE_READ,
        Permission.USER_READ
    ],
    Role.OPERATOR: [
        Permission.SYSTEM_READ,
        Permission.SERVER_READ,
        Permission.SERVER_WRITE,
        Permission.SERVER_DEPLOY,
        Permission.TEMPLATE_READ,
        Permission.TEMPLATE_WRITE,
        Permission.USER_READ
    ],
    Role.ADMIN: [
        Permission.SYSTEM_READ,
        Permission.SYSTEM_WRITE,
        Permission.SYSTEM_ADMIN,
        Permission.SERVER_READ,
        Permission.SERVER_WRITE,
        Permission.SERVER_DEPLOY,
        Permission.SERVER_DELETE,
        Permission.TEMPLATE_READ,
        Permission.TEMPLATE_WRITE,
        Permission.USER_READ,
        Permission.USER_WRITE,
        Permission.USER_ADMIN
    ]
}

def require_permission(permission):
    """Decorator to enforce permission requirements"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            current_user = get_current_user()
            if not current_user:
                return {"error": "Authentication required"}, 401
            
            user_permissions = get_user_permissions(current_user)
            if permission not in user_permissions:
                return {"error": "Insufficient permissions"}, 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# Usage example
@app.route('/api/servers', methods=['POST'])
@require_permission(Permission.SERVER_WRITE)
def create_server():
    # Server creation logic
    pass
```

### Session Management

**Secure Session Configuration**:

```python
from flask_session import Session
import redis

app.config.update(
    SECRET_KEY=os.environ['FLASK_SECRET_KEY'],
    SESSION_TYPE='redis',
    SESSION_REDIS=redis.from_url('redis://gough-redis:6379/1'),
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
    SESSION_KEY_PREFIX='gough:session:',
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8)
)

Session(app)

def logout_user():
    """Secure logout with session cleanup"""
    session_id = session.get('session_id')
    if session_id:
        # Add to blacklist
        redis_client.sadd('blacklisted_sessions', session_id)
        redis_client.expire('blacklisted_sessions', 86400 * 7)  # 7 days
    
    session.clear()
```

---

## Data Protection

### Encryption at Rest

**Database Encryption Configuration**:

```sql
-- PostgreSQL TDE (Transparent Data Encryption)
-- Configure in postgresql.conf
data_encryption = on
data_encryption_key = 'your-encryption-key'

-- Create encrypted tablespace
CREATE TABLESPACE encrypted_data 
LOCATION '/var/lib/postgresql/encrypted'
WITH (encryption_key = 'tablespace-key');

-- Create tables in encrypted tablespace
CREATE TABLE servers (
    id SERIAL PRIMARY KEY,
    hostname VARCHAR(255) NOT NULL,
    mac_address MACADDR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) TABLESPACE encrypted_data;
```

**File System Encryption**:

```bash
# LUKS encryption for data volumes
cryptsetup luksFormat /dev/sdb1
cryptsetup luksOpen /dev/sdb1 gough-data
mkfs.ext4 /dev/mapper/gough-data

# Mount encrypted filesystem
mkdir -p /var/lib/gough/encrypted
mount /dev/mapper/gough-data /var/lib/gough/encrypted

# Configure automatic mounting
echo 'gough-data /dev/sdb1 none luks' >> /etc/crypttab
echo '/dev/mapper/gough-data /var/lib/gough/encrypted ext4 defaults 0 2' >> /etc/fstab
```

### Encryption in Transit

**API Encryption**:

```python
from cryptography.fernet import Fernet
import ssl

class APIEncryption:
    def __init__(self, key=None):
        self.key = key or Fernet.generate_key()
        self.cipher = Fernet(self.key)
    
    def encrypt_api_payload(self, data):
        """Encrypt API payload"""
        json_data = json.dumps(data)
        encrypted_data = self.cipher.encrypt(json_data.encode())
        return base64.b64encode(encrypted_data).decode()
    
    def decrypt_api_payload(self, encrypted_data):
        """Decrypt API payload"""
        encrypted_bytes = base64.b64decode(encrypted_data.encode())
        decrypted_data = self.cipher.decrypt(encrypted_bytes)
        return json.loads(decrypted_data.decode())

# TLS configuration for internal services
def create_ssl_context():
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations('/app/certs/ca-cert.pem')
    context.load_cert_chain('/app/certs/client-cert.pem', '/app/certs/client-key.pem')
    return context
```

### Data Loss Prevention (DLP)

**Sensitive Data Detection**:

```python
import re
from enum import Enum

class SensitiveDataType(Enum):
    SSN = r'\b\d{3}-\d{2}-\d{4}\b'
    CREDIT_CARD = r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'
    EMAIL = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    IP_ADDRESS = r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'
    API_KEY = r'\b[A-Za-z0-9]{32,}\b'

class DataLossPreventionScanner:
    def __init__(self):
        self.patterns = {pattern_type: re.compile(pattern.value, re.IGNORECASE) 
                        for pattern_type, pattern in SensitiveDataType.__members__.items()}
    
    def scan_text(self, text):
        """Scan text for sensitive data"""
        findings = {}
        for data_type, pattern in self.patterns.items():
            matches = pattern.findall(text)
            if matches:
                findings[data_type.name] = len(matches)
        return findings
    
    def sanitize_logs(self, log_message):
        """Remove sensitive data from log messages"""
        sanitized = log_message
        for pattern in self.patterns.values():
            sanitized = pattern.sub('[REDACTED]', sanitized)
        return sanitized
```

### Backup Security

**Encrypted Backup Configuration**:

```bash
#!/bin/bash
# secure-backup.sh

BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backup"
ENCRYPTION_KEY="/etc/gough/backup.key"
GPG_RECIPIENT="admin@company.com"

# Database backup with encryption
docker exec gough-postgresql pg_dump -U gough gough | \
  gpg --trust-model always --encrypt --recipient "$GPG_RECIPIENT" \
  --output "$BACKUP_DIR/database_$BACKUP_DATE.sql.gpg"

# Configuration backup with encryption
tar -czf - /etc/gough /opt/gough/config | \
  gpg --trust-model always --encrypt --recipient "$GPG_RECIPIENT" \
  --output "$BACKUP_DIR/config_$BACKUP_DATE.tar.gz.gpg"

# Verify backup integrity
gpg --verify "$BACKUP_DIR/database_$BACKUP_DATE.sql.gpg"
gpg --verify "$BACKUP_DIR/config_$BACKUP_DATE.tar.gz.gpg"

# Secure backup to remote location
rsync -avz --delete "$BACKUP_DIR/" \
  backup-server:/secure/backups/gough/

# Cleanup old backups (keep 30 days)
find "$BACKUP_DIR" -name "*.gpg" -mtime +30 -delete
```

---

## Container Security

### Container Image Security

**Dockerfile Security Best Practices**:

```dockerfile
# Use minimal base images
FROM ubuntu:24.04-minimal

# Create non-root user
RUN groupadd -r gough && useradd -r -g gough gough

# Install security updates
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Copy application files
COPY --chown=gough:gough requirements.txt /app/
COPY --chown=gough:gough . /app/

# Install Python dependencies
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# Set working directory and user
WORKDIR /app
USER gough

# Remove unnecessary packages
RUN apt-get autoremove -y && \
    apt-get autoclean

# Use HEALTHCHECK
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Set security-focused CMD
CMD ["python3", "app.py"]
```

**Container Runtime Security**:

```yaml
# docker-compose.yml security configuration
version: '3.8'
services:
  management-server:
    image: gough/management-server:latest
    security_opt:
      - no-new-privileges:true
      - apparmor:gough-management
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - SETGID
      - SETUID
    read_only: true
    tmpfs:
      - /tmp
      - /var/run
    user: "1001:1001"
    ulimits:
      nproc: 1024
      nofile:
        soft: 1024
        hard: 2048
    mem_limit: 512m
    cpus: 0.5
    restart: unless-stopped
```

### Image Scanning and Vulnerability Management

**Automated Image Scanning**:

```yaml
# .github/workflows/security-scan.yml
name: Security Scan
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  container-scan:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v4
        
      - name: Build Docker image
        run: docker build -t gough/management-server:${{ github.sha }} .
        
      - name: Scan image with Trivy
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: gough/management-server:${{ github.sha }}
          format: 'sarif'
          output: 'trivy-results.sarif'
          severity: 'CRITICAL,HIGH'
          
      - name: Upload Trivy scan results
        uses: github/codeql-action/upload-sarif@v2
        if: always()
        with:
          sarif_file: 'trivy-results.sarif'
          
      - name: Scan with Snyk
        uses: snyk/actions/docker@master
        env:
          SNYK_TOKEN: ${{ secrets.SNYK_TOKEN }}
        with:
          image: gough/management-server:${{ github.sha }}
          args: --severity-threshold=high
```

### Runtime Protection

**Falco Security Rules** (`falco-rules.yaml`):

```yaml
# Gough-specific Falco rules
- rule: Gough Container Privilege Escalation
  desc: Detect privilege escalation in Gough containers
  condition: >
    spawned_process and container and
    (container.image.repository contains "gough") and
    (proc.name in (su, sudo, doas)) and
    not proc.pname in (systemd, systemd-user, init)
  output: >
    Privilege escalation in Gough container 
    (user=%user.name command=%proc.cmdline container=%container.name image=%container.image.repository)
  priority: CRITICAL

- rule: Gough Sensitive File Access
  desc: Detect access to sensitive Gough configuration files
  condition: >
    open_read and container and
    (container.image.repository contains "gough") and
    (fd.name startswith "/etc/gough/" or 
     fd.name startswith "/app/config/" or
     fd.name contains "secret" or
     fd.name contains "key")
  output: >
    Sensitive file access in Gough container 
    (file=%fd.name container=%container.name image=%container.image.repository)
  priority: WARNING

- rule: Gough Network Anomaly
  desc: Detect unusual network connections from Gough containers
  condition: >
    outbound and container and
    (container.image.repository contains "gough") and
    not fd.sip in (gough_allowed_ips) and
    not fd.sport in (80, 443, 53, 5432, 3306, 6379)
  output: >
    Unusual network connection from Gough container 
    (connection=%fd.sip:%fd.sport container=%container.name)
  priority: WARNING
```

---

## Monitoring and Incident Response

### Security Information and Event Management (SIEM)

**ELK Stack Security Configuration**:

```yaml
# elasticsearch.yml
cluster.name: gough-security
node.name: gough-elasticsearch
network.host: 0.0.0.0
discovery.type: single-node

# Security settings
xpack.security.enabled: true
xpack.security.transport.ssl.enabled: true
xpack.security.http.ssl.enabled: true
xpack.security.authc.api_key.enabled: true

# SSL configuration
xpack.security.transport.ssl.keystore.path: elastic-certificates.p12
xpack.security.transport.ssl.truststore.path: elastic-certificates.p12
xpack.security.http.ssl.keystore.path: elastic-certificates.p12
```

**Logstash Security Parsing** (`logstash.conf`):

```ruby
input {
  beats {
    port => 5044
    ssl => true
    ssl_certificate => "/etc/logstash/certs/logstash.crt"
    ssl_key => "/etc/logstash/certs/logstash.key"
  }
}

filter {
  if [fields][service] == "gough-management" {
    grok {
      match => { "message" => "%{TIMESTAMP_ISO8601:timestamp} %{LOGLEVEL:level} %{DATA:component} %{GREEDYDATA:message}" }
    }
    
    # Detect security events
    if [message] =~ /(?i)fail.*login|authentication.*fail|unauthorized|403|401/ {
      mutate {
        add_field => { "security_event" => "authentication_failure" }
        add_field => { "severity" => "high" }
      }
    }
    
    if [message] =~ /(?i)sql.*injection|union.*select|script.*alert/ {
      mutate {
        add_field => { "security_event" => "injection_attempt" }
        add_field => { "severity" => "critical" }
      }
    }
  }
  
  # GeoIP enrichment
  if [client_ip] {
    geoip {
      source => "client_ip"
      target => "geoip"
    }
  }
}

output {
  elasticsearch {
    hosts => ["gough-elasticsearch:9200"]
    ssl => true
    ssl_certificate_verification => true
    cacert => "/etc/logstash/certs/ca.crt"
    user => "logstash_writer"
    password => "${LOGSTASH_PASSWORD}"
    index => "gough-security-%{+YYYY.MM.dd}"
  }
}
```

### Incident Response Procedures

**Automated Incident Response** (`incident-response.py`):

```python
import logging
import smtplib
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

class IncidentResponseManager:
    def __init__(self):
        self.severity_levels = {
            'low': 1,
            'medium': 2, 
            'high': 3,
            'critical': 4
        }
        
    def handle_security_incident(self, incident_data):
        """Handle security incident based on severity"""
        severity = incident_data.get('severity', 'medium')
        incident_type = incident_data.get('type', 'unknown')
        
        # Log incident
        logging.critical(f"Security incident detected: {incident_type} - {severity}")
        
        # Immediate response actions
        if severity == 'critical':
            self.immediate_lockdown(incident_data)
            self.notify_security_team(incident_data, urgent=True)
            self.escalate_to_management(incident_data)
        elif severity == 'high':
            self.implement_containment(incident_data)
            self.notify_security_team(incident_data)
        else:
            self.log_and_monitor(incident_data)
    
    def immediate_lockdown(self, incident_data):
        """Immediate system lockdown for critical incidents"""
        affected_systems = incident_data.get('affected_systems', [])
        
        for system in affected_systems:
            # Disable affected user accounts
            if 'user_id' in incident_data:
                self.disable_user_account(incident_data['user_id'])
            
            # Block suspicious IP addresses
            if 'source_ip' in incident_data:
                self.block_ip_address(incident_data['source_ip'])
            
            # Isolate affected containers
            if 'container_id' in incident_data:
                self.isolate_container(incident_data['container_id'])
    
    def notify_security_team(self, incident_data, urgent=False):
        """Send notification to security team"""
        subject = f"{'URGENT: ' if urgent else ''}Security Incident - {incident_data.get('type', 'Unknown')}"
        
        body = f"""
        Security Incident Report
        
        Time: {datetime.now().isoformat()}
        Severity: {incident_data.get('severity', 'Unknown')}
        Type: {incident_data.get('type', 'Unknown')}
        Source IP: {incident_data.get('source_ip', 'Unknown')}
        Affected Systems: {', '.join(incident_data.get('affected_systems', []))}
        
        Description: {incident_data.get('description', 'No description available')}
        
        Recommended Actions:
        {self.get_recommended_actions(incident_data)}
        """
        
        self.send_email('security-team@company.com', subject, body)
        
        # Also send to Slack if configured
        self.send_slack_alert(incident_data)
    
    def get_recommended_actions(self, incident_data):
        """Get recommended response actions"""
        incident_type = incident_data.get('type', '')
        
        actions = {
            'authentication_failure': [
                "Review user access logs",
                "Check for brute force patterns",
                "Consider implementing account lockout",
                "Verify user identity if legitimate"
            ],
            'injection_attempt': [
                "Block source IP immediately",
                "Review application input validation",
                "Check database logs for compromise",
                "Update WAF rules if applicable"
            ],
            'privilege_escalation': [
                "Isolate affected system immediately",
                "Review system logs for timeline",
                "Check for persistence mechanisms",
                "Validate user permissions"
            ]
        }
        
        return '\n'.join(f"- {action}" for action in actions.get(incident_type, ["Manual investigation required"]))
```

### Security Metrics and Reporting

**Security Dashboard Configuration** (`security-dashboard.json`):

```json
{
  "dashboard": {
    "title": "Gough Security Dashboard",
    "tags": ["security", "gough"],
    "timezone": "UTC",
    "panels": [
      {
        "title": "Security Events Over Time",
        "type": "graph",
        "targets": [
          {
            "expr": "increase(security_events_total[1h])",
            "legendFormat": "{{severity}} - {{event_type}}"
          }
        ]
      },
      {
        "title": "Failed Authentication Attempts",
        "type": "stat",
        "targets": [
          {
            "expr": "sum(increase(authentication_failures_total[24h]))"
          }
        ]
      },
      {
        "title": "Geographic Distribution of Threats",
        "type": "worldmap",
        "targets": [
          {
            "expr": "sum by (country) (threat_events_by_country)"
          }
        ]
      },
      {
        "title": "Top Threat Sources",
        "type": "table",
        "targets": [
          {
            "expr": "topk(10, sum by (source_ip) (security_events_total))"
          }
        ]
      }
    ]
  }
}
```

---

## Compliance and Governance

### Compliance Frameworks

**SOC 2 Type II Controls**:

1. **Security Principle**:
   - Multi-factor authentication implementation
   - Regular access reviews and provisioning
   - Vulnerability management program
   - Incident response procedures

2. **Availability Principle**:
   - High availability architecture
   - Disaster recovery procedures
   - Performance monitoring
   - Capacity planning

3. **Processing Integrity Principle**:
   - Input validation controls
   - Data processing accuracy
   - Error handling procedures
   - Transaction logging

4. **Confidentiality Principle**:
   - Data encryption in transit and at rest
   - Access controls and segregation
   - Data loss prevention
   - Secure disposal procedures

**ISO 27001 Controls Implementation**:

```yaml
# Information Security Management System (ISMS)
security_controls:
  A.5_Information_Security_Policies:
    - security_policy_document: "/docs/security/policy.md"
    - policy_review_schedule: "annual"
    - approval_authority: "CISO"
  
  A.6_Organization_of_Information_Security:
    - security_roles_defined: true
    - incident_response_team: true
    - security_governance_committee: true
  
  A.8_Human_Resource_Security:
    - background_checks: true
    - security_awareness_training: true
    - termination_procedures: true
  
  A.9_Access_Control:
    - access_control_policy: true
    - user_access_management: true
    - privileged_access_management: true
    - network_access_control: true
  
  A.10_Cryptography:
    - cryptographic_controls: true
    - key_management: true
    - digital_signatures: true
  
  A.12_Operations_Security:
    - operational_procedures: true
    - change_management: true
    - capacity_management: true
    - malware_protection: true
  
  A.13_Communications_Security:
    - network_security_management: true
    - network_segregation: true
    - information_transfer: true
  
  A.14_System_Acquisition_Development_Maintenance:
    - secure_development_lifecycle: true
    - security_testing: true
    - production_acceptance: true
```

### Audit and Compliance Monitoring

**Automated Compliance Checking** (`compliance-check.py`):

```python
import subprocess
import json
from datetime import datetime

class ComplianceChecker:
    def __init__(self):
        self.compliance_results = {}
    
    def check_password_policy(self):
        """Check password policy compliance"""
        try:
            # Check password policy settings
            result = subprocess.run(['grep', '-E', 'password.*pam_pwquality', '/etc/pam.d/common-password'], 
                                  capture_output=True, text=True)
            
            self.compliance_results['password_policy'] = {
                'compliant': result.returncode == 0,
                'evidence': result.stdout,
                'requirement': 'SOC2-CC6.1'
            }
        except Exception as e:
            self.compliance_results['password_policy'] = {
                'compliant': False,
                'error': str(e),
                'requirement': 'SOC2-CC6.1'
            }
    
    def check_encryption_at_rest(self):
        """Check encryption at rest compliance"""
        try:
            # Check if data volumes are encrypted
            result = subprocess.run(['lsblk', '-f'], capture_output=True, text=True)
            encrypted_volumes = 'crypto_LUKS' in result.stdout
            
            self.compliance_results['encryption_at_rest'] = {
                'compliant': encrypted_volumes,
                'evidence': result.stdout,
                'requirement': 'SOC2-CC6.7'
            }
        except Exception as e:
            self.compliance_results['encryption_at_rest'] = {
                'compliant': False,
                'error': str(e),
                'requirement': 'SOC2-CC6.7'
            }
    
    def check_access_logging(self):
        """Check access logging compliance"""
        log_files = [
            '/var/log/auth.log',
            '/var/log/gough/access.log',
            '/var/log/audit/audit.log'
        ]
        
        compliant = all(os.path.exists(log_file) for log_file in log_files)
        
        self.compliance_results['access_logging'] = {
            'compliant': compliant,
            'evidence': f"Log files present: {log_files}",
            'requirement': 'SOC2-CC2.2'
        }
    
    def generate_compliance_report(self):
        """Generate compliance report"""
        total_checks = len(self.compliance_results)
        passed_checks = sum(1 for result in self.compliance_results.values() if result.get('compliant', False))
        
        report = {
            'timestamp': datetime.now().isoformat(),
            'compliance_score': (passed_checks / total_checks) * 100,
            'total_checks': total_checks,
            'passed_checks': passed_checks,
            'failed_checks': total_checks - passed_checks,
            'details': self.compliance_results
        }
        
        return report
    
    def run_all_checks(self):
        """Run all compliance checks"""
        self.check_password_policy()
        self.check_encryption_at_rest()
        self.check_access_logging()
        
        return self.generate_compliance_report()
```

---

## Security Hardening Checklist

### Pre-Deployment Security Checklist

**System Hardening**:
- [ ] Operating system fully updated with security patches
- [ ] Unnecessary services disabled and removed
- [ ] Firewall configured with minimal required ports
- [ ] SSH hardened (key-only auth, non-standard port)
- [ ] Audit logging enabled and configured
- [ ] File system permissions properly set
- [ ] User accounts follow principle of least privilege
- [ ] Password policy enforced
- [ ] Multi-factor authentication enabled
- [ ] Time synchronization configured (NTP)

**Network Security**:
- [ ] Network segmentation implemented
- [ ] VPN access configured for remote management
- [ ] Intrusion detection system deployed
- [ ] Network monitoring configured
- [ ] DNS security (DNS over HTTPS/TLS)
- [ ] Certificate management automated
- [ ] Load balancer security headers configured
- [ ] DDoS protection enabled

**Application Security**:
- [ ] Input validation implemented across all endpoints
- [ ] Output encoding prevents XSS attacks
- [ ] SQL injection prevention verified
- [ ] Authentication and session management secure
- [ ] Access controls properly implemented
- [ ] Security headers configured
- [ ] HTTPS enforced with proper certificates
- [ ] API rate limiting implemented
- [ ] Security logging comprehensive
- [ ] Error handling doesn't leak information

**Container Security**:
- [ ] Base images scanned for vulnerabilities
- [ ] Containers run as non-root users
- [ ] Resource limits configured
- [ ] Security contexts properly set
- [ ] Image signing and verification enabled
- [ ] Runtime security monitoring active
- [ ] Network policies restrict container communication
- [ ] Secrets management implemented
- [ ] Container registries secured
- [ ] Regular image updates scheduled

**Data Protection**:
- [ ] Encryption at rest enabled for all data stores
- [ ] Encryption in transit enforced
- [ ] Key management system implemented
- [ ] Database security hardened
- [ ] Backup encryption configured
- [ ] Data classification implemented
- [ ] Data retention policies defined
- [ ] Secure data disposal procedures
- [ ] Privacy controls implemented
- [ ] Data loss prevention configured

### Post-Deployment Security Monitoring

**Continuous Monitoring**:
- [ ] Security event monitoring active
- [ ] Vulnerability scanning automated
- [ ] Configuration drift detection
- [ ] Compliance monitoring automated
- [ ] Performance monitoring includes security metrics
- [ ] Log analysis for security events
- [ ] Threat intelligence integration
- [ ] User behavior analytics
- [ ] Network traffic analysis
- [ ] File integrity monitoring

**Incident Response Readiness**:
- [ ] Incident response plan documented and tested
- [ ] Security team contact information current
- [ ] Escalation procedures defined
- [ ] Evidence collection procedures documented
- [ ] Communication templates prepared
- [ ] Legal and regulatory notification procedures
- [ ] Recovery procedures documented and tested
- [ ] Lessons learned process established

**Regular Security Tasks**:
- [ ] Weekly vulnerability scans
- [ ] Monthly penetration testing
- [ ] Quarterly access reviews
- [ ] Annual security audits
- [ ] Regular backup testing
- [ ] Security awareness training
- [ ] Policy and procedure updates
- [ ] Threat model reviews
- [ ] Disaster recovery testing
- [ ] Compliance assessments

This comprehensive security guide provides the framework for implementing enterprise-grade security controls in Gough deployments, ensuring protection against modern threats while maintaining compliance with industry standards.