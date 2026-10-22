"""SSH Certificate Authority API endpoint tests.

Comprehensive coverage for app/api/ssh_ca.py endpoints:
- POST /api/v1/ssh-ca/initialize (admin-only)
- GET /api/v1/ssh-ca/public-key (any auth user)
- POST /api/v1/ssh-ca/sign (any auth user)
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
import importlib

import pytest


def _passthrough_decorator(*dargs, **dkwargs):
    """Support both styles: @auth_required and @require_scopes("...")."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    def _wrap(fn):
        return fn
    return _wrap


def _make_db_mock(first_result=None):
    """Create a properly configured db mock with proper integer comparisons."""
    db = MagicMock()
    # Configure ssh_ca_config to have id as integer, not MagicMock
    db.ssh_ca_config.id = 1
    # penguin-dal style: db(...).select().first()
    db.return_value.select.return_value.first.return_value = first_result
    # Also support ssh_ca_configs style
    db.ssh_ca_configs.select.return_value.first.return_value = first_result
    return db


@pytest.fixture
def ssh_ca_app(monkeypatch):
    """Build Quart app with SSH CA blueprint; patch auth at module level."""
    import sys
    import app.middleware as mw_mod
    import app.security.scope_enforcement as scope_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough_decorator)
    monkeypatch.setattr(scope_mod, "require_scopes", _passthrough_decorator)

    if "app.api.ssh_ca" in sys.modules:
        monkeypatch.setitem(sys.modules, "app.api.ssh_ca", sys.modules["app.api.ssh_ca"])

    import app.api.ssh_ca as ssh_ca_mod
    ssh_ca_mod = importlib.reload(ssh_ca_mod)

    from quart import Quart, g

    app = Quart(__name__)
    app.register_blueprint(ssh_ca_mod.ssh_ca_bp, url_prefix="/api/v1/ssh-ca")

    @app.before_request
    async def _inject_identity():
        g.current_user = {
            "id": 1,
            "username": "tester",
            "_jwt_payload": {
                "sub": "tester",
                "tenant": "acme",
                "scope": "gough.ssh_ca.admin",
            },
        }
        g.tenant_context = SimpleNamespace(tenant_id="acme")

    # Don't patch get_db here - each test will patch it with proper mock config
    monkeypatch.setattr(ssh_ca_mod, "get_current_user", lambda: g.current_user)
    monkeypatch.setattr(ssh_ca_mod, "get_audit_logger", lambda: None)

    return app, ssh_ca_mod


@pytest.fixture
def ssh_ca_client(ssh_ca_app):
    """Create test client for SSH CA app."""
    app, _ = ssh_ca_app
    return app.test_client()


# =============================================================================
# POST /api/v1/ssh-ca/initialize Tests
# =============================================================================


