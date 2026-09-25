"""Authentication module for Gough.

Provides JWT-based authentication for Quart backend with PyDAL datastore.
Handles user login, logout, token refresh, and password reset flows.
"""

from quart import Blueprint, request, jsonify, current_app, make_response
from datetime import UTC, datetime, timedelta
from typing import Any, cast
import bcrypt
import secrets
from functools import wraps

from penguin_aaa.authn.types import Claims

from ..db.run_db import run_db
from ..middleware import (
    COOKIE_ACCESS_NAME,
    COOKIE_CSRF_NAME,
    COOKIE_REFRESH_NAME,
    cookies_are_secure,
)
from ..models import get_db
from ..security.scope_policy import _expand_roles_to_scopes
from ..security_datastore import PyDALUser, PyDALRole


auth_bp = Blueprint("auth", __name__)

# Path scoping matches app.middleware's cookie contract: the access + CSRF
# cookies are sent to every route; the refresh cookie is scoped to the auth
# blueprint only (the sole consumer of gough_refresh).
_REFRESH_COOKIE_PATH = "/api/v1/auth"


def _set_auth_cookies(response: Any, access_token: str, refresh_token: str) -> None:
    """Set the three browser auth cookies (regression: security audit 2026-09-22).

    ``gough_access``/``gough_refresh`` are HttpOnly -- never JS-readable, which
    is what closes the XSS/localStorage exfiltration gap. ``gough_csrf`` is
    deliberately NOT HttpOnly: JS must read it and echo it back as
    ``X-CSRF-Token`` on state-changing requests (double-submit defense, see
    ``app.middleware`` CSRF bridge). ``Secure`` is gated on app config
    (DEBUG/TESTING) only, never request data -- see ``cookies_are_secure``.
    Tokens are ALSO kept in the JSON body (unchanged) for CLI/service clients
    that never touch cookies.
    """
    secure = cookies_are_secure(current_app.config)
    csrf_token = secrets.token_urlsafe(32)
    response.set_cookie(
        COOKIE_ACCESS_NAME, access_token, path="/",
        secure=secure, httponly=True, samesite="Lax",
    )
    response.set_cookie(
        COOKIE_REFRESH_NAME, refresh_token, path=_REFRESH_COOKIE_PATH,
        secure=secure, httponly=True, samesite="Lax",
    )
    response.set_cookie(
        COOKIE_CSRF_NAME, csrf_token, path="/",
        secure=secure, httponly=False, samesite="Lax",
    )


def _clear_auth_cookies(response: Any) -> None:
    """Clear all three browser auth cookies (logout)."""
    secure = cookies_are_secure(current_app.config)
    response.delete_cookie(
        COOKIE_ACCESS_NAME, path="/", secure=secure, httponly=True, samesite="Lax",
    )
    response.delete_cookie(
        COOKIE_REFRESH_NAME, path=_REFRESH_COOKIE_PATH,
        secure=secure, httponly=True, samesite="Lax",
    )
    response.delete_cookie(
        COOKIE_CSRF_NAME, path="/", secure=secure, httponly=False, samesite="Lax",
    )


