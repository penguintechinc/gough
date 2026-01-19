"""
TFTP server for serving iPXE boot binaries.

Serves undionly.kpxe (BIOS) and ipxe.efi (UEFI) from local filesystem.
"""

import asyncio
import structlog
from pathlib import Path
from py3tftp.protocols import TFTPServerProtocol
from py3tftp.file_io import FileReader

from worker.config import WorkerConfig

logger = structlog.get_logger()


class TFTPServer:
    """TFTP server for iPXE binaries."""

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.running = False
        self.transport = None
        self.protocol = None

    async def start(self):
        """Start TFTP server."""
        loop = asyncio.get_running_loop()

        # Verify TFTP root exists
        tftp_root = Path(self.config.tftp_root)
        if not tftp_root.exists():
            logger.error("tftp_root_not_found", path=str(tftp_root))
            raise FileNotFoundError(f"TFTP root not found: {tftp_root}")

        # Check for required files
        required_files = ["undionly.kpxe", "ipxe.efi"]
        for filename in required_files:
            file_path = tftp_root / filename
            if not file_path.exists():
                logger.warning("tftp_file_missing", file=filename)
            else:
                logger.info("tftp_file_found", file=filename, size=file_path.stat().st_size)

        # Create TFTP protocol
        self.protocol = TFTPServerProtocol(
            file_handler_cls=FileReader,
            root_path=str(tftp_root),
        )

        # Start UDP server
        try:
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: self.protocol,
                local_addr=("0.0.0.0", self.config.tftp_port),
            )
            self.running = True
            logger.info(
                "tftp_server_listening",
                port=self.config.tftp_port,
                root=str(tftp_root),
            )

            # Keep running until stopped
            while self.running:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error("tftp_server_error", error=str(e))
            raise

    async def stop(self):
        """Stop TFTP server."""
        self.running = False
        if self.transport:
            self.transport.close()
            logger.info("tftp_server_stopped")
