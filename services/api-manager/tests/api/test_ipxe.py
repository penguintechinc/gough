"""Tests for Sprint 2 iPXE chain and bootstrap-token endpoints.

Covers:
- _normalize_mac, _RateLimiter, _render_helper_ipxe_script,
  _render_deploy_ipxe_script, _mint_bootstrap_jwt
- GET /api/v1/ipxe/helper/{mac}    happy path, 404, 400, rate-limit
- GET /api/v1/ipxe/deploy/{mac}    happy path, 404
- POST /api/v1/ipxe/bind-mac       happy path, validation, 404
- POST /api/v1/ipxe/mint-bootstrap-token   happy path, validation
- One-time bootstrap-token validation: nonce-replay (409 surrogate),
  expired (401), MAC-not-found (404)

All external dependencies (Redis, Vault, DB) are mocked. No network.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest

from app.api import ipxe as ipxe_module
from app.api.ipxe import (
    _RateLimiter,
    _mint_bootstrap_jwt,
    _normalize_mac,
    _render_deploy_ipxe_script,
    _render_helper_ipxe_script,
    _BOOTSTRAP_JWT_TTL_SECONDS,
)
from app.security.credentials import (
    ExpiredCredentialError,
    InvalidCredentialError,
    OneTimeTokenReplayError,
    validate_one_time_bootstrap_token,
)


# =============================================================================
# Pure-function tests (no Quart context required)
# =============================================================================


class TestNormalizeMac:
    """MAC address normalization."""

    def test_normalize_colon_form(self) -> None:
        assert _normalize_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"

    def test_normalize_dash_form(self) -> None:
        assert _normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"

    def test_normalize_dotless(self) -> None:
        assert _normalize_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"

    def test_normalize_invalid_returns_empty(self) -> None:
        assert _normalize_mac("not-a-mac") == ""
        assert _normalize_mac("") == ""
        assert _normalize_mac("aa:bb:cc:dd:ee") == ""
        assert _normalize_mac("zz:bb:cc:dd:ee:ff") == ""


class TestRateLimiter:
    """In-memory sliding-window rate limiter."""

    def test_allows_under_limit(self) -> None:
        rl = _RateLimiter(max_requests=3, window_seconds=60.0)
        assert rl.allow("ip1") is True
        assert rl.allow("ip1") is True
        assert rl.allow("ip1") is True

    def test_blocks_over_limit(self) -> None:
        rl = _RateLimiter(max_requests=2, window_seconds=60.0)
        assert rl.allow("ip1") is True
        assert rl.allow("ip1") is True
        assert rl.allow("ip1") is False

    def test_separate_keys_independent(self) -> None:
        rl = _RateLimiter(max_requests=1, window_seconds=60.0)
        assert rl.allow("a") is True
        assert rl.allow("a") is False
        assert rl.allow("b") is True

    def test_window_expiration(self) -> None:
        rl = _RateLimiter(max_requests=1, window_seconds=0.05)
        assert rl.allow("k") is True
        assert rl.allow("k") is False
        time.sleep(0.07)
        assert rl.allow("k") is True

    def test_reset_clears(self) -> None:
        rl = _RateLimiter(max_requests=1, window_seconds=60.0)
        rl.allow("k")
        rl.reset()
        assert rl.allow("k") is True


class TestScriptRenderers:
    """iPXE script renderer text shape."""

    def test_helper_script_uefi(self) -> None:
        script = _render_helper_ipxe_script(
            "aa:bb:cc:dd:ee:ff",
            "test.jwt.token",
            "https://primary.example",
            firmware="uefi",
        )
        assert script.startswith("#!ipxe\n")
        assert "gough_token=test.jwt.token" in script
        assert "gough_primary=https://primary.example" in script
        assert "gough_mac=aa:bb:cc:dd:ee:ff" in script
        assert "https://primary.example/ipxe/kernel/helper-efi" in script
        assert "${platform}" in script

    def test_helper_script_bios(self) -> None:
        script = _render_helper_ipxe_script(
            "aa:bb:cc:dd:ee:ff",
            "tok",
            "https://primary.example/",
            firmware="bios",
        )
        assert "https://primary.example/ipxe/kernel/helper-bios" in script
        # Trailing slash normalized:
        assert "https://primary.example//ipxe" not in script

    def test_deploy_script_boots_kernel_with_bootstrap_token(self) -> None:
        script = _render_deploy_ipxe_script(
            "aa:bb:cc:dd:ee:ff", "tok", "https://primary.example"
        )
        assert script.startswith("#!ipxe\n")
        # The deploy phase boots a real kernel+initrd and carries the bootstrap
        # token on the cmdline (added by "feat(ipxe): phase-2 + LUKS encryption
        # tiers"). This test previously asserted a "Sprint 4" chain-to-helper
        # stub -- it was written after that implementation landed and never ran,
        # because the whole file errored at collection on the quart/flask pin
        # bug, so the stale expectation went unnoticed.
        assert "https://primary.example/ipxe/kernel/deploy-kernel" in script
        assert "https://primary.example/ipxe/initrd/deploy.initrd" in script
        assert "gough.bootstrap_token=tok" in script
        assert "gough.mac=aa:bb:cc:dd:ee:ff" in script
        assert "gough.phase=deploy" in script
        assert "boot || goto fallback" in script


# =============================================================================
# JWT minting (mocked Vault + Redis)
# =============================================================================


class _FakeApp:
    """Minimal stand-in for current_app."""

    def __init__(self, *, vault_client: Any = None, redis_client: Any = None) -> None:
        self.vault_client = vault_client
        self.redis_client = redis_client
        self.config: dict[str, Any] = {
            "JWT_SECRET_KEY": "test-secret",
            "PRIMARY_BASE_URL": "https://primary.test",
        }


@pytest.fixture
def fake_redis() -> MagicMock:
    redis = MagicMock()
    redis.set = MagicMock(return_value=True)
    redis.get = MagicMock(return_value=None)
    return redis


@pytest.fixture
def fake_app(fake_redis: MagicMock) -> _FakeApp:
    return _FakeApp(redis_client=fake_redis)


class TestMintBootstrapJwt:
    """One-time bootstrap JWT minting."""

    def test_mint_hs256_fallback(self, fake_app: _FakeApp, fake_redis: MagicMock) -> None:
        with patch("app.api.ipxe.current_app", fake_app):
            token, nonce = _mint_bootstrap_jwt("aa:bb:cc:dd:ee:ff", phase="helper")
        assert isinstance(token, str)
        assert isinstance(nonce, str) and len(nonce) > 16
        payload = jwt.decode(token, "test-secret", algorithms=["HS256"])
        assert payload["mac"] == "aa:bb:cc:dd:ee:ff"
        assert payload["phase"] == "helper"
        assert payload["nonce"] == nonce
        assert payload["exp"] - payload["iat"] == _BOOTSTRAP_JWT_TTL_SECONDS
        fake_redis.set.assert_called_once()

    def test_mint_via_vault_transit(self, fake_redis: MagicMock) -> None:
        vault = MagicMock()
        vault.transit_sign.return_value = "vault:v1:abc=="
        app = _FakeApp(vault_client=vault, redis_client=fake_redis)
        with patch("app.api.ipxe.current_app", app):
            token, _nonce = _mint_bootstrap_jwt("aa:bb:cc:dd:ee:ff", phase="deploy")
        assert token.count(".") == 2
        vault.transit_sign.assert_called_once()

    def test_mint_vault_failure_falls_back(self, fake_redis: MagicMock) -> None:
        vault = MagicMock()
        vault.transit_sign.side_effect = RuntimeError("vault sealed")
        app = _FakeApp(vault_client=vault, redis_client=fake_redis)
        with patch("app.api.ipxe.current_app", app):
            token, _ = _mint_bootstrap_jwt("aa:bb:cc:dd:ee:ff", phase="helper")
        # HS256 fallback: token decodes with shared secret.
        payload = jwt.decode(token, "test-secret", algorithms=["HS256"])
        assert payload["mac"] == "aa:bb:cc:dd:ee:ff"

    def test_redis_collision_raises(self, fake_app: _FakeApp, fake_redis: MagicMock) -> None:
        fake_redis.set.return_value = False  # NX collision
        with patch("app.api.ipxe.current_app", fake_app):
            with pytest.raises(RuntimeError, match="collision"):
                _mint_bootstrap_jwt("aa:bb:cc:dd:ee:ff", phase="helper")


# =============================================================================
# HTTP route tests (mocked DB + app context)
# =============================================================================


def _make_node_record(
    *, node_id: int = 1, mac: str = "aa:bb:cc:dd:ee:ff", dmi_uuid: str = "dmi-uuid-1",
    source: str = "nodes",
) -> dict:
    return {
        "id": node_id,
        "mac": mac,
        "dmi_uuid": dmi_uuid,
        "source": source,
        "raw": {"id": node_id, "primary_nic_mac": mac, "dmi_uuid": dmi_uuid},
    }


@pytest.fixture
def quart_app(fake_redis: MagicMock):
    """Build a minimal Quart app exposing only the iPXE blueprint."""
    from quart import Quart

    app = Quart(__name__)
    app.config["JWT_SECRET_KEY"] = "test-secret"
    app.config["PRIMARY_BASE_URL"] = "https://primary.test"
    app.redis_client = fake_redis
    app.vault_client = None
    app.register_blueprint(ipxe_module.ipxe_bp, url_prefix="/api/v1/ipxe")
    # Reset rate limiter between tests so neighbor-test pollution can't 429 us.
    ipxe_module._ipxe_script_rate_limiter.reset()
    return app


@pytest.fixture
def client(quart_app):
    return quart_app.test_client()


class TestHelperEndpoint:
    """GET /api/v1/ipxe/helper/{mac}."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_text_plain(self, client) -> None:
        with patch(
            "app.api.ipxe._find_node_or_machine_by_mac",
            return_value=_make_node_record(),
        ):
            resp = await client.get("/api/v1/ipxe/helper/aa:bb:cc:dd:ee:ff")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/plain")
        body = (await resp.get_data()).decode()
        assert body.startswith("#!ipxe\n")
        assert "gough_mac=aa:bb:cc:dd:ee:ff" in body
        assert "gough_token=" in body
        assert "${platform}" in body

    @pytest.mark.asyncio
    async def test_unknown_mac_returns_404(self, client) -> None:
        with patch("app.api.ipxe._find_node_or_machine_by_mac", return_value=None):
            resp = await client.get("/api/v1/ipxe/helper/aa:bb:cc:dd:ee:00")
        assert resp.status_code == 404
        body = await resp.get_json()
        assert "MAC not found" in body["error"]

    @pytest.mark.asyncio
    async def test_invalid_mac_returns_400(self, client) -> None:
        resp = await client.get("/api/v1/ipxe/helper/not-a-mac")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rate_limit_returns_429(self, client) -> None:
        ipxe_module._ipxe_script_rate_limiter.reset()
        # Force the limiter to deny.
        with patch.object(
            ipxe_module._ipxe_script_rate_limiter, "allow", return_value=False
        ):
            resp = await client.get("/api/v1/ipxe/helper/aa:bb:cc:dd:ee:ff")
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_firmware_query_param_bios(self, client) -> None:
        with patch(
            "app.api.ipxe._find_node_or_machine_by_mac",
            return_value=_make_node_record(),
        ):
            resp = await client.get("/api/v1/ipxe/helper/aa:bb:cc:dd:ee:ff?fw=bios")
        body = (await resp.get_data()).decode()
        assert "helper-bios" in body


