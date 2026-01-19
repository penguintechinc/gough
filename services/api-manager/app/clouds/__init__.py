"""Cloud Provider Abstraction Module.

Provides a unified interface for managing machines across multiple cloud
providers including MaaS, LXD, AWS, GCP, Azure, and Vultr.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import (
    BaseCloud,
    CloudError,
    CloudAuthError,
    CloudNotFoundError,
    CloudQuotaError,
    Machine,
    MachineSpec,
    MachineState,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# Registry of available cloud providers
CLOUD_REGISTRY: dict[str, type[BaseCloud]] = {}


def register_cloud(name: str):
    """Decorator to register a cloud provider."""
    def decorator(cls: type[BaseCloud]):
        CLOUD_REGISTRY[name] = cls
        return cls
    return decorator


# Import providers to trigger registration
try:
    from .maas import MaaSCloud
    CLOUD_REGISTRY["maas"] = MaaSCloud
except ImportError:
    log.debug("MaaS provider not available (missing dependencies)")

try:
    from .lxd import LXDCloud
    CLOUD_REGISTRY["lxd"] = LXDCloud
except ImportError:
    log.debug("LXD provider not available (missing dependencies)")

try:
    from .aws import AWSCloud
    CLOUD_REGISTRY["aws"] = AWSCloud
except ImportError:
    log.debug("AWS provider not available (missing dependencies)")

try:
    from .gcp import GCPCloud
    CLOUD_REGISTRY["gcp"] = GCPCloud
except ImportError:
    log.debug("GCP provider not available (missing dependencies)")

try:
    from .azure import AzureCloud
    CLOUD_REGISTRY["azure"] = AzureCloud
except ImportError:
    log.debug("Azure provider not available (missing dependencies)")

try:
    from .vultr import VultrCloud
    CLOUD_REGISTRY["vultr"] = VultrCloud
except ImportError:
    log.debug("Vultr provider not available (missing dependencies)")


def get_cloud_provider(provider_type: str, config: dict) -> BaseCloud:
    """Factory function to get a cloud provider instance.

    Args:
        provider_type: Type of cloud provider (maas, lxd, aws, gcp, azure, vultr)
        config: Provider-specific configuration

    Returns:
        Configured cloud provider instance

    Raises:
        CloudError: If provider type is unknown or configuration is invalid
    """
    provider_type = provider_type.lower()

    if provider_type not in CLOUD_REGISTRY:
        available = list(CLOUD_REGISTRY.keys())
        raise CloudError(f"Unknown cloud provider: {provider_type}. Available: {available}")

    provider_class = CLOUD_REGISTRY[provider_type]

    try:
        provider = provider_class(config)
        return provider
    except Exception as e:
        raise CloudError(f"Failed to initialize {provider_type} provider: {e}")


def list_available_providers() -> list[dict]:
    """List all available cloud providers.

    Returns:
        List of provider info dictionaries
    """
    providers = []
    for name, cls in CLOUD_REGISTRY.items():
        providers.append({
            "name": name,
            "description": cls.__doc__.split("\n")[0] if cls.__doc__ else "",
            "supports_cloud_init": getattr(cls, "supports_cloud_init", True),
        })
    return providers


__all__ = [
    "BaseCloud",
    "CloudError",
    "CloudAuthError",
    "CloudNotFoundError",
    "CloudQuotaError",
    "Machine",
    "MachineSpec",
    "MachineState",
    "CLOUD_REGISTRY",
    "get_cloud_provider",
    "list_available_providers",
    "register_cloud",
]
