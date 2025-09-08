# Gough Hypervisor - Monitoring and Logging Setup Guide

## Phase 10.2 Infrastructure Implementation

This document provides comprehensive instructions for deploying and managing the monitoring and logging infrastructure for the Gough Hypervisor project.

## Overview

The Gough monitoring and logging stack includes:

### Monitoring Stack
- **Prometheus**: Metrics collection and storage
- **Grafana**: Visualization and dashboards
- **AlertManager**: Alert handling and notifications
- **Node Exporter**: System metrics
- **cAdvisor**: Container metrics
- **Database Exporters**: PostgreSQL, MySQL, Redis metrics
- **Blackbox Exporter**: Endpoint monitoring

### Logging Stack (ELK)
- **Elasticsearch**: Log storage and indexing
- **Logstash**: Log processing and transformation
- **Kibana**: Log visualization and analysis
- **Filebeat**: Log shipping from containers

## Quick Start

### 1. Prerequisites

Ensure you have:
- Docker and Docker Compose installed
- At least 8GB RAM available for the stack
- 50GB free disk space for logs and metrics storage
- Network access for service communication

### 2. Environment Configuration

Copy and configure the monitoring environment:

```bash
cp .env.monitoring .env.monitoring.local
```

Edit `.env.monitoring.local` with your specific configuration:
- Slack webhook URL for alerts
- Email SMTP settings
- Resource limits based on your hardware
- SSL certificates if using HTTPS

### 3. Deploy Monitoring Stack

Deploy with monitoring profile:

```bash
# Start monitoring services
docker-compose --profile monitoring up -d

# Verify services are running
docker-compose ps
```

### 4. Deploy Logging Stack

Deploy with logging profile:

```bash
# Start logging services
docker-compose --profile logging up -d

# Check Elasticsearch cluster health
curl -X GET "localhost:9200/_cluster/health?pretty"
```

### 5. Deploy Both Stacks

For complete monitoring and logging:

```bash
# Start all monitoring and logging services
docker-compose --profile monitoring --profile logging up -d
```

## Service Access

Once deployed, access the services via:

- **Grafana**: http://localhost:3000 (admin/admin - change on first login)
- **Prometheus**: http://localhost:9090
- **AlertManager**: http://localhost:9093
- **Kibana**: http://localhost:5601
- **Elasticsearch**: http://localhost:9200

## Grafana Dashboard Setup

### Default Dashboards

The following dashboards are automatically provisioned:

1. **Gough System Overview** (`gough-system-overview`)
   - Service availability
   - System resource usage
   - Container performance
   - Network metrics

2. **MaaS Monitoring** (`gough-maas-monitoring`)
   - MaaS service status
   - API response times
   - Provisioning metrics
   - Resource utilization

### Adding Custom Dashboards

1. Access Grafana at http://localhost:3000
2. Navigate to Dashboards → Browse
3. Import dashboard JSON files from `config/grafana/dashboards/`
4. Or create new dashboards using the Grafana UI

## Alert Configuration

### Email Alerts

Configure email notifications in AlertManager:

1. Edit `config/alertmanager/alertmanager.yml`
2. Update SMTP settings:
   ```yaml
   global:
     smtp_smarthost: 'your-smtp-server:587'
     smtp_from: 'alerts@yourdomain.com'
     smtp_auth_username: 'your-username'
     smtp_auth_password: 'your-password'
   ```

### Slack Alerts

Configure Slack notifications:

1. Create a Slack webhook URL
2. Set the `SLACK_WEBHOOK_URL` environment variable
3. Restart AlertManager: `docker-compose restart alertmanager`

### Custom Alert Rules

Add custom alerts by editing `config/prometheus/alerts/gough-alerts.yml`:

```yaml
- alert: CustomAlert
  expr: your_metric > threshold
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Custom alert triggered"
    description: "Description of the alert condition"
```

## Log Analysis with Kibana

### Initial Setup

1. Access Kibana at http://localhost:5601
2. Navigate to Stack Management → Index Patterns
3. Create index patterns for:
   - `gough-logs-*` for general logs
   - `gough-errors-*` for error logs
   - `gough-security-*` for security logs

### Common Log Queries

**View MaaS provisioning logs:**
```
service:maas AND (commissioning OR deployment OR pxe_boot)
```

**Find error messages:**
```
severity:error AND @timestamp:[now-1h TO now]
```

**Monitor FleetDM activity:**
```
service:fleetdm AND (enrollment OR query OR osquery)
```

**Database slow queries:**
```
service:(postgres OR mysql) AND slow_query:true
```

## Performance Tuning

### Resource Allocation

