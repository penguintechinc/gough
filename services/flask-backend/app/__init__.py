"""Flask Backend Application Factory for Gough.

This module creates and configures the Flask application with:
- Flask-Security-Too for authentication (mandatory per CLAUDE.md)
- PyDAL for database operations
- CORS for cross-origin requests
- Prometheus metrics for monitoring
- Audit logging for security events
- Rate limiting for API protection
"""

from flask import Flask
from flask_cors import CORS
from flask_security import Security
from prometheus_client import make_wsgi_app
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from .config import Config
from .models import init_db, get_db
from .security_datastore import PyDALUserDatastore
from .audit import init_audit_logger
from .rate_limit import init_rate_limiter
from .ssh_ca import SSHCertificateAuthority
from .websocket import init_websocket


def create_app(config_class: type = Config) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Initialize CORS
    CORS(app, resources={
        r"/api/*": {
            "origins": app.config.get("CORS_ORIGINS", "*"),
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization"],
        }
    })

    # Initialize database
    with app.app_context():
        db = init_db(app)

        # Initialize Flask-Security-Too with PyDAL datastore
        user_datastore = PyDALUserDatastore(db)
        security = Security(app, user_datastore)

        # Store datastore in app for access in blueprints
        app.user_datastore = user_datastore
        app.security = security

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

        # Create default admin user if none exists
        _create_default_admin(user_datastore, db)

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

    app.register_blueprint(auth_bp, url_prefix="/api/v1/auth")
    app.register_blueprint(users_bp, url_prefix="/api/v1/users")
    app.register_blueprint(hello_bp, url_prefix="/api/v1")
    app.register_blueprint(secrets_bp, url_prefix="/api/v1/secrets")
    app.register_blueprint(clouds_bp, url_prefix="/api/v1/clouds")
    app.register_blueprint(teams_bp, url_prefix="/api/v1/teams")
    app.register_blueprint(ssh_ca_bp, url_prefix="/api/v1/ssh-ca")
    app.register_blueprint(shell_bp, url_prefix="/api/v1/shell")
    app.register_blueprint(agents_bp, url_prefix="/api/v1/agents")

    # Health check endpoint
    @app.route("/healthz")
    def health_check():
        """Health check endpoint."""
        try:
            db = get_db()
            db.executesql("SELECT 1")
            return {"status": "healthy", "database": "connected"}, 200
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}, 503

    # Readiness check endpoint
    @app.route("/readyz")
    def readiness_check():
        """Readiness check endpoint."""
        return {"status": "ready"}, 200

    # Add Prometheus metrics endpoint
    app.wsgi_app = DispatcherMiddleware(
        app.wsgi_app,
        {"/metrics": make_wsgi_app()}
    )

    return app


def _create_default_admin(user_datastore: PyDALUserDatastore, db) -> None:
    """Create default admin user if no users exist."""
    import os
    from flask_security.utils import hash_password

    # Check if any users exist
    user_count = db(db.auth_user).count()
    if user_count > 0:
        return

    # Get admin credentials from environment or use defaults
    admin_email = os.getenv("ADMIN_EMAIL", "admin@gough.local")
    admin_password = os.getenv("ADMIN_PASSWORD", "changeme123")

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
        password=hash_password(admin_password),
        full_name="System Administrator",
        roles=[admin_role],
        active=True,
    )

    db.commit()
