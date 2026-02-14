#!/usr/bin/env python3
"""
Integration Tests for MaaS Provisioning Workflow
End-to-end testing of MaaS integration, machine commissioning, and deployment
"""

import json
import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

import pytest
import requests


class TestMaasProvisioningWorkflow:
    """Integration test cases for MaaS provisioning workflow."""

    @pytest.fixture
    def maas_test_config(self):
        """MaaS test environment configuration."""
        return {
            'MAAS_URL': 'http://test-maas:5240/MAAS',
            'MAAS_API_KEY': 'test:integration:key',
            'MAAS_USERNAME': 'test-admin',
            'TEST_MACHINE_MAC': '00:16:3e:12:34:56',
            'TEST_SUBNET': '192.168.100.0/24',
            'TEST_VLAN': 'test-vlan',
            'COMMISSIONING_TIMEOUT': 600,  # 10 minutes
            'DEPLOYMENT_TIMEOUT': 1800     # 30 minutes
        }

    @pytest.fixture
    def test_machine_spec(self):
        """Test machine specification."""
        return {
            'hostname': 'test-integration-server',
            'mac_address': '00:16:3e:12:34:56',
            'power_type': 'virsh',
            'power_parameters': {
                'power_address': 'qemu+ssh://admin@hypervisor/system',
                'power_id': 'test-vm-01'
            },
            'architecture': 'amd64',
            'min_memory': 4096,
            'min_cpu_count': 2,
            'min_storage': 50
        }

    @pytest.mark.integration
    @pytest.mark.maas
    def test_machine_discovery_and_enlistment(self, maas_test_config, test_machine_spec, mock_maas_client):
        """Test machine discovery and automatic enlistment process."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        from gough.containers.management_server.py4web_app.controllers.webhooks import process_maas_webhook
        
        # Mock new machine enlistment webhook
        enlistment_webhook = {
            'type': 'machine',
            'action': 'new',
            'system_id': 'test-machine-001',
            'hostname': test_machine_spec['hostname'],
            'status': 'New',
            'mac_addresses': [test_machine_spec['mac_address']],
            'architecture': test_machine_spec['architecture']
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_test_config)
            
            # Process enlistment webhook
            result = process_maas_webhook(enlistment_webhook)
            
            assert result['status'] == 'processed'
            assert result['machine_enlisted'] == True
            
            # Verify machine appears in database
            client = MaasAPIClient()
            machines = client.get_machines(status='New')
            
            test_machine = next((m for m in machines if m['hostname'] == test_machine_spec['hostname']), None)
            assert test_machine is not None
            assert test_machine['status_name'] == 'New'

    @pytest.mark.integration
    @pytest.mark.maas
    @pytest.mark.slow
    def test_machine_commissioning_workflow(self, maas_test_config, test_machine_spec, mock_maas_client):
        """Test complete machine commissioning workflow."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import commission_machine
        
        machine_id = 'test-machine-commissioning'
        
        # Mock commissioning progression
        commissioning_states = [
            {'status_name': 'Commissioning', 'status': 1},
            {'status_name': 'Testing', 'status': 7},
            {'status_name': 'Ready', 'status': 4}
        ]
        
        mock_maas_client.get_machine.side_effect = [
            {**test_machine_spec, 'system_id': machine_id, **state}
            for state in commissioning_states
        ]
        
        mock_maas_client.commission_machine.return_value = {
            'system_id': machine_id,
            'status_name': 'Commissioning'
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_test_config)
            
            # Start commissioning
            result = commission_machine(machine_id)
            
            assert result['status'] == 'started'
            assert result['machine_id'] == machine_id
            
            # Simulate waiting for commissioning completion
            # In real test, this would poll the actual MaaS API
            commissioning_complete = False
            max_attempts = 10
            attempts = 0
            
            while not commissioning_complete and attempts < max_attempts:
                client = MaasAPIClient()
                machine = client.get_machine(machine_id)
                
                if machine['status_name'] == 'Ready':
                    commissioning_complete = True
                    break
                
                time.sleep(1)  # Short sleep for test
                attempts += 1
            
            assert commissioning_complete == True
            assert machine['status_name'] == 'Ready'

    @pytest.mark.integration
    @pytest.mark.maas
    @pytest.mark.slow
    def test_machine_deployment_workflow(self, maas_test_config, test_machine_spec, mock_maas_client, sample_cloud_init_template):
        """Test complete machine deployment workflow."""
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import deploy_machine
        
        machine_id = 'test-machine-deployment'
        
        # Mock deployment progression
        deployment_states = [
            {'status_name': 'Deploying', 'status': 9},
            {'status_name': 'Deployed', 'status': 6}
        ]
        
        mock_maas_client.get_machine.side_effect = [
            {**test_machine_spec, 'system_id': machine_id, **state}
            for state in deployment_states
        ]
        
        mock_maas_client.deploy_machine.return_value = {
            'system_id': machine_id,
            'status_name': 'Deploying'
        }
        
        deployment_config = {
            'distro_series': 'jammy',
            'user_data': sample_cloud_init_template['template_content'],
            'hostname': test_machine_spec['hostname']
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_test_config)
            
            # Start deployment
            result = deploy_machine(machine_id, deployment_config)
            
            assert result['status'] == 'started'
            assert result['machine_id'] == machine_id
            assert result['deployment_config'] == deployment_config
            
            # Simulate waiting for deployment completion
            deployment_complete = False
            max_attempts = 20
            attempts = 0
            
            while not deployment_complete and attempts < max_attempts:
                from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
                client = MaasAPIClient()
                machine = client.get_machine(machine_id)
                
                if machine['status_name'] == 'Deployed':
                    deployment_complete = True
                    break
                
                time.sleep(2)  # Longer sleep for deployment
                attempts += 1
            
            assert deployment_complete == True
            assert machine['status_name'] == 'Deployed'

    @pytest.mark.integration
    @pytest.mark.maas
    def test_network_configuration_integration(self, maas_test_config, test_machine_spec, mock_maas_client):
        """Test network configuration during machine deployment."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        machine_id = 'test-network-config'
        network_config = {
            'version': 2,
            'ethernets': {
                'eth0': {
                    'addresses': ['192.168.100.10/24'],
                    'gateway4': '192.168.100.1',
                    'nameservers': {
                        'addresses': ['8.8.8.8', '8.8.4.4']
                    }
                }
            }
        }
        
        mock_maas_client.configure_network_interface.return_value = {'configured': True}
        mock_maas_client.get_machine.return_value = {
            **test_machine_spec,
            'system_id': machine_id,
            'status_name': 'Ready',
            'ip_addresses': ['192.168.100.10']
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_test_config)
            
            client = MaasAPIClient()
            
            # Configure network
            result = client.configure_network_interface(machine_id, network_config)
            assert result['configured'] == True
            
            # Verify network configuration
            machine = client.get_machine(machine_id)
            assert '192.168.100.10' in machine.get('ip_addresses', [])

    @pytest.mark.integration
    @pytest.mark.maas
    def test_storage_configuration_integration(self, maas_test_config, test_machine_spec, mock_maas_client):
        """Test storage layout configuration during deployment."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        machine_id = 'test-storage-config'
        storage_config = {
            'layout': 'lvm',
            'root_device': '/dev/sda',
            'root_size': '50G',
            'swap_size': '4G',
            'boot_size': '2G'
        }
        
        mock_maas_client.configure_storage.return_value = {
            'configured': True,
            'layout': 'lvm'
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_test_config)
            
            client = MaasAPIClient()
            result = client.configure_storage(machine_id, storage_config)
            
            assert result['configured'] == True
            assert result['layout'] == 'lvm'

    @pytest.mark.integration
    @pytest.mark.maas
    def test_power_management_integration(self, maas_test_config, test_machine_spec, mock_maas_client):
        """Test power management operations integration."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        machine_id = 'test-power-management'
        
        # Mock power operation responses
        mock_maas_client.power_on_machine.return_value = {'power_state': 'on'}
        mock_maas_client.power_off_machine.return_value = {'power_state': 'off'}
        mock_maas_client.power_cycle_machine.return_value = {'power_state': 'cycling'}
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_test_config)
            
            client = MaasAPIClient()
            
            # Test power on
            result = client.power_on_machine(machine_id)
            assert result['power_state'] == 'on'
            
            # Test power off
            result = client.power_off_machine(machine_id)
            assert result['power_state'] == 'off'
            
            # Test power cycle
            result = client.power_cycle_machine(machine_id)
            assert result['power_state'] == 'cycling'

    @pytest.mark.integration
    @pytest.mark.maas
    def test_webhook_integration(self, maas_test_config, test_machine_spec):
        """Test MaaS webhook integration for real-time updates."""
        from gough.containers.management_server.py4web_app.controllers.webhooks import MaasWebhookHandler
        
        webhook_handler = MaasWebhookHandler()
        
        # Test different webhook events
        webhook_events = [
            {
                'type': 'machine',
                'action': 'status_change',
                'system_id': 'test-webhook-machine',
                'old_status': 'Commissioning',
                'new_status': 'Ready',
                'timestamp': datetime.utcnow().isoformat()
            },
            {
                'type': 'machine',
                'action': 'power_state_change',
                'system_id': 'test-webhook-machine',
                'old_power_state': 'off',
                'new_power_state': 'on',
                'timestamp': datetime.utcnow().isoformat()
            },
            {
                'type': 'machine',
                'action': 'deployment_complete',
                'system_id': 'test-webhook-machine',
                'deployment_result': 'success',
                'timestamp': datetime.utcnow().isoformat()
            }
        ]
        
        for event in webhook_events:
            result = webhook_handler.process_webhook(event)
            
            assert result['processed'] == True
            assert result['event_type'] == event['action']

    @pytest.mark.integration
    @pytest.mark.maas
    def test_bulk_machine_operations(self, maas_test_config, mock_maas_client):
        """Test bulk operations on multiple machines."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        
        machine_ids = [f'bulk-test-machine-{i:02d}' for i in range(5)]
        
        # Mock bulk operation responses
        mock_maas_client.commission_machines.return_value = {
            'operation_id': 'bulk-commission-001',
            'machines': machine_ids,
            'status': 'started'
        }
        
        mock_maas_client.get_bulk_operation_status.return_value = {
            'operation_id': 'bulk-commission-001',
            'status': 'completed',
            'successful_machines': machine_ids,
            'failed_machines': []
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_test_config)
            
            client = MaasAPIClient()
            
            # Start bulk commissioning
            result = client.commission_machines(machine_ids)
            
            assert result['status'] == 'started'
            assert len(result['machines']) == 5
            
            # Check bulk operation status
            status = client.get_bulk_operation_status('bulk-commission-001')
            assert status['status'] == 'completed'
            assert len(status['successful_machines']) == 5

    @pytest.mark.integration
    @pytest.mark.maas
    def test_error_handling_and_recovery(self, maas_test_config, mock_maas_client):
        """Test error handling and recovery in MaaS operations."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient, MaasAPIError
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import handle_maas_error
        
        machine_id = 'test-error-handling'
        
        # Test various error scenarios
        error_scenarios = [
            {'error': 'Machine not found', 'status_code': 404},
            {'error': 'Machine not ready for deployment', 'status_code': 409},
            {'error': 'Power control failed', 'status_code': 503},
            {'error': 'Network timeout', 'status_code': 408}
        ]
        
        for scenario in error_scenarios:
            # Mock API error
            mock_response = Mock()
            mock_response.status_code = scenario['status_code']
            mock_response.json.return_value = {'error': scenario['error']}
            mock_maas_client.deploy_machine.side_effect = MaasAPIError(scenario['error'])
            
            with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
                mock_config.return_value = Mock(**maas_test_config)
                
                # Test error handling
                result = handle_maas_error(machine_id, scenario['error'])
                
                assert result['error_handled'] == True
                assert result['recovery_action'] in ['retry', 'fail', 'manual_intervention']

    @pytest.mark.integration
    @pytest.mark.maas
    @pytest.mark.slow
    def test_complete_provisioning_lifecycle(self, maas_test_config, test_machine_spec, mock_maas_client, sample_cloud_init_template):
        """Test complete machine provisioning lifecycle."""
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import ProvisioningOrchestrator
        
        orchestrator = ProvisioningOrchestrator()
        machine_id = 'test-lifecycle-machine'
        
        # Mock the complete lifecycle
        lifecycle_states = [
            {'status_name': 'New', 'step': 'enlisted'},
            {'status_name': 'Commissioning', 'step': 'commissioning'},
            {'status_name': 'Testing', 'step': 'testing'},
            {'status_name': 'Ready', 'step': 'ready'},
            {'status_name': 'Deploying', 'step': 'deploying'},
            {'status_name': 'Deployed', 'step': 'deployed'}
        ]
        
        mock_maas_client.get_machine.side_effect = [
            {**test_machine_spec, 'system_id': machine_id, **state}
            for state in lifecycle_states
        ]
        
        provisioning_spec = {
            'machine_id': machine_id,
            'hostname': test_machine_spec['hostname'],
            'os_series': 'jammy',
            'cloud_init_template': sample_cloud_init_template['template_content'],
            'network_config': {
                'type': 'static',
                'ip': '192.168.100.20/24',
                'gateway': '192.168.100.1'
            },
            'storage_config': {
                'layout': 'lvm',
                'root_size': '50G'
            }
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_test_config)
            
            # Execute complete provisioning
            result = orchestrator.provision_machine(provisioning_spec)
            
            assert result['status'] == 'completed'
            assert result['machine_id'] == machine_id
            assert result['final_state'] == 'Deployed'
            assert 'provisioning_time' in result
            assert result['provisioning_time'] > 0

    @pytest.mark.integration
    @pytest.mark.maas
    def test_concurrent_provisioning(self, maas_test_config, mock_maas_client):
        """Test concurrent provisioning of multiple machines."""
        import threading
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import provision_machine_async
        
        machine_count = 5
        machine_ids = [f'concurrent-machine-{i:02d}' for i in range(machine_count)]
        results = {}
        
        def provision_machine_thread(machine_id):
            spec = {
                'machine_id': machine_id,
                'hostname': f'host-{machine_id}',
                'os_series': 'jammy'
            }
            result = provision_machine_async(spec)
            results[machine_id] = result
        
        # Mock successful provisioning for all machines
        mock_maas_client.deploy_machine.return_value = {'status': 'deploying'}
        mock_maas_client.get_machine.return_value = {'status_name': 'Deployed'}
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_test_config)
            
            # Start concurrent provisioning
            threads = []
            for machine_id in machine_ids:
                thread = threading.Thread(target=provision_machine_thread, args=(machine_id,))
                threads.append(thread)
                thread.start()
            
            # Wait for all threads to complete
            for thread in threads:
                thread.join()
            
            # Verify all provisioning completed successfully
            assert len(results) == machine_count
            for machine_id, result in results.items():
                assert result['status'] in ['completed', 'success']

    @pytest.mark.integration
    @pytest.mark.maas
    def test_resource_constraints_handling(self, maas_test_config, mock_maas_client):
        """Test handling of resource constraints during provisioning."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import check_resource_availability
        
        # Test insufficient resources scenario
        mock_maas_client.get_machines.return_value = [
            {'system_id': 'low-mem-machine', 'memory': 2048, 'status_name': 'Ready'},  # Low memory
            {'system_id': 'low-cpu-machine', 'cpu_count': 1, 'status_name': 'Ready'},  # Low CPU
            {'system_id': 'suitable-machine', 'memory': 8192, 'cpu_count': 4, 'status_name': 'Ready'}
        ]
        
        resource_requirements = {
            'min_memory': 4096,
            'min_cpu_count': 2,
            'min_storage': 50
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config:
            mock_config.return_value = Mock(**maas_test_config)
            
            # Check resource availability
            result = check_resource_availability(resource_requirements)
            
            assert result['suitable_machines'] == 1
            assert result['insufficient_resources'] == 2
            assert 'suitable-machine' in result['available_machines']

    @pytest.mark.integration
    @pytest.mark.maas
    def test_maas_api_rate_limiting_integration(self, maas_test_config, mock_requests):
        """Test MaaS API rate limiting integration."""
        from gough.containers.management_server.py4web_app.lib.maas_api import MaasAPIClient, handle_rate_limiting
        
        # Mock rate limit response
        rate_limit_response = Mock()
        rate_limit_response.status_code = 429
        rate_limit_response.headers = {'Retry-After': '60', 'X-RateLimit-Remaining': '0'}
        
        # First request hits rate limit, second succeeds
        mock_requests['get'].side_effect = [
            rate_limit_response,
            Mock(status_code=200, json=lambda: [{'system_id': 'test'}])
        ]
        
        with patch('gough.containers.management_server.py4web_app.lib.maas_api.get_config') as mock_config, \
             patch('time.sleep') as mock_sleep:  # Speed up test
            
            mock_config.return_value = Mock(**maas_test_config)
            
            client = MaasAPIClient()
            result = client.get_machines()
            
            # Should eventually succeed after rate limit retry
            assert len(result) == 1
            mock_sleep.assert_called()  # Verify retry delay was applied