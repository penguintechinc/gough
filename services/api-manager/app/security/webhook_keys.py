"""Webhook signing key management backed by HashiCorp Vault transit.

Provides per-tenant key lifecycle (create, rotate, JWKS export) for the three
signing modes supported by Gough webhooks per the spec:

  - hmac_sha256             — symmetric HMAC; secret stored in Vault KV.
  - ecdsa_p256_sha256       — ECDSA over NIST P-256 (SHA-256); Vault transit.
  - ed25519                 — Ed25519 (default); Vault transit.

Public keys for asymmetric modes are exported as a JWKS document for consumer
verification via the anonymous ``GET /api/v1/webhooks/keys/{tenant}`` endpoint.
Rotation creates a new ``kid`` and retains the previous key for a 30-day grace
window per the cryptographic algorithm defaults in the platform spec.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import os
import secrets
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Public valid signing modes -- mirrored in app/api/webhooks.py.
HMAC_SHA256: str = "hmac_sha256"
ECDSA_P256_SHA256: str = "ecdsa_p256_sha256"
ED25519: str = "ed25519"

VALID_MODES: frozenset[str] = frozenset({HMAC_SHA256, ECDSA_P256_SHA256, ED25519})

# Spec: previous keys retained 30 days after rotation.
GRACE_PERIOD_DAYS: int = 30

# Vault paths.
_TRANSIT_MOUNT: str = "transit"
_KV_MOUNT: str = "secret"
_KV_HMAC_PREFIX: str = "gough/webhooks/hmac"


def _b64url(data: bytes) -> str:
    """RFC 7515 base64url, no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


@dataclass(slots=True, frozen=True)
class KeyId:
    """Stable identifier for a webhook signing key version."""

    tenant_id: str
    mode: str
    kid: str
    created_at: _dt.datetime

    def vault_key_name(self) -> str:
        """Vault transit key name."""
        return f"{self.tenant_id}/webhook-{self.mode}"

    def kv_path(self) -> str:
        """Vault KV path (HMAC mode only)."""
        return f"{_KV_HMAC_PREFIX}/{self.tenant_id}/{self.kid}"


