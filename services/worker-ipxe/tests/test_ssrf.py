"""SSRF defence for presigned storage URLs: classify once, connect to the checked IP."""

import asyncio
import ipaddress
import socket

import httpx
import pytest
import respx
from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager
from worker.services import http_server as http_mod
from worker.services.http_server import (
    HTTPBootServer,
    _ip_blocked,
    _make_storage_client,
    _PinnedIPBackend,
    _storage_host,
    _validate_presigned_url,
)

REAL_RESOLVE = http_mod._resolve_host  # captured before the autouse fixture patches it


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an IP literal."""
    return ipaddress.ip_address(text)


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "127.1.2.3",
        "0.0.0.0",
        "::1",
        "::",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "240.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_blocked_addresses(addr: str) -> None:
    """Loopback, metadata, multicast, reserved and private ranges (incl. IPv4-mapped IPv6) are blocked."""
    assert _ip_blocked(_ip(addr), allow_private=False) is not None


@pytest.mark.parametrize(
    "addr",
    ["127.0.0.1", "0.0.0.0", "::1", "169.254.169.254", "224.0.0.1", "::ffff:127.0.0.1"],
)
def test_always_blocked_even_for_storage_host(addr: str) -> None:
    """The storage-host exception only relaxes private ranges, never loopback/link-local/multicast."""
    assert _ip_blocked(_ip(addr), allow_private=True) is not None


def test_public_and_storage_private_addresses_allowed() -> None:
    """Public IPs pass; private IPs pass only for the configured storage host."""
    assert _ip_blocked(_ip("93.184.216.34"), allow_private=False) is None
    assert _ip_blocked(_ip("10.43.0.7"), allow_private=False) is not None
    assert _ip_blocked(_ip("10.43.0.7"), allow_private=True) is None


@pytest.mark.parametrize(
    "endpoint, expected",
    [
        (None, None),
        ("", None),
        ("minio:9000", "minio"),
        ("http://MinIO.svc:9000", "minio.svc"),
        ("https://s3.example.com", "s3.example.com"),
    ],
)
def test_storage_host_parsing(endpoint: str | None, expected: str | None) -> None:
    """The storage host is the lowercase hostname of the configured endpoint, with or without scheme."""
    cfg = WorkerConfig(
        api_manager_url="http://a",
        worker_api_key="k" * 32,
        worker_id="w",
        dhcp_mode="disabled",
        dhcp_interface="eth0",
        storage_endpoint=endpoint,
    )

    assert _storage_host(cfg) == expected


def test_literal_private_ip_allowed_only_for_storage_host() -> None:
    """A literal private IP in the URL is refused unless it is the configured storage host."""
    url = "http://10.43.0.7:9000/b/o"

    assert _validate_presigned_url(url)[0] is False
    assert _validate_presigned_url(url, storage_host="10.43.0.7")[0] is True
    assert (
        _validate_presigned_url("http://127.0.0.1/b", storage_host="127.0.0.1")[0]
        is False
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://a.localhost/x",
        "http://LOCALHOST/x",
        "http:///x",
        "http://[::ffff:7f00:1]/x",
    ],
)
def test_more_blocked_urls(url: str) -> None:
    """localhost aliases, missing host and IPv4-mapped loopback are all refused."""
    assert _validate_presigned_url(url)[0] is False


@pytest.mark.asyncio
async def test_resolves_once_and_pins_connection_to_checked_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DNS answer is classified once and the client dials exactly that IP (no second lookup)."""
    calls: list[tuple[str, int]] = []

    async def resolver(host: str, port: int) -> list[str]:
        """Record the lookup and answer with a public IP."""
        calls.append((host, port))
        return ["93.184.216.34"]

    monkeypatch.setattr(http_mod, "_resolve_host", resolver)

    client = await _make_storage_client("https://store.example.com/b/o?sig=1", None)
    async with client:
        backend = client._transport._pool._network_backend  # type: ignore[attr-defined]
        assert isinstance(backend, _PinnedIPBackend) and backend._ip == "93.184.216.34"
        assert client.follow_redirects is False

    assert calls == [("store.example.com", 443)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        ["10.0.0.5"],
        ["127.0.0.1"],
        ["93.184.216.34", "169.254.169.254"],
        ["::ffff:127.0.0.1"],
        ["0.0.0.0"],
    ],
)
async def test_hostname_resolving_to_internal_ip_is_refused(
    monkeypatch: pytest.MonkeyPatch, answers: list[str]
) -> None:
    """A public-looking name that resolves (even partly) to an internal address is rejected (DNS rebinding/aliasing)."""

    async def resolver(host: str, port: int) -> list[str]:
        """Return the hostile answers."""
        return answers

    monkeypatch.setattr(http_mod, "_resolve_host", resolver)

    with pytest.raises(ValueError, match="presigned URL points to"):
        await _make_storage_client("http://rebind.example.com/b/o", None)


