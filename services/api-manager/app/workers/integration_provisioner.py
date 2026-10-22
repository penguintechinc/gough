"""PenguinTech product integration provisioner.

Implements the spec section "PenguinTech Product Integration Matrix ->
Service-account lifecycle". Every integration uses a per-cluster service
account on the integrated product. The integrated product applies its own
permission and license enforcement to that account - Gough does NOT
duplicate license enforcement on its side.

Supported products:
    tobogganing, squawk, skauswatch, waddleai, nest, license-server, waddlebot

Each product has:
    * provisioning endpoint (POST to mint a service account)
    * introspect endpoint (GET to validate scope of an existing account)
    * revoke endpoint (DELETE)

Credentials are stored in Vault under
``secret/gough/<cluster-id>/integrations/<product>/{primary,secondary}``.

The rotate flow is dual-write:
    1. mint a new credential -> store at /secondary
    2. validate /secondary on the product
    3. promote /secondary to /primary, delete old /primary
At no point is the secondary slot empty before the primary is replaced.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import httpx

from ..clients.vault import VaultClient, VaultError

logger = logging.getLogger(__name__)


SUPPORTED_PRODUCTS: frozenset[str] = frozenset(
    {
        "tobogganing",
        "squawk",
        "skauswatch",
        "waddleai",
        "nest",
        "license-server",
        "waddlebot",
    }
)


# Minimum scope each Gough cluster service-account must hold on each product.
# Source: spec "PenguinTech Product Integration Matrix" -> required scopes column.
REQUIRED_SCOPES: dict[str, frozenset[str]] = {
    "tobogganing": frozenset({"tobogganing.tunnel.write", "tobogganing.health.read"}),
    "squawk": frozenset({"squawk.dhcp.write", "squawk.dns.write", "squawk.health.read"}),
    "skauswatch": frozenset(
        {"skauswatch.spire.bundle.read", "skauswatch.spire.entry.write"}
    ),
    "waddleai": frozenset({"waddleai.inference.invoke"}),
    "nest": frozenset({"nest.bucket.write", "nest.bucket.read"}),
    "license-server": frozenset({"license.entitlement.read"}),
    "waddlebot": frozenset({"waddlebot.notify.write"}),
}


class IntegrationError(Exception):
    """Base error for integration provisioning."""


class UnsupportedProductError(IntegrationError):
    """Product is not in SUPPORTED_PRODUCTS."""


class ProductApiError(IntegrationError):
    """Product admin API returned a non-2xx response."""


class CredentialMissingError(IntegrationError):
    """No credentials found for product."""


@dataclass(slots=True)
class Credentials:
    """Service-account credentials returned by the product."""

    product: str
    account_id: str
    client_id: str
    client_secret: str
    scopes: list[str] = field(default_factory=list)
    issued_at: str = ""
    expires_at: str = ""

    def to_vault(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_vault(cls, product: str, data: Mapping[str, Any]) -> "Credentials":
        return cls(
            product=product,
            account_id=str(data.get("account_id", "")),
            client_id=str(data.get("client_id", "")),
            client_secret=str(data.get("client_secret", "")),
            scopes=list(data.get("scopes", []) or []),
            issued_at=str(data.get("issued_at", "")),
            expires_at=str(data.get("expires_at", "")),
        )


@dataclass(slots=True)
class ScopeValidationResult:
    """Result of calling a product's token-introspect endpoint."""

    product: str
    valid: bool
    granted_scopes: list[str]
    missing_scopes: list[str]
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- helpers ----------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expiry_iso(seconds: int = 90 * 24 * 60 * 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _vault_path(cluster_id: str, product: str, slot: str) -> str:
    if slot not in ("primary", "secondary"):
        raise ValueError(f"slot must be 'primary' or 'secondary', got {slot!r}")
    return f"gough/{cluster_id}/integrations/{product}/{slot}"


def _check_product(product: str) -> None:
    if product not in SUPPORTED_PRODUCTS:
        raise UnsupportedProductError(
            f"{product!r} is not a supported product (got {sorted(SUPPORTED_PRODUCTS)})"
        )


# ---------- product admin endpoint config -----------------------------------

# Each product is configured via env vars - the API URL and an admin bootstrap
# token used only to mint the per-cluster service account.

@dataclass(slots=True)
class ProductEndpoint:
    base_url: str
    admin_token: str

    @property
    def provision_path(self) -> str:
        return "/api/v1/admin/service-accounts"

    @property
    def introspect_path(self) -> str:
        return "/api/v1/admin/service-accounts/introspect"

    def revoke_path(self, account_id: str) -> str:
        return f"/api/v1/admin/service-accounts/{account_id}"


def _load_endpoint(product: str) -> ProductEndpoint:
    """Load product admin URL + bootstrap token from env."""
    _check_product(product)
    env_key = product.upper().replace("-", "_")
    base = os.getenv(f"INTEGRATION_{env_key}_URL")
    token = os.getenv(f"INTEGRATION_{env_key}_ADMIN_TOKEN")
    if not base:
        raise IntegrationError(
            f"Missing INTEGRATION_{env_key}_URL env var for product {product}"
        )
    if not token:
        raise IntegrationError(
            f"Missing INTEGRATION_{env_key}_ADMIN_TOKEN env var for product {product}"
        )
    return ProductEndpoint(base_url=base.rstrip("/"), admin_token=token)


# ---------- public API -------------------------------------------------------


class IntegrationProvisioner:
    """Provision, validate, rotate, and revoke product service accounts."""

    def __init__(
        self,
        vault_client: VaultClient,
        cluster_id: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        self.vault: VaultClient = vault_client
        self.cluster_id: str = cluster_id or os.getenv("GOUGH_CLUSTER_ID", "default")
        self._http: Optional[httpx.AsyncClient] = http_client
        self._owns_http: bool = http_client is None
        self._timeout: float = request_timeout_seconds

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---- credential storage ------------------------------------------------

    def _read_credential(
        self, product: str, slot: str = "primary"
    ) -> Optional[Credentials]:
        path = _vault_path(self.cluster_id, product, slot)
        try:
            resp = self.vault.kv_read(path)
        except VaultError as exc:
            logger.error("Vault read failed for %s: %s", path, exc)
            return None
        if not resp.data:
            return None
        return Credentials.from_vault(product, resp.data)

    def _write_credential(
        self, product: str, creds: Credentials, slot: str = "primary"
    ) -> None:
        path = _vault_path(self.cluster_id, product, slot)
        self.vault.kv_write(path, creds.to_vault())

    def _delete_credential(self, product: str, slot: str) -> None:
        # Vault soft-delete - overwrite with empty marker so subsequent reads
        # return empty data (kv_read already handles missing path gracefully).
        path = _vault_path(self.cluster_id, product, slot)
        try:
            self.vault.kv_write(path, {"deleted_at": _now_iso()})
        except VaultError as exc:
            logger.warning("Could not soft-delete %s: %s", path, exc)

    # ---- ensure_service_account -------------------------------------------

    async def ensure_service_account(
        self, product: str, cluster_id: Optional[str] = None
    ) -> Credentials:
        """Return existing primary credentials or provision new ones.

        Args:
            product: Product key (must be in SUPPORTED_PRODUCTS).
            cluster_id: Optional override of self.cluster_id.

        Returns:
            Valid Credentials for the per-cluster service account.
        """
        _check_product(product)
        if cluster_id:
            self.cluster_id = cluster_id

        existing = self._read_credential(product, "primary")
        if existing and existing.client_secret:
            logger.info("Found existing primary credentials for %s", product)
            return existing

        creds = await self._mint_service_account(product)
        self._write_credential(product, creds, slot="primary")
        logger.info(
            "Provisioned new service account product=%s account_id=%s",
            product,
            creds.account_id,
        )
        return creds

    async def _mint_service_account(self, product: str) -> Credentials:
        """Call the product's admin API to mint a new service account."""
        endpoint = _load_endpoint(product)
        client = await self._client()
        body = {
            "name": f"gough-{self.cluster_id}",
            "cluster_id": self.cluster_id,
            "scopes": sorted(REQUIRED_SCOPES.get(product, frozenset())),
            "lifetime_seconds": 90 * 24 * 60 * 60,
        }
        url = f"{endpoint.base_url}{endpoint.provision_path}"
        try:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {endpoint.admin_token}"},
                json=body,
            )
        except httpx.RequestError as exc:
            raise ProductApiError(f"Could not reach {product} at {url}: {exc}") from exc
        if resp.status_code >= 300:
            raise ProductApiError(
                f"Provision failed for {product}: HTTP {resp.status_code} {resp.text}"
            )
        data = resp.json()
        return Credentials(
            product=product,
            account_id=str(data["account_id"]),
            client_id=str(data["client_id"]),
            client_secret=str(data["client_secret"]),
            scopes=list(data.get("scopes", [])),
            issued_at=str(data.get("issued_at", _now_iso())),
            expires_at=str(data.get("expires_at", _expiry_iso())),
        )

    # ---- validate_scope ----------------------------------------------------

    async def validate_scope(
        self, product: str, credentials: Optional[Credentials] = None
    ) -> ScopeValidationResult:
        """Call the product's introspect endpoint and verify scope.

        Args:
            product: Product key.
            credentials: Optional credentials to validate; defaults to primary.

        Returns:
            ScopeValidationResult.
        """
        _check_product(product)
        creds = credentials or self._read_credential(product, "primary")
        if creds is None or not creds.client_secret:
            raise CredentialMissingError(
                f"No primary credentials stored for {product}"
            )

        endpoint = _load_endpoint(product)
        client = await self._client()
        url = f"{endpoint.base_url}{endpoint.introspect_path}"
        try:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {endpoint.admin_token}"},
                json={"client_id": creds.client_id, "client_secret": creds.client_secret},
            )
        except httpx.RequestError as exc:
            raise ProductApiError(f"Introspect failed for {product}: {exc}") from exc
        if resp.status_code >= 300:
            raise ProductApiError(
                f"Introspect HTTP {resp.status_code} for {product}: {resp.text}"
            )
        data = resp.json()
        granted = list(data.get("scopes", []) or [])
        required = REQUIRED_SCOPES.get(product, frozenset())
        missing = sorted(s for s in required if s not in set(granted))
        return ScopeValidationResult(
            product=product,
            valid=bool(data.get("active", False)) and not missing,
            granted_scopes=granted,
            missing_scopes=missing,
            expires_at=str(data.get("expires_at", "")),
        )

    # ---- rotate ------------------------------------------------------------

    async def rotate(self, product: str) -> Credentials:
        """Dual-write rotation - never leaves zero valid credentials.

        Steps:
            1. mint new credential, store as /secondary
            2. validate /secondary on product
            3. promote /secondary -> /primary
            4. revoke old /primary on product
            5. delete /secondary slot

        Returns:
            The new active primary credentials.
        """
        _check_product(product)
        old_primary = self._read_credential(product, "primary")

        new_creds = await self._mint_service_account(product)
        # Step 1 - write secondary first (primary is still valid).
        self._write_credential(product, new_creds, slot="secondary")

        # Step 2 - validate secondary scope on product.
        validation = await self.validate_scope(product, new_creds)
        if not validation.valid:
            # Roll back: revoke the just-minted account, leave primary intact.
            await self._revoke_on_product(product, new_creds.account_id, best_effort=True)
            self._delete_credential(product, slot="secondary")
            raise ProductApiError(
                f"Scope validation failed on rotated cred for {product}: "
                f"missing={validation.missing_scopes}"
            )

        # Step 3 - promote secondary to primary atomically (write primary,
        # then delete secondary). Even if interrupted between these two
        # writes, primary holds the new value and rotation completes on retry.
        self._write_credential(product, new_creds, slot="primary")

        # Step 4 - revoke old credential best-effort. Failure here does not
        # leave the system without credentials; both old and new are valid
        # until the product evicts the old account.
        if old_primary and old_primary.account_id:
            await self._revoke_on_product(
                product, old_primary.account_id, best_effort=True
            )

        # Step 5 - clear secondary slot.
        self._delete_credential(product, slot="secondary")
        logger.info(
            "Rotated credentials product=%s old=%s new=%s",
            product,
            getattr(old_primary, "account_id", None),
            new_creds.account_id,
        )
        return new_creds

    # ---- compromise response ----------------------------------------------

    async def compromise_response(self, product: str) -> Credentials:
        """Immediate revoke of current credential + mint new account.

        Unlike rotate(), this does NOT preserve the old credential. Used when
        a credential is known to be leaked.
        """
        _check_product(product)
        old = self._read_credential(product, "primary")
        if old and old.account_id:
            await self._revoke_on_product(product, old.account_id, best_effort=False)

        new_creds = await self._mint_service_account(product)
        self._write_credential(product, new_creds, slot="primary")
        # Wipe secondary in case a rotation was in flight when compromise hit.
        self._delete_credential(product, slot="secondary")
        logger.warning(
            "Compromise response executed product=%s revoked=%s new=%s",
            product,
            getattr(old, "account_id", None),
            new_creds.account_id,
        )
        return new_creds

    async def _revoke_on_product(
        self, product: str, account_id: str, *, best_effort: bool
    ) -> None:
        endpoint = _load_endpoint(product)
        client = await self._client()
        url = f"{endpoint.base_url}{endpoint.revoke_path(account_id)}"
        try:
            resp = await client.delete(
                url, headers={"Authorization": f"Bearer {endpoint.admin_token}"}
            )
        except httpx.RequestError as exc:
            if best_effort:
                logger.warning("Best-effort revoke failed for %s/%s: %s", product, account_id, exc)
                return
            raise ProductApiError(
                f"Revoke failed for {product}/{account_id}: {exc}"
            ) from exc
        if resp.status_code >= 300 and resp.status_code != 404:
            msg = (
                f"Revoke HTTP {resp.status_code} for {product}/{account_id}: {resp.text}"
            )
            if best_effort:
                logger.warning(msg)
                return
            raise ProductApiError(msg)
