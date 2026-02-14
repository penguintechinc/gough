"""Authentication module for Gough.

Provides JWT-based authentication for Quart backend with PyDAL datastore.
Handles user login, logout, token refresh, and password reset flows.
"""

from quart import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta
import jwt
import bcrypt
import secrets
from functools import wraps

from ..models import get_db
from ..security_datastore import PyDALUser, PyDALRole


auth_bp = Blueprint("auth", __name__)


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def generate_jwt_token(user_id: int, expires_in_minutes: int = 30) -> str:
    """Generate a JWT token for a user."""
    payload = {
        "user_id": user_id,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(minutes=expires_in_minutes),
    }
    token = jwt.encode(
        payload,
        current_app.config.get("JWT_SECRET_KEY"),
        algorithm="HS256"
    )
    return token


def generate_refresh_token(user_id: int) -> str:
    """Generate a refresh token and store it in the database."""
    db = get_db()

    # Create token
    token_value = secrets.token_urlsafe(32)
    token_hash = bcrypt.hashpw(token_value.encode(), bcrypt.gensalt()).decode()

    # Calculate expiration
    expires_at = datetime.utcnow() + timedelta(
        days=current_app.config.get("JWT_REFRESH_TOKEN_EXPIRES", timedelta(days=7)).days
    )

    # Store in database
    db.auth_refresh_tokens.insert(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
        revoked=False,
        created_at=datetime.utcnow(),
    )
    db.commit()

    return token_value


def verify_jwt_token(token: str) -> dict | None:
    """Verify a JWT token and return payload if valid."""
    try:
        payload = jwt.decode(
            token,
            current_app.config.get("JWT_SECRET_KEY"),
            algorithms=["HS256"]
        )
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def verify_refresh_token(user_id: int, token_value: str) -> bool:
    """Verify a refresh token."""
    db = get_db()

    # Find token record
    token_record = db(
        (db.auth_refresh_tokens.user_id == user_id)
        & (db.auth_refresh_tokens.revoked == False)
        & (db.auth_refresh_tokens.expires_at > datetime.utcnow())
    ).select().first()

    if not token_record:
        return False

    # Verify hash
    return bcrypt.checkpw(token_value.encode(), token_record.token_hash.encode())


def revoke_refresh_token(user_id: int, token_value: str) -> bool:
    """Revoke a refresh token."""
    db = get_db()

    # Find and mark as revoked
    updated = db(
        (db.auth_refresh_tokens.user_id == user_id)
        & (db.auth_refresh_tokens.revoked == False)
    ).update(revoked=True)
    db.commit()

    return updated > 0


def require_auth(f):
    """Decorator to require JWT authentication on a route."""
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        # Get token from Authorization header
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return jsonify({"error": "Missing Authorization header"}), 401

        # Extract token
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return jsonify({"error": "Invalid Authorization header format"}), 401

        token = parts[1]

        # Verify token
        payload = verify_jwt_token(token)
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401

        # Get user from database
        db = get_db()
        user_row = db(db.auth_user.id == payload.get("user_id")).select().first()

        if not user_row or not user_row.active:
            return jsonify({"error": "User not found or inactive"}), 401

        # Get user roles
        user_datastore = current_app.user_datastore
        roles = user_datastore._get_user_roles(user_row.id)
        user = PyDALUser(user_row, roles=roles)

        # Store user in request context
        request.user = user

        return await f(*args, **kwargs)

    return decorated_function


def require_role(*roles):
    """Decorator to require specific roles."""
    def decorator(f):
        @wraps(f)
        async def decorated_function(*args, **kwargs):
            if not hasattr(request, "user"):
                return jsonify({"error": "Authentication required"}), 401

            user = request.user
            if not any(user.has_role(role) for role in roles):
                return jsonify({"error": "Insufficient permissions"}), 403

            return await f(*args, **kwargs)

        return decorated_function

    return decorator


# ============================================================================
# Authentication Routes
# ============================================================================


@auth_bp.route("/login", methods=["POST"])
async def login():
    """Login endpoint - validate credentials and issue tokens.

    Request body:
        - email: user email address
        - password: user password

    Returns:
        200: {access_token, refresh_token, user}
        401: Invalid credentials
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    email = data.get("email", "").strip()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    # Find user
    db = get_db()
    user_row = db(db.auth_user.email == email).select().first()

    if not user_row:
        return jsonify({"error": "Invalid email or password"}), 401

    # Verify password
    if not verify_password(password, user_row.password):
        return jsonify({"error": "Invalid email or password"}), 401

    # Check if user is active
    if not user_row.active:
        return jsonify({"error": "User account is inactive"}), 403

    # Update login tracking
    client_ip = request.remote_addr or "unknown"
    db(db.auth_user.id == user_row.id).update(
        last_login_at=user_row.current_login_at,
        current_login_at=datetime.utcnow(),
        last_login_ip=user_row.current_login_ip,
        current_login_ip=client_ip,
        login_count=user_row.login_count + 1,
    )
    db.commit()

    # Generate tokens
    access_token = generate_jwt_token(user_row.id, expires_in_minutes=30)
    refresh_token = generate_refresh_token(user_row.id)

    # Get user roles
    user_datastore = current_app.user_datastore
    roles = user_datastore._get_user_roles(user_row.id)
    user = PyDALUser(user_row, roles=roles)

    # Build response
    return jsonify({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "roles": [r.name for r in user.roles],
        },
    }), 200


@auth_bp.route("/refresh", methods=["POST"])
async def refresh():
    """Refresh token endpoint - issue new access token.

    Request body:
        - refresh_token: previously issued refresh token

    Returns:
        200: {access_token}
        401: Invalid refresh token
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    refresh_token = data.get("refresh_token", "").strip()

    if not refresh_token:
        return jsonify({"error": "Refresh token required"}), 400

    # Verify token format (should contain user_id or we need to extract it)
    # For now, we'll verify against stored tokens
    db = get_db()

    # Find valid token record
    token_record = db(
        (db.auth_refresh_tokens.revoked == False)
        & (db.auth_refresh_tokens.expires_at > datetime.utcnow())
    ).select()

    # Find matching token by hash
    user_id = None
    for record in token_record:
        if bcrypt.checkpw(refresh_token.encode(), record.token_hash.encode()):
            user_id = record.user_id
            break

    if not user_id:
        return jsonify({"error": "Invalid or expired refresh token"}), 401

    # Get user
    user_row = db(db.auth_user.id == user_id).select().first()

    if not user_row or not user_row.active:
        return jsonify({"error": "User not found or inactive"}), 401

    # Generate new access token
    access_token = generate_jwt_token(user_row.id, expires_in_minutes=30)

    return jsonify({"access_token": access_token}), 200


