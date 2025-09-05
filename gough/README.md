# Gough Hypervisor Automation System

Gough is a hypervisor automation system named after Gough Island - home to penguins and providing them structure, just as this hypervisor gives the entire ecosystem a place to call home. The system combines Ubuntu MaaS (as one component), custom Python 3.12 services, and Ansible to deliver automated bare metal server provisioning with Ubuntu 24.04 LTS.

Repository: https://github.com/penguintechinc/gough

## 🏗️ System Architecture

### Container Architecture
- **MaaS Container**: Ubuntu MaaS server for PXE boot and bare metal provisioning (only MaaS component)
- **Gough Management Server**: Custom py4web portal for hypervisor configuration and orchestration
- **Gough Agent Container**: Custom monitoring and management agent deployed to servers
- **FleetDM Server**: OSQuery fleet management for security monitoring
- **Supporting Services**: PostgreSQL, MySQL, Redis, and Nginx (optional)

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

## 🚀 Quick Start

### Prerequisites
- Docker and Docker Compose
- Ubuntu 20.04+ or similar Linux distribution
- At least 8GB RAM and 50GB disk space
- Network access for package downloads
- Sudo access for initial setup

### Installation
1. **Clone and setup the system:**
   ```bash
   git clone <repository-url>
   cd gough
   ./scripts/init-system.sh
   ```

2. **Configure environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env with your network configuration
   vim .env
   ```

3. **Start all services:**
   ```bash
   docker-compose up -d
   ```

4. **Access the web interfaces:**
   - MaaS Web UI: http://localhost:5240/MAAS/
   - Management Portal: http://localhost:8000/
   - FleetDM Security: https://localhost:8443/

### Default Credentials
- **MaaS Admin**: admin / admin
- **Fleet Admin**: admin@fleet.local / admin123

## 📋 Features

### Core Functionality
✅ **Automated PXE Boot**: Complete PXE boot setup with DHCP, DNS, and TFTP  
✅ **Bare Metal Provisioning**: Deploy Ubuntu 24.04 LTS on physical hardware  
✅ **Web Management Portal**: Modern py4web-based management interface  
✅ **Cloud-Init Integration**: Dynamic server configuration with templates  
✅ **Package Management**: Automated installation of Docker, LXD, and custom packages  
✅ **Ansible Orchestration**: Full automation with Ansible playbooks and roles  
✅ **Security Monitoring**: FleetDM integration with OSQuery agents  
✅ **Agent Deployment**: Automatic agent container deployment to provisioned servers  

### Management Portal Features
- 📊 **Real-time Dashboard**: Server inventory, deployment status, and resource monitoring
- 🖥️ **Server Management**: Machine discovery, commissioning, and deployment control
- 📝 **Template Editor**: Create and manage cloud-init templates for different server roles
- 📦 **Package Configuration**: Define package sets for automated installation
- 🔐 **User Management**: Authentication, role-based access, and API key management
- 📈 **Monitoring Integration**: FleetDM security monitoring and OSQuery management
- 🔧 **System Configuration**: MaaS settings, network configuration, and service management

### Security Features
- 🔒 **TLS Encryption**: All services use TLS certificates
- 🛡️ **Fleet Security**: OSQuery agents for security monitoring
- 🔑 **API Authentication**: JWT-based API security
- 🚫 **Network Isolation**: Container network segmentation
- 📋 **Audit Logging**: Comprehensive audit trails

## 📂 Project Structure

```
gough/
├── containers/                      # Container configurations
│   ├── maas/                       # MaaS server container
│   │   ├── Dockerfile
│   │   ├── config/
│   │   └── scripts/
│   ├── management-server/           # py4web management portal
│   │   ├── Dockerfile
│   │   ├── py4web-app/
│   │   │   ├── controllers/         # Web application controllers
│   │   │   ├── models.py            # Database models
│   │   │   ├── modules/             # Custom modules (MaaS client, etc.)
│   │   │   └── templates/           # HTML templates
│   │   └── requirements.txt
│   ├── agent/                       # Server monitoring agent
│   │   ├── Dockerfile
│   │   ├── scripts/
│   │   └── config/
│   └── fleetdm/                     # FleetDM security server
│       ├── Dockerfile
│       ├── config/
│       └── scripts/
├── ansible/                         # Ansible automation
│   ├── ansible.cfg
│   ├── inventory/
│   ├── playbooks/
│   │   └── deploy-server.yml        # Main deployment playbook
│   └── roles/
│       ├── common-setup/
│       ├── security-hardening/
│       ├── monitoring-setup/
│       └── agent-deployment/
├── cloud-init/                      # Cloud-init templates
│   └── templates/
│       ├── base-server.yaml         # Base Ubuntu 24.04 template
│       └── docker-host.yaml         # Docker host template
├── config/                          # Configuration files
│   ├── secrets.example.yml          # Secrets template
│   ├── ssl/                         # SSL certificates
│   └── ssh/                         # SSH keys
├── scripts/                         # Automation scripts
│   ├── init-system.sh              # System initialization
│   └── deploy-server.sh            # Server deployment
├── docker-compose.yml              # Service orchestration
├── .env.example                    # Environment template
└── README.md                       # This file
```

## 🔧 Configuration

### Environment Variables
Key configuration settings in `.env`:

```bash
# MaaS Configuration
MAAS_URL=http://172.20.0.10:5240/MAAS/
MAAS_API_KEY=consumer_key:token_key:token_secret
DHCP_SUBNET=192.168.1.0/24
DHCP_RANGE_START=192.168.1.100
DHCP_RANGE_END=192.168.1.200

