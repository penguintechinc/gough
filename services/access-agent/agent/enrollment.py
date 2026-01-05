"""Agent enrollment with Gough management server.

Handles initial enrollment using one-time enrollment key,
then switches to JWT tokens for ongoing authentication.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import httpx

from .config import AgentConfig

log = logging.getLogger(__name__)


class EnrollmentError(Exception):
    """Raised when agent enrollment fails."""
    pass


class AgentEnrollment:
    """Handles agent enrollment with management server."""

    def __init__(self, config: AgentConfig):
        """Initialize enrollment handler.

        Args:
            config: Agent configuration
        """
        self.config = config
        self._http_client: Optional[httpx.Client] = None

    @property
    def http_client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.Client(
                base_url=self.config.management_server_url,
                verify=self.config.verify_ssl,
                timeout=30.0,
            )
        return self._http_client

    def is_enrolled(self) -> bool:
        """Check if agent is already enrolled.

        Returns:
            True if valid tokens exist
        """
        token_path = Path(self.config.token_file)
        if not token_path.exists():
            return False

        try:
            with open(token_path) as f:
                tokens = json.load(f)

            return bool(
                tokens.get("access_token")
                and tokens.get("refresh_token")
                and tokens.get("agent_id")
            )
        except (json.JSONDecodeError, IOError):
            return False

    def load_tokens(self) -> bool:
        """Load tokens from file into config.

        Returns:
            True if tokens loaded successfully
        """
        token_path = Path(self.config.token_file)
        if not token_path.exists():
            return False

        try:
            with open(token_path) as f:
                tokens = json.load(f)

            self.config.agent_id = tokens.get("agent_id")
            self.config.access_token = tokens.get("access_token")
            self.config.refresh_token = tokens.get("refresh_token")
            self.config.ca_public_key = tokens.get("ca_public_key")

            log.info(f"Loaded tokens for agent {self.config.agent_id}")
            return True

        except (json.JSONDecodeError, IOError) as e:
            log.error(f"Failed to load tokens: {e}")
            return False

    def save_tokens(self) -> None:
        """Save tokens to file."""
        token_path = Path(self.config.token_file)
        token_path.parent.mkdir(parents=True, exist_ok=True)

        tokens = {
            "agent_id": self.config.agent_id,
            "access_token": self.config.access_token,
            "refresh_token": self.config.refresh_token,
            "ca_public_key": self.config.ca_public_key,
            "saved_at": datetime.utcnow().isoformat(),
        }

        with open(token_path, "w") as f:
            json.dump(tokens, f, indent=2)

        # Secure file permissions
        token_path.chmod(0o600)

        log.info(f"Saved tokens to {token_path}")

    def enroll(self) -> Tuple[str, str, str]:
        """Enroll agent with management server.

        Uses enrollment key for initial authentication, receives
        JWT access and refresh tokens in response.

        Returns:
            Tuple of (agent_id, access_token, refresh_token)

        Raises:
            EnrollmentError: If enrollment fails
        """
        if not self.config.enrollment_key:
            raise EnrollmentError("No enrollment key configured")

        log.info(
            f"Enrolling agent {self.config.hostname} with "
            f"{self.config.management_server_url}"
        )

        try:
            response = self.http_client.post(
                "/api/v1/agents/enroll",
                headers={
                    "X-Enrollment-Key": self.config.enrollment_key,
                    "Content-Type": "application/json",
                },
                json={
                    "hostname": self.config.hostname,
                    "ip_address": self._get_primary_ip(),
                    "agent_version": "1.0.0",
                    "capabilities": self.config.capabilities,
                },
            )

            if response.status_code == 401:
                raise EnrollmentError("Invalid or expired enrollment key")

            if response.status_code == 409:
                raise EnrollmentError("Enrollment key already used")

            if response.status_code != 201:
                error_msg = response.json().get("error", "Unknown error")
                raise EnrollmentError(f"Enrollment failed: {error_msg}")

            data = response.json()

            # Store enrollment response
            self.config.agent_id = data["agent_id"]
            self.config.access_token = data["access_token"]
            self.config.refresh_token = data["refresh_token"]
            self.config.ca_public_key = data.get("ca_public_key")

            # Apply server-provided config if present
            if "config" in data:
                server_config = data["config"]
                if "heartbeat_interval" in server_config:
                    self.config.heartbeat_interval = server_config["heartbeat_interval"]

            # Save tokens
            self.save_tokens()

            # Save CA public key
            if self.config.ca_public_key:
                self._save_ca_public_key()

            log.info(f"Successfully enrolled as agent {self.config.agent_id}")

            return (
                self.config.agent_id,
                self.config.access_token,
                self.config.refresh_token,
            )

        except httpx.RequestError as e:
            raise EnrollmentError(f"Network error during enrollment: {e}")

    def refresh_tokens(self) -> Tuple[str, str]:
        """Refresh JWT tokens.

        Uses refresh token to obtain new access and refresh tokens.

        Returns:
            Tuple of (access_token, refresh_token)

        Raises:
            EnrollmentError: If refresh fails
        """
        if not self.config.refresh_token:
            raise EnrollmentError("No refresh token available")

        log.debug("Refreshing JWT tokens")

        try:
            response = self.http_client.post(
                "/api/v1/agents/refresh",
                headers={
                    "Authorization": f"Bearer {self.config.refresh_token}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code == 401:
                raise EnrollmentError("Refresh token expired or revoked")

            if response.status_code != 200:
                error_msg = response.json().get("error", "Unknown error")
                raise EnrollmentError(f"Token refresh failed: {error_msg}")

            data = response.json()

            self.config.access_token = data["access_token"]
            self.config.refresh_token = data["refresh_token"]

            # Save updated tokens
            self.save_tokens()

            log.info("Successfully refreshed JWT tokens")

            return (
                self.config.access_token,
                self.config.refresh_token,
            )

        except httpx.RequestError as e:
            raise EnrollmentError(f"Network error during token refresh: {e}")

    def _get_primary_ip(self) -> Optional[str]:
        """Get primary IP address of this host."""
        import socket

        try:
            # Connect to external address to determine primary interface
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return None

    def _save_ca_public_key(self) -> None:
        """Save CA public key to file."""
        if not self.config.ca_public_key:
            return

        ca_path = Path(self.config.ca_public_key_file)
        ca_path.parent.mkdir(parents=True, exist_ok=True)
        ca_path.write_text(self.config.ca_public_key)
        ca_path.chmod(0o644)

        log.info(f"Saved CA public key to {ca_path}")

    def close(self) -> None:
        """Close HTTP client."""
        if self._http_client:
            self._http_client.close()
            self._http_client = None
