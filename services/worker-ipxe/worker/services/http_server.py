"""
HTTP boot server for iPXE scripts, kernels, and cloud-init.

Provides dynamic iPXE script generation and serves boot images.
"""

import asyncio
import ipaddress
import re
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlparse
import structlog
from decouple import config as env_config
from quart import Quart, request, Response, jsonify
from quart.wrappers.response import ResponseBody
from hypercorn.config import Config
from hypercorn.asyncio import serve
import httpcore
import httpx

from worker.boot_tokens import BootTokenStore, StoreUnavailableError, create_token_store
from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager
from worker.services.ipxe_handler import _MACHINE_ID_RE, IPXEHandler, _normalize_mac

logger = structlog.get_logger()

# Storage (MinIO/S3) timeouts: fast connect failure, long read for multi-GB images.
STORAGE_CONNECT_TIMEOUT_S = 10.0
STORAGE_READ_TIMEOUT_S = 300.0
DEFAULT_IMAGE_CHUNK_SIZE = 1024 * 1024  # 1 MiB

# /boot-event hardening: body cap, per-source-IP rate limit, strict field allowlist.
BOOT_EVENT_MAX_BYTES = 4096
BOOT_EVENT_RATE_LIMIT = 30
BOOT_EVENT_RATE_WINDOW_S = 60
BOOT_EVENT_MAC_RATE_LIMIT = 10
IPXE_RATE_LIMIT = 60
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43,128}")
BOOT_EVENT_STAGES = frozenset(
    {"discovery", "boot", "installing", "cloud_init", "complete", "failed"}
)
_BOOT_EVENT_FIELDS = frozenset({"mac", "machine_id", "stage", "timestamp", "message"})
_TIMESTAMP_RE = re.compile(r"[0-9TZ:.+-]{1,32}")
_MESSAGE_MAX = 256


