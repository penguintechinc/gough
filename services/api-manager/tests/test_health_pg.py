"""Real-Postgres tests proving health/ready liveness checks use ``executesql``.

``app.config["db"]`` is injected directly (bypassing the ``before_serving``
``init_db`` hook, which needs Vault/SPIRE/etc.), so these exercise exactly the
route handler's own ``SELECT 1`` liveness check against a real penguin-dal
``DB`` bound to Postgres -- proving the ``db.engine.connect()`` -> raw-SQLAlchemy
path is fully gone and ``db.executesql`` alone is sufficient for the route to
report healthy.
"""

from __future__ import annotations

import pytest

from app import create_app
from app.config import TestingConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/healthz"])
async def test_health_ok_with_real_db(pg_db, path):
    """Health aliases return 200 when ``db.executesql("SELECT 1")`` succeeds."""
    app = await create_app(TestingConfig)
    app.config["db"] = pg_db
    client = app.test_client()

    resp = await client.get(path)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["status"] == "healthy"
    assert data["database"] == "connected"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ready", "/readyz"])
async def test_ready_ok_with_real_db(pg_db, path):
    """Readiness aliases report database "up" when ``executesql`` succeeds."""
    app = await create_app(TestingConfig)
    app.config["db"] = pg_db
    client = app.test_client()

    resp = await client.get(path)

    data = await resp.get_json()
    assert data["checks"]["database"] == "up"
    # Overall status depends on other (Vault/SPIRE/NATS) checks, not asserted
    # here -- only the database liveness check is in scope for this task.


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/healthz"])
async def test_health_503_when_db_none(path):
    """Health aliases stay honest: 503 when ``app.config["db"]`` is None.

    No ``pg_db`` fixture here -- ``app.config`` has no "db" key at all since
    ``before_serving`` (which would call ``init_db``) never runs under a bare
    ``test_client()``, so ``get_db()`` returns None exactly as it would in a
    genuine degraded-database deployment.
    """
    app = await create_app(TestingConfig)
    client = app.test_client()

    resp = await client.get(path)

    assert resp.status_code == 503
    data = await resp.get_json()
    assert data["status"] == "unhealthy"
    assert data["database"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ready", "/readyz"])
async def test_ready_degraded_when_db_none(path):
    """Readiness aliases report degraded-mode database when db is None."""
    app = await create_app(TestingConfig)
    client = app.test_client()

    resp = await client.get(path)

    data = await resp.get_json()
    assert data["checks"]["database"] == "unavailable (degraded mode)"
