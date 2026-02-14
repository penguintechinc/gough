"""Main entry point for Gough Access Agent.

Orchestrates enrollment, heartbeat, and rssh server components.
"""

import asyncio
import logging
import signal
import sys
from typing import Any, Dict

from .auth import AgentAuth
from .cert_validator import CertificateValidator
from .config import AgentConfig
from .enrollment import AgentEnrollment, EnrollmentError
from .heartbeat import HeartbeatService
from .rssh_server import RSSHServer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

log = logging.getLogger(__name__)


class GoughAgent:
    """Main Gough Access Agent class."""

    def __init__(self, config: AgentConfig):
        """Initialize agent.

        Args:
            config: Agent configuration
        """
        self.config = config
        self.enrollment: AgentEnrollment = None
        self.auth: AgentAuth = None
        self.heartbeat: HeartbeatService = None
        self.rssh_server: RSSHServer = None
        self.cert_validator: CertificateValidator = None
        self._running = False

    async def start(self) -> None:
        """Start the agent."""
        log.info(f"Starting Gough Access Agent on {self.config.hostname}")

        # Ensure directories exist
        self.config.ensure_directories()

        # Initialize enrollment handler
        self.enrollment = AgentEnrollment(self.config)

        # Check enrollment status
        if self.enrollment.is_enrolled():
            log.info("Agent already enrolled, loading tokens")
            self.enrollment.load_tokens()
        else:
            log.info("Agent not enrolled, starting enrollment")
            try:
                self.enrollment.enroll()
            except EnrollmentError as e:
                log.critical(f"Enrollment failed: {e}")
                raise SystemExit(1)

        # Initialize auth handler
        self.auth = AgentAuth(self.config, self.enrollment)

        # Initialize certificate validator
        if self.config.ca_public_key:
            self.cert_validator = CertificateValidator(self.config.ca_public_key)
        else:
            log.warning("No CA public key, certificate validation disabled")

        # Initialize heartbeat service
        self.heartbeat = HeartbeatService(
            config=self.config,
            auth=self.auth,
            command_handler=self._handle_server_command,
        )

        # Initialize rssh server
        if self.cert_validator:
            self.rssh_server = RSSHServer(
                config=self.config,
                cert_validator=self.cert_validator,
                on_session_start=self._on_session_start,
                on_session_end=self._on_session_end,
            )

        # Start services
        self._running = True

        # Start heartbeat
        await self.heartbeat.start()

        # Start rssh server (runs in thread)
        if self.rssh_server:
            self.rssh_server.start()

        log.info("Gough Access Agent started successfully")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the agent."""
        log.info("Stopping Gough Access Agent")
        self._running = False

        # Stop rssh server
        if self.rssh_server:
            self.rssh_server.stop()

        # Stop heartbeat
        if self.heartbeat:
            await self.heartbeat.stop()

        # Close enrollment client
        if self.enrollment:
            self.enrollment.close()

        # Cleanup certificate validator
        if self.cert_validator:
            self.cert_validator.cleanup()

        log.info("Gough Access Agent stopped")

    def _handle_server_command(self, command: Dict[str, Any]) -> None:
        """Handle command from management server.

        Args:
            command: Command dictionary
        """
        cmd_type = command.get("type")

        if cmd_type == "reload_config":
            log.info("Received reload_config command")
            # Reload configuration

        elif cmd_type == "terminate_session":
            session_id = command.get("session_id")
            log.info(f"Received terminate_session command for {session_id}")
            # Terminate specific session

        elif cmd_type == "shutdown":
            log.info("Received shutdown command")
            self._running = False

        else:
            log.warning(f"Unknown command type: {cmd_type}")

    def _on_session_start(self, session_id: str) -> None:
        """Called when a shell session starts.

        Args:
            session_id: Session identifier
        """
        if self.heartbeat:
            count = self.rssh_server.get_active_session_count()
            self.heartbeat.set_active_sessions(count)

        log.info(f"Shell session started: {session_id}")

    def _on_session_end(self, session_id: str) -> None:
        """Called when a shell session ends.

        Args:
            session_id: Session identifier
        """
        if self.heartbeat:
            count = self.rssh_server.get_active_session_count()
            self.heartbeat.set_active_sessions(count)

        log.info(f"Shell session ended: {session_id}")


async def main() -> None:
    """Main entry point."""
    # Load configuration
    config = AgentConfig.from_env()

    # Set log level
    logging.getLogger().setLevel(config.log_level)

    # Create agent
    agent = GoughAgent(config)

    # Handle signals
    loop = asyncio.get_event_loop()

    def signal_handler():
        log.info("Received shutdown signal")
        asyncio.create_task(agent.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await agent.start()
    except Exception as e:
        log.critical(f"Agent failed: {e}")
        raise SystemExit(1)
    finally:
        await agent.stop()


def run() -> None:
    """Entry point for console script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
