"""First-party OIDC token provider + local verifier wiring (penguin-aaa 0.2.1).

Gough is its OWN issuer. It mints ES256 access/id tokens with an
``OIDCProvider`` and validates its own tokens locally with a
``StaticKeyVerifier`` built from the public key exported out of the same
keystore -- there is no external OIDC discovery/JWKS endpoint to fetch.

penguin-aaa forbids HS256 (algorithm-confusion protection) and requires the
issuer to be an https URL even in dev. ``StaticKeyVerifier.verify_token`` is
synchronous, but ``OIDCAuthMiddleware`` awaits ``rp.verify_token`` and expects
a dict; ``AsyncStaticKeyVerifier`` bridges both gaps (thread-pool + Claims ->
dict). This mirrors the proven Quart/hypercorn spike.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from penguin_aaa.authn.oidc_provider import OIDCProvider, OIDCProviderConfig
from penguin_aaa.authn.static_key import StaticKeyConfig, StaticKeyVerifier
from penguin_aaa.crypto.keystore import FileKeyStore, KeyStore, MemoryKeyStore


@dataclass(slots=True, frozen=True)
class OIDCSettings:
    """Immutable OIDC provider settings resolved from app config."""

    issuer: str
    audience: str
    algorithm: str
    token_ttl: timedelta
    max_token_ttl: timedelta
    refresh_ttl: timedelta
    key_store_path: str
    in_memory: bool

    @classmethod
    def from_config(cls, config: Any) -> "OIDCSettings":
        """Build settings from a Quart ``app.config`` mapping."""
        token_ttl = config.get("JWT_ACCESS_TOKEN_EXPIRES", timedelta(minutes=30))
        # OIDCProviderConfig requires token_ttl <= max_token_ttl (default 1h).
        max_token_ttl = max(token_ttl, timedelta(hours=1))
        return cls(
            issuer=config.get("OIDC_ISSUER", "https://gough.localhost.local"),
            audience=config.get("OIDC_AUDIENCE", "gough-api"),
            algorithm=config.get("OIDC_ALGORITHM", "ES256"),
            token_ttl=token_ttl,
            max_token_ttl=max_token_ttl,
            refresh_ttl=config.get("JWT_REFRESH_TOKEN_EXPIRES", timedelta(days=7)),
            key_store_path=config.get(
                "GOUGH_KEY_STORE_PATH", "/var/gough/keys/oidc_keys.json"
            ),
            # In-memory keystore for dev/test; file-backed (persistent) otherwise.
            in_memory=bool(config.get("DEBUG") or config.get("TESTING")),
        )


class AsyncStaticKeyVerifier:
    """Async adapter over the synchronous ``StaticKeyVerifier``.

    ``OIDCAuthMiddleware`` awaits ``rp.verify_token(token)`` and stores the
    result as ``scope["state"]["claims"]``. This runs the sync verifier off the
    event loop and returns the validated claims as a plain dict (what gough's
    handlers read via ``request.scope["state"]["claims"]``).
    """

    __slots__ = ("_verifier",)

    def __init__(self, verifier: StaticKeyVerifier) -> None:
        self._verifier = verifier

    async def verify_token(self, raw_token: str) -> dict[str, Any]:
        """Validate ``raw_token`` off the event loop; return claims as a dict.

        Rejects anything that is not an access token: the provider signs id
        tokens with the SAME key (they'd otherwise pass signature/iss/aud), and
        stamps ``token_use``. ``Claims`` drops ``token_use``, so it is read from
        the already-verified JWT. A non-access token raises -> the ASGI
        middleware turns that into 401.
        """
        loop = asyncio.get_running_loop()
        claims = await loop.run_in_executor(None, self._verifier.verify_token, raw_token)
        # raw_token is cryptographically verified on the line above
        # (StaticKeyVerifier.verify_token checks signature/iss/aud/exp and
        # required claims). penguin-aaa's strict Claims model drops the
        # non-standard `token_use` claim, so it is re-read here from the SAME,
        # already-authenticated token — this is NOT unverified trust, hence the
        # suppression of the generic unverified-jwt-decode rule.
        # nosemgrep: unverified-jwt-decode
        token_use = jwt.decode(raw_token, options={"verify_signature": False}).get(
            "token_use"
        )
        if token_use != "access":
            raise ValueError(f"token_use {token_use!r} is not an access token")
        return claims.model_dump()


def _build_keystore(settings: OIDCSettings) -> KeyStore:
    """Construct the ES256 keystore for the given settings (memory or file).

    Production (``in_memory=False``) REQUIRES the key file to already exist and
    be non-empty: ``FileKeyStore`` self-generates a key on a missing file, so a
    fresh boot per replica would mint divergent signing keys and callers would
    get cross-replica 401s. The key must be pre-provisioned and shared (e.g. a
    mounted Secret). Fail fast rather than self-generate.
    """
    if settings.in_memory:
        return MemoryKeyStore(algorithm=settings.algorithm)

    path = Path(settings.key_store_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(
            f"OIDC ES256 key store {path} is missing or empty. In production the "
            "signing key must be pre-provisioned and shared across replicas "
            "(e.g. a mounted Secret at GOUGH_KEY_STORE_PATH); refusing to "
            "self-generate divergent per-replica keys."
        )
    return FileKeyStore(path=path, algorithm=settings.algorithm)


def build_oidc(
    settings: OIDCSettings,
) -> tuple[OIDCProvider, AsyncStaticKeyVerifier]:
    """Build the token provider + local async verifier from settings.

    Returns a ``(provider, async_verifier)`` pair. The provider mints tokens at
    login/refresh; the async verifier is handed to ``OIDCAuthMiddleware`` to
    validate incoming bearer tokens locally.
    """
    keystore = _build_keystore(settings)

    provider = OIDCProvider(
        config=OIDCProviderConfig(
            issuer=settings.issuer,
            audiences=[settings.audience],
            algorithm=settings.algorithm,
            token_ttl=settings.token_ttl,
            max_token_ttl=settings.max_token_ttl,
            refresh_ttl=settings.refresh_ttl,
        ),
        keystore=keystore,
    )

    # Export the active public key as PEM for the local static-key verifier.
    signing_key, _kid = keystore.get_signing_key()
    public_key_pem = (
        signing_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    verifier = StaticKeyVerifier(
        config=StaticKeyConfig(
            issuer=settings.issuer,
            audience=settings.audience,
            public_key=public_key_pem,
            algorithms=[settings.algorithm],
        )
    )

    return provider, AsyncStaticKeyVerifier(verifier)
