"""Coverage tests for agents API - focus on error paths and edge cases."""

from __future__ import annotations

import hashlib
import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from quart import Quart, g


def _passthrough_decorator(*dargs, **dkwargs):
    """Stub for auth_required / require_scopes."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


@pytest.fixture()
def agents_app(monkeypatch):
    """Create a Quart test app with agents API and mocked dependencies."""
    # Bypass all auth decorators
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough_decorator)
    monkeypatch.setattr(mw_mod, "admin_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    # Preserve module in sys.modules before reload
    import sys
    if "app.api.agents" in sys.modules:
        monkeypatch.setitem(sys.modules, "app.api.agents", sys.modules["app.api.agents"])

    # Reload to apply decorator bypasses
    import app.api.agents as agents_mod
    agents_mod = importlib.reload(agents_mod)

    # Create mock DB with proper integer IDs for Python 3.14 compatibility
    mock_db = MagicMock()
    mock_db.enrollment_keys = MagicMock()
    mock_db.enrollment_keys.id = 1
    mock_db.enrollment_keys.key_hash = MagicMock()
    mock_db.enrollment_keys.is_used = MagicMock()
    mock_db.enrollment_keys.created_at = MagicMock()
    mock_db.enrollment_keys.created_by = MagicMock()
    mock_db.enrollment_keys.expires_at = MagicMock()
    mock_db.enrollment_keys.used_by_agent = MagicMock()

    mock_db.access_agents = MagicMock()
    mock_db.access_agents.id = 1
    mock_db.access_agents.agent_id = MagicMock()
    mock_db.access_agents.hostname = MagicMock()
    mock_db.access_agents.ip_address = MagicMock()
    mock_db.access_agents.status = MagicMock()
    mock_db.access_agents.capabilities = MagicMock()
    mock_db.access_agents.last_heartbeat = MagicMock()
    mock_db.access_agents.enrolled_at = MagicMock()
    mock_db.access_agents.created_at = MagicMock()
    mock_db.access_agents.enrollment_key_hash = MagicMock()
    mock_db.access_agents.enrollment_completed = MagicMock()
    mock_db.access_agents.updated_at = MagicMock()

    mock_db.ssh_ca_config = MagicMock()
    mock_db.ssh_ca_config.is_active = MagicMock()

    monkeypatch.setattr(agents_mod, "get_db", lambda: mock_db)

    # Create test app
    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["CLUSTER_ID"] = "test-cluster"
    app.config["JWT_SECRET_KEY"] = "test-secret-key"
    app.url_map.strict_slashes = False
    app.register_blueprint(agents_mod.agents_bp)

    @app.before_request
    async def _inject_auth():
        g.current_user = {
            "id": "test-user-id",
            "username": "test-admin",
            "_jwt_payload": {
                "sub": "test-admin",
                "tenant": "default",
                "scope": "gough.agents.read gough.agents.admin",
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return app, agents_mod, mock_db


@pytest.fixture()
def agents_client(agents_app):
    """Create test client from agents_app."""
    app, agents_mod, mock_db = agents_app
    return app.test_client(), agents_mod, mock_db


# ============================================================================
# create_enrollment_key - Error Path (Lines 85-88)
# ============================================================================


@pytest.mark.asyncio
async def test_create_enrollment_key_exception_rollback(agents_client):
    """Test exception handling with db.rollback() in create_enrollment_key."""
    client, agents_mod, mock_db = agents_client

    # Setup: make insert raise exception
    mock_db.enrollment_keys.insert.side_effect = ValueError("DB Insert failed")

    response = await client.post(
        "/api/v1/agents/enrollment-keys",
        json={"expires_in_hours": 24},
    )

    assert response.status_code == 500
    assert "error" in (await response.get_json())
    mock_db.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_create_enrollment_key_with_metadata(agents_client):
    """Test create_enrollment_key with metadata parameter."""
    client, agents_mod, mock_db = agents_client

    mock_db.enrollment_keys.insert.return_value = 1
    mock_db.commit = MagicMock()

    response = await client.post(
        "/api/v1/agents/enrollment-keys",
        json={"expires_in_hours": 12, "metadata": {"env": "prod"}},
    )

    assert response.status_code == 201
    data = await response.get_json()
    assert "enrollment_key" in data
    assert "key_id" in data


# ============================================================================
# list_enrollment_keys - Query Handling (Lines 106-125)
# ============================================================================


@pytest.mark.asyncio
async def test_list_enrollment_keys_with_include_used_true(agents_client):
    """Test list_enrollment_keys with include_used=true (line 104)."""
    client, agents_mod, mock_db = agents_client

    # Setup mock query chain for include_used=true
    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.as_list.return_value = [
        {
            "id": 1,
            "created_by": "user123",
            "expires_at": datetime(2025, 12, 1, 12, 0),
            "is_used": True,
            "used_by_agent": 42,
            "created_at": datetime(2025, 11, 1, 12, 0),
        }
    ]

    response = await client.get("/api/v1/agents/enrollment-keys?include_used=true")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 1
    assert data["enrollment_keys"][0]["is_used"] is True


@pytest.mark.asyncio
async def test_list_enrollment_keys_with_include_used_false(agents_client):
    """Test list_enrollment_keys filtering out used keys (line 107-108)."""
    client, agents_mod, mock_db = agents_client

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.as_list.return_value = [
        {
            "id": 1,
            "created_by": "user123",
            "expires_at": datetime(2025, 12, 1, 12, 0),
            "is_used": False,
            "used_by_agent": None,
            "created_at": datetime(2025, 11, 1, 12, 0),
        }
    ]

    response = await client.get("/api/v1/agents/enrollment-keys?include_used=false")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 1
    assert data["enrollment_keys"][0]["is_used"] is False


@pytest.mark.asyncio
async def test_list_enrollment_keys_empty(agents_client):
    """Test list_enrollment_keys with no keys."""
    client, agents_mod, mock_db = agents_client

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.as_list.return_value = []

    response = await client.get("/api/v1/agents/enrollment-keys")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 0
    assert data["enrollment_keys"] == []


@pytest.mark.asyncio
async def test_list_enrollment_keys_null_dates(agents_client):
    """Test list_enrollment_keys with null datetime fields (lines 117-122)."""
    client, agents_mod, mock_db = agents_client

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.as_list.return_value = [
        {
            "id": 1,
            "created_by": "user123",
            "expires_at": None,
            "is_used": False,
            "used_by_agent": None,
            "created_at": None,
        }
    ]

    response = await client.get("/api/v1/agents/enrollment-keys")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["enrollment_keys"][0]["expires_at"] is None
    assert data["enrollment_keys"][0]["created_at"] is None


# ============================================================================
# revoke_enrollment_key - Error Path (Lines 156-159)
# ============================================================================


@pytest.mark.asyncio
async def test_revoke_enrollment_key_exception_path(agents_client):
    """Test exception handling with db.rollback() in revoke_enrollment_key."""
    client, agents_mod, mock_db = agents_client

    # Setup: key exists but delete raises exception
    mock_key = MagicMock()
    mock_db.enrollment_keys.return_value = mock_key

    delete_query = MagicMock()
    mock_db.return_value = delete_query
    delete_query.delete.side_effect = RuntimeError("DB Delete failed")

    response = await client.delete("/api/v1/agents/enrollment-keys/1")

    assert response.status_code == 500
    mock_db.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_revoke_enrollment_key_not_found(agents_client):
    """Test revoke_enrollment_key when key doesn't exist."""
    client, agents_mod, mock_db = agents_client

    mock_db.enrollment_keys.return_value = None

    response = await client.delete("/api/v1/agents/enrollment-keys/999")

    assert response.status_code == 404
    data = await response.get_json()
    assert "not found" in data["error"]


