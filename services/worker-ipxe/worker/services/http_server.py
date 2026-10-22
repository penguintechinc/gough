"""
HTTP boot server for iPXE scripts, kernels, and cloud-init.

Provides dynamic iPXE script generation and serves boot images.
"""

import re
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlparse
import structlog
from decouple import config as env_config
from quart import Quart, request, Response, jsonify
from quart.wrappers.response import ResponseBody
from hypercorn.config import Config
from hypercorn.asyncio import serve
import httpx

from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager
from worker.services.ipxe_handler import IPXEHandler

logger = structlog.get_logger()

# Storage (MinIO/S3) timeouts: fast connect failure, long read for multi-GB images.
STORAGE_CONNECT_TIMEOUT_S = 10.0
STORAGE_READ_TIMEOUT_S = 300.0
DEFAULT_IMAGE_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def _load_image_chunk_size() -> int:
    """Read IMAGE_STREAM_CHUNK_SIZE (bytes) from env; fail loudly if below 1."""
    size = env_config("IMAGE_STREAM_CHUNK_SIZE", default=DEFAULT_IMAGE_CHUNK_SIZE, cast=int)
    if size < 1:
        raise ValueError(f"IMAGE_STREAM_CHUNK_SIZE must be >= 1, got {size}")
    return size


def _storage_timeout() -> httpx.Timeout:
    """Return the storage timeout: short connect, long read/write for large images."""
    return httpx.Timeout(STORAGE_READ_TIMEOUT_S, connect=STORAGE_CONNECT_TIMEOUT_S)


@dataclass(slots=True)
class _StorageStream:
    """Owns an open streamed storage GET; close() is idempotent and safe to call repeatedly."""

    client: httpx.AsyncClient
    upstream: httpx.Response
    closed: bool = False

    async def close(self) -> None:
        """Close upstream then client exactly once; later calls are no-ops."""
        if self.closed:
            return
        self.closed = True
        await self.upstream.aclose()
        await self.client.aclose()


async def _open_storage_stream(presigned_url: str) -> _StorageStream:
    """Open a streamed GET to storage without reading the body.

    Caller must close the returned stream on every path.
    """
    client = httpx.AsyncClient(timeout=_storage_timeout())
    try:
        upstream = await client.send(client.build_request("GET", presigned_url), stream=True)
    except BaseException:
        await client.aclose()
        raise
    return _StorageStream(client=client, upstream=upstream)


_CONTENT_RANGE_TOTAL = re.compile(r"^bytes 0-0/(\d+)$")


def _total_length(upstream: httpx.Response) -> int | None:
    """Return the full object size from a ranged (206) or plain (200) storage response.

    206: total from Content-Range (None when the total is '*' or missing).
    200: Content-Length, meaning the server ignored Range and sent the whole object.
    Returns None for content-encoded bodies, since decoded length differs from the wire length.
    """
    if "content-encoding" in upstream.headers:
        return None
    if upstream.status_code == 206:
        match = _CONTENT_RANGE_TOTAL.match(upstream.headers.get("content-range", ""))
        return int(match.group(1)) if match else None
    length = upstream.headers.get("content-length", "")
    return int(length) if length.isdigit() else None


def _outbound_image_headers(total_length: int | None) -> dict[str, str]:
    """Build client-facing headers; Content-Length is set only when the total is known."""
    headers = {"Cache-Control": "public, max-age=3600"}
    if total_length is not None:
        headers["Content-Length"] = str(total_length)
    return headers


def _upstream_error_response(upstream_status: int) -> Response:
    """Map a non-2xx storage status to a client error: 404 stays 404, else 502."""
    if upstream_status == 404:
        return Response("Image not found", status=404)
    return Response("Upstream storage error", status=502)


class _ImageHeadResponse(Response):
    """Empty-body HEAD response that keeps the storage-reported Content-Length.

    Quart otherwise overwrites Content-Length with the empty body's length (0).
    """

    automatically_set_content_length = False


@dataclass(slots=True, frozen=True)
class _ImagePreflight:
    """Result of the ranged storage pre-flight: total size when known."""

    total_length: int | None


class ImageStorageError(Exception):
    """Raised inside a streamed body when storage fails after the 200 was committed."""


