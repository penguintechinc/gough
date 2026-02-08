"""Team Management API Endpoints.

Provides REST API for managing teams, team memberships, and
resource assignments. Supports team creation, member management,
and resource allocation to teams.
"""

from __future__ import annotations

import logging
from datetime import datetime

from quart import Blueprint, g, jsonify, request

from ..middleware import auth_required, get_current_user, roles_required, user_has_role
from ..models import get_db

log = logging.getLogger(__name__)

teams_bp = Blueprint("teams", __name__, url_prefix="/api/v1/teams")


# ============================================================================
# Team Management
# ============================================================================


@teams_bp.route("/", methods=["POST"])
@auth_required
@roles_required("admin")
async def create_team():
    """Create a new team.

    Request Body:
        name: Team name (required, must be unique)
        description: Team description (optional)
        metadata: JSON metadata (optional)

    Returns:
        201: Team created successfully
        400: Invalid request
        409: Team name already exists
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    name = data.get("name", "").strip()

    if not name:
        return jsonify({"error": "Team name required"}), 400

    if len(name) > 255:
        return jsonify({
            "error": "Team name must be 255 characters or less"
        }), 400

    db = get_db()

    # Check if team name already exists
    existing = db(db.resource_teams.name == name).select().first()
    if existing:
        return jsonify({"error": "Team name already exists"}), 409

    description = data.get("description", "").strip()
    metadata = data.get("metadata")

    try:
        current_user = get_current_user()
        team_id = db.resource_teams.insert(
            name=name,
            description=description or None,
            created_by=current_user["id"],
            is_active=True,
            metadata=str(metadata) if metadata else None,
        )

        db.commit()

        team = db.resource_teams(team_id)

        return jsonify({
            "message": "Team created successfully",
            "team": {
                "id": team.id,
                "name": team.name,
                "description": team.description,
                "created_by": team.created_by,
                "is_active": team.is_active,
                "created_at": (
                    team.created_at.isoformat()
                    if team.created_at else None
                ),
            },
        }), 201

    except Exception as e:
        db.rollback()
        log.exception(f"Error creating team: {e}")
        return jsonify({"error": str(e)}), 500


@teams_bp.route("/", methods=["GET"])
@auth_required
async def list_user_teams():
    """List teams the current user belongs to.

    Query Parameters:
        active: Filter by active status (optional, default: true)

    Returns:
        200: List of user's teams
    """
    db = get_db()
    current_user = get_current_user()

    # Get teams where user is a member
    user_teams = db(
        (db.team_members.user_id == current_user["id"])
        & (db.resource_teams.id == db.team_members.team_id)
    ).select(db.resource_teams.ALL, orderby=db.resource_teams.name).as_list()

    teams = []
    for team_row in user_teams:
        team_dict = {
            "id": team_row["resource_teams"]["id"],
            "name": team_row["resource_teams"]["name"],
            "description": team_row["resource_teams"]["description"],
            "created_by": team_row["resource_teams"]["created_by"],
            "is_active": team_row["resource_teams"]["is_active"],
            "created_at": team_row["resource_teams"]["created_at"].isoformat()
            if team_row["resource_teams"]["created_at"] else None,
        }
        teams.append(team_dict)

    return jsonify({
        "teams": teams,
        "count": len(teams),
    }), 200


@teams_bp.route("/<int:team_id>", methods=["GET"])
@auth_required
async def get_team(team_id: int):
    """Get team details.

    Args:
        team_id: ID of the team

    Returns:
        200: Team details
        404: Team not found
    """
    db = get_db()

    team = db.resource_teams(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    current_user = get_current_user()

    # Check if user is a member of the team
    member = db(
        (db.team_members.team_id == team_id)
        & (db.team_members.user_id == current_user["id"])
    ).select().first()

    if not member and user_has_role("admin"):
        pass  # Admins can view any team
    elif not member:
        return jsonify({"error": "Access denied"}), 403

    # Count members
    member_count = db(db.team_members.team_id == team_id).count()

    return jsonify({
        "team": {
            "id": team.id,
            "name": team.name,
            "description": team.description,
            "created_by": team.created_by,
            "is_active": team.is_active,
            "member_count": member_count,
            "created_at": (
                team.created_at.isoformat()
                if team.created_at else None
            ),
            "updated_at": (
                team.updated_at.isoformat()
                if team.updated_at else None
            ),
        },
    }), 200


@teams_bp.route("/<int:team_id>", methods=["PATCH"])
@auth_required
async def update_team(team_id: int):
    """Update team details.

    Args:
        team_id: ID of the team

    Request Body:
        name: New team name (optional)
        description: New description (optional)
        is_active: Active status (optional)

    Returns:
        200: Team updated
        400: Invalid request
        403: Access denied
        404: Team not found
    """
    db = get_db()

    team = db.resource_teams(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    current_user = get_current_user()

    # Check permissions: only owner/admin can update
    if not user_has_role("admin"):
        owner = db(
            (db.team_members.team_id == team_id)
            & (db.team_members.user_id == current_user["id"])
            & (db.team_members.role == "owner")
        ).select().first()

        if not owner:
            return jsonify({"error": "Access denied"}), 403

    data = await request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    try:
        update_data = {}

        if "name" in data:
            name = data["name"].strip()
            if not name:
                return jsonify({
                    "error": "Team name cannot be empty"
                }), 400
            if len(name) > 255:
                return jsonify({
                    "error": "Team name must be 255 characters or less"
                }), 400

            # Check uniqueness excluding current team
            existing = db(
                (db.resource_teams.name == name)
                & (db.resource_teams.id != team_id)
            ).select().first()
            if existing:
                return jsonify({"error": "Team name already exists"}), 409

            update_data["name"] = name

        if "description" in data:
            update_data["description"] = data["description"].strip() or None

        if "is_active" in data:
            update_data["is_active"] = bool(data["is_active"])

        if update_data:
            update_data["updated_at"] = datetime.utcnow()
            db(db.resource_teams.id == team_id).update(**update_data)
            db.commit()

        team = db.resource_teams(team_id)

        return jsonify({
            "message": "Team updated successfully",
            "team": {
                "id": team.id,
                "name": team.name,
                "description": team.description,
                "is_active": team.is_active,
                "updated_at": (
                    team.updated_at.isoformat()
                    if team.updated_at else None
                ),
            },
        }), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error updating team {team_id}: {e}")
        return jsonify({"error": str(e)}), 500


@teams_bp.route("/<int:team_id>", methods=["DELETE"])
@auth_required
@roles_required("admin")
async def delete_team(team_id: int):
    """Delete a team.

    Args:
        team_id: ID of the team

    Returns:
        200: Team deleted
        404: Team not found
        500: Server error
    """
    db = get_db()

    team = db.resource_teams(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    try:
        # Delete cascades to team_members and resource_assignments
        db(db.resource_teams.id == team_id).delete()
        db.commit()

        return jsonify({"message": "Team deleted successfully"}), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error deleting team {team_id}: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Team Members Management
# ============================================================================


@teams_bp.route("/<int:team_id>/members", methods=["POST"])
@auth_required
async def add_member(team_id: int):
    """Add a member to the team.

    Args:
        team_id: ID of the team

    Request Body:
        user_id: ID of user to add (required)
        role: Team role (owner, admin, member, viewer, default: member)
        expires_at: Optional expiration datetime (ISO format)

    Returns:
        201: Member added
        400: Invalid request
        403: Access denied
        404: Team or user not found
        409: User already member
    """
    db = get_db()

    team = db.resource_teams(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    current_user = get_current_user()

    # Check permissions: team owner or admin
    if not user_has_role("admin"):
        member_role = db(
            (db.team_members.team_id == team_id)
            & (db.team_members.user_id == current_user["id"])
        ).select().first()

        if not member_role or member_role.role not in ["owner", "admin"]:
            return jsonify({"error": "Access denied"}), 403

    data = await request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "User ID required"}), 400

    # Verify user exists
    user = db.auth_user(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    # Check if already a member
    existing = db(
        (db.team_members.team_id == team_id)
        & (db.team_members.user_id == user_id)
    ).select().first()

    if existing:
        return jsonify({"error": "User is already a member"}), 409

    role = data.get("role", "member").lower()
    valid_roles = ["owner", "admin", "member", "viewer"]
    if role not in valid_roles:
        return jsonify({
            "error": f"Invalid role. Must be one of: {', '.join(valid_roles)}"
        }), 400

    try:
        expires_at = None
        if "expires_at" in data and data["expires_at"]:
            try:
                expires_at = datetime.fromisoformat(data["expires_at"])
            except ValueError:
                return jsonify({
                    "error": "Invalid expires_at datetime format"
                }), 400

        member_id = db.team_members.insert(
            team_id=team_id,
            user_id=user_id,
            role=role,
            added_by=current_user["id"],
            expires_at=expires_at,
        )

        db.commit()

        member = db.team_members(member_id)

        return jsonify({
            "message": "Member added successfully",
            "member": {
                "id": member.id,
                "team_id": member.team_id,
                "user_id": member.user_id,
                "role": member.role,
                "added_by": member.added_by,
                "added_at": (
                    member.added_at.isoformat()
                    if member.added_at else None
                ),
                "expires_at": (
                    member.expires_at.isoformat()
                    if member.expires_at else None
                ),
            },
        }), 201

    except Exception as e:
        db.rollback()
        log.exception(f"Error adding member to team {team_id}: {e}")
        return jsonify({"error": str(e)}), 500


@teams_bp.route("/<int:team_id>/members", methods=["GET"])
@auth_required
async def list_members(team_id: int):
    """List team members.

    Args:
        team_id: ID of the team

    Returns:
        200: List of members
        403: Access denied
        404: Team not found
    """
    db = get_db()

    team = db.resource_teams(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    current_user = get_current_user()

    # Check if user is a member
    if not user_has_role("admin"):
        member = db(
            (db.team_members.team_id == team_id)
            & (db.team_members.user_id == current_user["id"])
        ).select().first()

        if not member:
            return jsonify({"error": "Access denied"}), 403

    members = db(db.team_members.team_id == team_id).select().as_list()

    members_data = []
    for member in members:
        members_data.append({
            "id": member["id"],
            "user_id": member["user_id"],
            "role": member["role"],
            "added_by": member["added_by"],
            "added_at": (
                member["added_at"].isoformat()
                if member["added_at"] else None
            ),
            "expires_at": (
                member["expires_at"].isoformat()
                if member["expires_at"] else None
            ),
        })

    return jsonify({
        "team_id": team_id,
        "members": members_data,
        "count": len(members_data),
    }), 200


@teams_bp.route("/<int:team_id>/members/<int:user_id>", methods=["DELETE"])
@auth_required
async def remove_member(team_id: int, user_id: int):
    """Remove a member from the team.

    Args:
        team_id: ID of the team
        user_id: ID of user to remove

    Returns:
        200: Member removed
        403: Access denied
        404: Team or membership not found
    """
    db = get_db()

    team = db.resource_teams(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    current_user = get_current_user()

    # Check permissions: only team admin/owner or global admin
    if not user_has_role("admin"):
        requester_role = db(
            (db.team_members.team_id == team_id)
            & (db.team_members.user_id == current_user["id"])
        ).select().first()

        if not requester_role or requester_role.role not in ["owner", "admin"]:
            return jsonify({"error": "Access denied"}), 403

    member = db(
        (db.team_members.team_id == team_id)
        & (db.team_members.user_id == user_id)
    ).select().first()

    if not member:
        return jsonify({"error": "Team member not found"}), 404

    try:
        db(
            (db.team_members.team_id == team_id)
            & (db.team_members.user_id == user_id)
        ).delete()
        db.commit()

        return jsonify({"message": "Member removed successfully"}), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error removing member from team {team_id}: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Resource Management
# ============================================================================


@teams_bp.route("/<int:team_id>/resources", methods=["POST"])
@auth_required
async def assign_resource(team_id: int):
    """Assign a resource to the team.

    Args:
        team_id: ID of the team

    Request Body:
        resource_type: Type of resource (e.g., 'cloud', 'machine', 'network')
        resource_id: ID of the resource
        permissions: JSON array of permissions (required)

    Returns:
        201: Resource assigned
        400: Invalid request
        403: Access denied
        404: Team not found
    """
    db = get_db()

    team = db.resource_teams(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    current_user = get_current_user()

    # Check permissions
    if not user_has_role("admin"):
        member = db(
            (db.team_members.team_id == team_id)
            & (db.team_members.user_id == current_user["id"])
        ).select().first()

        if not member or member.role not in ["owner", "admin"]:
            return jsonify({"error": "Access denied"}), 403

    data = await request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    resource_type = data.get("resource_type", "").strip()
    resource_id = data.get("resource_id", "").strip()
    permissions = data.get("permissions")

    if not resource_type:
        return jsonify({"error": "resource_type required"}), 400

    if not resource_id:
        return jsonify({"error": "resource_id required"}), 400

    if permissions is None:
        return jsonify({"error": "permissions required"}), 400

    if not isinstance(permissions, (list, dict)):
        return jsonify({"error": "permissions must be a list or object"}), 400

    try:
        assignment_id = db.resource_assignments.insert(
            team_id=team_id,
            resource_type=resource_type,
            resource_id=resource_id,
            permissions=str(permissions),
            assigned_by=current_user["id"],
        )

        db.commit()

        assignment = db.resource_assignments(assignment_id)

        return jsonify({
            "message": "Resource assigned successfully",
            "assignment": {
                "id": assignment.id,
                "team_id": assignment.team_id,
                "resource_type": assignment.resource_type,
                "resource_id": assignment.resource_id,
                "permissions": assignment.permissions,
                "assigned_by": assignment.assigned_by,
                "assigned_at": assignment.assigned_at.isoformat()
                if assignment.assigned_at else None,
            },
        }), 201

    except Exception as e:
        db.rollback()
        log.exception(f"Error assigning resource to team {team_id}: {e}")
        return jsonify({"error": str(e)}), 500


@teams_bp.route("/<int:team_id>/resources", methods=["GET"])
@auth_required
async def list_resources(team_id: int):
    """List resources assigned to the team.

    Args:
        team_id: ID of the team

    Returns:
        200: List of assigned resources
        403: Access denied
        404: Team not found
    """
    db = get_db()

    team = db.resource_teams(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    current_user = get_current_user()

    # Check if user is a member
    if not user_has_role("admin"):
        member = db(
            (db.team_members.team_id == team_id)
            & (db.team_members.user_id == current_user["id"])
        ).select().first()

        if not member:
            return jsonify({"error": "Access denied"}), 403

    resources = (
        db(db.resource_assignments.team_id == team_id)
        .select()
        .as_list()
    )

    resources_data = []
    for resource in resources:
        resources_data.append({
            "id": resource["id"],
            "team_id": resource["team_id"],
            "resource_type": resource["resource_type"],
            "resource_id": resource["resource_id"],
            "permissions": resource["permissions"],
            "assigned_by": resource["assigned_by"],
            "assigned_at": resource["assigned_at"].isoformat()
            if resource["assigned_at"] else None,
        })

    return jsonify({
        "team_id": team_id,
        "resources": resources_data,
        "count": len(resources_data),
    }), 200


@teams_bp.route(
    "/<int:team_id>/resources/<int:assignment_id>",
    methods=["DELETE"]
)
@auth_required
async def unassign_resource(team_id: int, assignment_id: int):
    """Unassign a resource from the team.

    Args:
        team_id: ID of the team
        assignment_id: ID of the resource assignment

    Returns:
        200: Resource unassigned
        403: Access denied
        404: Team or assignment not found
    """
    db = get_db()

    team = db.resource_teams(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    assignment = db.resource_assignments(assignment_id)
    if not assignment or assignment.team_id != team_id:
        return jsonify({"error": "Resource assignment not found"}), 404

    current_user = get_current_user()

    # Check permissions
    if not user_has_role("admin"):
        member = db(
            (db.team_members.team_id == team_id)
            & (db.team_members.user_id == current_user["id"])
        ).select().first()

        if not member or member.role not in ["owner", "admin"]:
            return jsonify({"error": "Access denied"}), 403

    try:
        db(db.resource_assignments.id == assignment_id).delete()
        db.commit()

        return jsonify({"message": "Resource unassigned successfully"}), 200

    except Exception as e:
        db.rollback()
        log.exception(f"Error unassigning resource from team {team_id}: {e}")
        return jsonify({"error": str(e)}), 500
