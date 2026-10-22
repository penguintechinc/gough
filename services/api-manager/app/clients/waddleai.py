"""WaddleAI integration client for the Gough capacity & risk planes.

Implements the universal *Integration Authentication Pattern* from the
spec section "PenguinTech Product Integration Matrix → WaddleAI":

    1. Pull a per-cluster service-account credential bundle from Vault
       at ``secret/gough/<cluster-id>/integrations/waddleai/credentials``
       (KV v2). The bundle holds the service-account ``sub`` (machine
       identity), audience, scopes, and the Vault transit key name used
       to sign OIDC machine JWTs on every call.
    2. Mint a short-lived OIDC machine JWT using Vault transit
       ``transit_sign`` (no private key ever leaves Vault).
    3. Send the request to WaddleAI with ``Authorization: Bearer <jwt>``.

License-handling per spec — the integration is **always configured** in
Gough; license enforcement lives on WaddleAI's side. We therefore do
**not** pre-check entitlement and we do **not** raise an exception when
WaddleAI returns 402 — we surface the structured error verbatim to the
caller in a typed dataclass so the API endpoint can include it in the
response envelope's ``meta``.

Errors
======

* ``WaddleAIError``               — base class for unexpected hard errors.
* ``WaddleAIDegradedResponse``    — typed dataclass returned for 402 (the
                                    integration is healthy; license absent
                                    on WaddleAI's side). NOT an exception.
* ``WaddleAIUnavailableResponse`` — typed dataclass returned for 5xx /
                                    timeouts / network errors after retry
                                    exhaustion. NOT an exception.

Networking
==========

* ``httpx.AsyncClient`` — async, HTTP/2 enabled.
* 30s request timeout.
* Exponential backoff 5s → 10s → 20s → 40s → 60s (cap), max 3 retries
  on 5xx and transport errors. **Never** retries on 4xx (including 402,
  401, 403, 404) because those are caller-actionable.

All return models are Pydantic v2; all internal state classes use
``@dataclass(slots=True)`` per backend Python standards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field, field_validator

from .vault import VaultClient, VaultError, VaultPermissionDenied, VaultSealedError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (tuned per spec)
# ---------------------------------------------------------------------------

#: Default request timeout in seconds — covers TLS handshake + body read.
DEFAULT_TIMEOUT_SECONDS: float = 30.0

#: Maximum retries on 5xx / transport errors. 4xx are never retried.
MAX_RETRIES: int = 3

#: Exponential backoff schedule (seconds). Index = retry attempt (0-based).
#: Caps at 60s. Spec: "exponential backoff 5s→60s, max 3 retries on 5xx".
BACKOFF_SCHEDULE_SECONDS: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0, 60.0)

#: OIDC machine JWT lifetime. Short-lived per security.md.
JWT_LIFETIME_SECONDS: int = 300

#: Skew window (seconds) added to ``nbf`` to absorb minor clock drift.
JWT_NBF_SKEW_SECONDS: int = 30

#: Vault KV v2 path template for the per-cluster service-account bundle.
VAULT_CREDENTIALS_PATH_TEMPLATE: str = (
    "gough/{cluster_id}/integrations/waddleai/credentials"
)

#: Allowed forecast horizons per spec (Capacity API surface).
ALLOWED_HORIZON_DAYS: frozenset[int] = frozenset({1, 7, 30})


# ---------------------------------------------------------------------------
# Exceptions and degradation envelopes
# ---------------------------------------------------------------------------


class WaddleAIError(Exception):
    """Base exception for unrecoverable WaddleAI client errors.

    Used for credential-bundle problems, signing failures, and
    programming errors. Network and HTTP errors are surfaced as typed
    dataclasses (``WaddleAIDegradedResponse`` / ``WaddleAIUnavailableResponse``)
    so callers never see a 5xx leak through to the operator.
    """


class WaddleAIConfigError(WaddleAIError):
    """Raised when the Vault credentials bundle is missing or malformed."""


class WaddleAISigningError(WaddleAIError):
    """Raised when Vault transit signing fails for non-recoverable reasons."""


@dataclass(slots=True)
class WaddleAIDegradedResponse:
    """License absent on WaddleAI's side (HTTP 402).

    This is *not* an error from Gough's perspective — the integration is
    healthy; the operator just hasn't activated WaddleAI's license. The
    structured error body is preserved verbatim so we can surface it to
    the operator unchanged.

    Attributes
    ----------
    status_code:
        Always ``402`` for this type. Kept for symmetry with
        ``WaddleAIUnavailableResponse``.
    waddleai_message:
        Human-readable message from WaddleAI's error body. Surfaced
        verbatim in ``meta.waddleai_message``.
    waddleai_body:
        Full structured error body from WaddleAI (parsed JSON if the
        body was JSON, otherwise ``{"raw": "<text>"}``).
    """

    status_code: int = 402
    waddleai_message: str = ""
    waddleai_body: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WaddleAIUnavailableResponse:
    """WaddleAI is unreachable or returning 5xx after retry exhaustion.

    Attributes
    ----------
    status_code:
        ``0`` for transport failures, otherwise the final upstream HTTP
        status code (typically 5xx).
    diagnostic:
        One-line operator-facing diagnostic. Never includes secrets or
        full response bodies.
    """

    status_code: int = 0
    diagnostic: str = ""


# ---------------------------------------------------------------------------
# Pydantic v2 wire models
# ---------------------------------------------------------------------------


class NodeForecast(BaseModel):
    """Per-node resource forecast for a single horizon."""

    node_id: int = Field(..., description="Gough node primary key")
    cpu_used_pct: float = Field(..., ge=0.0, le=100.0)
    cpu_predicted_pct: float = Field(..., ge=0.0, le=100.0)
    ram_used_pct: float = Field(..., ge=0.0, le=100.0)
    ram_predicted_pct: float = Field(..., ge=0.0, le=100.0)
    disk_used_pct: float = Field(..., ge=0.0, le=100.0)
    disk_predicted_pct: float = Field(..., ge=0.0, le=100.0)
    net_used_bps: float = Field(..., ge=0.0)
    net_predicted_bps: float = Field(..., ge=0.0)
    confidence: str = Field(
        default="high",
        description="Forecast confidence — high|medium|low (low for stub)",
    )

    @field_validator("confidence")
    @classmethod
    def _confidence_enum(cls, v: str) -> str:
        if v not in ("high", "medium", "low"):
            raise ValueError(f"confidence must be high|medium|low, got {v!r}")
        return v


class ClusterAggregate(BaseModel):
    """Cluster-wide aggregate snapshot used for headroom decisions."""

    cpu_used_pct: float = Field(..., ge=0.0, le=100.0)
    cpu_predicted_pct: float = Field(..., ge=0.0, le=100.0)
    ram_used_pct: float = Field(..., ge=0.0, le=100.0)
    ram_predicted_pct: float = Field(..., ge=0.0, le=100.0)
    disk_used_pct: float = Field(..., ge=0.0, le=100.0)
    disk_predicted_pct: float = Field(..., ge=0.0, le=100.0)


class ForecastBundle(BaseModel):
    """Top-level forecast object returned from WaddleAI / synthesized locally."""

    horizon_days: int = Field(..., description="Forecast horizon in days")
    generated_at: datetime = Field(..., description="UTC generation time")
    confidence: str = Field(default="high")
    per_node: list[NodeForecast] = Field(default_factory=list)
    cluster: ClusterAggregate

    @field_validator("horizon_days")
    @classmethod
    def _horizon_allowed(cls, v: int) -> int:
        if v not in ALLOWED_HORIZON_DAYS:
            raise ValueError(
                f"horizon_days must be one of {sorted(ALLOWED_HORIZON_DAYS)}, got {v}"
            )
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_enum(cls, v: str) -> str:
        if v not in ("high", "medium", "low"):
            raise ValueError(f"confidence must be high|medium|low, got {v!r}")
        return v


class NodeRiskScore(BaseModel):
    """Outage-precursor risk score for a single node."""

    node_id: int = Field(..., description="Gough node primary key")
    risk_score: float = Field(..., ge=0.0, le=1.0, description="0.0=safe, 1.0=imminent")
    horizon_days: int = Field(default=7, description="Horizon the score applies to")
    drivers: list[str] = Field(
        default_factory=list,
        description="Top contributing signal names (e.g. smart.reallocated_sectors)",
    )
    confidence: str = Field(default="high")

    @field_validator("confidence")
    @classmethod
    def _confidence_enum(cls, v: str) -> str:
        if v not in ("high", "medium", "low"):
            raise ValueError(f"confidence must be high|medium|low, got {v!r}")
        return v


class MetricSample(BaseModel):
    """A single time-series sample pushed into WaddleAI's ingest API."""

    node_id: int
    metric_name: str = Field(..., min_length=1, max_length=255)
    value: float
    timestamp: datetime
    labels: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Credentials:
    """Service-account credential bundle materialized from Vault."""

    sub: str
    audience: str
    scopes: tuple[str, ...]
    transit_key_name: str
    issuer: str

    @classmethod
    def from_vault_data(cls, data: dict[str, Any]) -> "_Credentials":
        try:
            sub = str(data["sub"])
            audience = str(data["audience"])
            transit_key_name = str(data["transit_key_name"])
            issuer = str(data.get("issuer", "gough.api-manager"))
            raw_scopes = data.get("scopes", [])
        except KeyError as exc:
            raise WaddleAIConfigError(
                f"WaddleAI credentials bundle missing key: {exc}"
            ) from exc

        if not isinstance(raw_scopes, (list, tuple)):
            raise WaddleAIConfigError(
                "WaddleAI credentials bundle 'scopes' must be a list"
            )
        scopes = tuple(str(s) for s in raw_scopes)

        if not sub or not audience or not transit_key_name:
            raise WaddleAIConfigError(
                "WaddleAI credentials bundle has empty required field"
            )
        return cls(
            sub=sub,
            audience=audience,
            scopes=scopes,
            transit_key_name=transit_key_name,
            issuer=issuer,
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class WaddleAIClient:
    """Async client for WaddleAI's capacity-prediction & risk surfaces.

    Parameters
    ----------
    endpoint:
        Base URL for the WaddleAI service (no trailing slash required;
        the client normalizes it). Per spec this is the cluster-internal
        gRPC/REST endpoint resolved from the WaddleAI integration record.
    vault_client:
        ``VaultClient`` instance used to (a) read the credentials bundle
        and (b) sign OIDC machine JWTs via the transit engine.
    cluster_id:
        Identifier of the local Gough cluster — slots into the Vault
        KV path ``secret/gough/<cluster-id>/integrations/waddleai/credentials``.
    http_client:
        Optional pre-configured ``httpx.AsyncClient`` for tests. When
        ``None``, the client lazily creates one with HTTP/2 and the
        default timeout.
    timeout:
        Per-request timeout in seconds. Defaults to 30s.
    """

    def __init__(
        self,
        endpoint: str,
        vault_client: VaultClient,
        cluster_id: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint must be a non-empty URL")
        if not cluster_id:
            raise ValueError("cluster_id must be a non-empty string")

        self._endpoint = endpoint.rstrip("/")
        self._vault = vault_client
        self._cluster_id = cluster_id
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = http_client
        self._owns_http: bool = http_client is None
        self._creds: Optional[_Credentials] = None

    # ---- public API ------------------------------------------------------

    async def forecast(
        self,
        horizon_days: int = 7,
        node_ids: Optional[list[int]] = None,
    ) -> ForecastBundle | WaddleAIDegradedResponse | WaddleAIUnavailableResponse:
        """Pull capacity forecast for the given horizon.

        Parameters
        ----------
        horizon_days:
            One of ``1``, ``7``, or ``30``.
        node_ids:
            Optional filter — when provided, only forecasts for these
            nodes are requested.
        """
        if horizon_days not in ALLOWED_HORIZON_DAYS:
            raise ValueError(
                f"horizon_days must be one of {sorted(ALLOWED_HORIZON_DAYS)}, "
                f"got {horizon_days}"
            )

        params: dict[str, Any] = {"horizon_days": horizon_days}
        if node_ids:
            params["node_ids"] = ",".join(str(n) for n in node_ids)

        result = await self._request("GET", "/api/v1/forecast", params=params)
        if isinstance(result, (WaddleAIDegradedResponse, WaddleAIUnavailableResponse)):
            return result
        try:
            return ForecastBundle.model_validate(result)
        except Exception as exc:  # pragma: no cover - schema bug surfacing
            log.exception("WaddleAI forecast response failed schema validation")
            return WaddleAIUnavailableResponse(
                status_code=502,
                diagnostic=f"forecast schema mismatch: {exc.__class__.__name__}",
            )

    async def risks(
        self,
    ) -> list[NodeRiskScore] | WaddleAIDegradedResponse | WaddleAIUnavailableResponse:
        """Pull outage-precursor risk scores. Sorted desc by ``risk_score``."""
        result = await self._request("GET", "/api/v1/risks")
        if isinstance(result, (WaddleAIDegradedResponse, WaddleAIUnavailableResponse)):
            return result
        try:
            items = [NodeRiskScore.model_validate(r) for r in (result or [])]
        except Exception as exc:  # pragma: no cover - schema bug surfacing
            log.exception("WaddleAI risks response failed schema validation")
            return WaddleAIUnavailableResponse(
                status_code=502,
                diagnostic=f"risks schema mismatch: {exc.__class__.__name__}",
            )
        items.sort(key=lambda r: r.risk_score, reverse=True)
        return items

    async def ingest_metrics(
        self,
        batch: list[MetricSample],
    ) -> None | WaddleAIDegradedResponse | WaddleAIUnavailableResponse:
        """Push a 60s metrics batch into WaddleAI's ingest API.

        Returns ``None`` on success; degradation envelopes otherwise so
        the caller can drop the batch and retry on the next interval
        without crashing the ingest loop.
        """
        if not batch:
            return None

        payload = {
            "samples": [s.model_dump(mode="json") for s in batch],
        }
        result = await self._request(
            "POST",
            "/api/v1/ingest/metrics",
            json_body=payload,
        )
        if isinstance(result, (WaddleAIDegradedResponse, WaddleAIUnavailableResponse)):
            return result
        return None

    async def aclose(self) -> None:
        """Close the underlying HTTP client when this object owns it."""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    # ---- internals -------------------------------------------------------

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                http2=True,
            )
        return self._http

    def _load_credentials(self) -> _Credentials:
        """Materialize the service-account bundle from Vault (cached)."""
        if self._creds is not None:
            return self._creds

        path = VAULT_CREDENTIALS_PATH_TEMPLATE.format(cluster_id=self._cluster_id)
        try:
            resp = self._vault.kv_read(path)
        except VaultPermissionDenied as exc:
            raise WaddleAIConfigError(
                f"Vault permission denied reading {path}"
            ) from exc
        except VaultSealedError as exc:
            # Bubble as unavailable-style: this is not a programming bug.
            raise WaddleAIConfigError(
                f"Vault sealed/unreachable while reading {path}"
            ) from exc
        except VaultError as exc:
            raise WaddleAIConfigError(f"Vault error reading {path}: {exc}") from exc

        if not resp.data:
            raise WaddleAIConfigError(
                f"WaddleAI credentials bundle missing at Vault path {path}"
            )
        self._creds = _Credentials.from_vault_data(resp.data)
        return self._creds

    def _mint_machine_jwt(self, creds: _Credentials) -> str:
        """Mint an OIDC machine JWT signed via Vault transit.

        Format follows compact-JWT: ``header.payload.signature``. Header
        and payload are base64url(JSON); signature is sourced from
        Vault's transit ``sign_data`` and stripped of its ``vault:vN:``
        prefix to fit standard JWT framing.
        """
        import base64

        now = int(time.time())
        header = {"alg": "EdDSA", "typ": "JWT", "kid": creds.transit_key_name}
        payload = {
            "iss": creds.issuer,
            "sub": creds.sub,
            "aud": creds.audience,
            "iat": now,
            "nbf": now - JWT_NBF_SKEW_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
            "jti": str(uuid.uuid4()),
            "scope": " ".join(creds.scopes),
            "tenant": "gough-system",
        }

        def b64url(obj: dict[str, Any]) -> str:
            raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        signing_input = f"{b64url(header)}.{b64url(payload)}"

        try:
            vault_sig = self._vault.transit_sign(
                key_name=creds.transit_key_name,
                message=signing_input.encode(),
            )
        except VaultError as exc:
            raise WaddleAISigningError(
                f"Vault transit_sign failed for key {creds.transit_key_name}: {exc}"
            ) from exc

        # Vault returns "vault:v<n>:<base64sig>". Strip the prefix to keep a
        # standards-compliant JWT serialization.
        if ":" in vault_sig:
            sig_b64 = vault_sig.rsplit(":", 1)[-1]
        else:
            sig_b64 = vault_sig

        return f"{signing_input}.{sig_b64}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any | WaddleAIDegradedResponse | WaddleAIUnavailableResponse:
        """Perform an authenticated request with retries.

        Returns:
            * The parsed JSON body on 2xx.
            * ``WaddleAIDegradedResponse`` on 402 (no retry).
            * ``WaddleAIUnavailableResponse`` on 5xx/transport errors
              after retry exhaustion.

        Other 4xx (401/403/404/422 …) are returned as
        ``WaddleAIUnavailableResponse`` with the upstream code so the
        caller still gets a non-crashing path; they are not retried.
        """
        creds = self._load_credentials()
        client = self._ensure_http()
        url = f"{self._endpoint}{path}"

        last_diagnostic = ""
        last_status = 0

        for attempt in range(MAX_RETRIES + 1):
            try:
                jwt_token = self._mint_machine_jwt(creds)
            except WaddleAIError as exc:
                # Signing failed for non-recoverable reason — degrade.
                log.error("WaddleAI JWT signing failed: %s", exc)
                return WaddleAIUnavailableResponse(
                    status_code=0,
                    diagnostic=f"jwt_signing_failed:{exc.__class__.__name__}",
                )

            headers = {
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/json",
                "X-Gough-Cluster-ID": self._cluster_id,
            }

            try:
                resp = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_diagnostic = (
                    f"transport:{exc.__class__.__name__}:{type(exc).__name__}"
                )
                last_status = 0
                log.warning(
                    "WaddleAI %s %s transport error (attempt %d/%d): %s",
                    method,
                    path,
                    attempt + 1,
                    MAX_RETRIES + 1,
                    exc,
                )
                if attempt < MAX_RETRIES:
                    await self._sleep_backoff(attempt)
                    continue
                return WaddleAIUnavailableResponse(
                    status_code=0,
                    diagnostic=last_diagnostic,
                )

            status = resp.status_code
            last_status = status

            if 200 <= status < 300:
                if status == 204 or not resp.content:
                    return None
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    return WaddleAIUnavailableResponse(
                        status_code=status,
                        diagnostic="invalid_json_body",
                    )

            if status == 402:
                # License absent on WaddleAI's side. Surface verbatim,
                # do not retry, do not raise.
                body = self._safe_json(resp)
                msg = ""
                if isinstance(body, dict):
                    err = body.get("error") if isinstance(body.get("error"), dict) else None
                    if err and isinstance(err.get("message"), str):
                        msg = err["message"]
                    elif isinstance(body.get("message"), str):
                        msg = body["message"]
                if not msg:
                    msg = f"WaddleAI returned 402: {resp.text[:160]}"
                log.info("WaddleAI 402 license_required: %s", msg)
                return WaddleAIDegradedResponse(
                    status_code=402,
                    waddleai_message=msg,
                    waddleai_body=body if isinstance(body, dict) else {"raw": resp.text},
                )

            if 400 <= status < 500:
                # Other client errors are caller-actionable and not
                # retried. We return an unavailable envelope rather than
                # raising so /capacity/* never returns a hard 5xx.
                log.warning("WaddleAI %s %s -> %d (no retry)", method, path, status)
                return WaddleAIUnavailableResponse(
                    status_code=status,
                    diagnostic=f"upstream_{status}",
                )

            # 5xx — retry with backoff.
            last_diagnostic = f"upstream_{status}"
            log.warning(
                "WaddleAI %s %s -> %d (attempt %d/%d)",
                method,
                path,
                status,
                attempt + 1,
                MAX_RETRIES + 1,
            )
            if attempt < MAX_RETRIES:
                await self._sleep_backoff(attempt)
                continue
            return WaddleAIUnavailableResponse(
                status_code=status,
                diagnostic=last_diagnostic,
            )

        # Defensive — should be unreachable; loop exits explicitly above.
        return WaddleAIUnavailableResponse(
            status_code=last_status,
            diagnostic=last_diagnostic or "unknown",
        )

    @staticmethod
    async def _sleep_backoff(attempt: int) -> None:
        idx = min(attempt, len(BACKOFF_SCHEDULE_SECONDS) - 1)
        await asyncio.sleep(BACKOFF_SCHEDULE_SECONDS[idx])

    @staticmethod
    def _safe_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return {"raw": resp.text}


__all__ = [
    "ALLOWED_HORIZON_DAYS",
    "ClusterAggregate",
    "ForecastBundle",
    "MetricSample",
    "NodeForecast",
    "NodeRiskScore",
    "WaddleAIClient",
    "WaddleAIConfigError",
    "WaddleAIDegradedResponse",
    "WaddleAIError",
    "WaddleAISigningError",
    "WaddleAIUnavailableResponse",
]
