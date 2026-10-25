"""Parsing, credential resolution and failure-mode tests for cluster discovery.

Complements ``test_cluster_discovery.py`` (SSRF vetting and pinned connect) with
the response-shape parsers for LXD and Kubernetes inventories, the Vault
bearer-token lookup, the metadata/loopback blocklist, and every
``ClusterDiscoveryUnavailable`` branch reachable without real network I/O.
IP-literal endpoints are used so ``getaddrinfo`` never touches DNS.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.clients import cluster_discovery as cd
from app.clients.cluster_discovery import (
    ClusterDiscoveryUnavailable,
    DiscoveredMember,
    discover_inventory,
)

# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def test_lxd_parser_maps_fields_and_skips_non_dict_items() -> None:
    """LXD members keep name/url/status, join roles with commas, and drop non-dict entries."""
    payload = {
        "metadata": [
            {
                "server_name": "n1",
                "url": "https://10.0.0.1:8443",
                "roles": ["database", "event"],
                "status": "Online",
            },
            "garbage-entry",
            {"server_name": "n2", "url": None, "roles": [], "status": "Offline"},
        ]
    }

    members = cd._parse_lxd_members(payload)

    assert members == [
        DiscoveredMember(
            name="n1",
            address="https://10.0.0.1:8443",
            role="database,event",
            status="Online",
        ),
        DiscoveredMember(name="n2", address=None, role=None, status="Offline"),
    ]


def test_lxd_parser_handles_missing_metadata() -> None:
    """A payload without metadata yields an empty inventory instead of raising."""
    assert cd._parse_lxd_members({}) == []
    assert cd._parse_lxd_members({"metadata": None}) == []


def test_k8s_parser_ready_and_not_ready_and_first_usable_address() -> None:
    """K8s nodes are workers; Ready=True maps to ready, anything else to not_ready; first non-empty address wins."""
    payload = {
        "items": [
            {
                "metadata": {"name": "w1"},
                "status": {
                    "addresses": [
                        {"type": "InternalIP", "address": ""},
                        {"type": "InternalIP", "address": "10.1.0.4"},
                    ],
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            },
            {
                "metadata": {"name": "w2"},
                "status": {
                    "addresses": [],
                    "conditions": [{"type": "Ready", "status": "False"}],
                },
            },
            "not-a-dict",
        ]
    }

    members = cd._parse_k8s_nodes(payload)

    assert members == [
        DiscoveredMember(name="w1", address="10.1.0.4", role="worker", status="ready"),
        DiscoveredMember(name="w2", address=None, role="worker", status="not_ready"),
    ]


def test_k8s_parser_missing_items_yields_empty() -> None:
    """A payload without items yields an empty inventory."""
    assert cd._parse_k8s_nodes({}) == []


@pytest.mark.parametrize(
    ("ip", "blocked"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("169.254.169.254", True),
        ("fe80::1", True),
        ("fd00:ec2::254", True),
        ("10.0.0.5", False),
        ("192.168.1.10", False),
    ],
)
def test_blocklist_covers_loopback_link_local_and_metadata(
    ip: str, blocked: bool
) -> None:
    """Loopback, link-local and cloud-metadata addresses are blocked; private LAN ranges are not."""
    assert cd._is_blocked_ip(ipaddress.ip_address(ip)) is blocked


# ---------------------------------------------------------------------------
# Vault bearer-token resolution
# ---------------------------------------------------------------------------


def test_bearer_token_empty_ref_returns_none_without_vault_call() -> None:
    """No credentials_ref means unauthenticated discovery, with no Vault lookup attempted."""
    with patch("app.clients.vault.VaultClient") as vault_cls:
        assert cd._resolve_bearer_token("") is None
    vault_cls.assert_not_called()


def test_bearer_token_returned_from_vault_secret() -> None:
    """A KV secret carrying ``token`` is returned as a string."""
    client = MagicMock()
    client.kv_read.return_value = MagicMock(data={"token": 12345})
    with patch("app.clients.vault.VaultClient", return_value=client):
        assert cd._resolve_bearer_token("kv/lxd") == "12345"
    client.kv_read.assert_called_once_with("kv/lxd")


def test_bearer_token_vault_failure_degrades_to_unauthenticated() -> None:
    """A Vault exception is logged and swallowed; discovery proceeds without a token."""
    client = MagicMock()
    client.kv_read.side_effect = RuntimeError("vault sealed")
    with patch("app.clients.vault.VaultClient", return_value=client):
        assert cd._resolve_bearer_token("kv/lxd") is None


def test_bearer_token_secret_without_token_key_is_none() -> None:
    """A secret lacking a ``token`` field yields no bearer token."""
    client = MagicMock()
    client.kv_read.return_value = MagicMock(data={"password": "x"})
    with patch("app.clients.vault.VaultClient", return_value=client):
        assert cd._resolve_bearer_token("kv/lxd") is None


# ---------------------------------------------------------------------------
# Endpoint vetting failures (no network)
# ---------------------------------------------------------------------------


async def test_endpoint_without_host_rejected() -> None:
    """An http URL with no host component is refused before resolution."""
    with pytest.raises(ClusterDiscoveryUnavailable, match="has no resolvable host"):
        await cd._validate_discovery_endpoint("http://:8443")


async def test_metadata_ip_literal_rejected_on_discover() -> None:
    """A metadata IP literal endpoint is refused by discover_inventory before any HTTP call."""
    with patch("app.clients.cluster_discovery.httpx.AsyncClient") as client_cls:
        with pytest.raises(ClusterDiscoveryUnavailable, match="blocked address"):
            await discover_inventory(
                kind="k8s", endpoint="http://169.254.169.254/latest"
            )
    client_cls.assert_not_called()


async def test_unresolvable_host_reported_as_unavailable() -> None:
    """A resolver OSError is wrapped as ClusterDiscoveryUnavailable with the host named."""
    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", side_effect=OSError("nxdomain")):
        with pytest.raises(ClusterDiscoveryUnavailable, match="could not be resolved"):
            await cd._validate_discovery_endpoint("https://nowhere.invalid")


async def test_resolution_with_no_usable_address_rejected() -> None:
    """If every resolved sockaddr is non-IP garbage, there is nothing to pin and the call fails."""
    loop = asyncio.get_running_loop()

    async def _garbage(host: str, port: int) -> list[tuple]:
        return [(2, 1, 6, "", ("not-an-ip", port))]

    with patch.object(loop, "getaddrinfo", side_effect=_garbage):
        with pytest.raises(
            ClusterDiscoveryUnavailable, match="did not resolve to a usable IP"
        ):
            await cd._validate_discovery_endpoint("https://cluster.example.com")


# ---------------------------------------------------------------------------
# HTTP outcome handling
# ---------------------------------------------------------------------------


def _client_returning(
    response: Any = None, error: BaseException | None = None
) -> AsyncMock:
    """AsyncMock httpx client whose GET returns ``response`` or raises ``error``."""
    client = AsyncMock()
    if error is not None:
        client.get = AsyncMock(side_effect=error)
    else:
        client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.follow_redirects = False
    return client


def _json_response(
    payload: Any = None, *, json_error: BaseException | None = None
) -> MagicMock:
    """Response double with a no-op raise_for_status and a json() that returns or raises."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if json_error is not None:
        resp.json = MagicMock(side_effect=json_error)
    else:
        resp.json = MagicMock(return_value=payload)
    return resp


