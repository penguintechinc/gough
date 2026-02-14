"""Azure Cloud Provider Implementation.

Provides Azure Virtual Machine management via the Azure Resource Manager API.
Supports service principal authentication and managed identity.
"""

from __future__ import annotations

import base64
import logging
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

# Azure SDK imports with graceful handling
try:
    from azure.identity import (
        ClientSecretCredential,
        DefaultAzureCredential,
        ManagedIdentityCredential,
    )
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.compute.models import (
        DiskCreateOptionTypes,
        HardwareProfile,
        LinuxConfiguration,
        ManagedDiskParameters,
        NetworkInterfaceReference,
        NetworkProfile,
        OSProfile,
        OSDisk,
        SshConfiguration,
        SshPublicKey,
        StorageAccountTypes,
        StorageProfile,
        VirtualMachine,
        VirtualMachineImageReference,
    )
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.resource import ResourceManagementClient
    from azure.core.exceptions import (
        AzureError,
        ClientAuthenticationError,
        ResourceNotFoundError,
    )
    AZURE_AVAILABLE = True
except ImportError:
    AZURE_AVAILABLE = False


# Azure VM power state to MachineState mapping
AZURE_STATE_MAP: dict[str, MachineState] = {
    "PowerState/starting": MachineState.PENDING,
    "PowerState/running": MachineState.RUNNING,
    "PowerState/stopping": MachineState.PENDING,
    "PowerState/stopped": MachineState.STOPPED,
    "PowerState/deallocating": MachineState.PENDING,
    "PowerState/deallocated": MachineState.STOPPED,
    "PowerState/unknown": MachineState.UNKNOWN,
}

# Azure provisioning state mapping
AZURE_PROVISION_STATE_MAP: dict[str, MachineState] = {
    "Creating": MachineState.PENDING,
    "Updating": MachineState.PENDING,
    "Deleting": MachineState.PENDING,
    "Succeeded": MachineState.RUNNING,  # Will be overridden by power state
    "Failed": MachineState.ERROR,
    "Canceled": MachineState.ERROR,
}


