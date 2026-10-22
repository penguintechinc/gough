"""Regression tests for streaming boot-image proxying in serve_image.

Covers bounded streaming (no full-body buffering), Content-Length passthrough,
upstream error status propagation, upstream close on client disconnect, and
split connect/read timeouts. Uses httpx.MockTransport, so no network is touched.
"""

from unittest.mock import Mock

import httpx
import pytest

from worker.config import WorkerConfig
from worker.services import http_server
from worker.services.http_server import (
    DEFAULT_IMAGE_CHUNK_SIZE,
    STORAGE_CONNECT_TIMEOUT_S,
    STORAGE_READ_TIMEOUT_S,
    HTTPBootServer,
    _iter_image_chunks,
    _outbound_image_headers,
)

REAL_ASYNC_CLIENT = httpx.AsyncClient
API_HOST = "api.test"
STORAGE_URL = "https://storage.example.com/boot/ubuntu.iso"


class CountingUpstream(httpx.AsyncByteStream):
    """Lazy upstream body that records how many chunks were pulled and whether it was closed."""

    def __init__(self, total_chunks: int, chunk: bytes) -> None:
        self.total_chunks = total_chunks
        self.chunk = chunk
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):  # type: ignore[override]
        """Yield chunks one at a time, counting each pull."""
        for _ in range(self.total_chunks):
            self.yielded += 1
            yield self.chunk

    async def aclose(self) -> None:
        """Record that the upstream stream was closed."""
        self.closed = True


def _make_handler(
    storage_status: int, body: CountingUpstream | None, extra_headers: dict[str, str]
):
    """Build a MockTransport handler serving the API presign call and the storage GET."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == API_HOST:
            return httpx.Response(200, json={"url": STORAGE_URL})
        if body is None:
            return httpx.Response(storage_status)
        return httpx.Response(storage_status, stream=body, headers=extra_headers)

    return handler


def _patch_clients(monkeypatch: pytest.MonkeyPatch, handler, captured: list[dict]) -> None:
    """Route every httpx.AsyncClient in http_server through the mock transport."""

    def factory(*args, **kwargs):
        captured.append(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(http_server.httpx, "AsyncClient", factory)


def _make_server(monkeypatch: pytest.MonkeyPatch, chunk_size: int = 1024) -> HTTPBootServer:
    """Create an HTTPBootServer with a mocked enrollment manager and a small chunk size."""
    monkeypatch.setenv("IMAGE_STREAM_CHUNK_SIZE", str(chunk_size))
    cfg = WorkerConfig(
        api_manager_url=f"http://{API_HOST}",
        worker_api_key="k",
        worker_id="w",
        dhcp_mode="disabled",
        dhcp_interface="eth0",
    )
    enrollment = Mock()
    enrollment.get_auth_headers.return_value = {}
    return HTTPBootServer(cfg, enrollment)


@pytest.mark.asyncio
async def test_image_route_streams_full_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route returns 200 and the full 1000-chunk body via a streamed response.

    Regression for OOM: the old response.content path raises ResponseNotRead on a
    streamed upstream, so a reintroduced buffering read fails this 200 assertion.
    """
    upstream = CountingUpstream(total_chunks=1000, chunk=b"x" * 1024)
    captured: list[dict] = []
    _patch_clients(monkeypatch, _make_handler(200, upstream, {}), captured)
    server = _make_server(monkeypatch, chunk_size=1024)

    resp = await server.app.test_client().get("/images/boot/ubuntu.iso")

    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=3600"
    body = await resp.get_data()
    assert len(body) == 1000 * 1024
    assert upstream.closed is True


@pytest.mark.asyncio
async def test_iter_image_chunks_is_lazy_and_bounded() -> None:
    """Chunks are pulled from upstream on demand, never the whole body up front."""
    upstream = CountingUpstream(total_chunks=1000, chunk=b"x" * 1024)
    client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    response = httpx.Response(200, stream=upstream)
    gen = _iter_image_chunks(client, response, chunk_size=1024)

    first = await gen.__anext__()
    assert len(first) == 1024
    assert upstream.yielded <= 2
    await gen.aclose()


