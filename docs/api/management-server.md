# Management Server API

The Gough Management Server provides comprehensive RESTful APIs for managing the entire hypervisor automation system. This document covers all available endpoints, authentication methods, request/response formats, and usage examples.

## API Overview

### Base URL
- **Development**: `http://localhost:8000/api`
- **Production**: `https://your-domain.com/api`

### Authentication
All API endpoints require authentication using JSON Web Tokens (JWT).

#### Getting an API Token

```bash
# Login to get JWT token
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@gough.local",
    "password": "your-password"
  }'
```

Response:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expires_in": 3600,
  "token_type": "Bearer"
}
```

#### Using the API Token

```bash
# Use the token in subsequent requests
curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." \
     http://localhost:8000/api/servers
```

### Response Format

All API responses follow a consistent format:

```json
{
  "status": "success|error",
  "data": {
    // Response data
  },
  "message": "Human readable message",
  "pagination": {
    "page": 1,
    "per_page": 20,
    "total": 100,
    "pages": 5
  },
  "timestamp": "2023-12-07T10:30:00Z"
}
```

### Error Handling

Error responses include detailed information:

```json
{
  "status": "error",
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid input data",
    "details": {
      "field": "hostname",
      "error": "Hostname is required"
    }
  },
  "timestamp": "2023-12-07T10:30:00Z"
}
```

## Authentication Endpoints

### POST /api/auth/login
Authenticate user and obtain JWT token.

**Request:**
```json
{
  "email": "admin@gough.local",
  "password": "password"
}
```

**Response:**
```json
{
  "status": "success",
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIs...",
    "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
    "expires_in": 3600,
    "token_type": "Bearer",
    "user": {
      "id": 1,
      "email": "admin@gough.local",
      "name": "Administrator",
      "role": "admin"
    }
  }
}
```

### POST /api/auth/refresh
Refresh an expired JWT token.

**Request:**
```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIs..."
}
```

### POST /api/auth/logout
Invalidate the current JWT token.

**Headers:**
```
Authorization: Bearer <token>
```

### GET /api/auth/me
Get current user information.

**Headers:**
```
Authorization: Bearer <token>
```

**Response:**
```json
{
  "status": "success",
  "data": {
    "id": 1,
    "email": "admin@gough.local",
    "name": "Administrator",
    "role": "admin",
    "permissions": ["read", "write", "admin"],
    "created_at": "2023-01-01T00:00:00Z",
    "last_login": "2023-12-07T10:00:00Z"
  }
}
```

## System Status Endpoints

### GET /api/status
Get overall system health status.

**Response:**
```json
{
  "status": "success",
  "data": {
    "system": "healthy",
    "components": {
      "database": "connected",
      "maas": "connected",
      "fleetdm": "connected",
      "redis": "connected"
    },
    "uptime": "5 days, 12:30:45",
    "version": "1.0.0"
  }
}
```

### GET /api/stats
Get system statistics and metrics.

**Response:**
```json
{
  "status": "success",
  "data": {
    "servers": {
      "total": 45,
      "commissioned": 35,
      "deployed": 30,
      "failed": 2
    },
    "jobs": {
      "pending": 3,
      "running": 1,
      "completed": 127,
      "failed": 5
    },
    "resources": {
      "cpu_usage": "45%",
      "memory_usage": "67%",
      "disk_usage": "23%"
    }
  }
}
```

## Server Management Endpoints

### GET /api/servers
List all managed servers with filtering and pagination.

**Query Parameters:**
- `page`: Page number (default: 1)
- `per_page`: Items per page (default: 20, max: 100)
- `status`: Filter by status (commissioned, deployed, failed, etc.)
- `search`: Search by hostname or MAC address
- `sort`: Sort field (hostname, status, created_at)
- `order`: Sort order (asc, desc)

**Example:**
```bash
curl "http://localhost:8000/api/servers?status=deployed&page=1&per_page=10" \
  -H "Authorization: Bearer <token>"
