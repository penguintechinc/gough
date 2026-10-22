"""Authentication Endpoints."""

import hashlib
import json
import secrets
import string
from datetime import datetime, timedelta

import bcrypt
import jwt
from quart import Blueprint, current_app, jsonify, request, Response

from .middleware import auth_required, get_current_user
from .models import (
    create_user,
    get_user_by_email,
    get_user_by_id,
    is_refresh_token_valid,
    revoke_all_user_tokens,
    revoke_refresh_token,
    store_refresh_token,
)

auth_bp = Blueprint("auth", __name__)


def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against hash."""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(user_id: int, role: str) -> str:
    """Create JWT access token."""
    expires = datetime.utcnow() + current_app.config["JWT_ACCESS_TOKEN_EXPIRES"]
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "exp": expires,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, current_app.config["JWT_SECRET_KEY"], algorithm="HS256")


def create_refresh_token(user_id: int) -> tuple[str, datetime]:
    """Create JWT refresh token and store hash in database."""
    expires = datetime.utcnow() + current_app.config["JWT_REFRESH_TOKEN_EXPIRES"]
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": expires,
        "iat": datetime.utcnow(),
    }
    token = jwt.encode(payload, current_app.config["JWT_SECRET_KEY"], algorithm="HS256")

    # Store hash of token in database for revocation
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    store_refresh_token(user_id, token_hash, expires)

    return token, expires


@auth_bp.route("/login", methods=["POST"])
async def login():
    """Login endpoint - returns access and refresh tokens."""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    # Find user
    user = get_user_by_email(email)
    if not user:
        return jsonify({"error": "Invalid email or password"}), 401

    # Verify password
    if not verify_password(password, user["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401

    # Check if user is active
    if not user.get("is_active"):
        return jsonify({"error": "Account is deactivated"}), 401

    # Generate tokens
    access_token = create_access_token(user["id"], user["role"])
    refresh_token, refresh_expires = create_refresh_token(user["id"])

    return jsonify({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": int(current_app.config["JWT_ACCESS_TOKEN_EXPIRES"].total_seconds()),
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user.get("full_name", ""),
            "role": user["role"],
        },
    }), 200


@auth_bp.route("/refresh", methods=["POST"])
async def refresh():
    """Refresh access token using refresh token."""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    refresh_token = data.get("refresh_token", "")

    if not refresh_token:
        return jsonify({"error": "Refresh token required"}), 400

    # Decode token
    try:
        payload = jwt.decode(
            refresh_token,
            current_app.config["JWT_SECRET_KEY"],
            algorithms=["HS256"],
        )
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Refresh token expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid refresh token"}), 401

    # Verify token type
    if payload.get("type") != "refresh":
        return jsonify({"error": "Invalid token type"}), 401

    # Check if token is revoked
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    if not is_refresh_token_valid(token_hash):
        return jsonify({"error": "Refresh token has been revoked"}), 401

    # Get user
    user_id = int(payload["sub"])
    user = get_user_by_id(user_id)
    if not user or not user.get("is_active"):
        return jsonify({"error": "User not found or deactivated"}), 401

    # Revoke old refresh token
    revoke_refresh_token(token_hash)

    # Generate new tokens
    access_token = create_access_token(user["id"], user["role"])
    new_refresh_token, refresh_expires = create_refresh_token(user["id"])

    return jsonify({
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "token_type": "Bearer",
        "expires_in": int(current_app.config["JWT_ACCESS_TOKEN_EXPIRES"].total_seconds()),
    }), 200


@auth_bp.route("/logout", methods=["POST"])
@auth_required
async def logout():
    """Logout endpoint - revokes all refresh tokens for user."""
    user = get_current_user()

    # Revoke all user's refresh tokens
    revoked_count = revoke_all_user_tokens(user["id"])

    return jsonify({
        "message": "Successfully logged out",
        "tokens_revoked": revoked_count,
    }), 200


@auth_bp.route("/me", methods=["GET"])
@auth_required
async def get_me():
    """Get current user profile."""
    user = get_current_user()

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
    """Register new user (creates viewer role by default)."""
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    full_name = data.get("full_name", "").strip()

    # Validation
    if not email:
        return jsonify({"error": "Email is required"}), 400

    if not password or len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    # Check if user exists
    existing = get_user_by_email(email)
    if existing:
        return jsonify({"error": "Email already registered"}), 409

    # Create user
    password_hash = hash_password(password)
    user = create_user(
        email=email,
        password_hash=password_hash,
        full_name=full_name,
        role="viewer",  # Default role for self-registration
    )

    return jsonify({
        "message": "Registration successful",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user.get("full_name", ""),
            "role": user["role"],
        },
    }), 201


# Device code flow constants and helpers
_DEVICE_CODE_TTL = 600  # 10 minutes
_DEVICE_POLL_INTERVAL = 5  # seconds
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # No O, I, L, 1, 0


def _generate_user_code() -> str:
    """Generate user code in XXXX-XXXX format using alphanumeric minus ambiguous chars."""
    code = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{code[:4]}-{code[4:]}"


def _get_redis_client():
    """Get Redis client from app extensions."""
    try:
        return current_app.extensions.get("redis_client")
    except (AttributeError, RuntimeError):
        return None


def _mint_device_tokens(user_id: int, scope: str) -> tuple[str, str]:
    """Create JWT access and refresh tokens for device code flow."""
    access_token = create_access_token(user_id, "operator")
    refresh_token, _ = create_refresh_token(user_id)
    return access_token, refresh_token


@auth_bp.route("/device", methods=["POST"])
async def device_authorization():
    """RFC 8628 device authorization endpoint.

    Request body (form-encoded):
    - client_id: Must be "gough-cli"
    - scope: OIDC scopes (space-separated)

    Response:
    {
      "device_code": "...",
      "user_code": "XXXX-XXXX",
      "verification_uri": "http://localhost:8080/device",
      "verification_uri_complete": "http://localhost:8080/device?user_code=XXXX-XXXX",
      "expires_in": 600,
      "interval": 5
    }
    """
    data = await request.form
    client_id = data.get("client_id", "").strip()
    scope = data.get("scope", "openid offline_access gough.operator").strip()

    if not client_id:
        return jsonify({"error": "invalid_request", "error_description": "client_id required"}), 400

    if client_id != "gough-cli":
        return jsonify({"error": "unauthorized_client", "error_description": "Unsupported client"}), 400

    # Generate codes
    device_code = secrets.token_urlsafe(32)
    user_code = _generate_user_code()

    # Store in Redis if available
    redis_client = _get_redis_client()
    if redis_client:
        device_data = {
            "user_code": user_code,
            "client_id": client_id,
            "scope": scope,
            "expires_at": (datetime.utcnow() + timedelta(seconds=_DEVICE_CODE_TTL)).isoformat(),
            "authorized": False,
        }
        try:
            await redis_client.setex(
                f"device:{device_code}",
                _DEVICE_CODE_TTL,
                json.dumps(device_data),
            )
        except Exception as e:
            current_app.logger.error("Failed to store device code in Redis: %s", e)
            return jsonify({"error": "server_error"}), 500

    base_url = current_app.config.get("BASE_URL", "http://localhost:8080")

    return jsonify({
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": f"{base_url}/device",
        "verification_uri_complete": f"{base_url}/device?user_code={user_code}",
        "expires_in": _DEVICE_CODE_TTL,
        "interval": _DEVICE_POLL_INTERVAL,
    }), 200


@auth_bp.route("/token", methods=["POST"])
async def token_endpoint():
    """RFC 8628 token endpoint (polling with device_code grant) + refresh token support.

    For device code:
    Request body (form-encoded):
    - grant_type: "urn:ietf:params:oauth:grant-type:device_code"
    - client_id: "gough-cli"
    - device_code: from /device endpoint

    For refresh token:
    Request body (form-encoded):
    - grant_type: "refresh_token"
    - refresh_token: from previous token response

    Response (on success):
    {
      "access_token": "...",
      "refresh_token": "...",
      "token_type": "Bearer",
      "expires_in": 3600,
      "scope": "openid offline_access gough.operator"
    }
    """
    data = await request.form
    grant_type = data.get("grant_type", "").strip()

    # Handle device code grant
    if grant_type == "urn:ietf:params:oauth:grant-type:device_code":
        client_id = data.get("client_id", "").strip()
        device_code = data.get("device_code", "").strip()

        if not client_id or not device_code:
            return jsonify({"error": "invalid_request"}), 400

        if client_id != "gough-cli":
            return jsonify({"error": "invalid_client"}), 401

        # Look up device code in Redis
        redis_client = _get_redis_client()
        if not redis_client:
            return jsonify({"error": "server_error"}), 500

        try:
            device_data_str = await redis_client.get(f"device:{device_code}")
        except Exception as e:
            current_app.logger.error("Redis lookup failed: %s", e)
            return jsonify({"error": "server_error"}), 500

        if not device_data_str:
            return jsonify({"error": "expired_token"}), 400

        try:
            device_data = json.loads(device_data_str)
        except json.JSONDecodeError:
            return jsonify({"error": "server_error"}), 500

        # Check authorization status
        if not device_data.get("authorized"):
            return jsonify({"error": "authorization_pending"}), 400

        # Check for tokens
        access_token = device_data.get("access_token")
        refresh_token = device_data.get("refresh_token")

        if not access_token or not refresh_token:
            return jsonify({"error": "server_error"}), 500

        # Clean up device code from Redis
        try:
            await redis_client.delete(f"device:{device_code}")
        except Exception as e:
            current_app.logger.warning("Failed to delete device code: %s", e)

        return jsonify({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": int(current_app.config["JWT_ACCESS_TOKEN_EXPIRES"].total_seconds()),
            "scope": device_data.get("scope", "openid offline_access gough.operator"),
        }), 200

    # Handle refresh token grant
    elif grant_type == "refresh_token":
        refresh_token = data.get("refresh_token", "").strip()

        if not refresh_token:
            return jsonify({"error": "invalid_request"}), 400

        # Decode and validate refresh token
        try:
            payload = jwt.decode(
                refresh_token,
                current_app.config["JWT_SECRET_KEY"],
                algorithms=["HS256"],
            )
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "invalid_grant"}), 400
        except jwt.InvalidTokenError:
            return jsonify({"error": "invalid_grant"}), 400

        if payload.get("type") != "refresh":
            return jsonify({"error": "invalid_grant"}), 400

        # Verify token not revoked
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        if not is_refresh_token_valid(token_hash):
            return jsonify({"error": "invalid_grant"}), 400

        # Get user
        user_id = int(payload["sub"])
        user = get_user_by_id(user_id)
        if not user or not user.get("is_active"):
            return jsonify({"error": "invalid_grant"}), 400

        # Revoke old refresh token
        revoke_refresh_token(token_hash)

        # Issue new tokens
        new_access_token = create_access_token(user["id"], user["role"])
        new_refresh_token, _ = create_refresh_token(user["id"])

        return jsonify({
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "Bearer",
            "expires_in": int(current_app.config["JWT_ACCESS_TOKEN_EXPIRES"].total_seconds()),
        }), 200

    else:
        return jsonify({"error": "unsupported_grant_type"}), 400


@auth_bp.route("/device/approve", methods=["GET", "POST"])
async def device_approve():
    """User approval page for device code flow.

    GET: Display approval form with user_code
    POST: Mark device code as authorized and mint tokens
    """
    redis_client = _get_redis_client()
    if not redis_client:
        return Response(
            "<html><body><h1>Device Authorization Not Available</h1></body></html>",
            content_type="text/html",
            status=503,
        )

    if request.method == "GET":
        user_code = request.args.get("user_code", "")
        return Response(
            f"""<!DOCTYPE html>
