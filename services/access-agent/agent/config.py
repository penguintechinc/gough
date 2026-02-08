"""Configuration management for Gough Access Agent."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass(slots=True)
class AgentConfig:
    """Access Agent configuration."""

    # Management server connection
    management_server_url: str = "https://gough.local"
    enrollment_key: Optional[str] = None

    # Agent identity
    agent_id: Optional[str] = None
    hostname: str = field(default_factory=lambda: os.uname().nodename)

    # JWT tokens (populated after enrollment)
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_file: str = "/var/lib/gough-agent/tokens.json"

    # SSH CA
    ca_public_key: Optional[str] = None
    ca_public_key_file: str = "/var/lib/gough-agent/ca.pub"

    # rssh server settings
    rssh_listen_host: str = "0.0.0.0"
    rssh_listen_port: int = 2222
    host_key_file: str = "/var/lib/gough-agent/host_key"

    # Capabilities
    capabilities: List[str] = field(default_factory=lambda: ["ssh"])

    # Heartbeat settings
    heartbeat_interval: int = 30  # seconds
    heartbeat_timeout: int = 10   # seconds

    # TLS settings
    verify_ssl: bool = True
    ca_cert_file: Optional[str] = None

    # Logging
    log_level: str = "INFO"
    log_file: Optional[str] = "/var/log/gough-agent/agent.log"

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Load configuration from environment variables."""
        return cls(
            management_server_url=os.getenv(
                "GOUGH_MANAGEMENT_SERVER", "https://gough.local"
            ),
            enrollment_key=os.getenv("GOUGH_ENROLLMENT_KEY"),
            hostname=os.getenv("GOUGH_HOSTNAME", os.uname().nodename),
            rssh_listen_host=os.getenv("GOUGH_RSSH_HOST", "0.0.0.0"),
            rssh_listen_port=int(os.getenv("GOUGH_RSSH_PORT", "2222")),
            heartbeat_interval=int(os.getenv("GOUGH_HEARTBEAT_INTERVAL", "30")),
            verify_ssl=os.getenv("GOUGH_VERIFY_SSL", "true").lower() == "true",
            log_level=os.getenv("GOUGH_LOG_LEVEL", "INFO"),
            capabilities=os.getenv(
                "GOUGH_CAPABILITIES", "ssh"
            ).split(","),
        )

    @classmethod
    def from_file(cls, config_file: str) -> "AgentConfig":
        """Load configuration from YAML file."""
        path = Path(config_file)
        if not path.exists():
            return cls.from_env()

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls(
            management_server_url=data.get(
                "management_server_url",
                os.getenv("GOUGH_MANAGEMENT_SERVER", "https://gough.local")
            ),
            enrollment_key=data.get(
                "enrollment_key",
                os.getenv("GOUGH_ENROLLMENT_KEY")
            ),
            hostname=data.get("hostname", os.uname().nodename),
            rssh_listen_host=data.get("rssh_listen_host", "0.0.0.0"),
            rssh_listen_port=data.get("rssh_listen_port", 2222),
            heartbeat_interval=data.get("heartbeat_interval", 30),
            verify_ssl=data.get("verify_ssl", True),
            log_level=data.get("log_level", "INFO"),
            capabilities=data.get("capabilities", ["ssh"]),
        )

    def ensure_directories(self) -> None:
        """Ensure required directories exist."""
        for path_str in [
            self.token_file,
            self.ca_public_key_file,
            self.host_key_file,
            self.log_file,
        ]:
            if path_str:
                Path(path_str).parent.mkdir(parents=True, exist_ok=True)
