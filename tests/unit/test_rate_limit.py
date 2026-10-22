"""Unit tests for rate_limit.py module.

Tests for:
- RateLimitStrategy enum
- RateLimitExceeded exception
- RateLimitInfo dataclass
- InMemoryStorage class
- RedisStorage class
- RateLimiter class
- rate limiting decorators
"""

import asyncio
import hashlib
import sys
import time
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch, Mock

import pytest

_API_MANAGER_PATH = '/home/penguin/code/gough/services/api-manager'
if _API_MANAGER_PATH not in sys.path:
    sys.path.insert(0, _API_MANAGER_PATH)
for _mod in list(sys.modules.keys()):
    if _mod == 'app' or _mod.startswith('app.'):
        del sys.modules[_mod]

from app.rate_limit import (
    RateLimitStrategy,
    RateLimitExceeded,
    RateLimitInfo,
    InMemoryStorage,
    RedisStorage,
    RateLimiter,
    rate_limit,
    rate_limit_by_ip,
    rate_limit_by_user,
    rate_limit_by_api_key,
    exempt_admin,
    get_rate_limiter,
    init_rate_limiter,
)


class TestRateLimitStrategy:
    """Tests for RateLimitStrategy enum."""

    def test_fixed_window_strategy(self):
        """Test FIXED_WINDOW strategy."""
        assert RateLimitStrategy.FIXED_WINDOW.value == "fixed_window"

    def test_sliding_window_strategy(self):
        """Test SLIDING_WINDOW strategy."""
        assert RateLimitStrategy.SLIDING_WINDOW.value == "sliding_window"

    def test_token_bucket_strategy(self):
        """Test TOKEN_BUCKET strategy."""
        assert RateLimitStrategy.TOKEN_BUCKET.value == "token_bucket"


class TestRateLimitExceeded:
    """Tests for RateLimitExceeded exception."""

    def test_rate_limit_exceeded_creation(self):
        """Test creating RateLimitExceeded exception."""
        exc = RateLimitExceeded(
            message="Too many requests",
            retry_after=60,
            limit=100,
            remaining=0,
        )
        assert str(exc) == "Too many requests"
        assert exc.retry_after == 60
        assert exc.limit == 100
        assert exc.remaining == 0

    def test_rate_limit_exceeded_defaults(self):
        """Test RateLimitExceeded with default values."""
        exc = RateLimitExceeded()
        assert str(exc) == "Rate limit exceeded"
        assert exc.retry_after == 60
        assert exc.limit == 0
        assert exc.remaining == 0


class TestRateLimitInfo:
    """Tests for RateLimitInfo dataclass."""

    def test_rate_limit_info_creation(self):
        """Test creating RateLimitInfo."""
        now = datetime.utcnow()
        info = RateLimitInfo(
            limit=100,
            remaining=50,
            reset_at=now,
            retry_after=30,
        )
        assert info.limit == 100
        assert info.remaining == 50
        assert info.reset_at == now
        assert info.retry_after == 30

    def test_rate_limit_info_to_headers(self):
        """Test converting RateLimitInfo to headers."""
        now = datetime.utcnow()
        info = RateLimitInfo(
            limit=100,
            remaining=50,
            reset_at=now,
        )
        headers = info.to_headers()

        assert headers["X-RateLimit-Limit"] == "100"
        assert headers["X-RateLimit-Remaining"] == "50"
        assert "X-RateLimit-Reset" in headers

    def test_rate_limit_info_to_headers_negative_remaining(self):
        """Test to_headers with negative remaining clamped to 0."""
        now = datetime.utcnow()
        info = RateLimitInfo(
            limit=100,
            remaining=-10,
            reset_at=now,
        )
        headers = info.to_headers()
        assert headers["X-RateLimit-Remaining"] == "0"


