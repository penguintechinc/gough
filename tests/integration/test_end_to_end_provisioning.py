#!/usr/bin/env python3
"""
End-to-End Provisioning Integration Tests
Complete workflow testing from bare metal through monitoring deployment
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

import pytest


class TestEndToEndProvisioning:
    """End-to-end provisioning test cases."""

    @pytest.fixture
    def e2e_test_config(self):
        """End-to-end test configuration."""
        return {
            'TEST_ENVIRONMENT': 'integration',
            'MAAS_URL': 'http://test-maas:5240/MAAS',
            'FLEET_URL': 'http://test-fleet:8080',
            'MANAGEMENT_SERVER_URL': 'http://test-mgmt:8000',
            'DATABASE_URL': 'postgresql://test:test@test-db:5432/gough_test',
            'REDIS_URL': 'redis://test-redis:6379/1',
            'PROVISIONING_TIMEOUT': 3600,  # 1 hour
            'MONITORING_SETUP_TIMEOUT': 300  # 5 minutes
        }

    @pytest.fixture
    def test_server_specification(self):
        """Complete server specification for testing."""
        return {
            'hostname': 'e2e-test-server-01',
            'mac_address': '00:16:3e:e2:e1:01',
            'power_type': 'virsh',
            'power_parameters': {
                'power_address': 'qemu+ssh://admin@hypervisor/system',
                'power_id': 'e2e-test-vm-01'
            },
            'architecture': 'amd64',
            'memory': 8192,
            'cpu_count': 4,
            'storage': 100,
            'zone': 'test-zone',
            'pool': 'test-pool',
            'network_config': {
                'interface': 'eth0',
                'ip_address': '192.168.100.101',
                'subnet': '192.168.100.0/24',
                'gateway': '192.168.100.1',
                'dns': ['8.8.8.8', '8.8.4.4']
            },
            'storage_config': {
                'layout': 'lvm',
                'root_size': '70G',
                'swap_size': '8G',
                'var_size': '20G'
            },
            'os_config': {
                'series': 'jammy',
                'kernel': 'generic'
            },
            'role': 'monitoring-server',
            'environment': 'test'
        }

    @pytest.fixture
    def deployment_specification(self):
        """Complete deployment specification."""
        return {
            'cloud_init_template': '''#cloud-config
hostname: e2e-test-server-01
fqdn: e2e-test-server-01.test.local

users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    groups: [sudo, docker]
    ssh_authorized_keys:
      - ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ... test@example.com

packages:
  - curl
  - wget
  - git
  - htop
  - docker.io
  - prometheus
  - node-exporter

package_update: true
package_upgrade: true

write_files:
  - path: /etc/prometheus/prometheus.yml
    content: |
      global:
        scrape_interval: 15s
      scrape_configs:
        - job_name: 'node'
          static_configs:
            - targets: ['localhost:9100']
    permissions: '0644'
    owner: prometheus:prometheus
  
  - path: /opt/gough/agent/config.yml
    content: |
      agent:
        id: e2e-test-server-01
        management_server: http://test-mgmt:8000
        heartbeat_interval: 30
        log_level: info
      monitoring:
        osquery_enabled: true
        prometheus_enabled: true
    permissions: '0644'

runcmd:
  - systemctl enable docker
  - systemctl start docker
  - systemctl enable prometheus
  - systemctl start prometheus
  - systemctl enable prometheus-node-exporter
  - systemctl start prometheus-node-exporter
  - curl -L https://github.com/gough-project/agent/releases/latest/download/gough-agent -o /opt/gough/agent/gough-agent
  - chmod +x /opt/gough/agent/gough-agent
  - systemctl enable gough-agent
  - systemctl start gough-agent

final_message: "E2E test server provisioning completed"
''',
            'package_config': {
                'name': 'monitoring-server-packages',
                'packages': [
                    'prometheus',
                    'prometheus-node-exporter',
                    'grafana',
                    'docker.io',
                    'docker-compose',
                    'osquery'
                ],
                'repositories': [
                    'deb [arch=amd64] https://download.docker.com/linux/ubuntu jammy stable'
                ],
                'post_install_scripts': '''
#!/bin/bash
# Configure monitoring stack
systemctl enable prometheus
systemctl enable grafana-server
systemctl enable prometheus-node-exporter

# Setup Gough agent
mkdir -p /opt/gough/agent
chown ubuntu:ubuntu /opt/gough/agent

# Configure OSQuery
mkdir -p /etc/osquery
chown osquery:osquery /etc/osquery
'''
            }
        }

    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_complete_provisioning_workflow(self, e2e_test_config, test_server_specification, 
                                                 deployment_specification, mock_database,
                                                 mock_maas_client, mock_fleet_client, mock_ansible_runner):
        """Test complete end-to-end provisioning workflow."""
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import E2EProvisioningOrchestrator
        
        orchestrator = E2EProvisioningOrchestrator()
        
        # Create complete provisioning request
        provisioning_request = {
            'server_spec': test_server_specification,
            'deployment_spec': deployment_specification,
            'monitoring_config': {
                'osquery_enrollment': True,
                'prometheus_monitoring': True,
                'log_aggregation': True
            },
            'validation_checks': {
                'connectivity_test': True,
                'service_health_check': True,
                'monitoring_verification': True
            }
        }
        
        # Mock the complete workflow steps
        workflow_steps = [
            {'step': 'machine_discovery', 'status': 'completed', 'duration': 10},
            {'step': 'machine_commissioning', 'status': 'completed', 'duration': 300},
            {'step': 'network_configuration', 'status': 'completed', 'duration': 30},
            {'step': 'storage_configuration', 'status': 'completed', 'duration': 45},
            {'step': 'os_deployment', 'status': 'completed', 'duration': 600},
            {'step': 'package_installation', 'status': 'completed', 'duration': 180},
            {'step': 'service_configuration', 'status': 'completed', 'duration': 120},
            {'step': 'monitoring_setup', 'status': 'completed', 'duration': 60},
            {'step': 'validation_tests', 'status': 'completed', 'duration': 90}
        ]
        
        # Mock successful execution of each step
        with patch.multiple(orchestrator,
                           discover_machine=Mock(return_value={'status': 'success', 'machine_id': 'test-machine-01'}),
                           commission_machine=Mock(return_value={'status': 'success', 'commission_time': 300}),
                           configure_network=Mock(return_value={'status': 'success', 'ip_assigned': '192.168.100.101'}),
                           configure_storage=Mock(return_value={'status': 'success', 'layout': 'lvm'}),
                           deploy_os=Mock(return_value={'status': 'success', 'deployment_time': 600}),
                           install_packages=Mock(return_value={'status': 'success', 'packages_installed': 15}),
                           configure_services=Mock(return_value={'status': 'success', 'services_configured': 8}),
                           setup_monitoring=Mock(return_value={'status': 'success', 'agents_installed': 3}),
                           validate_deployment=Mock(return_value={'status': 'success', 'validation_score': 100})):
            
            # Execute complete provisioning workflow
            result = await orchestrator.execute_provisioning(provisioning_request)
            
            # Verify successful completion
            assert result['status'] == 'completed'
            assert result['provisioning_id'] is not None
            assert result['machine_id'] == 'test-machine-01'
            assert result['total_duration'] > 0
            assert result['validation_score'] == 100
            
            # Verify all workflow steps completed
            assert len(result['workflow_steps']) == len(workflow_steps)
            for step in result['workflow_steps']:
                assert step['status'] == 'completed'

    @pytest.mark.e2e
    @pytest.mark.slow
    def test_agent_deployment_and_enrollment(self, e2e_test_config, test_server_specification,
                                           mock_fleet_client, mock_database):
        """Test agent deployment and FleetDM enrollment."""
        from gough.containers.management_server.py4web_app.lib.tasks.monitoring import deploy_gough_agent, enroll_with_fleet
        
        machine_id = 'test-agent-machine'
        server_hostname = test_server_specification['hostname']
        
        # Mock agent deployment
        mock_agent_deployment = {
            'status': 'success',
            'agent_id': f'gough-agent-{machine_id}',
            'installation_path': '/opt/gough/agent',
            'config_file': '/opt/gough/agent/config.yml',
            'service_status': 'active'
        }
        
        # Mock FleetDM enrollment
        mock_enrollment = {
            'host_id': 12345,
            'enrollment_status': 'success',
            'osquery_version': '5.10.2',
            'enrollment_time': datetime.utcnow().isoformat()
        }
        
        mock_fleet_client.enroll_host.return_value = mock_enrollment
        
        with patch('gough.containers.management_server.py4web_app.lib.tasks.monitoring.deploy_agent') as mock_deploy, \
             patch('gough.containers.management_server.py4web_app.lib.tasks.monitoring.configure_osquery') as mock_osquery:
            
            mock_deploy.return_value = mock_agent_deployment
            mock_osquery.return_value = {'status': 'configured', 'config_applied': True}
            
            # Deploy agent
            agent_result = deploy_gough_agent(machine_id, server_hostname)
            assert agent_result['status'] == 'success'
            assert agent_result['agent_id'] == f'gough-agent-{machine_id}'
            
            # Enroll with FleetDM
            enrollment_result = enroll_with_fleet(server_hostname, agent_result['agent_id'])
            assert enrollment_result['enrollment_status'] == 'success'
            assert enrollment_result['host_id'] == 12345

    @pytest.mark.e2e
    def test_monitoring_stack_deployment(self, test_server_specification, mock_ansible_runner):
        """Test deployment of complete monitoring stack."""
        from gough.containers.management_server.py4web_app.lib.tasks.monitoring import deploy_monitoring_stack
        
        monitoring_config = {
            'prometheus': {
                'enabled': True,
                'scrape_interval': '15s',
                'retention': '30d',
                'storage_path': '/var/lib/prometheus'
            },
            'grafana': {
                'enabled': True,
                'admin_password': 'secure_password',
                'dashboard_provisioning': True
            },
            'node_exporter': {
                'enabled': True,
                'collectors': ['cpu', 'memory', 'disk', 'network']
            },
            'osquery': {
                'enabled': True,
                'config_refresh_interval': 300,
                'log_results': True
            }
        }
        
        # Mock successful Ansible playbook execution
        mock_ansible_runner.run.return_value = Mock(
            status='successful',
            rc=0,
            stdout='PLAY RECAP: test-server : ok=25 changed=15 unreachable=0 failed=0',
            stats={
                test_server_specification['hostname']: {
                    'ok': 25,
                    'changed': 15,
                    'unreachable': 0,
                    'failed': 0
                }
            }
        )
        
        result = deploy_monitoring_stack(test_server_specification['hostname'], monitoring_config)
        
        assert result['status'] == 'success'
        assert result['components_deployed'] == 4  # prometheus, grafana, node_exporter, osquery
        assert result['tasks_successful'] == 25
        assert result['tasks_changed'] == 15

    @pytest.mark.e2e
    def test_network_connectivity_validation(self, test_server_specification, e2e_test_config):
        """Test network connectivity validation after deployment."""
        from gough.containers.management_server.py4web_app.lib.validation.connectivity import validate_network_connectivity
        
        server_ip = test_server_specification['network_config']['ip_address']
        connectivity_tests = [
            {'type': 'ping', 'target': server_ip, 'timeout': 5},
            {'type': 'ssh', 'target': server_ip, 'port': 22, 'timeout': 10},
            {'type': 'http', 'target': f'http://{server_ip}:9090', 'service': 'prometheus', 'timeout': 15},
            {'type': 'http', 'target': f'http://{server_ip}:3000', 'service': 'grafana', 'timeout': 15},
            {'type': 'http', 'target': f'http://{server_ip}:9100/metrics', 'service': 'node_exporter', 'timeout': 10}
        ]
        
        with patch('subprocess.run') as mock_subprocess, \
             patch('requests.get') as mock_requests:
            
            # Mock successful ping
            mock_subprocess.return_value = Mock(returncode=0, stdout='PING successful')
            
            # Mock successful HTTP responses
            mock_requests.return_value = Mock(status_code=200, text='Service healthy')
            
            results = []
            for test in connectivity_tests:
                result = validate_network_connectivity(test)
                results.append(result)
            
            # All connectivity tests should pass
            for result in results:
                assert result['status'] == 'success'
                assert result['response_time'] > 0

    @pytest.mark.e2e
    def test_service_health_validation(self, test_server_specification):
        """Test service health validation after deployment."""
        from gough.containers.management_server.py4web_app.lib.validation.health import validate_service_health
        
        services_to_check = [
            {'name': 'ssh', 'type': 'systemd', 'expected_status': 'active'},
            {'name': 'docker', 'type': 'systemd', 'expected_status': 'active'},
            {'name': 'prometheus', 'type': 'systemd', 'expected_status': 'active'},
            {'name': 'grafana-server', 'type': 'systemd', 'expected_status': 'active'},
            {'name': 'prometheus-node-exporter', 'type': 'systemd', 'expected_status': 'active'},
            {'name': 'osqueryd', 'type': 'systemd', 'expected_status': 'active'},
            {'name': 'gough-agent', 'type': 'systemd', 'expected_status': 'active'}
        ]
        
        server_hostname = test_server_specification['hostname']
        
        with patch('paramiko.SSHClient') as mock_ssh:
            # Mock SSH connection and command execution
            mock_client = Mock()
            mock_ssh.return_value = mock_client
            
            # Mock systemctl status commands
            mock_client.exec_command.return_value = (
                Mock(),  # stdin
                Mock(read=lambda: b'active (running)'),  # stdout
                Mock(read=lambda: b'')  # stderr
            )
            
            health_results = []
            for service in services_to_check:
                result = validate_service_health(server_hostname, service)
                health_results.append(result)
            
            # All services should be healthy
            for result in health_results:
                assert result['status'] == 'healthy'
                assert result['service_status'] == 'active'

    @pytest.mark.e2e
    def test_osquery_data_collection(self, test_server_specification, mock_fleet_client):
        """Test OSQuery data collection and FleetDM integration."""
        from gough.containers.management_server.py4web_app.lib.monitoring.osquery import execute_osquery_queries
        
        test_queries = [
            {
                'name': 'system_info',
                'query': 'SELECT hostname, cpu_brand, physical_memory FROM system_info;',
                'expected_results': 1
            },
            {
                'name': 'processes',
                'query': 'SELECT name, pid, state FROM processes WHERE state = "R";',
                'expected_results': 5
            },
            {
                'name': 'network_interfaces',
                'query': 'SELECT interface, address FROM interface_addresses WHERE interface != "lo";',
                'expected_results': 1
            }
        ]
        
        # Mock FleetDM query execution results
        mock_query_results = {
            'system_info': [
                {
                    'hostname': test_server_specification['hostname'],
                    'cpu_brand': 'Intel Core i7',
                    'physical_memory': '8589934592'
                }
            ],
            'processes': [
                {'name': 'systemd', 'pid': '1', 'state': 'R'},
                {'name': 'kthreadd', 'pid': '2', 'state': 'R'},
                {'name': 'prometheus', 'pid': '1234', 'state': 'R'},
                {'name': 'grafana-server', 'pid': '1235', 'state': 'R'},
                {'name': 'osqueryd', 'pid': '1236', 'state': 'R'}
            ],
            'network_interfaces': [
                {
                    'interface': 'eth0',
                    'address': test_server_specification['network_config']['ip_address']
                }
            ]
        }
        
        mock_fleet_client.run_query.return_value = {'campaign_id': 'test-campaign-123'}
        mock_fleet_client.get_query_results.side_effect = lambda query_name: mock_query_results.get(query_name, [])
        
        results = execute_osquery_queries(test_server_specification['hostname'], test_queries)
        
        assert len(results) == len(test_queries)
        for i, result in enumerate(results):
            query = test_queries[i]
            assert result['query_name'] == query['name']
            assert len(result['results']) >= query['expected_results']

    @pytest.mark.e2e
    def test_log_aggregation_setup(self, test_server_specification):
        """Test log aggregation setup and configuration."""
        from gough.containers.management_server.py4web_app.lib.monitoring.logs import setup_log_aggregation
        
        log_config = {
            'sources': [
                '/var/log/syslog',
                '/var/log/auth.log',
                '/var/log/prometheus/prometheus.log',
                '/var/log/grafana/grafana.log'
            ],
            'destinations': [
                {
                    'type': 'elasticsearch',
                    'endpoint': 'http://test-elastic:9200',
                    'index_pattern': 'gough-logs-{date}'
                },
                {
                    'type': 'file',
                    'path': '/var/log/gough/aggregated.log',
                    'rotation': 'daily'
                }
            ],
            'filters': [
                {'field': 'level', 'exclude': ['DEBUG']},
                {'field': 'source', 'include': ['prometheus', 'grafana', 'gough-agent']}
            ]
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.monitoring.logs.configure_filebeat') as mock_filebeat, \
             patch('gough.containers.management_server.py4web_app.lib.monitoring.logs.configure_logrotate') as mock_logrotate:
            
            mock_filebeat.return_value = {'status': 'configured', 'sources_configured': 4}
            mock_logrotate.return_value = {'status': 'configured', 'rotation_jobs': 2}
            
            result = setup_log_aggregation(test_server_specification['hostname'], log_config)
            
            assert result['status'] == 'success'
            assert result['log_sources'] == 4
            assert result['destinations'] == 2
            assert result['filebeat_configured'] == True

    @pytest.mark.e2e
    @pytest.mark.slow
    def test_deployment_rollback_capability(self, test_server_specification, mock_database, mock_maas_client):
        """Test deployment rollback capability."""
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import rollback_deployment
        
        # Create a completed deployment job
        deployment_data = {
            'job_id': 'e2e-rollback-test',
            'server_id': 1,
            'status': 'Completed',
            'ansible_playbook': 'monitoring-server.yml',
            'started_on': datetime.utcnow() - timedelta(hours=1),
            'completed_on': datetime.utcnow() - timedelta(minutes=30)
        }
        
        job_id = mock_database.deployment_jobs.insert(**deployment_data)
        mock_database.commit()
        
        # Mock rollback operations
        rollback_steps = [
            {'step': 'stop_services', 'status': 'success'},
            {'step': 'remove_packages', 'status': 'success'},
            {'step': 'restore_configuration', 'status': 'success'},
            {'step': 'release_machine', 'status': 'success'}
        ]
        
        mock_maas_client.release_machine.return_value = {'status': 'releasing'}
        
        with patch('gough.containers.management_server.py4web_app.lib.tasks.deployment.execute_rollback_steps') as mock_rollback:
            mock_rollback.return_value = {
                'status': 'success',
                'steps_completed': rollback_steps,
                'rollback_duration': 180
            }
            
            result = rollback_deployment('e2e-rollback-test')
            
            assert result['status'] == 'success'
            assert len(result['steps_completed']) == 4
            assert result['rollback_duration'] > 0

    @pytest.mark.e2e
    def test_multi_environment_deployment(self, e2e_test_config):
        """Test deployment across multiple environments."""
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import multi_environment_deploy
        
        environments = ['development', 'staging', 'production']
        deployment_configs = {
            'development': {
                'replicas': 1,
                'resources': 'minimal',
                'monitoring': 'basic'
            },
            'staging': {
                'replicas': 2,
                'resources': 'standard',
                'monitoring': 'enhanced'
            },
            'production': {
                'replicas': 3,
                'resources': 'high',
                'monitoring': 'comprehensive'
            }
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.tasks.deployment.deploy_to_environment') as mock_deploy:
            mock_deploy.return_value = {'status': 'success', 'deployment_id': 'test-deploy-123'}
            
            results = multi_environment_deploy(deployment_configs)
            
            assert len(results) == 3
            for env, result in results.items():
                assert result['status'] == 'success'
                assert 'deployment_id' in result

    @pytest.mark.e2e
    def test_disaster_recovery_procedures(self, test_server_specification, mock_database):
        """Test disaster recovery procedures and data backup."""
        from gough.containers.management_server.py4web_app.lib.backup.disaster_recovery import execute_backup, test_recovery
        
        backup_config = {
            'databases': ['postgresql://test-db:5432/gough'],
            'configuration_files': ['/etc/gough/', '/opt/gough/config/'],
            'monitoring_data': ['/var/lib/prometheus/', '/var/lib/grafana/'],
            'backup_destination': 's3://gough-backups/test/',
            'encryption': True,
            'compression': True
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.backup.disaster_recovery.create_backup') as mock_backup, \
             patch('gough.containers.management_server.py4web_app.lib.backup.disaster_recovery.verify_backup') as mock_verify:
            
            mock_backup.return_value = {
                'backup_id': 'backup-e2e-test',
                'backup_size': 1024000000,  # 1GB
                'files_backed_up': 15000,
                'duration': 300
            }
            
            mock_verify.return_value = {
                'verification_status': 'success',
                'integrity_check': 'passed',
                'restoration_test': 'success'
            }
            
            # Execute backup
            backup_result = execute_backup(backup_config)
            assert backup_result['backup_id'] is not None
            assert backup_result['backup_size'] > 0
            
            # Test recovery
            recovery_result = test_recovery(backup_result['backup_id'])
            assert recovery_result['verification_status'] == 'success'

    @pytest.mark.e2e
    @pytest.mark.performance
    def test_performance_benchmarking(self, test_server_specification):
        """Test performance benchmarking of deployed services."""
        from gough.containers.management_server.py4web_app.lib.validation.performance import run_performance_benchmarks
        
        benchmark_config = {
            'cpu_benchmark': {
                'duration': 60,
                'threads': 4,
                'target_score': 1000
            },
            'memory_benchmark': {
                'allocation_size': '1G',
                'iterations': 100,
                'target_latency': 10  # ms
            },
            'disk_benchmark': {
                'test_file_size': '1G',
                'io_pattern': 'random',
                'target_iops': 1000
            },
            'network_benchmark': {
                'bandwidth_test': True,
                'latency_test': True,
                'target_bandwidth': 1000  # Mbps
            }
        }
        
        with patch('gough.containers.management_server.py4web_app.lib.validation.performance.run_cpu_benchmark') as mock_cpu, \
             patch('gough.containers.management_server.py4web_app.lib.validation.performance.run_memory_benchmark') as mock_memory, \
             patch('gough.containers.management_server.py4web_app.lib.validation.performance.run_disk_benchmark') as mock_disk, \
             patch('gough.containers.management_server.py4web_app.lib.validation.performance.run_network_benchmark') as mock_network:
            
            mock_cpu.return_value = {'score': 1200, 'result': 'pass'}
            mock_memory.return_value = {'latency_ms': 8, 'result': 'pass'}
            mock_disk.return_value = {'iops': 1500, 'result': 'pass'}
            mock_network.return_value = {'bandwidth_mbps': 1200, 'latency_ms': 2, 'result': 'pass'}
            
            results = run_performance_benchmarks(test_server_specification['hostname'], benchmark_config)
            
            assert results['cpu_benchmark']['result'] == 'pass'
            assert results['memory_benchmark']['result'] == 'pass'
            assert results['disk_benchmark']['result'] == 'pass'
            assert results['network_benchmark']['result'] == 'pass'
            assert results['overall_result'] == 'pass'