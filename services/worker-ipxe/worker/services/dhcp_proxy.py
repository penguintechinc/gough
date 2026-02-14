"""
ProxyDHCP server for PXE boot (port 4011).

Responds to PXE requests with boot server information without assigning IPs.
Works alongside existing DHCP server.
"""

import asyncio
import struct
import structlog
from typing import Optional

from worker.config import WorkerConfig

logger = structlog.get_logger()


class DHCPProxyServer:
    """ProxyDHCP server for PXE boot."""

    DHCP_SERVER_PORT = 67
    DHCP_CLIENT_PORT = 68
    PROXYDHCP_PORT = 4011

    # DHCP message types
    DHCP_DISCOVER = 1
    DHCP_OFFER = 2
    DHCP_REQUEST = 3
    DHCP_ACK = 5

    # DHCP options
    DHCP_MESSAGE_TYPE = 53
    DHCP_VENDOR_CLASS_ID = 60
    DHCP_TFTP_SERVER = 66
    DHCP_BOOT_FILE = 67
    DHCP_END = 255

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.running = False
        self.transport: Optional[asyncio.DatagramTransport] = None

    def _parse_dhcp_packet(self, data: bytes) -> Optional[dict]:
        """Parse DHCP packet and extract relevant fields."""
        if len(data) < 240:
            return None

        try:
            # Parse DHCP header
            op, htype, hlen, hops = struct.unpack("!BBBB", data[0:4])
            xid = struct.unpack("!I", data[4:8])[0]
            flags = struct.unpack("!H", data[10:12])[0]
            ciaddr = data[12:16]
            yiaddr = data[16:20]
            siaddr = data[20:24]
            giaddr = data[24:28]
            chaddr = data[28:44]

            # Extract MAC address
            mac = ":".join(f"{b:02x}" for b in chaddr[:hlen])

            # Parse options
            options = {}
            i = 236  # Options start after magic cookie
            magic = struct.unpack("!I", data[236:240])[0]
            if magic != 0x63825363:
                return None

            i = 240
            while i < len(data):
                opt_code = data[i]
                if opt_code == self.DHCP_END:
                    break
                if opt_code == 0:  # Padding
                    i += 1
                    continue

                opt_len = data[i + 1]
                opt_data = data[i + 2 : i + 2 + opt_len]
                options[opt_code] = opt_data
                i += 2 + opt_len

            return {
                "op": op,
                "xid": xid,
                "flags": flags,
                "mac": mac,
                "chaddr": chaddr[:hlen],
                "options": options,
            }
        except Exception as e:
            logger.error("dhcp_packet_parse_error", error=str(e))
            return None

    def _is_pxe_request(self, packet: dict) -> bool:
        """Check if DHCP packet is a PXE request."""
        vendor_class = packet["options"].get(self.DHCP_VENDOR_CLASS_ID, b"")
        return vendor_class.startswith(b"PXEClient")

    def _build_proxydhcp_offer(self, request: dict) -> bytes:
        """Build ProxyDHCP OFFER response."""
        boot_url = self.config.get_boot_url()

        # Determine boot file based on architecture
        # TODO: Parse architecture from DHCP option 93
        boot_file = "ipxe.efi"  # Default to UEFI

        # Build DHCP packet
        packet = bytearray(300)

        # DHCP header
        packet[0] = 2  # BOOTREPLY
        packet[1] = 1  # Ethernet
        packet[2] = 6  # MAC address length
        packet[3] = 0  # Hops
        struct.pack_into("!I", packet, 4, request["xid"])  # Transaction ID
        struct.pack_into("!H", packet, 10, request["flags"])  # Flags

        # Addresses (all zero for ProxyDHCP)
        # Client hardware address
        packet[28:28 + len(request["chaddr"])] = request["chaddr"]

        # Magic cookie
        struct.pack_into("!I", packet, 236, 0x63825363)

        # DHCP options
        opts = bytearray()

        # Option 53: DHCP Message Type (OFFER)
        opts.extend([self.DHCP_MESSAGE_TYPE, 1, self.DHCP_OFFER])

        # Option 60: Vendor Class Identifier
        vendor = b"PXEClient"
        opts.extend([self.DHCP_VENDOR_CLASS_ID, len(vendor)])
        opts.extend(vendor)

        # Option 66: TFTP Server Name
        tftp_server = self.config.get_boot_url().encode("ascii")
        opts.extend([self.DHCP_TFTP_SERVER, len(tftp_server)])
        opts.extend(tftp_server)

        # Option 67: Boot File Name
        boot_file_bytes = boot_file.encode("ascii")
        opts.extend([self.DHCP_BOOT_FILE, len(boot_file_bytes)])
        opts.extend(boot_file_bytes)

        # End option
        opts.append(self.DHCP_END)

        # Copy options to packet
        packet[240:240 + len(opts)] = opts

        return bytes(packet[:240 + len(opts)])

    class DHCPProxyProtocol(asyncio.DatagramProtocol):
        """Asyncio protocol for ProxyDHCP server."""

        def __init__(self, server):
            self.server = server

        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            asyncio.create_task(self.server._handle_packet(data, addr))

    async def _handle_packet(self, data: bytes, addr: tuple):
        """Handle incoming DHCP packet."""
        packet = self._parse_dhcp_packet(data)
        if not packet:
            return

        # Only respond to PXE requests
        if not self._is_pxe_request(packet):
            return

        msg_type = packet["options"].get(self.DHCP_MESSAGE_TYPE)
        if not msg_type or len(msg_type) != 1:
            return

        msg_type_code = msg_type[0]

        if msg_type_code == self.DHCP_DISCOVER:
            logger.info(
                "proxydhcp_discover",
                mac=packet["mac"],
                client=addr[0],
            )

            # Send ProxyDHCP OFFER
            offer = self._build_proxydhcp_offer(packet)
            self.transport.sendto(offer, (addr[0], self.DHCP_CLIENT_PORT))

            logger.debug(
                "proxydhcp_offer_sent",
                mac=packet["mac"],
                client=addr[0],
            )

    async def start(self):
        """Start ProxyDHCP server."""
        loop = asyncio.get_running_loop()

        try:
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: self.DHCPProxyProtocol(self),
                local_addr=("0.0.0.0", self.PROXYDHCP_PORT),
            )
            self.running = True

            logger.info(
                "proxydhcp_server_started",
                port=self.PROXYDHCP_PORT,
                interface=self.config.dhcp_interface,
            )

            # Keep running
            while self.running:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error("proxydhcp_server_error", error=str(e))
            raise

    async def stop(self):
        """Stop ProxyDHCP server."""
        self.running = False
        if self.transport:
            self.transport.close()
            logger.info("proxydhcp_server_stopped")