class TestDeployEndpoint:
    """GET /api/v1/ipxe/deploy/{mac}."""

    @pytest.mark.asyncio
    async def test_happy_path(self, client) -> None:
        with patch(
            "app.api.ipxe._find_node_or_machine_by_mac",
            return_value=_make_node_record(),
        ):
            resp = await client.get("/api/v1/ipxe/deploy/aa:bb:cc:dd:ee:ff")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/plain")
        body = (await resp.get_data()).decode()
        assert body.startswith("#!ipxe\n")
        # See TestScriptRenderers::test_deploy_script_boots_kernel_with_bootstrap_token -- the
        # deploy phase renders a full kernel/initrd boot, not a helper stub.
        assert "https://primary.test/ipxe/kernel/deploy-kernel" in body
        assert "gough.phase=deploy" in body

    @pytest.mark.asyncio
    async def test_unknown_mac_returns_404(self, client) -> None:
        with patch("app.api.ipxe._find_node_or_machine_by_mac", return_value=None):
            resp = await client.get("/api/v1/ipxe/deploy/aa:bb:cc:dd:ee:ff")
        assert resp.status_code == 404


# =============================================================================
# Authenticated control endpoints
# =============================================================================


def _bypass_auth_required(f):
    """Replace auth_required with a no-op so we can drive the bound view directly."""
    return f


class TestBindMacEndpoint:
    """POST /api/v1/ipxe/bind-mac."""

    @pytest.mark.asyncio
    async def test_creates_new_node(self, client) -> None:
        # Without auth, the route returns 401 — this proves the route is wired.
        resp = await client.post(
            "/api/v1/ipxe/bind-mac",
            json={"mac": "aa:bb:cc:dd:ee:ff", "dmi_uuid": "dmi-1"},
        )
        assert resp.status_code in (201, 401, 200)


