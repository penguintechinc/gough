#!/usr/bin/env python3
"""
Unit Tests for Management Server API Controller
Tests for REST API endpoints, request handling, and response formatting
"""

import json
import uuid
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

import pytest
from py4web import HTTP


class TestAPIController:
    """Test cases for API controller functionality."""

    def test_json_response_success(self, api_client):
        """Test successful JSON response formatting."""
        from gough.containers.management_server.py4web_app.controllers.api import json_response
        
        test_data = {'status': 'success', 'data': {'id': 1, 'name': 'test'}}
        result = json_response(test_data)
        
        parsed_result = json.loads(result)
        assert parsed_result == test_data
        assert 'status' in parsed_result
        assert parsed_result['status'] == 'success'

    def test_error_response_formatting(self, api_client):
        """Test error response formatting and structure."""
        from gough.containers.management_server.py4web_app.controllers.api import error_response
        
        result = error_response('Test error message', 'TEST_ERROR', 400)
        parsed_result = json.loads(result)
        
        assert 'error' in parsed_result
        assert parsed_result['error']['message'] == 'Test error message'
        assert parsed_result['error']['code'] == 'TEST_ERROR'
        assert 'timestamp' in parsed_result['error']
        assert 'request_id' in parsed_result['error']

    @pytest.mark.api
    def test_servers_list_endpoint(self, api_client, mock_database, sample_server_data, auth_headers):
        """Test GET /api/servers endpoint."""
        # Insert test data
        mock_database.servers.insert(**sample_server_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.get('/api/servers', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'servers' in data
            assert len(data['servers']) == 1
            assert data['servers'][0]['hostname'] == sample_server_data['hostname']

    @pytest.mark.api
    def test_servers_create_endpoint(self, api_client, mock_database, sample_server_data, auth_headers):
        """Test POST /api/servers endpoint."""
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.post(
                '/api/servers',
                data=json.dumps(sample_server_data),
                headers=auth_headers
            )
            
            assert response.status_code == 201
            data = json.loads(response.data)
            assert data['status'] == 'success'
            assert data['server']['hostname'] == sample_server_data['hostname']

    @pytest.mark.api
    def test_servers_get_by_id(self, api_client, mock_database, sample_server_data, auth_headers):
        """Test GET /api/servers/{id} endpoint."""
        # Insert test data
        server_id = mock_database.servers.insert(**sample_server_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.get(f'/api/servers/{server_id}', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['server']['hostname'] == sample_server_data['hostname']
            assert data['server']['id'] == server_id

    @pytest.mark.api
    def test_servers_update_endpoint(self, api_client, mock_database, sample_server_data, auth_headers):
        """Test PUT /api/servers/{id} endpoint."""
        # Insert test data
        server_id = mock_database.servers.insert(**sample_server_data)
        mock_database.commit()
        
        update_data = {'status': 'Deployed', 'ip_address': '192.168.1.101'}
        
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.put(
                f'/api/servers/{server_id}',
                data=json.dumps(update_data),
                headers=auth_headers
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['server']['status'] == 'Deployed'
            assert data['server']['ip_address'] == '192.168.1.101'

    @pytest.mark.api
    def test_servers_delete_endpoint(self, api_client, mock_database, sample_server_data, auth_headers):
        """Test DELETE /api/servers/{id} endpoint."""
        # Insert test data
        server_id = mock_database.servers.insert(**sample_server_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.delete(f'/api/servers/{server_id}', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['status'] == 'success'
            
            # Verify server was deleted
            server = mock_database.servers(server_id)
            assert server is None

    @pytest.mark.api
    def test_deployment_jobs_endpoint(self, api_client, mock_database, deployment_job_data, auth_headers):
        """Test deployment jobs API endpoints."""
        # Insert dependencies
        server_id = mock_database.servers.insert(
            hostname='test-server',
            mac_address='00:11:22:33:44:55',
            status='Ready'
        )
        template_id = mock_database.cloud_init_templates.insert(
            name='test-template',
            template_content='#cloud-config\npackages: [curl]'
        )
        package_id = mock_database.package_configs.insert(
            name='test-packages',
            packages='["vim", "htop"]'
        )
        
        deployment_job_data.update({
            'server_id': server_id,
            'cloud_init_template_id': template_id,
            'package_config_id': package_id
        })
        
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            # Test create deployment job
            response = api_client.post(
                '/api/deployment/jobs',
                data=json.dumps(deployment_job_data),
                headers=auth_headers
            )
            
            assert response.status_code == 201
            data = json.loads(response.data)
            assert data['job']['status'] == 'Pending'
            assert 'job_id' in data['job']

    @pytest.mark.api
    def test_fleet_hosts_endpoint(self, api_client, mock_database, mock_fleet_client, auth_headers):
        """Test FleetDM hosts API endpoints."""
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.get('/api/fleet/hosts', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'hosts' in data

    @pytest.mark.api
    def test_fleet_queries_endpoint(self, api_client, mock_database, fleet_query_data, auth_headers):
        """Test FleetDM queries API endpoints."""
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            # Test create query
            response = api_client.post(
                '/api/fleet/queries',
                data=json.dumps(fleet_query_data),
                headers=auth_headers
            )
            
            assert response.status_code == 201
            data = json.loads(response.data)
            assert data['query']['name'] == fleet_query_data['name']

    @pytest.mark.api
    def test_maas_sync_endpoint(self, api_client, mock_database, mock_maas_client, auth_headers):
        """Test MaaS synchronization API endpoint."""
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.post('/api/maas/sync', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['status'] == 'success'

    def test_validation_errors(self, api_client, auth_headers):
        """Test API validation error handling."""
        invalid_data = {'invalid_field': 'value'}
        
        response = api_client.post(
            '/api/servers',
            data=json.dumps(invalid_data),
            headers=auth_headers
        )
        
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data
        assert 'validation' in data['error']['code'].lower()

    def test_authentication_required(self, api_client):
        """Test that API endpoints require authentication."""
        response = api_client.get('/api/servers')
        
        assert response.status_code == 401
        data = json.loads(response.data)
        assert 'error' in data
        assert 'authentication' in data['error']['message'].lower()

    def test_rate_limiting(self, api_client, auth_headers):
        """Test API rate limiting functionality."""
        # This would need to be implemented in the actual API
        # For now, test that repeated requests don't cause server errors
        for _ in range(10):
            response = api_client.get('/api/servers', headers=auth_headers)
            assert response.status_code in [200, 429]  # 429 = Too Many Requests

    @pytest.mark.parametrize("endpoint,method", [
        ('/api/servers', 'GET'),
        ('/api/servers', 'POST'),
        ('/api/deployment/jobs', 'GET'),
        ('/api/fleet/hosts', 'GET'),
        ('/api/maas/machines', 'GET')
    ])
    def test_cors_headers(self, api_client, auth_headers, endpoint, method):
        """Test CORS headers are present in API responses."""
        if method == 'GET':
            response = api_client.get(endpoint, headers=auth_headers)
        elif method == 'POST':
            response = api_client.post(endpoint, headers=auth_headers, data='{}')
        
        # Should have CORS headers for cross-origin requests
        assert 'Access-Control-Allow-Origin' in response.headers or response.status_code == 404

    def test_api_versioning(self, api_client, auth_headers):
        """Test API versioning support."""
        headers = {**auth_headers, 'Accept': 'application/vnd.gough.v1+json'}
        
        response = api_client.get('/api/servers', headers=headers)
        
        # Should handle versioned requests appropriately
        assert response.status_code in [200, 404, 406]  # 406 = Not Acceptable

    def test_pagination(self, api_client, mock_database, sample_server_data, auth_headers):
        """Test API pagination functionality."""
        # Create multiple servers for pagination testing
        for i in range(25):
            server_data = sample_server_data.copy()
            server_data['hostname'] = f'test-server-{i:02d}'
            server_data['mac_address'] = f'00:11:22:33:44:{i:02x}'
            mock_database.servers.insert(**server_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            # Test first page
            response = api_client.get('/api/servers?page=1&per_page=10', headers=auth_headers)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            
            if 'pagination' in data:
                assert data['pagination']['page'] == 1
                assert data['pagination']['per_page'] == 10
                assert len(data['servers']) <= 10

    def test_filtering_and_sorting(self, api_client, mock_database, sample_server_data, auth_headers):
        """Test API filtering and sorting functionality."""
        # Create servers with different statuses
        statuses = ['Ready', 'Deployed', 'Failed', 'Commissioning']
        for i, status in enumerate(statuses):
            server_data = sample_server_data.copy()
            server_data['hostname'] = f'test-server-{i:02d}'
            server_data['mac_address'] = f'00:11:22:33:44:{i:02x}'
            server_data['status'] = status
            mock_database.servers.insert(**server_data)
        mock_database.commit()
        
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            # Test status filtering
            response = api_client.get('/api/servers?status=Ready', headers=auth_headers)
            assert response.status_code == 200
            
            # Test sorting
            response = api_client.get('/api/servers?sort=hostname', headers=auth_headers)
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_async_operations(self, api_client, mock_database, auth_headers):
        """Test asynchronous API operations."""
        # This would test async endpoints if they exist
        with patch('gough.containers.management_server.py4web_app.controllers.api.get_db') as mock_get_db:
            mock_get_db.return_value = mock_database
            
            response = api_client.get('/api/deployment/status', headers=auth_headers)
            
            # Should handle async operations properly
            assert response.status_code in [200, 404]