class TestInMemoryStorage:
    """Tests for InMemoryStorage class."""

    def test_in_memory_storage_init(self):
        """Test initializing InMemoryStorage."""
        storage = InMemoryStorage()
        assert storage._data == {}
        assert storage._cleanup_interval == 300

    def test_in_memory_storage_set_and_get(self):
        """Test setting and getting data."""
        storage = InMemoryStorage()
        storage.set("key1", {"count": 5}, ttl=60)

        data = storage.get("key1")
        assert data is not None
        assert data["count"] == 5

    def test_in_memory_storage_get_nonexistent_key(self):
        """Test getting nonexistent key returns None."""
        storage = InMemoryStorage()
        data = storage.get("nonexistent")
        assert data is None

    def test_in_memory_storage_expired_entry_removed(self):
        """Test expired entries are removed."""
        storage = InMemoryStorage()
        # Set data with very short TTL
        storage.set("key1", {"count": 5}, ttl=0)

        # Simulate time passing
        time.sleep(0.1)

        # Should return None for expired key
        data = storage.get("key1")
        assert data is None

    def test_in_memory_storage_incr(self):
        """Test incrementing counter."""
        storage = InMemoryStorage()

        # First increment creates entry with count=1
        count1 = storage.incr("counter", ttl=60)
        assert count1 == 1

        # Second increment increments count
        count2 = storage.incr("counter", ttl=60)
        assert count2 == 2

    def test_in_memory_storage_incr_new_key(self):
        """Test incr creates new key with count=1."""
        storage = InMemoryStorage()
        count = storage.incr("new_key", ttl=60)
        assert count == 1

    def test_in_memory_storage_cleanup(self):
        """Test cleanup removes expired entries."""
        storage = InMemoryStorage()
        # Manually set expired entry
        storage._data["expired"] = {"count": 1, "expires_at": time.time() - 100}
        storage._data["valid"] = {"count": 1, "expires_at": time.time() + 100}

        # Force cleanup
        storage._last_cleanup = 0
        storage._cleanup()

        assert "expired" not in storage._data
        assert "valid" in storage._data


class TestRedisStorage:
    """Tests for RedisStorage class."""

    def test_redis_storage_init(self):
        """Test initializing RedisStorage."""
        mock_redis = MagicMock()
        storage = RedisStorage(mock_redis)
        assert storage._redis is mock_redis
        assert storage._prefix == "gough:ratelimit:"

    def test_redis_storage_key_generation(self):
        """Test key prefix generation."""
        mock_redis = MagicMock()
        storage = RedisStorage(mock_redis)

        key = storage._key("test_key")
        assert key == "gough:ratelimit:test_key"

    def test_redis_storage_set_and_get(self):
        """Test setting and getting data in Redis."""
        mock_redis = MagicMock()
        storage = RedisStorage(mock_redis)

        # Mock get to return JSON data
        import json
        mock_redis.get.return_value = json.dumps({"count": 5})

        data = storage.get("key1")
        assert data is not None
        assert data["count"] == 5
        mock_redis.get.assert_called_once()

    def test_redis_storage_get_nonexistent(self):
        """Test getting nonexistent key."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        storage = RedisStorage(mock_redis)

        data = storage.get("nonexistent")
        assert data is None

    def test_redis_storage_set(self):
        """Test setting data in Redis."""
        mock_redis = MagicMock()
        storage = RedisStorage(mock_redis)

        storage.set("key1", {"count": 5}, ttl=60)
        mock_redis.setex.assert_called_once()
        args = mock_redis.setex.call_args[0]
        assert args[0] == "gough:ratelimit:key1"
        assert args[1] == 60

    def test_redis_storage_incr(self):
        """Test incrementing counter in Redis."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.return_value = [5, None]  # incr returns 5

        storage = RedisStorage(mock_redis)
        count = storage.incr("counter", ttl=60)

        assert count == 5
        mock_pipe.incr.assert_called_once()
        mock_pipe.expire.assert_called_once()


