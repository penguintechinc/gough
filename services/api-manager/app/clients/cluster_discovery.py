"""Brownfield cluster discovery boundary for adoption (M2).

``app.api.clusters.adopt_cluster``'s M2 execution ingests an existing LXD or
Kubernetes cluster's node/member inventory into gough's model. gough has no
existing client for an arbitrary *external* (not-yet-managed) LXD or K8s
endpoint -- ``app/clients/lxd_extra.py`` talks to clusters gough already
manages via Vault-pinned credentials, which do not exist yet for a cluster
that has not been adopted. This module is the real boundary that gap needs:
a genuine HTTPS call against the target cluster's own REST API, parsing its
real response shape (LXD's ``/1.0/cluster/members`` or Kubernetes'
``/api/v1/nodes``) -- never a fabricated member list. An unreachable or
non-2xx endpoint raises :class:`ClusterDiscoveryUnavailable`; the caller
treats that as a hard failure and rolls back, it never reports members that
were never actually observed.

``credentials_ref`` is a Vault path per ``security.md`` (secrets are never
inline) -- resolving it to a bearer token requires ``VaultClient``, which
needs ``VAULT_TOKEN`` configured. Best-effort: when Vault is not configured
in this environment, discovery proceeds unauthenticated and lets the target
API's own ``401``/``403`` surface as the failure, rather than silently
skipping discovery.

Tests patch :func:`discover_inventory` wholesale to exercise both the
success and rollback/failure paths without a live cluster.

SSRF note (``endpoint`` is fully operator-controlled request input, feeding
straight into an outbound fetch): :func:`_validate_discovery_endpoint`
resolves the host and rejects loopback/link-local/cloud-metadata targets
(``127.0.0.0/8``, ``::1``, ``169.254.0.0/16`` incl. the ``169.254.169.254``
metadata address, ``fe80::/10``, ``fd00:ec2::254``) before any network I/O.
Private ranges (``10/8``, ``172.16/12``, ``192.168/16``) are deliberately
allowed -- a brownfield cluster legitimately lives on the operator's own
network; that's an accepted, admin-scoped risk, not the SSRF vector this
guards against. # regression: SSRF cluster_discovery (sec review)

DNS-rebinding TOCTOU note: the check above used to hand the *hostname* to
``httpx``, which re-resolves at connect time -- a DNS answer that changes
between the check and the connect (rebinding) could still reach a blocked
address. :func:`_validate_discovery_endpoint` now returns a
:class:`_VettedEndpoint` carrying the single IP address it just vetted, and
:func:`discover_inventory` connects to that literal IP (never the hostname
again), with the original hostname preserved only as the ``Host`` header
and TLS SNI name for certificate verification. No second DNS lookup occurs
between validation and connect, and redirect-following is explicitly
disabled so a 302 response cannot reintroduce the bypass.
# regression: SSRF TOCTOU cluster_discovery (sec review)
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

#: Discovery call timeout. Short because this gates a user-facing request.
DISCOVERY_TIMEOUT_SECONDS = float(os.environ.get("CLUSTER_DISCOVERY_TIMEOUT_SECONDS", "10"))

_LXD_MEMBERS_PATH = "/1.0/cluster/members"
_K8S_NODES_PATH = "/api/v1/nodes"

_VALID_KINDS = frozenset({"lxd", "k8s"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Addresses blocked outright even though they fall outside the generic
#: loopback/link-local checks below (cloud-provider IMDS endpoints on
#: non-link-local ranges).
_METADATA_EXACT_BLOCK: frozenset[str] = frozenset({"fd00:ec2::254"})


class ClusterDiscoveryError(Exception):
    """Base class for brownfield cluster discovery failures."""


class ClusterDiscoveryUnavailable(ClusterDiscoveryError):
    """The target cluster could not be reached/parsed to discover inventory."""


@dataclass(slots=True)
class DiscoveredMember:
    """One node/member observed on the adopted cluster."""

    name: str
    address: str | None = None
    role: str | None = None
    status: str | None = None


@dataclass(slots=True, frozen=True)
class _VettedEndpoint:
    """A discovery endpoint whose host has already been resolved and
    cleared against the SSRF blocklist.

    ``pinned_ip`` is the exact, already-vetted address the outbound
    connection MUST use -- it is never re-resolved. ``host`` is kept
    separately, used only for the ``Host`` header and TLS SNI, so the
    connection target and the certificate/virtual-host identity can differ
    safely. # regression: SSRF TOCTOU cluster_discovery (sec review)
    """

    scheme: str
    host: str
    pinned_ip: str
    port: int


def _resolve_bearer_token(credentials_ref: str) -> str | None:
    """Best-effort Vault lookup for ``credentials_ref``.

    Returns ``None`` (proceed unauthenticated) rather than raising when
    Vault is not configured -- an unconfigured Vault is an environment gap,
    not a reason to fabricate discovery results, so the HTTP call below
    still happens and surfaces the target API's own auth failure if a token
    was actually required.
    """
    if not credentials_ref:
        return None
    try:
        from app.clients.vault import VaultClient

        client = VaultClient()
        secret = client.kv_read(credentials_ref)
        token = secret.data.get("token") if hasattr(secret, "data") else None
        return str(token) if token else None
    except Exception as exc:  # noqa: BLE001 - best-effort only, see docstring
        log.warning(
            "credentials_ref=%s could not be resolved via Vault (%s); "
            "proceeding with discovery unauthenticated",
            credentials_ref,
            exc,
        )
        return None


def _parse_lxd_members(payload: dict) -> list[DiscoveredMember]:
    raw = payload.get("metadata") or []
    members: list[DiscoveredMember] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        roles = item.get("roles") or []
        members.append(
            DiscoveredMember(
                name=str(item.get("server_name", "")),
                address=item.get("url"),
                role=",".join(roles) if roles else None,
                status=item.get("status"),
            )
        )
    return members


def _parse_k8s_nodes(payload: dict) -> list[DiscoveredMember]:
    raw = payload.get("items") or []
    members: list[DiscoveredMember] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        addresses = status.get("addresses") or []
        address = next(
            (a.get("address") for a in addresses if isinstance(a, dict) and a.get("address")),
            None,
        )
        conditions = status.get("conditions") or []
        ready = any(
            isinstance(c, dict) and c.get("type") == "Ready" and c.get("status") == "True"
            for c in conditions
        )
        members.append(
            DiscoveredMember(
                name=str(meta.get("name", "")),
                address=address,
                role="worker",
                status="ready" if ready else "not_ready",
            )
        )
    return members


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True iff ``ip`` is loopback, link-local, or a known cloud-metadata
    address -- the SSRF-relevant blocklist. Private ranges are NOT blocked
    here; see module docstring."""
    if str(ip) in _METADATA_EXACT_BLOCK:
        return True
    return bool(ip.is_loopback or ip.is_link_local)


