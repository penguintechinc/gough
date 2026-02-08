# Architecture Overview

Gough is designed as a containerized, microservices-based hypervisor automation system that provides comprehensive bare metal server provisioning capabilities. This document outlines the system architecture, design principles, and component interactions.

## Design Principles

### Core Design Philosophy

1. **Modular Architecture**: Each component serves a specific purpose and can be developed, deployed, and scaled independently
2. **Container-First**: All services run in Docker containers for consistency, portability, and isolation
3. **API-Driven**: Everything is accessible and automatable through well-defined APIs
4. **Security by Design**: Security considerations are built into every component and interaction
5. **Scalability**: Designed to handle enterprise-scale deployments with hundreds of servers
6. **Observability**: Comprehensive logging, monitoring, and alerting throughout the system

### Technology Stack

- **Containerization**: Docker and Docker Compose
- **Web Framework**: py4web (Python-based modern web framework)
- **Database**: PostgreSQL (management data) and MySQL (FleetDM)
- **Caching**: Redis for session management and caching
- **Provisioning**: Ubuntu MaaS (Metal as a Service)
- **Security Monitoring**: FleetDM with OSQuery agents
- **Orchestration**: Ansible for configuration management
- **Networking**: Docker networking with custom bridge
- **SSL/TLS**: Self-signed or Let's Encrypt certificates

## System Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Gough Architecture                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────┐    ┌──────────────┐    ┌─────────────┐            │
│  │  External   │    │   Network    │    │  Physical   │            │
│  │  Networks   │    │  Services    │    │  Servers    │            │
│  │             │    │              │    │             │            │
│  │ ┌─────────┐ │    │ ┌──────────┐ │    │ ┌─────────┐ │            │
│  │ │Internet │ │    │ │   DNS    │ │    │ │ Server  │ │            │
│  │ └─────────┘ │    │ │   DHCP   │ │    │ │ Node 1  │ │            │
│  │ ┌─────────┐ │    │ │   TFTP   │ │    │ └─────────┘ │            │
│  │ │ Mgmt    │ │    │ │   PXE    │ │    │ ┌─────────┐ │            │
│  │ │ Network │ │    │ └──────────┘ │    │ │ Server  │ │            │
│  │ └─────────┘ │    └──────────────┘    │ │ Node N  │ │            │
│  └─────────────┘                        │ └─────────┘ │            │
│                                          └─────────────┘            │
│                            │                    │                   │
│                            └─────────┬──────────┘                   │
│                                     │                               │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │                   Docker Container Network                      │ │
│  │                        (172.20.0.0/16)                         │ │
│  │                                                                 │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │ │
│  │  │    MaaS      │  │ Management   │  │   FleetDM    │          │ │
│  │  │  Container   │  │    Server    │  │  Security    │          │ │
│  │  │              │  │  Container   │  │  Container   │          │ │
│  │  │ ┌──────────┐ │  │              │  │              │          │ │
│  │  │ │ Region   │ │  │ ┌──────────┐ │  │ ┌──────────┐ │          │ │
│  │  │ │ Controller│ │  │ │  py4web  │ │  │ │ Fleet    │ │          │ │
│  │  │ └──────────┘ │  │ │ Portal   │ │  │ │ Server   │ │          │ │
│  │  │ ┌──────────┐ │  │ └──────────┘ │  │ └──────────┘ │          │ │
│  │  │ │ Rack     │ │  │ ┌──────────┐ │  │ ┌──────────┐ │          │ │
│  │  │ │ Controller│ │  │ │ MaaS API │ │  │ │ OSQuery  │ │          │ │
│  │  │ └──────────┘ │  │ │ Client   │ │  │ │ Endpoint │ │          │ │
│  │  └──────────────┘  │ └──────────┘ │  │ └──────────┘ │          │ │
│  │                    └──────────────┘  └──────────────┘          │ │
│  │                                                                 │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │ │
│  │  │ PostgreSQL   │  │    MySQL     │  │    Redis     │          │ │
│  │  │  Database    │  │  Database    │  │   Cache      │          │ │
│  │  └──────────────┘  └──────────────┘  └──────────────┘          │ │
│  │                                                                 │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │ │
│  │  │   Nginx      │  │ Prometheus   │  │   Grafana    │          │ │
│  │  │   Proxy      │  │ Monitoring   │  │  Dashboard   │          │ │
│  │  │  (Optional)  │  │  (Optional)  │  │  (Optional)  │          │ │
│  │  └──────────────┘  └──────────────┘  └──────────────┘          │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. MaaS Container (Ubuntu Metal as a Service)

**Purpose**: Provides PXE boot, DHCP, DNS, and bare metal provisioning services.