# Management Server
SECRET_KEY=change-this-secret-key-in-production
DATABASE_URL=postgresql://postgres:postgres@postgres:5432/management

# FleetDM
FLEET_URL=https://172.20.0.3:8443
FLEET_ADMIN_EMAIL=admin@fleet.local
FLEET_ADMIN_PASSWORD=admin123

# Security
SSH_PUBLIC_KEY=ssh-ed25519_AAAAC3NzaC1lZDI1NTE5AAAAI...
ANSIBLE_SSH_PUBLIC_KEY=ssh-ed25519_AAAAC3NzaC1lZDI1NTE5AAAAI...
```

### Secrets Management
Store sensitive data in `config/secrets.yml`:

```yaml
# API Keys and tokens
maas:
  api_key: "consumer:token:secret"
fleetdm:
  api_token: "fleet-api-token"
  enroll_secret: "osquery-enroll-secret"

# Database credentials
databases:
  postgresql:
    password: "secure-postgres-password"
  mysql:
    password: "secure-mysql-password"

# SSH Keys
ssh_keys:
  maas_user_public_key: "ssh-ed25519 ..."
  ansible_private_key: |
    -----BEGIN OPENSSH PRIVATE KEY-----
    ...
    -----END OPENSSH PRIVATE KEY-----
```

## 🖥️ Usage

### Server Deployment

1. **Add physical servers to MaaS:**
   - Boot servers from network (PXE)
   - Servers will auto-discover and appear in MaaS
   - Commission servers through MaaS UI or management portal

2. **Deploy using management portal:**
   - Access http://localhost:8000/
   - Navigate to "Deploy Servers"
   - Select servers and configuration templates
   - Monitor deployment progress

3. **Deploy using CLI:**
   ```bash
   ./scripts/deploy-server.sh \
     --hostname web01 \
     --mac 00:11:22:33:44:55 \
     --template docker-host \
     --groups docker_hosts
   ```

### Template Management

Create custom cloud-init templates in `cloud-init/templates/`:

```yaml
#cloud-config
# Custom server template
hostname: ${HOSTNAME}
package_update: true
packages:
  - docker.io
  - docker-compose
  - nginx

runcmd:
  - systemctl enable docker
  - systemctl start docker
  - usermod -aG docker ubuntu

write_files:
  - path: /etc/docker/daemon.json
    content: |
      {
        "log-driver": "json-file",
        "log-opts": {
          "max-size": "10m"
        }
      }
```

### Monitoring and Security

1. **FleetDM Security Dashboard:**
   - Access https://localhost:8443/
   - View enrolled hosts and security status
   - Run security queries across your fleet

2. **Agent Management:**
   - Agents automatically deploy to provisioned servers
   - Monitor server health at http://localhost:8000/servers/
   - Execute remote commands through the portal

## 🛠️ Development

### Local Development Setup

1. **Setup development environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r containers/management-server/requirements.txt
   ```

