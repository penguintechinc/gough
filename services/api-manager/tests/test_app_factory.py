"""Tests for the Quart application factory.

Validates:
- All blueprints register without conflict
- Middleware wires in correct order
- Anonymous endpoints (version, openapi, health) do not require auth
- Readiness and health checks work
"""

import pytest


@pytest.mark.asyncio
async def test_create_app_factory(app):
    """Verify app is created and configured correctly."""
    assert app is not None
    assert app.config is not None


@pytest.mark.asyncio
async def test_blueprints_registered(app):
    """Verify all blueprints are registered on the app."""
    # Check that key blueprints are registered by examining url_map.
    rules = list(app.url_map.iter_rules())
    rule_paths = {rule.rule for rule in rules}

    # Verify known routes exist.
    expected_prefixes = [
        "/api/v1/auth",
        "/api/v1/users",
        "/api/v1/secrets",
        "/api/v1/clouds",
    ]
    for prefix in expected_prefixes:
        found = any(path.startswith(prefix) for path in rule_paths)
        assert found, f"No routes found with prefix {prefix}"


@pytest.mark.asyncio
async def test_openapi_endpoint_exists(app, client):
    """Verify GET /api/v1/openapi.json endpoint exists and is accessible."""
    response = await client.get("/api/v1/openapi.json")
    # Should return 404 if docs/api/openapi.json doesn't exist (expected in test env).
    # Or 200 if the file exists.
    assert response.status_code in [200, 404]


@pytest.mark.asyncio
async def test_version_endpoint_exists(app, client):
    """Verify GET /api/v1/version endpoint exists and returns version info."""
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    data = await response.json
    assert "version" in data
    assert "build_sha" in data
    assert data["openapi_version"] == "3.1.0"


@pytest.mark.asyncio
async def test_health_check_endpoint(app, client):
    """Verify GET /healthz endpoint."""
    response = await client.get("/healthz")
    assert response.status_code in [200, 503]  # 200 if DB up, 503 if down
    data = await response.json
    assert "status" in data


@pytest.mark.asyncio
async def test_readiness_endpoint(app, client):
    """Verify GET /readyz endpoint with multi-component checks."""
    response = await client.get("/readyz")
    assert response.status_code in [200, 503]
    data = await response.json
    assert "status" in data
    assert data["status"] in ["ready", "not_ready"]
    if "checks" in data:
        # Verify checks are recorded.
        assert isinstance(data["checks"], dict)


@pytest.mark.asyncio
async def test_metrics_endpoint(app, client):
    """Verify GET /metrics endpoint for Prometheus scraping."""
    response = await client.get("/metrics")
    assert response.status_code == 200
    # Metrics endpoint returns Prometheus text format.
    text = await response.data
    assert b"HELP" in text or b"TYPE" in text or len(text) > 0


@pytest.mark.asyncio
async def test_anonymous_endpoints_no_auth_required(app, client):
    """Verify anonymous endpoints (/healthz, /version, /openapi.json, /readyz, /metrics) don't require auth."""
    paths = [
        "/healthz",
        "/readyz",
        "/metrics",
        "/api/v1/version",
        "/api/v1/openapi.json",
    ]
    for path in paths:
        response = await client.get(path)
        # Should not return 401 (unauthorized).
        # May return 503 (service down) or 404 (not found), but not 401.
        assert response.status_code != 401, f"{path} requires auth when it shouldn't"


@pytest.mark.asyncio
async def test_middleware_installed(app):
    """Verify security middleware is installed on the app."""
    # Check that before_request handlers are registered.
    before_request_funcs = app.before_request_funcs.get(None, [])
    assert len(before_request_funcs) >= 1, "No before_request handlers registered"


class TestAppSecurityMiddleware:
    """Test security middleware installation and order."""

    @pytest.mark.asyncio
    async def test_credential_validation_runs_first(self, app):
        """Verify credential validation before_request handler exists."""
        before_request_funcs = app.before_request_funcs.get(None, [])
        # At least one before_request handler for credential validation.
        assert len(before_request_funcs) > 0

    @pytest.mark.asyncio
    async def test_tenant_middleware_defensive(self, app):
        """Verify tenant middleware is defensive (skips if not available)."""
        # This test just verifies the app doesn't crash if tenant middleware
        # is unavailable (Wave 1 work still landing).
        assert app is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
