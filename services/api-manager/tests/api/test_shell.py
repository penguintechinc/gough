"""Tests for Shell Access API Endpoints (app/api/shell.py)."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from quart import Quart, g


def _passthrough_decorator(*dargs, **dkwargs):
    """Stub for auth_required / require_scopes."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


@pytest.fixture()
def mock_db():
    """Create a mock database object."""
    db = MagicMock()
    return db


@pytest.fixture()
def shell_app(mock_db, monkeypatch):
    """Create a Quart app with shell blueprint and auth stubbed."""
    # Patch get_db before importing shell module
    import app.models as models_mod

    monkeypatch.setattr(models_mod, "get_db", lambda: mock_db)

    # Stub auth decorators
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod
    import app.audit as audit_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)
    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: None)

    # Reload shell module with patched decorators
    import app.api.shell as shell_mod

    shell_mod = importlib.reload(shell_mod)

    # Patch get_db and helper functions in shell module
    monkeypatch.setattr(shell_mod, "get_db", lambda: mock_db)
    monkeypatch.setattr(
        shell_mod,
        "get_current_user",
        lambda: {"id": 1, "email": "test@example.com", "role": "admin"},
    )
    monkeypatch.setattr(shell_mod, "user_has_role", lambda role: True)
    monkeypatch.setattr(shell_mod, "get_audit_logger", lambda: None)

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["JWT_SECRET_KEY"] = "test-secret-key"
    app.url_map.strict_slashes = False

    app.register_blueprint(shell_mod.shell_bp)

    @app.before_request
    async def _inject_auth():
        """Stub authentication."""
        g.current_user = {
            "id": 1,
            "email": "test@example.com",
            "role": "admin",
            "is_active": True,
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return app, shell_mod, mock_db


class TestCreateSession:
    """Tests for POST /api/v1/shell/sessions."""

    @pytest.mark.asyncio
    async def test_create_session_success(self, shell_app):
        """Test successful shell session creation."""
        app, shell_mod, mock_db = shell_app

        # Patch check_shell_access to allow access
        with patch.object(
            shell_mod, "check_shell_access", return_value=(True, None)
        ):
            # Mock access_agents query
            fake_agent = MagicMock()
            fake_agent.id = 1
            fake_agent.agent_id = "agent-001"

            # Setup side effect for db() calls
            def _db_call(*args, **kwargs):
                q = MagicMock()
                q.select.return_value = MagicMock(first=lambda: fake_agent)
                return q

            mock_db.side_effect = _db_call
            mock_db.shell_sessions.insert.return_value = 1
            mock_db.commit.return_value = None

            client = app.test_client()
            response = await client.post(
                "/api/v1/shell/sessions",
                json={
                    "resource_type": "vm",
                    "resource_id": "vm-123",
                    "session_type": "ssh",
                },
            )

            assert response.status_code == 201
            data = await response.get_json()
            assert data["message"] == "Shell session created successfully"
            assert data["session_id"]
            assert data["session_type"] == "ssh"
            assert data["agent_id"] == "agent-001"
            assert data["websocket_url"]

    @pytest.mark.asyncio
    async def test_create_session_missing_resource_type(self, shell_app):
        """Test session creation fails without resource_type."""
        app, _, _ = shell_app
        client = app.test_client()

        response = await client.post(
            "/api/v1/shell/sessions",
            json={"resource_id": "vm-123", "session_type": "ssh"},
        )

        assert response.status_code == 400
        data = await response.get_json()
        assert "resource_type required" in data["error"]

    @pytest.mark.asyncio
    async def test_create_session_missing_resource_id(self, shell_app):
        """Test session creation fails without resource_id."""
        app, _, _ = shell_app
        client = app.test_client()

        response = await client.post(
            "/api/v1/shell/sessions",
            json={"resource_type": "vm", "session_type": "ssh"},
        )

        assert response.status_code == 400
        data = await response.get_json()
        assert "resource_id required" in data["error"]

    @pytest.mark.asyncio
    async def test_create_session_invalid_session_type(self, shell_app):
        """Test session creation fails with invalid session_type."""
        app, _, _ = shell_app
        client = app.test_client()

        response = await client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "vm-123",
                "session_type": "invalid",
            },
        )

        assert response.status_code == 400
        data = await response.get_json()
        assert "Invalid session_type" in data["error"]

    @pytest.mark.asyncio
    async def test_create_session_access_denied(self, shell_app):
        """Test session creation fails when user lacks access."""
        app, shell_mod, mock_db = shell_app

        # Setup mock to deny access
        fake_user = MagicMock()
        fake_user.__bool__ = lambda s: True
        mock_db.auth_user.return_value = fake_user

        fake_role = MagicMock()
        fake_role.name = "viewer"
        mock_db.return_value.select.return_value = [fake_role]

        # Patch check_shell_access to return False
        with patch.object(
            shell_mod, "check_shell_access", return_value=(False, "Access denied")
        ):
            client = app.test_client()
            response = await client.post(
                "/api/v1/shell/sessions",
                json={
                    "resource_type": "vm",
                    "resource_id": "vm-123",
                    "session_type": "ssh",
                },
            )

            assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_create_session_no_active_agent(self, shell_app):
        """Test session creation fails when no active agent available."""
        app, shell_mod, mock_db = shell_app

        # Setup mock to allow access
        fake_user = MagicMock()
        fake_user.__bool__ = lambda s: True
        mock_db.auth_user.return_value = fake_user

        fake_role = MagicMock()
        fake_role.name = "admin"
        mock_db.return_value.select.return_value = [fake_role]

        # Patch check_shell_access to allow access
        with patch.object(
            shell_mod, "check_shell_access", return_value=(True, None)
        ):
            # Setup mock db to return no agent
            def _db_call(*args, **kwargs):
                q = MagicMock()
                q.select.return_value = MagicMock(first=lambda: None)
                return q

            mock_db.side_effect = _db_call

            client = app.test_client()
            response = await client.post(
                "/api/v1/shell/sessions",
                json={
                    "resource_type": "vm",
                    "resource_id": "vm-123",
                    "session_type": "ssh",
                },
            )

            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_create_session_empty_body(self, shell_app):
        """Test session creation fails with empty request body."""
        app, _, _ = shell_app
        client = app.test_client()

        response = await client.post(
            "/api/v1/shell/sessions",
            json=None,
        )

        assert response.status_code == 400


