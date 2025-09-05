# Security Overview

This document outlines the comprehensive security architecture of the Gough Hypervisor Automation System, covering security principles, threat models, protection mechanisms, and compliance considerations.

## Security Architecture

### Security-First Design

Gough implements a multi-layered security architecture based on defense-in-depth principles:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Gough Security Layers                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              Application Security Layer                      │   │
│  │  • Authentication & Authorization                           │   │
│  │  • JWT Token Management                                     │   │
│  │  • Role-Based Access Control (RBAC)                        │   │
│  │  • API Security & Rate Limiting                            │   │
│  │  • Input Validation & Sanitization                         │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                             │                                       │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              Transport Security Layer                        │   │
│  │  • TLS 1.3 Encryption                                      │   │
│  │  • Certificate Management                                   │   │
│  │  • mTLS for Inter-Service Communication                    │   │
│  │  • SSH Key Management                                       │   │
│  │  • Secure WebSocket Connections                            │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                             │                                       │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                Network Security Layer                       │   │
│  │  • Network Segmentation (VLANs)                            │   │
│  │  • Firewall Rules & Access Control                         │   │
│  │  • Container Network Isolation                             │   │
│  │  • Intrusion Detection System (IDS)                        │   │
│  │  • Network Monitoring & Analysis                           │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                             │                                       │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              Container Security Layer                        │   │
│  │  • Container Isolation                                      │   │
│  │  • Non-Root User Execution                                  │   │
│  │  • Read-Only Filesystems                                   │   │
│  │  • Capability Dropping                                     │   │
│  │  • Security Scanning                                       │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                             │                                       │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                Host Security Layer                          │   │
│  │  • Operating System Hardening                              │   │
│  │  • File System Permissions                                 │   │
│  │  • Process Monitoring                                      │   │
│  │  • System Audit Logging                                    │   │
│  │  • Host-Based Intrusion Detection                          │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                             │                                       │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │               Physical Security Layer                       │   │
│  │  • Secure Boot Process                                     │   │
│  │  • Hardware Security Modules                               │   │
│  │  • Physical Access Controls                                │   │
│  │  • Data Center Security                                    │   │
│  │  • Environmental Controls                                  │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

## Authentication and Authorization

### Multi-Factor Authentication

Gough supports multiple authentication methods:

#### JWT-Based Authentication
```javascript
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "scope": "read write admin"
}
```

#### API Key Authentication
```bash
# API key format for service accounts
API_KEY="gough_ak_1234567890abcdef1234567890abcdef"
Authorization: Bearer gough_ak_1234567890abcdef1234567890abcdef
```

#### SSH Key-Based Authentication
```bash
# SSH public key format for server access
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq... user@gough.local
```

### Role-Based Access Control (RBAC)

#### User Roles

| Role | Permissions | Description |
|------|------------|-------------|
| **Admin** | All permissions | Full system access |
| **Operator** | Read, Deploy, Manage | Server provisioning and management |
| **Monitor** | Read, View Logs | Read-only access and monitoring |
| **Service** | API Access | Service account access |

#### Permission Matrix

| Resource | Admin | Operator | Monitor | Service |
|----------|--------|----------|---------|---------|
| View Servers | ✓ | ✓ | ✓ | ✓ |
| Commission Servers | ✓ | ✓ | ✗ | ✓ |
| Deploy Servers | ✓ | ✓ | ✗ | ✓ |
| Release Servers | ✓ | ✓ | ✗ | ✗ |
| Delete Servers | ✓ | ✗ | ✗ | ✗ |
| Manage Users | ✓ | ✗ | ✗ | ✗ |
| View Logs | ✓ | ✓ | ✓ | ✗ |
| System Config | ✓ | ✗ | ✗ | ✗ |
| Security Settings | ✓ | ✗ | ✗ | ✗ |

### Token Management

