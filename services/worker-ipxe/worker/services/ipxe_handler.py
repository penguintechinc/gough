"""
iPXE script generation and cloud-init handling.

Queries api-manager for machine state and generates appropriate boot scripts.
"""

import structlog
import httpx
from typing import Optional

from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager

logger = structlog.get_logger()


class IPXEHandler:
    """Handles iPXE script generation and cloud-init data."""

    def __init__(self, config: WorkerConfig, enrollment: EnrollmentManager):
        self.config = config
        self.enrollment = enrollment

    async def generate_script(self, mac: str) -> str:
        """
        Generate iPXE boot script for machine by MAC address.

        Queries api-manager for machine state and returns appropriate script.
        """
        # Normalize MAC address (remove colons, dashes, uppercase)
        mac_normalized = mac.replace(":", "").replace("-", "").lower()

        # Query api-manager for boot script
        api_url = f"{self.config.api_manager_url}/api/v1/internal/boot-script/{mac_normalized}"
        headers = self.enrollment.get_auth_headers()

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(api_url, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    script = data.get("script", "")

                    logger.info(
                        "ipxe_script_generated",
                        mac=mac_normalized,
                        machine_id=data.get("machine_id"),
                        status=data.get("status"),
                    )

                    return script

                elif response.status_code == 404:
                    # Unknown machine - return discovery script
                    logger.info("unknown_machine_discovered", mac=mac_normalized)
                    return self._generate_discovery_script(mac_normalized)

                else:
                    logger.error(
                        "boot_script_fetch_failed",
                        mac=mac_normalized,
                        status=response.status_code,
                    )
                    return self._generate_error_script("API request failed")

        except httpx.ConnectError:
            logger.error("api_manager_unreachable", mac=mac_normalized)
            return self._generate_error_script("API manager unreachable")
        except Exception as e:
            logger.error("ipxe_script_error", mac=mac_normalized, error=str(e))
            return self._generate_error_script(str(e))

    def _generate_discovery_script(self, mac: str) -> str:
        """
        Generate discovery iPXE script for unknown machines.

        Boots into a minimal discovery image that reports hardware info.
        """
        boot_url = self.config.get_boot_url()

        script = f"""#!ipxe
# Discovery script for new machine: {mac}

echo ======================================
echo Gough Provisioning - Machine Discovery
echo ======================================
echo MAC Address: {mac}
echo.

# Report discovery to api-manager
echo Registering machine with provisioning server...
chain {boot_url}/boot-event || goto failed

# Boot discovery image
echo Booting discovery image...
kernel {boot_url}/images/discovery/vmlinuz initrd=initrd ip=dhcp
initrd {boot_url}/images/discovery/initrd
boot || goto failed

:failed
echo.
echo Discovery boot failed. Dropping to iPXE shell.
echo Type 'reboot' to restart or 'exit' to continue booting.
shell
"""
        return script

    def _generate_error_script(self, error_message: str) -> str:
        """Generate error iPXE script."""
        script = f"""#!ipxe
echo ======================================
echo Gough Provisioning - Error
echo ======================================
echo {error_message}
echo.
echo Dropping to iPXE shell.
echo Type 'reboot' to restart.
shell
"""
        return script

    async def get_cloud_init_metadata(self, machine_id: str) -> str:
        """
        Fetch cloud-init metadata for machine.

        Returns YAML metadata for cloud-init datasource.
        """
        api_url = f"{self.config.api_manager_url}/api/v1/internal/cloud-init/{machine_id}/meta-data"
        headers = self.enrollment.get_auth_headers()

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(api_url, headers=headers)

                if response.status_code == 200:
                    return response.text
                else:
                    logger.error(
                        "cloud_init_metadata_fetch_failed",
                        machine_id=machine_id,
                        status=response.status_code,
                    )
                    return "instance-id: error\nlocal-hostname: unknown\n"

        except Exception as e:
            logger.error("cloud_init_metadata_error", machine_id=machine_id, error=str(e))
            return "instance-id: error\nlocal-hostname: unknown\n"

    async def get_cloud_init_userdata(self, machine_id: str) -> str:
        """
        Fetch cloud-init user-data for machine.

        Returns merged cloud-init YAML with all eggs applied.
        """
        api_url = f"{self.config.api_manager_url}/api/v1/internal/cloud-init/{machine_id}/user-data"
        headers = self.enrollment.get_auth_headers()

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(api_url, headers=headers)

                if response.status_code == 200:
                    return response.text
                else:
                    logger.error(
                        "cloud_init_userdata_fetch_failed",
                        machine_id=machine_id,
                        status=response.status_code,
                    )
                    return "#cloud-config\n# Error fetching user-data\n"

        except Exception as e:
            logger.error("cloud_init_userdata_error", machine_id=machine_id, error=str(e))
            return "#cloud-config\n# Error fetching user-data\n"
