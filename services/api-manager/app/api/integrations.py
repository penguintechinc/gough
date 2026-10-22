"""Integrations API.

Implements spec section "API Surface -> Integrations" and the operator-visible
surface from "PenguinTech Product Integration Matrix". Exposes the integration
status table and lifecycle operations (configure, rotate, compromise, validate)
for each PenguinTech product.

Routes:
    GET    /api/v1/integrations/status                       gough.cluster.read
    GET    /api/v1/integrations/{product}                    gough.cluster.read
    POST   /api/v1/integrations/{product}/configure          gough.cluster.admin
    POST   /api/v1/integrations/{product}/rotate-credentials gough.cluster.admin
    POST   /api/v1/integrations/{product}/compromise-response gough.cluster.superadmin
    POST   /api/v1/integrations/{product}/validate-scope     gough.cluster.admin

product is one of: tobogganing, squawk, skauswatch, waddleai, nest,
license-server, waddlebot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import dataclasses
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from quart import Blueprint, current_app, jsonify, request

from ..middleware import auth_required
from ..security.scope_enforcement import require_scopes
from ..workers.integration_provisioner import (
    Credentials,
    CredentialMissingError,
    IntegrationError,
    IntegrationProvisioner,
    ProductApiError,
    REQUIRED_SCOPES,
    SUPPORTED_PRODUCTS,
    UnsupportedProductError,
)

log = logging.getLogger(__name__)

integrations_bp = Blueprint("integrations", __name__)


# ---- product status table ---------------------------------------------------


@dataclass(slots=True)
class IntegrationStatusRow:
    """One row of the operator-visible integration table."""

    product: str
    provider: str  # "external" | "builtin" | "biome-deployed"
    status: str  # "configured" | "active" | "unconfigured" | "unreachable" | "degraded" | "ready"
    version: str
    auth: str
    action_needed: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "provider": self.provider,
            "status": self.status,
            "version": self.version,
            "auth": self.auth,
            "action_needed": self.action_needed,
        }


# Default rows from the spec's "Operator-Visible Surface" example. These
# defaults are overlaid with live provisioning state at request time.
_DEFAULT_ROWS: dict[str, IntegrationStatusRow] = {
    "tobogganing": IntegrationStatusRow(
        product="tobogganing",
        provider="external",
        status="configured",
        version="2.4.1",
        auth="SPIFFE-fed",
        action_needed="—",
    ),
    "squawk": IntegrationStatusRow(
        product="squawk",
        provider="builtin",
        status="active",
        version="M1-fallback",
        auth="built-in",
        action_needed="Deploy Squawk biome",
    ),
    "skauswatch": IntegrationStatusRow(
        product="skauswatch",
        provider="builtin",
        status="active",
        version="M1-fallback",
        auth="built-in",
        action_needed="Deploy Skauswatch biome",
    ),
    "waddleai": IntegrationStatusRow(
        product="waddleai",
        provider="external",
        status="unconfigured",
        version="—",
        auth="OIDC-machine",
        action_needed="License absent (banner shown)",
    ),
    "nest": IntegrationStatusRow(
        product="nest",
        provider="biome-deployed",
        status="ready",
        version="1.0.0",
        auth="storage-acls",
        action_needed="—",
    ),
    "license-server": IntegrationStatusRow(
        product="license-server",
        provider="external",
        status="configured",
        version="—",
        auth="OIDC-machine",
        action_needed="—",
    ),
    "waddlebot": IntegrationStatusRow(
        product="waddlebot",
        provider="external",
        status="unconfigured",
        version="—",
        auth="OIDC-machine",
        action_needed="Configure webhook",
    ),
}


# ---- provisioner accessor ---------------------------------------------------


def _get_provisioner() -> IntegrationProvisioner:
    """Return the per-app IntegrationProvisioner, creating one if needed."""
    prov: Optional[IntegrationProvisioner] = current_app.config.get(
        "INTEGRATION_PROVISIONER"
    )
    if prov is None:
        from ..clients.vault import VaultClient

        vault = current_app.config.get("VAULT_CLIENT") or VaultClient()
        prov = IntegrationProvisioner(
            vault_client=vault,
            cluster_id=os.getenv("GOUGH_CLUSTER_ID"),
        )
        current_app.config["INTEGRATION_PROVISIONER"] = prov
    return prov


def _validate_product(product: str) -> Optional[tuple[Any, int]]:
    if product not in SUPPORTED_PRODUCTS:
        return (
            jsonify(
                {
                    "error": "unsupported_product",
                    "product": product,
                    "supported": sorted(SUPPORTED_PRODUCTS),
                }
            ),
            404,
        )
    return None


def _safe_credentials_view(creds: Credentials) -> dict[str, Any]:
    """Return credentials with the secret redacted (never echo client_secret)."""
    return {
        "product": creds.product,
        "account_id": creds.account_id,
        "client_id": creds.client_id,
        "client_secret_masked": (
            f"{creds.client_secret[:4]}...{creds.client_secret[-4:]}"
            if len(creds.client_secret) > 8
            else "****"
        ),
        "scopes": creds.scopes,
        "issued_at": creds.issued_at,
        "expires_at": creds.expires_at,
    }


async def _probe_integration_health(
    product: str, creds: Optional[Credentials] = None
) -> str:
    """Probe integration health; return status: 'active', 'unreachable', 'unconfigured', or 'degraded'."""
    if not creds or not creds.client_secret:
        return "unconfigured"

    # Construct endpoint URL per product
    health_endpoints = {
        "tobogganing": "https://tobogganing.penguintech.cloud/api/v1/health",
        "squawk": "http://squawk:8080/api/v1/health",
        "skauswatch": "https://skauswatch.penguintech.cloud/api/v1/health",
        "waddleai": "https://waddleai.penguintech.cloud/api/v1/health",
        "waddlebot": "https://waddlebot.app/api/v1/health",
    }

    endpoint = health_endpoints.get(product)
    if not endpoint:
        return "degraded"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(endpoint)
            if resp.status_code == 200:
                return "active"
            else:
                return "degraded"
    except (httpx.NetworkError, asyncio.TimeoutError):
        return "unreachable"
    except Exception:
        return "degraded"


# ---- routes -----------------------------------------------------------------


@integrations_bp.route("/status", methods=["GET"])
@auth_required
@require_scopes("gough.cluster.read")
async def get_integration_status() -> Any:
    """Return the operator-visible integration table.

    Per spec "PenguinTech Product Integration Matrix -> Operator-Visible Surface".
    Probes each integration's /healthz endpoint to determine status.
    """
    prov = _get_provisioner()
    rows: list[dict[str, Any]] = []
    for product, default in _DEFAULT_ROWS.items():
        row = IntegrationStatusRow(**dataclasses.asdict(default))
        creds = prov._read_credential(product, "primary")  # noqa: SLF001 - intentional

        # Probe health
        health_status = await _probe_integration_health(product, creds)
        row.status = health_status

        if creds and creds.client_secret:
            row.action_needed = "—"
        rows.append(row.to_dict())
    return jsonify({"integrations": rows}), 200


@integrations_bp.route("/<product>", methods=["GET"])
@auth_required
@require_scopes("gough.cluster.read")
async def get_integration(product: str) -> Any:
    """Return per-product config + service-account scope."""
    err = _validate_product(product)
    if err:
        return err
    prov = _get_provisioner()
    creds = prov._read_credential(product, "primary")  # noqa: SLF001
    default = _DEFAULT_ROWS[product]
    body: dict[str, Any] = {
        "product": product,
        "provider": default.provider,
        "auth": default.auth,
        "required_scopes": sorted(REQUIRED_SCOPES.get(product, frozenset())),
        "configured": bool(creds and creds.client_secret),
    }
    if creds and creds.client_secret:
        body["service_account"] = _safe_credentials_view(creds)
    return jsonify(body), 200


@integrations_bp.route("/<product>/configure", methods=["POST"])
@auth_required
@require_scopes("gough.cluster.admin")
async def configure_integration(product: str) -> Any:
    """Provision (or re-provision) the per-cluster service account on the product."""
    err = _validate_product(product)
    if err:
        return err
    prov = _get_provisioner()
    payload = await request.get_json(silent=True) or {}
    cluster_id = payload.get("cluster_id")
    try:
        creds = await prov.ensure_service_account(
            product, cluster_id=cluster_id if isinstance(cluster_id, str) else None
        )
    except UnsupportedProductError as exc:
        return jsonify({"error": "unsupported_product", "detail": str(exc)}), 400
    except ProductApiError as exc:
        log.error("Configure integration failed product=%s: %s", product, exc)
        return jsonify({"error": "provision_failed", "detail": str(exc)}), 502
    except IntegrationError as exc:
        log.error("Configure integration failed product=%s: %s", product, exc)
        return jsonify({"error": "provision_failed", "detail": str(exc)}), 500
    return (
        jsonify(
            {
                "product": product,
                "configured": True,
                "service_account": _safe_credentials_view(creds),
            }
        ),
        200,
    )


@integrations_bp.route("/<product>/rotate-credentials", methods=["POST"])
@auth_required
@require_scopes("gough.cluster.admin")
async def rotate_integration(product: str) -> Any:
    """90-day dual-write credential rotation."""
    err = _validate_product(product)
    if err:
        return err
    prov = _get_provisioner()
    try:
        creds = await prov.rotate(product)
    except CredentialMissingError as exc:
        return jsonify({"error": "no_credentials", "detail": str(exc)}), 404
    except ProductApiError as exc:
        log.error("Rotate failed product=%s: %s", product, exc)
        return jsonify({"error": "rotate_failed", "detail": str(exc)}), 502
    return (
        jsonify(
            {
                "product": product,
                "rotated": True,
                "service_account": _safe_credentials_view(creds),
            }
        ),
        200,
    )


@integrations_bp.route("/<product>/compromise-response", methods=["POST"])
@auth_required
@require_scopes("gough.cluster.superadmin")
async def compromise_response(product: str) -> Any:
    """Immediate revoke + new-account flow for a leaked credential."""
    err = _validate_product(product)
    if err:
        return err
    prov = _get_provisioner()
    try:
        creds = await prov.compromise_response(product)
    except ProductApiError as exc:
        log.error("Compromise response failed product=%s: %s", product, exc)
        return jsonify({"error": "compromise_failed", "detail": str(exc)}), 502
    return (
        jsonify(
            {
                "product": product,
                "revoked": True,
                "new_service_account": _safe_credentials_view(creds),
            }
        ),
        200,
    )


@integrations_bp.route("/<product>/validate-scope", methods=["POST"])
@auth_required
@require_scopes("gough.cluster.admin")
async def validate_scope(product: str) -> Any:
    """Verify the per-cluster service account has the required minimum scope."""
    err = _validate_product(product)
    if err:
        return err
    prov = _get_provisioner()
    try:
        result = await prov.validate_scope(product)
    except CredentialMissingError as exc:
        return jsonify({"error": "no_credentials", "detail": str(exc)}), 404
    except ProductApiError as exc:
        log.error("Validate scope failed product=%s: %s", product, exc)
        return jsonify({"error": "validate_failed", "detail": str(exc)}), 502
    return (
        jsonify(
            {
                "product": product,
                "required_scopes": sorted(REQUIRED_SCOPES.get(product, frozenset())),
                **result.to_dict(),
            }
        ),
        200,
    )