#### JWT Token Security
```python
# JWT configuration
JWT_ALGORITHM = 'RS256'  # RSA with SHA-256
JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)
JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)
JWT_BLACKLIST_ENABLED = True
JWT_BLACKLIST_TOKEN_CHECKS = ['access', 'refresh']

# Token validation
def validate_token(token):
    try:
        payload = jwt.decode(
            token,
            current_app.config['JWT_PUBLIC_KEY'],
            algorithms=['RS256'],
            options={"verify_signature": True, "verify_exp": True}
        )
        
        # Check if token is blacklisted
        if is_token_blacklisted(payload['jti']):
            raise InvalidTokenError("Token has been revoked")
            
        return payload
        
    except jwt.ExpiredSignatureError:
        raise InvalidTokenError("Token has expired")
    except jwt.InvalidTokenError as e:
        raise InvalidTokenError(f"Invalid token: {str(e)}")
```

## Transport Layer Security (TLS)

### TLS Configuration

#### Certificate Management
```bash
# Generate self-signed certificates for development
openssl req -x509 -newkey rsa:4096 -nodes \
  -out /etc/ssl/certs/gough.crt \
  -keyout /etc/ssl/private/gough.key \
  -days 365 \
  -subj "/C=US/ST=State/L=City/O=Gough/CN=gough.local"

# Production: Use Let's Encrypt
certbot certonly --standalone -d gough.yourdomain.com
```

#### TLS Settings
```nginx
# Nginx TLS configuration
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-RSA-AES128-SHA256:ECDHE-RSA-AES256-SHA384;
ssl_prefer_server_ciphers on;
ssl_session_cache shared:SSL:10m;
ssl_session_timeout 5m;
ssl_stapling on;
ssl_stapling_verify on;

# Security headers
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
add_header X-Frame-Options DENY always;
add_header X-Content-Type-Options nosniff always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
```

### Mutual TLS (mTLS) for Service Communication

```yaml
# mTLS configuration for inter-service communication
version: '3.8'
services:
  management-server:
    environment:
      - TLS_CLIENT_CERT=/etc/ssl/client.crt
      - TLS_CLIENT_KEY=/etc/ssl/client.key
      - TLS_CA_CERT=/etc/ssl/ca.crt
    volumes:
      - ./ssl:/etc/ssl:ro
```

## Network Security

### Network Segmentation

#### VLAN Configuration
```bash
# Management VLAN (10)
ip link add link eth0 name eth0.10 type vlan id 10
ip addr add 10.0.1.100/24 dev eth0.10
ip link set eth0.10 up

# Provisioning VLAN (20)
ip link add link eth0 name eth0.20 type vlan id 20
ip addr add 10.0.2.100/24 dev eth0.20
ip link set eth0.20 up

# Monitoring VLAN (30)
ip link add link eth0 name eth0.30 type vlan id 30
ip addr add 10.0.3.100/24 dev eth0.30
ip link set eth0.30 up
```

### Firewall Rules

#### UFW Configuration
```bash
# Default policies
ufw default deny incoming
ufw default allow outgoing

# Management access
ufw allow from 10.0.1.0/24 to any port 22,80,443,8000,5240,8443

# Provisioning network
ufw allow from 10.0.2.0/24 to any port 53,67,69,80,443

# Monitoring network
ufw allow from 10.0.3.0/24 to any port 8443,9090

# Container network
ufw allow from 172.20.0.0/16 to any port 5432,3306,6379

# Block direct internet access from provisioning network
ufw deny from 10.0.2.0/24 to 0.0.0.0/0

# Enable firewall
ufw enable
```

#### iptables Rules
```bash
#!/bin/bash
# Advanced iptables configuration

# Flush existing rules
iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X
iptables -t mangle -F
iptables -t mangle -X

# Default policies
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT

# Allow loopback
iptables -A INPUT -i lo -j ACCEPT

# Allow established connections
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Rate limiting for SSH
iptables -A INPUT -p tcp --dport 22 -m limit --limit 3/min --limit-burst 3 -j ACCEPT
iptables -A INPUT -p tcp --dport 22 -j DROP

# Management network access
iptables -A INPUT -s 10.0.1.0/24 -p tcp --dport 80 -j ACCEPT
iptables -A INPUT -s 10.0.1.0/24 -p tcp --dport 443 -j ACCEPT
iptables -A INPUT -s 10.0.1.0/24 -p tcp --dport 8000 -j ACCEPT
iptables -A INPUT -s 10.0.1.0/24 -p tcp --dport 5240 -j ACCEPT
iptables -A INPUT -s 10.0.1.0/24 -p tcp --dport 8443 -j ACCEPT

# Provisioning network services
iptables -A INPUT -s 10.0.2.0/24 -p udp --dport 53 -j ACCEPT
iptables -A INPUT -s 10.0.2.0/24 -p udp --dport 67 -j ACCEPT
iptables -A INPUT -s 10.0.2.0/24 -p udp --dport 69 -j ACCEPT

# Log dropped packets
iptables -A INPUT -j LOG --log-prefix "IPT-DROP: "
iptables -A INPUT -j DROP
```

