"""
Configuration management for worker-ipxe.

Loads configuration from environment variables with validation.
"""

import os
from dataclasses import dataclass
from typing import Optional
from decouple import config


@dataclass(slots=True)
class WorkerConfig:
    """Worker-iPXE configuration."""

    # API Manager connection
    api_manager_url: str
    worker_api_key: str
    worker_id: str

    # DHCP configuration
    dhcp_mode: str  # full, proxy, disabled
    dhcp_interface: str
    dhcp_subnet: Optional[str] = None
    dhcp_range_start: Optional[str] = None
    dhcp_range_end: Optional[str] = None
    dhcp_gateway: Optional[str] = None
    dhcp_dns_servers: str = "8.8.8.8,8.8.4.4"

    # TFTP configuration
    tftp_enabled: bool = True
    tftp_port: int = 69
    tftp_root: str = "/var/lib/ipxe/tftp"

    # HTTP boot server
    http_port: int = 8080
    http_boot_url: Optional[str] = None

    # Storage (MinIO/S3)
    storage_endpoint: Optional[str] = None
    storage_access_key: Optional[str] = None
    storage_secret_key: Optional[str] = None
    storage_bucket_boot: str = "boot-images"
    storage_bucket_eggs: str = "eggs"
    storage_use_ssl: bool = False

    # Worker behavior
    heartbeat_interval: int = 30  # seconds
    enrollment_retry_interval: int = 10  # seconds
    max_enrollment_retries: int = 60

    # Logging
    log_level: str = "INFO"

    # Metrics
    metrics_enabled: bool = True
    metrics_port: int = 9090

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        """Load configuration from environment variables."""

        # Required settings
        api_manager_url = config("API_MANAGER_URL", default="http://api-manager:5000")
        worker_api_key = config("WORKER_API_KEY", default="worker-secret")
        worker_id = config("WORKER_ID", default=os.environ.get("HOSTNAME", "worker-ipxe-1"))

        # DHCP settings
        dhcp_mode = config("DHCP_MODE", default="proxy")
        if dhcp_mode not in ["full", "proxy", "disabled"]:
            raise ValueError(f"Invalid DHCP_MODE: {dhcp_mode}. Must be full, proxy, or disabled.")

        dhcp_interface = config("DHCP_INTERFACE", default="eth0")

        # Optional DHCP settings (required for full mode)
        dhcp_subnet = config("DHCP_SUBNET", default=None)
        dhcp_range_start = config("DHCP_RANGE_START", default=None)
        dhcp_range_end = config("DHCP_RANGE_END", default=None)
        dhcp_gateway = config("DHCP_GATEWAY", default=None)
        dhcp_dns_servers = config("DHCP_DNS_SERVERS", default="8.8.8.8,8.8.4.4")

        # Validate full DHCP mode requirements
        if dhcp_mode == "full":
            if not all([dhcp_subnet, dhcp_range_start, dhcp_range_end, dhcp_gateway]):
                raise ValueError(
                    "DHCP_MODE=full requires DHCP_SUBNET, DHCP_RANGE_START, "
                    "DHCP_RANGE_END, and DHCP_GATEWAY to be set."
                )

        # TFTP settings
        tftp_enabled = config("TFTP_ENABLED", default=True, cast=bool)
        tftp_port = config("TFTP_PORT", default=69, cast=int)
        tftp_root = config("TFTP_ROOT", default="/var/lib/ipxe/tftp")

        # HTTP boot settings
        http_port = config("HTTP_PORT", default=8080, cast=int)
        http_boot_url = config("HTTP_BOOT_URL", default=None)

        # Storage settings
        storage_endpoint = config("STORAGE_ENDPOINT", default=None)
        storage_access_key = config("STORAGE_ACCESS_KEY", default=None)
        storage_secret_key = config("STORAGE_SECRET_KEY", default=None)
        storage_bucket_boot = config("STORAGE_BUCKET_BOOT_IMAGES", default="boot-images")
        storage_bucket_eggs = config("STORAGE_BUCKET_EGGS", default="eggs")
        storage_use_ssl = config("STORAGE_USE_SSL", default=False, cast=bool)

        # Worker behavior
        heartbeat_interval = config("HEARTBEAT_INTERVAL", default=30, cast=int)
        enrollment_retry_interval = config("ENROLLMENT_RETRY_INTERVAL", default=10, cast=int)
        max_enrollment_retries = config("MAX_ENROLLMENT_RETRIES", default=60, cast=int)

        # Logging
        log_level = config("LOG_LEVEL", default="INFO")

        # Metrics
        metrics_enabled = config("METRICS_ENABLED", default=True, cast=bool)
        metrics_port = config("METRICS_PORT", default=9090, cast=int)

        return cls(
            api_manager_url=api_manager_url,
            worker_api_key=worker_api_key,
            worker_id=worker_id,
            dhcp_mode=dhcp_mode,
            dhcp_interface=dhcp_interface,
            dhcp_subnet=dhcp_subnet,
            dhcp_range_start=dhcp_range_start,
            dhcp_range_end=dhcp_range_end,
            dhcp_gateway=dhcp_gateway,
            dhcp_dns_servers=dhcp_dns_servers,
            tftp_enabled=tftp_enabled,
            tftp_port=tftp_port,
            tftp_root=tftp_root,
            http_port=http_port,
            http_boot_url=http_boot_url,
            storage_endpoint=storage_endpoint,
            storage_access_key=storage_access_key,
            storage_secret_key=storage_secret_key,
            storage_bucket_boot=storage_bucket_boot,
            storage_bucket_eggs=storage_bucket_eggs,
            storage_use_ssl=storage_use_ssl,
            heartbeat_interval=heartbeat_interval,
            enrollment_retry_interval=enrollment_retry_interval,
            max_enrollment_retries=max_enrollment_retries,
            log_level=log_level,
            metrics_enabled=metrics_enabled,
            metrics_port=metrics_port,
        )

    def get_boot_url(self) -> str:
        """Get the HTTP boot base URL."""
        if self.http_boot_url:
            return self.http_boot_url

        # Construct from interface IP if possible
        try:
            import netifaces
            addrs = netifaces.ifaddresses(self.dhcp_interface)
            if netifaces.AF_INET in addrs:
                ip = addrs[netifaces.AF_INET][0]['addr']
                return f"http://{ip}:{self.http_port}"
        except Exception:
            pass

        # Fallback to localhost (not ideal for production)
        return f"http://localhost:{self.http_port}"
