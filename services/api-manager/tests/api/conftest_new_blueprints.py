"""Pytest fixtures for new blueprint tests (migration, clusters, primary).

Provides a Quart test client with the three blueprints registered and auth stubbed.
"""

from types import SimpleNamespace
import pytest
from quart import Quart, g


@pytest.fixture()
def client():
    """Create a Quart test client with migration, clusters, primary blueprints."""
    from app.api.migration import migration_bp
    from app.api.clusters import clusters_bp
    from app.api.primary import primary_bp

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["CLUSTER_ID"] = "default"

    app.register_blueprint(migration_bp, url_prefix="/api/v1/migration")
    app.register_blueprint(clusters_bp, url_prefix="/api/v1/clusters")
    app.register_blueprint(primary_bp, url_prefix="/api/v1/primary")

    @app.before_request
    async def _inject_auth():
        """Stub authentication with default admin user."""
        g.current_user = {
            "id": 1,
            "username": "test-operator",
            "role": "admin",  # Full access for testing
            "_jwt_payload": {
                "sub": "test-operator",
                "tenant": "default",
                "scope": (
                    "gough.capacity.read gough.migration.policy "
                    "gough.migration.trigger gough.migration.override-lock "
                    "gough.storage.read gough.storage.configure "
                    "gough.cluster.read gough.cluster.admin gough.cluster.superadmin"
                ),
                "mfa": True,
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")
        g.mfa_verified = True  # Assume MFA for testing

    return app.test_client()