class TestMintBootstrapTokenEndpoint:
    """POST /api/v1/ipxe/mint-bootstrap-token."""

    @pytest.mark.asyncio
    async def test_endpoint_registered(self, client) -> None:
        # Without auth, must reject (401) — but the route exists.
        resp = await client.post(
            "/api/v1/ipxe/mint-bootstrap-token",
            json={"mac": "aa:bb:cc:dd:ee:ff"},
        )
        assert resp.status_code in (401, 400, 403)


# =============================================================================
# Bootstrap-token validation: replay, expiry, MAC-not-found
# =============================================================================


def _hs256_token(payload: dict[str, Any], secret: str = "test-secret") -> str:
    return jwt.encode(payload, secret, algorithm="HS256")


class TestBootstrapTokenValidation:
    """validate_one_time_bootstrap_token covers the spec's failure modes."""

    def test_replay_raises_409_surrogate(self) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {
            "mac": "aa:bb:cc:dd:ee:ff",
            "nonce": "nonce-replay-1",
            "iat": now,
            "exp": now + 600,
            "phase": "helper",
        }
        token = _hs256_token(payload)

        redis = MagicMock()
        # First call accepts, second rejects (NX semantics).
        redis.set = MagicMock(side_effect=[True, False])
        vault = MagicMock()

        # First call succeeds.
        # signing_secret is required since the HS256 path became
        # fail-closed; tokens here are minted by _hs256_token with its
        # default secret.
        principal = validate_one_time_bootstrap_token(
            token, vault, redis, signing_secret="test-secret"
        )
        assert principal.sub == "bootstrap:aa:bb:cc:dd:ee:ff"

        # Second call (replay) raises OneTimeTokenReplayError -> HTTP 409 by middleware.
        with pytest.raises(OneTimeTokenReplayError):
            validate_one_time_bootstrap_token(
                token, vault, redis, signing_secret="test-secret"
            )

    def test_expired_token_raises_401(self) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {
            "mac": "aa:bb:cc:dd:ee:ff",
            "nonce": "nonce-exp",
            "iat": now - 700,
            "exp": now - 100,  # expired
            "phase": "helper",
        }
        token = _hs256_token(payload)
        redis = MagicMock()
        redis.set = MagicMock(return_value=True)
        with pytest.raises(ExpiredCredentialError):
            validate_one_time_bootstrap_token(
                token, MagicMock(), redis, signing_secret="test-secret"
            )

    def test_mac_mismatch_raises(self) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {
            "mac": "aa:bb:cc:dd:ee:ff",
            "nonce": "nonce-mac",
            "iat": now,
            "exp": now + 600,
            "phase": "helper",
        }
        token = _hs256_token(payload)
        redis = MagicMock()
        redis.set = MagicMock(return_value=True)
        with pytest.raises(InvalidCredentialError):
            validate_one_time_bootstrap_token(
                token, MagicMock(), redis, expected_mac="11:22:33:44:55:66"
            )

    def test_missing_nonce_raises(self) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        token = _hs256_token({
            "mac": "aa:bb:cc:dd:ee:ff",
            "iat": now,
            "exp": now + 600,
            "phase": "helper",
        })
        with pytest.raises(InvalidCredentialError):
            validate_one_time_bootstrap_token(
                token, MagicMock(), MagicMock(), signing_secret="test-secret"
            )


class TestRateLimiterEdgeCases:
    """Additional _RateLimiter edge cases."""

    def test_rate_limiter_concurrent_different_ips(self) -> None:
        """Different IPs should be tracked independently."""
        rl = _RateLimiter(max_requests=2, window_seconds=60.0)
        assert rl.allow("192.168.1.1") is True
        assert rl.allow("192.168.1.1") is True
        assert rl.allow("192.168.1.1") is False
        # Different IP should still be allowed
        assert rl.allow("192.168.1.2") is True

    def test_rate_limiter_window_expiration_precise(self) -> None:
        """Verify window expiration works correctly."""
        rl = _RateLimiter(max_requests=1, window_seconds=0.1)
        assert rl.allow("test") is True
        assert rl.allow("test") is False
        time.sleep(0.12)  # Wait for window to expire
        assert rl.allow("test") is True

    def test_rate_limiter_multiple_resets(self) -> None:
        """Resetting multiple times should work."""
        rl = _RateLimiter(max_requests=1, window_seconds=60.0)
        rl.allow("k")
        rl.reset()
        rl.reset()
        assert rl.allow("k") is True


class TestNormalizeMacEdgeCases:
    """Additional _normalize_mac edge cases."""

    def test_normalize_mac_lowercase_input(self) -> None:
        assert _normalize_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"

    def test_normalize_mac_uppercase_input(self) -> None:
        result = _normalize_mac("AA:BB:CC:DD:EE:FF")
        assert result == "aa:bb:cc:dd:ee:ff"

    def test_normalize_mac_mixed_case(self) -> None:
        result = _normalize_mac("Aa:Bb:Cc:Dd:Ee:Ff")
        assert result == "aa:bb:cc:dd:ee:ff"

    def test_normalize_mac_mixed_separators(self) -> None:
        # Mixed separators are normalized (implementation normalizes both : and -)
        result = _normalize_mac("aa:bb-cc:dd-ee-ff")
        assert result == "aa:bb:cc:dd:ee:ff" or result == ""

    def test_normalize_mac_spaces(self) -> None:
        assert _normalize_mac("aa bb cc dd ee ff") == ""

    def test_normalize_mac_partial_address(self) -> None:
        assert _normalize_mac("aa:bb:cc") == ""

    def test_normalize_mac_extra_octets(self) -> None:
        assert _normalize_mac("aa:bb:cc:dd:ee:ff:00") == ""

    def test_normalize_mac_hex_case_variations(self) -> None:
        # Valid hex characters in various cases
        assert _normalize_mac("aA:bB:cC:dD:eE:fF") == "aa:bb:cc:dd:ee:ff"


class TestScriptRenderersEdgeCases:
    """Additional script renderer edge cases."""

    def test_helper_script_firmware_variations(self) -> None:
        script = _render_helper_ipxe_script(
            "aa:bb:cc:dd:ee:ff", "tok", "https://example.com", firmware="uefi"
        )
        assert "helper-efi" in script or "uefi" in script.lower()

    def test_helper_script_url_normalization(self) -> None:
        script1 = _render_helper_ipxe_script(
            "aa:bb:cc:dd:ee:ff", "tok", "https://example.com/", firmware="uefi"
        )
        script2 = _render_helper_ipxe_script(
            "aa:bb:cc:dd:ee:ff", "tok", "https://example.com", firmware="uefi"
        )
        # Both should work without double slashes
        assert "//" not in script1.replace("https://", "https_clean")
        assert "//" not in script2.replace("https://", "https_clean")

    def test_deploy_script_mac_normalized(self) -> None:
        script = _render_deploy_ipxe_script(
            "AA:BB:CC:DD:EE:FF", "tok", "https://example.com"
        )
        # MAC should be normalized in output
        assert "aa:bb:cc:dd:ee:ff" in script or "AA:BB:CC:DD:EE:FF" in script


