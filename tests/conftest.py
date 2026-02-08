#!/usr/bin/env python3
"""
Gough Testing Framework - Global Test Configuration
Pytest configuration with fixtures for comprehensive testing
"""

import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, Optional
from unittest.mock import Mock, patch

import pytest
import redis
from pydal import DAL
from py4web import HTTP

# Test configuration
TEST_CONFIG = {
    'DEBUG': True,
    'TESTING': True,
    'DATABASE_URL': os.getenv('DATABASE_URL', 'sqlite:///:memory:'),
    'REDIS_URL': os.getenv('REDIS_URL', 'redis://localhost:6379/1'),
    'MAAS_URL': os.getenv('MAAS_URL', 'http://localhost:5240/MAAS/'),
    'FLEET_URL': os.getenv('FLEET_URL', 'http://localhost:8080'),
    'SECRET_KEY': 'test_secret_key_not_for_production',
    'JWT_SECRET': 'test_jwt_secret_not_for_production',
    'DISABLE_AUTH': os.getenv('DISABLE_AUTH', '1') == '1'
}


@pytest.fixture(scope='session')
def event_loop():
    """Create an instance of the default event loop for the test session."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope='session')
def test_config():
    """Provide test configuration."""
    return TEST_CONFIG


@pytest.fixture(scope='function')
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as temp_path:
        yield Path(temp_path)


@pytest.fixture(scope='function')
def mock_database():
    """Create an in-memory test database with all tables."""
    db = DAL('sqlite:///:memory:', migrate=True, lazy_tables=True)
    
    # Import and define all tables
    from gough.containers.management_server.py4web_app.models import define_tables
    define_tables(db)
    
    # Commit any pending changes
    db.commit()
    
    yield db
    
    # Cleanup
    db.close()


@pytest.fixture(scope='function')
def sample_server_data():
    """Provide sample server data for testing."""
    return {
        'hostname': 'test-server-01',
        'mac_address': '00:11:22:33:44:55',
        'ip_address': '192.168.1.100',
        'status': 'Ready',
        'architecture': 'amd64',
        'memory': 16384,
        'cpu_count': 8,
        'storage': 500,
        'power_type': 'ipmi',
        'zone': 'default',
        'pool': 'default'
    }


@pytest.fixture(scope='function')
def sample_cloud_init_template():
    """Provide sample cloud-init template data."""
    return {
        'name': 'test-template',
        'description': 'Test cloud-init template',
        'template_content': '''#cloud-config
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ... test@example.com

packages:
  - curl
  - wget
  - git

runcmd:
  - systemctl enable ssh
  - systemctl start ssh''',
        'template_type': 'user-data',
        'is_default': False
    }


@pytest.fixture(scope='function')
def sample_package_config():
    """Provide sample package configuration data."""
    return {
        'name': 'docker-host',
        'description': 'Docker host configuration',
        'packages': json.dumps(['docker.io', 'docker-compose', 'htop', 'vim']),
        'repositories': json.dumps([
            'deb [arch=amd64] https://download.docker.com/linux/ubuntu focal stable'
        ]),
        'pre_install_scripts': 'apt-get update && apt-get upgrade -y',
        'post_install_scripts': 'systemctl enable docker && systemctl start docker',
        'is_default': False
    }


@pytest.fixture(scope='function')
def mock_redis():
    """Create a mock Redis client for testing."""
    mock_redis = Mock(spec=redis.Redis)
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True
    mock_redis.delete.return_value = 1
    mock_redis.exists.return_value = False
    mock_redis.expire.return_value = True
    
    with patch('gough.containers.management_server.py4web_app.lib.redis_client.get_redis_client') as mock_get_redis:
        mock_get_redis.return_value = mock_redis
        yield mock_redis


@pytest.fixture(scope='function')
def mock_maas_client():
    """Create a mock MaaS API client for testing."""
    mock_client = Mock()
    
    # Mock successful responses
    mock_client.get_machines.return_value = [
        {
            'system_id': 'test-machine-01',
            'hostname': 'test-server-01',
            'status_name': 'Ready',
            'architecture': 'amd64/generic',
            'memory': 16384,
            'cpu_count': 8,
            'storage': 500.0,
            'power_type': 'ipmi',
            'zone': {'name': 'default'},
            'pool': {'name': 'default'},
            'ip_addresses': ['192.168.1.100'],
            'boot_interface': {
                'mac_address': '00:11:22:33:44:55'
            }
        }
    ]
    
    mock_client.commission_machine.return_value = {'system_id': 'test-machine-01'}
    mock_client.deploy_machine.return_value = {'system_id': 'test-machine-01'}
    mock_client.release_machine.return_value = {'system_id': 'test-machine-01'}
    mock_client.get_machine_status.return_value = 'Ready'
    
    with patch('gough.containers.management_server.py4web_app.lib.maas_api.MaasAPIClient') as mock_maas:
        mock_maas.return_value = mock_client
        yield mock_client


@pytest.fixture(scope='function')
def mock_fleet_client():
    """Create a mock FleetDM client for testing."""
    mock_client = Mock()
    
    # Mock successful responses
    mock_client.get_hosts.return_value = [
        {
            'id': 1,
            'hostname': 'test-server-01',
            'uuid': str(uuid.uuid4()),
            'platform': 'ubuntu',
            'osquery_version': '5.10.2',
            'last_seen_at': datetime.utcnow().isoformat(),
            'status': 'online'
        }
    ]
    
    mock_client.enroll_host.return_value = {'host_id': 1}
    mock_client.run_query.return_value = {'campaign_id': 123}
    mock_client.get_query_results.return_value = []
    
    with patch('gough.containers.management_server.py4web_app.modules.fleet_client.FleetClient') as mock_fleet:
        mock_fleet.return_value = mock_client
        yield mock_client


@pytest.fixture(scope='function')
def mock_ansible_runner():
    """Create a mock Ansible runner for testing."""
    mock_runner = Mock()
    
    # Mock successful playbook execution
    mock_runner.run.return_value = Mock(
        status='successful',
        rc=0,
        stdout='PLAY RECAP: test-server-01 : ok=10 changed=5 unreachable=0 failed=0',
        stats={
            'test-server-01': {
                'ok': 10,
                'changed': 5,
                'unreachable': 0,
                'failed': 0
            }
        }
    )
    
    with patch('gough.containers.management_server.py4web_app.lib.tasks.deployment.ansible_runner') as mock_ansible:
        mock_ansible.run.return_value = mock_runner
        yield mock_runner


@pytest.fixture(scope='function')
def auth_headers():
    """Provide authentication headers for API testing."""
    return {
        'Authorization': 'Bearer test_jwt_token',
        'Content-Type': 'application/json'
    }


@pytest.fixture(scope='function')
def api_client():
    """Create a test client for API testing."""
    from gough.containers.management_server.py4web_app import create_app
    
    app = create_app(TEST_CONFIG)
    app.config['TESTING'] = True
    
    with app.test_client() as client:
        with app.app_context():
            yield client


@pytest.fixture(scope='function')
def deployment_job_data():
    """Provide sample deployment job data."""
    return {
        'job_id': f'deploy-{uuid.uuid4()}',
        'server_id': 1,
        'cloud_init_template_id': 1,
        'package_config_id': 1,
        'status': 'Pending',
        'ansible_playbook': 'server-deployment.yml',
        'log_output': '',
        'error_message': None,
        'started_on': None,
        'completed_on': None
    }


@pytest.fixture(scope='function')
def fleet_query_data():
    """Provide sample FleetDM query data."""
    return {
        'name': 'system_info',
        'description': 'Get system information',
        'query': 'SELECT hostname, cpu_brand, memory FROM system_info;',
        'category': 'system',
        'is_scheduled': False,
        'interval_seconds': 3600,
        'created_by': 'test_user'
    }


class MockResponse:
    """Mock HTTP response for testing."""
    
    def __init__(self, json_data: Dict, status_code: int = 200, headers: Dict = None):
        self.json_data = json_data
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(json_data)
        self.content = self.text.encode()
    
    def json(self):
        return self.json_data
    
    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


@pytest.fixture
def mock_requests():
    """Mock requests library for external API calls."""
    with patch('requests.get') as mock_get, \
         patch('requests.post') as mock_post, \
         patch('requests.put') as mock_put, \
         patch('requests.delete') as mock_delete:
        
        # Default successful responses
        mock_get.return_value = MockResponse({'status': 'success'})
        mock_post.return_value = MockResponse({'status': 'success'}, 201)
        mock_put.return_value = MockResponse({'status': 'success'})
        mock_delete.return_value = MockResponse({'status': 'success'}, 204)
        
        yield {
            'get': mock_get,
            'post': mock_post,
            'put': mock_put,
            'delete': mock_delete
        }


# Performance testing fixtures
@pytest.fixture(scope='session')
def performance_config():
    """Configuration for performance tests."""
    return {
        'max_response_time': 2.0,  # seconds
        'max_memory_usage': 100,   # MB
        'concurrent_users': 10,
        'test_duration': 30        # seconds
    }


# Cleanup fixtures
@pytest.fixture(autouse=True)
def cleanup_after_test(mock_redis):
    """Cleanup after each test."""
    yield
    
    # Clear Redis mock
    mock_redis.reset_mock()


# Pytest hooks
def pytest_configure(config):
    """Configure pytest with additional settings."""
    # Ensure test reports directory exists
    reports_dir = Path('tests/reports')
    reports_dir.mkdir(parents=True, exist_ok=True)


def pytest_collection_modifyitems(config, items):
    """Modify test collection to add markers based on location."""
    for item in items:
        # Add markers based on test file location
        test_path = str(item.fspath)
        
        if '/unit/' in test_path:
            item.add_marker(pytest.mark.unit)
        elif '/integration/' in test_path:
            item.add_marker(pytest.mark.integration)
        elif '/performance/' in test_path:
            item.add_marker(pytest.mark.performance)
        
        # Add specific component markers
        if '/management_server/' in test_path:
            if '/controllers/' in test_path:
                item.add_marker(pytest.mark.api)
            elif '/models/' in test_path:
                item.add_marker(pytest.mark.database)
            elif '/auth' in test_path:
                item.add_marker(pytest.mark.auth)
        elif '/agent/' in test_path:
            item.add_marker(pytest.mark.agent)
        elif '/ansible/' in test_path:
            item.add_marker(pytest.mark.ansible)
        elif '/cloud_init/' in test_path:
            item.add_marker(pytest.mark.cloud_init)


def pytest_report_header(config):
    """Add custom header to test reports."""
    return [
        "Gough Hypervisor Testing Framework",
        f"Test Environment: {os.getenv('ENVIRONMENT', 'local')}",
        f"Database: {TEST_CONFIG['DATABASE_URL']}",
        f"Redis: {TEST_CONFIG['REDIS_URL']}"
    ]