"""Tests for app/rate_limit.py rate limiting module."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from quart import Quart, g, jsonify


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(**config) -> Quart:
    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["RATE_LIMIT_ENABLED"] = True
    for k, v in config.items():
        app.config[k] = v
    return app


# ---------------------------------------------------------------------------
# RateLimitInfo
# ---------------------------------------------------------------------------

class TestRateLimitInfo:
    def test_to_headers(self):
        from app.rate_limit import RateLimitInfo
        info = RateLimitInfo(limit=100, remaining=50, reset_at=datetime(2030, 1, 1, 0, 0, 0))
        headers = info.to_headers()
        assert headers["X-RateLimit-Limit"] == "100"
        assert headers["X-RateLimit-Remaining"] == "50"
        assert "X-RateLimit-Reset" in headers

    def test_remaining_clamped_to_zero(self):
        from app.rate_limit import RateLimitInfo
        info = RateLimitInfo(limit=10, remaining=-5, reset_at=datetime(2030, 1, 1))
        headers = info.to_headers()
        assert headers["X-RateLimit-Remaining"] == "0"

    def test_retry_after_default_zero(self):
        from app.rate_limit import RateLimitInfo
        info = RateLimitInfo(limit=10, remaining=5, reset_at=datetime(2030, 1, 1))
        assert info.retry_after == 0


# ---------------------------------------------------------------------------
# RateLimitExceeded
# ---------------------------------------------------------------------------

class TestRateLimitExceeded:
    def test_default_values(self):
        from app.rate_limit import RateLimitExceeded
        exc = RateLimitExceeded()
        assert exc.retry_after == 60
        assert exc.limit == 0
        assert exc.remaining == 0

    def test_custom_values(self):
        from app.rate_limit import RateLimitExceeded
        exc = RateLimitExceeded("Too many", retry_after=30, limit=100, remaining=0)
        assert exc.retry_after == 30
        assert exc.limit == 100
        assert str(exc) == "Too many"

    def test_is_exception(self):
        from app.rate_limit import RateLimitExceeded
        assert issubclass(RateLimitExceeded, Exception)


# ---------------------------------------------------------------------------
# RateLimitStrategy enum
# ---------------------------------------------------------------------------

class TestRateLimitStrategy:
    def test_enum_values(self):
        from app.rate_limit import RateLimitStrategy
        assert RateLimitStrategy.FIXED_WINDOW.value == "fixed_window"
        assert RateLimitStrategy.SLIDING_WINDOW.value == "sliding_window"
        assert RateLimitStrategy.TOKEN_BUCKET.value == "token_bucket"


# ---------------------------------------------------------------------------
# InMemoryStorage
# ---------------------------------------------------------------------------

class TestInMemoryStorage:
    def test_get_nonexistent_returns_none(self):
        from app.rate_limit import InMemoryStorage
        storage = InMemoryStorage()
        assert storage.get("missing") is None

    def test_set_and_get(self):
        from app.rate_limit import InMemoryStorage
        storage = InMemoryStorage()
        storage.set("mykey", {"count": 5}, ttl=60)
        result = storage.get("mykey")
        assert result is not None
        assert result["count"] == 5

    def test_incr_creates_new_key(self):
        from app.rate_limit import InMemoryStorage
        storage = InMemoryStorage()
        count = storage.incr("newkey", ttl=60)
        assert count == 1

    def test_incr_increments_existing(self):
        from app.rate_limit import InMemoryStorage
        storage = InMemoryStorage()
        storage.incr("k", ttl=60)
        storage.incr("k", ttl=60)
        count = storage.incr("k", ttl=60)
        assert count == 3

    def test_expired_entry_returns_none(self):
        from app.rate_limit import InMemoryStorage
        storage = InMemoryStorage()
        storage.set("expkey", {"count": 1}, ttl=-1)  # Already expired
        assert storage.get("expkey") is None

    def test_cleanup_removes_expired(self):
        from app.rate_limit import InMemoryStorage
        storage = InMemoryStorage()
        storage.set("exp1", {"count": 1}, ttl=-5)
        storage.set("valid", {"count": 3}, ttl=60)
        storage._last_cleanup = 0  # Force cleanup on next get
        storage._cleanup()
        assert storage.get("valid") is not None


# ---------------------------------------------------------------------------
# RedisStorage
# ---------------------------------------------------------------------------

class TestRedisStorage:
    def test_get_returns_none_when_redis_empty(self):
        from app.rate_limit import RedisStorage
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        storage = RedisStorage(mock_redis)
        assert storage.get("somekey") is None

    def test_get_parses_json(self):
        import json
        from app.rate_limit import RedisStorage
        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps({"count": 7})
        storage = RedisStorage(mock_redis)
        result = storage.get("somekey")
        assert result == {"count": 7}

    def test_set_calls_setex(self):
        from app.rate_limit import RedisStorage
        mock_redis = MagicMock()
        storage = RedisStorage(mock_redis)
        storage.set("k", {"count": 1}, ttl=30)
        mock_redis.setex.assert_called_once()

    def test_incr_uses_pipeline(self):
        from app.rate_limit import RedisStorage
        mock_redis = MagicMock()
        pipe = MagicMock()
        pipe.execute.return_value = [5, True]
        mock_redis.pipeline.return_value = pipe
        storage = RedisStorage(mock_redis)
        result = storage.incr("k", ttl=60)
        assert result == 5
        pipe.incr.assert_called_once()
        pipe.expire.assert_called_once()

    def test_key_prefix(self):
        from app.rate_limit import RedisStorage
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        storage = RedisStorage(mock_redis)
        storage.get("testkey")
        mock_redis.get.assert_called_with("gough:ratelimit:testkey")


# ---------------------------------------------------------------------------
# RateLimiter.init_app
# ---------------------------------------------------------------------------

class TestRateLimiterInitApp:
    def test_init_with_memory_storage(self):
        from app.rate_limit import RateLimiter, InMemoryStorage
        app = _make_app()
        limiter = RateLimiter()
        limiter.init_app(app)
        assert isinstance(limiter._storage, InMemoryStorage)
        assert app.extensions["rate_limiter"] is limiter

    def test_init_disabled(self):
        from app.rate_limit import RateLimiter
        app = _make_app(RATE_LIMIT_ENABLED=False)
        limiter = RateLimiter(app)
        assert limiter._enabled is False

    def test_init_enabled_by_default(self):
        from app.rate_limit import RateLimiter
        app = _make_app()
        limiter = RateLimiter(app)
        assert limiter._enabled is True

    def test_init_with_redis_falls_back_on_error(self):
        from app.rate_limit import RateLimiter, InMemoryStorage
        app = _make_app(RATE_LIMIT_REDIS_URL="redis://bad-host:6379/0")
        limiter = RateLimiter(app)
        assert isinstance(limiter._storage, InMemoryStorage)

    def test_default_limits_parsed_from_config(self):
        from app.rate_limit import RateLimiter
        app = _make_app(RATE_LIMIT_DEFAULT="50/minute")
        limiter = RateLimiter(app)
        assert limiter._default_limits == [(50, 60)]

    def test_extensions_dict_created_if_missing(self):
        from app.rate_limit import RateLimiter
        app = _make_app()
        if hasattr(app, "extensions"):
            del app.extensions
        limiter = RateLimiter(app)
        assert hasattr(app, "extensions")
        assert "rate_limiter" in app.extensions


# ---------------------------------------------------------------------------
# RateLimiter._parse_limit_string
# ---------------------------------------------------------------------------

class TestParseLimitString:
    def test_per_second(self):
        from app.rate_limit import RateLimiter
        limiter = RateLimiter()
        assert limiter._parse_limit_string("10/second") == [(10, 1)]

    def test_per_minute(self):
        from app.rate_limit import RateLimiter
        limiter = RateLimiter()
        assert limiter._parse_limit_string("100/minute") == [(100, 60)]

    def test_per_hour(self):
        from app.rate_limit import RateLimiter
        limiter = RateLimiter()
        assert limiter._parse_limit_string("500/hour") == [(500, 3600)]

    def test_per_day(self):
        from app.rate_limit import RateLimiter
        limiter = RateLimiter()
        assert limiter._parse_limit_string("1000/day") == [(1000, 86400)]

    def test_multiple_tiers_semicolon(self):
        from app.rate_limit import RateLimiter
        limiter = RateLimiter()
        result = limiter._parse_limit_string("5/second;100/minute")
        assert (5, 1) in result
        assert (100, 60) in result

    def test_invalid_string_returns_default(self):
        from app.rate_limit import RateLimiter
        limiter = RateLimiter()
        result = limiter._parse_limit_string("invalid")
        assert result == [(100, 60)]

    def test_plural_seconds(self):
        from app.rate_limit import RateLimiter
        limiter = RateLimiter()
        result = limiter._parse_limit_string("10/seconds")
        assert result == [(10, 1)]

    def test_plural_minutes(self):
        from app.rate_limit import RateLimiter
        limiter = RateLimiter()
        result = limiter._parse_limit_string("60/minutes")
        assert result == [(60, 60)]


# ---------------------------------------------------------------------------
# RateLimiter._get_identifier
# ---------------------------------------------------------------------------

class TestGetIdentifier:
    @pytest.mark.anyio
    async def test_uses_current_user_id(self):
        from app.rate_limit import RateLimiter
        app = _make_app()
        limiter = RateLimiter(app)
        async with app.test_request_context("/"):
            g.current_user = {"id": 42}
            result = await limiter._get_identifier()
            assert result == "user:42"

    @pytest.mark.anyio
    async def test_falls_back_to_remote_addr(self):
        from app.rate_limit import RateLimiter
        app = _make_app()
        limiter = RateLimiter(app)
        async with app.test_request_context("/"):
            # remote_addr may be None or 127.0.0.1 in test context; just verify no crash
            result = await limiter._get_identifier()
            assert result.startswith("ip:")

    @pytest.mark.anyio
    async def test_uses_x_forwarded_for(self):
        from app.rate_limit import RateLimiter
        app = _make_app()
        limiter = RateLimiter(app)
        async with app.test_request_context(
            "/", headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"}
        ):
            result = await limiter._get_identifier()
            assert "10.0.0.1" in result

    @pytest.mark.anyio
    async def test_uses_x_real_ip(self):
        from app.rate_limit import RateLimiter
        app = _make_app()
        limiter = RateLimiter(app)
        async with app.test_request_context(
            "/", headers={"X-Real-IP": "192.168.1.1"}
        ):
            result = await limiter._get_identifier()
            assert "192.168.1.1" in result


# ---------------------------------------------------------------------------
# RateLimiter._get_endpoint_key
# ---------------------------------------------------------------------------

class TestGetEndpointKey:
    @pytest.mark.anyio
    async def test_returns_method_endpoint(self):
        from app.rate_limit import RateLimiter
        app = _make_app()
        limiter = RateLimiter(app)
        async with app.test_request_context("/test"):
            result = limiter._get_endpoint_key()
            assert "GET" in result

    @pytest.mark.anyio
    async def test_unknown_endpoint(self):
        from app.rate_limit import RateLimiter
        app = _make_app()
        limiter = RateLimiter(app)
        async with app.test_request_context("/"):
            result = limiter._get_endpoint_key()
            assert "unknown" in result or "GET" in result


# ---------------------------------------------------------------------------
# RateLimiter.check_rate_limit
# ---------------------------------------------------------------------------

class TestCheckRateLimit:
    @pytest.mark.anyio
    async def test_disabled_returns_empty_info(self):
        from app.rate_limit import RateLimiter
        app = _make_app(RATE_LIMIT_ENABLED=False)
        limiter = RateLimiter(app)
        async with app.test_request_context(
            "/"
        ):
            info = await limiter.check_rate_limit()
            assert info.limit == 0

    @pytest.mark.anyio
    async def test_first_request_passes_and_decrements_remaining(self):
        from app.rate_limit import RateLimiter
        app = _make_app(RATE_LIMIT_DEFAULT="100/minute")
        limiter = RateLimiter(app)
        async with app.test_request_context(
            "/test"
        ):
            info = await limiter.check_rate_limit(limits=[(100, 60)])
            assert info.remaining == 99
            assert info.limit == 100

    @pytest.mark.anyio
    async def test_rate_limit_exceeded_raises(self):
        from app.rate_limit import RateLimiter, RateLimitExceeded
        app = _make_app()
        limiter = RateLimiter(app)
        # Mock the storage to report a count above the limit
        limiter._storage.incr = MagicMock(return_value=999)
        async with app.test_request_context(
            "/test"
        ):
            with pytest.raises(RateLimitExceeded):
                await limiter.check_rate_limit(limits=[(5, 60)])

    @pytest.mark.anyio
    async def test_uses_custom_key_prefix(self):
        from app.rate_limit import RateLimiter
        app = _make_app(RATE_LIMIT_DEFAULT="100/minute")
        limiter = RateLimiter(app)
        async with app.test_request_context(
            "/test"
        ):
            info = await limiter.check_rate_limit(
                limits=[(100, 60)], key_prefix="custom"
            )
            assert info.limit == 100


# ---------------------------------------------------------------------------
# get_rate_limiter
# ---------------------------------------------------------------------------

class TestGetRateLimiter:
    def test_returns_none_outside_app_context(self):
        from app.rate_limit import get_rate_limiter
        assert get_rate_limiter() is None

    @pytest.mark.anyio
    async def test_returns_limiter_inside_app_context(self):
        from app.rate_limit import RateLimiter, get_rate_limiter
        app = _make_app()
        limiter = RateLimiter(app)
        async with app.app_context():
            result = get_rate_limiter()
            assert result is limiter


# ---------------------------------------------------------------------------
# init_rate_limiter
# ---------------------------------------------------------------------------

class TestInitRateLimiter:
    def test_init_rate_limiter_returns_limiter(self):
        from app.rate_limit import init_rate_limiter, RateLimiter
        app = _make_app()
        limiter = init_rate_limiter(app)
        assert isinstance(limiter, RateLimiter)
        assert app.extensions["rate_limiter"] is limiter


# ---------------------------------------------------------------------------
# exempt_admin
# ---------------------------------------------------------------------------

class TestExemptAdmin:
    @pytest.mark.anyio
    async def test_exempt_true_for_admin_user(self):
        from app.rate_limit import exempt_admin
        app = _make_app()
        async with app.test_request_context("/"):
            g.current_user = {"role": "admin", "id": 1}
            assert exempt_admin() is True

    @pytest.mark.anyio
    async def test_exempt_false_for_viewer(self):
        from app.rate_limit import exempt_admin
        app = _make_app()
        async with app.test_request_context("/"):
            g.current_user = {"role": "viewer", "id": 2}
            assert exempt_admin() is False

    @pytest.mark.anyio
    async def test_exempt_false_when_no_user(self):
        from app.rate_limit import exempt_admin
        app = _make_app()
        async with app.test_request_context("/"):
            assert exempt_admin() is False


# ---------------------------------------------------------------------------
# rate_limit decorator
# ---------------------------------------------------------------------------

class TestRateLimitDecorator:
    def test_returns_callable_decorator(self):
        from app.rate_limit import rate_limit
        decorator = rate_limit("10/second")
        assert callable(decorator)

    def test_no_args_returns_callable(self):
        from app.rate_limit import rate_limit
        decorator = rate_limit()
        assert callable(decorator)

    @pytest.mark.anyio
    async def test_passthrough_when_no_limiter(self):
        """When no rate limiter attached to app, function is called directly."""
        from app.rate_limit import rate_limit
        app = _make_app()

        @rate_limit("10/minute")
        async def my_view():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            result = await my_view()
            assert result[1] == 200

    @pytest.mark.anyio
    async def test_exempt_when_returns_true(self):
        from app.rate_limit import rate_limit, RateLimiter
        app = _make_app()
        RateLimiter(app)

        @rate_limit("1/minute", exempt_when=lambda: True)
        async def my_view():
            return jsonify({"ok": True}), 200

        async with app.test_request_context(
            "/"
        ):
            async with app.app_context():
                result = await my_view()
            assert result[1] == 200

    @pytest.mark.anyio
    async def test_with_custom_key_func(self):
        from app.rate_limit import rate_limit
        app = _make_app()

        @rate_limit("100/minute", key_func=lambda: "custom-key")
        async def my_view():
            return jsonify({"ok": True}), 200

        async with app.test_request_context("/"):
            result = await my_view()
            assert result[1] == 200


# ---------------------------------------------------------------------------
# rate_limit_by_ip / rate_limit_by_user / rate_limit_by_api_key decorators
# ---------------------------------------------------------------------------

class TestRateLimitHelperDecorators:
    def test_rate_limit_by_ip_callable(self):
        from app.rate_limit import rate_limit_by_ip
        assert callable(rate_limit_by_ip("50/minute"))

    def test_rate_limit_by_user_callable(self):
        from app.rate_limit import rate_limit_by_user
        assert callable(rate_limit_by_user("200/minute"))

    def test_rate_limit_by_api_key_callable(self):
        from app.rate_limit import rate_limit_by_api_key
        assert callable(rate_limit_by_api_key("500/hour"))

    @pytest.mark.anyio
    async def test_rate_limit_by_user_with_authenticated_user(self):
        from app.rate_limit import rate_limit_by_user
        app = _make_app()

        @rate_limit_by_user("10/minute")
        async def view():
            return jsonify({}), 200

        async with app.test_request_context(
            "/"
        ):
            g.current_user = {"id": 99}
            result = await view()
            assert result[1] == 200

    @pytest.mark.anyio
    async def test_rate_limit_by_user_anonymous_fallback(self):
        from app.rate_limit import rate_limit_by_user
        app = _make_app()

        @rate_limit_by_user("10/minute")
        async def view():
            return jsonify({}), 200

        async with app.test_request_context(
            "/"
        ):
            result = await view()
            assert result[1] == 200

    @pytest.mark.anyio
    async def test_rate_limit_by_api_key_with_key(self):
        from app.rate_limit import rate_limit_by_api_key
        app = _make_app()

        @rate_limit_by_api_key("100/hour")
        async def view():
            return jsonify({}), 200

        async with app.test_request_context(
            "/",
            headers={"X-API-Key": "secret-key-123"},
        ):
            result = await view()
            assert result[1] == 200

    @pytest.mark.anyio
    async def test_rate_limit_by_api_key_without_key(self):
        from app.rate_limit import rate_limit_by_api_key
        app = _make_app()

        @rate_limit_by_api_key("100/hour")
        async def view():
            return jsonify({}), 200

        async with app.test_request_context(
            "/"
        ):
            result = await view()
            assert result[1] == 200

    @pytest.mark.anyio
    async def test_rate_limit_by_ip_key_func(self):
        from app.rate_limit import rate_limit_by_ip
        app = _make_app()

        @rate_limit_by_ip("50/minute")
        async def view():
            return jsonify({}), 200

        async with app.test_request_context(
            "/"
        ):
            result = await view()
            assert result[1] == 200
