"""Regression tests for security fixes: bootstrap token signature verification,
auth-bypass header removal, and fail-closed config validation.

Tests FIX 1: Bootstrap token signature verification
Tests FIX 2: Removal of X-Gough-Test-Bypass-Auth header bypass
Tests FIX 3: Fail-closed secret validation at startup
"""

from __future__ import annotations

import jwt
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from quart import Quart, g
from app.security.credentials import (
    validate_one_time_bootstrap_token,
    InvalidCredentialError,
)
from app.config import Config, ProductionConfig, DevelopmentConfig


class TestBootstrapTokenSignatureVerification:
    """FIX 1: Bootstrap token signature verification with HS256."""

    @pytest.fixture
    def redis_mock(self):
        """Mock Redis client for nonce tracking."""
        client = MagicMock()
        client.set = MagicMock(return_value=True)  # Always succeeds for nonce SET NX
        return client

    @pytest.fixture
    def vault_mock(self):
        """Mock Vault client."""
        return MagicMock()

    def test_bootstrap_token_wrong_signature_rejected(self, redis_mock, vault_mock):
        """Bootstrap token signed with wrong secret is rejected.

        Regression: gh-SECURITY-FIX-1
        """
        correct_secret = "the-real-bootstrap-secret"
        wrong_secret = "attacker-forged-secret"

        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=5)

        payload = {
            "nonce": "unique-nonce-12345",
            "mac": "aa:bb:cc:dd:ee:ff",
            "phase": "helper",
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        }

        # Sign with correct secret
        legitimate_token = jwt.encode(payload, correct_secret, algorithm="HS256")

        # Try to verify with correct secret — should succeed
        principal = validate_one_time_bootstrap_token(
            legitimate_token, vault_mock, redis_mock, signing_secret=correct_secret
        )
        assert principal.sub == "bootstrap:aa:bb:cc:dd:ee:ff"

        # Now forge a token with wrong secret and try to verify with correct secret
        forged_token = jwt.encode(payload, wrong_secret, algorithm="HS256")

        # Should reject the forged token when verified with correct secret
        with pytest.raises(InvalidCredentialError, match="signature invalid"):
            validate_one_time_bootstrap_token(
                forged_token, vault_mock, redis_mock, signing_secret=correct_secret
            )

    def test_bootstrap_token_correct_signature_accepted(self, redis_mock, vault_mock):
        """Bootstrap token signed with correct secret is accepted.

        Regression: gh-SECURITY-FIX-1
        """
        secret = "shared-bootstrap-secret"

        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=4)  # Within 10-minute TTL

        payload = {
            "nonce": "valid-nonce-67890",
            "mac": "11:22:33:44:55:66",
            "phase": "deploy",
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        }

        token = jwt.encode(payload, secret, algorithm="HS256")

        # Should accept with correct secret
        principal = validate_one_time_bootstrap_token(
            token, vault_mock, redis_mock, signing_secret=secret
        )
        assert principal.sub == "bootstrap:11:22:33:44:55:66"
        assert principal.tenant_id == "__default__"
        redis_mock.set.assert_called_once()

    def test_bootstrap_token_alg_none_rejected(self, redis_mock, vault_mock):
        """Bootstrap token with alg=none is explicitly rejected.

        Regression: gh-SECURITY-FIX-1
        """
        secret = "some-secret"

        # Create a token with alg=none by manipulating the JWT directly
        # (In Python-JWT, we can't directly use alg=none, so we'll catch the error)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=5)

        payload = {
            "nonce": "nonce-for-alg-test",
            "mac": "aa:bb:cc:dd:ee:ff",
            "phase": "helper",
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        }

        # Try with an invalid algorithm header (simulated)
        # The PyJWT library will reject invalid algorithms automatically
        invalid_token = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJub25jZSI6InRlc3QifQ."

        with pytest.raises(InvalidCredentialError):
            validate_one_time_bootstrap_token(
                invalid_token, vault_mock, redis_mock, signing_secret=secret
            )

    def test_bootstrap_token_hs256_without_secret_fails_closed(self, redis_mock, vault_mock):
        """An HS256 token with no signing_secret is REJECTED (no unverified decode).

        Regression: gh-SECURITY-FIX-1. The old code fell through to
        verify_signature=False when no secret was supplied — this asserts the
        validator now fails closed instead of trusting an unverified token.
        """
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=3)
        payload = {
            "nonce": "no-secret-nonce",
            "mac": "ff:ee:dd:cc:bb:aa",
            "phase": "helper",
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        }
        token = jwt.encode(payload, "attacker-secret", algorithm="HS256")

        with pytest.raises(InvalidCredentialError):
            validate_one_time_bootstrap_token(
                token, vault_client=None, redis_client=redis_mock, signing_secret=None
            )

    def test_bootstrap_token_vault_transit_verified(self, redis_mock):
        """A vault-transit token is accepted only when Vault confirms the signature."""
        import base64 as _b64
        import json as _json

        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=3)
        header = {"alg": "vault-transit", "typ": "JWT", "kid": "gough-bootstrap-jwt"}
        payload = {
            "nonce": "vault-nonce",
            "mac": "aa:bb:cc:dd:ee:ff",
            "phase": "helper",
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        }

        def _seg(obj):
            return _b64.urlsafe_b64encode(
                _json.dumps(obj, separators=(",", ":")).encode()
            ).rstrip(b"=").decode()

        sig_seg = _b64.urlsafe_b64encode(b"vault:v1:fakesig").rstrip(b"=").decode()
        token = f"{_seg(header)}.{_seg(payload)}.{sig_seg}"

        # Vault says the signature is valid -> accepted.
        good_vault = MagicMock()
        good_vault.transit_verify_signature = MagicMock(return_value=True)
        principal = validate_one_time_bootstrap_token(
            token, vault_client=good_vault, redis_client=redis_mock, signing_secret=None
        )
        assert principal.sub == "bootstrap:aa:bb:cc:dd:ee:ff"
        good_vault.transit_verify_signature.assert_called_once()

        # Vault says the signature is invalid -> rejected.
        bad_vault = MagicMock()
        bad_vault.transit_verify_signature = MagicMock(return_value=False)
        with pytest.raises(InvalidCredentialError):
            validate_one_time_bootstrap_token(
                token, vault_client=bad_vault, redis_client=redis_mock, signing_secret=None
            )


