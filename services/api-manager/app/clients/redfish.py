"""Redfish HTTPS client for BMC out-of-band control.

Implements the Gough spec's "Hardware Management → Out-of-Band Control (BMC)" and
"BMC Network Hardening" requirements. Credentials are sourced exclusively from
HashiCorp Vault via ``username_ref`` / ``password_ref`` on the ``node_bmc`` row;
plaintext credentials must never appear in constructor arguments, logs, or
serialized state.

Key behaviors
-------------
* **TOFU cert pinning.** First successful connect captures the SHA-256 of the
  presented BMC certificate and persists it to ``node_bmc.cert_fingerprint``;
  every subsequent connect validates the pin and raises
  :class:`BMCCertPinMismatch` on mismatch with no fallback. Pin rotation is the
  caller's responsibility (operator-approved API in ``api/v1/nodes.py``).
  TOFU (first connect without existing pin) requires BMC_ALLOW_INSECURE_TLS=true
  to explicitly allow unverified TLS handshake.
* **Default-credential refusal.** On first connect we probe whether well-known
  factory credential pairs (``root/calvin``, ``admin/admin``) authenticate; if
  any do, the binding is refused — :class:`BMCDefaultCredentialsDetected` is
  raised, ``node_bmc.factory_creds_detected`` is set, an audit row is appended,
  and a ``gough.bmc.default_credentials`` NATS event is emitted.
* **Session token caching.** Successful X-Auth-Token sessions are cached in
  Redis under ``bmc:<node_id>:session`` with TTL = vendor session TTL minus 30s.
  401 responses invalidate the cache and trigger exactly one re-login.
* **Audit trail.** Every public operation emits an audit-chain event via
  :class:`AuditEventWriter` with ``actor_sub='system:bmc-client'``,
  ``resource_kind='node_bmc'``, ``resource_id=node_id``,
  ``action='bmc.<verb>'``.

This module is purely synchronous (``requests``-based); BMC operations are
infrequent and benefit from blocking semantics + connection-level timeouts.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Optional, Protocol
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_SESSION_TTL_SEC = 1800
SESSION_TTL_MARGIN_SEC = 30
AUDIT_ACTOR_SUB = "system:bmc-client"
AUDIT_RESOURCE_KIND = "node_bmc"
NATS_SUBJECT_DEFAULT_CREDS = "gough.bmc.default_credentials"

# Well-known factory credential pairs that must never be left configured. Order
# is significant: the first pair that authenticates is reported.
DEFAULT_CREDENTIAL_PAIRS: tuple[tuple[str, str], ...] = (
    ("root", "calvin"),       # Dell iDRAC factory default
    ("admin", "admin"),       # Supermicro / generic factory default
    ("ADMIN", "ADMIN"),       # Older Supermicro firmware
    ("Administrator", "Administrator"),  # HPE iLO factory default
)

BootTarget = Literal["Pxe", "Hdd", "Cd", "UsbKey", "BiosSetup", "None"]
ResetType = Literal["On", "ForceOff", "GracefulShutdown", "GracefulRestart", "ForceRestart", "PowerCycle"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BMCError(Exception):
    """Base class for all BMC client errors."""


class BMCUnreachable(BMCError):
    """BMC endpoint did not respond, TLS handshake failed, or DNS failed."""


class BMCAuthFailure(BMCError):
    """BMC rejected credentials (HTTP 401/403) after a fresh login attempt."""


class BMCCertPinMismatch(BMCError):
    """Presented BMC cert fingerprint does not match the pinned value."""


class BMCFirmwareUpdateFailed(BMCError):
    """A firmware update operation reported a terminal failure."""


class BMCDefaultCredentialsDetected(BMCError):
    """Factory default credentials are still active on the BMC; binding refused."""

    def __init__(self, username: str) -> None:
        super().__init__(
            f"BMC accepts factory default credentials (user={username!r}); "
            "binding refused per BMC Network Hardening policy."
        )
        self.username = username


class BMCOperationFailed(BMCError):
    """Generic BMC operation failure — non-2xx response that isn't auth/pin related."""


