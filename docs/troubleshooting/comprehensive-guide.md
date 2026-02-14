# Gough Troubleshooting Guide

This comprehensive troubleshooting guide covers common issues, diagnostic procedures, and solutions for the Gough hypervisor automation system.

## Table of Contents

1. [General Troubleshooting](#general-troubleshooting)
2. [System Component Issues](#system-component-issues)
3. [Network and Connectivity Issues](#network-and-connectivity-issues)
4. [Server Provisioning Issues](#server-provisioning-issues)
5. [Container and Service Issues](#container-and-service-issues)
6. [Database and Storage Issues](#database-and-storage-issues)
7. [Security and Authentication Issues](#security-and-authentication-issues)
8. [Performance Issues](#performance-issues)
9. [Monitoring and Logging Issues](#monitoring-and-logging-issues)
10. [Recovery Procedures](#recovery-procedures)
11. [Diagnostic Tools and Scripts](#diagnostic-tools-and-scripts)

---

## General Troubleshooting

### Initial Diagnostic Steps

When encountering any issue with Gough, follow these initial diagnostic steps:

1. **Check System Health Dashboard**
   - Log into the management portal
   - Review system status indicators
   - Check for active alerts

2. **Verify Service Status**
   ```bash
   # Check all container status
   docker ps -a
   
   # Check specific service logs
   docker logs gough-management-server
   docker logs gough-maas-server
   docker logs gough-fleetdm
   ```

3. **Check Resource Utilization**
   ```bash
   # System resources
   top
   df -h
   free -m
   
   # Container resources
   docker stats
   ```

4. **Review Recent Logs**
   ```bash
   # System logs
   journalctl -xe
   
   # Application logs
   tail -f /var/log/gough/*.log
   ```

### Common Diagnostic Commands

**System Information**:
```bash
# System overview
hostnamectl
systemctl status docker
docker --version
docker-compose --version

# Network information
ip addr show
netstat -tlnp
ss -tlnp

# Disk space
df -h
du -sh /var/lib/docker
```

**Service Health Checks**:
```bash
# Management Server API
curl -k https://localhost:8000/api/status

# MaaS API
curl -k http://localhost:5240/MAAS/api/2.0/version/

# FleetDM API
curl -k https://localhost:8443/api/v1/fleet/version
```

---

## System Component Issues

### Management Server Issues

#### Issue: Management Portal Not Accessible

**Symptoms**:
- Web interface returns 500/502/503 errors
- Connection timeouts or refused connections
- Login page not loading

**Diagnostic Steps**:

1. **Check container status**:
   ```bash
   docker ps | grep management-server
   docker logs gough-management-server --tail 50
   ```

2. **Verify port binding**:
   ```bash
   netstat -tlnp | grep :8000
   ss -tlnp | grep :8000
   ```

3. **Test internal connectivity**:
   ```bash
   docker exec gough-management-server curl -I http://localhost:8000/health
   ```

**Solutions**:

1. **Restart management server**:
   ```bash
   docker-compose restart management-server
   ```

2. **Check configuration**:
   ```bash
   docker exec gough-management-server cat /app/config.py
   ```

3. **Database connectivity**:
   ```bash
   docker exec gough-management-server python -c "
   from app import db
   try:
       db.engine.execute('SELECT 1')
       print('Database connection: OK')
   except Exception as e:
       print(f'Database error: {e}')
   "
   ```

#### Issue: Database Connection Errors

**Symptoms**:
- "Database connection failed" errors
- 500 errors in web interface
- User authentication failures

**Diagnostic Steps**:

1. **Check PostgreSQL status**:
   ```bash
   docker logs gough-postgresql --tail 50
   docker exec gough-postgresql pg_isready -U gough
   ```

2. **Test database connectivity**:
   ```bash
   docker exec gough-postgresql psql -U gough -d gough -c "SELECT version();"
   ```

3. **Check connection parameters**:
   ```bash
   docker exec gough-management-server env | grep -i database
   ```

**Solutions**:

1. **Restart database**:
   ```bash
   docker-compose restart postgresql
   ```

2. **Check database logs**:
   ```bash
   docker exec gough-postgresql tail -f /var/log/postgresql/postgresql-13-main.log
   ```

3. **Reset database connection**:
   ```bash
   docker-compose down
   docker-compose up -d postgresql
   # Wait for database to start
   sleep 30
   docker-compose up -d
   ```

### MaaS Server Issues

#### Issue: MaaS Server Not Responding

**Symptoms**:
- MaaS web interface not accessible
- API calls timeout or return errors
- Server commissioning failures

**Diagnostic Steps**:

1. **Check MaaS services**:
   ```bash
   docker exec gough-maas-server systemctl status maas-rackd
   docker exec gough-maas-server systemctl status maas-regiond
   ```

2. **Check MaaS logs**:
   ```bash
   docker logs gough-maas-server --tail 100
   docker exec gough-maas-server tail -f /var/log/maas/maas.log
   ```

3. **Test MaaS API**:
   ```bash
   curl -H "Authorization: OAuth consumer_key:token_key:token_secret" \
        http://localhost:5240/MAAS/api/2.0/version/
   ```

**Solutions**:

1. **Restart MaaS services**:
   ```bash
   docker exec gough-maas-server systemctl restart maas-rackd
   docker exec gough-maas-server systemctl restart maas-regiond
   ```

2. **Check database connectivity**:
   ```bash
   docker exec gough-maas-server sudo -u postgres psql -c "SELECT datname FROM pg_database;"
   ```

3. **Reconfigure MaaS**:
   ```bash
   docker exec -it gough-maas-server maas-region apikey --username=admin
   ```

#### Issue: DHCP/PXE Boot Problems

**Symptoms**:
- Servers don't receive IP addresses
- PXE boot fails or times out
- Network discovery not working

**Diagnostic Steps**:

1. **Check DHCP service**:
   ```bash
   docker exec gough-maas-server systemctl status isc-dhcp-server
   docker exec gough-maas-server tail -f /var/log/syslog | grep dhcp
   ```

2. **Check TFTP service**:
   ```bash
   docker exec gough-maas-server systemctl status tftpd-hpa
   netstat -ulnp | grep :69
   ```

3. **Test network configuration**:
   ```bash
   docker exec gough-maas-server ip addr show
   docker exec gough-maas-server route -n
   ```

**Solutions**:

1. **Restart network services**:
   ```bash
   docker exec gough-maas-server systemctl restart isc-dhcp-server
   docker exec gough-maas-server systemctl restart tftpd-hpa
   ```

2. **Check DHCP configuration**:
   ```bash
   docker exec gough-maas-server cat /etc/dhcp/dhcpd.conf
   ```

3. **Verify network bridge**:
   ```bash
   # On host system
   brctl show
   ip link show docker0
   ```

---

## Network and Connectivity Issues

### Container Network Issues

#### Issue: Containers Cannot Communicate

**Symptoms**:
- Services cannot reach each other
- API calls between containers fail
- Database connections timeout

**Diagnostic Steps**:

1. **Check Docker networks**:
   ```bash
   docker network ls
   docker network inspect gough_default
   ```

2. **Test container connectivity**:
   ```bash
   docker exec gough-management-server ping gough-postgresql
   docker exec gough-management-server nslookup gough-maas-server
   ```

3. **Check firewall rules**:
   ```bash
   iptables -L -n
   ufw status
   ```

**Solutions**:

1. **Recreate network**:
   ```bash
   docker-compose down
   docker network prune
   docker-compose up -d
   ```

2. **Check DNS resolution**:
   ```bash
   docker exec gough-management-server cat /etc/resolv.conf
   ```

### Host Network Issues

#### Issue: External Network Connectivity Problems

**Symptoms**:
- Cannot reach external APIs
- Package installation failures
- Certificate validation errors

**Diagnostic Steps**:

1. **Test external connectivity**:
   ```bash
   ping google.com
   curl -I https://github.com
   dig google.com
   ```

2. **Check proxy settings**:
   ```bash
   echo $http_proxy
   echo $https_proxy
   cat /etc/environment
   ```

3. **DNS resolution test**:
   ```bash
   nslookup github.com
   cat /etc/resolv.conf
   ```

**Solutions**:

1. **Configure proxy for Docker**:
   ```bash
   # Create /etc/systemd/system/docker.service.d/http-proxy.conf
   [Service]
   Environment="HTTP_PROXY=http://proxy.company.com:8080"
   Environment="HTTPS_PROXY=http://proxy.company.com:8080"
   Environment="NO_PROXY=localhost,127.0.0.1"
   
   systemctl daemon-reload
   systemctl restart docker
   ```

2. **Update DNS settings**:
   ```bash
   # Edit /etc/systemd/resolved.conf
   DNS=8.8.8.8 8.8.4.4
   systemctl restart systemd-resolved
   ```

---

## Server Provisioning Issues

### MaaS Integration Issues

#### Issue: Servers Not Discovered

**Symptoms**:
- Physical servers boot but don't appear in MaaS
- PXE boot successful but no commissioning
- Servers stuck in "New" status

**Diagnostic Steps**:

1. **Check DHCP leases**:
   ```bash
   docker exec gough-maas-server cat /var/lib/dhcp/dhcpd.leases
   ```

2. **Monitor network traffic**:
   ```bash
   tcpdump -i any port 67 or port 68
   ```

3. **Check MaaS region controller**:
   ```bash
   docker exec gough-maas-server maas-region list-machines
   ```

**Solutions**:

1. **Restart MaaS services**:
   ```bash
   docker exec gough-maas-server systemctl restart maas-rackd
   docker exec gough-maas-server systemctl restart maas-regiond
   ```

2. **Check network configuration**:
   ```bash
   docker exec gough-maas-server maas admin subnets read
   ```

3. **Manual server addition**:
   ```bash
   docker exec gough-maas-server maas admin machines create \
     mac_addresses=52:54:00:12:34:56 \
     architecture=amd64/generic \
     power_type=ipmi \
     power_parameters_power_address=192.168.1.100
   ```

#### Issue: Server Commissioning Failures

**Symptoms**:
- Servers fail hardware tests
- Commissioning times out
- "Failed testing" status

**Diagnostic Steps**:

1. **Check commissioning logs**:
   ```bash
   docker exec gough-maas-server maas admin node-script-results read <system_id>
   ```

2. **Review test results**:
   ```bash
   docker exec gough-maas-server maas admin commissioning-results read <system_id>
   ```

3. **Check server console**:
   - Access IPMI console
   - Review boot messages
   - Check hardware errors

**Solutions**:

1. **Skip failed tests**:
   ```bash
   docker exec gough-maas-server maas admin machine commission <system_id> \
     skip_tests=memory,storage
   ```

2. **Update commissioning scripts**:
   ```bash
   docker exec gough-maas-server maas admin commissioning-scripts update
   ```

3. **Manual hardware verification**:
   - Verify RAM is properly seated
   - Check storage device connections
   - Verify network cable connections

#### Issue: Server Deployment Failures

**Symptoms**:
- OS deployment fails
- Cloud-init errors
- Servers don't complete deployment

**Diagnostic Steps**:

1. **Check deployment logs**:
   ```bash
   docker exec gough-maas-server maas admin machine get-curtin-config <system_id>
   ```

2. **Review installation logs**:
   ```bash
   docker exec gough-maas-server tail -f /var/log/maas/maas.log | grep <system_id>
   ```

3. **Check cloud-init logs on target server**:
   ```bash
   ssh ubuntu@<server-ip> sudo tail -f /var/log/cloud-init.log
   ```

**Solutions**:

1. **Retry deployment**:
   ```bash
   docker exec gough-maas-server maas admin machine deploy <system_id> \
     distro_series=jammy hwe_kernel=hwe-22.04
   ```

2. **Update cloud-init template**:
   - Verify template syntax
   - Check variable substitution
   - Test template on development server

3. **Check network connectivity during deployment**:
   - Verify package repository access
   - Check internet connectivity
   - Verify DNS resolution

---

## Container and Service Issues

### Docker Issues

#### Issue: Container Fails to Start

**Symptoms**:
- Container exits immediately
- "Container exited with code 1" errors
- Services unavailable

**Diagnostic Steps**:

1. **Check container logs**:
   ```bash
   docker logs <container-name> --tail 50
   docker logs <container-name> --follow
   ```

2. **Inspect container configuration**:
   ```bash
   docker inspect <container-name>
   ```

3. **Check resource usage**:
   ```bash
   docker stats
   df -h /var/lib/docker
   ```

**Solutions**:

1. **Restart with debug mode**:
   ```bash
   docker run -it <image-name> /bin/bash
   ```

2. **Check for port conflicts**:
   ```bash
   netstat -tlnp | grep <port>
   ```

3. **Clean up Docker resources**:
   ```bash
   docker system prune -a
   docker volume prune
   ```

#### Issue: Out of Disk Space

**Symptoms**:
- "No space left on device" errors
- Container fails to write files
- Database write failures

**Diagnostic Steps**:

1. **Check disk usage**:
   ```bash
   df -h
   du -sh /var/lib/docker
   docker system df
   ```

2. **Identify large files**:
   ```bash
   find /var/lib/docker -size +100M -type f
   ```

**Solutions**:

1. **Clean up Docker**:
   ```bash
   docker system prune -a
   docker image prune -a
   docker volume prune
   ```

2. **Remove old container logs**:
   ```bash
   find /var/lib/docker/containers -name "*.log" -size +100M -delete
   ```

3. **Configure log rotation**:
   ```json
   // /etc/docker/daemon.json
   {
     "log-driver": "json-file",
     "log-opts": {
       "max-size": "100m",
       "max-file": "5"
     }
   }
   ```

---

## Database and Storage Issues

### PostgreSQL Issues

#### Issue: Database Connection Pool Exhaustion

**Symptoms**:
- "Too many connections" errors
- Application timeouts
- Slow database responses

**Diagnostic Steps**:

1. **Check active connections**:
   ```bash
   docker exec gough-postgresql psql -U gough -c "
     SELECT count(*) FROM pg_stat_activity;
     SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
   "
   ```

2. **Check connection limits**:
   ```bash
   docker exec gough-postgresql psql -U gough -c "SHOW max_connections;"
   ```

**Solutions**:

1. **Increase connection limit**:
   ```sql
   -- In PostgreSQL
   ALTER SYSTEM SET max_connections = 200;
   SELECT pg_reload_conf();
   ```

2. **Configure connection pooling**:
   ```bash
   # Install PgBouncer
   docker run -d --name pgbouncer \
     -p 6432:6432 \
     -e DATABASES_HOST=gough-postgresql \
     -e DATABASES_PORT=5432 \
     -e DATABASES_USER=gough \
     -e DATABASES_PASSWORD=password \
     -e DATABASES_DBNAME=gough \
     pgbouncer/pgbouncer
   ```

#### Issue: Database Performance Issues

**Symptoms**:
- Slow query responses
- High CPU usage on database
- Application timeouts

**Diagnostic Steps**:

1. **Check slow queries**:
   ```sql
   SELECT query, mean_time, calls, total_time
   FROM pg_stat_statements
   ORDER BY total_time DESC
   LIMIT 10;
   ```

2. **Check database statistics**:
   ```sql
   SELECT schemaname, tablename, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch
   FROM pg_stat_user_tables;
   ```

**Solutions**:

1. **Update statistics**:
   ```bash
   docker exec gough-postgresql psql -U gough -c "ANALYZE;"
   ```

2. **Create missing indexes**:
   ```sql
   -- Example indexes
   CREATE INDEX CONCURRENTLY idx_servers_status ON servers(status);
   CREATE INDEX CONCURRENTLY idx_jobs_created_at ON jobs(created_at);
   ```

3. **Vacuum database**:
   ```bash
   docker exec gough-postgresql psql -U gough -c "VACUUM ANALYZE;"
   ```

---

## Security and Authentication Issues

### Authentication Problems

#### Issue: JWT Token Issues

**Symptoms**:
- "Invalid token" errors
- Authentication failures
- API access denied

**Diagnostic Steps**:

1. **Check token validity**:
   ```bash
   # Decode JWT token
   echo "<token>" | cut -d. -f2 | base64 -d | jq
   ```

2. **Check system time**:
   ```bash
   date
   ntpq -c peers
   ```

3. **Verify secret key**:
   ```bash
   docker exec gough-management-server env | grep JWT_SECRET
   ```

**Solutions**:

1. **Regenerate tokens**:
   ```bash
   curl -X POST http://localhost:8000/api/auth/login \
     -H "Content-Type: application/json" \
     -d '{"email": "admin@gough.local", "password": "password"}'
   ```

2. **Synchronize system time**:
   ```bash
   ntpdate pool.ntp.org
   systemctl restart systemd-timesyncd
   ```

#### Issue: SSL/TLS Certificate Problems

**Symptoms**:
- "Certificate verification failed" errors
- Browser security warnings
- API calls rejected

**Diagnostic Steps**:

1. **Check certificate validity**:
   ```bash
   openssl s_client -connect localhost:8443 -servername localhost
   openssl x509 -in cert.pem -text -noout
   ```

2. **Check certificate expiration**:
   ```bash
   echo | openssl s_client -connect localhost:8443 2>/dev/null | 
   openssl x509 -noout -dates
   ```

**Solutions**:

1. **Generate new certificates**:
   ```bash
   # Self-signed certificate
   openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes
   ```

2. **Configure certificate in containers**:
   ```yaml
   # docker-compose.yml
   volumes:
     - ./certs/cert.pem:/app/cert.pem
     - ./certs/key.pem:/app/key.pem
   ```

---

## Performance Issues

### System Performance Problems

#### Issue: High CPU Usage

**Symptoms**:
- System sluggish response
- High load averages
- Container CPU limits exceeded

**Diagnostic Steps**:

1. **Identify CPU usage**:
   ```bash
   top
   htop
   docker stats
   ```

2. **Profile applications**:
   ```bash
   # Python profiling
   docker exec gough-management-server py-spy top --pid 1
   ```

**Solutions**:

1. **Scale services horizontally**:
   ```yaml
   # docker-compose.yml
   services:
     management-server:
       deploy:
         replicas: 3
   ```

2. **Optimize database queries**:
   ```sql
   -- Add indexes for frequently queried columns
   CREATE INDEX CONCURRENTLY ON servers(status, created_at);
   ```

#### Issue: Memory Issues

**Symptoms**:
- Out of memory errors
- Container restarts
- System swap usage

**Diagnostic Steps**:

1. **Check memory usage**:
   ```bash
   free -m
   docker stats
   ps aux --sort=-%mem | head
   ```

2. **Check swap usage**:
   ```bash
   swapon --show
   vmstat 1 5
   ```

**Solutions**:

1. **Increase container memory limits**:
   ```yaml
   # docker-compose.yml
   services:
     management-server:
       mem_limit: 2g
   ```

2. **Configure swap if needed**:
   ```bash
   fallocate -l 2G /swapfile
   chmod 600 /swapfile
   mkswap /swapfile
   swapon /swapfile
   ```

---

## Monitoring and Logging Issues

### Log Management Problems

#### Issue: Log Files Growing Too Large

**Symptoms**:
- Disk space issues
- Slow log searches
- System performance degradation

**Solutions**:

1. **Configure log rotation**:
   ```bash
   # /etc/logrotate.d/gough
   /var/log/gough/*.log {
     daily
     rotate 7
     compress
     delaycompress
     missingok
     notifempty
     create 644 gough gough
   }
   ```

2. **Docker log limits**:
   ```yaml
   # docker-compose.yml
   services:
     management-server:
       logging:
         driver: "json-file"
         options:
           max-size: "100m"
           max-file: "5"
   ```

#### Issue: Missing Logs

**Symptoms**:
- No log entries for certain events
- Debugging difficulties
- Audit trail gaps

**Solutions**:

1. **Check log levels**:
   ```bash
   docker exec gough-management-server env | grep LOG_LEVEL
   ```

2. **Enable debug logging**:
   ```bash
   docker-compose up -d -e LOG_LEVEL=DEBUG
   ```

---

## Recovery Procedures

### System Recovery

#### Complete System Recovery

**When to use**: Total system failure, corruption, or major configuration issues

**Procedure**:

1. **Stop all services**:
   ```bash
   docker-compose down -v
   ```

2. **Backup current state**:
   ```bash
   mkdir -p /backup/gough-$(date +%Y%m%d)
   cp -r /var/lib/docker/volumes /backup/gough-$(date +%Y%m%d)/
   ```

3. **Restore from backup**:
   ```bash
   # Restore database
   docker-compose up -d postgresql
   docker exec -i gough-postgresql psql -U gough < backup.sql
   
   # Restore configuration
   cp -r backup/config/* ./
   ```

4. **Restart services**:
   ```bash
   docker-compose up -d
   ```

#### Database Recovery

**Automated backup restoration**:

```bash
#!/bin/bash
# restore-database.sh

BACKUP_DATE=${1:-latest}
BACKUP_PATH="/backup/postgresql"

echo "Stopping services..."
docker-compose stop management-server

echo "Restoring database from $BACKUP_DATE..."
docker exec -i gough-postgresql psql -U gough -c "DROP DATABASE IF EXISTS gough_restore;"
docker exec -i gough-postgresql psql -U gough -c "CREATE DATABASE gough_restore;"
docker exec -i gough-postgresql psql -U gough gough_restore < "$BACKUP_PATH/gough-$BACKUP_DATE.sql"

echo "Switching to restored database..."
docker exec -i gough-postgresql psql -U gough -c "
  DROP DATABASE gough;
  ALTER DATABASE gough_restore RENAME TO gough;
"

echo "Restarting services..."
docker-compose up -d
```

### Emergency Procedures

#### Emergency MaaS Reset

**When to use**: MaaS database corruption or severe configuration issues

```bash
#!/bin/bash
# emergency-maas-reset.sh

echo "WARNING: This will reset MaaS completely!"
read -p "Continue? (y/N): " -n 1 -r
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 1
fi

# Stop MaaS
docker-compose stop maas-server

# Reset MaaS database
docker exec gough-postgresql psql -U postgres -c "DROP DATABASE IF EXISTS maasdb;"
docker exec gough-postgresql psql -U postgres -c "CREATE DATABASE maasdb OWNER maas;"

# Restart and reconfigure
docker-compose up -d maas-server
sleep 30

# Reconfigure MaaS
docker exec -it gough-maas-server maas-region createadmin \
  --username admin \
  --password admin \
  --email admin@gough.local

echo "MaaS reset complete. Please reconfigure through web interface."
```

---

## Diagnostic Tools and Scripts

### System Health Check Script

```bash
#!/bin/bash
# gough-health-check.sh

echo "=== Gough System Health Check ==="
echo "Date: $(date)"
echo

# Container status
echo "=== Container Status ==="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo

# Service health checks
echo "=== Service Health Checks ==="
services=(
  "http://localhost:8000/api/status:Management Server"
  "http://localhost:5240/MAAS/api/2.0/version/:MaaS Server"
  "https://localhost:8443/api/v1/fleet/version:FleetDM Server"
)

for service in "${services[@]}"; do
  url="${service%:*}"
  name="${service#*:}"
  if curl -s -f -k "$url" >/dev/null 2>&1; then
    echo "✓ $name: OK"
  else
    echo "✗ $name: FAILED"
  fi
done
echo

# Resource usage
echo "=== Resource Usage ==="
echo "CPU: $(top -bn1 | grep "Cpu(s)" | sed "s/.*, *\([0-9.]*\)%* id.*/\1/" | awk '{print 100 - $1"%"}')"
echo "Memory: $(free | grep Mem | awk '{printf("%.1f%%", $3/$2 * 100.0)}')"
echo "Disk: $(df -h / | tail -1 | awk '{print $5}')"
echo

# Network connectivity
echo "=== Network Connectivity ==="
if ping -c 1 google.com >/dev/null 2>&1; then
  echo "✓ Internet connectivity: OK"
else
  echo "✗ Internet connectivity: FAILED"
fi

if nslookup google.com >/dev/null 2>&1; then
  echo "✓ DNS resolution: OK"
else
  echo "✗ DNS resolution: FAILED"
fi
echo

# Database connectivity
echo "=== Database Connectivity ==="
if docker exec gough-postgresql pg_isready -U gough >/dev/null 2>&1; then
  echo "✓ PostgreSQL: OK"
else
  echo "✗ PostgreSQL: FAILED"
fi
echo

echo "=== Health check complete ==="
```

### Log Analysis Script

```bash
#!/bin/bash
# analyze-logs.sh

DAYS=${1:-1}
LOG_DIR="/var/log/gough"

echo "=== Log Analysis (Last $DAYS day(s)) ==="

# Error summary
echo "=== Error Summary ==="
find $LOG_DIR -name "*.log" -mtime -$DAYS -exec grep -h "ERROR\|CRITICAL" {} \; | 
  awk '{print $1, $2, $4}' | sort | uniq -c | sort -rn | head -10

# Warning summary
echo "=== Warning Summary ==="
find $LOG_DIR -name "*.log" -mtime -$DAYS -exec grep -h "WARNING" {} \; | 
  awk '{print $4}' | sort | uniq -c | sort -rn | head -5

# Most active components
echo "=== Most Active Components ==="
find $LOG_DIR -name "*.log" -mtime -$DAYS -exec basename {} .log \; | 
  sort | uniq -c | sort -rn

echo "=== Analysis complete ==="
```

### Performance Monitor Script

```bash
#!/bin/bash
# performance-monitor.sh

DURATION=${1:-60}
INTERVAL=5

echo "=== Performance Monitor (${DURATION}s) ==="
echo "Timestamp,CPU%,Memory%,Disk%,Load1m,Load5m,Load15m"

for i in $(seq 1 $((DURATION/INTERVAL))); do
  timestamp=$(date '+%H:%M:%S')
  cpu=$(top -bn1 | grep "Cpu(s)" | sed "s/.*, *\([0-9.]*\)%* id.*/\1/" | awk '{print 100 - $1}')
  memory=$(free | grep Mem | awk '{printf("%.1f", $3/$2 * 100.0)}')
  disk=$(df -h / | tail -1 | awk '{print $5}' | sed 's/%//')
  load=$(uptime | awk -F'load average:' '{ print $2 }' | sed 's/,//g')
  
  echo "$timestamp,$cpu,$memory,$disk,$load"
  sleep $INTERVAL
done
```

### Backup Verification Script

```bash
#!/bin/bash
# verify-backups.sh

BACKUP_DIR="/backup"
MAX_AGE_DAYS=7

echo "=== Backup Verification ==="

# Check backup directories
for component in postgresql management fleetdm; do
  backup_path="$BACKUP_DIR/$component"
  
  if [ ! -d "$backup_path" ]; then
    echo "✗ $component: Backup directory missing"
    continue
  fi
  
  # Find latest backup
  latest_backup=$(find "$backup_path" -name "*.sql" -o -name "*.tar.gz" | 
                 xargs ls -t | head -1)
  
  if [ -z "$latest_backup" ]; then
    echo "✗ $component: No backups found"
    continue
  fi
  
  # Check backup age
  backup_age=$(find "$latest_backup" -mtime +$MAX_AGE_DAYS)
  if [ -n "$backup_age" ]; then
    echo "⚠ $component: Backup older than $MAX_AGE_DAYS days"
  else
    echo "✓ $component: Recent backup available"
  fi
  
  # Check backup size
  backup_size=$(du -sh "$latest_backup" | cut -f1)
  echo "  Latest backup: $(basename "$latest_backup") ($backup_size)"
done

echo "=== Verification complete ==="
```

This comprehensive troubleshooting guide provides systematic approaches to diagnosing and resolving issues across all components of the Gough hypervisor automation system.