async def _validate_discovery_endpoint(endpoint: str) -> _VettedEndpoint:
    """Fail-closed SSRF guard for an operator-supplied adopt ``endpoint``.

    Resolves the host exactly once and rejects it (raising
    :class:`ClusterDiscoveryUnavailable`) if any resolved address is
    loopback/link-local/cloud-metadata, or if the scheme isn't http(s) --
    all BEFORE any network I/O happens. Returns a :class:`_VettedEndpoint`
    pinning one already-vetted resolved address -- the caller connects to
    that literal IP and never re-resolves the host, closing the
    DNS-rebinding TOCTOU window between this check and the actual connect.
    # regression: SSRF TOCTOU cluster_discovery (sec review)
    """
    parsed = urlparse(endpoint)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ClusterDiscoveryUnavailable(
            f"endpoint scheme {parsed.scheme!r} is not allowed; only http/https"
        )
    host = parsed.hostname
    if not host:
        raise ClusterDiscoveryUnavailable(f"endpoint {endpoint!r} has no resolvable host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    loop = asyncio.get_running_loop()
    try:
        resolved = await loop.getaddrinfo(host, port)
    except OSError as exc:
        raise ClusterDiscoveryUnavailable(
            f"endpoint host {host!r} could not be resolved: {exc}"
        ) from exc

    pinned_ip: str | None = None
    for _family, _type, _proto, _canonname, sockaddr in resolved:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise ClusterDiscoveryUnavailable(
                f"endpoint host {host!r} resolves to a blocked address "
                f"{ip} (loopback/link-local/metadata)"
            )
        if pinned_ip is None:
            pinned_ip = str(ip)

    if pinned_ip is None:
        raise ClusterDiscoveryUnavailable(
            f"endpoint host {host!r} did not resolve to a usable IP address"
        )

    return _VettedEndpoint(scheme=parsed.scheme, host=host, pinned_ip=pinned_ip, port=port)


async def discover_inventory(
    *, kind: str, endpoint: str, credentials_ref: str = ""
) -> list[DiscoveredMember]:
    """Discover a brownfield cluster's member inventory for adoption.

    ``kind`` must be ``"lxd"`` or ``"k8s"``. Raises
    :class:`ClusterDiscoveryUnavailable` if ``endpoint`` is empty, resolves
    to a blocked (loopback/link-local/metadata) address, is unreachable, or
    the response cannot be parsed.
    """
    if kind not in _VALID_KINDS:
        raise ClusterDiscoveryUnavailable(f"unsupported adoption kind={kind!r}")
    if not endpoint:
        raise ClusterDiscoveryUnavailable(
            "endpoint is required to discover an adopted cluster's inventory"
        )
    vetted = await _validate_discovery_endpoint(endpoint)

    path = _LXD_MEMBERS_PATH if kind == "lxd" else _K8S_NODES_PATH
    # Connect to the pinned IP literally -- never the hostname again (that
    # would re-resolve at connect time and reopen the DNS-rebinding TOCTOU
    # window the validation above just closed). The hostname is preserved
    # below only as the Host header / TLS SNI name.
    # regression: SSRF TOCTOU cluster_discovery (sec review)
    connect_host = f"[{vetted.pinned_ip}]" if ":" in vetted.pinned_ip else vetted.pinned_ip
    url = f"{vetted.scheme}://{connect_host}:{vetted.port}{path}"
    display_url = f"{vetted.scheme}://{vetted.host}:{vetted.port}{path}"

    headers: dict[str, str] = {"Host": vetted.host}
    token = _resolve_bearer_token(credentials_ref)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(
            timeout=DISCOVERY_TIMEOUT_SECONDS, follow_redirects=False
        ) as http:
            # Explicit, not just relying on the httpx default: a redirect
            # response (e.g. 302 -> http://169.254.169.254) must never be
            # followed, or it would reintroduce the exact SSRF bypass the
            # pinned-IP connect above closes.
            # regression: SSRF TOCTOU cluster_discovery (sec review)
            assert http.follow_redirects is False, (
                "httpx redirect-following must stay disabled for discovery requests"
            )
            response = await http.get(
                url,
                headers=headers,
                extensions={"sni_hostname": vetted.host},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise ClusterDiscoveryUnavailable(
            f"brownfield cluster unreachable at {display_url!r}: {exc}"
        ) from exc
    except ValueError as exc:  # JSON decode failure
        raise ClusterDiscoveryUnavailable(
            f"brownfield cluster at {display_url!r} returned a non-JSON response: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise ClusterDiscoveryUnavailable(
            f"brownfield cluster at {display_url!r} returned an unexpected response shape"
        )

    return _parse_lxd_members(payload) if kind == "lxd" else _parse_k8s_nodes(payload)


__all__ = [
    "DISCOVERY_TIMEOUT_SECONDS",
    "ClusterDiscoveryError",
    "ClusterDiscoveryUnavailable",
    "DiscoveredMember",
    "discover_inventory",
]