# ---------------------------------------------------------------------------
# Protocols (loose duck-typed interfaces injected by the caller)
# ---------------------------------------------------------------------------


class _VaultClientProto(Protocol):
    def kv_read(self, path: str) -> Any:  # pragma: no cover - protocol
        ...


class _NodeBmcProto(Protocol):
    """Subset of fields the client mutates / reads on the ``node_bmc`` ORM row."""

    node_id: int
    endpoint: str
    username_ref: str
    password_ref: str
    cert_fingerprint: Optional[str]
    session_ttl_sec: int
    capabilities: Any
    factory_creds_detected: bool


class _AuditWriterProto(Protocol):
    def append(self, **kwargs: Any) -> Any:  # pragma: no cover - protocol
        ...


class _RedisProto(Protocol):
    def get(self, key: str) -> Any: ...  # pragma: no cover
    def set(self, key: str, value: Any, ex: Optional[int] = ..., nx: bool = ...) -> Any: ...  # pragma: no cover
    def delete(self, *keys: str) -> int: ...  # pragma: no cover


class _NatsPublisherProto(Protocol):
    def publish(self, subject: str, payload: dict[str, Any]) -> None:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Domain models (dataclasses with slots — see backend-python.md)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Sensor:
    """Thermal/power sensor reading from ``/redfish/v1/Chassis/.../Thermal|Power``."""

    name: str
    reading: Optional[float]
    units: Optional[str]
    health: Optional[str]
    kind: Literal["temperature", "fan", "power", "voltage"]


@dataclass(slots=True)
class SelEntry:
    """System Event Log entry from ``/redfish/v1/Systems/.../LogServices/Sel/Entries``."""

    id: str
    created: Optional[str]
    severity: Optional[str]
    message: str
    sensor_type: Optional[str] = None
    entry_code: Optional[str] = None


@dataclass(slots=True)
class FirmwareComponent:
    """Inventory entry from ``/redfish/v1/UpdateService/FirmwareInventory``."""

    component_id: str
    name: str
    version: str
    updateable: bool
    manufacturer: Optional[str] = None
    release_date: Optional[str] = None


@dataclass(slots=True)
class _SessionToken:
    token: str
    location: str
    expires_at_epoch: float


@dataclass(slots=True)
class _RedfishOpResult:
    status_code: int
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cert-pinning HTTPS adapter
# ---------------------------------------------------------------------------


def _fingerprint_from_der(der_bytes: bytes) -> str:
    """Return a colon-separated uppercase SHA-256 fingerprint (95 chars).

    Matches ``node_bmc.cert_fingerprint`` storage format ``"AA:BB:..."``.
    """
    digest = hashlib.sha256(der_bytes).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _normalize_fingerprint(fp: str) -> str:
    """Normalize a fingerprint to uppercase colon-separated form for comparison."""
    cleaned = fp.replace(":", "").replace(" ", "").strip().upper()
    if len(cleaned) != 64:
        raise ValueError(f"invalid SHA-256 fingerprint length: {len(cleaned)}")
    return ":".join(cleaned[i : i + 2] for i in range(0, len(cleaned), 2))


