# MaaS Integration API

This document details the integration between Gough and Ubuntu MaaS (Metal as a Service), including API wrappers, authentication methods, data synchronization, and operational procedures.

## Overview

Gough uses Ubuntu MaaS as its core bare metal provisioning engine. The Management Server integrates with MaaS through its REST API to provide a unified interface for server lifecycle management.

### Integration Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Gough MaaS Integration                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────┐ │
│  │   Management    │    │     MaaS API    │    │    MaaS     │ │
│  │     Server      │◄──►│    Wrapper      │◄──►│   Server    │ │
│  │                 │    │                 │    │             │ │
│  │ ┌─────────────┐ │    │ ┌─────────────┐ │    │ ┌─────────┐ │ │
│  │ │   Portal    │ │    │ │OAuth 1.0    │ │    │ │Region   │ │ │
│  │ │   APIs      │ │    │ │Auth Client  │ │    │ │Controller│ │ │
│  │ └─────────────┘ │    │ └─────────────┘ │    │ └─────────┘ │ │
│  │ ┌─────────────┐ │    │ ┌─────────────┐ │    │ ┌─────────┐ │ │
│  │ │Background   │ │    │ │HTTP Client  │ │    │ │Rack     │ │ │
│  │ │Jobs/Celery  │ │    │ │Pool         │ │    │ │Controller│ │ │
│  │ └─────────────┘ │    │ └─────────────┘ │    │ └─────────┘ │ │
│  │ ┌─────────────┐ │    │ ┌─────────────┐ │    │ ┌─────────┐ │ │
│  │ │Data Sync    │ │    │ │Error        │ │    │ │Database │ │ │
│  │ │Service      │ │    │ │Handling     │ │    │ │         │ │ │
│  │ └─────────────┘ │    │ └─────────────┘ │    │ └─────────┘ │ │
│  └─────────────────┘    └─────────────────┘    └─────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

## MaaS API Client

### Client Configuration

The MaaS client is configured through environment variables and database settings:

```python
# Environment Configuration
MaAS_URL = "http://maas:5240/MAAS/"
MaAS_API_KEY = "consumer_key:token_key:token_secret"
MaAS_API_VERSION = "2.0"
MaAS_TIMEOUT = 30
MaAS_RETRY_ATTEMPTS = 3
MaAS_RETRY_DELAY = 5
```

### Authentication

MaaS uses OAuth 1.0 authentication with three-part API keys:

```python
class MaaSClient:
    def __init__(self, maas_url, api_key):
        self.maas_url = maas_url.rstrip('/')
        self.api_key = api_key
        self.api_base = urljoin(self.maas_url, '/api/2.0/')
        self.session = requests.Session()
        self._setup_auth()
    
    def _setup_auth(self):
        """Setup OAuth 1.0 authentication"""
        parts = self.api_key.split(':')
        if len(parts) != 3:
            raise ValueError("Invalid API key format")
        
        consumer_key, token_key, token_secret = parts
        
        # OAuth 1.0 signature
        auth_header = (
            f'OAuth oauth_version="1.0", '
            f'oauth_signature_method="PLAINTEXT", '
            f'oauth_consumer_key="{consumer_key}", '
            f'oauth_token="{token_key}", '
            f'oauth_signature="{consumer_key}&{token_secret}"'
        )
        
        self.session.headers.update({
            'Authorization': auth_header,
            'Content-Type': 'application/json'
        })
```

### API Methods

#### Machine Management

##### List Machines

```python
def get_machines(self, status=None, tags=None):
    """Get all machines from MaaS
    
    Args:
        status: Filter by status (New, Commissioning, Ready, etc.)
        tags: Filter by tags
    
    Returns:
        list: Machine objects
    """
    params = {}
    if status:
        params['status'] = status
    if tags:
        params['tags'] = ','.join(tags)
    
    response = self.session.get(
        urljoin(self.api_base, 'machines/'),
        params=params
    )
    response.raise_for_status()
    return response.json()
```

