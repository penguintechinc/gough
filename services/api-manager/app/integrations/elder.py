"""Elder Integration Client.

Provides async HTTP client for Elder service integration. Handles:
- Host registration and management
- Application endpoint registration
- Machine synchronization
- Health checks and connectivity validation

Elder is a service discovery and host management system that maintains
a centralized registry of infrastructure resources.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


class ElderError(Exception):
    """Base exception for Elder integration errors."""

    pass


class ElderAuthError(ElderError):
    """Authentication or authorization error."""

    pass


class ElderConnectionError(ElderError):
    """Connection or network error."""

    pass


class ElderNotFoundError(ElderError):
    """Resource not found error."""

    pass


class ElderConflictError(ElderError):
    """Resource conflict error."""

    pass


@dataclass(slots=True)
class HostRegistration:
    """Host registration data."""

    hostname: str
    ip: str
    fqdn: str
    apps: list[str] = field(default_factory=list)
    zone: str = "default"
    pool: str = "default"
    tags: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AppEndpoint:
    """Application endpoint registration."""

    app_name: str
    hosts: list[str]
    port: int
    protocol: str = "http"
    path: str = "/"
    health_check_url: Optional[str] = None
    priority: int = 100


class ElderClient:
    """Async HTTP client for Elder service integration.

    Provides methods for host and application management with Elder,
    including registration, updates, and health checks.
    """

    # Retry configuration
    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = 1.0
    REQUEST_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        elder_url: str,
        api_key: str,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
    ):
        """Initialize Elder client.

        Args:
            elder_url: Base URL for Elder service
            api_key: API key for authentication
            timeout: Request timeout in seconds
            max_retries: Maximum retry attempts
        """
        self.elder_url = elder_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> ElderClient:
        """Async context manager entry."""
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    async def _ensure_client(self) -> None:
        """Ensure async client is initialized."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Gough/ElderClient",
                },
            )

    async def close(self) -> None:
        """Close async client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Make HTTP request with retry logic.

        Args:
            method: HTTP method
            path: API path (without base URL)
            data: JSON body data
            params: Query parameters

        Returns:
            Response data as dictionary

        Raises:
            ElderAuthError: Authentication failed
            ElderConnectionError: Connection failed
            ElderNotFoundError: Resource not found
            ElderConflictError: Resource conflict
            ElderError: Other errors
        """
        await self._ensure_client()

        url = f"{self.elder_url}{path}"
        last_error = None

        for attempt in range(self.max_retries):
            try:
                log.debug(
                    f"Elder {method} {path} (attempt {attempt + 1}/{self.max_retries})"
                )

                response = await self._client.request(
                    method=method,
                    url=url,
                    json=data,
                    params=params,
                )

                # Handle authentication errors
                if response.status_code == 401:
                    log.error("Elder authentication failed: invalid API key")
                    raise ElderAuthError("Authentication failed: invalid API key")

                # Handle not found errors
                if response.status_code == 404:
                    log.warning(f"Elder resource not found: {path}")
                    raise ElderNotFoundError(f"Resource not found: {path}")

                # Handle conflict errors
                if response.status_code == 409:
                    log.warning(f"Elder resource conflict: {path}")
                    raise ElderConflictError(f"Resource conflict: {path}")

                # Handle server errors with retry
                if response.status_code >= 500:
                    last_error = ElderConnectionError(
                        f"Server error {response.status_code}"
                    )
                    if attempt < self.max_retries - 1:
                        wait_time = self.RETRY_DELAY_SECONDS * (2 ** attempt)
                        log.warning(
                            f"Elder server error, retrying in {wait_time}s: {response.status_code}"
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    raise last_error

                # Handle successful response
                if response.status_code in (200, 201, 202):
                    try:
                        return response.json()
                    except json.JSONDecodeError:
                        return {}

                # Handle other client errors
                if 400 <= response.status_code < 500:
                    error_msg = f"Client error {response.status_code}"
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("error", error_msg)
                    except json.JSONDecodeError:
                        pass
                    log.error(f"Elder {method} {path}: {error_msg}")
                    raise ElderError(error_msg)

                # Unexpected status code
                log.error(f"Unexpected Elder response: {response.status_code}")
                raise ElderError(f"Unexpected response code: {response.status_code}")

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_error = ElderConnectionError(f"Connection failed: {str(e)}")
                if attempt < self.max_retries - 1:
                    wait_time = self.RETRY_DELAY_SECONDS * (2 ** attempt)
                    log.warning(
                        f"Elder connection failed, retrying in {wait_time}s: {str(e)}"
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise last_error

            except ElderError:
                raise

        # All retries exhausted
        if last_error:
            raise last_error
        raise ElderError("Request failed after all retries")

    async def health_check(self) -> bool:
        """Check Elder connectivity and health.

        Returns:
            True if Elder is healthy

        Raises:
            ElderConnectionError: Connection failed
        """
        try:
            result = await self._request("GET", "/api/v1/health")
            is_healthy = result.get("status") == "healthy"
            if is_healthy:
                log.debug("Elder health check passed")
            else:
                log.warning("Elder health check failed: not healthy")
            return is_healthy
        except ElderError as e:
            log.error(f"Elder health check failed: {str(e)}")
            raise ElderConnectionError(f"Health check failed: {str(e)}")

    async def register_host(
        self,
        hostname: str,
        ip: str,
        fqdn: str,
        apps: Optional[list[str]] = None,
        zone: str = "default",
        pool: str = "default",
        tags: Optional[dict[str, str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Register a host with Elder.

        Args:
            hostname: Short hostname
            ip: IP address
            fqdn: Fully qualified domain name
            apps: List of application names
            zone: Zone/region
            pool: Resource pool
            tags: Tags/labels
            metadata: Additional metadata

        Returns:
            Elder response with registered host details

        Raises:
            ElderError: Registration failed
        """
        payload = {
            "hostname": hostname,
            "ip": ip,
            "fqdn": fqdn,
            "apps": apps or [],
            "zone": zone,
            "pool": pool,
            "tags": tags or {},
            "metadata": metadata or {},
            "registered_at": datetime.utcnow().isoformat(),
        }

        log.info(f"Registering host with Elder: {hostname} ({ip})")

        try:
            result = await self._request("POST", "/api/v1/hosts", data=payload)
            log.info(f"Host registered successfully: {hostname}")
            return result
        except ElderConflictError:
            # Host already exists, update instead
            log.info(f"Host already exists, updating: {hostname}")
            return await self.update_host(hostname, **payload)

    async def update_host(
        self,
        hostname: str,
        **kwargs,
    ) -> dict[str, Any]:
        """Update host details in Elder.

        Args:
            hostname: Short hostname
            **kwargs: Fields to update (ip, fqdn, apps, zone, pool, tags, metadata)

        Returns:
            Elder response with updated host details

        Raises:
            ElderError: Update failed
        """
        # Filter out None values and timestamp
        payload = {k: v for k, v in kwargs.items() if v is not None}

        log.info(f"Updating host in Elder: {hostname}")

        try:
            result = await self._request(
                "PUT", f"/api/v1/hosts/{hostname}", data=payload
            )
            log.info(f"Host updated successfully: {hostname}")
            return result
        except ElderNotFoundError:
            log.warning(f"Host not found in Elder, registering: {hostname}")
            # If update fails because host doesn't exist, register it
            if "ip" in kwargs and "fqdn" in kwargs:
                return await self.register_host(
                    hostname=hostname,
                    ip=kwargs["ip"],
                    fqdn=kwargs["fqdn"],
                    apps=kwargs.get("apps"),
                    zone=kwargs.get("zone", "default"),
                    pool=kwargs.get("pool", "default"),
                    tags=kwargs.get("tags"),
                    metadata=kwargs.get("metadata"),
                )
            raise

    async def deregister_host(self, hostname: str) -> dict[str, Any]:
        """Remove host from Elder.

        Args:
            hostname: Short hostname

        Returns:
            Elder response

        Raises:
            ElderError: Deregistration failed
        """
        log.info(f"Deregistering host from Elder: {hostname}")

        try:
            result = await self._request("DELETE", f"/api/v1/hosts/{hostname}")
            log.info(f"Host deregistered successfully: {hostname}")
            return result
        except ElderNotFoundError:
            log.warning(f"Host not found in Elder (already removed): {hostname}")
            return {"message": "Host already removed"}

    async def register_app(
        self,
        app_name: str,
        hosts: list[str],
        port: int,
        protocol: str = "http",
        path: str = "/",
        health_check_url: Optional[str] = None,
        priority: int = 100,
    ) -> dict[str, Any]:
        """Register an application endpoint with Elder.

        Args:
            app_name: Application name
            hosts: List of hostnames
            port: Port number
            protocol: Protocol (http/https/grpc)
            path: Base path for application
            health_check_url: Health check endpoint URL
            priority: Priority for load balancing

        Returns:
            Elder response with registered application

        Raises:
            ElderError: Registration failed
        """
        payload = {
            "app_name": app_name,
            "hosts": hosts,
            "port": port,
            "protocol": protocol,
            "path": path,
            "health_check_url": health_check_url,
            "priority": priority,
            "registered_at": datetime.utcnow().isoformat(),
        }

        log.info(f"Registering app endpoint with Elder: {app_name}")

        try:
            result = await self._request(
                "POST", "/api/v1/applications", data=payload
            )
            log.info(f"Application registered successfully: {app_name}")
            return result
        except ElderConflictError:
            # App already exists, update instead
            log.info(f"Application already exists, updating: {app_name}")
            return await self._request(
                "PUT", f"/api/v1/applications/{app_name}", data=payload
            )

    async def sync_machine(self, machine: dict[str, Any]) -> dict[str, Any]:
        """Sync machine data to Elder.

        Synchronizes a machine record from Gough's database with Elder's
        infrastructure registry for unified resource discovery.

        Args:
            machine: Machine data dictionary with fields:
                - system_id or external_id: Unique machine identifier
                - hostname: Machine hostname
                - ip_address: IP address
                - status: Current status
                - machine_type: Instance type/size
                - architecture: CPU architecture
                - cpu_count: Number of CPUs
                - memory_mb: Memory in megabytes
                - storage_gb: Storage in gigabytes
                - zone: Zone/region
                - tags: Tags/labels
                - metadata: Additional metadata

        Returns:
            Elder response with sync status

        Raises:
            ElderError: Sync failed
        """
        # Extract/normalize machine data
        machine_id = machine.get("system_id") or machine.get("external_id")
        hostname = machine.get("hostname") or f"machine-{machine_id}"
        ip = machine.get("ip_address")
        status = machine.get("status", "unknown")

        # Build sync payload
        payload = {
            "machine_id": machine_id,
            "hostname": hostname,
            "ip_address": ip,
            "status": status,
            "machine_type": machine.get("machine_type"),
            "architecture": machine.get("architecture", "amd64"),
            "cpu_count": machine.get("cpu_count"),
            "memory_mb": machine.get("memory_mb"),
            "storage_gb": machine.get("storage_gb"),
            "zone": machine.get("zone", "default"),
            "tags": machine.get("tags", {}),
            "metadata": machine.get("metadata", {}),
            "synced_at": datetime.utcnow().isoformat(),
        }

        log.info(f"Syncing machine to Elder: {hostname} ({machine_id})")

        try:
            result = await self._request(
                "POST", "/api/v1/machines/sync", data=payload
            )
            log.info(f"Machine synced successfully: {hostname}")
            return result
        except ElderError as e:
            log.error(f"Machine sync failed: {str(e)}")
            raise


async def get_elder_client(db) -> Optional[ElderClient]:
    """Get configured Elder client from database.

    Reads Elder configuration from elder_config table and returns
    initialized client, or None if not configured.

    Args:
        db: PyDAL database instance

    Returns:
        ElderClient instance or None if not configured

    Raises:
        ElderError: Configuration invalid
    """
    # Check if elder_config table exists
    if "elder_config" not in db.tables:
        log.debug("Elder configuration not available (table missing)")
        return None

    # Get active configuration
    config = db(db.elder_config.is_active).select().first()

    if not config:
        log.debug("No active Elder configuration found")
        return None

    # Validate configuration
    if not config.elder_url or not config.api_key:
        log.error("Elder configuration incomplete (missing url or api_key)")
        raise ElderError("Elder configuration incomplete")

    log.debug(f"Initializing Elder client: {config.elder_url}")

    return ElderClient(
        elder_url=config.elder_url,
        api_key=config.api_key,
        timeout=float(config.timeout or 10.0),
        max_retries=int(config.max_retries or 3),
    )
