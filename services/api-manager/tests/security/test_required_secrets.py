"""Fail-loud startup validation of required secrets (api-manager).

Regression: k8s default-secrets triage 2026-11-10 -- the Kubernetes manifests
shipped "changeme"-style secrets and the beta Helm values dropped every
secretKeyRef, so the app booted on its built-in dev defaults. The app must now
refuse to start whenever a required secret is unset, empty or a known default,
in EVERY config class (no DEBUG/TESTING bypass). Error messages must name the
variable and never echo the value.
"""

from __future__ import annotations

import secrets as _secrets
from typing import Any

import pytest
from app.config import Config, DevelopmentConfig, ProductionConfig, TestingConfig

STRONG_KEY = _secrets.token_urlsafe(48)
STRONG_SALT = _secrets.token_urlsafe(48)
STRONG_JWT = _secrets.token_urlsafe(48)
STRONG_DB_PASS = _secrets.token_urlsafe(32)
STRONG_REDIS_PASS = _secrets.token_urlsafe(32)

KNOWN_DEFAULTS = [
    "changeme",
    "changeme123",
    "CHANGEME",
    "alpha-changeme123",
    "changeme-in-production",
    "change-me-in-production",
    "prodpass",
    "prodpassword",
    "prodsecretkey",
    "devpass",
    "devpassword",
    "secret",
    "password",
    "gough",
    "gough_pass",
    "dev-secret-key-change-in-production",
    "dev-salt-change-in-production",
]


def _make_config(
    base: type[Config] = ProductionConfig, **overrides: Any
) -> type[Config]:
    """Build a config subclass with strong defaults, then apply overrides.

    Every required secret starts strong so each test isolates exactly one
    missing/default value.
    """
    attrs: dict[str, Any] = {
        "SECRET_KEY": STRONG_KEY,
        "SECURITY_PASSWORD_SALT": STRONG_SALT,
        "JWT_SECRET_KEY": STRONG_JWT,
        "DB_TYPE": "postgres",
        "DB_PASS": STRONG_DB_PASS,
        "REDIS_URL": f"redis://:{STRONG_REDIS_PASS}@redis:6379/0",
        "RATE_LIMIT_REDIS_URL": f"redis://:{STRONG_REDIS_PASS}@redis:6379/0",
    }
    attrs.update(overrides)
    return type("_Cfg", (base,), attrs)  # dynamic subclass of base


def _validate(cfg: type) -> None:
    """Run the module-level validator against a config class."""
    from app.security.required_secrets import validate_required_secrets

    validate_required_secrets(cfg)


def _error_type() -> type[BaseException]:
    """Return the validator's error type (imported lazily so a missing module fails per-test)."""
    from app.security.required_secrets import MissingSecretError

    return MissingSecretError


class TestValidatorRejects:
    """Each required secret is rejected when unset, empty or a known default."""

    @pytest.mark.parametrize("var", ["SECRET_KEY", "SECURITY_PASSWORD_SALT", "DB_PASS"])
    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_missing_or_empty(self, var: str, bad: str | None) -> None:
        """Unset / empty / whitespace-only is refused and the variable is named."""
        with pytest.raises(_error_type(), match=var):
            ov: dict[str, Any] = {var: bad}
            _validate(_make_config(**ov))

    @pytest.mark.parametrize("var", ["SECRET_KEY", "SECURITY_PASSWORD_SALT", "DB_PASS"])
    @pytest.mark.parametrize("bad", KNOWN_DEFAULTS)
    def test_known_default(self, var: str, bad: str) -> None:
        """Known default/placeholder values are refused."""
        with pytest.raises(_error_type(), match=var):
            ov: dict[str, Any] = {var: bad}
            _validate(_make_config(**ov))

    def test_jwt_secret_known_default(self) -> None:
        """JWT_SECRET_KEY (defaults to SECRET_KEY) is also checked."""
        with pytest.raises(_error_type(), match="JWT_SECRET_KEY"):
            _validate(
                _make_config(JWT_SECRET_KEY="dev-secret-key-change-in-production")
            )

    @pytest.mark.parametrize("var", ["SECRET_KEY", "SECURITY_PASSWORD_SALT"])
    def test_short_key_rejected(self, var: str) -> None:
        """Signing key and salt must be at least 16 characters."""
        with pytest.raises(_error_type(), match=var):
            ov: dict[str, Any] = {var: "Xq7!short"}
            _validate(_make_config(**ov))

    @pytest.mark.parametrize(
        "url",
        [
            "redis://redis:6379/0",
            "redis://user@redis:6379/0",
            "redis://:changeme@redis:6379/0",
            "redis://:@redis:6379/0",
            "rediss://default:password@redis:6380/0",
        ],
    )
    def test_redis_without_strong_password(self, url: str) -> None:
        """A configured Redis URL without a strong password is refused."""
        with pytest.raises(_error_type(), match="REDIS_URL"):
            _validate(_make_config(REDIS_URL=url))

    def test_rate_limit_redis_without_password(self) -> None:
        """RATE_LIMIT_REDIS_URL is validated independently of REDIS_URL."""
        with pytest.raises(_error_type(), match="RATE_LIMIT_REDIS_URL"):
            _validate(_make_config(RATE_LIMIT_REDIS_URL="redis://redis:6379/1"))

    def test_all_problems_reported_together(self) -> None:
        """Every failing variable is listed in a single error."""
        cfg = _make_config(
            SECRET_KEY="", SECURITY_PASSWORD_SALT="changeme", DB_PASS=None
        )
        with pytest.raises(_error_type()) as exc:
            _validate(cfg)
        msg = str(exc.value)
        for var in ("SECRET_KEY", "SECURITY_PASSWORD_SALT", "DB_PASS"):
            assert var in msg

    @pytest.mark.parametrize("base", [DevelopmentConfig, TestingConfig, Config])
    def test_no_debug_or_testing_bypass(self, base: type[Config]) -> None:
        """DEBUG and TESTING configs are validated exactly like production."""
        with pytest.raises(_error_type(), match="SECRET_KEY"):
            _validate(_make_config(base=base, SECRET_KEY="changeme"))


