"""JWT authentication handling for Gough Access Agent."""

import logging
from datetime import datetime, timedelta
from typing import Optional

import jwt

from .config import AgentConfig
from .enrollment import AgentEnrollment, EnrollmentError

log = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when authentication fails."""
    pass


class AgentAuth:
    """Handles JWT authentication for agent API calls."""

    def __init__(self, config: AgentConfig, enrollment: AgentEnrollment):
        """Initialize auth handler.

        Args:
            config: Agent configuration
            enrollment: Enrollment handler for token refresh
        """
        self.config = config
        self.enrollment = enrollment
        self._token_expiry: Optional[datetime] = None

    def get_auth_headers(self) -> dict:
        """Get authentication headers for API requests.

        Automatically refreshes token if expired or near expiry.

        Returns:
            Dict with Authorization header

        Raises:
            AuthError: If no valid token available
        """
        if not self.config.access_token:
            raise AuthError("No access token available")

        # Check if token needs refresh
        if self._should_refresh_token():
            self._refresh_token()

        return {
            "Authorization": f"Bearer {self.config.access_token}",
        }

    def _should_refresh_token(self) -> bool:
        """Check if access token should be refreshed.

        Returns True if token is expired or expires within 5 minutes.
        """
        if not self.config.access_token:
            return True

        try:
            # Decode without verification to check expiry
            payload = jwt.decode(
                self.config.access_token,
                options={"verify_signature": False},
            )

            exp = payload.get("exp")
            if not exp:
                return True

            # Refresh if expires within 5 minutes
            expiry = datetime.fromtimestamp(exp)
            return datetime.utcnow() >= expiry - timedelta(minutes=5)

        except jwt.InvalidTokenError:
            return True

    def _refresh_token(self) -> None:
        """Refresh the access token."""
        try:
            log.info("Refreshing access token")
            self.enrollment.refresh_tokens()

        except EnrollmentError as e:
            log.error(f"Token refresh failed: {e}")
            raise AuthError(f"Token refresh failed: {e}")

    def validate_access_token(self) -> bool:
        """Validate current access token.

        Returns:
            True if token is valid and not expired
        """
        if not self.config.access_token:
            return False

        try:
            payload = jwt.decode(
                self.config.access_token,
                options={"verify_signature": False},
            )

            exp = payload.get("exp")
            if not exp:
                return False

            return datetime.fromtimestamp(exp) > datetime.utcnow()

        except jwt.InvalidTokenError:
            return False

    def get_agent_id_from_token(self) -> Optional[str]:
        """Extract agent ID from access token.

        Returns:
            Agent ID or None if not found
        """
        if not self.config.access_token:
            return None

        try:
            payload = jwt.decode(
                self.config.access_token,
                options={"verify_signature": False},
            )

            # Agent tokens have sub like "agent:uuid"
            sub = payload.get("sub", "")
            if sub.startswith("agent:"):
                return sub[6:]
            return sub

        except jwt.InvalidTokenError:
            return None