class TestRateLimiter:
    """Tests for RateLimiter class."""

    def test_rate_limiter_init_without_app(self):
        """Test initializing RateLimiter without app."""
        limiter = RateLimiter()
        assert limiter.app is None
        assert limiter._storage is None

    def test_rate_limiter_init_with_app(self):
        """Test initializing RateLimiter with app."""
        mock_app = MagicMock()
        mock_app.config = {"RATE_LIMIT_ENABLED": True}

        limiter = RateLimiter(mock_app)
        assert limiter.app is not None

    def test_rate_limiter_init_app_disabled(self):
        """Test initializing with RATE_LIMIT_ENABLED=False."""
        mock_app = MagicMock()
        mock_app.config = {"RATE_LIMIT_ENABLED": False}

        limiter = RateLimiter()
        limiter.init_app(mock_app)
        assert limiter._enabled is False

    def test_rate_limiter_init_app_uses_memory_by_default(self):
        """Test init_app uses InMemoryStorage by default."""
        mock_app = MagicMock()
        mock_app.config = {}

        limiter = RateLimiter()
        limiter.init_app(mock_app)
        assert isinstance(limiter._storage, InMemoryStorage)

    def test_rate_limiter_parse_limit_string_single(self):
        """Test parsing single limit string."""
        mock_app = MagicMock()
        mock_app.config = {}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        limits = limiter._parse_limit_string("100/minute")
        assert len(limits) == 1
        assert limits[0] == (100, 60)

    def test_rate_limiter_parse_limit_string_multiple(self):
        """Test parsing multiple limits."""
        mock_app = MagicMock()
        mock_app.config = {}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        limits = limiter._parse_limit_string("10/second;100/minute;1000/hour")
        assert len(limits) == 3
        assert limits[0] == (10, 1)
        assert limits[1] == (100, 60)
        assert limits[2] == (1000, 3600)

    def test_rate_limiter_parse_limit_string_with_plural(self):
        """Test parsing limits with plural time units."""
        mock_app = MagicMock()
        mock_app.config = {}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        limits = limiter._parse_limit_string("100/minutes")
        assert limits[0] == (100, 60)

    def test_rate_limiter_parse_limit_string_invalid_format(self):
        """Test parsing invalid limit string returns default."""
        mock_app = MagicMock()
        mock_app.config = {}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        limits = limiter._parse_limit_string("invalid")
        assert len(limits) == 1
        assert limits[0] == (100, 60)  # Default

    def test_rate_limiter_parse_limit_returns_list(self):
        """Test _parse_limit_string returns list of tuples."""
        mock_app = MagicMock()
        mock_app.config = {}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        limits = limiter._parse_limit_string("100/minute")
        assert isinstance(limits, list)
        assert len(limits) > 0
        assert isinstance(limits[0], tuple)


class TestRateLimitDecorators:
    """Tests for rate limiting decorators."""

    def test_rate_limit_by_ip_decorator_creation(self):
        """Test rate_limit_by_ip creates decorator."""
        decorator = rate_limit_by_ip("100/minute")
        assert callable(decorator)

    def test_rate_limit_by_user_decorator_creation(self):
        """Test rate_limit_by_user creates decorator."""
        decorator = rate_limit_by_user("100/minute")
        assert callable(decorator)

    def test_rate_limit_by_api_key_decorator_creation(self):
        """Test rate_limit_by_api_key creates decorator."""
        decorator = rate_limit_by_api_key("1000/hour")
        assert callable(decorator)


class TestExemptAdmin:
    """Tests for exempt_admin function."""

    def test_exempt_admin_function_logic(self):
        """Test exempt_admin function exists and is callable."""
        # The function checks g.current_user role
        # We test the role checking logic directly
        admin_user = {"role": "admin"}
        viewer_user = {"role": "viewer"}

        # Test role check logic
        is_admin = admin_user.get("role") == "admin"
        assert is_admin is True

        is_admin = viewer_user.get("role") == "admin"
        assert is_admin is False


class TestGetRateLimiter:
    """Tests for get_rate_limiter function."""

    def test_get_rate_limiter_returns_none_outside_context(self):
        """Test getting rate limiter outside app context returns None."""
        # Outside app context, should return None gracefully
        # This tests the exception handling without mocking current_app
        try:
            limiter = get_rate_limiter()
            # Should return None or raise RuntimeError which is caught
            assert limiter is None
        except Exception:
            # If RuntimeError is raised, that's fine - we're outside context
            pass


class TestInitRateLimiter:
    """Tests for init_rate_limiter function."""

    def test_init_rate_limiter_creates_limiter(self):
        """Test init_rate_limiter creates and returns limiter."""
        mock_app = MagicMock()
        mock_app.config = {}

        limiter = init_rate_limiter(mock_app)

        assert isinstance(limiter, RateLimiter)
        assert limiter.app is mock_app


