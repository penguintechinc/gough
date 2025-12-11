# Gough Hypervisor Automation System - Claude Code Context

## Project Overview

Gough is a comprehensive hypervisor automation system that uses Ubuntu MaaS, custom Python 3.12 services, and Ansible to netboot hardware with Ubuntu 24.04 LTS. Named after Gough Island - home to penguins and providing them structure, just as this hypervisor gives the entire ecosystem a place to call home.

**Repository**: https://github.com/penguintechinc/gough  
**Company**: Penguin Tech Inc (sales@penguintech.io)  
**Licensing**: AGPL 3.0 for personal/internal use, enterprise license required for commercial use  
**Enterprise Pricing**: $5/compute node per month (discounts starting at 100+ nodes)  

## System Architecture

### Container Architecture
1. **MaaS Container** - Ubuntu MaaS server for PXE boot and bare metal provisioning
2. **Gough Management Server** - Custom py4web portal for hypervisor configuration and orchestration
3. **Gough Agent Container** - Custom monitoring and management agent deployed to servers
4. **FleetDM Server** - OSQuery fleet management for security monitoring

## Implementation Status - COMPLETED ✅

All 11 phases of the Gough hypervisor automation system have been successfully implemented:

### ✅ Phase 1-2: Foundation & MaaS (Completed)
- Project structure and environment configuration
- MaaS container with PostgreSQL, DHCP, DNS, and PXE boot capabilities
- Ubuntu 24.04 LTS image management and API integration

### ✅ Phase 3-4: Management Server & Agent (Completed)
- Complete py4web application with Python 3.12
- Dashboard, MaaS integration, configuration management, user management
- Cloud-init template system with dynamic rendering
- Lightweight agent container with monitoring and management capabilities

### ✅ Phase 5-6: FleetDM & Ansible Integration (Completed)
- FleetDM server with MySQL backend and Redis for live queries
- OSQuery agent deployment with automatic enrollment
- Complete Ansible orchestration with playbooks and roles
- MaaS dynamic inventory system

### ✅ Phase 7-8: Infrastructure & Development (Completed)
- Docker Compose configuration with networking and volumes
- MaaS API wrapper library with async capabilities
- REST API endpoints and background job processing with Celery
- Webhook handlers, template processing engine, and logging aggregation
- JWT/OAuth2 authentication, TLS certificates, HashiCorp Vault integration

### ✅ Phase 9: Testing & Validation (Completed)
- Comprehensive test framework with 4,000+ lines of test code
- Unit tests, integration tests, and performance tests
- 80%+ code coverage with automated CI/CD pipeline
- Docker-based test environment with all services

### ✅ Phase 10: Documentation & Deployment (Completed)
- Complete system architecture documentation with Mermaid diagrams
- OpenAPI 3.0.3 specification with 50+ documented endpoints
- Comprehensive user guide (850+ lines)
- Ansible playbook documentation (1,000+ lines)
- Troubleshooting guide (800+ lines)
- Security best practices (1,200+ lines)
- Production deployment checklist (1,500+ lines)
- Backup/restore procedures (1,000+ lines)
- Prometheus/Grafana monitoring stack
- ELK logging aggregation with Filebeat

### ✅ Phase 11: Production Readiness (Completed)
- MaaS HA configuration with multi-node setup
- PostgreSQL master-slave replication
- HAProxy/Nginx load balancing
- Kubernetes deployment manifests and Helm charts
- PagerDuty integration with escalation policies
- Automated Ubuntu image updates
- Container rolling update workflows
- Database maintenance automation
- Log rotation configuration

## Key Features Delivered

### 🔧 **Enterprise Automation**
- Bare metal provisioning via MaaS API
- Dynamic package installation through cloud-init
- Automatic agent deployment to provisioned servers
- Comprehensive Ansible orchestration

### 🖥️ **Management Interface**
- Professional py4web portal with responsive UI
- Real-time server inventory management
- Template-based cloud-init configuration
- Job monitoring and deployment tracking
- User management with RBAC

