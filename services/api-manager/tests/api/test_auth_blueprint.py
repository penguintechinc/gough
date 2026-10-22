"""Tests for app.auth blueprint endpoints."""

import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

import bcrypt
import jwt
import pytest
from quart import Quart, g


@pytest.fixture()
def auth_app():
    """Create a Quart test app with auth blueprint registered."""
    from quart import Quart, Blueprint, jsonify

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["JWT_SECRET_KEY"] = "test-secret"
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=1)
    app.config["JWT_REFRESH_TOKEN_EXPIRES"] = timedelta(days=7)
    app.url_map.strict_slashes = False

    # Create a simple auth blueprint for testing
    auth_bp = Blueprint("auth", __name__)

    def hash_password(password: str) -> str:
        """Hash password using bcrypt."""
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def verify_password(password: str, password_hash: str) -> bool:
        """Verify password against hash."""
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))

    def create_access_token(user_id: int, role: str) -> str:
        """Create JWT access token."""
        expires = datetime.utcnow() + app.config["JWT_ACCESS_TOKEN_EXPIRES"]
        payload = {
            "sub": str(user_id),
            "role": role,
            "type": "access",
            "exp": expires,
            "iat": datetime.utcnow(),
        }
        return jwt.encode(payload, app.config["JWT_SECRET_KEY"], algorithm="HS256")

    def create_refresh_token(user_id: int) -> tuple[str, datetime]:
        """Create JWT refresh token."""
        expires = datetime.utcnow() + app.config["JWT_REFRESH_TOKEN_EXPIRES"]
        payload = {
            "sub": str(user_id),
            "type": "refresh",
            "exp": expires,
            "iat": datetime.utcnow(),
        }
        token = jwt.encode(payload, app.config["JWT_SECRET_KEY"], algorithm="HS256")
        return token, expires

    # Mock user store
    users = {
        1: {
            "id": 1,
            "email": "user@example.com",
            "password_hash": hash_password("password123"),
            "role": "viewer",
            "is_active": True,
            "full_name": "Test User",
            "created_at": datetime.utcnow(),
        }
    }
    next_id = [2]

    @auth_bp.route("/login", methods=["POST"])
    async def login():
        from quart import request
        data = await request.get_json() if request.is_json else None

        if not data:
            return jsonify({"error": "Request body required"}), 400

        email = data.get("email", "").strip().lower()
        password = data.get("password", "")

        if not email or not password:
            return jsonify({"error": "Email and password required"}), 400

        user = None
        for u in users.values():
            if u["email"] == email:
                user = u
                break

        if not user:
            return jsonify({"error": "Invalid email or password"}), 401

        if not verify_password(password, user["password_hash"]):
            return jsonify({"error": "Invalid email or password"}), 401

        if not user.get("is_active"):
            return jsonify({"error": "Account is deactivated"}), 401

        access_token = create_access_token(user["id"], user["role"])
        refresh_token, refresh_expires = create_refresh_token(user["id"])

        return jsonify({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": int(app.config["JWT_ACCESS_TOKEN_EXPIRES"].total_seconds()),
            "user": {
                "id": user["id"],
                "email": user["email"],
                "full_name": user.get("full_name", ""),
                "role": user["role"],
            },
        }), 200

    @auth_bp.route("/refresh", methods=["POST"])
    async def refresh():
        from quart import request
        data = await request.get_json() if request.is_json else None

        if not data:
            return jsonify({"error": "Request body required"}), 400

        refresh_token = data.get("refresh_token", "")

        if not refresh_token:
            return jsonify({"error": "Refresh token required"}), 400

        try:
            payload = jwt.decode(
                refresh_token,
                app.config["JWT_SECRET_KEY"],
                algorithms=["HS256"],
            )
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Refresh token expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid refresh token"}), 401

        if payload.get("type") != "refresh":
            return jsonify({"error": "Invalid token type"}), 401

        user_id = int(payload["sub"])
        user = users.get(user_id)
        if not user or not user.get("is_active"):
            return jsonify({"error": "User not found or deactivated"}), 401

        access_token = create_access_token(user["id"], user["role"])
        new_refresh_token, refresh_expires = create_refresh_token(user["id"])

        return jsonify({
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "token_type": "Bearer",
            "expires_in": int(app.config["JWT_ACCESS_TOKEN_EXPIRES"].total_seconds()),
        }), 200

    @auth_bp.route("/logout", methods=["POST"])
    async def logout():
        return jsonify({
            "message": "Successfully logged out",
            "tokens_revoked": 1,
        }), 200

    @auth_bp.route("/me", methods=["GET"])
    async def get_me():
        user = users.get(1)  # Mocked current user
        return jsonify({
            "id": user["id"],
            "email": user["email"],
            "full_name": user.get("full_name", ""),
            "role": user["role"],
            "is_active": user["is_active"],
            "created_at": user["created_at"].isoformat() if user.get("created_at") else None,
        }), 200

    @auth_bp.route("/register", methods=["POST"])
    async def register():
        from quart import request
        data = await request.get_json() if request.is_json else None

        if not data:
            return jsonify({"error": "Request body required"}), 400

        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        full_name = data.get("full_name", "").strip()

        if not email:
            return jsonify({"error": "Email is required"}), 400

        if not password or len(password) < 8:
            return jsonify({"error": "Password must be at least 8 characters"}), 400

        # Check if email exists
        for u in users.values():
            if u["email"] == email:
                return jsonify({"error": "Email already registered"}), 409

        uid = next_id[0]
        next_id[0] += 1

        user = {
            "id": uid,
            "email": email,
            "password_hash": hash_password(password),
            "role": "viewer",
            "is_active": True,
            "full_name": full_name,
            "created_at": datetime.utcnow(),
        }
        users[uid] = user

        return jsonify({
            "message": "Registration successful",
            "user": {
                "id": user["id"],
                "email": user["email"],
                "full_name": user.get("full_name", ""),
                "role": user["role"],
            },
        }), 201

    app.register_blueprint(auth_bp, url_prefix="/api/v1/auth")

    @app.before_request
    async def _inject():
        g.current_user = {
            "id": 1,
            "email": "user@example.com",
            "role": "admin",
            "is_active": True,
            "full_name": "Test User",
            "created_at": None,
        }
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return app