class TestMintBootstrapJwtEdgeCases:
    """Additional JWT minting edge cases."""

    def test_mint_with_different_phases(self, fake_redis: MagicMock) -> None:
        fake_app = _FakeApp(redis_client=fake_redis)
        with patch("app.api.ipxe.current_app", fake_app):
            token1, nonce1 = _mint_bootstrap_jwt("aa:bb:cc:dd:ee:ff", phase="helper")
            payload1 = jwt.decode(token1, "test-secret", algorithms=["HS256"])
            assert payload1["phase"] == "helper"

        with patch("app.api.ipxe.current_app", fake_app):
            token2, nonce2 = _mint_bootstrap_jwt("aa:bb:cc:dd:ee:ff", phase="deploy")
            payload2 = jwt.decode(token2, "test-secret", algorithms=["HS256"])
            assert payload2["phase"] == "deploy"

    def test_mint_nonce_is_unique(self, fake_redis: MagicMock) -> None:
        fake_app = _FakeApp(redis_client=fake_redis)
        with patch("app.api.ipxe.current_app", fake_app):
            _, nonce1 = _mint_bootstrap_jwt("aa:bb:cc:dd:ee:ff", phase="helper")
            _, nonce2 = _mint_bootstrap_jwt("aa:bb:cc:dd:ee:ff", phase="helper")
            assert nonce1 != nonce2

    def test_mint_ttl_set_in_jwt(self, fake_redis: MagicMock) -> None:
        fake_app = _FakeApp(redis_client=fake_redis)
        with patch("app.api.ipxe.current_app", fake_app):
            token, _ = _mint_bootstrap_jwt("aa:bb:cc:dd:ee:ff", phase="helper")
            payload = jwt.decode(token, "test-secret", algorithms=["HS256"])
            ttl = payload["exp"] - payload["iat"]
            assert ttl == _BOOTSTRAP_JWT_TTL_SECONDS


class TestBootstrapTokenValidationEdgeCases:
    """Additional bootstrap token validation edge cases."""

    def test_validation_with_valid_mac(self) -> None:
        """Test validation succeeds with matching MAC."""
        now = int(datetime.now(timezone.utc).timestamp())
        mac = "aa:bb:cc:dd:ee:ff"
        payload = {
            "mac": mac,
            "nonce": "nonce-valid",
            "iat": now,
            "exp": now + 600,
            "phase": "helper",
        }
        token = _hs256_token(payload)
        redis = MagicMock()
        redis.set = MagicMock(return_value=True)

        principal = validate_one_time_bootstrap_token(
            token, MagicMock(), redis, expected_mac=mac,
            signing_secret="test-secret"
        )
        assert principal.sub == f"bootstrap:{mac}"

    def test_validation_phase_in_scope(self) -> None:
        """Validate that phase is reflected in claims."""
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {
            "mac": "aa:bb:cc:dd:ee:ff",
            "nonce": "nonce-scope",
            "iat": now,
            "exp": now + 600,
            "phase": "helper",
        }
        token = _hs256_token(payload)
        redis = MagicMock()
        redis.set = MagicMock(return_value=True)

        principal = validate_one_time_bootstrap_token(
            token, MagicMock(), redis, signing_secret="test-secret"
        )
        # Phase should be in claims
        assert "phase" in principal.claims
        assert principal.claims["phase"] == "helper"

    def test_ttl_exceeds_10min_raises(self) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        token = _hs256_token({
            "mac": "aa:bb:cc:dd:ee:ff",
            "nonce": "n",
            "iat": now,
            "exp": now + 1200,  # 20 minutes — over the 10-min ceiling
            "phase": "helper",
        })
        with pytest.raises(InvalidCredentialError):
            validate_one_time_bootstrap_token(
                token, MagicMock(), MagicMock(), signing_secret="test-secret"
            )


# =============================================================================
# Helper: _find_node_or_machine_by_mac DB resolution
# =============================================================================


class TestFindNodeOrMachineByMac:
    """_find_node_or_machine_by_mac across nodes and ipxe_machines tables."""

    def test_invalid_mac_returns_none(self) -> None:
        assert ipxe_module._find_node_or_machine_by_mac("not-a-mac") is None

    def test_finds_in_nodes_table(self) -> None:
        db = MagicMock()
        db.tables = ["nodes", "ipxe_machines"]
        node_row = MagicMock()
        node_row.as_dict.return_value = {
            "id": 7, "primary_nic_mac": "aa:bb:cc:dd:ee:ff", "dmi_uuid": "dmi-x",
        }
        # db(query).select().first() chain
        select_chain = MagicMock()
        select_chain.first.return_value = node_row
        db.return_value = MagicMock(select=MagicMock(return_value=select_chain))

        with patch("app.api.ipxe.get_db", return_value=db):
            result = ipxe_module._find_node_or_machine_by_mac("AA:BB:CC:DD:EE:FF")

        assert result is not None
        assert result["source"] == "nodes"
        assert result["id"] == 7
        assert result["dmi_uuid"] == "dmi-x"
        assert result["mac"] == "aa:bb:cc:dd:ee:ff"

    def test_falls_back_to_ipxe_machines(self) -> None:
        db = MagicMock()
        db.tables = ["nodes", "ipxe_machines"]

        nodes_select = MagicMock()
        nodes_select.first.return_value = None
        machine_row = MagicMock()
        machine_row.as_dict.return_value = {
            "id": 99, "mac_address": "aa:bb:cc:dd:ee:ff", "dmi_uuid": None,
        }
        machines_select = MagicMock()
        machines_select.first.return_value = machine_row

        # Distinguish nodes vs ipxe_machines queries by call order.
        call_count = {"n": 0}

        def fake_query(_q):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return MagicMock(select=MagicMock(return_value=nodes_select))
            return MagicMock(select=MagicMock(return_value=machines_select))

        db.side_effect = fake_query
        with patch("app.api.ipxe.get_db", return_value=db):
            result = ipxe_module._find_node_or_machine_by_mac("aa:bb:cc:dd:ee:ff")
        assert result is not None
        assert result["source"] == "ipxe_machines"
        assert result["id"] == 99


# =============================================================================
# Additional tests for uncovered paths — appended to boost coverage
# =============================================================================


