"""Tests for app.api.teams blueprint (team management API).

Tests CRUD operations for teams, team memberships, and resource assignments.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


def _passthrough(*dargs, **dkwargs):
    """Decorator passthrough: supports both @decorator and @decorator() styles."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture()
def mock_db():
    """Create a mock penguin-dal DB with all required tables."""
    db = MagicMock()

    # Configure table mocks
    db.resource_teams = MagicMock()
    db.team_members = MagicMock()
    db.resource_assignments = MagicMock()
    db.auth_user = MagicMock()

    # Add commit/rollback methods
    db.commit = MagicMock()
    db.rollback = MagicMock()

    return db


@pytest.fixture()
def teams_app(monkeypatch, mock_db):
    """Create a minimal Quart app with teams blueprint registered."""
    # Patch decorators to passthroughs
    def _passthrough(*dargs, **dkwargs):
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]
        return lambda fn: fn

    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough)

    # Reload teams module with patched decorators
    import app.api.teams as teams_mod
    teams_mod = importlib.reload(teams_mod)

    # Patch get_db and middleware functions
    monkeypatch.setattr(teams_mod, "get_db", lambda: mock_db)
    monkeypatch.setattr(teams_mod, "get_current_user", lambda: {
        "id": 1,
        "email": "admin@test.com",
        "role": "admin"
    })
    monkeypatch.setattr(teams_mod, "user_has_role", lambda role: role == "admin")

    # Create Quart app
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
# Tests: POST /api/v1/teams (create_team)
# ============================================================================


@pytest.mark.asyncio
async def test_create_team_success(teams_app):
    """Should create a new team successfully."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    # Setup mock DB responses
    mock_db.return_value.select.return_value.first.return_value = None  # no existing team
    mock_db.resource_teams.insert.return_value = 1  # new team_id
    team_obj = MagicMock()
    team_obj.id = 1
    team_obj.name = "Test Team"
    team_obj.description = "A test team"
    team_obj.created_by = 1
    team_obj.is_active = True
    team_obj.created_at = datetime.now(timezone.utc)
    mock_db.resource_teams.return_value = team_obj

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/",
        json={
            "name": "Test Team",
            "description": "A test team",
            "metadata": {"key": "value"}
        }
    )

    assert response.status_code == 201
    data = await response.get_json()
    assert data["message"] == "Team created successfully"
    assert data["team"]["name"] == "Test Team"
    assert data["team"]["id"] == 1


@pytest.mark.asyncio
async def test_create_team_no_body(teams_app):
    """Should return 400 if no request body."""
    app, _ = teams_app

    client = app.test_client()
    response = await client.post("/api/v1/teams/", json=None)

    assert response.status_code == 400
    data = await response.get_json()
    assert "Request body required" in data["error"]


@pytest.mark.asyncio
async def test_create_team_no_name(teams_app):
    """Should return 400 if name is missing."""
    app, _ = teams_app

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/",
        json={"description": "No name provided"}
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "Team name required" in data["error"]


@pytest.mark.asyncio
async def test_create_team_empty_name(teams_app):
    """Should return 400 if name is empty after strip."""
    app, _ = teams_app

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/",
        json={"name": "   "}
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "Team name required" in data["error"]


@pytest.mark.asyncio
async def test_create_team_name_too_long(teams_app):
    """Should return 400 if name exceeds 255 characters."""
    app, _ = teams_app

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/",
        json={"name": "x" * 256}
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "255 characters or less" in data["error"]


@pytest.mark.asyncio
async def test_create_team_duplicate_name(teams_app):
    """Should return 409 if team name already exists."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    # Setup: existing team with same name
    existing = MagicMock()
    existing.name = "Duplicate Team"

    db_query = MagicMock()
    db_query.select.return_value.first.return_value = existing
    mock_db.return_value = db_query

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/",
        json={"name": "Duplicate Team"}
    )

    assert response.status_code == 409
    data = await response.get_json()
    assert "already exists" in data["error"]


