# Network Architecture

This document details the network architecture of the Gough system, including container networking, external network requirements, security considerations, and traffic flow patterns.

## Network Overview

Gough uses a multi-tier network architecture that separates management, provisioning, and monitoring traffic for security and performance optimization.

### Network Tiers

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Gough Network Architecture                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Internet ──── Firewall ──── Management Network (10.0.1.0/24)      │
│                    │                    │                          │
│                    │                    ├─ Admin Workstations      │
│                    │                    ├─ Gough Management Portal │
│                    │                    └─ FleetDM Dashboard       │
│                    │                                               │
│                    ├──── Provisioning Network (10.0.2.0/24)       │
│                    │              │                               │
│                    │              ├─ MaaS DHCP/PXE Server         │
│                    │              ├─ Target Server 1               │
│                    │              ├─ Target Server 2               │
│                    │              └─ Target Server N               │
│                    │                                               │
│                    └──── Monitoring Network (10.0.3.0/24)         │
│                                   │                               │
│                                   ├─ OSQuery Agents               │
│                                   ├─ System Monitors              │
│                                   └─ Log Collectors               │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │            Docker Container Network (172.20.0.0/16)            │ │
│  │                                                                 │ │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │ │
│  │  │   MaaS   │ │ Mgmt     │ │ FleetDM  │ │ Database │          │ │
│  │  │ 172.20.  │ │ Server   │ │ 172.20.  │ │ Services │          │ │
│  │  │ 0.10     │ │ 172.20.  │ │ 0.30     │ │ 172.20.  │          │ │
│  │  │          │ │ 0.20     │ │          │ │ 0.40-50  │          │ │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘          │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

## Container Networking

### Docker Bridge Network

**Network Name**: `maas-network`  
**Subnet**: `172.20.0.0/16`  
**Gateway**: `172.20.0.1`  
**Driver**: `bridge`

### Container IP Allocation

| Service | Container Name | IP Address | Ports |
|---------|---------------|------------|-------|
| MaaS Server | maas-server | 172.20.0.10 | 5240-5249, 53, 67, 69 |
| Management Server | management-server | 172.20.0.20 | 8000 |
| FleetDM | fleetdm | 172.20.0.30 | 8080, 8443 |
| PostgreSQL | postgres | 172.20.0.40 | 5432 |
| MySQL | mysql | 172.20.0.41 | 3306 |
| Redis | redis | 172.20.0.42 | 6379 |
| Nginx Proxy | nginx | 172.20.0.50 | 80, 443 |
| Prometheus | prometheus | 172.20.0.60 | 9090 |
| Grafana | grafana | 172.20.0.61 | 3000 |

### Docker Compose Network Configuration

```yaml
networks:
  maas-network:
    driver: bridge
    ipam:
      driver: default
      config:
        - subnet: 172.20.0.0/16
          gateway: 172.20.0.1
          ip_range: 172.20.0.0/24
    driver_opts:
      com.docker.network.bridge.name: gough-br0
      com.docker.network.bridge.enable_icc: "true"
      com.docker.network.bridge.enable_ip_masquerade: "true"
```

## External Network Requirements

### Management Network

**Purpose**: Administrative access to web interfaces and APIs

**Requirements**:
- **Subnet**: Dedicated management VLAN (e.g., 10.0.1.0/24)
- **Access**: HTTPS (443), SSH (22) for administration
- **Security**: Firewall rules, VPN access recommended
- **Bandwidth**: 100 Mbps minimum for management traffic

**Services Accessible**:
- Management Portal: https://management.domain.com
- MaaS Web UI: https://maas.domain.com:5240
- FleetDM Dashboard: https://fleet.domain.com:8443
- Monitoring: https://monitoring.domain.com (Grafana)

### Provisioning Network

**Purpose**: PXE boot and server provisioning

**Requirements**:
- **Subnet**: Isolated provisioning VLAN (e.g., 10.0.2.0/24)
- **DHCP**: Managed by MaaS container
- **PXE Boot**: UDP ports 67 (DHCP), 69 (TFTP), 53 (DNS)
- **Bandwidth**: 1 Gbps minimum for OS image deployment
- **Isolation**: No internet access for security

**Network Services**:
- DHCP Server: Automatic IP assignment
- TFTP Server: Boot image delivery
- DNS Server: Local name resolution
- HTTP/HTTPS: OS image and package downloads

### Monitoring Network

**Purpose**: Security monitoring and system telemetry

**Requirements**:
- **Subnet**: Dedicated monitoring VLAN (e.g., 10.0.3.0/24)
- **Access**: Limited to monitoring agents and collectors
- **Security**: Encrypted communication (TLS)
- **Bandwidth**: 100 Mbps for monitoring data

**Traffic Types**:
- OSQuery agent reporting
- System metrics collection
- Log forwarding
- Security event streaming