## Container Security

### Container Hardening

#### Docker Security Configuration
```dockerfile
# Security-hardened Dockerfile
FROM python:3.12-slim

# Create non-root user
RUN groupadd -r gough && useradd -r -g gough gough

# Install dependencies and clean up
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Copy application with proper ownership
COPY --chown=gough:gough . /app
WORKDIR /app

# Switch to non-root user
USER gough

# Use exec form for CMD
CMD ["python", "app.py"]
```

#### Docker Compose Security
```yaml
version: '3.8'
services:
  management-server:
    security_opt:
      - no-new-privileges:true
      - apparmor:docker-default
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - DAC_OVERRIDE
      - FOWNER
      - SETGID
      - SETUID
    read_only: true
    tmpfs:
      - /tmp:noexec,nosuid,size=128m
    volumes:
      - app_data:/data:rw
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

### Image Security Scanning

#### Container Image Scanning
```bash
# Using Trivy for vulnerability scanning
trivy image gough/management-server:latest

# Using Docker Scout
docker scout cves gough/management-server:latest

# Using Snyk
snyk container test gough/management-server:latest
```

#### Security Policies
```yaml
# OPA (Open Policy Agent) security policy
package docker.security

deny[msg] {
    input.User == "root"
    msg := "Container should not run as root user"
}

deny[msg] {
    input.Privileged == true
    msg := "Container should not run in privileged mode"
}

deny[msg] {
    "ALL" in input.CapDrop
    not "CAP_SYS_ADMIN" in input.CapAdd
    msg := "Container should drop all capabilities except required ones"
}
```

## Data Protection

### Encryption at Rest

#### Database Encryption
```sql
-- PostgreSQL encryption
ALTER SYSTEM SET ssl = on;
ALTER SYSTEM SET ssl_cert_file = '/etc/ssl/certs/server.crt';
ALTER SYSTEM SET ssl_key_file = '/etc/ssl/private/server.key';

-- Enable encryption for sensitive columns
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Encrypted storage for API keys
CREATE TABLE api_keys (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    encrypted_key BYTEA,
    created_at TIMESTAMP DEFAULT NOW()
);

INSERT INTO api_keys (user_id, encrypted_key) 
VALUES (1, pgp_sym_encrypt('sensitive-api-key', 'encryption-passphrase'));
```

#### File System Encryption
```bash
# LUKS encryption for sensitive data
cryptsetup luksFormat /dev/sdb
cryptsetup luksOpen /dev/sdb encrypted_data
mkfs.ext4 /dev/mapper/encrypted_data
mount /dev/mapper/encrypted_data /opt/gough/encrypted

# Secure file permissions
chmod 700 /opt/gough/encrypted
chown root:root /opt/gough/encrypted
```

### Secrets Management

#### HashiCorp Vault Integration
```python
import hvac

class VaultClient:
    def __init__(self, vault_url, vault_token):
        self.client = hvac.Client(url=vault_url, token=vault_token)
    
    def get_secret(self, path):
        \"\"\"Retrieve secret from Vault\"\"\"
        response = self.client.secrets.kv.v2.read_secret_version(path=path)
        return response['data']['data']
    
    def store_secret(self, path, secret_data):
        \"\"\"Store secret in Vault\"\"\"
        return self.client.secrets.kv.v2.create_or_update_secret(
            path=path,
            secret=secret_data
        )

# Usage
vault = VaultClient('https://vault.local:8200', vault_token)
maas_api_key = vault.get_secret('gough/maas/api-key')['key']
```

## Security Monitoring

### FleetDM Security Monitoring

#### OSQuery Security Queries
```sql
-- Monitor SSH connections
SELECT 
    username,
    remote_address,
    remote_port,
    time