**Key Functions**:
- **PXE Boot Server**: Netboot physical servers over the network
- **DHCP Server**: Assigns IP addresses to discovering machines
- **DNS Server**: Provides name resolution for the provisioning network
- **TFTP Server**: Delivers boot images and kernel files
- **Region Controller**: Central management of MaaS services
- **Rack Controller**: Local services for hardware management

**Technologies**:
- Ubuntu 24.04 LTS base image
- MaaS 3.x server packages
- PostgreSQL database (internal to MaaS)
- Bind9 DNS server
- ISC DHCP server
- TFTP server

**Exposed Ports**:
- `5240`: MaaS Web UI
- `5241-5242`: MaaS API endpoints
- `53`: DNS queries
- `67`: DHCP server
- `69`: TFTP server
- `80/443`: HTTP/HTTPS for images

### 2. Gough Management Server

**Purpose**: Central web portal for system management and orchestration.

**Key Functions**:
- **Web Dashboard**: Real-time system status and server inventory
- **MaaS Integration**: Interface with MaaS APIs for provisioning
- **Template Management**: Create and manage cloud-init templates
- **User Management**: Authentication and role-based access control
- **Job Scheduling**: Background task processing with Celery
- **API Gateway**: RESTful APIs for all system functions

**Technologies**:
- Python 3.12 runtime
- py4web web framework
- SQLAlchemy ORM with PostgreSQL
- Celery for background jobs
- Redis for caching and sessions
- JWT for API authentication

**Database Schema**:
- **servers**: Physical server inventory
- **deployment_jobs**: Provisioning job tracking
- **cloud_init_templates**: Template library
- **users**: User accounts and permissions
- **system_logs**: Audit and event logging
- **maas_config**: MaaS connection settings

### 3. FleetDM Security Container

**Purpose**: Security monitoring and compliance management using OSQuery.

**Key Functions**:
- **OSQuery Management**: Deploy and manage OSQuery agents
- **Security Monitoring**: Real-time security event collection
- **Compliance Reporting**: Track system compliance and policy violations
- **Query Orchestration**: Distribute security queries across the fleet
- **Alert Management**: Security incident alerting and response

**Technologies**:
- FleetDM server (Go-based)
- MySQL database for Fleet data
- Redis for live query caching
- OSQuery agents on managed servers
- TLS/mTLS for secure communication

### 4. Agent Container

**Purpose**: Lightweight monitoring and management agent deployed to provisioned servers.

**Key Functions**:
- **System Monitoring**: Resource utilization and health metrics
- **Log Collection**: Forward logs to central management
- **Command Execution**: Remote command execution capabilities
- **Configuration Management**: Apply configuration changes
- **Health Reporting**: Regular health check reporting

**Technologies**:
- Python 3.12 minimal runtime
- SystemD integration
- Docker API client (for container monitoring)
- Secure HTTP/HTTPS communication

## Data Flow Architecture

### Server Provisioning Flow

```
1. Physical Server Boot
   ↓
2. PXE Boot Request → MaaS DHCP/TFTP
   ↓
3. Server Discovery → MaaS Region Controller
   ↓
4. Commission Request → Management Portal
   ↓
5. Template Selection → Cloud-Init Generation
   ↓
6. Deployment Command → MaaS API
   ↓
7. OS Installation → Ubuntu 24.04 LTS
   ↓
8. Cloud-Init Execution → Agent Installation
   ↓
9. Agent Registration → Management Portal
   ↓
10. Fleet Enrollment → FleetDM Server
```

### Management Flow

```
User Request → Management Portal
     ↓
Authentication → JWT Token
     ↓
API Processing → Database Query/Update
     ↓
Background Job → Celery Worker
     ↓
MaaS API Call → Server Action
     ↓
Status Update → Database & Cache
     ↓
Real-time Update → WebSocket (optional)
     ↓
User Notification → Web Dashboard
```

### Monitoring Flow

```
OSQuery Agent → FleetDM Server
     ↓
Security Events → MySQL Database
     ↓
Alert Rules → Alert Processing
     ↓
Notification → Management Portal
     ↓
Dashboard Update → User Interface

System Metrics → Management Agent
     ↓
Metrics Collection → Prometheus (optional)
     ↓
Dashboard → Grafana (optional)
```

## Network Architecture

### Container Networking

**Docker Bridge Network**: `maas-network (172.20.0.0/16)`

**Service Communication**:
- All containers communicate over the Docker bridge network
- Internal DNS resolution provided by Docker
- No direct external access except through exposed ports

**External Networks**:
- **Management Network**: Administrator access to web interfaces
- **Provisioning Network**: PXE boot and server provisioning
- **Monitoring Network**: Agent communication and monitoring

### Port Mapping

