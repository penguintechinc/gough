"""Integration clients for external services.

This module provides integration clients for various external services:
- Elder: Host registration and service discovery
- Other integrations as needed
"""

from .elder import (
    ElderClient,
    ElderError,
    ElderAuthError,
    ElderConnectionError,
    ElderNotFoundError,
    ElderConflictError,
    HostRegistration,
    AppEndpoint,
    get_elder_client,
)

__all__ = [
    "ElderClient",
    "ElderError",
    "ElderAuthError",
    "ElderConnectionError",
    "ElderNotFoundError",
    "ElderConflictError",
    "HostRegistration",
    "AppEndpoint",
    "get_elder_client",
]
