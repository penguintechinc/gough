"""
iPXE script generation and cloud-init handling.

Queries api-manager for machine state and generates appropriate boot scripts.
"""

import re

import structlog
import httpx

from worker.boot_tokens import DISCOVERY_TOKEN_TTL_SECONDS, BootTokenStore
from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager

logger = structlog.get_logger()

# Canonical 12-hex-digit MAC (after normalisation) and machine IDs (UUID-shaped).
_MAC_RE = re.compile(r"^[0-9a-f]{12}$")
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_CLOUD_INIT_META_FALLBACK = "instance-id: error\nlocal-hostname: unknown\n"
_CLOUD_INIT_USERDATA_FALLBACK = "#cloud-config\n# Error fetching user-data\n"


def _normalize_mac(mac: str) -> str | None:
    """Return the lowercase 12-hex MAC without separators, or None if malformed."""
    normalized = mac.replace(":", "").replace("-", "").lower()
    return normalized if _MAC_RE.fullmatch(normalized) else None


class IPXEHandler:
    """Handles iPXE script generation and cloud-init data."""

    def __init__(
        self,
        config: WorkerConfig,
        enrollment: EnrollmentManager,
        token_store: BootTokenStore | None = None,
    ):
        self.config = config
        self.enrollment = enrollment
        self.token_store = token_store

    async def _embed_boot_token(
        self, script: str, mac: str, source_ip: str | None, discovery: bool
    ) -> str:
        """Mint the MAC's one live token, bound to the requesting IP, as iPXE var ${boot_token}.

        Known machines get the full TTL and every stage; unknown (discovery) machines get
        a shorter TTL and may only report the discovery stage. Without a token store the
        script is returned unchanged. Stronger design (follow-up): api-manager mints the
        token inside the boot script it returns and the worker only verifies it; today
        /internal/boot-script returns a bare script, so the worker mints.
        """
        if self.token_store is None:
            return script
        if not source_ip:
            raise ValueError("cannot bind boot token: client address unknown")
        token = await self.token_store.issue(
            mac,
            source_ip,
            ttl_seconds=DISCOVERY_TOKEN_TTL_SECONDS if discovery else None,
            stages=frozenset({"discovery"}) if discovery else None,
        )
        line = f"set boot_token {token}\n"
        if script.startswith("#!ipxe"):
            head, _, rest = script.partition("\n")
            return f"{head}\n{line}{rest}"
        return f"#!ipxe\n{line}{script}"

    async def generate_script(self, mac: str, source_ip: str | None = None) -> str:
        """
        Generate iPXE boot script for machine by MAC address.

        Queries api-manager for machine state and returns appropriate script.
        """
        # Normalize MAC address and reject anything that is not 12 hex digits
        # (the value is interpolated into a URL path and an iPXE script)
        mac_normalized = _normalize_mac(mac)
        if mac_normalized is None:
            logger.warning("ipxe_invalid_mac", mac_length=len(mac))
            return self._generate_error_script("Invalid MAC address")

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

                    return await self._embed_boot_token(script, mac_normalized, source_ip, discovery=False)

                elif response.status_code == 404:
                    # Unknown machine - return discovery script
                    logger.info("unknown_machine_discovered", mac=mac_normalized)
                    return await self._embed_boot_token(
                        self._generate_discovery_script(mac_normalized),
                        mac_normalized,
                        source_ip,
                        discovery=True,
                    )

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

# Boot discovery image (${{boot_token}} authorises its /boot-event callbacks)
echo Booting discovery image...
kernel {boot_url}/images/discovery/vmlinuz initrd=initrd ip=dhcp gough.boot_token=${{boot_token}} gough.mac={mac}
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
        """Generate error iPXE script; line breaks are flattened so text cannot inject iPXE commands."""
        safe_message = " ".join(error_message.splitlines())
        script = f"""#!ipxe
echo ======================================
echo Gough Provisioning - Error
echo ======================================
echo {safe_message}
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
        if not _MACHINE_ID_RE.fullmatch(machine_id):
            logger.warning("cloud_init_invalid_machine_id", machine_id_length=len(machine_id))
            return _CLOUD_INIT_META_FALLBACK
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
                    return _CLOUD_INIT_META_FALLBACK

        except Exception as e:
            logger.error("cloud_init_metadata_error", machine_id=machine_id, error=str(e))
            return _CLOUD_INIT_META_FALLBACK

    async def get_cloud_init_userdata(self, machine_id: str) -> str:
        """
        Fetch cloud-init user-data for machine.

        Returns merged cloud-init YAML with all biomes applied.
        """
        if not _MACHINE_ID_RE.fullmatch(machine_id):
            logger.warning("cloud_init_invalid_machine_id", machine_id_length=len(machine_id))
            return _CLOUD_INIT_USERDATA_FALLBACK
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
                    return _CLOUD_INIT_USERDATA_FALLBACK

        except Exception as e:
            logger.error("cloud_init_userdata_error", machine_id=machine_id, error=str(e))
            return _CLOUD_INIT_USERDATA_FALLBACK