def _mint_token_set(user_row: Any, roles: list[PyDALRole]) -> Any:
    """Mint an ES256 access/id token set for a user via the OIDC provider.

    Scopes are expanded from the user's role names (all gough authorization is
    scope-based). ``iss``/``aud``/``iat``/``exp`` on the ``Claims`` object are
    placeholders that satisfy validation -- the provider overrides them from its
    own config when signing. All gough data is tenant ``__default__``.
    """
    provider = current_app.config["OIDC_PROVIDER"]
    settings = current_app.config["OIDC_SETTINGS"]
    role_names = [r.name for r in roles]
    now = datetime.now(UTC)
    claims = Claims(
        sub=str(user_row.id),
        iss=settings.issuer,
        aud=[settings.audience],
        iat=now,
        exp=now + settings.token_ttl,
        scope=_expand_roles_to_scopes(role_names),
        roles=role_names,
        tenant="__default__",
        teams=[],
        ext={},
    )
    return provider.issue_token_set(claims)


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    return bcrypt.checkpw(password.encode(), password_hash.encode())


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
    """Decorator to require authentication on a route.

    Bearer validation is performed upstream by the ASGI ``OIDCAuthMiddleware``,
    which populates ``request.scope["state"]["claims"]``. This decorator loads
    the corresponding user (by the token ``sub``) and exposes it as
    ``request.user`` for handlers that read it. A request that reached here
    without validated claims is rejected 401.
    """
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        scope = cast("dict[str, Any]", request.scope)
        claims = (scope.get("state") or {}).get("claims")
        if not claims:
            return jsonify({"error": "Missing or invalid token"}), 401

        try:
            user_id = int(claims.get("sub"))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid token subject"}), 401

        # Get user from database + roles. Regression: gh-22. User fetch +
        # roles fetch is one unit of work -- off the event loop via
        # run_db() instead of blocking the request coroutine inline.
        db = get_db()
        user_datastore = current_app.user_datastore

        def _load_user() -> tuple[Any, Any]:
            row = db(db.auth_user.id == user_id).select().first()
            if not row or not row.active:
                return None, None
            return row, user_datastore._get_user_roles(row.id)

        user_row, roles = await run_db(_load_user)

        if not user_row:
            return jsonify({"error": "User not found or inactive"}), 401

        # Store user in request context
        request.user = PyDALUser(user_row, roles=roles)

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

    # Find user. Regression: gh-22. Off the event loop via run_db()
    # instead of blocking the request coroutine inline.
    db = get_db()
    user_row = await run_db(lambda: db(db.auth_user.email == email).select().first())

    if not user_row:
        return jsonify({"error": "Invalid email or password"}), 401

    # Verify password
    if not verify_password(password, user_row.password):
        return jsonify({"error": "Invalid email or password"}), 401

    # Check if user is active
    if not user_row.active:
        return jsonify({"error": "User account is inactive"}), 403

    # Regression: gh-22. Login-tracking update + commit + refresh-token
    # insert + roles fetch is one unit of work -- stays in one run_db()
    # closure per the house rule (see app/db/run_db.py).
    client_ip = request.remote_addr or "unknown"
    user_datastore = current_app.user_datastore

    def _finish_login() -> tuple[str, Any]:
        db(db.auth_user.id == user_row.id).update(
            last_login_at=user_row.current_login_at,
            current_login_at=datetime.utcnow(),
            last_login_ip=user_row.current_login_ip,
            current_login_ip=client_ip,
            login_count=user_row.login_count + 1,
        )
        db.commit()

        # generate_refresh_token() issues its own insert+commit
        refresh_token = generate_refresh_token(user_row.id)
        roles = user_datastore._get_user_roles(user_row.id)
        return refresh_token, roles

    refresh_token, roles = await run_db(_finish_login)

    # Mint the ES256 access/id token set via the OIDC provider (no DB). The
    # DB-backed bcrypt refresh token above is what the /refresh endpoint
    # verifies; the provider's own opaque refresh token is unused.
    token_set = _mint_token_set(user_row, roles)

    user = PyDALUser(user_row, roles=roles)

    # Build response. Tokens stay in the JSON body (CLI/service clients);
    # HttpOnly cookies are ALSO set for browser clients (regression: security
    # audit 2026-09-22 -- closes the localStorage/XSS exfiltration gap).
    response = await make_response(jsonify({
        "access_token": token_set.access_token,
        "id_token": token_set.id_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": token_set.expires_in,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "roles": [r.name for r in user.roles],
        },
    }), 200)
    _set_auth_cookies(response, token_set.access_token, refresh_token)
    return response


@auth_bp.route("/refresh", methods=["POST"])
async def refresh():
    """Refresh token endpoint - issue new access token.

    Request body:
        - refresh_token: previously issued refresh token (optional if the
          ``gough_refresh`` cookie is present -- regression: security audit
          2026-09-22, browser clients never see the refresh token in JS).

    Returns:
        200: {access_token}
        400: No refresh token in body or cookie
        401: Invalid refresh token
    """
    data = await request.get_json(silent=True) or {}

    body_token = (data.get("refresh_token") or "").strip()
    refresh_token = body_token or (request.cookies.get(COOKIE_REFRESH_NAME) or "").strip()

    if not refresh_token:
        return jsonify({"error": "Refresh token required"}), 400

    db = get_db()
    user_datastore = current_app.user_datastore

    # Regression: gh-22. Verify the DB refresh token, revoke it, and issue a
    # fresh one -- all one unit of work off the event loop via run_db().
    def _rotate_refresh() -> tuple[str, Any, str | None, list[PyDALRole]]:
        records = db(
            (db.auth_refresh_tokens.revoked == False)
            & (db.auth_refresh_tokens.expires_at > datetime.utcnow())
        ).select()

        matched_id = None
        user_id = None
        for record in records:
            if bcrypt.checkpw(refresh_token.encode(), record.token_hash.encode()):
                matched_id = record.id
                user_id = record.user_id
                break

        if not user_id:
            return "invalid_token", None, None, []

        user_row = db(db.auth_user.id == user_id).select().first()
        if not user_row or not user_row.active:
            return "inactive", None, None, []

        # Revoke the presented token and issue a rotated one.
        db(db.auth_refresh_tokens.id == matched_id).update(revoked=True)
        db.commit()
        new_refresh = generate_refresh_token(user_id)
        roles = user_datastore._get_user_roles(user_id)
        return "ok", user_row, new_refresh, roles

    status, user_row, new_refresh, roles = await run_db(_rotate_refresh)

    if status == "invalid_token":
        return jsonify({"error": "Invalid or expired refresh token"}), 401

    if status == "inactive" or not user_row:
        return jsonify({"error": "User not found or inactive"}), 401

    # Mint a fresh ES256 access/id token set (no DB).
    token_set = _mint_token_set(user_row, roles)

    response = await make_response(jsonify({
        "access_token": token_set.access_token,
        "id_token": token_set.id_token,
        "refresh_token": new_refresh,
        "token_type": "Bearer",
        "expires_in": token_set.expires_in,
    }), 200)
    _set_auth_cookies(response, token_set.access_token, new_refresh)
    return response