class TestValidatorNeverLeaksValues:
    """Error text names variables, never their values."""

    def test_value_not_in_message(self) -> None:
        """Neither a weak nor a strong co-configured value appears in the error."""
        weak = "prodpass-ZZtopMarker"
        cfg = _make_config(DB_PASS=weak, SECRET_KEY="short-ZZmarker")
        with pytest.raises(_error_type()) as exc:
            _validate(cfg)
        msg = str(exc.value)
        assert weak not in msg
        assert "short-ZZmarker" not in msg
        assert STRONG_SALT not in msg
        assert STRONG_REDIS_PASS not in msg


class TestValidatorAccepts:
    """Strong explicit values pass; conditional secrets are only required when used."""

    def test_strong_values_pass(self) -> None:
        """All strong values: no error."""
        _validate(_make_config())

    def test_sqlite_needs_no_db_password(self) -> None:
        """DB_PASS is not required when DB_TYPE is sqlite."""
        _validate(_make_config(DB_TYPE="sqlite", DB_PASS=None))

    def test_redis_not_configured_needs_no_password(self) -> None:
        """Empty Redis URLs mean Redis is not configured; no password required."""
        _validate(_make_config(REDIS_URL="", RATE_LIMIT_REDIS_URL=""))

    def test_redis_password_from_url(self) -> None:
        """A strong password embedded in the URL (user or password-only form) passes."""
        _validate(
            _make_config(
                REDIS_URL=f"rediss://gough:{STRONG_REDIS_PASS}@redis:6380/0",
                RATE_LIMIT_REDIS_URL=f"redis://:{STRONG_REDIS_PASS}@redis:6379/1",
            )
        )


class TestAppStartup:
    """The app factory refuses to start with weak secrets and starts with strong ones."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "override",
        [
            {"SECRET_KEY": ""},
            {"SECRET_KEY": "changeme"},
            {"SECURITY_PASSWORD_SALT": None},
            {"SECURITY_PASSWORD_SALT": "devpass"},
            {"DB_PASS": "prodpass"},
            {"DB_PASS": ""},
            {"REDIS_URL": "redis://redis:6379/0"},
        ],
    )
    async def test_create_app_refuses_weak_secret(
        self, override: dict[str, Any]
    ) -> None:
        """create_app raises before building the app when any required secret is weak."""
        from app import create_app

        with pytest.raises(_error_type()):
            await create_app(_make_config(**override))

    @pytest.mark.asyncio
    async def test_create_app_starts_with_strong_values(self) -> None:
        """create_app returns an app when every required secret is strong."""
        from app import create_app

        app = await create_app(
            _make_config(base=TestingConfig, DB_TYPE="sqlite", DB_PASS=None)
        )
        assert app is not None
        assert app.config["SECRET_KEY"] == STRONG_KEY

    def test_validate_secrets_delegates(self) -> None:
        """Config.validate_secrets (the existing startup hook) runs the new validator."""
        with pytest.raises(_error_type(), match="SECRET_KEY"):
            _make_config(SECRET_KEY="changeme").validate_secrets()
