"""Tests for integration health probing."""

from __future__ import annotations

import pytest
import httpx
from unittest.mock import AsyncMock, patch

from app.api.integrations import _probe_integration_health
from app.workers.integration_provisioner import Credentials


@pytest.mark.asyncio
async def test_probe_integration_health_active():
    """Test health probe returns 'active' on 200 response."""
    creds = Credentials(
        product="tobogganing",
        account_id="acc-123",
        client_id="cli-456",
        client_secret="secret",
        scopes=["scope1"],
        issued_at="2025-01-01T00:00:00Z",
        expires_at="2025-12-31T23:59:59Z",
    )

    with patch("app.api.integrations.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        status = await _probe_integration_health("tobogganing", creds)
        assert status == "active"


@pytest.mark.asyncio
async def test_probe_integration_health_degraded():
    """Test health probe returns 'degraded' on non-200 response."""
    creds = Credentials(
        product="tobogganing",
        account_id="acc-123",
        client_id="cli-456",
        client_secret="secret",
        scopes=["scope1"],
        issued_at="2025-01-01T00:00:00Z",
        expires_at="2025-12-31T23:59:59Z",
    )

    with patch("app.api.integrations.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_resp = AsyncMock()
        mock_resp.status_code = 500
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        status = await _probe_integration_health("tobogganing", creds)
        assert status == "degraded"


@pytest.mark.asyncio
async def test_probe_integration_health_unreachable():
    """Test health probe returns 'unreachable' on network error."""
    creds = Credentials(
        product="tobogganing",
        account_id="acc-123",
        client_id="cli-456",
        client_secret="secret",
        scopes=["scope1"],
        issued_at="2025-01-01T00:00:00Z",
        expires_at="2025-12-31T23:59:59Z",
    )

    with patch("app.api.integrations.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.side_effect = httpx.NetworkError("Connection refused")
        mock_client_class.return_value = mock_client

        status = await _probe_integration_health("tobogganing", creds)
        assert status == "unreachable"


@pytest.mark.asyncio
async def test_probe_integration_health_unconfigured():
    """Test health probe returns 'unconfigured' when no credentials."""
    status = await _probe_integration_health("tobogganing", None)
    assert status == "unconfigured"


@pytest.mark.asyncio
async def test_probe_integration_health_unconfigured_empty_secret():
    """Test health probe returns 'unconfigured' when credentials have no secret."""
    creds = Credentials(
        product="tobogganing",
        account_id="acc-123",
        client_id="cli-456",
        client_secret="",
        scopes=["scope1"],
        issued_at="2025-01-01T00:00:00Z",
        expires_at="2025-12-31T23:59:59Z",
    )

    status = await _probe_integration_health("tobogganing", creds)
    assert status == "unconfigured"


@pytest.mark.asyncio
async def test_probe_integration_health_unknown_product():
    """Test health probe returns 'degraded' for unknown product."""
    creds = Credentials(
        product="unknown",
        account_id="acc-123",
        client_id="cli-456",
        client_secret="secret",
        scopes=["scope1"],
        issued_at="2025-01-01T00:00:00Z",
        expires_at="2025-12-31T23:59:59Z",
    )

    status = await _probe_integration_health("unknown", creds)
    assert status == "degraded"