class TestListSessions:
    """Tests for GET /api/v1/shell/sessions."""

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self, shell_app):
        """Test listing sessions when none exist."""
        app, _, mock_db = shell_app

        # Mock empty sessions
        def _db_call(*args, **kwargs):
            q = MagicMock()
            q.select.return_value = MagicMock(as_list=lambda: [])
            return q

        mock_db.side_effect = _db_call

        client = app.test_client()
        response = await client.get("/api/v1/shell/sessions")

        assert response.status_code == 200
        data = await response.get_json()
        assert data["count"] == 0
        assert data["sessions"] == []

    @pytest.mark.asyncio
    async def test_list_sessions_with_sessions(self, shell_app):
        """Test listing active sessions.

        # regression: gh-22
        list_sessions' SELECT now runs via run_db() instead of blocking
        the request coroutine inline -- proves it still returns seeded
        sessions correctly through that thread hop.
        """
        app, _, mock_db = shell_app

        # Mock sessions
        now = datetime.utcnow()
        sessions = [
            {
                "session_id": "session-1",
                "resource_type": "vm",
                "resource_id": "vm-123",
                "session_type": "ssh",
                "started_at": now,
                "client_ip": "192.168.1.100",
            }
        ]

        def _db_call(*args, **kwargs):
            q = MagicMock()
            q.select.return_value = MagicMock(as_list=lambda: sessions)
            return q

        mock_db.side_effect = _db_call

        client = app.test_client()
        response = await client.get("/api/v1/shell/sessions")

        assert response.status_code == 200
        data = await response.get_json()
        assert data["count"] == 1
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["session_id"] == "session-1"
        assert data["sessions"][0]["session_type"] == "ssh"


class TestTerminateSession:
    """Tests for DELETE /api/v1/shell/sessions/<session_id>."""

    @pytest.mark.asyncio
    async def test_terminate_session_success(self, shell_app):
        """Test successful session termination."""
        app, _, mock_db = shell_app

        # Mock session record
        fake_session = MagicMock()
        fake_session.user_id = 1
        fake_session.ended_at = None
        fake_session.started_at = datetime.utcnow() - timedelta(minutes=5)
        fake_session.resource_type = "vm"
        fake_session.resource_id = "vm-123"

        def _db_call(*args, **kwargs):
            q = MagicMock()
            q.select.return_value = MagicMock(first=lambda: fake_session)
            q.update.return_value = None
            return q

        mock_db.side_effect = _db_call
        mock_db.commit.return_value = None

        client = app.test_client()
        response = await client.delete("/api/v1/shell/sessions/session-1")

        assert response.status_code == 200
        data = await response.get_json()
        assert data["message"] == "Session terminated successfully"
        assert data["session_id"] == "session-1"
        assert data["duration_seconds"] == 300  # 5 minutes

    @pytest.mark.asyncio
    async def test_terminate_session_not_found(self, shell_app):
        """Test termination fails for non-existent session."""
        app, _, mock_db = shell_app

        def _db_call(*args, **kwargs):
            q = MagicMock()
            q.select.return_value = MagicMock(first=lambda: None)
            return q

        mock_db.side_effect = _db_call

        client = app.test_client()
        response = await client.delete("/api/v1/shell/sessions/nonexistent")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_terminate_session_not_owner(self, shell_app, monkeypatch):
        """Test termination fails if not owner or admin."""
        app, shell_mod, mock_db = shell_app

        # Mock session owned by different user
        fake_session = MagicMock()
        fake_session.user_id = 999  # Different user
        fake_session.ended_at = None

        def _db_call(*args, **kwargs):
            q = MagicMock()
            q.select.return_value = MagicMock(first=lambda: fake_session)
            return q

        mock_db.side_effect = _db_call

        # Patch user_has_role to return False (not admin)
        monkeypatch.setattr(shell_mod, "user_has_role", lambda role: False)

        client = app.test_client()
        response = await client.delete("/api/v1/shell/sessions/session-1")

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_terminate_session_already_terminated(self, shell_app):
        """Test termination fails if session already ended."""
        app, _, mock_db = shell_app

        # Mock already-terminated session
        fake_session = MagicMock()
        fake_session.user_id = 1
        fake_session.ended_at = datetime.utcnow() - timedelta(hours=1)

        def _db_call(*args, **kwargs):
            q = MagicMock()
            q.select.return_value = MagicMock(first=lambda: fake_session)
            return q

        mock_db.side_effect = _db_call

        client = app.test_client()
        response = await client.delete("/api/v1/shell/sessions/session-1")

        assert response.status_code == 400


