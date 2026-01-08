# Gough - Claude Code Context

## Project Overview

Gough is a comprehensive project template incorporating best practices and patterns from Penguin Tech Inc projects. It provides a standardized foundation for multi-language projects with enterprise-grade infrastructure and integrated licensing.

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

## License & Legal

**License File**: `LICENSE.md` (located at project root)

**License Type**: Limited AGPL-3.0 with commercial use restrictions and Contributor Employer Exception

The `LICENSE.md` file is located at the project root following industry standards. This project uses a modified AGPL-3.0 license with additional exceptions for commercial use and special provisions for companies employing contributors.

- **License Server**: https://license.penguintech.io
- **Company Website**: www.penguintech.io
- **Support**: support@penguintech.io

---

**Current Version**: See `.version` file
**Last Updated**: 2025-12-18
**Maintained by**: Penguin Tech Inc