class TestNodeEventAuthBypassRemoved:
    """FIX 2: Removal of X-Gough-Test-Bypass-Auth header bypass.

    The fix removes the bypass header logic from post_node_event at nodes.py.
    This test verifies the bypass code path is removed by checking the source directly.
    """

    def test_no_bypass_header_logic_in_post_node_event(self):
        """Verify that X-Gough-Test-Bypass-Auth bypass logic is removed.

        Regression: gh-SECURITY-FIX-2
        """
        from pathlib import Path

        # Read the source file directly to avoid import errors
        # From /home/penguin/code/gough/services/api-manager/tests/ -> up 1 = /services/api-manager/
        nodes_file = Path(__file__).resolve().parent.parent / "app" / "api" / "nodes.py"
        source = nodes_file.read_text(encoding="utf-8")

        # The bypass should NOT be in the source
        assert "X-Gough-Test-Bypass-Auth" not in source, (
            "X-Gough-Test-Bypass-Auth bypass header should be removed from nodes.py"
        )

        # The endpoint should still have the mTLS validation logic
        assert "peer_cert_pem" in source, (
            "post_node_event should still validate mTLS peer certificate"
        )

        # Should return 401 when no peer cert
        assert "unauthorized" in source.lower(), (
            "post_node_event should return 401 unauthorized when no peer cert"
        )


