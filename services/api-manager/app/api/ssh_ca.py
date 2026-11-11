"""SSH Certificate Authority API Endpoints.

Provides REST API for SSH certificate authority operations including:
- CA initialization and key management
- SSH certificate signing for users
- Public key retrieval
- Access control based on resource permissions
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any

from quart import Blueprint, g, jsonify, request

from ..db.run_db import run_db
from ..middleware import auth_required, get_current_user, roles_required
from ..audit import AuditEventType, get_audit_logger
from ..models import get_db
from ..permissions import check_shell_access
from ..ssh_ca import SSHCertificateAuthority, SSHCAException

log = logging.getLogger(__name__)

ssh_ca_bp = Blueprint("ssh_ca", __name__, url_prefix="/api/v1/ssh-ca")

#: Hard ceiling on user-certificate lifetime. Requests above it are rejected
#: (400), never silently clamped, so a caller always knows what it holds.
SSH_CERT_MAX_VALIDITY_SECONDS = 3600

#: Env var holding the comma-separated principal allowlist. Unset/empty means
#: no principal is signable (fail closed).
SSH_PRINCIPAL_ALLOWLIST_ENV = "GOUGH_SSH_CA_ALLOWED_PRINCIPALS"

#: Principals that are never signed, even if an operator lists them.
_FORBIDDEN_PRINCIPALS: frozenset[str] = frozenset({"root"})

#: POSIX-portable login name: what a principal must look like before it is
#: even compared to the allowlist (keeps separators out of ssh-keygen -n).
_PRINCIPAL_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

#: Resource tables a certificate may be bound to. ``check_shell_access``
#: resolves ``db[resource_type]`` -- an unrestricted value let a caller name
#: any table whose row it "created" (e.g. its own enrollment key) and be
#: granted shell. Only nodes are host-addressable and tenant-owned.
_SIGNABLE_RESOURCE_TYPES: frozenset[str] = frozenset({"nodes"})

_RESOURCE_ID_RE = re.compile(r"^[1-9][0-9]{0,17}$")

#: Issued principals are bound to the target host: a node's sshd accepts
#: ``<login>@node-<id>`` via its AuthorizedPrincipalsFile, so a certificate
#: minted for node 7 is useless on node 8.
_HOST_BOUND_PRINCIPAL_FMT = "{principal}@node-{resource_id}"


def _allowed_principals() -> frozenset[str]:
    """Read the operator-configured principal allowlist (never includes root)."""
    raw = os.environ.get(SSH_PRINCIPAL_ALLOWLIST_ENV, "")
    configured = frozenset(p.strip() for p in raw.split(",") if p.strip())
    return configured - _FORBIDDEN_PRINCIPALS


def _validate_principals(principals: Any) -> str | None:
    """Return an error message if ``principals`` is not signable, else None.

    Rejects non-lists, non-string or malformed entries, ``root`` (always), and
    anything outside the configured allowlist (empty allowlist rejects all).
    """
    if not isinstance(principals, list) or not principals:
        return "principals must be a non-empty list"
    allowed = _allowed_principals()
    for principal in principals:
        if not isinstance(principal, str) or not _PRINCIPAL_RE.match(principal):
            return "principals must be valid login names"
        if principal in _FORBIDDEN_PRINCIPALS:
            return f"principal '{principal}' is never signed"
        if principal not in allowed:
            return f"principal '{principal}' is not in the signing allowlist"
    return None


def _validate_validity(validity_seconds: Any) -> str | None:
    """Return an error message unless ``validity_seconds`` is an int in (0, max]."""
    if isinstance(validity_seconds, bool) or not isinstance(validity_seconds, int):
        return "validity_seconds must be a positive integer"
    if validity_seconds <= 0:
        return "validity_seconds must be a positive integer"
    if validity_seconds > SSH_CERT_MAX_VALIDITY_SECONDS:
        return f"validity_seconds must not exceed {SSH_CERT_MAX_VALIDITY_SECONDS}"
    return None


def _current_tenant_id() -> str:
    """Return the validated tenant from the request context (JWT ``tenant``)."""
    tc = g.get("tenant_context")
    if tc is not None:
        return str(getattr(tc, "tenant_id", "__default__"))
    user = g.get("current_user") or {}
    payload = user.get("_jwt_payload") or {}
    return str(payload.get("tenant", "__default__"))


def _is_cross_tenant() -> bool:
    """True iff the validated token is a cross-tenant (super-admin) token."""
    tc = g.get("tenant_context")
    if tc is not None:
        return bool(getattr(tc, "cross_tenant", False))
    user = g.get("current_user") or {}
    payload = user.get("_jwt_payload") or {}
    return bool(payload.get("cross_tenant", False))


def _load_node_tenant(node_id: int) -> str | None:
    """Return the owning tenant of node ``node_id``, or None if it does not exist."""
    db = get_db()
    if db is None:
        raise RuntimeError("database not initialised")
    node = db(db.nodes.id == node_id).select(db.nodes.id, db.nodes.tenant_id).first()
    if node is None:
        return None
    return str(getattr(node, "tenant_id", None) or "__default__")


# ============================================================================
# SSH Certificate Authority Management
# ============================================================================


@ssh_ca_bp.route("/initialize", methods=["POST"])
@auth_required
@roles_required("admin")
async def initialize_ca():
    """Initialize SSH Certificate Authority.

    First-time setup: generates new CA key pair and stores in database
    and Vault. Admin-only operation.

    Returns:
        201: CA initialized successfully
        400: Invalid request
        409: CA already initialized
        500: Initialization failed
    """
    db = get_db()

    try:
        # Check if CA already exists. Regression: gh-22. Off the event loop
        # via run_db() instead of blocking the request coroutine inline.
        def _fetch_existing() -> Any:
            return db(db.ssh_ca_config.id > 0).select().first()

        existing_ca = await run_db(_fetch_existing)
        if existing_ca:
            return (
                jsonify(
                    {"error": "Certificate Authority already initialized"}
                ),
                409,
            )

        # Initialize CA with new key pair
        ca = SSHCertificateAuthority()
        ca.initialize()

        # Store CA configuration in database
        json_data = await request.get_json()
        ca_name = (
            json_data.get("ca_name", "Gough SSH CA")
            if json_data
            else "Gough SSH CA"
        )

        # Regression: gh-22. Off the event loop via run_db() instead of
        # blocking the request coroutine inline.
        def _store_ca_config() -> None:
            db.ssh_ca_config.insert(
                ca_name=ca_name,
                public_key=ca.get_public_key(),
                created_at=datetime.utcnow(),
                initialized=True,
            )
            db.commit()

        await run_db(_store_ca_config)

        current_user = get_current_user()

        # Audit log. Regression: gh-22. AuditLogger.log() issues its own
        # synchronous db.system_logs.insert()+commit() -- off the event
        # loop via run_db() instead of blocking the request coroutine
        # inline.
        audit_logger = get_audit_logger()
        if audit_logger:
            await run_db(lambda: audit_logger.log(
                event_type=AuditEventType.CERT_ISSUED,
                message=f"SSH CA initialized: {ca_name}",
                user_id=current_user["id"],
                resource_type="ssh_ca",
                resource_id="0",
                details={"action": "ca_initialization", "ca_name": ca_name},
            ))

        log.info(f"SSH CA initialized by user {current_user['id']}")

        return (
            jsonify(
                {
                    "message": "SSH CA initialized successfully",
                    "ca_name": ca_name,
                }
            ),
            201,
        )

    except SSHCAException as e:
        log.error(f"CA initialization failed: {str(e)}")
        return jsonify({"error": f"CA initialization failed: {str(e)}"}), 500
    except Exception as e:
        log.error(f"Unexpected error during CA initialization: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


@ssh_ca_bp.route("/public-key", methods=["GET"])
@auth_required
async def get_public_key():
    """Get SSH Certificate Authority public key.

    Available to all authenticated users.

    Returns:
        200: Public key retrieved successfully
            {"public_key": "ssh-rsa AAAA...", "ca_name": "..."}
        404: CA not initialized
        500: Retrieval failed
    """
    db = get_db()

    try:
        # Get CA configuration. Regression: gh-22. Off the event loop via
        # run_db() instead of blocking the request coroutine inline.
        def _fetch_ca_config() -> Any:
            return db(db.ssh_ca_config.id > 0).select().first()

        ca_config = await run_db(_fetch_ca_config)

        if not ca_config:
            return (
                jsonify({"error": "Certificate Authority not initialized"}),
                404,
            )

        return (
            jsonify(
                {
                    "public_key": ca_config.public_key,
                    "ca_name": ca_config.ca_name,
                }
            ),
            200,
        )

    except Exception as e:
        log.error(f"Error retrieving CA public key: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


@ssh_ca_bp.route("/sign", methods=["POST"])
@auth_required
async def sign_certificate():
    """Sign user's SSH public key with CA.

    Request Body:
        public_key: User's SSH public key (required)
        resource_type: Resource table; only ``nodes`` is signable (required)
        resource_id: Node id, must belong to the caller's tenant (required)
        principals: Login names from the ``GOUGH_SSH_CA_ALLOWED_PRINCIPALS``
            allowlist; ``root`` is always rejected (required)
        validity_seconds: Lifetime in seconds, 1..3600 (default 3600)

    Issued principals are host-bound as ``<login>@node-<id>``.

    Returns:
        200: Certificate signed successfully
            {"certificate": "ssh-rsa-cert-v01...", "valid_until":
            "ISO datetime", "key_id": "..."}
        400: Invalid request or user lacks shell permission
        404: CA not initialized or resource not found
        500: Signing failed
    """
    db = get_db()
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    # Validate required fields
    public_key = str(data.get("public_key") or "").strip()
    resource_type = str(data.get("resource_type") or "").strip()
    resource_id = str(data.get("resource_id") or "").strip()
    principals = data.get("principals", [])
    validity_seconds = data.get("validity_seconds", SSH_CERT_MAX_VALIDITY_SECONDS)

    if not all([public_key, resource_type, resource_id, principals]):
        return (
            jsonify(
                {
                    "error": (
                        "Missing required fields: public_key, "
                        "resource_type, resource_id, principals"
                    )
                }
            ),
            400,
        )

    principal_error = _validate_principals(principals)
    if principal_error:
        return jsonify({"error": principal_error}), 400

    validity_error = _validate_validity(validity_seconds)
    if validity_error:
        return jsonify({"error": validity_error}), 400

    if resource_type not in _SIGNABLE_RESOURCE_TYPES:
        return jsonify({
            "error": "resource_type must be one of: "
            + ", ".join(sorted(_SIGNABLE_RESOURCE_TYPES))
        }), 400
    if not _RESOURCE_ID_RE.match(resource_id):
        return jsonify({"error": "resource_id must be a node id"}), 400
    node_id = int(resource_id)

    try:
        current_user = get_current_user()

        # Bind to the requested host: the node must exist inside the caller's
        # tenant (404, not 403, so existence is not confirmed cross-tenant).
        node_tenant = await run_db(lambda: _load_node_tenant(node_id))
        if node_tenant is None or (
            not _is_cross_tenant() and node_tenant != _current_tenant_id()
        ):
            return jsonify({"error": "Resource not found"}), 404

        # Check user has shell access to resource. Regression: gh-22. Off
        # the event loop via run_db() instead of blocking the request
        # coroutine inline (check_shell_access issues its own direct
        # penguin-dal reads -- app.permissions.check_resource_permission).
        def _check_access() -> bool:
            return check_shell_access(
                current_user["id"], resource_type, node_id
            )

        if not await run_db(_check_access):
            return (
                jsonify(
                    {
                        "error": (
                            "User does not have shell access to this "
                            "resource"
                        )
                    }
                ),
                400,
            )

        # Get CA configuration. Regression: gh-22. Off the event loop via
        # run_db() instead of blocking the request coroutine inline.
        def _fetch_ca_config() -> Any:
            return db(db.ssh_ca_config.id > 0).select().first()

        ca_config = await run_db(_fetch_ca_config)
        if not ca_config:
            return (
                jsonify({"error": "Certificate Authority not initialized"}),
                404,
            )

        # Initialize CA and sign certificate
        ca = SSHCertificateAuthority()
        bound_principals = [
            _HOST_BOUND_PRINCIPAL_FMT.format(principal=p, resource_id=node_id)
            for p in principals
        ]
        certificate, key_id = ca.sign_public_key(
            public_key=public_key,
            principals=bound_principals,
            valid_seconds=validity_seconds,
            user_id=current_user["id"],
        )

        # Calculate validity end time
        valid_until = datetime.utcnow() + timedelta(seconds=validity_seconds)

        # Audit log. Regression: gh-22. AuditLogger.log() issues its own
        # synchronous db.system_logs.insert()+commit() -- off the event
        # loop via run_db() instead of blocking the request coroutine
        # inline.
        audit_logger = get_audit_logger()
        if audit_logger:
            await run_db(lambda: audit_logger.log(
                event_type=AuditEventType.CERT_ISSUED,
                message=f"Certificate signed for {resource_type}/{resource_id}",
                user_id=current_user["id"],
                resource_type=resource_type,
                resource_id=resource_id,
                details={
                    "action": "certificate_signed",
                    "principals": bound_principals,
                    "validity_seconds": validity_seconds,
                    "key_id": key_id,
                },
            ))

        log.info(
            f"SSH certificate signed for user {current_user['id']} "
            f"on {resource_type}/{resource_id}, principals={bound_principals}"
        )

        return (
            jsonify(
                {
                    "certificate": certificate,
                    "valid_until": valid_until.isoformat(),
                    "key_id": key_id,
                    "principals": bound_principals,
                }
            ),
            200,
        )

    except SSHCAException as e:
        log.error(f"Certificate signing failed: {str(e)}")
        return (
            jsonify({"error": f"Certificate signing failed: {str(e)}"}),
            500,
        )
    except Exception as e:
        log.error(f"Unexpected error during certificate signing: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500
