"""Behaviour tests for ProxyDHCP packet parsing, PXE detection, offer building and message handling.

Fixtures are byte-exact captures in the wire layout of a PXE client DISCOVER (RFC 2131 header,
magic cookie, then TLV options), so the parser is exercised on real frames, not on helpers.
"""

import struct
from typing import Any

import pytest
from worker.config import WorkerConfig
from worker.services.dhcp_proxy import DHCPProxyServer

# UEFI PXE DISCOVER: xid 0x3903f326, flags broadcast, chaddr 00:50:56:c0:00:08,
# options 53=DISCOVER, 60="PXEClient", 55=params, 93=arch 7 (EFI x64), END.
PXE_DISCOVER_HEX = (
    "010106003903f3260000800000000000000000000000000000000000005056c00008"
    "00000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000006382"
    "53633501013c09505845436c69656e743704010306425d020007ff"
)
PXE_DISCOVER = bytes.fromhex(PXE_DISCOVER_HEX)
ADDR = ("192.168.50.10", 68)


def _server(
    monkeypatch: pytest.MonkeyPatch, boot_url: str = "http://10.1.1.5:8080"
) -> DHCPProxyServer:
    """Create a proxy server whose boot URL is fixed by config."""
    monkeypatch.delenv("DHCP_MAX_INFLIGHT", raising=False)
    cfg = WorkerConfig(
        api_manager_url="http://api.test",
        worker_api_key="k",
        worker_id="w",
        dhcp_mode="proxy",
        dhcp_interface="eth0",
        http_boot_url=boot_url,
    )
    return DHCPProxyServer(cfg)


def _packet(
    options: bytes,
    *,
    op: int = 1,
    xid: int = 0x11223344,
    flags: int = 0,
    chaddr: bytes = b"\xaa\xbb\xcc\xdd\xee\xff",
    hlen: int = 6,
    magic: int = 0x63825363
) -> bytes:
    """Assemble a DHCP datagram from header fields and a raw option blob."""
    # op,htype,hlen,hops | xid | secs,flags | ciaddr,yiaddr,siaddr,giaddr (zeroed)
    header = struct.pack("!BBBBIHH", op, 1, hlen, 0, xid, 0, flags) + bytes(16)
    chaddr_field = chaddr.ljust(16, b"\x00")
    body = header + chaddr_field + bytes(64 + 128)  # sname + file
    return body + struct.pack("!I", magic) + options


class FakeTransport:
    """Records sendto calls in place of an asyncio datagram transport."""

    def __init__(self) -> None:
        """Start with no sent datagrams."""
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        """Record one outbound datagram."""
        self.sent.append((data, addr))

    def close(self) -> None:
        """Mark the transport closed (no-op for the fake)."""


def test_fixture_is_well_formed_pxe_discover() -> None:
    """The fixture is exactly 265 bytes with the DHCP magic cookie at offset 236."""
    assert len(PXE_DISCOVER) == 265
    assert PXE_DISCOVER[236:240] == bytes.fromhex("63825363")


def test_parse_captured_pxe_discover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parsing a captured PXE DISCOVER yields header fields, MAC and the decoded option table."""
    packet = _server(monkeypatch)._parse_dhcp_packet(PXE_DISCOVER)

    assert packet is not None
    assert packet["op"] == 1
    assert packet["xid"] == 0x3903F326
    assert packet["flags"] == 0x8000
    assert packet["mac"] == "00:50:56:c0:00:08"
    assert packet["chaddr"] == bytes.fromhex("005056c00008")
    assert packet["options"][53] == b"\x01"
    assert packet["options"][60] == b"PXEClient"
    assert packet["options"][55] == bytes([1, 3, 6, 66])
    assert packet["options"][93] == bytes([0, 7])


def test_mac_is_truncated_to_hlen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only hlen bytes of chaddr are reported as the MAC; trailing padding is ignored."""
    raw = _packet(bytes([53, 1, 1, 255]), chaddr=b"\x01\x02\x03\x04", hlen=4)

    packet = _server(monkeypatch)._parse_dhcp_packet(raw)

    assert packet is not None
    assert packet["mac"] == "01:02:03:04"
    assert packet["chaddr"] == b"\x01\x02\x03\x04"