### 🛡️ **Security & Monitoring**
- FleetDM integration with OSQuery agents
- 30+ predefined security and system monitoring queries
- JWT/OAuth2 authentication with audit logging
- TLS encryption and HashiCorp Vault secrets management
- Comprehensive alerting with PagerDuty integration

### 📊 **Observability**
- Prometheus metrics collection
- Grafana dashboards for system visualization
- ELK stack for centralized logging
- Real-time status monitoring and alerting
- Performance metrics and health checks

### 🏗️ **Production Ready**
- High availability with automatic failover
- Database replication and backup procedures
- Load balancing and service discovery
- Kubernetes orchestration with Helm charts
- 99.9% uptime target support

## Technical Stack

### **Languages & Frameworks**
- Python 3.12 (Management Server, API, Automation)
- py4web (Web Framework)
- JavaScript/HTML/CSS (Frontend)
- YAML (Configuration, Ansible, Docker Compose)
- SQL (PostgreSQL, MySQL)

### **Infrastructure**
- Docker & Docker Compose
- Kubernetes with Helm
- Ubuntu MaaS 3.x
- PostgreSQL (primary database)
- MySQL (FleetDM)
- Redis (caching, sessions, job queue)
- Elasticsearch (logging)

### **Automation & Monitoring**
- Ansible (orchestration)
- Celery (background jobs)
- FleetDM + OSQuery (endpoint monitoring)
- Prometheus + Grafana (metrics)
- ELK Stack (logging)
- HAProxy/Nginx (load balancing)

### **Security**
- HashiCorp Vault (secrets management)
- Let's Encrypt + self-signed certificates
- JWT/OAuth2 authentication
- RBAC with audit logging
- Network segmentation

## Project Structure

```
gough/
├── containers/
│   ├── maas/                    # MaaS server container
│   ├── management-server/       # py4web management portal
│   ├── agent/                   # Monitoring agent container
│   └── fleetdm/                # FleetDM server container
├── ansible/
│   ├── playbooks/              # Orchestration playbooks
│   ├── roles/                  # Ansible roles
│   └── inventory/              # Dynamic inventory scripts
├── cloud-init/
│   └── templates/              # Cloud-init templates
├── k8s/
│   ├── deployments/            # Kubernetes manifests
│   └── helm/                   # Helm charts
├── config/
│   ├── monitoring/             # Prometheus/Grafana config
│   ├── logging/                # ELK stack configuration
│   └── ha/                     # High availability config
├── scripts/
│   ├── monitoring/             # Monitoring scripts
│   ├── database-maintenance/   # DB maintenance scripts
│   └── image-management/       # Ubuntu image management
├── tests/                      # Comprehensive test suite
├── docs/                       # Complete documentation
├── docker-compose.yml          # Main service configuration
└── .github/workflows/          # CI/CD pipeline
```

## Success Criteria - ALL MET ✅

1. ✅ **Automated Provisioning**: Netboot and provision Ubuntu 24.04 LTS on bare metal
2. ✅ **Web Management**: Functional py4web portal for configuration and monitoring
3. ✅ **Package Management**: Dynamic package installation via cloud-init
4. ✅ **Agent Deployment**: Automatic agent container deployment to provisioned servers
5. ✅ **Fleet Monitoring**: Working FleetDM server with OSQuery agents reporting
6. ✅ **API Integration**: Full MaaS API integration with management portal
7. ✅ **Scalability**: Support for provisioning 100+ servers concurrently
8. ✅ **Security**: TLS encryption, authentication, and audit logging
9. ✅ **Documentation**: Complete documentation for all components (7,650+ lines)
10. ✅ **Testing**: 80%+ code coverage and passing integration tests

## Deployment Instructions

### Quick Start (Development)
```bash
cd gough/
cp .env.example .env
# Edit .env with your configuration
docker-compose up -d
```

### Production Deployment
```bash
# High Availability Setup
docker-compose -f docker-compose.yml -f config/ha/docker-compose.ha.yml up -d

# With Monitoring & Logging
docker-compose --profile monitoring --profile logging up -d

# Kubernetes Deployment
helm install gough ./k8s/helm/gough/
```

