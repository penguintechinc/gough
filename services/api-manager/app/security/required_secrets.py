"""Fail-loud startup validation of required secrets.

The api-manager must never boot on a missing, empty or well-known default
secret: Kubernetes manifests previously shipped "changeme" values and a Helm
values override silently dropped every secretKeyRef, so pods ran on the
config's dev defaults. This check runs in EVERY config class (no DEBUG/TESTING
bypass); tests supply explicit strong values instead. Messages name the
variable and the reason, never the value.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# Exact (case-insensitive) values that are placeholders, never real secrets.
_KNOWN_DEFAULT_EXACT = frozenset(
    {
        "secret",
        "password",
        "gough",
        "gough_pass",
        "admin",
        "minioadmin",
        "default",
        "test",
    }
)
# Substring/prefix patterns covering the families seen in this repo's history
# (changeme*, change-me*, prodpass*, prodsecr*, devpass*, dev-secret-key-*,
# dev-salt-*, staging*pass/secret) -- case-insensitive.
_KNOWN_DEFAULT_PATTERN = re.compile(
    r"change[-_]?me"
    r"|^prodpass|^prodsecr|^prodsalt|^prodredis"
    r"|^devpass|^devsecr|^devsalt|^devredis"
    r"|^stagingpass|^stagingsecr|^stagingsalt"
    r"|^dev-secret-key|^dev-salt",
    re.IGNORECASE,
)
# Signing key and salt material must be at least this long.
_MIN_KEY_LENGTH = 16
_PASSWORDLESS_DB_TYPES = frozenset({"sqlite"})
_GENERATE_HINT = "generate one with: openssl rand -base64 48"


class MissingSecretError(RuntimeError):
    """Raised at startup when one or more required secrets are unusable."""


def is_known_default(value: str) -> bool:
    """Return True when ``value`` is a known placeholder/default secret."""
    v = value.strip()
    return v.lower() in _KNOWN_DEFAULT_EXACT or bool(_KNOWN_DEFAULT_PATTERN.search(v))


def _check_value(name: str, value: object, min_length: int = 0) -> str | None:
    """Return a value-free problem description for one secret, or None if acceptable."""
    if value is None or not isinstance(value, str) or not value.strip():
        return f"{name} is unset or empty"
    if is_known_default(value):
        return f"{name} is a known default/placeholder value"
    if min_length and len(value.strip()) < min_length:
        return f"{name} is shorter than {min_length} characters"
    return None


def _redis_password(url: str) -> str | None:
    """Extract the password from a redis:// / rediss:// / unix:// URL, if any."""
    parsed = urlparse(url)
    if parsed.password:
        return parsed.password
    query_pw = parse_qs(parsed.query).get("password")
    return query_pw[0] if query_pw else None


def _check_redis(name: str, url: object) -> str | None:
    """A configured (non-empty) Redis URL must embed a strong password."""
    if not isinstance(url, str) or not url.strip():
        return None  # Redis not configured -- nothing to check.
    password = _redis_password(url.strip())
    if not password:
        return f"{name} is configured without a password (Redis must require auth)"
    if is_known_default(password):
        return f"{name} password is a known default/placeholder value"
    return None


def collect_problems(config: type) -> list[str]:
    """Return every value-free problem with the required secrets on ``config``."""
    problems: list[str | None] = [
        _check_value(
            "SECRET_KEY", getattr(config, "SECRET_KEY", None), _MIN_KEY_LENGTH
        ),
        _check_value(
            "SECURITY_PASSWORD_SALT",
            getattr(config, "SECURITY_PASSWORD_SALT", None),
            _MIN_KEY_LENGTH,
        ),
    ]
    jwt_secret = getattr(config, "JWT_SECRET_KEY", None)
    if jwt_secret is not None:
        problems.append(_check_value("JWT_SECRET_KEY", jwt_secret, _MIN_KEY_LENGTH))

    db_type = str(getattr(config, "DB_TYPE", "") or "").strip().lower()
    if db_type not in _PASSWORDLESS_DB_TYPES:
        problems.append(_check_value("DB_PASS", getattr(config, "DB_PASS", None)))

    problems.append(_check_redis("REDIS_URL", getattr(config, "REDIS_URL", None)))
    problems.append(
        _check_redis(
            "RATE_LIMIT_REDIS_URL", getattr(config, "RATE_LIMIT_REDIS_URL", None)
        )
    )
    return [p for p in problems if p]


def validate_required_secrets(config: type) -> None:
    """Refuse to start when any required secret is unset, empty or a known default.

    Raises:
        MissingSecretError: listing every failing variable by name (never value).
    """
    problems = collect_problems(config)
    if problems:
        raise MissingSecretError(
            "Refusing to start: required secrets are not configured -- "
            + "; ".join(problems)
            + f". Provide them via the externally managed Secret ({_GENERATE_HINT})."
        )