@pytest.mark.asyncio
async def test_create_team_db_error(teams_app):
    """Should return 500 on database error."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    # Setup: no existing team, but insert fails
    db_query = MagicMock()
    db_query.select.return_value.first.return_value = None
    mock_db.return_value = db_query
    mock_db.resource_teams.insert.side_effect = Exception("DB error")

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/",
        json={"name": "Test Team"}
    )

    assert response.status_code == 500
    data = await response.get_json()
    assert "error" in data


# ============================================================================
# Tests: GET /api/v1/teams (list_user_teams)
# ============================================================================


@pytest.mark.asyncio
async def test_list_user_teams_success(teams_app):
    """Should list teams for current user."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    # Setup: user is member of 2 teams
    teams_list = [
        {
            "resource_teams": {
                "id": 1,
                "name": "Team A",
                "description": "First team",
                "created_by": 1,
                "is_active": True,
                "created_at": datetime.now(timezone.utc)
            }
        },
        {
            "resource_teams": {
                "id": 2,
                "name": "Team B",
                "description": "Second team",
                "created_by": 2,
                "is_active": True,
                "created_at": datetime.now(timezone.utc)
            }
        }
    ]

    db_query = MagicMock()
    db_query.select.return_value.as_list.return_value = teams_list
    mock_db.return_value = db_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 2
    assert len(data["teams"]) == 2
    assert data["teams"][0]["name"] == "Team A"


@pytest.mark.asyncio
async def test_list_user_teams_empty(teams_app):
    """Should return empty list if user is not member of any teams."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    db_query = MagicMock()
    db_query.select.return_value.as_list.return_value = []
    mock_db.return_value = db_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 0
    assert data["teams"] == []


# ============================================================================
# Tests: GET /api/v1/teams/<int:team_id> (get_team)
# ============================================================================


@pytest.mark.asyncio
async def test_get_team_success(teams_app):
    """Should get team details."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    team.name = "Test Team"
    team.description = "A test team"
    team.created_by = 1
    team.is_active = True
    team.created_at = datetime.now(timezone.utc)
    team.updated_at = datetime.now(timezone.utc)

    mock_db.resource_teams.return_value = team

    # Mock member count
    member_query = MagicMock()
    member_query.count.return_value = 5
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/1")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["team"]["name"] == "Test Team"
    assert data["team"]["member_count"] == 5


@pytest.mark.asyncio
async def test_get_team_not_found(teams_app):
    """Should return 404 if team doesn't exist."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    mock_db.resource_teams.return_value = None

    client = app.test_client()
    response = await client.get("/api/v1/teams/999")

    assert response.status_code == 404
    data = await response.get_json()
    assert "Team not found" in data["error"]


@pytest.mark.asyncio
async def test_get_team_non_member_non_admin(teams_app, monkeypatch):
    """Non-member non-admin should get 403."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    # Make user_has_role return False for admin
    monkeypatch.setattr(teams_mod, "user_has_role", lambda role: False)

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # User is not a member
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = None
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/1")

    assert response.status_code == 403
    data = await response.get_json()
    assert "Access denied" in data["error"]


# ============================================================================
# Tests: PATCH /api/v1/teams/<int:team_id> (update_team)
# ============================================================================


