"""Agent enrollment with Gough management server.

Handles initial enrollment using one-time enrollment key,
then switches to JWT tokens for ongoing authentication.
"""

import json
import logging
import os
import random
import secrets
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

import httpx

from . import __version__
from .config import AgentConfig

log = logging.getLogger(__name__)


class EnrollmentError(Exception):
    """Raised when agent enrollment fails."""
    pass


def _error_message(response: httpx.Response) -> str:
    """Extract a human-readable error from a non-success response.

    Falls back to the raw body (or a generic message) when the body is not a JSON object,
    e.g. a proxy HTML error page, so the caller always gets an EnrollmentError.

    Args:
        response: The failed HTTP response.

    Returns:
        Error text suitable for inclusion in an EnrollmentError message.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:200] or "Unknown error"
    if isinstance(body, dict):
        return str(body.get("error", "Unknown error"))
    return "Unknown error"


# Statuses worth retrying: transient server-side failures and explicit throttling.
# Every other 4xx (notably 401/403/409) is a definitive answer and is never retried.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date).

    Args:
        value: Raw header value, or None.

    Returns:
        Seconds to wait (>= 0), or None when absent/unparseable.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class AgentEnrollment:
    """Handles agent enrollment with management server."""

    def __init__(self, config: AgentConfig):
        """Initialize enrollment handler.

        Args:
            config: Agent configuration
        """
        self.config = config
        self._http_client: Optional[httpx.Client] = None
        # Injectable for tests; jitter is not security-sensitive.
        self._sleep: Callable[[float], None] = time.sleep
        self._monotonic: Callable[[], float] = time.monotonic
        self._uniform: Callable[[float, float], float] = random.uniform  # noqa: S311

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

        # Write a fresh sibling file created 0600 with O_EXCL (never world-readable, never
        # a partial file at the final path), then atomically rename over the target.
        tmp_path = token_path.with_name(f".{token_path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(tokens, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, token_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        log.info(f"Saved tokens to {token_path}")

    def _post_with_retry(self, path: str, **kwargs: Any) -> httpx.Response:
        """POST with bounded exponential backoff and full jitter.

        Retries network errors and 429/500/502/503/504 only; any other status is returned
        immediately. ``Retry-After`` (429/503) is honoured as a minimum delay. Attempts and
        total elapsed time are both bounded by the config.

        Args:
            path: Request path relative to the management server.
            **kwargs: Passed to ``httpx.Client.post``.

        Returns:
            The last response (success, non-retryable, or retries exhausted).

        Raises:
            httpx.RequestError: If the final attempt still fails at the transport level.
        """
        cfg = self.config
        attempts = max(1, cfg.enroll_max_attempts)
        deadline = self._monotonic() + max(0.0, cfg.enroll_max_total_s)
        attempt = 0
        while True:
            attempt += 1
            retry_after: Optional[float] = None
            last: Optional[httpx.Response] = None
            try:
                response = self.http_client.post(path, **kwargs)
            except httpx.RequestError as e:
                if attempt >= attempts:
                    raise
                reason = type(e).__name__
            else:
                if response.status_code not in RETRYABLE_STATUSES or attempt >= attempts:
                    return response
                last = response
                last = response
                reason = f"HTTP {response.status_code}"
                if response.status_code in (429, 503):
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))

            ceiling = min(cfg.enroll_max_delay_s, cfg.enroll_base_delay_s * (2 ** (attempt - 1)))
            delay = self._uniform(0.0, ceiling)
            if retry_after is not None:
                delay = max(delay, retry_after)
            if self._monotonic() + delay > deadline:
                log.warning(f"Enrollment retry budget exhausted after {attempt} attempt(s)")
                if last is not None:
                    return last
                raise httpx.ConnectError(f"retry budget exhausted ({reason})")
            log.warning(
                f"Enrollment attempt {attempt}/{attempts} failed ({reason}); retrying in {delay:.1f}s"
            )
            self._sleep(delay)

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
            response = self._post_with_retry(
                "/api/v1/agents/enroll",
                headers={
                    "X-Enrollment-Key": self.config.enrollment_key,
                    "Content-Type": "application/json",
                },
                json={
                    "hostname": self.config.hostname,
                    "ip_address": self._get_primary_ip(),
                    "agent_version": __version__,
                    "capabilities": self.config.capabilities,
                },
            )

            if response.status_code == 401:
                raise EnrollmentError("Invalid or expired enrollment key")

            if response.status_code == 409:
                raise EnrollmentError("Enrollment key already used")

            if response.status_code != 201:
                raise EnrollmentError(f"Enrollment failed: {_error_message(response)}")

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
                raise EnrollmentError(f"Token refresh failed: {_error_message(response)}")

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
