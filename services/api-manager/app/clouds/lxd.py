"""LXD Cloud Provider Implementation.

Provides unified machine management for LXD containers and virtual machines
using certificate-based HTTPS authentication via the pylxd library.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from .base import (
    BaseCloud,
    CloudAuthError,
    CloudError,
    CloudNotFoundError,
    CloudQuotaError,
    Machine,
    MachineSpec,
    MachineState,
)

log = logging.getLogger(__name__)

# LXD state to MachineState mapping
LXD_STATE_MAP: dict[str, MachineState] = {
    "Running": MachineState.RUNNING,
    "Stopped": MachineState.STOPPED,
    "Frozen": MachineState.STOPPED,  # Map frozen to stopped (paused state)
    "Starting": MachineState.PENDING,
    "Stopping": MachineState.PENDING,
    "Aborting": MachineState.ERROR,
    "Error": MachineState.ERROR,
}


class LXDCloud(BaseCloud):
    """LXD Cloud Provider for containers and virtual machines.

    Supports both LXD containers (system containers) and virtual machines
    using certificate-based HTTPS authentication.

    Configuration:
        LXD_API_URL: LXD API endpoint (e.g., https://lxd.example.com:8443)
        LXD_CLIENT_CERT: Path to client certificate file
        LXD_CLIENT_KEY: Path to client key file
        LXD_TRUST_PASSWORD: Trust password for initial authentication (optional)
        LXD_VERIFY_SSL: Whether to verify SSL certificates (default: True)
        LXD_PROJECT: LXD project name (default: "default")
    """

    provider_type: str = "lxd"
    supports_cloud_init: bool = True

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize LXD cloud provider.

        Args:
            config: Configuration dictionary with LXD connection settings
        """
        super().__init__(config)
        self._client: Any = None
        self._provider_id: str = config.get("provider_id", "")

        # Extract configuration with environment variable fallbacks
        self._api_url = config.get(
            "LXD_API_URL",
            os.environ.get("LXD_API_URL", "https://localhost:8443")
        )
        self._client_cert = config.get(
            "LXD_CLIENT_CERT",
            os.environ.get("LXD_CLIENT_CERT", "")
        )
        self._client_key = config.get(
            "LXD_CLIENT_KEY",
            os.environ.get("LXD_CLIENT_KEY", "")
        )
        self._trust_password = config.get(
            "LXD_TRUST_PASSWORD",
            os.environ.get("LXD_TRUST_PASSWORD", "")
        )
        self._verify_ssl = config.get(
            "LXD_VERIFY_SSL",
            os.environ.get("LXD_VERIFY_SSL", "true").lower() == "true"
        )
        self._project = config.get(
            "LXD_PROJECT",
            os.environ.get("LXD_PROJECT", "default")
        )

    def authenticate(self) -> bool:
        """Authenticate with LXD server using client certificates.

        Returns:
            True if authentication successful

        Raises:
            CloudAuthError: If authentication fails
        """
        try:
            import pylxd
        except ImportError:
            raise CloudAuthError(
                "pylxd library not installed. Install with: pip install pylxd"
            )

        try:
            # Validate certificate paths
            if self._client_cert and not os.path.isfile(self._client_cert):
                raise CloudAuthError(
                    f"Client certificate not found: {self._client_cert}"
                )
            if self._client_key and not os.path.isfile(self._client_key):
                raise CloudAuthError(
                    f"Client key not found: {self._client_key}"
                )

            # Build certificate tuple if provided
            cert: tuple[str, str] | None = None
            if self._client_cert and self._client_key:
                cert = (self._client_cert, self._client_key)

            # Create LXD client
            self._client = pylxd.Client(
                endpoint=self._api_url,
                cert=cert,
                verify=self._verify_ssl,
                project=self._project,
            )

            # Attempt to authenticate with trust password if not trusted
            if not self._client.trusted:
                if self._trust_password:
                    self._client.authenticate(self._trust_password)
                else:
                    raise CloudAuthError(
                        "Client is not trusted and no trust password provided"
                    )

            # Verify connection by fetching server info
            info = self._client.host_info
            log.info(
                f"Connected to LXD server: {info.get('environment', {}).get('server_name', 'unknown')}"
            )

            self._authenticated = True
            return True

        except pylxd.exceptions.ClientConnectionFailed as e:
            raise CloudAuthError(f"Failed to connect to LXD server: {e}")
        except pylxd.exceptions.LXDAPIException as e:
            raise CloudAuthError(f"LXD authentication failed: {e}")
        except Exception as e:
            raise CloudAuthError(f"Unexpected error during LXD authentication: {e}")

    def _ensure_authenticated(self) -> None:
        """Ensure client is authenticated before making API calls."""
        if not self._authenticated or self._client is None:
            self.authenticate()

    def _get_instance(self, machine_id: str) -> Any:
        """Get an instance (container or VM) by name.

        Args:
            machine_id: Instance name

        Returns:
            pylxd instance object

        Raises:
            CloudNotFoundError: If instance not found
        """
        import pylxd

        try:
            # Try containers first
            return self._client.instances.get(machine_id)
        except pylxd.exceptions.NotFound:
            raise CloudNotFoundError(f"Instance not found: {machine_id}")
        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to get instance {machine_id}: {e}")

    def _map_lxd_state(self, status: str) -> MachineState:
        """Map LXD instance status to unified MachineState.

        Args:
            status: LXD status string

        Returns:
            Mapped MachineState enum value
        """
        return LXD_STATE_MAP.get(status, MachineState.UNKNOWN)

    def _instance_to_machine(self, instance: Any) -> Machine:
        """Convert pylxd instance to Machine object.

        Args:
            instance: pylxd instance object

        Returns:
            Machine object
        """
        # Extract network addresses
        public_ips: list[str] = []
        private_ips: list[str] = []

        try:
            state = instance.state()
            if state and state.network:
                for iface_name, iface_data in state.network.items():
                    if iface_name == "lo":
                        continue
                    for addr_info in iface_data.get("addresses", []):
                        addr = addr_info.get("address", "")
                        family = addr_info.get("family", "")
                        scope = addr_info.get("scope", "")

                        if family == "inet" and addr:
                            # Classify based on scope or address range
                            if scope == "global" or self._is_public_ip(addr):
                                public_ips.append(addr)
                            else:
                                private_ips.append(addr)
                        elif family == "inet6" and addr and scope == "global":
                            public_ips.append(addr)
        except Exception as e:
            log.debug(f"Could not fetch network state for {instance.name}: {e}")

        # Extract timestamps
        created_at: datetime | None = None
        updated_at: datetime | None = None

        if hasattr(instance, "created_at") and instance.created_at:
            try:
                created_at = datetime.fromisoformat(
                    instance.created_at.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        if hasattr(instance, "last_used_at") and instance.last_used_at:
            try:
                updated_at = datetime.fromisoformat(
                    instance.last_used_at.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        # Determine instance type (container or VM)
        instance_type = getattr(instance, "type", "container")

        # Extract image info from config
        image = ""
        if hasattr(instance, "config"):
            image = instance.config.get("image.description", "")
            if not image:
                image = instance.config.get("image.os", "")

        # Build extra metadata
        extra: dict[str, Any] = {
            "type": instance_type,
            "architecture": getattr(instance, "architecture", ""),
            "profiles": list(getattr(instance, "profiles", [])),
            "ephemeral": getattr(instance, "ephemeral", False),
            "stateful": getattr(instance, "stateful", False),
        }

        # Add resource limits if available
        if hasattr(instance, "config"):
            config = instance.config
            if "limits.cpu" in config:
                extra["cpu_limit"] = config["limits.cpu"]
            if "limits.memory" in config:
                extra["memory_limit"] = config["limits.memory"]

        return Machine(
            id=instance.name,
            name=instance.name,
            state=self._map_lxd_state(instance.status),
            provider=self.provider_type,
            provider_id=self._provider_id,
            region=self._project,  # Use project as region
            image=image,
            size=extra.get("cpu_limit", "") + "/" + extra.get("memory_limit", ""),
            public_ips=public_ips,
            private_ips=private_ips,
            created_at=created_at,
            updated_at=updated_at,
            tags=dict(getattr(instance, "config", {}).get("user", {}) or {}),
            extra=extra,
        )

    def _is_public_ip(self, ip: str) -> bool:
        """Check if an IP address is public.

        Args:
            ip: IP address string

        Returns:
            True if IP is public
        """
        import ipaddress

        try:
            addr = ipaddress.ip_address(ip)
            return not addr.is_private and not addr.is_loopback
        except ValueError:
            return False

    def list_machines(self, filters: dict | None = None) -> list[Machine]:
        """List all instances (containers and VMs).

        Args:
            filters: Optional filters
                - type: "container" or "virtual-machine"
                - status: LXD status string

        Returns:
            List of Machine objects

        Raises:
            CloudError: On API errors
        """
        import pylxd

        self._ensure_authenticated()
        filters = filters or {}

        try:
            instances = self._client.instances.all()

            machines: list[Machine] = []
            for instance in instances:
                # Apply filters
                if filters.get("type"):
                    instance_type = getattr(instance, "type", "container")
                    if instance_type != filters["type"]:
                        continue

                if filters.get("status"):
                    if instance.status != filters["status"]:
                        continue

                machines.append(self._instance_to_machine(instance))

            return machines

        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to list instances: {e}")

    def get_machine(self, machine_id: str) -> Machine:
        """Get a specific instance by name.

        Args:
            machine_id: Instance name

        Returns:
            Machine object

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        self._ensure_authenticated()
        instance = self._get_instance(machine_id)
        return self._instance_to_machine(instance)

    def create_machine(self, spec: MachineSpec) -> Machine:
        """Create a new instance (container or VM).

        Args:
            spec: Machine specification
                - name: Instance name
                - image: Image alias or fingerprint (e.g., "ubuntu:22.04")
                - size: Not directly used, but can contain limits (e.g., "2/4GiB")
                - cloud_init: Cloud-init user data
                - networks: List of network names
                - storage_gb: Root disk size in GB
                - extra: Additional options
                    - type: "container" (default) or "virtual-machine"
                    - profiles: List of profiles to apply
                    - ephemeral: Whether instance is ephemeral

        Returns:
            Created Machine object

        Raises:
            CloudQuotaError: If quota exceeded
            CloudError: On API errors
        """
        import pylxd

        self._ensure_authenticated()

        try:
            # Determine instance type
            instance_type = spec.extra.get("type", "container")
            if instance_type not in ("container", "virtual-machine"):
                instance_type = "container"

            # Parse image source
            image_source = self._parse_image_source(spec.image)

            # Build instance configuration
            config: dict[str, Any] = {}

            # Apply cloud-init if provided
            if spec.cloud_init:
                config["user.user-data"] = spec.cloud_init

            # Parse size for resource limits (format: "cpu/memory")
            if spec.size:
                parts = spec.size.split("/")
                if len(parts) >= 1 and parts[0]:
                    config["limits.cpu"] = parts[0]
                if len(parts) >= 2 and parts[1]:
                    config["limits.memory"] = parts[1]

            # Add SSH keys via cloud-init if no cloud-init provided
            if spec.ssh_keys and not spec.cloud_init:
                ssh_keys_yaml = "\n".join(
                    f"  - {key}" for key in spec.ssh_keys
                )
                config["user.user-data"] = f"""#cloud-config
ssh_authorized_keys:
{ssh_keys_yaml}
"""

            # Add tags as user config
            for key, value in spec.tags.items():
                config[f"user.{key}"] = value

            # Build devices configuration
            devices: dict[str, Any] = {}

            # Add networks
            for idx, network in enumerate(spec.networks):
                devices[f"eth{idx}"] = {
                    "type": "nic",
                    "network": network,
                    "name": f"eth{idx}",
                }

            # Add root disk with specified size
            if spec.storage_gb > 0:
                devices["root"] = {
                    "type": "disk",
                    "path": "/",
                    "pool": spec.extra.get("storage_pool", "default"),
                    "size": f"{spec.storage_gb}GiB",
                }

            # Determine profiles
            profiles = spec.extra.get("profiles", ["default"])

            # Create instance
            instance = self._client.instances.create(
                {
                    "name": spec.name,
                    "type": instance_type,
                    "source": image_source,
                    "config": config,
                    "devices": devices,
                    "profiles": profiles,
                    "ephemeral": spec.extra.get("ephemeral", False),
                },
                wait=True,
            )

            log.info(f"Created LXD instance: {spec.name} (type: {instance_type})")

            # Start the instance if auto_start is not explicitly disabled
            if spec.extra.get("auto_start", True):
                instance.start(wait=True)
                log.info(f"Started LXD instance: {spec.name}")

            return self._instance_to_machine(instance)

        except pylxd.exceptions.LXDAPIException as e:
            error_msg = str(e)
            if "quota" in error_msg.lower() or "limit" in error_msg.lower():
                raise CloudQuotaError(f"LXD quota exceeded: {e}")
            raise CloudError(f"Failed to create instance: {e}")

    def _parse_image_source(self, image: str) -> dict[str, Any]:
        """Parse image specification into LXD source config.

        Supports formats:
            - "ubuntu:22.04" - remote:alias format
            - "images:ubuntu/22.04" - remote:alias with specific remote
            - "fingerprint:abc123" - specific fingerprint
            - "local:my-image" - local image by alias

        Args:
            image: Image specification string

        Returns:
            LXD source configuration dictionary
        """
        if ":" in image:
            parts = image.split(":", 1)
            prefix = parts[0].lower()
            value = parts[1]

            if prefix == "fingerprint":
                return {"type": "image", "fingerprint": value}
            elif prefix == "local":
                return {"type": "image", "alias": value}
            else:
                # Remote image (e.g., "ubuntu:22.04" or "images:ubuntu/22.04")
                return {
                    "type": "image",
                    "protocol": "simplestreams",
                    "server": self._get_remote_url(prefix),
                    "alias": value,
                }
        else:
            # Assume local alias
            return {"type": "image", "alias": image}

    def _get_remote_url(self, remote_name: str) -> str:
        """Get URL for a named remote.

        Args:
            remote_name: Remote name

        Returns:
            Remote URL
        """
        remotes = {
            "ubuntu": "https://cloud-images.ubuntu.com/releases",
            "ubuntu-daily": "https://cloud-images.ubuntu.com/daily",
            "images": "https://images.linuxcontainers.org",
        }
        return remotes.get(remote_name, f"https://{remote_name}")

    def destroy_machine(self, machine_id: str) -> bool:
        """Destroy/delete an instance.

        Args:
            machine_id: Instance name

        Returns:
            True if destroyed successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            # Stop instance if running
            if instance.status == "Running":
                instance.stop(wait=True)
                log.info(f"Stopped LXD instance before deletion: {machine_id}")

            # Delete instance
            instance.delete(wait=True)
            log.info(f"Deleted LXD instance: {machine_id}")
            return True

        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to destroy instance {machine_id}: {e}")

    def start_machine(self, machine_id: str) -> bool:
        """Start a stopped instance.

        Args:
            machine_id: Instance name

        Returns:
            True if started successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            if instance.status == "Running":
                log.info(f"Instance {machine_id} is already running")
                return True

            if instance.status == "Frozen":
                instance.unfreeze(wait=True)
            else:
                instance.start(wait=True)

            log.info(f"Started LXD instance: {machine_id}")
            return True

        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to start instance {machine_id}: {e}")

    def stop_machine(self, machine_id: str) -> bool:
        """Stop a running instance.

        Args:
            machine_id: Instance name

        Returns:
            True if stopped successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            if instance.status == "Stopped":
                log.info(f"Instance {machine_id} is already stopped")
                return True

            instance.stop(wait=True)
            log.info(f"Stopped LXD instance: {machine_id}")
            return True

        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to stop instance {machine_id}: {e}")

    def reboot_machine(self, machine_id: str) -> bool:
        """Reboot an instance.

        Args:
            machine_id: Instance name

        Returns:
            True if rebooted successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            instance.restart(wait=True)
            log.info(f"Rebooted LXD instance: {machine_id}")
            return True

        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to reboot instance {machine_id}: {e}")

    def freeze_machine(self, machine_id: str) -> bool:
        """Freeze (pause) a running instance.

        Args:
            machine_id: Instance name

        Returns:
            True if frozen successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            if instance.status != "Running":
                raise CloudError(
                    f"Cannot freeze instance {machine_id}: not running"
                )

            instance.freeze(wait=True)
            log.info(f"Frozen LXD instance: {machine_id}")
            return True

        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to freeze instance {machine_id}: {e}")

    def unfreeze_machine(self, machine_id: str) -> bool:
        """Unfreeze (resume) a frozen instance.

        Args:
            machine_id: Instance name

        Returns:
            True if unfrozen successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            if instance.status != "Frozen":
                raise CloudError(
                    f"Cannot unfreeze instance {machine_id}: not frozen"
                )

            instance.unfreeze(wait=True)
            log.info(f"Unfrozen LXD instance: {machine_id}")
            return True

        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to unfreeze instance {machine_id}: {e}")

    def list_images(self, filters: dict | None = None) -> list[dict]:
        """List available images.

        Args:
            filters: Optional filters
                - architecture: Filter by architecture

        Returns:
            List of image info dictionaries
        """
        import pylxd

        self._ensure_authenticated()
        filters = filters or {}

        try:
            images = self._client.images.all()

            result: list[dict] = []
            for image in images:
                # Apply filters
                if filters.get("architecture"):
                    if image.architecture != filters["architecture"]:
                        continue

                properties = getattr(image, "properties", {}) or {}
                result.append({
                    "id": image.fingerprint,
                    "fingerprint": image.fingerprint,
                    "aliases": [
                        a.get("name", "") for a in getattr(image, "aliases", [])
                    ],
                    "architecture": image.architecture,
                    "description": properties.get("description", ""),
                    "os": properties.get("os", ""),
                    "release": properties.get("release", ""),
                    "size": getattr(image, "size", 0),
                    "created_at": getattr(image, "created_at", ""),
                    "public": getattr(image, "public", False),
                })

            return result

        except pylxd.exceptions.LXDAPIException as e:
            log.error(f"Failed to list images: {e}")
            return []

    def list_profiles(self) -> list[dict]:
        """List available profiles.

        Returns:
            List of profile info dictionaries
        """
        import pylxd

        self._ensure_authenticated()

        try:
            profiles = self._client.profiles.all()

            result: list[dict] = []
            for profile in profiles:
                result.append({
                    "name": profile.name,
                    "description": getattr(profile, "description", ""),
                    "config": dict(getattr(profile, "config", {}) or {}),
                    "devices": dict(getattr(profile, "devices", {}) or {}),
                })

            return result

        except pylxd.exceptions.LXDAPIException as e:
            log.error(f"Failed to list profiles: {e}")
            return []

    def list_networks(self) -> list[dict]:
        """List available networks.

        Returns:
            List of network info dictionaries
        """
        import pylxd

        self._ensure_authenticated()

        try:
            networks = self._client.networks.all()

            result: list[dict] = []
            for network in networks:
                result.append({
                    "name": network.name,
                    "description": getattr(network, "description", ""),
                    "type": getattr(network, "type", ""),
                    "managed": getattr(network, "managed", False),
                    "config": dict(getattr(network, "config", {}) or {}),
                })

            return result

        except pylxd.exceptions.LXDAPIException as e:
            log.error(f"Failed to list networks: {e}")
            return []

    def list_storage_pools(self) -> list[dict]:
        """List available storage pools.

        Returns:
            List of storage pool info dictionaries
        """
        import pylxd

        self._ensure_authenticated()

        try:
            pools = self._client.storage_pools.all()

            result: list[dict] = []
            for pool in pools:
                result.append({
                    "name": pool.name,
                    "driver": getattr(pool, "driver", ""),
                    "description": getattr(pool, "description", ""),
                    "config": dict(getattr(pool, "config", {}) or {}),
                })

            return result

        except pylxd.exceptions.LXDAPIException as e:
            log.error(f"Failed to list storage pools: {e}")
            return []

    def list_regions(self) -> list[dict]:
        """List available regions (projects in LXD).

        Returns:
            List of project info dictionaries
        """
        import pylxd

        self._ensure_authenticated()

        try:
            # In LXD, projects serve as regions/namespaces
            projects = self._client.projects.all()

            result: list[dict] = []
            for project in projects:
                result.append({
                    "id": project.name,
                    "name": project.name,
                    "description": getattr(project, "description", ""),
                })

            return result

        except pylxd.exceptions.LXDAPIException as e:
            log.error(f"Failed to list projects: {e}")
            return [{"id": "default", "name": "default", "description": ""}]

    def get_console_output(self, machine_id: str) -> str:
        """Get console output from an instance.

        Args:
            machine_id: Instance name

        Returns:
            Console output string
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            # Get console log if available
            console = instance.console_log()
            return console if console else ""

        except pylxd.exceptions.LXDAPIException as e:
            log.debug(f"Could not get console output for {machine_id}: {e}")
            return ""

    def execute_command(
        self,
        machine_id: str,
        command: list[str],
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a command inside an instance.

        Args:
            machine_id: Instance name
            command: Command and arguments as list
            environment: Optional environment variables

        Returns:
            Dictionary with stdout, stderr, and exit_code
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        if instance.status != "Running":
            raise CloudError(
                f"Cannot execute command: instance {machine_id} is not running"
            )

        try:
            result = instance.execute(
                command,
                environment=environment or {},
            )

            return {
                "stdout": result.stdout if hasattr(result, "stdout") else "",
                "stderr": result.stderr if hasattr(result, "stderr") else "",
                "exit_code": result.exit_code if hasattr(result, "exit_code") else -1,
            }

        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Command execution failed: {e}")

    def create_snapshot(
        self,
        machine_id: str,
        snapshot_name: str,
        stateful: bool = False,
    ) -> dict[str, Any]:
        """Create a snapshot of an instance.

        Args:
            machine_id: Instance name
            snapshot_name: Name for the snapshot
            stateful: Whether to include memory state (VMs only)

        Returns:
            Snapshot info dictionary
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            instance.snapshots.create(
                snapshot_name,
                stateful=stateful,
                wait=True,
            )

            log.info(f"Created snapshot {snapshot_name} for instance {machine_id}")

            return {
                "name": snapshot_name,
                "instance": machine_id,
                "stateful": stateful,
                "created_at": datetime.utcnow().isoformat(),
            }

        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to create snapshot: {e}")

    def list_snapshots(self, machine_id: str) -> list[dict]:
        """List snapshots for an instance.

        Args:
            machine_id: Instance name

        Returns:
            List of snapshot info dictionaries
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            snapshots = instance.snapshots.all()

            result: list[dict] = []
            for snap in snapshots:
                result.append({
                    "name": snap.name,
                    "stateful": getattr(snap, "stateful", False),
                    "created_at": getattr(snap, "created_at", ""),
                })

            return result

        except pylxd.exceptions.LXDAPIException as e:
            log.error(f"Failed to list snapshots for {machine_id}: {e}")
            return []

    def restore_snapshot(self, machine_id: str, snapshot_name: str) -> bool:
        """Restore an instance from a snapshot.

        Args:
            machine_id: Instance name
            snapshot_name: Snapshot name to restore

        Returns:
            True if restored successfully
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            snapshot = instance.snapshots.get(snapshot_name)
            snapshot.restore(wait=True)

            log.info(f"Restored instance {machine_id} from snapshot {snapshot_name}")
            return True

        except pylxd.exceptions.NotFound:
            raise CloudNotFoundError(
                f"Snapshot not found: {snapshot_name} for instance {machine_id}"
            )
        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to restore snapshot: {e}")

    def delete_snapshot(self, machine_id: str, snapshot_name: str) -> bool:
        """Delete a snapshot.

        Args:
            machine_id: Instance name
            snapshot_name: Snapshot name to delete

        Returns:
            True if deleted successfully
        """
        import pylxd

        self._ensure_authenticated()
        instance = self._get_instance(machine_id)

        try:
            snapshot = instance.snapshots.get(snapshot_name)
            snapshot.delete(wait=True)

            log.info(f"Deleted snapshot {snapshot_name} for instance {machine_id}")
            return True

        except pylxd.exceptions.NotFound:
            raise CloudNotFoundError(
                f"Snapshot not found: {snapshot_name} for instance {machine_id}"
            )
        except pylxd.exceptions.LXDAPIException as e:
            raise CloudError(f"Failed to delete snapshot: {e}")