FROM user_ssh_keys 
WHERE username != 'root';

-- Check for suspicious processes
SELECT 
    name,
    path,
    cmdline,
    parent,
    uid,
    gid
FROM processes 
WHERE 
    (name LIKE '%nc%' OR name LIKE '%ncat%' OR name LIKE '%netcat%') OR
    (cmdline LIKE '%/bin/sh%' AND parent NOT IN (SELECT pid FROM processes WHERE name IN ('sshd', 'bash', 'zsh')));

-- Monitor file integrity
SELECT 
    target_path,
    md5,
    sha1,
    sha256,
    mtime,
    ctime
FROM file_events 
WHERE target_path IN (
    '/etc/passwd',
    '/etc/shadow',
    '/etc/sudoers',
    '/etc/ssh/sshd_config'
);
```

#### Security Alert Rules
```yaml
# FleetDM security policies
apiVersion: v1
kind: policy
spec:
  name: "Detect Privilege Escalation"
  query: |
    SELECT uid, username, shell 
    FROM users 
    WHERE uid = 0 AND username != 'root'
  description: "Detect unauthorized root-level accounts"
  resolution: "Investigate and remove unauthorized root accounts"
  critical: true

---
apiVersion: v1
kind: policy
spec:
  name: "Monitor Docker Container Security"
  query: |
    SELECT 
      id,
      name,
      image,
      privileged,
      security_opt
    FROM docker_containers 
    WHERE privileged = 1
  description: "Alert on privileged containers"
  resolution: "Review container security configuration"
  critical: false
```

### Intrusion Detection

#### OSSEC Configuration
```xml
<!-- OSSEC rules for Gough -->
<group name="gough,">
  <rule id="100001" level="5">
    <if_sid>5501</if_sid>
    <match>authentication failure</match>
    <description>Authentication failure for Gough services</description>
    <group>authentication_failed,pci_dss_10.2.4,pci_dss_10.2.5,</group>
  </rule>

  <rule id="100002" level="10" frequency="5" timeframe="300">
    <if_matched_sid>100001</if_matched_sid>
    <description>Multiple authentication failures for Gough</description>
    <mitre>
      <id>T1110</id>
    </mitre>
  </rule>
</group>
```

## Audit Logging

### Comprehensive Audit Trail

#### Application Audit Logging
```python
import logging
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text

class AuditLog(Base):
    __tablename__ = 'audit_logs'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer)
    username = Column(String(255))
    action = Column(String(255))
    resource = Column(String(255))
    resource_id = Column(String(255))
    details = Column(Text)
    ip_address = Column(String(45))
    user_agent = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)
    success = Column(Boolean, default=True)

def audit_log(action, resource, resource_id=None, details=None, user=None):
    \"\"\"Log security-relevant events\"\"\"
    log_entry = AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else 'system',
        action=action,
        resource=resource,
        resource_id=str(resource_id) if resource_id else None,
        details=details,
        ip_address=request.remote_addr if request else None,
        user_agent=request.user_agent.string if request else None
    )
    
    db.session.add(log_entry)
    db.session.commit()
    
    # Also log to syslog
    syslog.syslog(syslog.LOG_INFO, 
        f"AUDIT: {action} {resource} {resource_id or ''} by {user.username if user else 'system'}")