**Example Response:**
```json
[
  {
    "system_id": "4y3h8a",
    "hostname": "web-server-01",
    "architecture": "amd64/generic",
    "status_name": "Ready",
    "status": 4,
    "power_state": "off",
    "power_type": "ipmi",
    "memory": 8192,
    "cpu_count": 4,
    "storage": 107374182400,
    "ip_addresses": ["192.168.1.101"],
    "mac_addresses": ["52:54:00:12:34:56"],
    "tag_names": ["web", "production"],
    "zone": {"name": "default"},
    "pool": {"name": "default"},
    "created": "2023-12-01T10:00:00.000Z",
    "updated": "2023-12-07T10:30:00.000Z"
  }
]
```

##### Get Machine Details

```python
def get_machine(self, system_id):
    """Get detailed machine information"""
    response = self.session.get(
        urljoin(self.api_base, f'machines/{system_id}/')
    )
    response.raise_for_status()
    return response.json()
```

##### Commission Machine

```python
def commission_machine(self, system_id, enable_ssh=True, skip_networking=False, 
                      testing_scripts=None):
    """Commission a machine"""
    data = {
        'enable_ssh': enable_ssh,
        'skip_networking': skip_networking
    }
    
    if testing_scripts:
        data['testing_scripts'] = ','.join(testing_scripts)
    
    response = self.session.post(
        urljoin(self.api_base, f'machines/{system_id}/'),
        params={'op': 'commission'},
        data=data
    )
    response.raise_for_status()
    return response.json()
```

##### Deploy Machine

```python
def deploy_machine(self, system_id, distro_series='jammy', 
                  user_data='', kernel='', hostname=''):
    """Deploy operating system to machine"""
    data = {
        'distro_series': distro_series,
        'install_kvm': False
    }
    
    if user_data:
        # MaaS expects base64 encoded user data
        import base64
        data['user_data'] = base64.b64encode(user_data.encode()).decode()
    
    if kernel:
        data['kernel'] = kernel
        
    if hostname:
        data['hostname'] = hostname
    
    response = self.session.post(
        urljoin(self.api_base, f'machines/{system_id}/'),
        params={'op': 'deploy'},
        data=data
    )
    response.raise_for_status()
    return response.json()
```

##### Release Machine

```python
def release_machine(self, system_id, secure_erase=False, quick_erase=True):
    """Release a deployed machine"""
    data = {
        'secure_erase': secure_erase,
        'quick_erase': quick_erase
    }
    
    response = self.session.post(
        urljoin(self.api_base, f'machines/{system_id}/'),
        params={'op': 'release'},
        data=data
    )
    response.raise_for_status()
    return response.json()
```

#### Power Management

```python
def power_on_machine(self, system_id):
    """Power on a machine"""
    return self._power_action(system_id, 'on')

def power_off_machine(self, system_id):
    """Power off a machine"""
    return self._power_action(system_id, 'off')

def power_cycle_machine(self, system_id):
    """Power cycle a machine"""
    return self._power_action(system_id, 'cycle')

def _power_action(self, system_id, action):
    """Execute power action"""
    response = self.session.post(
        urljoin(self.api_base, f'machines/{system_id}/'),
        params={'op': f'power_{action}'}
    )
    response.raise_for_status()
    return response.json()
```

#### Network Management

```python
def get_subnets(self):
    """Get all subnets"""
    response = self.session.get(urljoin(self.api_base, 'subnets/'))
    response.raise_for_status()
    return response.json()

def get_vlans(self):
    """Get all VLANs"""
    response = self.session.get(urljoin(self.api_base, 'vlans/'))
    response.raise_for_status()
    return response.json()

def get_spaces(self):
    """Get all spaces (network segments)"""
    response = self.session.get(urljoin(self.api_base, 'spaces/'))
    response.raise_for_status()
    return response.json()
```

#### Zone and Pool Management

```python
def get_zones(self):
    """Get availability zones"""
    response = self.session.get(urljoin(self.api_base, 'zones/'))
    response.raise_for_status()
    return response.json()

def get_resource_pools(self):
    """Get resource pools"""
    response = self.session.get(urljoin(self.api_base, 'resource-pools/'))
    response.raise_for_status()
    return response.json()
```

## Data Synchronization

### Sync Strategy

Gough maintains a local cache of MaaS data for performance and offline capabilities:

