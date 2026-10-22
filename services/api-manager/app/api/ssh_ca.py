"""SSH Certificate Authority API Endpoints.

Provides REST API for SSH certificate authority operations including:
- CA initialization and key management
- SSH certificate signing for users
- Public key retrieval
- Access control based on resource permissions
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from quart import Blueprint, g, jsonify, request

from ..db.run_db import run_db
from ..middleware import auth_required, get_current_user, roles_required
from ..audit import AuditEventType, get_audit_logger
from ..models import get_db
from ..permissions import check_shell_access
from ..ssh_ca import (
    SSHCertificateAuthority,
    SSHCAException,
    generate_key_id,
    validate_principal_syntax,
    validate_principals,
)
from .shell import user_is_admin

log = logging.getLogger(__name__)

ssh_ca_bp = Blueprint("ssh_ca", __name__, url_prefix="/api/v1/ssh-ca")

MAX_API_CERT_VALIDITY_SECONDS = 900


def resolve_agent_id(resource_type: str, resource_id: str) -> str | None:
    """Server-side resource -> access-agent lookup (blocking DB call).

    The ``host:<agent_id>`` principal is derived from this authorized lookup
    only (agent whose ``agent_id`` or ``hostname`` is the resource), never from
    request input. Returns None when no active agent matches (fail closed).
    """
    db = get_db()
    row = db(
        (db.access_agents.status == "active")
        & (
            (db.access_agents.agent_id == resource_id)
            | (db.access_agents.hostname == resource_id)
        )
    ).select().first()
    return row.agent_id if row else None


def allowed_login_principals(
    user_id: int, resource_type: str, resource_id: str
) -> list[str]:
    """Per-resource login allowlist from the user's team assignments (blocking).

    Reads ``principals`` from dict-form assignment permissions; admins also get
    the ``SSH_DEFAULT_LOGIN_PRINCIPALS`` env defaults. Empty list = nothing.
    """
    import json
    import os

    db = get_db()
    allowed: set[str] = set()
    if user_is_admin(user_id):
        allowed |= {p for p in os.getenv("SSH_DEFAULT_LOGIN_PRINCIPALS", "").split(",") if p}
    team_ids = [
        tm.team_id
        for tm in db(db.team_members.user_id == user_id).select(db.team_members.team_id)
    ]
    if team_ids:
        rows = db(
            (db.resource_assignments.team_id.belongs(team_ids))
            & (db.resource_assignments.resource_type == resource_type)
            & (db.resource_assignments.resource_id == resource_id)
        ).select()
        for row in rows:
            try:
                perms = json.loads(row.permissions)
            except (ValueError, TypeError):
                continue
            if isinstance(perms, dict) and isinstance(perms.get("principals"), list):
                allowed |= {p for p in perms["principals"] if isinstance(p, str)}
    return sorted(allowed)


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
        resource_type: Type of resource (e.g., 'vm', 'container') (required)
        resource_id: UUID of resource (required)
        principals: List of principals (e.g., ['ubuntu', 'root']) (required)
        validity_seconds: Certificate validity period in seconds (required)

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
    public_key = data.get("public_key", "").strip()
    resource_type = data.get("resource_type", "").strip()
    resource_id = data.get("resource_id", "").strip()
    principals = data.get("principals", [])
    validity_seconds = data.get("validity_seconds", 3600)

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

    if not isinstance(principals, list) or len(principals) == 0:
        return jsonify({"error": "principals must be a non-empty list"}), 400

    if not isinstance(validity_seconds, int) or validity_seconds <= 0:
        return (
            jsonify(
                {"error": "validity_seconds must be a positive integer"}
            ),
            400,
        )

    try:
        current_user = get_current_user()

        # Principal hygiene first: reserved (host:/allow-root) or malformed
        # requests are rejected outright, never silently filtered.
        try:
            for p in principals:
                validate_principal_syntax(p)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        validity_seconds = min(validity_seconds, MAX_API_CERT_VALIDITY_SECONDS)

        def _authorize() -> tuple[str | None, str | None, bool]:
            """Return (agent_id, error, is_admin) for the authorized resource."""
            if not check_shell_access(current_user["id"], resource_type, resource_id):
                return None, "User does not have shell access to this resource", False
            agent_id = resolve_agent_id(resource_type, resource_id)
            if not agent_id:
                return None, "No active access agent for this resource", False
            allowed = allowed_login_principals(
                current_user["id"], resource_type, resource_id
            )
            if not validate_principals(principals, allowed):
                return None, "Principal not permitted for this resource", False
            return agent_id, None, user_is_admin(current_user["id"])

        agent_id, auth_err, is_admin = await run_db(_authorize)
        if auth_err:
            status = 404 if "agent" in auth_err else 403
            return jsonify({"error": auth_err}), status

        # allow-root is a separate, explicit grant: root login requested AND
        # the dangerous gough.shell.root scope AND admin role.
        scopes = set(
            (current_user.get("_jwt_payload") or {}).get("scope") or []
        )
        wants_root = "root" in principals
        allow_root = wants_root and is_admin and "gough.shell.root" in scopes
        if wants_root and not allow_root:
            return jsonify({"error": "Root access not permitted"}), 403

        def _fetch_ca_config() -> Any:
            return db(db.ssh_ca_config.id > 0).select().first()

        ca_config = await run_db(_fetch_ca_config)
        if not ca_config:
            return (
                jsonify({"error": "Certificate Authority not initialized"}),
                404,
            )

        ca = SSHCertificateAuthority()
        key_id = generate_key_id(current_user["email"], resource_id)
        certificate = await ca.sign_user_key(
            public_key=public_key,
            principals=principals,
            validity_seconds=validity_seconds,
            key_id=key_id,
            host_ids=[agent_id],
            allow_root=allow_root,
        )

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
                    "principals": principals,
                    "host_principal": f"host:{agent_id}",
                    "allow_root": allow_root,
                    "validity_seconds": validity_seconds,
                    "key_id": key_id,
                },
            ))

        log.info(
            f"SSH certificate signed for user {current_user['id']} "
            f"on {resource_type}/{resource_id}, principals={principals}"
        )

        return (
            jsonify(
                {
                    "certificate": certificate,
                    "valid_until": valid_until.isoformat(),
                    "key_id": key_id,
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
