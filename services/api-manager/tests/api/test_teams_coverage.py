"""Coverage improvement tests for app.api.teams blueprint.

Focuses on edge cases and error paths not fully covered by test_teams.py.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


def _passthrough(*dargs, **dkwargs):
    """Decorator passthrough: supports both @decorator and @decorator() styles."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


@pytest.fixture()
def mock_db():
    """Create a mock penguin-dal DB with all required tables."""
    db = MagicMock()
    db.resource_teams = MagicMock()
    db.team_members = MagicMock()
    db.resource_assignments = MagicMock()
    db.auth_user = MagicMock()
    db.commit = MagicMock()
    db.rollback = MagicMock()
    return db


@pytest.fixture()
def teams_app(monkeypatch, mock_db):
    """Create a minimal Quart app with teams blueprint registered."""
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough)

    import app.api.teams as teams_mod
    teams_mod = importlib.reload(teams_mod)

    monkeypatch.setattr(teams_mod, "get_db", lambda: mock_db)
    monkeypatch.setattr(teams_mod, "get_current_user", lambda: {
        "id": 1,
        "email": "admin@test.com",
        "role": "admin"
    })
    monkeypatch.setattr(teams_mod, "user_has_role", lambda role: role == "admin")

    from quart import Quart, g

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.url_map.strict_slashes = False
    app.register_blueprint(teams_mod.teams_bp)

    @app.before_request
    async def _inject_identity():
        g.current_user = {
            "id": 1,
            "email": "admin@test.com",
            "role": "admin"
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return app, teams_mod


# ============================================================================
# Add Member Tests - Expires_at and DB Errors
# ============================================================================


@pytest.mark.asyncio
async def test_add_member_with_expires_at_valid(teams_app):
    """Should add a member with valid expires_at datetime."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    user = MagicMock()
    user.id = 2
    mock_db.auth_user.return_value = user

    member_query = MagicMock()
    member_query.select.return_value.first.return_value = None
    mock_db.return_value = member_query

    new_member = MagicMock()
    new_member.id = 1
    new_member.team_id = 1
    new_member.user_id = 2
    new_member.role = "admin"
    new_member.added_by = 1
    new_member.added_at = datetime.now(timezone.utc)
    future_date = datetime(2025, 12, 31, 23, 59, 59)
    new_member.expires_at = future_date

    mock_db.team_members.insert.return_value = 1
    mock_db.team_members.return_value = new_member

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/members",
        json={
            "user_id": 2,
            "role": "admin",
            "expires_at": "2025-12-31T23:59:59"
        }
    )

    assert response.status_code == 201
    data = await response.get_json()
    assert data["member"]["role"] == "admin"


@pytest.mark.asyncio
async def test_add_member_with_expires_at_invalid_format(teams_app):
    """Should return 400 if expires_at format is invalid."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    user = MagicMock()
    user.id = 2
    mock_db.auth_user.return_value = user

    member_query = MagicMock()
    member_query.select.return_value.first.return_value = None
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/members",
        json={
            "user_id": 2,
            "expires_at": "invalid-date"
        }
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "Invalid expires_at datetime format" in data["error"]


@pytest.mark.asyncio
async def test_add_member_db_error(teams_app):
    """Should return 500 if database error during insert."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    user = MagicMock()
    user.id = 2
    mock_db.auth_user.return_value = user

    member_query = MagicMock()
    member_query.select.return_value.first.return_value = None
    mock_db.return_value = member_query

    mock_db.team_members.insert.side_effect = Exception("DB error")

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/members",
        json={"user_id": 2}
    )

    assert response.status_code == 500
    assert mock_db.rollback.called


@pytest.mark.asyncio
async def test_add_member_non_admin_no_permission(teams_app, monkeypatch):
    """Non-admin member without owner/admin role should get 403."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    monkeypatch.setattr(teams_mod, "user_has_role", lambda role: False)

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # User is not owner/admin in team
    member_role = MagicMock()
    member_role.role = "member"
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = member_role
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/members",
        json={"user_id": 2}
    )

    assert response.status_code == 403
    data = await response.get_json()
    assert "Access denied" in data["error"]


# ============================================================================
# List Members Tests - Empty List and Access Control
# ============================================================================


@pytest.mark.asyncio
async def test_list_members_empty(teams_app):
    """Should return empty list when no members exist."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # Admin user can view without being a member
    members_query = MagicMock()
    members_query.select.return_value.as_list.return_value = []
    mock_db.return_value = members_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/1/members")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 0
    assert data["members"] == []


@pytest.mark.asyncio
async def test_list_members_non_member_access_denied(teams_app, monkeypatch):
    """Non-member non-admin should get 403."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    monkeypatch.setattr(teams_mod, "user_has_role", lambda role: False)

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # User is not a member
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = None
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/1/members")

    assert response.status_code == 403
    data = await response.get_json()
    assert "Access denied" in data["error"]


