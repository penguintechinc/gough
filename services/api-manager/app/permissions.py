"""Team and resource permission helpers for Gough.

Provides RBAC (role-based access control) for team membership and resource
access.
"""

from functools import wraps
from typing import Callable

from quart import g, jsonify

from .middleware import get_current_user
from .models import get_db

# Team role hierarchy
TEAM_ROLES = ["owner", "admin", "member", "viewer"]

# Resource permission types
RESOURCE_PERMISSIONS = ["read", "write", "execute", "admin", "shell"]


def check_team_access(
    user_id: int, team_id: int, required_role: str = "member"
) -> bool:
    """Check if user is in team with required role level.

    Args:
        user_id: User ID to check
        team_id: Team ID to check membership
        required_role: Minimum role required (owner > admin > member > viewer)

    Returns:
        True if user has required role or higher in team
    """
    db = get_db()
    try:
        # Find team membership record
        membership = db(
            (db.team_members.user_id == user_id)
            & (db.team_members.team_id == team_id)
        ).select(db.team_members.role).first()

        if not membership:
            return False

        user_role = membership.role
        required_idx = TEAM_ROLES.index(required_role)
        user_idx = TEAM_ROLES.index(user_role)

        # Higher index means higher privilege
        return user_idx >= required_idx
    except (KeyError, AttributeError):
        return False


def check_resource_permission(
    user_id: int,
    resource_type: str,
    resource_id: int,
    permission: str = "read",
) -> bool:
    """Check if user has permission for resource via team assignment.

    Args:
        user_id: User ID to check
        resource_type: Type of resource (e.g., 'cloud_provider', 'lxd_cluster')
        resource_id: Resource ID
        permission: Required permission (read, write, execute, admin, shell)

    Returns:
        True if user has permission
    """
    db = get_db()
    try:
        # Get resource owner/team
        resource_table = db[resource_type]
        resource = db(resource_table.id == resource_id).select().first()

        if not resource:
            return False

        # Check if user is owner
        if hasattr(resource, "created_by") and resource.created_by == user_id:
            return True

        # Check team permissions if resource is team-scoped
        if hasattr(resource, "team_id"):
            team_id = resource.team_id
            perms = db(
                (db.resource_permissions.user_id == user_id)
                & (db.resource_permissions.resource_type == resource_type)
                & (db.resource_permissions.resource_id == resource_id)
            ).select(db.resource_permissions.permission).first()

            if perms and permission in (perms.permission or "").split(","):
                return True

            # Fall back to team role check for admin/write
            if permission in ("admin", "write"):
                return check_team_access(user_id, team_id, "admin")

            # read/execute allowed for all team members
            if permission in ("read", "execute"):
                return check_team_access(user_id, team_id, "member")

        return False
    except (KeyError, AttributeError):
        return False


def check_shell_access(
    user_id: int, resource_type: str, resource_id: int
) -> bool:
    """Shortcut for checking shell permission on resource.

    Args:
        user_id: User ID to check
        resource_type: Type of resource
        resource_id: Resource ID

    Returns:
        True if user has shell permission
    """
    return check_resource_permission(
        user_id, resource_type, resource_id, "shell"
    )


def require_team_permission(required_role: str = "member") -> Callable:
    """Decorator: require user to have role in team from request context.

    Expects team_id in request args, JSON, or path parameter.

    Args:
        required_role: Minimum role required

    Returns:
        403 Forbidden if check fails
    """

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            from quart import request

            # Get team_id from kwargs (route param), JSON, or args
            team_id = kwargs.get("team_id")
            if not team_id and await request.is_json:
                json_data = await request.get_json()
                team_id = json_data.get("team_id")
            if not team_id:
                team_id = request.args.get("team_id", type=int)

            if not team_id or not get_current_user():
                return jsonify({"error": "Forbidden"}), 403

            user = get_current_user()
            if not check_team_access(user["id"], team_id, required_role):
                return jsonify({"error": "Forbidden"}), 403

            result = fn(*args, **kwargs)
            if hasattr(result, '__await__'):
                return await result
            return result

        return wrapped

    return decorator


def require_resource_permission(permission: str = "read") -> Callable:
    """Decorator: require user to have permission on resource.

    Expects resource_type and resource_id in request args, JSON, or path
    parameters.

    Args:
        permission: Required permission type

    Returns:
        403 Forbidden if check fails
    """

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            from quart import request

            # Get resource identifiers from kwargs or request
            resource_type = (
                kwargs.get("resource_type")
                or request.args.get("resource_type")
            )
            resource_id = (
                kwargs.get("resource_id", type=int)
                or request.args.get("resource_id", type=int)
            )

            if not await request.is_json:
                request_data = {}
            else:
                request_data = await request.get_json() or {}

            resource_type = resource_type or request_data.get("resource_type")
            resource_id = resource_id or request_data.get("resource_id")

            if not resource_type or not resource_id or not get_current_user():
                return jsonify({"error": "Forbidden"}), 403

            user = get_current_user()
            if not check_resource_permission(
                user["id"], resource_type, resource_id, permission
            ):
                return jsonify({"error": "Forbidden"}), 403

            result = fn(*args, **kwargs)
            if hasattr(result, '__await__'):
                return await result
            return result

        return wrapped

    return decorator
