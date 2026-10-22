"""Unit tests for ``app.grpc_auth.GrpcAuthInterceptor``.

Proves the core Plan 4 security property: an RPC carrying neither a valid
peer SVID (mTLS X.509 chain-validated against the gough SPIRE trust bundle)
nor a valid signed machine JWT is REJECTED (UNAUTHENTICATED, never reaches
the servicer); an RPC carrying either one is ACCEPTED. Mocks the SVID
trust bundle / keystore (a real self-signed CA + leaf cert, mirroring
``tests/security/test_credentials.py``'s ``x509_cert_and_ca`` fixture) and
the OIDC verifier (``app.security.oidc.AsyncStaticKeyVerifier``) rather than
standing up real SPIRE/Vault infrastructure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.grpc_auth import GrpcAuthInterceptor
from app.security.credentials import MissingCredentialError, Principal

pytestmark = pytest.mark.asyncio


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def x509_cert_and_ca() -> tuple[str, str]:
    """A CA cert + a leaf SVID cert (SPIFFE SAN) signed by it, as PEM strings.

    Mirrors ``tests/security/test_credentials.py``'s fixture of the same
    name -- ``validate_service_svid``'s chain validation requires a genuine
    two-party CA->leaf chain, not a single self-signed cert.
    """
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256(), default_backend())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    leaf_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-service")])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_subject)
        .issuer_name(ca_subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier("spiffe://gough.test/node/node-1")]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256(), default_backend())
    )

    cert_pem = leaf_cert.public_bytes(serialization.Encoding.PEM).decode()
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode()
    return cert_pem, ca_pem


class _AbortCalled(Exception):
    """Mirrors real ``grpc.aio`` semantics: ``ServicerContext.abort`` always raises."""

    def __init__(self, code: Any, details: str) -> None:
        self.code = code
        self.details = details
        super().__init__(f"{code}: {details}")


class FakeServicerContext:
    """Minimal stand-in for ``grpc.aio.ServicerContext``.

    Supplies only what ``GrpcAuthInterceptor._authenticate`` reads
    (``auth_context()``, ``invocation_metadata()``) plus ``abort()``.
    """

    def __init__(
        self,
        *,
        peer_cert_pem: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._auth_context: dict[str, Any] = {}
        if peer_cert_pem is not None:
            self._auth_context["x509_pem_cert"] = (peer_cert_pem.encode(),)
        self._metadata = tuple((metadata or {}).items())
        self.aborted: tuple[Any, str] | None = None

    def auth_context(self) -> dict[str, Any]:
        return self._auth_context

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata

    async def abort(self, code: Any, details: str = "") -> None:
        self.aborted = (code, details)
        raise _AbortCalled(code, details)


class _HandlerCallDetails:
    """Minimal stand-in for ``grpc.HandlerCallDetails`` -- only ``.method`` is read."""

    def __init__(self, method: str) -> None:
        self.method = method


def _make_handler(behavior: Any) -> grpc.RpcMethodHandler:
    """Build a real unary-unary RpcMethodHandler wrapping ``behavior``."""
    return grpc.unary_unary_rpc_method_handler(behavior)


# =============================================================================
# _authenticate: SVID path
# =============================================================================


async def test_authenticate_accepts_valid_svid(x509_cert_and_ca: tuple[str, str]) -> None:
    """A peer cert chain-validated against the configured trust bundle is accepted."""
    cert_pem, ca_pem = x509_cert_and_ca
    interceptor = GrpcAuthInterceptor(trust_bundle_pem=ca_pem, oidc_verifier=None)
    context = FakeServicerContext(peer_cert_pem=cert_pem)

    principal = await interceptor._authenticate(context)

    assert isinstance(principal, Principal)
    assert principal.spiffe_id == "spiffe://gough.test/node/node-1"


async def test_authenticate_svid_wrong_ca_falls_back_and_rejects(
    x509_cert_and_ca: tuple[str, str],
) -> None:
    """A cert NOT issued by the configured trust bundle is rejected (falls
    through to the machine-JWT check, which also fails with none
    configured)."""
    _, real_ca_pem = x509_cert_and_ca

    # Self-signed cert from an unrelated CA -- issuer won't match real_ca_pem's subject.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "untrusted")])
    bogus_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier("spiffe://attacker.example/evil")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256(), default_backend())
    )
    cert_pem = bogus_cert.public_bytes(serialization.Encoding.PEM).decode()

    interceptor = GrpcAuthInterceptor(trust_bundle_pem=real_ca_pem, oidc_verifier=None)
    context = FakeServicerContext(peer_cert_pem=cert_pem)

    with pytest.raises(MissingCredentialError):
        await interceptor._authenticate(context)


# =============================================================================
# _authenticate: machine JWT path
# =============================================================================


async def test_authenticate_accepts_valid_machine_jwt() -> None:
    """A valid machine JWT (verified by the mocked OIDC verifier) is accepted."""
    verifier = AsyncMock()
    verifier.verify_token.return_value = {
        "sub": "machine:discovery-agent",
        "tenant": "__default__",
        "scope": ["nodes:write"],
    }
    interceptor = GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=verifier)
    context = FakeServicerContext(metadata={"authorization": "Bearer fake-token"})

    principal = await interceptor._authenticate(context)

    assert principal.sub == "machine:discovery-agent"
    assert "nodes:write" in principal.scopes
    verifier.verify_token.assert_awaited_once_with("fake-token")


async def test_authenticate_rejects_non_machine_sub() -> None:
    """A JWT that verifies but whose sub isn't 'machine:*' is rejected."""
    verifier = AsyncMock()
    verifier.verify_token.return_value = {"sub": "user:alice", "tenant": "__default__"}
    interceptor = GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=verifier)
    context = FakeServicerContext(metadata={"authorization": "Bearer fake-token"})

    with pytest.raises(MissingCredentialError):
        await interceptor._authenticate(context)


