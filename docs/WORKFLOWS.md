# Gough CI/CD Workflows Documentation

This document describes the GitHub Actions workflows used in the Gough Hypervisor Automation System project for continuous integration, testing, building, and releasing containerized services.

## Overview

Gough uses a multi-service containerized architecture with the following four main services:

1. **MaaS Container** - Ubuntu MaaS server for bare metal provisioning
2. **Management Server** - Python/py4web management portal
3. **Agent Container** - Monitoring and management agent
4. **FleetDM Server** - OSQuery fleet management

All workflows implement `.WORKFLOW` compliance with version management, epoch64 timestamps, and comprehensive security scanning.

## Version Management System

### Format

```
vMajor.Minor.Patch
```

- **Major**: Breaking changes, API changes, removed features
- **Minor**: Significant new features and functionality additions
- **Patch**: Minor updates, bug fixes, security patches

### `.version` File

The `.version` file in the repository root controls versioning and triggers release workflows:

```bash
# Set version
echo "1.2.3" > .version
git add .version
git commit -m "Release v1.2.3"
git push
```

### Epoch64 Timestamps

All builds include an epoch64 timestamp (Unix timestamp in seconds) for build identification:
- Generated automatically in workflows
- Used in container image tags for non-version builds
- Format: `alpha-<epoch64>` (non-main) or `beta-<epoch64>` (main)

## Workflows

### 1. build-containers.yml

**Purpose**: Build and push multi-architecture container images for all four Gough services.

**Trigger Events**:
- Push to `main`, `v1.x`, `develop`, or `feature/**` branches
- Git tags matching `v*.*.*`
- Path changes: `.version`, `gough/containers/**`, `.github/workflows/build-containers.yml`
- Manual workflow dispatch

**Services Built**:
- `gough-maas` (Ubuntu MaaS server)
- `gough-management-server` (py4web portal)
- `gough-agent` (Monitoring agent)
- `gough-fleetdm` (FleetDM/OSQuery)

**Architecture Support**: `linux/amd64`, `linux/arm64` (multi-arch builds)

#### Build Matrix Strategy

The workflow uses a matrix strategy to build all services in parallel. The `build-matrix` job generates:

1. **Container Matrix**: Defines all services with their contexts, Dockerfiles, and platforms
2. **Epoch64 Timestamp**: Unix timestamp for build identification
3. **Version Detection**: Reads `.version` file and detects changes

#### Image Tagging Strategy

Image tags depend on branch and version file status:

| Scenario | Main Branch | Other Branches |
|----------|-------------|-----------------|
| Regular build (no `.version` change) | `beta-<epoch64>` | `alpha-<epoch64>` |
| Version release (`.version` changed) | `v<semver>-beta` | `v<semver>-alpha` |
| Tagged release (git tag) | `v<semver>`, `<major>.<minor>`, `<major>`, `latest` | N/A |

**Example**: Updating `.version` to `1.2.0` on main branch creates images tagged:
- `v1.2.0-beta`
- `main-<sha>`
- Plus other metadata tags

#### Jobs

**build-matrix**:
- Generates container list and metadata
- Calculates epoch64 timestamp
- Detects version file changes
- Outputs shared data for build job

**build-containers** (matrix):
- Sets up Docker and QEMU for multi-arch builds
- Authenticates with GitHub Container Registry
- Extracts metadata and generates tags
- Checks/creates default Dockerfiles if missing
- Builds and pushes multi-arch images
- Runs Trivy vulnerability scanning
- Generates Software Bill of Materials (SBOM)

**create-manifest**:
- Creates multi-arch manifest lists
- Annotates architectures (amd64, arm64)
- Pushes manifests to registry

**update-compose-files**:
- Updates `docker-compose.yml` files with new tags
- Commits and pushes changes automatically

**release-summary**:
- Generates build summary in workflow run

#### Security Scanning

- **Trivy**: Container vulnerability scanning (CRITICAL, HIGH severity)
- **SBOM**: Software Bill of Materials (SPDX format)
- **Results**: Uploaded to GitHub security tab

#### Build Arguments

Each container build receives:
- `VERSION`: Current semantic version from `.version` file
- `BUILD_DATE`: Commit timestamp
- `VCS_REF`: Git commit SHA

### 2. tests.yml

**Purpose**: Comprehensive testing pipeline with unit, integration, E2E, performance, and security tests.

**Trigger Events**:
- Push to `main` or `develop` branches
- Pull requests to `main` or `develop`
- Daily schedule at 2 AM UTC
- Path changes: `gough/**`, `tests/**`, `.version`, `.github/workflows/tests.yml`

#### Test Jobs

**unit-tests**:
- Python versions: 3.8, 3.9, 3.10, 3.11
- Caches pip dependencies
- Runs unit test suite
- Uploads coverage reports
- Uploads to Codecov

**lint-and-quality**:
- Black (code formatting)
- isort (import sorting)
- flake8 (style guide)
- mypy (type checking)
- pylint (code analysis)
- bandit (security analysis)
- safety (dependency security)
- Uploads all lint results

**integration-tests**:
- Requires unit-tests to pass
- Services:
  - PostgreSQL 13 (database)
  - Redis 7 (cache)
- Waits for service health
- Runs integration test suite

**e2e-tests**:
- Requires unit-tests and integration-tests
- Runs on push or schedule only
- Starts full test environment with docker-compose
- Collects logs on failure
- Cleans up test environment

**performance-tests**:
- Runs on schedule or with `[perf]` commit message
- Uses Locust for load testing
- Validates performance thresholds
- Detects performance regressions

**security-tests**:
- Trivy vulnerability scanner (filesystem)
- CodeQL analysis (Python)

