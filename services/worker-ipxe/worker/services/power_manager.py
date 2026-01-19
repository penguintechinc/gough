"""
Power management for bare metal machines.

Supports IPMI, Redfish, and Wake-on-LAN for remote power control.
"""

import asyncio
import structlog
import subprocess
from typing import Optional, Literal
from dataclasses import dataclass

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

        # Build ipmitool command
        cmd = [
            "ipmitool",
            "-I", "lanplus",
            "-H", credentials.address,
            "-U", credentials.username,
            "-P", credentials.password,
            *ipmi_cmd.split(),
        ]

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
        """Execute Redfish power control via REST API."""
        import httpx

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

            async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
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
        """Get power status via Redfish."""
        import httpx

        redfish_url = f"https://{credentials.address}/redfish/v1/Systems/1"

        try:
            async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
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
        import socket

        try:
            # Parse MAC address
            mac_bytes = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))

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
        cmd = [
            "ipmitool",
            "-I", "lanplus",
            "-H", credentials.address,
            "-U", credentials.username,
            "-P", credentials.password,
            "chassis", "bootdev", ipmi_device, persistence,
        ]

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