```

**Response:**
```json
{
  "status": "success",
  "data": [
    {
      "id": 1,
      "hostname": "web-server-01",
      "system_id": "maas-system-id-123",
      "mac_address": "52:54:00:12:34:56",
      "ip_address": "192.168.1.101",
      "status": "deployed",
      "architecture": "amd64/generic",
      "memory": 8192,
      "cpu_cores": 4,
      "storage": 102400,
      "tags": ["web", "production"],
      "created_at": "2023-12-01T10:00:00Z",
      "deployed_at": "2023-12-01T10:30:00Z",
      "template": "docker-host",
      "agent_status": "online",
      "last_seen": "2023-12-07T10:25:00Z"
    }
  ],
  "pagination": {
    "page": 1,
    "per_page": 10,
    "total": 30,
    "pages": 3
  }
}
```

### GET /api/servers/{id}
Get detailed information about a specific server.

**Response:**
```json
{
  "status": "success",
  "data": {
    "id": 1,
    "hostname": "web-server-01",
    "system_id": "maas-system-id-123",
    "mac_address": "52:54:00:12:34:56",
    "ip_address": "192.168.1.101",
    "status": "deployed",
    "architecture": "amd64/generic",
    "power_state": "on",
    "memory": 8192,
    "cpu_cores": 4,
    "cpu_speed": 2400,
    "storage_devices": [
      {
        "name": "sda",
        "size": 102400,
        "type": "ssd",
        "model": "Samsung SSD"
      }
    ],
    "network_interfaces": [
      {
        "name": "ens3",
        "mac_address": "52:54:00:12:34:56",
        "ip_address": "192.168.1.101",
        "subnet": "192.168.1.0/24",
        "vlan": 0
      }
    ],
    "tags": ["web", "production"],
    "deployment_history": [
      {
        "timestamp": "2023-12-01T10:30:00Z",
        "action": "deploy",
        "template": "docker-host",
        "status": "success"
      }
    ],
    "agent_info": {
      "status": "online",
      "version": "1.0.0",
      "last_seen": "2023-12-07T10:25:00Z",
      "system_info": {
        "os": "Ubuntu 24.04 LTS",
        "kernel": "6.5.0-generic",
        "uptime": "6 days, 2:15:30"
      }
    }
  }
}
```

### POST /api/servers
Register a new server (enlisting).

**Request:**
```json
{
  "mac_address": "52:54:00:12:34:57",
  "hostname": "new-server-01",
  "architecture": "amd64/generic",
  "power_type": "ipmi",
  "power_parameters": {
    "power_address": "192.168.1.50",
    "power_user": "admin",
    "power_pass": "password"
  },
  "tags": ["staging"]
}
```

**Response:**
```json
{
  "status": "success",
  "data": {
    "id": 2,
    "system_id": "maas-system-id-456",
    "hostname": "new-server-01",
    "mac_address": "52:54:00:12:34:57",
    "status": "new",
    "message": "Server registered successfully. Commission to complete setup."
  }
}
```

### POST /api/servers/{id}/commission
Commission a server (hardware discovery and testing).

**Request:**
```json
{
  "enable_ssh": true,
  "skip_networking": false,
  "testing_scripts": ["smartctl-validate", "stress-ng-cpu-long"]
}
```

### POST /api/servers/{id}/deploy
Deploy an operating system to a server.

**Request:**
```json
{
  "template": "docker-host",
  "hostname": "web-server-01",
  "user_data": "custom-cloud-init-data",
  "distro_series": "jammy",
  "kernel": "",
  "tags": ["web", "production"],
  "environment_variables": {
    "DOCKER_VERSION": "24.0.7",
    "NODE_ENV": "production"
  }
}
```

**Response:**
```json
{
  "status": "success",
  "data": {
    "job_id": "deploy-job-123",
    "server_id": 1,
    "status": "queued",
    "estimated_duration": "15-20 minutes",
    "template": "docker-host"
  }
}
```

### POST /api/servers/{id}/release
Release a deployed server back to available pool.

**Request:**
```json
{
  "secure_erase": true,
  "quick_erase": false
}
```

### POST /api/servers/{id}/power/{action}
Control server power state.

**Actions:** `on`, `off`, `cycle`, `query`

**Request (for power cycle):**
```json
{
  "force": false
}
```

### DELETE /api/servers/{id}
Remove a server from management (delete from MaaS).

## Job Management Endpoints

### GET /api/jobs
List deployment and management jobs.

**Query Parameters:**
- `status`: Filter by status (pending, running, completed, failed)
- `type`: Filter by job type (deploy, commission, release)
- `server_id`: Filter by server ID

**Response:**
```json
{
  "status": "success",
  "data": [
    {
      "id": 123,
      "type": "deploy",
      "server_id": 1,
      "hostname": "web-server-01",
      "status": "running",
      "progress": 65,
      "template": "docker-host",
      "created_at": "2023-12-07T10:00:00Z",
      "started_at": "2023-12-07T10:01:00Z",
      "estimated_completion": "2023-12-07T10:20:00Z",
      "log_url": "/api/jobs/123/logs"
    }
  ]
}
```

### GET /api/jobs/{id}
Get detailed job information and status.

### GET /api/jobs/{id}/logs
Get job execution logs.

**Response:**
```json
{
  "status": "success",
  "data": {
    "logs": [
      {
        "timestamp": "2023-12-07T10:01:00Z",
        "level": "info",
        "message": "Starting server deployment"
      },
      {
        "timestamp": "2023-12-07T10:01:30Z",
        "level": "info",
        "message": "Downloading Ubuntu 24.04 LTS image"
      }
    ],
    "real_time_url": "ws://localhost:8000/api/jobs/123/logs/stream"
  }
}
```

### POST /api/jobs/{id}/cancel
Cancel a running job.

### DELETE /api/jobs/{id}
Delete a completed job record.

## Template Management Endpoints

### GET /api/templates
List all cloud-init templates.

**Response:**
```json
{
  "status": "success",
  "data": [
    {
      "id": 1,
      "name": "docker-host",
      "description": "Docker host with container runtime",
      "category": "containers",
      "version": "1.0",
      "os_series": ["jammy"],
      "architecture": ["amd64"],
      "packages": ["docker.io", "docker-compose"],
      "variables": [
        {
          "name": "DOCKER_VERSION",
          "type": "string",
          "default": "24.0.7",
          "description": "Docker version to install"
        }
      ],
      "created_at": "2023-11-01T00:00:00Z",
      "updated_at": "2023-12-01T00:00:00Z"
    }
  ]
}
```

### GET /api/templates/{id}
Get template details and content.

**Response:**
```json
{
  "status": "success",
  "data": {
    "id": 1,
    "name": "docker-host",
    "description": "Docker host with container runtime",
    "content": "#cloud-config\npackage_update: true\npackages:\n  - docker.io\n  - docker-compose\n...",
    "variables": [...],
    "usage_count": 15,
    "last_used": "2023-12-06T15:30:00Z"
  }
}
```

### POST /api/templates
Create a new cloud-init template.

**Request:**
```json
{
  "name": "kubernetes-node",
  "description": "Kubernetes worker node",
  "category": "orchestration",
  "os_series": ["jammy"],
  "architecture": ["amd64"],
  "content": "#cloud-config\npackage_update: true\npackages:\n  - kubelet\n  - kubeadm\n  - kubectl\n...",
  "variables": [
    {
      "name": "K8S_VERSION",
      "type": "string",
      "default": "1.28.0",
      "description": "Kubernetes version",
      "required": true
    }
  ]
}
```

### PUT /api/templates/{id}
Update an existing template.

### DELETE /api/templates/{id}
Delete a template (only if not in use).

### POST /api/templates/{id}/validate
Validate template syntax and variables.

**Response:**
```json
{
  "status": "success",
  "data": {
    "valid": true,
    "syntax_errors": [],
    "warnings": [
      "Consider adding error handling for package installation"
    ],
    "variables_valid": true,
    "missing_variables": []
  }
}
```

## Configuration Management Endpoints

### GET /api/config/maas
Get MaaS configuration settings.

**Response:**
```json
{
  "status": "success",
  "data": {
    "maas_url": "http://maas:5240/MAAS/",
    "connection_status": "connected",
    "api_version": "2.0",
    "region_name": "default",
    "default_series": "jammy",
    "default_kernel": "",
    "dns_servers": ["8.8.8.8", "8.8.4.4"],
    "ntp_servers": ["pool.ntp.org"],
    "proxy_url": "",
    "enable_dhcp": true,
    "subnet_config": {
      "subnet": "192.168.1.0/24",
      "gateway": "192.168.1.1",
      "range_start": "192.168.1.100",
      "range_end": "192.168.1.200"
    }
  }
}
```

### PUT /api/config/maas
Update MaaS configuration.

**Request:**
```json
{
  "maas_url": "http://maas:5240/MAAS/",
  "api_key": "consumer_key:token_key:token_secret",
  "dns_servers": ["8.8.8.8", "8.8.4.4"],
  "ntp_servers": ["pool.ntp.org"],
  "default_series": "jammy"
}
```

### GET /api/config/fleetdm
Get FleetDM configuration settings.

### PUT /api/config/fleetdm
Update FleetDM configuration.

### POST /api/config/test-connection
Test external service connections.

**Request:**
```json
{
  "service": "maas",
  "config": {
    "maas_url": "http://maas:5240/MAAS/",
    "api_key": "consumer_key:token_key:token_secret"
  }
}
```

## User Management Endpoints

### GET /api/users
List all users (admin only).

### POST /api/users
Create a new user (admin only).

**Request:**
```json
{
  "email": "user@example.com",
  "name": "John Doe",
  "password": "secure-password",
  "role": "operator",
  "permissions": ["read", "deploy"]
}
```

### PUT /api/users/{id}
Update user information.

### DELETE /api/users/{id}
Delete a user (admin only).

### POST /api/users/{id}/reset-password
Reset user password (admin only).

## Monitoring and Logs Endpoints

### GET /api/logs
Get system logs with filtering.

**Query Parameters:**
- `level`: Filter by log level (debug, info, warning, error)
- `component`: Filter by component (maas, management, fleet, agent)
- `start_date`: Start date filter
- `end_date`: End date filter

### GET /api/metrics
Get system metrics for monitoring.

**Response:**
```json
{
  "status": "success",
  "data": {
    "timestamp": "2023-12-07T10:30:00Z",
    "system": {
      "cpu_usage": 45.2,
      "memory_usage": 67.8,
      "disk_usage": 23.1,
      "network_in": 1024000,
      "network_out": 512000
    },
    "services": {
      "active_connections": 25,
      "response_time_avg": 150,
      "requests_per_second": 12
    },
    "servers": {
      "total": 45,
      "online": 42,
      "deploying": 2,
      "failed": 1
    }
  }
}
```

### GET /api/alerts
Get active system alerts.

## WebSocket API

For real-time updates, connect to WebSocket endpoints:

### Server Status Updates
```javascript
// Connect to server status updates
const ws = new WebSocket('ws://localhost:8000/api/ws/servers');
ws.onmessage = function(event) {
    const update = JSON.parse(event.data);
    // Handle server status update
    console.log('Server update:', update);
};
```

### Job Progress Updates
```javascript
// Connect to job progress updates
const ws = new WebSocket('ws://localhost:8000/api/ws/jobs/123');
ws.onmessage = function(event) {
    const progress = JSON.parse(event.data);
    // Update progress bar
    updateProgress(progress.percentage, progress.message);
};
```

## Rate Limiting

API requests are rate limited:
- **Authenticated requests**: 1000 requests per hour per user
- **Unauthenticated requests**: 100 requests per hour per IP
- **Bulk operations**: 10 requests per minute per user

Rate limit headers are included in all responses:
```
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 999
X-RateLimit-Reset: 1701944400
```

## API Versioning

The API uses URL versioning:
- Current version: `v1`
- URL format: `/api/v1/endpoint`
- Version header: `Accept: application/vnd.gough.v1+json`

## SDK and Client Libraries

### Python Client

```python
from gough_client import GoughClient