```python
class MaaSSyncService:
    def __init__(self, maas_client, db_session):
        self.maas_client = maas_client
        self.db = db_session
    
    def sync_all(self):
        """Full synchronization with MaaS"""
        self.sync_machines()
        self.sync_subnets()
        self.sync_zones()
        self.sync_resource_pools()
    
    def sync_machines(self):
        """Sync machine data from MaaS"""
        try:
            maas_machines = self.maas_client.get_machines()
            
            for maas_machine in maas_machines:
                # Update or create local machine record
                local_machine = self.db.query(Server).filter(
                    Server.system_id == maas_machine['system_id']
                ).first()
                
                if local_machine:
                    self._update_machine(local_machine, maas_machine)
                else:
                    self._create_machine(maas_machine)
                    
            self.db.commit()
            
        except Exception as e:
            self.db.rollback()
            logger.error(f"Machine sync failed: {str(e)}")
            raise
    
    def _update_machine(self, local_machine, maas_machine):
        """Update local machine with MaaS data"""
        local_machine.hostname = maas_machine['hostname']
        local_machine.status = maas_machine['status_name']
        local_machine.power_state = maas_machine['power_state']
        local_machine.memory = maas_machine['memory']
        local_machine.cpu_cores = maas_machine['cpu_count']
        local_machine.storage = maas_machine['storage']
        local_machine.ip_addresses = maas_machine['ip_addresses']
        local_machine.updated_at = datetime.utcnow()
    
    def _create_machine(self, maas_machine):
        """Create new local machine record"""
        machine = Server(
            system_id=maas_machine['system_id'],
            hostname=maas_machine['hostname'],
            mac_address=maas_machine['mac_addresses'][0] if maas_machine['mac_addresses'] else '',
            status=maas_machine['status_name'],
            architecture=maas_machine['architecture'],
            power_state=maas_machine['power_state'],
            memory=maas_machine['memory'],
            cpu_cores=maas_machine['cpu_count'],
            storage=maas_machine['storage'],
            ip_addresses=maas_machine['ip_addresses'],
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        self.db.add(machine)
```

### Periodic Synchronization

```python
# Celery task for periodic sync
from celery import shared_task

@shared_task
def sync_maas_data():
    """Periodic synchronization task"""
    try:
        maas_client = MaaSClient(
            current_app.config['MAAS_URL'],
            current_app.config['MAAS_API_KEY']
        )
        
        with db.session() as session:
            sync_service = MaaSSyncService(maas_client, session)
            sync_service.sync_all()
            
        logger.info("MaaS synchronization completed successfully")
        
    except Exception as e:
        logger.error(f"MaaS synchronization failed: {str(e)}")
        raise

# Schedule periodic sync (every 5 minutes)
from celery.schedules import crontab

BEAT_SCHEDULE = {
    'sync-maas-data': {
        'task': 'sync_maas_data',
        'schedule': crontab(minute='*/5'),
    },
}
```

## Event Handling

### MaaS Webhooks

MaaS can send webhooks for machine state changes:

```python
from flask import request, jsonify

@app.route('/api/webhooks/maas', methods=['POST'])
def handle_maas_webhook():
    """Handle MaaS webhook events"""
    try:
        event_data = request.get_json()
        
        # Verify webhook signature (if configured)
        if not verify_webhook_signature(request):
            return jsonify({'error': 'Invalid signature'}), 401
        
        # Process event based on type
        event_type = event_data.get('type')
        
        if event_type == 'MACHINE_STATUS_CHANGED':
            handle_machine_status_change(event_data)
        elif event_type == 'MACHINE_DEPLOYED':
            handle_machine_deployed(event_data)
        elif event_type == 'MACHINE_RELEASED':
            handle_machine_released(event_data)
        
        return jsonify({'status': 'ok'})
        
    except Exception as e:
        logger.error(f"Webhook processing failed: {str(e)}")
        return jsonify({'error': 'Processing failed'}), 500

def handle_machine_status_change(event_data):
    """Handle machine status change event"""
    system_id = event_data['system_id']
    new_status = event_data['status']
    
    # Update local database
    machine = db.session.query(Server).filter(
        Server.system_id == system_id
    ).first()
    
    if machine:
        machine.status = new_status
        machine.updated_at = datetime.utcnow()
        db.session.commit()
        
        # Emit WebSocket event to update UI
        emit_server_update(machine)
        
        # Trigger any dependent actions
        if new_status == 'Deployed':
            # Start agent deployment
            deploy_agent.delay(machine.id)
```

