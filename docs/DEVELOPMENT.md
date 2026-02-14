# Gough Local Development Guide

Complete guide to setting up a local development environment for Gough, a multi-language infrastructure management platform featuring Python Flask backend, Go microservices, and Node.js/React frontend.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Initial Setup](#initial-setup)
3. [Starting Development Environment](#starting-development-environment)
4. [Development Workflow](#development-workflow)
5. [Common Tasks](#common-tasks)
6. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### System Requirements

- **macOS 12+**, **Linux (Ubuntu 20.04+)**, or **Windows 10+ with WSL2**
- **Docker Desktop** 4.0+ (or Docker Engine 20.10+)
- **Docker Compose** 2.0+
- **Git** 2.30+
- **Python** 3.13+ (for Flask backend development)
- **Node.js** 18+ (for WebUI development)
- **Go** 1.24.2+ (for Go microservices)

### Optional Tools

- **Docker Buildx** (for multi-architecture builds)
- **Helm** (for Kubernetes deployments)
- **kubectl** (for Kubernetes clusters)
- **ansible** (for infrastructure provisioning)

### Installation

**macOS (Homebrew)**:
```bash
brew install docker docker-compose git python node go
brew install --cask docker
```

**Ubuntu/Debian**:
```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose git python3.13 nodejs golang-1.24
sudo usermod -aG docker $USER  # Allow docker without sudo
newgrp docker                   # Activate group change
```

**Verify Installation**:
```bash
docker --version      # Docker 20.10+
docker-compose --version  # Docker Compose 2.0+
git --version
python3 --version     # Python 3.13+
node --version        # Node.js 18+
go version           # Go 1.24+
```

---

## Initial Setup

### Clone Repository

```bash
git clone https://github.com/PenguinCloud/Gough.git
cd Gough
```

### Install Dependencies

```bash
# Install all project dependencies
make setup
```

This runs:
1. Python environment setup (venv, requirements)
2. Node.js dependency installation (npm install)
3. Go module setup (go mod download)
4. Pre-commit hooks installation
5. Database initialization

### Environment Configuration

Copy and customize environment files:

```bash
# Copy example environment files
cp .env.example .env
cp .env.local.example .env.local  # Optional: local overrides
```

**Key Environment Variables for Gough**:
```bash
# Database Configuration
DB_TYPE=postgresql              # postgres, mysql, mariadb, sqlite
DB_HOST=localhost
DB_PORT=5432
DB_NAME=gough_dev
DB_USER=postgres
DB_PASSWORD=postgres

# Flask Backend
FLASK_ENV=development
FLASK_DEBUG=1
SECRET_KEY=your-secret-key-for-dev

# MAAS Integration (if using MaaS for provisioning)
MAAS_API_KEY=your-maas-api-key
MAAS_API_URL=http://maas-server:5240/MAAS
MAAS_USERNAME=admin

# License (Development - all features available)
RELEASE_MODE=false
LICENSE_KEY=not-required-in-dev

# Port Configuration
FLASK_PORT=5000
GO_PORT=8000
WEBUI_PORT=3000
REDIS_PORT=6379
POSTGRES_PORT=5432

# Cloud Providers (optional for multi-cloud support)
GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcp-credentials.json
AWS_ACCESS_KEY_ID=your-aws-key
AWS_SECRET_ACCESS_KEY=your-aws-secret
AZURE_SUBSCRIPTION_ID=your-azure-subscription
```

### Database Initialization

```bash
# Create database and run migrations
make db-init

# Seed with mock data (3-4 servers/agents per category)
make seed-mock-data

# Verify database connection
make db-health
```

---

## Starting Development Environment

### Quick Start (All Services)

```bash
# Start all services in one command
make dev

# This runs:
# - PostgreSQL database
# - Redis cache
# - Flask backend (port 5000) - MAAS management and agent APIs
# - Go backend (port 8000) - High-performance networking, resource monitoring
# - Node.js WebUI (port 3000) - Web interface for infrastructure management

# Access the application:
# Web UI:      http://localhost:3000
# Flask API:   http://localhost:5000/api/v1
# Go API:      http://localhost:8000/api/v1
# Database UI: http://localhost:8080 (Adminer, if enabled)
```

### Individual Service Management

**Start specific services**:
```bash
# Start only Flask backend (MAAS integration, user management)
docker-compose up -d flask-backend

# Start WebUI and database
docker-compose up -d postgres webui

# Start without detaching (see logs)
docker-compose up flask-backend

# Start Go backend (high-performance network ops)
docker-compose up -d go-backend
```

**View service logs**:
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f flask-backend

# Last 100 lines, follow new entries
docker-compose logs -f --tail=100 webui
```

**Stop services**:
```bash
# Stop all services (keep data)
docker-compose down

# Stop and remove volumes (clean slate)
docker-compose down -v

# Restart services
docker-compose restart

# Rebuild and restart (apply code changes)
docker-compose down && docker-compose up -d --build
```

### Development Docker Compose Files

- **`docker-compose.dev.yml`**: Local development (hot-reload, debug ports, development mode)
- **`docker-compose.yml`**: Production-like (health checks, resource limits, optimized)

Use dev version locally:
```bash
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up
```

---

## Development Workflow

### 1. Start Development Environment

```bash
make dev        # Start all services
make seed-data  # Populate with test data
```

### 2. Make Code Changes

Edit files in your favorite editor. Services auto-reload:

- **Python (Flask)**: Reload on file save (FLASK_DEBUG=1)
- **Node.js (React)**: Hot reload (Vite dev server)
- **Go**: Requires restart (`docker-compose restart go-backend`)

### 3. Verify Changes

```bash
# Quick smoke tests
make smoke-test

# Run linters
make lint

# Run unit tests (specific service)
cd services/flask-backend && pytest tests/unit/

# Run all tests
make test
```

### 4. Populate Mock Data for Feature Testing

After implementing a new feature (e.g., new server type, agent feature), create mock data scripts:

```bash
# Create mock data script for servers
cat > scripts/mock-data/seed-servers.py << 'EOF'
from dal import DAL

def seed_servers():
    db = DAL('postgresql://user:password@localhost/gough_dev')

    servers = [
        {"name": "server-prod-01", "type": "physical", "status": "active", "ip": "192.168.1.10"},
        {"name": "server-dev-01", "type": "virtual", "status": "active", "ip": "192.168.1.20"},
        {"name": "server-staging", "type": "container", "status": "inactive", "ip": "192.168.1.30"},
        {"name": "server-monitoring", "type": "physical", "status": "active", "ip": "192.168.1.40"},
    ]

    for server in servers:
        db.servers.insert(**server)

    print(f"✓ Seeded {len(servers)} servers")

if __name__ == "__main__":
    seed_servers()
EOF

# Run the mock data script
python scripts/mock-data/seed-servers.py

# Add to seed-all.py orchestrator
echo "from seed_servers import seed_servers; seed_servers()" >> scripts/mock-data/seed-all.py
```

### 5. Run Pre-Commit Checklist

Before committing, run the comprehensive pre-commit script:

```bash
./scripts/pre-commit/pre-commit.sh
```

**Steps**:
1. ✅ Linters (flake8, black, eslint, golangci-lint)
2. ✅ Security scans (bandit, npm audit, gosec)
3. ✅ Secret detection (no API keys, passwords, tokens)
4. ✅ Build & Run (build all containers, verify runtime)
5. ✅ Smoke tests (build, health checks, UI loads)
6. ✅ Unit tests (isolated component testing)
7. ✅ Integration tests (component interactions)
8. ✅ Version update & Docker standards

**Troubleshooting Pre-Commit**:

See [Pre-Commit Documentation](PRE_COMMIT.md) for detailed guidance on:
- Fixing linting errors
- Resolving security vulnerabilities
- Excluding files from checks
- Bypassing specific checks (with justification)

### 6. Testing & Validation

Comprehensive testing guide:

**Complete Testing Guide**: [Testing Documentation](TESTING.md)

**Quick Test Commands**:
```bash
# Smoke tests only (fast, <2 min)
make smoke-test

# Unit tests only
make test-unit

# Integration tests only
make test-integration

# All tests
make test

# Specific test file
pytest tests/unit/test_server_management.py

# Cross-architecture testing (QEMU)
make test-multiarch
```

### 7. Create Pull Request

Once tests pass:

```bash
# Push branch
git push origin feature-branch-name

# Create PR via GitHub CLI
gh pr create --title "Brief feature description" \
  --body "Detailed description of changes"

# Or use web UI: https://github.com/PenguinCloud/Gough/compare
```

### 8. Code Review & Merge

- Address review feedback
- Re-run tests if changes made
- Merge when approved

---

## Common Tasks

### Adding a New Python Dependency

```bash
# Add to services/flask-backend/requirements.txt
echo "new-package==1.0.0" >> services/flask-backend/requirements.txt

# Rebuild Flask container
docker-compose up -d --build flask-backend

# Verify import works
docker-compose exec flask-backend python -c "import new_package"
```

### Adding a New Node.js Dependency

```bash
# Add to services/webui/package.json
npm install new-package

# Rebuild WebUI container
docker-compose up -d --build webui

# Verify in running container
docker-compose exec webui npm list new-package
```

### Adding a New Go Dependency

```bash
# Add to services/go-backend/go.mod
cd services/go-backend && go get github.com/package/name

# Rebuild Go container
docker-compose up -d --build go-backend

# Verify in running container
docker-compose exec go-backend go mod list
```

### Adding a New Environment Variable

```bash
# Add to .env
echo "NEW_VAR=value" >> .env

# Restart services to pick up new variable
docker-compose restart

# Verify it's set
docker-compose exec flask-backend printenv | grep NEW_VAR
```

### Debugging a Service

**View logs in real-time**:
```bash
docker-compose logs -f flask-backend
```

**Access container shell**:
```bash
# Python service
docker-compose exec flask-backend bash

# Node.js service
docker-compose exec webui bash

# Go service
docker-compose exec go-backend sh
```

**Execute commands in container**:
```bash
# Run Python script
docker-compose exec flask-backend python -c "print('hello')"

# Check service health
docker-compose exec flask-backend curl http://localhost:5000/api/v1/health
```

### Database Operations

**Connect to database**:
```bash
# PostgreSQL
docker-compose exec postgres psql -U postgres -d gough_dev

# View schema
\dt                    # PostgreSQL tables
```

**Reset database**:
```bash
# Full reset (deletes all data)
docker-compose down -v
make db-init
make seed-mock-data
```

**Run migrations**:
```bash
# Auto-migrate on startup
docker-compose restart flask-backend

# Or manually run migration
docker-compose exec flask-backend python -m migrations
```

### Working with Git Branches

```bash
# Create feature branch
git checkout -b feature/new-feature-name

# Keep branch updated with main
git fetch origin
git rebase origin/main

# Clean commit history before PR
git rebase -i origin/main  # Interactive rebase

# Push branch
git push origin feature/new-feature-name
```

### Database Backups

```bash
# Backup PostgreSQL
docker-compose exec postgres pg_dump -U postgres gough_dev > backup.sql

# Restore from backup
docker-compose exec -T postgres psql -U postgres gough_dev < backup.sql
```

### Testing MAAS Integration

```bash
# Verify MAAS connectivity
docker-compose exec flask-backend python -c "
from app.modules.maas_client import MaasClient
client = MaasClient()
print(client.get_machines())
"

# View MAAS provisioning status
docker-compose logs -f flask-backend | grep maas
```

### Testing Agent Deployment

```bash
# View agent logs
docker-compose logs -f access-agent

# Test agent health endpoint
curl http://localhost:5001/health

# Monitor resource usage
docker-compose stats access-agent
```

---

## Troubleshooting

### Services Won't Start

**Check if ports are already in use**:
```bash
# Find what's using port 5000
lsof -i :5000

# Kill the process
kill -9 <PID>

# Or use different ports in .env
FLASK_PORT=5001
```

**Docker daemon not running**:
```bash
# macOS
open /Applications/Docker.app

# Linux
sudo systemctl start docker

# Windows (Docker Desktop)
# Start Docker Desktop from Applications
```

### Database Connection Error

```bash
# Verify database container is running
docker-compose ps postgres

# Check database credentials in .env
cat .env | grep DB_

# Connect to database directly
docker-compose exec postgres psql -U postgres -d postgres

# View logs
docker-compose logs postgres
```

### Flask Backend Won't Start

```bash
# Check logs
docker-compose logs flask-backend

# Verify database migration
docker-compose exec flask-backend python -c "from app import db; db.create_all()"

# Reset and rebuild
docker-compose down
docker-compose up -d --build flask-backend
```

### WebUI Won't Load

```bash
# Check Node.js build
docker-compose logs webui

# Verify Vite dev server is running
curl http://localhost:3000

# Rebuild WebUI
docker-compose down
docker-compose up -d --build webui
```

### MAAS Integration Issues

```bash
# Verify MAAS API connectivity
docker-compose exec flask-backend python -c "
import os
print(f'MAAS URL: {os.getenv(\"MAAS_API_URL\")}')
print(f'MAAS Key: {os.getenv(\"MAAS_API_KEY\")[:10]}...')
"

# Test API endpoint
curl -X GET "http://localhost:5240/MAAS/api/2.0/machines/" \
  -H "Authorization: ApiKey <api-key>"
```

### Go Backend Performance Issues

```bash
# Check resource usage
docker-compose stats go-backend

# View Go runtime metrics
curl http://localhost:8000/metrics

# Check logs
docker-compose logs -f go-backend
```

### Smoke Tests Failing

**Check which test failed**:
```bash
# Run individually
./tests/smoke/build/test-flask-build.sh
./tests/smoke/api/test-flask-health.sh
./tests/smoke/webui/test-pages-load.sh
```

**Common issues**:
- Service not healthy (logs: `docker-compose logs <service>`)
- Port not exposed (check docker-compose.yml)
- API endpoint not implemented
- Missing environment variables

See [Testing Documentation - Smoke Tests](TESTING.md#smoke-tests) for detailed troubleshooting.

### Git Merge Conflicts

```bash
# View conflicts
git status

# Edit conflicted files (marked with <<<<, ====, >>>>)
# Remove conflict markers and keep desired code

# Mark as resolved
git add <resolved-file>

# Complete merge
git commit -m "Resolve merge conflicts"
```

### Slow Docker Builds

```bash
# Check Docker disk usage
docker system df

# Clean up unused images/containers
docker system prune

# Rebuild without cache (slow, but fresh)
docker-compose build --no-cache flask-backend
```

### QEMU Cross-Architecture Build Issues

**QEMU not available**:
```bash
# Install QEMU support
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

# Verify buildx setup
docker buildx ls
```

**Slow arm64 build with QEMU**:
```bash
# Expected: 2-5x slower with QEMU emulation
# Use only for final validation, not every iteration

# Build native architecture (fast)
docker buildx build --load .

# Build alternate with QEMU (slow)
docker buildx build --platform linux/arm64 .
```

See [Testing Documentation - Cross-Architecture Testing](TESTING.md#cross-architecture-testing) for complete details.

---

## Tips & Best Practices

### Hot Reload Development

For fastest iteration:
```bash
# Start services once
docker-compose up -d

# Edit Python files → auto-reload (FLASK_DEBUG=1)
# Edit JavaScript files → hot reload (Vite)
# Edit Go files → restart service
```

### Environment-Specific Configuration

```bash
# Development settings (auto-loaded)
.env              # Default development config
.env.local        # Local machine overrides (gitignored)

# Production settings (via secret management)
Kubernetes secrets
AWS Secrets Manager
HashiCorp Vault
```

### Code Organization

Keep project clean:
```bash
# Remove old branches
git branch -D old-branch

# Clean local Docker images
docker image prune -a

# Clean unused containers
docker container prune
```

### Performance Tips

```bash
# Use specific services to reduce memory usage
docker-compose up postgres flask-backend  # Skip Go backend, WebUI

# Use lightweight testing
make smoke-test  # Instead of full test suite while developing

# Cache Docker layers by building in order of frequency of change
Dockerfile: base → dependencies → code → entrypoint
```

---

## Related Documentation

- **Testing**: [Testing Documentation](TESTING.md)
  - Mock data scripts
  - Smoke tests
  - Unit/integration/E2E tests
  - Performance tests
  - Cross-architecture testing

- **Pre-Commit**: [Pre-Commit Checklist](PRE_COMMIT.md)
  - Linting requirements
  - Security scanning
  - Build verification
  - Test requirements

- **Deployment**: [Deployment Guide](deployment/)
  - Containerization
  - Kubernetes deployment
  - Docker Compose production
  - Health checks

- **Standards**: [Development Standards](STANDARDS.md)
  - Architecture decisions
  - Code style
  - API conventions
  - Database patterns

- **Workflows**: [CI/CD Workflows](WORKFLOWS.md)
  - GitHub Actions pipelines
  - Build automation
  - Test automation
  - Release processes

---

**Last Updated**: 2026-01-06
**Maintained by**: Penguin Tech Inc
**Project**: Gough Infrastructure Management Platform
