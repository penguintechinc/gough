"""Behaviour tests for HTTPBootServer routes: status codes, content types, upstream auth and lifecycle."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager
from worker.services import http_server as http_mod
from worker.services.http_server import HTTPBootServer

API = "http://api.test"
BOOT_URL = "http://10.20.30.40:8080"
MAC = "aa:bb:cc:dd:ee:ff"
MAC_PATH = "aabbccddeeff"
CLIENT = {"client": ("10.1.1.1", 4000)}
FALLBACK_META = "instance-id: error\nlocal-hostname: unknown\n"
FALLBACK_USERDATA = "#cloud-config\n# Error fetching user-data\n"


def _server(monkeypatch: pytest.MonkeyPatch) -> HTTPBootServer:
    """Build the HTTP boot server with a session-enrolled enrollment manager."""
    monkeypatch.delenv("IMAGE_STREAM_CHUNK_SIZE", raising=False)
    cfg = WorkerConfig(
        api_manager_url=API,
        worker_api_key="http-key", redis_url="redis://test:6379/0",
        worker_id="w-http",
        dhcp_mode="disabled",
        dhcp_interface="eth0",
        http_port=8080,
        http_boot_url=BOOT_URL,
    )
    enrollment = EnrollmentManager(cfg)
    enrollment.session_token = "sess-http"
    return HTTPBootServer(cfg, enrollment)


@pytest.mark.asyncio
async def test_health_returns_json_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /health returns 200 JSON identifying the service as healthy."""
    server = _server(monkeypatch)

    resp = await server.app.test_client().get("/health")

    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    assert await resp.get_json() == {"status": "healthy", "service": "worker-ipxe"}


