"""Access Agent Management API Endpoints.

Provides REST API for agent enrollment, authentication, and management.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

import jwt
from flask import Blueprint, current_app, jsonify, request
from flask_security import auth_required, current_user, roles_required

from ..audit import get_audit_logger
from ..models import get_db

log = logging.getLogger(__name__)

agents_bp = Blueprint("agents", __name__, url_prefix="/api/v1/agents")


# ============================================================================
# Enrollment Key Management
# ============================================================================


@agents_bp.route("/enrollment-keys", methods=["POST"])
@auth_required()
@roles_required("admin")
def create_enrollment_key():
    """Generate a new enrollment key for agent enrollment.

    Request Body (optional):
        expires_in_hours: Key validity in hours (default: 24)
        metadata: Optional JSON metadata for the agent

    Returns:
        201: Enrollment key created
        500: Server error
    """
    data = request.get_json() or {}
    expires_in_hours = data.get("expires_in_hours", 24)
    metadata = data.get("metadata")

    db = get_db()

    try:
        # Generate secure enrollment key
        # Format: ENROLL-XXXX-XXXX-XXXX-XXXX
        key_parts = [secrets.token_hex(2).upper() for _ in range(4)]
        enrollment_key = f"ENROLL-{'-'.join(key_parts)}"

        # Hash the key for storage
        key_hash = hashlib.sha256(enrollment_key.encode()).hexdigest()

        # Calculate expiry
        expires_at = datetime.utcnow() + timedelta(hours=expires_in_hours)

        # Store key
        key_id = db.enrollment_keys.insert(
            key_hash=key_hash,
            created_by=current_user.id,
            expires_at=expires_at,
            metadata=str(metadata) if metadata else None,
        )

        db.commit()

        log.info(f"Enrollment key created by user {current_user.id}")

        return jsonify({
            "message": "Enrollment key created",
            "enrollment_key": enrollment_key,
            "expires_at": expires_at.isoformat(),
            "key_id": key_id,
        }), 201

    except Exception as e:
        db.rollback()
        log.exception(f"Error creating enrollment key: {e}")
        return jsonify({"error": str(e)}), 500


@agents_bp.route("/enrollment-keys", methods=["GET"])
@auth_required()
@roles_required("admin")
def list_enrollment_keys():
    """List all enrollment keys.

    Query Parameters:
        include_used: Include used keys (default: false)

    Returns:
        200: List of enrollment keys
    """
    db = get_db()
    include_used = request.args.get("include_used", "false").lower() == "true"

    query = db.enrollment_keys.id > 0
    if not include_used:
        query &= db.enrollment_keys.is_used is False

    keys = db(query).select(orderby=~db.enrollment_keys.created_at).as_list()

    keys_data = []
    for key in keys:
        keys_data.append({
            "id": key["id"],
            "created_by": key["created_by"],
            "expires_at": key["expires_at"].isoformat()
            if key["expires_at"] else None,
            "is_used": key["is_used"],
            "used_by_agent": key["used_by_agent"],
            "created_at": key["created_at"].isoformat()
            if key["created_at"] else None,
        })

    return jsonify({
        "enrollment_keys": keys_data,
        "count": len(keys_data),
    }), 200


@agents_bp.route("/enrollment-keys/<int:key_id>", methods=["DELETE"])
@auth_required()
@roles_required("admin")
def revoke_enrollment_key(key_id: int):
    """Revoke an enrollment key.

    Args:
        key_id: ID of the enrollment key

    Returns:
        200: Key revoked
        404: Key not found
    """
    db = get_db()

    key = db.enrollment_keys(key_id)
    if not key:
        return jsonify({"error": "Enrollment key not found"}), 404

    try:
        db(db.enrollment_keys.id == key_id).delete()
        db.commit()

        return jsonify({"message": "Enrollment key revoked"}), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error revoking enrollment key: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Agent Enrollment
# ============================================================================


@agents_bp.route("/enroll", methods=["POST"])
def enroll_agent():
    """Enroll a new access agent.

    Uses enrollment key for authentication.

    Headers:
        X-Enrollment-Key: The enrollment key

    Request Body:
        hostname: Agent hostname (required)
        ip_address: Agent IP address (optional)
        agent_version: Agent version string (optional)
        capabilities: List of capabilities (optional)

    Returns:
        201: Agent enrolled, returns JWT tokens
        400: Invalid request
        401: Invalid or expired enrollment key
        409: Enrollment key already used
    """
    # Get enrollment key from header
    enrollment_key = request.headers.get("X-Enrollment-Key")
    if not enrollment_key:
        return jsonify({"error": "Enrollment key required"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    hostname = data.get("hostname", "").strip()
    if not hostname:
        return jsonify({"error": "Hostname required"}), 400

    db = get_db()

    # Hash and find enrollment key
    key_hash = hashlib.sha256(enrollment_key.encode()).hexdigest()
    key_record = db(db.enrollment_keys.key_hash == key_hash).select().first()

    if not key_record:
        return jsonify({"error": "Invalid enrollment key"}), 401

    if key_record.is_used:
        return jsonify({"error": "Enrollment key already used"}), 409

    if key_record.expires_at and key_record.expires_at < datetime.utcnow():
        return jsonify({"error": "Enrollment key expired"}), 401

    try:
        # Generate agent ID
        agent_id = str(uuid.uuid4())

        # Create agent record
        agent_db_id = db.access_agents.insert(
            agent_id=agent_id,
            hostname=hostname,
            ip_address=data.get("ip_address"),
            enrollment_key_hash=key_hash,
            enrollment_completed=True,
            status="active",
            capabilities=str(data.get("capabilities", ["ssh"])),
            enrolled_at=datetime.utcnow(),
            last_heartbeat=datetime.utcnow(),
        )

        # Mark enrollment key as used
        db(db.enrollment_keys.id == key_record.id).update(
            is_used=True,
            used_by_agent=agent_db_id,
        )

        # Generate JWT tokens
        caps = data.get("capabilities", ["ssh"])
        access_token = _create_agent_access_token(agent_id, caps)
        refresh_token, refresh_expires = _create_agent_refresh_token(agent_id)

        db.commit()

        # Get CA public key
        ca_config = db(db.ssh_ca_config.is_active is True).select().first()
        ca_public_key = ca_config.public_key if ca_config else None

        # Audit log
        audit_logger = get_audit_logger()
        if audit_logger:
            audit_logger.log_agent_enroll(
                agent_id=agent_id,
                hostname=hostname,
                agent_version=data.get("agent_version", "unknown"),
                details={
                    "ip_address": data.get("ip_address"),
                    "capabilities": data.get("capabilities"),
                },
            )

        log.info(f"Agent {hostname} enrolled as {agent_id}")

        return jsonify({
            "message": "Agent enrolled successfully",
            "agent_id": agent_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_token_expires_in": 3600,
            "refresh_token_expires_in": 2592000,
            "ca_public_key": ca_public_key,
            "config": {
                "heartbeat_interval": 30,
            },
        }), 201

    except Exception as e:
        db.rollback()
        log.exception(f"Error enrolling agent: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Agent Authentication
# ============================================================================


@agents_bp.route("/refresh", methods=["POST"])
def refresh_agent_token():
    """Refresh agent JWT tokens.

    Uses refresh token for authentication.

    Headers:
        Authorization: Bearer <refresh_token>

    Returns:
        200: New tokens issued
        401: Invalid or expired refresh token
    """
    # Get refresh token from header
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Authorization header required"}), 401

    refresh_token = auth_header[7:]

    try:
        # Decode and validate token
        payload = jwt.decode(
            refresh_token,
            current_app.config["JWT_SECRET_KEY"],
            algorithms=["HS256"],
        )

        if payload.get("type") != "agent_refresh":
            return jsonify({"error": "Invalid token type"}), 401

        agent_id = payload.get("sub", "").replace("agent:", "")
        if not agent_id:
            return jsonify({"error": "Invalid token"}), 401

        db = get_db()

        # Verify agent exists and is active
        agent = db(db.access_agents.agent_id == agent_id).select().first()
        if not agent:
            return jsonify({"error": "Agent not found"}), 401

        if agent.status != "active":
            return jsonify({"error": "Agent is not active"}), 401

        # Generate new tokens
        caps = eval(agent.capabilities) if agent.capabilities else ["ssh"]
        capabilities = caps
        access_token = _create_agent_access_token(agent_id, capabilities)
        new_refresh_token, _ = _create_agent_refresh_token(agent_id)

        log.debug(f"Tokens refreshed for agent {agent_id}")

        return jsonify({
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "access_token_expires_in": 3600,
            "refresh_token_expires_in": 2592000,
        }), 200

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Refresh token expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid refresh token"}), 401


@agents_bp.route("/heartbeat", methods=["POST"])
def agent_heartbeat():
    """Receive heartbeat from agent.

    Headers:
        Authorization: Bearer <access_token>

    Request Body:
        agent_id: Agent ID
        status: Agent status
        active_sessions: Number of active sessions
        resource_usage: Resource usage metrics

    Returns:
        200: Heartbeat acknowledged
        401: Authentication failed
    """
    # Validate agent token
    agent_id = _validate_agent_token()
    if not agent_id:
        return jsonify({"error": "Authentication required"}), 401

    data = request.get_json() or {}

    db = get_db()

    try:
        # Update agent record
        db(db.access_agents.agent_id == agent_id).update(
            last_heartbeat=datetime.utcnow(),
            status=data.get("status", "active"),
        )

        db.commit()

        # Check for pending commands (future feature)
        commands = []

        return jsonify({
            "status": "ok",
            "commands": commands,
        }), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error processing heartbeat: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Agent Management
# ============================================================================


@agents_bp.route("/", methods=["GET"])
@auth_required()
@roles_required("admin")
def list_agents():
    """List all access agents.

    Query Parameters:
        status: Filter by status (active, suspended, pending)

    Returns:
        200: List of agents
    """
    db = get_db()

    status_filter = request.args.get("status")

    query = db.access_agents.id > 0
    if status_filter:
        query &= db.access_agents.status == status_filter

    agents = db(query).select(
        orderby=~db.access_agents.last_heartbeat
    ).as_list()

    agents_data = []
    for agent in agents:
        agents_data.append({
            "id": agent["id"],
            "agent_id": agent["agent_id"],
            "hostname": agent["hostname"],
            "ip_address": agent["ip_address"],
            "status": agent["status"],
            "capabilities": agent["capabilities"],
            "last_heartbeat": agent["last_heartbeat"].isoformat()
            if agent["last_heartbeat"] else None,
            "enrolled_at": agent["enrolled_at"].isoformat()
            if agent["enrolled_at"] else None,
        })

    return jsonify({
        "agents": agents_data,
        "count": len(agents_data),
    }), 200


@agents_bp.route("/<agent_id>", methods=["GET"])
@auth_required()
@roles_required("admin")
def get_agent(agent_id: str):
    """Get agent details.

    Args:
        agent_id: Agent UUID

    Returns:
        200: Agent details
        404: Agent not found
    """
    db = get_db()

    agent = db(db.access_agents.agent_id == agent_id).select().first()
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    return jsonify({
        "agent": {
            "id": agent.id,
            "agent_id": agent.agent_id,
            "hostname": agent.hostname,
            "ip_address": agent.ip_address,
            "status": agent.status,
            "capabilities": agent.capabilities,
            "enrollment_completed": agent.enrollment_completed,
            "last_heartbeat": agent.last_heartbeat.isoformat()
            if agent.last_heartbeat else None,
            "enrolled_at": agent.enrolled_at.isoformat()
            if agent.enrolled_at else None,
            "created_at": agent.created_at.isoformat()
            if agent.created_at else None,
        },
    }), 200


@agents_bp.route("/<agent_id>/suspend", methods=["POST"])
@auth_required()
@roles_required("admin")
def suspend_agent(agent_id: str):
    """Suspend an agent.

    Args:
        agent_id: Agent UUID

    Returns:
        200: Agent suspended
        404: Agent not found
    """
    db = get_db()

    agent = db(db.access_agents.agent_id == agent_id).select().first()
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    try:
        db(db.access_agents.agent_id == agent_id).update(
            status="suspended",
            updated_at=datetime.utcnow(),
        )
        db.commit()

        log.info(f"Agent {agent_id} suspended by user {current_user.id}")

        return jsonify({"message": "Agent suspended"}), 200

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500


@agents_bp.route("/<agent_id>/resume", methods=["POST"])
@auth_required()
@roles_required("admin")
def resume_agent(agent_id: str):
    """Resume a suspended agent.

    Args:
        agent_id: Agent UUID

    Returns:
        200: Agent resumed
        404: Agent not found
    """
    db = get_db()

    agent = db(db.access_agents.agent_id == agent_id).select().first()
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    try:
        db(db.access_agents.agent_id == agent_id).update(
            status="active",
            updated_at=datetime.utcnow(),
        )
        db.commit()

        log.info(f"Agent {agent_id} resumed by user {current_user.id}")

        return jsonify({"message": "Agent resumed"}), 200

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Helper Functions
# ============================================================================


def _create_agent_access_token(agent_id: str, capabilities: list) -> str:
    """Create JWT access token for agent.

    Args:
        agent_id: Agent UUID
        capabilities: List of agent capabilities

    Returns:
        JWT access token
    """
    expires = datetime.utcnow() + timedelta(hours=1)
    payload = {
        "sub": f"agent:{agent_id}",
        "type": "agent_access",
        "capabilities": capabilities,
        "exp": expires,
        "iat": datetime.utcnow(),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(
        payload,
        current_app.config["JWT_SECRET_KEY"],
        algorithm="HS256",
    )


def _create_agent_refresh_token(agent_id: str) -> tuple:
    """Create JWT refresh token for agent.

    Args:
        agent_id: Agent UUID

    Returns:
        Tuple of (token, expires_at)
    """
    expires = datetime.utcnow() + timedelta(days=30)
    payload = {
        "sub": f"agent:{agent_id}",
        "type": "agent_refresh",
        "exp": expires,
        "iat": datetime.utcnow(),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(
        payload,
        current_app.config["JWT_SECRET_KEY"],
        algorithm="HS256",
    )
    return token, expires


def _validate_agent_token() -> Optional[str]:
    """Validate agent access token from request.

    Returns:
        Agent ID if valid, None otherwise
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]

    try:
        payload = jwt.decode(
            token,
            current_app.config["JWT_SECRET_KEY"],
            algorithms=["HS256"],
        )

        if payload.get("type") != "agent_access":
            return None

        agent_id = payload.get("sub", "").replace("agent:", "")
        return agent_id if agent_id else None

    except jwt.InvalidTokenError:
        return None
