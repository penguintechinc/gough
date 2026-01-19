"""
Worker-iPXE main entry point.

Starts all services (DHCP, TFTP, HTTP) and manages lifecycle.
"""

import asyncio
import logging
import signal
import sys
import structlog
from prometheus_client import start_http_server

from worker.config import WorkerConfig
from worker.enrollment import EnrollmentManager
from worker.heartbeat import HeartbeatManager
from worker.services.tftp_server import TFTPServer
from worker.services.http_server import HTTPBootServer
from worker.services.dhcp_proxy import DHCPProxyServer
from worker.services.dhcp_server import DHCPFullServer

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


class WorkerIPXE:
    """Main worker application coordinator."""

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.enrollment = EnrollmentManager(config)
        self.heartbeat = HeartbeatManager(config, self.enrollment)

        # Services
        self.tftp_server: TFTPServer = None
        self.http_server: HTTPBootServer = None
        self.dhcp_server = None  # DHCPProxyServer or DHCPFullServer

        self.running = False
        self.tasks = []

    async def start(self):
        """Start all worker services."""
        logger.info(
            "worker_starting",
            worker_id=self.config.worker_id,
            dhcp_mode=self.config.dhcp_mode,
        )

        # Enroll with api-manager
        logger.info("enrolling_with_api_manager")
        if not await self.enrollment.enroll_with_retry():
            logger.error("enrollment_failed_exiting")
            sys.exit(1)

        # Start Prometheus metrics server
        if self.config.metrics_enabled:
            start_http_server(self.config.metrics_port)
            logger.info("prometheus_metrics_enabled", port=self.config.metrics_port)

        # Start heartbeat
        await self.heartbeat.start()

        # Start TFTP server
        if self.config.tftp_enabled:
            self.tftp_server = TFTPServer(self.config)
            tftp_task = asyncio.create_task(self.tftp_server.start())
            self.tasks.append(tftp_task)
            logger.info("tftp_server_started", port=self.config.tftp_port)

        # Start HTTP boot server
        self.http_server = HTTPBootServer(self.config, self.enrollment)
        http_task = asyncio.create_task(self.http_server.start())
        self.tasks.append(http_task)
        logger.info("http_boot_server_started", port=self.config.http_port)

        # Start DHCP service based on mode
        if self.config.dhcp_mode == "proxy":
            self.dhcp_server = DHCPProxyServer(self.config)
            dhcp_task = asyncio.create_task(self.dhcp_server.start())
            self.tasks.append(dhcp_task)
            logger.info("dhcp_proxy_server_started")
        elif self.config.dhcp_mode == "full":
            self.dhcp_server = DHCPFullServer(self.config)
            dhcp_task = asyncio.create_task(self.dhcp_server.start())
            self.tasks.append(dhcp_task)
            logger.info("dhcp_full_server_started")
        else:
            logger.info("dhcp_disabled")

        self.running = True
        logger.info("worker_started_all_services_running")

    async def stop(self):
        """Stop all worker services."""
        logger.info("worker_stopping")
        self.running = False

        # Stop heartbeat
        await self.heartbeat.stop()

        # Stop DHCP server
        if self.dhcp_server:
            await self.dhcp_server.stop()

        # Stop HTTP server
        if self.http_server:
            await self.http_server.stop()

        # Stop TFTP server
        if self.tftp_server:
            await self.tftp_server.stop()

        # Cancel all tasks
        for task in self.tasks:
            task.cancel()

        await asyncio.gather(*self.tasks, return_exceptions=True)

        logger.info("worker_stopped")

    async def run(self):
        """Run worker until shutdown signal."""
        await self.start()

        # Wait for shutdown signal
        stop_event = asyncio.Event()

        def signal_handler():
            logger.info("shutdown_signal_received")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)

        # Wait for stop event
        await stop_event.wait()

        # Cleanup
        await self.stop()


async def main():
    """Main entry point."""
    import logging

    # Load configuration
    try:
        config = WorkerConfig.from_env()
    except ValueError as e:
        logger.error("configuration_error", error=str(e))
        sys.exit(1)

    # Set log level
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
    )

    # Create and run worker
    worker = WorkerIPXE(config)
    try:
        await worker.run()
    except Exception as e:
        logger.error("worker_fatal_error", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
