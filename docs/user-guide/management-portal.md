# Gough Management Portal User Guide

This comprehensive user guide covers all aspects of using the Gough Management Portal for hypervisor automation and bare metal server provisioning.

## Table of Contents

1. [Getting Started](#getting-started)
2. [Dashboard Overview](#dashboard-overview)
3. [Server Management](#server-management)
4. [Template Management](#template-management)
5. [Job Monitoring](#job-monitoring)
6. [System Configuration](#system-configuration)
7. [User Management](#user-management)
8. [Monitoring and Alerts](#monitoring-and-alerts)
9. [Workflows and Best Practices](#workflows-and-best-practices)
10. [Troubleshooting](#troubleshooting)

---

## Getting Started

### Accessing the Portal

The Gough Management Portal is accessible through your web browser:

- **Production**: `https://your-gough-domain.com`
- **Development**: `http://localhost:8000`

### First-Time Login

1. **Navigate to the login page**
   - Open your web browser and go to the portal URL
   - You'll be automatically redirected to the login page if not authenticated

2. **Enter your credentials**
   ```
   Default admin credentials:
   Email: admin@gough.local
   Password: (set during installation)
   ```

3. **Complete initial setup** (first-time users)
   - Change default password
   - Configure system settings
   - Set up MaaS and FleetDM connections

### User Interface Overview

The portal uses a modern, responsive design with the following main components:

```
┌─────────────────────────────────────────────────────────┐
│ Header: Gough Logo | Navigation | User Menu | Alerts    │
├─────────────────────────────────────────────────────────┤
│ Sidebar Navigation                │ Main Content Area   │
│ • Dashboard                       │                     │
│ • Servers                         │                     │
│ • Jobs                            │                     │
│ • Templates                       │                     │
│ • Configuration                   │                     │
│ • Users                           │                     │
│ • Monitoring                      │                     │
├───────────────────────────────────┤                     │
│ Quick Actions                     │                     │
│ • Deploy Server                   │                     │
│ • Create Template                 │                     │
│ • View Logs                       │                     │
└───────────────────────────────────┴─────────────────────┘
```

---

## Dashboard Overview

The dashboard provides a real-time overview of your entire Gough system.

### Key Metrics Display

**System Health Panel**
- Overall system status (Healthy/Degraded/Unhealthy)
- Component connectivity status
- System uptime and version information

**Server Statistics**
- Total servers under management
- Servers by status (New, Ready, Deployed, Failed)
- Recent deployment activity
- Geographic distribution (if configured)

**Job Activity**
- Active jobs counter
- Recent job history
- Success/failure rates
- Queue depth and processing times

**Resource Utilization**
- CPU, Memory, and Disk usage graphs
- Network throughput charts
- Database performance metrics
- Container resource consumption

### Workflow: Daily System Check

1. **Login to the portal**
   - Check for any critical alerts in the header
   - Review system health panel for any degraded components

2. **Review server status**
   - Scan the server statistics for failed deployments
   - Check for servers requiring attention

3. **Monitor active jobs**
   - Review any long-running jobs
   - Check for failed jobs that need investigation

4. **Examine resource usage**
   - Ensure system resources are within normal ranges
   - Identify potential capacity issues

---

## Server Management

Server management is the core functionality of the Gough portal, providing complete lifecycle management for bare metal servers.

### Server Discovery and Registration

**Automatic Discovery (Recommended)**
1. **Enable MaaS DHCP/DNS**
   - Navigate to Configuration → MaaS Settings
   - Ensure DHCP server is enabled and configured
   - Verify network ranges and VLAN settings

2. **Physical server boot**
   - Power on the server (ensure network cable connected)
   - Server will PXE boot and appear in "New" status
   - MaaS will automatically discover hardware specifications

**Manual Registration**
1. **Navigate to Servers → Add New Server**
2. **Fill in server details**:
   ```
   MAC Address: 52:54:00:12:34:56 (required)
   Hostname: web-server-01 (optional)
   Architecture: amd64/generic (default)
   Power Type: IPMI/Manual/Virsh
   Power Parameters: Based on power type
   Tags: web, production (for organization)
   ```

3. **Submit registration**
   - Server will appear with "New" status
   - Commission the server to discover hardware

### Server Commissioning

Commissioning discovers hardware capabilities and tests system functionality.

**Workflow: Commission a New Server**

1. **Select server from the list**
   - Status must be "New" or "Failed Testing"
   - Click on server hostname or use checkbox + Actions menu

2. **Configure commissioning options**
   ```
   ┌─ Commission Server Options ─────────────────┐
   │ ☑ Enable SSH Access                        │
   │ ☐ Skip Network Testing                     │
   │ ☑ Run Hardware Tests                       │
   │                                             │
   │ Testing Scripts:                            │
   │ ☑ smartctl-validate (HDD/SSD health)      │
   │ ☑ memtester (Memory testing)              │
   │ ☑ stress-ng-cpu-long (CPU stress test)    │
   │ ☐ network-validation (Network tests)       │
   │                                             │
   │ [Cancel] [Commission Server]                │
   └─────────────────────────────────────────────┘
   ```

3. **Monitor commissioning progress**
   - Job will be created and shown in Jobs section
   - Real-time progress updates via WebSocket
   - Typical duration: 10-30 minutes depending on tests

4. **Review commissioning results**
   - Server status changes to "Ready" when successful
   - Hardware specifications automatically populated
   - Failed tests show in server details with logs

### Server Deployment

Deploy operating systems and applications to commissioned servers.

**Workflow: Deploy Ubuntu Server**

1. **Select commissioned server**
   - Server status must be "Ready"
   - Click "Deploy" button or use Actions menu

2. **Choose deployment template**
   ```
   ┌─ Deploy Server Configuration ──────────────┐
   │ Template: [Docker Host ▼]                  │
   │           Base Server                      │
   │           Docker Host                      │
   │           Kubernetes Node                  │
   │           Database Server                  │
   │           Custom Template                  │
   │                                            │
   │ Hostname: web-server-01                    │
   │ OS Series: Ubuntu 24.04 LTS (jammy)       │
   │ Kernel: (Default)                          │
   │                                            │
   │ Tags: web, production                      │
   │                                            │
   │ Environment Variables:                     │
   │ DOCKER_VERSION: 24.0.7                    │
   │ NODE_ENV: production                       │
   │ TIMEZONE: UTC                              │
   │                                            │
   │ [Cancel] [Deploy Server]                   │
   └────────────────────────────────────────────┘
   ```

3. **Monitor deployment progress**
   - Deployment job created with estimated completion time
   - Progress bar shows current phase
   - Live log streaming available

4. **Verify deployment success**
   - Server status changes to "Deployed"
   - Agent automatically installs and registers
   - SSH access available using configured keys

### Server Lifecycle Management

**Release a Server**
1. Select deployed server → Actions → Release
2. Configure release options:
   ```
   ┌─ Release Server Options ────────────────────┐
   │ ☑ Secure Erase (recommended)              │
   │ ☐ Quick Erase (faster, less secure)      │
   │                                            │
   │ This will:                                 │
   │ • Power off the server                    │
   │ • Erase all data from disks               │
   │ • Remove deployment configuration         │
   │ • Return server to "Ready" status         │
   │                                            │
   │ [Cancel] [Release Server]                  │
   └────────────────────────────────────────────┘
   ```

**Power Management**
1. Select server → Actions → Power → [On/Off/Cycle/Query]
2. Confirm power action in dialog
3. Monitor power state change in server list

**Server Actions Summary**
| Status | Available Actions |
|--------|------------------|
| New | Commission, Delete |
| Commissioning | Cancel Job, View Logs |
| Ready | Deploy, Test, Delete |
| Deploying | Cancel Job, View Logs |
| Deployed | Release, Power Control, SSH |
| Broken | Commission, Delete |

---

## Template Management

Templates define the software stack and configuration applied during server deployment.

### Understanding Templates

**Template Components**
- **Cloud-init YAML**: System configuration and package installation
- **Variable definitions**: Customizable parameters
- **Metadata**: Description, compatibility, version information
- **Validation rules**: Input validation for variables

### Built-in Templates

**Base Server Template**
```yaml
#cloud-config
package_update: true
package_upgrade: true

packages:
  - curl
  - wget
  - git
  - python3-pip
  - htop
  - vim

users:
  - name: gough
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - "{{ SSH_PUBLIC_KEY }}"

timezone: "{{ TIMEZONE | default('UTC') }}"

runcmd:
  - systemctl enable ssh
  - systemctl start ssh
```

**Docker Host Template**
```yaml
#cloud-config
package_update: true
package_upgrade: true

packages:
  - docker.io
  - docker-compose
  - curl
  - wget

users:
  - name: gough
    groups: docker
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - "{{ SSH_PUBLIC_KEY }}"

runcmd:
  - systemctl enable docker
  - systemctl start docker
  - docker --version
  - usermod -aG docker ubuntu
```

### Creating Custom Templates

**Workflow: Create a Custom Template**

1. **Navigate to Templates → New Template**

2. **Fill in template metadata**:
   ```
   Name: Kubernetes Worker Node
   Description: Kubernetes worker node with kubeadm
   Category: Orchestration
   OS Series: jammy (Ubuntu 24.04)
   Architecture: amd64
   Version: 1.0
   ```

3. **Define template variables**:
   ```
   ┌─ Template Variables ────────────────────────┐
   │ Variable Name: K8S_VERSION                 │
   │ Type: String                               │
   │ Default: 1.28.0                            │
   │ Required: ☑                                │
   │ Description: Kubernetes version to install │
   │ Validation: ^1\.[0-9]+\.[0-9]+$           │
   │                                            │
   │ [Add Variable] [Remove]                    │
   └────────────────────────────────────────────┘
   ```

4. **Write cloud-init template**:
   ```yaml
   #cloud-config
   package_update: true
   
   apt:
     sources:
       kubernetes:
         source: "deb https://packages.cloud.google.com/apt/ kubernetes-xenial main"
         key: |
           -----BEGIN PGP PUBLIC KEY BLOCK-----
           [GPG key content]
           -----END PGP PUBLIC KEY BLOCK-----
   
   packages:
     - kubelet={{ K8S_VERSION }}-00
     - kubeadm={{ K8S_VERSION }}-00
     - kubectl={{ K8S_VERSION }}-00
     - docker.io
   
   runcmd:
     - apt-mark hold kubelet kubeadm kubectl
     - systemctl enable kubelet
     - systemctl enable docker
   ```

5. **Validate template**
   - Click "Validate Template" button
   - Review syntax errors and warnings
   - Fix any issues before saving

6. **Save and test**
   - Save template
   - Test deployment on a test server
   - Make adjustments as needed

### Template Management Best Practices

**Naming Convention**
- Use descriptive, hierarchical names
- Include version numbers for major changes
- Examples: `web-server-nginx-1.2`, `db-postgresql-15`

**Variable Design**
- Provide sensible defaults for all variables
- Use validation patterns for critical inputs
- Document variable purposes clearly

**Testing Strategy**
- Always test templates on development servers first
- Create test cases for different variable combinations
- Maintain separate dev/staging/prod template versions

---

## Job Monitoring

The job system handles all background operations including server commissioning, deployment, and maintenance tasks.

### Job Types and Lifecycle

**Job Types**
- **Commission**: Hardware discovery and testing
- **Deploy**: OS and application installation
- **Release**: Server cleanup and reset
- **Power**: Power management operations
- **Test**: Hardware validation scripts
- **Backup**: System backup operations

**Job States**
1. **Pending**: Queued for execution
2. **Running**: Currently executing
3. **Completed**: Finished successfully
4. **Failed**: Completed with errors
5. **Cancelled**: Manually cancelled

### Job Monitoring Interface

**Job List View**
```
┌─ Active Jobs ────────────────────────────────────────────┐
│ ID   │ Type    │ Server        │ Status  │ Progress │ ETA │
├──────┼─────────┼───────────────┼─────────┼──────────┼─────┤
│ 1234 │ Deploy  │ web-server-01 │ Running │ 65%      │ 8m  │
│ 1235 │ Commission│ db-server-02│ Running │ 30%      │15m  │
│ 1236 │ Release │ test-server-01│ Pending │ --       │ --  │
└──────┴─────────┴───────────────┴─────────┴──────────┴─────┘
```

**Job Detail View**
- Click on any job to view detailed information
- Real-time log streaming
- Progress breakdown by phase
- Resource usage during execution
- Error details and stack traces

### Workflow: Monitor Server Deployment

1. **Navigate to Jobs section**
   - Shows all jobs with real-time updates
   - Filter by status, type, or server

2. **Select deployment job**
   - Click on job ID or server name
   - View deployment phases and progress

3. **Monitor deployment phases**:
   ```
   Phase 1: Server Power-On        [✓] Completed (30s)
   Phase 2: PXE Boot              [✓] Completed (45s)
   Phase 3: Image Download        [✓] Completed (180s)
   Phase 4: OS Installation       [▶] Running (65% - 5m30s)
   Phase 5: Cloud-Init Execution  [ ] Pending
   Phase 6: Agent Installation    [ ] Pending
   Phase 7: Post-Deploy Testing   [ ] Pending
   ```

4. **View real-time logs**
   - Live log streaming in web interface
   - Color-coded log levels (Info/Warning/Error)
   - Search and filter capabilities

5. **Handle job issues**
   - Cancel running jobs if needed
   - Retry failed jobs with same parameters
   - Download job logs for analysis

---

## System Configuration

System configuration manages connections to external services and global settings.

### MaaS Integration Configuration

**Initial MaaS Setup**

1. **Navigate to Configuration → MaaS Settings**

2. **Configure connection parameters**:
   ```
   ┌─ MaaS Configuration ────────────────────────┐
   │ MaaS URL: http://maas:5240/MAAS/           │
   │ API Key: [Generate from MaaS admin panel]  │
   │                                            │
   │ Connection Status: ● Connected             │
   │ API Version: 2.9                           │
   │ Region Name: default                       │
   │                                            │
   │ [Test Connection] [Save Configuration]     │
   └────────────────────────────────────────────┘
   ```

3. **Configure network settings**:
   ```
   ┌─ Network Configuration ─────────────────────┐
   │ Default OS: Ubuntu 24.04 LTS (jammy)      │
   │ Default Kernel: (Latest)                   │
   │                                            │
   │ DNS Servers:                               │
   │ • 8.8.8.8                                  │
   │ • 8.8.4.4                                  │
   │                                            │
   │ NTP Servers:                               │
   │ • pool.ntp.org                             │
   │                                            │
   │ Proxy URL: (Optional)                      │
   │                                            │
   │ [Add DNS] [Add NTP] [Save]                 │
   └────────────────────────────────────────────┘
   ```

4. **Configure DHCP ranges** (if managing DHCP):
   ```
   ┌─ DHCP Configuration ────────────────────────┐
   │ Subnet: 192.168.1.0/24                     │
   │ Gateway: 192.168.1.1                       │
   │ Range Start: 192.168.1.100                 │
   │ Range End: 192.168.1.200                   │
   │                                            │
   │ ☑ Enable DHCP Management                   │
   │ ☑ Dynamic DNS Updates                      │
   │                                            │
   │ [Save DHCP Settings]                       │
   └────────────────────────────────────────────┘
   ```

### FleetDM Security Configuration

**FleetDM Integration Setup**

1. **Navigate to Configuration → Security Settings**

2. **Configure FleetDM connection**:
   ```
   ┌─ FleetDM Configuration ─────────────────────┐
   │ Fleet URL: https://fleetdm:8443            │
   │ API Token: [Generate from Fleet admin]    │
   │ Enrollment Secret: [Auto-generated]       │
   │                                            │
   │ Connection Status: ● Connected             │
   │ Fleet Version: 4.21.0                     │
   │                                            │
   │ Certificate Validation: ☑ Enabled         │
   │ Auto-Enrollment: ☑ Enabled                │
   │                                            │
   │ [Test Connection] [Save Configuration]     │
   └────────────────────────────────────────────┘
   ```

3. **Configure security policies**:
   ```
   Security Monitoring Settings:
   ☑ Enable automatic OSQuery deployment
   ☑ Collect system inventory data
   ☑ Monitor process execution
   ☑ Track network connections
   ☐ Enable file integrity monitoring
   ☑ Log security events
   
   Alert Thresholds:
   • Failed login attempts: 5 per hour
   • Suspicious process execution: Immediate
   • Unauthorized file changes: 10 per hour
   • Network anomalies: 5 per hour
   ```

### Global System Settings

**System Preferences**

1. **Navigate to Configuration → System Settings**

2. **Configure global preferences**:
   ```
   ┌─ System Configuration ──────────────────────┐
   │ System Name: Gough Production              │
   │ Time Zone: UTC                             │
   │ Date Format: YYYY-MM-DD                    │
   │ Language: English (US)                     │
   │                                            │
   │ Session Settings:                          │
   │ • Session timeout: 8 hours                 │
   │ • Remember login: ☑ Enabled               │
   │ • Multi-session: ☑ Allowed                │
   │                                            │
   │ Notification Settings:                     │
   │ • Email notifications: ☑ Enabled          │
   │ • Webhook notifications: ☑ Enabled        │
   │ • Slack integration: ☐ Disabled           │
   │                                            │
   │ [Save Settings]                            │
   └────────────────────────────────────────────┘
   ```

---

## User Management

User management provides role-based access control and user account administration.

### User Roles and Permissions

**Role Hierarchy**
1. **Administrator**: Full system access
   - Manage all servers and jobs
   - Configure system settings
   - Manage user accounts
   - Access security features

2. **Operator**: Day-to-day operations
   - Deploy and manage servers
   - Create and modify templates
   - Monitor jobs and system status
   - Limited configuration access

3. **Viewer**: Read-only access
   - View server status and information
   - Monitor job progress
   - Access logs and reports
   - No modification capabilities

**Permission Matrix**
| Feature | Administrator | Operator | Viewer |
|---------|---------------|----------|--------|
| View Dashboard | ✓ | ✓ | ✓ |
| Manage Servers | ✓ | ✓ | ✗ |
| Deploy/Release | ✓ | ✓ | ✗ |
| Manage Templates | ✓ | ✓ | ✗ |
| View Jobs | ✓ | ✓ | ✓ |
| Cancel Jobs | ✓ | ✓ | ✗ |
| System Config | ✓ | Limited | ✗ |
| User Management | ✓ | ✗ | ✗ |
| Security Settings | ✓ | ✗ | ✗ |

### User Account Management

**Workflow: Create New User Account**

1. **Navigate to Users → Add User**
   - Available to Administrators only

2. **Fill in user information**:
   ```
   ┌─ Create User Account ───────────────────────┐
   │ Email: operator@company.com                │
   │ Full Name: Jane Operator                   │
   │ Role: [Operator ▼]                         │
   │                                            │
   │ Initial Password:                          │
   │ [Generate Random] [Set Custom]             │
   │                                            │
   │ Account Settings:                          │
   │ ☑ Require password change on first login  │
   │ ☑ Enable email notifications              │
   │ ☐ Account expires (set date)              │
   │                                            │
   │ Additional Permissions (Optional):         │
   │ ☐ Template management                      │
   │ ☐ Job cancellation                        │
   │ ☐ Configuration viewing                    │
   │                                            │
   │ [Cancel] [Create User]                     │
   └────────────────────────────────────────────┘
   ```

3. **Send welcome email**
   - System automatically sends login credentials
   - Include getting started guide
   - Provide support contact information

**User Profile Management**

Users can manage their own profiles:

1. **Access profile via user menu** (top-right corner)

2. **Update personal information**:
   ```
   ┌─ User Profile ──────────────────────────────┐
   │ Email: user@company.com (read-only)        │
   │ Full Name: John User                       │
   │ Phone: +1-555-0123                         │
   │ Department: IT Operations                  │
   │                                            │
   │ Change Password:                           │
   │ Current Password: [••••••••]               │
   │ New Password: [••••••••]                   │
   │ Confirm Password: [••••••••]               │
   │                                            │
   │ Notification Preferences:                  │
   │ ☑ Job completion notifications             │
   │ ☑ System alert notifications              │
   │ ☐ Weekly summary reports                   │
   │                                            │
   │ [Save Changes]                             │
   └────────────────────────────────────────────┘
   ```

3. **API key management**:
   ```
   ┌─ API Keys ──────────────────────────────────┐
   │ Active API Keys:                           │
   │                                            │
   │ Key: gough_****************************    │
   │ Created: 2023-12-01 10:30:00               │
   │ Last Used: 2023-12-07 09:15:00             │
   │ [Regenerate] [Revoke]                      │
   │                                            │
   │ [Generate New API Key]                     │
   └────────────────────────────────────────────┘
   ```

---

## Monitoring and Alerts

Comprehensive monitoring provides visibility into system health and performance.

### System Health Monitoring

**Component Health Dashboard**
- Database connectivity and performance
- MaaS server responsiveness
- FleetDM integration status
- Container resource utilization
- Network connectivity status

**Performance Metrics**
- API response times
- Job processing throughput
- Database query performance
- Memory and CPU utilization
- Disk space and I/O metrics

### Alert Configuration

**Alert Types and Severities**

1. **Critical Alerts** (Immediate attention required)
   - System component failures
   - Security breaches detected
   - Data corruption events
   - Service outages

2. **Warning Alerts** (Action required soon)
   - High resource utilization
   - Job failures
   - Performance degradation
   - Certificate expiration warnings

3. **Info Alerts** (Informational)
   - Successful deployments
   - System maintenance notices
   - Configuration changes
   - User activity summaries

**Workflow: Configure Alert Rules**

1. **Navigate to Monitoring → Alert Rules**

2. **Create new alert rule**:
   ```
   ┌─ Create Alert Rule ─────────────────────────┐
   │ Rule Name: High CPU Usage                  │
   │ Description: Alert when CPU > 80%          │
   │                                            │
   │ Condition:                                 │
   │ Metric: system.cpu.usage                   │
   │ Operator: Greater Than                     │
   │ Threshold: 80                              │
   │ Duration: 5 minutes                        │
   │                                            │
   │ Severity: Warning                          │
   │                                            │
   │ Notification Methods:                      │
   │ ☑ Email: admin@company.com                 │
   │ ☑ Webhook: https://hooks.slack.com/...     │
   │ ☐ SMS: +1-555-0123                         │
   │                                            │
   │ [Test Alert] [Save Rule]                   │
   └────────────────────────────────────────────┘
   ```

3. **Configure notification settings**:
   ```
   Notification Settings:
   • Email template: High Priority Alert
   • Escalation: After 30 minutes if not acknowledged
   • Quiet hours: 22:00 - 06:00 (reduce non-critical alerts)
   • Alert grouping: Group similar alerts within 10 minutes
   • Auto-resolution: Clear alerts when conditions resolve
   ```

### Log Management and Analysis

**Centralized Logging**
- Application logs from management server
- MaaS server operational logs
- FleetDM security event logs
- Agent communication logs
- System audit trails

**Log Analysis Tools**
1. **Search and filtering**
   - Full-text search across all logs
   - Time-based filtering
   - Component-based filtering
   - Log level filtering

2. **Log aggregation and reporting**
   - Daily/weekly/monthly summaries
   - Error trend analysis
   - Performance baseline reports
   - Security event summaries

---

## Workflows and Best Practices

This section covers common workflows and operational best practices for using Gough effectively.

### Server Provisioning Workflow

**Standard Provisioning Process**

1. **Pre-provisioning checklist**
   - [ ] Network connectivity verified
   - [ ] IPMI/power management configured
   - [ ] Appropriate template selected
   - [ ] Resource requirements confirmed
   - [ ] Security policies reviewed

2. **Provisioning steps**
   ```
   Step 1: Server Discovery
   ├─ Physical server powered on
   ├─ PXE boot initiated
   ├─ MaaS detects new hardware
   └─ Server appears in "New" status
   
   Step 2: Commissioning
   ├─ Select server in portal
   ├─ Configure commissioning options
   ├─ Monitor progress (10-30 minutes)
   └─ Verify "Ready" status
   
   Step 3: Deployment
   ├─ Select appropriate template
   ├─ Configure deployment parameters
   ├─ Monitor deployment progress
   └─ Verify successful completion
   
   Step 4: Post-deployment
   ├─ Verify agent connectivity
   ├─ Test application functionality
   ├─ Configure monitoring
   └─ Document deployment details
   ```

3. **Post-deployment validation**
   - SSH connectivity test
   - Application service verification
   - Security agent enrollment
   - Monitoring system integration

### Template Development Workflow

**Best Practices for Template Creation**

1. **Planning phase**
   - Define requirements clearly
   - Identify required packages and services
   - Plan variable parameterization
   - Consider security requirements

2. **Development phase**
   - Start with base template
   - Add packages incrementally
   - Test each addition
   - Document all variables

3. **Testing phase**
   - Test on development servers first
   - Validate with different variable combinations
   - Performance testing under load
   - Security scanning

4. **Deployment phase**
   - Staging environment deployment
   - User acceptance testing
   - Production rollout planning
   - Rollback procedure preparation

### Operational Procedures

**Daily Operations Checklist**

- [ ] Review system health dashboard
- [ ] Check for failed jobs and investigate
- [ ] Monitor resource utilization trends
- [ ] Review security alerts and events
- [ ] Verify backup completion status
- [ ] Update deployment documentation

**Weekly Operations Tasks**

- [ ] Review system performance metrics
- [ ] Analyze job completion trends
- [ ] Update templates with security patches
- [ ] Review user access and permissions
- [ ] Perform system maintenance tasks
- [ ] Test disaster recovery procedures

**Monthly Operations Tasks**

- [ ] Comprehensive system audit
- [ ] Capacity planning review
- [ ] Security policy updates
- [ ] User training and documentation updates
- [ ] Vendor software updates
- [ ] Compliance reporting

### Security Best Practices

**Access Control**
- Use principle of least privilege
- Regular access reviews and cleanup
- Strong password policies
- Multi-factor authentication where possible
- API key rotation schedule

**System Security**
- Keep all components updated
- Regular security scanning
- Network segmentation
- Encrypted communications
- Audit trail maintenance

**Operational Security**
- Change management procedures
- Configuration backup and versioning
- Incident response procedures
- Regular security training
- Compliance monitoring

---

## Troubleshooting

Common issues and their solutions for the Gough Management Portal.

### Login and Authentication Issues

**Problem: Cannot login to portal**

*Symptoms:*
- Login page displays "Invalid credentials"
- User account exists but login fails

*Solutions:*
1. **Check user account status**
   ```bash
   # Connect to management server container
   docker exec -it gough-management-server bash
   
   # Check user in database
   python manage.py shell
   >>> from models import User
   >>> user = User.get(User.email == 'user@company.com')
   >>> print(user.is_active, user.last_login)
   ```

2. **Reset user password**
   ```bash
   # Reset password via command line
   python manage.py reset-password user@company.com
   ```

3. **Check system logs**
   ```bash
   # View authentication logs
   docker logs gough-management-server | grep "auth"
   ```

**Problem: Session expires quickly**

*Solutions:*
1. Check session configuration in Settings → System
2. Verify Redis connectivity for session storage
3. Check browser cookie settings

### Server Discovery Issues

**Problem: Servers not appearing after PXE boot**

*Symptoms:*
- Physical server boots but not visible in portal
- MaaS shows server but Gough doesn't sync

*Solutions:*
1. **Verify MaaS connection**
   - Configuration → MaaS Settings → Test Connection
   - Check API credentials and URL

2. **Check network configuration**
   ```bash
   # Verify DHCP range and DNS settings
   # Ensure PXE network is properly configured
   # Check VLAN and network connectivity
   ```

3. **Manual server registration**
   - Use Servers → Add Server for manual registration
   - Enter MAC address and power management details

### Job Execution Problems

**Problem: Jobs stuck in "Pending" status**

*Solutions:*
1. **Check Celery workers**
   ```bash
   # Check worker status
   docker exec -it gough-management-server celery inspect active
   
   # Restart workers if needed
   docker-compose restart celery-worker
   ```

2. **Check Redis connectivity**
   ```bash
   # Test Redis connection
   docker exec -it gough-redis redis-cli ping
   ```

3. **Review job queue**
   ```bash
   # Check queue depth
   docker exec -it gough-management-server celery inspect reserved
   ```

**Problem: Deployment fails during cloud-init**

*Symptoms:*
- Server reaches "Deployed" status but agent doesn't connect
- Cloud-init logs show errors

*Solutions:*
1. **Check template syntax**
   - Use Template → Validate function
   - Review cloud-init documentation

2. **Check network connectivity**
   - Verify server can reach management portal
   - Check firewall rules

3. **Review deployment logs**
   - Jobs → Select failed job → View logs
   - Check cloud-init logs on target server

### Performance Issues

**Problem: Portal responds slowly**

*Solutions:*
1. **Check system resources**
   ```bash
   # Monitor container resource usage
   docker stats
   
   # Check database performance
   docker exec -it gough-postgresql pg_stat_activity
   ```

2. **Database optimization**
   ```bash
   # Run database maintenance
   docker exec -it gough-postgresql vacuumdb -a
   
   # Update statistics
   docker exec -it gough-postgresql analyze
   ```

3. **Cache optimization**
   ```bash
   # Clear Redis cache
   docker exec -it gough-redis redis-cli flushall
   ```

### Integration Issues

**Problem: FleetDM integration not working**

*Solutions:*
1. **Verify FleetDM configuration**
   - Configuration → Security Settings → Test Connection
   - Check API token and enrollment secret

2. **Check network connectivity**
   ```bash
   # Test FleetDM API connectivity
   curl -k -H "Authorization: Bearer <token>" \
        https://fleetdm:8443/api/v1/fleet/hosts
   ```

3. **Review security logs**
   - Check FleetDM logs for enrollment errors
   - Verify certificate configuration

### Emergency Procedures

**System Recovery**

1. **Container restart**
   ```bash
   # Restart all services
   docker-compose down
   docker-compose up -d
   ```

2. **Database recovery**
   ```bash
   # Restore from backup
   docker exec -i gough-postgresql psql -U gough < backup.sql
   ```

3. **Configuration reset**
   ```bash
   # Reset to default configuration
   docker exec -it gough-management-server python manage.py reset-config
   ```

**Support Contacts**

- **Technical Support**: support@penguintech.io
- **Documentation**: https://github.com/penguintechinc/gough/docs
- **Community Forum**: https://community.penguintech.io
- **Emergency Contact**: +1-555-PENGUIN

---

This comprehensive user guide provides detailed instructions for all aspects of the Gough Management Portal. For additional support or to report issues, please refer to the project documentation or contact support.