### Health Check
```bash
./scripts/monitoring/health-check.sh
```

## API Documentation

Complete OpenAPI 3.0.3 specification available at:
- **Local**: http://localhost:8000/docs
- **File**: `/docs/api/openapi-spec.yaml`

## Key Management Commands

```bash
# Test the system
./scripts/run_tests.sh

# Deploy monitoring stack
./scripts/monitoring/deploy-stack.sh --all

# Update Ubuntu images
./scripts/image-management/ubuntu-image-updater.sh

# Database maintenance
./scripts/database-maintenance/database-maintenance.sh

# Rolling container updates
./scripts/container-updates/rolling-update-manager.sh
```

## Documentation

Comprehensive documentation available in `/docs/`:
- System Architecture (`/docs/architecture/`)
- API Documentation (`/docs/api/`)
- User Guide (`/docs/user-guide/`)
- Ansible Documentation (`/docs/ansible/`)
- Troubleshooting (`/docs/troubleshooting/`)
- Security Best Practices (`/docs/security/`)
- Deployment Procedures (`/docs/deployment/`)
- Operations Manual (`/docs/operations/`)

## Enterprise Features

- **Multi-tenant**: Role-based access control with audit logging
- **High Availability**: Multi-node setup with automatic failover
- **Scalability**: Kubernetes-ready, supports 100+ servers
- **Security**: End-to-end encryption, secrets management, security monitoring
- **Monitoring**: Comprehensive observability with alerting
- **Compliance**: Audit logging, data retention, security hardening

## Critical Development Rules

### Development Philosophy: Safe, Stable, and Feature-Complete

**NEVER take shortcuts or the "easy route" - ALWAYS prioritize safety, stability, and feature completeness**

#### Core Principles
- **No Quick Fixes**: Resist quick workarounds or partial solutions
- **Complete Features**: Fully implemented with proper error handling and validation
- **Safety First**: Security, data integrity, and fault tolerance are non-negotiable
- **Stable Foundations**: Build on solid, tested components
- **Future-Proof Design**: Consider long-term maintainability and scalability
- **No Technical Debt**: Address issues properly the first time

#### Red Flags (Never Do These)
- Skipping input validation "just this once"
- Hardcoding credentials or configuration
- Ignoring error returns or exceptions
- Commenting out failing tests to make CI pass
- Deploying without proper testing
- Using deprecated or unmaintained dependencies
- Implementing partial features with "TODO" placeholders
- Bypassing security checks for convenience
- Assuming data is valid without verification
- Leaving debug code or backdoors in production

#### Quality Checklist Before Completion
- All error cases handled properly
- Unit tests cover all code paths
- Integration tests verify component interactions
- Security requirements fully implemented
- Performance meets acceptable standards
- Documentation complete and accurate
- Code review standards met
- No hardcoded secrets or credentials
- Logging and monitoring in place
- Build passes in containerized environment
- No security vulnerabilities in dependencies
- Edge cases and boundary conditions tested

### Git Workflow
- **NEVER commit automatically** unless explicitly requested by the user
- **NEVER push to remote repositories** under any circumstances
- **ONLY commit when explicitly asked** - never assume commit permission
- Always use feature branches for development
- Require pull request reviews for main branch
- Automated testing must pass before merge

### Local State Management (Crash Recovery)
- **ALWAYS maintain local .PLAN and .TODO files** for crash recovery
- **Keep .PLAN file updated** with current implementation plans and progress
- **Keep .TODO file updated** with task lists and completion status
- **Update these files in real-time** as work progresses
- **Add to .gitignore**: Both .PLAN and .TODO files must be in .gitignore
- **File format**: Use simple text format for easy recovery
- **Automatic recovery**: Upon restart, check for existing files to resume work

