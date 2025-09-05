# Container Specifications

This document provides detailed specifications for each container in the Gough system, including their purpose, configuration, dependencies, and resource requirements.

## Container Overview

Gough uses a microservices architecture with the following containers:

1. **MaaS Server** - Bare metal provisioning
2. **Management Server** - Web portal and API
3. **FleetDM** - Security monitoring
4. **PostgreSQL** - Management data store
5. **MySQL** - FleetDM data store
6. **Redis** - Caching and sessions
7. **Agent** - Server monitoring agent
8. **Nginx** - Reverse proxy (optional)
9. **Ansible** - Automation engine (on-demand)

## MaaS Server Container

### Specification

```yaml
Image: Custom built from Ubuntu 24.04
Base: ubuntu:24.04
Container Name: maas-server
Hostname: maas
Privileged: true (required for DHCP/DNS services)
```

### Purpose
Provides Metal as a Service functionality for bare metal server provisioning, including PXE boot, DHCP, DNS, and deployment services.

### Key Components
- **MaaS Region Controller**: Central management and API
- **MaaS Rack Controller**: Local services (DHCP, TFTP, DNS)
- **PostgreSQL**: Internal MaaS database
- **Bind9**: DNS server
- **ISC DHCP Server**: DHCP service
- **TFTP Server**: Boot image delivery

### Dockerfile Structure
```dockerfile
FROM ubuntu:24.04

# Install MaaS packages
RUN apt-get update && apt-get install -y \
    maas \
    maas-dhcp \
    maas-dns \
    postgresql \
    bind9 \
    isc-dhcp-server \
    tftpd-hpa \
    nginx

# Configuration files
COPY config/maas/ /etc/maas/
COPY scripts/init-maas.sh /usr/local/bin/
COPY scripts/entrypoint.sh /usr/local/bin/

# Expose required ports
EXPOSE 5240 5241 5242 5247 53/udp 67/udp 69/udp 80 443

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
```

### Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `MAAS_URL` | `http://maas:5240/MAAS/` | Base MaaS URL |
| `ENABLE_DHCP` | `true` | Enable DHCP service |
| `DHCP_SUBNET` | `192.168.1.0/24` | DHCP subnet |
| `DHCP_RANGE_START` | `192.168.1.100` | DHCP range start |
| `DHCP_RANGE_END` | `192.168.1.200` | DHCP range end |
| `DHCP_GATEWAY` | `192.168.1.1` | Default gateway |
| `DHCP_DNS_SERVERS` | `8.8.8.8,8.8.4.4` | DNS servers |

### Port Mapping
| Internal | External | Protocol | Purpose |
|----------|----------|----------|---------|
| 5240 | 5240 | TCP | MaaS Web UI |
| 5241 | 5241 | TCP | Region API |
| 5242 | 5242 | TCP | Rack API |
| 5247 | 5247 | TCP | Metadata API |
| 53 | 53 | UDP | DNS |
| 67 | 67 | UDP | DHCP |
| 69 | 69 | UDP | TFTP |
| 80 | 80 | TCP | HTTP |
| 443 | 443 | TCP | HTTPS |

### Volumes
| Host Path | Container Path | Purpose |
|-----------|----------------|---------|
| `maas_data` | `/var/lib/maas` | Persistent data |
| `maas_logs` | `/var/log/maas` | Log files |
| `./config/maas` | `/etc/maas/config` | Configuration |

### Resource Requirements
- **CPU**: 2 cores minimum, 4 recommended
- **Memory**: 4GB minimum, 8GB recommended
- **Storage**: 50GB minimum, 100GB recommended
- **Network**: Requires privileged mode for network services

### Health Check
```bash
curl -f http://localhost:5240/MAAS/ || exit 1
```

## Management Server Container

### Specification

```yaml
Image: Custom built from Python 3.12
Base: python:3.12-slim
Container Name: management-server
Hostname: management-server
Privileged: false
```

### Purpose
Central management portal providing web interface, APIs, and orchestration for the entire Gough system.

### Key Components
- **py4web Framework**: Web application framework
- **MaaS API Client**: Integration with MaaS
- **FleetDM API Client**: Security monitoring integration
- **Celery**: Background job processing
- **JWT Authentication**: API security
- **Template Engine**: Cloud-init template management

### Dockerfile Structure
```dockerfile
FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY py4web-app/ /opt/py4web-apps/maas_portal/
COPY scripts/ /opt/scripts/

# Create application user
RUN useradd -m -s /bin/bash py4web

WORKDIR /opt/py4web-apps/maas_portal
EXPOSE 8000

USER py4web
CMD ["py4web", "run", ".", "--host=0.0.0.0", "--port=8000"]
```

