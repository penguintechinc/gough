# Gough Hypervisor Automation System - Documentation

Welcome to the comprehensive documentation for Gough, a hypervisor automation system named after Gough Island - home to penguins and providing them structure, just as this hypervisor gives the entire ecosystem a place to call home.

## What is Gough?

Gough is an enterprise-grade bare metal automation system that combines Ubuntu MaaS (Metal as a Service), custom Python 3.12 services, and Ansible to deliver automated server provisioning with Ubuntu 24.04 LTS. The system provides a complete solution for managing physical server infrastructure at scale.

## Documentation Structure

### Getting Started
- **[Quick Start Guide](installation/quickstart.md)** - Get up and running in 15 minutes
- **[Production Deployment](installation/production.md)** - Production-ready installation guide

### System Architecture
- **[Architecture Overview](architecture/overview.md)** - System design and components
- **[Container Specifications](architecture/containers.md)** - Detailed container architecture
- **[Network Architecture](architecture/networking.md)** - Network design and configuration

### API Documentation
- **[Management Server API](api/management-server.md)** - RESTful API reference
- **[MaaS Integration API](api/maas-integration.md)** - MaaS API integration details

### Security
- **[Security Overview](security/overview.md)** - Security architecture and features
- **[Security Hardening](security/hardening.md)** - Production security checklist

### Troubleshooting
- **[Common Issues](troubleshooting/common-issues.md)** - Frequently encountered problems
- **[Logs and Debugging](troubleshooting/logs.md)** - Log locations and debugging guide

### Examples and Templates
- **[Cloud-Init Examples](examples/cloud-init.md)** - Cloud-init template library
- **[Ansible Examples](examples/ansible.md)** - Ansible playbook examples

## Key Features

### Core Functionality
- **Automated PXE Boot**: Complete PXE boot setup with DHCP, DNS, and TFTP
- **Bare Metal Provisioning**: Deploy Ubuntu 24.04 LTS on physical hardware
- **Web Management Portal**: Modern py4web-based management interface
- **Cloud-Init Integration**: Dynamic server configuration with templates
- **Package Management**: Automated installation of Docker, LXD, and custom packages
- **Ansible Orchestration**: Full automation with Ansible playbooks and roles
- **Security Monitoring**: FleetDM integration with OSQuery agents
- **Agent Deployment**: Automatic agent container deployment to provisioned servers

### Management Portal Features
- Real-time Dashboard with server inventory and resource monitoring
- Server Management for machine discovery and deployment control
- Template Editor for creating cloud-init templates
- Package Configuration for automated installation
- User Management with authentication and role-based access
- Monitoring Integration with FleetDM security monitoring
- System Configuration for MaaS settings and service management

### Security Features
- TLS Encryption for all services
- Fleet Security with OSQuery agents
- API Authentication using JWT tokens
- Network Isolation with container segmentation
- Comprehensive Audit Logging

## System Components

### Container Architecture
1. **MaaS Container** - Ubuntu MaaS server for PXE boot and bare metal provisioning
2. **Gough Management Server** - Custom py4web portal for hypervisor configuration
3. **Gough Agent Container** - Custom monitoring and management agent
4. **FleetDM Server** - OSQuery fleet management for security monitoring
5. **Supporting Services** - PostgreSQL, MySQL, Redis, and optional Nginx

### Network Architecture
```
┌─────────────────────────────────────────────────────────────────┐
│                    Docker Network (172.20.0.0/16)              │
├─────────────────┬─────────────────┬─────────────────┬───────────┤
│   MaaS Server   │ Management Web  │   FleetDM       │  Database │
│   (5240-5249)   │    Portal       │   Security      │ Services  │
│                 │   (8000)        │   (8443)        │           │
└─────────────────┴─────────────────┴─────────────────┴───────────┘
          │                 │                 │             │
          └─────────────────┼─────────────────┼─────────────┘
                           │                 │
┌─────────────────────────────────────────────────────────────────┐
│              Physical Network (192.168.1.0/24)                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │
│  │   Server    │  │   Server    │  │   Server    │    ...     │
│  │   Node 1    │  │   Node 2    │  │   Node 3    │            │
│  └─────────────┘  └─────────────┘  └─────────────┘            │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Navigation

| Category | Document | Description |
|----------|----------|-------------|
| **Installation** | [Quick Start](installation/quickstart.md) | 15-minute setup guide |
| | [Production](installation/production.md) | Production deployment |
| **Architecture** | [Overview](architecture/overview.md) | System design |
| | [Containers](architecture/containers.md) | Container specs |
| | [Networking](architecture/networking.md) | Network design |
| **API** | [Management Server](api/management-server.md) | REST API reference |
| | [MaaS Integration](api/maas-integration.md) | MaaS API details |
| **Security** | [Overview](security/overview.md) | Security architecture |
| | [Hardening](security/hardening.md) | Security checklist |
| **Troubleshooting** | [Common Issues](troubleshooting/common-issues.md) | Problem solutions |
| | [Logs](troubleshooting/logs.md) | Debugging guide |
| **Examples** | [Cloud-Init](examples/cloud-init.md) | Template examples |
| | [Ansible](examples/ansible.md) | Playbook examples |

## Prerequisites

- **Hardware**: At least 8GB RAM and 50GB disk space
- **Operating System**: Ubuntu 20.04+ or similar Linux distribution
- **Software**: Docker and Docker Compose
- **Network**: Network access for package downloads
- **Permissions**: Sudo access for initial setup

## Support and Community

- **Repository**: [https://github.com/penguintechinc/gough](https://github.com/penguintechinc/gough)
- **Issues**: Report bugs and feature requests via GitHub Issues
- **Documentation**: This documentation is version-controlled with the codebase
- **License**: GNU Affero General Public License v3.0 (AGPL-3.0) with commercial licensing options (see LICENSE file)

## Contributing

We welcome contributions to Gough! Please see the main project repository for:
- Development guidelines
- Code style requirements
- Testing procedures
- Pull request process

## Version Information

This documentation is maintained alongside the Gough codebase and reflects the current development state. For specific version information, please refer to the project repository tags and releases.

---

**Built with ❤️ by Penguin Tech Inc using Ubuntu MaaS, py4web, Ansible, and FleetDM**