"""Tests for ``app.clients.cluster_discovery``.

Covers the SSRF guard (``_validate_discovery_endpoint``) added after a
security review flagged that the adopt-endpoint is fully operator-controlled
request input flowing straight into an outbound fetch -- metadata/loopback/
link-local targets must be rejected before any network I/O, while private
ranges (a brownfield cluster's real, legitimate address) must still be
allowed. # regression: SSRF cluster_discovery (sec review)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.clients.cluster_discovery import (
    ClusterDiscoveryUnavailable,
    DiscoveredMember,
    discover_inventory,
)


@pytest.mark.asyncio
async def test_rejects_cloud_metadata_ip() -> None:
    """169.254.169.254 (AWS/GCP/Azure IMDS) must be rejected before any
    fetch -- the classic SSRF pivot to steal node cloud credentials."""
    with pytest.raises(ClusterDiscoveryUnavailable, match="blocked address"):
        await discover_inventory(
            kind="lxd", endpoint="http://169.254.169.254/latest/meta-data/",
        )


@pytest.mark.asyncio
async def test_rejects_loopback() -> None:
    with pytest.raises(ClusterDiscoveryUnavailable, match="blocked address"):
        await discover_inventory(kind="lxd", endpoint="http://127.0.0.1:8443")


@pytest.mark.asyncio
async def test_rejects_ipv6_loopback() -> None:
    with pytest.raises(ClusterDiscoveryUnavailable, match="blocked address"):
        await discover_inventory(kind="lxd", endpoint="http://[::1]:8443")


@pytest.mark.asyncio
async def test_rejects_ipv6_link_local() -> None:
    with pytest.raises(ClusterDiscoveryUnavailable, match="blocked address"):
        await discover_inventory(kind="lxd", endpoint="http://[fe80::1]:8443")


@pytest.mark.asyncio
async def test_rejects_non_http_scheme() -> None:
    with pytest.raises(ClusterDiscoveryUnavailable, match="scheme"):
        await discover_inventory(kind="lxd", endpoint="gopher://169.254.169.254/")


@pytest.mark.asyncio
async def test_rejects_file_scheme() -> None:
    with pytest.raises(ClusterDiscoveryUnavailable, match="scheme"):
        await discover_inventory(kind="lxd", endpoint="file:///etc/passwd")


@pytest.mark.asyncio
async def test_allows_private_network_endpoint() -> None:
    """A real brownfield cluster on the operator's own network (10/8) must
    NOT be blocked -- private ranges are an accepted, admin-scoped risk,
    not the SSRF vector this guard targets."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={
        "metadata": [{"server_name": "node-a", "url": "10.0.0.5", "status": "Online", "roles": []}],
    })

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.clients.cluster_discovery.httpx.AsyncClient", return_value=mock_client):
        members = await discover_inventory(kind="lxd", endpoint="https://10.0.0.5:8443")

    assert members == [DiscoveredMember(name="node-a", address="10.0.0.5", role=None, status="Online")]
    mock_client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_rejects_empty_endpoint() -> None:
    with pytest.raises(ClusterDiscoveryUnavailable, match="endpoint is required"):
        await discover_inventory(kind="lxd", endpoint="")


@pytest.mark.asyncio
async def test_rejects_unsupported_kind() -> None:
    with pytest.raises(ClusterDiscoveryUnavailable, match="unsupported"):
        await discover_inventory(kind="docker", endpoint="https://10.0.0.5:8443")
