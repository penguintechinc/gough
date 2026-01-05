"""Shell Access API Endpoints.

Provides REST API for creating and managing remote shell sessions.
Supports SSH, kubectl, docker, and cloud CLI session types.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_security import auth_required, current_user

from ..audit import get_audit_logger
from ..models import get_db

log = logging.getLogger(__name__)

shell_bp = Blueprint("shell", __name__, url_prefix="/api/v1/shell")


def check_shell_access(
    user_id: int,
    resource_type: str,
    resource_id: str,
) -> tuple[bool, str | None]:
    """Check if user has shell access to a resource.

    Args:
        user_id: ID of the user
        resource_type: Type of resource (vm, container, cluster, etc.)
        resource_id: ID of the resource

    Returns:
        Tuple of (has_access, error_message)
    """
    db = get_db()

    # Admin users have full access
    user = db.auth_user(user_id)
    if not user:
        return False, "User not found"

    # Check user roles
    user_roles = db(
        (db.auth_user_roles.user_id == user_id) & (
            db.auth_role.id == db.auth_user_roles.role_id)
    ).select(db.auth_role.name)

    role_names = {r.name for r in user_roles}

    if "admin" in role_names:
        return True, None

    # Check team-based access
    # Find teams user belongs to
    team_memberships = db(
        (db.team_members.user_id == user_id)
    ).select(db.team_members.team_id)

    team_ids = [tm.team_id for tm in team_memberships]

    if not team_ids:
        return (
            False,
            "User not member of any team with access to this resource"
        )

    # Check if any team has shell permission for this resource
    assignments = db(
        (db.resource_assignments.team_id.belongs(team_ids)) & (
            db.resource_assignments.resource_type == resource_type) & (
            db.resource_assignments.resource_id == resource_id)
    ).select()

    for assignment in assignments:
        # Parse permissions JSON
        import json
        try:
            permissions = json.loads(assignment.permissions)
            if isinstance(permissions, list) and "shell" in permissions:
                return True, None
            elif isinstance(permissions, dict) and permissions.get("shell"):
                return True, None
        except (json.JSONDecodeError, ValueError):
            continue

    return False, "User does not have shell permission for this resource"


# ============================================================================
# Shell Session Management
# ============================================================================


@shell_bp.route("/sessions", methods=["POST"])
@auth_required()
def create_session():
    """Create a new shell session.

    Request Body:
        resource_type: Type of resource (vm, container, cluster, etc.)
        resource_id: ID of the resource
        session_type: Session type (ssh, kubectl, docker, cloud_cli)

    Returns:
        201: Session created successfully
        400: Invalid request
        403: Access denied
        404: Resource or agent not found
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    resource_type = data.get("resource_type", "").strip()
    resource_id = data.get("resource_id", "").strip()
    session_type = data.get("session_type", "ssh").strip()

    if not resource_type:
        return jsonify({"error": "resource_type required"}), 400

    if not resource_id:
        return jsonify({"error": "resource_id required"}), 400

    valid_session_types = ["ssh", "kubectl", "docker", "cloud_cli"]
    if session_type not in valid_session_types:
        valid_types_str = ", ".join(valid_session_types)
        return jsonify({
            "error": f"Invalid session_type. Must be one of: {valid_types_str}"
        }), 400

    # Check permissions
    has_access, error_msg = check_shell_access(
        current_user.id,
        resource_type,
        resource_id
    )

    if not has_access:
        log.warning(
            f"User {current_user.email} denied shell access to "
            f"{resource_type}/{resource_id}: {error_msg}"
        )
        return jsonify({"error": error_msg or "Access denied"}), 403

    db = get_db()

    # Find appropriate access agent for this resource
    # This is a simplified version - real implementation would need
    # to match agents based on resource location, network, etc.
    agent = db(
        (db.access_agents.status == "active")
    ).select(orderby=db.access_agents.last_heartbeat).first()

    if not agent:
        return jsonify({
            "error": "No active access agents available for this resource"
        }), 404

    try:
        # Generate unique session ID
        session_id = str(uuid.uuid4())

        # Get client IP
        client_ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.headers.get("X-Real-IP")
            or request.remote_addr
        )

        # Create session record
        db.shell_sessions.insert(
            session_id=session_id,
            user_id=current_user.id,
            resource_type=resource_type,
            resource_id=resource_id,
            agent_id=agent.id,
            session_type=session_type,
            client_ip=client_ip,
            started_at=datetime.utcnow(),
        )

        db.commit()

        # Audit log
        audit_logger = get_audit_logger()
        if audit_logger:
            audit_logger.log_shell_session_create(
                session_id=session_id,
                target_host=f"{resource_type}/{resource_id}",
                target_user=current_user.email,
                details={
                    "session_type": session_type,
                    "agent_id": agent.agent_id,
                    "client_ip": client_ip,
                },
            )

        # Generate WebSocket URL
        # In production, this would be the actual WebSocket endpoint
        # with proper protocol (wss://) and hostname
        websocket_url = f"wss://gough.local/ws/shell/{session_id}"

        return jsonify({
            "message": "Shell session created successfully",
            "session_id": session_id,
            "websocket_url": websocket_url,
            "session_type": session_type,
            "agent_id": agent.agent_id,
        }), 201

    except Exception as e:
        db.rollback()
        log.exception(f"Error creating shell session: {e}")
        return jsonify({"error": str(e)}), 500


