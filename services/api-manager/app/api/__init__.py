"""API Endpoints Module.

This module contains all REST API endpoints for Gough organized by domain.
"""

from .agents import agents_bp
from .audit import audit_bp
from .biomes import biomes_bp
from .capacity import capacity_bp
from .clouds import clouds_bp
from .clusters import clusters_bp
from .disks import disks_bp
from .integrations import integrations_bp
from .ipxe import ipxe_bp
from .joiner_secrets import joiner_secrets_bp
from .lxd_cluster import lxd_bp
from .migration import migration_bp
from .nodes import nodes_bp
from .primary import primary_bp
from .secrets import secrets_bp
from .shell import shell_bp
from .ssh_ca import ssh_ca_bp
from .storage import storage_bp
from .teams import teams_bp
from .webhooks import webhooks_bp

__all__ = [
    "agents_bp",
    "lxd_bp",
    "audit_bp",
    "capacity_bp",
    "clouds_bp",
    "clusters_bp",
    "disks_bp",
    "biomes_bp",
    "integrations_bp",
    "ipxe_bp",
    "joiner_secrets_bp",
    "migration_bp",
    "nodes_bp",
    "primary_bp",
    "secrets_bp",
    "shell_bp",
    "ssh_ca_bp",
    "storage_bp",
    "teams_bp",
    "webhooks_bp",
]