class _LazyImageBody(ResponseBody):
    """Quart body that opens the storage GET only on first iteration.

    An unstarted body holds no connection: __aiter__ opens the stream inside the same
    try/finally that closes it, so open and close cannot diverge. __aexit__ closes any
    stream as belt-and-braces; close is idempotent.
    """

    def __init__(self, presigned_url: str, chunk_size: int) -> None:
        """Store the presigned URL and chunk size; acquires no resources."""
        self.presigned_url = presigned_url
        self.chunk_size = chunk_size
        self.stream: _StorageStream | None = None

    async def __aenter__(self) -> "_LazyImageBody":
        """Enter the body context; no resources are acquired here."""
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, tb: object) -> None:
        """Release any opened storage stream on every exit path."""
        if self.stream is not None:
            await self.stream.close()

    def __aiter__(self) -> AsyncIterator[bytes]:
        """Return the chunk generator, which performs the lazy GET."""
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[bytes]:
        """Open the storage GET, yield bounded chunks, and close in one try/finally."""
        try:
            self.stream = await _open_storage_stream(self.presigned_url)
            if not self.stream.upstream.is_success:
                # 200 already committed by the HEAD preflight: abort instead of sending an empty body
                logger.error("image_storage_get_failed", upstream_status=self.stream.upstream.status_code)
                raise ImageStorageError(f"storage GET returned {self.stream.upstream.status_code}")
            async for chunk in self.stream.upstream.aiter_bytes(self.chunk_size):
                yield chunk
        finally:
            if self.stream is not None:
                await self.stream.close()


def _validate_image_path(image_path: str) -> tuple[bool, str]:
    """Validate image_path to prevent path traversal and SSRF attacks.

    Returns (is_valid: bool, error_message: str).
    """
    # Check for null bytes
    if '\x00' in image_path:
        return False, "path contains null bytes"

    # Reject leading slash (absolute path)
    if image_path.startswith('/'):
        return False, "path must not start with /"

    # Reject backslashes (Windows-style)
    if '\\' in image_path:
        return False, "path contains backslashes"

    # Check for .. in original form
    if '..' in image_path:
        return False, "path contains .. traversal"

    # Check for URL-encoded traversal attempts (%2e%2e = .., %2f = /)
    if '%2e%2e' in image_path.lower() or '%2f' in image_path.lower():
        return False, "path contains URL-encoded traversal"

    # Strict allowlist: only [A-Za-z0-9._/-]
    if not re.match(r'^[A-Za-z0-9._/-]+$', image_path):
        return False, "path contains invalid characters"

    return True, ""


def _validate_presigned_url(presigned_url: str) -> tuple[bool, str]:
    """Validate presigned URL to mitigate SSRF attacks.

    Returns (is_valid: bool, error_message: str).
    """
    try:
        parsed = urlparse(presigned_url)
    except Exception as e:
        return False, f"invalid URL format: {e}"

    # Enforce HTTPS
    if parsed.scheme not in ('https', 'http'):
        return False, f"URL scheme must be http/https, got {parsed.scheme}"

    # Reject localhost/127.0.0.1 and private IP ranges (basic SSRF prevention)
    if parsed.hostname:
        hostname = parsed.hostname.lower()
        if hostname in ('localhost', '127.0.0.1', '::1') or hostname.startswith('[::1]'):
            return False, "presigned URL points to localhost"
        # Reject private IP ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16)
        if hostname.startswith(('10.', '172.16.', '172.17.', '172.18.', '172.19.',
                               '172.20.', '172.21.', '172.22.', '172.23.', '172.24.',
                               '172.25.', '172.26.', '172.27.', '172.28.', '172.29.',
                               '172.30.', '172.31.', '192.168.', '169.254.')):
            return False, "presigned URL points to private IP range"

    return True, ""


