"""
Full DHCP server for IP assignment and PXE boot.

Provides complete DHCP service (IP allocation + PXE boot info).
Uses dnsmasq wrapper for production-grade DHCP service.
"""

import asyncio
import structlog
import tempfile
from pathlib import Path
from typing import Optional

from worker.config import WorkerConfig

logger = structlog.get_logger()


class DHCPFullServer:
    """Full DHCP server using dnsmasq."""

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.running = False
        self.process: Optional[asyncio.subprocess.Process] = None
        self.config_file: Optional[Path] = None

    def _generate_dnsmasq_config(self) -> str:
        """Generate dnsmasq configuration."""
        boot_url = self.config.get_boot_url()
        dns_servers = self.config.dhcp_dns_servers.split(",")

        config = f"""# Gough DHCP Server Configuration
# Generated automatically - do not edit manually

# Interface binding
interface={self.config.dhcp_interface}
bind-interfaces

# DHCP range
dhcp-range={self.config.dhcp_range_start},{self.config.dhcp_range_end},12h

# Gateway
dhcp-option=3,{self.config.dhcp_gateway}

# DNS servers
dhcp-option=6,{','.join(dns_servers)}

# Domain name
dhcp-option=15,cluster.local

# Disable DNS server (DHCP only)
port=0

# Enable DHCP logging
log-dhcp

# TFTP settings
enable-tftp
tftp-root={self.config.tftp_root}

# PXE boot options
# BIOS clients
dhcp-match=set:bios,option:client-arch,0
dhcp-boot=tag:bios,undionly.kpxe

# UEFI clients
dhcp-match=set:efi-x86_64,option:client-arch,7
dhcp-match=set:efi-x86_64,option:client-arch,9
dhcp-boot=tag:efi-x86_64,ipxe.efi

# HTTP boot URL for iPXE chaining
dhcp-option=tag:!bios,tag:!efi-x86_64,67,"{boot_url}/boot.ipxe"

# PXE menu timeout
dhcp-option=vendor:PXEClient,6,2b

# Lease file
dhcp-leasefile=/tmp/dnsmasq.leases

# PID file
pid-file=/tmp/dnsmasq.pid
"""
        return config

    async def start(self):
        """Start dnsmasq DHCP server."""
        # Generate configuration file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".conf", delete=False, prefix="gough-dhcp-"
        ) as f:
            config_content = self._generate_dnsmasq_config()
            f.write(config_content)
            self.config_file = Path(f.name)

        logger.info(
            "dhcp_config_generated",
            config_file=str(self.config_file),
        )

        # Start dnsmasq process
        try:
            self.process = await asyncio.create_subprocess_exec(
                "dnsmasq",
                "--conf-file", str(self.config_file),
                "--no-daemon",
                "--log-facility=-",  # Log to stderr
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            self.running = True

            logger.info(
                "dhcp_server_started",
                pid=self.process.pid,
                interface=self.config.dhcp_interface,
                range=f"{self.config.dhcp_range_start}-{self.config.dhcp_range_end}",
            )

            # Monitor process output
            asyncio.create_task(self._monitor_output())

            # Wait for process to exit (shouldn't normally happen)
            await self.process.wait()

            if self.running:
                logger.error("dhcp_server_exited_unexpectedly", returncode=self.process.returncode)

        except FileNotFoundError:
            logger.error("dnsmasq_not_found", message="Install dnsmasq package")
            raise
        except Exception as e:
            logger.error("dhcp_server_start_error", error=str(e))
            raise

    async def _monitor_output(self):
        """Monitor and log dnsmasq output."""
        if not self.process or not self.process.stderr:
            return

        try:
            async for line in self.process.stderr:
                log_line = line.decode("utf-8", errors="ignore").strip()
                if log_line:
                    logger.info("dnsmasq_log", message=log_line)
        except Exception as e:
            logger.error("dhcp_log_monitor_error", error=str(e))

    async def stop(self):
        """Stop DHCP server."""
        self.running = False

        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
                logger.info("dhcp_server_stopped")
            except asyncio.TimeoutError:
                logger.warning("dhcp_server_kill_timeout_forcing")
                self.process.kill()
                await self.process.wait()
            except Exception as e:
                logger.error("dhcp_server_stop_error", error=str(e))

        # Clean up config file
        if self.config_file and self.config_file.exists():
            try:
                self.config_file.unlink()
                logger.debug("dhcp_config_file_removed")
            except Exception as e:
                logger.warning("dhcp_config_cleanup_error", error=str(e))