# Usage examples
audit_log("SERVER_DEPLOY", "server", server_id, f"Template: {template_name}", current_user)
audit_log("USER_LOGIN", "authentication", user.id, f"From: {request.remote_addr}", user)
audit_log("CONFIG_CHANGE", "maas_config", config.id, f"URL changed from {old_url} to {new_url}", current_user)
```

### Log Retention and Analysis

#### Log Management
```bash
# Logrotate configuration
/var/log/gough/*.log {
    daily
    rotate 90
    compress
    delaycompress
    missingok
    notifempty
    create 644 gough gough
    postrotate
        /bin/kill -HUP `cat /var/run/gough.pid 2> /dev/null` 2> /dev/null || true
    endscript
}

# Forward logs to SIEM
rsyslog_config="""
# Forward Gough logs to SIEM
$ModLoad imfile
$InputFileName /var/log/gough/audit.log
$InputFileTag gough-audit:
$InputFileStateFile gough-audit-state
$InputFileSeverity info
$InputRunFileMonitor

# Forward to SIEM server
*.* @@siem.example.com:514
"""
```

## Compliance and Standards

### Security Frameworks

#### NIST Cybersecurity Framework Alignment
| Function | Category | Gough Implementation |
|----------|----------|---------------------|
| **Identify** | Asset Management | Server inventory and classification |
| | Business Environment | Network segmentation and access control |
| | Governance | Security policies and procedures |
| **Protect** | Access Control | RBAC, authentication, authorization |
| | Awareness & Training | Security documentation and procedures |
| | Data Security | Encryption at rest and in transit |
| | Protective Technology | Firewalls, intrusion prevention |
| **Detect** | Anomalies & Events | OSQuery monitoring and alerting |
| | Security Monitoring | FleetDM security monitoring |
| **Respond** | Response Planning | Incident response procedures |
| | Communications | Alert mechanisms and notifications |
| **Recover** | Recovery Planning | Backup and disaster recovery |
| | Improvements | Security assessment and improvements |

#### CIS Controls Implementation
- **CIS Control 1**: Hardware Asset Inventory - Server discovery and tracking
- **CIS Control 2**: Software Asset Inventory - Container and application tracking
- **CIS Control 3**: Continuous Vulnerability Management - Security scanning
- **CIS Control 4**: Controlled Use of Administrative Privileges - RBAC implementation
- **CIS Control 5**: Secure Configuration - Hardening guidelines
- **CIS Control 6**: Maintenance, Monitoring, and Analysis of Audit Logs - Audit logging
- **CIS Control 8**: Malware Defenses - Container security scanning
- **CIS Control 11**: Secure Configuration for Network Devices - Network hardening
- **CIS Control 12**: Boundary Defense - Firewall and network segmentation
- **CIS Control 16**: Account Monitoring and Control - User activity monitoring

## Incident Response

### Security Incident Response Plan

#### Incident Classification
| Severity | Examples | Response Time |
|----------|----------|---------------|
| **Critical** | Unauthorized root access, data breach | 1 hour |
| **High** | Service compromise, privilege escalation | 4 hours |
| **Medium** | Failed authentication attempts, configuration drift | 24 hours |
| **Low** | Policy violations, minor security events | 72 hours |

#### Response Procedures
```bash
#!/bin/bash
# Incident response script

incident_response() {
    local severity=$1
    local description=$2
    
    # Log incident
    echo "[$(date)] SECURITY INCIDENT: $severity - $description" >> /var/log/gough/security.log
    
    # Immediate actions based on severity
    case $severity in
        "critical")
            # Isolate affected systems
            isolate_system
            # Notify security team immediately
            send_alert "CRITICAL" "$description"
            # Preserve evidence
            collect_evidence
            ;;
        "high")
            # Enhanced monitoring
            enable_debug_logging
            # Notify security team
            send_alert "HIGH" "$description"
            ;;
        "medium"|"low")
            # Log and monitor
            log_event "$severity" "$description"
            ;;
    esac
}

isolate_system() {
    # Block suspicious IP addresses
    ufw deny from $SUSPICIOUS_IP
    # Disable affected services
    systemctl stop suspicious_service
    # Create system snapshot
    create_forensic_image
}
```

## Security Testing

### Penetration Testing

#### Automated Security Testing
```bash
#!/bin/bash
# Automated security testing script

# Network scanning
nmap -sS -sV -A gough.local

# Web application security testing
nikto -h https://gough.local:8000
sqlmap -u "https://gough.local:8000/api/servers" --cookie="session=..."

# SSL/TLS testing
sslyze gough.local:443
testssl.sh gough.local:443

# Container security testing
docker-bench-security
```

### Security Metrics

#### Key Security Indicators
- Authentication failure rate
- Privilege escalation attempts
- Network intrusion attempts
- Vulnerability scan results
- Security patch compliance
- Certificate expiration tracking
- Access control violations

This security overview provides a comprehensive foundation for understanding and implementing security in the Gough system. Regular security assessments and updates to these measures are essential for maintaining a robust security posture.