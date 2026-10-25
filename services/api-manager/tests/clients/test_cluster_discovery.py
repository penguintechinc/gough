"""Tests for ``app.clients.cluster_discovery``.

Covers the SSRF guard (``_validate_discovery_endpoint``) added after a
security review flagged that the adopt-endpoint is fully operator-controlled
request input flowing straight into an outbound fetch -- metadata/loopback/
link-local targets must be rejected before any network I/O, while private
ranges (a brownfield cluster's real, legitimate address) must still be
allowed. # regression: SSRF cluster_discovery (sec review)
"""

from __future__ import annotations

import asyncio
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
            kind="lxd",
            endpoint="http://169.254.169.254/latest/meta-data/",
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
    mock_response.json = MagicMock(
        return_value={
            "metadata": [
                {
                    "server_name": "node-a",
                    "url": "10.0.0.5",
                    "status": "Online",
                    "roles": [],
                }
            ],
        }
    )

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    # Real httpx.AsyncClient defaults to follow_redirects=False; mirror that
    # on the mock so discover_inventory's explicit assertion holds.
    mock_client.follow_redirects = False

    with patch(
        "app.clients.cluster_discovery.httpx.AsyncClient", return_value=mock_client
    ):
        members = await discover_inventory(kind="lxd", endpoint="https://10.0.0.5:8443")

    assert members == [
        DiscoveredMember(name="node-a", address="10.0.0.5", role=None, status="Online")
    ]
    mock_client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_rejects_empty_endpoint() -> None:
    with pytest.raises(ClusterDiscoveryUnavailable, match="endpoint is required"):
        await discover_inventory(kind="lxd", endpoint="")


@pytest.mark.asyncio
async def test_rejects_unsupported_kind() -> None:
    with pytest.raises(ClusterDiscoveryUnavailable, match="unsupported"):
        await discover_inventory(kind="docker", endpoint="https://10.0.0.5:8443")


# regression: SSRF TOCTOU cluster_discovery (sec review)
@pytest.mark.asyncio
async def test_rejects_hostname_resolving_to_blocked_ip() -> None:
    """A hostname (not a bare IP literal) that resolves to a blocked
    address must be rejected before any network I/O -- the DNS-rebinding
    scenario: validation resolves the host itself, so a hostname whose
    only answer is a blocked IP is caught right here, never reaching the
    connect step at all."""
    loop = asyncio.get_running_loop()

    async def _fake_getaddrinfo(host: str, port: int) -> list[tuple]:
        assert host == "evil.example.com"
        return [(2, 1, 6, "", ("169.254.169.254", port))]

    with patch.object(loop, "getaddrinfo", side_effect=_fake_getaddrinfo):
        with pytest.raises(ClusterDiscoveryUnavailable, match="blocked address"):
            await discover_inventory(
                kind="lxd", endpoint="https://evil.example.com:8443"
            )


# regression: SSRF TOCTOU cluster_discovery (sec review)
@pytest.mark.asyncio
async def test_connect_pins_vetted_ip_not_hostname() -> None:
    """After validation resolves ``cluster.example.com`` to a vetted IP,
    the actual outbound connect MUST target that literal IP (never the
    hostname again -- re-resolving at connect time is exactly the
    DNS-rebinding TOCTOU window this closes), while the hostname survives
    only as the Host header and TLS SNI name. ``getaddrinfo`` must be
    called exactly once end to end -- a second call would mean a second,
    rebindable DNS lookup happened between validation and connect."""
    loop = asyncio.get_running_loop()
    getaddrinfo_calls: list[str] = []

    async def _fake_getaddrinfo(host: str, port: int) -> list[tuple]:
        getaddrinfo_calls.append(host)
        return [(2, 1, 6, "", ("203.0.113.9", port))]

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"metadata": []})

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.follow_redirects = False

    with patch.object(loop, "getaddrinfo", side_effect=_fake_getaddrinfo), patch(
        "app.clients.cluster_discovery.httpx.AsyncClient", return_value=mock_client
    ):
        await discover_inventory(
            kind="lxd", endpoint="https://cluster.example.com:8443"
        )

    # Exactly one DNS lookup for the whole call -- no re-resolution at
    # connect time.
    assert getaddrinfo_calls == ["cluster.example.com"]

    mock_client.get.assert_awaited_once()
    call = mock_client.get.await_args
    called_url = call.args[0]
    called_headers = call.kwargs["headers"]
    called_extensions = call.kwargs["extensions"]

    # Connect target is the pinned IP, not the hostname.
    assert "203.0.113.9" in called_url
    assert "cluster.example.com" not in called_url
    # Hostname survives only as Host header + SNI, for cert verification.
    assert called_headers["Host"] == "cluster.example.com"
    assert called_extensions["sni_hostname"] == "cluster.example.com"


# regression: SSRF TOCTOU cluster_discovery (sec review)
@pytest.mark.asyncio
async def test_redirects_disabled_on_discovery_client() -> None:
    """``follow_redirects=False`` must be passed explicitly to
    ``httpx.AsyncClient`` -- a 302 response redirecting to a blocked
    address (e.g. the metadata IP) must never be followed, or it would
    reintroduce the exact SSRF bypass the pinned-IP connect closes."""
    loop = asyncio.get_running_loop()

    async def _fake_getaddrinfo(host: str, port: int) -> list[tuple]:
        return [(2, 1, 6, "", ("203.0.113.9", port))]

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"metadata": []})

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.follow_redirects = False

    with patch.object(loop, "getaddrinfo", side_effect=_fake_getaddrinfo), patch(
        "app.clients.cluster_discovery.httpx.AsyncClient", return_value=mock_client
    ) as mock_ctor:
        await discover_inventory(
            kind="lxd", endpoint="https://cluster.example.com:8443"
        )

    assert mock_ctor.call_args.kwargs["follow_redirects"] is False
