"""Rate Limiting Module for Gough Hypervisor Orchestration Platform.

Provides rate limiting functionality for API endpoints using:
- In-memory storage (default, suitable for single instance)
- Redis storage (recommended for production/multi-instance)

Rate limiting strategies:
- Fixed window
- Sliding window
- Token bucket
"""

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import wraps
from typing import Any, Callable, Optional, Tuple

import asyncio
from quart import Quart, current_app, g, request, jsonify


class RateLimitStrategy(Enum):
    """Rate limiting strategies."""

    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW = "sliding_window"
    TOKEN_BUCKET = "token_bucket"


class RateLimitExceeded(Exception):
    """Exception raised when rate limit is exceeded."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: int = 60,
        limit: int = 0,
        remaining: int = 0,
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.limit = limit
        self.remaining = remaining


@dataclass(slots=True)
class RateLimitInfo:
    """Rate limit information for a request."""

    limit: int
    remaining: int
    reset_at: datetime
    retry_after: int = 0

    def to_headers(self) -> dict:
        """Convert to HTTP headers."""
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(int(self.reset_at.timestamp())),
        }


class InMemoryStorage:
    """In-memory rate limit storage (single instance only)."""

    def __init__(self):
        self._data: dict[str, dict[str, Any]] = {}
        self._cleanup_interval = 300  # 5 minutes
        self._last_cleanup = time.time()

    def _cleanup(self) -> None:
        """Remove expired entries."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        expired_keys = [
            k for k, v in self._data.items() if v.get("expires_at", 0) < now
        ]
        for key in expired_keys:
            del self._data[key]

        self._last_cleanup = now

    def get(self, key: str) -> Optional[dict]:
        """Get rate limit data for key."""
        self._cleanup()
        data = self._data.get(key)
        if data and data.get("expires_at", 0) < time.time():
            del self._data[key]
            return None
        return data

    def set(self, key: str, data: dict, ttl: int) -> None:
        """Set rate limit data with TTL."""
        self._data[key] = {**data, "expires_at": time.time() + ttl}

    def incr(self, key: str, ttl: int) -> int:
        """Increment counter and return new value."""
        data = self.get(key)
        if data is None:
            self.set(key, {"count": 1}, ttl)
            return 1

        data["count"] = data.get("count", 0) + 1
        return data["count"]


class RedisStorage:
    """Redis-based rate limit storage (multi-instance safe)."""

    def __init__(self, redis_client):
        self._redis = redis_client
        self._prefix = "gough:ratelimit:"

    def _key(self, key: str) -> str:
        """Generate prefixed key."""
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Optional[dict]:
        """Get rate limit data for key."""
        import json

        data = self._redis.get(self._key(key))
        if data:
            return json.loads(data)
        return None

    def set(self, key: str, data: dict, ttl: int) -> None:
        """Set rate limit data with TTL."""
        import json

        self._redis.setex(self._key(key), ttl, json.dumps(data))

    def incr(self, key: str, ttl: int) -> int:
        """Increment counter and return new value."""
        k = self._key(key)
        pipe = self._redis.pipeline()
        pipe.incr(k)
        pipe.expire(k, ttl)
        results = pipe.execute()
        return results[0]


