# Gough Hypervisor Testing Framework

Comprehensive testing suite for the Gough hypervisor automation system implementing Phase 9: Testing & Validation requirements.

## Overview

This testing framework provides complete coverage of the Gough hypervisor system including:

- **Unit Tests**: Fast, isolated tests for individual components
- **Integration Tests**: Tests for component interactions and external services
- **End-to-End Tests**: Complete workflow validation from provisioning to monitoring
- **Performance Tests**: Load testing, stress testing, and benchmarking
- **Security Tests**: Vulnerability scanning and security validation

## Test Coverage

### Phase 9.1 Unit Testing ✅

- **Management Server Controllers**: Complete test coverage for all API endpoints
  - `tests/unit/management_server/controllers/test_api.py` - REST API endpoints
  - `tests/unit/management_server/controllers/test_auth.py` - Authentication & authorization
  - `tests/unit/management_server/controllers/test_deployment.py` - Deployment orchestration

- **Models and Libraries**: Database models, business logic, and utilities
  - `tests/unit/management_server/models/test_database_models.py` - PyDAL models
  - `tests/unit/management_server/libraries/test_maas_api.py` - MaaS API client

- **Template Validation**: Cloud-init and Ansible validation
  - `tests/unit/cloud_init/test_template_validation.py` - Cloud-init validation
  - `tests/unit/ansible/test_playbook_validation.py` - Ansible syntax checking

### Phase 9.2 Integration Testing ✅

- **MaaS Provisioning Workflow**: End-to-end machine provisioning
  - `tests/integration/test_maas_provisioning_workflow.py` - Complete MaaS integration

- **End-to-End Provisioning**: Full system validation
  - `tests/integration/test_end_to_end_provisioning.py` - Complete provisioning pipeline

### Phase 9.3 Performance Testing ✅

- **Load Testing**: Management portal performance under load
  - `tests/performance/test_management_portal_load.py` - Comprehensive load testing

## Test Configuration

### Requirements

All test dependencies are defined in:
```
tests/requirements.txt
```

Key testing frameworks:
- **pytest**: Primary testing framework
- **pytest-cov**: Code coverage analysis
- **pytest-asyncio**: Async test support
- **pytest-xdist**: Parallel test execution
- **locust**: Load testing framework
- **factory-boy**: Test data generation

### Configuration Files

- `pytest.ini`: Main pytest configuration with coverage settings
- `tests/conftest.py`: Global test fixtures and configuration
- `docker-compose.test.yml`: Test environment orchestration
- `Dockerfile.test`: Test container configuration

## Running Tests

### Quick Start

```bash
# Run all tests with default settings
./scripts/run_tests.sh

# Run specific test types
./scripts/run_tests.sh -t unit
./scripts/run_tests.sh -t integration -i
./scripts/run_tests.sh -t performance -p

# Run with custom options
./scripts/run_tests.sh -t all -j 8 -c 85
```

### Test Script Options

```bash
Usage: ./scripts/run_tests.sh [OPTIONS]

OPTIONS:
    -t, --test-type TYPE        Test type (unit|integration|performance|e2e|all)
    -e, --environment ENV       Environment (local|docker|ci)
    -j, --parallel-jobs NUM     Parallel jobs [default: 4]
    -c, --coverage-threshold    Coverage threshold [default: 80]
    -p, --performance          Enable performance tests
    -i, --integration          Enable integration tests
    -d, --docker               Run in Docker containers
    --markers MARKERS          Custom pytest markers
    --clean                    Clean previous artifacts
    --setup-env                Setup test environment
    --validate-env             Validate environment setup
```

### Docker Testing

```bash
# Start complete test environment
docker-compose -f docker-compose.test.yml up -d

# Run tests in containers
./scripts/run_tests.sh -e docker

# Clean up
docker-compose -f docker-compose.test.yml down -v
```

## Test Structure

### Directory Layout

