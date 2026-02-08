# FleetDM Integration for Gough Hypervisor

This directory contains the FleetDM container configuration and related files for the Gough Hypervisor Automation System. FleetDM provides OSQuery fleet management for security monitoring and system visibility across deployed servers.

## Directory Structure

```
containers/fleetdm/
├── Dockerfile              # FleetDM container configuration
├── config/
│   ├── fleet.yml           # Main FleetDM server configuration
│   ├── osquery.flags       # OSQuery agent configuration flags
│   └── packs/              # Query packs for monitoring
│       ├── gough-security-monitoring.json
│       ├── gough-system-monitoring.json
│       └── gough-hypervisor-monitoring.json
├── scripts/
│   ├── entrypoint.sh       # Container startup script
│   ├── generate-certs.sh   # SSL certificate generation
│   └── setup-policies.sh   # Initial setup and policy configuration
├── ssl/                    # SSL certificates (generated at runtime)
└── README.md              # This file
```

## Features Implemented

### ✅ OSQuery Configuration
- Complete OSQuery agent configuration flags for FleetDM communication
- SSL/TLS certificate management for secure communication
- Host identification and enrollment settings
- Performance and resource management settings

### ✅ Query Packs
- **Security Monitoring**: Failed SSH logins, privileged commands, network connections, file integrity monitoring, suspicious processes
- **System Monitoring**: Hardware info, resource utilization, disk usage, memory usage, running services
- **Hypervisor Monitoring**: VM/container status, Docker monitoring, LXD containers, resource allocation

### ✅ Automatic Enrollment
- Ansible playbook for OSQuery agent deployment (`ansible/playbooks/deploy-osquery.yml`)
- Cloud-init template with OSQuery pre-installation (`cloud-init/templates/osquery-server.yaml`)
- Automated enrollment secret management
- Health check and monitoring scripts

### ✅ Management Portal Integration
- FleetDM API client for Python integration
- Fleet dashboard with host statistics and status
- Host management and detailed views
- Query execution and results display
- Enrollment secret management

### ✅ Dashboard Integration
- Real-time FleetDM statistics on main dashboard
- Host status summary (online/offline/new/MIA)
- Recent OSQuery results display
- System health monitoring integration

### ✅ Alert Configuration Interface
- Alert rule creation and management
- Multiple condition types (query results, host offline, new hosts)
- Notification channels (email, webhook, Slack, syslog)
- Alert history and resolution tracking
- Severity levels and cooldown periods

### ✅ Advanced Query Builder
- Interactive query builder with OSQuery schema information
- Syntax validation and auto-completion suggestions
- Pre-defined query patterns organized by category
- Query import/export functionality (JSON/SQL formats)
- Query execution history and result caching

## Configuration

### Environment Variables

The FleetDM container uses these environment variables:

```bash
FLEET_MYSQL_ADDRESS=mysql:3306
FLEET_MYSQL_DATABASE=fleet
FLEET_MYSQL_USERNAME=fleet
FLEET_MYSQL_PASSWORD=fleetpass
FLEET_REDIS_ADDRESS=redis:6379
FLEET_ADMIN_EMAIL=admin@fleet.local
FLEET_ADMIN_PASSWORD=admin123
```

### OSQuery Agent Configuration

Agents are configured with these key settings:

```
--tls_hostname=fleetdm:8080
--enroll_secret_path=/etc/osquery/enroll_secret
--host_identifier=uuid
--config_plugin=tls
--logger_plugin=tls
--disable_enrollment=false
```

## Deployment

### Using Docker Compose

Start the complete stack:
```bash
cd /path/to/gough/
docker-compose up -d
```

Start only FleetDM and dependencies:
```bash
docker-compose up -d mysql redis fleetdm
```

### Manual OSQuery Deployment

Deploy OSQuery agents to existing servers:
```bash
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/deploy-osquery.yml
```

### Cloud-Init Integration

Use the OSQuery-enabled server template for new deployments:
```yaml
# In cloud-init configuration
template: osquery-server.yaml
variables:
  FLEETDM_URL: https://your-fleet-server:8443
  FLEET_ENROLL_SECRET: your-enrollment-secret
  GOUGH_MANAGEMENT_URL: http://your-management-server:8000
```

## Management Portal Access

### FleetDM Dashboard
- URL: `http://localhost:8000/fleet/dashboard`
- Features: Host overview, system statistics, recent activities

### Host Management
- URL: `http://localhost:8000/fleet/hosts`
- Features: Host listing, search, detailed views, software inventory

### Query Management
- URL: `http://localhost:8000/fleet/queries`
- Features: Saved queries, query packs, execution history