async def test_authenticate_rejects_jwt_verification_failure() -> None:
    """A JWT that fails signature/exp/aud verification is rejected."""
    verifier = AsyncMock()
    verifier.verify_token.side_effect = ValueError("bad signature")
    interceptor = GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=verifier)
    context = FakeServicerContext(metadata={"authorization": "Bearer fake-token"})

    with pytest.raises(MissingCredentialError):
        await interceptor._authenticate(context)


# =============================================================================
# _authenticate: no credentials at all
# =============================================================================


async def test_authenticate_rejects_no_credentials() -> None:
    """Neither a peer cert nor a machine JWT -> rejected."""
    interceptor = GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=None)
    context = FakeServicerContext()

    with pytest.raises(MissingCredentialError):
        await interceptor._authenticate(context)


# =============================================================================
# intercept_service: end-to-end RPC rejection / acceptance
# =============================================================================


async def test_intercept_service_rejects_unauthenticated_rpc() -> None:
    """An RPC with no SVID/JWT never reaches the handler and is aborted
    UNAUTHENTICATED -- the core property this task implements."""
    handler_called = False

    async def behavior(request: Any, context: Any) -> Any:
        nonlocal handler_called
        handler_called = True
        return request

    async def continuation(_details: Any) -> grpc.RpcMethodHandler:
        return _make_handler(behavior)

    interceptor = GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=None)
    handler = await interceptor.intercept_service(
        continuation, _HandlerCallDetails("/gough.Identity/IssueSVID")
    )
    assert handler is not None

    context = FakeServicerContext()
    with pytest.raises(_AbortCalled) as exc_info:
        await handler.unary_unary("request", context)

    assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED
    assert context.aborted is not None
    assert context.aborted[0] == grpc.StatusCode.UNAUTHENTICATED
    assert handler_called is False, "servicer must never run for an unauthenticated RPC"


async def test_intercept_service_accepts_valid_machine_jwt() -> None:
    """An RPC with a valid machine JWT reaches the handler and returns normally."""
    async def behavior(request: Any, context: Any) -> Any:
        return f"handled:{request}"

    async def continuation(_details: Any) -> grpc.RpcMethodHandler:
        return _make_handler(behavior)

    verifier = AsyncMock()
    verifier.verify_token.return_value = {"sub": "machine:discovery-agent", "tenant": "__default__"}
    interceptor = GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=verifier)
    handler = await interceptor.intercept_service(
        continuation, _HandlerCallDetails("/gough.Identity/IssueSVID")
    )
    assert handler is not None

    context = FakeServicerContext(metadata={"authorization": "Bearer fake-token"})
    result = await handler.unary_unary("ping", context)

    assert result == "handled:ping"
    assert context.aborted is None


async def test_intercept_service_accepts_valid_svid(x509_cert_and_ca: tuple[str, str]) -> None:
    """An RPC with a valid peer SVID reaches the handler and returns normally."""
    cert_pem, ca_pem = x509_cert_and_ca

    async def behavior(request: Any, context: Any) -> Any:
        return f"handled:{request}"

    async def continuation(_details: Any) -> grpc.RpcMethodHandler:
        return _make_handler(behavior)

    interceptor = GrpcAuthInterceptor(trust_bundle_pem=ca_pem, oidc_verifier=None)
    handler = await interceptor.intercept_service(
        continuation, _HandlerCallDetails("/gough.Identity/IssueSVID")
    )
    assert handler is not None

    context = FakeServicerContext(peer_cert_pem=cert_pem)
    result = await handler.unary_unary("ping", context)

    assert result == "handled:ping"
    assert context.aborted is None