class HTTPBootServer:
    """HTTP server for boot files and iPXE scripts."""

    def __init__(self, config: WorkerConfig, enrollment: EnrollmentManager):
        self.config = config
        self.enrollment = enrollment
        self.app = Quart(__name__)
        self.ipxe_handler = IPXEHandler(config, enrollment)
        self.image_chunk_size = _load_image_chunk_size()
        self.server_task = None

        # Register routes
        self._register_routes()

    def _register_routes(self):
        """Register HTTP routes."""

        @self.app.route("/health")
        async def health():
            """Health check endpoint."""
            return jsonify({"status": "healthy", "service": "worker-ipxe"})

        @self.app.route("/ipxe/<mac>.ipxe")
        async def ipxe_script(mac: str):
            """
            Generate iPXE script for machine by MAC address.

            Queries api-manager for machine state and returns appropriate script.
            """
            logger.info("ipxe_script_request", mac=mac, client_ip=request.remote_addr)

            try:
                script = await self.ipxe_handler.generate_script(mac)
                return Response(script, mimetype="text/plain")
            except Exception as e:
                logger.error("ipxe_script_generation_error", mac=mac, error=str(e))
                return Response(
                    f"#!ipxe\necho Error generating boot script: {e}\nshell\n",
                    mimetype="text/plain",
                    status=500,
                )

        @self.app.route("/cloud-init/<machine_id>/meta-data")
        async def cloud_init_metadata(machine_id: str):
            """Cloud-init metadata endpoint."""
            logger.info("cloud_init_metadata_request", machine_id=machine_id)

            try:
                metadata = await self.ipxe_handler.get_cloud_init_metadata(machine_id)
                return Response(metadata, mimetype="text/yaml")
            except Exception as e:
                logger.error("cloud_init_metadata_error", machine_id=machine_id, error=str(e))
                return Response(f"# Error: {e}\n", mimetype="text/yaml", status=500)

        @self.app.route("/cloud-init/<machine_id>/user-data")
        async def cloud_init_userdata(machine_id: str):
            """Cloud-init user-data endpoint."""
            logger.info("cloud_init_userdata_request", machine_id=machine_id)

            try:
                userdata = await self.ipxe_handler.get_cloud_init_userdata(machine_id)
                return Response(userdata, mimetype="text/cloud-config")
            except Exception as e:
                logger.error("cloud_init_userdata_error", machine_id=machine_id, error=str(e))
                return Response(f"#cloud-config\n# Error: {e}\n", mimetype="text/cloud-config", status=500)

        @self.app.route("/images/<path:image_path>")
        async def serve_image(image_path: str):
            """
            Proxy boot images from storage (MinIO/S3).

            Validates image_path for traversal attacks and presigned URL for SSRF.
            Avoids exposing storage credentials to booting machines.
            """
            logger.info("image_request", path=image_path)

            # Validate image_path against path traversal attacks
            is_valid, error_msg = _validate_image_path(image_path)
            if not is_valid:
                logger.warning("image_path_validation_failed", path=image_path, reason=error_msg)
                return Response(f"Invalid path: {error_msg}", status=400)

            try:
                # Request presigned URL from api-manager
                api_url = f"{self.config.api_manager_url}/api/v1/internal/image-url/{image_path}"
                headers = self.enrollment.get_auth_headers()

                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(api_url, headers=headers)

                    if response.status_code == 200:
                        data = response.json()
                        presigned_url = data.get("url")

                        # Validate presigned URL to prevent SSRF
                        is_valid_url, url_error = _validate_presigned_url(presigned_url)
                        if not is_valid_url:
                            logger.warning(
                                "presigned_url_validation_failed",
                                path=image_path,
                                reason=url_error,
                            )
                            return Response("Invalid presigned URL", status=400)

                        preflight = await self._preflight_image(presigned_url, image_path)
                        if isinstance(preflight, Response):
                            return preflight
                        out_headers = _outbound_image_headers(preflight.total_length)
                        if request.method == "HEAD":
                            out_headers["Content-Type"] = "application/octet-stream"
                            return _ImageHeadResponse(b"", headers=out_headers)
                        return Response(
                            _LazyImageBody(presigned_url, self.image_chunk_size),
                            mimetype="application/octet-stream",
                            headers=out_headers,
                        )
                    else:
                        logger.error("image_url_fetch_failed", status=response.status_code)
                        return Response("Image not found", status=404)

            except Exception as e:
                logger.error("image_serve_error", path=image_path, error=str(e))
                return Response("Error serving image", status=500)

        @self.app.route("/boot-event", methods=["POST"])
        async def boot_event():
            """
            Receive boot event callbacks from machines.

            Called by cloud-init during provisioning to report progress.
            """
            data = await request.get_json()
            logger.info("boot_event_received", event=data)

            try:
                # Forward event to api-manager
                api_url = f"{self.config.api_manager_url}/api/v1/internal/boot-event"
                headers = self.enrollment.get_auth_headers()
                headers["Content-Type"] = "application/json"

                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(api_url, json=data, headers=headers)

                return jsonify({"status": "received"})
            except Exception as e:
                logger.error("boot_event_forward_error", error=str(e))
                return jsonify({"status": "error", "message": str(e)}), 500

    async def _preflight_image(self, presigned_url: str, image_path: str) -> "_ImagePreflight | Response":
        """Ranged GET pre-flight (bytes=0-0): returns the total size or an error Response.

        Same method as the presigned URL (SigV4 binds the method, so HEAD would 403).
        The pre-flight response is always closed without reading the body.
        """
        async with httpx.AsyncClient(timeout=_storage_timeout()) as client:
            request = client.build_request("GET", presigned_url, headers={"Range": "bytes=0-0"})
            upstream = await client.send(request, stream=True)
            try:
                if not upstream.is_success:
                    logger.error(
                        "image_storage_preflight_failed",
                        path=image_path,
                        upstream_status=upstream.status_code,
                    )
                    return _upstream_error_response(upstream.status_code)
                return _ImagePreflight(total_length=_total_length(upstream))
            finally:
                await upstream.aclose()

    async def start(self):
        """Start HTTP server."""
        config = Config()
        config.bind = [f"0.0.0.0:{self.config.http_port}"]
        config.accesslog = "-"
        config.errorlog = "-"

        logger.info("http_server_starting", port=self.config.http_port)

        try:
            await serve(self.app, config)
        except Exception as e:
            logger.error("http_server_error", error=str(e))
            raise

    async def stop(self):
        """Stop HTTP server."""
        logger.info("http_server_stopped")