@auth_bp.route("/logout", methods=["POST"])
@require_auth
async def logout():
    """Logout endpoint - revoke refresh token and clear browser auth cookies.

    Returns:
        200: Success message
    """
    data = await request.get_json() or {}
    refresh_token = data.get("refresh_token", "") or request.cookies.get(
        COOKIE_REFRESH_NAME, ""
    )

    if refresh_token:
        # Revoke the refresh token. Regression: gh-22. Off the event loop
        # via run_db() instead of blocking the request coroutine inline.
        await run_db(lambda: revoke_refresh_token(request.user.id, refresh_token))

    response = await make_response(
        jsonify({"message": "Logged out successfully"}), 200
    )
    _clear_auth_cookies(response)
    return response


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

    # Get user from database. Regression: gh-22. Off the event loop via
    # run_db() instead of blocking the request coroutine inline.
    db = get_db()
    user_row = await run_db(lambda: db(db.auth_user.id == request.user.id).select().first())

    if not user_row:
        return jsonify({"error": "User not found"}), 404

    # Verify current password
    if not verify_password(current_password, user_row.password):
        return jsonify({"error": "Invalid current password"}), 401

    # Update password. Regression: gh-22. Update + commit is one unit of
    # work -- off the event loop via run_db() instead of blocking the
    # request coroutine inline.
    new_hash = hash_password(new_password)

    def _apply_password_change() -> None:
        db(db.auth_user.id == user_row.id).update(
            password=new_hash,
            updated_at=datetime.utcnow(),
        )
        db.commit()

    await run_db(_apply_password_change)

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

    # Find user. Regression: gh-22. Off the event loop via run_db() instead
    # of blocking the request coroutine inline.
    db = get_db()
    user_row = await run_db(lambda: db(db.auth_user.email == email).select().first())

    # Always return success for security (don't leak user existence)
    if not user_row:
        return jsonify({"message": "If email exists, reset link has been sent"}), 200

    # Generate reset token (valid for 24 hours)
    reset_token = secrets.token_urlsafe(32)
    reset_hash = bcrypt.hashpw(reset_token.encode(), bcrypt.gensalt()).decode()
    expires_at = datetime.utcnow() + timedelta(hours=24)

    # Regression: gh-22. Revoke-existing + insert-new + commit is one unit
    # of work -- stays in one run_db() closure per the house rule (see
    # app/db/run_db.py).
    def _store_reset_token() -> None:
        # Revoke any existing reset tokens for this user
        db(db.auth_password_resets.user_id == user_row.id).delete()

        # Store reset token with expiration
        db.auth_password_resets.insert(
            user_id=user_row.id,
            token_hash=reset_hash,
            expires_at=expires_at,
            used=False,
            created_at=datetime.utcnow(),
        )
        db.commit()

    await run_db(_store_reset_token)

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

    # Find valid reset token + matching user. Regression: gh-22. Off the
    # event loop via run_db() instead of blocking the request coroutine
    # inline.
    db = get_db()

    def _verify_reset_token() -> tuple[str, Any, Any]:
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
            return "invalid_token", None, None

        return "ok", db(db.auth_user.id == user_id).select().first(), token_record_id

    status, user_row, token_record_id = await run_db(_verify_reset_token)

    if status == "invalid_token":
        return jsonify({"error": "Invalid or expired reset token"}), 401

    if not user_row:
        return jsonify({"error": "User not found"}), 404

    # Regression: gh-22. Password update + mark-token-used + commit is one
    # unit of work -- stays in one run_db() closure per the house rule
    # (see app/db/run_db.py).
    new_hash = hash_password(new_password)

    def _apply_reset() -> None:
        db(db.auth_user.id == user_row.id).update(
            password=new_hash,
            updated_at=datetime.utcnow(),
        )

        # Mark reset token as used
        db(db.auth_password_resets.id == token_record_id).update(used=True)
        db.commit()

    await run_db(_apply_reset)

    return jsonify({"message": "Password reset successfully"}), 200


@auth_bp.route("/health", methods=["GET"])
async def health():
    """Health check endpoint for auth service."""
    return jsonify({"status": "healthy"}), 200
