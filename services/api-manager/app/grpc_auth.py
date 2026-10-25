"""gRPC server-side auth interceptor: enforce SVID or machine JWT on every RPC.

Plan 4 (mTLS/SPIFFE-SVID auth). Every RPC the gRPC listener (app.grpc_runner)
serves must carry EITHER a peer SVID (mTLS X.509 client certificate, chain-
validated against the gough SPIRE trust bundle) OR a short-lived, signed
machine OIDC JWT in ``authorization: Bearer <token>`` metadata -- never
neither. This is the gRPC-transport equivalent of the HTTP SERVICE_SVID path
``app.api.nodes.post_node_event`` already enforces, and reuses the exact same
validators (``app.security.credentials.validate_service_svid``) plus the
same ES256 verifier the HTTP OIDC gate validates against
(``app.security.oidc.AsyncStaticKeyVerifier`` -- NOT the M1-era
``validate_machine_jwt``/``validate_user_jwt`` sketch in
``app.security.credentials``, which assumes a generic RS256 JWKS that gough's
actual self-issued ES256/StaticKeyVerifier scheme never populates and is
unwired anywhere in this codebase).

There is deliberately no "allow unauthenticated in DEBUG/TESTING" branch
here: ``app.grpc_runner.serve``'s ``insecure_allowed`` flag only relaxes the
*transport* (plaintext instead of mTLS, so local dev without a provisioned
SPIRE agent can still boot), never this interceptor. A caller with neither a
valid SVID nor a valid machine JWT is rejected in every environment -- an
unauthenticated RPC reaching a servicer is exactly the gap this module
closes (see ``app.grpc_runner`` module docstring, formerly a bare TODO).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import grpc
from app.metrics import grpc_auth_rejected_total
from app.security.credentials import (
    CredentialError,
    CredentialType,
    MissingCredentialError,
    Principal,
    validate_service_svid,
)
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import ExtensionOID
from grpc import aio

if TYPE_CHECKING:
    from app.security.oidc import AsyncStaticKeyVerifier

log = logging.getLogger(__name__)

_BEARER_PREFIX = "Bearer "


def _extract_spiffe_id(cert_pem: str) -> str | None:
    """Pull the ``spiffe://`` URI out of a cert's SAN, with NO chain validation.

    Used only to build a trivially-satisfied ``allowed_spiffe_ids`` singleton
    for ``validate_service_svid`` below. This codebase's gRPC trust model is
    "signed by our own gough CA" (any such caller is authenticated), not a
    smaller explicit per-SPIFFE-ID allowlist -- but ``validate_service_svid``
    has no "accept any" sentinel: an empty ``frozenset`` means "accept none"
    (``spiffe_id not in allowed_spiffe_ids`` is always true), the opposite of
    what's needed here. The real security boundary stays the cryptographic
    chain/signature verification ``validate_service_svid`` performs AFTER its
    allowlist check -- a forged ``spiffe_id`` extracted here only ever earns
    a doomed self-match against that one value, never a bypass of chain
    validation against the actual trust bundle.
    """
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
        san_ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
        for san in san_ext.value:  # type: ignore[attr-defined]
            if isinstance(san, x509.UniformResourceIdentifier) and san.value.startswith(
                "spiffe://"
            ):
                return san.value
    except (
        Exception
    ) as exc:  # noqa: BLE001 - any parse failure just means "no SVID here"
        log.debug("gRPC peer cert SPIFFE SAN extraction failed: %s", exc)
    return None


class GrpcAuthInterceptor(aio.ServerInterceptor):
    """Rejects any RPC lacking a valid peer SVID or machine JWT.

    ``trust_bundle_pem``/``oidc_verifier`` are sourced once at server
    startup (``app.grpc_runner.serve``, ultimately from env vars /
    ``app.config["OIDC_VERIFIER"]`` -- never re-derived per-RPC) and reused
    for every call, matching the same construction the HTTP OIDC gate and
    ``post_node_event`` already use.
    """

    def __init__(
        self,
        *,
        trust_bundle_pem: str | None,
        oidc_verifier: AsyncStaticKeyVerifier | None,
    ) -> None:
        self._trust_bundle_pem = trust_bundle_pem
        self._oidc_verifier = oidc_verifier

    async def _authenticate(self, context: aio.ServicerContext[Any, Any]) -> Principal:
        """Return the authenticated Principal for this RPC, or raise CredentialError."""
        auth_context = context.auth_context() or {}
        pem_values = auth_context.get("x509_pem_cert")

        if pem_values and self._trust_bundle_pem:
            peer_cert_pem = next(iter(pem_values))
            if isinstance(peer_cert_pem, bytes):
                peer_cert_pem = peer_cert_pem.decode()
            claimed_spiffe_id = _extract_spiffe_id(peer_cert_pem)
            try:
                return validate_service_svid(
                    peer_cert_pem=peer_cert_pem,
                    trust_bundle_pem=self._trust_bundle_pem,
                    allowed_spiffe_ids=(
                        frozenset({claimed_spiffe_id})
                        if claimed_spiffe_id
                        else frozenset()
                    ),
                )
            except CredentialError as exc:
                # A presented-but-invalid cert doesn't short-circuit rejection --
                # fall through to the machine-JWT check below (a caller might
                # legitimately offer both during a SVID rotation window).
                log.warning("gRPC peer SVID rejected: %s", exc)

        metadata = dict(context.invocation_metadata() or ())
        auth_header = metadata.get("authorization", "")
        if auth_header.startswith(_BEARER_PREFIX) and self._oidc_verifier is not None:
            token = auth_header[len(_BEARER_PREFIX) :]
            try:
                claims = await self._oidc_verifier.verify_token(token)
            except Exception as exc:
                raise MissingCredentialError(
                    f"machine JWT verification failed: {exc}"
                ) from exc

            sub = claims.get("sub", "")
            if not isinstance(sub, str) or not sub.startswith("machine:"):
                raise MissingCredentialError(
                    f"machine JWT sub must start with 'machine:', got {sub!r}"
                )
            return Principal(
                cred_type=CredentialType.MACHINE_JWT,
                sub=sub,
                tenant_id=claims.get("tenant", "__default__"),
                scopes=frozenset(claims.get("scope") or []),
                claims=claims,
            )

        raise MissingCredentialError(
            "no valid peer SVID or machine JWT presented for this RPC"
        )

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        """Wrap the resolved handler so auth runs before any servicer code."""
        handler = await continuation(handler_call_details)
        if handler is None:
            return handler

        method = handler_call_details.method

        async def _reject(
            context: aio.ServicerContext[Any, Any], exc: CredentialError
        ) -> None:
            grpc_auth_rejected_total.labels(
                method=method, reason=type(exc).__name__
            ).inc()
            log.warning("gRPC RPC %s rejected: %s", method, exc)
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))

        if handler.unary_unary is not None:
            inner = handler.unary_unary

            async def unary_unary(request: Any, context: Any) -> Any:
                try:
                    await self._authenticate(context)
                except CredentialError as exc:
                    await _reject(context, exc)
                    return None
                return await inner(request, context)

            return handler._replace(unary_unary=unary_unary)

        if handler.unary_stream is not None:
            inner = handler.unary_stream

            async def unary_stream(request: Any, context: Any) -> Any:
                try:
                    await self._authenticate(context)
                except CredentialError as exc:
                    await _reject(context, exc)
                    return
                async for response in inner(request, context):
                    yield response

            return handler._replace(unary_stream=unary_stream)

        if handler.stream_unary is not None:
            inner = handler.stream_unary

            async def stream_unary(request_iterator: Any, context: Any) -> Any:
                try:
                    await self._authenticate(context)
                except CredentialError as exc:
                    await _reject(context, exc)
                    return None
                return await inner(request_iterator, context)

            return handler._replace(stream_unary=stream_unary)

        if handler.stream_stream is not None:
            inner = handler.stream_stream

            async def stream_stream(request_iterator: Any, context: Any) -> Any:
                try:
                    await self._authenticate(context)
                except CredentialError as exc:
                    await _reject(context, exc)
                    return
                async for response in inner(request_iterator, context):
                    yield response

            return handler._replace(stream_stream=stream_stream)

        return handler