2. **Run services individually:**
   ```bash
   # Start databases only
   docker-compose up -d postgres mysql redis
   
   # Run management server locally
   cd containers/management-server
   py4web run py4web-app
   ```

3. **Testing:**
   ```bash
   # Test MaaS connectivity
   curl -H "Authorization: OAuth $MAAS_API_KEY" \
        "$MAAS_URL/api/2.0/machines/"
   
   # Test management portal
   curl http://localhost:8000/api/status
   
   # Test FleetDM
   curl -k https://localhost:8443/api/v1/fleet/version
   ```

### API Documentation

The management portal provides RESTful APIs:

- `GET /api/status` - System health status
- `GET /api/servers` - List managed servers
- `POST /api/servers/{id}/deploy` - Deploy server
- `GET /api/templates` - List cloud-init templates
- `POST /api/templates` - Create template
- `GET /api/jobs` - List deployment jobs

## 🔍 Troubleshooting

### Common Issues

1. **MaaS services not starting:**
   ```bash
   docker-compose logs maas
   docker-compose exec maas journalctl -u maas-regiond
   ```

2. **Database connection issues:**
   ```bash
   docker-compose exec postgres pg_isready
   docker-compose exec mysql mysqladmin ping
   ```

3. **Network connectivity problems:**
   ```bash
   docker network ls
   docker network inspect maas-automation_maas-network
   ```

4. **Agent deployment failures:**
   ```bash
   # Check cloud-init logs on target server
   sudo cloud-init status --long
   sudo journalctl -u cloud-final
   ```

### Log Locations

- **Container logs:** `docker-compose logs [service]`
- **Application logs:** `logs/` directory
- **System logs:** `/var/log/maas/` (inside containers)
- **Deployment logs:** `logs/deploy-YYYYMMDD-HHMMSS.log`

## 📊 Monitoring and Alerting

### Built-in Monitoring

- **System Dashboard:** Real-time server status and metrics
- **Deployment Monitoring:** Track provisioning progress and failures
- **Resource Monitoring:** CPU, memory, disk usage across fleet
- **Security Monitoring:** OSQuery-based security event collection

### Integration Options

- **Prometheus:** Metrics collection endpoint at `:9323/metrics`
- **Grafana:** Dashboard templates available in `monitoring/`
- **ELK Stack:** Log forwarding configuration examples
- **Slack/Teams:** Webhook integration for deployment notifications

## 🔐 Security Considerations

### Production Security Checklist

- [ ] Change all default passwords and API keys
- [ ] Generate new SSL certificates for your domain
- [ ] Configure firewall rules for container network
- [ ] Enable audit logging for all administrative actions
- [ ] Set up backup encryption for sensitive data
- [ ] Configure network segmentation between management and provisioning
- [ ] Enable 2FA for management portal access
- [ ] Regular security updates for all container images

### Network Security

- All inter-service communication uses TLS
- Container network isolation with custom bridge
- Firewall rules limit external access
- API endpoints protected with authentication
- SSH key-based authentication only

## 📦 Backup and Recovery

### Backup Strategy

```bash
# Automated backup script
./scripts/backup-system.sh

# Manual backup
docker-compose exec postgres pg_dump management > backup.sql
docker-compose exec mysql mysqldump fleet > fleet-backup.sql
```

### Disaster Recovery

1. **System Recovery:**
   - Restore configuration files and secrets
   - Recreate containers with persistent data
   - Restore database backups
   - Re-enroll agents with FleetDM

2. **Data Recovery:**
   - Database backups in `backups/` directory
   - Container volumes contain persistent data
   - Configuration templates in git repository

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes with tests
4. Submit a pull request

### Development Guidelines

- Follow Python PEP 8 style guide
- Add tests for new functionality
- Update documentation for API changes
- Use conventional commit messages

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🆘 Support

### Documentation
- [Ubuntu MaaS Documentation](https://maas.io/docs)
- [py4web Documentation](https://py4web.com/_documentation)
- [FleetDM Documentation](https://fleetdm.com/docs)
- [Ansible Documentation](https://docs.ansible.com/)

### Community
- GitHub Issues for bug reports and feature requests
- Wiki for additional documentation and examples
- Discussions for questions and community support

---

**Gough - Built with ❤️ by Penguin Tech Inc using Ubuntu MaaS, py4web, Ansible, and FleetDM**