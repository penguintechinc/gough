"""Quart Backend Application Factory for Gough.

This module creates and configures the Quart application with:
- JWT-based authentication with penguin-dal user datastore
- penguin-dal for database operations
- CORS for cross-origin requests
- Prometheus metrics for monitoring
- Audit logging for security events
- Rate limiting for API protection
"""

import os
import json
import bcrypt
import yaml
from pathlib import Path
from quart import Quart, Response
from quart_cors import cors
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from penguin_aaa.middleware.asgi import AuditMiddleware, OIDCAuthMiddleware
from penguin_aaa.audit.emitter import Emitter

from .config import Config
from .db.run_db import run_db
from .models import init_db, get_db
from .security_datastore import PyDALUserDatastore
from .audit import init_audit_logger
from .rate_limit import init_rate_limiter, install_global_rate_limiting
from .ssh_ca import SSHCertificateAuthority
from .websocket import init_websocket
from .middleware import CookieAuthShimMiddleware, wire_middleware
from .catalog import seed_builtin_biomes


def _resolve_cors_origins(app: Quart) -> list[str]:
    """Resolve CORS allow-origin list, fail-closed by default.

    Regression: audit cors-wildcard 2026-09-22. ``CORS_ORIGINS`` unset means
    "no explicit operator choice": under DEBUG/TESTING that resolves to a
    permissive ``["*"]`` (local dev, test suites), otherwise to ``[]`` (no
    cross-origin access -- same-origin only) so production never silently
    defaults to a wildcard. An operator-set value is always honored as-is
    (comma-separated list, or the literal ``*``) -- that is a deliberate
    choice, not a default.
    """
    raw = app.config.get("CORS_ORIGINS") or ""
    if raw:
        if raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    if app.config.get("DEBUG") or app.config.get("TESTING"):
        return ["*"]
    return []