```
tests/
├── conftest.py                 # Global fixtures
├── requirements.txt            # Test dependencies
├── unit/                      # Unit tests
│   ├── management_server/
│   │   ├── controllers/       # API controller tests
│   │   ├── models/           # Database model tests
│   │   └── libraries/        # Library tests
│   ├── agent/                # Agent tests
│   ├── ansible/              # Ansible tests
│   └── cloud_init/           # Cloud-init tests
├── integration/              # Integration tests
│   ├── test_maas_provisioning_workflow.py
│   └── test_end_to_end_provisioning.py
├── performance/              # Performance tests
│   └── test_management_portal_load.py
├── fixtures/                 # Test data and configurations
├── mocks/                    # Mock services
├── utils/                    # Test utilities
└── reports/                  # Generated test reports
```

### Test Markers

Tests are organized using pytest markers:

```python
@pytest.mark.unit          # Fast, isolated unit tests
@pytest.mark.integration   # Integration tests with external services
@pytest.mark.performance   # Performance and load tests
@pytest.mark.e2e          # End-to-end workflow tests
@pytest.mark.slow         # Long-running tests
@pytest.mark.maas         # MaaS-specific tests
@pytest.mark.fleetdm      # FleetDM-specific tests
@pytest.mark.agent        # Agent-specific tests
```

### Key Fixtures

Global fixtures defined in `conftest.py`:

- `mock_database`: In-memory test database
- `mock_redis`: Mock Redis client
- `mock_maas_client`: Mock MaaS API client
- `mock_fleet_client`: Mock FleetDM client
- `mock_ansible_runner`: Mock Ansible execution
- `auth_headers`: Authentication headers for API tests
- `sample_server_data`: Test server specifications
- `deployment_job_data`: Test deployment configurations

## CI/CD Integration

### GitHub Actions Workflow

The testing pipeline is automated using GitHub Actions (`.github/workflows/tests.yml`):

**Workflow Stages:**

1. **Unit Tests**: Fast feedback across multiple Python versions
2. **Lint and Quality**: Code formatting, style, and security checks
3. **Integration Tests**: Database and service integration
4. **End-to-End Tests**: Complete system validation
5. **Performance Tests**: Load testing and benchmarking
6. **Security Tests**: Vulnerability scanning
7. **Test Reporting**: Combined results and coverage

**Triggered On:**
- Push to `main` or `develop` branches
- Pull requests
- Daily scheduled runs (2 AM UTC)
- Manual workflow dispatch

### Test Environment Services

The CI/CD pipeline uses the following test services:

- **PostgreSQL**: Database testing
- **Redis**: Caching and session testing
- **Mock MaaS**: Simulated MaaS API responses
- **Mock FleetDM**: Simulated FleetDM integration
- **Elasticsearch**: Log aggregation testing
- **Prometheus**: Metrics collection testing
- **Grafana**: Dashboard and visualization testing

## Coverage Requirements

### Target Coverage: 80%+

The testing framework enforces a minimum 80% code coverage requirement across:

- **Line Coverage**: Percentage of code lines executed
- **Branch Coverage**: Percentage of code branches tested
- **Function Coverage**: Percentage of functions called

### Coverage Reports

Coverage reports are generated in multiple formats:

- **HTML Report**: `tests/reports/coverage_html/index.html`
- **XML Report**: `tests/reports/coverage.xml` (for CI/CD)
- **JSON Report**: `tests/reports/coverage.json` (for analysis)
- **Terminal Output**: Real-time coverage feedback

## Performance Benchmarks

### Load Testing Thresholds

Performance tests validate against these thresholds:

- **Average Response Time**: < 2.0 seconds
- **95th Percentile Response Time**: < 5.0 seconds
- **Error Rate**: < 5%
- **Throughput**: > 100 requests/second
- **Memory Growth**: < 500MB during load
- **Memory Leaks**: < 10% variance after cleanup

