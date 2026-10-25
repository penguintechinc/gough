"""
Power management for bare metal machines.

Supports IPMI, Redfish, and Wake-on-LAN for remote power control.
"""

import asyncio
import hashlib
import hmac
import os
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Literal, Optional

import httpcore
import httpx
import structlog

logger = structlog.get_logger()

PowerAction = Literal["on", "off", "cycle", "reset", "status"]
PowerState = Literal["on", "off", "unknown"]


@dataclass(slots=True)
class BMCCredentials:
    """BMC authentication credentials."""

    address: str
    username: str
    password: str
    power_type: str  # ipmi, redfish, wol
    cert_fingerprint: Optional[str] = None  # SHA-256 fingerprint for cert pinning


def _normalize_fingerprint(fp: str) -> str:
    """Normalize fingerprint to uppercase colon-separated form."""
    cleaned = fp.replace(":", "").replace(" ", "").strip().upper()
    if len(cleaned) != 64:
        raise ValueError(f"Invalid SHA-256 fingerprint length: {len(cleaned)}")
    return ":".join(cleaned[i : i + 2] for i in range(0, len(cleaned), 2))


class _PinnedStream(httpcore.AsyncNetworkStream):
    """Network stream that checks the peer leaf certificate against a pinned SHA-256.

    The check runs right after the TLS handshake on the very connection that will carry
    the request, so no credentials are sent to a BMC presenting any other certificate.
    """

    def __init__(
        self, inner: httpcore.AsyncNetworkStream, pin: str | None, host: str
    ) -> None:
        """Wrap `inner`; `pin` is the normalised fingerprint expected after start_tls."""
        self._inner = inner
        self._pin = pin
        self._host = host

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """Delegate read."""
        return await self._inner.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        """Delegate write."""
        await self._inner.write(buffer, timeout)

    async def aclose(self) -> None:
        """Delegate close."""
        await self._inner.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Handshake, then require the leaf certificate to match the pin (else close and raise)."""
        tls = await self._inner.start_tls(ssl_context, server_hostname, timeout)
        if self._pin is None:
            return tls
        ssl_obj = tls.get_extra_info("ssl_object")
        der = ssl_obj.getpeercert(binary_form=True) if ssl_obj is not None else None
        live = hashlib.sha256(der).hexdigest().upper() if der else ""
        if not der or not hmac.compare_digest(_normalize_fingerprint(live), self._pin):
            await tls.aclose()
            raise RuntimeError(
                f"BMC {self._host} certificate does not match the pinned fingerprint"
            )
        return _PinnedStream(tls, None, self._host)

    def get_extra_info(self, info: str) -> object:
        """Delegate extra-info lookup."""
        return self._inner.get_extra_info(info)


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend whose TLS streams enforce a pinned leaf-certificate fingerprint."""

    def __init__(self, pin: str, host: str) -> None:
        """Store the normalised pin and the BMC host (for error messages)."""
        self._pin = pin
        self._host = host
        self._real = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        """Open TCP, returning a stream that pin-checks after start_tls."""
        inner = await self._real.connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        return _PinnedStream(inner, self._pin, self._host)

    async def sleep(self, seconds: float) -> None:
        """Delegate sleep."""
        await self._real.sleep(seconds)


def _build_redfish_client(
    credentials: BMCCredentials, timeout: float
) -> httpx.AsyncClient:
    """Build a Redfish client with mandatory TLS verification, or raise RuntimeError.

    Order: a per-BMC pinned leaf fingerprint (credentials.cert_fingerprint) wins, else
    the REDFISH_CA_FILE bundle verifies the chain and hostname. Neither configured means
    refuse: there is deliberately no insecure fallback.
    """
    if credentials.cert_fingerprint:
        try:
            pin = _normalize_fingerprint(credentials.cert_fingerprint)
        except ValueError as exc:
            raise RuntimeError(f"Invalid stored fingerprint format: {exc}") from exc
        # The chain is self-signed/private by nature; trust is the pin, checked per connection.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        transport = httpx.AsyncHTTPTransport(verify=ctx)
        transport._pool._network_backend = _PinnedBackend(pin, credentials.address)  # type: ignore[attr-defined]
        return httpx.AsyncClient(transport=transport, timeout=timeout, trust_env=False)

    ca_file = os.environ.get("REDFISH_CA_FILE", "").strip()
    if not ca_file:
        raise RuntimeError(
            f"BMC {credentials.address}: refusing Redfish without TLS verification; set "
            "REDFISH_CA_FILE or a pinned cert_fingerprint for this BMC"
        )
    if not os.path.isfile(ca_file):
        raise RuntimeError(f"REDFISH_CA_FILE {ca_file!r} does not exist")
    try:
        ctx = ssl.create_default_context(cafile=ca_file)
    except (ssl.SSLError, OSError) as exc:
        raise RuntimeError(
            f"REDFISH_CA_FILE {ca_file!r} is not a usable CA bundle: {exc}"
        ) from exc
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return httpx.AsyncClient(verify=ctx, timeout=timeout, trust_env=False)