def test_padding_bytes_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero (pad) option bytes between options are skipped and do not corrupt parsing."""
    raw = _packet(bytes([0, 0, 53, 1, 2, 0, 255]))

    packet = _server(monkeypatch)._parse_dhcp_packet(raw)

    assert packet is not None
    assert packet["options"] == {53: b"\x02"}


def test_missing_end_option_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A datagram that ends after the last option (no 255) still parses."""
    raw = _packet(bytes([53, 1, 1]))

    packet = _server(monkeypatch)._parse_dhcp_packet(raw)

    assert packet is not None
    assert packet["options"] == {53: b"\x01"}


@pytest.mark.parametrize("length", [0, 1, 100, 239])
def test_short_datagram_rejected(monkeypatch: pytest.MonkeyPatch, length: int) -> None:
    """Anything shorter than the 240-byte fixed header plus cookie is rejected without parsing."""
    assert _server(monkeypatch)._parse_dhcp_packet(PXE_DISCOVER[:length]) is None


def test_bad_magic_cookie_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame whose cookie is not 0x63825363 is not DHCP and is dropped."""
    corrupted = bytearray(PXE_DISCOVER)
    corrupted[236:240] = b"\x00\x00\x00\x00"

    assert _server(monkeypatch)._parse_dhcp_packet(bytes(corrupted)) is None


def test_option_overrunning_datagram_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An option whose declared length runs past the end of the datagram makes the packet malformed."""
    raw = _packet(bytes([60, 40]) + b"PXEClient")  # declares 40 bytes, only 9 present

    assert _server(monkeypatch)._parse_dhcp_packet(raw) is None


def test_option_missing_length_byte_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An option code at the very end with no length byte is rejected by the bounds error path."""
    raw = _packet(bytes([53]))

    assert _server(monkeypatch)._parse_dhcp_packet(raw) is None


def test_is_pxe_request_detects_vendor_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only vendor class identifiers starting with PXEClient mark a PXE request."""
    server = _server(monkeypatch)

    assert server._is_pxe_request({"options": {60: b"PXEClient:Arch:00007"}}) is True
    assert server._is_pxe_request({"options": {60: b"MSFT 5.0"}}) is False
    assert server._is_pxe_request({"options": {}}) is False


def test_build_offer_round_trips_header_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offer echoes xid, flags and chaddr, is BOOTREPLY and carries the expected options."""
    server = _server(monkeypatch, boot_url="http://10.1.1.5:8080")
    request = server._parse_dhcp_packet(PXE_DISCOVER)
    assert request is not None

    offer = server._build_proxydhcp_offer(request)

    assert offer[0] == 2  # BOOTREPLY
    assert offer[1] == 1 and offer[2] == 6
    assert struct.unpack("!I", offer[4:8])[0] == 0x3903F326
    assert struct.unpack("!H", offer[10:12])[0] == 0x8000
    assert offer[28:34] == bytes.fromhex("005056c00008")
    assert offer[236:240] == bytes.fromhex("63825363")
    assert offer[-1] == 255

    parsed = server._parse_dhcp_packet(offer)
    assert parsed is not None
    assert parsed["options"][53] == bytes([DHCPProxyServer.DHCP_OFFER])
    assert parsed["options"][60] == b"PXEClient"
    assert parsed["options"][66] == b"http://10.1.1.5:8080"
    assert parsed["options"][67] == b"ipxe.efi"


def test_build_offer_length_matches_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offer length is the 240-byte header plus exactly the encoded options (no trailing garbage)."""
    server = _server(monkeypatch, boot_url="http://b:1")
    request = server._parse_dhcp_packet(PXE_DISCOVER)
    assert request is not None

    offer = server._build_proxydhcp_offer(request)

    expected_options = (
        3 + (2 + 9) + (2 + len(b"http://b:1")) + (2 + len(b"ipxe.efi")) + 1
    )
    assert len(offer) == 240 + expected_options


def test_handle_discover_sends_offer_to_client_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PXE DISCOVER triggers exactly one OFFER unicast to the client's port 68."""
    import asyncio

    server = _server(monkeypatch)
    transport = FakeTransport()
    server.transport = transport  # type: ignore[assignment]

    asyncio.run(server._handle_packet(PXE_DISCOVER, ADDR))

    assert len(transport.sent) == 1
    data, addr = transport.sent[0]
    assert addr == ("192.168.50.10", 68)
    assert data[0] == 2