**test-report**:
- Downloads all test artifacts
- Combines test results
- Calculates summary statistics
- Creates pull request comment with results
- Fails workflow if tests failed

### 3. version-release.yml

**Purpose**: Automatically create GitHub pre-releases when `.version` file is updated.

**Trigger Events**:
- Push to `main` branch
- Path changes: `.version`

#### Release Behavior

- Reads semantic version from `.version` file
- Skips release if version is `0.0.0` (default)
- Checks if release already exists (prevents duplicates)
- Generates release notes with version details
- Creates pre-release on GitHub
- Target: Current git commit

#### Release Notes

Generated release notes include:
- Semantic version
- Full version with build info
- Commit hash
- Branch name
- Note about automatic generation

## Path Filters

All workflows use path filters for efficiency:

### build-containers.yml Paths
- `.version` - Version file changes trigger builds
- `gough/containers/**` - Changes to container definitions
- `.github/workflows/build-containers.yml` - Workflow changes

### tests.yml Paths
- `gough/**` - Source code changes
- `tests/**` - Test changes
- `.version` - Version changes
- `.github/workflows/tests.yml` - Workflow changes

### version-release.yml Paths
- `.version` - Only triggers on version file changes

## Version Detection Logic

All workflows implement consistent version detection:

```bash
# Read .version file
if [ -f .version ]; then
  VERSION=$(cat .version | tr -d '[:space:]')
  # Extract semantic version (X.Y.Z)
  SEMVER=$(echo "$VERSION" | cut -d'.' -f1-3)

  # Check if .version changed in latest commit
  if git diff --name-only HEAD^ HEAD | grep -q "^.version$"; then
    VERSION_CHANGED=true
  else
    VERSION_CHANGED=false
  fi
else
  SEMVER=0.0.0
  VERSION_CHANGED=false
fi
```

## Container Image Registry

**Registry**: GitHub Container Registry (ghcr.io)

**Image Naming**:
```
ghcr.io/{owner}/gough-{service}:{tag}
```

**Services**:
- `ghcr.io/{owner}/gough-maas`
- `ghcr.io/{owner}/gough-management-server`
- `ghcr.io/{owner}/gough-agent`
- `ghcr.io/{owner}/gough-fleetdm`

**Authentication**: Uses `GITHUB_TOKEN` for pushing

## Common Scenarios

### Scenario 1: Regular Development Build

1. Developer pushes code to `feature/my-feature` branch
2. Workflow triggers with path changes
3. Containers built and tagged as `alpha-<epoch64>`
4. Containers pushed to ghcr.io (if not PR)
5. Tests run on unit/integration/lint
6. Trivy scanning executes
7. SBOM generated

### Scenario 2: Version Release

1. Developer updates `.version` file to `1.2.3`
2. Commits and pushes to main branch
3. `build-containers.yml` triggers:
   - Detects `.version` change
   - Builds images tagged `v1.2.3-beta`
   - Pushes to registry
4. `version-release.yml` triggers:
   - Reads version from `.version`
   - Creates GitHub pre-release `v1.2.3`
   - Generates release notes

### Scenario 3: Testing Pipeline

1. Pull request opened against main
2. Triggers `tests.yml`
3. Runs all test jobs in parallel
4. Creates PR comment with results
5. Fails if tests fail
6. Passes if all tests pass

## Security Considerations

### Container Security
- Multi-arch verification (amd64/arm64)
- Trivy vulnerability scanning (CRITICAL/HIGH)
- SBOM generation for supply chain transparency
- Labels with license info (AGPL-3.0)

### Code Security
- CodeQL analysis (Python)
- bandit security scanning
- safety dependency checking
- gosec for Go code (if applicable)

### Secrets Management
- Uses `GITHUB_TOKEN` (auto-provided)
- No hardcoded credentials
- Registry authentication via GitHub Actions

## Troubleshooting

### Build Failures

**Check**:
1. Path filters - verify paths match trigger
2. Dockerfile existence - workflow creates defaults
3. Docker build context - verify relative paths
4. Resource limits - QEMU multi-arch builds use resources

**Debug**:
```bash
# Manual build
docker build -t gough-maas:test gough/containers/maas/
docker buildx build --platform linux/amd64,linux/arm64 \
  -t gough-maas:test gough/containers/maas/
```

### Test Failures

**Check**:
1. Test dependencies installed
2. Service health (postgres, redis running)
3. Database migrations applied
4. Environment variables set

**Debug**:
```bash
# Run tests locally
pytest tests/ -v
./scripts/run_tests.sh -t unit -e ci
```

### Release Not Created

**Check**:
1. `.version` file changed (git shows in diff)
2. Commit pushed to main branch
3. Release doesn't already exist
4. Version not `0.0.0` (default)

**Debug**:
```bash
# Check version
cat .version

# Check git diff
git diff HEAD^ HEAD -- .version

# Check releases
gh release list
```

## Best Practices

### Version Management
- Always use semantic versioning in `.version`
- Commit `.version` separately from code changes
- Document version bumps in commit message
- Use meaningful version increments (not random)

### Building
- Test locally before pushing
- Use feature branches for development
- Keep container Dockerfiles updated
- Monitor build times (multi-arch takes longer)

### Testing
- Run tests locally: `./scripts/run_tests.sh`
- Monitor coverage metrics
- Add tests for new features
- Review security scanner results

### Releasing
- Update `.version` file to desired version
- Push to main branch
- Verify release created on GitHub
- Monitor container image availability

## Related Documentation

- **System Architecture**: `/docs/architecture/`
- **Container Guide**: `/docs/deployment/`
- **Security Best Practices**: `/docs/security/`
- **Operations Manual**: `/docs/operations/`

---

**Last Updated**: 2025-12-11
**Version**: 1.0.0
**Maintained by**: Penguin Tech Inc
