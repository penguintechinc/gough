"""API Endpoints Module.

This module contains all REST API endpoints for Gough organized by domain.
"""

from .agents import agents_bp
from .clouds import clouds_bp
from .secrets import secrets_bp
from .shell import shell_bp
from .ssh_ca import ssh_ca_bp
from .storage import storage_bp
from .teams import teams_bp

__all__ = [
    "agents_bp",
    "clouds_bp",
    "secrets_bp",
    "shell_bp",
    "ssh_ca_bp",
    "storage_bp",
    "teams_bp",
]