# ============================================================================
# enroll_agent - Full Endpoint (Lines 190-281)
# ============================================================================


@pytest.mark.asyncio
async def test_enroll_agent_missing_enrollment_key(agents_client):
    """Test enroll_agent without X-Enrollment-Key header (line 190-191)."""
    client, agents_mod, mock_db = agents_client

    response = await client.post(
        "/api/v1/agents/enroll",
        json={"hostname": "agent1"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "Enrollment key required" in data["error"]


@pytest.mark.asyncio
async def test_enroll_agent_missing_body(agents_client):
    """Test enroll_agent with missing body (line 193-195)."""
    client, agents_mod, mock_db = agents_client

    response = await client.post(
        "/api/v1/agents/enroll",
        headers={"X-Enrollment-Key": "ENROLL-ABCD-EFGH-IJKL-MNOP"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "Request body required" in data["error"]


@pytest.mark.asyncio
async def test_enroll_agent_missing_hostname(agents_client):
    """Test enroll_agent without hostname (line 197-199)."""
    client, agents_mod, mock_db = agents_client

    response = await client.post(
        "/api/v1/agents/enroll",
        headers={"X-Enrollment-Key": "ENROLL-ABCD-EFGH-IJKL-MNOP"},
        json={"hostname": ""},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "Hostname required" in data["error"]


@pytest.mark.asyncio
async def test_enroll_agent_invalid_key(agents_client):
    """Test enroll_agent with invalid enrollment key (line 207-208)."""
    client, agents_mod, mock_db = agents_client

    # Setup: key not found
    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = None

    response = await client.post(
        "/api/v1/agents/enroll",
        headers={"X-Enrollment-Key": "ENROLL-INVALID-KEY-HERE"},
        json={"hostname": "agent1"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "Invalid enrollment key" in data["error"]


@pytest.mark.asyncio
async def test_enroll_agent_key_already_used(agents_client):
    """Test enroll_agent with already-used key (line 210-211)."""
    client, agents_mod, mock_db = agents_client

    mock_key = MagicMock()
    mock_key.is_used = True
    mock_key.expires_at = datetime.utcnow() + timedelta(hours=1)

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = mock_key

    response = await client.post(
        "/api/v1/agents/enroll",
        headers={"X-Enrollment-Key": "ENROLL-VALID-KEY-HERE"},
        json={"hostname": "agent1"},
    )

    assert response.status_code == 409
    data = await response.get_json()
    assert "already used" in data["error"]


@pytest.mark.asyncio
async def test_enroll_agent_key_expired(agents_client):
    """Test enroll_agent with expired key (line 213-214)."""
    client, agents_mod, mock_db = agents_client

    mock_key = MagicMock()
    mock_key.is_used = False
    mock_key.expires_at = datetime.utcnow() - timedelta(hours=1)

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = mock_key

    response = await client.post(
        "/api/v1/agents/enroll",
        headers={"X-Enrollment-Key": "ENROLL-EXPIRED-KEY-HERE"},
        json={"hostname": "agent1"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "expired" in data["error"]


@pytest.mark.asyncio
async def test_enroll_agent_success(agents_client):
    """Test enroll_agent successful flow (lines 216-276)."""
    client, agents_mod, mock_db = agents_client

    # Setup: valid key
    mock_key = MagicMock()
    mock_key.id = 1
    mock_key.is_used = False
    mock_key.expires_at = datetime.utcnow() + timedelta(hours=1)

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.side_effect = [mock_key, None]  # key, then ca_config

    mock_db.access_agents.insert.return_value = 42
    mock_db.commit = MagicMock()

    with patch("app.api.agents.get_audit_logger") as mock_audit:
        mock_audit.return_value = None

        response = await client.post(
            "/api/v1/agents/enroll",
            headers={"X-Enrollment-Key": "ENROLL-VALID-KEY-HERE"},
            json={"hostname": "agent1", "ip_address": "192.168.1.1", "capabilities": ["ssh"]},
        )

    assert response.status_code == 201
    data = await response.get_json()
    assert "agent_id" in data
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["message"] == "Agent enrolled successfully"


@pytest.mark.asyncio
async def test_enroll_agent_exception_path(agents_client):
    """Test enroll_agent exception handling (lines 278-281)."""
    client, agents_mod, mock_db = agents_client

    mock_key = MagicMock()
    mock_key.id = 1
    mock_key.is_used = False
    mock_key.expires_at = datetime.utcnow() + timedelta(hours=1)

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = mock_key

    # Make insert raise exception
    mock_db.access_agents.insert.side_effect = RuntimeError("DB Insert failed")

    response = await client.post(
        "/api/v1/agents/enroll",
        headers={"X-Enrollment-Key": "ENROLL-VALID-KEY-HERE"},
        json={"hostname": "agent1"},
    )

    assert response.status_code == 500
    mock_db.rollback.assert_called_once()


# ============================================================================
# refresh_agent_token - Full Endpoint (Lines 304-352)
# ============================================================================


@pytest.mark.asyncio
async def test_refresh_agent_token_no_header(agents_client):
    """Test refresh_agent_token without Authorization header (line 303-305)."""
    client, agents_mod, mock_db = agents_client

    response = await client.post("/api/v1/agents/refresh")

    assert response.status_code == 401
    data = await response.get_json()
    assert "Authorization header required" in data["error"]


@pytest.mark.asyncio
async def test_refresh_agent_token_bad_header(agents_client):
    """Test refresh_agent_token with malformed Authorization header."""
    client, agents_mod, mock_db = agents_client

    response = await client.post(
        "/api/v1/agents/refresh",
        headers={"Authorization": "BadFormat token123"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "Authorization header required" in data["error"]


@pytest.mark.asyncio
async def test_refresh_agent_token_wrong_type(agents_client):
    """Test refresh_agent_token with wrong token type (line 317-318)."""
    client, agents_mod, mock_db = agents_client

    # Create a token with wrong type
    payload = {
        "sub": "agent:test-agent-id",
        "type": "agent_access",  # Should be agent_refresh
        "exp": datetime.utcnow() + timedelta(hours=1),
    }
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    response = await client.post(
        "/api/v1/agents/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "Invalid token type" in data["error"]


@pytest.mark.asyncio
async def test_refresh_agent_token_missing_agent_id(agents_client):
    """Test refresh_agent_token with missing agent ID in token (line 320-322)."""
    client, agents_mod, mock_db = agents_client

    payload = {
        "sub": "agent:",  # Empty agent ID
        "type": "agent_refresh",
        "exp": datetime.utcnow() + timedelta(hours=1),
    }
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    response = await client.post(
        "/api/v1/agents/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "Invalid token" in data["error"]


@pytest.mark.asyncio
async def test_refresh_agent_token_agent_not_found(agents_client):
    """Test refresh_agent_token when agent doesn't exist (line 327-329)."""
    client, agents_mod, mock_db = agents_client

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = None

    payload = {
        "sub": "agent:nonexistent-agent",
        "type": "agent_refresh",
        "exp": datetime.utcnow() + timedelta(hours=1),
    }
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    response = await client.post(
        "/api/v1/agents/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "Agent not found" in data["error"]


@pytest.mark.asyncio
async def test_refresh_agent_token_agent_inactive(agents_client):
    """Test refresh_agent_token when agent is not active (line 331-332)."""
    client, agents_mod, mock_db = agents_client

    mock_agent = MagicMock()
    mock_agent.status = "suspended"

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = mock_agent

    payload = {
        "sub": "agent:suspended-agent",
        "type": "agent_refresh",
        "exp": datetime.utcnow() + timedelta(hours=1),
    }
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    response = await client.post(
        "/api/v1/agents/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "not active" in data["error"]


@pytest.mark.asyncio
async def test_refresh_agent_token_success(agents_client):
    """Test refresh_agent_token successful flow (line 334-347)."""
    client, agents_mod, mock_db = agents_client

    mock_agent = MagicMock()
    mock_agent.status = "active"
    mock_agent.capabilities = '["ssh", "sftp"]'

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = mock_agent

    payload = {
        "sub": "agent:test-agent",
        "type": "agent_refresh",
        "exp": datetime.utcnow() + timedelta(days=30),
    }
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    response = await client.post(
        "/api/v1/agents/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert "access_token" in data
    assert "refresh_token" in data


@pytest.mark.asyncio
async def test_refresh_agent_token_expired(agents_client):
    """Test refresh_agent_token with expired token (line 349-350)."""
    client, agents_mod, mock_db = agents_client

    payload = {
        "sub": "agent:test-agent",
        "type": "agent_refresh",
        "exp": datetime.utcnow() - timedelta(hours=1),
    }
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    response = await client.post(
        "/api/v1/agents/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "expired" in data["error"]


# ============================================================================
# agent_heartbeat - Error Paths (Lines 374-401)
# ============================================================================


@pytest.mark.asyncio
async def test_agent_heartbeat_no_auth(agents_client):
    """Test agent_heartbeat without auth token."""
    client, agents_mod, mock_db = agents_client

    response = await client.post(
        "/api/v1/agents/heartbeat",
        json={"status": "active"},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_agent_heartbeat_exception_path(agents_client):
    """Test agent_heartbeat exception handling (lines 398-401)."""
    client, agents_mod, mock_db = agents_client

    payload = {
        "sub": "agent:test-agent",
        "type": "agent_access",
        "exp": datetime.utcnow() + timedelta(hours=1),
    }
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    # Make update raise exception
    update_query = MagicMock()
    mock_db.return_value = update_query
    update_query.update.side_effect = RuntimeError("DB Update failed")

    response = await client.post(
        "/api/v1/agents/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "active"},
    )

    assert response.status_code == 500
    mock_db.rollback.assert_called_once()


# ============================================================================
# list_agents - Query/Filter Handling (Lines 425-448)
# ============================================================================


@pytest.mark.asyncio
async def test_list_agents_with_status_filter(agents_client):
    """Test list_agents with status filter (lines 423-427)."""
    client, agents_mod, mock_db = agents_client

    mock_agent = {
        "id": 1,
        "agent_id": "agent-uuid-1",
        "hostname": "agent1",
        "ip_address": "192.168.1.1",
        "status": "active",
        "capabilities": '["ssh"]',
        "enrollment_completed": True,
        "last_heartbeat": datetime.utcnow(),
        "enrolled_at": datetime.utcnow(),
    }

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.as_list.return_value = [mock_agent]

    response = await client.get("/api/v1/agents/?status=active")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 1
    assert data["agents"][0]["status"] == "active"


@pytest.mark.asyncio
async def test_list_agents_no_filter(agents_client):
    """Test list_agents without status filter."""
    client, agents_mod, mock_db = agents_client

    mock_agent = {
        "id": 1,
        "agent_id": "agent-uuid-1",
        "hostname": "agent1",
        "ip_address": "192.168.1.1",
        "status": "active",
        "capabilities": '["ssh"]',
        "enrollment_completed": False,
        "last_heartbeat": datetime.utcnow(),
        "enrolled_at": datetime.utcnow(),
    }

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.as_list.return_value = [mock_agent]

    response = await client.get("/api/v1/agents/")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["count"] == 1


@pytest.mark.asyncio
async def test_list_agents_null_dates(agents_client):
    """Test list_agents with null datetime fields (lines 442-445)."""
    client, agents_mod, mock_db = agents_client

    mock_agent = {
        "id": 1,
        "agent_id": "agent-uuid-1",
        "hostname": "agent1",
        "ip_address": "192.168.1.1",
        "status": "active",
        "capabilities": '["ssh"]',
        "enrollment_completed": True,
        "last_heartbeat": None,
        "enrolled_at": None,
    }

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.as_list.return_value = [mock_agent]

    response = await client.get("/api/v1/agents/")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["agents"][0]["last_heartbeat"] is None
    assert data["agents"][0]["enrolled_at"] is None


# ============================================================================
# get_agent - Full Endpoint (Lines 454-489)
# ============================================================================


@pytest.mark.asyncio
async def test_get_agent_found(agents_client):
    """Test get_agent when agent exists (lines 469-489)."""
    client, agents_mod, mock_db = agents_client

    mock_agent = MagicMock()
    mock_agent.id = 1
    mock_agent.agent_id = "agent-uuid-1"
    mock_agent.hostname = "agent1"
    mock_agent.ip_address = "192.168.1.1"
    mock_agent.status = "active"
    mock_agent.capabilities = '["ssh"]'
    mock_agent.enrollment_completed = True
    mock_agent.last_heartbeat = datetime.utcnow()
    mock_agent.enrolled_at = datetime.utcnow()
    mock_agent.created_at = datetime.utcnow()

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = mock_agent

    response = await client.get("/api/v1/agents/agent-uuid-1")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["agent"]["agent_id"] == "agent-uuid-1"
    assert data["agent"]["status"] == "active"


@pytest.mark.asyncio
async def test_get_agent_not_found(agents_client):
    """Test get_agent when agent doesn't exist."""
    client, agents_mod, mock_db = agents_client

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = None

    response = await client.get("/api/v1/agents/nonexistent-agent")

    assert response.status_code == 404
    data = await response.get_json()
    assert "not found" in data["error"]


@pytest.mark.asyncio
async def test_get_agent_null_dates(agents_client):
    """Test get_agent with null datetime fields (lines 482-487)."""
    client, agents_mod, mock_db = agents_client

    mock_agent = MagicMock()
    mock_agent.id = 1
    mock_agent.agent_id = "agent-uuid-1"
    mock_agent.hostname = "agent1"
    mock_agent.ip_address = "192.168.1.1"
    mock_agent.status = "active"
    mock_agent.capabilities = '["ssh"]'
    mock_agent.enrollment_completed = True
    mock_agent.last_heartbeat = None
    mock_agent.enrolled_at = None
    mock_agent.created_at = None

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = mock_agent

    response = await client.get("/api/v1/agents/agent-uuid-1")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["agent"]["last_heartbeat"] is None
    assert data["agent"]["enrolled_at"] is None
    assert data["agent"]["created_at"] is None


# ============================================================================
# suspend_agent - Error Paths (Lines 523-525)
# ============================================================================


@pytest.mark.asyncio
async def test_suspend_agent_not_found(agents_client):
    """Test suspend_agent when agent doesn't exist."""
    client, agents_mod, mock_db = agents_client

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = None

    response = await client.post("/api/v1/agents/nonexistent/suspend")

    assert response.status_code == 404
    data = await response.get_json()
    assert "not found" in data["error"]


@pytest.mark.asyncio
async def test_suspend_agent_exception_path(agents_client):
    """Test suspend_agent exception handling (lines 523-525)."""
    client, agents_mod, mock_db = agents_client

    mock_agent = MagicMock()
    mock_query = MagicMock()
    mock_db.return_value = mock_query

    # First call: agent exists
    # Second call (in try block): update raises exception
    if_query = MagicMock()
    if_query.update.side_effect = RuntimeError("DB Update failed")
    mock_query.select.return_value.first.return_value = mock_agent
    mock_db.return_value = if_query

    response = await client.post("/api/v1/agents/test-agent/suspend")

    assert response.status_code == 500
    mock_db.rollback.assert_called_once()


# ============================================================================
# resume_agent - Error Paths (Lines 559-561)
# ============================================================================


@pytest.mark.asyncio
async def test_resume_agent_not_found(agents_client):
    """Test resume_agent when agent doesn't exist."""
    client, agents_mod, mock_db = agents_client

    mock_query = MagicMock()
    mock_db.return_value = mock_query
    mock_query.select.return_value.first.return_value = None

    response = await client.post("/api/v1/agents/nonexistent/resume")

    assert response.status_code == 404
    data = await response.get_json()
    assert "not found" in data["error"]


@pytest.mark.asyncio
async def test_resume_agent_exception_path(agents_client):
    """Test resume_agent exception handling (lines 559-561)."""
    client, agents_mod, mock_db = agents_client

    mock_agent = MagicMock()
    mock_query = MagicMock()
    mock_db.return_value = mock_query

    # First call: agent exists
    # Second call (in try block): update raises exception
    if_query = MagicMock()
    if_query.update.side_effect = RuntimeError("DB Update failed")
    mock_query.select.return_value.first.return_value = mock_agent
    mock_db.return_value = if_query

    response = await client.post("/api/v1/agents/test-agent/resume")

    assert response.status_code == 500
    mock_db.rollback.assert_called_once()


# ============================================================================
# Token Creation Helper Functions (Lines 579-617)
# ============================================================================


@pytest.mark.asyncio
async def test_create_agent_access_token(agents_app):
    """Test _create_agent_access_token helper (lines 569-592)."""
    app, agents_mod, mock_db = agents_app

    async with app.app_context():
        token = agents_mod._create_agent_access_token(
            "test-agent-id",
            ["ssh", "sftp"],
        )

        assert token is not None
        assert isinstance(token, str)

        # Decode and verify
        payload = jwt.decode(token, "test-secret-key", algorithms=["HS256"])
        assert payload["sub"] == "agent:test-agent-id"
        assert payload["type"] == "agent_access"
        assert payload["capabilities"] == ["ssh", "sftp"]
        assert "exp" in payload
        assert "jti" in payload


@pytest.mark.asyncio
async def test_create_agent_refresh_token(agents_app):
    """Test _create_agent_refresh_token helper (lines 595-617)."""
    app, agents_mod, mock_db = agents_app

    async with app.app_context():
        token, expires_at = agents_mod._create_agent_refresh_token("test-agent-id")

        assert token is not None
        assert isinstance(token, str)
        assert isinstance(expires_at, datetime)

        # Decode and verify
        payload = jwt.decode(token, "test-secret-key", algorithms=["HS256"])
        assert payload["sub"] == "agent:test-agent-id"
        assert payload["type"] == "agent_refresh"
        assert "exp" in payload
        assert "jti" in payload


# ============================================================================
# Validate Agent Token Helper (Lines 620-646)
# ============================================================================


@pytest.mark.asyncio
async def test_validate_agent_token_no_header(agents_app):
    """Test _validate_agent_token with no Authorization header (line 627)."""
    app, agents_mod, mock_db = agents_app

    # Create a test request context
    async with app.app_context():
        async with app.test_request_context(
            "/test",
            method="GET",
            headers={},
        ):
            result = await agents_mod._validate_agent_token()
            assert result is None


@pytest.mark.asyncio
async def test_validate_agent_token_invalid_header(agents_app):
    """Test _validate_agent_token with malformed header."""
    app, agents_mod, mock_db = agents_app

    async with app.app_context():
        async with app.test_request_context(
            "/test",
            method="GET",
            headers={"Authorization": "InvalidFormat token"},
        ):
            result = await agents_mod._validate_agent_token()
            assert result is None


@pytest.mark.asyncio
async def test_validate_agent_token_wrong_type(agents_app):
    """Test _validate_agent_token with wrong token type (line 639)."""
    app, agents_mod, mock_db = agents_app

    payload = {
        "sub": "agent:test-agent",
        "type": "agent_refresh",  # Wrong type
        "exp": datetime.utcnow() + timedelta(hours=1),
    }
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    async with app.app_context():
        async with app.test_request_context(
            "/test",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        ):
            result = await agents_mod._validate_agent_token()
            assert result is None


@pytest.mark.asyncio
async def test_validate_agent_token_missing_agent_id(agents_app):
    """Test _validate_agent_token with missing agent ID (lines 642-643)."""
    app, agents_mod, mock_db = agents_app

    payload = {
        "sub": "agent:",  # Empty agent ID
        "type": "agent_access",
        "exp": datetime.utcnow() + timedelta(hours=1),
    }
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    async with app.app_context():
        async with app.test_request_context(
            "/test",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        ):
            result = await agents_mod._validate_agent_token()
            assert result is None


@pytest.mark.asyncio
async def test_validate_agent_token_invalid_token(agents_app):
    """Test _validate_agent_token with malformed token (line 645)."""
    app, agents_mod, mock_db = agents_app

    async with app.app_context():
        async with app.test_request_context(
            "/test",
            method="GET",
            headers={"Authorization": "Bearer malformed.token.here"},
        ):
            result = await agents_mod._validate_agent_token()
            assert result is None


@pytest.mark.asyncio
async def test_validate_agent_token_success(agents_app):
    """Test _validate_agent_token successful validation."""
    app, agents_mod, mock_db = agents_app

    payload = {
        "sub": "agent:test-agent-id",
        "type": "agent_access",
        "exp": datetime.utcnow() + timedelta(hours=1),
    }
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")

    async with app.app_context():
        async with app.test_request_context(
            "/test",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        ):
            result = await agents_mod._validate_agent_token()
            assert result == "test-agent-id"