### Polling Strategy

For environments where webhooks aren't available:

```python
@shared_task
def poll_machine_status(system_id):
    """Poll specific machine status"""
    try:
        maas_client = get_maas_client()
        maas_machine = maas_client.get_machine(system_id)
        
        # Update local database
        local_machine = db.session.query(Server).filter(
            Server.system_id == system_id
        ).first()
        
        if local_machine and local_machine.status != maas_machine['status_name']:
            old_status = local_machine.status
            local_machine.status = maas_machine['status_name']
            local_machine.updated_at = datetime.utcnow()
            db.session.commit()
            
            # Log status change
            logger.info(f"Machine {system_id} status changed: {old_status} -> {maas_machine['status_name']}")
            
            # Emit update
            emit_server_update(local_machine)
            
    except Exception as e:
        logger.error(f"Status polling failed for {system_id}: {str(e)}")
```

## Error Handling

### Retry Logic

```python
import time
from functools import wraps

def retry_on_failure(max_attempts=3, delay=5, backoff=2):
    """Decorator for retrying failed API calls"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempts = 0
            current_delay = delay
            
            while attempts < max_attempts:
                try:
                    return func(*args, **kwargs)
                except (requests.exceptions.RequestException, 
                       requests.exceptions.Timeout) as e:
                    attempts += 1
                    if attempts >= max_attempts:
                        logger.error(f"Max retry attempts reached for {func.__name__}: {str(e)}")
                        raise
                    
                    logger.warning(f"Attempt {attempts} failed for {func.__name__}: {str(e)}. Retrying in {current_delay}s")
                    time.sleep(current_delay)
                    current_delay *= backoff
                    
        return wrapper
    return decorator

class MaaSClient:
    @retry_on_failure(max_attempts=3)
    def get_machines(self):
        # Implementation with automatic retry
        pass
```

### Error Types

```python
class MaaSError(Exception):
    """Base MaaS error"""
    pass

class MaaSConnectionError(MaaSError):
    """MaaS connection error"""
    pass

class MaaSAuthenticationError(MaaSError):
    """MaaS authentication error"""
    pass

class MaaSValidationError(MaaSError):
    """MaaS validation error"""
    pass

class MaaSResourceError(MaaSError):
    """MaaS resource error"""
    pass

def handle_maas_error(response):
    """Convert HTTP errors to specific MaaS errors"""
    if response.status_code == 401:
        raise MaaSAuthenticationError("Invalid API credentials")
    elif response.status_code == 400:
        raise MaaSValidationError(f"Validation error: {response.text}")
    elif response.status_code == 404:
        raise MaaSResourceError("Resource not found")
    elif response.status_code >= 500:
        raise MaaSConnectionError(f"MaaS server error: {response.status_code}")
    else:
        response.raise_for_status()
```

## Configuration Management

### MaaS Configuration

```python
class MaaSConfiguration:
    def __init__(self):
        self.load_config()
    
    def load_config(self):
        """Load MaaS configuration from database"""
        config = db.session.query(MaaSConfig).filter(
            MaaSConfig.is_active == True
        ).first()
        
        if not config:
            raise MaaSError("No active MaaS configuration found")
        
        self.maas_url = config.maas_url
        self.api_key = config.api_key
        self.timeout = config.timeout or 30
        self.retry_attempts = config.retry_attempts or 3
    
    def test_connection(self):
        """Test MaaS connection"""
        try:
            client = MaaSClient(self.maas_url, self.api_key)
            response = client.session.get(
                urljoin(client.api_base, 'version/')
            )
            response.raise_for_status()
            
            version_info = response.json()
            logger.info(f"MaaS connection successful. Version: {version_info}")
            return True
            
        except Exception as e:
            logger.error(f"MaaS connection test failed: {str(e)}")
            return False
    
    def update_config(self, **kwargs):
        """Update MaaS configuration"""
        config = db.session.query(MaaSConfig).filter(
            MaaSConfig.is_active == True
        ).first()
        
        for key, value in kwargs.items():
            if hasattr(config, key):
                setattr(config, key, value)
        
        config.updated_at = datetime.utcnow()
        db.session.commit()
        
        # Reload configuration
        self.load_config()
```

