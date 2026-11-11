"""Elder egress policy + per-request pinned-IP transport.

Regression: security review of PR #52.

* Finding 2 (DNS-rebinding TOCTOU): ``elder_url`` was only vetted at save
  time; the Elder client re-resolved the hostname on every call. The
  outbound side now resolves + vets on EVERY request and connects to the
  vetted IP, keeping the original name for the Host header and TLS SNI /
  certificate verification.
* Finding 4 (incomplete blocklist): Elder gets its own strict destination
  policy (public-only unless operator-allowlisted) instead of reusing
  cluster discovery's permissive one, which stays unchanged.
"""

from __future__ import annotations

import ipaddress
import socket

import httpx
import pytest

from app.clients import cluster_discovery
from app.integrations import elder_egress
from app.integrations.elder import ElderClient, ElderConnectionError
from app.integrations.elder_egress import (
    ElderAllowlist,
    ElderDestinationRefused,
    ElderEgressTransport,
    is_destination_allowed,
    load_allowlist,
    vet_elder_destination,
)

EMPTY = ElderAllowlist(hosts=frozenset(), cidrs=())


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an address literal."""
    return ipaddress.ip_address(text)


class _Resolver:
    """Scripted resolver: each call pops the next answer set (last one repeats)."""

    def __init__(self, *answers: list[str]) -> None:
        self._answers = list(answers)
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> list[str]:
        """Return the next scripted answer set for ``host``."""
        self.calls.append(host)
        if len(self._answers) > 1:
            return self._answers.pop(0)
        return self._answers[0]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with an empty operator allowlist."""
    monkeypatch.delenv(elder_egress.ELDER_ALLOWED_HOSTS_ENV, raising=False)
    monkeypatch.delenv(elder_egress.ELDER_ALLOWED_CIDRS_ENV, raising=False)


DENIED = [
    # RFC1918 / ULA
    "10.0.0.1",
    "172.16.0.1",
    "172.31.255.254",
    "192.168.1.1",
    "fc00::1",
    "fd12:3456::1",
    # shared address space (CGNAT)
    "100.64.0.1",
    "100.127.255.254",
    # loopback
    "127.0.0.1",
    "127.8.9.10",
    "::1",
    # link-local incl. cloud metadata
    "169.254.169.254",
    "169.254.0.1",
    "fe80::1",
    "fd00:ec2::254",
    # unspecified / this-network
    "0.0.0.0",
    "0.1.2.3",
    "::",
    # multicast / broadcast
    "224.0.0.1",
    "239.255.255.250",
    "255.255.255.255",
    "ff02::1",
    # reserved
    "240.0.0.1",
    "192.0.0.8",
    # benchmarking
    "198.18.0.1",
    "198.19.255.254",
    # documentation
    "192.0.2.10",
    "198.51.100.10",
    "203.0.113.10",
    "2001:db8::1",
]

WRAPPED_DENIED = [
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "::ffff:10.0.0.1",  # IPv4-mapped private
    "::ffff:169.254.169.254",  # IPv4-mapped metadata
    "::127.0.0.1",  # IPv4-compatible loopback
    "::10.0.0.1",  # IPv4-compatible private
    "64:ff9b::7f00:1",  # NAT64 loopback
    "64:ff9b::a00:1",  # NAT64 private
    "64:ff9b::a9fe:a9fe",  # NAT64 metadata
    "2002:7f00:1::1",  # 6to4 loopback
    "2002:a00:1::1",  # 6to4 private
]

PUBLIC = [
    "8.8.8.8",
    "1.1.1.1",
    "2606:4700:4700::1111",
    "64:ff9b::808:808",
    "::ffff:8.8.8.8",
]