class TestCheckShellAccess:
    """Tests for check_shell_access helper function."""

    def test_check_shell_access_admin_user(self, shell_app):
        """Test admin user always has shell access."""
        app, shell_mod, mock_db = shell_app

        # Setup admin user
        fake_user = MagicMock()
        fake_user.__bool__ = lambda s: True
        mock_db.auth_user.return_value = fake_user

        fake_role = MagicMock()
        fake_role.name = "admin"
        mock_db.return_value.select.return_value = [fake_role]

        has_access, error_msg = shell_mod.check_shell_access(1, "vm", "vm-123")

        assert has_access is True
        assert error_msg is None

    def test_check_shell_access_user_not_found(self, shell_app):
        """Test returns False when user not found."""
        app, shell_mod, mock_db = shell_app

        mock_db.auth_user.return_value = None

        has_access, error_msg = shell_mod.check_shell_access(999, "vm", "vm-123")

        assert has_access is False
        assert "User not found" in error_msg

    def test_check_shell_access_non_admin_no_team(self, shell_app):
        """Test non-admin user without team has no access."""
        app, shell_mod, mock_db = shell_app

        # Setup non-admin user
        fake_user = MagicMock()
        fake_user.__bool__ = lambda s: True
        mock_db.auth_user.return_value = fake_user

        fake_role = MagicMock()
        fake_role.name = "viewer"
        mock_db.return_value.select.return_value = [fake_role]

        # Mock no team memberships
        def _db_call(*args, **kwargs):
            q = MagicMock()
            q.select.return_value = []
            return q

        mock_db.side_effect = _db_call

        has_access, error_msg = shell_mod.check_shell_access(1, "vm", "vm-123")

        assert has_access is False
        assert "not member of any team" in error_msg

    def test_check_shell_access_team_with_shell_permission_list(self, shell_app):
        """Test user with team having shell permission (list format)."""
        app, shell_mod, mock_db = shell_app

        # Setup user with team
        fake_user = MagicMock()
        fake_user.__bool__ = lambda s: True
        mock_db.auth_user.return_value = fake_user

        fake_role = MagicMock()
        fake_role.name = "viewer"
        mock_db.return_value.select.return_value = [fake_role]

        # Mock team membership
        fake_team_mem = MagicMock()
        fake_team_mem.team_id = 1

        # Mock resource assignment with shell permission
        fake_assignment = MagicMock()
        fake_assignment.permissions = json.dumps(["shell", "debug"])

        call_count = [0]

        def _db_call(*args, **kwargs):
            q = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:  # auth_user_roles query
                q.select.return_value = [fake_role]
            elif call_count[0] == 2:  # team_members query
                q.select.return_value = [fake_team_mem]
            elif call_count[0] == 3:  # resource_assignments query
                q.select.return_value = [fake_assignment]
            return q

        mock_db.side_effect = _db_call

        has_access, error_msg = shell_mod.check_shell_access(1, "vm", "vm-123")

        assert has_access is True
        assert error_msg is None

    def test_check_shell_access_team_with_shell_permission_dict(self, shell_app):
        """Test user with team having shell permission (dict format)."""
        app, shell_mod, mock_db = shell_app

        # Setup user with team
        fake_user = MagicMock()
        fake_user.__bool__ = lambda s: True
        mock_db.auth_user.return_value = fake_user

        fake_role = MagicMock()
        fake_role.name = "viewer"
        mock_db.return_value.select.return_value = [fake_role]

        # Mock team membership
        fake_team_mem = MagicMock()
        fake_team_mem.team_id = 1

        # Mock resource assignment with shell permission (dict format)
        fake_assignment = MagicMock()
        fake_assignment.permissions = json.dumps({"shell": True, "debug": False})

        call_count = [0]

        def _db_call(*args, **kwargs):
            q = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                q.select.return_value = [fake_role]
            elif call_count[0] == 2:
                q.select.return_value = [fake_team_mem]
            elif call_count[0] == 3:
                q.select.return_value = [fake_assignment]
            return q

        mock_db.side_effect = _db_call

        has_access, error_msg = shell_mod.check_shell_access(1, "vm", "vm-123")

        assert has_access is True
        assert error_msg is None
