"""Quart Backend Application Factory for Gough.

This module creates and configures the Quart application with:
- JWT-based authentication with PyDAL user datastore
- PyDAL for database operations
- CORS for cross-origin requests
- Prometheus metrics for monitoring
- Audit logging for security events
- Rate limiting for API protection
"""

import os
import bcrypt
from quart import Quart, Response
from quart_cors import cors
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from .config import Config
from .models import init_db, get_db
from .security_datastore import PyDALUserDatastore
from .audit import init_audit_logger
from .rate_limit import init_rate_limiter
from .ssh_ca import SSHCertificateAuthority
from .websocket import init_websocket


async def create_app(config_class: type = Config) -> Quart:
    """Create and configure the Quart application."""
    app = Quart(__name__, static_folder=None)  # Disable static files initially
    app.config.from_object(config_class)

    # Initialize CORS
    app = cors(app, allow_origin=app.config.get("CORS_ORIGINS", "*"),
               allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
               allow_headers=["Content-Type", "Authorization"])

    # Initialize database on startup
    @app.before_serving
    async def startup():
        """Initialize components before serving requests."""
        db = init_db(app)

        # Initialize JWT-based authentication with PyDAL datastore
        user_datastore = PyDALUserDatastore(db)

        # Store datastore in app for access in blueprints
        app.user_datastore = user_datastore

        # Initialize audit logger
        if app.config.get("AUDIT_ENABLED", True):
            init_audit_logger(app)

        # Initialize rate limiter
        if app.config.get("RATE_LIMIT_ENABLED", True):
            init_rate_limiter(app)

        # Initialize SSH Certificate Authority
        ssh_ca = SSHCertificateAuthority(app)
        app.ssh_ca = ssh_ca

        # Initialize WebSocket support
        socketio = init_websocket(app)
        app.socketio = socketio

        # Note: Default admin is created by SQLAlchemy during schema initialization

    # Register blueprints
    from .auth import auth_bp
    from .users import users_bp
    from .hello import hello_bp
    from .api.secrets import secrets_bp
    from .api.clouds import clouds_bp
    from .api.teams import teams_bp
    from .api.ssh_ca import ssh_ca_bp
    from .api.shell import shell_bp
    from .api.agents import agents_bp
    from .api.storage import storage_bp

    app.register_blueprint(auth_bp, url_prefix="/api/v1/auth")
    app.register_blueprint(users_bp, url_prefix="/api/v1/users")
    app.register_blueprint(hello_bp, url_prefix="/api/v1")
    app.register_blueprint(secrets_bp, url_prefix="/api/v1/secrets")
    app.register_blueprint(clouds_bp, url_prefix="/api/v1/clouds")
    app.register_blueprint(teams_bp, url_prefix="/api/v1/teams")
    app.register_blueprint(ssh_ca_bp, url_prefix="/api/v1/ssh-ca")
    app.register_blueprint(shell_bp, url_prefix="/api/v1/shell")
    app.register_blueprint(agents_bp, url_prefix="/api/v1/agents")
    app.register_blueprint(storage_bp, url_prefix="/api/v1/storage")

    # Health check endpoint
    @app.route("/healthz")
    async def health_check():
        """Health check endpoint."""
        try:
            db = get_db()
            db.executesql("SELECT 1")
            return {"status": "healthy", "database": "connected"}, 200
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}, 503

    # Readiness check endpoint
    @app.route("/readyz")
    async def readiness_check():
        """Readiness check endpoint."""
        return {"status": "ready"}, 200

    # Prometheus metrics endpoint for ASGI
    @app.route("/metrics")
    async def metrics():
        """Prometheus metrics endpoint."""
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    return app


def _create_default_admin(user_datastore: PyDALUserDatastore, db) -> None:
    """Create default admin user if no users exist."""
    # Check if any users exist
    user_count = db(db.auth_user).count()
    if user_count > 0:
        return

    # Get admin credentials from environment or use defaults
    admin_email = os.getenv("ADMIN_EMAIL", "admin@gough.local")
    admin_password = os.getenv("ADMIN_PASSWORD", "changeme123")

    # Hash password using bcrypt
    password_hash = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode()

    # Find or create admin role
    admin_role = user_datastore.find_role("admin")
    if not admin_role:
        admin_role = user_datastore.create_role(
            name="admin",
            description="Full system access"
        )

    # Create admin user
    user_datastore.create_user(
        email=admin_email,
        password=password_hash,
        full_name="System Administrator",
        roles=[admin_role],
        active=True,
    )

    db.commit()
