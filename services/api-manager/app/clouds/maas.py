"""Canonical MaaS (Metal as a Service) Cloud Provider Implementation.

Provides integration with MaaS for bare metal machine management including
commissioning, deployment, release, and power management operations.

MaaS API Documentation: https://maas.io/docs/api
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import requests
from oauthlib.oauth1 import Client

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

logger = logging.getLogger(__name__)


# MaaS machine status to MachineState mapping
MAAS_STATE_MAP: dict[str, MachineState] = {
    # Node statuses from MaaS
    "New": MachineState.PENDING,
    "Commissioning": MachineState.COMMISSIONING,
    "Failed commissioning": MachineState.ERROR,
    "Missing": MachineState.ERROR,
    "Ready": MachineState.READY,
    "Reserved": MachineState.ALLOCATED,
    "Allocated": MachineState.ALLOCATED,
    "Deploying": MachineState.DEPLOYING,
    "Deployed": MachineState.RUNNING,
    "Retired": MachineState.TERMINATED,
    "Broken": MachineState.ERROR,
    "Failed deployment": MachineState.ERROR,
    "Releasing": MachineState.PENDING,
    "Failed releasing": MachineState.ERROR,
    "Disk erasing": MachineState.PENDING,
    "Failed disk erasing": MachineState.ERROR,
    "Rescue mode": MachineState.RUNNING,
    "Entering rescue mode": MachineState.PENDING,
    "Failed to enter rescue mode": MachineState.ERROR,
    "Exiting rescue mode": MachineState.PENDING,
    "Failed to exit rescue mode": MachineState.ERROR,
    "Testing": MachineState.PENDING,
    "Failed testing": MachineState.ERROR,
}

# MaaS power states
MAAS_POWER_STATE_MAP: dict[str, MachineState] = {
    "on": MachineState.RUNNING,
    "off": MachineState.STOPPED,
    "unknown": MachineState.UNKNOWN,
    "error": MachineState.ERROR,
}


class MaaSCloud(BaseCloud):
    """Canonical MaaS cloud provider implementation.

    Supports OAuth 1.0a authentication and provides machine lifecycle
    management for bare metal servers.

    Configuration:
        MAAS_API_URL: MaaS API URL (e.g., http://maas.example.com/MAAS)
        MAAS_API_KEY: OAuth credentials in format consumer_key:token:secret
    """

    provider_type: str = "maas"
    supports_cloud_init: bool = True

    # API version to use
    API_VERSION: str = "2.0"

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize MaaS cloud provider.

        Args:
            config: Configuration dictionary containing:
                - MAAS_API_URL: MaaS API base URL
                - MAAS_API_KEY: OAuth key in consumer_key:token:secret format
                - provider_id: Cloud providers table ID (optional)

        Raises:
            CloudAuthError: If required configuration is missing
        """
        super().__init__(config)

        self.api_url = config.get("MAAS_API_URL", "").rstrip("/")
        self.api_key = config.get("MAAS_API_KEY", "")
        self.provider_id = config.get("provider_id", "")

        if not self.api_url:
            raise CloudAuthError("MAAS_API_URL is required")
        if not self.api_key:
            raise CloudAuthError("MAAS_API_KEY is required")

        # Parse OAuth credentials
        try:
            parts = self.api_key.split(":")
            if len(parts) != 3:
                raise ValueError("Invalid format")
            self.consumer_key = parts[0]
            self.token_key = parts[1]
            self.token_secret = parts[2]
        except ValueError as e:
            raise CloudAuthError(
                f"MAAS_API_KEY must be in format consumer_key:token:secret: {e}"
            ) from e

        # Initialize OAuth client
        self._oauth_client = Client(
            client_key=self.consumer_key,
            resource_owner_key=self.token_key,
            resource_owner_secret=self.token_secret,
            signature_method="PLAINTEXT",
        )

        # Session for connection pooling
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        })

        # Request timeout in seconds
        self.timeout = config.get("timeout", 30)

    def _get_api_url(self, endpoint: str) -> str:
        """Build full API URL for an endpoint.

        Args:
            endpoint: API endpoint path

        Returns:
            Full API URL
        """
        return f"{self.api_url}/api/{self.API_VERSION}/{endpoint.lstrip('/')}"

    def _sign_request(
        self,
        method: str,
        url: str,
        body: str | None = None,
    ) -> dict[str, str]:
        """Sign a request using OAuth 1.0a.

        Args:
            method: HTTP method
            url: Full URL
            body: Request body

        Returns:
            Headers dictionary with OAuth authorization
        """
        uri, headers, _ = self._oauth_client.sign(
            uri=url,
            http_method=method,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return dict(headers)

    def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated API request.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint
            data: Form data for POST/PUT
            params: Query parameters

        Returns:
            JSON response data

        Raises:
            CloudAuthError: On authentication errors
            CloudNotFoundError: On 404 errors
            CloudQuotaError: On quota/capacity errors
            CloudError: On other API errors
        """
        url = self._get_api_url(endpoint)

        if params:
            url = f"{url}?{urlencode(params)}"

        body = urlencode(data) if data else None
        headers = self._sign_request(method, url, body)

        try:
            response = self._session.request(
                method=method,
                url=url,
                data=body,
                headers=headers,
                timeout=self.timeout,
            )

            # Handle HTTP errors
            if response.status_code == 401:
                raise CloudAuthError("MaaS authentication failed: invalid credentials")
            elif response.status_code == 403:
                raise CloudAuthError(
                    f"MaaS authorization denied: {response.text}"
                )
            elif response.status_code == 404:
                raise CloudNotFoundError(f"Resource not found: {endpoint}")
            elif response.status_code == 409:
                # Conflict - often quota or state conflict
                raise CloudQuotaError(f"Resource conflict: {response.text}")
            elif response.status_code >= 400:
                raise CloudError(
                    f"MaaS API error {response.status_code}: {response.text}"
                )

            # Return JSON if available
            if response.content:
                try:
                    return response.json()
                except ValueError:
                    return response.text

            return None

        except requests.exceptions.ConnectionError as e:
            raise CloudError(f"Failed to connect to MaaS: {e}") from e
        except requests.exceptions.Timeout as e:
            raise CloudError(f"MaaS request timeout: {e}") from e
        except requests.exceptions.RequestException as e:
            raise CloudError(f"MaaS request failed: {e}") from e

    def _map_state(self, maas_status: str, power_state: str | None = None) -> MachineState:
        """Map MaaS status and power state to unified MachineState.

        Args:
            maas_status: MaaS status_name field
            power_state: MaaS power_state field (optional)

        Returns:
            Unified MachineState
        """
        # First check the machine status
        state = MAAS_STATE_MAP.get(maas_status, MachineState.UNKNOWN)

        # For deployed machines, also consider power state
        if state == MachineState.RUNNING and power_state:
            power_mapped = MAAS_POWER_STATE_MAP.get(power_state)
            if power_mapped == MachineState.STOPPED:
                return MachineState.STOPPED

        return state

    def _parse_machine(self, data: dict[str, Any]) -> Machine:
        """Parse MaaS machine data into Machine object.

        Args:
            data: MaaS machine JSON data

        Returns:
            Machine object
        """
        # Extract IPs from interface data
        public_ips: list[str] = []
        private_ips: list[str] = []

        for interface in data.get("interface_set", []):
            for link in interface.get("links", []):
                ip_address = link.get("ip_address")
                if ip_address:
                    # MaaS doesn't distinguish public/private well
                    # Use simple heuristic based on RFC1918
                    if self._is_private_ip(ip_address):
                        private_ips.append(ip_address)
                    else:
                        public_ips.append(ip_address)

        # Parse timestamps
        created_at = None
        if data.get("created"):
            try:
                created_at = datetime.fromisoformat(
                    data["created"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        updated_at = None
        if data.get("updated"):
            try:
                updated_at = datetime.fromisoformat(
                    data["updated"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # Build tags dict from tag list
        tags = {}
        for tag in data.get("tag_names", []):
            tags[tag] = "true"

        # Get distro info for image
        image = ""
        if data.get("distro_series"):
            image = f"{data.get('osystem', 'ubuntu')}/{data['distro_series']}"

        # Get zone for region
        zone = data.get("zone", {})
        region = zone.get("name", "") if isinstance(zone, dict) else str(zone)

        return Machine(
            id=data.get("system_id", ""),
            name=data.get("hostname", data.get("fqdn", "")),
            state=self._map_state(
                data.get("status_name", "Unknown"),
                data.get("power_state"),
            ),
            provider=self.provider_type,
            provider_id=self.provider_id,
            region=region,
            image=image,
            size=self._get_machine_size(data),
            public_ips=public_ips,
            private_ips=private_ips,
            created_at=created_at,
            updated_at=updated_at,
            tags=tags,
            extra={
                "fqdn": data.get("fqdn", ""),
                "system_id": data.get("system_id", ""),
                "architecture": data.get("architecture", ""),
                "cpu_count": data.get("cpu_count", 0),
                "memory_mb": data.get("memory", 0),
                "storage_gb": self._calculate_storage(data),
                "power_type": data.get("power_type", ""),
                "power_state": data.get("power_state", ""),
                "status": data.get("status_name", ""),
                "status_message": data.get("status_message", ""),
                "pool": data.get("pool", {}).get("name", ""),
                "owner": data.get("owner", ""),
            },
        )

    def _is_private_ip(self, ip: str) -> bool:
        """Check if IP address is private (RFC1918).

        Args:
            ip: IP address string

        Returns:
            True if private IP
        """
        try:
            parts = [int(p) for p in ip.split(".")]
            if len(parts) != 4:
                return True  # Assume private if not valid IPv4

            # 10.0.0.0/8
            if parts[0] == 10:
                return True
            # 172.16.0.0/12
            if parts[0] == 172 and 16 <= parts[1] <= 31:
                return True
            # 192.168.0.0/16
            if parts[0] == 192 and parts[1] == 168:
                return True
            # Loopback
            if parts[0] == 127:
                return True

            return False
        except (ValueError, AttributeError):
            return True

    def _get_machine_size(self, data: dict[str, Any]) -> str:
        """Generate a size description from machine specs.

        Args:
            data: MaaS machine data

        Returns:
            Human-readable size string
        """
        cpu_count = data.get("cpu_count", 0)
        memory_mb = data.get("memory", 0)
        storage_gb = self._calculate_storage(data)

        parts = []
        if cpu_count:
            parts.append(f"{cpu_count}cpu")
        if memory_mb:
            parts.append(f"{memory_mb // 1024}GB")
        if storage_gb:
            parts.append(f"{storage_gb}GB")

        return "-".join(parts) if parts else "unknown"

    def _calculate_storage(self, data: dict[str, Any]) -> int:
        """Calculate total storage from block devices.

        Args:
            data: MaaS machine data

        Returns:
            Total storage in GB
        """
        total_bytes = 0
        for device in data.get("blockdevice_set", []):
            total_bytes += device.get("size", 0)

        for device in data.get("physicalblockdevice_set", []):
            total_bytes += device.get("size", 0)

        return total_bytes // (1024 ** 3)

    def authenticate(self) -> bool:
        """Authenticate with MaaS by testing API access.

        Returns:
            True if authentication successful

        Raises:
            CloudAuthError: If authentication fails
        """
        try:
            # Test authentication by fetching version info
            self._request("GET", "version/")
            self._authenticated = True
            logger.info("MaaS authentication successful")
            return True
        except CloudAuthError:
            self._authenticated = False
            raise
        except CloudError as e:
            self._authenticated = False
            raise CloudAuthError(f"MaaS authentication failed: {e}") from e

    def list_machines(self, filters: dict | None = None) -> list[Machine]:
        """List all machines in MaaS.

        Args:
            filters: Optional filters:
                - hostname: Filter by hostname pattern
                - zone: Filter by zone name
                - pool: Filter by resource pool
                - status: Filter by status
                - tags: Filter by tags (list)

        Returns:
            List of Machine objects

        Raises:
            CloudError: On API errors
        """
        params = {}

        if filters:
            if filters.get("hostname"):
                params["hostname"] = filters["hostname"]
            if filters.get("zone"):
                params["zone"] = filters["zone"]
            if filters.get("pool"):
                params["pool"] = filters["pool"]
            if filters.get("status"):
                params["status"] = filters["status"]
            if filters.get("tags"):
                # MaaS accepts multiple tags parameter
                params["tags"] = ",".join(filters["tags"])

        data = self._request("GET", "machines/", params=params)

        machines = []
        for machine_data in data or []:
            try:
                machines.append(self._parse_machine(machine_data))
            except Exception as e:
                logger.warning(
                    f"Failed to parse machine {machine_data.get('system_id')}: {e}"
                )

        return machines

    def get_machine(self, machine_id: str) -> Machine:
        """Get a specific machine by system_id.

        Args:
            machine_id: MaaS system_id

        Returns:
            Machine object

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        data = self._request("GET", f"machines/{machine_id}/")
        return self._parse_machine(data)

    def create_machine(self, spec: MachineSpec) -> Machine:
        """Allocate and deploy a machine in MaaS.

        This is a two-step process:
        1. Allocate a machine matching the spec
        2. Deploy the allocated machine with the OS image

        Args:
            spec: Machine specification including:
                - name: Desired hostname
                - image: OS image (e.g., "ubuntu/jammy", "ubuntu/focal")
                - size: Not used directly (MaaS uses physical hardware)
                - region: Zone name
                - cloud_init: User data script
                - ssh_keys: SSH public keys (added to deployed user)
                - networks: Network constraints
                - extra: Additional options:
                    - pool: Resource pool name
                    - tags: Required machine tags
                    - cpu_count: Minimum CPU count
                    - memory: Minimum memory (MB)
                    - arch: Architecture (e.g., "amd64")

        Returns:
            Deployed Machine object (in DEPLOYING state initially)

        Raises:
            CloudQuotaError: If no matching machines available
            CloudError: On API errors
        """
        # Step 1: Allocate a machine
        allocate_data: dict[str, Any] = {}

        if spec.name:
            allocate_data["name"] = spec.name

        if spec.region:
            allocate_data["zone"] = spec.region

        # Handle extra options
        if spec.extra.get("pool"):
            allocate_data["pool"] = spec.extra["pool"]
        if spec.extra.get("tags"):
            allocate_data["tags"] = ",".join(spec.extra["tags"])
        if spec.extra.get("cpu_count"):
            allocate_data["cpu_count"] = spec.extra["cpu_count"]
        if spec.extra.get("memory"):
            allocate_data["mem"] = spec.extra["memory"]
        if spec.extra.get("arch"):
            allocate_data["arch"] = spec.extra["arch"]

        # Network constraints
        if spec.networks:
            # MaaS uses fabric/vlan/subnet constraints
            allocate_data["interfaces"] = ",".join(spec.networks)

        try:
            allocated = self._request("POST", "machines/", data={"op": "allocate", **allocate_data})
        except CloudError as e:
            if "No matching machine" in str(e) or "409" in str(e):
                raise CloudQuotaError(
                    "No machines available matching the specification"
                ) from e
            raise

        system_id = allocated.get("system_id")
        if not system_id:
            raise CloudError("Failed to allocate machine: no system_id returned")

        logger.info(f"Allocated machine {system_id} for deployment")

        # Step 2: Deploy the machine
        deploy_data: dict[str, Any] = {}

        # Parse image specification (format: osystem/distro_series)
        if spec.image:
            if "/" in spec.image:
                osystem, distro_series = spec.image.split("/", 1)
                deploy_data["osystem"] = osystem
                deploy_data["distro_series"] = distro_series
            else:
                deploy_data["distro_series"] = spec.image

        # Cloud-init user data
        if spec.cloud_init:
            deploy_data["user_data"] = spec.cloud_init

        try:
            deployed = self._request(
                "POST",
                f"machines/{system_id}/",
                data={"op": "deploy", **deploy_data},
            )
        except CloudError as e:
            # Release the allocated machine on deploy failure
            logger.error(f"Deploy failed for {system_id}, releasing: {e}")
            try:
                self._request(
                    "POST",
                    f"machines/{system_id}/",
                    data={"op": "release"},
                )
            except CloudError:
                pass
            raise

        return self._parse_machine(deployed)

    def destroy_machine(self, machine_id: str) -> bool:
        """Release a machine back to the pool.

        In MaaS, machines are physical and cannot be destroyed.
        This releases the machine for reuse.

        Args:
            machine_id: MaaS system_id

        Returns:
            True if released successfully

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        self._request(
            "POST",
            f"machines/{machine_id}/",
            data={"op": "release"},
        )
        logger.info(f"Released machine {machine_id}")
        return True

    def start_machine(self, machine_id: str) -> bool:
        """Power on a machine.

        Args:
            machine_id: MaaS system_id

        Returns:
            True if power on command sent successfully

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        self._request(
            "POST",
            f"machines/{machine_id}/",
            data={"op": "power_on"},
        )
        logger.info(f"Powered on machine {machine_id}")
        return True

    def stop_machine(self, machine_id: str) -> bool:
        """Power off a machine.

        Args:
            machine_id: MaaS system_id

        Returns:
            True if power off command sent successfully

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        self._request(
            "POST",
            f"machines/{machine_id}/",
            data={"op": "power_off"},
        )
        logger.info(f"Powered off machine {machine_id}")
        return True

    def reboot_machine(self, machine_id: str) -> bool:
        """Reboot a machine using hard power cycle.

        Args:
            machine_id: MaaS system_id

        Returns:
            True if reboot successful

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        # Power off
        self._request(
            "POST",
            f"machines/{machine_id}/",
            data={"op": "power_off", "stop_mode": "hard"},
        )

        # Wait briefly for power off
        time.sleep(2)

        # Power on
        self._request(
            "POST",
            f"machines/{machine_id}/",
            data={"op": "power_on"},
        )

        logger.info(f"Rebooted machine {machine_id}")
        return True

    def commission_machine(
        self,
        machine_id: str,
        enable_ssh: bool = False,
        skip_networking: bool = False,
        skip_storage: bool = False,
    ) -> Machine:
        """Start commissioning process for a machine.

        Commissioning tests hardware and configures the machine for deployment.

        Args:
            machine_id: MaaS system_id
            enable_ssh: Enable SSH during commissioning
            skip_networking: Skip network configuration
            skip_storage: Skip storage configuration

        Returns:
            Machine in COMMISSIONING state

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        data: dict[str, Any] = {"op": "commission"}

        if enable_ssh:
            data["enable_ssh"] = "1"
        if skip_networking:
            data["skip_networking"] = "1"
        if skip_storage:
            data["skip_storage"] = "1"

        result = self._request(
            "POST",
            f"machines/{machine_id}/",
            data=data,
        )

        logger.info(f"Started commissioning for machine {machine_id}")
        return self._parse_machine(result)

    def abort_operation(self, machine_id: str) -> Machine:
        """Abort current operation on a machine.

        Args:
            machine_id: MaaS system_id

        Returns:
            Updated Machine object

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        result = self._request(
            "POST",
            f"machines/{machine_id}/",
            data={"op": "abort"},
        )
        logger.info(f"Aborted operation on machine {machine_id}")
        return self._parse_machine(result)

    def set_zone(self, machine_id: str, zone: str) -> Machine:
        """Set the zone for a machine.

        Args:
            machine_id: MaaS system_id
            zone: Zone name

        Returns:
            Updated Machine object

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        result = self._request(
            "PUT",
            f"machines/{machine_id}/",
            data={"zone": zone},
        )
        logger.info(f"Set zone for machine {machine_id} to {zone}")
        return self._parse_machine(result)

    def set_pool(self, machine_id: str, pool: str) -> Machine:
        """Set the resource pool for a machine.

        Args:
            machine_id: MaaS system_id
            pool: Pool name

        Returns:
            Updated Machine object

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        result = self._request(
            "PUT",
            f"machines/{machine_id}/",
            data={"pool": pool},
        )
        logger.info(f"Set pool for machine {machine_id} to {pool}")
        return self._parse_machine(result)

    def add_tag(self, machine_id: str, tag: str) -> bool:
        """Add a tag to a machine.

        Args:
            machine_id: MaaS system_id
            tag: Tag name

        Returns:
            True if tag added successfully

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        # First ensure tag exists
        try:
            self._request("POST", "tags/", data={"name": tag})
        except CloudError:
            pass  # Tag may already exist

        # Add tag to machine
        self._request(
            "POST",
            f"tags/{tag}/",
            data={"op": "update_nodes", "add": machine_id},
        )
        logger.info(f"Added tag {tag} to machine {machine_id}")
        return True

    def remove_tag(self, machine_id: str, tag: str) -> bool:
        """Remove a tag from a machine.

        Args:
            machine_id: MaaS system_id
            tag: Tag name

        Returns:
            True if tag removed successfully

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        self._request(
            "POST",
            f"tags/{tag}/",
            data={"op": "update_nodes", "remove": machine_id},
        )
        logger.info(f"Removed tag {tag} from machine {machine_id}")
        return True

    def get_power_parameters(self, machine_id: str) -> dict[str, Any]:
        """Get power parameters for a machine.

        Args:
            machine_id: MaaS system_id

        Returns:
            Power parameters dictionary

        Raises:
            CloudNotFoundError: If machine not found
            CloudError: On API errors
        """
        result = self._request(
            "GET",
            f"machines/{machine_id}/",
            params={"op": "power_parameters"},
        )
        return result or {}

    def list_images(self, filters: dict | None = None) -> list[dict]:
        """List available boot resources (images).

        Args:
            filters: Optional filters (not used currently)

        Returns:
            List of image info dictionaries
        """
        try:
            data = self._request("GET", "boot-resources/")
            images = []

            for resource in data or []:
                images.append({
                    "id": resource.get("id"),
                    "name": f"{resource.get('name')}/{resource.get('architecture')}",
                    "osystem": resource.get("name", "").split("/")[0] if "/" in resource.get("name", "") else resource.get("name"),
                    "release": resource.get("name", "").split("/")[1] if "/" in resource.get("name", "") else "",
                    "architecture": resource.get("architecture", ""),
                    "type": resource.get("type", ""),
                    "subarches": resource.get("subarches", ""),
                })

            return images
        except CloudError:
            return []

    def list_sizes(self, filters: dict | None = None) -> list[dict]:
        """List available machine configurations.

        In MaaS, sizes are determined by physical hardware.
        This returns available machines grouped by specs.

        Args:
            filters: Optional filters

        Returns:
            List of size info dictionaries (unique hardware configurations)
        """
        try:
            machines = self.list_machines(filters={"status": "Ready"})
            sizes: dict[str, dict] = {}

            for machine in machines:
                size_key = machine.size
                if size_key not in sizes:
                    sizes[size_key] = {
                        "id": size_key,
                        "name": size_key,
                        "cpu_count": machine.extra.get("cpu_count", 0),
                        "memory_mb": machine.extra.get("memory_mb", 0),
                        "storage_gb": machine.extra.get("storage_gb", 0),
                        "count": 0,
                    }
                sizes[size_key]["count"] += 1

            return list(sizes.values())
        except CloudError:
            return []

    def list_regions(self) -> list[dict]:
        """List available zones.

        Args:
            None

        Returns:
            List of zone info dictionaries
        """
        try:
            data = self._request("GET", "zones/")
            zones = []

            for zone in data or []:
                zones.append({
                    "id": zone.get("id"),
                    "name": zone.get("name", ""),
                    "description": zone.get("description", ""),
                })

            return zones
        except CloudError:
            return []

    def list_pools(self) -> list[dict]:
        """List available resource pools.

        Returns:
            List of pool info dictionaries
        """
        try:
            data = self._request("GET", "resourcepools/")
            pools = []

            for pool in data or []:
                pools.append({
                    "id": pool.get("id"),
                    "name": pool.get("name", ""),
                    "description": pool.get("description", ""),
                })

            return pools
        except CloudError:
            return []

    def list_tags(self) -> list[dict]:
        """List available tags.

        Returns:
            List of tag info dictionaries
        """
        try:
            data = self._request("GET", "tags/")
            tags = []

            for tag in data or []:
                tags.append({
                    "name": tag.get("name", ""),
                    "definition": tag.get("definition", ""),
                    "comment": tag.get("comment", ""),
                    "kernel_opts": tag.get("kernel_opts", ""),
                })

            return tags
        except CloudError:
            return []

    def get_console_output(self, machine_id: str) -> str:
        """Get console output from a machine.

        Args:
            machine_id: MaaS system_id

        Returns:
            Console output string (from commissioning/installation logs)
        """
        try:
            # Get installation log
            result = self._request(
                "GET",
                f"machines/{machine_id}/",
                params={"op": "get_curtin_config"},
            )
            return result if isinstance(result, str) else ""
        except CloudError:
            return ""

    def get_events(
        self,
        machine_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Get events for a machine or all machines.

        Args:
            machine_id: Optional MaaS system_id to filter by
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries
        """
        try:
            params: dict[str, Any] = {"limit": limit}
            if machine_id:
                params["id"] = machine_id

            data = self._request("GET", "events/", params=params)

            events = []
            for event in data.get("events", []):
                events.append({
                    "id": event.get("id"),
                    "type": event.get("type", ""),
                    "description": event.get("description", ""),
                    "created": event.get("created", ""),
                    "node": event.get("node", ""),
                    "hostname": event.get("hostname", ""),
                })

            return events
        except CloudError:
            return []

    def test_connection(self) -> dict[str, Any]:
        """Test connection and return MaaS server info.

        Returns:
            Dictionary with version and capabilities info

        Raises:
            CloudError: On connection failure
        """
        version_info = self._request("GET", "version/")
        return {
            "connected": True,
            "version": version_info.get("version", "unknown"),
            "subversion": version_info.get("subversion", ""),
            "capabilities": version_info.get("capabilities", []),
        }
