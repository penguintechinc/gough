"""Tests for the read-only, root-confined TFTP handler in worker.services.tftp_server."""

import asyncio
from pathlib import Path

import pytest
from py3tftp.exceptions import ProtocolException
from py3tftp.tftp_packet import TFTPPacketFactory

from worker.services.tftp_server import RootedFileReader, RootedTFTPServerProtocol


@pytest.fixture
def tftp_root(tmp_path: Path) -> Path:
    """Create a TFTP root containing one boot file, a subdirectory and a secret outside it."""
    root = tmp_path / "root"
    (root / "subdir").mkdir(parents=True)
    (root / "undionly.kpxe").write_bytes(b"BIOS-BOOT")
    (root / "subdir" / "inner.bin").write_bytes(b"INNER")
    (tmp_path / "secret.txt").write_bytes(b"SECRET")
    return root


@pytest.fixture
def protocol(tftp_root: Path) -> RootedTFTPServerProtocol:
    """Build a protocol bound to the test root on a throwaway event loop."""
    loop = asyncio.new_event_loop()
    try:
        yield RootedTFTPServerProtocol(tftp_root.resolve(), "127.0.0.1", loop)
    finally:
        loop.close()


def read_all(handler: RootedFileReader) -> bytes:
    """Drain a file handler and return its full contents."""
    data = b""
    while True:
        chunk = handler.read_chunk(512)
        if not chunk:
            return data
        data += chunk


def test_serves_file_under_root(tftp_root: Path) -> None:
    """A plain filename inside the root is readable in full."""
    handler = RootedFileReader(tftp_root, b"undionly.kpxe", 512, b"octet")
    assert read_all(handler) == b"BIOS-BOOT"


def test_serves_nested_file_under_root(tftp_root: Path) -> None:
    """A relative path into a subdirectory under the root is readable."""
    handler = RootedFileReader(tftp_root, b"subdir/inner.bin", 512, b"octet")
    assert read_all(handler) == b"INNER"


@pytest.mark.parametrize(
    "name",
    [
        b"../secret.txt",
        b"/etc/passwd",
        b"subdir/../../secret.txt",
        b"subdir/../../../../../../etc/passwd",
    ],
)
def test_rejects_traversal(tftp_root: Path, name: bytes) -> None:
    """Names that resolve outside the root raise FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        RootedFileReader(tftp_root, name, 512, b"octet")


def test_rejects_symlink_pointing_outside_root(tftp_root: Path, tmp_path: Path) -> None:
    """A symlink inside the root that targets a file outside it is refused."""
    (tftp_root / "link.txt").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(FileNotFoundError):
        RootedFileReader(tftp_root, b"link.txt", 512, b"octet")


def test_rejects_directory_name(tftp_root: Path) -> None:
    """A name that resolves to a directory is not served."""
    with pytest.raises(FileNotFoundError):
        RootedFileReader(tftp_root, b"subdir", 512, b"octet")


def test_rejects_missing_file(tftp_root: Path) -> None:
    """A name with no matching file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        RootedFileReader(tftp_root, b"missing.kpxe", 512, b"octet")


@pytest.mark.parametrize("name", [b"undionly.kpxe\x00.txt", b"\x00", b"sub\x00dir/inner.bin"])
def test_rejects_nul_bytes(tftp_root: Path, name: bytes) -> None:
    """Filenames containing NUL bytes are rejected before any path resolution."""
    with pytest.raises(FileNotFoundError):
        RootedFileReader(tftp_root, name, 512, b"octet")


def test_write_request_is_rejected(protocol: RootedTFTPServerProtocol) -> None:
    """A WRQ never reaches a file handler: select_protocol raises ProtocolException."""
    wrq = TFTPPacketFactory().from_bytes(b"\x00\x02" + b"upload.bin\x00octet\x00")
    with pytest.raises(ProtocolException):
        protocol.select_protocol(wrq)


def test_read_request_is_accepted(protocol: RootedTFTPServerProtocol) -> None:
    """A RRQ passes select_protocol and yields a handler bound to the root."""
    rrq = TFTPPacketFactory().from_bytes(b"\x00\x01" + b"undionly.kpxe\x00octet\x00")
    assert protocol.select_protocol(rrq) is not None
    handler = protocol.select_file_handler(rrq)(b"undionly.kpxe", 512)
    assert read_all(handler) == b"BIOS-BOOT"
