# Project Template - Claude Code Context

## Project Overview

This is a comprehensive project template incorporating best practices and patterns from Penguin Tech Inc projects. It provides a standardized foundation for multi-language projects with enterprise-grade infrastructure and integrated licensing.

**Template Features:**
- Multi-language support (Go 1.24.x, Python 3.12/3.13, Node.js 18+)
- Enterprise security and licensing integration
- Comprehensive CI/CD pipeline
- Production-ready containerization
- Monitoring and observability
- Version management system
- PenguinTech License Server integration

## Technology Stack

### Languages & Frameworks

**Language Selection Criteria (Case-by-Case Basis):**
- **Python 3.13**: Default choice for most applications
  - Web applications and APIs
  - Business logic and data processing
  - Integration services and connectors
- **Go 1.24.x**: ONLY for high-traffic/performance-critical applications
  - Applications handling >10K requests/second
  - Network-intensive services
  - Low-latency requirements (<10ms)
  - CPU-bound operations requiring maximum throughput

**Python Stack:**
- **Python**: 3.13 for all applications (3.12+ minimum)
- **Web Framework**: Flask + Flask-Security-Too (mandatory)
- **Database Libraries** (mandatory for all Python applications):
  - **SQLAlchemy**: Database initialization and schema creation only
  - **PyDAL**: Runtime database operations and migrations
- **Performance**: Dataclasses with slots, type hints, async/await required

**Frontend Stack:**
- **React**: ReactJS for all frontend applications
- **Node.js**: 18+ for build tooling and React development
- **JavaScript/TypeScript**: Modern ES2022+ standards

**Go Stack (When Required):**
- **Go**: 1.24.x (latest patch version, minimum 1.24.2)
- **Database**: Use DAL with PostgreSQL/MySQL cross-support (e.g., GORM, sqlx)
- Use only for traffic-intensive applications

### Infrastructure & DevOps
- **Containers**: Docker with multi-stage builds, Docker Compose
- **Orchestration**: Kubernetes with Helm charts
- **Configuration Management**: Ansible for infrastructure automation
- **CI/CD**: GitHub Actions with comprehensive pipelines
- **Monitoring**: Prometheus metrics, Grafana dashboards
- **Logging**: Structured logging with configurable levels

### Databases & Storage
- **Primary**: PostgreSQL (default, configurable via `DB_TYPE` environment variable)
- **Cache**: Redis/Valkey with optional TLS and authentication
- **Supported Databases** (ALL must be supported by default):
  - **PostgreSQL**: Primary/default database for production
  - **MySQL**: Full support for MySQL 8.0+
  - **MariaDB Galera**: Cluster support with WSREP, auto-increment, transaction handling
  - **SQLite**: Development and lightweight deployments
- **Database Libraries (Python)**:
  - **SQLAlchemy**: Used ONLY for database initialization and schema creation
  - **PyDAL**: Used for ALL runtime database operations and migrations
  - `DB_TYPE` must match PyDAL connection string prefixes exactly
- **Database Libraries (Go)**: GORM or sqlx (mandatory for cross-database support)
  - Must support PostgreSQL, MySQL/MariaDB, and SQLite
  - Stable, well-maintained library required
- **Migrations**: PyDAL handles all migrations via `migrate=True`
- **MariaDB Galera Support**: Handle Galera-specific requirements (WSREP, auto-increment, transactions)

