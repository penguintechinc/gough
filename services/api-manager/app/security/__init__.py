"""Security module for Gough API Manager.

Provides OIDC scope management and centralized scope policy definitions.
Exports scope policy for use by middleware and decorators.
"""

from .scope_policy import KNOWN_SCOPES, SCOPE_POLICY

__all__ = ["KNOWN_SCOPES", "SCOPE_POLICY"]
