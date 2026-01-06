# Gough Testing Guide

Comprehensive testing documentation for Gough infrastructure management platform, including unit tests, integration tests, smoke tests, mock data, and cross-architecture validation for multi-language support (Python Flask, Node.js React, Go microservices).

## Overview

Testing is organized into multiple levels to ensure comprehensive coverage, fast feedback, and production-ready code:

| Test Level | Purpose | Speed | Coverage |
|-----------|---------|-------|----------|
| **Smoke Tests** | Fast verification of basic functionality | <2 min | Build, run, API health, UI loads |
| **Unit Tests** | Isolated function/method testing | <1 min | Code logic, edge cases |
| **Integration Tests** | Component interaction verification | 1-5 min | Data flow, API contracts |
| **E2E Tests** | Critical workflows end-to-end | 5-10 min | User scenarios, business logic |
| **Performance Tests** | Scalability and throughput validation | 5-15 min | Load, latency, resource usage |

---

## Mock Data Scripts

### Purpose

Mock data scripts populate the development database with realistic test data, enabling:
- Rapid local development without manual data entry
- Consistent test data across the development team
- Documentation of expected data structure and relationships
- Quick feature iteration with pre-populated databases

### Location & Structure

```
scripts/mock-data/
├── seed-all.py             # Orchestrator: runs all seeders in order
├── seed-servers.py         # 3-4 servers with different types/statuses
├── seed-agents.py          # 3-4 agents with different versions
├── seed-networks.py        # 3-4 networks with different configs
├── seed-maas-integration.py # 3-4 MAAS instances with creds
├── seed-[feature].py       # Additional feature-specific seeders
└── README.md               # Instructions for running mock data
```

### Naming Convention

- **Python**: `seed-{feature-name}.py`
- **Shell**: `seed-{feature-name}.sh`
- **Organization**: One seeder per logical entity/feature

### Scope: 3-4 Items Per Feature

Each seeder should create **exactly 3-4 representative items** to test all feature variations without creating excessive test data:

**Example (Servers)**:
```python
# seed-servers.py
items = [
    {"name": "server-prod-01", "type": "physical", "status": "active"},
    {"name": "server-dev-01", "type": "virtual", "status": "active"},
    {"name": "server-staging", "type": "container", "status": "inactive"},
    {"name": "server-monitoring", "type": "physical", "status": "active"},
]
```

**Example (Agents)**:
```python
# seed-agents.py
items = [
    {"version": "1.0.0", "status": "online", "type": "full-agent"},
    {"version": "1.1.0", "status": "online", "type": "lite-agent"},
    {"version": "0.9.5", "status": "offline", "type": "full-agent"},
    {"version": "1.0.0", "status": "updating", "type": "full-agent"},
]
```

### Execution

**Seed all test data**:
```bash
make seed-mock-data          # Via Makefile
python scripts/mock-data/seed-all.py  # Direct execution
```

**Seed specific feature**:
```bash
python scripts/mock-data/seed-servers.py
python scripts/mock-data/seed-agents.py
python scripts/mock-data/seed-networks.py
```

### Implementation Pattern

**Python (PyDAL)**:
```python
#!/usr/bin/env python3
"""Seed mock data for servers entity."""

import os
import sys
from dal import DAL

def seed_servers():
    db = DAL('sqlite:memory')  # or use DB_TYPE env var

    servers = [
        {"name": "prod-server-01", "type": "physical", "status": "active"},
        {"name": "dev-server-01", "type": "virtual", "status": "active"},
        {"name": "staging-server", "type": "container", "status": "inactive"},
        {"name": "monitor-server", "type": "physical", "status": "active"},
    ]

    for server in servers:
        db.servers.insert(**server)

    print(f"✓ Seeded {len(servers)} servers")

if __name__ == "__main__":
    seed_servers()
```

**Shell (curl/API)**:
```bash
#!/bin/bash
# seed-networks.sh

API_URL="${API_URL:-http://localhost:5000}"
TOKEN="${AUTH_TOKEN}"

# Network 1
curl -X POST "$API_URL/api/v1/networks" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Production Network", "cidr": "10.0.0.0/24"}'

# Network 2
curl -X POST "$API_URL/api/v1/networks" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "Development Network", "cidr": "10.1.0.0/24"}'

# Network 3
curl -X POST "$API_URL/api/v1/networks" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "Monitoring Network", "cidr": "10.2.0.0/24"}'

# Network 4
curl -X POST "$API_URL/api/v1/networks" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "Storage Network", "cidr": "10.3.0.0/24"}'

echo "✓ Seeded 4 networks"
```

### Makefile Integration

Add to your `Makefile`:

