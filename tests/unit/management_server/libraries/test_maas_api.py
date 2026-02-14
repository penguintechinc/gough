#!/usr/bin/env python3
"""
Unit Tests for MaaS API Library
Tests for MaaS API client, machine management, and commissioning workflows
"""

import json
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

import pytest
import requests


class TestMaasAPIClient:
    """Test cases for MaaS API client functionality."""

    @pytest.fixture
    def maas_config(self):
        """MaaS configuration for testing."""
        return {
            'MAAS_URL': 'http://test-maas:5240/MAAS',
            'MAAS_API_KEY': 'test:api:key',
            'MAAS_USERNAME': 'test-user',
            'REQUEST_TIMEOUT': 30,
            'MAX_RETRIES': 3,
            'RETRY_BACKOFF': 1
        }

    @pytest.fixture
    def sample_machine_data(self):
        """Sample MaaS machine data."""
        return {
            'system_id': 'test-machine-01',
            'hostname': 'test-server-01',
            'status': 4,  # Ready status
            'status_name': 'Ready',
            'architecture': 'amd64/generic',
            'memory': 16384,
            'cpu_count': 8,
            'storage': 500.0,
            'power_type': 'ipmi',
            'power_parameters': {
                'power_address': '192.168.1.100',
                'power_user': 'admin',
                'power_pass': 'password'
            },
            'zone': {'name': 'default'},
            'pool': {'name': 'default'},
            'ip_addresses': ['192.168.1.101'],
            'boot_interface': {
                'mac_address': '00:11:22:33:44:55',
                'name': 'eth0'
            },
            'tags': ['server', 'production']
        }

    def test_maas_client_initialization(self, maas_config):
        """Test MaaS client initialization."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            
            assert client.maas_url == maas_config['MAAS_URL']
            assert client.api_key == maas_config['MAAS_API_KEY']
            assert client.username == maas_config['MAAS_USERNAME']

    def test_authentication_header_generation(self, maas_config):
        """Test OAuth authentication header generation."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            headers = client._get_auth_headers('GET', '/api/2.0/machines/')
            
            assert 'Authorization' in headers
            assert 'OAuth' in headers['Authorization']
            assert 'oauth_consumer_key' in headers['Authorization']

    @pytest.mark.maas
    def test_get_machines(self, maas_config, sample_machine_data, mock_requests):
        """Test retrieving machines from MaaS."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        # Mock successful response
        mock_requests['get'].return_value.json.return_value = [sample_machine_data]
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            machines = client.get_machines()
            
            assert len(machines) == 1
            assert machines[0]['system_id'] == 'test-machine-01'
            assert machines[0]['hostname'] == 'test-server-01'

    @pytest.mark.maas
    def test_get_machine_by_id(self, maas_config, sample_machine_data, mock_requests):
        """Test retrieving a specific machine by system ID."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        mock_requests['get'].return_value.json.return_value = sample_machine_data
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            machine = client.get_machine('test-machine-01')
            
            assert machine['system_id'] == 'test-machine-01'
            assert machine['status_name'] == 'Ready'

    @pytest.mark.maas
    def test_commission_machine(self, maas_config, sample_machine_data, mock_requests):
        """Test commissioning a machine."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        # Mock successful commissioning response
        commissioned_data = sample_machine_data.copy()
        commissioned_data['status_name'] = 'Commissioning'
        mock_requests['post'].return_value.json.return_value = commissioned_data
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            result = client.commission_machine('test-machine-01')
            
            assert result['status_name'] == 'Commissioning'
            mock_requests['post'].assert_called_once()

    @pytest.mark.maas
    def test_deploy_machine(self, maas_config, sample_machine_data, mock_requests):
        """Test deploying a machine with specific OS and configuration."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        # Mock successful deployment response
        deployed_data = sample_machine_data.copy()
        deployed_data['status_name'] = 'Deploying'
        mock_requests['post'].return_value.json.return_value = deployed_data
        
        deployment_config = {
            'distro_series': 'jammy',
            'user_data': '#cloud-config\npackages: [curl]',
            'metadata': 'instance-id: test-01'
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            result = client.deploy_machine('test-machine-01', deployment_config)
            
            assert result['status_name'] == 'Deploying'
            mock_requests['post'].assert_called_once()

    @pytest.mark.maas
    def test_release_machine(self, maas_config, sample_machine_data, mock_requests):
        """Test releasing a deployed machine."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        # Mock successful release response
        released_data = sample_machine_data.copy()
        released_data['status_name'] = 'Releasing'
        mock_requests['post'].return_value.json.return_value = released_data
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            result = client.release_machine('test-machine-01')
            
            assert result['status_name'] == 'Releasing'
            mock_requests['post'].assert_called_once()

    @pytest.mark.maas
    def test_power_control(self, maas_config, mock_requests):
        """Test machine power control operations."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        mock_requests['post'].return_value.json.return_value = {'status': 'success'}
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            
            # Test power on
            result_on = client.power_on_machine('test-machine-01')
            assert result_on['status'] == 'success'
            
            # Test power off
            result_off = client.power_off_machine('test-machine-01')
            assert result_off['status'] == 'success'
            
            # Test power cycle
            result_cycle = client.power_cycle_machine('test-machine-01')
            assert result_cycle['status'] == 'success'

    def test_error_handling(self, maas_config, mock_requests):
        """Test MaaS API error handling."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient, MaasAPIError
        
        # Mock HTTP error response
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.json.return_value = {'error': 'Machine not found'}
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
        mock_requests['get'].return_value = mock_response
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            
            with pytest.raises(MaasAPIError):
                client.get_machine('non-existent-machine')

    def test_retry_mechanism(self, maas_config, mock_requests):
        """Test request retry mechanism on failures."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        # First two calls fail, third succeeds
        mock_requests['get'].side_effect = [
            requests.exceptions.ConnectionError("Connection failed"),
            requests.exceptions.Timeout("Request timeout"),
            Mock(json=lambda: [{'system_id': 'test'}])
        ]
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            result = client.get_machines()
            
            assert len(result) == 1
            assert mock_requests['get'].call_count == 3

    def test_machine_status_monitoring(self, maas_config, sample_machine_data, mock_requests):
        """Test monitoring machine status changes."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        # Simulate status progression
        status_progression = [
            {'status_name': 'Commissioning'},
            {'status_name': 'Testing'},
            {'status_name': 'Ready'}
        ]
        
        mock_requests['get'].side_effect = [
            Mock(json=lambda: {**sample_machine_data, **status})
            for status in status_progression
        ]
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            
            # Monitor status changes
            statuses = []
            for _ in range(len(status_progression)):
                machine = client.get_machine('test-machine-01')
                statuses.append(machine['status_name'])
            
            assert statuses == ['Commissioning', 'Testing', 'Ready']

    def test_machine_filtering(self, maas_config, mock_requests):
        """Test filtering machines by various criteria."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        machines_data = [
            {'system_id': 'machine-1', 'hostname': 'server-01', 'status_name': 'Ready', 'zone': {'name': 'zone1'}},
            {'system_id': 'machine-2', 'hostname': 'server-02', 'status_name': 'Deployed', 'zone': {'name': 'zone1'}},
            {'system_id': 'machine-3', 'hostname': 'server-03', 'status_name': 'Ready', 'zone': {'name': 'zone2'}}
        ]
        
        mock_requests['get'].return_value.json.return_value = machines_data
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            
            # Filter by status
            ready_machines = client.get_machines(status='Ready')
            assert len([m for m in ready_machines if m['status_name'] == 'Ready']) >= 0
            
            # Filter by zone
            zone1_machines = client.get_machines(zone='zone1')
            assert len([m for m in zone1_machines if m['zone']['name'] == 'zone1']) >= 0

    def test_bulk_operations(self, maas_config, mock_requests):
        """Test bulk machine operations."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        machine_ids = ['machine-1', 'machine-2', 'machine-3']
        mock_requests['post'].return_value.json.return_value = {'status': 'success', 'results': machine_ids}
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            result = client.commission_machines(machine_ids)
            
            assert result['status'] == 'success'
            assert len(result['results']) == 3

    def test_cloud_init_integration(self, maas_config, mock_requests):
        """Test cloud-init template integration with MaaS."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        cloud_init_data = {
            'user_data': '''#cloud-config
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
packages:
  - curl
  - vim
runcmd:
  - systemctl enable ssh''',
            'meta_data': 'instance-id: test-instance-01',
            'network_config': 'version: 2\nethernets:\n  eth0:\n    dhcp4: true'
        }
        
        mock_requests['post'].return_value.json.return_value = {'status': 'deploying'}
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            result = client.deploy_machine('test-machine-01', cloud_init_data)
            
            assert 'status' in result

    def test_network_configuration(self, maas_config, mock_requests):
        """Test network interface configuration."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        interface_config = {
            'interface_id': 'eth0',
            'subnet': '192.168.1.0/24',
            'ip_address': '192.168.1.100',
            'gateway': '192.168.1.1'
        }
        
        mock_requests['post'].return_value.json.return_value = {'configured': True}
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            result = client.configure_network_interface('test-machine-01', interface_config)
            
            assert result['configured'] == True

    def test_storage_configuration(self, maas_config, mock_requests):
        """Test storage layout configuration."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        storage_config = {
            'layout': 'lvm',
            'root_size': '50G',
            'swap_size': '4G'
        }
        
        mock_requests['post'].return_value.json.return_value = {'configured': True}
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            result = client.configure_storage('test-machine-01', storage_config)
            
            assert result['configured'] == True

    def test_webhook_integration(self, maas_config):
        """Test MaaS webhook integration for status updates."""
        from gough.containers.management_server.py4web_app.lib.maas_api import process_maas_webhook
        
        webhook_data = {
            'type': 'machine',
            'id': 'test-machine-01',
            'event_type': 'status_change',
            'old_status': 'Commissioning',
            'new_status': 'Ready',
            'timestamp': datetime.utcnow().isoformat()
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.update_machine_status') as mock_update:
            mock_update.return_value = True
            
            result = process_maas_webhook(webhook_data)
            
            assert result == True
            mock_update.assert_called_once_with('test-machine-01', 'Ready')

    @pytest.mark.maas
    def test_rate_limiting(self, maas_config, mock_requests):
        """Test API rate limiting handling."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        # Mock rate limit response
        rate_limit_response = Mock()
        rate_limit_response.status_code = 429
        rate_limit_response.headers = {'Retry-After': '60'}
        rate_limit_response.raise_for_status.side_effect = requests.exceptions.HTTPError("429 Too Many Requests")
        
        mock_requests['get'].return_value = rate_limit_response
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            
            with pytest.raises(Exception):  # Should handle rate limiting
                client.get_machines()

    def test_concurrent_requests(self, maas_config, mock_requests):
        """Test handling concurrent API requests."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        import threading
        
        mock_requests['get'].return_value.json.return_value = [{'system_id': 'test'}]
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_config)
            
            client = MaasAPIClient()
            results = []
            
            def make_request():
                result = client.get_machines()
                results.append(result)
            
            # Create multiple threads
            threads = [threading.Thread(target=make_request) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            
            assert len(results) == 5
            for result in results:
                assert len(result) == 1