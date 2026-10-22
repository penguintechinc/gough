"""
TFTP server for serving iPXE boot binaries.

Serves undionly.kpxe (BIOS) and ipxe.efi (UEFI) from local filesystem.
"""

import asyncio
import os
import structlog
from pathlib import Path
from py3tftp.exceptions import ProtocolException
from py3tftp.file_io import FileReader
from py3tftp.protocols import TFTPServerProtocol

from worker.config import WorkerConfig

logger = structlog.get_logger()


class RootedFileReader(FileReader):
    """
    Read-only file handler confined to a configured TFTP root.

    py3tftp 1.x resolves requested names against the process cwd and exposes no
    root option, so this bypasses FileReader.__init__ and resolves under `root`
    itself, rejecting any path that escapes it.
    """

    def __init__(self, root: Path, fname, chunk_size=0, mode=None):
        """Open `fname` under `root`; raise FileNotFoundError on traversal or missing file."""
        self._f = None
        name = os.fsdecode(fname)
        if "\x00" in name:
            raise FileNotFoundError("filename contains NUL byte")
        root_resolved = root.resolve()
        candidate = (root_resolved / name.lstrip("/")).resolve()
        if not candidate.is_relative_to(root_resolved) or not candidate.is_file():
            raise FileNotFoundError(name)
        self.fname = candidate
        self.chunk_size = chunk_size
        self._f = self._open_file()
        self.finished = False
        if mode == b"netascii":
            from py3tftp.netascii import Netascii

            self._f = Netascii(self._f)


class RootedTFTPServerProtocol(TFTPServerProtocol):
    """
    Read-only py3tftp server protocol serving files from `root`.

    Write requests are rejected because the worker only serves boot binaries.
    """

    def __init__(self, root: Path, host_interface: str, loop: asyncio.AbstractEventLoop):
        """Bind the protocol to a TFTP root, listen address and event loop."""
        super().__init__(host_interface, loop, {})
        self.root = root

    def select_protocol(self, packet):
        """Allow only read requests; reject writes."""
        if packet.is_wrq():
            raise ProtocolException("TFTP writes are disabled")
        return super().select_protocol(packet)

    def select_file_handler(self, packet):
        """Return a handler factory bound to this protocol's root."""
        root = self.root
        return lambda filename, opts: RootedFileReader(root, filename, opts, packet.mode)


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
        self.protocol = RootedTFTPServerProtocol(tftp_root.resolve(), "0.0.0.0", loop)

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
