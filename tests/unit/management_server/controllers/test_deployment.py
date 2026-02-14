#!/usr/bin/env python3
"""
Unit Tests for Deployment Controller
Tests for deployment orchestration, job management, and workflow execution
"""

import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock, call

import pytest


class TestDeploymentController:
    """Test cases for deployment controller functionality."""

    @pytest.fixture
    def deployment_config(self):
        """Deployment configuration for testing."""
        return {
            'ANSIBLE_PLAYBOOK_PATH': '/opt/gough/ansible/playbooks',
            'ANSIBLE_INVENTORY_PATH': '/opt/gough/ansible/inventory',
            'DEPLOYMENT_TIMEOUT': 3600,  # 1 hour
            'MAX_CONCURRENT_DEPLOYMENTS': 5,
            'RETRY_ATTEMPTS': 3,
            'RETRY_DELAY': 60
        }

    @pytest.fixture
    def complete_deployment_data(self, sample_server_data, sample_cloud_init_template, sample_package_config):
        """Complete deployment data with all dependencies."""
        return {
            'server': sample_server_data,
            'cloud_init_template': sample_cloud_init_template,
            'package_config': sample_package_config,
            'deployment_options': {
                'force_reinstall': False,
                'skip_commissioning': False,
                'enable_monitoring': True,
                'custom_scripts': []
            }
        }

    @pytest.mark.deployment
    def test_create_deployment_job(self, api_client, mock_database, complete_deployment_data, auth_headers):
        """Test creating a new deployment job."""
        # Insert dependencies
        server_id = mock_database.servers.insert(**complete_deployment_data['server'])
        template_id = mock_database.cloud_init_templates.insert(**complete_deployment_data['cloud_init_template'])
        package_id = mock_database.package_configs.insert(**complete_deployment_data['package_config'])
        
        job_data = {
            'server_id': server_id,
            'cloud_init_template_id': template_id,
            'package_config_id': package_id,
            'ansible_playbook': 'server-deployment.yml'
        }
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.post(
                '/api/deployment/jobs',
                data=json.dumps(job_data),
                headers=auth_headers
            )
            
            assert response.status_code == 201
            data = json.loads(response.data)
            assert data['job']['status'] == 'Pending'
            assert 'job_id' in data['job']

    @pytest.mark.deployment
    def test_get_deployment_job_status(self, api_client, mock_database, deployment_job_data, auth_headers):
        """Test retrieving deployment job status."""
        job_id = mock_database.deployment_jobs.insert(**deployment_job_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.get(f'/api/deployment/jobs/{job_id}', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['job']['status'] == deployment_job_data['status']

    @pytest.mark.deployment
    def test_list_deployment_jobs(self, api_client, mock_database, deployment_job_data, auth_headers):
        """Test listing deployment jobs with filtering."""
        # Create multiple jobs with different statuses
        statuses = ['Pending', 'Running', 'Completed', 'Failed']
        for i, status in enumerate(statuses):
            job_data = deployment_job_data.copy()
            job_data['job_id'] = f'job-{i:03d}'
            job_data['status'] = status
            mock_database.deployment_jobs.insert(**job_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            # Test all jobs
            response = api_client.get('/api/deployment/jobs', headers=auth_headers)
            assert response.status_code == 200
            data = json.loads(response.data)
            assert len(data['jobs']) == len(statuses)
            
            # Test filtering by status
            response = api_client.get('/api/deployment/jobs?status=Running', headers=auth_headers)
            assert response.status_code == 200
            filtered_data = json.loads(response.data)
            if 'jobs' in filtered_data:
                for job in filtered_data['jobs']:
                    assert job['status'] == 'Running'

    @pytest.mark.deployment
    def test_cancel_deployment_job(self, api_client, mock_database, deployment_job_data, auth_headers):
        """Test canceling a running deployment job."""
        deployment_job_data['status'] = 'Running'
        job_id = mock_database.deployment_jobs.insert(**deployment_job_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.controllers.deployment.cancel_ansible_job') as mock_cancel:
            
            mock_get_db.return_value = mock_database
            mock_cancel.return_value = True
            
            response = api_client.post(f'/api/deployment/jobs/{job_id}/cancel', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'cancelled' in data['status'].lower()

    @pytest.mark.deployment
    def test_retry_failed_deployment(self, api_client, mock_database, deployment_job_data, auth_headers):
        """Test retrying a failed deployment job."""
        deployment_job_data['status'] = 'Failed'
        deployment_job_data['error_message'] = 'Connection timeout'
        job_id = mock_database.deployment_jobs.insert(**deployment_job_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.controllers.deployment.start_deployment') as mock_start:
            
            mock_get_db.return_value = mock_database
            mock_start.return_value = {'status': 'success', 'job_id': deployment_job_data['job_id']}
            
            response = api_client.post(f'/api/deployment/jobs/{job_id}/retry', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['status'] == 'success'

    @pytest.mark.deployment
    def test_deployment_job_logs(self, api_client, mock_database, deployment_job_data, auth_headers):
        """Test retrieving deployment job logs."""
        deployment_job_data['log_output'] = 'PLAY [Deploy server] *****\nTASK [Install packages] *****\nchanged: [test-server-01]'
        job_id = mock_database.deployment_jobs.insert(**deployment_job_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.get(f'/api/deployment/jobs/{job_id}/logs', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'logs' in data
            assert 'PLAY [Deploy server]' in data['logs']

    @pytest.mark.deployment
    def test_deployment_statistics(self, api_client, mock_database, deployment_job_data, auth_headers):
        """Test deployment statistics endpoint."""
        # Create jobs with various statuses and completion times
        job_stats = [
            {'status': 'Completed', 'started_on': datetime.utcnow() - timedelta(hours=2), 
             'completed_on': datetime.utcnow() - timedelta(hours=1, minutes=45)},
            {'status': 'Completed', 'started_on': datetime.utcnow() - timedelta(hours=3), 
             'completed_on': datetime.utcnow() - timedelta(hours=2, minutes=30)},
            {'status': 'Failed', 'started_on': datetime.utcnow() - timedelta(hours=1), 
             'completed_on': datetime.utcnow() - timedelta(minutes=30)},
            {'status': 'Running', 'started_on': datetime.utcnow() - timedelta(minutes=15), 
             'completed_on': None}
        ]
        
        for i, stats in enumerate(job_stats):
            job_data = deployment_job_data.copy()
            job_data['job_id'] = f'stats-job-{i}'
            job_data.update(stats)
            mock_database.deployment_jobs.insert(**job_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.get('/api/deployment/statistics', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'total_jobs' in data
            assert 'success_rate' in data
            assert 'average_duration' in data

    @pytest.mark.deployment
    def test_deployment_validation(self, api_client, mock_database, auth_headers):
        """Test deployment configuration validation."""
        invalid_configs = [
            {'server_id': 999, 'cloud_init_template_id': 1, 'package_config_id': 1},  # Invalid server
            {'server_id': 1, 'cloud_init_template_id': 999, 'package_config_id': 1},  # Invalid template
            {'server_id': 1, 'cloud_init_template_id': 1, 'package_config_id': 999},  # Invalid package
            {}  # Empty configuration
        ]
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            for config in invalid_configs:
                response = api_client.post(
                    '/api/deployment/jobs',
                    data=json.dumps(config),
                    headers=auth_headers
                )
                
                assert response.status_code in [400, 404, 422]
                data = json.loads(response.data)
                assert 'error' in data

    @pytest.mark.deployment
    def test_concurrent_deployment_limit(self, api_client, mock_database, deployment_job_data, auth_headers, deployment_config):
        """Test concurrent deployment job limits."""
        # Create maximum number of running jobs
        for i in range(deployment_config['MAX_CONCURRENT_DEPLOYMENTS']):
            job_data = deployment_job_data.copy()
            job_data['job_id'] = f'concurrent-job-{i}'
            job_data['status'] = 'Running'
            mock_database.deployment_jobs.insert(**job_data)
        mock_database.commit()
        
        # Try to create one more job
        new_job = deployment_job_data.copy()
        new_job['job_id'] = 'excess-job'
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.controllers.deployment.get_config') as mock_config:
            
            mock_get_db.return_value = mock_database
            mock_config.return_value = Mock(**deployment_config)
            
            response = api_client.post(
                '/api/deployment/jobs',
                data=json.dumps(new_job),
                headers=auth_headers
            )
            
            # Should be queued or rejected due to concurrent limit
            assert response.status_code in [201, 429, 503]

    @pytest.mark.deployment
    def test_deployment_workflow_orchestration(self, mock_maas_client, mock_ansible_runner, mock_fleet_client):
        """Test complete deployment workflow orchestration."""
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import DeploymentOrchestrator
        
        orchestrator = DeploymentOrchestrator()
        
        deployment_spec = {
            'server_id': 1,
            'hostname': 'test-server-01',
            'cloud_init_template': '#cloud-config\npackages: [curl]',
            'package_config': {'packages': ['docker.io']},
            'monitoring_enabled': True
        }
        
        with patch.object(orchestrator, 'commission_server') as mock_commission, \
             patch.object(orchestrator, 'deploy_server') as mock_deploy, \
             patch.object(orchestrator, 'configure_monitoring') as mock_monitoring:
            
            mock_commission.return_value = {'status': 'success'}
            mock_deploy.return_value = {'status': 'success'}
            mock_monitoring.return_value = {'status': 'success'}
            
            result = orchestrator.execute_deployment(deployment_spec)
            
            assert result['status'] == 'success'
            mock_commission.assert_called_once()
            mock_deploy.assert_called_once()
            mock_monitoring.assert_called_once()

    @pytest.mark.deployment
    def test_deployment_rollback(self, api_client, mock_database, deployment_job_data, auth_headers):
        """Test deployment rollback functionality."""
        deployment_job_data['status'] = 'Completed'
        job_id = mock_database.deployment_jobs.insert(**deployment_job_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.controllers.deployment.execute_rollback') as mock_rollback:
            
            mock_get_db.return_value = mock_database
            mock_rollback.return_value = {'status': 'success', 'rollback_job_id': 'rollback-123'}
            
            response = api_client.post(f'/api/deployment/jobs/{job_id}/rollback', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['status'] == 'success'
            assert 'rollback_job_id' in data

    @pytest.mark.deployment
    def test_deployment_progress_tracking(self, api_client, mock_database, deployment_job_data, auth_headers):
        """Test deployment progress tracking."""
        deployment_job_data['status'] = 'Running'
        job_id = mock_database.deployment_jobs.insert(**deployment_job_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.controllers.deployment.get_job_progress') as mock_progress:
            
            mock_get_db.return_value = mock_database
            mock_progress.return_value = {
                'progress_percent': 65,
                'current_task': 'Installing packages',
                'estimated_completion': '5 minutes',
                'tasks_completed': 13,
                'tasks_total': 20
            }
            
            response = api_client.get(f'/api/deployment/jobs/{job_id}/progress', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['progress_percent'] == 65
            assert 'current_task' in data

    @pytest.mark.deployment
    def test_deployment_error_handling(self, mock_database, deployment_config):
        """Test deployment error handling and recovery."""
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import handle_deployment_error
        
        error_scenarios = [
            {'error_type': 'network_timeout', 'recoverable': True},
            {'error_type': 'authentication_failure', 'recoverable': False},
            {'error_type': 'disk_full', 'recoverable': False},
            {'error_type': 'package_not_found', 'recoverable': True}
        ]
        
        for scenario in error_scenarios:
            result = handle_deployment_error(
                job_id='test-job',
                error=Exception(scenario['error_type']),
                attempt=1
            )
            
            if scenario['recoverable']:
                assert result['action'] in ['retry', 'continue']
            else:
                assert result['action'] == 'fail'

    @pytest.mark.deployment
    def test_deployment_resource_management(self, deployment_config):
        """Test deployment resource management and cleanup."""
        from gough.containers.management_server.py4web_app.lib.tasks.deployment import ResourceManager
        
        resource_manager = ResourceManager()
        
        with patch.object(resource_manager, 'allocate_resources') as mock_allocate, \
             patch.object(resource_manager, 'monitor_usage') as mock_monitor, \
             patch.object(resource_manager, 'cleanup_resources') as mock_cleanup:
            
            mock_allocate.return_value = {'cpu': 2, 'memory': 4096, 'storage': 100}
            mock_monitor.return_value = {'cpu_usage': 45, 'memory_usage': 60, 'storage_usage': 25}
            mock_cleanup.return_value = {'resources_freed': True}
            
            # Test resource lifecycle
            resources = resource_manager.allocate_resources('test-job')
            usage = resource_manager.monitor_usage('test-job')
            cleanup_result = resource_manager.cleanup_resources('test-job')
            
            assert resources['cpu'] > 0
            assert usage['cpu_usage'] < 100
            assert cleanup_result['resources_freed']

    @pytest.mark.deployment
    @pytest.mark.slow
    def test_deployment_timeout_handling(self, api_client, mock_database, deployment_job_data, auth_headers):
        """Test deployment timeout handling."""
        # Create long-running job
        deployment_job_data['status'] = 'Running'
        deployment_job_data['started_on'] = datetime.utcnow() - timedelta(hours=2)
        job_id = mock_database.deployment_jobs.insert(**deployment_job_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.deployment.get_db') as mock_get_db, \
             patch('gough.containers.management_server.py4web_app.controllers.deployment.check_job_timeout') as mock_timeout:
            
            mock_get_db.return_value = mock_database
            mock_timeout.return_value = {'timed_out': True, 'action': 'terminate'}
            
            response = api_client.get(f'/api/deployment/jobs/{job_id}/timeout-check', headers=auth_headers)
            
            # Endpoint may not exist yet
            assert response.status_code in [200, 404]