```makefile
.PHONY: seed-mock-data
seed-mock-data:
	@echo "Seeding mock data..."
	@python scripts/mock-data/seed-all.py
	@echo "✓ Mock data seeding complete"

.PHONY: clean-data
clean-data:
	@echo "Clearing mock data..."
	@rm -f data/dev.db
	@echo "✓ Mock data cleared"
```

### When to Create Mock Data Scripts

**Create a mock data script after each new feature/entity completion**:
- After implementing server management → create `seed-servers.py`
- After implementing agent registration → create `seed-agents.py`
- After implementing network configuration → create `seed-networks.py`
- After implementing MAAS integration → create `seed-maas-integration.py`

This ensures developers can immediately test the feature without manual setup.

---

## Smoke Tests

### Purpose

Smoke tests provide fast verification that basic functionality works after code changes, preventing regressions in core features.

### Requirements (Mandatory)

All projects **MUST** implement smoke tests before committing:

- ✅ **Build Tests**: All containers build successfully without errors
- ✅ **Run Tests**: All containers start and remain healthy
- ✅ **API Health Checks**: All API endpoints respond with 200/healthy status
- ✅ **Page Load Tests**: All web pages load without JavaScript errors
- ✅ **Tab Navigation Tests**: All tabs/routes navigate without console errors

### Location & Structure

```
tests/smoke/
├── build/          # Container build verification
│   ├── test-flask-build.sh
│   ├── test-go-build.sh
│   └── test-webui-build.sh
├── run/            # Container runtime and health
│   ├── test-flask-run.sh
│   ├── test-go-run.sh
│   └── test-webui-run.sh
├── api/            # API health endpoint validation
│   ├── test-flask-health.sh
│   ├── test-go-health.sh
│   └── README.md
├── webui/          # Page load and tab navigation
│   ├── test-pages-load.sh
│   ├── test-tabs-navigate.sh
│   └── README.md
├── run-all.sh      # Execute all smoke tests
└── README.md       # Documentation
```

### Execution

**Run all smoke tests**:
```bash
make smoke-test              # Via Makefile
./tests/smoke/run-all.sh     # Direct execution
```

**Run specific test category**:
```bash
./tests/smoke/build/test-flask-build.sh
./tests/smoke/api/test-flask-health.sh
./tests/smoke/webui/test-pages-load.sh
```

### Speed Requirement

Complete smoke test suite **MUST run in under 2 minutes** to provide fast feedback during development.

### Implementation Examples

**Build Test (Shell)**:
```bash
#!/bin/bash
# tests/smoke/build/test-flask-build.sh

set -e

echo "Testing Flask backend build..."
cd services/flask-backend

# Attempt to build the container
if docker build -t flask-backend:test .; then
    echo "✓ Flask backend builds successfully"
    exit 0
else
    echo "✗ Flask backend build failed"
    exit 1
fi
```

**Health Check Test**:
```bash
#!/bin/bash
# tests/smoke/api/test-flask-health.sh

set -e

echo "Checking Flask API health..."
HEALTH_URL="http://localhost:5000/api/v1/health"

RESPONSE=$(curl -s -w "\n%{http_code}" "$HEALTH_URL")
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)

if [ "$HTTP_CODE" = "200" ]; then
    echo "✓ Flask API is healthy (HTTP $HTTP_CODE)"
    exit 0
else
    echo "✗ Flask API is unhealthy (HTTP $HTTP_CODE)"
    exit 1
fi
```

**Go Backend Health Check**:
```bash
#!/bin/bash
# tests/smoke/api/test-go-health.sh

set -e

echo "Checking Go API health..."
HEALTH_URL="http://localhost:8000/health"

RESPONSE=$(curl -s -w "\n%{http_code}" "$HEALTH_URL")
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)

if [ "$HTTP_CODE" = "200" ]; then
    echo "✓ Go API is healthy (HTTP $HTTP_CODE)"
    exit 0
else
    echo "✗ Go API is unhealthy (HTTP $HTTP_CODE)"
    exit 1
fi
```

**Page Load Test (Playwright)**:
```bash
#!/bin/bash
# tests/smoke/webui/test-pages-load.sh

npx playwright test tests/smoke/webui/pages.spec.ts \
  --config=tests/smoke/webui/playwright.config.ts
```

### Pre-Commit Integration

Smoke tests run as part of the pre-commit checklist (step 5) and **must pass before proceeding** to full test suite:

```bash
./scripts/pre-commit/pre-commit.sh
# Step 1: Linters
# Step 2: Security scans
# Step 3: No secrets
# Step 4: Build & Run
# Step 5: Smoke tests ← Must pass
# Step 6: Full tests
```

---

## Unit Tests

### Purpose

Unit tests verify individual functions and methods in isolation with mocked dependencies.

### Location

