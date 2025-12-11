# Gough Development & CI/CD Standards

This document defines development, testing, and CI/CD standards for the Gough Hypervisor Automation System project.

## Table of Contents

1. [Version Management](#version-management)
2. [CI/CD Standards](#cicd-standards)
3. [Code Quality](#code-quality)
4. [Security Requirements](#security-requirements)
5. [Testing Standards](#testing-standards)
6. [Container Standards](#container-standards)
7. [Git Workflow](#git-workflow)

## Version Management

### Version Format

Gough uses semantic versioning with the following format:

```
vMajor.Minor.Patch
```

- **Major**: Breaking changes, API incompatibilities, architectural changes
- **Minor**: New features, significant enhancements, non-breaking changes
- **Patch**: Bug fixes, security patches, minor improvements

### .version File

The `.version` file in the repository root controls all versioning:

```bash
# Example: /home/penguin/code/Gough/.version
1.2.3
```

**Rules**:
- Must contain only semantic version (X.Y.Z format)
- No extra text or comments
- Whitespace is trimmed automatically
- Changing this file triggers releases and version builds

### Version Bumping

Update the `.version` file and commit separately:

```bash
# Bump patch version (1.2.3 -> 1.2.4)
echo "1.2.4" > .version
git add .version
git commit -m "Release v1.2.4"

# Bump minor version (1.2.3 -> 1.3.0)
echo "1.3.0" > .version
git add .version
git commit -m "Release v1.3.0"

# Bump major version (1.2.3 -> 2.0.0)
echo "2.0.0" > .version
git add .version
git commit -m "Release v2.0.0"
```

### Git Tagging

When a version is released, GitHub Actions automatically:
1. Detects `.version` file change
2. Creates pre-release on GitHub
3. Builds containers with version tags

**Manual tagging** (if needed):

```bash
git tag v1.2.3 -m "Release v1.2.3"
git push origin v1.2.3
```

## CI/CD Standards

### Workflow Structure

Gough maintains three core workflows:

1. **build-containers.yml** - Multi-service container builds
2. **tests.yml** - Comprehensive testing pipeline
3. **version-release.yml** - Automated releases

### Path Filters

All workflows use path filters to avoid unnecessary runs:

| Workflow | Paths |
|----------|-------|
| build-containers.yml | `.version`, `gough/containers/**`, `.github/workflows/build-containers.yml` |
| tests.yml | `gough/**`, `tests/**`, `.version`, `.github/workflows/tests.yml` |
| version-release.yml | `.version` (main branch only) |

### Build Outputs

All container builds produce:

- **Container Images**: Multi-arch (amd64/arm64) images to ghcr.io
- **Vulnerability Reports**: Trivy SARIF results
- **Software Bill of Materials**: SPDX JSON format
- **Build Artifacts**: Test reports and coverage metrics

### Tag Strategy

Image tags follow this pattern:

| Scenario | Tag Format |
|----------|-----------|
| Regular build (non-main) | `alpha-<epoch64>` |
| Regular build (main) | `beta-<epoch64>` |
| Version release (non-main) | `v<semver>-alpha` |
| Version release (main) | `v<semver>-beta` |
| Git tag release | `v<semver>`, `<major>`, `<major>.<minor>`, `latest` |

**Example**:
- Main branch, no version change: `beta-1702316400`
- Feature branch, no version change: `alpha-1702316400`
- Main branch, version change to 1.2.3: `v1.2.3-beta`

### Epoch64 Timestamps

All builds include Unix timestamps (epoch64) for identification:

```bash
# Generated in build jobs
EPOCH64=$(date +%s)  # Example output: 1702316400
```

Used in:
- Development image tags (`alpha-<epoch64>`, `beta-<epoch64>`)
- Build identification
- Debugging and tracing

## Code Quality

### Python Standards

Gough is primarily Python-based (py4web management server).

**Mandatory Linting**:
- **black**: Code formatting
- **isort**: Import sorting
- **flake8**: Style guide enforcement
- **mypy**: Type checking
- **pylint**: Code quality analysis
- **bandit**: Security scanning

**Configuration**:
- Line length: 100 characters
- Python version: 3.9+
- Type hints: Required for all functions

**Example Pre-commit Checklist**:
```bash
# Format code
black gough/ tests/

# Sort imports
isort gough/ tests/

# Lint
flake8 gough/ tests/
mypy gough/ --ignore-missing-imports
pylint gough/

# Security scan
bandit -r gough/
```

### Style Guidelines

**Code Organization**:
- One service per container
- Logical module structure
- Separate concerns (config, API, logic)
- Comprehensive docstrings

**Naming Conventions**:
- `snake_case` for variables and functions
- `PascalCase` for classes
- `UPPER_CASE` for constants
- Meaningful names (no single letters except `i`, `j`, `k` in loops)

**Documentation**:
- Module docstrings required
- Function docstrings (Google style)
- Complex logic requires inline comments
- Doctest examples encouraged

### Type Hints

All Python code requires type hints:

```python
from typing import Optional, List, Dict

def get_version() -> str:
    """Get application version."""
    return "1.2.3"

def process_nodes(nodes: List[Dict[str, any]]) -> Optional[bool]:
    """Process list of nodes."""
    return True if nodes else None
```

## Security Requirements

### Container Security

**Base Images**:
- Use official, minimal base images
- Prefer `ubuntu:24.04`, `python:3.12-slim`, `debian:bookworm-slim`
- Avoid Alpine for Python (glibc issues)
- Regular updates (watch for CVEs)

**Dockerfile Best Practices**:
- Run as non-root user
- Multi-stage builds when possible
- Minimal final image size
- Clear layer organization

**Example Dockerfile**:
```dockerfile
FROM python:3.12-slim

LABEL maintainer="sales@penguintech.io"
LABEL org.opencontainers.image.title="Gough Management Server"

# Create non-root user
RUN groupadd -r gough && useradd -r -g gough gough

WORKDIR /app

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY --chown=gough:gough . .

# Run as non-root
USER gough

EXPOSE 8000
CMD ["python", "-m", "py4web", "run", "py4web-app"]
```

### Vulnerability Scanning

**Trivy Container Scanning**:
- Runs on all container builds
- Minimum severity: HIGH
- Results uploaded to GitHub Security tab
- SARIFs stored as artifacts

**Dependency Scanning**:
- bandit for Python security
- safety for Python dependencies
- CodeQL for code analysis

### Supply Chain Security

**Software Bill of Materials (SBOM)**:
- Generated using Anchore for all builds
- SPDX JSON format
- Uploaded as artifacts
- Used for compliance and tracking

## Testing Standards

### Test Organization

```
tests/
├── unit/           # Unit tests (fast, isolated)
├── integration/    # Integration tests (services, databases)
├── e2e/           # End-to-end tests (full workflows)
├── performance/   # Performance benchmarks
├── conftest.py    # Shared fixtures
└── requirements.txt # Test dependencies
```

### Testing Requirements

**Unit Tests**:
- Cover all code paths
- Mock external dependencies
- Fast execution (<100ms per test)
- No network or database access

**Integration Tests**:
- Test component interactions
- Use real databases (in containers)
- Test API endpoints
- Moderate execution time (<5s per test)

**E2E Tests**:
- Test complete workflows
- Use full docker-compose environment
- Test critical user paths
- Slower but most realistic

**Coverage Requirements**:
- Minimum: 70% code coverage
- Target: 85%+ code coverage
- Required for `gough/` package (not tests/)
- Upload to Codecov on unit tests

### Running Tests Locally

```bash
# All tests
./scripts/run_tests.sh

# Unit tests only (fast)
./scripts/run_tests.sh -t unit

# Integration tests
./scripts/run_tests.sh -t integration

# Specific test file
pytest tests/unit/test_models.py -v

# With coverage
pytest tests/ --cov=gough --cov-report=html
```

### Test Fixtures

Common test fixtures (in `conftest.py`):
- Database connections
- Flask application
- Redis cache
- MaaS API mocks

## Container Standards

### Four Service Architecture

Gough consists of four containerized services:

| Service | Base Image | Purpose |
|---------|-----------|---------|
| **MaaS** | ubuntu:24.04 | Bare metal provisioning via PXE |
| **Management Server** | python:3.12-slim | py4web portal and APIs |
| **Agent** | ubuntu:24.04 | Monitoring and management agent |
| **FleetDM** | fleetdm/fleet:latest | OSQuery fleet management |

### Dockerfile Locations

```
gough/containers/
├── maas/
│   ├── Dockerfile
│   ├── config/
│   └── scripts/
├── management-server/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── py4web-app/
├── agent/
│   ├── Dockerfile
│   ├── config/
│   └── scripts/
└── fleetdm/
    ├── Dockerfile
    └── config/
```

### Image Naming Convention

```
ghcr.io/{github-org}/gough-{service}:{tag}
```

**Examples**:
- `ghcr.io/penguintechinc/gough-maas:v1.2.3-beta`
- `ghcr.io/penguintechinc/gough-management-server:beta-1702316400`
- `ghcr.io/penguintechinc/gough-agent:alpha-1702316400`
- `ghcr.io/penguintechinc/gough-fleetdm:latest`

### Container Labels (OCI Standards)

All containers must include OCI-compliant labels:

```dockerfile
LABEL org.opencontainers.image.title="Gough Service Name"
LABEL org.opencontainers.image.description="Service description"
LABEL org.opencontainers.image.vendor="Penguin Tech Inc"
LABEL org.opencontainers.image.licenses="AGPL-3.0"
LABEL org.opencontainers.image.version="1.2.3"
LABEL org.opencontainers.image.source="https://github.com/penguintechinc/gough"
LABEL org.opencontainers.image.documentation="https://github.com/penguintechinc/gough/tree/main/docs"
LABEL maintainer="sales@penguintech.io"
```

### Multi-Architecture Builds

**Supported Architectures**:
- `linux/amd64` (x86-64)
- `linux/arm64` (ARM 64-bit)

**Build Requirements**:
- Use Docker Buildx for multi-arch
- QEMU setup for cross-platform building
- Test both architectures before release
- GHA runners support both natively

## Git Workflow

### Branch Strategy

**Branch Types**:
- `main` - Production-ready code
- `v1.x` - Long-term support branch
- `develop` - Development integration
- `feature/name` - Feature branches
- `bugfix/name` - Bug fix branches
- `hotfix/name` - Production hotfixes

**Rules**:
- Never commit directly to `main`
- Use feature branches for all changes
- Create pull requests for review
- Require status checks to pass

### Commit Message Format

```
<type>: <subject>

<body>

<footer>
```

**Types**:
- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation
- `test:` Test additions/changes
- `ci:` CI/CD pipeline changes
- `chore:` Build, dependencies, etc.

**Examples**:
```
feat: Add cloud-init template editor to management server

fix: Correct MaaS API timeout handling

ci: Add gosec scanning to Go workflows

docs: Update deployment guide
```

### Pull Requests

**Requirements**:
- Descriptive title and description
- Linked to relevant issues
- All status checks pass
- Code review approval required
- Up-to-date with main branch

**Pre-merge Checklist**:
- All tests passing
- Code reviewed
- No security warnings
- Documentation updated
- Version updated (if applicable)

### Tag Format

Git tags follow semantic versioning:

```
v1.2.3          # Release version
v1.2.3-rc.1     # Release candidate
v1.2.3-alpha.1  # Alpha version
v1.2.3-beta.1   # Beta version
```

## Pre-Commit Checklist

Before pushing code, verify:

- [ ] All tests pass locally: `./scripts/run_tests.sh`
- [ ] Linting passes: `black`, `flake8`, `mypy`
- [ ] Security scans clean: `bandit`, `safety`
- [ ] Type hints present: `mypy gough/`
- [ ] No hardcoded secrets or credentials
- [ ] Documentation updated
- [ ] `.version` updated (if releasing)
- [ ] Commit messages follow format

## Common Patterns

### Adding a New Container Service

1. Create service directory: `gough/containers/new-service/`
2. Write Dockerfile with OCI labels
3. Create configuration files
4. Update workflow matrix in `build-containers.yml`
5. Add tests in `tests/new-service/`
6. Document in `/docs/`

### Deploying a New Version

1. Commit code changes to feature branch
2. Create pull request and get approval
3. Merge to `develop` or `main`
4. Update `.version` file with new version
5. Commit and push `.version` change
6. GitHub Actions auto-creates release
7. Monitor build and test workflows

### Responding to Security Issues

1. Create `hotfix/CVE-XXXX` branch from main
2. Fix vulnerability
3. Add tests validating the fix
4. Update `.version` with patch bump
5. Merge to main (expedited review)
6. Announce security advisory

## Monitoring & Observability

### Workflow Monitoring

- Check GitHub Actions tab for status
- Review workflow run details
- Monitor build times
- Track failure rates

### Security Monitoring

- Review Trivy scan results
- Check CodeQL alerts
- Monitor Dependabot PRs
- Review bandit reports

### Performance Monitoring

- Track build times
- Monitor image size
- Review test execution times
- Track coverage trends

## Troubleshooting Guide

### Build Issues

**Problem**: Build fails with "Dockerfile not found"
**Solution**: Workflow creates default Dockerfiles, verify in build logs

**Problem**: Multi-arch build is slow
**Solution**: QEMU emulation is slow, use native runners when possible

**Problem**: Image push fails
**Solution**: Check GitHub token permissions, verify registry credentials

### Test Issues

**Problem**: Tests fail locally but pass in CI
**Solution**: Check Python version, install test dependencies, check env vars

**Problem**: Coverage is below threshold
**Solution**: Add unit tests for uncovered code paths

**Problem**: Integration tests timeout
**Solution**: Increase timeout in workflow, check service startup time

### Release Issues

**Problem**: Release not created when `.version` changes
**Solution**: Verify branch is main, version not 0.0.0, check git diff

**Problem**: Wrong version in release
**Solution**: Check `.version` file content, re-release with correct version

## Related Documentation

- **Workflows Documentation**: `/docs/WORKFLOWS.md`
- **Architecture Guide**: `/docs/architecture/`
- **Deployment Guide**: `/docs/deployment/`
- **Security Best Practices**: `/docs/security/`

---

**Last Updated**: 2025-12-11
**Version**: 1.0.0
**Maintained by**: Penguin Tech Inc