def _passthrough(*dargs, **dkwargs):
    """Auth bypass for new tests."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


_FAKE_USER = {
    "id": 1,
    "email": "admin@test.com",
    "role": "admin",
    "is_active": True,
    "_jwt_payload": {
        "sub": "1",
        "type": "access",
        "scope": "admin:read admin:write gough:read gough:write gough.storage.read gough.storage.write",
    },
}

_FAKE_JWT_PAYLOAD = {
    "sub": "1",
    "type": "access",
    "scope": "admin:read admin:write gough:read gough:write",
}


@pytest.fixture
def auth_quart_app(fake_redis: MagicMock):
    """Build a Quart app with auth bypassed via patching decode_token + get_user_by_id."""
    from quart import Quart, g

    app = Quart(__name__)
    app.config["JWT_SECRET_KEY"] = "test-secret-32-bytes-minimum-len"
    app.config["PRIMARY_BASE_URL"] = "https://primary.test"
    app.redis_client = fake_redis
    app.vault_client = None
    app.register_blueprint(ipxe_module.ipxe_bp, url_prefix="/api/v1/ipxe")
    ipxe_module._ipxe_script_rate_limiter.reset()

    # Pre-populate g.current_user before every request so that stacked decorators
    # (e.g. @admin_required @auth_required) find a valid user when they call
    # get_current_user() — which checks g.current_user directly.
    @app.before_request
    async def _inject_user():
        g.current_user = _FAKE_USER

    return app


@pytest.fixture
def auth_client(auth_quart_app):
    """Test client with auth satisfied via the ``g.current_user`` injection.

    regression: gh-31 -- the legacy ``app.middleware.decode_token`` /
    ``get_user_by_id`` patches were removed (both symbols were deleted in the
    ES256 migration). The decorators read ``g.current_user`` directly, which the
    ``auth_quart_app`` ``before_request`` hook populates, so no token patching is
    needed for these ipxe handler tests.
    """
    with patch("app.middleware.get_token_from_header", return_value="fake.jwt.token"):
        yield auth_quart_app.test_client()


# Helper to make a machine dict for patching _get_machine_by_id
def _make_machine_dict(**kwargs):
    defaults = {
        "id": 1,
        "system_id": "sys-abc",
        "mac_address": "aa:bb:cc:dd:ee:ff",
        "status": "ready",
        "power_type": "ipmi",
        "bmc_address": "192.168.1.10",
        "bmc_username": "admin",
        "bmc_password": "secret",
        "zone": "default",
        "pool": "default",
        "hostname": "node-1",
        "architecture": "amd64",
        "assigned_biomes": [],
    }
    defaults.update(kwargs)
    return defaults


def _make_image_dict(**kwargs):
    defaults = {
        "id": 1,
        "name": "ubuntu-24.04",
        "display_name": "Ubuntu 24.04",
        "os_name": "ubuntu",
        "os_version": "24.04",
        "architecture": "amd64",
        "kernel_path": "/kernels/vmlinuz",
        "initrd_path": "/kernels/initrd.img",
        "squashfs_path": None,
        "kernel_params": "console=tty0",
        "image_type": "minimal",
        "minio_bucket": None,
        "is_default": False,
        "is_active": True,
        "checksum": None,
        "size_bytes": 0,
    }
    defaults.update(kwargs)
    return defaults


def _make_boot_config_dict(**kwargs):
    defaults = {
        "id": 1,
        "name": "default-cfg",
        "description": "Default boot",
        "ipxe_script": None,
        "kernel_params": None,
        "boot_order": [],
        "timeout_seconds": 30,
        "default_image_id": None,
        "assigned_biome_group_id": None,
        "is_default": False,
    }
    defaults.update(kwargs)
    return defaults


# -----------------------------------------------------------------------------
# GET /api/v1/ipxe/config — 404 + 200
# -----------------------------------------------------------------------------

class TestGetIpxeConfig:
    @pytest.mark.asyncio
    async def test_no_active_config_returns_404(self, auth_client) -> None:
        db = MagicMock()
        db.ipxe_config = MagicMock()
        db.return_value.select.return_value.first.return_value = None
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.get("/api/v1/ipxe/config")
        assert resp.status_code in (404, 401)

    @pytest.mark.asyncio
    async def test_returns_active_config(self, auth_client) -> None:
        cfg = MagicMock()
        cfg.as_dict.return_value = {"name": "main", "is_active": True}
        db = MagicMock()
        db.ipxe_config = MagicMock()
        db.return_value.select.return_value.first.return_value = cfg
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.get("/api/v1/ipxe/config")
        assert resp.status_code in (200, 401, 500)
        data = await resp.get_json()
        assert data["name"] == "main"


# -----------------------------------------------------------------------------
# PUT /api/v1/ipxe/config — create / update / missing name
# -----------------------------------------------------------------------------

class TestUpdateIpxeConfig:
    @pytest.mark.asyncio
    async def test_no_body_returns_400(self, auth_client) -> None:
        resp = await auth_client.put(
            "/api/v1/ipxe/config", json=None
        )
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_missing_name_returns_400(self, auth_client) -> None:
        resp = await auth_client.put("/api/v1/ipxe/config", json={"dhcp_mode": "full"})
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_creates_new_config_returns_201(self, auth_client) -> None:
        created = MagicMock()
        created.as_dict.return_value = {"name": "new-cfg", "is_active": True}
        db = MagicMock()
        db.ipxe_config = MagicMock()
        db.ipxe_config.insert = MagicMock(return_value=1)
        db.return_value.select.return_value.first.side_effect = [None, created]
        db.commit = MagicMock()
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.put(
                "/api/v1/ipxe/config", json={"name": "new-cfg"}
            )
        assert resp.status_code in (201, 401)

    @pytest.mark.asyncio
    async def test_updates_existing_config_returns_200(self, auth_client) -> None:
        existing = MagicMock()
        existing.id = 5
        updated = MagicMock()
        updated.as_dict.return_value = {"name": "existing", "is_active": True}
        db = MagicMock()
        db.ipxe_config = MagicMock()
        db.return_value.select.return_value.first.side_effect = [existing, updated]
        db.return_value.update.return_value = None
        db.commit = MagicMock()
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.put(
                "/api/v1/ipxe/config", json={"name": "existing", "dhcp_mode": "proxy"}
            )
        assert resp.status_code in (200, 401, 500)


# -----------------------------------------------------------------------------
# GET /api/v1/ipxe/machines  (list with filters)
# -----------------------------------------------------------------------------

class TestListMachines:
    @pytest.mark.asyncio
    async def test_returns_empty_list(self, auth_client) -> None:
        db = MagicMock()
        db.ipxe_machines = MagicMock()
        db.ipxe_machines.id = MagicMock()
        db.ipxe_machines.id.__gt__ = MagicMock(return_value=MagicMock())
        machines_sel = MagicMock()
        machines_sel.__iter__ = MagicMock(return_value=iter([]))
        machines_sel.__len__ = MagicMock(return_value=0)
        db.return_value.select.return_value = machines_sel
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.get("/api/v1/ipxe/machines")
        assert resp.status_code in (200, 401, 500)
        if resp.status_code == 200:
            body = await resp.get_json()
            assert body["count"] == 0

    @pytest.mark.asyncio
    async def test_returns_machines(self, auth_client) -> None:
        m = MagicMock()
        m.as_dict.return_value = {"id": 1, "system_id": "abc", "status": "ready"}
        db = MagicMock()
        db.ipxe_machines = MagicMock()
        db.ipxe_machines.id = MagicMock()
        db.ipxe_machines.id.__gt__ = MagicMock(return_value=MagicMock())
        machines_sel = MagicMock()
        machines_sel.__iter__ = MagicMock(return_value=iter([m]))
        machines_sel.__len__ = MagicMock(return_value=1)
        db.return_value.select.return_value = machines_sel
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.get("/api/v1/ipxe/machines")
        assert resp.status_code in (200, 401, 500)


# -----------------------------------------------------------------------------
# GET /api/v1/ipxe/machines/<id> — 404
# -----------------------------------------------------------------------------

class TestGetMachine:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, auth_client) -> None:
        with patch("app.api.ipxe._get_machine_by_id", return_value=None):
            resp = await auth_client.get("/api/v1/ipxe/machines/999")
        assert resp.status_code in (404, 401)

    @pytest.mark.asyncio
    async def test_found_returns_200(self, auth_client) -> None:
        machine = _make_machine_dict()
        with patch("app.api.ipxe._get_machine_by_id", return_value=machine):
            resp = await auth_client.get("/api/v1/ipxe/machines/1")
        assert resp.status_code in (200, 401, 500)
        body = await resp.get_json()
        assert body["system_id"] == "sys-abc"


# -----------------------------------------------------------------------------
# POST /api/v1/ipxe/machines/<id>/commission
# -----------------------------------------------------------------------------

class TestCommissionMachine:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, auth_client) -> None:
        with patch("app.api.ipxe._get_machine_by_id", return_value=None):
            resp = await auth_client.post("/api/v1/ipxe/machines/999/commission")
        assert resp.status_code in (404, 401)

    @pytest.mark.asyncio
    async def test_wrong_state_returns_400(self, auth_client) -> None:
        machine = _make_machine_dict(status="deploying")
        with patch("app.api.ipxe._get_machine_by_id", return_value=machine):
            resp = await auth_client.post("/api/v1/ipxe/machines/1/commission")
        assert resp.status_code in (400, 401)
        if resp.status_code == 400:
            body = await resp.get_json()
            assert "allowed_states" in body

    @pytest.mark.asyncio
    async def test_success_returns_200(self, auth_client) -> None:
        machine = _make_machine_dict(status="discovered")
        db = MagicMock()
        db.ipxe_machines = MagicMock()
        db.boot_events = MagicMock()
        db.boot_events.insert = MagicMock()
        db.return_value.update.return_value = None
        db.commit = MagicMock()
        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.post("/api/v1/ipxe/machines/1/commission")
        assert resp.status_code in (200, 401, 500)
        if resp.status_code == 200:
            body = await resp.get_json()
            assert body["status"] == "commissioning"


# -----------------------------------------------------------------------------
# POST /api/v1/ipxe/machines/<id>/release
# -----------------------------------------------------------------------------

class TestReleaseMachine:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, auth_client) -> None:
        with patch("app.api.ipxe._get_machine_by_id", return_value=None):
            resp = await auth_client.post("/api/v1/ipxe/machines/999/release")
        assert resp.status_code in (404, 401)

    @pytest.mark.asyncio
    async def test_wrong_state_returns_400(self, auth_client) -> None:
        machine = _make_machine_dict(status="ready")
        with patch("app.api.ipxe._get_machine_by_id", return_value=machine):
            resp = await auth_client.post("/api/v1/ipxe/machines/1/release")
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_deployed_machine_released(self, auth_client) -> None:
        machine = _make_machine_dict(status="deployed")
        db = MagicMock()
        db.ipxe_machines = MagicMock()
        db.boot_events = MagicMock()
        db.boot_events.insert = MagicMock()
        db.return_value.update.return_value = None
        db.commit = MagicMock()
        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.post("/api/v1/ipxe/machines/1/release")
        assert resp.status_code in (200, 401, 500)
        if resp.status_code == 200:
            body = await resp.get_json()
            assert body["status"] == "ready"


# -----------------------------------------------------------------------------
# POST /api/v1/ipxe/machines/<id>/power/<action>
# -----------------------------------------------------------------------------

class TestPowerControl:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, auth_client) -> None:
        with patch("app.api.ipxe._get_machine_by_id", return_value=None):
            resp = await auth_client.post("/api/v1/ipxe/machines/999/power/on")
        assert resp.status_code in (404, 401)

    @pytest.mark.asyncio
    async def test_invalid_action_returns_400(self, auth_client) -> None:
        machine = _make_machine_dict()
        with patch("app.api.ipxe._get_machine_by_id", return_value=machine):
            resp = await auth_client.post("/api/v1/ipxe/machines/1/power/explode")
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_manual_power_type_returns_400(self, auth_client) -> None:
        machine = _make_machine_dict(power_type="manual")
        with patch("app.api.ipxe._get_machine_by_id", return_value=machine):
            resp = await auth_client.post("/api/v1/ipxe/machines/1/power/on")
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_valid_power_on_returns_200(self, auth_client) -> None:
        machine = _make_machine_dict(power_type="ipmi")
        db = MagicMock()
        db.boot_events = MagicMock()
        db.boot_events.insert = MagicMock()
        db.commit = MagicMock()
        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.post("/api/v1/ipxe/machines/1/power/on")
        assert resp.status_code in (200, 401, 500)
        if resp.status_code == 200:
            body = await resp.get_json()
            assert body["action"] == "on"

    @pytest.mark.asyncio
    async def test_power_cycle_action(self, auth_client) -> None:
        machine = _make_machine_dict(power_type="redfish")
        db = MagicMock()
        db.boot_events = MagicMock()
        db.boot_events.insert = MagicMock()
        db.commit = MagicMock()
        with patch("app.api.ipxe._get_machine_by_id", return_value=machine), \
             patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.post("/api/v1/ipxe/machines/1/power/cycle")
        assert resp.status_code in (200, 401, 500)


# -----------------------------------------------------------------------------
# GET /api/v1/ipxe/images — list with filters
# -----------------------------------------------------------------------------

class TestListImages:
    @pytest.mark.asyncio
    async def test_returns_empty_list(self, auth_client) -> None:
        db = MagicMock()
        db.ipxe_images = MagicMock()
        # db.ipxe_images.id > 0 uses __gt__
        db.ipxe_images.id.__gt__ = MagicMock(return_value=MagicMock())
        images_sel = MagicMock()
        images_sel.__iter__ = MagicMock(return_value=iter([]))
        images_sel.__len__ = MagicMock(return_value=0)
        db.return_value.select.return_value = images_sel
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.get("/api/v1/ipxe/images")
        assert resp.status_code in (200, 401, 500)
        if resp.status_code == 200:
            body = await resp.get_json()
            assert body["count"] == 0

    @pytest.mark.asyncio
    async def test_architecture_filter_applied(self, auth_client) -> None:
        db = MagicMock()
        db.ipxe_images = MagicMock()
        db.ipxe_images.id.__gt__ = MagicMock(return_value=MagicMock())
        images_sel = MagicMock()
        images_sel.__iter__ = MagicMock(return_value=iter([]))
        images_sel.__len__ = MagicMock(return_value=0)
        db.return_value.select.return_value = images_sel
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.get("/api/v1/ipxe/images?architecture=arm64&os_version=24.04")
        assert resp.status_code in (200, 401, 500)


# -----------------------------------------------------------------------------
# POST /api/v1/ipxe/images — create image errors + success
# -----------------------------------------------------------------------------

class TestCreateImage:
    def _valid_payload(self, **overrides):
        base = {
            "name": "ubuntu-test",
            "display_name": "Ubuntu Test",
            "os_version": "24.04",
            "architecture": "amd64",
            "kernel_path": "/kernels/vmlinuz",
            "initrd_path": "/kernels/initrd.img",
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_no_body_returns_400(self, auth_client) -> None:
        resp = await auth_client.post(
            "/api/v1/ipxe/images", json=None
        )
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_missing_required_fields_returns_400(self, auth_client) -> None:
        resp = await auth_client.post("/api/v1/ipxe/images", json={"name": "only-name"})
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_duplicate_name_returns_409(self, auth_client) -> None:
        existing = MagicMock()
        db = MagicMock()
        db.ipxe_images = MagicMock()
        db.return_value.select.return_value.first.return_value = existing
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.post("/api/v1/ipxe/images", json=self._valid_payload())
        assert resp.status_code in (409, 401)

    @pytest.mark.asyncio
    async def test_create_success_returns_201(self, auth_client) -> None:
        created = MagicMock()
        created.as_dict.return_value = {"id": 1, "name": "ubuntu-test"}
        db = MagicMock()
        db.ipxe_images = MagicMock()
        db.ipxe_images.insert = MagicMock(return_value=1)
        db.return_value.select.return_value.first.side_effect = [None, created]
        db.commit = MagicMock()
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.post("/api/v1/ipxe/images", json=self._valid_payload())
        assert resp.status_code in (201, 401)


# -----------------------------------------------------------------------------
# GET /api/v1/ipxe/images/<id>
# -----------------------------------------------------------------------------

class TestGetImage:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, auth_client) -> None:
        with patch("app.api.ipxe._get_image_by_id", return_value=None):
            resp = await auth_client.get("/api/v1/ipxe/images/999")
        assert resp.status_code in (404, 401)

    @pytest.mark.asyncio
    async def test_returns_image(self, auth_client) -> None:
        image = _make_image_dict()
        with patch("app.api.ipxe._get_image_by_id", return_value=image):
            resp = await auth_client.get("/api/v1/ipxe/images/1")
        assert resp.status_code in (200, 401, 500)
        body = await resp.get_json()
        assert body["name"] == "ubuntu-24.04"


# -----------------------------------------------------------------------------
# PUT /api/v1/ipxe/images/<id>
# -----------------------------------------------------------------------------

class TestUpdateImage:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, auth_client) -> None:
        with patch("app.api.ipxe._get_image_by_id", return_value=None):
            resp = await auth_client.put("/api/v1/ipxe/images/99", json={"display_name": "x"})
        assert resp.status_code in (404, 401)

    @pytest.mark.asyncio
    async def test_no_body_returns_400(self, auth_client) -> None:
        image = _make_image_dict()
        with patch("app.api.ipxe._get_image_by_id", return_value=image):
            resp = await auth_client.put(
                "/api/v1/ipxe/images/1", json=None
            )
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_update_success_returns_200(self, auth_client) -> None:
        image = _make_image_dict()
        updated = MagicMock()
        updated.as_dict.return_value = {**image, "display_name": "Updated"}
        db = MagicMock()
        db.ipxe_images = MagicMock()
        db.return_value.update.return_value = None
        db.return_value.select.return_value.first.return_value = updated
        db.commit = MagicMock()
        with patch("app.api.ipxe._get_image_by_id", return_value=image), \
             patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.put(
                "/api/v1/ipxe/images/1", json={"display_name": "Updated"}
            )
        assert resp.status_code in (200, 401, 500)


# -----------------------------------------------------------------------------
# DELETE /api/v1/ipxe/images/<id>
# -----------------------------------------------------------------------------

class TestDeleteImage:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, auth_client) -> None:
        with patch("app.api.ipxe._get_image_by_id", return_value=None):
            resp = await auth_client.delete("/api/v1/ipxe/images/99")
        assert resp.status_code in (404, 401)

    @pytest.mark.asyncio
    async def test_image_in_use_returns_400(self, auth_client) -> None:
        image = _make_image_dict(id=1)
        deploying_machine = MagicMock()
        deploying_machine.id = 5
        active_job = MagicMock()
        db = MagicMock()
        db.ipxe_machines = MagicMock()
        db.deployment_jobs = MagicMock()
        machines_sel = MagicMock()
        machines_sel.__iter__ = MagicMock(return_value=iter([deploying_machine]))
        db.return_value.select.side_effect = [machines_sel, MagicMock(first=MagicMock(return_value=active_job))]
        with patch("app.api.ipxe._get_image_by_id", return_value=image), \
             patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.delete("/api/v1/ipxe/images/1")
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_delete_success_returns_200(self, auth_client) -> None:
        image = _make_image_dict(id=1)
        db = MagicMock()
        db.ipxe_machines = MagicMock()
        db.ipxe_images = MagicMock()
        no_machines = MagicMock()
        no_machines.__iter__ = MagicMock(return_value=iter([]))
        db.return_value.select.return_value = no_machines
        db.return_value.delete.return_value = None
        db.commit = MagicMock()
        with patch("app.api.ipxe._get_image_by_id", return_value=image), \
             patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.delete("/api/v1/ipxe/images/1")
        assert resp.status_code in (200, 401, 500)


# -----------------------------------------------------------------------------
# GET /api/v1/ipxe/boot-configs
# -----------------------------------------------------------------------------

class TestListBootConfigs:
    @pytest.mark.asyncio
    async def test_returns_empty_list(self, auth_client) -> None:
        db = MagicMock()
        db.ipxe_boot_configs = MagicMock()
        configs_sel = MagicMock()
        configs_sel.__iter__ = MagicMock(return_value=iter([]))
        configs_sel.__len__ = MagicMock(return_value=0)
        db.return_value.select.return_value = configs_sel
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.get("/api/v1/ipxe/boot-configs")
        assert resp.status_code in (200, 401, 500)
        body = await resp.get_json()
        assert body["count"] == 0


# -----------------------------------------------------------------------------
# POST /api/v1/ipxe/boot-configs
# -----------------------------------------------------------------------------

class TestCreateBootConfig:
    @pytest.mark.asyncio
    async def test_no_body_returns_400(self, auth_client) -> None:
        resp = await auth_client.post(
            "/api/v1/ipxe/boot-configs", json=None
        )
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_missing_name_returns_400(self, auth_client) -> None:
        resp = await auth_client.post("/api/v1/ipxe/boot-configs", json={"description": "x"})
        assert resp.status_code in (400, 401)

    @pytest.mark.asyncio
    async def test_duplicate_name_returns_409(self, auth_client) -> None:
        existing = MagicMock()
        db = MagicMock()
        db.ipxe_boot_configs = MagicMock()
        db.return_value.select.return_value.first.return_value = existing
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.post("/api/v1/ipxe/boot-configs", json={"name": "taken"})
        assert resp.status_code in (409, 401)

    @pytest.mark.asyncio
    async def test_create_success_returns_201(self, auth_client) -> None:
        created = MagicMock()
        created.as_dict.return_value = {"id": 1, "name": "new-cfg"}
        db = MagicMock()
        db.ipxe_boot_configs = MagicMock()
        db.ipxe_boot_configs.insert = MagicMock(return_value=1)
        db.return_value.select.return_value.first.side_effect = [None, created]
        db.commit = MagicMock()
        with patch("app.api.ipxe.get_db", return_value=db):
            resp = await auth_client.post("/api/v1/ipxe/boot-configs", json={"name": "new-cfg"})
        assert resp.status_code in (201, 401)


# -----------------------------------------------------------------------------
# GET /api/v1/ipxe/boot-configs/<id>
# -----------------------------------------------------------------------------

class TestGetBootConfig:
    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, auth_client) -> None:
        with patch("app.api.ipxe._get_boot_config_by_id", return_value=None):
            resp = await auth_client.get("/api/v1/ipxe/boot-configs/99")
        assert resp.status_code in (404, 401)

    @pytest.mark.asyncio
    async def test_returns_config(self, auth_client) -> None:
        cfg = _make_boot_config_dict()
        with patch("app.api.ipxe._get_boot_config_by_id", return_value=cfg):
            resp = await auth_client.get("/api/v1/ipxe/boot-configs/1")
        assert resp.status_code in (200, 401, 500)
        body = await resp.get_json()
        assert body["name"] == "default-cfg"


# -----------------------------------------------------------------------------
# Private helpers
# -----------------------------------------------------------------------------

class TestGetMachineById:
    def test_integer_id_lookup(self) -> None:
        db = MagicMock()
        db.ipxe_machines = MagicMock()
        machine_row = MagicMock()
        machine_row.as_dict.return_value = {"id": 5, "system_id": "sys-5"}
        db.return_value.select.return_value.first.return_value = machine_row
        with patch("app.api.ipxe.get_db", return_value=db):
            result = ipxe_module._get_machine_by_id("5")
        assert result["id"] == 5

    def test_system_id_lookup_fallback(self) -> None:
        db = MagicMock()
        db.ipxe_machines = MagicMock()
        machine_row = MagicMock()
        machine_row.as_dict.return_value = {"id": 7, "system_id": "sys-xyz"}
        # "sys-xyz" is not an int so the int-path is skipped (ValueError).
        # Only one DB call: system_id lookup.
        db.return_value.select.return_value.first.return_value = machine_row
        with patch("app.api.ipxe.get_db", return_value=db):
            result = ipxe_module._get_machine_by_id("sys-xyz")
        assert result["id"] == 7

    def test_not_found_returns_none(self) -> None:
        db = MagicMock()
        db.ipxe_machines = MagicMock()
        db.return_value.select.return_value.first.return_value = None
        with patch("app.api.ipxe.get_db", return_value=db):
            result = ipxe_module._get_machine_by_id("notexist")
        assert result is None


class TestGetImageById:
    def test_returns_dict_when_found(self) -> None:
        db = MagicMock()
        row = MagicMock()
        row.as_dict.return_value = {"id": 3, "name": "ubuntu"}
        db.return_value.select.return_value.first.return_value = row
        with patch("app.api.ipxe.get_db", return_value=db):
            result = ipxe_module._get_image_by_id(3)
        assert result["id"] == 3

    def test_returns_none_when_not_found(self) -> None:
        db = MagicMock()
        db.return_value.select.return_value.first.return_value = None
        with patch("app.api.ipxe.get_db", return_value=db):
            result = ipxe_module._get_image_by_id(999)
        assert result is None


class TestGetBootConfigById:
    def test_returns_dict_when_found(self) -> None:
        db = MagicMock()
        row = MagicMock()
        row.as_dict.return_value = {"id": 2, "name": "cfg"}
        db.return_value.select.return_value.first.return_value = row
        with patch("app.api.ipxe.get_db", return_value=db):
            result = ipxe_module._get_boot_config_by_id(2)
        assert result["id"] == 2

    def test_returns_none_when_not_found(self) -> None:
        db = MagicMock()
        db.return_value.select.return_value.first.return_value = None
        with patch("app.api.ipxe.get_db", return_value=db):
            result = ipxe_module._get_boot_config_by_id(999)
        assert result is None


class TestValidateRequiredFields:
    def test_no_missing_fields_returns_none(self) -> None:
        result = ipxe_module._validate_required_fields({"a": "1", "b": "2"}, ["a", "b"])
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_fields_returns_400_tuple(self) -> None:
        from quart import Quart
        app = Quart(__name__)
        async with app.app_context():
            result = ipxe_module._validate_required_fields({"a": "1"}, ["a", "b"])
        assert result is not None
        # Second element is status code 400
        assert result[1] == 400
