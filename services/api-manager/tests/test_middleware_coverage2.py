"""Coverage tests for middleware.py uncovered lines."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from quart import Quart, g, jsonify


def _make_app() -> Quart:
    app = Quart(__name__)
    app.config["JWT_SECRET_KEY"] = "test-secret-key-for-middleware-tests-32"
    app.config["TESTING"] = True
    return app


def _make_token(app: Quart, **kwargs) -> str:
    import jwt as pyjwt
    payload = {
        "sub": "1",
        "type": "access",
        "scope": "gough.cluster.admin",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    payload.update(kwargs)
    return pyjwt.encode(payload, app.config["JWT_SECRET_KEY"], algorithm="HS256")


_FAKE_ADMIN = {
    "id": 2,
    "email": "admin@example.com",
    "role": "admin",
    "is_active": True,
}


class TestAuthRequiredSyncHandler:
    """``auth_required`` calls the handler when a principal is present.

    regression: gh-31 -- the decorator no longer decodes a bearer token or loads
    a user (that moved to the ASGI gate + ``_populate_current_user`` shim); it
    only requires ``g.current_user`` to be set. The real token->principal path is
    covered end-to-end in tests/api/test_auth_e2e_gh31.py.
    """

    @pytest.mark.anyio
    async def test_handler_runs_with_present_principal(self):
        """auth_required invokes the async handler once a principal is present."""
        app = _make_app()
        from app.middleware import auth_required

        @auth_required
        async def async_handler():
            return jsonify({"msg": "response"}), 200

        async with app.test_request_context("/"):
            g.current_user = {"id": 1, "is_active": True}
            result = await async_handler()
            response, status = result
            assert status == 200


class TestRoleRequiredDecorator:
    """Test role_required decorator (scope-bundle based)."""

    @pytest.mark.anyio
    async def test_role_required_scope_bundle_match(self):
        """role_required accepts a token whose scope satisfies the role bundle."""
        app = _make_app()

        with patch("app.security.scope_enforcement.extract_scopes_from_jwt") as mock_extract:
            with patch("app.security.scope_enforcement.check_scopes") as mock_check:
                from app.middleware import role_required

                mock_extract.return_value = frozenset({"gough.cluster.admin"})
                mock_check.return_value = None

                @role_required("admin")
                async def protected():
                    return jsonify({"ok": True}), 200

                async with app.test_request_context("/"):
                    g.current_user = {**_FAKE_ADMIN, "_jwt_payload": {"scope": "gough.cluster.admin"}}
                    response, status = await protected()
                    assert status == 200
                    mock_check.assert_called()


# NOTE (regression: gh-31): the ``TestCredentialValidation`` class tested the
# deleted ``_credential_validation`` before_request hook (HS256 decode ->
# g.current_user). Authentication now happens in the ASGI ``OIDCAuthMiddleware``;
# the ``_populate_current_user`` shim reads ASGI-validated claims from
# ``request.scope["state"]["claims"]``. Valid / missing / invalid / inactive-user
# behaviour is proven end-to-end against the real gate in
# tests/api/test_auth_e2e_gh31.py, so those unit tests are removed here rather
# than kept against a deleted function.


class TestRecordRequestMetrics:
    """Test _record_request_metrics function."""

    @pytest.mark.anyio
    async def test_record_metrics_success_response(self):
        """Record metrics for successful response."""
        app = _make_app()

        from app.middleware import _record_request_metrics

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.get.return_value = "0.050"

        with patch("app.metrics.api_request_latency_seconds") as mock_latency:
            with patch("app.metrics.api_error_total") as mock_error:
                async with app.test_request_context("/api/v1/users/123", method="GET"):
                    result = await _record_request_metrics(mock_response)
                    assert result == mock_response
                    mock_latency.labels.assert_called()

    @pytest.mark.anyio
    async def test_record_metrics_error_response(self):
        """Record error metrics for 5xx response."""
        app = _make_app()

        from app.middleware import _record_request_metrics

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers.get.return_value = "1.023"

        with patch("app.metrics.api_request_latency_seconds") as mock_latency:
            with patch("app.metrics.api_error_total") as mock_error:
                async with app.test_request_context("/api/v1/users/123", method="POST"):
                    result = await _record_request_metrics(mock_response)
                    assert result == mock_response
                    mock_latency.labels.assert_called()
                    mock_error.labels.assert_called()

    @pytest.mark.anyio
    async def test_record_metrics_exception_silenced(self):
        """Exceptions in metrics recording are silenced."""
        app = _make_app()

        from app.middleware import _record_request_metrics

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("app.metrics.api_request_latency_seconds", side_effect=Exception("metric error")):
            async with app.test_request_context("/api/v1/test", method="GET"):
                result = await _record_request_metrics(mock_response)
                assert result == mock_response
