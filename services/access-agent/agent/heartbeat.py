"""Heartbeat service for Gough Access Agent.

Periodically sends heartbeat to management server to maintain
active status and receive commands.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

import httpx
import psutil

from .auth import AgentAuth
from .config import AgentConfig

log = logging.getLogger(__name__)


class HeartbeatService:
    """Manages periodic heartbeat to management server."""

    def __init__(
        self,
        config: AgentConfig,
        auth: AgentAuth,
        command_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        """Initialize heartbeat service.

        Args:
            config: Agent configuration
            auth: Authentication handler
            command_handler: Optional callback for server commands
        """
        self.config = config
        self.auth = auth
        self.command_handler = command_handler
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._active_sessions: int = 0
        self._consecutive_failures: int = 0
        self._max_failures: int = 5

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self.config.management_server_url,
                verify=self.config.verify_ssl,
                timeout=self.config.heartbeat_timeout,
            )
        return self._http_client

    def set_active_sessions(self, count: int) -> None:
        """Update active session count.

        Args:
            count: Number of active shell sessions
        """
        self._active_sessions = count

    async def start(self) -> None:
        """Start heartbeat service."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        log.info(
            f"Heartbeat service started (interval: {self.config.heartbeat_interval}s)"
        )

    async def stop(self) -> None:
        """Stop heartbeat service."""
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        log.info("Heartbeat service stopped")

    async def _heartbeat_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await self._send_heartbeat()
                self._consecutive_failures = 0

            except Exception as e:
                self._consecutive_failures += 1
                log.error(
                    f"Heartbeat failed ({self._consecutive_failures}/"
                    f"{self._max_failures}): {e}"
                )

                if self._consecutive_failures >= self._max_failures:
                    log.critical(
                        "Max heartbeat failures reached, agent may be disconnected"
                    )

            await asyncio.sleep(self.config.heartbeat_interval)

    async def _send_heartbeat(self) -> None:
        """Send heartbeat to management server."""
        try:
            headers = self.auth.get_auth_headers()

            # Collect system metrics
            resource_usage = self._get_resource_usage()

            response = await self.http_client.post(
                "/api/v1/agents/heartbeat",
                headers=headers,
                json={
                    "agent_id": self.config.agent_id,
                    "status": "active",
                    "active_sessions": self._active_sessions,
                    "resource_usage": resource_usage,
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )

            if response.status_code == 401:
                log.warning("Heartbeat auth failed, token may need refresh")
                raise Exception("Authentication failed")

            if response.status_code != 200:
                raise Exception(f"Heartbeat returned {response.status_code}")

            data = response.json()

            # Process server commands
            if "commands" in data and self.command_handler:
                for cmd in data["commands"]:
                    try:
                        self.command_handler(cmd)
                    except Exception as e:
                        log.error(f"Error handling command {cmd}: {e}")

            log.debug("Heartbeat sent successfully")

        except httpx.RequestError as e:
            raise Exception(f"Network error: {e}")

    def _get_resource_usage(self) -> Dict[str, Any]:
        """Collect system resource usage metrics."""
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()

            return {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_available_mb": memory.available // (1024 * 1024),
                "connections": len(psutil.net_connections()),
            }
        except Exception as e:
            log.warning(f"Failed to collect resource metrics: {e}")
            return {}