@pytest.mark.asyncio
async def test_ipxe_route_returns_plain_script_with_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /ipxe/<mac>.ipxe serves the generated script as text/plain and calls api-manager authenticated."""
    server = _server(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        route = router.get(f"{API}/api/v1/internal/boot-script/{MAC_PATH}").mock(
            return_value=httpx.Response(200, json={"script": "#!ipxe\nexit\n"})
        )
        resp = await server.app.test_client().get(f"/ipxe/{MAC}.ipxe", scope_base=CLIENT)
        body = await resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert body.startswith("#!ipxe\nset boot_token ")
    assert body.endswith("\nexit\n")
    assert route.calls.last.request.headers["X-Worker-API-Key"] == "http-key"


@pytest.mark.asyncio
async def test_ipxe_route_invalid_mac_returns_error_script_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed MAC still returns 200 with an error iPXE script, and never calls the API."""
    server = _server(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=r".*").mock(return_value=httpx.Response(200, json={}))
        resp = await server.app.test_client().get("/ipxe/not-a-mac.ipxe", scope_base=CLIENT)
        body = await resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Invalid MAC address" in body
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_ipxe_route_generation_failure_returns_500_script(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected failure in script generation becomes a 500 iPXE error script with the cause."""
    server = _server(monkeypatch)

    async def explode(mac: str) -> str:
        """Simulate a templating failure."""
        raise RuntimeError("template broke")

    server.ipxe_handler.generate_script = explode  # type: ignore[method-assign]

    resp = await server.app.test_client().get(f"/ipxe/{MAC}.ipxe", scope_base=CLIENT)
    body = await resp.get_data(as_text=True)

    assert resp.status_code == 500
    assert resp.mimetype == "text/plain"
    assert body == "#!ipxe\necho Error generating boot script\nshell\n"  # cause never reflected


@pytest.mark.asyncio
async def test_cloud_init_metadata_route_serves_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /cloud-init/<id>/meta-data relays api-manager metadata as text/yaml."""
    server = _server(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{API}/api/v1/internal/cloud-init/node-1/meta-data").mock(
            return_value=httpx.Response(200, text="instance-id: node-1\n")
        )
        resp = await server.app.test_client().get("/cloud-init/node-1/meta-data")
        body = await resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert resp.mimetype == "text/yaml"
    assert body == "instance-id: node-1\n"


@pytest.mark.asyncio
async def test_cloud_init_metadata_route_upstream_failure_is_fallback_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream failure degrades to the fallback document (200), which cloud-init can still parse."""
    server = _server(monkeypatch)
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/cloud-init/node-2/meta-data").mock(return_value=httpx.Response(502))
        resp = await server.app.test_client().get("/cloud-init/node-2/meta-data")
        body = await resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert body == FALLBACK_META


@pytest.mark.asyncio
async def test_cloud_init_metadata_route_exception_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception escaping the handler is surfaced as a 500 YAML comment with the message."""
    server = _server(monkeypatch)

    async def explode(machine_id: str) -> str:
        """Simulate an unexpected handler failure."""
        raise ValueError("meta exploded")

    server.ipxe_handler.get_cloud_init_metadata = explode  # type: ignore[method-assign]

    resp = await server.app.test_client().get("/cloud-init/node-3/meta-data")
    body = await resp.get_data(as_text=True)

    assert resp.status_code == 500
    assert resp.mimetype == "text/yaml"
    assert body == "# Error\n"  # exception text is never reflected


@pytest.mark.asyncio
async def test_cloud_init_userdata_route_serves_cloud_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /cloud-init/<id>/user-data relays user-data as text/cloud-config."""
    server = _server(monkeypatch)
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{API}/api/v1/internal/cloud-init/node-4/user-data").mock(
            return_value=httpx.Response(200, text="#cloud-config\npackages: [curl]\n")
        )
        resp = await server.app.test_client().get("/cloud-init/node-4/user-data")
        body = await resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert resp.mimetype == "text/cloud-config"
    assert body == "#cloud-config\npackages: [curl]\n"


@pytest.mark.asyncio
async def test_cloud_init_userdata_route_upstream_failure_is_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream 500 on user-data degrades to the fallback cloud-config (200)."""
    server = _server(monkeypatch)
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/cloud-init/node-5/user-data").mock(return_value=httpx.Response(500))
        resp = await server.app.test_client().get("/cloud-init/node-5/user-data")
        body = await resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert body == FALLBACK_USERDATA


@pytest.mark.asyncio
async def test_cloud_init_userdata_route_exception_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception from the user-data handler is a 500 cloud-config comment with the message."""
    server = _server(monkeypatch)

    async def explode(machine_id: str) -> str:
        """Simulate an unexpected handler failure."""
        raise KeyError("biome")

    server.ipxe_handler.get_cloud_init_userdata = explode  # type: ignore[method-assign]

    resp = await server.app.test_client().get("/cloud-init/node-6/user-data")
    body = await resp.get_data(as_text=True)

    assert resp.status_code == 500
    assert resp.mimetype == "text/cloud-config"
    assert body == "#cloud-config\n# Error\n"
    assert "biome" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, reason",
    [
        ("/images/%2e%2e/etc/passwd", "traversal"),
        ("/images/boot%252Fkernel", "URL-encoded traversal"),
        ("/images/boot/evil%20name.iso", "invalid characters"),
    ],
    ids=["dotdot", "double-encoded-slash", "space"],
)
async def test_image_route_rejects_invalid_paths_with_400(
    monkeypatch: pytest.MonkeyPatch, path: str, reason: str
) -> None:
    """Unsafe image paths are refused with 400 and never reach api-manager."""
    server = _server(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__regex=r".*").mock(return_value=httpx.Response(200, json={}))
        resp = await server.app.test_client().get(path)
        body = await resp.get_data(as_text=True)

    assert resp.status_code == 400
    assert body.startswith("Invalid path:")
    assert reason.lower() in body.lower()
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_image_route_unknown_image_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """api-manager reporting the image missing maps to a 404 for the booting machine."""
    server = _server(monkeypatch)
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/image-url/boot/nope.iso").mock(return_value=httpx.Response(404))
        resp = await server.app.test_client().get("/images/boot/nope.iso")
        body = await resp.get_data(as_text=True)

    assert resp.status_code == 404
    assert body == "Image not found"


@pytest.mark.asyncio
async def test_image_route_presigned_url_to_loopback_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A presigned URL pointing at loopback (SSRF) is refused with 400 and no storage request is made."""
    server = _server(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API}/api/v1/internal/image-url/boot/x.iso").mock(
            return_value=httpx.Response(200, json={"url": "http://127.0.0.2:9000/bucket/x.iso"})
        )
        storage = router.get(url__regex=r"http://127\.0\.0\.2.*").mock(return_value=httpx.Response(200))
        resp = await server.app.test_client().get("/images/boot/x.iso")
        body = await resp.get_data(as_text=True)

    assert resp.status_code == 400
    assert body == "Invalid presigned URL"
    assert storage.call_count == 0


@pytest.mark.asyncio
async def test_image_route_missing_url_field_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An api-manager 200 without a url field is rejected as an invalid presigned URL."""
    server = _server(monkeypatch)
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/image-url/boot/y.iso").mock(return_value=httpx.Response(200, json={}))
        resp = await server.app.test_client().get("/images/boot/y.iso")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_image_route_api_connect_error_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport failure talking to api-manager is a 500 with a generic body (no internals leaked)."""
    server = _server(monkeypatch)
    with respx.mock:
        respx.get(f"{API}/api/v1/internal/image-url/boot/z.iso").mock(side_effect=httpx.ConnectError("refused"))
        resp = await server.app.test_client().get("/images/boot/z.iso")
        body = await resp.get_data(as_text=True)

    assert resp.status_code == 500
    assert body == "Error serving image"


