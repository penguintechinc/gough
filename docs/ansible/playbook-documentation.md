# Gough Ansible Playbooks Documentation

This document provides comprehensive documentation for all Ansible playbooks, roles, and automation workflows in the Gough hypervisor automation system.

## Table of Contents

1. [Overview](#overview)
2. [Playbook Architecture](#playbook-architecture)
3. [Core Playbooks](#core-playbooks)
4. [Ansible Roles](#ansible-roles)
5. [Inventory Management](#inventory-management)
6. [Variables and Configuration](#variables-and-configuration)
7. [Best Practices](#best-practices)
8. [Troubleshooting](#troubleshooting)
9. [Examples and Use Cases](#examples-and-use-cases)

---

## Overview

The Gough Ansible automation framework provides complete lifecycle management for bare metal servers, from initial provisioning through ongoing management. The system integrates with MaaS for hardware provisioning and FleetDM for security monitoring.

### Key Features

- **Automated Server Provisioning**: Complete bare metal server deployment
- **Template-Based Configuration**: Standardized server configurations
- **Role-Based Deployment**: Different server roles (web, database, kubernetes)
- **Integration Management**: MaaS and FleetDM integration
- **Scalable Operations**: Parallel processing and batch operations
- **Environment Management**: Development, staging, and production support

### System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                 Ansible Control Node                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ site.yml    │  │ Playbooks   │  │ Roles       │     │
│  │ (Master)    │  │ Directory   │  │ Directory   │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
│           │              │                │             │
│           └──────────────┼────────────────┘             │
│                          │                              │
└──────────────────────────┼──────────────────────────────┘
                           │
           ┌───────────────┼───────────────┐
           │               │               │
     ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐
     │   MaaS    │  │  FleetDM  │  │ Deployed  │
     │   API     │  │    API    │  │  Servers  │
     └───────────┘  └───────────┘  └───────────┘
```

---

## Playbook Architecture

The Gough Ansible framework follows a hierarchical structure designed for scalability and maintainability.

### Directory Structure

```
gough/ansible/
├── site.yml                      # Master orchestration playbook
├── playbooks/                    # Individual playbooks
│   ├── provision-server.yml      # Server provisioning via MaaS
│   ├── configure-packages.yml    # Package installation and configuration
│   ├── deploy-agent.yml         # Agent deployment
│   ├── deploy-osquery.yml       # OSQuery/FleetDM integration
│   ├── deploy-server.yml        # Complete server deployment
│   └── update-fleet.yml         # Fleet management updates
├── roles/                        # Ansible roles
│   ├── maas-provision/           # MaaS provisioning role
│   ├── cloud-init-config/        # Cloud-init configuration
│   ├── agent-deployment/         # Agent deployment role
│   ├── docker-setup/             # Docker installation and configuration
│   ├── lxd-setup/               # LXD/LXC setup
│   └── osquery-install/         # OSQuery installation
├── inventory/                     # Inventory management
│   ├── hosts.yml                 # Static inventory
│   ├── maas-inventory.yml        # Dynamic MaaS inventory
│   └── group_vars/              # Group variable definitions
├── templates/                    # Jinja2 templates
│   ├── deployment-report.j2      # Deployment reporting
│   └── provision-report.j2       # Provisioning reports
└── vars/                         # Variable files
    ├── main.yml                  # Global variables
    ├── development.yml           # Development environment
    ├── staging.yml              # Staging environment
    └── production.yml           # Production environment
```

### Execution Flow

1. **site.yml** serves as the master orchestrator
2. Individual playbooks handle specific tasks
3. Roles provide reusable functionality
4. Inventory systems manage target hosts
5. Variables customize behavior per environment

---

## Core Playbooks

### 1. Site.yml - Master Orchestration Playbook

The master playbook coordinates all provisioning and deployment operations.

**Purpose**: Central orchestration of the entire server lifecycle

**Key Features**:
- Multi-environment support (development, staging, production)
- Flexible execution modes (full, provision-only, configure-only, deploy-only)
- Parallel processing for scalability
- Comprehensive error handling and reporting

**Usage Examples**:

```bash
# Full deployment of 5 Docker hosts in production
ansible-playbook site.yml -e "mode=full env=production count=5 role=docker_host"

# Provision only 3 Kubernetes nodes in staging
ansible-playbook site.yml -e "mode=provision-only env=staging count=3 role=kubernetes_node"

# Configure packages for existing servers
ansible-playbook site.yml -e "mode=configure-only env=development packages=base_standard"

# Deploy agents to existing infrastructure
ansible-playbook site.yml -e "mode=deploy-only env=production"
```

**Parameters**:

| Parameter | Description | Default | Valid Values |
|-----------|-------------|---------|--------------|
| mode | Orchestration mode | full | full, provision-only, configure-only, deploy-only |
| env | Target environment | development | development, staging, production |
| count | Number of servers | 1 | 1-100 |
| role | Server role | general_server | docker_host, kubernetes_node, general_server |
| packages | Package profile | base_standard | base_minimal, base_standard, base_extended |

**Execution Phases**:

1. **Pre-tasks**: Parameter validation and environment setup
2. **Provisioning**: Server commissioning and deployment via MaaS
3. **Configuration**: Package installation and system configuration
4. **Agent Deployment**: Gough agent and OSQuery installation
5. **Fleet Updates**: FleetDM enrollment and policy application
6. **Post-tasks**: Reporting and notification

### 2. provision-server.yml - Server Provisioning

Handles complete bare metal server provisioning through MaaS integration.

**Purpose**: Automate server discovery, commissioning, and OS deployment

**Key Features**:
- MaaS API integration for hardware management
- Automated machine selection and allocation
- Cloud-init template application
- Deployment status monitoring
- SSH connectivity verification

**Workflow**:

```mermaid
flowchart TD
    A[Validate Parameters] --> B[Check MaaS Connectivity]
    B --> C[Query Available Machines]
    C --> D[Select Machines for Provisioning]
    D --> E[Generate Cloud-Init Config]
    E --> F[Commission Selected Machines]
    F --> G[Wait for Commissioning]
    G --> H{Commissioning Successful?}
    H -->|Yes| I[Tag Machines]
    H -->|No| J[Report Failure]
    I --> K[Allocate Machines]
    K --> L[Deploy OS]
    L --> M[Monitor Deployment]
    M --> N{Deployment Complete?}
    N -->|Yes| O[Add to Inventory]
    N -->|No| P[Wait and Retry]
    P --> M
    O --> Q[Test SSH Connectivity]
    Q --> R[Generate Report]
```

**Usage Example**:

```bash
# Provision 3 Docker hosts in production with custom cloud-init
ansible-playbook playbooks/provision-server.yml \
  -e "provision_count=3" \
  -e "provision_role=docker_host" \
  -e "provision_environment=production" \
  -e "cloud_init_template=docker-host-custom.yaml" \
  -e "timeout_minutes=45"
```

**Variables**:

```yaml
# Required variables
maas_url: "http://maas:5240/MAAS"
maas_api_version: "2.0"
maas_oauth_token: "consumer_key:token_key:token_secret"

# Optional variables
ubuntu_series: "jammy"  # Ubuntu 24.04 LTS
hwe_kernel: "hwe-22.04"
commissioning_scripts: "update_firmware,internet-connectivity"
testing_scripts: "smartctl-validate,7z,fio"
ssh_private_key_path: "~/.ssh/id_rsa"
management_server_url: "http://management:8000"
```

### 3. configure-packages.yml - Package Configuration

Manages software installation and system configuration based on server roles.

**Purpose**: Install and configure software packages according to server role

**Key Features**:
- Role-based package selection
- Environment-specific configurations
- Custom package repositories
- Configuration validation
- Service management

**Package Profiles**:

| Profile | Description | Packages |
|---------|-------------|----------|
| base_minimal | Essential packages only | curl, wget, vim, htop, ssh |
| base_standard | Standard server packages | base_minimal + git, python3, docker.io |
| base_extended | Extended package set | base_standard + monitoring, security tools |
| docker_host | Docker container host | base_standard + docker-compose, containerd |
| kubernetes_node | Kubernetes worker | base_standard + kubelet, kubeadm, kubectl |
| database_server | Database server | base_standard + postgresql/mysql, backup tools |

**Usage Example**:

```bash
# Configure Docker hosts with extended packages in staging
ansible-playbook playbooks/configure-packages.yml \
  -e "config_role=docker_host" \
  -e "config_packages=base_extended" \
  -e "config_environment=staging" \
  --limit "docker_host_servers"
```

### 4. deploy-agent.yml - Agent Deployment

Deploys Gough management agents to provisioned servers.

**Purpose**: Install and configure Gough monitoring and management agents

**Key Features**:
- Automated agent installation
- Secure communication setup
- Health monitoring configuration
- Service management
- Log rotation setup

**Agent Components**:
- **Management Agent**: System monitoring and remote management
- **OSQuery Agent**: Security monitoring and compliance
- **Log Forwarder**: Centralized log collection
- **Health Check**: System health monitoring

**Usage Example**:

```bash
# Deploy agents to all servers in production
ansible-playbook playbooks/deploy-agent.yml \
  -e "deployment_environment=production" \
  -e "deployment_role=all" \
  --limit "deployed_servers"
```

### 5. deploy-osquery.yml - OSQuery Integration

Manages OSQuery deployment and FleetDM integration for security monitoring.

**Purpose**: Deploy OSQuery agents and integrate with FleetDM for security monitoring

**Key Features**:
- OSQuery installation and configuration
- FleetDM enrollment automation
- Security policy deployment
- Query pack management
- Alert configuration

**Usage Example**:

```bash
# Deploy OSQuery with custom security policies
ansible-playbook playbooks/deploy-osquery.yml \
  -e "fleet_url=https://fleetdm:8443" \
  -e "fleet_api_token=your-api-token" \
  -e "security_policy=production-strict" \
  --limit "production_servers"
```

### 6. update-fleet.yml - Fleet Management

Updates FleetDM configurations and manages fleet-wide operations.

**Purpose**: Manage FleetDM policies, queries, and configurations

**Key Features**:
- Policy deployment and updates
- Query pack management
- Fleet configuration updates
- Agent health monitoring
- Compliance reporting

---

## Ansible Roles

### 1. maas-provision - MaaS Provisioning Role

**Path**: `roles/maas-provision/`

**Purpose**: Provides reusable tasks for MaaS API interactions

**Tasks**:
- MaaS API connectivity verification
- Machine discovery and filtering
- Commissioning and testing
- Deployment orchestration
- Status monitoring

**Variables** (`vars/main.yml`):

```yaml
maas_api_version: "2.0"
maas_timeout: 30
commissioning_timeout: 1800  # 30 minutes
deployment_timeout: 2400     # 40 minutes
```

### 2. cloud-init-config - Cloud-Init Configuration

**Path**: `roles/cloud-init-config/`

**Purpose**: Generate and manage cloud-init configurations

**Features**:
- Template-based configuration generation
- Variable substitution and validation
- Multi-environment support
- Configuration testing

**Templates**:
- `base-server.yaml.j2`: Basic server configuration
- `docker-host.yaml.j2`: Docker host setup
- `kubernetes-node.yaml.j2`: Kubernetes node configuration

### 3. agent-deployment - Agent Deployment Role

**Path**: `roles/agent-deployment/`

**Purpose**: Deploy and configure Gough management agents

**Tasks**:
- Agent installation and setup
- Configuration file generation
- Service management
- Health check setup
- Log rotation configuration

**Handlers** (`handlers/main.yml`):

```yaml
- name: restart maas-agent
  systemd:
    name: maas-agent
    state: restarted

- name: reload systemd
  systemd:
    daemon_reload: yes

- name: restart osqueryd
  systemd:
    name: osqueryd
    state: restarted
```

### 4. docker-setup - Docker Configuration

**Path**: `roles/docker-setup/`

**Purpose**: Install and configure Docker container runtime

**Features**:
- Docker Engine installation
- Docker Compose setup
- Registry configuration
- Security hardening
- Resource limits

**Variables**:

```yaml
docker_version: "24.0.7"
docker_compose_version: "2.21.0"
docker_users:
  - ubuntu
  - gough
docker_daemon_options:
  log-driver: "json-file"
  log-opts:
    max-size: "100m"
    max-file: "5"
```

### 5. lxd-setup - LXD Configuration

**Path**: `roles/lxd-setup/`

**Purpose**: Install and configure LXD container runtime

**Features**:
- LXD installation
- Storage pool configuration
- Network setup
- Profile management
- Security configuration

### 6. osquery-install - OSQuery Installation

**Path**: `roles/osquery-install/`

**Purpose**: Install and configure OSQuery for security monitoring

**Features**:
- OSQuery package installation
- FleetDM enrollment
- Configuration management
- Query pack deployment
- Service management

---

## Inventory Management

### Static Inventory (hosts.yml)

Defines static host groups and variables:

```yaml
all:
  children:
    infrastructure:
      hosts:
        maas-server:
          ansible_host: 192.168.1.10
          ansible_user: ubuntu
        management-server:
          ansible_host: 192.168.1.11
          ansible_user: ubuntu
        fleetdm-server:
          ansible_host: 192.168.1.12
          ansible_user: ubuntu
    
    deployed_servers:
      children:
        docker_host_servers:
          vars:
            server_role: docker_host
            package_profile: docker_extended
        kubernetes_node_servers:
          vars:
            server_role: kubernetes_node
            package_profile: k8s_standard
        general_servers:
          vars:
            server_role: general_server
            package_profile: base_standard

  vars:
    ansible_ssh_private_key_file: ~/.ssh/gough-key
    ansible_python_interpreter: /usr/bin/python3
```

### Dynamic Inventory (maas-inventory.yml)

Integrates with MaaS for dynamic host discovery:

```yaml
plugin: community.general.maas
keyed_groups:
  - key: status_name
    prefix: maas_status
  - key: tag_names
    prefix: maas_tag
compose:
  ansible_host: ip_addresses[0] if ip_addresses else hostname
  server_role: tag_names | select('match', 'role-.*') | map('regex_replace', 'role-', '') | first
  server_environment: tag_names | select('match', 'env-.*') | map('regex_replace', 'env-', '') | first
```

### Group Variables

**group_vars/all.yml**:

```yaml
# Global configuration
management_server_url: "http://management:8000"
maas_url: "http://maas:5240/MAAS"
fleet_url: "https://fleetdm:8443"

# SSH Configuration
ansible_user: ubuntu
ansible_ssh_common_args: '-o StrictHostKeyChecking=no'
ansible_ssh_pipelining: true

# Timeouts
task_timeout: 300
deployment_timeout: 2400
```

**group_vars/docker_host_servers.yml**:

```yaml
# Docker-specific configuration
docker_version: "24.0.7"
docker_compose_version: "2.21.0"
docker_registry_mirrors:
  - "https://registry-mirror.company.com"

# Package configuration
additional_packages:
  - docker-compose
  - docker-buildx
  - docker-scan
```

**group_vars/production.yml**:

```yaml
# Production environment settings
security_hardening: true
monitoring_enabled: true
backup_enabled: true
log_level: "INFO"

# Security settings
ssh_port: 2222
firewall_enabled: true
fail2ban_enabled: true

# Monitoring
prometheus_node_exporter: true
osquery_enabled: true
log_shipping_enabled: true
```

---

## Variables and Configuration

### Variable Hierarchy

Ansible variable precedence (highest to lowest):

1. **Extra vars** (`-e` command line)
2. **Task vars** (in playbook tasks)
3. **Block vars** (in playbook blocks)
4. **Role and include vars**
5. **Play vars_files**
6. **Play vars_prompt**
7. **Play vars**
8. **Set_facts / registered vars**
9. **Host facts**
10. **Playbook host_vars**
11. **Playbook group_vars**
12. **Inventory host_vars**
13. **Inventory group_vars**
14. **Inventory vars**
15. **Role defaults**

### Configuration Templates

**MaaS Configuration** (`vars/maas.yml`):

```yaml
maas_config:
  url: "{{ maas_url }}"
  api_version: "2.0"
  oauth_token: "{{ vault_maas_oauth_token }}"
  region_name: "gough-region"
  
  # Network configuration
  dns_servers:
    - 8.8.8.8
    - 8.8.4.4
  ntp_servers:
    - pool.ntp.org
  
  # Default deployment settings
  default_os: "ubuntu"
  default_series: "jammy"
  default_kernel: "hwe-22.04"
  
  # Commissioning settings
  commissioning_scripts:
    - update_firmware
    - internet-connectivity
    - ntp
  
  testing_scripts:
    - smartctl-validate
    - memtester
    - 7z
    - fio
```

**FleetDM Configuration** (`vars/fleetdm.yml`):

```yaml
fleetdm_config:
  url: "{{ fleet_url }}"
  api_token: "{{ vault_fleet_api_token }}"
  enrollment_secret: "{{ vault_fleet_enrollment_secret }}"
  
  # Security policies
  security_policies:
    - name: "baseline-security"
      description: "Baseline security monitoring"
      queries:
        - "process_monitoring"
        - "network_connections"
        - "file_integrity"
    
    - name: "production-strict"
      description: "Strict production security monitoring"
      queries:
        - "baseline-security"
        - "privileged_commands"
        - "system_changes"
        - "compliance_checks"
  
  # Query packs
  query_packs:
    - name: "gough-monitoring"
      path: "packs/gough-monitoring.conf"
    - name: "security-baseline"
      path: "packs/security-baseline.conf"
```

### Environment-Specific Variables

**Development Environment** (`vars/development.yml`):

```yaml
environment: development
log_level: DEBUG
security_hardening: false
monitoring_interval: 60

# Resource limits (relaxed for development)
deployment_timeout: 1800
commissioning_timeout: 1200
ssh_timeout: 300

# Development-specific packages
dev_packages:
  - git
  - vim
  - curl
  - wget
  - htop
  - tree
  - jq
```

**Production Environment** (`vars/production.yml`):

```yaml
environment: production
log_level: INFO
security_hardening: true
monitoring_interval: 30

# Resource limits (strict for production)
deployment_timeout: 3600
commissioning_timeout: 1800
ssh_timeout: 600

# Production security settings
security_settings:
  ssh_port: 2222
  firewall_enabled: true
  fail2ban_enabled: true
  selinux_enabled: true
  audit_logging: true

# Backup configuration
backup_settings:
  enabled: true
  retention_days: 30
  backup_schedule: "0 2 * * *"
  backup_location: "s3://gough-backups-prod"
```

---

## Best Practices

### 1. Playbook Structure

**Modular Design**:
- Use roles for reusable functionality
- Keep playbooks focused on specific tasks
- Implement proper error handling
- Use handlers for service management

**Example Structure**:

```yaml
---
- name: Task description
  hosts: target_group
  become: true
  gather_facts: true
  
  pre_tasks:
    - name: Validate prerequisites
      assert:
        that:
          - required_var is defined
        fail_msg: "Required variable missing"
  
  tasks:
    - name: Main task
      include_role:
        name: role_name
      vars:
        role_var: "{{ playbook_var }}"
  
  post_tasks:
    - name: Verify task completion
      uri:
        url: "{{ health_check_url }}"
        status_code: 200
  
  handlers:
    - name: restart service
      systemd:
        name: service_name
        state: restarted
```

### 2. Variable Management

**Security**:
- Use Ansible Vault for sensitive data
- Implement variable validation
- Use environment-specific variable files
- Document all variables

**Example Vault Usage**:

```bash
# Create vault file
ansible-vault create vars/vault.yml

# Edit vault file
ansible-vault edit vars/vault.yml

# Run with vault
ansible-playbook site.yml --ask-vault-pass
```

**Vault Content** (`vars/vault.yml`):

```yaml
vault_maas_oauth_token: "consumer:token:secret"
vault_fleet_api_token: "your-fleet-api-token"
vault_ssh_private_key: |
  -----BEGIN RSA PRIVATE KEY-----
  [private key content]
  -----END RSA PRIVATE KEY-----
```

### 3. Error Handling

**Comprehensive Error Handling**:

```yaml
- name: Task with error handling
  uri:
    url: "{{ api_endpoint }}"
    method: POST
    body_format: json
    body: "{{ request_body }}"
  register: api_result
  retries: 3
  delay: 10
  until: api_result.status == 200
  failed_when: 
    - api_result.status != 200
    - api_result.json.error is defined
  rescue:
    - name: Handle API failure
      debug:
        msg: "API call failed: {{ api_result.json.error | default('Unknown error') }}"
    - name: Send notification
      mail:
        to: admin@company.com
        subject: "Ansible task failed"
        body: "Task failed with error: {{ api_result.json.error }}"
```

### 4. Performance Optimization

**Parallel Execution**:

```yaml
# Process servers in parallel
- name: Deploy to multiple servers
  hosts: deployed_servers
  serial: "30%"  # Process 30% of hosts at a time
  strategy: mitogen_linear  # Use mitogen for speed
```

**Fact Caching**:

```ini
# ansible.cfg
[defaults]
fact_caching = redis
fact_caching_connection = redis://localhost:6379/0
fact_caching_timeout = 3600
```

### 5. Testing and Validation

**Syntax Validation**:

```bash
# Check syntax
ansible-playbook --syntax-check site.yml

# Dry run
ansible-playbook --check site.yml

# List tasks
ansible-playbook --list-tasks site.yml
```

**Integration Testing**:

```yaml
- name: Integration tests
  block:
    - name: Test service connectivity
      uri:
        url: "http://{{ ansible_host }}:80"
        status_code: 200
      register: health_check
    
    - name: Test database connection
      postgresql_ping:
        db: application
        login_host: "{{ ansible_host }}"
      register: db_check
    
    - name: Verify all tests passed
      assert:
        that:
          - health_check.status == 200
          - db_check.is_available
        success_msg: "All integration tests passed"
```

---

## Troubleshooting

### Common Issues and Solutions

**1. SSH Connection Issues**

*Problem*: Cannot connect to target hosts

*Solutions*:

```bash
# Test SSH connectivity
ansible all -m ping -i inventory/hosts.yml

# Debug SSH issues
ansible-playbook site.yml -vvv --limit problematic_host

# Use different SSH settings
ansible-playbook site.yml -e "ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'"
```

**2. MaaS API Issues**

*Problem*: MaaS API calls failing

*Solutions*:

```yaml
# Add debug tasks to playbooks
- name: Debug MaaS connectivity
  uri:
    url: "{{ maas_url }}/api/{{ maas_api_version }}/version/"
    headers:
      Authorization: "OAuth {{ maas_oauth_token }}"
  register: maas_debug

- name: Display debug info
  debug:
    var: maas_debug
```

**3. Variable Issues**

*Problem*: Variables not defined or incorrect values

*Solutions*:

```bash
# Show variable values
ansible-playbook site.yml --list-hosts --list-tasks -e "debug=true"

# Debug variables in playbook
- name: Debug variables
  debug:
    msg: |
      maas_url: {{ maas_url }}
      environment: {{ environment }}
      server_role: {{ server_role }}
  when: debug | default(false)
```

**4. Performance Issues**

*Problem*: Playbooks running slowly

*Solutions*:

```ini
# ansible.cfg optimizations
[defaults]
host_key_checking = False
pipelining = True
forks = 20

[ssh_connection]
ssh_args = -o ControlMaster=auto -o ControlPersist=60s
```

### Debugging Commands

```bash
# Verbose output levels
ansible-playbook site.yml -v      # Basic verbosity
ansible-playbook site.yml -vv     # More verbosity
ansible-playbook site.yml -vvv    # High verbosity (includes SSH debug)
ansible-playbook site.yml -vvvv   # Maximum verbosity

# Limit execution to specific hosts
ansible-playbook site.yml --limit "web_servers:&production"
ansible-playbook site.yml --limit "192.168.1.10"

# Start from specific task
ansible-playbook site.yml --start-at-task "Configure Docker"

# Use tags
ansible-playbook site.yml --tags "provision,configure"
ansible-playbook site.yml --skip-tags "testing"

# Check mode (dry run)
ansible-playbook site.yml --check --diff
```

---

## Examples and Use Cases

### Use Case 1: Provision Development Environment

**Scenario**: Provision 3 Docker hosts for development testing

```bash
# Complete development environment setup
ansible-playbook site.yml \
  -e "mode=full" \
  -e "env=development" \
  -e "count=3" \
  -e "role=docker_host" \
  -e "packages=base_extended" \
  --tags "provision,configure,deploy"
```

**Variables** (`development.override.yml`):

```yaml
# Development overrides
deployment_timeout: 1200
log_level: DEBUG
security_hardening: false
monitoring_enabled: true
```

### Use Case 2: Scale Production Kubernetes Cluster

**Scenario**: Add 5 new Kubernetes worker nodes to production cluster

```bash
# Provision new Kubernetes nodes
ansible-playbook site.yml \
  -e "mode=provision-only" \
  -e "env=production" \
  -e "count=5" \
  -e "role=kubernetes_node" \
  --extra-vars "@vars/production.yml" \
  --extra-vars "@vars/kubernetes.yml"
```

**Kubernetes Variables** (`vars/kubernetes.yml`):

```yaml
kubernetes_version: "1.28.0"
container_runtime: "containerd"
pod_network_cidr: "10.244.0.0/16"
service_network_cidr: "10.96.0.0/12"

additional_packages:
  - kubelet
  - kubeadm
  - kubectl
  - containerd

systemd_services:
  - kubelet
  - containerd
```

### Use Case 3: Emergency Server Replacement

**Scenario**: Replace failed production web server

```bash
# Step 1: Provision replacement server
ansible-playbook playbooks/provision-server.yml \
  -e "provision_count=1" \
  -e "provision_role=web_server" \
  -e "provision_environment=production" \
  -e "emergency_deployment=true"

# Step 2: Deploy application stack
ansible-playbook playbooks/configure-packages.yml \
  -e "config_role=web_server" \
  -e "config_environment=production" \
  --limit "new_web_server"

# Step 3: Deploy monitoring agents
ansible-playbook playbooks/deploy-agent.yml \
  -e "deployment_environment=production" \
  --limit "new_web_server"
```

### Use Case 4: Bulk Security Update

**Scenario**: Update OSQuery agents across all production servers

```bash
# Update OSQuery on all production servers
ansible-playbook playbooks/deploy-osquery.yml \
  -e "osquery_version=5.10.2" \
  -e "fleet_environment=production" \
  -e "update_existing=true" \
  --limit "production:&deployed_servers" \
  --serial "25%"  # Process 25% at a time
```

### Use Case 5: Environment Migration

**Scenario**: Migrate staging environment configuration to production

```bash
# Export staging configuration
ansible-playbook utils/export-config.yml \
  -e "source_env=staging" \
  -e "export_path=/tmp/staging-export"

# Import to production (with validation)
ansible-playbook utils/import-config.yml \
  -e "target_env=production" \
  -e "import_path=/tmp/staging-export" \
  -e "validate_only=true"  # Dry run first

# Apply to production
ansible-playbook utils/import-config.yml \
  -e "target_env=production" \
  -e "import_path=/tmp/staging-export" \
  -e "validate_only=false"
```

### Custom Playbook Example

**Custom Database Deployment** (`playbooks/deploy-database.yml`):

```yaml
---
- name: Deploy Database Server
  hosts: database_servers
  become: true
  gather_facts: true
  serial: 1  # Deploy databases one at a time
  
  vars:
    db_type: "{{ database_type | default('postgresql') }}"
    db_version: "{{ database_version | default('15') }}"
    backup_enabled: "{{ enable_backups | default(true) }}"
  
  pre_tasks:
    - name: Validate database parameters
      assert:
        that:
          - db_type in ['postgresql', 'mysql', 'mongodb']
          - db_version is defined
          - ansible_memtotal_mb >= 4096
        fail_msg: "Invalid database configuration"
  
  tasks:
    - name: Install database server
      include_role:
        name: "{{ db_type }}-server"
      vars:
        version: "{{ db_version }}"
        enable_backup: "{{ backup_enabled }}"
    
    - name: Configure database security
      include_role:
        name: database-security
      vars:
        db_engine: "{{ db_type }}"
    
    - name: Setup monitoring
      include_role:
        name: database-monitoring
      vars:
        monitoring_type: prometheus
  
  post_tasks:
    - name: Verify database installation
      uri:
        url: "http://{{ ansible_host }}:5432"
        timeout: 10
      when: db_type == 'postgresql'
    
    - name: Register with management server
      uri:
        url: "{{ management_server_url }}/api/databases"
        method: POST
        body_format: json
        body:
          hostname: "{{ inventory_hostname }}"
          type: "{{ db_type }}"
          version: "{{ db_version }}"
          status: "active"
      when: management_server_url is defined
```

This comprehensive Ansible documentation provides everything needed to understand, deploy, and manage the Gough hypervisor automation system using Ansible playbooks and roles.