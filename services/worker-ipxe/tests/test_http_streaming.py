"""Regression tests for streaming boot-image proxying in serve_image.

Covers bounded streaming (no full-body buffering), Content-Length passthrough,
upstream error status propagation, upstream close on client disconnect, and
split connect/read timeouts. Uses httpx.MockTransport, so no network is touched.
"""

import asyncio
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
    _LazyImageBody,
    _outbound_image_headers,
    _StorageStream,
    _total_length,
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
        if request.headers.get("range"):
            if storage_status != 200:
                return httpx.Response(storage_status)
            return httpx.Response(
                206,
                headers={
                    "content-range": extra_headers.get("content-range", "bytes 0-0/*")
                },
            )
        return httpx.Response(storage_status, stream=body, headers=extra_headers)

    return handler


def _patch_clients(
    monkeypatch: pytest.MonkeyPatch, handler, captured: list[dict]
) -> None:
    """Route every httpx.AsyncClient in http_server through the mock transport."""

    def factory(*args, **kwargs):
        captured.append(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(http_server.httpx, "AsyncClient", factory)


async def _asgi_start_headers(
    app: object, method: str, path: str
) -> dict[bytes, bytes]:
    """Drive the raw ASGI app and return response-start headers as sent to hypercorn.

    Quart's test client recomputes Content-Length from the body, so header assertions
    on Content-Length must go through the ASGI layer the server actually uses.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    messages: list[dict] = []
    finished = asyncio.Event()

    async def receive() -> dict:
        """Hold the request open until the response body is complete."""
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        """Record ASGI messages and signal once the body is complete."""
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            finished.set()

    await app.asgi_app(scope, receive, send)  # type: ignore[attr-defined]
    start = next(m for m in messages if m["type"] == "http.response.start")
    return dict(start["headers"])


def _make_server(
    monkeypatch: pytest.MonkeyPatch, chunk_size: int = 1024
) -> HTTPBootServer:
    """Create an HTTPBootServer with a mocked enrollment manager and a small chunk size."""
    monkeypatch.setenv("IMAGE_STREAM_CHUNK_SIZE", str(chunk_size))
    cfg = WorkerConfig(
        api_manager_url=f"http://{API_HOST}",
        worker_api_key="k",
        redis_url="redis://test:6379/0",
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


def _stream_for(upstream: CountingUpstream) -> _StorageStream:
    """Wrap a CountingUpstream in a real httpx client and a _StorageStream owner."""
    client = REAL_ASYNC_CLIENT(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    return _StorageStream(client=client, upstream=httpx.Response(200, stream=upstream))


def _spy_open(
    monkeypatch: pytest.MonkeyPatch, upstream: CountingUpstream
) -> list[_StorageStream]:
    """Replace the lazy GET opener with one that records each opened stream."""
    opened: list[_StorageStream] = []

    async def fake_open(
        presigned_url: str, storage_host: str | None = None
    ) -> _StorageStream:
        """Return a stream over the given upstream and record it."""
        stream = _stream_for(upstream)
        opened.append(stream)
        return stream

    monkeypatch.setattr(http_server, "_open_storage_stream", fake_open)
    return opened


@pytest.mark.asyncio
async def test_no_get_opened_before_body_iterated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unstarted body opens no storage GET, so it holds no connection.

    Regression for the residual Quart gap: a body entered and exited without iteration
    (client gone before send) must never have connected to storage.
    """
    upstream = CountingUpstream(total_chunks=10, chunk=b"q" * 8)
    opened = _spy_open(monkeypatch, upstream)

    body = _LazyImageBody(STORAGE_URL, chunk_size=8)
    async with body:
        pass

    assert opened == []
    assert body.stream is None


@pytest.mark.asyncio
async def test_lazy_body_opens_on_iteration_and_closes_on_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First iteration opens the GET; completion closes upstream and client."""
    upstream = CountingUpstream(total_chunks=4, chunk=b"w" * 8)
    opened = _spy_open(monkeypatch, upstream)
    body = _LazyImageBody(STORAGE_URL, chunk_size=8)

    total = b""
    async with body:
        async for chunk in body:
            total += chunk

    assert len(opened) == 1
    assert total == b"w" * 32
    assert upstream.closed is True
    assert opened[0].client.is_closed is True


@pytest.mark.asyncio
async def test_lazy_body_disconnect_mid_stream_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the chunk iterator early closes upstream and client."""
    upstream = CountingUpstream(total_chunks=50, chunk=b"y" * 16)
    opened = _spy_open(monkeypatch, upstream)
    body = _LazyImageBody(STORAGE_URL, chunk_size=16)

    chunks = body.__aiter__()
    assert await chunks.__anext__() == b"y" * 16
    await chunks.aclose()

    assert upstream.closed is True
    assert opened[0].client.is_closed is True


@pytest.mark.asyncio
async def test_lazy_body_non_2xx_get_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A storage GET failing after the 200 was committed aborts the body and closes."""
    upstream = CountingUpstream(total_chunks=3, chunk=b"e")

    async def fake_open(
        presigned_url: str, storage_host: str | None = None
    ) -> _StorageStream:
        """Return a stream whose GET status is 500."""
        client = REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        )
        return _StorageStream(
            client=client, upstream=httpx.Response(500, stream=upstream)
        )

    monkeypatch.setattr(http_server, "_open_storage_stream", fake_open)
    body = _LazyImageBody(STORAGE_URL, chunk_size=1)

    with pytest.raises(http_server.ImageStorageError):
        async for _ in body:
            pass

    assert upstream.yielded == 0
    assert upstream.closed is True


@pytest.mark.asyncio
async def test_storage_stream_close_is_idempotent() -> None:
    """Calling close() twice closes once and does not raise."""
    upstream = CountingUpstream(total_chunks=1, chunk=b"a")
    stream = _stream_for(upstream)

    await stream.close()
    await stream.close()

    assert stream.closed is True
    assert upstream.closed is True


@pytest.mark.asyncio
async def test_body_exit_then_iterator_close_is_double_close_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body __aexit__ followed by the iterator aclose must not double-close."""
    upstream = CountingUpstream(total_chunks=5, chunk=b"b" * 4)
    _spy_open(monkeypatch, upstream)
    body = _LazyImageBody(STORAGE_URL, chunk_size=4)

    chunks = body.__aiter__()
    await chunks.__anext__()
    await body.__aexit__(None, None, None)
    await chunks.aclose()

    assert upstream.closed is True


@pytest.mark.asyncio
async def test_response_construction_failure_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If building the streamed body fails, the request returns 500 and no GET is opened."""
    upstream = CountingUpstream(total_chunks=10, chunk=b"c" * 1024)
    captured: list[dict] = []
    _patch_clients(monkeypatch, _make_handler(200, upstream, {}), captured)
    server = _make_server(monkeypatch)
    opened = _spy_open(monkeypatch, upstream)

    def boom(*args: object, **kwargs: object) -> None:
        """Simulate a failure while constructing the streamed body."""
        raise RuntimeError("body construction failed")

    monkeypatch.setattr(http_server, "_LazyImageBody", boom)

    resp = await server.app.test_client().get("/images/boot/broken.iso")

    assert resp.status_code == 500
    assert opened == []


@pytest.mark.asyncio
async def test_head_uses_ranged_preflight_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Client HEAD answers from the ranged pre-flight; no full GET is opened."""
    upstream = CountingUpstream(total_chunks=1000, chunk=b"h" * 1024)
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record method and Range; answer the pre-flight with a 206 total."""
        if request.url.host == API_HOST:
            return httpx.Response(200, json={"url": STORAGE_URL})
        seen.append((request.method, request.headers.get("range")))
        return httpx.Response(206, headers={"content-range": "bytes 0-0/1024000"})

    _patch_clients(monkeypatch, handler, [])
    server = _make_server(monkeypatch)

    headers = await _asgi_start_headers(server.app, "HEAD", "/images/boot/ubuntu.iso")

    assert headers[b"content-length"] == b"1024000"
    assert seen == [("GET", "bytes=0-0")]
    assert upstream.yielded == 0


@pytest.mark.asyncio
async def test_preflight_200_ignoring_range_closes_without_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that ignores Range returns 200: the pre-flight reads headers, never the body."""
    upstream = CountingUpstream(total_chunks=3, chunk=b"r" * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        """Ignore Range and return the full object with a known length."""
        if request.url.host == API_HOST:
            return httpx.Response(200, json={"url": STORAGE_URL})
        return httpx.Response(200, stream=upstream, headers={"content-length": "3072"})

    _patch_clients(monkeypatch, handler, [])
    server = _make_server(monkeypatch)

    headers = await _asgi_start_headers(server.app, "HEAD", "/images/boot/full.iso")

    assert headers[b"content-length"] == b"3072"
    assert upstream.yielded == 0
    assert upstream.closed is True


@pytest.mark.asyncio
async def test_preflight_403_maps_to_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """Storage 403 on the ranged pre-flight surfaces as 502, never 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return 403 for the ranged pre-flight."""
        if request.url.host == API_HOST:
            return httpx.Response(200, json={"url": STORAGE_URL})
        return httpx.Response(403)

    _patch_clients(monkeypatch, handler, [])
    server = _make_server(monkeypatch)

    resp = await server.app.test_client().open("/images/boot/denied.iso", method="HEAD")

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_star_total_omits_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """Content-Range total '*' means unknown size, so Content-Length is not forwarded."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a 206 whose total size is unknown."""
        if request.url.host == API_HOST:
            return httpx.Response(200, json={"url": STORAGE_URL})
        return httpx.Response(206, headers={"content-range": "bytes 0-0/*"})

    _patch_clients(monkeypatch, handler, [])
    server = _make_server(monkeypatch)

    headers = await _asgi_start_headers(server.app, "HEAD", "/images/boot/star.iso")

    assert b"content-length" not in headers


@pytest.mark.asyncio
async def test_get_preflights_with_ranged_get_then_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET does one ranged pre-flight; the full GET happens only during body streaming."""
    upstream = CountingUpstream(total_chunks=3, chunk=b"g" * 1024)
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Answer the pre-flight with a 206 total and stream the unranged GET."""
        if request.url.host == API_HOST:
            return httpx.Response(200, json={"url": STORAGE_URL})
        seen.append((request.method, request.headers.get("range")))
        if request.headers.get("range"):
            return httpx.Response(206, headers={"content-range": "bytes 0-0/3072"})
        return httpx.Response(200, stream=upstream)

    _patch_clients(monkeypatch, handler, [])
    server = _make_server(monkeypatch, chunk_size=1024)

    resp = await server.app.test_client().get("/images/boot/ubuntu.iso")

    assert resp.status_code == 200
    assert seen == [("GET", "bytes=0-0"), ("GET", None)]
    assert len(await resp.get_data()) == 3072
    assert upstream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_status", "expected_status"),
    [(404, 404), (403, 502), (500, 502), (503, 502)],
)
async def test_upstream_error_status_propagates(
    monkeypatch: pytest.MonkeyPatch, upstream_status: int, expected_status: int
) -> None:
    """Non-2xx storage responses surface as error statuses, never 200 with an error body.

    404 maps to 404; other upstream failures map to 502.
    """
    _patch_clients(monkeypatch, _make_handler(upstream_status, None, {}), [])
    server = _make_server(monkeypatch)

    resp = await server.app.test_client().get("/images/boot/missing.iso")

    assert resp.status_code == expected_status


def test_total_length_parsing_rules() -> None:
    """Total size comes from Content-Range on 206, Content-Length on 200; unknown yields None."""
    ranged = httpx.Response(
        206, headers={"content-range": "bytes 0-0/3072", "content-length": "1"}
    )
    star = httpx.Response(206, headers={"content-range": "bytes 0-0/*"})
    plain = httpx.Response(200, headers={"content-length": "3072"})
    encoded = httpx.Response(
        200, headers={"content-length": "3072", "content-encoding": "gzip"}
    )
    absent = httpx.Response(200)

    assert _total_length(ranged) == 3072
    assert _total_length(star) is None
    assert _total_length(plain) == 3072
    assert _total_length(encoded) is None
    assert _total_length(absent) is None


def test_outbound_headers_omit_content_length_when_unknown() -> None:
    """Content-Length is emitted only when the total size is known."""
    assert _outbound_image_headers(3072)["Content-Length"] == "3072"
    assert "Content-Length" not in _outbound_image_headers(None)
    assert _outbound_image_headers(None)["Cache-Control"] == "public, max-age=3600"


@pytest.mark.asyncio
async def test_storage_client_receives_split_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The storage AsyncClient is constructed with connect=10s and read=300s."""
    captured: list[dict] = []
    upstream = CountingUpstream(total_chunks=1, chunk=b"a")
    _patch_clients(monkeypatch, _make_handler(200, upstream, {}), captured)
    server = _make_server(monkeypatch)

    resp = await server.app.test_client().get("/images/boot/small.iso")
    assert resp.status_code == 200

    timeouts = [
        kw["timeout"] for kw in captured if isinstance(kw.get("timeout"), httpx.Timeout)
    ]
    storage = [t for t in timeouts if t.read == STORAGE_READ_TIMEOUT_S]
    assert (
        len(storage) >= 1
    )  # HEAD pre-flight and lazy GET both use the storage timeout
    assert all(t.connect == STORAGE_CONNECT_TIMEOUT_S for t in storage)


def test_chunk_size_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """IMAGE_STREAM_CHUNK_SIZE defaults to 1 MiB and honours an env override."""
    monkeypatch.delenv("IMAGE_STREAM_CHUNK_SIZE", raising=False)
    cfg = WorkerConfig(
        api_manager_url="http://x",
        worker_api_key="k",
        redis_url="redis://test:6379/0",
        worker_id="w",
        dhcp_mode="disabled",
        dhcp_interface="eth0",
    )
    assert HTTPBootServer(cfg, Mock()).image_chunk_size == DEFAULT_IMAGE_CHUNK_SIZE

    monkeypatch.setenv("IMAGE_STREAM_CHUNK_SIZE", "4096")
    assert HTTPBootServer(cfg, Mock()).image_chunk_size == 4096


def test_chunk_size_rejects_non_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunk size below 1 fails loudly at construction time."""
    monkeypatch.setenv("IMAGE_STREAM_CHUNK_SIZE", "0")
    cfg = WorkerConfig(
        api_manager_url="http://x",
        worker_api_key="k",
        redis_url="redis://test:6379/0",
        worker_id="w",
        dhcp_mode="disabled",
        dhcp_interface="eth0",
    )
    with pytest.raises(ValueError, match="IMAGE_STREAM_CHUNK_SIZE"):
        HTTPBootServer(cfg, Mock())
