# Quick Start Guide

Get Gough up and running in 15 minutes with this streamlined installation guide.

## Prerequisites

Before starting, ensure you have:

- Ubuntu 20.04+ or similar Linux distribution
- Docker and Docker Compose installed
- At least 8GB RAM and 50GB free disk space
- Sudo access for initial setup
- Network connectivity for package downloads

### Install Docker and Docker Compose

If not already installed:

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/download/v2.20.2/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Logout and login again for group changes to take effect
```

## Step 1: Clone the Repository

```bash
git clone https://github.com/penguintechinc/gough.git
cd gough/gough
```

## Step 2: Configure Environment

```bash
# Copy the example environment file
cp .env.example .env

# Edit the configuration with your network settings
vim .env
```

### Key Configuration Settings

Update these essential settings in `.env`:

```bash
# Network Configuration
DHCP_SUBNET=192.168.1.0/24
DHCP_RANGE_START=192.168.1.100
DHCP_RANGE_END=192.168.1.200
DHCP_GATEWAY=192.168.1.1

# Management Network
MANAGEMENT_SUBNET=172.20.0.0/16

# Security (Change these in production!)
SECRET_KEY=your-secret-key-here
MAAS_USER_PASSWORD=change-this-password
FLEET_ADMIN_PASSWORD=change-this-password

# SSH Keys (Add your public keys)
SSH_PUBLIC_KEY=ssh-ed25519 AAAAC3NzaC1lZDI1NTE5...
ANSIBLE_SSH_PUBLIC_KEY=ssh-ed25519 AAAAC3NzaC1lZDI1NTE5...
```

## Step 3: Initialize the System

```bash
# Make the initialization script executable
chmod +x scripts/init-system.sh

# Run the initialization script
./scripts/init-system.sh
```

This script will:
- Create necessary directories and permissions
- Generate SSL certificates
- Set up configuration files
- Prepare the database initialization scripts

## Step 4: Start the Services

```bash
# Start all core services
docker-compose up -d

# Check that all services are running
docker-compose ps
```

You should see all services showing as "Up" or "Up (healthy)":
- maas-server
- postgres
- redis
- management-server
- mysql
- fleetdm

## Step 5: Access the Web Interfaces

Once all services are running, access the web interfaces:

### MaaS Web UI
- **URL**: http://localhost:5240/MAAS/
- **Default Login**: admin / admin
- **Purpose**: Manage bare metal provisioning

### Management Portal
- **URL**: http://localhost:8000/
- **Default Login**: admin@gough.local / admin
- **Purpose**: Central management and orchestration

### FleetDM Security Dashboard
- **URL**: https://localhost:8443/
- **Default Login**: admin@fleet.local / admin123
- **Purpose**: Security monitoring and OSQuery management

## Step 6: Initial Configuration

### Configure MaaS

1. Access MaaS at http://localhost:5240/MAAS/
2. Complete the initial setup wizard:
   - Set admin credentials
   - Configure DNS forwarder (use 8.8.8.8)
   - Set up the default subnet (192.168.1.0/24)
   - Enable DHCP on the subnet

3. Generate API credentials:
   - Go to your username (top right) → Preferences
   - Go to API keys tab
   - Generate a new API key
   - Save this key for the Management Portal

### Configure Management Portal

1. Access Management Portal at http://localhost:8000/
2. Go to Settings
3. Enter your MaaS API credentials:
   - MaaS URL: `http://maas:5240/MAAS/`
   - API Key: (the key from MaaS)
4. Test the connection

### Verify FleetDM

1. Access FleetDM at https://localhost:8443/
2. Accept the self-signed certificate warning
3. Complete the setup wizard:
   - Set admin credentials
   - Configure organization settings
   - Download the Fleet Desktop installer (optional)

## Step 7: Test Server Provisioning

### Option A: Physical Hardware

1. Connect your physical server to the same network as the Gough system
2. Configure the server to PXE boot
3. Power on the server - it should appear in MaaS automatically
4. Commission the server through MaaS or the Management Portal
5. Deploy Ubuntu 24.04 LTS with the base-server template

### Option B: Virtual Machine (Testing)

For testing purposes, you can use a VM:

```bash
# Create a test VM with PXE boot enabled
virt-install \
  --name gough-test \
  --ram 2048 \
  --disk size=20 \
  --network bridge=virbr0,mac=52:54:00:12:34:56 \
  --pxe \
  --graphics none \
  --console pty,target_type=serial
```

## Common Quick Start Issues

### Services Not Starting

```bash
# Check service status
docker-compose ps

# View logs for a specific service
docker-compose logs maas-server
docker-compose logs management-server

# Restart a specific service
docker-compose restart management-server
```

### Port Conflicts

If you have port conflicts, modify these ports in `docker-compose.yml`:
- MaaS: 5240 (change both sides: "5241:5240")
- Management Portal: 8000 (change to "8001:8000")
- FleetDM: 8443 (change to "8444:8443")

### Database Connection Issues

```bash
# Check database status
docker-compose exec postgres pg_isready
docker-compose exec mysql mysqladmin ping

# View database logs
docker-compose logs postgres
docker-compose logs mysql
```

### Network Issues

Ensure your network configuration matches your actual network:

```bash
# Check your current network
ip route show default
ip addr show

# Update .env file with correct network settings
vim .env

# Restart services with new configuration
docker-compose down
docker-compose up -d
```

## Next Steps

Once your quick start installation is complete:

1. **Read the [Production Deployment Guide](production.md)** for production-ready configuration
2. **Explore the [Architecture Overview](../architecture/overview.md)** to understand the system design
3. **Check [Cloud-Init Examples](../examples/cloud-init.md)** for server configuration templates
4. **Review [Security Hardening](../security/hardening.md)** for production security

## Getting Help

If you encounter issues:

1. Check the [Common Issues](../troubleshooting/common-issues.md) guide
2. Review the [Logs and Debugging](../troubleshooting/logs.md) guide
3. Check the project repository issues
4. Ensure all prerequisites are met

## Verification Checklist

- [ ] All Docker containers are running and healthy
- [ ] MaaS web interface is accessible
- [ ] Management Portal is accessible
- [ ] FleetDM is accessible
- [ ] MaaS API connection is working in Management Portal
- [ ] Test server PXE boots successfully (if available)
- [ ] No error logs in any services

Congratulations! You now have a working Gough hypervisor automation system. The system is ready to provision bare metal servers with Ubuntu 24.04 LTS.