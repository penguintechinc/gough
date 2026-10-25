"""Change-password flow through the REAL auth stack (``real_auth_env``).

Asserts status codes, response bodies, and the persisted bcrypt hash for the
wrong-current, same-as-current, and success paths of
``POST /api/v1/auth/change-password``.
"""

from __future__ import annotations

from typing import Any

import bcrypt
import pytest

CURRENT_PW = "real-pass-123"


def _stored_hash(env: Any, user_id: int) -> str:
    """Read the auth_user.password column straight from the booted app's DB."""
    db = env.app.config["db"]
    row = db(db.auth_user.id == user_id).select().first()
    return str(row.password)


async def _change(env: Any, body: dict[str, Any]) -> Any:
    """POST change-password as the seeded operator with a real ES256 token."""
    token = env.mint(scope="")
    return await env.client.post(
        "/api/v1/auth/change-password",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_wrong_current_password_is_401_and_hash_untouched(
    real_auth_env: Any,
) -> None:
    """A wrong current password is refused with 401 and the stored hash is unchanged."""
    before = _stored_hash(real_auth_env, real_auth_env.user_id)

    resp = await _change(
        real_auth_env,
        {"current_password": "not-the-password", "new_password": "brand-new-pass-1"},
    )

    assert resp.status_code == 401
    assert (await resp.get_json()) == {"error": "Invalid current password"}
    assert _stored_hash(real_auth_env, real_auth_env.user_id) == before


@pytest.mark.asyncio
async def test_successful_change_persists_new_bcrypt_hash(real_auth_env: Any) -> None:
    """Correct current password + valid new password -> 200 and the stored hash verifies the new one."""
    new_pw = "brand-new-pass-2"

    resp = await _change(
        real_auth_env, {"current_password": CURRENT_PW, "new_password": new_pw}
    )

    assert resp.status_code == 200
    assert (await resp.get_json()) == {"message": "Password changed successfully"}
    stored = _stored_hash(real_auth_env, real_auth_env.user_id)
    assert bcrypt.checkpw(new_pw.encode(), stored.encode())
    assert not bcrypt.checkpw(CURRENT_PW.encode(), stored.encode())


@pytest.mark.asyncio
async def test_new_password_equal_to_current_is_rejected(real_auth_env: Any) -> None:
    """Policy: the new password must differ from the current one; reuse -> 400, hash unchanged."""
    before = _stored_hash(real_auth_env, real_auth_env.user_id)

    resp = await _change(
        real_auth_env, {"current_password": CURRENT_PW, "new_password": CURRENT_PW}
    )

    assert resp.status_code == 400
    assert (await resp.get_json()) == {
        "error": "New password must differ from current password"
    }
    assert _stored_hash(real_auth_env, real_auth_env.user_id) == before


@pytest.mark.asyncio
async def test_empty_current_password_rejected(real_auth_env: Any) -> None:
    """Empty current password is refused with 400 before any DB work."""
    resp = await _change(
        real_auth_env, {"current_password": "", "new_password": "x" * 12}
    )

    assert resp.status_code == 400
    assert (await resp.get_json()) == {"error": "Current and new passwords required"}