### Dependency Security Requirements
- **ALWAYS check for Dependabot alerts** before every commit
- **Monitor vulnerabilities via Socket.dev** for all dependencies
- **Mandatory security scanning** before any dependency changes
- **Fix all security alerts immediately** - no commits with outstanding vulnerabilities
- **Regular security audits**: `pip-audit`, `safety check`, `npm audit`

### Linting & Code Quality Requirements
- **ALL code must pass linting** before commit - no exceptions
- **Python**: flake8, black, isort, mypy (type checking), bandit (security)
- **JavaScript/TypeScript**: ESLint, Prettier
- **Ansible**: ansible-lint
- **Docker**: hadolint
- **YAML**: yamllint
- **Markdown**: markdownlint
- **Shell**: shellcheck
- **CodeQL**: All code must pass CodeQL security analysis
- **PEP Compliance**: Python code must follow PEP 8, PEP 257 (docstrings), PEP 484 (type hints)

### Build & Deployment Requirements
- **NEVER mark tasks as completed until successful build verification**
- All Python builds MUST be executed within Docker containers
- Use containerized builds for local development and CI/CD pipelines
- Build failures must be resolved before task completion

### Documentation Standards
- **README.md**: Keep as overview and pointer to comprehensive docs/ folder
- **docs/ folder**: Create comprehensive documentation for all aspects
- **RELEASE_NOTES.md**: Maintain in docs/ folder, prepend new version releases to top
- Update CLAUDE.md when adding significant context
- **Build status badges**: Always include in README.md
- **ASCII art**: Include catchy, project-appropriate ASCII art in README
- **Company homepage**: Point to www.penguintech.io
- **License**: All projects use Limited AGPL3 with preamble for fair use

### File Size Limits
- **Maximum file size**: 25,000 characters for ALL code and markdown files
- **Split large files**: Decompose into modules, libraries, or separate documents
- **CLAUDE.md exception**: Maximum 39,000 characters (only exception to 25K rule)
- **High-level approach**: CLAUDE.md contains high-level context and references detailed docs
- **Documentation strategy**: Create detailed documentation in `docs/` folder and link to them from CLAUDE.md

## Version Management System

**Format**: `vMajor.Minor.Patch.build`
- **Major**: Breaking changes, API changes, removed features
- **Minor**: Significant new features and functionality additions
- **Patch**: Minor updates, bug fixes, security patches
- **Build**: Epoch64 timestamp of build time

**Update Commands**:
```bash
./scripts/version/update-version.sh          # Increment build timestamp
./scripts/version/update-version.sh patch    # Increment patch version
./scripts/version/update-version.sh minor    # Increment minor version
./scripts/version/update-version.sh major    # Increment major version
```

## PenguinTech License Server Integration

All projects integrate with the centralized PenguinTech License Server at `https://license.penguintech.io` for feature gating and enterprise functionality.

**IMPORTANT: License enforcement is ONLY enabled when project is marked as release-ready**
- Development phase: All features available, no license checks
- Release phase: License validation required, feature gating active

**License Key Format**: `PENG-XXXX-XXXX-XXXX-XXXX-ABCD`

**Core Endpoints**:
- `POST /api/v2/validate` - Validate license
- `POST /api/v2/features` - Check feature entitlements
- `POST /api/v2/keepalive` - Report usage statistics

**Environment Variables**:
```bash
# License configuration
LICENSE_KEY=PENG-XXXX-XXXX-XXXX-XXXX-ABCD
LICENSE_SERVER_URL=https://license.penguintech.io
PRODUCT_NAME=gough

# Release mode (enables license enforcement)
RELEASE_MODE=false  # Development (default)
RELEASE_MODE=true   # Production (explicitly set)
```

**License Management Commands**:
```bash
make license-validate        # Validate license
make license-check-features  # Check available features
make license-debug           # Test license server connectivity
```

## CI/CD & Workflows

Gough implements `.WORKFLOW` compliance with comprehensive GitHub Actions pipelines.

### Workflow Overview

Three core workflows manage continuous integration, testing, building, and releasing:

1. **build-containers.yml** - Multi-arch container builds for all four services
2. **tests.yml** - Comprehensive testing pipeline (unit, integration, E2E, security)
3. **version-release.yml** - Automated GitHub releases on version updates

### Version Management

Version control is managed via the `.version` file in repository root:

```bash
# Example: /home/penguin/code/Gough/.version
1.2.3
```

**Versioning**: Semantic versioning (Major.Minor.Patch)
- Update `.version` and commit to trigger releases and version builds
- Workflows auto-detect version changes and generate appropriate tags
- GitHub Actions creates pre-releases automatically

### Build Process

Container builds use:
- **Epoch64 timestamps** for build identification (`alpha-<epoch64>`, `beta-<epoch64>`)
- **Path filters** to trigger only on relevant changes
- **Multi-arch builds** (linux/amd64, linux/arm64)
- **Security scanning** (Trivy, CodeQL)
- **SBOM generation** (Software Bill of Materials)

### Container Services

All four Gough services built in parallel via matrix strategy:

| Service | Registry | Multi-arch |
|---------|----------|-----------|
| MaaS | ghcr.io/.../gough-maas | amd64/arm64 |
| Management Server | ghcr.io/.../gough-management-server | amd64/arm64 |
| Agent | ghcr.io/.../gough-agent | amd64/arm64 |
| FleetDM | ghcr.io/.../gough-fleetdm | amd64/arm64 |

### Testing Pipeline

Tests run on every push and PR with:
- Unit tests (multiple Python versions: 3.8-3.11)
- Integration tests (PostgreSQL, Redis services)
- E2E tests (full docker-compose environment)
- Performance tests (scheduled)
- Security tests (Trivy, CodeQL)
- Code quality (black, flake8, mypy, pylint, bandit, safety)

### Documentation

Complete workflow and CI/CD documentation:

- **`/docs/WORKFLOWS.md`** - Detailed workflow behavior, triggers, jobs
- **`/docs/STANDARDS.md`** - Development standards, version management, CI/CD best practices
- **`/docs/CLAUDE.md`** - This file (project context and overview)

### Quick Start

**Running tests locally**:
```bash
./scripts/run_tests.sh          # All tests
./scripts/run_tests.sh -t unit  # Unit tests only
```

**Building containers**:
```bash
docker build -t gough-maas gough/containers/maas/
docker buildx build --platform linux/amd64,linux/arm64 \
  -t gough-maas gough/containers/maas/
```

**Releasing new version**:
```bash
echo "1.2.4" > .version
git add .version
git commit -m "Release v1.2.4"
git push  # Workflows auto-create release and build version-tagged images
```

### Pre-Commit Checklist

Before pushing code:
- [ ] All tests pass: `./scripts/run_tests.sh`
- [ ] Linting passes: `black`, `flake8`, `mypy`
- [ ] Security scans clean: `bandit`, `safety`
- [ ] No hardcoded secrets
- [ ] Documentation updated
- [ ] `.version` updated (if releasing new version)

### WaddleAI Integration (Optional)

For AI-powered features, integrate with WaddleAI located at `~/code/WaddleAI`.

**When to Use WaddleAI:**
- Natural language processing for log analysis
- Intelligent alerting and anomaly detection
- AI-powered infrastructure recommendations
- Predictive maintenance capabilities

**Integration Pattern:**
- WaddleAI runs as separate microservice container
- Communicate via REST API or gRPC
- Environment variable configuration for API endpoints
- License-gate AI features as enterprise functionality

## Support & Contact

- **Sales**: sales@penguintech.io
- **Company Homepage**: www.penguintech.io
- **Documentation**: Complete docs in `/docs/` directory
- **Issues**: GitHub Issues (when repository is public)
- **Enterprise Support**: Available with license
- **License Server Status**: https://status.penguintech.io

---

**Version**: 1.1.0
**Last Updated**: 2025-12-11
**Maintained by**: Penguin Tech Inc
**License Server**: https://license.penguintech.io

*Gough Hypervisor Automation System - Providing structure and home to your infrastructure ecosystem, just like Gough Island provides for its penguin community.*