### Python Dependencies
```python
# requirements.txt
py4web>=1.20230930.1
python-libmaas>=0.6.8
psycopg2-binary>=2.9.7
redis>=4.5.4
celery>=5.3.1
requests>=2.31.0
pyjwt>=2.8.0
pyyaml>=6.0.1
jinja2>=3.1.2
ansible-runner>=2.3.3
cryptography>=41.0.0
sqlalchemy>=2.0.20
alembic>=1.12.0
```

### Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://postgres:postgres@postgres:5432/management` | Database connection |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection |
| `MAAS_URL` | `http://maas:5240/MAAS/` | MaaS API URL |
| `FLEET_URL` | `https://fleetdm:8443` | FleetDM URL |
| `SECRET_KEY` | - | Application secret key |
| `DEBUG` | `false` | Debug mode |
| `LOG_LEVEL` | `INFO` | Logging level |

### Volumes
| Host Path | Container Path | Purpose |
|-----------|----------------|---------|
| `management_data` | `/opt/py4web-apps/maas_portal/databases` | App databases |
| `management_logs` | `/var/log/py4web` | Log files |
| `./cloud-init/templates` | `/opt/py4web-apps/maas_portal/cloud_init_templates` | Templates |
| `./config/management` | `/opt/py4web-apps/maas_portal/config` | Configuration |

### Resource Requirements
- **CPU**: 2 cores minimum, 4 recommended
- **Memory**: 2GB minimum, 4GB recommended
- **Storage**: 10GB minimum
- **Network**: Standard Docker networking

### Health Check
```bash
curl -f http://localhost:8000/ || exit 1
```

## FleetDM Container

### Specification

```yaml
Image: Custom built from fleetdm/fleet
Base: fleetdm/fleet:latest
Container Name: fleetdm
Hostname: fleetdm
Privileged: false
```

### Purpose
Security monitoring and compliance management using OSQuery fleet management.

### Key Components
- **Fleet Server**: OSQuery fleet management
- **Query Engine**: Security query orchestration
- **Policy Engine**: Compliance policy management
- **API Server**: Fleet management APIs
- **Agent Enrollment**: OSQuery agent management

### Dockerfile Structure
```dockerfile
FROM fleetdm/fleet:latest

# Install additional tools
USER root
RUN apk add --no-cache curl openssl

# Copy configuration
COPY config/ /etc/fleet/
COPY scripts/ /opt/scripts/

# Create fleet user and directories
RUN adduser -D -s /bin/sh fleet && \
    mkdir -p /var/lib/fleet /var/log/fleet && \
    chown -R fleet:fleet /var/lib/fleet /var/log/fleet

USER fleet
EXPOSE 8080 8443

CMD ["/usr/bin/fleet", "serve"]
```

### Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_MYSQL_ADDRESS` | `mysql:3306` | MySQL connection |
| `FLEET_MYSQL_DATABASE` | `fleet` | Database name |
| `FLEET_MYSQL_USERNAME` | `fleet` | Database user |
| `FLEET_MYSQL_PASSWORD` | - | Database password |
| `FLEET_REDIS_ADDRESS` | `redis:6379` | Redis connection |
| `FLEET_ADMIN_EMAIL` | `admin@fleet.local` | Admin email |
| `FLEET_ADMIN_PASSWORD` | - | Admin password |
| `FLEET_TLS_CERT` | `/etc/ssl/server.crt` | TLS certificate |
| `FLEET_TLS_KEY` | `/etc/ssl/server.key` | TLS private key |

### Volumes
| Host Path | Container Path | Purpose |
|-----------|----------------|---------|
| `fleet_data` | `/var/lib/fleet` | Persistent data |
| `fleet_logs` | `/var/log/fleet` | Log files |
| `./config/fleetdm` | `/etc/fleet/config` | Configuration |

### Resource Requirements
- **CPU**: 1 core minimum, 2 recommended
- **Memory**: 1GB minimum, 2GB recommended
- **Storage**: 20GB minimum
- **Network**: Requires TLS certificates

### Health Check
```bash
curl -f https://localhost:8443/api/v1/fleet/version -k || exit 1
```

## Database Containers

### PostgreSQL Container

```yaml
Image: postgres:15
Container Name: postgres
Purpose: Management server data storage
```

**Configuration:**
```yaml
environment:
  POSTGRES_DB: management
  POSTGRES_USER: postgres
  POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
```

**Resource Requirements:**
- CPU: 1 core minimum
- Memory: 1GB minimum, 2GB recommended
- Storage: 50GB minimum