async def test_unsupported_kind_rejected_before_network() -> None:
    """An adoption kind outside lxd/k8s is refused with the offending value named."""
    with pytest.raises(
        ClusterDiscoveryUnavailable, match="unsupported adoption kind='nomad'"
    ):
        await discover_inventory(kind="nomad", endpoint="https://203.0.113.9")


async def test_http_error_status_reported_unreachable() -> None:
    """A transport/HTTP error from the target is wrapped with the display URL."""
    client = _client_returning(error=httpx.ConnectError("refused"))
    with patch("app.clients.cluster_discovery.httpx.AsyncClient", return_value=client):
        with pytest.raises(
            ClusterDiscoveryUnavailable,
            match="unreachable at 'https://203.0.113.9:6443/api/v1/nodes'",
        ):
            await discover_inventory(kind="k8s", endpoint="https://203.0.113.9:6443")


async def test_non_json_body_reported_as_non_json() -> None:
    """A body that fails JSON decoding is reported as a non-JSON response."""
    client = _client_returning(
        response=_json_response(json_error=ValueError("bad json"))
    )
    with patch("app.clients.cluster_discovery.httpx.AsyncClient", return_value=client):
        with pytest.raises(ClusterDiscoveryUnavailable, match="non-JSON response"):
            await discover_inventory(kind="lxd", endpoint="https://203.0.113.9:8443")


async def test_list_payload_rejected_as_unexpected_shape() -> None:
    """A JSON array instead of an object is rejected as an unexpected response shape."""
    client = _client_returning(response=_json_response(payload=[{"metadata": []}]))
    with patch("app.clients.cluster_discovery.httpx.AsyncClient", return_value=client):
        with pytest.raises(
            ClusterDiscoveryUnavailable, match="unexpected response shape"
        ):
            await discover_inventory(kind="lxd", endpoint="https://203.0.113.9:8443")


async def test_successful_k8s_discovery_returns_parsed_members() -> None:
    """A valid K8s node list is parsed end-to-end into DiscoveredMember records."""
    body = {
        "items": [
            {
                "metadata": {"name": "worker-a"},
                "status": {
                    "addresses": [{"type": "InternalIP", "address": "10.2.0.7"}],
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            }
        ]
    }
    client = _client_returning(response=_json_response(payload=body))
    with patch("app.clients.cluster_discovery.httpx.AsyncClient", return_value=client):
        members = await discover_inventory(
            kind="k8s", endpoint="https://203.0.113.9:6443"
        )

    assert members == [
        DiscoveredMember(
            name="worker-a", address="10.2.0.7", role="worker", status="ready"
        )
    ]


async def test_ipv6_pinned_connect_is_bracketed() -> None:
    """An IPv6 pinned address is bracketed in the connect URL but kept bare in the Host header."""
    client = _client_returning(response=_json_response(payload={"metadata": []}))
    with patch("app.clients.cluster_discovery.httpx.AsyncClient", return_value=client):
        await discover_inventory(kind="lxd", endpoint="https://[2001:db8::9]:8443")

    url = client.get.await_args.args[0]
    assert url == "https://[2001:db8::9]:8443/1.0/cluster/members"
    assert client.get.await_args.kwargs["headers"]["Host"] == "2001:db8::9"
