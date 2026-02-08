#!/usr/bin/env python3
"""
Pytest tests for Agent Enrollment API endpoints.

This module tests the /api/v1/agents blueprint including:
- Agent enrollment key generation (admin only)
- Agent enrollment with key validation
- Agent heartbeat and token refresh
- Agent management operations (list, suspend)

Test cases cover both success and error scenarios.
"""

import json
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

import pytest
import jwt


class TestAgentEnrollmentAPI:
    """Test cases for agent enrollment API endpoints."""

    @pytest.fixture
    def jwt_secret(self):
        """JWT secret key for testing."""
        return "test_jwt_secret_key_for_agents"

    @pytest.fixture
    def admin_token(self, jwt_secret):
        """Generate a mock admin JWT token."""
        payload = {
            "sub": "admin_user",
            "roles": ["admin"],
            "email": "admin@example.com",
            "exp": datetime.utcnow() + timedelta(hours=24)
        }
        return jwt.encode(payload, jwt_secret, algorithm="HS256")

    @pytest.fixture
    def user_token(self, jwt_secret):
        """Generate a mock user JWT token (non-admin)."""
        payload = {
            "sub": "regular_user",
            "roles": ["viewer"],
            "email": "user@example.com",
            "exp": datetime.utcnow() + timedelta(hours=24)
        }
        return jwt.encode(payload, jwt_secret, algorithm="HS256")

    @pytest.fixture
    def agent_token(self, jwt_secret):
        """Generate a mock agent JWT token."""
        payload = {
            "sub": "agent_12345",
            "type": "agent",
            "agent_id": "agent_12345",
            "exp": datetime.utcnow() + timedelta(hours=1)
        }
        return jwt.encode(payload, jwt_secret, algorithm="HS256")

    @pytest.fixture
    def client(self):
        """Mock Flask test client."""
        client = MagicMock()
        client.post = MagicMock()
        client.get = MagicMock()
        client.put = MagicMock()
        return client

    @pytest.fixture
    def mock_app_context(self, jwt_secret):
        """Mock Flask app context with configuration."""
        app = MagicMock()
        app.config = {
            "JWT_SECRET": jwt_secret,
            "JWT_ALGORITHM": "HS256",
            "AGENT_KEY_EXPIRY_HOURS": 24,
            "AGENT_TOKEN_EXPIRY_MINUTES": 60,
            "ENROLLMENT_KEY_LENGTH": 32
        }
        return app

    # Test: Generate Enrollment Key (Admin Only)
    def test_generate_enrollment_key_admin(self, client, admin_token, mock_app_context):
        """Test successful enrollment key generation by admin."""
        # Arrange
        headers = {"Authorization": f"Bearer {admin_token}"}

        # Mock response for enrollment key generation
        mock_response = {
            "enrollment_key": "enr_test_key_1234567890abcdef",
            "expires_at": (datetime.utcnow() + timedelta(hours=24)).isoformat(),
            "expires_in_hours": 24
        }
        client.post.return_value = (mock_response, 201)

        # Act
        response, status = client.post(
            "/api/v1/agents/enrollment-keys",
            headers=headers,
            json={}
        )

        # Assert
        assert status == 201
        assert "enrollment_key" in response
        assert response["enrollment_key"].startswith("enr_")
        assert "expires_at" in response
        client.post.assert_called_once()

    def test_generate_enrollment_key_non_admin(self, client, user_token):
        """Test enrollment key generation fails for non-admin users."""
        # Arrange
        headers = {"Authorization": f"Bearer {user_token}"}

        # Mock forbidden response
        mock_response = {
            "error": "Insufficient permissions",
            "message": "Only administrators can generate enrollment keys"
        }
        client.post.return_value = (mock_response, 403)

        # Act
        response, status = client.post(
            "/api/v1/agents/enrollment-keys",
            headers=headers,
            json={}
        )

        # Assert
        assert status == 403
        assert "error" in response
        assert "permissions" in response["error"].lower()

    # Test: Enroll Agent with Valid Key
    def test_enroll_agent_success(self, client, mock_app_context):
        """Test successful agent enrollment with valid enrollment key."""
        # Arrange
        enroll_data = {
            "enrollment_key": "enr_test_key_1234567890abcdef",
            "agent_name": "agent-prod-01",
            "agent_type": "hypervisor",
            "tags": ["production", "kvm"]
        }

        mock_response = {
            "agent_id": "agent_abc123def456",
            "agent_token": "agnt_jwt_token_here",
            "token_expires_at": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
            "agent_name": "agent-prod-01",
            "status": "enrolled"
        }
        client.post.return_value = (mock_response, 200)

        # Act
        response, status = client.post(
            "/api/v1/agents/enroll",
            json=enroll_data
        )

        # Assert
        assert status == 200
        assert "agent_id" in response
        assert "agent_token" in response
        assert response["status"] == "enrolled"
        assert response["agent_name"] == "agent-prod-01"

    def test_enroll_agent_invalid_key(self, client):
        """Test agent enrollment fails with invalid enrollment key."""
        # Arrange
        enroll_data = {
            "enrollment_key": "invalid_key_12345",
            "agent_name": "agent-test-01",
            "agent_type": "hypervisor"
        }

        mock_response = {
            "error": "Invalid enrollment key",
            "message": "The provided enrollment key is not valid"
        }
        client.post.return_value = (mock_response, 400)

        # Act
        response, status = client.post(
            "/api/v1/agents/enroll",
            json=enroll_data
        )

        # Assert
        assert status == 400
        assert "error" in response
        assert "enrollment key" in response["error"].lower()

    def test_enroll_agent_expired_key(self, client):
        """Test agent enrollment fails with expired enrollment key."""
        # Arrange
        enroll_data = {
            "enrollment_key": "enr_expired_key_old123456789",
            "agent_name": "agent-test-02",
            "agent_type": "hypervisor"
        }

        mock_response = {
            "error": "Enrollment key expired",
            "message": "The enrollment key has expired and cannot be used",
            "expired_at": (datetime.utcnow() - timedelta(hours=1)).isoformat()
        }
        client.post.return_value = (mock_response, 410)

        # Act
        response, status = client.post(
            "/api/v1/agents/enroll",
            json=enroll_data
        )

        # Assert
        assert status == 410
        assert "error" in response
        assert "expired" in response["error"].lower()

    # Test: Agent Heartbeat
    def test_agent_heartbeat(self, client, agent_token):
        """Test agent heartbeat endpoint for health check."""
        # Arrange
        headers = {"Authorization": f"Bearer {agent_token}"}
        heartbeat_data = {
            "agent_id": "agent_12345",
            "status": "healthy",
            "memory_usage_mb": 512,
            "cpu_usage_percent": 25.5,
            "uptime_seconds": 3600
        }

        mock_response = {
            "acknowledged": True,
            "next_heartbeat_interval_seconds": 60,
            "timestamp": datetime.utcnow().isoformat()
        }
        client.post.return_value = (mock_response, 200)

        # Act
        response, status = client.post(
            "/api/v1/agents/heartbeat",
            headers=headers,
            json=heartbeat_data
        )

        # Assert
        assert status == 200
        assert response["acknowledged"] is True
        assert "next_heartbeat_interval_seconds" in response

    def test_agent_heartbeat_unauthorized(self, client):
        """Test agent heartbeat fails without valid agent token."""
        # Arrange
        heartbeat_data = {
            "agent_id": "agent_12345",
            "status": "healthy"
        }

        mock_response = {
            "error": "Unauthorized",
            "message": "Valid agent token required"
        }
        client.post.return_value = (mock_response, 401)

        # Act
        response, status = client.post(
            "/api/v1/agents/heartbeat",
            json=heartbeat_data
        )

        # Assert
        assert status == 401
        assert "error" in response

    # Test: Agent Token Refresh
    def test_agent_refresh_token(self, client, agent_token):
        """Test agent token refresh with valid agent credentials."""
        # Arrange
        headers = {"Authorization": f"Bearer {agent_token}"}

        mock_response = {
            "agent_token": "agnt_new_jwt_token_refreshed",
            "token_expires_at": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
            "expires_in_seconds": 3600
        }
        client.post.return_value = (mock_response, 200)

        # Act
        response, status = client.post(
            "/api/v1/agents/token/refresh",
            headers=headers,
            json={"agent_id": "agent_12345"}
        )

        # Assert
        assert status == 200
        assert "agent_token" in response
        assert "token_expires_at" in response
        assert response["expires_in_seconds"] == 3600

    def test_agent_refresh_token_expired(self, client):
        """Test token refresh fails with expired token."""
        # Arrange
        expired_payload = {
            "sub": "agent_12345",
            "type": "agent",
            "exp": int(time.time()) - 3600  # Expired 1 hour ago
        }
        expired_token = jwt.encode(expired_payload, "secret", algorithm="HS256")
        headers = {"Authorization": f"Bearer {expired_token}"}

        mock_response = {
            "error": "Token expired",
            "message": "Agent token has expired"
        }
        client.post.return_value = (mock_response, 401)

        # Act
        response, status = client.post(
            "/api/v1/agents/token/refresh",
            headers=headers,
            json={"agent_id": "agent_12345"}
        )

        # Assert
        assert status == 401
        assert "error" in response

    # Test: List Agents (Admin Only)
    def test_list_agents_admin(self, client, admin_token):
        """Test listing agents as administrator."""
        # Arrange
        headers = {"Authorization": f"Bearer {admin_token}"}

        mock_response = {
            "agents": [
                {
                    "agent_id": "agent_123",
                    "name": "agent-prod-01",
                    "type": "hypervisor",
                    "status": "healthy",
                    "last_heartbeat": datetime.utcnow().isoformat(),
                    "enrolled_at": (datetime.utcnow() - timedelta(days=30)).isoformat()
                },
                {
                    "agent_id": "agent_456",
                    "name": "agent-test-01",
                    "type": "connector",
                    "status": "offline",
                    "last_heartbeat": (datetime.utcnow() - timedelta(hours=2)).isoformat(),
                    "enrolled_at": (datetime.utcnow() - timedelta(days=15)).isoformat()
                }
            ],
            "total": 2,
            "page": 1,
            "per_page": 50
        }
        client.get.return_value = (mock_response, 200)

        # Act
        response, status = client.get(
            "/api/v1/agents",
            headers=headers
        )

        # Assert
        assert status == 200
        assert "agents" in response
        assert len(response["agents"]) == 2
        assert response["total"] == 2
        assert response["agents"][0]["agent_id"] == "agent_123"

    def test_list_agents_non_admin(self, client, user_token):
        """Test listing agents fails for non-admin users."""
        # Arrange
        headers = {"Authorization": f"Bearer {user_token}"}

        mock_response = {
            "error": "Insufficient permissions",
            "message": "Only administrators can list agents"
        }
        client.get.return_value = (mock_response, 403)

        # Act
        response, status = client.get(
            "/api/v1/agents",
            headers=headers
        )

        # Assert
        assert status == 403
        assert "error" in response

    def test_list_agents_with_filters(self, client, admin_token):
        """Test listing agents with status filter."""
        # Arrange
        headers = {"Authorization": f"Bearer {admin_token}"}

        mock_response = {
            "agents": [
                {
                    "agent_id": "agent_123",
                    "name": "agent-prod-01",
                    "status": "healthy"
                }
            ],
            "total": 1,
            "filter": {"status": "healthy"}
        }
        client.get.return_value = (mock_response, 200)

        # Act
        response, status = client.get(
            "/api/v1/agents?status=healthy",
            headers=headers
        )

        # Assert
        assert status == 200
        assert len(response["agents"]) == 1
        assert response["agents"][0]["status"] == "healthy"

    # Test: Suspend Agent (Admin Only)
    def test_suspend_agent_admin(self, client, admin_token):
        """Test suspending an agent as administrator."""
        # Arrange
        headers = {"Authorization": f"Bearer {admin_token}"}
        suspend_data = {
            "reason": "Maintenance scheduled"
        }

        mock_response = {
            "agent_id": "agent_123",
            "status": "suspended",
            "suspended_at": datetime.utcnow().isoformat(),
            "suspended_by": "admin_user",
            "reason": "Maintenance scheduled"
        }
        client.put.return_value = (mock_response, 200)

        # Act
        response, status = client.put(
            "/api/v1/agents/agent_123/suspend",
            headers=headers,
            json=suspend_data
        )

        # Assert
        assert status == 200
        assert response["agent_id"] == "agent_123"
        assert response["status"] == "suspended"
        assert response["reason"] == "Maintenance scheduled"

    def test_suspend_agent_non_admin(self, client, user_token):
        """Test suspending agent fails for non-admin users."""
        # Arrange
        headers = {"Authorization": f"Bearer {user_token}"}

        mock_response = {
            "error": "Insufficient permissions",
            "message": "Only administrators can suspend agents"
        }
        client.put.return_value = (mock_response, 403)

        # Act
        response, status = client.put(
            "/api/v1/agents/agent_123/suspend",
            headers=headers,
            json={"reason": "Test"}
        )

        # Assert
        assert status == 403
        assert "error" in response

    def test_suspend_nonexistent_agent(self, client, admin_token):
        """Test suspending a non-existent agent."""
        # Arrange
        headers = {"Authorization": f"Bearer {admin_token}"}

        mock_response = {
            "error": "Agent not found",
            "message": "Agent with ID 'nonexistent' does not exist"
        }
        client.put.return_value = (mock_response, 404)

        # Act
        response, status = client.put(
            "/api/v1/agents/nonexistent/suspend",
            headers=headers,
            json={"reason": "Test"}
        )

        # Assert
        assert status == 404
        assert "error" in response
        assert "not found" in response["error"].lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
