"""Behaviour tests for the TFTPServer lifecycle, netascii handling and read-only protocol rules."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from py3tftp.exceptions import ProtocolException
from py3tftp.netascii import Netascii
from worker.config import WorkerConfig
from worker.services.tftp_server import (
    RootedFileReader,
    RootedTFTPServerProtocol,
    TFTPServer,
)


def _cfg(root: Path, port: int = 0) -> WorkerConfig:
    """Config pointing the TFTP server at a root; port 0 asks the OS for a free UDP port."""
    return WorkerConfig(
        api_manager_url="http://api.test",
        worker_api_key="k",
        worker_id="w",
        dhcp_mode="disabled",
        dhcp_interface="eth0",
        tftp_port=port,
        tftp_root=str(root),
    )


@pytest.fixture
def boot_root(tmp_path: Path) -> Path:
    """A TFTP root holding the two boot binaries the worker advertises."""
    root = tmp_path / "tftp"
    root.mkdir()
    (root / "undionly.kpxe").write_bytes(b"BIOS")
    (root / "ipxe.efi").write_bytes(b"EFI-BINARY")
    return root


class _WRQ:
    """Minimal packet stand-in reporting a write request."""

    mode = b"octet"

    def is_wrq(self) -> bool:
        """Report this packet as a write request."""
        return True


class _RRQ:
    """Minimal packet stand-in reporting a read request."""

    mode = b"octet"

    def is_wrq(self) -> bool:
        """Report this packet as a read request."""
        return False


def test_tftp_server_init_defaults(boot_root: Path) -> None:
    """A new TFTPServer is not running and has no transport or protocol yet."""
    server = TFTPServer(_cfg(boot_root))

    assert server.running is False
    assert server.transport is None
    assert server.protocol is None


@pytest.mark.asyncio
async def test_start_fails_when_root_missing(tmp_path: Path) -> None:
    """A configured TFTP root that does not exist aborts startup with FileNotFoundError."""
    server = TFTPServer(_cfg(tmp_path / "absent"))

    with pytest.raises(FileNotFoundError, match="TFTP root not found"):
        await server.start()

    assert server.running is False


@pytest.mark.asyncio
async def test_start_serves_until_stopped_and_stop_closes_transport(
    boot_root: Path,
) -> None:
    """start() binds a real UDP socket, reports running, and returns once stop() is called."""
    server = TFTPServer(_cfg(boot_root))
    task = asyncio.create_task(server.start())

    for _ in range(100):  # wait for the endpoint to bind
        if server.running:
            break
        await asyncio.sleep(0.01)

    assert server.running is True
    assert server.transport is not None
    assert isinstance(server.protocol, RootedTFTPServerProtocol)
    assert server.protocol.root == boot_root.resolve()

    await server.stop()
    await asyncio.wait_for(task, timeout=5)

    assert server.running is False


@pytest.mark.asyncio
async def test_start_bind_failure_is_reraised(
    boot_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A UDP bind error (e.g. port 69 held by another process) propagates and leaves the server stopped."""
    server = TFTPServer(_cfg(boot_root))
    loop = asyncio.get_running_loop()

    async def refuse(factory: Any, local_addr: tuple[str, int]) -> None:
        """Simulate the TFTP port being unavailable."""
        raise OSError("address in use")

    monkeypatch.setattr(loop, "create_datagram_endpoint", refuse)

    with pytest.raises(OSError, match="address in use"):
        await server.start()

    assert server.running is False


@pytest.mark.asyncio
async def test_stop_without_start_is_safe(boot_root: Path) -> None:
    """stop() on a never-started server is a harmless no-op."""
    server = TFTPServer(_cfg(boot_root))

    await server.stop()

    assert server.running is False


def test_protocol_rejects_write_requests(boot_root: Path) -> None:
    """Write requests are refused outright; the worker only serves boot binaries."""
    loop = asyncio.new_event_loop()
    try:
        proto = RootedTFTPServerProtocol(boot_root.resolve(), "127.0.0.1", loop)
        with pytest.raises(ProtocolException, match="writes are disabled"):
            proto.select_protocol(_WRQ())
    finally:
        loop.close()


def test_protocol_handler_factory_serves_files_under_root(boot_root: Path) -> None:
    """The handler factory returned for a read request opens files inside the configured root."""
    loop = asyncio.new_event_loop()
    try:
        proto = RootedTFTPServerProtocol(boot_root.resolve(), "127.0.0.1", loop)
        factory = proto.select_file_handler(_RRQ())
        handler = factory(b"ipxe.efi", {})
    finally:
        loop.close()

    assert isinstance(handler, RootedFileReader)
    assert handler.fname == boot_root.resolve() / "ipxe.efi"


def test_netascii_mode_wraps_reader(boot_root: Path) -> None:
    """A netascii-mode transfer wraps the file in the netascii encoder."""
    handler = RootedFileReader(boot_root, b"undionly.kpxe", 512, b"netascii")

    assert isinstance(handler._f, Netascii)


def test_octet_mode_does_not_wrap_reader(boot_root: Path) -> None:
    """Binary (octet) transfers read the raw file without the netascii wrapper."""
    handler = RootedFileReader(boot_root, b"undionly.kpxe", 512, b"octet")

    assert not isinstance(handler._f, Netascii)
