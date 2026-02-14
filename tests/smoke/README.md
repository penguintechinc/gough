# Smoke Tests - Gough

Comprehensive smoke tests to verify basic functionality of all Gough services. These fast (<2 min total) tests validate that core services start and respond correctly.

## Overview

Smoke tests verify:
1. **Container Health** - All 5 containers start and pass health checks
2. **API Health Endpoints** - API responds to health checks
3. **iPXE Services** - TFTP serves binaries, HTTP boot responds
4. **WebUI Loads** - React frontend loads main pages

## Running Smoke Tests

### Alpha (Local)
Tests local Docker services on localhost:
```bash
./run-smoke-tests.sh alpha
# or skip builds
./run-smoke-tests.sh alpha quick
```

### Beta (Staging - dal2 cluster)
Tests dal2 staging deployment at https://dal2.penguintech.io with Cloudflare bypass:
```bash
./run-smoke-tests.sh beta
```

The beta tests automatically:
- Use `https://dal2.penguintech.io` as the API/WebUI base URL
- Send Host header `gough.penguintech.io` to bypass Cloudflare
- Skip container builds (tests live deployment only)
- Return appropriate HTTP status codes for validation

### From Project Root
```bash
make smoke-test
# or specific environment
./tests/smoke/run-smoke-tests.sh alpha
./tests/smoke/run-smoke-tests.sh beta
```

## Test Details

### test_containers.sh
Verifies all 5 main containers are running and healthy:
- gough-postgres (PostgreSQL)
- gough-api-manager (Python Quart API)
- gough-worker-ipxe (iPXE TFTP/HTTP services)
- gough-webui (Node.js React frontend)
- gough-access-agent (SSH access agent)

Health checks require containers to report:
- `healthy` - Container is healthy
- `starting` - Container is starting (acceptable)
- No healthcheck - Container running without healthcheck (acceptable)

**Duration**: ~10-20 seconds

### test_api_health.sh
Tests API health endpoints:
- `/healthz` - Kubernetes-style health check
- `/api/v1/health` - API-specific health check
- `/readyz` - Kubernetes-style readiness check
- `/api/v1/ready` - API-specific readiness check

Retries 10 times with 2-second delays to handle startup delays.

**Duration**: ~10-30 seconds

### test_ipxe_services.sh
Verifies iPXE worker functionality:
- TFTP port (UDP 69) is listening
- iPXE binary files exist in TFTP directory
- HTTP boot endpoint responds
- API Manager connectivity

**Duration**: ~20-40 seconds

### test_webui.sh
Validates WebUI loads correctly:
- Main page loads (HTTP 200)
- Health check endpoint responds
- React app responds
- Webpack server responding

**Duration**: ~20-40 seconds

## Integration with Development

### Before Development
```bash
docker-compose up -d
make smoke-test
make seed-mock-data
```

### Before Every Commit
Smoke tests must pass:
```bash
./tests/smoke/run-all.sh
```

If tests fail:
1. Check service logs: `docker-compose logs <service>`
2. Wait for services to fully start (30-60 seconds)
3. Retry: `./tests/smoke/run-all.sh`

### Makefile Integration
Add to Makefile (if not already present):
```makefile
smoke-test: ## Testing - Run smoke tests
	@$(SCRIPT_DIR)/tests/smoke/run-all.sh
```

Run with: `make smoke-test`

## Troubleshooting

### "Docker is not running"
```bash
# Start Docker
docker ps  # Should return container list
```

### "Containers not running"
```bash
# Start containers
docker-compose up -d

# Wait for startup
sleep 30

# Run tests
./tests/smoke/run-all.sh
```

### "API health checks fail"
- API may still be initializing (database migrations, etc.)
- Check logs: `docker-compose logs api-manager`
- Wait 30-60 seconds and retry
- Ensure database is ready: `docker-compose logs postgres`

### "WebUI pages not loading"
- WebUI may still be building
- Check logs: `docker-compose logs webui`
- Ensure Node.js dependencies installed
- Retry after 1-2 minutes

### "iPXE services fail"
- TFTP requires network privileges
- Check: `docker-compose logs worker-ipxe`
- Ensure docker-compose.yml has `cap_add: [NET_ADMIN, NET_RAW]`
- May require `network_mode: host` (already configured)

## Performance Requirements

- **Total Duration**: < 2 minutes for all tests
- **Container Checks**: ~10 seconds (no network required)
- **API Health**: ~30 seconds (with retries)
- **iPXE Services**: ~30 seconds (with retries)
- **WebUI Loads**: ~30 seconds (with retries)

Tests are designed to run in parallel where possible and retry gracefully if services are still starting.

## CI/CD Integration

These tests are suitable for:
- **Pre-deployment validation** - Run before deploying to any environment
- **Health checks** - Run periodically in production
- **Regression testing** - Run after updates
- **Performance baselines** - Measure startup time

## Standards Compliance

Follows CLAUDE.md testing standards:
- ✅ Fully working test scripts (no stubs)
- ✅ Fast execution (<2 min total)
- ✅ Covers critical paths (container startup, API health, services)
- ✅ Clear reporting and error messages
- ✅ Retry logic for transient failures
- ✅ Can be run locally or in CI/CD
- ✅ No external dependencies required

## Files

```
tests/smoke/
├── run-all.sh              # Master test runner
├── test_containers.sh      # Container health checks
├── test_api_health.sh      # API health endpoints
├── test_ipxe_services.sh   # iPXE TFTP/HTTP verification
├── test_webui.sh           # WebUI page loads
└── README.md               # This file
```
