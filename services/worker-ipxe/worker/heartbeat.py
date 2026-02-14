"""
Worker heartbeat manager.

Sends periodic heartbeats to api-manager to report health and status.
"""

import asyncio
import structlog
import httpx
from datetime import datetime

from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager

logger = structlog.get_logger()


class HeartbeatManager:
    """Manages periodic heartbeats to api-manager."""

    def __init__(self, config: WorkerConfig, enrollment: EnrollmentManager):
        self.config = config
        self.enrollment = enrollment
        self.running = False
        self.task: asyncio.Task = None
        self.last_heartbeat: datetime = None
        self.consecutive_failures = 0

    async def send_heartbeat(self) -> bool:
        """
        Send heartbeat to api-manager.

        Returns:
            True if heartbeat successful, False otherwise.
        """
        heartbeat_url = f"{self.config.api_manager_url}/api/v1/workers/heartbeat"

        payload = {
            "worker_id": self.config.worker_id,
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "services": {
                "dhcp": self.config.dhcp_mode != "disabled",
                "tftp": self.config.tftp_enabled,
                "http": True,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    heartbeat_url,
                    json=payload,
                    headers=self.enrollment.get_auth_headers(),
                )

                if response.status_code == 200:
                    self.last_heartbeat = datetime.utcnow()
                    self.consecutive_failures = 0
                    logger.debug(
                        "heartbeat_sent",
                        worker_id=self.config.worker_id,
                    )
                    return True
                else:
                    self.consecutive_failures += 1
                    logger.warning(
                        "heartbeat_failed",
                        status_code=response.status_code,
                        consecutive_failures=self.consecutive_failures,
                    )
                    return False

        except httpx.ConnectError:
            self.consecutive_failures += 1
            logger.warning(
                "heartbeat_connection_error",
                consecutive_failures=self.consecutive_failures,
            )
            return False
        except Exception as e:
            self.consecutive_failures += 1
            logger.error(
                "heartbeat_exception",
                error=str(e),
                consecutive_failures=self.consecutive_failures,
            )
            return False

    async def heartbeat_loop(self):
        """Background task that sends periodic heartbeats."""
        logger.info(
            "heartbeat_loop_started",
            interval_seconds=self.config.heartbeat_interval,
        )

        while self.running:
            await self.send_heartbeat()

            # Check if too many consecutive failures (potential re-enrollment needed)
            if self.consecutive_failures >= 5:
                logger.error(
                    "heartbeat_consecutive_failures_threshold",
                    failures=self.consecutive_failures,
                    action="attempting_re-enrollment",
                )
                self.enrollment.enrolled = False
                await self.enrollment.enroll_with_retry()
                self.consecutive_failures = 0

            await asyncio.sleep(self.config.heartbeat_interval)

    async def start(self):
        """Start heartbeat background task."""
        if self.running:
            logger.warning("heartbeat_already_running")
            return

        self.running = True
        self.task = asyncio.create_task(self.heartbeat_loop())
        logger.info("heartbeat_started")

    async def stop(self):
        """Stop heartbeat background task."""
        if not self.running:
            return

        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

        logger.info("heartbeat_stopped")
