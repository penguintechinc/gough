"""Base Cloud Provider Abstraction.

Defines the interface that all cloud providers must implement for unified
machine management across MaaS, LXD, AWS, GCP, Azure, and Vultr.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MachineState(str, Enum):
    """Unified machine state across all providers."""

    PENDING = "pending"           # Being created/provisioned
    RUNNING = "running"           # Running and accessible
    STOPPED = "stopped"           # Stopped but exists
    TERMINATED = "terminated"     # Destroyed/deleted
    ERROR = "error"               # In error state
    UNKNOWN = "unknown"           # State cannot be determined

    # MaaS-specific states mapped to unified
    COMMISSIONING = "commissioning"
    DEPLOYING = "deploying"
    READY = "ready"               # MaaS: ready for deployment
    ALLOCATED = "allocated"       # MaaS: allocated to user


@dataclass(slots=True)
class MachineSpec:
    """Specification for creating a new machine."""

    name: str
    image: str                           # OS image (e.g., "ubuntu-22.04", ami-xxx)
    size: str                            # Instance size (e.g., "t3.medium", "n1-standard-2")
    region: str = ""                     # Region/zone (optional for some providers)
    cloud_init: str = ""                 # Cloud-init user data
    ssh_keys: list[str] = field(default_factory=list)  # SSH public keys
    networks: list[str] = field(default_factory=list)  # Network IDs/names
    storage_gb: int = 0                  # Root disk size (0 = default)
    tags: dict[str, str] = field(default_factory=dict)  # Provider tags/labels
    extra: dict[str, Any] = field(default_factory=dict)  # Provider-specific options


@dataclass(slots=True)
class Machine:
    """Unified machine representation across all providers."""

    id: str                              # Provider-specific ID
    name: str                            # Machine name
    state: MachineState                  # Current state
    provider: str                        # Provider type (maas, lxd, aws, etc.)
    provider_id: str                     # Provider's cloud_providers table ID
    region: str = ""                     # Region/zone
    image: str = ""                      # OS image
    size: str = ""                       # Instance size
    public_ips: list[str] = field(default_factory=list)
    private_ips: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    tags: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)  # Provider-specific data

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "provider": self.provider,
            "provider_id": self.provider_id,
            "region": self.region,
            "image": self.image,
            "size": self.size,
            "public_ips": self.public_ips,
            "private_ips": self.private_ips,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "tags": self.tags,
            "extra": self.extra,
        }


class CloudError(Exception):
    """Base exception for cloud provider errors."""

    pass


class CloudAuthError(CloudError):
    """Authentication or authorization error."""

    pass


class CloudNotFoundError(CloudError):
    """Resource not found error."""

    pass


class CloudQuotaError(CloudError):
    """Resource quota or limit exceeded."""

    pass


class BaseCloud(ABC):
    """Abstract base class for cloud providers.

    All cloud provider implementations must inherit from this class and
    implement all abstract methods.
    """

    # Class-level attributes
    provider_type: str = "unknown"
    supports_cloud_init: bool = True

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize cloud provider with configuration.

        Args:
            config: Provider-specific configuration dictionary
        """
        self.config = config
        self._authenticated = False

    @abstractmethod
    def authenticate(self) -> bool:
        """Authenticate with the cloud provider.

        Returns:
            True if authentication successful

        Raises:
            CloudAuthError: If authentication fails
        """
        pass

    @abstractmethod
    def list_machines(self, filters: dict | None = None) -> list[Machine]:
        """List all machines.

        Args:
            filters: Optional filters (provider-specific)

        Returns:
            List of Machine objects

        Raises:
            CloudError: On API errors
        """
        pass

    @abstractmethod
    def get_machine(self, machine_id: str) -> Machine:
        """Get a specific machine by ID.

        Args:
            machine_id: Provider-specific machine ID

        Returns:
            Machine object

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        pass

    @abstractmethod
    def create_machine(self, spec: MachineSpec) -> Machine:
        """Create a new machine.

        Args:
            spec: Machine specification

        Returns:
            Created Machine object

        Raises:
            CloudQuotaError: If quota exceeded
            CloudError: On API errors
        """
        pass

    @abstractmethod
    def destroy_machine(self, machine_id: str) -> bool:
        """Destroy/delete a machine.

        Args:
            machine_id: Provider-specific machine ID

        Returns:
            True if destroyed successfully

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        pass

    @abstractmethod
    def start_machine(self, machine_id: str) -> bool:
        """Start a stopped machine.

        Args:
            machine_id: Provider-specific machine ID

        Returns:
            True if started successfully

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        pass

    @abstractmethod
    def stop_machine(self, machine_id: str) -> bool:
        """Stop a running machine.

        Args:
            machine_id: Provider-specific machine ID

        Returns:
            True if stopped successfully

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        pass

    def reboot_machine(self, machine_id: str) -> bool:
        """Reboot a machine.

        Default implementation stops then starts. Providers may override.

        Args:
            machine_id: Provider-specific machine ID

        Returns:
            True if rebooted successfully
        """
        self.stop_machine(machine_id)
        return self.start_machine(machine_id)

    def get_cloud_init_support(self) -> bool:
        """Check if provider supports cloud-init.

        Returns:
            True if cloud-init is supported
        """
        return self.supports_cloud_init

    def list_images(self, filters: dict | None = None) -> list[dict]:
        """List available images.

        Args:
            filters: Optional filters

        Returns:
            List of image info dictionaries
        """
        return []

    def list_sizes(self, filters: dict | None = None) -> list[dict]:
        """List available machine sizes.

        Args:
            filters: Optional filters

        Returns:
            List of size info dictionaries
        """
        return []

    def list_regions(self) -> list[dict]:
        """List available regions/zones.

        Returns:
            List of region info dictionaries
        """
        return []

    def get_console_output(self, machine_id: str) -> str:
        """Get console output from a machine.

        Args:
            machine_id: Provider-specific machine ID

        Returns:
            Console output string
        """
        return ""

    def wait_for_state(
        self,
        machine_id: str,
        target_state: MachineState,
        timeout: int = 300,
        interval: int = 10,
    ) -> Machine:
        """Wait for machine to reach a target state.

        Args:
            machine_id: Provider-specific machine ID
            target_state: State to wait for
            timeout: Maximum wait time in seconds
            interval: Polling interval in seconds

        Returns:
            Machine in target state

        Raises:
            CloudError: If timeout exceeded or error state reached
        """
        import time

        start_time = time.time()

        while time.time() - start_time < timeout:
            machine = self.get_machine(machine_id)

            if machine.state == target_state:
                return machine

            if machine.state == MachineState.ERROR:
                raise CloudError(f"Machine {machine_id} entered error state")

            time.sleep(interval)

        raise CloudError(
            f"Timeout waiting for machine {machine_id} to reach {target_state.value}"
        )
