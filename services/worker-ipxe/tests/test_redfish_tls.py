"""Redfish TLS enforcement: pinned leaf fingerprint, CA bundle, and refuse-otherwise, over a real TLS server."""

import asyncio
import hashlib
import ssl
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from certs import make_cert
from worker.services import power_manager as pm
from worker.services.power_manager import BMCCredentials, PowerManager

_BODY = b'{"PowerState": "On"}'
_RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
    + f"Content-Length: {len(_BODY)}\r\n\r\n".encode()
    + _BODY
)


class _Bmc:
    """A running test BMC: port, certificate and the request heads it has served."""

    def __init__(
        self, port: int, cert_pem: bytes, der: bytes, requests: list[bytes]
    ) -> None:
        """Record the listening port, certificate and request log."""
        self.port = port
        self.cert_pem = cert_pem
        self.fingerprint = hashlib.sha256(der).hexdigest().upper()
        self.requests = requests


@pytest_asyncio.fixture
async def bmc(tmp_path: Path) -> AsyncIterator[_Bmc]:
    """Serve a self-signed BMC certificate on 127.0.0.1 and record request bytes received."""
    cert_pem, key_pem, der = make_cert("localhost", san_ip="127.0.0.1")
    cert_file, key_file = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    requests: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Read one request head and reply."""
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(_RESPONSE)
            await writer.drain()
        except (asyncio.IncompleteReadError, ssl.SSLError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        yield _Bmc(port, cert_pem, der, requests)
    finally:
        server.close()
        await server.wait_closed()


def _creds(bmc: _Bmc, fingerprint: str | None = None) -> BMCCredentials:
    """Credentials aimed at the local test BMC."""
    return BMCCredentials(
        address=f"127.0.0.1:{bmc.port}",
        username="admin",
        password="pw-from-test",
        power_type="redfish",
        cert_fingerprint=fingerprint,
    )


@pytest.mark.asyncio
async def test_pinned_fingerprint_match_allows_request(
    bmc: _Bmc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matching leaf fingerprint lets the request (with credentials) through."""
    monkeypatch.delenv("REDFISH_CA_FILE", raising=False)

    ok, state = await PowerManager().power_control(
        _creds(bmc, bmc.fingerprint.lower()), "status"
    )

    assert (ok, state) == (True, "On")
    assert len(bmc.requests) == 1
    assert b"authorization: basic" in bmc.requests[0].lower()


@pytest.mark.asyncio
async def test_pinned_fingerprint_mismatch_sends_nothing(
    bmc: _Bmc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different certificate is refused after the handshake, before any HTTP bytes (credentials) are sent."""
    monkeypatch.delenv("REDFISH_CA_FILE", raising=False)

    ok, msg = await PowerManager().power_control(_creds(bmc, "AB" * 32), "status")

    assert ok is False
    assert "pinned fingerprint" in msg
    assert bmc.requests == []


@pytest.mark.asyncio
async def test_malformed_pin_is_refused(bmc: _Bmc) -> None:
    """A corrupt stored pin fails closed with a clear error and no connection."""
    ok, msg = await PowerManager().power_control(
        _creds(bmc, "not-a-fingerprint"), "status"
    )

    assert ok is False
    assert "Invalid stored fingerprint format" in msg
    assert bmc.requests == []


@pytest.mark.asyncio
async def test_ca_bundle_verifies_chain_and_hostname(
    bmc: _Bmc, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With REDFISH_CA_FILE trusting the BMC certificate, a normal verified request succeeds."""
    ca = tmp_path / "ca.pem"
    ca.write_bytes(bmc.cert_pem)
    monkeypatch.setenv("REDFISH_CA_FILE", str(ca))

    ok, state = await PowerManager().power_control(_creds(bmc), "status")

    assert (ok, state) == (True, "On")


@pytest.mark.asyncio
async def test_untrusted_cert_with_other_ca_is_rejected(
    bmc: _Bmc, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CA bundle that does not trust the BMC certificate fails verification; nothing is sent."""
    other = tmp_path / "other.pem"
    other.write_bytes(make_cert("someone-else")[0])
    monkeypatch.setenv("REDFISH_CA_FILE", str(other))

    ok, _ = await PowerManager().power_control(_creds(bmc), "status")

    assert ok is False
    assert bmc.requests == []


@pytest.mark.asyncio
async def test_neither_ca_nor_pin_refuses_with_clear_error(
    bmc: _Bmc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No CA bundle and no pin: refuse, no insecure fallback (BMC_ALLOW_INSECURE_TLS is ignored)."""
    monkeypatch.delenv("REDFISH_CA_FILE", raising=False)
    monkeypatch.setenv("BMC_ALLOW_INSECURE_TLS", "true")

    ok, msg = await PowerManager().power_control(_creds(bmc), "on")

    assert ok is False
    assert "REDFISH_CA_FILE" in msg and "cert_fingerprint" in msg
    assert bmc.requests == []


@pytest.mark.parametrize(
    "content, expect",
    [(None, "does not exist"), (b"garbage", "not a usable CA bundle")],
)
def test_bad_ca_file_is_refused(
    monkeypatch: pytest.MonkeyPatch, content: bytes | None, expect: str
) -> None:
    """A missing or unparseable REDFISH_CA_FILE raises a descriptive RuntimeError."""
    path = Path(tempfile.mkdtemp()) / "ca.pem"
    if content is not None:
        path.write_bytes(content)
    monkeypatch.setenv("REDFISH_CA_FILE", str(path))
    creds = BMCCredentials("h", "u", "p", "redfish")

    with pytest.raises(RuntimeError, match=expect):
        pm._build_redfish_client(creds, 5.0)


@pytest.mark.asyncio
async def test_pinned_stream_delegates_io_and_missing_cert_fails() -> None:
    """The pin wrapper passes read/write/close/info through and fails closed with no peer certificate."""

    class Inner:
        """Minimal network stream double."""

        closed = False

        async def read(self, n: int, timeout: float | None = None) -> bytes:
            """Return fixed bytes."""
            return b"r"

        async def write(self, b: bytes, timeout: float | None = None) -> None:
            """Accept writes."""

        async def aclose(self) -> None:
            """Record close."""
            self.closed = True

        async def start_tls(
            self,
            ctx: object,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> "Inner":
            """Return self as the TLS stream."""
            return self

        def get_extra_info(self, info: str) -> object:
            """No ssl_object available."""
            return None

    inner = Inner()
    stream = pm._PinnedStream(inner, "AB:" * 31 + "AB", "h")  # type: ignore[arg-type]
    assert await stream.read(1) == b"r"
    await stream.write(b"x")
    assert stream.get_extra_info("ssl_object") is None
    with pytest.raises(RuntimeError, match="does not match the pinned fingerprint"):
        await stream.start_tls(ssl.create_default_context())
    assert inner.closed is True
    await stream.aclose()

    backend = pm._PinnedBackend("AB:" * 31 + "AB", "h")
    await backend.sleep(0)