📚 **Supported DB_TYPE Values**: See [Development Standards - Database Standards](docs/STANDARDS.md#database-standards) for complete list and configuration details.

### Security & Authentication
- **Flask-Security-Too**: Mandatory for all Flask applications
  - Role-based access control (RBAC)
  - User authentication and session management
  - Password hashing with bcrypt
  - Email confirmation and password reset
  - Two-factor authentication (2FA)
- **TLS**: Enforce TLS 1.2 minimum, prefer TLS 1.3
- **HTTP3/QUIC**: Utilize UDP with TLS for high-performance connections where possible
- **Authentication**: JWT and MFA (standard), mTLS where applicable
- **SSO**: SAML/OAuth2 SSO as enterprise-only features
- **Secrets**: Environment variable management
- **Scanning**: Trivy vulnerability scanning, CodeQL analysis
- **Code Quality**: All code must pass CodeQL security analysis

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
PRODUCT_NAME=your-product-identifier

# Release mode (enables license enforcement)
RELEASE_MODE=false  # Development (default)
RELEASE_MODE=true   # Production (explicitly set)
```

📚 **Detailed Documentation**: [License Server Integration Guide](docs/licensing/license-server-integration.md)

## WaddleAI Integration (Optional)

For projects requiring AI capabilities, integrate with WaddleAI located at `~/code/WaddleAI`.

**When to Use WaddleAI:**
- Natural language processing (NLP)
- Machine learning model inference
- AI-powered features and automation
- Intelligent data analysis
- Chatbots and conversational interfaces

**Integration Pattern:**
- WaddleAI runs as separate microservice container
- Communicate via REST API or gRPC
- Environment variable configuration for API endpoints
- License-gate AI features as enterprise functionality

📚 **WaddleAI Documentation**: See WaddleAI project at `~/code/WaddleAI` for integration details

## Project Structure

```
project-name/
├── .github/             # CI/CD pipelines and templates
│   └── workflows/       # GitHub Actions for each container
├── services/            # Microservices (separate containers by default)
│   ├── flask-backend/   # Flask + PyDAL backend (auth, users, standard APIs)
│   ├── go-backend/      # Go high-performance backend (XDP/AF_XDP, NUMA)
│   ├── webui/           # Node.js + React frontend shell
│   └── connector/       # Integration services (placeholder)
├── shared/              # Shared components
├── infrastructure/      # Infrastructure as code
├── scripts/             # Utility scripts
├── tests/               # Test suites (unit, integration, e2e, performance)
├── docs/                # Documentation
├── config/              # Configuration files
├── docker-compose.yml   # Production environment
├── docker-compose.dev.yml # Local development
├── Makefile             # Build automation
├── .version             # Version tracking
└── CLAUDE.md            # This file
```

### Three-Container Architecture

| Container | Purpose | When to Use |
|-----------|---------|-------------|
| **flask-backend** | Standard APIs, auth, CRUD | <10K req/sec, business logic |
| **go-backend** | High-performance networking | >10K req/sec, <10ms latency |
| **webui** | Node.js + React frontend | All frontend applications |

**Default Roles**: Admin (full access), Maintainer (read/write, no user mgmt), Viewer (read-only)

📚 **Architecture diagram and details**: [Development Standards - Microservices Architecture](docs/STANDARDS.md#microservices-architecture)

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

## Development Workflow

### Local Development Setup
```bash
git clone <repository-url>
cd project-name
make setup                    # Install dependencies
make dev                      # Start development environment
```

### Essential Commands
```bash
# Development
make dev                      # Start development services
make test                     # Run all tests
make lint                     # Run linting
make build                    # Build all services
make clean                    # Clean build artifacts

# Production
make docker-build             # Build containers
make docker-push              # Push to registry
make deploy-dev               # Deploy to development
make deploy-prod              # Deploy to production

# Testing
make test-unit               # Run unit tests
make test-integration        # Run integration tests
make test-e2e                # Run end-to-end tests

# License Management
make license-validate        # Validate license
make license-check-features  # Check available features
```

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
- ❌ Skipping input validation "just this once"
- ❌ Hardcoding credentials or configuration
- ❌ Ignoring error returns or exceptions
- ❌ Commenting out failing tests to make CI pass
- ❌ Deploying without proper testing
- ❌ Using deprecated or unmaintained dependencies
- ❌ Implementing partial features with "TODO" placeholders
- ❌ Bypassing security checks for convenience
- ❌ Assuming data is valid without verification
- ❌ Leaving debug code or backdoors in production

#### Quality Checklist Before Completion
- ✅ All error cases handled properly
- ✅ Unit tests cover all code paths
- ✅ Integration tests verify component interactions
- ✅ Security requirements fully implemented
- ✅ Performance meets acceptable standards
- ✅ Documentation complete and accurate
- ✅ Code review standards met
- ✅ No hardcoded secrets or credentials
- ✅ Logging and monitoring in place
- ✅ Build passes in containerized environment
- ✅ No security vulnerabilities in dependencies
- ✅ Edge cases and boundary conditions tested

### Git Workflow
- **NEVER commit automatically** unless explicitly requested by the user
- **NEVER push to remote repositories** under any circumstances
- **ONLY commit when explicitly asked** - never assume commit permission
- **Prefer `gh` CLI over direct GitHub access** - use GitHub CLI (`gh`) for all GitHub operations (PRs, issues, releases, repo info) instead of web scraping or direct API calls
- Always use feature branches for development
- Require pull request reviews for main branch
- Automated testing must pass before merge

**Before Every Commit - Security Scanning**:
- **Run security audits on all modified packages**:
  - **Go packages**: Run `gosec ./...` on modified Go services
  - **Node.js packages**: Run `npm audit` on modified Node.js services
  - **Python packages**: Run `bandit -r .` and `safety check` on modified Python services
- **Do NOT commit if security vulnerabilities are found** - fix all issues first
- **Document vulnerability fixes** in commit message if applicable

**Before Every Commit - API Testing**:
- **Create and run API testing scripts** for each modified container service
- **Testing scope**: All new endpoints and modified functionality
- **Test files location**: `tests/api/` directory with service-specific subdirectories
  - `tests/api/flask-backend/` - Flask backend API tests
  - `tests/api/go-backend/` - Go backend API tests
  - `tests/api/webui/` - WebUI container tests
- **Run before commit**: Each test script should be executable and pass completely
- **Test coverage**: Health checks, authentication, CRUD operations, error cases
- **Command pattern**: `cd services/<service-name> && npm run test:api` or equivalent

**Before Every Commit - Screenshots**:
- **Run screenshot tool to update UI screenshots in documentation**
  - Run `cd services/webui && npm run screenshots` to capture current UI state
  - This automatically removes old screenshots and captures fresh ones
  - Commit updated screenshots with relevant feature/documentation changes

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
- **Regular security audits**: `npm audit`, `go mod audit`, `safety check`

### Linting & Code Quality Requirements
- **ALL code must pass linting** before commit - no exceptions
- **Python**: flake8, black, isort, mypy (type checking), bandit (security)
- **JavaScript/TypeScript**: ESLint, Prettier
- **Go**: golangci-lint (includes staticcheck, gosec, etc.)
- **Ansible**: ansible-lint
- **Docker**: hadolint
- **YAML**: yamllint
- **Markdown**: markdownlint
- **Shell**: shellcheck
- **CodeQL**: All code must pass CodeQL security analysis
- **PEP Compliance**: Python code must follow PEP 8, PEP 257 (docstrings), PEP 484 (type hints)

### Build & Deployment Requirements
- **NEVER mark tasks as completed until successful build verification**
- All Go and Python builds MUST be executed within Docker containers
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
- **Keep focused**: Critical context, architectural decisions, and workflow instructions only
- **User approval required**: ALWAYS ask user permission before splitting CLAUDE.md files
- **Use Task Agents**: Utilize task agents (subagents) to be more expedient and efficient when making changes to large files, updating or reviewing multiple files, or performing complex multi-step operations
- **Avoid sed/cat**: Use sed and cat commands only when necessary; prefer dedicated Read/Edit/Write tools for file operations

### Task Agent Usage Guidelines

**Model Selection:**
- **Haiku model**: Use for the majority of task agent work (file searches, simple edits, routine operations)
- **Sonnet model**: Use for more complex jobs requiring deeper reasoning (architectural decisions, complex refactoring, multi-file coordination)
- Default to haiku unless the task explicitly requires complex analysis

**Response Size Requirements:**
- **CRITICAL**: Task agents MUST return minimal responses to avoid context overload of the orchestration model
- Agents should return only essential information: file paths, line numbers, brief summaries
- Avoid returning full file contents or verbose explanations in agent responses
- Use bullet points and concise formatting in agent outputs

**Concurrency Limits:**
- **Maximum 10 task agents** running concurrently at any time
- Even with minimal responses, running more than 10 agents risks context overload
- Queue additional tasks if the limit would be exceeded
- Monitor active agent count before spawning new agents

**Best Practices:**
- Provide clear, specific prompts to agents to get focused responses
- Request only the information needed, not comprehensive analysis
- Use agents for parallelizable work (searching multiple directories, checking multiple files)
- Combine related small tasks into single agent calls when possible

## Development Standards

Comprehensive development standards are documented separately to keep this file concise.

📚 **Complete Standards Documentation**: [Development Standards](docs/STANDARDS.md)

### Quick Reference

**API Versioning**:
- ALL REST APIs MUST use versioning: `/api/v{major}/endpoint` format
- Semantic versioning for major versions only in URL
- Support current and previous versions (N-1) minimum
- Add deprecation headers to old versions
- Document migration paths for version changes

**Database Standards**:
- SQLAlchemy for database initialization and schema creation only
- PyDAL mandatory for ALL runtime database operations and migrations
- Supported databases: PostgreSQL, MySQL, MariaDB Galera, SQLite
- Thread-safe usage with thread-local connections
- Environment variable configuration for all database settings
- Connection pooling and retry logic required
- Async/multi-threading based on workload (see Performance Optimization)

**Protocol Support**:
- **Inter-container communication** (within cluster): gRPC or HTTP/3 (QUIC) preferred
  - Lower latency, better performance, binary protocols
  - Use for service-to-service calls between containers
- **External communication** (clients, integrations): REST API over HTTPS
  - Flask REST endpoints for client-facing APIs
  - Used for outside-of-cluster integrations and third-party access
- Environment variables for protocol configuration
- Multi-protocol implementation required

**Performance Optimization (Python):**
- Dataclasses with slots mandatory (30-50% memory reduction)
- Type hints required for all Python code
- **Concurrency selection based on workload:**
  - `asyncio` + `databases` library for I/O-bound operations (>100 concurrent requests)
  - `threading` + `ThreadPoolExecutor` for blocking I/O and legacy integrations
  - `multiprocessing` for CPU-bound operations
- Connection pool sizing: `(2 * CPU_cores) + disk_spindles`
- Avoid premature optimization - profile first

**High-Performance Networking (Case-by-Case):**
- XDP (eXpress Data Path): Kernel-level packet processing
- AF_XDP: Zero-copy socket for user-space packet processing
- Use only for network-intensive applications requiring >100K packets/sec
- Evaluate Python vs Go based on traffic requirements

**Microservices Architecture**:
- Web UI, API, and Connector as **separate containers by default**
- Single responsibility per service
- API-first design
- Independent deployment and scaling
- Each service has its own Dockerfile and dependencies

**MarchProxy API Gateway/LB Integration**:
- Applications are expected to run behind MarchProxy (`~/code/MarchProxy`)
- **DO NOT include MarchProxy in default deployment** - it's external infrastructure
- **Generate MarchProxy-compatible import configuration** in `config/marchproxy/`
- Import config via MarchProxy's API: `POST /api/v1/services/import`
- See [Development Standards - MarchProxy Integration](docs/STANDARDS.md#marchproxy-api-gateway-integration)

**Docker Standards**:
- Multi-arch builds (amd64/arm64)
- Debian-slim base images
- Docker Compose for local development
- Minimal host port exposure

**Testing**:
- Unit tests: Network isolated, mocked dependencies
- Integration tests: Component interactions
- E2E tests: Critical workflows
- Performance tests: Scalability validation

**Security**:
- TLS 1.2+ required
- Input validation mandatory
- JWT, MFA, mTLS standard
- SSO as enterprise feature

## Application Architecture

**ALWAYS use microservices architecture** - decompose into specialized, independently deployable containers:

1. **Web UI Container**: ReactJS frontend (separate container, served via nginx)
2. **Application API Container**: Flask + Flask-Security-Too backend (separate container)
3. **Connector Container**: External system integration (separate container)

**Default Container Separation**: Web UI and API are ALWAYS separate containers by default. This provides:
- Independent scaling of frontend and backend
- Different resource allocation per service
- Separate deployment lifecycles
- Technology-specific optimization

**Benefits**:
- Independent scaling
- Technology diversity
- Team autonomy
- Resilience
- Continuous deployment

📚 **Detailed Architecture Patterns**: See [Development Standards - Microservices Architecture](docs/STANDARDS.md#microservices-architecture)

## Common Integration Patterns

📚 **Complete code examples and integration patterns**: [Development Standards](docs/STANDARDS.md)

Key integration patterns documented:
- Flask + Flask-Security-Too + PyDAL authentication
- Database integration with multi-DB support
- ReactJS frontend with API client
- License-gated features
- Prometheus monitoring integration

## Website Integration Requirements

**Required websites**: Marketing/Sales (Node.js) + Documentation (Markdown)

**Design**: Multi-page, modern aesthetic, subtle gradients, responsive, performance-focused

**Repository**: Sparse checkout submodule from `github.com/penguintechinc/website` with `{app_name}/` and `{app_name}-docs/` folders

## Troubleshooting & Support

**Common Issues**: Port conflicts, database connections, license validation, build failures, test failures

**Quick Debug**: `docker-compose logs -f <service>` | `make debug` | `make health`

**Support**: support@penguintech.io | sales@penguintech.io | https://status.penguintech.io

📚 **Detailed troubleshooting**: [Development Standards](docs/STANDARDS.md) | [License Guide](docs/licensing/license-server-integration.md)

## CI/CD & Workflows

**Build Tags**: `beta-<epoch64>` (main) | `alpha-<epoch64>` (other) | `vX.X.X-beta` (version release) | `vX.X.X` (tagged release)

**Version**: `.version` file in root, semver format, monitored by all workflows

### Pre-Commit Checklist

**CRITICAL: You MUST run the pre-commit script before every commit:**

```bash
./scripts/pre-commit/pre-commit.sh
```

Results logged to: `/tmp/pre-commit-<project>-<epoch>/summary.log`

Quick reference (see [docs/PRE_COMMIT.md](docs/PRE_COMMIT.md) for full details):
1. Linters → 2. Security scans → 3. No secrets → 4. Build & Run → 5. Tests → 6. Version update → 7. Docker debian-slim

**Only commit when asked** — run pre-commit script, verify all checks pass, then wait for approval before `git commit`.

### Applying Code Changes

**After making code changes, rebuild and restart containers to apply changes:**

```bash
# All services
docker compose down && docker compose up -d --build

# Single service
docker compose up -d --build <service-name>
```

**IMPORTANT:** `docker compose restart` and `docker restart` do NOT apply code changes - they only restart the existing container with old code. Always use `--build` to rebuild images with new code.

📚 **Complete CI/CD documentation**: [Workflows](docs/WORKFLOWS.md) | [Standards](docs/STANDARDS.md)

## Template Customization

**Adding Languages/Services**: Create in `services/`, add Dockerfile, update CI/CD, add linting/testing, update docs.

**Enterprise Integration**: License server, multi-tenancy, usage tracking, audit logging, monitoring.

📚 **Detailed customization guides**: [Development Standards](docs/STANDARDS.md)

---

**Template Version**: 1.3.0
**Last Updated**: 2025-12-03
**Maintained by**: Penguin Tech Inc
**License Server**: https://license.penguintech.io

**Key Updates in v1.3.0:**
- Three-container architecture: Flask backend, Go backend, WebUI shell
- WebUI shell with Node.js + React, role-based access (Admin, Maintainer, Viewer)
- Flask backend with PyDAL, JWT auth, user management
- Go backend with XDP/AF_XDP support, NUMA-aware memory pools
- GitHub Actions workflows for multi-arch builds (AMD64, ARM64)
- Gold text theme by default, Elder sidebar pattern, WaddlePerf tabs
- Docker Compose updated for new architecture

**Key Updates in v1.2.0:**
- Web UI and API as separate containers by default
- Mandatory linting for all languages (flake8, ansible-lint, eslint, etc.)
- CodeQL inspection compliance required
- Multi-database support by design (all PyDAL databases + MariaDB Galera)
- DB_TYPE environment variable with input validation
- Flask as sole web framework (PyDAL for database abstraction)

**Key Updates in v1.1.0:**
- Flask-Security-Too mandatory for authentication
- ReactJS as standard frontend framework
- Python 3.13 vs Go decision criteria
- XDP/AF_XDP guidance for high-performance networking
- WaddleAI integration patterns
- Release-mode license enforcement
- Performance optimization requirements (dataclasses with slots)

*This template provides a production-ready foundation for enterprise software development with comprehensive tooling, security, operational capabilities, and integrated licensing management.*
