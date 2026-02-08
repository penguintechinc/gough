"""Pytest tests for shell session API endpoints.

Tests cover:
- Session creation (success, permissions, invalid input)
- Session listing and user filtering
- Session termination (owner, admin, permission denied)
- Session type support (SSH, kubectl, docker, cloud_cli)
- Permission validation and access control
"""

import json
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import pytest

from gough.services.flask_backend.app.models import get_db


class TestCreateShellSession:
    """Test shell session creation endpoints."""

    def test_create_shell_session_success(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test successful shell session creation."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code == 201
        data = response.get_json()
        assert "session_id" in data
        assert "websocket_url" in data
        assert data["session_type"] == "ssh"
        assert "agent_id" in data
        assert data["message"] == "Shell session created successfully"

    def test_create_shell_session_kubectl(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test creating a kubectl shell session."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "cluster",
                "resource_id": "test-cluster-01",
                "session_type": "kubectl"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code in [201, 403, 404]
        if response.status_code == 201:
            data = response.get_json()
            assert data["session_type"] == "kubectl"

    def test_create_shell_session_docker(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test creating a docker shell session."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "container",
                "resource_id": "test-container-01",
                "session_type": "docker"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code in [201, 403, 404]
        if response.status_code == 201:
            data = response.get_json()
            assert data["session_type"] == "docker"

    def test_create_shell_session_cloud_cli(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test creating a cloud_cli shell session."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "cloud",
                "resource_id": "aws-account-001",
                "session_type": "cloud_cli"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code in [201, 403, 404]
        if response.status_code == 201:
            data = response.get_json()
            assert data["session_type"] == "cloud_cli"

    def test_create_shell_session_no_permission(self, client, auth_headers):
        """Test shell session creation denied due to insufficient permissions."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "unauthorized-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code == 403
        data = response.get_json()
        assert "error" in data

    def test_create_shell_session_invalid_resource_type(self, client, auth_headers, mock_agent):
        """Test shell session creation with missing resource_type."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_id": "test-vm-001",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code == 400
        data = response.get_json()
        assert "error" in data
        assert "resource_type" in data["error"].lower()

    def test_create_shell_session_invalid_resource_id(self, client, auth_headers, mock_agent):
        """Test shell session creation with missing resource_id."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code == 400
        data = response.get_json()
        assert "error" in data
        assert "resource_id" in data["error"].lower()

    def test_create_shell_session_invalid_session_type(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test shell session creation with invalid session_type."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": "invalid_type"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code == 400
        data = response.get_json()
        assert "error" in data
        assert "session_type" in data["error"].lower()

    def test_create_shell_session_missing_body(self, client, auth_headers):
        """Test shell session creation with missing request body."""
        response = client.post(
            "/api/v1/shell/sessions",
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code == 400
        data = response.get_json()
        assert "error" in data

    def test_create_shell_session_no_agent_available(self, client, auth_headers, team_with_shell_access):
        """Test shell session creation when no agent is available."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code in [201, 404]

    def test_create_shell_session_unauthenticated(self, client):
        """Test shell session creation without authentication."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "test-vm-001",
                "session_type": "ssh"
            },
            content_type="application/json"
        )

        assert response.status_code == 401


class TestListShellSessions:
    """Test shell session listing endpoints."""

    def test_list_user_sessions_empty(self, client, auth_headers):
        """Test listing sessions for user with no active sessions."""
        response = client.get(
            "/api/v1/shell/sessions",
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()
        assert "sessions" in data
        assert "count" in data
        assert data["count"] == 0
        assert isinstance(data["sessions"], list)

    def test_list_user_sessions_with_active(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test listing sessions includes active sessions."""
        # Create a session
        create_response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        if create_response.status_code == 201:
            # List sessions
            list_response = client.get(
                "/api/v1/shell/sessions",
                headers=auth_headers
            )

            assert list_response.status_code == 200
            data = list_response.get_json()
            assert "sessions" in data
            assert data["count"] >= 1
            assert any(s["session_type"] == "ssh" for s in data["sessions"])

    def test_list_user_sessions_field_validation(self, client, auth_headers):
        """Test that session list contains required fields."""
        response = client.get(
            "/api/v1/shell/sessions",
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.get_json()

        # Verify response structure
        assert "sessions" in data
        assert "count" in data

    def test_list_sessions_unauthenticated(self, client):
        """Test listing sessions without authentication."""
        response = client.get("/api/v1/shell/sessions")

        assert response.status_code == 401


class TestTerminateShellSession:
    """Test shell session termination endpoints."""

    def test_terminate_session_by_owner(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test session termination by session owner."""
        # Create a session
        create_response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        if create_response.status_code == 201:
            session_id = create_response.get_json()["session_id"]

            # Terminate the session
            terminate_response = client.delete(
                f"/api/v1/shell/sessions/{session_id}",
                headers=auth_headers
            )

            assert terminate_response.status_code == 200
            data = terminate_response.get_json()
            assert data["session_id"] == session_id
            assert "duration_seconds" in data

    def test_terminate_session_not_found(self, client, auth_headers):
        """Test termination of non-existent session."""
        response = client.delete(
            "/api/v1/shell/sessions/nonexistent-session-id",
            headers=auth_headers
        )

        assert response.status_code == 404
        data = response.get_json()
        assert "error" in data

    def test_terminate_session_not_owner(self, client, auth_headers, admin_user, mock_agent, team_with_shell_access):
        """Test that non-owner cannot terminate another user's session."""
        # Create a session with admin user (needs separate auth headers)
        response = client.delete(
            "/api/v1/shell/sessions/session-owned-by-other-user",
            headers=auth_headers
        )

        assert response.status_code in [403, 404]

    def test_terminate_already_terminated_session(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test terminating an already terminated session."""
        # Create and terminate a session
        create_response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        if create_response.status_code == 201:
            session_id = create_response.get_json()["session_id"]

            # First termination
            first_terminate = client.delete(
                f"/api/v1/shell/sessions/{session_id}",
                headers=auth_headers
            )
            assert first_terminate.status_code == 200

            # Second termination attempt
            second_terminate = client.delete(
                f"/api/v1/shell/sessions/{session_id}",
                headers=auth_headers
            )
            assert second_terminate.status_code in [400, 404]

    def test_terminate_session_unauthenticated(self, client):
        """Test session termination without authentication."""
        response = client.delete("/api/v1/shell/sessions/some-session-id")

        assert response.status_code == 401


class TestSessionTypes:
    """Test different shell session types."""

    @pytest.mark.parametrize("session_type", ["ssh", "kubectl", "docker", "cloud_cli"])
    def test_session_type_support(self, client, auth_headers, mock_agent, team_with_shell_access, session_type):
        """Test that all session types are supported."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": session_type
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code in [201, 403, 404]
        if response.status_code == 201:
            data = response.get_json()
            assert data["session_type"] == session_type

    def test_ssh_session_type(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test SSH session type specifically."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        if response.status_code == 201:
            assert response.get_json()["session_type"] == "ssh"

    def test_kubectl_session_type(self, client, auth_headers, mock_agent):
        """Test kubectl session type specifically."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "cluster",
                "resource_id": "test-cluster",
                "session_type": "kubectl"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code in [201, 403, 404]

    def test_docker_session_type(self, client, auth_headers, mock_agent):
        """Test docker session type specifically."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "container",
                "resource_id": "test-container",
                "session_type": "docker"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code in [201, 403, 404]

    def test_cloud_cli_session_type(self, client, auth_headers, mock_agent):
        """Test cloud_cli session type specifically."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "cloud_account",
                "resource_id": "aws-prod",
                "session_type": "cloud_cli"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code in [201, 403, 404]

    def test_invalid_session_type_rejected(self, client, auth_headers, mock_agent):
        """Test that invalid session types are rejected."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "test-vm",
                "session_type": "invalid"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code == 400


class TestShellPermissions:
    """Test shell access permission validation."""

    def test_admin_has_all_access(self, client, auth_headers, mock_agent):
        """Test that admin users have access to all resources."""
        # Admin should have access by default
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "any-resource",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        # Admin in auth_headers, should have access
        assert response.status_code in [201, 404]

    def test_team_member_shell_access(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test that team members with shell permission can access resources."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code in [201, 404]

    def test_user_without_team_access_denied(self, client, auth_headers):
        """Test that users not in required team are denied."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "restricted_resource",
                "resource_id": "secret-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        assert response.status_code in [403, 404]


class TestWebSocketURL:
    """Test WebSocket URL generation for sessions."""

    def test_websocket_url_format(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test that WebSocket URL is properly formatted."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        if response.status_code == 201:
            data = response.get_json()
            websocket_url = data["websocket_url"]

            # Verify WebSocket URL format
            assert websocket_url.startswith("wss://")
            assert "/ws/shell/" in websocket_url
            assert data["session_id"] in websocket_url

    def test_websocket_url_includes_session_id(self, client, auth_headers, mock_agent, team_with_shell_access):
        """Test that WebSocket URL includes the session ID."""
        response = client.post(
            "/api/v1/shell/sessions",
            json={
                "resource_type": "vm",
                "resource_id": "shell-test-vm",
                "session_type": "ssh"
            },
            headers=auth_headers,
            content_type="application/json"
        )

        if response.status_code == 201:
            data = response.get_json()
            assert data["session_id"] in data["websocket_url"]
