"""
HTTP boot server for iPXE scripts, kernels, and cloud-init.

Provides dynamic iPXE script generation and serves boot images.
"""

import structlog
from quart import Quart, request, Response, jsonify
from hypercorn.config import Config
from hypercorn.asyncio import serve
import httpx

from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager
from worker.services.ipxe_handler import IPXEHandler

logger = structlog.get_logger()


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

            This avoids exposing storage credentials to booting machines.
            """
            logger.info("image_request", path=image_path)

            try:
                # Request presigned URL from api-manager
                api_url = f"{self.config.api_manager_url}/api/v1/internal/image-url/{image_path}"
                headers = self.enrollment.get_auth_headers()

                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(api_url, headers=headers)

                    if response.status_code == 200:
                        data = response.json()
                        presigned_url = data.get("url")

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