@shell_bp.route("/sessions", methods=["GET"])
@auth_required()
def list_sessions():
    """List user's active shell sessions.

    Returns:
        200: List of active sessions
    """
    db = get_db()

    # Get user's active sessions (not ended)
    sessions = db(
        (db.shell_sessions.user_id == current_user.id) & (
            db.shell_sessions.ended_at is None)
    ).select(orderby=~db.shell_sessions.started_at).as_list()

    sessions_data = []
    for session in sessions:
        # Calculate duration
        started = session.get("started_at")
        duration_seconds = None
        if started:
            if isinstance(started, str):
                started = datetime.fromisoformat(started)
            duration_seconds = int(
                (datetime.utcnow() - started).total_seconds()
            )

        started_at_str = (
            started.isoformat()
            if isinstance(started, datetime)
            else started
        )
        sessions_data.append({
            "session_id": session["session_id"],
            "resource_type": session["resource_type"],
            "resource_id": session["resource_id"],
            "session_type": session["session_type"],
            "started_at": started_at_str,
            "duration_seconds": duration_seconds,
            "client_ip": session.get("client_ip"),
        })

    return jsonify({
        "sessions": sessions_data,
        "count": len(sessions_data),
    }), 200


@shell_bp.route("/sessions/<session_id>", methods=["DELETE"])
@auth_required()
def terminate_session(session_id: str):
    """Terminate a shell session.

    Args:
        session_id: ID of the session to terminate

    Returns:
        200: Session terminated
        403: Access denied
        404: Session not found
    """
    db = get_db()

    # Find session
    session = db(db.shell_sessions.session_id == session_id).select().first()

    if not session:
        return jsonify({"error": "Session not found"}), 404

    # Check permissions: owner or admin
    is_owner = session.user_id == current_user.id
    is_admin = current_user.has_role("admin")
    if not (is_owner or is_admin):
        return jsonify({"error": "Access denied"}), 403

    # Check if already terminated
    if session.ended_at:
        return jsonify({"error": "Session already terminated"}), 400

    try:
        # Calculate duration
        duration_seconds = None
        if session.started_at:
            duration_seconds = int(
                (datetime.utcnow() - session.started_at).total_seconds()
            )

        # Update session with end time
        db(db.shell_sessions.session_id == session_id).update(
            ended_at=datetime.utcnow()
        )

        db.commit()

        # Audit log
        audit_logger = get_audit_logger()
        if audit_logger:
            audit_logger.log_shell_session_terminate(
                session_id=session_id,
                reason="user_requested",
                duration_seconds=duration_seconds,
                details={
                    "resource_type": session.resource_type,
                    "resource_id": session.resource_id,
                },
            )

        return jsonify({
            "message": "Session terminated successfully",
            "session_id": session_id,
            "duration_seconds": duration_seconds,
        }), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error terminating session {session_id}: {e}")
        return jsonify({"error": str(e)}), 500