```
tests/unit/
├── flask-backend/
│   ├── test_server_management.py
│   ├── test_agent_registration.py
│   ├── test_maas_integration.py
│   └── test_api.py
├── go-backend/
│   ├── server_test.go
│   ├── network_test.go
│   └── api_test.go
└── webui/
    ├── components/
    │   ├── ServerList.test.tsx
    │   └── AgentStatus.test.tsx
    └── utils/
        └── helpers.test.ts
```

### Execution

```bash
make test-unit              # All unit tests
pytest tests/unit/          # Python
go test ./...               # Go
npm test                    # JavaScript/TypeScript
```

### Requirements

- All dependencies must be mocked
- Network calls must be stubbed
- Database access must be isolated
- Tests must run in parallel when possible

### Example (Python/Flask)

```python
# tests/unit/flask-backend/test_server_management.py
import pytest
from unittest.mock import Mock, patch
from app.modules.server_manager import ServerManager

@pytest.fixture
def mock_db():
    return Mock()

@pytest.fixture
def server_manager(mock_db):
    return ServerManager(db=mock_db)

def test_get_server_by_id(server_manager, mock_db):
    """Test retrieving a single server."""
    mock_db.servers.where.return_value.select.return_value = [
        {"id": 1, "name": "server-01", "type": "physical"}
    ]

    result = server_manager.get_server(1)
    assert result["id"] == 1
    assert result["name"] == "server-01"

def test_create_server_validation(server_manager):
    """Test server creation with invalid data."""
    with pytest.raises(ValueError):
        server_manager.create_server({"name": ""})  # Empty name should fail
```

---

## Integration Tests

### Purpose

Integration tests verify that components work together correctly, including real database interactions and service communication.

### Location

```
tests/integration/
├── flask-backend/
│   ├── test_server_creation_flow.py
│   ├── test_agent_deployment.py
│   ├── test_maas_provisioning.py
│   └── test_api_contracts.py
├── services/
│   ├── test_flask_go_communication.py
│   └── test_data_pipeline.py
└── database/
    ├── test_migrations.py
    └── test_queries.py
```

### Execution

```bash
make test-integration       # All integration tests
pytest tests/integration/   # Python
go test -tags=integration ./...  # Go
npm run test:integration    # JavaScript
```

### Requirements

- Use real databases (test instances)
- Test complete workflows
- Verify API contracts
- Test error scenarios

### Example (Python)

```python
# tests/integration/flask-backend/test_server_creation_flow.py
import pytest
from app import create_app, db

@pytest.fixture
def client():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

def test_server_creation_workflow(client):
    """Test complete server creation workflow."""
    # Create server
    response = client.post('/api/v1/servers', json={
        'name': 'test-server',
        'type': 'physical',
        'ip': '192.168.1.1'
    })
    assert response.status_code == 201
    server_id = response.json['id']

    # Retrieve server
    response = client.get(f'/api/v1/servers/{server_id}')
    assert response.status_code == 200
    assert response.json['name'] == 'test-server'

    # Update server
    response = client.put(f'/api/v1/servers/{server_id}', json={
        'status': 'active'
    })
    assert response.status_code == 200
```

---

## End-to-End Tests

### Purpose

E2E tests verify critical user workflows from start to finish, testing the entire application stack.

### Location

```
tests/e2e/
├── server-provisioning.spec.ts
├── agent-deployment.spec.ts
├── maas-integration.spec.ts
└── critical-workflows.spec.ts
```

### Execution

```bash
make test-e2e               # All E2E tests
npx playwright test tests/e2e/  # Playwright
```

### Example (Playwright)

```typescript
// tests/e2e/server-provisioning.spec.ts
import { test, expect } from '@playwright/test';

test('provision new server through UI', async ({ page }) => {
  // Navigate to servers page
  await page.goto('http://localhost:3000/servers');

  // Click "New Server" button
  await page.click('button:has-text("New Server")');

  // Fill form
  await page.fill('[name="name"]', 'test-server-01');
  await page.fill('[name="ip"]', '192.168.1.100');
  await page.selectOption('[name="type"]', 'physical');

  // Submit form
  await page.click('button:has-text("Provision")');

  // Verify success
  await expect(page).toHaveURL(/\/servers\/\d+/);
  await expect(page.locator('text=Server provisioning started')).toBeVisible();
});
```

---

## Performance Tests

### Purpose

Performance tests validate scalability, throughput, and resource usage under load.

### Location

```
tests/performance/
├── load-test.js
├── stress-test.js
└── profile-report.md
```

### Execution

```bash
make test-performance
npm run test:performance
```

---

## Cross-Architecture Testing

### Purpose

Cross-architecture testing ensures the application builds and runs correctly on both amd64 and arm64 architectures, preventing platform-specific bugs in multi-language Gough components.

### When to Test

**Before every final commit**, test on the alternate architecture:
- Developing on amd64 → Build and test arm64 with QEMU
- Developing on arm64 → Build and test amd64 with QEMU

