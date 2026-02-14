"""Vultr Cloud Provider Implementation.

Provides integration with Vultr VPS using their REST API v2 for unified
machine management through the BaseCloud interface.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any

import requests

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


# Vultr state to unified state mapping
VULTR_STATE_MAP: dict[str, MachineState] = {
    "pending": MachineState.PENDING,
    "active": MachineState.RUNNING,
    "suspended": MachineState.STOPPED,
    "resizing": MachineState.PENDING,
    "locked": MachineState.STOPPED,
}


class VultrCloud(BaseCloud):
    """Vultr cloud provider implementation using REST API v2.

    Supports VPS instance lifecycle management including creation, deletion,
    start/stop operations, and cloud-init user data injection.

    Config parameters:
        VULTR_API_KEY: Vultr API key for authentication
        VULTR_REGION: Default region (e.g., 'ewr' for New Jersey)
    """

    provider_type: str = "vultr"
    supports_cloud_init: bool = True

    # Vultr API v2 base URL
    API_BASE_URL: str = "https://api.vultr.com/v2"

    # Request timeout in seconds
    REQUEST_TIMEOUT: int = 30

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize Vultr cloud provider.

        Args:
            config: Configuration dictionary containing:
                - VULTR_API_KEY: Vultr API key (required)
                - VULTR_REGION: Default region (optional, defaults to 'ewr')

        Raises:
            CloudAuthError: If API key is not provided
        """
        super().__init__(config)

        self.api_key: str = config.get("VULTR_API_KEY", "")
        self.default_region: str = config.get("VULTR_REGION", "ewr")
        self.provider_id: str = config.get("provider_id", "")

        if not self.api_key:
            raise CloudAuthError("VULTR_API_KEY is required")

        self._session: requests.Session | None = None

    @property
    def session(self) -> requests.Session:
        """Get or create a requests session with authentication headers."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            })
        return self._session

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to the Vultr API.

        Args:
            method: HTTP method (GET, POST, DELETE, PATCH)
            endpoint: API endpoint (e.g., '/instances')
            params: Query parameters
            json_data: JSON body data

        Returns:
            Response JSON as dictionary

        Raises:
            CloudAuthError: On authentication failures (401, 403)
            CloudNotFoundError: When resource not found (404)
            CloudQuotaError: On quota exceeded (429, 402)
            CloudError: On other API errors
        """
        url = f"{self.API_BASE_URL}{endpoint}"

        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                timeout=self.REQUEST_TIMEOUT,
            )
        except requests.exceptions.Timeout as e:
            raise CloudError(f"Request to Vultr API timed out: {e}")
        except requests.exceptions.ConnectionError as e:
            raise CloudError(f"Failed to connect to Vultr API: {e}")
        except requests.exceptions.RequestException as e:
            raise CloudError(f"Request to Vultr API failed: {e}")

        # Handle response status codes
        if response.status_code == 204:
            # No content response (e.g., successful delete)
            return {}

        if response.status_code in (401, 403):
            raise CloudAuthError(
                f"Vultr authentication failed: {response.text}"
            )

        if response.status_code == 404:
            raise CloudNotFoundError(
                f"Resource not found: {response.text}"
            )

        if response.status_code in (402, 429):
            raise CloudQuotaError(
                f"Vultr quota exceeded or payment required: {response.text}"
            )

        if response.status_code >= 400:
            raise CloudError(
                f"Vultr API error ({response.status_code}): {response.text}"
            )

        # Return empty dict for empty responses
        if not response.text:
            return {}

        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    def authenticate(self) -> bool:
        """Authenticate with Vultr by validating the API key.

        Returns:
            True if authentication successful

        Raises:
            CloudAuthError: If authentication fails
        """
        try:
            # Use the account endpoint to validate authentication
            self._request("GET", "/account")
            self._authenticated = True
            log.info("Successfully authenticated with Vultr")
            return True
        except CloudAuthError:
            self._authenticated = False
            raise
        except CloudError as e:
            self._authenticated = False
            raise CloudAuthError(f"Failed to authenticate with Vultr: {e}")

    def _parse_instance(self, instance_data: dict[str, Any]) -> Machine:
        """Parse Vultr instance data into a Machine object.

        Args:
            instance_data: Raw instance data from Vultr API

        Returns:
            Machine object with normalized data
        """
        # Map Vultr status to unified state
        vultr_status = instance_data.get("status", "").lower()
        vultr_power = instance_data.get("power_status", "").lower()

        # Determine state based on status and power_status
        if vultr_status == "active" and vultr_power == "running":
            state = MachineState.RUNNING
        elif vultr_status == "active" and vultr_power == "stopped":
            state = MachineState.STOPPED
        elif vultr_status in VULTR_STATE_MAP:
            state = VULTR_STATE_MAP[vultr_status]
        else:
            state = MachineState.UNKNOWN

        # Parse timestamps
        created_at = None
        date_created = instance_data.get("date_created")
        if date_created:
            try:
                created_at = datetime.fromisoformat(
                    date_created.replace("Z", "+00:00")
                )
            except ValueError:
                log.warning(f"Failed to parse date_created: {date_created}")

        # Extract IP addresses
        main_ip = instance_data.get("main_ip", "")
        public_ips = [main_ip] if main_ip and main_ip != "0.0.0.0" else []

        internal_ip = instance_data.get("internal_ip", "")
        private_ips = [internal_ip] if internal_ip else []

        # Extract additional IPs from v6 networks
        v6_network = instance_data.get("v6_network", "")
        v6_main_ip = instance_data.get("v6_main_ip", "")
        if v6_main_ip:
            public_ips.append(v6_main_ip)

        # Build tags from Vultr tags list
        tags: dict[str, str] = {}
        vultr_tags = instance_data.get("tags", [])
        for i, tag in enumerate(vultr_tags):
            tags[f"tag_{i}"] = tag

        # Include label as a tag if present
        label = instance_data.get("label", "")
        if label:
            tags["label"] = label

        return Machine(
            id=instance_data.get("id", ""),
            name=label or instance_data.get("hostname", ""),
            state=state,
            provider=self.provider_type,
            provider_id=self.provider_id,
            region=instance_data.get("region", ""),
            image=instance_data.get("os", ""),
            size=instance_data.get("plan", ""),
            public_ips=public_ips,
            private_ips=private_ips,
            created_at=created_at,
            updated_at=None,  # Vultr doesn't provide update timestamp
            tags=tags,
            extra={
                "vcpu_count": instance_data.get("vcpu_count"),
                "ram": instance_data.get("ram"),
                "disk": instance_data.get("disk"),
                "bandwidth": instance_data.get("allowed_bandwidth"),
                "os_id": instance_data.get("os_id"),
                "app_id": instance_data.get("app_id"),
                "features": instance_data.get("features", []),
                "hostname": instance_data.get("hostname"),
                "server_status": instance_data.get("server_status"),
                "power_status": instance_data.get("power_status"),
                "v6_network": v6_network,
                "v6_network_size": instance_data.get("v6_network_size"),
            },
        )

    def list_machines(self, filters: dict | None = None) -> list[Machine]:
        """List all Vultr VPS instances.

        Args:
            filters: Optional filters:
                - region: Filter by region
                - label: Filter by label
                - tag: Filter by tag

        Returns:
            List of Machine objects

        Raises:
            CloudError: On API errors
        """
        machines: list[Machine] = []
        cursor = ""

        # Vultr uses cursor-based pagination
        while True:
            params: dict[str, Any] = {"per_page": 100}
            if cursor:
                params["cursor"] = cursor

            # Apply filters
            if filters:
                if "region" in filters:
                    params["region"] = filters["region"]
                if "label" in filters:
                    params["label"] = filters["label"]
                if "tag" in filters:
                    params["tag"] = filters["tag"]

            response = self._request("GET", "/instances", params=params)

            instances = response.get("instances", [])
            for instance_data in instances:
                machines.append(self._parse_instance(instance_data))

            # Check for more pages
            meta = response.get("meta", {})
            links = meta.get("links", {})
            cursor = links.get("next", "")

            if not cursor:
                break

        log.info(f"Listed {len(machines)} Vultr instances")
        return machines

    def get_machine(self, machine_id: str) -> Machine:
        """Get a specific Vultr VPS instance by ID.

        Args:
            machine_id: Vultr instance ID

        Returns:
            Machine object

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        response = self._request("GET", f"/instances/{machine_id}")
        instance_data = response.get("instance", {})

        if not instance_data:
            raise CloudNotFoundError(f"Instance {machine_id} not found")

        return self._parse_instance(instance_data)

    def create_machine(self, spec: MachineSpec) -> Machine:
        """Create a new Vultr VPS instance.

        Args:
            spec: Machine specification including:
                - name: Label for the instance
                - image: OS ID or snapshot ID
                - size: Plan ID (e.g., 'vc2-1c-1gb')
                - region: Region ID (e.g., 'ewr')
                - cloud_init: User data script
                - ssh_keys: List of SSH key IDs
                - tags: Instance tags

        Returns:
            Created Machine object

        Raises:
            CloudQuotaError: If quota exceeded
            CloudError: On API errors
        """
        region = spec.region or self.default_region

        # Build request payload
        payload: dict[str, Any] = {
            "region": region,
            "plan": spec.size,
            "label": spec.name,
        }

        # Handle image specification
        # Vultr uses os_id for OS images, snapshot_id for snapshots,
        # iso_id for ISOs, or app_id for marketplace apps
        image = spec.image
        if image.startswith("snapshot-"):
            payload["snapshot_id"] = image.replace("snapshot-", "")
        elif image.startswith("app-"):
            payload["app_id"] = int(image.replace("app-", ""))
        elif image.startswith("iso-"):
            payload["iso_id"] = image.replace("iso-", "")
        else:
            # Assume it's an OS ID (integer)
            try:
                payload["os_id"] = int(image)
            except ValueError:
                # Try to look up the OS by name
                os_id = self._resolve_os_name(image)
                if os_id:
                    payload["os_id"] = os_id
                else:
                    raise CloudError(f"Invalid image specification: {image}")

        # Add SSH keys if provided
        if spec.ssh_keys:
            payload["sshkey_id"] = spec.ssh_keys

        # Add cloud-init user data if provided
        if spec.cloud_init:
            # Vultr expects base64-encoded user data
            user_data_encoded = base64.b64encode(
                spec.cloud_init.encode("utf-8")
            ).decode("utf-8")
            payload["user_data"] = user_data_encoded

        # Add hostname from extra or derive from name
        hostname = spec.extra.get("hostname", "")
        if hostname:
            payload["hostname"] = hostname

        # Add tags if provided
        if spec.tags:
            # Vultr uses a flat list of tag strings
            tag_list = list(spec.tags.values())
            if tag_list:
                payload["tags"] = tag_list

        # Additional options from extra
        if spec.extra.get("enable_ipv6"):
            payload["enable_ipv6"] = True

        if spec.extra.get("backups"):
            payload["backups"] = "enabled"

        if spec.extra.get("ddos_protection"):
            payload["ddos_protection"] = True

        if spec.extra.get("activation_email") is False:
            payload["activation_email"] = False

        # Private networking
        if spec.networks:
            # VPC IDs for private networking
            payload["attach_vpc"] = spec.networks

        log.info(f"Creating Vultr instance: {spec.name} in {region}")

        try:
            response = self._request("POST", "/instances", json_data=payload)
        except CloudError as e:
            # Check for quota-related errors
            error_str = str(e).lower()
            if "insufficient" in error_str or "quota" in error_str:
                raise CloudQuotaError(f"Vultr quota exceeded: {e}")
            raise

        instance_data = response.get("instance", {})
        if not instance_data:
            raise CloudError("Failed to create instance: empty response")

        machine = self._parse_instance(instance_data)
        log.info(f"Created Vultr instance: {machine.id}")

        return machine

    def _resolve_os_name(self, os_name: str) -> int | None:
        """Resolve an OS name to its Vultr OS ID.

        Args:
            os_name: OS name (e.g., 'ubuntu-22.04', 'debian-12')

        Returns:
            OS ID if found, None otherwise
        """
        os_name_lower = os_name.lower()

        # Common OS name mappings
        os_mappings: dict[str, int] = {
            "ubuntu-24.04": 2284,
            "ubuntu-22.04": 1743,
            "ubuntu-20.04": 387,
            "debian-12": 2136,
            "debian-11": 477,
            "centos-9": 2076,
            "centos-stream-9": 2076,
            "rocky-9": 1869,
            "rocky-linux-9": 1869,
            "almalinux-9": 1868,
            "fedora-39": 2186,
        }

        if os_name_lower in os_mappings:
            return os_mappings[os_name_lower]

        # Try to fetch from API if not in cache
        try:
            os_list = self.list_images()
            for os_info in os_list:
                if os_name_lower in os_info.get("name", "").lower():
                    return os_info.get("id")
        except CloudError:
            pass

        return None

    def destroy_machine(self, machine_id: str) -> bool:
        """Destroy/delete a Vultr VPS instance.

        Args:
            machine_id: Vultr instance ID

        Returns:
            True if destroyed successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        log.info(f"Destroying Vultr instance: {machine_id}")

        self._request("DELETE", f"/instances/{machine_id}")

        log.info(f"Destroyed Vultr instance: {machine_id}")
        return True

    def start_machine(self, machine_id: str) -> bool:
        """Start a stopped Vultr VPS instance.

        Args:
            machine_id: Vultr instance ID

        Returns:
            True if started successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        log.info(f"Starting Vultr instance: {machine_id}")

        self._request("POST", f"/instances/{machine_id}/start")

        log.info(f"Started Vultr instance: {machine_id}")
        return True

    def stop_machine(self, machine_id: str) -> bool:
        """Stop a running Vultr VPS instance.

        Args:
            machine_id: Vultr instance ID

        Returns:
            True if stopped successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        log.info(f"Stopping Vultr instance: {machine_id}")

        self._request("POST", f"/instances/{machine_id}/halt")

        log.info(f"Stopped Vultr instance: {machine_id}")
        return True

    def reboot_machine(self, machine_id: str) -> bool:
        """Reboot a Vultr VPS instance.

        Args:
            machine_id: Vultr instance ID

        Returns:
            True if reboot initiated successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        log.info(f"Rebooting Vultr instance: {machine_id}")

        self._request("POST", f"/instances/{machine_id}/reboot")

        log.info(f"Rebooted Vultr instance: {machine_id}")
        return True

    def reinstall_machine(self, machine_id: str, hostname: str = "") -> bool:
        """Reinstall a Vultr VPS instance with a fresh OS.

        Args:
            machine_id: Vultr instance ID
            hostname: Optional new hostname

        Returns:
            True if reinstall initiated successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        log.info(f"Reinstalling Vultr instance: {machine_id}")

        payload: dict[str, Any] = {}
        if hostname:
            payload["hostname"] = hostname

        self._request(
            "POST",
            f"/instances/{machine_id}/reinstall",
            json_data=payload if payload else None,
        )

        log.info(f"Reinstalled Vultr instance: {machine_id}")
        return True

    def list_images(self, filters: dict | None = None) -> list[dict]:
        """List available Vultr operating systems.

        Args:
            filters: Optional filters:
                - type: Filter by type ('oses', 'apps', 'snapshots')

        Returns:
            List of image info dictionaries
        """
        images: list[dict] = []
        filter_type = filters.get("type", "oses") if filters else "oses"

        if filter_type in ("oses", "all"):
            cursor = ""
            while True:
                params: dict[str, Any] = {"per_page": 100}
                if cursor:
                    params["cursor"] = cursor

                response = self._request("GET", "/os", params=params)

                for os_data in response.get("os", []):
                    images.append({
                        "id": os_data.get("id"),
                        "name": os_data.get("name"),
                        "arch": os_data.get("arch"),
                        "family": os_data.get("family"),
                        "type": "os",
                    })

                meta = response.get("meta", {})
                links = meta.get("links", {})
                cursor = links.get("next", "")
                if not cursor:
                    break

        if filter_type in ("apps", "all"):
            cursor = ""
            while True:
                params = {"per_page": 100}
                if cursor:
                    params["cursor"] = cursor

                response = self._request("GET", "/applications", params=params)

                for app_data in response.get("applications", []):
                    images.append({
                        "id": f"app-{app_data.get('id')}",
                        "name": app_data.get("name"),
                        "short_name": app_data.get("short_name"),
                        "deploy_name": app_data.get("deploy_name"),
                        "type": "app",
                    })

                meta = response.get("meta", {})
                links = meta.get("links", {})
                cursor = links.get("next", "")
                if not cursor:
                    break

        return images

    def list_sizes(self, filters: dict | None = None) -> list[dict]:
        """List available Vultr instance plans (sizes).

        Args:
            filters: Optional filters:
                - type: Plan type ('all', 'vc2', 'vhf', 'vdc', etc.)

        Returns:
            List of plan info dictionaries
        """
        sizes: list[dict] = []
        cursor = ""

        params: dict[str, Any] = {"per_page": 100}

        # Filter by plan type
        if filters and "type" in filters:
            plan_type = filters["type"]
            if plan_type != "all":
                params["type"] = plan_type

        while True:
            if cursor:
                params["cursor"] = cursor

            response = self._request("GET", "/plans", params=params)

            for plan_data in response.get("plans", []):
                sizes.append({
                    "id": plan_data.get("id"),
                    "name": plan_data.get("id"),  # Vultr uses ID as name
                    "vcpu_count": plan_data.get("vcpu_count"),
                    "ram": plan_data.get("ram"),
                    "disk": plan_data.get("disk"),
                    "disk_count": plan_data.get("disk_count"),
                    "bandwidth": plan_data.get("bandwidth"),
                    "monthly_cost": plan_data.get("monthly_cost"),
                    "type": plan_data.get("type"),
                    "locations": plan_data.get("locations", []),
                })

            meta = response.get("meta", {})
            links = meta.get("links", {})
            cursor = links.get("next", "")
            if not cursor:
                break

        return sizes

    def list_regions(self) -> list[dict]:
        """List available Vultr regions.

        Returns:
            List of region info dictionaries
        """
        regions: list[dict] = []

        response = self._request("GET", "/regions")

        for region_data in response.get("regions", []):
            regions.append({
                "id": region_data.get("id"),
                "city": region_data.get("city"),
                "country": region_data.get("country"),
                "continent": region_data.get("continent"),
                "options": region_data.get("options", []),
            })

        return regions

    def list_ssh_keys(self) -> list[dict]:
        """List registered SSH keys.

        Returns:
            List of SSH key info dictionaries
        """
        ssh_keys: list[dict] = []
        cursor = ""

        while True:
            params: dict[str, Any] = {"per_page": 100}
            if cursor:
                params["cursor"] = cursor

            response = self._request("GET", "/ssh-keys", params=params)

            for key_data in response.get("ssh_keys", []):
                ssh_keys.append({
                    "id": key_data.get("id"),
                    "name": key_data.get("name"),
                    "ssh_key": key_data.get("ssh_key"),
                    "date_created": key_data.get("date_created"),
                })

            meta = response.get("meta", {})
            links = meta.get("links", {})
            cursor = links.get("next", "")
            if not cursor:
                break

        return ssh_keys

    def create_ssh_key(self, name: str, ssh_key: str) -> dict:
        """Create a new SSH key.

        Args:
            name: Name for the SSH key
            ssh_key: Public SSH key content

        Returns:
            Created SSH key info dictionary
        """
        payload = {
            "name": name,
            "ssh_key": ssh_key,
        }

        response = self._request("POST", "/ssh-keys", json_data=payload)
        return response.get("ssh_key", {})

    def delete_ssh_key(self, ssh_key_id: str) -> bool:
        """Delete an SSH key.

        Args:
            ssh_key_id: SSH key ID

        Returns:
            True if deleted successfully
        """
        self._request("DELETE", f"/ssh-keys/{ssh_key_id}")
        return True

    def get_bandwidth(self, machine_id: str) -> dict:
        """Get bandwidth usage for an instance.

        Args:
            machine_id: Vultr instance ID

        Returns:
            Bandwidth usage dictionary
        """
        response = self._request("GET", f"/instances/{machine_id}/bandwidth")
        return response.get("bandwidth", {})

    def get_neighbors(self, machine_id: str) -> list[str]:
        """Get other instances on the same physical host.

        Args:
            machine_id: Vultr instance ID

        Returns:
            List of neighbor instance IDs
        """
        response = self._request("GET", f"/instances/{machine_id}/neighbors")
        return response.get("neighbors", [])

    def get_console_output(self, machine_id: str) -> str:
        """Get console URL for an instance (not traditional console output).

        Note: Vultr doesn't provide raw console output, but provides
        a web console URL instead.

        Args:
            machine_id: Vultr instance ID

        Returns:
            Web console URL
        """
        # Vultr provides a web-based VNC console
        # This returns the URL rather than raw output
        response = self._request(
            "GET",
            f"/instances/{machine_id}/vnc-url"
        )
        return response.get("vnc_url", {}).get("url", "")

    def create_snapshot(
        self,
        machine_id: str,
        description: str = ""
    ) -> dict:
        """Create a snapshot of an instance.

        Args:
            machine_id: Vultr instance ID
            description: Snapshot description

        Returns:
            Snapshot info dictionary
        """
        payload: dict[str, Any] = {"instance_id": machine_id}
        if description:
            payload["description"] = description

        response = self._request("POST", "/snapshots", json_data=payload)
        return response.get("snapshot", {})

    def list_snapshots(self) -> list[dict]:
        """List all snapshots.

        Returns:
            List of snapshot info dictionaries
        """
        snapshots: list[dict] = []
        cursor = ""

        while True:
            params: dict[str, Any] = {"per_page": 100}
            if cursor:
                params["cursor"] = cursor

            response = self._request("GET", "/snapshots", params=params)

            for snapshot_data in response.get("snapshots", []):
                snapshots.append({
                    "id": snapshot_data.get("id"),
                    "description": snapshot_data.get("description"),
                    "date_created": snapshot_data.get("date_created"),
                    "size": snapshot_data.get("size"),
                    "status": snapshot_data.get("status"),
                    "os_id": snapshot_data.get("os_id"),
                    "app_id": snapshot_data.get("app_id"),
                })

            meta = response.get("meta", {})
            links = meta.get("links", {})
            cursor = links.get("next", "")
            if not cursor:
                break

        return snapshots

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """Delete a snapshot.

        Args:
            snapshot_id: Snapshot ID

        Returns:
            True if deleted successfully
        """
        self._request("DELETE", f"/snapshots/{snapshot_id}")
        return True