@pytest.mark.asyncio
async def test_iter_image_chunks_closes_upstream_on_disconnect() -> None:
    """Closing the body generator early (client disconnect) closes upstream and client."""
    upstream = CountingUpstream(total_chunks=50, chunk=b"y" * 16)
    client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    response = httpx.Response(200, stream=upstream)
    gen = _iter_image_chunks(client, response, chunk_size=16)

    first = await gen.__anext__()
    assert first == b"y" * 16

    await gen.aclose()  # simulates hypercorn closing the body on client disconnect

    assert upstream.closed is True
    assert client.is_closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_status", "expected_status"),
    [(404, 404), (403, 502), (500, 502), (503, 502)],
)
async def test_upstream_error_status_propagates(
    monkeypatch: pytest.MonkeyPatch, upstream_status: int, expected_status: int
) -> None:
    """Non-2xx storage responses surface as error statuses, never 200 with an error body.

    Regression: the old code returned 200 with whatever body storage sent.
    404 maps to 404; other upstream failures map to 502.
    """
    captured: list[dict] = []
    _patch_clients(monkeypatch, _make_handler(upstream_status, None, {}), captured)
    server = _make_server(monkeypatch)

    resp = await server.app.test_client().get("/images/boot/missing.iso")

    assert resp.status_code == expected_status
    assert resp.status_code != 200


def test_outbound_content_length_passthrough_rules() -> None:
    """Content-Length is forwarded for identity bodies and dropped for content-encoded ones."""
    plain = httpx.Response(200, headers={"content-length": "123"})
    encoded = httpx.Response(200, headers={"content-length": "123", "content-encoding": "gzip"})
    absent = httpx.Response(200)

    assert _outbound_image_headers(plain)["Content-Length"] == "123"
    assert "Content-Length" not in _outbound_image_headers(encoded)
    assert "Content-Length" not in _outbound_image_headers(absent)
    assert _outbound_image_headers(absent)["Cache-Control"] == "public, max-age=3600"


@pytest.mark.asyncio
async def test_storage_client_receives_split_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The storage AsyncClient is constructed with connect=10s and read=300s."""
    captured: list[dict] = []
    upstream = CountingUpstream(total_chunks=1, chunk=b"a")
    _patch_clients(monkeypatch, _make_handler(200, upstream, {}), captured)
    server = _make_server(monkeypatch)

    resp = await server.app.test_client().get("/images/boot/small.iso")
    assert resp.status_code == 200

    timeouts = [kw["timeout"] for kw in captured if isinstance(kw.get("timeout"), httpx.Timeout)]
    storage = [t for t in timeouts if t.read == STORAGE_READ_TIMEOUT_S]
    assert len(storage) == 1
    assert storage[0].connect == STORAGE_CONNECT_TIMEOUT_S


def test_chunk_size_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """IMAGE_STREAM_CHUNK_SIZE defaults to 1 MiB and honours an env override."""
    monkeypatch.delenv("IMAGE_STREAM_CHUNK_SIZE", raising=False)
    cfg = WorkerConfig(
        api_manager_url="http://x", worker_api_key="k", worker_id="w",
        dhcp_mode="disabled", dhcp_interface="eth0",
    )
    assert HTTPBootServer(cfg, Mock()).image_chunk_size == DEFAULT_IMAGE_CHUNK_SIZE

    monkeypatch.setenv("IMAGE_STREAM_CHUNK_SIZE", "4096")
    assert HTTPBootServer(cfg, Mock()).image_chunk_size == 4096


def test_chunk_size_rejects_non_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunk size below 1 fails loudly at construction time."""
    monkeypatch.setenv("IMAGE_STREAM_CHUNK_SIZE", "0")
    cfg = WorkerConfig(
        api_manager_url="http://x", worker_api_key="k", worker_id="w",
        dhcp_mode="disabled", dhcp_interface="eth0",
    )
    with pytest.raises(ValueError, match="IMAGE_STREAM_CHUNK_SIZE"):
        HTTPBootServer(cfg, Mock())