<html>
<head>
    <title>Device Authorization</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        .form-container {{ max-width: 400px; }}
        .code {{ font-size: 24px; font-weight: bold; margin: 20px 0; }}
        button {{ padding: 10px 20px; margin-right: 10px; }}
    </style>
</head>
<body>
    <h1>Device Authorization</h1>
    <p>A device is requesting access. If you initiated this, enter the code below:</p>
    <div class="code">{user_code or "----"}</div>
    <form method="POST" class="form-container">
        <input type="hidden" name="user_code" value="{user_code}">
        <button type="submit" name="action" value="approve">Approve</button>
        <button type="submit" name="action" value="deny">Deny</button>
    </form>
</body>
</html>""",
            content_type="text/html",
        )

    else:  # POST
        user_code = (await request.form).get("user_code", "").strip()
        action = (await request.form).get("action", "").strip()

        if not user_code:
            return Response(
                "<html><body><h1>Error</h1><p>User code required</p></body></html>",
                content_type="text/html",
                status=400,
            )

        # Find device code by user_code
        try:
            keys = await redis_client.keys("device:*")
        except Exception as e:
            current_app.logger.error("Redis keys lookup failed: %s", e)
            return Response(
                "<html><body><h1>Error</h1><p>Server error</p></body></html>",
                content_type="text/html",
                status=500,
            )

        device_code_found = None
        for key in keys:
            try:
                data_str = await redis_client.get(key)
                if data_str:
                    data = json.loads(data_str)
                    if data.get("user_code") == user_code:
                        device_code_found = key.decode() if isinstance(key, bytes) else key
                        device_code_found = device_code_found.replace("device:", "")
                        break
            except Exception:
                continue

        if not device_code_found:
            return Response(
                "<html><body><h1>Error</h1><p>Code not found or expired</p></body></html>",
                content_type="text/html",
                status=400,
            )

        if action == "deny":
            try:
                await redis_client.delete(f"device:{device_code_found}")
            except Exception as e:
                current_app.logger.warning("Failed to delete denied device code: %s", e)

            return Response(
                "<html><body><h1>Denied</h1><p>Device authorization denied</p></body></html>",
                content_type="text/html",
            )

        # Approve: update Redis entry with tokens (using a hardcoded operator user for M1)
        try:
            data_str = await redis_client.get(f"device:{device_code_found}")
            device_data = json.loads(data_str)

            # For M1, use a default operator user (ID 1 - admin)
            # In production, this would prompt for user selection/login
            user_id = 1
            user = get_user_by_id(user_id)
            if not user:
                return Response(
                    "<html><body><h1>Error</h1><p>User not found</p></body></html>",
                    content_type="text/html",
                    status=500,
                )

            access_token, refresh_token = _mint_device_tokens(user_id, device_data.get("scope", ""))

            device_data["authorized"] = True
            device_data["access_token"] = access_token
            device_data["refresh_token"] = refresh_token

            await redis_client.setex(
                f"device:{device_code_found}",
                _DEVICE_CODE_TTL,
                json.dumps(device_data),
            )
        except Exception as e:
            current_app.logger.error("Failed to authorize device code: %s", e)
            return Response(
                "<html><body><h1>Error</h1><p>Server error</p></body></html>",
                content_type="text/html",
                status=500,
            )

        return Response(
            "<html><body><h1>Success</h1><p>Device authorized. Return to your CLI.</p></body></html>",
            content_type="text/html",
        )