class WebhookKeyManager:
    """Manage webhook signing keys via Vault transit + KV.

    The ``vault_client`` parameter is an ``hvac.Client`` (or compatible) that
    is already authenticated. All Vault interactions are kept thin to allow
    test doubles to substitute any of the four endpoints used:

      - secrets.transit.create_key
      - secrets.transit.rotate_key
      - secrets.transit.read_key
      - secrets.kv.v2.create_or_update_secret / read_secret_version
    """

    def __init__(self, vault_client: Any) -> None:
        if vault_client is None:
            raise ValueError("vault_client is required")
        self._vault = vault_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def ensure_keys_for_tenant(self, tenant_id: str, mode: str) -> KeyId:
        """Ensure a signing key exists for ``(tenant_id, mode)``.

        Creates the Vault transit key (asymmetric modes) or KV secret
        (HMAC mode) if absent. Idempotent; returns the active ``KeyId``.
        """
        self._validate_mode(mode)
        if not tenant_id:
            raise ValueError("tenant_id is required")

        if mode == HMAC_SHA256:
            return self._ensure_hmac_key(tenant_id)
        return self._ensure_transit_key(tenant_id, mode)

    def rotate(self, tenant_id: str, mode: str) -> KeyId:
        """Rotate the active signing key.

        Creates a new ``kid``; the previous key is retained for the
        spec-defined grace period and is included in JWKS exports until
        it expires.
        """
        self._validate_mode(mode)
        if not tenant_id:
            raise ValueError("tenant_id is required")

        if mode == HMAC_SHA256:
            return self._rotate_hmac_key(tenant_id)
        return self._rotate_transit_key(tenant_id, mode)

    def get_jwks(self, tenant_id: str) -> dict[str, list[dict[str, str]]]:
        """Return JWKS document with all in-grace public keys for tenant.

        HMAC keys are intentionally excluded — JWKS is for asymmetric
        public-key verification only. Consumers using HMAC must obtain
        the shared secret via the authenticated webhook config.
        """
        if not tenant_id:
            raise ValueError("tenant_id is required")

        keys: list[dict[str, str]] = []
        cutoff = _now() - _dt.timedelta(days=GRACE_PERIOD_DAYS)

        for mode in (ED25519, ECDSA_P256_SHA256):
            try:
                versions = self._read_transit_versions(tenant_id, mode)
            except _KeyNotFound:
                continue
            for kid, public_pem, created in versions:
                if created < cutoff:
                    continue
                jwk = self._public_pem_to_jwk(public_pem, mode, kid)
                if jwk is not None:
                    keys.append(jwk)

        return {"keys": keys}

    # ------------------------------------------------------------------
    # HMAC (symmetric)
    # ------------------------------------------------------------------
    def _ensure_hmac_key(self, tenant_id: str) -> KeyId:
        existing = self._read_active_hmac(tenant_id)
        if existing is not None:
            return existing
        return self._mint_hmac(tenant_id)

    def _rotate_hmac_key(self, tenant_id: str) -> KeyId:
        return self._mint_hmac(tenant_id)

    def _mint_hmac(self, tenant_id: str) -> KeyId:
        secret_bytes = secrets.token_bytes(32)
        kid = self._new_kid(HMAC_SHA256)
        created = _now()
        path = f"{_KV_HMAC_PREFIX}/{tenant_id}/{kid}"
        self._vault.secrets.kv.v2.create_or_update_secret(
            path=path,
            secret={
                "secret_b64": _b64url(secret_bytes),
                "created_at": created.isoformat(),
                "active": True,
            },
            mount_point=_KV_MOUNT,
        )
        # Record pointer to active kid.
        self._vault.secrets.kv.v2.create_or_update_secret(
            path=f"{_KV_HMAC_PREFIX}/{tenant_id}/_active",
            secret={"kid": kid, "created_at": created.isoformat()},
            mount_point=_KV_MOUNT,
        )
        return KeyId(tenant_id=tenant_id, mode=HMAC_SHA256, kid=kid, created_at=created)

    def _read_active_hmac(self, tenant_id: str) -> Optional[KeyId]:
        try:
            resp = self._vault.secrets.kv.v2.read_secret_version(
                path=f"{_KV_HMAC_PREFIX}/{tenant_id}/_active",
                mount_point=_KV_MOUNT,
            )
        except Exception:
            return None
        if not resp:
            return None
        data = (resp.get("data") or {}).get("data") or {}
        kid = data.get("kid")
        created_iso = data.get("created_at")
        if not kid or not created_iso:
            return None
        return KeyId(
            tenant_id=tenant_id,
            mode=HMAC_SHA256,
            kid=kid,
            created_at=_dt.datetime.fromisoformat(created_iso),
        )

    def get_hmac_secret(self, tenant_id: str, kid: str) -> bytes:
        """Return the raw HMAC secret bytes for the given kid."""
        resp = self._vault.secrets.kv.v2.read_secret_version(
            path=f"{_KV_HMAC_PREFIX}/{tenant_id}/{kid}",
            mount_point=_KV_MOUNT,
        )
        data = (resp.get("data") or {}).get("data") or {}
        secret_b64 = data.get("secret_b64")
        if not secret_b64:
            raise KeyError(f"hmac secret not found: {tenant_id}/{kid}")
        # urlsafe b64 (unpadded) -- restore padding
        pad = (-len(secret_b64)) % 4
        return base64.urlsafe_b64decode(secret_b64 + ("=" * pad))

    # ------------------------------------------------------------------
    # Transit (asymmetric)
    # ------------------------------------------------------------------
    def _ensure_transit_key(self, tenant_id: str, mode: str) -> KeyId:
        name = f"{tenant_id}/webhook-{mode}"
        try:
            read = self._vault.secrets.transit.read_key(name=name, mount_point=_TRANSIT_MOUNT)
            data = (read.get("data") or {}) if isinstance(read, dict) else {}
            latest_version = data.get("latest_version") or 1
            created_at = self._parse_transit_creation_time(data, latest_version)
            kid = f"{mode}-v{latest_version}"
            return KeyId(tenant_id=tenant_id, mode=mode, kid=kid, created_at=created_at)
        except Exception:
            # Create the key.
            self._vault.secrets.transit.create_key(
                name=name,
                key_type=self._transit_type(mode),
                exportable=False,
                allow_plaintext_backup=False,
                mount_point=_TRANSIT_MOUNT,
            )
            return KeyId(
                tenant_id=tenant_id,
                mode=mode,
                kid=f"{mode}-v1",
                created_at=_now(),
            )

    def _rotate_transit_key(self, tenant_id: str, mode: str) -> KeyId:
        # Ensure exists first (rotate requires existing key).
        self._ensure_transit_key(tenant_id, mode)
        name = f"{tenant_id}/webhook-{mode}"
        self._vault.secrets.transit.rotate_key(name=name, mount_point=_TRANSIT_MOUNT)
        read = self._vault.secrets.transit.read_key(name=name, mount_point=_TRANSIT_MOUNT)
        data = (read.get("data") or {}) if isinstance(read, dict) else {}
        latest_version = data.get("latest_version") or 1
        created = self._parse_transit_creation_time(data, latest_version)
        return KeyId(
            tenant_id=tenant_id,
            mode=mode,
            kid=f"{mode}-v{latest_version}",
            created_at=created,
        )

    def _read_transit_versions(
        self, tenant_id: str, mode: str
    ) -> list[tuple[str, str, _dt.datetime]]:
        """Return ``[(kid, public_pem, created_at), ...]`` for all versions."""
        name = f"{tenant_id}/webhook-{mode}"
        try:
            read = self._vault.secrets.transit.read_key(name=name, mount_point=_TRANSIT_MOUNT)
        except Exception as exc:  # pragma: no cover - defensive
            raise _KeyNotFound(str(exc))
        data = (read.get("data") or {}) if isinstance(read, dict) else {}
        keys = data.get("keys") or {}
        if not keys:
            raise _KeyNotFound(f"no key versions for {name}")

        out: list[tuple[str, str, _dt.datetime]] = []
        for ver_str, info in keys.items():
            public_pem: str = ""
            created: _dt.datetime
            if isinstance(info, dict):
                public_pem = info.get("public_key", "") or ""
                ct = info.get("creation_time")
                created = self._parse_iso(ct) if ct else _now()
            else:
                created = _now()
            kid = f"{mode}-v{ver_str}"
            if public_pem:
                out.append((kid, public_pem, created))
        return out

    @staticmethod
    def _parse_transit_creation_time(data: dict, version: int) -> _dt.datetime:
        keys = data.get("keys") or {}
        info = keys.get(str(version)) or keys.get(version)
        if isinstance(info, dict):
            ct = info.get("creation_time")
            if ct:
                return WebhookKeyManager._parse_iso(ct)
        return _now()

    @staticmethod
    def _parse_iso(value: str) -> _dt.datetime:
        # Vault may return RFC3339 with "Z"; normalize.
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return _dt.datetime.fromisoformat(value)
        except Exception:
            return _now()

    @staticmethod
    def _transit_type(mode: str) -> str:
        if mode == ED25519:
            return "ed25519"
        if mode == ECDSA_P256_SHA256:
            return "ecdsa-p256"
        raise ValueError(f"unsupported transit mode: {mode}")

    # ------------------------------------------------------------------
    # JWKS conversion
    # ------------------------------------------------------------------
    @staticmethod
    def _public_pem_to_jwk(
        public_pem: str, mode: str, kid: str
    ) -> Optional[dict[str, str]]:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import (
                ec as _ec,
                ed25519 as _ed25519,
            )
        except Exception:  # pragma: no cover - cryptography is required
            return None

        try:
            pub = serialization.load_pem_public_key(public_pem.encode("ascii"))
        except Exception:
            return None

        if mode == ED25519 and isinstance(pub, _ed25519.Ed25519PublicKey):
            raw = pub.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            return {
                "kty": "OKP",
                "crv": "Ed25519",
                "kid": kid,
                "x": _b64url(raw),
                "alg": "EdDSA",
                "use": "sig",
            }

        if mode == ECDSA_P256_SHA256 and isinstance(pub, _ec.EllipticCurvePublicKey):
            numbers = pub.public_numbers()
            x = numbers.x.to_bytes(32, "big")
            y = numbers.y.to_bytes(32, "big")
            return {
                "kty": "EC",
                "crv": "P-256",
                "kid": kid,
                "x": _b64url(x),
                "y": _b64url(y),
                "alg": "ES256",
                "use": "sig",
            }

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_mode(mode: str) -> None:
        if mode not in VALID_MODES:
            raise ValueError(
                f"invalid signing_mode {mode!r}; expected one of {sorted(VALID_MODES)}"
            )

    @staticmethod
    def _new_kid(mode: str) -> str:
        # Caller-friendly, sortable: ``<mode>-<YYYYMMDD>-<rand4>``.
        now = _now()
        rand = secrets.token_hex(2)
        return f"{mode}-{now.strftime('%Y%m%d')}-{rand}"


class _KeyNotFound(Exception):
    """Internal: transit key absent."""
