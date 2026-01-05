"""GCP Cloud Provider Implementation.

Provides integration with Google Cloud Platform Compute Engine for unified
machine management. Uses service account authentication and supports full
instance lifecycle operations.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
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

# GCP instance status to MachineState mapping
GCP_STATE_MAP: dict[str, MachineState] = {
    "PROVISIONING": MachineState.PENDING,
    "STAGING": MachineState.PENDING,
    "RUNNING": MachineState.RUNNING,
    "STOPPING": MachineState.RUNNING,  # Transitional state
    "STOPPED": MachineState.STOPPED,
    "SUSPENDING": MachineState.RUNNING,  # Transitional state
    "SUSPENDED": MachineState.STOPPED,
    "REPAIRING": MachineState.ERROR,
    "TERMINATED": MachineState.TERMINATED,
}


class GCPCloud(BaseCloud):
    """GCP Compute Engine cloud provider implementation.

    Provides full machine lifecycle management for GCP Compute Engine instances
    using service account authentication.

    Configuration:
        GCP_PROJECT_ID: GCP project ID
        GCP_ZONE: Default zone for instances
        GCP_CREDENTIALS_FILE: Path to service account JSON file

    Example:
        config = {
            "GCP_PROJECT_ID": "my-project",
            "GCP_ZONE": "us-central1-a",
            "GCP_CREDENTIALS_FILE": "/path/to/service-account.json",
        }
        provider = GCPCloud(config)
        provider.authenticate()
    """

    provider_type: str = "gcp"
    supports_cloud_init: bool = True

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize GCP cloud provider.

        Args:
            config: Configuration dictionary with GCP credentials and settings

        Raises:
            CloudError: If required configuration is missing
        """
        super().__init__(config)

        self.project_id = config.get("GCP_PROJECT_ID", "")
        self.zone = config.get("GCP_ZONE", "us-central1-a")
        self.credentials_file = config.get("GCP_CREDENTIALS_FILE", "")
        self.provider_id = config.get("provider_id", "")

        # Validate required configuration
        if not self.project_id:
            raise CloudError("GCP_PROJECT_ID is required")

        # GCP API clients (initialized on authenticate)
        self._compute_client = None
        self._instances_client = None
        self._images_client = None
        self._machine_types_client = None
        self._zones_client = None
        self._firewalls_client = None
        self._networks_client = None

    def authenticate(self) -> bool:
        """Authenticate with GCP using service account credentials.

        Uses either the credentials file specified in config or falls back
        to Application Default Credentials (ADC).

        Returns:
            True if authentication successful

        Raises:
            CloudAuthError: If authentication fails
        """
        try:
            from google.cloud import compute_v1
            from google.oauth2 import service_account
        except ImportError as e:
            raise CloudAuthError(
                "google-cloud-compute library not installed. "
                "Install with: pip install google-cloud-compute"
            ) from e

        try:
            credentials = None

            # Use service account credentials file if provided
            if self.credentials_file:
                creds_path = Path(self.credentials_file)
                if not creds_path.exists():
                    raise CloudAuthError(
                        f"Credentials file not found: {self.credentials_file}"
                    )

                credentials = service_account.Credentials.from_service_account_file(
                    str(creds_path),
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                log.info("Using service account credentials from %s", self.credentials_file)
            else:
                # Use Application Default Credentials
                log.info("Using Application Default Credentials for GCP")

            # Initialize compute clients
            client_kwargs = {"credentials": credentials} if credentials else {}

            self._instances_client = compute_v1.InstancesClient(**client_kwargs)
            self._images_client = compute_v1.ImagesClient(**client_kwargs)
            self._machine_types_client = compute_v1.MachineTypesClient(**client_kwargs)
            self._zones_client = compute_v1.ZonesClient(**client_kwargs)
            self._firewalls_client = compute_v1.FirewallsClient(**client_kwargs)
            self._networks_client = compute_v1.NetworksClient(**client_kwargs)

            # Test authentication by listing zones
            request = compute_v1.ListZonesRequest(project=self.project_id)
            list(self._zones_client.list(request=request))

            self._authenticated = True
            log.info("Successfully authenticated with GCP project: %s", self.project_id)
            return True

        except Exception as e:
            error_msg = str(e)
            if "401" in error_msg or "403" in error_msg:
                raise CloudAuthError(f"GCP authentication failed: {error_msg}") from e
            raise CloudAuthError(f"Failed to authenticate with GCP: {error_msg}") from e

    def _ensure_authenticated(self) -> None:
        """Ensure the provider is authenticated.

        Raises:
            CloudAuthError: If not authenticated
        """
        if not self._authenticated or self._instances_client is None:
            raise CloudAuthError("Not authenticated. Call authenticate() first.")

    def _map_state(self, gcp_status: str) -> MachineState:
        """Map GCP instance status to unified MachineState.

        Args:
            gcp_status: GCP instance status string

        Returns:
            Corresponding MachineState
        """
        return GCP_STATE_MAP.get(gcp_status, MachineState.UNKNOWN)

    def _instance_to_machine(self, instance: Any) -> Machine:
        """Convert GCP instance object to Machine dataclass.

        Args:
            instance: GCP compute instance object

        Returns:
            Machine representation
        """
        # Extract IP addresses
        public_ips: list[str] = []
        private_ips: list[str] = []

        for interface in getattr(instance, "network_interfaces", []):
            if hasattr(interface, "network_i_p") and interface.network_i_p:
                private_ips.append(interface.network_i_p)

            for access_config in getattr(interface, "access_configs", []):
                if hasattr(access_config, "nat_i_p") and access_config.nat_i_p:
                    public_ips.append(access_config.nat_i_p)

        # Extract labels/tags
        tags: dict[str, str] = {}
        if hasattr(instance, "labels") and instance.labels:
            tags = dict(instance.labels)

        # Parse timestamps
        created_at = None
        if hasattr(instance, "creation_timestamp") and instance.creation_timestamp:
            try:
                created_at = datetime.fromisoformat(
                    instance.creation_timestamp.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        # Extract machine type (last segment of the URL)
        machine_type = ""
        if hasattr(instance, "machine_type") and instance.machine_type:
            machine_type = instance.machine_type.split("/")[-1]

        # Extract zone (last segment of the URL)
        zone = self.zone
        if hasattr(instance, "zone") and instance.zone:
            zone = instance.zone.split("/")[-1]

        # Extract image info from disks
        image = ""
        for disk in getattr(instance, "disks", []):
            if getattr(disk, "boot", False):
                source_image = getattr(disk, "source", "") or ""
                if source_image:
                    image = source_image.split("/")[-1]
                break

        # Build extra metadata
        extra: dict[str, Any] = {
            "self_link": getattr(instance, "self_link", ""),
            "status_message": getattr(instance, "status_message", ""),
            "can_ip_forward": getattr(instance, "can_ip_forward", False),
        }

        # Add network tags if present
        if hasattr(instance, "tags") and instance.tags:
            extra["network_tags"] = list(getattr(instance.tags, "items", []) or [])

        return Machine(
            id=str(instance.id) if hasattr(instance, "id") else "",
            name=instance.name if hasattr(instance, "name") else "",
            state=self._map_state(instance.status if hasattr(instance, "status") else ""),
            provider=self.provider_type,
            provider_id=self.provider_id,
            region=zone,
            image=image,
            size=machine_type,
            public_ips=public_ips,
            private_ips=private_ips,
            created_at=created_at,
            updated_at=None,
            tags=tags,
            extra=extra,
        )

    def list_machines(self, filters: dict | None = None) -> list[Machine]:
        """List all instances in the configured zone.

        Args:
            filters: Optional filters
                - zone: Override default zone
                - all_zones: If True, list from all zones in the project

        Returns:
            List of Machine objects

        Raises:
            CloudError: On API errors
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1

            machines: list[Machine] = []
            filters = filters or {}

            all_zones = filters.get("all_zones", False)
            target_zone = filters.get("zone", self.zone)

            if all_zones:
                # List instances from all zones
                request = compute_v1.AggregatedListInstancesRequest(
                    project=self.project_id
                )
                agg_list = self._instances_client.aggregated_list(request=request)

                for zone_name, instances_scoped in agg_list:
                    if hasattr(instances_scoped, "instances"):
                        for instance in instances_scoped.instances:
                            machines.append(self._instance_to_machine(instance))
            else:
                # List instances from specific zone
                request = compute_v1.ListInstancesRequest(
                    project=self.project_id,
                    zone=target_zone,
                )
                instances = self._instances_client.list(request=request)

                for instance in instances:
                    machines.append(self._instance_to_machine(instance))

            log.debug("Listed %d machines from GCP", len(machines))
            return machines

        except Exception as e:
            raise CloudError(f"Failed to list GCP instances: {e}") from e

    def get_machine(self, machine_id: str) -> Machine:
        """Get a specific instance by name or ID.

        Args:
            machine_id: Instance name or numeric ID

        Returns:
            Machine object

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1
            from google.api_core.exceptions import NotFound

            # GCP primarily uses instance names, but we support IDs too
            # First try as a name
            try:
                request = compute_v1.GetInstanceRequest(
                    project=self.project_id,
                    zone=self.zone,
                    instance=machine_id,
                )
                instance = self._instances_client.get(request=request)
                return self._instance_to_machine(instance)
            except NotFound:
                pass

            # If not found by name, try to find by numeric ID
            if machine_id.isdigit():
                machines = self.list_machines()
                for machine in machines:
                    if machine.id == machine_id:
                        return machine

            raise CloudNotFoundError(f"Instance not found: {machine_id}")

        except CloudNotFoundError:
            raise
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            raise CloudError(f"Failed to get GCP instance: {e}") from e

    def create_machine(self, spec: MachineSpec) -> Machine:
        """Create a new Compute Engine instance.

        Args:
            spec: Machine specification

        Returns:
            Created Machine object

        Raises:
            CloudQuotaError: If quota exceeded
            CloudError: On API errors
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1
            from google.api_core.exceptions import ResourceExhausted

            zone = spec.region if spec.region else self.zone
            machine_type = f"zones/{zone}/machineTypes/{spec.size}"

            # Resolve image to full URL
            source_image = self._resolve_image(spec.image)

            # Build boot disk configuration
            boot_disk = compute_v1.AttachedDisk(
                auto_delete=True,
                boot=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    source_image=source_image,
                    disk_size_gb=spec.storage_gb if spec.storage_gb > 0 else None,
                ),
            )

            # Build network interfaces
            network_interfaces = self._build_network_interfaces(spec, zone)

            # Build instance object
            instance = compute_v1.Instance(
                name=spec.name,
                machine_type=machine_type,
                disks=[boot_disk],
                network_interfaces=network_interfaces,
            )

            # Add labels from tags
            if spec.tags:
                instance.labels = spec.tags

            # Add metadata (cloud-init and SSH keys)
            metadata_items = []

            # Add cloud-init user data
            if spec.cloud_init:
                metadata_items.append(
                    compute_v1.Items(key="user-data", value=spec.cloud_init)
                )

            # Add SSH keys
            if spec.ssh_keys:
                # GCP format: username:ssh-key
                ssh_keys_value = "\n".join(
                    f"gough:{key}" for key in spec.ssh_keys
                )
                metadata_items.append(
                    compute_v1.Items(key="ssh-keys", value=ssh_keys_value)
                )

            # Add extra metadata from spec.extra
            for key, value in spec.extra.get("metadata", {}).items():
                if isinstance(value, str):
                    metadata_items.append(compute_v1.Items(key=key, value=value))

            if metadata_items:
                instance.metadata = compute_v1.Metadata(items=metadata_items)

            # Add network tags from spec.extra
            if "network_tags" in spec.extra:
                instance.tags = compute_v1.Tags(items=spec.extra["network_tags"])

            # Create the instance
            request = compute_v1.InsertInstanceRequest(
                project=self.project_id,
                zone=zone,
                instance_resource=instance,
            )

            operation = self._instances_client.insert(request=request)

            # Wait for operation to complete
            self._wait_for_operation(operation, zone)

            log.info("Created GCP instance: %s in zone %s", spec.name, zone)

            # Retrieve and return the created instance
            return self.get_machine(spec.name)

        except ResourceExhausted as e:
            raise CloudQuotaError(f"GCP quota exceeded: {e}") from e
        except Exception as e:
            error_str = str(e).lower()
            if "quota" in error_str or "limit" in error_str:
                raise CloudQuotaError(f"GCP quota exceeded: {e}") from e
            raise CloudError(f"Failed to create GCP instance: {e}") from e

    def _resolve_image(self, image_spec: str) -> str:
        """Resolve image specification to full image URL.

        Args:
            image_spec: Image name, family, or full URL

        Returns:
            Full image URL

        Supports formats:
            - Full URL: projects/ubuntu-os-cloud/global/images/ubuntu-2204-lts
            - Family: ubuntu-2204-lts (searches common projects)
            - Name: ubuntu-2204-jammy-v20231213
        """
        # If already a full URL, return as-is
        if image_spec.startswith("projects/") or image_spec.startswith("https://"):
            return image_spec

        # Common image projects to search
        image_projects = [
            self.project_id,
            "ubuntu-os-cloud",
            "debian-cloud",
            "centos-cloud",
            "rhel-cloud",
            "rocky-linux-cloud",
            "fedora-cloud",
            "windows-cloud",
            "cos-cloud",
        ]

        from google.cloud import compute_v1

        # Try to find image by family first
        for project in image_projects:
            try:
                request = compute_v1.GetFromFamilyImageRequest(
                    project=project,
                    family=image_spec,
                )
                image = self._images_client.get_from_family(request=request)
                return image.self_link
            except Exception:
                pass

        # Try to find image by name
        for project in image_projects:
            try:
                request = compute_v1.GetImageRequest(
                    project=project,
                    image=image_spec,
                )
                image = self._images_client.get(request=request)
                return image.self_link
            except Exception:
                pass

        # Fallback: assume it's a family in ubuntu-os-cloud
        log.warning("Could not resolve image %s, using as family in ubuntu-os-cloud", image_spec)
        return f"projects/ubuntu-os-cloud/global/images/family/{image_spec}"

    def _build_network_interfaces(
        self, spec: MachineSpec, zone: str
    ) -> list:
        """Build network interface configuration.

        Args:
            spec: Machine specification
            zone: Target zone

        Returns:
            List of network interface configurations
        """
        from google.cloud import compute_v1

        if spec.networks:
            # Use specified networks
            interfaces = []
            for network in spec.networks:
                # Check if it's a subnetwork or network
                if "subnetwork" in network.lower() or "/" in network:
                    interface = compute_v1.NetworkInterface(
                        subnetwork=network,
                        access_configs=[
                            compute_v1.AccessConfig(
                                name="External NAT",
                                type_="ONE_TO_ONE_NAT",
                            )
                        ],
                    )
                else:
                    interface = compute_v1.NetworkInterface(
                        network=f"projects/{self.project_id}/global/networks/{network}",
                        access_configs=[
                            compute_v1.AccessConfig(
                                name="External NAT",
                                type_="ONE_TO_ONE_NAT",
                            )
                        ],
                    )
                interfaces.append(interface)
            return interfaces
        else:
            # Use default network
            return [
                compute_v1.NetworkInterface(
                    network=f"projects/{self.project_id}/global/networks/default",
                    access_configs=[
                        compute_v1.AccessConfig(
                            name="External NAT",
                            type_="ONE_TO_ONE_NAT",
                        )
                    ],
                )
            ]

    def _wait_for_operation(
        self, operation: Any, zone: str, timeout: int = 300
    ) -> None:
        """Wait for a zone operation to complete.

        Args:
            operation: GCP operation object
            zone: Zone where operation is running
            timeout: Maximum wait time in seconds

        Raises:
            CloudError: If operation fails or times out
        """
        import time
        from google.cloud import compute_v1

        operations_client = compute_v1.ZoneOperationsClient()
        start_time = time.time()

        while time.time() - start_time < timeout:
            request = compute_v1.GetZoneOperationRequest(
                project=self.project_id,
                zone=zone,
                operation=operation.name,
            )
            result = operations_client.get(request=request)

            if result.status == compute_v1.Operation.Status.DONE:
                if result.error:
                    errors = [e.message for e in result.error.errors]
                    raise CloudError(f"Operation failed: {'; '.join(errors)}")
                return

            time.sleep(2)

        raise CloudError(f"Operation timed out after {timeout} seconds")

    def destroy_machine(self, machine_id: str) -> bool:
        """Delete a Compute Engine instance.

        Args:
            machine_id: Instance name

        Returns:
            True if destroyed successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1
            from google.api_core.exceptions import NotFound

            request = compute_v1.DeleteInstanceRequest(
                project=self.project_id,
                zone=self.zone,
                instance=machine_id,
            )

            operation = self._instances_client.delete(request=request)
            self._wait_for_operation(operation, self.zone)

            log.info("Destroyed GCP instance: %s", machine_id)
            return True

        except NotFound as e:
            raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            raise CloudError(f"Failed to destroy GCP instance: {e}") from e

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
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1
            from google.api_core.exceptions import NotFound

            request = compute_v1.StartInstanceRequest(
                project=self.project_id,
                zone=self.zone,
                instance=machine_id,
            )

            operation = self._instances_client.start(request=request)
            self._wait_for_operation(operation, self.zone)

            log.info("Started GCP instance: %s", machine_id)
            return True

        except NotFound as e:
            raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            raise CloudError(f"Failed to start GCP instance: {e}") from e

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
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1
            from google.api_core.exceptions import NotFound

            request = compute_v1.StopInstanceRequest(
                project=self.project_id,
                zone=self.zone,
                instance=machine_id,
            )

            operation = self._instances_client.stop(request=request)
            self._wait_for_operation(operation, self.zone)

            log.info("Stopped GCP instance: %s", machine_id)
            return True

        except NotFound as e:
            raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            raise CloudError(f"Failed to stop GCP instance: {e}") from e

    def reboot_machine(self, machine_id: str) -> bool:
        """Reboot an instance using GCP's reset operation.

        Args:
            machine_id: Instance name

        Returns:
            True if rebooted successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1
            from google.api_core.exceptions import NotFound

            request = compute_v1.ResetInstanceRequest(
                project=self.project_id,
                zone=self.zone,
                instance=machine_id,
            )

            operation = self._instances_client.reset(request=request)
            self._wait_for_operation(operation, self.zone)

            log.info("Rebooted GCP instance: %s", machine_id)
            return True

        except NotFound as e:
            raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            raise CloudError(f"Failed to reboot GCP instance: {e}") from e

    def list_images(self, filters: dict | None = None) -> list[dict]:
        """List available images.

        Args:
            filters: Optional filters
                - project: Image project (default: ubuntu-os-cloud)
                - family: Filter by image family

        Returns:
            List of image info dictionaries
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1

            filters = filters or {}
            project = filters.get("project", "ubuntu-os-cloud")
            family_filter = filters.get("family", "")

            request = compute_v1.ListImagesRequest(project=project)
            images_list = self._images_client.list(request=request)

            images = []
            for image in images_list:
                # Skip deprecated images unless explicitly requested
                if hasattr(image, "deprecated") and image.deprecated:
                    if image.deprecated.state == "DEPRECATED":
                        continue

                # Apply family filter
                if family_filter:
                    if not hasattr(image, "family") or family_filter not in image.family:
                        continue

                images.append({
                    "id": str(image.id) if hasattr(image, "id") else "",
                    "name": image.name if hasattr(image, "name") else "",
                    "family": getattr(image, "family", ""),
                    "description": getattr(image, "description", ""),
                    "status": getattr(image, "status", ""),
                    "disk_size_gb": getattr(image, "disk_size_gb", 0),
                    "self_link": getattr(image, "self_link", ""),
                    "creation_timestamp": getattr(image, "creation_timestamp", ""),
                })

            return images

        except Exception as e:
            log.error("Failed to list GCP images: %s", e)
            return []

    def list_sizes(self, filters: dict | None = None) -> list[dict]:
        """List available machine types.

        Args:
            filters: Optional filters
                - zone: Zone to list machine types from

        Returns:
            List of machine type info dictionaries
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1

            filters = filters or {}
            zone = filters.get("zone", self.zone)

            request = compute_v1.ListMachineTypesRequest(
                project=self.project_id,
                zone=zone,
            )
            machine_types = self._machine_types_client.list(request=request)

            sizes = []
            for mt in machine_types:
                sizes.append({
                    "id": str(mt.id) if hasattr(mt, "id") else "",
                    "name": mt.name if hasattr(mt, "name") else "",
                    "description": getattr(mt, "description", ""),
                    "vcpus": getattr(mt, "guest_cpus", 0),
                    "memory_mb": getattr(mt, "memory_mb", 0),
                    "is_shared_cpu": getattr(mt, "is_shared_cpu", False),
                    "zone": zone,
                })

            return sizes

        except Exception as e:
            log.error("Failed to list GCP machine types: %s", e)
            return []

    def list_regions(self) -> list[dict]:
        """List available zones.

        Returns:
            List of zone info dictionaries
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1

            request = compute_v1.ListZonesRequest(project=self.project_id)
            zones = self._zones_client.list(request=request)

            regions = []
            for zone in zones:
                # Extract region from zone name (e.g., us-central1-a -> us-central1)
                region = "-".join(zone.name.split("-")[:2]) if zone.name else ""

                regions.append({
                    "id": str(zone.id) if hasattr(zone, "id") else "",
                    "name": zone.name if hasattr(zone, "name") else "",
                    "region": region,
                    "status": getattr(zone, "status", ""),
                    "description": getattr(zone, "description", ""),
                })

            return regions

        except Exception as e:
            log.error("Failed to list GCP zones: %s", e)
            return []

    def get_console_output(self, machine_id: str) -> str:
        """Get serial console output from an instance.

        Args:
            machine_id: Instance name

        Returns:
            Console output string
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1

            request = compute_v1.GetSerialPortOutputInstanceRequest(
                project=self.project_id,
                zone=self.zone,
                instance=machine_id,
            )

            response = self._instances_client.get_serial_port_output(request=request)
            return response.contents if hasattr(response, "contents") else ""

        except Exception as e:
            log.error("Failed to get console output for %s: %s", machine_id, e)
            return ""

    def list_networks(self) -> list[dict]:
        """List available VPC networks.

        Returns:
            List of network info dictionaries
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1

            request = compute_v1.ListNetworksRequest(project=self.project_id)
            networks = self._networks_client.list(request=request)

            result = []
            for network in networks:
                result.append({
                    "id": str(network.id) if hasattr(network, "id") else "",
                    "name": network.name if hasattr(network, "name") else "",
                    "description": getattr(network, "description", ""),
                    "self_link": getattr(network, "self_link", ""),
                    "auto_create_subnetworks": getattr(network, "auto_create_subnetworks", False),
                    "routing_mode": getattr(network, "routing_config", {}).get("routing_mode", ""),
                })

            return result

        except Exception as e:
            log.error("Failed to list GCP networks: %s", e)
            return []

    def list_firewalls(self) -> list[dict]:
        """List firewall rules.

        Returns:
            List of firewall rule info dictionaries
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1

            request = compute_v1.ListFirewallsRequest(project=self.project_id)
            firewalls = self._firewalls_client.list(request=request)

            result = []
            for fw in firewalls:
                allowed = []
                for rule in getattr(fw, "allowed", []):
                    allowed.append({
                        "protocol": getattr(rule, "I_p_protocol", ""),
                        "ports": list(getattr(rule, "ports", [])),
                    })

                result.append({
                    "id": str(fw.id) if hasattr(fw, "id") else "",
                    "name": fw.name if hasattr(fw, "name") else "",
                    "description": getattr(fw, "description", ""),
                    "network": getattr(fw, "network", "").split("/")[-1],
                    "direction": getattr(fw, "direction", ""),
                    "priority": getattr(fw, "priority", 1000),
                    "source_ranges": list(getattr(fw, "source_ranges", [])),
                    "target_tags": list(getattr(fw, "target_tags", [])),
                    "allowed": allowed,
                    "disabled": getattr(fw, "disabled", False),
                })

            return result

        except Exception as e:
            log.error("Failed to list GCP firewalls: %s", e)
            return []

    def create_firewall(
        self,
        name: str,
        network: str = "default",
        allowed: list[dict] | None = None,
        source_ranges: list[str] | None = None,
        target_tags: list[str] | None = None,
        description: str = "",
        priority: int = 1000,
    ) -> dict:
        """Create a firewall rule.

        Args:
            name: Firewall rule name
            network: Network name
            allowed: List of allowed protocols/ports
            source_ranges: Source IP ranges
            target_tags: Target instance tags
            description: Rule description
            priority: Rule priority (lower = higher priority)

        Returns:
            Created firewall rule info
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1

            allowed = allowed or [{"protocol": "tcp", "ports": ["22"]}]
            source_ranges = source_ranges or ["0.0.0.0/0"]

            allowed_rules = []
            for rule in allowed:
                allowed_rules.append(
                    compute_v1.Allowed(
                        I_p_protocol=rule.get("protocol", "tcp"),
                        ports=rule.get("ports", []),
                    )
                )

            firewall = compute_v1.Firewall(
                name=name,
                network=f"projects/{self.project_id}/global/networks/{network}",
                allowed=allowed_rules,
                source_ranges=source_ranges,
                description=description,
                priority=priority,
            )

            if target_tags:
                firewall.target_tags = target_tags

            request = compute_v1.InsertFirewallRequest(
                project=self.project_id,
                firewall_resource=firewall,
            )

            operation = self._firewalls_client.insert(request=request)

            # Wait for global operation
            self._wait_for_global_operation(operation)

            log.info("Created GCP firewall rule: %s", name)

            return {
                "name": name,
                "network": network,
                "allowed": allowed,
                "source_ranges": source_ranges,
                "target_tags": target_tags or [],
            }

        except Exception as e:
            raise CloudError(f"Failed to create firewall rule: {e}") from e

    def delete_firewall(self, name: str) -> bool:
        """Delete a firewall rule.

        Args:
            name: Firewall rule name

        Returns:
            True if deleted successfully
        """
        self._ensure_authenticated()

        try:
            from google.cloud import compute_v1

            request = compute_v1.DeleteFirewallRequest(
                project=self.project_id,
                firewall=name,
            )

            operation = self._firewalls_client.delete(request=request)
            self._wait_for_global_operation(operation)

            log.info("Deleted GCP firewall rule: %s", name)
            return True

        except Exception as e:
            raise CloudError(f"Failed to delete firewall rule: {e}") from e

    def _wait_for_global_operation(self, operation: Any, timeout: int = 300) -> None:
        """Wait for a global operation to complete.

        Args:
            operation: GCP operation object
            timeout: Maximum wait time in seconds

        Raises:
            CloudError: If operation fails or times out
        """
        import time
        from google.cloud import compute_v1

        operations_client = compute_v1.GlobalOperationsClient()
        start_time = time.time()

        while time.time() - start_time < timeout:
            request = compute_v1.GetGlobalOperationRequest(
                project=self.project_id,
                operation=operation.name,
            )
            result = operations_client.get(request=request)

            if result.status == compute_v1.Operation.Status.DONE:
                if result.error:
                    errors = [e.message for e in result.error.errors]
                    raise CloudError(f"Operation failed: {'; '.join(errors)}")
                return

            time.sleep(2)

        raise CloudError(f"Operation timed out after {timeout} seconds")
