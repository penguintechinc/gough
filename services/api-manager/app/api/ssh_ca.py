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

from quart import Blueprint, g, jsonify, request

from ..middleware import auth_required, get_current_user, roles_required
from ..audit import AuditEventType, get_audit_logger
from ..models import get_db
from ..permissions import check_shell_access
from ..ssh_ca import SSHCertificateAuthority, SSHCAException

log = logging.getLogger(__name__)

ssh_ca_bp = Blueprint("ssh_ca", __name__, url_prefix="/api/v1/ssh-ca")


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
        # Check if CA already exists
        existing_ca = db(db.ssh_ca_config.id > 0).select().first()
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

        db.ssh_ca_config.insert(
            ca_name=ca_name,
            public_key=ca.get_public_key(),
            created_at=datetime.utcnow(),
            initialized=True,
        )
        db.commit()

        current_user = get_current_user()

        # Audit log
        audit_logger = get_audit_logger()
        if audit_logger:
            audit_logger.log(
                event_type=AuditEventType.CERT_ISSUED,
                message=f"SSH CA initialized: {ca_name}",
                user_id=current_user["id"],
                resource_type="ssh_ca",
                resource_id="0",
                details={"action": "ca_initialization", "ca_name": ca_name},
            )

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
        # Get CA configuration
        ca_config = db(db.ssh_ca_config.id > 0).select().first()

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

        # Check user has shell access to resource
        if not check_shell_access(
            current_user["id"], resource_type, resource_id
        ):
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

        # Get CA configuration
        ca_config = db(db.ssh_ca_config.id > 0).select().first()
        if not ca_config:
            return (
                jsonify({"error": "Certificate Authority not initialized"}),
                404,
            )

        # Initialize CA and sign certificate
        ca = SSHCertificateAuthority()
        certificate, key_id = ca.sign_public_key(
            public_key=public_key,
            principals=principals,
            valid_seconds=validity_seconds,
            user_id=current_user["id"],
        )

        # Calculate validity end time
        valid_until = datetime.utcnow() + timedelta(seconds=validity_seconds)

        # Audit log
        audit_logger = get_audit_logger()
        if audit_logger:
            audit_logger.log(
                event_type=AuditEventType.CERT_ISSUED,
                message=f"Certificate signed for {resource_type}/{resource_id}",
                user_id=current_user["id"],
                resource_type=resource_type,
                resource_id=resource_id,
                details={
                    "action": "certificate_signed",
                    "principals": principals,
                    "validity_seconds": validity_seconds,
                    "key_id": key_id,
                },
            )

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