## Performance Optimization

### Connection Pooling

```python
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

class MaaSClient:
    def __init__(self, maas_url, api_key):
        self.session = requests.Session()
        
        # Configure connection pooling
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504]
            )
        )
        
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        # Set timeouts
        self.session.timeout = 30
```

### Caching

```python
from functools import lru_cache
import time

class CachedMaaSClient(MaaSClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache_timeout = 300  # 5 minutes
        self._cache_timestamps = {}
    
    def _is_cache_valid(self, cache_key):
        """Check if cache entry is still valid"""
        if cache_key not in self._cache_timestamps:
            return False
        
        return time.time() - self._cache_timestamps[cache_key] < self._cache_timeout
    
    @lru_cache(maxsize=128)
    def get_machine(self, system_id):
        """Cached machine lookup"""
        cache_key = f"machine_{system_id}"
        
        if not self._is_cache_valid(cache_key):
            # Clear cache entry
            self.get_machine.cache_clear()
            
        result = super().get_machine(system_id)
        self._cache_timestamps[cache_key] = time.time()
        return result
```

## Monitoring and Logging

### API Monitoring

```python
import time
from functools import wraps

def monitor_api_call(func):
    """Monitor MaaS API call performance"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        
        try:
            result = func(*args, **kwargs)
            duration = time.time() - start_time
            
            logger.info(f"MaaS API call {func.__name__} completed in {duration:.2f}s")
            
            # Record metrics
            record_api_metric(func.__name__, duration, 'success')
            
            return result
            
        except Exception as e:
            duration = time.time() - start_time
            
            logger.error(f"MaaS API call {func.__name__} failed after {duration:.2f}s: {str(e)}")
            
            # Record error metrics
            record_api_metric(func.__name__, duration, 'error')
            
            raise
            
    return wrapper

def record_api_metric(operation, duration, status):
    """Record API call metrics"""
    # Implementation depends on monitoring system (Prometheus, etc.)
    pass
```

### Health Checks

```python
@app.route('/api/health/maas')
def maas_health_check():
    """MaaS health check endpoint"""
    try:
        client = get_maas_client()
        
        # Test basic connectivity
        start_time = time.time()
        version_info = client.session.get(
            urljoin(client.api_base, 'version/'),
            timeout=5
        ).json()
        response_time = time.time() - start_time
        
        # Test machine listing (lightweight)
        machines_count = len(client.get_machines())
        
        return jsonify({
            'status': 'healthy',
            'maas_version': version_info,
            'response_time': response_time,
            'machines_count': machines_count,
            'timestamp': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.utcnow().isoformat()
        }), 503
```

## Best Practices

### API Usage

1. **Rate Limiting**: Respect MaaS rate limits and implement client-side throttling
2. **Timeouts**: Always set appropriate timeouts for API calls
3. **Error Handling**: Implement comprehensive error handling and retry logic
4. **Caching**: Cache frequently accessed data to reduce API load
5. **Monitoring**: Monitor API performance and error rates

### Data Consistency

1. **Synchronization**: Implement regular data synchronization
2. **Conflict Resolution**: Handle conflicts between local and MaaS data
3. **Transactions**: Use database transactions for consistency
4. **Idempotency**: Ensure operations are idempotent where possible

### Security

1. **Secure Storage**: Store API keys securely
2. **Key Rotation**: Implement regular API key rotation
3. **Access Control**: Limit MaaS API access to necessary operations
4. **Audit Logging**: Log all MaaS API interactions

This integration documentation provides comprehensive coverage of the MaaS integration patterns used in Gough. The API wrapper provides a robust foundation for all bare metal provisioning operations while maintaining high performance and reliability.