## Network Security

### Firewall Rules

#### Management Network (10.0.1.0/24)

```bash
# Allow HTTPS to management interfaces
allow from 10.0.1.0/24 to any port 443 proto tcp

# Allow SSH for administration
allow from 10.0.1.0/24 to any port 22 proto tcp

# Allow MaaS web interface
allow from 10.0.1.0/24 to any port 5240 proto tcp

# Allow FleetDM interface
allow from 10.0.1.0/24 to any port 8443 proto tcp

# Allow monitoring interfaces
allow from 10.0.1.0/24 to any port 3000,9090 proto tcp

# Deny all other traffic
deny from 10.0.1.0/24 to any
```

#### Provisioning Network (10.0.2.0/24)

```bash
# Allow PXE boot services
allow from 10.0.2.0/24 to any port 67 proto udp
allow from 10.0.2.0/24 to any port 69 proto udp
allow from 10.0.2.0/24 to any port 53 proto udp

# Allow HTTP/HTTPS for package downloads
allow from 10.0.2.0/24 to any port 80,443 proto tcp

# Allow SSH for provisioned servers
allow from 10.0.2.0/24 to any port 22 proto tcp

# Block internet access from provisioning network
deny from 10.0.2.0/24 to 0.0.0.0/0
```

#### Monitoring Network (10.0.3.0/24)

```bash
# Allow OSQuery agent communication
allow from 10.0.3.0/24 to any port 8443 proto tcp

# Allow metrics collection
allow from 10.0.3.0/24 to any port 9090,9323 proto tcp

# Allow log forwarding
allow from 10.0.3.0/24 to any port 514,5514 proto tcp
allow from 10.0.3.0/24 to any port 514 proto udp

# Deny all other traffic
deny from 10.0.3.0/24 to any
```

### Network Segmentation

#### VLAN Configuration

| VLAN ID | Network | Purpose | Security Level |
|---------|---------|---------|----------------|
| 10 | 10.0.1.0/24 | Management | High |
| 20 | 10.0.2.0/24 | Provisioning | Medium |
| 30 | 10.0.3.0/24 | Monitoring | Medium |
| 100 | 172.20.0.0/16 | Container Network | Internal |

#### Inter-VLAN Routing Rules

```bash
# Management can access all networks
allow from 10.0.1.0/24 to 10.0.2.0/24
allow from 10.0.1.0/24 to 10.0.3.0/24

# Provisioning network isolation
deny from 10.0.2.0/24 to 10.0.1.0/24
deny from 10.0.2.0/24 to 10.0.3.0/24

# Monitoring can report to management
allow from 10.0.3.0/24 to 10.0.1.0/24 port 8443,9090
deny from 10.0.3.0/24 to 10.0.2.0/24
```

## Traffic Flow Patterns

### Server Provisioning Flow

```
1. PXE Boot Request
   Physical Server ─UDP:67─→ MaaS DHCP (10.0.2.10)
   
2. TFTP Boot Image
   Physical Server ─UDP:69─→ MaaS TFTP (10.0.2.10)
   
3. OS Installation
   Physical Server ─TCP:80─→ MaaS HTTP (10.0.2.10)
   
4. Cloud-Init Configuration
   Physical Server ─TCP:443─→ Management Server (172.20.0.20)
   
5. Agent Registration
   Provisioned Server ─TCP:8000─→ Management Portal (172.20.0.20)
   
6. Security Enrollment
   OSQuery Agent ─TCP:8443─→ FleetDM Server (172.20.0.30)
```

### Management Flow

```
1. User Authentication
   Admin Browser ─TCP:443─→ Management Portal (172.20.0.20)
   
2. API Requests
   Management Portal ─TCP:5241─→ MaaS API (172.20.0.10)
   
3. Database Queries
   Management Portal ─TCP:5432─→ PostgreSQL (172.20.0.40)
   
4. Cache Operations
   Management Portal ─TCP:6379─→ Redis (172.20.0.42)
   
5. Background Jobs
   Celery Worker ─TCP:6379─→ Redis Queue (172.20.0.42)
```

### Monitoring Flow

```
1. System Metrics
   Server Agents ─TCP:8000─→ Management Portal (172.20.0.20)
   
2. Security Events
   OSQuery Agents ─TCP:8443─→ FleetDM Server (172.20.0.30)
   
3. Log Collection
   All Services ─TCP:514─→ Syslog Server (External)
   
4. Metrics Export
   Services ─TCP:9090─→ Prometheus (172.20.0.60)
   
5. Dashboard Display
   Grafana ─TCP:9090─→ Prometheus (172.20.0.60)
```

## Network Performance

### Bandwidth Requirements

#### By Network Tier

