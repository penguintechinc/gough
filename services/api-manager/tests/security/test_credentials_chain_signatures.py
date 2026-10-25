"""CA signature verification for every supported key type, and the credentials
middleware dispatch table.

``_verify_x509_signature`` is exercised with EC and Ed25519 CAs (the RSA path is
covered elsewhere) plus an unsupported key type. ``credentials_middleware`` is
driven through a real Quart request context so the anonymous bypass, each
credential-type route, and ``g.principal`` population are asserted directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import app.security.credentials as creds
import jwt
import pytest
from app.security.credentials import (
    CredentialType,
    InvalidCredentialError,
    MissingCredentialError,
    Principal,
    _verify_x509_signature,
    credentials_middleware,
)
from app.security.scope_policy import ANONYMOUS_PATHS
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519
from cryptography.x509.oid import NameOID
from quart import Quart, g


def _build_cert(
    subject_key: Any, issuer_key: Any, *, hash_alg: Any
) -> x509.Certificate:
    """Self-describing leaf/CA certificate for ``subject_key`` signed by ``issuer_key``."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
    )
    return builder.sign(issuer_key, hash_alg)


def test_verify_signature_ec_ca_accepts_matching_chain() -> None:
    """A leaf signed by the EC CA verifies against that CA's public key."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _build_cert(
        ec.generate_private_key(ec.SECP256R1()), ca_key, hash_alg=hashes.SHA256()
    )

    _verify_x509_signature(leaf, ca_key.public_key())  # must not raise


def test_verify_signature_ec_ca_rejects_foreign_signer() -> None:
    """A leaf signed by a different EC key is rejected with InvalidCredentialError."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    other_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _build_cert(
        ec.generate_private_key(ec.SECP256R1()), other_key, hash_alg=hashes.SHA256()
    )

    with pytest.raises(InvalidCredentialError, match="signature verification failed"):
        _verify_x509_signature(leaf, ca_key.public_key())


def test_verify_signature_ed25519_ca_accepts_matching_chain() -> None:
    """A leaf signed by an Ed25519 CA verifies (Ed25519 has no separate hash)."""
    ca_key = ed25519.Ed25519PrivateKey.generate()
    leaf = _build_cert(ed25519.Ed25519PrivateKey.generate(), ca_key, hash_alg=None)

    _verify_x509_signature(leaf, ca_key.public_key())  # must not raise


def test_verify_signature_ed25519_ca_rejects_foreign_signer() -> None:
    """An Ed25519 leaf signed by another key is rejected."""
    ca_key = ed25519.Ed25519PrivateKey.generate()
    other_key = ed25519.Ed25519PrivateKey.generate()
    leaf = _build_cert(ed25519.Ed25519PrivateKey.generate(), other_key, hash_alg=None)

    with pytest.raises(InvalidCredentialError):
        _verify_x509_signature(leaf, ca_key.public_key())


def test_verify_signature_unsupported_ca_key_type_rejected() -> None:
    """A CA key type outside RSA/EC/Ed25519 is refused with a named error."""
    signer = ec.generate_private_key(ec.SECP256R1())
    leaf = _build_cert(signer, signer, hash_alg=hashes.SHA256())
    dsa_pub = dsa.generate_private_key(key_size=2048).public_key()

    with pytest.raises(
        InvalidCredentialError, match="Unsupported CA key type: DSAPublicKey"
    ):
        _verify_x509_signature(leaf, dsa_pub)


# ---------------------------------------------------------------------------
# credentials_middleware dispatch
# ---------------------------------------------------------------------------


@pytest.fixture()
def cred_app() -> Quart:
    """Quart app carrying the JWT signing secret the bootstrap route reads."""
    app = Quart(__name__)
    app.config["JWT_SECRET_KEY"] = "unit-test-secret"
    return app


def _principal(cred_type: CredentialType, sub: str = "x") -> Principal:
    """Build a minimal Principal of ``cred_type`` for stubbed validators to return."""
    return Principal(
        cred_type=cred_type, sub=sub, tenant_id="t1", scopes=frozenset({"a:b"})
    )


def _bearer(payload: dict[str, Any]) -> str:
    """Return an HS256 token for ``payload`` (signature is not checked by routing)."""
    return "Bearer " + jwt.encode(payload, "routing-only", algorithm="HS256")


async def _run(
    cred_app: Quart,
    *,
    path: str = "/api/v1/things",
    method: str = "GET",
    headers: dict[str, str] | None = None,
    peer_cert_pem: str | None = None,
) -> Any:
    """Invoke credentials_middleware inside a request context; return its result."""
    async with cred_app.test_request_context(
        path, method=method, headers=headers or {}
    ) as ctx:
        ctx.request.peer_cert_pem = peer_cert_pem  # type: ignore[attr-defined]
        return await credentials_middleware(
            ctx.request,
            vault_client=None,
            redis_client=None,
            spire_trust_bundle="bundle",
            allowed_spiffe_ids=frozenset({"spiffe://penguintech.io/dev/x"}),
            jwks_keys=[],
            audience="gough",
            issuer="https://auth",
        ), g.get("principal")


