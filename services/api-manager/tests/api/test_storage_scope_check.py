"""Scope check used by the storage endpoints (``app.api.storage._user_has_scope``).

The scope claim may arrive as a space-delimited string or a JSON list; anything
else, or a missing identity, must deny. Exercised inside a real request context
so ``g.current_user`` is read exactly as in production.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.api.storage import _user_has_scope
from quart import Quart, g


async def _check(user: Any, scope: str) -> bool:
    """Evaluate ``_user_has_scope`` with ``g.current_user`` set to ``user``."""
    app = Quart(__name__)
    async with app.app_context():
        g.current_user = user
        return _user_has_scope(scope)


@pytest.mark.parametrize(
    ("payload_scope", "expected"),
    [
        ("gough.storage.read gough.storage.write", True),
        ("gough.storage.write", False),
        ("", False),
    ],
)
async def test_space_delimited_scope_string(payload_scope: str, expected: bool) -> None:
    """A scope string grants exactly the listed scopes."""
    user = {"_jwt_payload": {"scope": payload_scope}}

    assert await _check(user, "gough.storage.read") is expected


async def test_list_scope_claim_grants_listed_scope() -> None:
    """A list-valued scope claim is honoured, ignoring non-string entries."""
    user = {"_jwt_payload": {"scope": ["gough.storage.write", 7, None]}}

    assert await _check(user, "gough.storage.write") is True
    assert await _check(user, "gough.storage.read") is False


async def test_unsupported_scope_type_denies() -> None:
    """A scope claim that is neither string nor list grants nothing."""
    user = {"_jwt_payload": {"scope": {"gough.storage.read": True}}}

    assert await _check(user, "gough.storage.read") is False


async def test_missing_identity_denies() -> None:
    """No authenticated user means no scopes, so the check fails closed."""
    assert await _check(None, "gough.storage.read") is False
    assert await _check({}, "gough.storage.read") is False