async def create_app(config_class: type = Config) -> Quart:
    """Create and configure the Quart application."""
    app = Quart(__name__, static_folder=None)  # Disable static files initially
    app.config.from_object(config_class)

    # Validate secrets at startup to prevent production with dev defaults
    config_class.validate_secrets()

    # Initialize CORS (fail-closed default; see _resolve_cors_origins).
    app = cors(app, allow_origin=_resolve_cors_origins(app),
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

        # Initialize audit logger (graceful degradation if fails)
        if app.config.get("AUDIT_ENABLED", True):
            try:
                init_audit_logger(app)
            except Exception as e:
                import sys
                print(f"WARNING: Audit logger initialization failed: {e}", file=sys.stderr)
                print("Continuing without audit logger", file=sys.stderr)

        # Initialize rate limiter (graceful degradation if fails). Also installs
        # the global /api/ rate-limit floor (regression: security audit
        # 2026-09-22 -- only 6 of 106 input endpoints previously carried an
        # explicit @rate_limit decorator); existing per-route decorators keep
        # applying on top of this floor.
        if app.config.get("RATE_LIMIT_ENABLED", True):
            try:
                init_rate_limiter(app)
                install_global_rate_limiting(app)
            except Exception as e:
                import sys
                print(f"WARNING: Rate limiter initialization failed: {e}", file=sys.stderr)
                print("Continuing without rate limiter", file=sys.stderr)

        # Initialize SSH Certificate Authority
        ssh_ca = SSHCertificateAuthority(app)
        app.ssh_ca = ssh_ca

        # Initialize WebSocket support
        socketio = init_websocket(app)
        app.socketio = socketio

        # Seed built-in biomes (idempotent, only if DB available)
        if db is not None:
            try:
                seed_builtin_biomes(db)
            except Exception as e:
                import sys
                print(f"WARNING: Failed to seed built-in biomes: {e}", file=sys.stderr)

        # Note: Default admin is created by SQLAlchemy during schema initialization

    # Initialize capacity predictor (used by /capacity/* endpoints)
    @app.before_serving
    async def _init_capacity_predictor():
        """Initialize WaddleAI client and capacity predictor (gracefully degrade if unavailable)."""
        try:
            from .clients.vault import VaultClient
            from .clients.waddleai import WaddleAIClient
            from .clients.prometheus import PrometheusClient
            from .workers.capacity_predictor import CapacityPredictor
            import redis.asyncio as redis

            vault = VaultClient()
            waddleai_endpoint = app.config.get(
                "WADDLEAI_ENDPOINT", "https://waddleai.cluster.svc:8443"
            )
            waddleai_cluster_id = app.config.get("CLUSTER_ID", "dal2")
            prometheus_endpoint = app.config.get(
                "PROMETHEUS_ENDPOINT", "http://prometheus:9090"
            )

            waddleai = WaddleAIClient(
                endpoint=waddleai_endpoint,
                vault_client=vault,
                cluster_id=waddleai_cluster_id,
            )
            prometheus = PrometheusClient(endpoint=prometheus_endpoint)

            # Optional Redis cache (None disables caching)
            redis_client = None
            redis_url = app.config.get("REDIS_URL")
            if redis_url:
                try:
                    redis_client = await redis.from_url(redis_url)
                except Exception as e:
                    app.logger.warning("Redis connection failed; caching disabled: %s", e)

            predictor = CapacityPredictor(
                db_session=app.config.get("db"),
                prometheus_client=prometheus,
                waddleai_client=waddleai,
                redis_client=redis_client,
            )
            app.capacity_predictor = predictor
        except Exception as e:
            app.logger.warning("Capacity predictor initialization failed: %s", e)
            app.logger.warning("Continuing without capacity predictor (degraded mode)")

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
    from .api.ipxe import ipxe_bp
    from .api.webhooks import webhooks_bp
    from .api.biomes import biomes_bp
    from .api.nodes import nodes_bp
    from .api.disks import disks_bp
    from .api.joiner_secrets import joiner_secrets_bp
    from .api.audit import audit_bp
    from .api.capacity import capacity_bp
    from .api.integrations import integrations_bp
    from .api.migration import migration_bp
    from .api.clusters import clusters_bp
    from .api.primary import primary_bp
    from .api.vault import vault_bp

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
    app.register_blueprint(ipxe_bp, url_prefix="/api/v1/ipxe")
    app.register_blueprint(webhooks_bp, url_prefix="/api/v1/webhooks")
    # Biomes blueprint declares its own ``/api/v1/biomes`` prefix internally.
    app.register_blueprint(biomes_bp)
    app.register_blueprint(nodes_bp)
    app.register_blueprint(disks_bp, url_prefix="/api/v1/nodes")
    app.register_blueprint(joiner_secrets_bp, url_prefix="/api/v1")
    app.register_blueprint(audit_bp, url_prefix="/api/v1/audit")
    app.register_blueprint(capacity_bp)
    app.register_blueprint(integrations_bp, url_prefix="/api/v1/integrations")
    app.register_blueprint(migration_bp, url_prefix="/api/v1/migration")
    app.register_blueprint(clusters_bp, url_prefix="/api/v1/clusters")
    app.register_blueprint(primary_bp, url_prefix="/api/v1/primary")
    app.register_blueprint(vault_bp, url_prefix="/api/v1/vault")

    # Wire authentication + authorization middleware (defensive if Wave 1 not ready).
    await wire_middleware(app)

    # Register metrics instrumentation middleware
    from .middleware import _record_request_metrics
    app.after_request(_record_request_metrics)

    # OpenAPI spec endpoint. Requires a valid bearer token (regression: audit
    # openapi-anon 2026-09-22) -- the full 176-route spec is a reconnaissance
    # map for an attacker and must not be servable anonymously. Auth is
    # enforced by the ASGI OIDCAuthMiddleware (this path was removed from
    # ANONYMOUS_PATHS) + fail-closed scope enforcement (registered in
    # SCOPE_POLICY with an empty required-scope set: any authenticated
    # principal, no specific scope needed). See app/security/scope_policy.py.
    @app.route("/api/v1/openapi.json")
    async def openapi_json():
        """Serve the pre-generated OpenAPI 3.1 spec."""
        # parents[3] == <repo-root> from services/api-manager/app/__init__.py
        # (matches app/openapi_export.py's _default_out_dir(); parents[2] was
        # an off-by-one that resolved to services/docs/api/... and could
        # never find the file -- found while regression-testing Fix 2).
        spec_path = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"
        if spec_path.exists():
            spec_text = spec_path.read_text(encoding="utf-8")
            return Response(spec_text, mimetype="application/json")
        return {"error": "OpenAPI spec not found"}, 404

    # OpenAPI spec YAML endpoint. Same auth requirement as openapi.json above
    # (regression: audit openapi-anon 2026-09-22).
    @app.route("/api/v1/openapi.yaml")
    async def openapi_yaml():
        """Serve the OpenAPI 3.1 spec in YAML format."""
        # See parents[3] note in openapi_json() above.
        spec_path = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"
        if spec_path.exists():
            with open(spec_path, encoding="utf-8") as f:
                spec_dict = json.load(f)
            spec_yaml = yaml.dump(spec_dict, default_flow_style=False, sort_keys=False)
            return Response(spec_yaml, mimetype="application/yaml")
        return {"error": "OpenAPI spec not found"}, 404

    # Health check aliases
    @app.route("/health")
    async def health_alias():
        """Alias for /healthz endpoint."""
        try:
            db = get_db()
            if db is None:
                # Degraded mode: DB not available but app is still running
                return {"status": "unhealthy", "database": "unavailable"}, 503
            # Regression: gh-22. Off the event loop via run_db() instead
            # of blocking the request coroutine inline.
            await run_db(lambda: db.executesql("SELECT 1"))
            return {"status": "healthy", "database": "connected"}, 200
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}, 503

    @app.route("/ready")
    async def ready_alias():
        """Alias for /readyz endpoint."""
        readiness_state = {"status": "ready", "checks": {}}

        # Check Database (graceful if unavailable). Regression: gh-22.
        # Off the event loop via run_db() instead of blocking the request
        # coroutine inline.
        try:
            db = get_db()
            if db is None:
                readiness_state["checks"]["database"] = "unavailable (degraded mode)"
            else:
                await run_db(lambda: db.executesql("SELECT 1"))
                readiness_state["checks"]["database"] = "up"
        except Exception as e:
            readiness_state["status"] = "not_ready"
            readiness_state["checks"]["database"] = f"down: {str(e)[:50]}"

        # Check Vault (defensive: may not be initialized in dev).
        try:
            from .clients.vault import VaultClient  # noqa: F401
            readiness_state["checks"]["vault"] = "up"
        except Exception as e:
            readiness_state["checks"]["vault"] = f"error: {str(e)[:50]}"

        # Check SPIRE
        try:
            from pyspiffe.workload_api import WorkloadAPIClient  # noqa: F401
            readiness_state["checks"]["spire"] = "unchecked"
        except Exception as e:
            readiness_state["checks"]["spire"] = f"error: {str(e)[:50]}"

        # Check NATS
        try:
            from nats.aio.client import Client  # noqa: F401
            readiness_state["checks"]["nats"] = "unchecked"
        except Exception as e:
            readiness_state["checks"]["nats"] = f"error: {str(e)[:50]}"

        # Check gRPC server (set by the _start_grpc before_serving hook; non-fatal
        # to overall readiness -- same degrade-not-fail pattern as vault/spire/nats
        # above -- see gh-22).
        readiness_state["checks"]["grpc"] = app.config.get("GRPC_STATUS", "starting")

        status_code = 200 if readiness_state["status"] == "ready" else 503
        return readiness_state, status_code

    # Version endpoint (anonymous).
    @app.route("/api/v1/version")
    async def version_info():
        """Return app version and build info."""
        version_file = Path(__file__).resolve().parents[2] / ".version"
        build_sha_file = Path(__file__).resolve().parents[2] / ".build_sha"

        version = "0.0.0"
        build_sha = "unknown"

        if version_file.exists():
            version = version_file.read_text(encoding="utf-8").strip()
        if build_sha_file.exists():
            build_sha = build_sha_file.read_text(encoding="utf-8").strip()

        return {
            "version": version,
            "build_sha": build_sha,
            "openapi_version": "3.1.0",
            "milestone": "v1.0",
            "cluster_id": app.config.get("CLUSTER_ID", "unknown"),
        }

    # Health check endpoint
    @app.route("/healthz")
    async def health_check():
        """Health check endpoint."""
        try:
            db = get_db()
            if db is None:
                # Degraded mode: DB not available but app is still running
                return {"status": "unhealthy", "database": "unavailable"}, 503
            # Regression: gh-22. Off the event loop via run_db() instead
            # of blocking the request coroutine inline.
            await run_db(lambda: db.executesql("SELECT 1"))
            return {"status": "healthy", "database": "connected"}, 200
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}, 503

    # Readiness check endpoint (Postgres + Vault + SPIRE + NATS).
    @app.route("/readyz")
    async def readiness_check():
        """Readiness: Vault unsealed, Postgres reachable, SPIRE up, gRPC warm."""
        readiness_state = {"status": "ready", "checks": {}}

        # Check Database (graceful if unavailable). Regression: gh-22.
        # Off the event loop via run_db() instead of blocking the request
        # coroutine inline.
        try:
            db = get_db()
            if db is None:
                readiness_state["checks"]["database"] = "unavailable (degraded mode)"
            else:
                await run_db(lambda: db.executesql("SELECT 1"))
                readiness_state["checks"]["database"] = "up"
        except Exception as e:
            readiness_state["status"] = "not_ready"
            readiness_state["checks"]["database"] = f"down: {str(e)[:50]}"

        # Check Vault (defensive: may not be initialized in dev).
        try:
            from .clients.vault import VaultClient  # noqa: F401
            readiness_state["checks"]["vault"] = "up"
        except Exception as e:
            readiness_state["checks"]["vault"] = f"error: {str(e)[:50]}"

        # Check SPIRE
        try:
            from pyspiffe.workload_api import WorkloadAPIClient  # noqa: F401
            readiness_state["checks"]["spire"] = "unchecked"
        except Exception as e:
            readiness_state["checks"]["spire"] = f"error: {str(e)[:50]}"

        # Check NATS
        try:
            from nats.aio.client import Client  # noqa: F401
            readiness_state["checks"]["nats"] = "unchecked"
        except Exception as e:
            readiness_state["checks"]["nats"] = f"error: {str(e)[:50]}"

        # Check gRPC server (set by the _start_grpc before_serving hook; non-fatal
        # to overall readiness -- same degrade-not-fail pattern as vault/spire/nats
        # above -- see gh-22).
        readiness_state["checks"]["grpc"] = app.config.get("GRPC_STATUS", "starting")

        status_code = 200 if readiness_state["status"] == "ready" else 503
        return readiness_state, status_code

    # Prometheus metrics endpoint for ASGI
    @app.route("/metrics")
    async def metrics():
        """Prometheus metrics endpoint."""
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    # Start gRPC server as background task alongside Quart/hypercorn.
    #
    # GRPC_ENABLED (default True) is a kill switch for environments that
    # genuinely don't want the gRPC listener (e.g. a REST-only deployment).
    # Failure is non-fatal to the HTTP app either way -- it's surfaced
    # loudly (ERROR log + traceback, /ready and /readyz "grpc" check) rather
    # than swallowed, since the previous silent-warning behavior is exactly
    # what let a broken gRPC stack ship undetected. See gh-22.
    @app.before_serving
    async def _start_grpc() -> None:
        """Start gRPC server (non-fatal to HTTP/REST; failure degrades readiness)."""
        import asyncio

        if not app.config.get("GRPC_ENABLED", True):
            app.logger.info("gRPC server disabled via GRPC_ENABLED=false")
            app.config["GRPC_STATUS"] = "disabled"
            return

        try:
            from . import grpc_runner
        except Exception:
            app.logger.error(
                "gRPC server import failed; continuing without gRPC (HTTP/REST only)",
                exc_info=True,
            )
            app.config["GRPC_STATUS"] = "down: import failed"
            return

        task = asyncio.ensure_future(grpc_runner.serve())

        def _on_grpc_task_done(t: "asyncio.Task[None]") -> None:
            """Surface a background gRPC server crash instead of losing it silently."""
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                app.logger.error("gRPC server task failed", exc_info=exc)
                app.config["GRPC_STATUS"] = f"down: {exc}"[:200]

        task.add_done_callback(_on_grpc_task_done)
        app.config["GRPC_STATUS"] = "up"

    # Wire the penguin-aaa ASGI OIDC auth gate. Gough is its OWN first-party
    # issuer: it mints ES256 access/id tokens (OIDCProvider, used by the auth
    # blueprint) and validates incoming bearer tokens locally with a
    # StaticKeyVerifier built from the keystore's public key -- no external
    # JWKS/discovery endpoint. On success the middleware sets
    # request.scope["state"]["claims"]; on failure it returns 401 before Quart
    # routing. Anonymous paths (login/refresh/health/version/iPXE) bypass the
    # gate via the template-aware ANONYMOUS_PATH_SET -- openapi.json/.yaml are
    # deliberately NOT in that set (regression: audit openapi-anon
    # 2026-09-22); they require a valid token, enforced via SCOPE_POLICY. The
    # provider +
    # settings are stashed on app.config for the login/refresh handlers.
    from .security.oidc import OIDCSettings, build_oidc
    from .security.scope_policy import ANONYMOUS_PATH_SET

    oidc_settings = OIDCSettings.from_config(app.config)
    oidc_provider, oidc_verifier = build_oidc(oidc_settings)
    app.config["OIDC_SETTINGS"] = oidc_settings
    app.config["OIDC_PROVIDER"] = oidc_provider
    app.asgi_app = OIDCAuthMiddleware(
        app.asgi_app,
        rp=oidc_verifier,
        public_paths=ANONYMOUS_PATH_SET,
    )

    # Cookie->Bearer shim (regression: security audit 2026-09-22 -- HIGH: the
    # web UI stored JWTs in localStorage, XSS-exfiltratable). Wrapped OUTSIDE
    # OIDCAuthMiddleware so it runs FIRST: when a request has no Authorization
    # header but does carry a gough_access cookie, it injects the header
    # before the OIDC gate ever inspects the request -- cookie and header auth
    # validate through the identical StaticKeyVerifier path. See
    # app.middleware.CookieAuthShimMiddleware for the full contract (cookie
    # names, CSRF double-submit bridge in install_security_middleware).
    app.asgi_app = CookieAuthShimMiddleware(app.asgi_app)

    # Wrap with audit middleware for request logging. Emitter requires at least one
    # sink; default to StdoutSink so the app always boots (production can configure
    # FileSink/SyslogSink/KillKrillSink). Gated by AUDIT_ENABLED. Applied last so
    # it is the outermost layer and records the OIDC gate's 401s too.
    if app.config.get("AUDIT_ENABLED", True):
        from penguin_aaa.audit.sinks import StdoutSink
        emitter = Emitter(StdoutSink())
        app.asgi_app = AuditMiddleware(app.asgi_app, emitter)

    return app


def _create_default_admin(user_datastore: PyDALUserDatastore, db) -> None:
    """Create default admin user if no users exist."""
    # Check if any users exist
    user_count = db(db.auth_user.id > 0).count()
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
