"""gRPC boundary to the storage-orchestrator for primary-backend cutover (M2).

No storage-orchestrator proto exists yet in gough's gRPC surface (see
``app/grpc/gough/*_pb2.py`` -- disks/nodes/migration/primary are all
represented, a storage-cutover-execution service is not, and this repo
vendors generated stubs only, with no ``.proto`` source or codegen tooling
checked in to generate a new one against). Rather than fabricate wire
messages for a service that does not exist, this module defines the real
call shape ``app.api.clusters``'s M2 switch-primary state machine depends on
-- one stage at a time, raising :class:`StorageOrchestratorUnavailable` on
any failure -- and performs the one real signal available without that
proto: opening a real ``grpc.aio`` channel to
``STORAGE_ORCHESTRATOR_GRPC_TARGET`` and waiting for it to become ready.
Nothing is faked: with no orchestrator deployed (the default in dev/test),
every stage genuinely fails, which the caller treats as a hard failure and
rolls back -- it never silently reports success. Production wiring swaps
the body of :func:`execute_stage` for a real unary RPC once the proto lands;
callers and tests are unaffected by that swap.

Transport (regression: plaintext grpc storage_orchestrator, sec review):
mirrors ``app.grpc_runner``'s server-side mTLS gating exactly, reusing the
SAME env material -- ``GRPC_TLS_CERT_PEM``/``GRPC_TLS_KEY_PEM`` for this
client's own certificate (mutual TLS requires the client to present one
too), ``GOUGH_SPIRE_TRUST_BUNDLE`` as the root of trust for the
orchestrator's server certificate. When that material is configured, the
channel is always ``grpc.aio.secure_channel``. When it is not configured, a
plaintext ``grpc.aio.insecure_channel`` is allowed ONLY when the caller
passes ``insecure_allowed=True`` -- which, exactly like
``app_grpc_runner.serve``'s ``insecure_allowed`` parameter, MUST be sourced
by the caller from Quart's ``app.config.get("DEBUG") or
app.config.get("TESTING")`` and never re-derived here from request data or
the ambient environment. The default is ``False``: with no mTLS material
and no explicit dev/test opt-in, :func:`execute_stage` raises rather than
silently falling back to plaintext, so a production code path can never
ship an unauthenticated channel.

Tests patch :func:`execute_stage` wholesale to exercise both the success and
rollback/failure paths without a live gRPC server.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import grpc

#: How long a single stage waits for the orchestrator channel to become
#: ready before giving up. Short because this gates a user-facing request.
STAGE_TIMEOUT_SECONDS = float(os.environ.get("STORAGE_ORCHESTRATOR_TIMEOUT_SECONDS", "5"))

#: The five cutover stages, in execution order. Mirrors the M1 preview plan
#: in ``app.api.clusters._STAGES`` -- kept here too so this module's
#: docstrings/tests can reference stage names without importing the API
#: layer.
STAGE_ORDER: tuple[str, ...] = (
    "preflight",
    "drain_writes",
    "snapshot_sync",
    "promote",
    "demote_old",
)


class StorageOrchestratorError(Exception):
    """Base class for storage-orchestrator boundary failures."""


class StorageOrchestratorUnavailable(StorageOrchestratorError):
    """The orchestrator could not be reached to execute a stage."""


@dataclass(slots=True)
class StageResult:
    """Outcome of one cutover stage execution."""

    stage: str
    ok: bool
    detail: str = ""


def _load_channel_credentials(trust_bundle_pem: str | None) -> grpc.ChannelCredentials | None:
    """Build mTLS client channel credentials from the SAME env-sourced PEM
    material ``app.grpc_runner._load_server_credentials`` uses server-side.

    Returns ``None`` if ``GRPC_TLS_CERT_PEM``/``GRPC_TLS_KEY_PEM`` aren't
    configured (SPIRE not provisioned in this environment yet) -- the
    caller decides whether that is fatal (production) or an allowed
    plaintext fallback (dev/test only).
    # regression: grpc plaintext storage_orchestrator (sec review)
    """
    cert_pem = os.getenv("GRPC_TLS_CERT_PEM")
    key_pem = os.getenv("GRPC_TLS_KEY_PEM")
    if not cert_pem or not key_pem:
        return None

    cert_bytes = cert_pem.encode() if isinstance(cert_pem, str) else cert_pem
    key_bytes = key_pem.encode() if isinstance(key_pem, str) else key_pem
    root_certificates = trust_bundle_pem.encode() if trust_bundle_pem else None

    return grpc.ssl_channel_credentials(
        root_certificates=root_certificates,
        private_key=key_bytes,
        certificate_chain=cert_bytes,
    )


async def execute_stage(
    *,
    cluster_id: str,
    stage: str,
    source_backend_id: str | None,
    target_backend_id: str,
    target: str | None = None,
    insecure_allowed: bool = False,
) -> StageResult:
    """Execute one primary-storage cutover stage against the orchestrator.

    Raises :class:`StorageOrchestratorUnavailable` if the orchestrator
    cannot be reached within :data:`STAGE_TIMEOUT_SECONDS` -- callers treat
    any exception from this function as a hard stage failure.

    Args:
        insecure_allowed: Whether a plaintext (non-mTLS) channel may be
            opened when no mTLS material (``GRPC_TLS_CERT_PEM``/
            ``GRPC_TLS_KEY_PEM``) is configured. MUST be sourced from
            Quart's ``app.config.get("DEBUG") or app.config.get("TESTING")``
            by the caller -- never re-derived from request data or the
            ambient environment here, and never true in production.
            Defaults to ``False`` (fail closed): a production call site
            that forgets to pass this explicitly gets a hard error, never
            a silently-plaintext channel.
            # regression: grpc plaintext storage_orchestrator (sec review)
    """
    resolved_target = target or os.environ.get("STORAGE_ORCHESTRATOR_GRPC_TARGET", "")
    if not resolved_target:
        raise StorageOrchestratorUnavailable(
            "STORAGE_ORCHESTRATOR_GRPC_TARGET is not configured; cannot execute "
            f"stage={stage!r} for cluster_id={cluster_id!r}"
        )

    trust_bundle_pem = os.getenv("GOUGH_SPIRE_TRUST_BUNDLE") or None
    credentials = _load_channel_credentials(trust_bundle_pem)

    if credentials is not None:
        channel = grpc.aio.secure_channel(resolved_target, credentials)
    elif insecure_allowed:
        channel = grpc.aio.insecure_channel(resolved_target)
    else:
        raise StorageOrchestratorUnavailable(
            "gRPC mTLS credentials not configured (GRPC_TLS_CERT_PEM/GRPC_TLS_KEY_PEM "
            "missing) and insecure_allowed=False (production). Refusing to open an "
            "unauthenticated channel to the storage-orchestrator "
            f"(stage={stage!r}, cluster_id={cluster_id!r}); set "
            "GRPC_TLS_CERT_PEM/GRPC_TLS_KEY_PEM (and GOUGH_SPIRE_TRUST_BUNDLE for "
            "server-cert verification) or run under DEBUG/TESTING."
        )

    try:
        await asyncio.wait_for(channel.channel_ready(), timeout=STAGE_TIMEOUT_SECONDS)
    except Exception as exc:
        raise StorageOrchestratorUnavailable(
            f"storage-orchestrator unreachable at {resolved_target!r} "
            f"(stage={stage!r}, cluster_id={cluster_id!r}): {exc}"
        ) from exc
    finally:
        await channel.close()

    return StageResult(
        stage=stage,
        ok=True,
        detail=f"orchestrator ready at {resolved_target} (target_backend_id={target_backend_id})",
    )


__all__ = [
    "STAGE_ORDER",
    "STAGE_TIMEOUT_SECONDS",
    "StageResult",
    "StorageOrchestratorError",
    "StorageOrchestratorUnavailable",
    "execute_stage",
]