@auth_bp.route("/logout", methods=["POST"])
@require_auth
async def logout():
    """Logout endpoint - revoke refresh token.

    Returns:
        200: Success message
    """
    data = await request.get_json() or {}
    refresh_token = data.get("refresh_token", "")

    if refresh_token:
        # Revoke the refresh token
        revoke_refresh_token(request.user.id, refresh_token)

    return jsonify({"message": "Logged out successfully"}), 200


@auth_bp.route("/me", methods=["GET"])
@require_auth
async def get_current_user():
    """Get current authenticated user.

    Returns:
        200: Current user object
        401: Not authenticated
    """
    user = request.user

    return jsonify({
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "active": user.active,
        "roles": [r.name for r in user.roles],
        "login_count": user.login_count,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }), 200


@auth_bp.route("/change-password", methods=["POST"])
@require_auth
async def change_password():
    """Change user password.

    Request body:
        - current_password: current password
        - new_password: new password

    Returns:
        200: Password changed
        400: Invalid request
        401: Invalid current password
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")

    if not current_password or not new_password:
        return jsonify({"error": "Current and new passwords required"}), 400

    if len(new_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    # Get user from database
    db = get_db()
    user_row = db(db.auth_user.id == request.user.id).select().first()

    if not user_row:
        return jsonify({"error": "User not found"}), 404

    # Verify current password
    if not verify_password(current_password, user_row.password):
        return jsonify({"error": "Invalid current password"}), 401

    # Update password
    new_hash = hash_password(new_password)
    db(db.auth_user.id == user_row.id).update(
        password=new_hash,
        updated_at=datetime.utcnow(),
    )
    db.commit()

    return jsonify({"message": "Password changed successfully"}), 200


@auth_bp.route("/request-password-reset", methods=["POST"])
async def request_password_reset():
    """Request password reset - sends reset link via email.

    Request body:
        - email: user email address

    Returns:
        200: Reset request sent (always, for security)
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    email = data.get("email", "").strip()

    if not email:
        return jsonify({"error": "Email required"}), 400

    # Find user
    db = get_db()
    user_row = db(db.auth_user.email == email).select().first()

    # Always return success for security (don't leak user existence)
    if not user_row:
        return jsonify({"message": "If email exists, reset link has been sent"}), 200

    # Generate reset token (valid for 24 hours)
    reset_token = secrets.token_urlsafe(32)
    reset_hash = bcrypt.hashpw(reset_token.encode(), bcrypt.gensalt()).decode()

    # Revoke any existing reset tokens for this user
    db(db.auth_password_resets.user_id == user_row.id).delete()

    # Store reset token with expiration
    expires_at = datetime.utcnow() + timedelta(hours=24)
    db.auth_password_resets.insert(
        user_id=user_row.id,
        token_hash=reset_hash,
        expires_at=expires_at,
        used=False,
        created_at=datetime.utcnow(),
    )
    db.commit()

    # In production, send email with reset link containing reset_token
    # For now, just log the token
    current_app.logger.info(f"Password reset requested for {email}")

    return jsonify({"message": "If email exists, reset link has been sent"}), 200


@auth_bp.route("/reset-password", methods=["POST"])
async def reset_password():
    """Reset password using reset token.

    Request body:
        - reset_token: reset token from email
        - new_password: new password

    Returns:
        200: Password reset
        401: Invalid reset token
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    reset_token = data.get("reset_token", "").strip()
    new_password = data.get("new_password", "")

    if not reset_token or not new_password:
        return jsonify({"error": "Reset token and new password required"}), 400

    if len(new_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    # Find valid reset token
    db = get_db()
    token_records = db(
        (db.auth_password_resets.used == False)
        & (db.auth_password_resets.expires_at > datetime.utcnow())
    ).select()

    # Find matching token by hash
    user_id = None
    token_record_id = None
    for record in token_records:
        if bcrypt.checkpw(reset_token.encode(), record.token_hash.encode()):
            user_id = record.user_id
            token_record_id = record.id
            break

    if not user_id:
        return jsonify({"error": "Invalid or expired reset token"}), 401

    # Get user
    user_row = db(db.auth_user.id == user_id).select().first()

    if not user_row:
        return jsonify({"error": "User not found"}), 404

    # Update password
    new_hash = hash_password(new_password)
    db(db.auth_user.id == user_id).update(
        password=new_hash,
        updated_at=datetime.utcnow(),
    )

    # Mark reset token as used
    db(db.auth_password_resets.id == token_record_id).update(used=True)
    db.commit()

    return jsonify({"message": "Password reset successfully"}), 200


@auth_bp.route("/health", methods=["GET"])
async def health():
    """Health check endpoint for auth service."""
    return jsonify({"status": "healthy"}), 200
