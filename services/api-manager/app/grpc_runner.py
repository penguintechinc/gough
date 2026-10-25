"""gRPC server runner (Plan 4: mTLS/SPIFFE-SVID auth).

Starts a grpc.aio server on port 50051 alongside the Quart application,
protected end-to-end:

- Transport: mTLS server credentials built from SPIRE-issued PEM material
  (``GRPC_TLS_CERT_PEM``/``GRPC_TLS_KEY_PEM``), with client-cert validation
  enabled whenever a trust bundle (``GOUGH_SPIRE_TRUST_BUNDLE``) is
  configured. ``insecure_allowed`` (sourced from Quart's DEBUG/TESTING
  config by the caller, ``app.__init__._start_grpc`` -- never re-derived
  here) permits a plaintext listener ONLY when no mTLS material is
  configured, for local dev/test without a provisioned SPIRE agent. This
  is never the production default: with ``insecure_allowed=False`` and no
  mTLS material, startup fails closed instead of silently serving an
  unauthenticated port (the bug this module replaces).
- Per-RPC auth: ``app.grpc_auth.GrpcAuthInterceptor`` enforces a valid peer
  SVID or signed machine JWT on every RPC regardless of transport --
  rejection is NOT gated by DEBUG/TESTING; relaxing the transport never
  relaxes who may call it.

Registered as a background asyncio task via app.before_serving in __init__.py.
"""

from __future__ import annotations

import logging
import os

# NOTE: the protoc-generated *_pb2_grpc.py files use bare `from gough import ...`.
# Importing app.grpc_server below puts app/grpc on sys.path (see its module
# header and app/grpc/__init__.py) before the `from gough import` statement runs.
import grpc
from app.grpc_auth import GrpcAuthInterceptor
from app.grpc_server import (
    AuditServicer,
    BiomesServicer,
    IdentityServicer,
    IPXEServicer,
    JoinerSecretsServicer,
)
from gough import (
    audit_pb2_grpc,
    biomes_pb2_grpc,
    identity_pb2_grpc,
    ipxe_pb2_grpc,
    joiner_pb2_grpc,
)
from grpc import aio

log = logging.getLogger(__name__)

_GRPC_PORT = 50051


def _load_server_credentials(
    trust_bundle_pem: str | None,
) -> grpc.ServerCredentials | None:
    """Build mTLS server credentials from env-sourced PEM material.

    Returns ``None`` if ``GRPC_TLS_CERT_PEM``/``GRPC_TLS_KEY_PEM`` aren't
    configured (SPIRE not provisioned in this environment yet) -- the
    caller decides whether that is fatal (production) or an allowed
    plaintext fallback (dev/test, see module docstring).

    Client-certificate verification (``require_client_auth``) is only
    requested when a trust bundle is present: requesting it without root
    certificates to validate against is both meaningless and a hard error
    for grpc-core. When no trust bundle is configured, the transport is
    still encrypted but does not itself authenticate the peer -- the
    ``GrpcAuthInterceptor`` machine-JWT path is what gates such callers.
    """
    cert_pem = os.getenv("GRPC_TLS_CERT_PEM")
    key_pem = os.getenv("GRPC_TLS_KEY_PEM")
    if not cert_pem or not key_pem:
        return None

    cert_bytes = cert_pem.encode() if isinstance(cert_pem, str) else cert_pem
    key_bytes = key_pem.encode() if isinstance(key_pem, str) else key_pem
    root_certificates = trust_bundle_pem.encode() if trust_bundle_pem else None

    return grpc.ssl_server_credentials(
        [(key_bytes, cert_bytes)],
        root_certificates=root_certificates,
        require_client_auth=root_certificates is not None,
    )


async def serve(
    port: int = _GRPC_PORT,
    *,
    insecure_allowed: bool = False,
    oidc_verifier: object | None = None,
) -> None:
    """Start and run the gRPC server until termination signal.

    Designed to run as an asyncio background task alongside Quart/hypercorn.

    Args:
        port: TCP port to bind.
        insecure_allowed: Whether a plaintext (non-mTLS) listener may be
            started when no mTLS material is configured. MUST be sourced
            from Quart's ``app.config.get("DEBUG") or app.config.get("TESTING")``
            by the caller -- never re-derived from the environment here, and
            never true in production. Does NOT affect per-RPC auth
            enforcement (see ``GrpcAuthInterceptor``).
        oidc_verifier: The same ``app.security.oidc.AsyncStaticKeyVerifier``
            instance the HTTP OIDC gate validates bearer tokens with
            (``app.config["OIDC_VERIFIER"]``), reused here so a machine JWT
            validates against the identical ES256 key material regardless
            of transport (gRPC or REST).

    Raises:
        RuntimeError: Production (``insecure_allowed=False``) with no mTLS
            material configured. Refuses to start an unauthenticated
            transport rather than silently falling back (regression this
            module fixes: the previous implementation always fell back to
            ``add_insecure_port`` with only a log warning).
    """
    trust_bundle_pem = os.getenv("GOUGH_SPIRE_TRUST_BUNDLE") or None

    server = aio.server(
        interceptors=[
            GrpcAuthInterceptor(
                trust_bundle_pem=trust_bundle_pem,
                oidc_verifier=oidc_verifier,
            )
        ]
    )

    ipxe_pb2_grpc.add_IPXEServicer_to_server(IPXEServicer(), server)
    joiner_pb2_grpc.add_JoinerSecretsServicer_to_server(JoinerSecretsServicer(), server)
    biomes_pb2_grpc.add_BiomesServicer_to_server(BiomesServicer(), server)
    audit_pb2_grpc.add_AuditServicer_to_server(AuditServicer(), server)
    identity_pb2_grpc.add_IdentityServicer_to_server(IdentityServicer(), server)

    credentials = _load_server_credentials(trust_bundle_pem)

    if credentials is not None:
        bound_port = server.add_secure_port(f"[::]:{port}", credentials)
        log.info(
            "gRPC server started with mTLS (client_auth=%s) on port %d (bound=%d)",
            trust_bundle_pem is not None,
            port,
            bound_port,
        )
    elif insecure_allowed:
        bound_port = server.add_insecure_port(f"[::]:{port}")
        log.warning(
            "gRPC mTLS not configured (GRPC_TLS_CERT_PEM/GRPC_TLS_KEY_PEM missing); "
            "running plaintext transport under DEBUG/TESTING -- GrpcAuthInterceptor "
            "still enforces a valid machine JWT on every RPC. Port %d (bound=%d)",
            port,
            bound_port,
        )
    else:
        raise RuntimeError(
            "gRPC mTLS credentials not configured (GRPC_TLS_CERT_PEM/GRPC_TLS_KEY_PEM "
            "missing) and insecure_allowed=False (production). Refusing to start an "
            "unauthenticated gRPC listener -- set GRPC_TLS_CERT_PEM/GRPC_TLS_KEY_PEM "
            "(and GOUGH_SPIRE_TRUST_BUNDLE for client-cert enforcement) or run under "
            "DEBUG/TESTING."
        )

    await server.start()
    await server.wait_for_termination()