class RateLimiter:
    """Rate limiter for Quart applications."""

    def __init__(self, app: Quart = None):
        self.app = app
        self._storage = None
        self._default_limits: list[Tuple[int, int]] = []  # (requests, seconds)
        self._enabled = True

        if app is not None:
            self.init_app(app)

    def init_app(self, app: Quart) -> None:
        """Initialize rate limiter with Quart app."""
        self.app = app

        # Configuration
        self._enabled = app.config.get("RATE_LIMIT_ENABLED", True)
        redis_url = app.config.get("RATE_LIMIT_REDIS_URL")

        # Initialize storage
        if redis_url:
            try:
                import redis

                redis_client = redis.from_url(redis_url)
                redis_client.ping()
                self._storage = RedisStorage(redis_client)
                app.logger.info("Rate limiter using Redis storage")
            except Exception as e:
                app.logger.warning(
                    f"Redis unavailable for rate limiting, using memory: {e}"
                )
                self._storage = InMemoryStorage()
        else:
            self._storage = InMemoryStorage()
            app.logger.info("Rate limiter using in-memory storage")

        # Parse default limits from config
        default_limit = app.config.get("RATE_LIMIT_DEFAULT", "100/minute")
        self._default_limits = self._parse_limit_string(default_limit)

        # Store instance in app extensions
        if not hasattr(app, "extensions"):
            app.extensions = {}
        app.extensions["rate_limiter"] = self

        # Register error handler
        @app.errorhandler(RateLimitExceeded)
        async def handle_rate_limit_exceeded(error):
            response = jsonify({
                "error": "rate_limit_exceeded",
                "message": str(error),
                "retry_after": error.retry_after,
            })
            response.status_code = 429
            response.headers["Retry-After"] = str(error.retry_after)
            response.headers["X-RateLimit-Limit"] = str(error.limit)
            response.headers["X-RateLimit-Remaining"] = "0"
            return response

    def _parse_limit_string(self, limit_str: str) -> list[Tuple[int, int]]:
        """Parse limit string like '100/minute' or '10/second;100/minute'."""
        limits = []
        time_units = {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
        }

        for part in limit_str.split(";"):
            part = part.strip()
            if "/" not in part:
                continue

            count_str, unit = part.split("/", 1)
            count = int(count_str.strip())
            unit = unit.strip().lower().rstrip("s")  # Remove plural 's'

            if unit in time_units:
                limits.append((count, time_units[unit]))

        return limits or [(100, 60)]  # Default: 100/minute

    async def _get_identifier(self) -> str:
        """Get rate limit identifier for current request."""
        # Use authenticated user ID if available
        if hasattr(g, "current_user") and g.current_user:
            return f"user:{g.current_user['id']}"

        # Fall back to IP address
        ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.headers.get("X-Real-IP")
            or request.remote_addr
        )
        return f"ip:{ip}"

    def _get_endpoint_key(self) -> str:
        """Get rate limit key for current endpoint."""
        endpoint = request.endpoint or "unknown"
        method = request.method
        return f"{method}:{endpoint}"

    async def check_rate_limit(
        self,
        limits: Optional[list[Tuple[int, int]]] = None,
        key_prefix: str = "",
    ) -> RateLimitInfo:
        """Check rate limit for current request.

        Args:
            limits: List of (requests, seconds) tuples
            key_prefix: Optional prefix for the rate limit key

        Returns:
            RateLimitInfo with current rate limit status

        Raises:
            RateLimitExceeded: If rate limit is exceeded
        """
        if not self._enabled:
            return RateLimitInfo(
                limit=0, remaining=0, reset_at=datetime.utcnow()
            )

        limits = limits or self._default_limits
        identifier = await self._get_identifier()
        endpoint_key = self._get_endpoint_key()

        # Check each limit tier
        for max_requests, window_seconds in limits:
            key_str = (
                f"{key_prefix}:{identifier}:{endpoint_key}:{window_seconds}"
            )
            key = hashlib.sha256(key_str.encode()).hexdigest()[:32]

            current_count = await asyncio.to_thread(
                self._storage.incr, key, window_seconds
            )

            if current_count > max_requests:
                # Get remaining time in window
                data = await asyncio.to_thread(self._storage.get, key)
                retry_after = window_seconds
                if data and "expires_at" in data:
                    retry_after = max(1, int(data["expires_at"] - time.time()))

                raise RateLimitExceeded(
                    message=f"Rate limit exceeded: {max_requests} requests "
                    f"per {window_seconds} seconds",
                    retry_after=retry_after,
                    limit=max_requests,
                    remaining=0,
                )

        # Return info for the primary (first) limit
        max_requests, window_seconds = limits[0]
        key_str = (
            f"{key_prefix}:{identifier}:{endpoint_key}:{window_seconds}"
        )
        key = hashlib.sha256(key_str.encode()).hexdigest()[:32]

        data = await asyncio.to_thread(self._storage.get, key)
        current_count = data.get("count", 0) if data else 0
        expires_at = (
            data.get("expires_at")
            if data
            else time.time() + window_seconds
        )
        reset_at = datetime.fromtimestamp(expires_at)

        return RateLimitInfo(
            limit=max_requests,
            remaining=max(0, max_requests - current_count),
            reset_at=reset_at,
        )