@pytest.mark.asyncio
async def test_update_team_success(teams_app):
    """Should update team details."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    team.name = "Updated Team"
    team.description = "Updated description"
    team.is_active = True
    team.updated_at = datetime.now(timezone.utc)

    mock_db.resource_teams.return_value = team

    # Mock the update query — first() must return None (no duplicate name exists)
    update_query = MagicMock()
    update_query.select.return_value.first.return_value = None
    mock_db.return_value = update_query

    client = app.test_client()
    response = await client.patch(
        "/api/v1/teams/1",
        json={"name": "Updated Team", "description": "Updated description"}
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert data["message"] == "Team updated successfully"


@pytest.mark.asyncio
async def test_update_team_not_found(teams_app):
    """Should return 404 if team doesn't exist."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    mock_db.resource_teams.return_value = None

    client = app.test_client()
    response = await client.patch(
        "/api/v1/teams/999",
        json={"name": "New Name"}
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_team_no_body(teams_app):
    """Should return 400 if no body provided."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    client = app.test_client()
    response = await client.patch("/api/v1/teams/1", json=None)

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_update_team_empty_name(teams_app):
    """Should return 400 if name is empty."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    client = app.test_client()
    response = await client.patch(
        "/api/v1/teams/1",
        json={"name": "   "}
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_update_team_name_too_long(teams_app):
    """Should return 400 if name exceeds 255 characters."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    client = app.test_client()
    response = await client.patch(
        "/api/v1/teams/1",
        json={"name": "x" * 256}
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_update_team_duplicate_name(teams_app):
    """Should return 409 if new name already exists."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # Mock existing team with different ID
    existing = MagicMock()
    existing.id = 2
    existing.name = "Duplicate"

    db_query = MagicMock()
    db_query.select.return_value.first.return_value = existing
    mock_db.return_value = db_query

    client = app.test_client()
    response = await client.patch(
        "/api/v1/teams/1",
        json={"name": "Duplicate"}
    )

    assert response.status_code == 409


# ============================================================================
# Tests: DELETE /api/v1/teams/<int:team_id> (delete_team)
# ============================================================================


@pytest.mark.asyncio
async def test_delete_team_success(teams_app):
    """Should delete a team."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # Mock delete query
    delete_query = MagicMock()
    mock_db.return_value = delete_query

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1")

    assert response.status_code == 200
    data = await response.get_json()
    assert "deleted successfully" in data["message"]


@pytest.mark.asyncio
async def test_delete_team_not_found(teams_app):
    """Should return 404 if team doesn't exist."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    mock_db.resource_teams.return_value = None

    client = app.test_client()
    response = await client.delete("/api/v1/teams/999")

    assert response.status_code == 404


# ============================================================================
# Tests: POST /api/v1/teams/<int:team_id>/members (add_member)
# ============================================================================


@pytest.mark.asyncio
async def test_add_member_success(teams_app):
    """Should add a member to the team."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # Mock user exists
    user = MagicMock()
    user.id = 2
    mock_db.auth_user.return_value = user

    # Mock no existing membership
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = None
    mock_db.return_value = member_query

    # Mock new member
    new_member = MagicMock()
    new_member.id = 1
    new_member.team_id = 1
    new_member.user_id = 2
    new_member.role = "member"
    new_member.added_by = 1
    new_member.added_at = datetime.now(timezone.utc)
    new_member.expires_at = None

    mock_db.team_members.insert.return_value = 1
    mock_db.team_members.return_value = new_member

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/members",
        json={"user_id": 2, "role": "member"}
    )

    assert response.status_code == 201
    data = await response.get_json()
    assert data["message"] == "Member added successfully"
    assert data["member"]["role"] == "member"


@pytest.mark.asyncio
async def test_add_member_team_not_found(teams_app):
    """Should return 404 if team doesn't exist."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    mock_db.resource_teams.return_value = None

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/999/members",
        json={"user_id": 2}
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_add_member_no_body(teams_app):
    """Should return 400 if no body."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    client = app.test_client()
    response = await client.post("/api/v1/teams/1/members", json=None)

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_add_member_no_user_id(teams_app):
    """Should return 400 if user_id missing."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/members",
        json={"role": "member"}
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "User ID required" in data["error"]


@pytest.mark.asyncio
async def test_add_member_user_not_found(teams_app):
    """Should return 404 if user doesn't exist."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    mock_db.auth_user.return_value = None

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/members",
        json={"user_id": 999}
    )

    assert response.status_code == 404
    data = await response.get_json()
    assert "User not found" in data["error"]


@pytest.mark.asyncio
async def test_add_member_already_member(teams_app):
    """Should return 409 if user is already a member."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    user = MagicMock()
    user.id = 2
    mock_db.auth_user.return_value = user

    # Mock existing membership
    existing_member = MagicMock()
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = existing_member
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/members",
        json={"user_id": 2}
    )

    assert response.status_code == 409
    data = await response.get_json()
    assert "already a member" in data["error"]


@pytest.mark.asyncio
async def test_add_member_invalid_role(teams_app):
    """Should return 400 if invalid role."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    user = MagicMock()
    user.id = 2
    mock_db.auth_user.return_value = user

    # Mock no existing membership
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = None
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/members",
        json={"user_id": 2, "role": "invalid_role"}
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "Invalid role" in data["error"]


# ============================================================================
# Tests: GET /api/v1/teams/<int:team_id>/members (list_members)
# ============================================================================


@pytest.mark.asyncio
async def test_list_members_success(teams_app):
    """Should list team members.

    # regression: gh-22
    list_members' SELECT now runs via run_db() instead of blocking the
    request coroutine inline -- proves it still returns seeded members
    correctly through that thread hop.
    """
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    members_list = [
        {
            "id": 1,
            "user_id": 1,
            "role": "owner",
            "added_by": 1,
            "added_at": datetime.now(timezone.utc),
            "expires_at": None
        },
        {
            "id": 2,
            "user_id": 2,
            "role": "member",
            "added_by": 1,
            "added_at": datetime.now(timezone.utc),
            "expires_at": None
        }
    ]

    # Mock user is a member
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = MagicMock(role="owner")
    member_query.select.return_value.as_list.return_value = members_list
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/1/members")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 2
    assert len(data["members"]) == 2


@pytest.mark.asyncio
async def test_list_members_team_not_found(teams_app):
    """Should return 404 if team doesn't exist."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    mock_db.resource_teams.return_value = None

    client = app.test_client()
    response = await client.get("/api/v1/teams/999/members")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_members_non_member_denied(teams_app, monkeypatch):
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


# ============================================================================
# Tests: DELETE /api/v1/teams/<int:team_id>/members/<int:user_id>
# ============================================================================


@pytest.mark.asyncio
async def test_remove_member_success(teams_app):
    """Should remove a member from the team."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # user_has_role("admin") is True in the fixture, so the requester check is
    # skipped. The member lookup db(...) call returns the existing member.
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = MagicMock(user_id=2)
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1/members/2")

    assert response.status_code == 200
    data = await response.get_json()
    assert "removed successfully" in data["message"]


@pytest.mark.asyncio
async def test_remove_member_team_not_found(teams_app):
    """Should return 404 if team doesn't exist."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    mock_db.resource_teams.return_value = None

    client = app.test_client()
    response = await client.delete("/api/v1/teams/999/members/2")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_remove_member_not_found(teams_app):
    """Should return 404 if member doesn't exist."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # user_has_role("admin") is True in the fixture, so the requester check is
    # skipped. Only ONE db(...) call happens: the member lookup, which returns None.
    member_query = MagicMock()
    member_query.select.return_value.first.return_value = None
    mock_db.return_value = member_query

    client = app.test_client()
    response = await client.delete("/api/v1/teams/1/members/999")

    assert response.status_code == 404


# ============================================================================
# Tests: POST /api/v1/teams/<int:team_id>/resources (assign_resource)
# ============================================================================


@pytest.mark.asyncio
async def test_assign_resource_success(teams_app):
    """Should assign a resource to the team."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    # Mock assignment created
    assignment = MagicMock()
    assignment.id = 1
    assignment.team_id = 1
    assignment.resource_type = "cloud"
    assignment.resource_id = "res-123"
    assignment.permissions = '["read", "write"]'
    assignment.assigned_by = 1
    assignment.assigned_at = datetime.now(timezone.utc)

    mock_db.resource_assignments.insert.return_value = 1
    mock_db.resource_assignments.return_value = assignment

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/resources",
        json={
            "resource_type": "cloud",
            "resource_id": "res-123",
            "permissions": ["read", "write"]
        }
    )

    assert response.status_code == 201
    data = await response.get_json()
    assert data["message"] == "Resource assigned successfully"