class TestConfigValidationFailClosed:
    """FIX 3: Fail-closed secret validation at startup."""

    def test_production_config_with_dev_secret_key_raises(self):
        """Production config raises if SECRET_KEY is still dev default.

        Regression: gh-SECURITY-FIX-3
        """
        # Create a production-like config with dev defaults
        with patch.object(ProductionConfig, "DEBUG", False):
            with patch.object(ProductionConfig, "TESTING", False):
                with patch.object(
                    ProductionConfig,
                    "SECRET_KEY",
                    "dev-secret-key-change-in-production",
                ):
                    with pytest.raises(RuntimeError, match="SECRET_KEY is set to dev default"):
                        ProductionConfig.validate_secrets()

    def test_production_config_with_dev_jwt_secret_raises(self):
        """Production config raises if JWT_SECRET_KEY is still dev default.

        Regression: gh-SECURITY-FIX-3
        """
        with patch.object(ProductionConfig, "DEBUG", False):
            with patch.object(ProductionConfig, "TESTING", False):
                with patch.object(ProductionConfig, "SECRET_KEY", "prod-secret-key-xyz"):
                    with patch.object(
                        ProductionConfig,
                        "JWT_SECRET_KEY",
                        "dev-secret-key-change-in-production",
                    ):
                        with pytest.raises(RuntimeError, match="JWT_SECRET_KEY is set to dev default"):
                            ProductionConfig.validate_secrets()

    def test_production_config_with_dev_password_salt_raises(self):
        """Production config raises if SECURITY_PASSWORD_SALT is still dev default.

        Regression: gh-SECURITY-FIX-3
        """
        with patch.object(ProductionConfig, "DEBUG", False):
            with patch.object(ProductionConfig, "TESTING", False):
                with patch.object(ProductionConfig, "SECRET_KEY", "prod-secret-key-xyz"):
                    with patch.object(
                        ProductionConfig, "JWT_SECRET_KEY", "prod-jwt-secret-xyz"
                    ):
                        with patch.object(
                            ProductionConfig,
                            "SECURITY_PASSWORD_SALT",
                            "dev-salt-change-in-production",
                        ):
                            with pytest.raises(
                                RuntimeError, match="SECURITY_PASSWORD_SALT is set to dev default"
                            ):
                                ProductionConfig.validate_secrets()

    def test_production_config_with_real_secrets_succeeds(self):
        """Production config validates successfully with real secrets.

        Regression: gh-SECURITY-FIX-3
        """
        with patch.object(ProductionConfig, "DEBUG", False):
            with patch.object(ProductionConfig, "TESTING", False):
                with patch.object(ProductionConfig, "SECRET_KEY", "real-prod-secret-abc123"):
                    with patch.object(ProductionConfig, "JWT_SECRET_KEY", "real-jwt-secret-def456"):
                        with patch.object(
                            ProductionConfig,
                            "SECURITY_PASSWORD_SALT",
                            "real-prod-salt-ghi789",
                        ):
                            # Should not raise
                            ProductionConfig.validate_secrets()

    def test_development_config_with_dev_secrets_allowed(self):
        """Development config allows dev-default secrets.

        Regression: gh-SECURITY-FIX-3
        """
        with patch.object(DevelopmentConfig, "DEBUG", True):
            with patch.object(DevelopmentConfig, "TESTING", False):
                with patch.object(
                    DevelopmentConfig,
                    "SECRET_KEY",
                    "dev-secret-key-change-in-production",
                ):
                    with patch.object(
                        DevelopmentConfig,
                        "JWT_SECRET_KEY",
                        "dev-secret-key-change-in-production",
                    ):
                        with patch.object(
                            DevelopmentConfig,
                            "SECURITY_PASSWORD_SALT",
                            "dev-salt-change-in-production",
                        ):
                            # Should not raise in dev mode
                            DevelopmentConfig.validate_secrets()

    def test_testing_config_with_dev_secrets_allowed(self):
        """Testing config allows dev-default secrets.

        Regression: gh-SECURITY-FIX-3
        """
        from app.config import TestingConfig

        with patch.object(TestingConfig, "DEBUG", False):
            with patch.object(TestingConfig, "TESTING", True):
                with patch.object(
                    TestingConfig,
                    "SECRET_KEY",
                    "dev-secret-key-change-in-production",
                ):
                    with patch.object(
                        TestingConfig,
                        "JWT_SECRET_KEY",
                        "dev-secret-key-change-in-production",
                    ):
                        with patch.object(
                            TestingConfig,
                            "SECURITY_PASSWORD_SALT",
                            "dev-salt-change-in-production",
                        ):
                            # Should not raise in test mode
                            TestingConfig.validate_secrets()

    @pytest.mark.asyncio
    async def test_create_app_calls_validate_secrets(self):
        """App factory calls validate_secrets at startup.

        Regression: gh-SECURITY-FIX-3
        """
        # Patch config class to detect validate_secrets call
        with patch.object(Config, "validate_secrets") as mock_validate:
            from app import create_app

            # This will fail due to missing DB, but we'll catch it
            # We just want to verify validate_secrets is called
            try:
                await create_app(Config)
            except Exception:
                pass  # Expected to fail due to DB not being set up

            mock_validate.assert_called_once()
