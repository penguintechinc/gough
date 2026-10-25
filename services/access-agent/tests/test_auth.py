"""Behaviour tests for JWT expiry checks and token refresh in AgentAuth."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import jwt
import pytest
from agent.auth import AgentAuth, AuthError
from agent.config import AgentConfig
from agent.enrollment import AgentEnrollment, EnrollmentError


def make_token(exp_offset_s: float | None, sub: str | None = "agent:abc-123") -> str:
    """Build an unsigned-for-test JWT whose exp is now plus the given offset.

    Args:
        exp_offset_s: Seconds from now until expiry, or None to omit ``exp``.
        sub: Subject claim, or None to omit it.

    Returns:
        Encoded JWT string.
    """
    claims: dict[str, Any] = {}
    if exp_offset_s is not None:
        claims["exp"] = int(time.time() + exp_offset_s)
    if sub is not None:
        claims["sub"] = sub
    return jwt.encode(claims, "test-secret", algorithm="HS256")


def build_auth(
    token: str | None, refresh_sets: str | None = None
) -> tuple[AgentAuth, MagicMock]:
    """Create an AgentAuth whose enrollment refresh is a recording fake.

    Args:
        token: Initial access token placed on the config (None for no token).
        refresh_sets: If given, the fake refresh stores this value as the new access token.

    Returns:
        Tuple of (auth handler, enrollment mock).
    """
    config = AgentConfig(access_token=token, refresh_token="refresh-1")
    enrollment = MagicMock(spec=AgentEnrollment)

    def _refresh() -> tuple[str, str]:
        if refresh_sets is not None:
            config.access_token = refresh_sets
        return (config.access_token or "", "refresh-2")

    enrollment.refresh_tokens.side_effect = _refresh
    return AgentAuth(config, enrollment), enrollment


def test_headers_without_token_raise() -> None:
    """get_auth_headers refuses to build headers when no access token is set."""
    auth, enrollment = build_auth(None)
    with pytest.raises(AuthError, match="No access token"):
        auth.get_auth_headers()
    enrollment.refresh_tokens.assert_not_called()


def test_headers_use_current_token_when_far_from_expiry() -> None:
    """A token with plenty of lifetime is used as-is without refreshing."""
    token = make_token(3600)
    auth, enrollment = build_auth(token)
    assert auth.get_auth_headers() == {"Authorization": f"Bearer {token}"}
    enrollment.refresh_tokens.assert_not_called()


def test_headers_refresh_when_within_five_minutes_of_expiry() -> None:
    """A token expiring inside the 5-minute window is refreshed before use."""
    fresh = make_token(3600)
    auth, enrollment = build_auth(make_token(120), refresh_sets=fresh)
    headers = auth.get_auth_headers()
    enrollment.refresh_tokens.assert_called_once()
    assert headers == {"Authorization": f"Bearer {fresh}"}


def test_headers_refresh_when_token_already_expired() -> None:
    """An expired token triggers a refresh."""
    fresh = make_token(3600)
    auth, enrollment = build_auth(make_token(-60), refresh_sets=fresh)
    auth.get_auth_headers()
    enrollment.refresh_tokens.assert_called_once()


def test_headers_refresh_when_token_has_no_exp_claim() -> None:
    """A token without exp cannot be trusted for lifetime, so it is refreshed."""
    fresh = make_token(3600)
    auth, enrollment = build_auth(make_token(None), refresh_sets=fresh)
    auth.get_auth_headers()
    enrollment.refresh_tokens.assert_called_once()


def test_headers_refresh_when_token_is_malformed() -> None:
    """A non-JWT access token is treated as needing refresh."""
    fresh = make_token(3600)
    auth, enrollment = build_auth("not-a-jwt", refresh_sets=fresh)
    auth.get_auth_headers()
    enrollment.refresh_tokens.assert_called_once()


def test_refresh_failure_surfaces_as_auth_error() -> None:
    """An EnrollmentError during refresh is re-raised as AuthError."""
    auth, enrollment = build_auth(make_token(-60))
    enrollment.refresh_tokens.side_effect = EnrollmentError("revoked")
    with pytest.raises(AuthError, match="Token refresh failed: revoked"):
        auth.get_auth_headers()


def test_expiry_check_is_timezone_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token valid for an hour is not refreshed regardless of the local timezone.

    Regression guard: exp must be compared as an absolute UTC instant. TZ is undone
    before tzset so later tests see the original zone.
    """
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        auth, enrollment = build_auth(make_token(3600))
        assert auth.validate_access_token() is True
        auth.get_auth_headers()
        enrollment.refresh_tokens.assert_not_called()
    finally:
        monkeypatch.undo()
        time.tzset()


def test_validate_access_token_states() -> None:
    """validate_access_token reports False for missing, expired, exp-less or malformed tokens."""
    assert build_auth(None)[0].validate_access_token() is False
    assert build_auth(make_token(3600))[0].validate_access_token() is True
    assert build_auth(make_token(-60))[0].validate_access_token() is False
    assert build_auth(make_token(None))[0].validate_access_token() is False
    assert build_auth("garbage")[0].validate_access_token() is False


def test_get_agent_id_strips_agent_prefix() -> None:
    """Agent subjects of the form agent:<uuid> yield the bare uuid."""
    assert (
        build_auth(make_token(3600, sub="agent:abc-123"))[0].get_agent_id_from_token()
        == "abc-123"
    )


def test_get_agent_id_returns_plain_subject_unchanged() -> None:
    """Subjects without the agent: prefix are returned verbatim."""
    assert (
        build_auth(make_token(3600, sub="user-9"))[0].get_agent_id_from_token()
        == "user-9"
    )


def test_get_agent_id_empty_subject_returns_empty_string() -> None:
    """A token with no sub claim yields an empty id rather than None."""
    assert build_auth(make_token(3600, sub=None))[0].get_agent_id_from_token() == ""


def test_get_agent_id_none_without_token_or_when_malformed() -> None:
    """Missing or malformed tokens yield None."""
    assert build_auth(None)[0].get_agent_id_from_token() is None
    assert build_auth("garbage")[0].get_agent_id_from_token() is None
