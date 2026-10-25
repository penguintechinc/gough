# Gough Testing Suite

Comprehensive testing infrastructure for local (alpha) and staging (beta) validation, plus Kubernetes deployment tests.

## Test Types

### 1. Alpha Smoke Tests (Local Validation)
Full pre-commit validation on local environment.

**Run via**:
```bash
make smoke-test             # Full tests (build + run + security + lint)
make smoke-test-quick       # Skip builds (faster)
```

**Test Phases**: Build, Runtime, API, Security, Linting, Unit Tests

### 2. Beta Smoke Tests (Staging)
Validate https://gough.penguintech.io

**Run via**: `make smoke-test-beta`

### 3. K8s Deployment
Build images and deploy to cluster via the Helm umbrella chart `k8s/helm/gough`.

**Run via** (Helm only; no raw manifests or kubectl apply):
```bash
make deploy-alpha                       # push images to ghcr.io + Helm upgrade --install to dal2alpha
```

## Quick Reference

| Test | Command | Duration |
|------|---------|----------|
| Alpha (full) | `make smoke-test` | ~5 min |
| Alpha (quick) | `make smoke-test-quick` | ~1 min |
| Beta | `make smoke-test-beta` | ~30 sec |
| K8s Deploy | `make deploy-alpha` | ~10 min |

See tests/smoke/ for details.
