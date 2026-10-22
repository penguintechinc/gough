"""Tests for app/security/webhook_keys.py.

Coverage:
- ensure_keys_for_tenant idempotency, rotate, JWKS export
- HMAC key minting, retrieval, rotation, exclusion from JWKS
- Mode validation, tenant validation
- JWKS grace-period filtering
"""

from __future__ import annotations

import base64
import datetime as _dt
from unittest.mock import patch

import pytest

from app.security.webhook_keys import (
    ECDSA_P256_SHA256,
    ED25519,
    GRACE_PERIOD_DAYS,
    HMAC_SHA256,
    KeyId,
    VALID_MODES,
    WebhookKeyManager,
)
from tests._webhook_fakes import FakeVaultClient


@pytest.fixture
def vault() -> FakeVaultClient:
    return FakeVaultClient()


@pytest.fixture
def mgr(vault: FakeVaultClient) -> WebhookKeyManager:
    return WebhookKeyManager(vault)


class TestValidation:
    def test_rejects_none_vault(self) -> None:
        with pytest.raises(ValueError):
            WebhookKeyManager(None)

    def test_invalid_mode_raises(self, mgr: WebhookKeyManager) -> None:
        with pytest.raises(ValueError):
            mgr.ensure_keys_for_tenant("t1", "rsa")

    def test_empty_tenant_raises(self, mgr: WebhookKeyManager) -> None:
        with pytest.raises(ValueError):
            mgr.ensure_keys_for_tenant("", ED25519)

    def test_valid_modes_set(self) -> None:
        assert VALID_MODES == frozenset({HMAC_SHA256, ECDSA_P256_SHA256, ED25519})


class TestEnsureKeysAsymmetric:
    def test_creates_ed25519_transit_key(
        self, mgr: WebhookKeyManager, vault: FakeVaultClient
    ) -> None:
        kid = mgr.ensure_keys_for_tenant("acme", ED25519)
        assert kid.tenant_id == "acme"
        assert kid.mode == ED25519
        assert kid.kid.startswith(f"{ED25519}-v")
        assert "acme/webhook-ed25519" in vault.secrets.transit.keys

    def test_creates_ecdsa_transit_key(
        self, mgr: WebhookKeyManager, vault: FakeVaultClient
    ) -> None:
        kid = mgr.ensure_keys_for_tenant("acme", ECDSA_P256_SHA256)
        assert vault.secrets.transit.keys["acme/webhook-ecdsa_p256_sha256"].key_type == "ecdsa-p256"
        assert kid.kid.startswith("ecdsa_p256_sha256-v")

    def test_idempotent(self, mgr: WebhookKeyManager, vault: FakeVaultClient) -> None:
        a = mgr.ensure_keys_for_tenant("acme", ED25519)
        b = mgr.ensure_keys_for_tenant("acme", ED25519)
        assert a.kid == b.kid
        assert len(vault.secrets.transit.keys) == 1


class TestRotate:
    def test_rotate_creates_new_version(
        self, mgr: WebhookKeyManager, vault: FakeVaultClient
    ) -> None:
        v1 = mgr.ensure_keys_for_tenant("acme", ED25519)
        v2 = mgr.rotate("acme", ED25519)
        assert v1.kid != v2.kid
        key = vault.secrets.transit.keys["acme/webhook-ed25519"]
        assert key.latest_version == 2
        assert set(key.versions) == {1, 2}

    def test_rotate_unknown_mode(self, mgr: WebhookKeyManager) -> None:
        with pytest.raises(ValueError):
            mgr.rotate("acme", "rsa")