### Concurrent Testing

- **Concurrent Users**: 1, 5, 10, 25, 50, 100 users
- **Test Duration**: 60 seconds per load level
- **Deployment Concurrency**: Up to 5 simultaneous deployments
- **Database Performance**: Sub-100ms query times

## Security Testing

### Vulnerability Scanning

- **Trivy**: Container and filesystem vulnerability scanning
- **CodeQL**: Static code analysis for security issues
- **Bandit**: Python security linting
- **Safety**: Dependency vulnerability checking

### Security Validation

- **Authentication Testing**: JWT token validation and security
- **Authorization Testing**: Role-based access control
- **Input Validation**: SQL injection and XSS prevention
- **Cloud-init Security**: Template safety validation
- **Ansible Security**: Playbook security analysis

## Test Data and Fixtures

### Sample Data Generation

Test fixtures provide realistic sample data:

- **Servers**: Various configurations and states
- **Cloud-init Templates**: Valid and invalid templates
- **Ansible Playbooks**: Complex deployment scenarios
- **Network Configurations**: Static and dynamic setups
- **User Accounts**: Different roles and permissions

### Mock Services

Mock implementations for external dependencies:

- **MaaS API**: Complete mock API responses
- **FleetDM API**: OSQuery integration simulation  
- **Ansible Runner**: Playbook execution simulation
- **Redis Cache**: In-memory cache simulation
- **External APIs**: HTTP service mocking

## Troubleshooting

### Common Issues

**Test Environment Setup:**
```bash
# Rebuild test environment
./scripts/run_tests.sh --clean --setup-env

# Validate environment
./scripts/run_tests.sh --validate-env
```

**Database Issues:**
```bash
# Reset test database
docker-compose -f docker-compose.test.yml restart test-postgres

# Check database connectivity
./scripts/run_tests.sh --validate-env
```

**Permission Issues:**
```bash
# Fix script permissions
chmod +x scripts/run_tests.sh

# Fix directory permissions
chmod -R 755 tests/
```

### Debug Mode

Enable detailed test debugging:

```bash
# Verbose output
./scripts/run_tests.sh -v

# Keep test containers running
docker-compose -f docker-compose.test.yml up --no-deps test-runner

# Manual test execution
docker exec -it gough_test-runner_1 /bin/bash
pytest -v --tb=long tests/unit/
```

### Log Collection

Test logs and artifacts:

```bash
# View all test logs
ls -la tests/reports/

# Docker container logs
docker-compose -f docker-compose.test.yml logs

# CI/CD artifacts
# Available in GitHub Actions workflow runs
```

## Contributing

### Adding New Tests

1. **Create test files** following the naming convention `test_*.py`
2. **Use appropriate markers** to categorize tests
3. **Follow fixture patterns** for consistent test data
4. **Add documentation** for complex test scenarios
5. **Update this README** for significant additions

### Test Best Practices

- **Keep unit tests fast** (< 1 second each)
- **Use meaningful test names** describing the scenario
- **Mock external dependencies** in unit tests
- **Test both success and failure scenarios**
- **Maintain high code coverage** without sacrificing quality
- **Use parametrized tests** for multiple scenarios
- **Clean up resources** in test teardown

### Performance Guidelines

- **Mark slow tests** with `@pytest.mark.slow`
- **Use appropriate timeouts** for integration tests
- **Monitor test execution time** and optimize where needed
- **Parallelize tests** where possible
- **Cache expensive operations** in fixtures

## Support

For testing framework support:

1. **Check test logs** in `tests/reports/`
2. **Validate environment** with `--validate-env`
3. **Review CI/CD pipeline** results
4. **Consult test documentation** for specific scenarios
5. **Contact development team** for complex issues

---

**Test Framework Version**: 1.0.0  
**Last Updated**: September 2025  
**Minimum Coverage**: 80%  
**Python Version**: 3.8+