### Query Builder
- URL: `http://localhost:8000/fleet/query_builder`
- Features: Interactive query building, testing, validation

### Alert Configuration
- URL: `http://localhost:8000/alerts`
- Features: Alert rules, notifications, history, resolution

### Enrollment Management
- URL: `http://localhost:8000/fleet/enrollment`
- Features: Enrollment secrets, host registration

## API Endpoints

### FleetDM Status
```
GET /api/fleet/status
```

### Query Execution
```
POST /fleet/run_query
{
  "query": "SELECT * FROM system_info;",
  "host_ids": [1, 2, 3]
}
```

### Query Results
```
GET /fleet/query_results/{campaign_id}
```

### Alert Management
```
POST /alerts/test/{alert_id}
GET /alerts/history
POST /alerts/resolve/{history_id}
```

## Query Examples

### Security Monitoring
```sql
-- Failed SSH login attempts
SELECT username, tty, host, datetime(time, 'unixepoch') as login_time 
FROM last 
WHERE username != '' AND tty NOT LIKE 'tty%' 
AND time > strftime('%s', 'now', '-24 hours') 
ORDER BY time DESC;

-- Processes with external network connections
SELECT p.name, p.pid, p.cmdline, ps.remote_address, ps.remote_port 
FROM processes p 
JOIN process_open_sockets ps ON p.pid = ps.pid 
WHERE ps.remote_address != '127.0.0.1' AND ps.remote_address != '::1';
```

### System Monitoring
```sql
-- System resource overview
SELECT hostname, 
       cpu_brand, 
       cpu_physical_cores,
       ROUND(physical_memory/1024/1024/1024, 2) as memory_gb,
       hardware_vendor, 
       hardware_model 
FROM system_info;

-- Top memory consuming processes
SELECT name, pid, 
       ROUND(resident_size/1024/1024, 2) as memory_mb,
       cmdline
FROM processes 
WHERE resident_size > 0 
ORDER BY resident_size DESC 
LIMIT 10;
```

### Container Monitoring
```sql
-- Docker container status
SELECT name, image, state, status, 
       datetime(created, 'unixepoch') as created_time 
FROM docker_containers 
ORDER BY created DESC;

-- Running container resource usage
SELECT dc.name, dc.image,
       p.resident_size/1024/1024 as memory_mb,
       p.user_time + p.system_time as cpu_time
FROM docker_containers dc 
JOIN processes p ON dc.pid = p.pid 
WHERE dc.state = 'running';
```

## Troubleshooting

### Check FleetDM Service
```bash
docker-compose logs fleetdm
curl -k https://localhost:8443/api/v1/fleet/version
```

### Check OSQuery Agent
```bash
# On deployed server
sudo systemctl status osquery
sudo tail -f /var/log/osquery/osqueryd.INFO
/usr/local/bin/osquery-health-check
```

### Verify Enrollment
```bash
# Check enrollment secret
docker-compose exec fleetdm cat /etc/fleet/enroll_secret

# Test agent enrollment
osqueryi --config_path=/etc/osquery/osquery.conf "SELECT * FROM osquery_info;"
```

### Database Issues
```bash
# Reset FleetDM database
docker-compose exec fleetdm fleet db prepare --config /etc/fleet/fleet.yml

# Check MySQL connection
docker-compose exec mysql mysql -u fleet -pfleetpass fleet -e "SHOW TABLES;"
```

## Security Considerations

1. **SSL Certificates**: Self-signed certificates are generated by default. Use proper CA-signed certificates in production.

2. **Enrollment Secrets**: Change default enrollment secrets and rotate them regularly.

3. **Database Passwords**: Change default MySQL passwords in production.

4. **Network Security**: Configure firewall rules to restrict FleetDM access.

5. **Query Permissions**: Review and audit saved queries for sensitive data exposure.

## Performance Tuning

1. **Query Intervals**: Adjust query intervals based on your monitoring needs and system capacity.

2. **Resource Limits**: Configure OSQuery memory and CPU limits in the agent configuration.

3. **Database Optimization**: Monitor MySQL performance and optimize queries as needed.

4. **Log Rotation**: Configure log rotation for OSQuery and FleetDM logs.

## Support

For issues specific to the Gough FleetDM integration:
- Check the system logs: `docker-compose logs management-server`
- Review FleetDM logs: `docker-compose logs fleetdm`
- Consult the main Gough documentation in the repository root

For FleetDM-specific issues:
- FleetDM Documentation: https://fleetdm.com/docs
- OSQuery Documentation: https://osquery.readthedocs.io/