### Setup (First Time)

Enable Docker buildx for multi-architecture builds:

```bash
docker buildx create --name multiarch --driver docker-container
docker buildx use multiarch
```

### Single Architecture Build

```bash
# Test current architecture (native, fast)
docker build -t flask-backend:test services/flask-backend/

# Or explicitly specify architecture
docker build --platform linux/amd64 -t flask-backend:test services/flask-backend/
```

### Cross-Architecture Build (QEMU)

```bash
# Test alternate architecture (uses QEMU emulation)
docker buildx build --platform linux/arm64 -t flask-backend:test services/flask-backend/

# Or test both simultaneously
docker buildx build --platform linux/amd64,linux/arm64 -t flask-backend:test services/flask-backend/
```

### Multi-Architecture Build Script

Create `scripts/build/test-multiarch.sh`:

```bash
#!/bin/bash
# Test both architectures before commit

set -e

SERVICES=("flask-backend" "go-backend" "webui")
ARCHITECTURES=("linux/amd64" "linux/arm64")

for service in "${SERVICES[@]}"; do
    echo "Testing $service on multiple architectures..."

    for arch in "${ARCHITECTURES[@]}"; do
        echo "  → Building for $arch..."
        docker buildx build \
            --platform "$arch" \
            -t "$service:multiarch-test" \
            "services/$service/" || {
            echo "✗ Build failed for $service on $arch"
            exit 1
        }
    done

    echo "✓ $service builds successfully on amd64 and arm64"
done

echo "✓ All services passed multi-architecture testing"
```

### Makefile Integration

```makefile
.PHONY: test-multiarch
test-multiarch:
	@echo "Testing multi-architecture builds..."
	@bash scripts/build/test-multiarch.sh

.PHONY: build-multiarch
build-multiarch:
	@docker buildx build \
		--platform linux/amd64,linux/arm64 \
		-t $(IMAGE_NAME):$(VERSION) \
		--push .
```

### Pre-Commit Integration

Add to pre-commit script (before final commit):

```bash
# Step 8: Cross-architecture testing
if [ "$ENABLE_QEMU_TEST" = "true" ]; then
    echo "Testing cross-architecture builds with QEMU..."
    make test-multiarch || exit 1
fi
```

### Troubleshooting

**QEMU not available**:
```bash
# Install QEMU support
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
```

**Slow builds with QEMU**:
- Expect 2-5x slower builds when using QEMU emulation
- Use for final validation, not every iteration
- Consider caching intermediate layers

**Architecture-specific issues**:
- File path separators (Windows vs Linux)
- Endianness in binary protocols
- Floating-point precision
- Package availability (especially for Python wheels)
- Go cross-compilation flags

---

## Multi-Language Testing Strategy

### Python (Flask Backend)

**Tools**: pytest, pytest-cov, black, flake8, mypy, bandit

**Test Structure**:
```bash
# Run Python tests
pytest tests/unit/ -v
pytest tests/integration/ -v
pytest tests/ --cov=app --cov-report=html
```

**Coverage Target**: >80% code coverage

### Node.js (WebUI)

**Tools**: Jest, Vitest, ESLint, Playwright

**Test Structure**:
```bash
# Run Node.js tests
npm run test              # Unit tests
npm run test:integration  # Integration tests
npm run test:e2e         # E2E tests
npm run lint             # ESLint
```

**Coverage Target**: >75% code coverage

### Go (Go Backend)

**Tools**: testing, golangci-lint, gosec

**Test Structure**:
```bash
# Run Go tests
go test ./... -v
go test ./... -cover
golangci-lint run ./...
gosec ./...
```

**Coverage Target**: >75% code coverage

---

## Test Execution Order (Pre-Commit)

Follow this order for efficient testing before commits:

1. **Linters** (fast, <1 min)
2. **Security scans** (fast, <1 min)
3. **Secrets check** (fast, <1 min)
4. **Build & Run** (5-10 min)
5. **Smoke tests** (fast, <2 min) ← Gates further testing
6. **Unit tests** (1-2 min)
7. **Integration tests** (2-5 min)
8. **E2E tests** (5-10 min)
9. **Cross-architecture build** (optional, slow)

## CI/CD Integration

All tests run automatically in GitHub Actions:

- **On PR**: Smoke + Unit + Integration tests
- **On main merge**: All tests + Performance tests
- **Nightly**: Performance + Cross-architecture tests
- **Release**: Full suite + Manual sign-off

See [Workflows](WORKFLOWS.md) for detailed CI/CD configuration.

---

**Last Updated**: 2026-01-06
**Maintained by**: Penguin Tech Inc
**Project**: Gough Infrastructure Management Platform
**Multi-Language Support**: Python 3.13, Node.js 18+, Go 1.24+