def _ipmi_base_cmd(credentials: BMCCredentials, *args: str) -> list[str]:
    """Build an ipmitool lanplus command; `-E` makes ipmitool read the password from IPMI_PASSWORD."""
    return [
        "ipmitool",
        "-I",
        "lanplus",
        "-H",
        credentials.address,
        "-U",
        credentials.username,
        "-E",
        *args,
    ]


def _ipmi_env(credentials: BMCCredentials) -> dict[str, str]:
    """Minimal child environment for ipmitool -E: the BMC password only, never the worker's own secrets."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
    env["IPMI_PASSWORD"] = credentials.password
    return env


class PowerManager:
    """Manages power operations for bare metal machines."""

    def __init__(self):
        pass

    async def power_control(
        self,
        credentials: BMCCredentials,
        action: PowerAction,
    ) -> tuple[bool, str]:
        """
        Execute power control action.

        Returns:
            (success: bool, message: str)
        """
        if credentials.power_type == "ipmi" and credentials.address.startswith("-"):
            return False, "Invalid BMC address"  # would be parsed as an ipmitool option
        if credentials.power_type == "ipmi":
            return await self._ipmi_control(credentials, action)
        elif credentials.power_type == "redfish":
            return await self._redfish_control(credentials, action)
        elif credentials.power_type == "wol":
            return await self._wol_control(credentials, action)
        else:
            return False, f"Unsupported power type: {credentials.power_type}"

    async def _ipmi_control(
        self,
        credentials: BMCCredentials,
        action: PowerAction,
    ) -> tuple[bool, str]:
        """Execute IPMI power control command."""
        # Map actions to ipmitool commands
        action_map = {
            "on": "power on",
            "off": "power off",
            "cycle": "power cycle",
            "reset": "power reset",
            "status": "power status",
        }

        ipmi_cmd = action_map.get(action)
        if not ipmi_cmd:
            return False, f"Invalid IPMI action: {action}"

        # Build ipmitool command; password travels in IPMI_PASSWORD (-E), never in argv
        cmd = _ipmi_base_cmd(credentials, *ipmi_cmd.split())

        try:
            logger.info(
                "ipmi_command_executing",
                address=credentials.address,
                action=action,
            )

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_ipmi_env(credentials),
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30.0,
            )

            if process.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore").strip()
                logger.info(
                    "ipmi_command_success",
                    address=credentials.address,
                    action=action,
                    output=output,
                )
                return True, output
            else:
                error = stderr.decode("utf-8", errors="ignore").strip()
                logger.error(
                    "ipmi_command_failed",
                    address=credentials.address,
                    action=action,
                    error=error,
                )
                return False, error

        except asyncio.TimeoutError:
            logger.error(
                "ipmi_command_timeout",
                address=credentials.address,
                action=action,
            )
            return False, "IPMI command timeout"
        except FileNotFoundError:
            logger.error("ipmitool_not_found")
            return False, "ipmitool not installed"
        except Exception as e:
            logger.error(
                "ipmi_command_exception",
                address=credentials.address,
                action=action,
                error=str(e),
            )
            return False, str(e)

    async def _redfish_control(
        self,
        credentials: BMCCredentials,
        action: PowerAction,
    ) -> tuple[bool, str]:
        """Execute Redfish power control via REST API with certificate verification."""
        # Map actions to Redfish reset types
        action_map = {
            "on": "On",
            "off": "ForceOff",
            "cycle": "ForceRestart",
            "reset": "ForceRestart",
        }

        reset_type = action_map.get(action)
        if not reset_type:
            if action == "status":
                return await self._redfish_get_status(credentials)
            return False, f"Invalid Redfish action: {action}"

        # Redfish API endpoint
        redfish_url = f"https://{credentials.address}/redfish/v1/Systems/1/Actions/ComputerSystem.Reset"

        payload = {"ResetType": reset_type}

        try:
            logger.info(
                "redfish_command_executing",
                address=credentials.address,
                action=action,
            )

            async with _build_redfish_client(credentials, 30.0) as client:
                response = await client.post(
                    redfish_url,
                    json=payload,
                    auth=(credentials.username, credentials.password),
                )

                if response.status_code in [200, 202, 204]:
                    logger.info(
                        "redfish_command_success",
                        address=credentials.address,
                        action=action,
                    )
                    return True, f"Redfish {action} successful"
                else:
                    logger.error(
                        "redfish_command_failed",
                        address=credentials.address,
                        action=action,
                        status=response.status_code,
                        response=response.text,
                    )
                    return False, f"Redfish error: {response.status_code}"

        except Exception as e:
            logger.error(
                "redfish_command_exception",
                address=credentials.address,
                action=action,
                error=str(e),
            )
            return False, str(e)

    async def _redfish_get_status(
        self,
        credentials: BMCCredentials,
    ) -> tuple[bool, str]:
        """Get power status via Redfish with certificate verification."""
        redfish_url = f"https://{credentials.address}/redfish/v1/Systems/1"

        try:
            async with _build_redfish_client(credentials, 10.0) as client:
                response = await client.get(
                    redfish_url,
                    auth=(credentials.username, credentials.password),
                )

                if response.status_code == 200:
                    data = response.json()
                    power_state = data.get("PowerState", "Unknown")
                    return True, power_state
                else:
                    return False, f"Status check failed: {response.status_code}"

        except Exception as e:
            return False, str(e)

    async def _wol_control(
        self,
        credentials: BMCCredentials,
        action: PowerAction,
    ) -> tuple[bool, str]:
        """
        Wake-on-LAN power control.

        Note: WOL can only power ON machines, not OFF.
        """
        if action != "on":
            return False, "Wake-on-LAN only supports power on"

        # credentials.address contains MAC address for WOL
        mac_address = credentials.address
        if not re.fullmatch(r"[0-9A-Fa-f]{2}([:-]?[0-9A-Fa-f]{2}){5}", mac_address):
            return (
                False,
                "Invalid MAC address",
            )  # also blocks option injection into wakeonlan

        try:
            # Send magic packet using wakeonlan utility or manual implementation
            cmd = ["wakeonlan", mac_address]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            await asyncio.wait_for(process.communicate(), timeout=5.0)

            if process.returncode == 0:
                logger.info("wol_packet_sent", mac=mac_address)
                return True, "Wake-on-LAN packet sent"
            else:
                return False, "Wake-on-LAN failed"

        except FileNotFoundError:
            # Fallback to manual magic packet construction
            return await self._send_wol_manual(mac_address)
        except Exception as e:
            logger.error("wol_error", mac=mac_address, error=str(e))
            return False, str(e)

    async def _send_wol_manual(self, mac_address: str) -> tuple[bool, str]:
        """Send Wake-on-LAN magic packet manually."""

        try:
            # Parse MAC address
            mac_bytes = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
            if len(mac_bytes) != 6:
                return False, f"Invalid MAC address length: {len(mac_bytes)} bytes"

            # Build magic packet (6 bytes of FF + 16 repetitions of MAC)
            magic_packet = b"\xff" * 6 + mac_bytes * 16

            # Send UDP broadcast
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic_packet, ("255.255.255.255", 9))
            sock.close()

            logger.info("wol_magic_packet_sent", mac=mac_address)
            return True, "Wake-on-LAN magic packet sent"

        except Exception as e:
            logger.error("wol_manual_error", mac=mac_address, error=str(e))
            return False, str(e)

    async def set_boot_device(
        self,
        credentials: BMCCredentials,
        device: Literal["pxe", "disk", "bios"],
        persistent: bool = False,
    ) -> tuple[bool, str]:
        """
        Set next boot device via IPMI.

        Args:
            credentials: BMC credentials
            device: Boot device (pxe, disk, bios)
            persistent: Make setting persistent across reboots
        """
        if credentials.power_type != "ipmi":
            return False, "Boot device setting only supported via IPMI"
        if credentials.address.startswith("-"):
            return False, "Invalid BMC address"

        # Map device names to IPMI boot device codes
        device_map = {
            "pxe": "pxe",
            "disk": "disk",
            "bios": "bios",
        }

        ipmi_device = device_map.get(device)
        if not ipmi_device:
            return False, f"Invalid boot device: {device}"

        # Build ipmitool command
        persistence = "persistent" if persistent else "options=efiboot"
        cmd = _ipmi_base_cmd(
            credentials, "chassis", "bootdev", ipmi_device, persistence
        )

        try:
            logger.info(
                "ipmi_set_boot_device",
                address=credentials.address,
                device=device,
                persistent=persistent,
            )

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_ipmi_env(credentials),
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30.0,
            )

            if process.returncode == 0:
                logger.info(
                    "ipmi_boot_device_set",
                    address=credentials.address,
                    device=device,
                )
                return True, f"Boot device set to {device}"
            else:
                error = stderr.decode("utf-8", errors="ignore").strip()
                return False, error

        except Exception as e:
            logger.error(
                "ipmi_boot_device_error",
                address=credentials.address,
                error=str(e),
            )
            return False, str(e)