| Service | Internal Port | External Port | Protocol | Purpose |
|---------|---------------|---------------|----------|---------|
| MaaS | 5240 | 5240 | HTTP | Web UI |
| MaaS API | 5241-5242 | 5241-5242 | HTTP | API Access |
| Management Server | 8000 | 8000 | HTTP/HTTPS | Web Portal |
| FleetDM | 8443 | 8443 | HTTPS | Security Dashboard |
| DNS | 53 | 53 | UDP | Name Resolution |
| DHCP | 67 | 67 | UDP | IP Assignment |
| TFTP | 69 | 69 | UDP | Boot Images |
| PostgreSQL | 5432 | - | TCP | Internal DB |
| MySQL | 3306 | - | TCP | Internal DB |
| Redis | 6379 | - | TCP | Internal Cache |

## Security Architecture

### Authentication and Authorization

**Multi-Layer Security**:
1. **Container Isolation**: Docker container security boundaries
2. **Network Segmentation**: Separate networks for management and provisioning
3. **API Authentication**: JWT-based API security
4. **TLS Encryption**: All external communication encrypted
5. **Database Security**: Encrypted database connections

**Access Control**:
- **Role-Based Access Control (RBAC)**: Different permission levels
- **API Key Management**: Secure API key generation and rotation
- **SSH Key Management**: Centralized SSH key distribution
- **Audit Logging**: Comprehensive security event logging

### Certificate Management

**TLS Certificates**:
- Self-signed certificates for internal development
- Let's Encrypt integration for production deployments
- Certificate rotation and renewal automation
- Mutual TLS (mTLS) for agent communication

## Scalability and Performance

### Horizontal Scaling

**Scalable Components**:
- **Management Server**: Multiple instances behind load balancer
- **Background Workers**: Scale Celery workers based on job queue
- **Database**: PostgreSQL clustering and read replicas
- **Agents**: Distributed across provisioned servers

**Resource Requirements**:
- **Minimum**: 8GB RAM, 4 CPU cores, 50GB storage
- **Recommended**: 32GB RAM, 8 CPU cores, 500GB storage
- **Enterprise**: 64GB+ RAM, 16+ CPU cores, 1TB+ storage

### Performance Optimization

**Database Optimization**:
- Connection pooling and query optimization
- Indexed columns for frequently queried data
- Periodic database maintenance and vacuuming

**Caching Strategy**:
- Redis caching for frequently accessed data
- Session storage in Redis
- API response caching where appropriate

**Background Processing**:
- Asynchronous job processing with Celery
- Job prioritization and queue management
- Failed job retry mechanisms

## Integration Points

### External System Integration

**APIs Provided**:
- RESTful management APIs
- WebSocket APIs for real-time updates
- Webhook callbacks for events

**APIs Consumed**:
- MaaS REST API for server management
- FleetDM API for security management
- Cloud provider APIs (optional)

**Integration Patterns**:
- Event-driven architecture with webhooks
- Polling for status updates where webhooks not available
- Idempotent operations for reliability

## Deployment Models

### Single-Node Deployment

**Use Case**: Development, testing, small environments
**Configuration**: All services on one Docker host
**Scaling**: Vertical scaling only

### Multi-Node Deployment

**Use Case**: Production environments
**Configuration**: Services distributed across multiple hosts
**Scaling**: Horizontal scaling with load balancing

### High Availability Deployment

**Use Case**: Mission-critical environments
**Configuration**: Redundant services with failover
**Requirements**: Load balancers, shared storage, database clustering

## Monitoring and Observability

### Built-in Monitoring

**System Health**:
- Container health checks
- Database connection monitoring
- API endpoint health verification
- Disk space and resource monitoring

**Application Monitoring**:
- Job processing metrics
- API response times
- Error rates and patterns
- User activity tracking

### Optional Monitoring Stack

**Prometheus Integration**:
- Custom metrics export
- System resource metrics
- Application performance metrics

**Grafana Dashboards**:
- System overview dashboard
- Server provisioning metrics
- Security monitoring dashboard
- Performance and capacity planning

## Future Architecture Considerations

### Planned Enhancements

1. **Kubernetes Support**: Native Kubernetes deployment options
2. **Microservices Refinement**: Further decomposition of services
3. **Event Streaming**: Apache Kafka or similar for event processing
4. **GitOps Integration**: Configuration management through Git
5. **Multi-Cloud Support**: Integration with cloud providers
6. **Advanced Security**: Integration with enterprise security tools

### Extensibility

The architecture is designed to support:
- Custom plugins and extensions
- Additional provisioning backends
- Third-party integration modules
- Custom monitoring and alerting systems

This architecture provides a solid foundation for enterprise-scale bare metal automation while maintaining flexibility for future enhancements and integrations.