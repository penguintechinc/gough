"""Webhook delivery worker.

Fan-out delivery for webhook events with the spec-mandated retry policy,
canonical signing-string construction, and standard ``X-Gough-*`` headers.

Spec reference (Developer & Operator Experience → Webhooks):

  Canonical signing string:
      ``ts || "." || event_id || "." || sha256(raw_body_hex)``

  Retry policy (max 8 attempts):
      ``[1s, 5s, 30s, 5m, 30m, 2h, 12h]``

  Dead-letter NATS subject on terminal failure:
      ``gough.webhook.<tenant>.dead_letter``

  Standard headers:
      X-Gough-Signature, X-Gough-Key-Id, X-Gough-Timestamp,
      X-Gough-Event-Id, X-Gough-Cluster-Id, X-Gough-Delivery-Attempt
      User-Agent: gough-webhook/1.0
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

import httpx

from ..db.rls import get_current_tenant, set_current_tenant
from ..security.webhook_keys import (
    ECDSA_P256_SHA256,
    ED25519,
    HMAC_SHA256,
    VALID_MODES,
    WebhookKeyManager,
)

log = logging.getLogger(__name__)


# Spec: explicit per-attempt delays (attempt #1 is immediate; subsequent retries
# wait these durations between attempts). Max attempts = 1 + len(_RETRY_DELAYS).
_RETRY_DELAYS_SECONDS: tuple[float, ...] = (
    1.0,           # 1s
    5.0,           # 5s
    30.0,          # 30s
    5 * 60.0,      # 5m
    30 * 60.0,     # 30m
    2 * 3600.0,    # 2h
    12 * 3600.0,   # 12h
)
MAX_ATTEMPTS: int = 1 + len(_RETRY_DELAYS_SECONDS)  # = 8

REQUEST_TIMEOUT_SECONDS: float = 10.0
USER_AGENT: str = "gough-webhook/1.0"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class WebhookEndpoint:
    """A subscriber endpoint."""

    id: str
    tenant_id: str
    url: str
    signing_mode: str
    event_filter: tuple[str, ...] = ()
    active: bool = True

    def matches(self, event_type: str) -> bool:
        if not self.active:
            return False
        if not self.event_filter:
            return True
        for pattern in self.event_filter:
            if pattern == "*" or pattern == event_type:
                return True
            if pattern.endswith(".*") and event_type.startswith(pattern[:-2] + "."):
                return True
        return False


@dataclass(slots=True)
class DeliveryResult:
    """Outcome of a single dispatch."""

    endpoint_id: str
    event_id: str
    delivered: bool
    attempts: int
    final_status: Optional[int]
    final_error: Optional[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _rfc3339(ts: _dt.datetime) -> str:
    return ts.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{ts.microsecond:06d}Z"
    )


def _new_event_id() -> str:
    """Generate a ULID (Crockford base32, 26 chars)."""
    ts_ms = int(_utcnow().timestamp() * 1000) & ((1 << 48) - 1)
    rand = _secrets.token_bytes(10)
    raw = ts_ms.to_bytes(6, "big") + rand
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    # Encode 16 bytes (128 bits) → 26 chars Crockford base32.
    n = int.from_bytes(raw, "big")
    out = []
    for _ in range(26):
        out.append(alphabet[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


def build_signing_string(timestamp: str, event_id: str, raw_body: bytes) -> bytes:
    """Construct the canonical signing string per spec.

    ``ts || "." || event_id || "." || sha256(raw_body)``  where the body
    digest is hex-encoded (lower-case).
    """
    body_hex = hashlib.sha256(raw_body).hexdigest()
    return f"{timestamp}.{event_id}.{body_hex}".encode("ascii")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
class WebhookDispatcher:
    """Fan-out webhook delivery with spec-compliant retry/signing."""

    def __init__(
        self,
        db_session: Any,
        vault_client: Any,
        *,
        cluster_id: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        nats_publisher: Optional[Callable[[str, bytes], Awaitable[None]]] = None,
        endpoint_loader: Optional[
            Callable[[str], Iterable[WebhookEndpoint]]
        ] = None,
    ) -> None:
        self._db = db_session
        self._key_mgr = WebhookKeyManager(vault_client)
        self._cluster_id = cluster_id or os.environ.get("GOUGH_CLUSTER_ID", "unknown")
        self._sleep = sleep
        self._nats_publisher = nats_publisher
        self._endpoint_loader = endpoint_loader
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def dispatch(self, event: dict) -> list[DeliveryResult]:
        """Fan-out ``event`` to all matching endpoints.

        ``event`` must include ``type`` and ``tenant_id``; ``id`` is generated
        if absent. Each delivery runs concurrently and returns its own result.
        """
        event_type = event.get("type")
        tenant_id = event.get("tenant_id")
        if not event_type or not isinstance(event_type, str):
            raise ValueError("event.type is required")
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("event.tenant_id is required")

        event = dict(event)
        event.setdefault("id", _new_event_id())
        event.setdefault("timestamp", _rfc3339(_utcnow()))

        endpoints = list(self._load_endpoints(tenant_id))
        targets = [ep for ep in endpoints if ep.matches(event_type)]
        if not targets:
            return []

        coros = [self._deliver_one(ep, event) for ep in targets]
        return await asyncio.gather(*coros)

    # ------------------------------------------------------------------
    # Endpoint loading
    # ------------------------------------------------------------------
    def _load_endpoints(self, tenant_id: str) -> Iterable[WebhookEndpoint]:
        if self._endpoint_loader is not None:
            return self._endpoint_loader(tenant_id)
        # Default: penguin-dal read from ``webhook_endpoints``.
        if self._db is None:
            return ()
        # webhook_endpoints is RLS-protected (baseline migration
        # `rls_tables` -- see
        # alembic/versions/20260805_1000_baseline_full_schema.py). This
        # dispatcher is a background worker invoked outside Quart's HTTP
        # request pipeline, so `app.db.rls`'s tenant ContextVar is never set
        # by anything upstream -- an unset GUC fails RLS closed to zero rows
        # (see that module's docstring), which would make every dispatch()
        # silently deliver to nobody rather than raise. Unlike a worker that
        # genuinely spans tenants within one DB unit (see
        # app.workers.audit_chain_writer._cross_tenant_scope()),
        # dispatch()/`_load_endpoints` always resolve exactly one tenant_id
        # per call (validated in dispatch()), so the least-privilege fix is
        # to scope the GUC to that one tenant for the read, not grant the
        # cross-tenant sentinel.
        previous_tenant = get_current_tenant()
        set_current_tenant(tenant_id)
        try:
            rows = self._db.executesql(
                "SELECT id, tenant_id, url, signing_mode, event_filter, active "
                "FROM webhook_endpoints WHERE tenant_id = %(t)s AND active = TRUE",
                {"t": tenant_id},
                as_dict=True,
            )
        except Exception:  # pragma: no cover - schema/runtime concerns
            log.exception("failed to load webhook endpoints")
            return ()
        finally:
            set_current_tenant(previous_tenant)
        out: list[WebhookEndpoint] = []
        for r in rows:
            ef = r["event_filter"]
            if isinstance(ef, str):
                try:
                    ef = json.loads(ef)
                except Exception:
                    ef = []
            out.append(
                WebhookEndpoint(
                    id=str(r["id"]),
                    tenant_id=str(r["tenant_id"]),
                    url=str(r["url"]),
                    signing_mode=str(r["signing_mode"]),
                    event_filter=tuple(ef or ()),
                    active=bool(r["active"]),
                )
            )
        return out

    # ------------------------------------------------------------------
    # Single delivery (retry loop)
    # ------------------------------------------------------------------
    async def _deliver_one(
        self, endpoint: WebhookEndpoint, event: dict
    ) -> DeliveryResult:
        body_bytes = json.dumps(
            event, separators=(",", ":"), sort_keys=True, ensure_ascii=False
        ).encode("utf-8")

        last_status: Optional[int] = None
        last_error: Optional[str] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            timestamp = _rfc3339(_utcnow())
            try:
                key_id = self._key_mgr.ensure_keys_for_tenant(
                    endpoint.tenant_id, endpoint.signing_mode
                )
                signature = self._sign_payload(
                    body_bytes,
                    endpoint.signing_mode,
                    endpoint.tenant_id,
                    key_id.kid,
                    timestamp=timestamp,
                    event_id=str(event["id"]),
                )
            except Exception as exc:
                last_error = f"signing_failed: {exc}"
                log.error("webhook signing failed", exc_info=True)
                await self._maybe_sleep(attempt)
                continue

            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": USER_AGENT,
                "X-Gough-Signature": signature,
                "X-Gough-Key-Id": key_id.kid,
                "X-Gough-Timestamp": timestamp,
                "X-Gough-Event-Id": str(event["id"]),
                "X-Gough-Cluster-Id": self._cluster_id,
                "X-Gough-Delivery-Attempt": str(attempt),
            }

            try:
                resp = await self._http.post(
                    endpoint.url,
                    content=body_bytes,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                last_status = resp.status_code
                if 200 <= resp.status_code < 300:
                    return DeliveryResult(
                        endpoint_id=endpoint.id,
                        event_id=str(event["id"]),
                        delivered=True,
                        attempts=attempt,
                        final_status=resp.status_code,
                        final_error=None,
                    )
                last_error = f"http_{resp.status_code}"
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
            except httpx.HTTPError as exc:
                last_error = f"http_error: {exc}"
            except Exception as exc:  # pragma: no cover - defensive
                last_error = f"unexpected: {exc}"

            await self._maybe_sleep(attempt)

        # Terminal failure → dead-letter.
        await self._dead_letter(endpoint, event, last_status, last_error)
        return DeliveryResult(
            endpoint_id=endpoint.id,
            event_id=str(event["id"]),
            delivered=False,
            attempts=MAX_ATTEMPTS,
            final_status=last_status,
            final_error=last_error,
        )

    async def _maybe_sleep(self, attempt: int) -> None:
        # ``attempt`` is the just-completed attempt (1-indexed).
        if attempt < 1 or attempt > len(_RETRY_DELAYS_SECONDS):
            return
        delay = _RETRY_DELAYS_SECONDS[attempt - 1]
        await self._sleep(delay)

    async def _dead_letter(
        self,
        endpoint: WebhookEndpoint,
        event: dict,
        last_status: Optional[int],
        last_error: Optional[str],
    ) -> None:
        subject = f"gough.webhook.{endpoint.tenant_id}.dead_letter"
        payload = {
            "endpoint_id": endpoint.id,
            "tenant_id": endpoint.tenant_id,
            "url": endpoint.url,
            "event": event,
            "last_status": last_status,
            "last_error": last_error,
            "attempts": MAX_ATTEMPTS,
            "failed_at": _rfc3339(_utcnow()),
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if self._nats_publisher is None:
            log.warning("dead-letter (no NATS publisher): %s", subject)
            return
        try:
            await self._nats_publisher(subject, encoded)
        except Exception:  # pragma: no cover - defensive
            log.exception("failed to publish dead-letter %s", subject)

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------
    def _sign_payload(
        self,
        body_bytes: bytes,
        signing_mode: str,
        tenant_id: str,
        key_id: str,
        *,
        timestamp: str,
        event_id: str,
    ) -> str:
        """Produce ``X-Gough-Signature`` header value for the given mode."""
        if signing_mode not in VALID_MODES:
            raise ValueError(f"invalid signing_mode: {signing_mode}")

        signing_input = build_signing_string(timestamp, event_id, body_bytes)

        if signing_mode == HMAC_SHA256:
            secret = self._key_mgr.get_hmac_secret(tenant_id, key_id)
            mac = hmac.new(secret, signing_input, hashlib.sha256).hexdigest()
            return f"v1.hmac={mac}"

        if signing_mode == ED25519:
            sig = self._vault_sign(tenant_id, signing_mode, signing_input)
            return f"v1.ed25519={_b64(sig)}"

        if signing_mode == ECDSA_P256_SHA256:
            sig_der = self._vault_sign(tenant_id, signing_mode, signing_input)
            return f"v1.ecdsa={_b64(sig_der)}"

        raise ValueError(f"unhandled signing_mode: {signing_mode}")

    def _vault_sign(self, tenant_id: str, mode: str, signing_input: bytes) -> bytes:
        """Sign via Vault transit; return raw signature bytes.

        For Ed25519: returns the 64-byte raw signature.
        For ECDSA P-256: returns the DER-encoded ``r||s`` signature.
        """
        vault = self._key_mgr._vault  # documented internal access
        name = f"{tenant_id}/webhook-{mode}"
        kwargs = {
            "name": name,
            "hash_input": _b64(signing_input),
            "mount_point": "transit",
        }
        if mode == ECDSA_P256_SHA256:
            kwargs["hash_algorithm"] = "sha2-256"
            kwargs["prehashed"] = False
            kwargs["marshaling_algorithm"] = "asn1"
        # Ed25519 transit signs the input directly (no prehash).
        try:
            resp = vault.secrets.transit.sign_data(**kwargs)
        except TypeError:
            # Older hvac signature: data instead of hash_input.
            kwargs.pop("hash_input")
            kwargs["input"] = _b64(signing_input)
            resp = vault.secrets.transit.sign_data(**kwargs)

        data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
        sig = data.get("signature") or ""
        # Vault returns ``vault:vN:<base64>``.
        if not sig.startswith("vault:"):
            raise RuntimeError(f"unexpected vault signature format: {sig!r}")
        b64_sig = sig.rsplit(":", 1)[-1]
        return base64.b64decode(b64_sig)