| Network Tier | Minimum | Recommended | Peak Usage |
|--------------|---------|-------------|------------|
| Management | 10 Mbps | 100 Mbps | During mass operations |
| Provisioning | 100 Mbps | 1 Gbps | OS image deployment |
| Monitoring | 10 Mbps | 100 Mbps | Security event storms |
| Container | 1 Gbps | 10 Gbps | Internal communication |

#### By Service

| Service | Bandwidth Usage | Network Impact |
|---------|-----------------|----------------|
| MaaS PXE Boot | 50 MB per server | High during provisioning |
| OS Deployment | 2-5 GB per server | Very high |
| Management Portal | 1-10 MB per session | Low |
| FleetDM Queries | 1-100 KB per query | Low |
| Log Forwarding | 10-100 MB per day | Medium |
| Metrics Collection | 1-10 MB per day | Low |

### Network Optimization

#### Provisioning Network

```bash
# Optimize for bulk data transfer
echo 'net.core.rmem_max = 134217728' >> /etc/sysctl.conf
echo 'net.core.wmem_max = 134217728' >> /etc/sysctl.conf
echo 'net.ipv4.tcp_rmem = 4096 87380 134217728' >> /etc/sysctl.conf
echo 'net.ipv4.tcp_wmem = 4096 65536 134217728' >> /etc/sysctl.conf
echo 'net.ipv4.tcp_congestion_control = bbr' >> /etc/sysctl.conf
sysctl -p
```

#### Container Network

```bash
# Docker daemon optimization
{
  "mtu": 1500,
  "bip": "172.20.0.1/16",
  "fixed-cidr": "172.20.0.0/24",
  "iptables": false,
  "userland-proxy": false
}
```

## Network Troubleshooting

### Common Network Issues

#### PXE Boot Failures

```bash
# Check DHCP service
docker-compose exec maas-server systemctl status isc-dhcp-server

# Verify DHCP leases
docker-compose exec maas-server cat /var/lib/dhcp/dhcpd.leases

# Check TFTP service
docker-compose exec maas-server systemctl status tftpd-hpa

# Test TFTP connectivity
tftp -v <maas-server-ip> -c get pxelinux.0
```

#### Container Communication

```bash
# Check Docker network
docker network inspect maas-automation_maas-network

# Test container connectivity
docker-compose exec management-server ping postgres
docker-compose exec management-server curl -f http://maas:5240/MAAS/

# Check DNS resolution
docker-compose exec management-server nslookup maas
```

#### External Access Issues

```bash
# Check firewall rules
sudo ufw status verbose

# Test port accessibility
telnet <server-ip> 8000
telnet <server-ip> 5240
telnet <server-ip> 8443

# Check routing
ip route show
ip route get <destination-ip>
```

### Network Monitoring

#### Traffic Analysis

```bash
# Monitor interface traffic
sudo iftop -i eth0
sudo nethogs
sudo iotop

# Analyze network connections
sudo netstat -tulpn
sudo ss -tulpn

# Packet capture
sudo tcpdump -i any -w /tmp/network.pcap host <server-ip>
```

#### Performance Monitoring

```bash
# Network performance testing
iperf3 -s  # On server
iperf3 -c <server-ip> -t 30  # On client

# Latency testing
ping -c 100 <server-ip>
mtr <server-ip>

# DNS performance
dig @<dns-server> <hostname>
nslookup <hostname> <dns-server>
```

## Network Security Best Practices

### Security Recommendations

1. **Network Segmentation**: Use VLANs to isolate different network tiers
2. **Firewall Rules**: Implement strict ingress and egress filtering
3. **Access Control**: Limit administrative access to management network
4. **Monitoring**: Deploy network monitoring and intrusion detection
5. **Encryption**: Use TLS/SSL for all external communications
6. **Regular Audits**: Perform regular network security assessments

### Security Monitoring

```bash
# Monitor network connections
sudo netstat -an | grep :22 | grep ESTABLISHED
sudo ss -tuln | grep :443

# Check for unusual traffic
sudo iftop -P -n -N
sudo tcpdump -i any -n 'not port 22'

# Review firewall logs
sudo tail -f /var/log/ufw.log
sudo journalctl -u ufw -f
```

## Network Documentation Maintenance

### Regular Tasks

1. **IP Address Management**: Maintain accurate IP address assignments
2. **Network Diagrams**: Keep network diagrams up to date
3. **Firewall Rules**: Review and update firewall rules quarterly
4. **Performance Baselines**: Establish and monitor network performance baselines
5. **Security Audits**: Conduct regular network security audits

### Change Management

All network changes should be:
- Documented in advance
- Tested in a development environment
- Implemented during maintenance windows
- Monitored for impact
- Documented in network change log

This network architecture provides a secure, scalable, and maintainable foundation for the Gough hypervisor automation system while ensuring proper isolation and performance for all system components.