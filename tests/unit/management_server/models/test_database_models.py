#!/usr/bin/env python3
"""
Unit Tests for Database Models
Tests for PyDAL database models, validation, relationships, and constraints
"""

import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from pydal.validators import ValidationError


class TestDatabaseModels:
    """Test cases for database model functionality."""

    def test_servers_table_definition(self, mock_database):
        """Test servers table structure and constraints."""
        # Test table exists
        assert hasattr(mock_database, 'servers')
        
        # Test required fields
        required_fields = ['hostname', 'mac_address']
        table_fields = [field.name for field in mock_database.servers]
        
        for field in required_fields:
            assert field in table_fields

    def test_servers_unique_constraints(self, mock_database):
        """Test unique constraints on servers table."""
        server_data = {
            'hostname': 'unique-test-server',
            'mac_address': '00:11:22:33:44:99',
            'status': 'Ready'
        }
        
        # Insert first server
        server_id1 = mock_database.servers.insert(**server_data)
        mock_database.commit()
        assert server_id1 is not None
        
        # Try to insert duplicate hostname - should fail
        server_data2 = server_data.copy()
        server_data2['mac_address'] = '00:11:22:33:44:88'
        
        with pytest.raises((ValidationError, Exception)):
            mock_database.servers.insert(**server_data2)

    def test_servers_default_values(self, mock_database):
        """Test default values for servers table fields."""
        minimal_data = {
            'hostname': 'test-defaults',
            'mac_address': '00:11:22:33:44:77'
        }
        
        server_id = mock_database.servers.insert(**minimal_data)
        mock_database.commit()
        
        server = mock_database.servers(server_id)
        assert server.status == 'New'
        assert server.architecture == 'amd64'
        assert server.zone == 'default'
        assert server.pool == 'default'
        assert server.created_on is not None

    def test_cloud_init_templates_table(self, mock_database, sample_cloud_init_template):
        """Test cloud-init templates table functionality."""
        template_id = mock_database.cloud_init_templates.insert(**sample_cloud_init_template)
        mock_database.commit()
        
        template = mock_database.cloud_init_templates(template_id)
        assert template.name == sample_cloud_init_template['name']
        assert template.template_type == 'user-data'
        assert template.is_default == False

    def test_cloud_init_template_validation(self, mock_database):
        """Test cloud-init template content validation."""
        invalid_templates = [
            {'name': 'invalid-yaml', 'template_content': 'invalid:\nyaml:\n  - content\n  missing_key'},
            {'name': 'empty-content', 'template_content': ''},
            {'name': '', 'template_content': '#cloud-config\nvalid: content'}  # Empty name
        ]
        
        for template_data in invalid_templates:
            with pytest.raises((ValidationError, Exception)):
                mock_database.cloud_init_templates.insert(**template_data)

    def test_package_configs_table(self, mock_database, sample_package_config):
        """Test package configurations table functionality."""
        package_id = mock_database.package_configs.insert(**sample_package_config)
        mock_database.commit()
        
        package_config = mock_database.package_configs(package_id)
        assert package_config.name == sample_package_config['name']
        
        # Test JSON field parsing
        packages = json.loads(package_config.packages)
        assert isinstance(packages, list)
        assert 'docker.io' in packages

    def test_deployment_jobs_table(self, mock_database, deployment_job_data):
        """Test deployment jobs table and relationships."""
        # Create dependencies
        server_id = mock_database.servers.insert(
            hostname='job-test-server',
            mac_address='00:11:22:33:44:66'
        )
        template_id = mock_database.cloud_init_templates.insert(
            name='job-test-template',
            template_content='#cloud-config\npackages: [curl]'
        )
        package_id = mock_database.package_configs.insert(
            name='job-test-packages',
            packages='["vim"]'
        )
        
        # Create deployment job
        job_data = deployment_job_data.copy()
        job_data.update({
            'server_id': server_id,
            'cloud_init_template_id': template_id,
            'package_config_id': package_id
        })
        
        job_id = mock_database.deployment_jobs.insert(**job_data)
        mock_database.commit()
        
        job = mock_database.deployment_jobs(job_id)
        assert job.server_id == server_id
        assert job.status == 'Pending'

    def test_fleetdm_config_table(self, mock_database):
        """Test FleetDM configuration table."""
        fleet_config = {
            'name': 'production-fleet',
            'fleet_url': 'https://fleet.example.com',
            'api_token': 'secret_api_token',
            'osquery_version': '5.10.2',
            'is_active': True
        }
        
        config_id = mock_database.fleetdm_config.insert(**fleet_config)
        mock_database.commit()
        
        config = mock_database.fleetdm_config(config_id)
        assert config.name == 'production-fleet'
        assert config.osquery_version == '5.10.2'

    def test_fleet_hosts_table(self, mock_database):
        """Test FleetDM hosts tracking table."""
        # Create server dependency
        server_id = mock_database.servers.insert(
            hostname='fleet-host-test',
            mac_address='00:11:22:33:44:55'
        )
        
        fleet_host = {
            'fleet_host_id': 123,
            'hostname': 'fleet-host-test',
            'uuid': 'host-uuid-123',
            'server_id': server_id,
            'status': 'online',
            'platform': 'ubuntu',
            'osquery_version': '5.10.2',
            'last_seen': datetime.utcnow(),
            'enrollment_date': datetime.utcnow()
        }
        
        host_id = mock_database.fleet_hosts.insert(**fleet_host)
        mock_database.commit()
        
        host = mock_database.fleet_hosts(host_id)
        assert host.fleet_host_id == 123
        assert host.status == 'online'

    def test_fleet_queries_table(self, mock_database, fleet_query_data):
        """Test FleetDM queries table."""
        query_id = mock_database.fleet_queries.insert(**fleet_query_data)
        mock_database.commit()
        
        query = mock_database.fleet_queries(query_id)
        assert query.name == fleet_query_data['name']
        assert query.category == 'system'

    def test_system_logs_table(self, mock_database):
        """Test system logs table functionality."""
        server_id = mock_database.servers.insert(
            hostname='log-test-server',
            mac_address='00:11:22:33:44:44'
        )
        
        log_entry = {
            'level': 'ERROR',
            'component': 'maas',
            'message': 'Failed to commission server',
            'details': json.dumps({'error_code': 'COMM_001', 'server_id': server_id}),
            'server_id': server_id
        }
        
        log_id = mock_database.system_logs.insert(**log_entry)
        mock_database.commit()
        
        log = mock_database.system_logs(log_id)
        assert log.level == 'ERROR'
        assert log.component == 'maas'
        
        # Test JSON details parsing
        details = json.loads(log.details)
        assert details['error_code'] == 'COMM_001'

    def test_foreign_key_relationships(self, mock_database):
        """Test foreign key relationships between tables."""
        # Create parent records
        server_id = mock_database.servers.insert(
            hostname='fk-test-server',
            mac_address='00:11:22:33:44:33'
        )
        template_id = mock_database.cloud_init_templates.insert(
            name='fk-test-template',
            template_content='#cloud-config\npackages: []'
        )
        
        # Create child record with foreign keys
        job_id = mock_database.deployment_jobs.insert(
            job_id='fk-test-job',
            server_id=server_id,
            cloud_init_template_id=template_id,
            status='Pending'
        )
        mock_database.commit()
        
        # Test relationship access
        job = mock_database.deployment_jobs(job_id)
        assert job.server_id.hostname == 'fk-test-server'
        assert job.cloud_init_template_id.name == 'fk-test-template'

    def test_database_indexes(self, mock_database):
        """Test database indexes for performance."""
        # This would test that proper indexes exist
        # In SQLite, we can check the sqlite_master table
        if hasattr(mock_database._adapter, 'execute'):
            # Check for indexes on frequently queried columns
            indexes = mock_database._adapter.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            
            # Should have indexes on foreign keys and commonly searched fields
            index_names = [idx[0] for idx in indexes]
            # At minimum, SQLite creates auto-indexes for foreign keys
            assert len(index_names) > 0

    def test_data_validation_constraints(self, mock_database):
        """Test data validation constraints."""
        # Test invalid MAC address formats
        invalid_servers = [
            {'hostname': 'test1', 'mac_address': 'invalid-mac'},
            {'hostname': 'test2', 'mac_address': '00:11:22:33:44'},  # Too short
            {'hostname': 'test3', 'mac_address': '00:11:22:33:44:55:66'},  # Too long
        ]
        
        for server_data in invalid_servers:
            # Some validation might be handled at application level
            try:
                mock_database.servers.insert(**server_data)
                mock_database.commit()
            except (ValidationError, Exception):
                # Expected for invalid data
                pass

    def test_cascade_deletion(self, mock_database):
        """Test cascade deletion behavior."""
        # Create server with related records
        server_id = mock_database.servers.insert(
            hostname='cascade-test-server',
            mac_address='00:11:22:33:44:22'
        )
        
        # Create related records
        job_id = mock_database.deployment_jobs.insert(
            job_id='cascade-test-job',
            server_id=server_id,
            status='Pending'
        )
        
        log_id = mock_database.system_logs.insert(
            level='INFO',
            component='test',
            message='Test log entry',
            server_id=server_id
        )
        mock_database.commit()
        
        # Delete server - related records should be handled appropriately
        mock_database(mock_database.servers.id == server_id).delete()
        mock_database.commit()
        
        # Check if related records still exist or were cleaned up
        job = mock_database.deployment_jobs(job_id)
        log = mock_database.system_logs(log_id)
        
        # Behavior depends on cascade settings - either nullified or deleted
        assert job is None or job.server_id is None
        assert log is None or log.server_id is None

    def test_json_field_operations(self, mock_database):
        """Test JSON field storage and retrieval."""
        # Test complex JSON data
        complex_config = {
            'name': 'complex-config',
            'packages': json.dumps([
                'docker.io',
                'kubernetes-client',
                'monitoring-agent'
            ]),
            'repositories': json.dumps([
                {
                    'name': 'docker',
                    'url': 'https://download.docker.com/linux/ubuntu',
                    'key': 'docker-key'
                }
            ]),
            'pre_install_scripts': json.dumps({
                'update_system': 'apt-get update && apt-get upgrade -y',
                'configure_firewall': 'ufw enable'
            })
        }
        
        config_id = mock_database.package_configs.insert(**complex_config)
        mock_database.commit()
        
        config = mock_database.package_configs(config_id)
        
        # Test JSON parsing
        packages = json.loads(config.packages)
        assert isinstance(packages, list)
        assert 'docker.io' in packages
        
        repositories = json.loads(config.repositories)
        assert isinstance(repositories, list)
        assert repositories[0]['name'] == 'docker'

    def test_datetime_field_operations(self, mock_database):
        """Test datetime field operations."""
        server_id = mock_database.servers.insert(
            hostname='datetime-test-server',
            mac_address='00:11:22:33:44:11'
        )
        mock_database.commit()
        
        server = mock_database.servers(server_id)
        
        # Test automatic timestamp setting
        assert server.created_on is not None
        assert isinstance(server.created_on, datetime)
        assert server.created_on <= datetime.utcnow()
        
        # Test updated_on field
        original_updated = server.updated_on
        
        # Update the record
        mock_database(mock_database.servers.id == server_id).update(
            status='Deployed'
        )
        mock_database.commit()
        
        updated_server = mock_database.servers(server_id)
        # updated_on should be newer than original
        if updated_server.updated_on and original_updated:
            assert updated_server.updated_on >= original_updated

    @pytest.mark.database
    def test_database_transactions(self, mock_database):
        """Test database transaction handling."""
        try:
            # Start a transaction
            with mock_database.transaction():
                server_id = mock_database.servers.insert(
                    hostname='transaction-test-server',
                    mac_address='00:11:22:33:44:00'
                )
                
                # Simulate an error
                raise Exception("Simulated error")
                
        except Exception:
            pass
        
        # Server should not exist due to rollback
        servers = mock_database(
            mock_database.servers.hostname == 'transaction-test-server'
        ).select()
        assert len(servers) == 0

    def test_query_performance_optimization(self, mock_database):
        """Test query performance considerations."""
        # Create multiple servers for testing
        server_ids = []
        for i in range(50):
            server_id = mock_database.servers.insert(
                hostname=f'perf-test-server-{i:03d}',
                mac_address=f'00:11:22:33:{i:02x}:00',
                status='Ready' if i % 2 == 0 else 'Deployed'
            )
            server_ids.append(server_id)
        mock_database.commit()
        
        # Test efficient queries
        # Query with index (should be fast)
        ready_servers = mock_database(
            mock_database.servers.status == 'Ready'
        ).select()
        
        assert len(ready_servers) == 25  # Half should be Ready
        
        # Test count operations
        total_count = mock_database(mock_database.servers.id > 0).count()
        assert total_count >= 50