class AzureCloud(BaseCloud):
    """Azure cloud provider for Virtual Machine management.

    Supports both service principal authentication and managed identity.
    Uses Azure Resource Manager APIs for VM lifecycle management.
    """

    provider_type: str = "azure"
    supports_cloud_init: bool = True

    # Required configuration keys
    REQUIRED_CONFIG = ["AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP"]

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize Azure cloud provider.

        Args:
            config: Configuration dictionary containing:
                - AZURE_SUBSCRIPTION_ID: Azure subscription ID (required)
                - AZURE_RESOURCE_GROUP: Resource group name (required)
                - AZURE_LOCATION: Azure region (default: eastus)
                - AZURE_CLIENT_ID: Service principal client ID (optional)
                - AZURE_CLIENT_SECRET: Service principal secret (optional)
                - AZURE_TENANT_ID: Azure AD tenant ID (optional)

        Raises:
            CloudError: If Azure SDK is not available
        """
        if not AZURE_AVAILABLE:
            raise CloudError(
                "Azure SDK not available. Install with: "
                "pip install azure-identity azure-mgmt-compute "
                "azure-mgmt-network azure-mgmt-resource"
            )

        super().__init__(config)

        # Validate required configuration
        for key in self.REQUIRED_CONFIG:
            if not config.get(key):
                raise CloudError(f"Missing required Azure configuration: {key}")

        # Extract configuration
        self.subscription_id: str = config["AZURE_SUBSCRIPTION_ID"]
        self.resource_group: str = config["AZURE_RESOURCE_GROUP"]
        self.location: str = config.get("AZURE_LOCATION", "eastus")

        # Service principal credentials (optional - can use managed identity)
        self.client_id: str | None = config.get("AZURE_CLIENT_ID")
        self.client_secret: str | None = config.get("AZURE_CLIENT_SECRET")
        self.tenant_id: str | None = config.get("AZURE_TENANT_ID")

        # Azure clients (initialized on authentication)
        self._credential: Any = None
        self._compute_client: ComputeManagementClient | None = None
        self._network_client: NetworkManagementClient | None = None
        self._resource_client: ResourceManagementClient | None = None

    def authenticate(self) -> bool:
        """Authenticate with Azure.

        Uses service principal if credentials are provided, otherwise falls
        back to managed identity or DefaultAzureCredential chain.

        Returns:
            True if authentication successful

        Raises:
            CloudAuthError: If authentication fails
        """
        try:
            # Choose authentication method
            if self.client_id and self.client_secret and self.tenant_id:
                log.info("Authenticating with Azure using service principal")
                self._credential = ClientSecretCredential(
                    tenant_id=self.tenant_id,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                )
            elif self.client_id:
                log.info("Authenticating with Azure using managed identity")
                self._credential = ManagedIdentityCredential(
                    client_id=self.client_id
                )
            else:
                log.info("Authenticating with Azure using default credentials")
                self._credential = DefaultAzureCredential()

            # Initialize management clients
            self._compute_client = ComputeManagementClient(
                credential=self._credential,
                subscription_id=self.subscription_id,
            )
            self._network_client = NetworkManagementClient(
                credential=self._credential,
                subscription_id=self.subscription_id,
            )
            self._resource_client = ResourceManagementClient(
                credential=self._credential,
                subscription_id=self.subscription_id,
            )

            # Validate credentials by listing resource groups
            list(self._resource_client.resource_groups.list())

            self._authenticated = True
            log.info("Azure authentication successful")
            return True

        except ClientAuthenticationError as e:
            raise CloudAuthError(f"Azure authentication failed: {e}")
        except AzureError as e:
            raise CloudAuthError(f"Azure API error during authentication: {e}")
        except Exception as e:
            raise CloudAuthError(f"Unexpected error during Azure auth: {e}")

    def _ensure_authenticated(self) -> None:
        """Ensure we are authenticated before making API calls."""
        if not self._authenticated or self._compute_client is None:
            self.authenticate()

    def _get_power_state(self, vm_name: str) -> str:
        """Get the power state of a VM.

        Args:
            vm_name: Name of the VM

        Returns:
            Power state string (e.g., "PowerState/running")
        """
        self._ensure_authenticated()
        assert self._compute_client is not None

        instance_view = self._compute_client.virtual_machines.instance_view(
            resource_group_name=self.resource_group,
            vm_name=vm_name,
        )

        for status in instance_view.statuses or []:
            if status.code and status.code.startswith("PowerState/"):
                return status.code

        return "PowerState/unknown"

    def _map_azure_state(
        self,
        provisioning_state: str | None,
        power_state: str | None = None,
    ) -> MachineState:
        """Map Azure VM states to unified MachineState.

        Args:
            provisioning_state: Azure provisioning state
            power_state: Azure power state (optional)

        Returns:
            Mapped MachineState
        """
        # Check provisioning state first
        if provisioning_state:
            if provisioning_state in ("Creating", "Updating", "Deleting"):
                return AZURE_PROVISION_STATE_MAP.get(
                    provisioning_state, MachineState.PENDING
                )
            if provisioning_state in ("Failed", "Canceled"):
                return MachineState.ERROR

        # Map power state if available
        if power_state:
            return AZURE_STATE_MAP.get(power_state, MachineState.UNKNOWN)

        # Default based on provisioning state
        if provisioning_state == "Succeeded":
            return MachineState.RUNNING

        return MachineState.UNKNOWN

    def _vm_to_machine(
        self,
        vm: VirtualMachine,
        include_power_state: bool = True,
    ) -> Machine:
        """Convert Azure VM to unified Machine object.

        Args:
            vm: Azure VirtualMachine object
            include_power_state: Whether to fetch power state (extra API call)

        Returns:
            Machine object
        """
        # Get power state if requested
        power_state = None
        if include_power_state and vm.name:
            try:
                power_state = self._get_power_state(vm.name)
            except Exception as e:
                log.warning(f"Failed to get power state for {vm.name}: {e}")

        # Map state
        state = self._map_azure_state(
            vm.provisioning_state,
            power_state,
        )

        # Extract IP addresses from network interfaces
        public_ips: list[str] = []
        private_ips: list[str] = []

        if vm.network_profile and vm.network_profile.network_interfaces:
            for nic_ref in vm.network_profile.network_interfaces:
                if nic_ref.id:
                    try:
                        nic_name = nic_ref.id.split("/")[-1]
                        ips = self._get_nic_ips(nic_name)
                        public_ips.extend(ips.get("public", []))
                        private_ips.extend(ips.get("private", []))
                    except Exception as e:
                        log.warning(f"Failed to get IPs for NIC {nic_ref.id}: {e}")

        # Extract image info
        image = ""
        if vm.storage_profile and vm.storage_profile.image_reference:
            img_ref = vm.storage_profile.image_reference
            if img_ref.offer and img_ref.sku:
                image = f"{img_ref.publisher}/{img_ref.offer}/{img_ref.sku}"
            elif img_ref.id:
                image = img_ref.id.split("/")[-1]

        # Extract size
        size = ""
        if vm.hardware_profile and vm.hardware_profile.vm_size:
            size = str(vm.hardware_profile.vm_size)

        # Extract tags
        tags = dict(vm.tags) if vm.tags else {}

        # Parse timestamps
        created_at = None
        if vm.time_created:
            created_at = vm.time_created

        return Machine(
            id=vm.vm_id or vm.name or "",
            name=vm.name or "",
            state=state,
            provider=self.provider_type,
            provider_id=self.config.get("provider_id", ""),
            region=vm.location or self.location,
            image=image,
            size=size,
            public_ips=public_ips,
            private_ips=private_ips,
            created_at=created_at,
            updated_at=datetime.utcnow(),
            tags=tags,
            extra={
                "provisioning_state": vm.provisioning_state,
                "power_state": power_state,
                "resource_group": self.resource_group,
                "zones": list(vm.zones) if vm.zones else [],
            },
        )

    def _get_nic_ips(self, nic_name: str) -> dict[str, list[str]]:
        """Get IP addresses from a network interface.

        Args:
            nic_name: Name of the network interface

        Returns:
            Dict with "public" and "private" IP lists
        """
        self._ensure_authenticated()
        assert self._network_client is not None

        result: dict[str, list[str]] = {"public": [], "private": []}

        try:
            nic = self._network_client.network_interfaces.get(
                resource_group_name=self.resource_group,
                network_interface_name=nic_name,
            )

            for ip_config in nic.ip_configurations or []:
                # Private IP
                if ip_config.private_ip_address:
                    result["private"].append(ip_config.private_ip_address)

                # Public IP
                if ip_config.public_ip_address and ip_config.public_ip_address.id:
                    pip_name = ip_config.public_ip_address.id.split("/")[-1]
                    try:
                        pip = self._network_client.public_ip_addresses.get(
                            resource_group_name=self.resource_group,
                            public_ip_address_name=pip_name,
                        )
                        if pip.ip_address:
                            result["public"].append(pip.ip_address)
                    except Exception:
                        pass

        except Exception as e:
            log.warning(f"Failed to get IPs for NIC {nic_name}: {e}")

        return result

    def list_machines(self, filters: dict | None = None) -> list[Machine]:
        """List all VMs in the resource group.

        Args:
            filters: Optional filters:
                - name_prefix: Filter by VM name prefix
                - tags: Filter by tags (dict)

        Returns:
            List of Machine objects
        """
        self._ensure_authenticated()
        assert self._compute_client is not None

        try:
            machines: list[Machine] = []
            vms = self._compute_client.virtual_machines.list(
                resource_group_name=self.resource_group,
            )

            for vm in vms:
                # Apply filters
                if filters:
                    # Name prefix filter
                    if "name_prefix" in filters:
                        if not vm.name or not vm.name.startswith(
                            filters["name_prefix"]
                        ):
                            continue

                    # Tag filter
                    if "tags" in filters and filters["tags"]:
                        vm_tags = vm.tags or {}
                        if not all(
                            vm_tags.get(k) == v
                            for k, v in filters["tags"].items()
                        ):
                            continue

                machines.append(self._vm_to_machine(vm))

            return machines

        except AzureError as e:
            raise CloudError(f"Failed to list Azure VMs: {e}")

    def get_machine(self, machine_id: str) -> Machine:
        """Get a specific VM by name or ID.

        Args:
            machine_id: VM name or ID

        Returns:
            Machine object

        Raises:
            CloudNotFoundError: If VM not found
        """
        self._ensure_authenticated()
        assert self._compute_client is not None

        try:
            # Try to get by name first (most common)
            vm = self._compute_client.virtual_machines.get(
                resource_group_name=self.resource_group,
                vm_name=machine_id,
            )
            return self._vm_to_machine(vm)

        except ResourceNotFoundError:
            # Try to find by VM ID
            try:
                for vm in self._compute_client.virtual_machines.list(
                    resource_group_name=self.resource_group,
                ):
                    if vm.vm_id == machine_id:
                        return self._vm_to_machine(vm)
            except AzureError:
                pass

            raise CloudNotFoundError(f"Azure VM not found: {machine_id}")

        except AzureError as e:
            raise CloudError(f"Failed to get Azure VM {machine_id}: {e}")

    def create_machine(self, spec: MachineSpec) -> Machine:
        """Create a new Azure VM.

        Args:
            spec: Machine specification with:
                - name: VM name (required)
                - image: Image reference (e.g., "Canonical/UbuntuServer/18.04-LTS")
                - size: VM size (e.g., "Standard_DS1_v2")
                - region: Azure region (optional, uses default)
                - cloud_init: Cloud-init user data
                - ssh_keys: SSH public keys for authentication
                - storage_gb: OS disk size in GB
                - tags: Resource tags
                - extra: Additional options:
                    - subnet_id: Subnet resource ID
                    - availability_zone: Zone number (1, 2, or 3)
                    - admin_username: Admin username (default: azureuser)

        Returns:
            Created Machine object

        Raises:
            CloudQuotaError: If quota exceeded
            CloudError: On API errors
        """
        self._ensure_authenticated()
        assert self._compute_client is not None
        assert self._network_client is not None

        try:
            location = spec.region or self.location
            admin_username = spec.extra.get("admin_username", "azureuser")

            # Parse image reference
            image_reference = self._parse_image_reference(spec.image)

            # Create network interface
            nic_name = f"{spec.name}-nic"
            nic = self._create_network_interface(
                nic_name=nic_name,
                location=location,
                subnet_id=spec.extra.get("subnet_id"),
                tags=spec.tags,
            )

            # Prepare OS profile
            os_profile = OSProfile(
                computer_name=spec.name,
                admin_username=admin_username,
            )

            # Configure SSH keys if provided
            if spec.ssh_keys:
                ssh_config = SshConfiguration(
                    public_keys=[
                        SshPublicKey(
                            path=f"/home/{admin_username}/.ssh/authorized_keys",
                            key_data=key,
                        )
                        for key in spec.ssh_keys
                    ]
                )
                os_profile.linux_configuration = LinuxConfiguration(
                    disable_password_authentication=True,
                    ssh=ssh_config,
                )
            else:
                os_profile.linux_configuration = LinuxConfiguration(
                    disable_password_authentication=False,
                )

            # Add cloud-init custom data
            if spec.cloud_init:
                os_profile.custom_data = base64.b64encode(
                    spec.cloud_init.encode("utf-8")
                ).decode("utf-8")

            # Prepare storage profile
            storage_profile = StorageProfile(
                image_reference=image_reference,
                os_disk=OSDisk(
                    name=f"{spec.name}-osdisk",
                    caching="ReadWrite",
                    create_option=DiskCreateOptionTypes.FROM_IMAGE,
                    managed_disk=ManagedDiskParameters(
                        storage_account_type=StorageAccountTypes.STANDARD_SSD_LRS,
                    ),
                ),
            )

            # Set disk size if specified
            if spec.storage_gb > 0:
                storage_profile.os_disk.disk_size_gb = spec.storage_gb

            # Prepare hardware profile
            hardware_profile = HardwareProfile(
                vm_size=spec.size or "Standard_DS1_v2",
            )

            # Prepare network profile
            network_profile = NetworkProfile(
                network_interfaces=[
                    NetworkInterfaceReference(
                        id=nic.id,
                        primary=True,
                    )
                ]
            )

            # Prepare VM parameters
            vm_params = VirtualMachine(
                location=location,
                tags=spec.tags or {},
                hardware_profile=hardware_profile,
                storage_profile=storage_profile,
                os_profile=os_profile,
                network_profile=network_profile,
            )

            # Add availability zone if specified
            if spec.extra.get("availability_zone"):
                vm_params.zones = [str(spec.extra["availability_zone"])]

            # Create VM
            log.info(f"Creating Azure VM: {spec.name} in {location}")
            poller = self._compute_client.virtual_machines.begin_create_or_update(
                resource_group_name=self.resource_group,
                vm_name=spec.name,
                parameters=vm_params,
            )

            # Wait for creation to complete
            vm = poller.result()

            log.info(f"Azure VM created: {spec.name}")
            return self._vm_to_machine(vm)

        except AzureError as e:
            error_msg = str(e)
            if "QuotaExceeded" in error_msg or "quota" in error_msg.lower():
                raise CloudQuotaError(f"Azure quota exceeded: {e}")
            raise CloudError(f"Failed to create Azure VM: {e}")
        except Exception as e:
            raise CloudError(f"Unexpected error creating Azure VM: {e}")

    def _parse_image_reference(self, image: str) -> VirtualMachineImageReference:
        """Parse image string to Azure image reference.

        Supports formats:
            - "publisher/offer/sku" or "publisher/offer/sku/version"
            - Full resource ID
            - Common aliases (ubuntu-22.04, etc.)

        Args:
            image: Image reference string

        Returns:
            VirtualMachineImageReference object
        """
        # Common image aliases
        IMAGE_ALIASES: dict[str, tuple[str, str, str, str]] = {
            "ubuntu-24.04": ("Canonical", "ubuntu-24_04-lts", "server", "latest"),
            "ubuntu-22.04": ("Canonical", "0001-com-ubuntu-server-jammy", "22_04-lts", "latest"),
            "ubuntu-20.04": ("Canonical", "0001-com-ubuntu-server-focal", "20_04-lts", "latest"),
            "ubuntu-18.04": ("Canonical", "UbuntuServer", "18.04-LTS", "latest"),
            "debian-12": ("Debian", "debian-12", "12", "latest"),
            "debian-11": ("Debian", "debian-11", "11", "latest"),
            "rhel-9": ("RedHat", "RHEL", "9-lvm", "latest"),
            "rhel-8": ("RedHat", "RHEL", "8-lvm", "latest"),
            "centos-7": ("OpenLogic", "CentOS", "7.9", "latest"),
            "windows-2022": ("MicrosoftWindowsServer", "WindowsServer", "2022-Datacenter", "latest"),
            "windows-2019": ("MicrosoftWindowsServer", "WindowsServer", "2019-Datacenter", "latest"),
        }

        # Check for alias
        if image.lower() in IMAGE_ALIASES:
            publisher, offer, sku, version = IMAGE_ALIASES[image.lower()]
            return VirtualMachineImageReference(
                publisher=publisher,
                offer=offer,
                sku=sku,
                version=version,
            )

        # Check for full resource ID
        if image.startswith("/"):
            return VirtualMachineImageReference(id=image)

        # Parse publisher/offer/sku[/version] format
        parts = image.split("/")
        if len(parts) >= 3:
            return VirtualMachineImageReference(
                publisher=parts[0],
                offer=parts[1],
                sku=parts[2],
                version=parts[3] if len(parts) > 3 else "latest",
            )

        # Default: treat as URN or ID
        return VirtualMachineImageReference(id=image)

    def _create_network_interface(
        self,
        nic_name: str,
        location: str,
        subnet_id: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> Any:
        """Create a network interface for a VM.

        Args:
            nic_name: Name for the network interface
            location: Azure region
            subnet_id: Subnet resource ID (optional)
            tags: Resource tags

        Returns:
            NetworkInterface object
        """
        assert self._network_client is not None

        # If no subnet specified, try to use default VNet
        if not subnet_id:
            subnet_id = self._get_default_subnet_id(location)

        # Create public IP address
        pip_name = f"{nic_name}-pip"
        pip_params = {
            "location": location,
            "sku": {"name": "Standard"},
            "public_ip_allocation_method": "Static",
            "public_ip_address_version": "IPv4",
            "tags": tags or {},
        }

        pip_poller = self._network_client.public_ip_addresses.begin_create_or_update(
            resource_group_name=self.resource_group,
            public_ip_address_name=pip_name,
            parameters=pip_params,
        )
        pip = pip_poller.result()

        # Create NIC
        nic_params = {
            "location": location,
            "ip_configurations": [
                {
                    "name": "ipconfig1",
                    "subnet": {"id": subnet_id},
                    "public_ip_address": {"id": pip.id},
                    "private_ip_allocation_method": "Dynamic",
                }
            ],
            "tags": tags or {},
        }

        nic_poller = self._network_client.network_interfaces.begin_create_or_update(
            resource_group_name=self.resource_group,
            network_interface_name=nic_name,
            parameters=nic_params,
        )

        return nic_poller.result()

    def _get_default_subnet_id(self, location: str) -> str:
        """Get or create a default subnet for VM deployment.

        Args:
            location: Azure region

        Returns:
            Subnet resource ID
        """
        assert self._network_client is not None

        vnet_name = f"gough-vnet-{location}"
        subnet_name = "default"

        try:
            # Try to get existing subnet
            subnet = self._network_client.subnets.get(
                resource_group_name=self.resource_group,
                virtual_network_name=vnet_name,
                subnet_name=subnet_name,
            )
            return subnet.id

        except ResourceNotFoundError:
            # Create VNet and subnet
            log.info(f"Creating default VNet: {vnet_name}")

            vnet_params = {
                "location": location,
                "address_space": {"address_prefixes": ["10.0.0.0/16"]},
                "subnets": [
                    {
                        "name": subnet_name,
                        "address_prefix": "10.0.0.0/24",
                    }
                ],
            }

            vnet_poller = self._network_client.virtual_networks.begin_create_or_update(
                resource_group_name=self.resource_group,
                virtual_network_name=vnet_name,
                parameters=vnet_params,
            )
            vnet = vnet_poller.result()

            # Get the subnet ID
            return f"{vnet.id}/subnets/{subnet_name}"

    def destroy_machine(self, machine_id: str) -> bool:
        """Destroy/delete an Azure VM and its resources.

        Args:
            machine_id: VM name or ID

        Returns:
            True if destroyed successfully

        Raises:
            CloudNotFoundError: If VM not found
            CloudError: On API errors
        """
        self._ensure_authenticated()
        assert self._compute_client is not None
        assert self._network_client is not None

        # Get VM to find associated resources
        machine = self.get_machine(machine_id)
        vm_name = machine.name

        try:
            log.info(f"Deleting Azure VM: {vm_name}")

            # Get VM details for resource cleanup
            vm = self._compute_client.virtual_machines.get(
                resource_group_name=self.resource_group,
                vm_name=vm_name,
            )

            # Collect resources to delete
            nic_ids: list[str] = []
            disk_names: list[str] = []

            if vm.network_profile and vm.network_profile.network_interfaces:
                nic_ids = [
                    nic.id for nic in vm.network_profile.network_interfaces if nic.id
                ]

            if vm.storage_profile and vm.storage_profile.os_disk:
                if vm.storage_profile.os_disk.name:
                    disk_names.append(vm.storage_profile.os_disk.name)

            # Delete VM
            poller = self._compute_client.virtual_machines.begin_delete(
                resource_group_name=self.resource_group,
                vm_name=vm_name,
            )
            poller.result()

            # Delete NICs and their public IPs
            for nic_id in nic_ids:
                nic_name = nic_id.split("/")[-1]
                try:
                    # Get NIC to find public IP
                    nic = self._network_client.network_interfaces.get(
                        resource_group_name=self.resource_group,
                        network_interface_name=nic_name,
                    )

                    pip_ids: list[str] = []
                    for ip_config in nic.ip_configurations or []:
                        if ip_config.public_ip_address and ip_config.public_ip_address.id:
                            pip_ids.append(ip_config.public_ip_address.id)

                    # Delete NIC
                    self._network_client.network_interfaces.begin_delete(
                        resource_group_name=self.resource_group,
                        network_interface_name=nic_name,
                    ).result()

                    # Delete public IPs
                    for pip_id in pip_ids:
                        pip_name = pip_id.split("/")[-1]
                        try:
                            self._network_client.public_ip_addresses.begin_delete(
                                resource_group_name=self.resource_group,
                                public_ip_address_name=pip_name,
                            ).result()
                        except Exception as e:
                            log.warning(f"Failed to delete public IP {pip_name}: {e}")

                except Exception as e:
                    log.warning(f"Failed to delete NIC {nic_name}: {e}")

            # Delete OS disk
            for disk_name in disk_names:
                try:
                    self._compute_client.disks.begin_delete(
                        resource_group_name=self.resource_group,
                        disk_name=disk_name,
                    ).result()
                except Exception as e:
                    log.warning(f"Failed to delete disk {disk_name}: {e}")

            log.info(f"Azure VM deleted: {vm_name}")
            return True

        except ResourceNotFoundError:
            raise CloudNotFoundError(f"Azure VM not found: {machine_id}")
        except AzureError as e:
            raise CloudError(f"Failed to delete Azure VM: {e}")

    def start_machine(self, machine_id: str) -> bool:
        """Start a stopped Azure VM.

        Args:
            machine_id: VM name or ID

        Returns:
            True if started successfully

        Raises:
            CloudNotFoundError: If VM not found
            CloudError: On API errors
        """
        self._ensure_authenticated()
        assert self._compute_client is not None

        # Get VM to ensure it exists and get name
        machine = self.get_machine(machine_id)
        vm_name = machine.name

        try:
            log.info(f"Starting Azure VM: {vm_name}")
            poller = self._compute_client.virtual_machines.begin_start(
                resource_group_name=self.resource_group,
                vm_name=vm_name,
            )
            poller.result()

            log.info(f"Azure VM started: {vm_name}")
            return True

        except ResourceNotFoundError:
            raise CloudNotFoundError(f"Azure VM not found: {machine_id}")
        except AzureError as e:
            raise CloudError(f"Failed to start Azure VM: {e}")

    def stop_machine(self, machine_id: str) -> bool:
        """Stop a running Azure VM.

        Deallocates the VM to stop billing for compute resources.

        Args:
            machine_id: VM name or ID

        Returns:
            True if stopped successfully

        Raises:
            CloudNotFoundError: If VM not found
            CloudError: On API errors
        """
        self._ensure_authenticated()
        assert self._compute_client is not None

        # Get VM to ensure it exists and get name
        machine = self.get_machine(machine_id)
        vm_name = machine.name

        try:
            log.info(f"Stopping Azure VM: {vm_name}")
            # Use deallocate to fully stop and release resources
            poller = self._compute_client.virtual_machines.begin_deallocate(
                resource_group_name=self.resource_group,
                vm_name=vm_name,
            )
            poller.result()

            log.info(f"Azure VM stopped: {vm_name}")
            return True

        except ResourceNotFoundError:
            raise CloudNotFoundError(f"Azure VM not found: {machine_id}")
        except AzureError as e:
            raise CloudError(f"Failed to stop Azure VM: {e}")

    def reboot_machine(self, machine_id: str) -> bool:
        """Reboot an Azure VM.

        Args:
            machine_id: VM name or ID

        Returns:
            True if rebooted successfully

        Raises:
            CloudNotFoundError: If VM not found
            CloudError: On API errors
        """
        self._ensure_authenticated()
        assert self._compute_client is not None

        # Get VM to ensure it exists and get name
        machine = self.get_machine(machine_id)
        vm_name = machine.name

        try:
            log.info(f"Rebooting Azure VM: {vm_name}")
            poller = self._compute_client.virtual_machines.begin_restart(
                resource_group_name=self.resource_group,
                vm_name=vm_name,
            )
            poller.result()

            log.info(f"Azure VM rebooted: {vm_name}")
            return True

        except ResourceNotFoundError:
            raise CloudNotFoundError(f"Azure VM not found: {machine_id}")
        except AzureError as e:
            raise CloudError(f"Failed to reboot Azure VM: {e}")

    def list_images(self, filters: dict | None = None) -> list[dict]:
        """List available VM images.

        Args:
            filters: Optional filters:
                - publisher: Filter by publisher
                - offer: Filter by offer
                - location: Azure region (default: configured location)

        Returns:
            List of image info dictionaries
        """
        self._ensure_authenticated()
        assert self._compute_client is not None

        location = (filters or {}).get("location", self.location)
        publisher = (filters or {}).get("publisher")

        images: list[dict] = []

        try:
            if publisher:
                # List images from specific publisher
                offers = self._compute_client.virtual_machine_images.list_offers(
                    location=location,
                    publisher_name=publisher,
                )
                for offer in offers:
                    offer_filter = (filters or {}).get("offer")
                    if offer_filter and offer.name != offer_filter:
                        continue

                    skus = self._compute_client.virtual_machine_images.list_skus(
                        location=location,
                        publisher_name=publisher,
                        offer=offer.name,
                    )
                    for sku in skus:
                        images.append({
                            "id": f"{publisher}/{offer.name}/{sku.name}",
                            "name": f"{offer.name} {sku.name}",
                            "publisher": publisher,
                            "offer": offer.name,
                            "sku": sku.name,
                        })
            else:
                # Return common image aliases
                for alias, (pub, offer, sku, _) in self._parse_image_reference.__code__.co_consts:
                    if isinstance(alias, str) and isinstance(pub, str):
                        images.append({
                            "id": alias,
                            "name": alias,
                            "publisher": pub,
                            "offer": offer,
                            "sku": sku,
                        })

        except AzureError as e:
            log.warning(f"Failed to list Azure images: {e}")

        # Add common aliases as fallback
        common_images = [
            {"id": "ubuntu-24.04", "name": "Ubuntu 24.04 LTS", "publisher": "Canonical"},
            {"id": "ubuntu-22.04", "name": "Ubuntu 22.04 LTS", "publisher": "Canonical"},
            {"id": "ubuntu-20.04", "name": "Ubuntu 20.04 LTS", "publisher": "Canonical"},
            {"id": "debian-12", "name": "Debian 12", "publisher": "Debian"},
            {"id": "debian-11", "name": "Debian 11", "publisher": "Debian"},
            {"id": "rhel-9", "name": "Red Hat Enterprise Linux 9", "publisher": "RedHat"},
            {"id": "rhel-8", "name": "Red Hat Enterprise Linux 8", "publisher": "RedHat"},
            {"id": "windows-2022", "name": "Windows Server 2022", "publisher": "Microsoft"},
            {"id": "windows-2019", "name": "Windows Server 2019", "publisher": "Microsoft"},
        ]

        if not images:
            images = common_images

        return images

    def list_sizes(self, filters: dict | None = None) -> list[dict]:
        """List available VM sizes.

        Args:
            filters: Optional filters:
                - location: Azure region (default: configured location)
                - family: VM family filter (e.g., "D", "E", "F")

        Returns:
            List of size info dictionaries
        """
        self._ensure_authenticated()
        assert self._compute_client is not None

        location = (filters or {}).get("location", self.location)
        family_filter = (filters or {}).get("family")

        sizes: list[dict] = []

        try:
            vm_sizes = self._compute_client.virtual_machine_sizes.list(
                location=location,
            )

            for size in vm_sizes:
                # Apply family filter
                if family_filter:
                    if not size.name or family_filter.upper() not in size.name.upper():
                        continue

                sizes.append({
                    "id": size.name,
                    "name": size.name,
                    "vcpus": size.number_of_cores,
                    "memory_mb": size.memory_in_mb,
                    "os_disk_size_mb": size.os_disk_size_in_mb,
                    "resource_disk_size_mb": size.resource_disk_size_in_mb,
                    "max_data_disk_count": size.max_data_disk_count,
                })

        except AzureError as e:
            log.warning(f"Failed to list Azure VM sizes: {e}")

        return sizes

    def list_regions(self) -> list[dict]:
        """List available Azure regions.

        Returns:
            List of region info dictionaries
        """
        self._ensure_authenticated()
        assert self._resource_client is not None

        regions: list[dict] = []

        try:
            locations = self._resource_client.subscriptions.list_locations(
                subscription_id=self.subscription_id,
            )

            for loc in locations:
                if loc.metadata and loc.metadata.region_type == "Physical":
                    regions.append({
                        "id": loc.name,
                        "name": loc.display_name,
                        "geography": loc.metadata.geography if loc.metadata else "",
                        "paired_region": (
                            loc.metadata.paired_region[0].name
                            if loc.metadata and loc.metadata.paired_region
                            else ""
                        ),
                    })

        except AzureError as e:
            log.warning(f"Failed to list Azure regions: {e}")

        return regions

    def get_console_output(self, machine_id: str) -> str:
        """Get boot diagnostics console output from a VM.

        Note: Requires boot diagnostics to be enabled on the VM.

        Args:
            machine_id: VM name or ID

        Returns:
            Console output string (may be empty if not available)
        """
        self._ensure_authenticated()
        assert self._compute_client is not None

        # Get VM to ensure it exists and get name
        machine = self.get_machine(machine_id)
        vm_name = machine.name

        try:
            # Get boot diagnostics
            instance_view = self._compute_client.virtual_machines.instance_view(
                resource_group_name=self.resource_group,
                vm_name=vm_name,
            )

            if instance_view.boot_diagnostics:
                # Try to get serial console output
                if instance_view.boot_diagnostics.serial_console_log_blob_uri:
                    # Would need to fetch from blob storage
                    return f"Console output available at: {instance_view.boot_diagnostics.serial_console_log_blob_uri}"

            return ""

        except AzureError as e:
            log.warning(f"Failed to get console output for {vm_name}: {e}")
            return ""