@pytest.mark.asyncio
async def test_list_members_with_expires_at(teams_app):
    """Should list members including those with expires_at."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    members_list = [
        {
            "id": 1,
            "user_id": 2,
            "role": "owner",
            "added_by": 1,
            "added_at": datetime.now(timezone.utc),
            "expires_at": None,
        },
        {
            "id": 2,
            "user_id": 3,
            "role": "member",
            "added_by": 1,
            "added_at": datetime.now(timezone.utc),
            "expires_at": datetime(2025, 12, 31, 23, 59, 59),
        }
    ]

    members_query = MagicMock()
    members_query.select.return_value.as_list.return_value = members_list
    mock_db.return_value = members_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/1/members")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 2
    assert data["members"][0]["expires_at"] is None
    assert data["members"][1]["expires_at"] is not None


# ============================================================================
# Remove Member Tests - DB Error and Permission Checks
# ============================================================================


@pytest.mark.asyncio
async def test_remove_member_db_error(teams_app):
    """Should return 500 if database error during delete."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    member = MagicMock()
    delete_query = MagicMock()
    delete_query.select.return_value.first.return_value = member
    mock_db.return_value = delete_query

    delete_query.delete.side_effect = Exception("DB error")

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1/members/2")

    assert response.status_code == 500
    assert mock_db.rollback.called


@pytest.mark.asyncio
async def test_remove_member_non_member_access_denied(teams_app, monkeypatch):
    """Non-member without permission should get 403."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    monkeypatch.setattr(teams_mod, "user_has_role", lambda role: False)

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # Requester has no team membership
    requester_query = MagicMock()
    requester_query.select.return_value.first.return_value = None
    mock_db.return_value = requester_query

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1/members/2")

    assert response.status_code == 403
    data = await response.get_json()
    assert "Access denied" in data["error"]


@pytest.mark.asyncio
async def test_remove_member_viewer_role_no_permission(teams_app, monkeypatch):
    """Member with viewer role should not be able to remove members."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    monkeypatch.setattr(teams_mod, "user_has_role", lambda role: False)

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # Requester is member but with viewer role
    requester_role = MagicMock()
    requester_role.role = "viewer"
    requester_query = MagicMock()
    requester_query.select.return_value.first.return_value = requester_role
    mock_db.return_value = requester_query

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1/members/2")

    assert response.status_code == 403


# ============================================================================
# Resource Assignment Tests - Edge Cases and Errors
# ============================================================================


@pytest.mark.asyncio
async def test_assign_resource_success(teams_app):
    """Should assign a resource to the team."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # Admin can assign without being member
    assignment = MagicMock()
    assignment.id = 1
    assignment.team_id = 1
    assignment.resource_type = "cloud"
    assignment.resource_id = "aws-prod"
    assignment.permissions = '{"read": true, "write": false}'
    assignment.assigned_by = 1
    assignment.assigned_at = datetime.now(timezone.utc)

    mock_db.resource_assignments.insert.return_value = 1
    mock_db.resource_assignments.return_value = assignment

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/resources",
        json={
            "resource_type": "cloud",
            "resource_id": "aws-prod",
            "permissions": {"read": True, "write": False}
        }
    )

    assert response.status_code == 201
    data = await response.get_json()
    assert data["assignment"]["resource_type"] == "cloud"


@pytest.mark.asyncio
async def test_assign_resource_missing_resource_type(teams_app):
    """Should return 400 if resource_type missing."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/resources",
        json={
            "resource_id": "aws-prod",
            "permissions": {"read": True}
        }
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "resource_type required" in data["error"]


@pytest.mark.asyncio
async def test_assign_resource_missing_resource_id(teams_app):
    """Should return 400 if resource_id missing."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/resources",
        json={
            "resource_type": "cloud",
            "permissions": {"read": True}
        }
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "resource_id required" in data["error"]


@pytest.mark.asyncio
async def test_assign_resource_missing_permissions(teams_app):
    """Should return 400 if permissions missing."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/resources",
        json={
            "resource_type": "cloud",
            "resource_id": "aws-prod"
        }
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "permissions required" in data["error"]