Adjust resource limits based on your infrastructure:

**For small deployments (< 20 servers):**
- Elasticsearch: 2GB RAM
- Logstash: 1GB RAM
- Prometheus: 1GB RAM
- Grafana: 512MB RAM

**For medium deployments (20-50 servers):**
- Elasticsearch: 4GB RAM
- Logstash: 2GB RAM
- Prometheus: 2GB RAM
- Grafana: 1GB RAM

**For large deployments (50+ servers):**
- Elasticsearch: 8GB RAM
- Logstash: 4GB RAM
- Prometheus: 4GB RAM
- Grafana: 2GB RAM

### Storage Configuration

Configure data retention policies:

**Prometheus retention:**
```yaml
command:
  - '--storage.tsdb.retention.time=30d'
  - '--storage.tsdb.retention.size=50GB'
```

**Elasticsearch index lifecycle:**
```bash
# Delete indices older than 30 days
curl -X DELETE "localhost:9200/gough-logs-$(date -d '30 days ago' +%Y.%m.%d)"
```

## Maintenance and Operations

### Daily Operations

1. **Check service health:**
   ```bash
   docker-compose ps
   ./scripts/health-check.sh
   ```

2. **Monitor disk usage:**
   ```bash
   df -h
   docker system df
   ```

3. **Review alerts:**
   - Check AlertManager for active alerts
   - Review Grafana dashboards for anomalies
   - Examine Kibana for error patterns

### Weekly Maintenance

1. **Clean up old data:**
   ```bash
   # Clean Docker logs
   docker system prune -f
   
   # Clean old Elasticsearch indices
   ./scripts/cleanup-indices.sh
   ```

2. **Backup configurations:**
   ```bash
   tar -czf gough-config-backup-$(date +%Y%m%d).tar.gz config/
   ```

3. **Update dashboards and alerts:**
   - Review and update Grafana dashboards
   - Tune alert thresholds based on observed patterns
   - Add new monitoring targets as infrastructure grows

### Troubleshooting

**Elasticsearch cluster issues:**
```bash
# Check cluster health
curl localhost:9200/_cluster/health?pretty

# Check node status
curl localhost:9200/_cat/nodes?v

# Check indices status
curl localhost:9200/_cat/indices?v
```

**Prometheus scraping issues:**
```bash
# Check target status
curl localhost:9090/api/v1/targets

# Check configuration
curl localhost:9090/api/v1/status/config
```

**Grafana dashboard issues:**
```bash
# Check Grafana logs
docker-compose logs grafana

# Verify datasource connectivity
curl -u admin:admin localhost:3000/api/datasources
```

## Scaling Considerations

### Horizontal Scaling

For larger deployments, consider:

1. **Multiple Elasticsearch nodes:**
   - Configure cluster discovery
   - Set appropriate shard and replica counts
   - Use dedicated master nodes

2. **Prometheus federation:**
   - Deploy regional Prometheus instances
   - Configure global Prometheus for federation
   - Use recording rules for aggregation

3. **Logstash workers:**
   - Increase pipeline workers
   - Use multiple Logstash instances
   - Implement load balancing

### Vertical Scaling

Adjust resource limits in docker-compose.yml:

```yaml
services:
  elasticsearch:
    deploy:
      resources:
        limits:
          memory: 8G
        reservations:
          memory: 4G
```

## Security Best Practices

1. **Enable authentication:**
   - Configure X-Pack security for Elasticsearch
   - Use LDAP/AD integration for Grafana
   - Set strong passwords for all services

2. **Network security:**
   - Use TLS for inter-service communication
   - Implement firewall rules
   - Use VPN for remote access

3. **Data protection:**
   - Encrypt data at rest
   - Implement backup encryption
   - Regular security audits

## Integration with Gough Services

The monitoring stack automatically discovers and monitors:

- **MaaS containers**: Resource usage, API metrics
- **FleetDM services**: Query performance, agent status
- **Management server**: Web application metrics, database performance
- **Infrastructure services**: PostgreSQL, MySQL, Redis, Nginx

Custom metrics can be added by:

1. Exposing metrics endpoints in applications
2. Updating Prometheus configuration
3. Creating corresponding Grafana panels

## Support and Documentation

For additional help:

- Review Grafana documentation: https://grafana.com/docs/
- Check Prometheus guides: https://prometheus.io/docs/
- Elasticsearch reference: https://www.elastic.co/guide/
- Gough project documentation: `docs/` directory

## Changelog

- **v1.0.0**: Initial monitoring and logging stack implementation
- Phase 10.2 complete with full ELK stack and Prometheus monitoring