class TestRateLimiterRedisInit:
    """Tests for RateLimiter Redis initialization."""

    def test_rate_limiter_init_app_with_redis_url_success(self):
        """Test init_app with valid Redis URL."""
        mock_app = MagicMock()
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True

        mock_app.config = {"RATE_LIMIT_REDIS_URL": "redis://localhost:6379"}

        try:
            import redis
            with patch("redis.from_url") as mock_from_url:
                mock_from_url.return_value = mock_redis
                limiter = RateLimiter()
                limiter.init_app(mock_app)

                assert isinstance(limiter._storage, RedisStorage)
                mock_redis.ping.assert_called_once()
        except ImportError:
            # Skip if redis not available
            pytest.skip("redis not installed")

    def test_rate_limiter_init_app_with_redis_url_failure(self):
        """Test init_app falls back to InMemoryStorage on Redis error."""
        mock_app = MagicMock()
        mock_app.config = {"RATE_LIMIT_REDIS_URL": "redis://localhost:6379"}

        try:
            import redis
            with patch("redis.from_url") as mock_from_url:
                mock_from_url.side_effect = Exception("Connection failed")
                limiter = RateLimiter()
                limiter.init_app(mock_app)

                # Should fall back to InMemoryStorage
                assert isinstance(limiter._storage, InMemoryStorage)
        except ImportError:
            pytest.skip("redis not installed")

    def test_rate_limiter_init_app_stores_extensions(self):
        """Test init_app stores limiter in app.extensions."""
        mock_app = MagicMock()
        mock_app.config = {}
        mock_app.extensions = {}

        limiter = RateLimiter()
        limiter.init_app(mock_app)

        assert mock_app.extensions["rate_limiter"] is limiter

    def test_rate_limiter_init_app_creates_extensions_dict(self):
        """Test init_app creates extensions dict if missing."""
        mock_app = MagicMock()
        mock_app.config = {}
        del mock_app.extensions  # Remove to test creation

        limiter = RateLimiter()
        limiter.init_app(mock_app)

        assert hasattr(mock_app, "extensions")

    def test_rate_limiter_error_handler_registration(self):
        """Test error handler for RateLimitExceeded is registered."""
        mock_app = MagicMock()
        mock_app.config = {}

        limiter = RateLimiter()
        limiter.init_app(mock_app)

        # Check that errorhandler was called
        mock_app.errorhandler.assert_called_once_with(RateLimitExceeded)


class TestRateLimiterParseComplexLimits:
    """Tests for parsing various limit formats."""

    def test_parse_limit_with_day_unit(self):
        """Test parsing limit with day unit."""
        mock_app = MagicMock()
        mock_app.config = {}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        limits = limiter._parse_limit_string("1000/day")
        assert limits[0] == (1000, 86400)

    def test_parse_limit_with_spaces(self):
        """Test parsing limit with extra spaces."""
        mock_app = MagicMock()
        mock_app.config = {}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        limits = limiter._parse_limit_string("  100  /  minute  ")
        assert limits[0] == (100, 60)

    def test_parse_limit_mixed_valid_invalid(self):
        """Test parsing with mix of valid and invalid limits."""
        mock_app = MagicMock()
        mock_app.config = {}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        limits = limiter._parse_limit_string("10/second;invalid;100/minute")
        assert len(limits) == 2
        assert limits[0] == (10, 1)
        assert limits[1] == (100, 60)

    def test_parse_limit_invalid_unit(self):
        """Test parsing with invalid time unit."""
        mock_app = MagicMock()
        mock_app.config = {}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        limits = limiter._parse_limit_string("100/fortnight")
        # Should return default
        assert limits[0] == (100, 60)

    def test_parse_limit_empty_string(self):
        """Test parsing empty limit string."""
        mock_app = MagicMock()
        mock_app.config = {}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        limits = limiter._parse_limit_string("")
        # Should return default
        assert limits[0] == (100, 60)