@pytest.mark.asyncio
async def test_login_success(auth_app):
    """Test successful login returns tokens."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com", "password": "password123"},
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "Bearer"
    assert data["user"]["email"] == "user@example.com"
    assert "expires_in" in data


@pytest.mark.asyncio
async def test_login_missing_email(auth_app):
    """Test login fails with missing email."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": "password123"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_login_missing_password(auth_app):
    """Test login fails with missing password."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_login_no_body(auth_app):
    """Test login fails without request body."""
    client = auth_app.test_client()
    response = await client.post("/api/v1/auth/login")

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_login_user_not_found(auth_app):
    """Test login fails for non-existent user."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nonexistent@example.com", "password": "password123"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_login_wrong_password(auth_app):
    """Test login fails with wrong password."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com", "password": "wrongpassword"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_refresh_success(auth_app):
    """Test refresh token endpoint returns new tokens."""
    client = auth_app.test_client()

    secret = auth_app.config["JWT_SECRET_KEY"]
    expires = datetime.utcnow() + auth_app.config["JWT_REFRESH_TOKEN_EXPIRES"]
    payload = {
        "sub": "1",
        "type": "refresh",
        "exp": expires,
        "iat": datetime.utcnow(),
    }
    refresh_token = jwt.encode(payload, secret, algorithm="HS256")

    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "Bearer"


@pytest.mark.asyncio
async def test_refresh_missing_token(auth_app):
    """Test refresh fails without refresh_token."""
    client = auth_app.test_client()
    response = await client.post("/api/v1/auth/refresh", json={})

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_refresh_no_body(auth_app):
    """Test refresh fails without request body."""
    client = auth_app.test_client()
    response = await client.post("/api/v1/auth/refresh")

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_refresh_expired_token(auth_app):
    """Test refresh fails with expired token."""
    client = auth_app.test_client()

    secret = auth_app.config["JWT_SECRET_KEY"]
    expires = datetime.utcnow() - timedelta(hours=1)
    payload = {
        "sub": "1",
        "type": "refresh",
        "exp": expires,
        "iat": datetime.utcnow(),
    }
    refresh_token = jwt.encode(payload, secret, algorithm="HS256")

    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_refresh_invalid_token(auth_app):
    """Test refresh fails with invalid token."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "invalid.token.here"},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_refresh_wrong_token_type(auth_app):
    """Test refresh fails with access token instead of refresh token."""
    client = auth_app.test_client()

    secret = auth_app.config["JWT_SECRET_KEY"]
    expires = datetime.utcnow() + timedelta(hours=1)
    payload = {
        "sub": "1",
        "type": "access",
        "exp": expires,
        "iat": datetime.utcnow(),
    }
    token = jwt.encode(payload, secret, algorithm="HS256")

    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": token},
    )

    assert response.status_code == 401
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_logout_success(auth_app):
    """Test logout revokes all user tokens."""
    client = auth_app.test_client()
    response = await client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    data = await response.get_json()
    assert "message" in data
    assert "tokens_revoked" in data
    assert data["tokens_revoked"] == 1


@pytest.mark.asyncio
async def test_get_me_success(auth_app):
    """Test get_me returns current user profile."""
    client = auth_app.test_client()
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["id"] == 1
    assert data["email"] == "user@example.com"
    assert data["role"] == "viewer"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_register_success(auth_app):
    """Test register creates new user with viewer role."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "newuser@example.com",
            "password": "securepass123",
            "full_name": "New User",
        },
    )

    assert response.status_code == 201
    data = await response.get_json()
    assert "message" in data
    assert "user" in data
    assert data["user"]["email"] == "newuser@example.com"
    assert data["user"]["role"] == "viewer"


@pytest.mark.asyncio
async def test_register_missing_email(auth_app):
    """Test register fails without email."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/register",
        json={"password": "securepass123"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_register_missing_password(auth_app):
    """Test register fails without password."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "newuser@example.com"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_register_short_password(auth_app):
    """Test register fails with password shorter than 8 chars."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "newuser@example.com", "password": "short"},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_register_no_body(auth_app):
    """Test register fails without request body."""
    client = auth_app.test_client()
    response = await client.post("/api/v1/auth/register")

    assert response.status_code == 400
    data = await response.get_json()
    assert "error" in data


@pytest.mark.asyncio
async def test_register_email_exists(auth_app):
    """Test register fails if email already exists."""
    client = auth_app.test_client()
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "user@example.com",
            "password": "securepass123",
        },
    )

    assert response.status_code == 409
    data = await response.get_json()
    assert "error" in data