async def test_anonymous_path_bypasses_credential_check(cred_app: Quart) -> None:
    """A path listed in ANONYMOUS_PATHS returns None and sets no principal, even with no creds."""
    method, path = next(iter(ANONYMOUS_PATHS))
    async with cred_app.test_request_context(path, method=method) as ctx:
        result = await credentials_middleware(
            ctx.request, None, None, "bundle", frozenset(), [], "gough", "https://auth"
        )
        assert result is None
        assert g.get("principal") is None


async def test_no_credentials_raises_missing(cred_app: Quart) -> None:
    """A protected path with neither bearer token nor peer cert raises MissingCredentialError."""
    with pytest.raises(MissingCredentialError):
        await _run(cred_app)


async def test_peer_cert_without_pem_value_is_rejected(
    cred_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SVID routing with an empty peer PEM is refused before the validator runs."""
    called = []
    monkeypatch.setattr(
        creds, "validate_service_svid", lambda *a, **k: called.append(a)
    )
    monkeypatch.setattr(
        creds, "detect_credential_type", lambda h, p: CredentialType.SERVICE_SVID
    )

    with pytest.raises(InvalidCredentialError, match="requires peer certificate"):
        await _run(cred_app, peer_cert_pem=None)
    assert called == []


async def test_service_svid_dispatches_to_svid_validator(
    cred_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mTLS request routes to validate_service_svid with bundle + allowlist, and sets g.principal."""
    expected = _principal(
        CredentialType.SERVICE_SVID, sub="spiffe://penguintech.io/dev/x"
    )
    seen: dict[str, Any] = {}

    def _fake(pem: str, bundle: str, allowed: frozenset[str]) -> Principal:
        seen.update(pem=pem, bundle=bundle, allowed=allowed)
        return expected

    monkeypatch.setattr(creds, "validate_service_svid", _fake)

    result, principal = await _run(cred_app, peer_cert_pem="PEM-DATA")

    assert result is expected and principal is expected
    assert seen == {
        "pem": "PEM-DATA",
        "bundle": "bundle",
        "allowed": frozenset({"spiffe://penguintech.io/dev/x"}),
    }


async def test_bootstrap_token_uses_configured_signing_secret(
    cred_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A phase=helper token is validated with the app's JWT_SECRET_KEY and the vault/redis clients."""
    expected = _principal(CredentialType.ONE_TIME_BOOTSTRAP)
    captured: dict[str, Any] = {}

    def _fake(
        token: str, vault: Any, redis: Any, *, signing_secret: str | None
    ) -> Principal:
        captured.update(token=token, vault=vault, redis=redis, secret=signing_secret)
        return expected

    monkeypatch.setattr(creds, "validate_one_time_bootstrap_token", _fake)
    header = _bearer({"phase": "helper", "sub": "node-1"})

    async with cred_app.test_request_context(
        "/api/v1/things", method="POST", headers={"Authorization": header}
    ) as ctx:
        result = await credentials_middleware(
            ctx.request,
            "VAULT",
            "REDIS",
            "bundle",
            frozenset(),
            [],
            "gough",
            "https://auth",
        )
        assert g.get("principal") is expected

    assert result is expected
    assert captured["token"] == header[len("Bearer ") :]
    assert captured["vault"] == "VAULT" and captured["redis"] == "REDIS"
    assert captured["secret"] == "unit-test-secret"


async def test_machine_jwt_routes_to_machine_validator(
    cred_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine:-prefixed subject is validated by validate_machine_jwt with audience/issuer."""
    expected = _principal(CredentialType.MACHINE_JWT, sub="machine:agent")
    seen: dict[str, Any] = {}

    def _fake(token: str, keys: list[dict], audience: str, issuer: str) -> Principal:
        seen.update(token=token, audience=audience, issuer=issuer)
        return expected

    monkeypatch.setattr(creds, "validate_machine_jwt", _fake)
    monkeypatch.setattr(
        creds,
        "validate_user_jwt",
        lambda *a, **k: pytest.fail("user validator must not run"),
    )
    header = _bearer({"sub": "machine:agent"})

    result, principal = await _run(cred_app, headers={"Authorization": header})

    assert result is expected and principal is expected
    assert seen == {"token": header[7:], "audience": "gough", "issuer": "https://auth"}


async def test_plain_subject_routes_to_user_validator(
    cred_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-machine subject falls through to the USER_JWT validator."""
    expected = _principal(CredentialType.USER_JWT, sub="alice")
    monkeypatch.setattr(creds, "validate_user_jwt", lambda *a, **k: expected)
    monkeypatch.setattr(
        creds,
        "validate_machine_jwt",
        lambda *a, **k: pytest.fail("machine validator must not run"),
    )

    result, principal = await _run(
        cred_app, headers={"Authorization": _bearer({"sub": "alice"})}
    )

    assert result is expected and principal is expected


async def test_undecodable_bearer_is_invalid_not_missing(cred_app: Quart) -> None:
    """A Bearer value that is not a JWT raises InvalidCredentialError (fail closed)."""
    with pytest.raises(InvalidCredentialError, match="Cannot decode JWT"):
        await _run(cred_app, headers={"Authorization": "Bearer not-a-jwt"})