@pytest.mark.asyncio
async def test_storage_host_may_resolve_private_but_not_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-cluster storage on a private IP works; if it resolved to loopback it would still be refused."""
    answers = ["10.43.0.7"]

    async def resolver(host: str, port: int) -> list[str]:
        """Return the current scripted answer."""
        return answers

    monkeypatch.setattr(http_mod, "_resolve_host", resolver)

    client = await _make_storage_client("http://minio:9000/b/o", "minio")
    await client.aclose()
    answers[:] = ["127.0.0.1"]
    with pytest.raises(ValueError):
        await _make_storage_client("http://minio:9000/b/o", "minio")


@pytest.mark.asyncio
async def test_unresolvable_or_empty_answer_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver failure and an empty answer set both refuse, never fall through to an unchecked connect."""

    async def boom(host: str, port: int) -> list[str]:
        """Simulate NXDOMAIN."""
        raise socket.gaierror("nodename nor servname")

    monkeypatch.setattr(http_mod, "_resolve_host", boom)
    with pytest.raises(ValueError, match="cannot resolve"):
        await _make_storage_client("http://nx.example.com/o", None)

    async def empty(host: str, port: int) -> list[str]:
        """Return no addresses."""
        return []

    monkeypatch.setattr(http_mod, "_resolve_host", empty)
    with pytest.raises(ValueError, match="did not resolve"):
        await _make_storage_client("http://nx.example.com/o", None)


@pytest.mark.asyncio
async def test_literal_public_ip_skips_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """An IP-literal URL is classified directly with no DNS lookup."""

    async def never(host: str, port: int) -> list[str]:
        """Fail if DNS is consulted."""
        raise AssertionError("DNS must not be used for literals")

    monkeypatch.setattr(http_mod, "_resolve_host", never)

    client = await _make_storage_client("http://93.184.216.34/o", None)
    await client.aclose()


@pytest.mark.asyncio
async def test_real_resolver_uses_event_loop_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production resolver returns the IP strings from getaddrinfo."""

    async def fake_gai(host: str, port: int, **kw: object) -> list[tuple[object, ...]]:
        """Two A records."""
        return [
            (0, 0, 0, "", ("93.184.216.34", port)),
            (0, 0, 0, "", ("93.184.216.35", port)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_gai)

    assert await REAL_RESOLVE("x.example.com", 443) == [
        "93.184.216.34",
        "93.184.216.35",
    ]


@pytest.mark.asyncio
async def test_pinned_backend_dials_pinned_ip_not_requested_host() -> None:
    """The backend connects to the pinned IP even though httpcore asks for a different hostname."""
    hits: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Record bytes from the client."""
        hits.append(await reader.read(5))
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    backend = _PinnedIPBackend("127.0.0.1")
    stream = await backend.connect_tcp("attacker-controlled.example", port)
    await stream.write(b"hello")
    await asyncio.sleep(0.05)
    await stream.aclose()
    await backend.sleep(0)
    server.close()
    await server.wait_closed()

    assert hits == [b"hello"]


def _server() -> HTTPBootServer:
    """HTTP server configured with an in-cluster storage endpoint."""
    cfg = WorkerConfig(
        api_manager_url="http://api.test",
        worker_api_key="k" * 32,
        worker_id="w",
        dhcp_mode="disabled",
        dhcp_interface="eth0",
        redis_url="redis://t:6379/0",
        storage_endpoint="http://minio:9000",
    )
    enrollment = EnrollmentManager(cfg)
    enrollment.session_token = "s"
    return HTTPBootServer(cfg, enrollment)


@pytest.mark.asyncio
async def test_route_rejects_hostname_that_resolves_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: api-manager returns a name that resolves to 10.x; the image route answers 400 and never fetches."""

    async def resolver(host: str, port: int) -> list[str]:
        """Resolve to a private address."""
        return ["10.0.0.9"]

    monkeypatch.setattr(http_mod, "_resolve_host", resolver)
    with respx.mock(assert_all_called=False) as router:
        router.get("http://api.test/api/v1/internal/image-url/boot/x.iso").mock(
            return_value=httpx.Response(
                200, json={"url": "https://evil.example.com/b/x.iso"}
            )
        )
        storage = router.get(url__regex=r"https://evil\.example\.com.*").mock(
            return_value=httpx.Response(200)
        )
        resp = await _server().app.test_client().get("/images/boot/x.iso")

    assert resp.status_code == 400
    assert storage.call_count == 0