@pytest.mark.asyncio
async def test_assign_resource_no_resource_type(teams_app):
    """Should return 400 if resource_type missing."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    client = app.test_client()
    response = await client.post(
        "/api/v1/teams/1/resources",
        json={"resource_id": "res-123", "permissions": ["read"]}
    )

    assert response.status_code == 400


# ============================================================================
# Tests: GET /api/v1/teams/<int:team_id>/resources (list_resources)
# ============================================================================


@pytest.mark.asyncio
async def test_list_resources_success(teams_app):
    """Should list resources assigned to the team."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    resources_list = [
        {
            "id": 1,
            "team_id": 1,
            "resource_type": "cloud",
            "resource_id": "res-123",
            "permissions": '["read"]',
            "assigned_by": 1,
            "assigned_at": datetime.now(timezone.utc)
        }
    ]

    # Mock user is admin
    resources_query = MagicMock()
    resources_query.select.return_value.as_list.return_value = resources_list
    mock_db.return_value = resources_query

    client = app.test_client()
    response = await client.get("/api/v1/teams/1/resources")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 1


# ============================================================================
# Tests: DELETE /api/v1/teams/<int:team_id>/resources/<int:assignment_id>
# ============================================================================


@pytest.mark.asyncio
async def test_unassign_resource_success(teams_app):
    """Should unassign a resource from the team."""
    app, teams_mod = teams_app
    mock_db = teams_mod.get_db()

    team = MagicMock()
    team.id = 1
    mock_db.resource_teams.return_value = team

    assignment = MagicMock()
    assignment.id = 1
    assignment.team_id = 1
    mock_db.resource_assignments.return_value = assignment

    # Mock delete query
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