@pytest.mark.asyncio
async def test_start_serves_on_configured_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """start() hands the app to hypercorn bound on 0.0.0.0 at HTTP_PORT with logs to stdout."""
    server = _server(monkeypatch)
    seen: dict[str, Any] = {}

    async def fake_serve(app: object, config: Any) -> None:
        """Capture the bind configuration instead of listening."""
        seen["app"] = app
        seen["bind"] = list(config.bind)
        seen["accesslog"] = config.accesslog
        seen["errorlog"] = config.errorlog

    monkeypatch.setattr(http_mod, "serve", fake_serve)

    await server.start()

    assert seen["app"] is server.app
    assert seen["bind"] == ["0.0.0.0:8080"]
    assert seen["accesslog"] == "-" and seen["errorlog"] == "-"


@pytest.mark.asyncio
async def test_start_error_is_logged_and_reraised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A serve failure propagates to the caller so the worker can fail loudly."""
    server = _server(monkeypatch)

    async def bind_fail(app: object, config: Any) -> None:
        """Simulate the port being unavailable."""
        raise OSError("port 8080 in use")

    monkeypatch.setattr(http_mod, "serve", bind_fail)

    with pytest.raises(OSError, match="port 8080 in use"):
        await server.start()


@pytest.mark.asyncio
async def test_stop_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() completes without error (hypercorn is shut down by cancelling start())."""
    server = _server(monkeypatch)

    await server.stop()


def test_image_path_validator_accepts_allowed_charset() -> None:
    """Paths made only of [A-Za-z0-9._/-] are accepted, including dotted names."""
    assert http_mod._validate_image_path("boot/ubuntu-22.04/vmlinuz") == (True, "")


@pytest.mark.parametrize("candidate", ["a\n", "a b", "a?x=1", "a\\b"])
def test_image_path_validator_rejects_trailing_newline_and_others(candidate: str) -> None:
    """Trailing newline and other disallowed characters are rejected (regex must fullmatch)."""
    is_valid, _ = http_mod._validate_image_path(candidate)

    assert is_valid is False


def test_helper_upstream_error_mapping() -> None:
    """404 from storage stays 404; any other non-2xx becomes 502."""
    assert http_mod._upstream_error_response(404).status_code == 404
    assert http_mod._upstream_error_response(403).status_code == 502
    assert http_mod._upstream_error_response(500).status_code == 502


def test_helper_total_length_rules() -> None:
    """Content-Range total is read on 206; Content-Length on 200; encoded or unknown lengths give None."""
    def resp(status: int, headers: dict[str, str]) -> httpx.Response:
        """Build a bare upstream response."""
        return httpx.Response(status, headers=headers)

    check: Callable[[httpx.Response], int | None] = http_mod._total_length
    assert check(resp(206, {"content-range": "bytes 0-0/4096"})) == 4096
    assert check(resp(206, {"content-range": "bytes 0-0/*"})) is None
    assert check(resp(200, {"content-length": "77"})) == 77
    assert check(resp(200, {"content-length": "nope"})) is None
    assert check(resp(200, {"content-length": "5", "content-encoding": "gzip"})) is None


def test_helper_outbound_headers_include_length_only_when_known() -> None:
    """Content-Length is only set on the client response when the total is known."""
    assert http_mod._outbound_image_headers(10) == {
        "Cache-Control": "public, max-age=3600",
        "Content-Length": "10",
    }
    assert "Content-Length" not in http_mod._outbound_image_headers(None)


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://s3.amazonaws.com/bucket/key", True),
        ("http://8.8.8.8/x", True),
        ("ftp://s3.example.com/x", False),
        ("http://localhost:9000/x", False),
        ("http://foo.localhost/x", False),
        ("http://127.0.0.1/x", False),
        ("http://127.0.0.2/x", False),
        ("http://0.0.0.0/x", False),
        ("http://10.1.2.3/x", False),
        ("http://172.20.0.5/x", False),
        ("http://172.32.0.5/x", True),
        ("http://192.168.1.1/x", False),
        ("http://169.254.169.254/latest", False),
        ("http://[::1]/x", False),
        ("http://[::ffff:127.0.0.1]/x", False),
        ("http:///nohost", False),
    ],
)
def test_presigned_url_ssrf_classification(url: str, ok: bool) -> None:
    """Literal private, loopback, link-local and unspecified hosts are refused; public hosts pass."""
    is_valid, msg = http_mod._validate_presigned_url(url)

    assert is_valid is ok
    assert (msg == "") is ok


def test_presigned_url_rejects_non_string() -> None:
    """A missing (None) URL is refused rather than raising."""
    is_valid, msg = http_mod._validate_presigned_url(None)  # type: ignore[arg-type]

    assert is_valid is False
    assert "not a string" in msg