@pytest.mark.asyncio
async def test_handle_ignores_non_pxe_discover(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain (non-PXE) DISCOVER gets no ProxyDHCP reply."""
    server = _server(monkeypatch)
    transport = FakeTransport()
    server.transport = transport  # type: ignore[assignment]
    plain = _packet(bytes([53, 1, 1, 60, 8]) + b"MSFT 5.0" + bytes([255]))

    await server._handle_packet(plain, ADDR)

    assert transport.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        bytes([60, 9])
        + b"PXEClient"
        + bytes([53, 1, 3, 255]),  # REQUEST: not answered by this proxy
        bytes([60, 9])
        + b"PXEClient"
        + bytes([53, 2, 1, 1, 255]),  # malformed message-type length
        bytes([60, 9]) + b"PXEClient" + bytes([255]),  # no message-type option at all
    ],
    ids=["request", "bad-type-length", "missing-type"],
)
async def test_handle_skips_unhandled_or_malformed_message_types(
    monkeypatch: pytest.MonkeyPatch, options: bytes
) -> None:
    """Only a well-formed DISCOVER is answered; other or malformed message types are silently ignored."""
    server = _server(monkeypatch)
    transport = FakeTransport()
    server.transport = transport  # type: ignore[assignment]

    await server._handle_packet(_packet(options), ADDR)

    assert transport.sent == []


@pytest.mark.asyncio
async def test_handle_drops_unparseable_datagram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage datagrams never reach the reply path."""
    server = _server(monkeypatch)
    transport = FakeTransport()
    server.transport = transport  # type: ignore[assignment]

    await server._handle_packet(b"\x00" * 50, ADDR)

    assert transport.sent == []


def test_protocol_forwards_datagrams_to_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The asyncio protocol records its transport and hands each datagram to dispatch_packet."""
    server = _server(monkeypatch)
    received: list[tuple[bytes, Any]] = []
    server.dispatch_packet = lambda data, addr: received.append((data, addr))  # type: ignore[method-assign]
    proto = DHCPProxyServer.DHCPProxyProtocol(server)
    transport = FakeTransport()

    proto.connection_made(transport)  # type: ignore[arg-type]
    proto.datagram_received(PXE_DISCOVER, ADDR)

    assert proto.transport is transport
    assert received == [(PXE_DISCOVER, ADDR)]


@pytest.mark.asyncio
async def test_stop_closes_transport_and_cancels_inflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop() closes the transport first, then cancels and awaits in-flight handler tasks."""
    import asyncio

    server = _server(monkeypatch)
    closed: list[bool] = []

    class ClosingTransport(FakeTransport):
        """Transport that records when close() is called."""

        def close(self) -> None:
            """Record the close and mark it before handlers are cancelled."""
            closed.append(True)

    server.transport = ClosingTransport()  # type: ignore[assignment]
    started = asyncio.Event()

    async def hang(data: bytes, addr: tuple) -> None:
        """Block until cancelled, emulating a slow handler."""
        started.set()
        await asyncio.sleep(3600)

    server._handle_packet = hang  # type: ignore[method-assign]
    server.dispatch_packet(PXE_DISCOVER, ADDR)
    await started.wait()

    await server.stop()

    assert closed == [True]
    assert server.running is False
    assert server._tasks == set()


@pytest.mark.asyncio
async def test_start_binds_port_4011_and_stop_ends_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start() binds the ProxyDHCP port via a datagram endpoint and runs until stop()."""
    import asyncio

    server = _server(monkeypatch)
    bound: list[tuple[str, int]] = []
    transport = FakeTransport()
    loop = asyncio.get_running_loop()

    async def fake_endpoint(
        factory: Any, local_addr: tuple[str, int]
    ) -> tuple[Any, None]:
        """Capture the bind address and return the fake transport."""
        bound.append(local_addr)
        factory()
        return transport, None

    monkeypatch.setattr(loop, "create_datagram_endpoint", fake_endpoint)
    task = asyncio.create_task(server.start())
    await asyncio.sleep(0.01)
    assert server.running is True

    await server.stop()
    await asyncio.wait_for(task, timeout=5)

    assert bound == [("0.0.0.0", 4011)]


@pytest.mark.asyncio
async def test_start_bind_failure_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bind error (e.g. port in use) is re-raised and the server is left not running."""
    import asyncio

    server = _server(monkeypatch)
    loop = asyncio.get_running_loop()

    async def refuse(factory: Any, local_addr: tuple[str, int]) -> None:
        """Simulate the port already being taken."""
        raise OSError("address already in use")

    monkeypatch.setattr(loop, "create_datagram_endpoint", refuse)

    with pytest.raises(OSError, match="address already in use"):
        await server.start()

    assert server.running is False