client = GoughClient('http://localhost:8000', token='your-jwt-token')

# List servers
servers = client.servers.list()

# Deploy a server
job = client.servers.deploy(
    server_id=1,
    template='docker-host',
    hostname='web-server-01'
)
```

### Shell/Curl Examples

```bash
#!/bin/bash
# Gough API Examples

BASE_URL="http://localhost:8000/api"
TOKEN="your-jwt-token"

# Get system status
curl -H "Authorization: Bearer $TOKEN" "$BASE_URL/status"

# List servers
curl -H "Authorization: Bearer $TOKEN" "$BASE_URL/servers"

# Deploy a server
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"template": "docker-host", "hostname": "web-01"}' \
  "$BASE_URL/servers/1/deploy"
```

## Error Codes

| HTTP Code | Error Code | Description |
|-----------|------------|-------------|
| 400 | VALIDATION_ERROR | Invalid input data |
| 401 | UNAUTHORIZED | Invalid or missing authentication |
| 403 | FORBIDDEN | Insufficient permissions |
| 404 | NOT_FOUND | Resource not found |
| 409 | CONFLICT | Resource conflict (e.g., hostname exists) |
| 429 | RATE_LIMITED | Rate limit exceeded |
| 500 | INTERNAL_ERROR | Server error |
| 502 | EXTERNAL_ERROR | External service error (MaaS, FleetDM) |
| 503 | SERVICE_UNAVAILABLE | Service temporarily unavailable |

## Best Practices

1. **Authentication**: Always use HTTPS in production and rotate tokens regularly
2. **Pagination**: Use pagination for large result sets
3. **Error Handling**: Implement proper error handling with exponential backoff
4. **Rate Limiting**: Respect rate limits and implement client-side throttling
5. **Monitoring**: Monitor API usage and performance
6. **Versioning**: Always specify API version in requests

This API documentation provides comprehensive coverage of all Management Server endpoints. For additional support or questions, refer to the troubleshooting guide or submit an issue to the project repository.