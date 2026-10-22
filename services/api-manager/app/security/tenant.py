"""Tenant isolation primitives for multi-tenant security layer 1 + 4.

Layer 1: JWT tenant claim extraction; reject if missing or mismatched.
Layer 4: Postgres GUC (app.current_tenant) for RLS policies.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


# ==============================================================================
# Constants
# ==============================================================================

TENANT_CLAIM_NAME: str = "tenant"
DEFAULT_TENANT: str = "__default__"


# ==============================================================================
# Data Models
# ==============================================================================

class TenantContext(BaseModel):
    """Tenant context extracted from JWT + current request."""

    tenant_id: str = Field(..., min_length=1, description="Tenant ID from JWT claim")
    cross_tenant: bool = Field(
        default=False,
        description="Super-admin override to access cross-tenant data"
    )

    class Config:
        """Pydantic config."""
        frozen = True


# ==============================================================================
# Exceptions
# ==============================================================================

class TenantClaimMissingError(Exception):
    """Raised when tenant claim is missing from JWT payload."""

    pass


class TenantMismatchError(Exception):
    """Raised when request body tenant_id differs from JWT tenant claim."""

    pass


# ==============================================================================
# Tenant Extraction & Validation
# ==============================================================================

def extract_tenant_from_jwt(payload: dict) -> TenantContext:
    """Extract and validate tenant context from JWT payload.

    Args:
        payload: Decoded JWT payload dictionary.

    Returns:
        TenantContext with tenant_id + cross_tenant flag.

    Raises:
        TenantClaimMissingError: If tenant claim absent or empty.
    """
    tenant_id = payload.get(TENANT_CLAIM_NAME)

    if not tenant_id or not isinstance(tenant_id, str):
        raise TenantClaimMissingError(
            f"JWT missing or invalid {TENANT_CLAIM_NAME} claim"
        )

    cross_tenant = payload.get("cross_tenant", False) is True

    return TenantContext(tenant_id=tenant_id, cross_tenant=cross_tenant)


def assert_tenant_match(token_tenant: str, body_tenant: Optional[str]) -> None:
    """Validate request body tenant_id matches JWT tenant claim.

    Used by handlers that accept tenant_id in request body to prevent
    users from accessing data of a different tenant.

    Args:
        token_tenant: Tenant ID extracted from JWT.
        body_tenant: Tenant ID from request body (if provided).

    Raises:
        TenantMismatchError: If body_tenant is not None and != token_tenant.
    """
    if body_tenant is not None and body_tenant != token_tenant:
        raise TenantMismatchError(
            f"Request body tenant_id ({body_tenant}) "
            f"does not match JWT tenant ({token_tenant})"
        )


# ==============================================================================
# Middleware Integration
# ==============================================================================
#
# The request-time tenant bridge now lives in
# ``app.middleware.install_security_middleware`` (Step 1). It reads the
# ASGI-validated claims from ``request.scope["state"]["claims"]``, calls
# ``extract_tenant_from_jwt`` above to build the ``TenantContext``, stores it on
# ``g.tenant_context``, and pushes the Postgres ``app.current_tenant`` GUC via
# ``app.db.rls.set_current_tenant`` (RLS Layer 4 -- FIX #7a). The former
# ``tenant_middleware`` here re-decoded the bearer token itself with the
# HS256 ``decode_token`` helper, which penguin-aaa replaced; it was removed
# rather than left as dead code.