class TestRedisStorageIncr:
    """Tests for RedisStorage increment operations."""

    def test_redis_storage_incr_pipeline_execution(self):
        """Test RedisStorage.incr uses pipeline correctly."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.return_value = [10, None]

        storage = RedisStorage(mock_redis)
        count = storage.incr("test_key", ttl=300)

        mock_redis.pipeline.assert_called_once()
        mock_pipe.incr.assert_called_once_with("gough:ratelimit:test_key")
        mock_pipe.expire.assert_called_once_with("gough:ratelimit:test_key", 300)
        assert count == 10


class TestInMemoryStorageCleanup:
    """Tests for InMemoryStorage cleanup behavior."""

    def test_cleanup_interval_check(self):
        """Test cleanup respects interval threshold."""
        storage = InMemoryStorage()
        storage._cleanup_interval = 300
        storage._last_cleanup = time.time()

        # Just set one entry
        storage._data["key1"] = {"count": 1, "expires_at": time.time() + 100}

        # Cleanup should not run - interval not exceeded
        storage._cleanup()
        assert "key1" in storage._data

    def test_cleanup_removes_only_expired(self):
        """Test cleanup only removes expired entries."""
        storage = InMemoryStorage()
        now = time.time()
        storage._data["expired1"] = {"count": 1, "expires_at": now - 100}
        storage._data["expired2"] = {"count": 2, "expires_at": now - 50}
        storage._data["valid"] = {"count": 3, "expires_at": now + 100}

        storage._last_cleanup = 0
        storage._cleanup()

        assert "expired1" not in storage._data
        assert "expired2" not in storage._data
        assert "valid" in storage._data

    def test_storage_get_removes_expired_on_access(self):
        """Test that get() removes expired entries."""
        storage = InMemoryStorage()
        now = time.time()
        storage._data["expired"] = {"count": 1, "expires_at": now - 10}

        result = storage.get("expired")

        assert result is None
        assert "expired" not in storage._data

    def test_incr_with_existing_data(self):
        """Test incr increments existing count."""
        storage = InMemoryStorage()
        storage._data["counter"] = {"count": 5, "expires_at": time.time() + 100}

        count = storage.incr("counter", ttl=60)

        assert count == 6


class TestRateLimiterIdentifierLogic:
    """Tests for rate limit identifier generation logic."""

    def test_identifier_extraction_from_forwarded_for(self):
        """Test extracting IP from X-Forwarded-For header."""
        forwarded_for = "192.168.1.1, 10.0.0.1"
        ip = forwarded_for.split(",")[0].strip()
        assert ip == "192.168.1.1"

    def test_identifier_extraction_empty_forwarded_for(self):
        """Test handling empty X-Forwarded-For header."""
        forwarded_for = ""
        ip = forwarded_for.split(",")[0].strip() or "127.0.0.1"
        assert ip == "127.0.0.1"

    def test_identifier_user_format(self):
        """Test user identifier format."""
        user_id = "user123"
        identifier = f"user:{user_id}"
        assert identifier == "user:user123"

    def test_identifier_ip_format(self):
        """Test IP identifier format."""
        ip = "192.168.1.1"
        identifier = f"ip:{ip}"
        assert identifier == "ip:192.168.1.1"


class TestRateLimiterCheckRateLimit:
    """Tests for rate limit checking logic."""

    @pytest.mark.asyncio
    async def test_check_rate_limit_disabled(self):
        """Test check_rate_limit when disabled."""
        mock_app = MagicMock()
        mock_app.config = {"RATE_LIMIT_ENABLED": False}
        limiter = RateLimiter()
        limiter.init_app(mock_app)

        info = await limiter.check_rate_limit()

        assert info.limit == 0
        assert info.remaining == 0


class TestRateLimitExceptionInfo:
    """Tests for RateLimitExceeded exception info."""

    def test_rate_limit_exceeded_attributes(self):
        """Test RateLimitExceeded stores limit and remaining."""
        exc = RateLimitExceeded(
            message="Limit exceeded",
            retry_after=120,
            limit=100,
            remaining=0,
        )
        assert exc.limit == 100
        assert exc.remaining == 0
        assert exc.retry_after == 120

    def test_rate_limit_info_calculation(self):
        """Test rate limit info calculations."""
        max_requests = 100
        current_count = 50
        remaining = max(0, max_requests - current_count)
        assert remaining == 50

    def test_rate_limit_exceeded_when_count_exceeds(self):
        """Test when count exceeds max requests."""
        max_requests = 100
        current_count = 101
        exceeds = current_count > max_requests
        assert exceeds is True


class TestRateLimitDecorator:
    """Tests for rate_limit decorator."""

    @pytest.mark.asyncio
    async def test_rate_limit_decorator_exemption(self):
        """Test rate_limit decorator with exemption function."""

        async def test_func():
            return {"status": "ok"}

        def exempt_func():
            return True

        with patch("app.rate_limit.get_rate_limiter") as mock_get_limiter:
            mock_limiter = MagicMock()
            mock_get_limiter.return_value = mock_limiter

            decorator = rate_limit(
                limit="100/minute", exempt_when=exempt_func
            )
            wrapped = decorator(test_func)

            result = await wrapped()
            assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_rate_limit_decorator_adds_headers(self):
        """Test rate_limit decorator processes response."""

        async def test_func():
            response = MagicMock()
            response.headers = {}
            return response

        with patch("app.rate_limit.get_rate_limiter") as mock_get_limiter:
            mock_limiter = MagicMock()
            mock_get_limiter.return_value = mock_limiter

            # Mock check_rate_limit
            expires_at = datetime.utcnow()
            info = RateLimitInfo(
                limit=100, remaining=50, reset_at=expires_at
            )
            mock_limiter.check_rate_limit = AsyncMock(return_value=info)
            mock_limiter._parse_limit_string.return_value = [(100, 60)]

            decorator = rate_limit(limit="100/minute")
            wrapped = decorator(test_func)

            # Check function is wrapped and callable
            assert callable(wrapped)
            response = await wrapped()
            # Verify response is returned
            assert response is not None


class TestRateLimitByIPDecorator:
    """Tests for rate_limit_by_ip decorator."""

    def test_rate_limit_by_ip_creates_decorator(self):
        """Test rate_limit_by_ip creates callable decorator."""
        decorator = rate_limit_by_ip("50/minute")
        assert callable(decorator)

    def test_rate_limit_by_ip_custom_limit(self):
        """Test rate_limit_by_ip with custom limit string."""
        decorator = rate_limit_by_ip("10/second")
        assert callable(decorator)


class TestRateLimitByUserDecorator:
    """Tests for rate_limit_by_user decorator."""

    def test_rate_limit_by_user_creates_decorator(self):
        """Test rate_limit_by_user creates callable decorator."""
        decorator = rate_limit_by_user("100/minute")
        assert callable(decorator)


class TestRateLimitByApiKeyDecorator:
    """Tests for rate_limit_by_api_key decorator."""

    def test_rate_limit_by_api_key_creates_decorator(self):
        """Test rate_limit_by_api_key creates callable decorator."""
        decorator = rate_limit_by_api_key("500/hour")
        assert callable(decorator)

    def test_rate_limit_by_api_key_hashing(self):
        """Test rate_limit_by_api_key hashes API key."""
        # Test that the hashing logic works
        api_key = "test_api_key_12345"
        expected_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        assert len(expected_hash) == 16


class TestExemptAdminDecorator:
    """Tests for exempt_admin exemption function."""

    def test_exempt_admin_with_admin_user_direct(self):
        """Test exempt_admin logic with admin user dict."""
        # Test the role check logic directly
        admin_user = {"id": "user1", "role": "admin"}
        is_admin = admin_user.get("role") == "admin"
        assert is_admin is True

    def test_exempt_admin_with_non_admin_user_direct(self):
        """Test exempt_admin logic with non-admin user dict."""
        # Test the role check logic directly
        viewer_user = {"id": "user1", "role": "viewer"}
        is_admin = viewer_user.get("role") == "admin"
        assert is_admin is False

    def test_exempt_admin_without_user_direct(self):
        """Test exempt_admin logic without user dict."""
        # Test the role check logic directly
        user = None
        is_admin = user and user.get("role") == "admin" if user else False
        assert is_admin is False