@pytest.mark.asyncio
async def test_assign_resource_invalid_permissions_type(teams_app):
    """Should return 400 if permissions is not dict/list."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/resources",
        json={
            "resource_type": "cloud",
            "resource_id": "aws-prod",
            "permissions": "invalid"
        }
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "must be a list or object" in data["error"]


@pytest.mark.asyncio
async def test_assign_resource_db_error(teams_app):
    """Should return 500 if database error during insert."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    mock_db.resource_assignments.insert.side_effect = Exception("DB error")

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/resources",
        json={
            "resource_type": "cloud",
            "resource_id": "aws-prod",
            "permissions": {"read": True}
        }
    )

    assert response.status_code == 500
    assert mock_db.rollback.called


@pytest.mark.asyncio
async def test_assign_resource_non_admin_no_permission(teams_app, monkeypatch):
    """Non-admin member without owner/admin role should get 403."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    monkeypatch.setattr(teams_mod, "user_has_role", lambda role: False)

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    member = MagicMock()
    member.role = "viewer"
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = member
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/resources",
        json={
            "resource_type": "cloud",
            "resource_id": "aws-prod",
            "permissions": {"read": True}
        }
    )

    assert response.status_code == 403


# ============================================================================
# List Resources Tests
# ============================================================================


@pytest.mark.asyncio
async def test_list_resources_success(teams_app):
    """Should list resources assigned to team."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    resources = [
        {
            "id": 1,
            "team_id": 1,
            "resource_type": "cloud",
            "resource_id": "aws-prod",
            "permissions": '{"read": true}',
            "assigned_by": 1,
            "assigned_at": datetime.now(timezone.utc),
        },
        {
            "id": 2,
            "team_id": 1,
            "resource_type": "machine",
            "resource_id": "server-01",
            "permissions": '{"admin": true}',
            "assigned_by": 1,
            "assigned_at": datetime.now(timezone.utc),
        }
    ]

    resources_query = MagicMock()
    resources_query.select.return_value.as_list.return_value = resources
    mock_db.return_value = resources_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/1/resources")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 2
    assert len(data["resources"]) == 2


@pytest.mark.asyncio
async def test_list_resources_empty(teams_app):
    """Should return empty list when no resources assigned."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    resources_query = MagicMock()
    resources_query.select.return_value.as_list.return_value = []
    mock_db.return_value = resources_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/1/resources")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 0
    assert data["resources"] == []


@pytest.mark.asyncio
async def test_list_resources_non_member_access_denied(teams_app, monkeypatch):
    """Non-member non-admin should get 403."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    monkeypatch.setattr(teams_mod, "user_has_role", lambda role: False)

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    member_query = MagicMock()
    member_query.select.return_value.first.return_value = None
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/1/resources")

    assert response.status_code == 403
    data = await response.get_json()
    assert "Access denied" in data["error"]


# ============================================================================
# Unassign Resource Tests
# ============================================================================


@pytest.mark.asyncio
async def test_unassign_resource_success(teams_app):
    """Should unassign a resource from team."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    assignment = MagicMock()
    assignment.team_id = 1
    mock_db.resource_assignments.return_value = assignment

    delete_query = MagicMock()
    mock_db.return_value = delete_query

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1/resources/1")

    assert response.status_code == 200
    data = await response.get_json()
    assert "unassigned successfully" in data["message"]


@pytest.mark.asyncio
async def test_unassign_resource_not_found(teams_app):
    """Should return 404 if assignment doesn't exist."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    mock_db.resource_assignments.return_value = None

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1/resources/999")

    assert response.status_code == 404
    data = await response.get_json()
    assert "not found" in data["error"]


@pytest.mark.asyncio
async def test_unassign_resource_wrong_team(teams_app):
    """Should return 404 if assignment belongs to different team."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    assignment = MagicMock()
    assignment.team_id = 2  # Different team
    mock_db.resource_assignments.return_value = assignment

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1/resources/1")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unassign_resource_db_error(teams_app):
    """Should return 500 if database error during delete."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    assignment = MagicMock()
    assignment.team_id = 1
    mock_db.resource_assignments.return_value = assignment

    delete_query = MagicMock()
    delete_query.delete.side_effect = Exception("DB error")
    mock_db.return_value = delete_query

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1/resources/1")

    assert response.status_code == 500
    assert mock_db.rollback.called


@pytest.mark.asyncio
async def test_unassign_resource_non_admin_no_permission(teams_app, monkeypatch):
    """Non-admin without owner/admin role should get 403."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    monkeypatch.setattr(teams_mod, "user_has_role", lambda role: False)

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    assignment = MagicMock()
    assignment.team_id = 1
    mock_db.resource_assignments.return_value = assignment

    member = MagicMock()
    member.role = "viewer"
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = member
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1/resources/1")

    assert response.status_code == 403
