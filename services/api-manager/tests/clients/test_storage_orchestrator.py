"""Tests for ``app.clients.storage_orchestrator``.

Covers the gRPC transport gating added after a security review flagged
``grpc.aio.insecure_channel`` shipping inter-service traffic in plaintext,
violating the mTLS requirement already enforced server-side in
``app.grpc_runner``. Production must never be able to open a plaintext
channel -- only an explicit, caller-sourced DEBUG/TESTING opt-in may, and
only when no mTLS material is configured.
# regression: grpc plaintext storage_orchestrator (sec review)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.clients.storage_orchestrator import (
    StorageOrchestratorUnavailable,
    execute_stage,
)


def _mock_channel() -> MagicMock:
    channel = MagicMock()
    channel.channel_ready = AsyncMock(return_value=None)
    channel.close = AsyncMock(return_value=None)
    return channel


# regression: grpc plaintext storage_orchestrator (sec review)
@pytest.mark.asyncio
async def test_prod_without_tls_material_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production (no DEBUG/TESTING opt-in, no mTLS material configured)
    must raise rather than silently falling back to a plaintext channel --
    the exact regression this fix closes."""
    monkeypatch.delenv("GRPC_TLS_CERT_PEM", raising=False)
    monkeypatch.delenv("GRPC_TLS_KEY_PEM", raising=False)
    monkeypatch.delenv("GOUGH_SPIRE_TRUST_BUNDLE", raising=False)

    with (
        patch(
            "app.clients.storage_orchestrator.grpc.aio.insecure_channel"
        ) as mock_insecure,
        patch(
            "app.clients.storage_orchestrator.grpc.aio.secure_channel"
        ) as mock_secure,
    ):
        with pytest.raises(
            StorageOrchestratorUnavailable, match="insecure_allowed=False"
        ):
            await execute_stage(
                cluster_id="c1",
                stage="preflight",
                source_backend_id=None,
                target_backend_id="b2",
                target="orchestrator.internal:50061",
            )

    mock_insecure.assert_not_called()
    mock_secure.assert_not_called()


# regression: grpc plaintext storage_orchestrator (sec review)
@pytest.mark.asyncio
async def test_builds_secure_channel_with_certs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When GRPC_TLS_CERT_PEM/KEY_PEM (+ trust bundle) are configured, the
    channel MUST be ``grpc.aio.secure_channel`` built from the SAME
    env-sourced PEM material ``app.grpc_runner`` uses server-side -- never
    ``insecure_channel``."""
    monkeypatch.setenv("GRPC_TLS_CERT_PEM", "CERT-PEM")
    monkeypatch.setenv("GRPC_TLS_KEY_PEM", "KEY-PEM")
    monkeypatch.setenv("GOUGH_SPIRE_TRUST_BUNDLE", "BUNDLE-PEM")

    mock_channel = _mock_channel()
    sentinel_creds = object()

    with (
        patch(
            "app.clients.storage_orchestrator.grpc.ssl_channel_credentials",
            return_value=sentinel_creds,
        ) as mock_ssl_creds,
        patch(
            "app.clients.storage_orchestrator.grpc.aio.secure_channel",
            return_value=mock_channel,
        ) as mock_secure,
        patch(
            "app.clients.storage_orchestrator.grpc.aio.insecure_channel"
        ) as mock_insecure,
    ):
        result = await execute_stage(
            cluster_id="c1",
            stage="preflight",
            source_backend_id=None,
            target_backend_id="b2",
            target="orchestrator.internal:50061",
        )

    assert result.ok is True
    mock_insecure.assert_not_called()
    mock_secure.assert_called_once_with("orchestrator.internal:50061", sentinel_creds)
    mock_ssl_creds.assert_called_once_with(
        root_certificates=b"BUNDLE-PEM",
        private_key=b"KEY-PEM",
        certificate_chain=b"CERT-PEM",
    )
    mock_channel.channel_ready.assert_awaited_once()
    mock_channel.close.assert_awaited_once()


# regression: grpc plaintext storage_orchestrator (sec review)
@pytest.mark.asyncio
async def test_insecure_allowed_only_as_explicit_devtest_optin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no mTLS material configured, a plaintext channel may be opened
    ONLY when the caller explicitly passes ``insecure_allowed=True``
    (sourced from Quart's DEBUG/TESTING config by the caller, mirroring
    ``app.grpc_runner.serve``) -- never by default."""
    monkeypatch.delenv("GRPC_TLS_CERT_PEM", raising=False)
    monkeypatch.delenv("GRPC_TLS_KEY_PEM", raising=False)
    monkeypatch.delenv("GOUGH_SPIRE_TRUST_BUNDLE", raising=False)

    mock_channel = _mock_channel()

    with (
        patch(
            "app.clients.storage_orchestrator.grpc.aio.insecure_channel",
            return_value=mock_channel,
        ) as mock_insecure,
        patch(
            "app.clients.storage_orchestrator.grpc.aio.secure_channel"
        ) as mock_secure,
    ):
        result = await execute_stage(
            cluster_id="c1",
            stage="preflight",
            source_backend_id=None,
            target_backend_id="b2",
            target="orchestrator.internal:50061",
            insecure_allowed=True,
        )

    assert result.ok is True
    mock_secure.assert_not_called()
    mock_insecure.assert_called_once_with("orchestrator.internal:50061")


# regression: grpc plaintext storage_orchestrator (sec review)
@pytest.mark.asyncio
async def test_insecure_allowed_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """``insecure_allowed`` defaults to ``False`` -- a call site that
    forgets to pass it explicitly fails closed instead of silently opening
    a plaintext channel."""
    monkeypatch.delenv("GRPC_TLS_CERT_PEM", raising=False)
    monkeypatch.delenv("GRPC_TLS_KEY_PEM", raising=False)
    monkeypatch.delenv("GOUGH_SPIRE_TRUST_BUNDLE", raising=False)

    with pytest.raises(StorageOrchestratorUnavailable):
        await execute_stage(
            cluster_id="c1",
            stage="preflight",
            source_backend_id=None,
            target_backend_id="b2",
            target="orchestrator.internal:50061",
        )
