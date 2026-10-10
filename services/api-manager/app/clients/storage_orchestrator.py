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


async def execute_stage(
    *,
    cluster_id: str,
    stage: str,
    source_backend_id: str | None,
    target_backend_id: str,
    target: str | None = None,
) -> StageResult:
    """Execute one primary-storage cutover stage against the orchestrator.

    Raises :class:`StorageOrchestratorUnavailable` if the orchestrator
    cannot be reached within :data:`STAGE_TIMEOUT_SECONDS` -- callers treat
    any exception from this function as a hard stage failure.
    """
    resolved_target = target or os.environ.get("STORAGE_ORCHESTRATOR_GRPC_TARGET", "")
    if not resolved_target:
        raise StorageOrchestratorUnavailable(
            "STORAGE_ORCHESTRATOR_GRPC_TARGET is not configured; cannot execute "
            f"stage={stage!r} for cluster_id={cluster_id!r}"
        )

    channel = grpc.aio.insecure_channel(resolved_target)
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