def _fetch_cert_fingerprint(host: str, port: int, timeout: float) -> str:
    """Open a raw TLS connection and return the peer cert SHA-256 fingerprint.

    Extracted as a module-level function so it can be patched in tests that
    use requests_mock (which bypasses transport adapters entirely).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            raw_ctx = ssl.create_default_context()
            raw_ctx.check_hostname = False
            raw_ctx.verify_mode = ssl.CERT_NONE
            with raw_ctx.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except OSError as exc:
        raise BMCUnreachable(f"TLS handshake failed for {host}:{port}: {exc}") from exc

    if not der:
        raise BMCUnreachable(f"no peer certificate from {host}:{port}")

    return _fingerprint_from_der(der)


class _PinnedAdapter(HTTPAdapter):
    """HTTPAdapter that captures and optionally pins the peer cert SHA-256.

    On first connect (``expected_fingerprint is None``) it captures the cert
    and stores it on ``self.captured_fingerprint``. On subsequent connects it
    raises :class:`BMCCertPinMismatch` immediately if the live cert SHA-256
    differs from ``expected_fingerprint`` — there is no fallback.
    """

    def __init__(self, expected_fingerprint: Optional[str]) -> None:
        self.expected_fingerprint: Optional[str] = (
            _normalize_fingerprint(expected_fingerprint) if expected_fingerprint else None
        )
        self.captured_fingerprint: Optional[str] = None
        super().__init__()

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        ctx = ssl.create_default_context()
        # BMC certs are typically self-signed; we DO NOT validate the CA chain
        # (we pin the fingerprint instead). Hostname checks are also disabled
        # because BMC certs frequently use serial numbers in the CN.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        self.poolmanager = PoolManager(*args, **kwargs)

    def send(self, request: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        # Out-of-band fingerprint capture: open a raw socket to the host:port,
        # do the TLS handshake, grab the DER-encoded peer cert, then close.
        # This runs once per request — cheap; BMC traffic is low-volume.
        parsed = urlparse(request.url)
        host = parsed.hostname or ""
        port = parsed.port or 443
        timeout = kwargs.get("timeout") or DEFAULT_TIMEOUT_SEC
        if isinstance(timeout, tuple):
            connect_timeout = timeout[0]
        else:
            connect_timeout = float(timeout)

        live_fp = _fetch_cert_fingerprint(host, port, connect_timeout)
        self.captured_fingerprint = live_fp

        if self.expected_fingerprint is not None and live_fp != self.expected_fingerprint:
            raise BMCCertPinMismatch(
                f"BMC cert fingerprint mismatch for {host}:{port}: "
                f"pinned={self.expected_fingerprint} live={live_fp}"
            )

        return super().send(request, **kwargs)


# ---------------------------------------------------------------------------
# Redfish client
# ---------------------------------------------------------------------------


class RedfishClient:
    """Synchronous Redfish client with cert pinning, default-creds refusal,
    Redis-backed session caching, and audit-chain integration.

    All credentials are sourced from Vault: the ``node_bmc`` row holds
    ``username_ref`` and ``password_ref`` which are KV v2 paths. Plaintext
    credentials never enter the constructor.
    """

    # ------------------------------------------------------------------
    # Construction / context
    # ------------------------------------------------------------------

    def __init__(
        self,
        endpoint: str,
        vault_client: _VaultClientProto,
        node_bmc_record: _NodeBmcProto,
        *,
        audit_writer: Optional[_AuditWriterProto] = None,
        redis_client: Optional[_RedisProto] = None,
        nats_publisher: Optional[_NatsPublisherProto] = None,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        cluster_id: Optional[str] = None,
        on_fingerprint_pinned: Optional[Any] = None,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint is required")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https":
            raise ValueError(f"BMC endpoint must use https://, got {parsed.scheme!r}")

        self.endpoint = endpoint.rstrip("/")
        self._vault = vault_client
        self._node_bmc = node_bmc_record
        self._audit = audit_writer
        self._redis = redis_client
        self._nats = nats_publisher
        self._timeout = timeout
        self._cluster_id = cluster_id
        self._on_pinned = on_fingerprint_pinned

        self._adapter = _PinnedAdapter(
            expected_fingerprint=getattr(node_bmc_record, "cert_fingerprint", None)
        )
        self._session = requests.Session()
        self._session.mount("https://", self._adapter)
        # We disable requests' own verify because cert pinning is enforced in
        # _PinnedAdapter.send().
        self._session.verify = False

        self._token: Optional[_SessionToken] = None

    @property
    def session_cache_key(self) -> str:
        return f"bmc:{self._node_bmc.node_id}:session"

    # ------------------------------------------------------------------
    # Vault credential resolution
    # ------------------------------------------------------------------

    def _read_credentials(self) -> tuple[str, str]:
        """Resolve ``username_ref``/``password_ref`` via Vault KV v2 lookups.

        ``username_ref`` and ``password_ref`` are stored as ``"<path>#<field>"``
        per the Gough spec (``secret/gough/<cluster-id>/nodes/<node-id>/bmc#username``).
        """
        username = self._kv_lookup(self._node_bmc.username_ref)
        password = self._kv_lookup(self._node_bmc.password_ref)
        return username, password

    def _kv_lookup(self, ref: str) -> str:
        if "#" not in ref:
            raise ValueError(f"credential ref must be 'path#field', got {ref!r}")
        path, _, field_name = ref.partition("#")
        resp = self._vault.kv_read(path)
        data = getattr(resp, "data", None)
        if data is None and isinstance(resp, dict):
            data = resp.get("data")
        if not isinstance(data, dict) or field_name not in data:
            raise BMCAuthFailure(
                f"Vault KV path {path!r} missing field {field_name!r}"
            )
        value = data[field_name]
        if not isinstance(value, str) or not value:
            raise BMCAuthFailure(f"Vault credential at {ref!r} is empty")
        return value

    # ------------------------------------------------------------------
    # Audit + telemetry helpers
    # ------------------------------------------------------------------

    def _audit_event(
        self,
        action: str,
        *,
        before: Optional[dict[str, Any]] = None,
        after: Optional[dict[str, Any]] = None,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.append(
                actor_sub=AUDIT_ACTOR_SUB,
                action=f"bmc.{action}",
                resource_kind=AUDIT_RESOURCE_KIND,
                resource_id=str(self._node_bmc.node_id),
                actor_scope=["system:bmc-client"],
                before=before,
                after=after,
            )
        except Exception:  # pragma: no cover - audit failures must not mask BMC ops
            logger.exception("audit append failed for action=%s node=%s",
                             action, self._node_bmc.node_id)

    def _emit_default_creds_event(self, username: str) -> None:
        if self._nats is None:
            return
        payload = {
            "node_id": self._node_bmc.node_id,
            "endpoint": self.endpoint,
            "username": username,
            "cluster_id": self._cluster_id,
            "ts": time.time(),
        }
        try:
            self._nats.publish(NATS_SUBJECT_DEFAULT_CREDS, payload)
        except Exception:  # pragma: no cover
            logger.exception("NATS publish failed for default-creds alert")

    # ------------------------------------------------------------------
    # First-connect: capture pin + refuse default credentials
    # ------------------------------------------------------------------

    def first_connect(self) -> str:
        """Perform first-connect handshake: pin cert, refuse default creds.

        Returns the captured fingerprint. Mutates ``node_bmc_record``:
        ``cert_fingerprint`` is set on success; ``factory_creds_detected`` is
        set if any default credential pair authenticates.

        TOFU (capturing cert without pre-existing pin) requires
        BMC_ALLOW_INSECURE_TLS=true to be explicitly set.
        """
        # Capture the cert fingerprint via a direct raw TLS connection so that
        # the capture is reliable even when the requests session is intercepted
        # (e.g. by requests_mock in tests).
        parsed = urlparse(f"{self.endpoint}/redfish/v1/")
        host = parsed.hostname or ""
        port = parsed.port or 443
        timeout = (
            self._timeout[0] if isinstance(self._timeout, tuple) else float(self._timeout)
        )

        # Gate TOFU behind explicit env flag when no pin is pre-configured
        if not getattr(self._node_bmc, "cert_fingerprint", None):
            if os.getenv("BMC_ALLOW_INSECURE_TLS", "").lower() not in ("true", "1", "yes"):
                raise BMCUnreachable(
                    f"BMC {host}:{port} has no certificate pin configured and "
                    "BMC_ALLOW_INSECURE_TLS is not enabled. Set BMC_ALLOW_INSECURE_TLS=true "
                    "to allow TOFU (Trust On First Use) certificate capture (NOT RECOMMENDED). "
                    "Operator should manually configure the BMC certificate fingerprint instead."
                )
            logger.warning(
                "BMC TOFU unverified certificate capture: node_id=%s endpoint=%s "
                "(set cert_fingerprint to pin the certificate for future connections)",
                self._node_bmc.node_id,
                self.endpoint,
            )

        captured = _fetch_cert_fingerprint(host, port, timeout)
        self._adapter.captured_fingerprint = captured

        # Enforce pin mismatch immediately if a pin was already set.
        if (
            self._adapter.expected_fingerprint is not None
            and captured != self._adapter.expected_fingerprint
        ):
            raise BMCCertPinMismatch(
                f"BMC cert fingerprint mismatch for {host}:{port}: "
                f"pinned={self._adapter.expected_fingerprint} live={captured}"
            )

        # Touch ServiceRoot to verify reachability.
        try:
            resp = self._session.get(
                f"{self.endpoint}/redfish/v1/", timeout=self._timeout
            )
        except BMCCertPinMismatch:
            raise
        except BMCUnreachable:
            raise
        except requests.RequestException as exc:
            raise BMCUnreachable(f"failed to reach Redfish service root: {exc}") from exc

        if resp.status_code >= 500:
            raise BMCUnreachable(
                f"Redfish service root returned {resp.status_code}"
            )

        # Persist the pin if this was a TOFU first-connect.
        if not getattr(self._node_bmc, "cert_fingerprint", None):
            self._node_bmc.cert_fingerprint = captured
            self._adapter.expected_fingerprint = captured
            if callable(self._on_pinned):
                try:
                    self._on_pinned(captured)
                except Exception:  # pragma: no cover
                    logger.exception("on_fingerprint_pinned callback failed")
            self._audit_event(
                "cert_pinned",
                after={"cert_fingerprint": captured},
            )

        # Default-credential probe.
        for user, pw in DEFAULT_CREDENTIAL_PAIRS:
            if self._probe_login(user, pw):
                self._node_bmc.factory_creds_detected = True
                self._audit_event(
                    "default_credentials_detected",
                    after={"username": user},
                )
                self._emit_default_creds_event(user)
                raise BMCDefaultCredentialsDetected(user)

        return captured

    def _probe_login(self, username: str, password: str) -> bool:
        """Probe a credential pair without persisting the resulting session."""
        try:
            resp = self._session.post(
                f"{self.endpoint}/redfish/v1/SessionService/Sessions",
                json={"UserName": username, "Password": password},
                timeout=self._timeout,
            )
        except BMCCertPinMismatch:
            raise
        except requests.RequestException:
            return False
        if resp.status_code in (200, 201):
            # Best-effort logout to avoid leaving a stray session on the BMC.
            location = resp.headers.get("Location")
            token = resp.headers.get("X-Auth-Token")
            if location and token:
                try:
                    self._session.delete(
                        self._absolute(location),
                        headers={"X-Auth-Token": token},
                        timeout=self._timeout,
                    )
                except requests.RequestException:  # pragma: no cover
                    pass
            return True
        return False

    # ------------------------------------------------------------------
    # Session login + caching
    # ------------------------------------------------------------------

    def _absolute(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return f"{self.endpoint}{path_or_url}"

    def _login(self) -> _SessionToken:
        """Acquire an X-Auth-Token, caching it in Redis when configured."""
        cached = self._cached_session()
        if cached is not None:
            self._token = cached
            return cached

        username, password = self._read_credentials()
        try:
            resp = self._session.post(
                f"{self.endpoint}/redfish/v1/SessionService/Sessions",
                json={"UserName": username, "Password": password},
                timeout=self._timeout,
            )
        except BMCCertPinMismatch:
            raise
        except requests.RequestException as exc:
            raise BMCUnreachable(f"login request failed: {exc}") from exc

        if resp.status_code in (401, 403):
            raise BMCAuthFailure(f"BMC rejected operator credentials ({resp.status_code})")
        if resp.status_code not in (200, 201):
            raise BMCOperationFailed(f"login returned HTTP {resp.status_code}")

        token = resp.headers.get("X-Auth-Token") or ""
        location = resp.headers.get("Location") or ""
        if not token or not location:
            raise BMCOperationFailed("login response missing X-Auth-Token/Location headers")

        ttl = max(int(getattr(self._node_bmc, "session_ttl_sec", DEFAULT_SESSION_TTL_SEC)),
                  SESSION_TTL_MARGIN_SEC + 1)
        sess = _SessionToken(
            token=token,
            location=self._absolute(location),
            expires_at_epoch=time.time() + (ttl - SESSION_TTL_MARGIN_SEC),
        )
        self._token = sess
        self._cache_session(sess, ttl - SESSION_TTL_MARGIN_SEC)
        return sess

    def _cached_session(self) -> Optional[_SessionToken]:
        if self._redis is None:
            return None
        try:
            raw = self._redis.get(self.session_cache_key)
        except Exception:  # pragma: no cover
            return None
        if not raw:
            return None
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            token, location, expires = text.split("|", 2)
            sess = _SessionToken(token=token, location=location, expires_at_epoch=float(expires))
        except (ValueError, AttributeError):
            return None
        if sess.expires_at_epoch <= time.time():
            self._invalidate_cached_session()
            return None
        return sess

    def _cache_session(self, sess: _SessionToken, ttl_sec: int) -> None:
        if self._redis is None:
            return
        payload = f"{sess.token}|{sess.location}|{sess.expires_at_epoch}"
        try:
            self._redis.set(self.session_cache_key, payload, ex=int(ttl_sec))
        except Exception:  # pragma: no cover
            logger.warning("redis set failed for %s", self.session_cache_key)

    def _invalidate_cached_session(self) -> None:
        self._token = None
        if self._redis is None:
            return
        try:
            self._redis.delete(self.session_cache_key)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # Authenticated request primitive
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        retry_on_401: bool = True,
    ) -> _RedfishOpResult:
        # Verify cert pin before every authenticated request.
        if self._adapter.expected_fingerprint is not None:
            parsed = urlparse(self._absolute(path))
            _req_host = parsed.hostname or ""
            _req_port = parsed.port or 443
            _req_timeout = (
                self._timeout[0] if isinstance(self._timeout, tuple)
                else float(self._timeout)
            )
            live_fp = _fetch_cert_fingerprint(_req_host, _req_port, _req_timeout)
            if live_fp != self._adapter.expected_fingerprint:
                raise BMCCertPinMismatch(
                    f"BMC cert fingerprint mismatch: "
                    f"pinned={self._adapter.expected_fingerprint} live={live_fp}"
                )

        sess = self._token or self._login()
        url = self._absolute(path)
        headers = {"X-Auth-Token": sess.token, "Accept": "application/json"}
        try:
            resp = self._session.request(
                method,
                url,
                json=json_body,
                headers=headers,
                timeout=self._timeout,
            )
        except BMCCertPinMismatch:
            raise
        except requests.RequestException as exc:
            raise BMCUnreachable(f"{method} {path} failed: {exc}") from exc

        if resp.status_code == 401 and retry_on_401:
            self._invalidate_cached_session()
            return self._request(method, path, json_body=json_body, retry_on_401=False)
        if resp.status_code in (401, 403):
            raise BMCAuthFailure(f"{method} {path} rejected with HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise BMCOperationFailed(
                f"{method} {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        body: dict[str, Any]
        if resp.content:
            try:
                body = resp.json()
            except (ValueError, Exception):
                body = {}
        else:
            body = {}
        return _RedfishOpResult(status_code=resp.status_code, body=body, headers=dict(resp.headers))

    # ------------------------------------------------------------------
    # Power control
    # ------------------------------------------------------------------

    def _reset(self, reset_type: ResetType) -> None:
        before = {"power_action": None}
        after = {"power_action": reset_type}
        self._audit_event("power_pre", before=before, after=after)
        self._request(
            "POST",
            "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
            json_body={"ResetType": reset_type},
        )
        self._audit_event(f"power_{reset_type.lower()}", before=before, after=after)

    def power_on(self) -> None:
        self._reset("On")

    def power_off(self) -> None:
        self._reset("ForceOff")

    def power_cycle(self) -> None:
        self._reset("PowerCycle")

    def force_restart(self) -> None:
        self._reset("ForceRestart")

    # ------------------------------------------------------------------
    # One-time boot override (Phase 1→2 reboot trigger)
    # ------------------------------------------------------------------

    def set_one_time_boot_target(self, target: BootTarget) -> None:
        body = {
            "Boot": {
                "BootSourceOverrideTarget": target,
                "BootSourceOverrideEnabled": "Once",
            }
        }
        self._request("PATCH", "/redfish/v1/Systems/1", json_body=body)
        self._audit_event(
            "set_one_time_boot",
            after={"target": target},
        )

    # ------------------------------------------------------------------
    # Sensors / SEL / firmware inventory
    # ------------------------------------------------------------------

    def get_sensors(self) -> list[Sensor]:
        thermal = self._request("GET", "/redfish/v1/Chassis/1/Thermal").body
        power = self._request("GET", "/redfish/v1/Chassis/1/Power").body
        sensors: list[Sensor] = []
        for entry in _safe_iter(thermal.get("Temperatures")):
            sensors.append(
                Sensor(
                    name=str(entry.get("Name", "")),
                    reading=_to_float(entry.get("ReadingCelsius")),
                    units="Celsius",
                    health=_get_in(entry, ("Status", "Health")),
                    kind="temperature",
                )
            )
        for entry in _safe_iter(thermal.get("Fans")):
            sensors.append(
                Sensor(
                    name=str(entry.get("Name", "")),
                    reading=_to_float(entry.get("Reading")),
                    units=str(entry.get("ReadingUnits", "RPM")),
                    health=_get_in(entry, ("Status", "Health")),
                    kind="fan",
                )
            )
        for entry in _safe_iter(power.get("PowerSupplies")):
            sensors.append(
                Sensor(
                    name=str(entry.get("Name", "")),
                    reading=_to_float(entry.get("PowerOutputWatts")),
                    units="Watts",
                    health=_get_in(entry, ("Status", "Health")),
                    kind="power",
                )
            )
        for entry in _safe_iter(power.get("Voltages")):
            sensors.append(
                Sensor(
                    name=str(entry.get("Name", "")),
                    reading=_to_float(entry.get("ReadingVolts")),
                    units="Volts",
                    health=_get_in(entry, ("Status", "Health")),
                    kind="voltage",
                )
            )
        self._audit_event("get_sensors", after={"count": len(sensors)})
        return sensors

    def get_sel_log(self) -> list[SelEntry]:
        body = self._request(
            "GET", "/redfish/v1/Systems/1/LogServices/Sel/Entries"
        ).body
        entries: list[SelEntry] = []
        for entry in _safe_iter(body.get("Members")):
            entries.append(
                SelEntry(
                    id=str(entry.get("Id") or entry.get("@odata.id", "")),
                    created=entry.get("Created"),
                    severity=entry.get("Severity"),
                    message=str(entry.get("Message", "")),
                    sensor_type=entry.get("SensorType"),
                    entry_code=entry.get("EntryCode"),
                )
            )
        self._audit_event("get_sel_log", after={"count": len(entries)})
        return entries

    def get_firmware_inventory(self) -> list[FirmwareComponent]:
        body = self._request(
            "GET", "/redfish/v1/UpdateService/FirmwareInventory"
        ).body
        components: list[FirmwareComponent] = []
        for member in _safe_iter(body.get("Members")):
            href = member.get("@odata.id")
            if not href:
                continue
            detail = self._request("GET", href).body
            components.append(
                FirmwareComponent(
                    component_id=str(detail.get("Id") or href.rsplit("/", 1)[-1]),
                    name=str(detail.get("Name", "")),
                    version=str(detail.get("Version", "")),
                    updateable=bool(detail.get("Updateable", False)),
                    manufacturer=detail.get("Manufacturer"),
                    release_date=detail.get("ReleaseDate"),
                )
            )
        self._audit_event("get_firmware_inventory", after={"count": len(components)})
        return components

    # ------------------------------------------------------------------
    # Firmware update
    # ------------------------------------------------------------------

    def update_firmware(self, component_id: str, image_url: str) -> str:
        """Submit a firmware update via the Redfish UpdateService and return the
        operation (task) id. The caller is responsible for polling the task to
        terminal state.
        """
        body = {
            "ImageURI": image_url,
            "Targets": [f"/redfish/v1/UpdateService/FirmwareInventory/{component_id}"],
        }
        result = self._request(
            "POST",
            "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
            json_body=body,
        )
        task_loc = result.headers.get("Location") or _get_in(result.body, ("@odata.id",))
        if not task_loc:
            raise BMCFirmwareUpdateFailed(
                f"firmware update for {component_id} accepted but no task id returned"
            )
        task_id = task_loc.rsplit("/", 1)[-1]
        self._audit_event(
            "firmware_update_submitted",
            after={"component_id": component_id, "task_id": task_id, "image_url": image_url},
        )
        return task_id

    # ------------------------------------------------------------------
    # BIOS settings
    # ------------------------------------------------------------------

    def get_bios_settings(self) -> dict[str, Any]:
        body = self._request("GET", "/redfish/v1/Systems/1/Bios").body
        attrs = body.get("Attributes")
        out: dict[str, Any] = dict(attrs) if isinstance(attrs, dict) else {}
        self._audit_event("get_bios_settings", after={"attribute_count": len(out)})
        return out

    def set_bios_setting(self, key: str, value: Any) -> None:
        self._request(
            "PATCH",
            "/redfish/v1/Systems/1/Bios/Settings",
            json_body={"Attributes": {key: value}},
        )
        self._audit_event(
            "set_bios_setting",
            before={"key": key},
            after={"key": key, "value": value},
        )

    # ------------------------------------------------------------------
    # Capability discovery
    # ------------------------------------------------------------------

    def get_capabilities(self) -> dict[str, Any]:
        """Probe service root + UpdateService to detect vendor + license-tier
        features. Result is cached on ``node_bmc.capabilities``.
        """
        root = self._request("GET", "/redfish/v1/").body
        update_service = self._request("GET", "/redfish/v1/UpdateService").body
        oem = root.get("Oem") if isinstance(root.get("Oem"), dict) else {}
        vendor = next(iter(oem.keys())) if oem else _detect_vendor_string(root)
        caps: dict[str, Any] = {
            "redfish_version": root.get("RedfishVersion"),
            "vendor": vendor,
            "product": root.get("Product"),
            "supports_simple_update": bool(
                _get_in(update_service, ("Actions", "#UpdateService.SimpleUpdate"))
            ),
            "firmware_inventory_uri": _get_in(
                update_service, ("FirmwareInventory", "@odata.id")
            ),
            "session_service_uri": _get_in(root, ("SessionService", "@odata.id")),
        }
        self._node_bmc.capabilities = caps
        self._audit_event("get_capabilities", after=caps)
        return caps

    # ------------------------------------------------------------------
    # Pin rotation (operator-driven; called from API layer)
    # ------------------------------------------------------------------

    def rotate_cert_fingerprint(self, new_fingerprint: str) -> None:
        """Replace the pinned fingerprint with ``new_fingerprint``.

        Performs no probe — the caller (``api/v1/nodes.py``) is responsible for
        scope-gating + operator approval per spec line 922.
        """
        normalized = _normalize_fingerprint(new_fingerprint)
        before = {"cert_fingerprint": getattr(self._node_bmc, "cert_fingerprint", None)}
        self._node_bmc.cert_fingerprint = normalized
        self._adapter.expected_fingerprint = normalized
        self._invalidate_cached_session()
        self._audit_event(
            "cert_pin_rotated",
            before=before,
            after={"cert_fingerprint": normalized},
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "RedfishClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_iter(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item


def _to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _get_in(obj: Any, path: tuple[str, ...]) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _detect_vendor_string(root: dict[str, Any]) -> Optional[str]:
    name = root.get("Name") or root.get("Product")
    if not isinstance(name, str):
        return None
    lower = name.lower()
    if "idrac" in lower or "dell" in lower:
        return "Dell"
    if "ilo" in lower or "hpe" in lower:
        return "HPE"
    if "supermicro" in lower:
        return "Supermicro"
    if "lenovo" in lower or "xclarity" in lower:
        return "Lenovo"
    if "openbmc" in lower:
        return "OpenBMC"
    return name
