"""
Worker enrollment with API manager.

Registers the worker with the central api-manager on startup.
"""

import asyncio
import structlog
import httpx
from typing import Optional

from worker.config import WorkerConfig

logger = structlog.get_logger()


class EnrollmentManager:
    """Manages worker enrollment with api-manager."""

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.enrolled = False
        self.session_token: Optional[str] = None

    async def enroll(self) -> bool:
        """
        Enroll worker with api-manager.

        Returns:
            True if enrollment successful, False otherwise.
        """
        enrollment_url = f"{self.config.api_manager_url}/api/v1/workers/enroll"

        payload = {
            "worker_id": self.config.worker_id,
            "worker_type": "ipxe",
            "capabilities": {
                "dhcp_mode": self.config.dhcp_mode,
                "tftp_enabled": self.config.tftp_enabled,
                "http_boot_enabled": True,
                "power_management": ["ipmi", "redfish", "wol"],
            },
            "network": {
                "interface": self.config.dhcp_interface,
                "http_port": self.config.http_port,
                "tftp_port": self.config.tftp_port,
            },
        }

        headers = {
            "X-Worker-API-Key": self.config.worker_api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    enrollment_url,
                    json=payload,
                    headers=headers,
                )

                if response.status_code == 200:
                    data = response.json()
                    self.session_token = data.get("session_token")
                    self.enrolled = True
                    logger.info(
                        "worker_enrolled",
                        worker_id=self.config.worker_id,
                        session_token=self.session_token[:16] + "..." if self.session_token else None,
                    )
                    return True
                else:
                    logger.error(
                        "enrollment_failed",
                        status_code=response.status_code,
                        response=response.text,
                    )
                    return False

        except httpx.ConnectError:
            logger.warning(
                "enrollment_connection_error",
                api_manager_url=self.config.api_manager_url,
            )
            return False
        except Exception as e:
            logger.error("enrollment_exception", error=str(e))
            return False

    async def enroll_with_retry(self) -> bool:
        """
        Attempt enrollment with exponential backoff.

        Returns:
            True if eventually enrolled, False if max retries exceeded.
        """
        retry_count = 0

        while retry_count < self.config.max_enrollment_retries:
            logger.info(
                "attempting_enrollment",
                attempt=retry_count + 1,
                max_attempts=self.config.max_enrollment_retries,
            )

            if await self.enroll():
                return True

            retry_count += 1
            wait_time = min(
                self.config.enrollment_retry_interval * (2 ** min(retry_count - 1, 5)),
                60,  # Max 60 seconds between retries
            )

            logger.info("enrollment_retry_wait", wait_seconds=wait_time)
            await asyncio.sleep(wait_time)

        logger.error(
            "enrollment_max_retries_exceeded",
            max_retries=self.config.max_enrollment_retries,
        )
        return False

    def get_auth_headers(self) -> dict[str, str]:
        """Get authentication headers for API requests."""
        return {
            "X-Worker-API-Key": self.config.worker_api_key,
            "X-Worker-Session-Token": self.session_token or "",
        }
