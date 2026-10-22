"""
HTTP boot server for iPXE scripts, kernels, and cloud-init.

Provides dynamic iPXE script generation and serves boot images.
"""

import re
import os
from urllib.parse import urlparse
import structlog
from quart import Quart, request, Response, jsonify
from hypercorn.config import Config
from hypercorn.asyncio import serve
import httpx

from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager
from worker.services.ipxe_handler import IPXEHandler

logger = structlog.get_logger()


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

                        # Stream image from storage
                        async with httpx.AsyncClient(timeout=300.0) as storage_client:
                            image_response = await storage_client.get(presigned_url)
                            return Response(
                                image_response.content,
                                mimetype="application/octet-stream",
                                headers={
                                    "Content-Length": str(len(image_response.content)),
                                    "Cache-Control": "public, max-age=3600",
                                },
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