class TestDestinationPolicy:
    """Strict, public-only destination policy for the Elder sink."""

    @pytest.mark.parametrize("addr", DENIED)
    def test_denied_range_refused(self, addr: str) -> None:
        """Every non-public range is refused with an empty allowlist."""
        assert is_destination_allowed(_ip(addr), "elder.example", EMPTY) is False

    @pytest.mark.parametrize("addr", WRAPPED_DENIED)
    def test_wrapped_ipv4_unwrapped_and_refused(self, addr: str) -> None:
        """Mapped / compatible / NAT64 / 6to4 forms are judged by the embedded IPv4."""
        assert is_destination_allowed(_ip(addr), "elder.example", EMPTY) is False

    @pytest.mark.parametrize("addr", PUBLIC)
    def test_public_allowed(self, addr: str) -> None:
        """Globally routable destinations (incl. wrapped public IPv4) are allowed."""
        assert is_destination_allowed(_ip(addr), "elder.example", EMPTY) is True

    def test_private_allowed_only_when_cidr_allowlisted(self) -> None:
        """A private on-prem Elder needs an explicit CIDR allowlist entry."""
        allow = ElderAllowlist(
            hosts=frozenset(), cidrs=(ipaddress.ip_network("10.20.0.0/16"),)
        )
        assert is_destination_allowed(_ip("10.20.0.5"), "elder.lan", allow) is True
        assert is_destination_allowed(_ip("10.21.0.5"), "elder.lan", allow) is False
        assert (
            is_destination_allowed(_ip("::ffff:10.20.0.5"), "elder.lan", allow) is True
        )

    def test_private_allowed_only_when_host_allowlisted(self) -> None:
        """A private on-prem Elder may instead be allowlisted by hostname."""
        allow = ElderAllowlist(hosts=frozenset({"elder.lan"}), cidrs=())
        assert is_destination_allowed(_ip("192.168.4.4"), "elder.lan", allow) is True
        assert is_destination_allowed(_ip("192.168.4.4"), "ELDER.LAN", allow) is True
        assert is_destination_allowed(_ip("192.168.4.4"), "other.lan", allow) is False

    @pytest.mark.parametrize(
        "addr",
        [
            "127.0.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fd00:ec2::254",
            "224.0.0.1",
            "::ffff:127.0.0.1",
            "64:ff9b::a9fe:a9fe",
        ],
    )
    def test_hard_denied_never_allowlistable(self, addr: str) -> None:
        """Loopback/link-local/metadata/unspecified/multicast stay refused even if allowlisted."""
        allow = ElderAllowlist(
            hosts=frozenset({"elder.lan"}),
            cidrs=(ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")),
        )
        assert is_destination_allowed(_ip(addr), "elder.lan", allow) is False


class TestAllowlistConfig:
    """Operator allowlist comes from env, default empty."""

    def test_default_empty(self) -> None:
        """Unset env -> no allowlist."""
        assert load_allowlist() == EMPTY

    def test_parses_hosts_and_cidrs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Comma-separated hosts (case-folded) and CIDRs are parsed."""
        monkeypatch.setenv(
            elder_egress.ELDER_ALLOWED_HOSTS_ENV, " Elder.LAN , elder2.lan,"
        )
        monkeypatch.setenv(
            elder_egress.ELDER_ALLOWED_CIDRS_ENV, "10.20.0.0/16, fd00:1::/64"
        )
        allow = load_allowlist()
        assert allow.hosts == frozenset({"elder.lan", "elder2.lan"})
        assert allow.cidrs == (
            ipaddress.ip_network("10.20.0.0/16"),
            ipaddress.ip_network("fd00:1::/64"),
        )

    def test_invalid_cidr_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A malformed CIDR is a loud refusal, never a silently-ignored entry."""
        monkeypatch.setenv(elder_egress.ELDER_ALLOWED_CIDRS_ENV, "10.20.0.0/99")
        with pytest.raises(ElderDestinationRefused):
            load_allowlist()


class TestVetElderDestination:
    """URL-level vetting: scheme, port, every resolved address."""

    async def test_public_host_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A public answer is pinned and the original host kept for SNI."""
        monkeypatch.setattr(elder_egress, "_resolve_host", _Resolver(["93.184.216.34"]))
        vetted = await vet_elder_destination("https://elder.example.com/api")
        assert (vetted.host, vetted.pinned_ip, vetted.port) == (
            "elder.example.com",
            "93.184.216.34",
            443,
        )

    async def test_any_denied_answer_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mixed answer set containing one private address is refused."""
        monkeypatch.setattr(
            elder_egress, "_resolve_host", _Resolver(["93.184.216.34", "10.0.0.1"])
        )
        with pytest.raises(ElderDestinationRefused):
            await vet_elder_destination("https://elder.example.com/")

    async def test_private_host_allowed_via_env_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GOUGH_ELDER_ALLOWED_CIDRS opts a private Elder in explicitly."""
        monkeypatch.setattr(elder_egress, "_resolve_host", _Resolver(["10.20.0.5"]))
        with pytest.raises(ElderDestinationRefused):
            await vet_elder_destination("https://elder.lan/")
        monkeypatch.setenv(elder_egress.ELDER_ALLOWED_CIDRS_ENV, "10.20.0.0/16")
        vetted = await vet_elder_destination("https://elder.lan/")
        assert vetted.pinned_ip == "10.20.0.5"

    @pytest.mark.parametrize(
        "url",
        ["http://elder.example.com/", "https://elder.example.com:99999/", "https:///x"],
    )
    async def test_bad_scheme_port_or_host_refused(
        self, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        """Non-https, out-of-range port and host-less URLs are refused (never ValueError)."""
        monkeypatch.setattr(elder_egress, "_resolve_host", _Resolver(["93.184.216.34"]))
        with pytest.raises(ElderDestinationRefused):
            await vet_elder_destination(url)

    async def test_unresolvable_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A resolver failure is a refusal."""

        async def _boom(host: str, port: int) -> list[str]:
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(elder_egress, "_resolve_host", _boom)
        with pytest.raises(ElderDestinationRefused):
            await vet_elder_destination("https://nope.example/")


class _Capture(httpx.AsyncBaseTransport):
    """Inner transport that records the request it was handed."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Record and answer 200 {"status": "healthy"}."""
        self.requests.append(request)
        return httpx.Response(200, json={"status": "healthy"})


class TestPinnedTransport:
    """Every outbound request is re-vetted and sent to the pinned IP."""

    async def test_connects_to_pinned_ip_with_original_host_and_sni(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """URL host = vetted IP; Host header + sni_hostname = original name (TLS verify)."""
        monkeypatch.setattr(elder_egress, "_resolve_host", _Resolver(["93.184.216.34"]))
        inner = _Capture()
        async with httpx.AsyncClient(
            transport=ElderEgressTransport(inner=inner)
        ) as http:
            resp = await http.get("https://elder.example.com/api/v1/health")
        assert resp.status_code == 200
        sent = inner.requests[0]
        assert sent.url.host == "93.184.216.34"
        assert sent.url.scheme == "https"
        assert sent.url.path == "/api/v1/health"
        assert sent.headers["Host"] == "elder.example.com"
        assert sent.extensions["sni_hostname"] == "elder.example.com"

    async def test_ipv6_pinned_ip_bracketed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An IPv6 pin produces a valid URL and keeps the original Host."""
        monkeypatch.setattr(
            elder_egress, "_resolve_host", _Resolver(["2606:4700:4700::1111"])
        )
        inner = _Capture()
        async with httpx.AsyncClient(
            transport=ElderEgressTransport(inner=inner)
        ) as http:
            await http.get("https://elder.example.com:8443/x")
        sent = inner.requests[0]
        assert sent.url.host == "2606:4700:4700::1111"
        assert sent.url.port == 8443
        assert sent.headers["Host"] == "elder.example.com:8443"
        assert sent.extensions["sni_hostname"] == "elder.example.com"

    async def test_rebinding_refused_at_request_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Public at save time, private at request time -> refused before any connect."""
        resolver = _Resolver(["93.184.216.34"], ["10.0.0.7"])
        monkeypatch.setattr(elder_egress, "_resolve_host", resolver)
        await vet_elder_destination("https://rebind.example/")  # save-time check passes
        inner = _Capture()
        client = ElderClient(
            "https://rebind.example", "k", max_retries=3, inner_transport=inner
        )
        with pytest.raises(ElderConnectionError):
            await client.health_check()
        await client.close()
        assert inner.requests == []  # nothing ever reached the network layer
        assert resolver.calls == ["rebind.example", "rebind.example"]

    async def test_every_request_is_revetted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two requests through one client resolve twice; the second (private) is refused."""
        resolver = _Resolver(["93.184.216.34"], ["127.0.0.1"])
        monkeypatch.setattr(elder_egress, "_resolve_host", resolver)
        inner = _Capture()
        client = ElderClient("https://elder.example.com", "k", inner_transport=inner)
        assert await client.health_check() is True
        with pytest.raises(ElderConnectionError):
            await client.health_check()
        await client.close()
        assert len(inner.requests) == 1
        assert len(resolver.calls) == 2

    async def test_client_uses_pinned_transport_by_default(self) -> None:
        """ElderClient's real httpx client is wired through ElderEgressTransport."""
        client = ElderClient("https://elder.example.com", "k")
        await client._ensure_client()
        assert client._client is not None
        assert isinstance(client._client._transport, ElderEgressTransport)
        assert client._client.follow_redirects is False
        await client.close()


class TestClusterDiscoveryUnchanged:
    """Cluster discovery keeps its own (private-allowing) semantics."""

    @pytest.mark.parametrize(
        "addr", ["10.0.0.1", "192.168.1.1", "100.64.0.1", "172.16.0.1"]
    )
    def test_private_still_allowed_for_discovery(self, addr: str) -> None:
        """Brownfield clusters on the operator's network remain reachable for adoption."""
        assert cluster_discovery._is_blocked_ip(_ip(addr)) is False

    @pytest.mark.parametrize("addr", ["127.0.0.1", "169.254.169.254", "::1", "0.0.0.0"])
    def test_blocked_still_blocked_for_discovery(self, addr: str) -> None:
        """Discovery's own blocklist is unchanged."""
        assert cluster_discovery._is_blocked_ip(_ip(addr)) is True