class TestJWKS:
    def test_empty_when_no_keys(self, mgr: WebhookKeyManager) -> None:
        assert mgr.get_jwks("acme") == {"keys": []}

    def test_includes_ed25519_after_create(self, mgr: WebhookKeyManager) -> None:
        mgr.ensure_keys_for_tenant("acme", ED25519)
        jwks = mgr.get_jwks("acme")
        assert len(jwks["keys"]) == 1
        k = jwks["keys"][0]
        assert k["kty"] == "OKP"
        assert k["crv"] == "Ed25519"
        assert k["alg"] == "EdDSA"
        assert k["kid"].startswith("ed25519-v")
        # base64url x must decode to 32 bytes (Ed25519 public key length).
        x = k["x"]
        pad = (-len(x)) % 4
        raw = base64.urlsafe_b64decode(x + ("=" * pad))
        assert len(raw) == 32

    def test_includes_ecdsa(self, mgr: WebhookKeyManager) -> None:
        mgr.ensure_keys_for_tenant("acme", ECDSA_P256_SHA256)
        jwks = mgr.get_jwks("acme")
        assert any(k["kty"] == "EC" and k["crv"] == "P-256" for k in jwks["keys"])

    def test_includes_both_modes(self, mgr: WebhookKeyManager) -> None:
        mgr.ensure_keys_for_tenant("acme", ED25519)
        mgr.ensure_keys_for_tenant("acme", ECDSA_P256_SHA256)
        jwks = mgr.get_jwks("acme")
        kinds = {k["kty"] for k in jwks["keys"]}
        assert kinds == {"OKP", "EC"}

    def test_grace_period_keeps_old_kid(
        self, mgr: WebhookKeyManager, vault: FakeVaultClient
    ) -> None:
        mgr.ensure_keys_for_tenant("acme", ED25519)
        mgr.rotate("acme", ED25519)
        jwks = mgr.get_jwks("acme")
        kids = sorted(k["kid"] for k in jwks["keys"])
        assert kids == [f"{ED25519}-v1", f"{ED25519}-v2"]

    def test_grace_period_drops_expired(
        self, mgr: WebhookKeyManager, vault: FakeVaultClient
    ) -> None:
        mgr.ensure_keys_for_tenant("acme", ED25519)
        mgr.rotate("acme", ED25519)
        # Manually backdate v1 past the grace cutoff.
        key = vault.secrets.transit.keys["acme/webhook-ed25519"]
        old = (
            _dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(days=GRACE_PERIOD_DAYS + 1)
        ).isoformat()
        key.versions[1].creation_time = old
        jwks = mgr.get_jwks("acme")
        kids = [k["kid"] for k in jwks["keys"]]
        assert kids == [f"{ED25519}-v2"]

    def test_hmac_excluded_from_jwks(self, mgr: WebhookKeyManager) -> None:
        mgr.ensure_keys_for_tenant("acme", HMAC_SHA256)
        assert mgr.get_jwks("acme") == {"keys": []}


class TestHMAC:
    def test_create_and_get_secret(
        self, mgr: WebhookKeyManager, vault: FakeVaultClient
    ) -> None:
        kid = mgr.ensure_keys_for_tenant("acme", HMAC_SHA256)
        secret = mgr.get_hmac_secret("acme", kid.kid)
        assert isinstance(secret, bytes)
        assert len(secret) == 32

    def test_idempotent_returns_same_kid(self, mgr: WebhookKeyManager) -> None:
        a = mgr.ensure_keys_for_tenant("acme", HMAC_SHA256)
        b = mgr.ensure_keys_for_tenant("acme", HMAC_SHA256)
        assert a.kid == b.kid

    def test_rotate_changes_secret(self, mgr: WebhookKeyManager) -> None:
        a = mgr.ensure_keys_for_tenant("acme", HMAC_SHA256)
        old_secret = mgr.get_hmac_secret("acme", a.kid)
        b = mgr.rotate("acme", HMAC_SHA256)
        assert a.kid != b.kid
        new_secret = mgr.get_hmac_secret("acme", b.kid)
        assert old_secret != new_secret

    def test_get_missing_secret_raises(self, mgr: WebhookKeyManager) -> None:
        with pytest.raises(KeyError):
            mgr.get_hmac_secret("acme", "missing")


class TestKeyIdHelpers:
    def test_vault_paths(self) -> None:
        kid = KeyId(
            tenant_id="acme",
            mode=ED25519,
            kid="ed25519-v1",
            created_at=_dt.datetime.now(_dt.timezone.utc),
        )
        assert kid.vault_key_name() == "acme/webhook-ed25519"
        assert kid.kv_path() == "gough/webhooks/hmac/acme/ed25519-v1"