class TestInitializeCA:
    """Tests for POST /api/v1/ssh-ca/initialize endpoint."""

    @pytest.mark.asyncio
    async def test_initialize_ca_success(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test successful CA initialization."""
        app, ssh_ca_mod = ssh_ca_app

        db_mock = _make_db_mock(first_result=None)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        ca_instance = MagicMock()
        ca_instance.initialize.return_value = None
        ca_instance.get_public_key.return_value = "ssh-rsa AAAA..."
        monkeypatch.setattr(ssh_ca_mod, "SSHCertificateAuthority", lambda *a, **kw: ca_instance)

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/initialize",
            json={"ca_name": "Test CA"}
        )

        assert response.status_code == 201
        data = await response.get_json()
        assert data["ca_name"] == "Test CA"

    @pytest.mark.asyncio
    async def test_initialize_ca_already_exists(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test CA initialization when CA already exists."""
        app, ssh_ca_mod = ssh_ca_app

        existing_ca = MagicMock()
        db_mock = _make_db_mock(first_result=existing_ca)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/initialize",
            json={"ca_name": "Test CA"}
        )

        assert response.status_code == 409
        data = await response.get_json()
        assert "already" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_initialize_ca_ssh_exception(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test CA initialization with exception during initialize."""
        app, ssh_ca_mod = ssh_ca_app

        db_mock = _make_db_mock(first_result=None)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        ca_instance = MagicMock()
        ca_instance.initialize.side_effect = RuntimeError("CA init failed")
        monkeypatch.setattr(ssh_ca_mod, "SSHCertificateAuthority", lambda *a, **kw: ca_instance)

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/initialize",
            json={"ca_name": "Test CA"}
        )

        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_initialize_ca_default_name(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test CA initialization with default name."""
        app, ssh_ca_mod = ssh_ca_app

        db_mock = _make_db_mock(first_result=None)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        ca_instance = MagicMock()
        ca_instance.initialize.return_value = None
        ca_instance.get_public_key.return_value = "ssh-rsa AAAA..."
        monkeypatch.setattr(ssh_ca_mod, "SSHCertificateAuthority", lambda *a, **kw: ca_instance)

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/initialize",
            json={}
        )

        assert response.status_code == 201
        data = await response.get_json()
        assert data["ca_name"] == "Gough SSH CA"

    @pytest.mark.asyncio
    async def test_initialize_ca_missing_ca_name_field(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test CA initialization with missing ca_name but allows default."""
        app, ssh_ca_mod = ssh_ca_app

        db_mock = _make_db_mock(first_result=None)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        ca_instance = MagicMock()
        ca_instance.initialize.return_value = None
        ca_instance.get_public_key.return_value = "ssh-rsa AAAA..."
        monkeypatch.setattr(ssh_ca_mod, "SSHCertificateAuthority", lambda *a, **kw: ca_instance)

        response = await ssh_ca_client.post("/api/v1/ssh-ca/initialize")

        assert response.status_code in [201, 400]


# =============================================================================
# GET /api/v1/ssh-ca/public-key Tests
# =============================================================================


class TestGetPublicKey:
    """Tests for GET /api/v1/ssh-ca/public-key endpoint."""

    @pytest.mark.asyncio
    async def test_get_public_key_success(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test successful public key retrieval."""
        app, ssh_ca_mod = ssh_ca_app

        ca_config = MagicMock()
        ca_config.public_key = "ssh-rsa AAAA..."
        ca_config.ca_name = "Test CA"
        db_mock = _make_db_mock(first_result=ca_config)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        response = await ssh_ca_client.get("/api/v1/ssh-ca/public-key")

        assert response.status_code == 200
        data = await response.get_json()
        assert data["public_key"] == "ssh-rsa AAAA..."
        assert data["ca_name"] == "Test CA"

    @pytest.mark.asyncio
    async def test_get_public_key_not_initialized(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test public key retrieval when CA not initialized."""
        app, ssh_ca_mod = ssh_ca_app

        db_mock = _make_db_mock(first_result=None)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        response = await ssh_ca_client.get("/api/v1/ssh-ca/public-key")

        assert response.status_code == 404
        data = await response.get_json()
        assert "not initialized" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_get_public_key_exception(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test public key retrieval with exception."""
        app, ssh_ca_mod = ssh_ca_app

        db_mock = MagicMock()
        db_mock.ssh_ca_configs = MagicMock()
        db_mock.ssh_ca_configs.select.side_effect = RuntimeError("DB error")
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        response = await ssh_ca_client.get("/api/v1/ssh-ca/public-key")

        assert response.status_code == 500


# =============================================================================
# POST /api/v1/ssh-ca/sign Tests
# =============================================================================


class TestSignCertificate:
    """Tests for POST /api/v1/ssh-ca/sign endpoint."""

    @pytest.mark.asyncio
    async def test_sign_certificate_success(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test successful certificate signing."""
        app, ssh_ca_mod = ssh_ca_app

        ca_config = MagicMock()
        db_mock = _make_db_mock(first_result=ca_config)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        monkeypatch.setattr(ssh_ca_mod, "check_shell_access", lambda *a: True)

        ca_instance = MagicMock()
        ca_instance.sign_public_key.return_value = ("ssh-rsa-cert-v01@...", "key-id-123")
        monkeypatch.setattr(ssh_ca_mod, "SSHCertificateAuthority", lambda *a, **kw: ca_instance)

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={
                "public_key": "ssh-rsa AAAA...",
                "resource_type": "vm",
                "resource_id": "res-123",
                "principals": ["ubuntu"],
                "validity_seconds": 3600,
            }
        )

        assert response.status_code == 200
        data = await response.get_json()
        assert data["certificate"] == "ssh-rsa-cert-v01@..."
        assert data["key_id"] == "key-id-123"

    @pytest.mark.asyncio
    async def test_sign_certificate_no_body(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing with no request body."""
        app, ssh_ca_mod = ssh_ca_app

        response = await ssh_ca_client.post("/api/v1/ssh-ca/sign")

        assert response.status_code == 400
        data = await response.get_json()
        assert "request" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_sign_certificate_missing_fields(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing with missing required fields."""
        app, ssh_ca_mod = ssh_ca_app

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={"public_key": "ssh-rsa AAAA..."}
        )

        assert response.status_code == 400
        data = await response.get_json()
        assert "required" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_sign_certificate_principals_not_list(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing with principals as non-list."""
        app, ssh_ca_mod = ssh_ca_app

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={
                "public_key": "ssh-rsa AAAA...",
                "resource_type": "vm",
                "resource_id": "res-123",
                "principals": "ubuntu",
                "validity_seconds": 3600,
            }
        )

        assert response.status_code == 400
        data = await response.get_json()
        assert "principals" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_sign_certificate_principals_empty_list(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing with empty principals list."""
        app, ssh_ca_mod = ssh_ca_app

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={
                "public_key": "ssh-rsa AAAA...",
                "resource_type": "vm",
                "resource_id": "res-123",
                "principals": [],
                "validity_seconds": 3600,
            }
        )

        assert response.status_code == 400
        data = await response.get_json()
        assert "principals" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_sign_certificate_validity_seconds_not_int(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing with validity_seconds as non-int."""
        app, ssh_ca_mod = ssh_ca_app

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={
                "public_key": "ssh-rsa AAAA...",
                "resource_type": "vm",
                "resource_id": "res-123",
                "principals": ["ubuntu"],
                "validity_seconds": "3600",
            }
        )

        assert response.status_code == 400
        data = await response.get_json()
        assert "validity" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_sign_certificate_validity_seconds_non_positive(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing with non-positive validity_seconds."""
        app, ssh_ca_mod = ssh_ca_app

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={
                "public_key": "ssh-rsa AAAA...",
                "resource_type": "vm",
                "resource_id": "res-123",
                "principals": ["ubuntu"],
                "validity_seconds": 0,
            }
        )

        assert response.status_code == 400
        data = await response.get_json()
        assert "validity" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_sign_certificate_no_shell_access(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing without shell access."""
        app, ssh_ca_mod = ssh_ca_app

        monkeypatch.setattr(ssh_ca_mod, "check_shell_access", lambda *a: False)

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={
                "public_key": "ssh-rsa AAAA...",
                "resource_type": "vm",
                "resource_id": "res-123",
                "principals": ["ubuntu"],
                "validity_seconds": 3600,
            }
        )

        assert response.status_code == 400
        data = await response.get_json()
        assert "shell" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_sign_certificate_ca_not_initialized(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing when CA not initialized."""
        app, ssh_ca_mod = ssh_ca_app

        db_mock = _make_db_mock(first_result=None)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        monkeypatch.setattr(ssh_ca_mod, "check_shell_access", lambda *a: True)

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={
                "public_key": "ssh-rsa AAAA...",
                "resource_type": "vm",
                "resource_id": "res-123",
                "principals": ["ubuntu"],
                "validity_seconds": 3600,
            }
        )

        assert response.status_code == 404
        data = await response.get_json()
        assert "not initialized" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_sign_certificate_ssh_exception(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing with exception during sign."""
        app, ssh_ca_mod = ssh_ca_app

        db_mock = MagicMock()
        ca_config = MagicMock()
        db_mock.ssh_ca_configs = MagicMock()
        db_mock.ssh_ca_configs.select.return_value.first.return_value = ca_config
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        monkeypatch.setattr(ssh_ca_mod, "check_shell_access", lambda *a: True)

        ca_instance = MagicMock()
        ca_instance.sign_public_key.side_effect = RuntimeError("Signing failed")
        monkeypatch.setattr(ssh_ca_mod, "SSHCertificateAuthority", lambda *a, **kw: ca_instance)

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={
                "public_key": "ssh-rsa AAAA...",
                "resource_type": "vm",
                "resource_id": "res-123",
                "principals": ["ubuntu"],
                "validity_seconds": 3600,
            }
        )

        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_sign_certificate_multiple_principals(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing with multiple principals."""
        app, ssh_ca_mod = ssh_ca_app

        ca_config = MagicMock()
        db_mock = _make_db_mock(first_result=ca_config)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        monkeypatch.setattr(ssh_ca_mod, "check_shell_access", lambda *a: True)

        ca_instance = MagicMock()
        ca_instance.sign_public_key.return_value = ("cert", "key-id")
        monkeypatch.setattr(ssh_ca_mod, "SSHCertificateAuthority", lambda *a, **kw: ca_instance)

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={
                "public_key": "ssh-rsa AAAA...",
                "resource_type": "vm",
                "resource_id": "res-123",
                "principals": ["ubuntu", "root", "admin"],
                "validity_seconds": 7200,
            }
        )

        assert response.status_code == 200
        data = await response.get_json()
        assert "certificate" in data

    @pytest.mark.asyncio
    async def test_sign_certificate_long_validity(self, ssh_ca_client, ssh_ca_app, monkeypatch):
        """Test signing with long validity period."""
        app, ssh_ca_mod = ssh_ca_app

        ca_config = MagicMock()
        db_mock = _make_db_mock(first_result=ca_config)
        monkeypatch.setattr(ssh_ca_mod, "get_db", lambda: db_mock)

        monkeypatch.setattr(ssh_ca_mod, "check_shell_access", lambda *a: True)

        ca_instance = MagicMock()
        ca_instance.sign_public_key.return_value = ("cert", "key-id")
        monkeypatch.setattr(ssh_ca_mod, "SSHCertificateAuthority", lambda *a, **kw: ca_instance)

        response = await ssh_ca_client.post(
            "/api/v1/ssh-ca/sign",
            json={
                "public_key": "ssh-rsa AAAA...",
                "resource_type": "vm",
                "resource_id": "res-123",
                "principals": ["ubuntu"],
                "validity_seconds": 86400,  # 1 day
            }
        )

        assert response.status_code == 200
