"""Regression tests: global /api/ rate-limit floor.

regression: security audit 2026-09-22 (MEDIUM)

Only 6 of 106 input endpoints previously carried an explicit ``@rate_limit``
decorator, leaving the rest completely unbounded. These tests exercise
``app.rate_limit.install_global_rate_limiting`` directly against a minimal
Quart app (same pattern as tests/test_rate_limit.py) rather than booting the
full production app, since the floor is app-agnostic middleware.
"""

from __future__ import annotations

import pytest
from quart import Quart, jsonify


def _make_app(**config) -> Quart:
    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["RATE_LIMIT_ENABLED"] = True
    for key, value in config.items():
        app.config[key] = value

    @app.route("/api/v1/widgets")
    async def widgets():
        return jsonify({"ok": True}), 200

    @app.route("/api/v1/auth/login", methods=["POST"])
    async def login():
        return jsonify({"ok": True}), 200

    @app.route("/api/v1/agents/enroll", methods=["POST"])
    async def enroll():
        return jsonify({"ok": True}), 200

    @app.route("/healthz")
    async def health():
        return jsonify({"status": "healthy"}), 200

    @app.route("/api/v1/openapi.json")
    async def openapi_json():
        return jsonify({"ok": True}), 200

    return app


class TestGlobalRateLimitFloor:
    @pytest.mark.anyio
    async def test_nth_request_over_limit_returns_429(self):
        from app.rate_limit import init_rate_limiter, install_global_rate_limiting

        app = _make_app(RATE_LIMIT_GLOBAL_DEFAULT="2/minute")
        init_rate_limiter(app)
        install_global_rate_limiting(app)
        client = app.test_client()

        r1 = await client.get("/api/v1/widgets")
        r2 = await client.get("/api/v1/widgets")
        r3 = await client.get("/api/v1/widgets")

        assert (r1.status_code, r2.status_code) == (200, 200)
        assert r3.status_code == 429
        body = await r3.get_json()
        assert body["error"] == "rate_limit_exceeded"
        assert "Retry-After" in r3.headers

    @pytest.mark.anyio
    async def test_health_and_openapi_exempt_from_global_floor(self):
        from app.rate_limit import init_rate_limiter, install_global_rate_limiting

        # A floor so tight that any non-exempt route would 429 on request 2.
        app = _make_app(RATE_LIMIT_GLOBAL_DEFAULT="1/minute")
        init_rate_limiter(app)
        install_global_rate_limiting(app)
        client = app.test_client()

        for _ in range(5):
            resp = await client.get("/healthz")
            assert resp.status_code != 429

        for _ in range(5):
            resp = await client.get("/api/v1/openapi.json")
            assert resp.status_code != 429

    @pytest.mark.anyio
    async def test_stricter_limit_on_auth_sensitive_routes(self):
        from app.rate_limit import init_rate_limiter, install_global_rate_limiting

        app = _make_app(
            RATE_LIMIT_GLOBAL_DEFAULT="1000/minute",
            RATE_LIMIT_AUTH_SENSITIVE="2/minute",
        )
        init_rate_limiter(app)
        install_global_rate_limiting(app)
        client = app.test_client()

        statuses = [
            (await client.post("/api/v1/auth/login", json={})).status_code
            for _ in range(3)
        ]
        assert statuses == [200, 200, 429]

        # Same tight strict budget applies independently to agent enrollment.
        enroll_statuses = [
            (await client.post("/api/v1/agents/enroll", json={})).status_code
            for _ in range(3)
        ]
        assert enroll_statuses == [200, 200, 429]

    @pytest.mark.anyio
    async def test_limiter_backend_failure_fails_open_not_500(self, monkeypatch):
        from app.rate_limit import init_rate_limiter, install_global_rate_limiting

        app = _make_app()
        limiter = init_rate_limiter(app)
        install_global_rate_limiting(app)

        async def _boom(*args, **kwargs):
            raise RuntimeError("rate limiter backend unavailable")

        monkeypatch.setattr(limiter, "check_rate_limit", _boom)
        client = app.test_client()

        resp = await client.get("/api/v1/widgets")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_disabled_flag_skips_global_floor_entirely(self):
        from app.rate_limit import init_rate_limiter, install_global_rate_limiting

        app = _make_app(RATE_LIMIT_GLOBAL_DEFAULT="1/minute", RATE_LIMIT_ENABLED=False)
        init_rate_limiter(app)
        install_global_rate_limiting(app)
        client = app.test_client()

        for _ in range(5):
            resp = await client.get("/api/v1/widgets")
            assert resp.status_code != 429


class TestGlobalRateLimitApplies:
    def test_non_api_path_not_covered(self):
        from app.rate_limit import _global_rate_limit_applies

        assert _global_rate_limit_applies("/healthz") is False
        assert _global_rate_limit_applies("/readyz") is False
        assert _global_rate_limit_applies("/metrics") is False

    def test_openapi_paths_exempt(self):
        from app.rate_limit import _global_rate_limit_applies

        assert _global_rate_limit_applies("/api/v1/openapi.json") is False
        assert _global_rate_limit_applies("/api/v1/openapi.yaml") is False

    def test_ordinary_api_path_covered(self):
        from app.rate_limit import _global_rate_limit_applies

        assert _global_rate_limit_applies("/api/v1/nodes") is True
        assert _global_rate_limit_applies("/api/v1/auth/login") is True