### MySQL Container

```yaml
Image: mysql:8.0
Container Name: mysql
Purpose: FleetDM data storage
```

**Configuration:**
```yaml
environment:
  MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
  MYSQL_DATABASE: fleet
  MYSQL_USER: fleet
  MYSQL_PASSWORD: ${MYSQL_PASSWORD}
```

**Resource Requirements:**
- CPU: 1 core minimum
- Memory: 1GB minimum, 2GB recommended
- Storage: 20GB minimum

## Redis Container

### Specification

```yaml
Image: redis:7-alpine
Container Name: redis
Purpose: Caching and session storage
```

**Configuration:**
```yaml
command: redis-server --appendonly yes
```

**Resource Requirements:**
- CPU: 0.5 cores minimum
- Memory: 512MB minimum, 1GB recommended
- Storage: 5GB minimum

## Agent Container

### Specification

```yaml
Image: Custom built from Python 3.12
Base: python:3.12-alpine
Container Name: agent-{server-id}
Privileged: false
```

### Purpose
Lightweight monitoring and management agent deployed to provisioned servers.

### Dockerfile Structure
```dockerfile
FROM python:3.12-alpine

# Install dependencies
RUN apk add --no-cache \
    curl \
    docker-cli \
    && pip install --no-cache-dir \
        requests \
        psutil \
        docker \
        schedule

# Copy agent code
COPY scripts/ /opt/maas-agent/
COPY config/ /opt/maas-agent/config/

# Create agent user
RUN adduser -D -s /bin/sh agent

USER agent
WORKDIR /opt/maas-agent

CMD ["python", "agent.py"]
```

### Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `MANAGEMENT_SERVER_URL` | `http://management-server:8000` | Management server URL |
| `AGENT_API_KEY` | - | Agent authentication key |
| `SERVER_ID` | - | Unique server identifier |
| `ENABLE_OSQUERY` | `true` | Enable OSQuery integration |
| `MONITORING_INTERVAL` | `60` | Monitoring interval (seconds) |

### Resource Requirements
- **CPU**: 0.2 cores minimum
- **Memory**: 128MB minimum
- **Storage**: 1GB minimum

## Supporting Containers

### Nginx Proxy (Optional)

```yaml
Image: nginx:alpine
Container Name: nginx-proxy
Purpose: Reverse proxy and SSL termination
```

**Configuration:**
- SSL termination for all services
- Load balancing for management servers
- Static file serving

### Ansible Container (On-Demand)

```yaml
Image: cytopia/ansible:latest
Container Name: ansible
Purpose: Configuration management and orchestration
```

**Configuration:**
- Mounted playbooks and roles
- SSH key access for server management
- On-demand execution

## Container Dependencies

### Startup Order

```mermaid
graph TD
    A[PostgreSQL] --> D[Management Server]
    B[MySQL] --> E[FleetDM]
    C[Redis] --> D
    C --> E
    F[MaaS] --> D
    D --> G[Nginx Proxy]
    E --> G
    F --> G
```

### Health Check Dependencies

All containers implement health checks that are used by Docker Compose to ensure proper startup order and service readiness.

## Container Security

### Security Considerations

1. **Non-Root Users**: All applications run as non-root users where possible
2. **Read-Only Filesystems**: Use read-only root filesystems where applicable
3. **Minimal Base Images**: Use minimal base images to reduce attack surface
4. **Capability Dropping**: Drop unnecessary Linux capabilities
5. **Network Policies**: Implement network segmentation

### Security Configuration

```yaml
security_opt:
  - no-new-privileges:true
cap_drop:
  - ALL
cap_add:
  - CHOWN
  - DAC_OVERRIDE
  - FOWNER
  - SETGID
  - SETUID
```

## Resource Management

### Production Resource Allocation

| Container | CPU Limit | Memory Limit | Storage |
|-----------|-----------|--------------|---------|
| MaaS | 4 cores | 8GB | 100GB |
| Management | 2 cores | 4GB | 20GB |
| FleetDM | 1 core | 2GB | 50GB |
| PostgreSQL | 2 cores | 4GB | 100GB |
| MySQL | 1 core | 2GB | 50GB |
| Redis | 0.5 cores | 1GB | 10GB |
| Nginx | 0.5 cores | 512MB | 1GB |

### Container Monitoring

Each container provides:
- Health check endpoints
- Prometheus metrics (where applicable)
- Structured logging
- Resource utilization metrics

This specification provides the foundation for understanding, deploying, and maintaining each container in the Gough system. Regular updates to these specifications should be made as the system evolves.