def _resolve_client_ip(
    peer: str | None, forwarded_for: str | None, trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
) -> str | None:
    """Return the client IP, or None when it cannot be determined (callers reject).

    X-Forwarded-For is honoured only when the immediate peer is in a trusted proxy CIDR;
    the chain is walked right to left and the first untrusted hop is the client, so
    entries a client prepends cannot spoof its address.
    """
    def parse(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        """Parse an address, tolerating whitespace; None if malformed."""
        try:
            return ipaddress.ip_address(text.strip())
        except ValueError:
            return None

    peer_ip = parse(peer) if peer else None
    if peer_ip is None:
        return None
    if not any(peer_ip in net for net in trusted) or not forwarded_for:
        return str(peer_ip)
    for hop in reversed(forwarded_for.split(",")):
        ip = parse(hop)
        if ip is None:
            return None  # malformed chain from a trusted proxy: do not guess
        if not any(ip in net for net in trusted):
            return str(ip)
    return None


@dataclass(slots=True, frozen=True)
class _BootEvent:
    """A validated boot-event: the only shape ever forwarded to api-manager."""

    mac: str
    stage: str
    machine_id: str | None = None
    timestamp: str | None = None
    message: str | None = None

    def as_payload(self) -> dict[str, str]:
        """Return only the populated, validated fields for forwarding."""
        return {
            k: v
            for k, v in (
                ("mac", self.mac),
                ("stage", self.stage),
                ("machine_id", self.machine_id),
                ("timestamp", self.timestamp),
                ("message", self.message),
            )
            if v is not None
        }


def _parse_boot_event(data: object) -> tuple[_BootEvent | None, str]:
    """Validate a /boot-event body against the strict schema; returns (event, error)."""
    if not isinstance(data, dict):
        return None, "JSON object body required"
    unknown = set(data) - _BOOT_EVENT_FIELDS
    if unknown:
        return None, "unknown fields not allowed"
    if not all(isinstance(v, str) for v in data.values()):
        return None, "all fields must be strings"
    mac = _normalize_mac(data.get("mac", ""))
    if mac is None:
        return None, "invalid mac"
    stage = data.get("stage", "")
    if stage not in BOOT_EVENT_STAGES:
        return None, "invalid stage"
    machine_id = data.get("machine_id")
    if machine_id is not None and not _MACHINE_ID_RE.fullmatch(machine_id):
        return None, "invalid machine_id"
    timestamp = data.get("timestamp")
    if timestamp is not None and not _TIMESTAMP_RE.fullmatch(timestamp):
        return None, "invalid timestamp"
    message = data.get("message")
    if message is not None and (len(message) > _MESSAGE_MAX or not message.isprintable()):
        return None, "invalid message"
    return _BootEvent(mac, stage, machine_id, timestamp, message), ""


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


async def _open_storage_stream(presigned_url: str, storage_host: str | None = None) -> _StorageStream:
    """Open a streamed GET to storage without reading the body.

    The host is resolved and classified once and the connection goes to that checked IP.
    Caller must close the returned stream on every path.
    """
    client = await _make_storage_client(presigned_url, storage_host)
    try:
        upstream = await client.send(client.build_request("GET", presigned_url), stream=True)
    except BaseException:
        await client.aclose()
        raise
    return _StorageStream(client=client, upstream=upstream)


_CONTENT_RANGE_TOTAL = re.compile(r"^bytes 0-0/(\d+)$")
_IMAGE_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+")


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

    def __init__(self, presigned_url: str, chunk_size: int, storage_host: str | None = None) -> None:
        """Store the presigned URL, chunk size and storage host; acquires no resources."""
        self.presigned_url = presigned_url
        self.chunk_size = chunk_size
        self.storage_host = storage_host
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
            self.stream = await _open_storage_stream(self.presigned_url, self.storage_host)
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

    # Strict allowlist: only [A-Za-z0-9._/-] (fullmatch: `$` would accept a trailing newline)
    if not _IMAGE_PATH_RE.fullmatch(image_path):
        return False, "path contains invalid characters"

    return True, ""


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_private: bool) -> str | None:
    """Return a reason if `ip` must not be fetched from, else None.

    Loopback, unspecified, link-local (cloud metadata), multicast and reserved ranges are
    always blocked, IPv4-mapped IPv6 included. Private ranges are blocked unless the host
    is the operator-configured storage endpoint (in-cluster MinIO lives on a private IP).
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_unspecified:
        return "presigned URL points to localhost"
    if ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return "presigned URL points to private IP range"
    if ip.is_private and not allow_private:
        return "presigned URL points to private IP range"
    return None


def _storage_host(config: WorkerConfig) -> str | None:
    """Hostname of the configured storage endpoint (the only host allowed to be private)."""
    endpoint = config.storage_endpoint
    if not endpoint:
        return None
    parsed = urlparse(endpoint if "//" in endpoint else f"//{endpoint}")
    return (parsed.hostname or "").lower() or None


def _validate_presigned_url(presigned_url: str, storage_host: str | None = None) -> tuple[bool, str]:
    """Validate presigned URL to mitigate SSRF attacks (syntax and literal-IP checks).

    DNS names are resolved and classified at connect time by `_make_storage_client`,
    which connects to the exact IP it checked. Returns (is_valid, error_message).
    """
    if not isinstance(presigned_url, str):
        return False, "presigned URL is missing or not a string"
    try:
        parsed = urlparse(presigned_url)
        hostname = (parsed.hostname or "").lower()
    except ValueError as e:
        return False, f"invalid URL format: {e}"

    if parsed.scheme not in ('https', 'http'):
        return False, f"URL scheme must be http/https, got {parsed.scheme}"

    if not hostname:
        return False, "presigned URL has no host"
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False, "presigned URL points to localhost"

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return True, ""  # DNS name: classified after resolution

    reason = _ip_blocked(ip, allow_private=hostname == storage_host)
    return (False, reason) if reason else (True, "")


async def _resolve_host(host: str, port: int) -> list[str]:
    """Resolve `host` to IP strings via the event loop's resolver (patchable seam)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class _PinnedIPBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend that dials a pre-checked IP while URL, Host, SNI and cert checks keep the hostname."""

    def __init__(self, ip: str) -> None:
        """Store the validated IP every connection will use."""
        self._ip = ip
        self._real = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        """Connect to the pinned IP, ignoring a fresh DNS answer for `host`."""
        return await self._real.connect_tcp(
            self._ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        """Delegate sleep."""
        await self._real.sleep(seconds)


async def _make_storage_client(
    presigned_url: str, storage_host: str | None, timeout: httpx.Timeout | None = None
) -> httpx.AsyncClient:
    """Return a client that connects only to an IP resolved and classified right here.

    Resolve once, reject if ANY answer is blocked (defeats multi-record rebinding), then
    pin the connection to the first checked IP so a second DNS lookup cannot differ.
    Redirects stay off and env proxies are ignored. Raises ValueError when blocked.
    """
    parsed = urlparse(presigned_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        ips = [host] if _is_ip_literal(host) else await _resolve_host(host, port)
    except OSError as exc:
        raise ValueError(f"cannot resolve storage host: {exc}") from exc
    if not ips:
        raise ValueError("storage host did not resolve")
    for raw in ips:
        reason = _ip_blocked(ipaddress.ip_address(raw), allow_private=host == storage_host)
        if reason:
            raise ValueError(reason)
    transport = httpx.AsyncHTTPTransport()
    transport._pool._network_backend = _PinnedIPBackend(ips[0])  # type: ignore[attr-defined]
    return httpx.AsyncClient(
        transport=transport, timeout=timeout or _storage_timeout(), follow_redirects=False, trust_env=False
    )


def _is_ip_literal(host: str) -> bool:
    """True when `host` parses as an IP address."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class HTTPBootServer:
    """HTTP server for boot files and iPXE scripts."""

    def __init__(
        self,
        config: WorkerConfig,
        enrollment: EnrollmentManager,
        token_store: BootTokenStore | None = None,
    ):
        self.config = config
        self.enrollment = enrollment
        self.app = Quart(__name__)
        self.app.config["MAX_CONTENT_LENGTH"] = BOOT_EVENT_MAX_BYTES
        if token_store is None:
            if not config.redis_url:
                raise ValueError("REDIS_URL is required for boot tokens and rate limiting")
            token_store = create_token_store(config.redis_url)
        self.token_store = token_store
        self.trusted_proxies = tuple(ipaddress.ip_network(c) for c in config.trusted_proxies)
        self.ipxe_handler = IPXEHandler(config, enrollment, token_store)
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
            client_ip = self._client_ip()
            logger.info("ipxe_script_request", client_ip=client_ip)
            if client_ip is None:
                return Response("#!ipxe\necho Unknown client address\nshell\n", mimetype="text/plain", status=400)

            try:
                if not await self.token_store.allow(f"ipxe:{client_ip}", IPXE_RATE_LIMIT, BOOT_EVENT_RATE_WINDOW_S):
                    logger.warning("ipxe_rate_limited", client_ip=client_ip)
                    return Response("#!ipxe\necho Rate limited\nshell\n", mimetype="text/plain", status=429)
            except StoreUnavailableError:
                return Response("#!ipxe\necho Service unavailable\nshell\n", mimetype="text/plain", status=503)

            try:
                script = await self.ipxe_handler.generate_script(mac, client_ip)
                return Response(script, mimetype="text/plain")
            except Exception as e:
                logger.error("ipxe_script_generation_error", mac=mac, error=str(e))
                return Response(
                    "#!ipxe\necho Error generating boot script\nshell\n",
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
                return Response("# Error\n", mimetype="text/yaml", status=500)

        @self.app.route("/cloud-init/<machine_id>/user-data")
        async def cloud_init_userdata(machine_id: str):
            """Cloud-init user-data endpoint."""
            logger.info("cloud_init_userdata_request", machine_id=machine_id)

            try:
                userdata = await self.ipxe_handler.get_cloud_init_userdata(machine_id)
                return Response(userdata, mimetype="text/cloud-config")
            except Exception as e:
                logger.error("cloud_init_userdata_error", machine_id=machine_id, error=str(e))
                return Response("#cloud-config\n# Error\n", mimetype="text/cloud-config", status=500)

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
                        is_valid_url, url_error = _validate_presigned_url(
                            presigned_url, _storage_host(self.config)
                        )
                        if not is_valid_url:
                            logger.warning(
                                "presigned_url_validation_failed",
                                path=image_path,
                                reason=url_error,
                            )
                            return Response("Invalid presigned URL", status=400)

                        try:
                            preflight = await self._preflight_image(presigned_url, image_path)
                        except ValueError as blocked:
                            logger.warning(
                                "presigned_url_validation_failed", path=image_path, reason=str(blocked)
                            )
                            return Response("Invalid presigned URL", status=400)
                        if isinstance(preflight, Response):
                            return preflight
                        out_headers = _outbound_image_headers(preflight.total_length)
                        if request.method == "HEAD":
                            out_headers["Content-Type"] = "application/octet-stream"
                            return _ImageHeadResponse(b"", headers=out_headers)
                        return Response(
                            _LazyImageBody(presigned_url, self.image_chunk_size, _storage_host(self.config)),
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
            Receive boot event callbacks from booting machines.

            Unauthenticated callers cannot reach the hub with the worker's credentials:
            the request needs the single-use per-boot token embedded in the iPXE script,
            bound to the MAC in the body, plus a per-source-IP rate limit. Only schema-
            validated fields are forwarded and no upstream detail is echoed back.
            """
            client_ip = self._client_ip()
            if client_ip is None:
                return jsonify({"status": "error"}), 400  # no shared "unknown" bucket

            token = request.headers.get("X-Boot-Token", "")
            if not _TOKEN_RE.fullmatch(token):
                # Cheap rejection: no store access, so junk cannot burn anyone's rate budget
                return jsonify({"status": "unauthorized"}), 401

            try:
                if not await self.token_store.allow(
                    f"event-ip:{client_ip}", BOOT_EVENT_RATE_LIMIT, BOOT_EVENT_RATE_WINDOW_S
                ):
                    logger.warning("boot_event_rate_limited", client_ip=client_ip)
                    return jsonify({"status": "error"}), 429

                event, error = _parse_boot_event(await request.get_json(silent=True))
                if event is None:
                    return jsonify({"status": "error", "message": error}), 400

                if not await self.token_store.allow(
                    f"event-mac:{event.mac}", BOOT_EVENT_MAC_RATE_LIMIT, BOOT_EVENT_RATE_WINDOW_S
                ):
                    logger.warning("boot_event_mac_rate_limited")
                    return jsonify({"status": "error"}), 429

                if not await self.token_store.redeem(token, event.mac, client_ip, event.stage):
                    logger.warning("boot_event_token_rejected", client_ip=client_ip)
                    return jsonify({"status": "unauthorized"}), 401
            except StoreUnavailableError:
                logger.error("boot_event_store_unavailable")
                return jsonify({"status": "error"}), 503

            logger.info("boot_event_received", stage=event.stage, machine_id=event.machine_id)

            try:
                api_url = f"{self.config.api_manager_url}/api/v1/internal/boot-event"
                headers = self.enrollment.get_auth_headers()
                headers["Content-Type"] = "application/json"

                async with httpx.AsyncClient(timeout=5.0) as client:
                    upstream = await client.post(api_url, json=event.as_payload(), headers=headers)
            except Exception as e:
                logger.error("boot_event_forward_error", error=str(e))
                return jsonify({"status": "error"}), 502

            if not upstream.is_success:
                logger.error("boot_event_upstream_rejected", status=upstream.status_code)
                return jsonify({"status": "error"}), 502

            return jsonify({"status": "accepted"}), 202

    def _client_ip(self) -> str | None:
        """Client IP for binding and rate limiting (None if unknown); proxy-aware via TRUSTED_PROXIES."""
        return _resolve_client_ip(
            request.remote_addr, request.headers.get("X-Forwarded-For"), self.trusted_proxies
        )

    async def _preflight_image(self, presigned_url: str, image_path: str) -> "_ImagePreflight | Response":
        """Ranged GET pre-flight (bytes=0-0): returns the total size or an error Response.

        Same method as the presigned URL (SigV4 binds the method, so HEAD would 403).
        The pre-flight response is always closed without reading the body.
        """
        client = await _make_storage_client(presigned_url, _storage_host(self.config))
        async with client:
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
