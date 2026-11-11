"""Outbound destination policy and pinned-IP transport for the Elder sink.

``elder_url`` is operator-supplied and fetched by this server with a bearer
API key attached, so it is an SSRF sink. Two defences live here:

* **Strict destination policy** (:func:`is_destination_allowed`). Elder gets
  its own policy rather than cluster discovery's (which deliberately allows
  private ranges for brownfield adoption). A destination is allowed only if
  it is globally routable, or if its hostname / an operator CIDR is on the
  allowlist (``GOUGH_ELDER_ALLOWED_HOSTS`` / ``GOUGH_ELDER_ALLOWED_CIDRS``,
  default empty) so on-prem private Elder deployments are an explicit opt-in.
  Loopback, link-local (incl. cloud metadata), unspecified/``0.0.0.0/8``,
  multicast and broadcast are refused even when allowlisted. IPv4-mapped,
  IPv4-compatible, NAT64 (``64:ff9b::/96``, ``64:ff9b:1::/48``) and 6to4
  addresses are judged by their embedded IPv4.
  # regression: PR #52 review (incomplete blocklist for a new sink)

* **Per-request resolve + pin** (:class:`ElderEgressTransport`). Vetting
  only at save time left a DNS-rebinding TOCTOU: the client re-resolved the
  hostname on every call. The transport resolves and vets on EVERY request,
  then connects to the vetted literal IP while keeping the original name as
  the ``Host`` header and as ``sni_hostname`` -- which httpcore uses for
  both TLS SNI and certificate hostname verification. Same pattern as
  :mod:`app.clients.cluster_discovery` (PR #41).
  # regression: PR #52 review (SSRF DNS-rebinding TOCTOU)
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

ELDER_ALLOWED_HOSTS_ENV = "GOUGH_ELDER_ALLOWED_HOSTS"
ELDER_ALLOWED_CIDRS_ENV = "GOUGH_ELDER_ALLOWED_CIDRS"

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _nets(*cidrs: str) -> tuple[IPNetwork, ...]:
    """Parse a fixed list of CIDR literals."""
    return tuple(ipaddress.ip_network(c) for c in cidrs)


#: Never reachable, allowlist or not: loopback, link-local (cloud metadata),
#: unspecified / this-network, multicast, limited broadcast.
_HARD_DENY: tuple[IPNetwork, ...] = _nets(
    "0.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "224.0.0.0/4",
    "255.255.255.255/32",
    "::/128",
    "::1/128",
    "fe80::/10",
    "ff00::/8",
    "fd00:ec2::254/128",
)

#: Refused unless allowlisted: private, shared (CGNAT), IETF-protocol,
#: documentation, benchmarking, reserved, ULA, site-local, discard.
_SOFT_DENY: tuple[IPNetwork, ...] = _nets(
    "10.0.0.0/8",
    "100.64.0.0/10",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "240.0.0.0/4",
    "fc00::/7",
    "fec0::/10",
    "100::/64",
    "2001::/23",
    "2001:db8::/32",
    "3fff::/20",
)

_IPV4_COMPAT = ipaddress.ip_network("::/96")
_NAT64 = _nets("64:ff9b::/96", "64:ff9b:1::/48")


class ElderDestinationRefused(Exception):
    """An Elder URL / resolved address is not an allowed outbound destination."""


@dataclass(slots=True, frozen=True)
class ElderAllowlist:
    """Operator opt-in for non-public Elder destinations (hostnames, CIDRs)."""

    hosts: frozenset[str]
    cidrs: tuple[IPNetwork, ...]


@dataclass(slots=True, frozen=True)
class VettedElderEndpoint:
    """An Elder URL whose host resolved only to allowed addresses.

    ``pinned_ip`` is the address the connection MUST use (never re-resolved);
    ``host`` is kept for the Host header and TLS SNI / cert verification.
    """

    scheme: str
    host: str
    port: int
    pinned_ip: str


def load_allowlist() -> ElderAllowlist:
    """Read the operator allowlist from env (comma-separated, default empty).

    A malformed CIDR raises :class:`ElderDestinationRefused` (fail closed)
    rather than being silently dropped.
    """
    hosts = frozenset(
        h.strip().lower().rstrip(".")
        for h in os.environ.get(ELDER_ALLOWED_HOSTS_ENV, "").split(",")
        if h.strip()
    )
    cidrs: list[IPNetwork] = []
    for raw in os.environ.get(ELDER_ALLOWED_CIDRS_ENV, "").split(","):
        if not raw.strip():
            continue
        try:
            cidrs.append(ipaddress.ip_network(raw.strip(), strict=False))
        except ValueError as exc:
            raise ElderDestinationRefused(
                f"{ELDER_ALLOWED_CIDRS_ENV} entry {raw.strip()!r} is not a valid CIDR"
            ) from exc
    return ElderAllowlist(hosts=hosts, cidrs=tuple(cidrs))


def embedded_ipv4(ip: IPAddress) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address embedded in a mapped/compat/NAT64/6to4 IPv6 address."""
    if isinstance(ip, ipaddress.IPv4Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    low32 = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if any(ip in net for net in _NAT64):
        return low32
    if ip in _IPV4_COMPAT and int(ip) > 1:  # ::/128 and ::1 are not wrappers
        return low32
    return None


def _in_any(ip: IPAddress, nets: tuple[IPNetwork, ...]) -> bool:
    """True iff ``ip`` falls inside one of ``nets`` (same address family)."""
    return any(ip.version == n.version and ip in n for n in nets)


def is_destination_allowed(ip: IPAddress, host: str, allowlist: ElderAllowlist) -> bool:
    """Decide whether Elder traffic may go to ``ip`` (reached via ``host``).

    Wrapped IPv4 forms are judged by the embedded IPv4. Hard-denied ranges
    are never allowed; other non-public ranges only when ``host`` or the
    address is allowlisted; globally routable addresses always.
    """
    inner = embedded_ipv4(ip)
    candidates: tuple[IPAddress, ...] = (ip,) if inner is None else (inner, ip)
    target = candidates[0]
    if any(_in_any(c, _HARD_DENY) for c in candidates):
        return False
    public = not _in_any(target, _SOFT_DENY) and target.is_global
    if public:
        return True
    if host.lower().rstrip(".") in allowlist.hosts:
        return True
    return any(_in_any(c, allowlist.cidrs) for c in candidates)


async def _resolve_host(host: str, port: int) -> list[str]:
    """Resolve ``host`` to every address the system resolver returns (patch point)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port)
    return [str(sockaddr[0]) for _f, _t, _p, _c, sockaddr in infos]


async def vet_elder_destination(
    url: str, allowlist: ElderAllowlist | None = None
) -> VettedElderEndpoint:
    """Resolve ``url``'s host and vet EVERY answer against the Elder policy.

    Raises :class:`ElderDestinationRefused` for a non-https scheme, missing
    host, unparseable port, resolver failure, or any disallowed address.
    Messages may name the host/IP and are for server-side logs only.
    """
    allow = allowlist if allowlist is not None else load_allowlist()
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError as exc:
        raise ElderDestinationRefused(f"elder_url is malformed: {exc}") from exc
    if parts.scheme != "https":
        raise ElderDestinationRefused(f"elder_url scheme {parts.scheme!r} is not https")
    host = parts.hostname
    if not host:
        raise ElderDestinationRefused("elder_url has no host")

    try:
        literal: IPAddress | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        answers = [str(literal)]
    else:
        try:
            answers = await _resolve_host(host, port)
        except OSError as exc:
            raise ElderDestinationRefused(
                f"elder host {host!r} did not resolve: {exc}"
            ) from exc

    pinned: str | None = None
    for answer in answers:
        try:
            ip = ipaddress.ip_address(answer.split("%", 1)[0])
        except ValueError:
            continue
        if not is_destination_allowed(ip, host, allow):
            raise ElderDestinationRefused(
                f"elder host {host!r} resolves to disallowed address {ip}"
            )
        if pinned is None:
            pinned = str(ip)
    if pinned is None:
        raise ElderDestinationRefused(
            f"elder host {host!r} resolved to no usable address"
        )
    return VettedElderEndpoint(
        scheme=parts.scheme, host=host, port=port, pinned_ip=pinned
    )


class ElderEgressTransport(httpx.AsyncBaseTransport):
    """httpx transport that re-vets and IP-pins every outbound Elder request.

    The request URL's host is swapped for the freshly vetted IP; the
    original ``Host`` header is kept and ``sni_hostname`` is set to the
    original name so TLS SNI and certificate verification still target it.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        """Wrap ``inner`` (default: a verifying ``httpx.AsyncHTTPTransport``)."""
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Vet + pin, then delegate; raises :class:`ElderDestinationRefused` first."""
        vetted = await vet_elder_destination(str(request.url))
        if "Host" not in request.headers:
            request.headers["Host"] = request.url.netloc.decode("ascii")
        request.url = request.url.copy_with(host=vetted.pinned_ip)
        request.extensions = {**request.extensions, "sni_hostname": vetted.host}
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._inner.aclose()


__all__ = [
    "ELDER_ALLOWED_CIDRS_ENV",
    "ELDER_ALLOWED_HOSTS_ENV",
    "ElderAllowlist",
    "ElderDestinationRefused",
    "ElderEgressTransport",
    "VettedElderEndpoint",
    "embedded_ipv4",
    "is_destination_allowed",
    "load_allowlist",
    "vet_elder_destination",
]