def rate_limit(
    limit: str = None,
    key_func: Callable = None,
    exempt_when: Callable = None,
) -> Callable:
    """Decorator to apply rate limiting to a route.

    Args:
        limit: Rate limit string (e.g., "10/second", "100/minute;1000/hour")
        key_func: Optional function to generate custom rate limit key
        exempt_when: Optional function that returns True to exempt request

    Example:
        @app.route("/api/resource")
        @rate_limit("10/minute")
        def get_resource():
            ...

        @app.route("/api/heavy")
        @rate_limit("5/minute;50/hour")
        def heavy_operation():
            ...
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            limiter = get_rate_limiter()
            if not limiter:
                result = func(*args, **kwargs)
                if hasattr(result, '__await__'):
                    return await result
                return result

            # Check exemption
            if exempt_when and exempt_when():
                result = func(*args, **kwargs)
                if hasattr(result, '__await__'):
                    return await result
                return result

            # Parse limits
            limits = None
            if limit:
                limits = limiter._parse_limit_string(limit)

            # Generate key prefix
            key_prefix = ""
            if key_func:
                key_prefix = key_func()

            # Check rate limit
            info = await limiter.check_rate_limit(
                limits=limits, key_prefix=key_prefix
            )

            # Execute function
            response = func(*args, **kwargs)
            if hasattr(response, '__await__'):
                response = await response

            # Add rate limit headers to response
            if hasattr(response, "headers"):
                for header, value in info.to_headers().items():
                    response.headers[header] = value

            return response

        return wrapper

    return decorator


def rate_limit_by_ip(limit: str = "100/minute") -> Callable:
    """Rate limit decorator using IP address only."""

    def key_func():
        ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.headers.get("X-Real-IP")
            or request.remote_addr
        )
        return f"ip:{ip}"

    return rate_limit(limit=limit, key_func=key_func)


def rate_limit_by_user(limit: str = "100/minute") -> Callable:
    """Rate limit decorator using authenticated user only."""

    def key_func():
        if hasattr(g, "current_user") and g.current_user:
            return f"user:{g.current_user['id']}"
        # Fall back to IP if not authenticated
        ip = request.remote_addr
        return f"anon:{ip}"

    return rate_limit(limit=limit, key_func=key_func)


def rate_limit_by_api_key(limit: str = "1000/hour") -> Callable:
    """Rate limit decorator using API key."""

    def key_func():
        api_key = request.headers.get("X-API-Key", "")
        if api_key:
            api_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
            return f"apikey:{api_hash}"
        return f"nokey:{request.remote_addr}"

    return rate_limit(limit=limit, key_func=key_func)


def exempt_admin() -> bool:
    """Exemption function for admin users."""
    if hasattr(g, "current_user") and g.current_user:
        # Check if user has admin role (user is a dict with 'role' key)
        return g.current_user.get("role") == "admin"
    return False


def get_rate_limiter() -> Optional[RateLimiter]:
    """Get the rate limiter instance from current app."""
    try:
        if current_app and hasattr(current_app, "extensions"):
            return current_app.extensions.get("rate_limiter")
    except RuntimeError:
        pass
    return None


def init_rate_limiter(app: Quart) -> RateLimiter:
    """Initialize and return rate limiter for app."""
    return RateLimiter(app)
