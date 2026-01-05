"""API Endpoints Module.

This module contains all REST API endpoints for Gough organized by domain.
"""

from .clouds import clouds_bp
from .secrets import secrets_bp
from .ssh_ca import ssh_ca_bp
from .teams import teams_bp

__all__ = ["secrets_bp", "clouds_bp", "teams_bp", "ssh_ca_bp"]
