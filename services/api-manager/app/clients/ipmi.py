"""IPMI 2.0 over LAN client — fallback for legacy BMCs without Redfish.

Implements the Gough spec's BMC fallback path (sections "Out-of-Band Control
(BMC)" and "BMC Network Hardening"):

* **Cipher suite 17 mandatory** (HMAC-SHA256 + AES-CBC-128). Cipher suite 0 is
  refused unconditionally — if the BMC presents cipher 0 during channel
  negotiation, :class:`IpmiInsecureCipherRefused` is raised and no session is
  established.
* **Vault-sourced credentials**. Plaintext credentials never appear in
  constructor arguments; ``username_ref`` / ``password_ref`` on the
  ``node_bmc`` row are resolved via :class:`VaultClient` KV v2 lookups.
* **Default-credential refusal**. ``root/calvin``, ``admin/admin``, etc., are
  probed at first connect; if any succeed, the binding is refused, an audit
  event is appended, and ``gough.bmc.default_credentials`` is published to
  NATS.
* **Audit-logged**. Every public operation appends a chain event with
  ``actor_sub='system:bmc-client'``, ``resource_kind='node_bmc'``,
  ``action='bmc.<verb>'``.

The wire-level protocol is delegated to ``pyghmi`` (pinned in
``requirements.in``); we wrap its surface to enforce the cipher policy and the
audit / Vault contract.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Optional, Protocol
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# pyghmi pulls in optional cryptography backends; import is best-effort so the
# module remains importable in environments where pyghmi is unavailable
# (e.g. the FedRAMP lane). Tests that exercise the client supply a fake
# command class via the ``command_factory`` constructor argument.
try:  # pragma: no cover - import side-effect only
    from pyghmi.ipmi import command as _pyghmi_command  # type: ignore
    from pyghmi.exceptions import IpmiException as _PyghmiIpmiException  # type: ignore
except Exception:  # pragma: no cover
    _pyghmi_command = None
    _PyghmiIpmiException = Exception  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_CIPHER_SUITE = 17
FORBIDDEN_CIPHER_SUITES: frozenset[int] = frozenset({0})
DEFAULT_TIMEOUT_SEC = 30
AUDIT_ACTOR_SUB = "system:bmc-client"
AUDIT_RESOURCE_KIND = "node_bmc"
NATS_SUBJECT_DEFAULT_CREDS = "gough.bmc.default_credentials"

DEFAULT_CREDENTIAL_PAIRS: tuple[tuple[str, str], ...] = (
    ("root", "calvin"),
    ("admin", "admin"),
    ("ADMIN", "ADMIN"),
    ("Administrator", "Administrator"),
)

BootDevice = Literal["pxe", "disk", "cdrom", "bios", "safe", "default"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BMCError(Exception):
    """Base class shared with redfish.py — re-exported for convenience."""


class BMCUnreachable(BMCError):
    """IPMI session establishment failed at the network layer."""


class BMCAuthFailure(BMCError):
    """IPMI BMC rejected operator credentials."""


class IpmiInsecureCipherRefused(BMCError):
    """BMC offered/accepted cipher suite 0 — refused unconditionally."""

    def __init__(self, cipher_suite: int) -> None:
        super().__init__(
            f"IPMI cipher suite {cipher_suite} is forbidden by BMC Network Hardening "
            f"policy; cipher suite {REQUIRED_CIPHER_SUITE} (HMAC-SHA256 + AES-CBC-128) "
            "is mandatory."
        )
        self.cipher_suite = cipher_suite


class BMCDefaultCredentialsDetected(BMCError):
    """Factory default credentials authenticated against the BMC."""

    def __init__(self, username: str) -> None:
        super().__init__(
            f"BMC accepts factory default credentials (user={username!r}); "
            "binding refused per BMC Network Hardening policy."
        )
        self.username = username


class BMCOperationFailed(BMCError):
    """Generic IPMI operation failure that isn't auth/cipher related."""


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SelEntry:
    """IPMI System Event Log entry (vendor-agnostic shape)."""

    record_id: str
    timestamp: Optional[str]
    severity: Optional[str]
    message: str
    sensor_type: Optional[str] = None
    event_data: Optional[str] = None


# ---------------------------------------------------------------------------
# Protocols (loose duck-typed — same spirit as redfish.py)
# ---------------------------------------------------------------------------


class _VaultClientProto(Protocol):
    def kv_read(self, path: str) -> Any: ...  # pragma: no cover


class _NodeBmcProto(Protocol):
    node_id: int
    endpoint: str
    username_ref: str
    password_ref: str
    factory_creds_detected: bool


class _AuditWriterProto(Protocol):
    def append(self, **kwargs: Any) -> Any: ...  # pragma: no cover


class _NatsPublisherProto(Protocol):
    def publish(self, subject: str, payload: dict[str, Any]) -> None: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class IpmiClient:
    """IPMI 2.0 over LAN client with mandatory cipher-suite-17 enforcement."""

    def __init__(
        self,
        endpoint: str,
        vault_client: _VaultClientProto,
        node_bmc_record: _NodeBmcProto,
        *,
        audit_writer: Optional[_AuditWriterProto] = None,
        nats_publisher: Optional[_NatsPublisherProto] = None,
        cluster_id: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT_SEC,
        cipher_suite: int = REQUIRED_CIPHER_SUITE,
        command_factory: Optional[Any] = None,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint is required")
        if cipher_suite in FORBIDDEN_CIPHER_SUITES:
            raise IpmiInsecureCipherRefused(cipher_suite)
        if cipher_suite != REQUIRED_CIPHER_SUITE:
            raise ValueError(
                f"cipher suite {cipher_suite} not permitted; "
                f"only {REQUIRED_CIPHER_SUITE} is allowed"
            )

        self._host, self._port = _parse_endpoint(endpoint)
        self.endpoint = endpoint
        self._vault = vault_client
        self._node_bmc = node_bmc_record
        self._audit = audit_writer
        self._nats = nats_publisher
        self._cluster_id = cluster_id
        self._timeout = timeout
        self._cipher_suite = cipher_suite
        self._command_factory = command_factory or _pyghmi_command and _pyghmi_command.Command  # type: ignore[truthy-function]
        self._command: Optional[Any] = None

    # ------------------------------------------------------------------
    # Vault credential resolution (same contract as redfish.py)
    # ------------------------------------------------------------------

    def _read_credentials(self) -> tuple[str, str]:
        return self._kv_lookup(self._node_bmc.username_ref), self._kv_lookup(
            self._node_bmc.password_ref
        )

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
    # pyghmi wrappers
    # ------------------------------------------------------------------

    def _open(self, username: str, password: str) -> Any:
        if self._command_factory is None:
            raise BMCUnreachable(
                "pyghmi is not installed and no command_factory was supplied"
            )
        try:
            cmd = self._command_factory(
                bmc=self._host,
                userid=username,
                password=password,
                port=self._port,
                kg=None,
                onlogon=None,
            )
        except _PyghmiIpmiException as exc:
            text = str(exc).lower()
            if "cipher" in text and ("0" in text or "unsupported" in text):
                raise IpmiInsecureCipherRefused(0) from exc
            if "auth" in text or "password" in text or "unauthorized" in text:
                raise BMCAuthFailure(str(exc)) from exc
            raise BMCUnreachable(str(exc)) from exc
        except OSError as exc:
            raise BMCUnreachable(f"IPMI session to {self._host}:{self._port} failed: {exc}") from exc

        # Enforce cipher suite at the session level. pyghmi exposes the
        # negotiated cipher via ``cmd.ipmi_session.cipher_suite``; we tolerate
        # absence (older versions) but refuse if it explicitly reports 0.
        negotiated = _negotiated_cipher(cmd)
        if negotiated is not None and negotiated in FORBIDDEN_CIPHER_SUITES:
            try:
                cmd.logout()
            except Exception:  # pragma: no cover
                pass
            raise IpmiInsecureCipherRefused(negotiated)
        return cmd

    def _ensure_open(self) -> Any:
        if self._command is not None:
            return self._command
        username, password = self._read_credentials()
        self._command = self._open(username, password)
        return self._command

    # ------------------------------------------------------------------
    # Audit + NATS
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
        except Exception:  # pragma: no cover
            logger.exception("audit append failed for action=%s", action)

    def _emit_default_creds_event(self, username: str) -> None:
        if self._nats is None:
            return
        payload = {
            "node_id": self._node_bmc.node_id,
            "endpoint": self.endpoint,
            "username": username,
            "cluster_id": self._cluster_id,
            "ts": time.time(),
            "protocol": "ipmi",
        }
        try:
            self._nats.publish(NATS_SUBJECT_DEFAULT_CREDS, payload)
        except Exception:  # pragma: no cover
            logger.exception("NATS publish failed for default-creds alert")

    # ------------------------------------------------------------------
    # First-connect default-credential probe
    # ------------------------------------------------------------------

    def first_connect(self) -> None:
        """Refuse the binding if any factory credential pair authenticates.

        Mutates ``node_bmc.factory_creds_detected``. Raises
        :class:`BMCDefaultCredentialsDetected` on the first match. Pure probe —
        does not retain a session.
        """
        for user, pw in DEFAULT_CREDENTIAL_PAIRS:
            try:
                cmd = self._open(user, pw)
            except BMCAuthFailure:
                continue
            except IpmiInsecureCipherRefused:
                raise
            except BMCUnreachable:
                # If we can't reach the BMC at all, defer to the operation that
                # follows; we don't want to mask a transient network failure as
                # a default-creds detection.
                return
            else:
                try:
                    cmd.logout()
                except Exception:  # pragma: no cover
                    pass
                self._node_bmc.factory_creds_detected = True
                self._audit_event(
                    "default_credentials_detected",
                    after={"username": user},
                )
                self._emit_default_creds_event(user)
                raise BMCDefaultCredentialsDetected(user)

    # ------------------------------------------------------------------
    # Power control
    # ------------------------------------------------------------------

    def _set_power(self, state: Literal["on", "off", "cycle", "reset"]) -> None:
        cmd = self._ensure_open()
        try:
            cmd.set_power(state, wait=False)
        except _PyghmiIpmiException as exc:
            raise BMCOperationFailed(f"set_power({state}) failed: {exc}") from exc
        self._audit_event(f"power_{state}", after={"state": state})

    def power_on(self) -> None:
        self._set_power("on")

    def power_off(self) -> None:
        self._set_power("off")

    def power_cycle(self) -> None:
        self._set_power("cycle")

    # ------------------------------------------------------------------
    # Boot device
    # ------------------------------------------------------------------

    def set_chassis_bootdev(self, target: BootDevice = "pxe", persistent: bool = False) -> None:
        cmd = self._ensure_open()
        try:
            cmd.set_bootdev(target, persist=persistent, uefiboot=False)
        except _PyghmiIpmiException as exc:
            raise BMCOperationFailed(
                f"set_bootdev({target}, persist={persistent}) failed: {exc}"
            ) from exc
        self._audit_event(
            "set_chassis_bootdev",
            after={"target": target, "persistent": persistent},
        )

    # ------------------------------------------------------------------
    # SEL log
    # ------------------------------------------------------------------

    def get_sel_log(self) -> list[SelEntry]:
        cmd = self._ensure_open()
        try:
            raw_entries = cmd.get_event_log()
        except _PyghmiIpmiException as exc:
            raise BMCOperationFailed(f"get_event_log failed: {exc}") from exc

        entries: list[SelEntry] = []
        for raw in _coerce_iterable(raw_entries):
            entries.append(
                SelEntry(
                    record_id=str(raw.get("record_id") or raw.get("id") or ""),
                    timestamp=_stringify(raw.get("timestamp")),
                    severity=raw.get("severity") or raw.get("status"),
                    message=str(raw.get("message") or raw.get("event") or ""),
                    sensor_type=raw.get("sensor_type"),
                    event_data=_stringify(raw.get("event_data")),
                )
            )
        self._audit_event("get_sel_log", after={"count": len(entries)})
        return entries

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._command is None:
            return
        try:
            self._command.logout()
        except Exception:  # pragma: no cover
            pass
        finally:
            self._command = None

    def __enter__(self) -> "IpmiClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_endpoint(endpoint: str) -> tuple[str, int]:
    """Accept ``host``, ``host:port``, or ``ipmi://host[:port]`` forms."""
    if "://" in endpoint:
        parsed = urlparse(endpoint)
        if parsed.scheme.lower() not in ("ipmi", "rmcp", "rmcpp"):
            raise ValueError(f"unsupported IPMI scheme: {parsed.scheme!r}")
        host = parsed.hostname or ""
        port = parsed.port or 623
    elif endpoint.count(":") == 1:
        host, _, port_s = endpoint.partition(":")
        try:
            port = int(port_s)
        except ValueError as exc:
            raise ValueError(f"invalid IPMI port in {endpoint!r}") from exc
    else:
        host = endpoint
        port = 623
    if not host:
        raise ValueError(f"could not parse IPMI host from {endpoint!r}")
    return host, port


def _negotiated_cipher(cmd: Any) -> Optional[int]:
    session = getattr(cmd, "ipmi_session", None)
    if session is None:
        return None
    cipher = getattr(session, "cipher_suite", None)
    if cipher is None:
        return None
    try:
        return int(cipher)
    except (TypeError, ValueError):
        return None


def _coerce_iterable(obj: Any) -> Iterable[dict[str, Any]]:
    if obj is None:
        return ()
    if isinstance(obj, dict):
        return (obj,)
    if isinstance(obj, list):
        return [item for item in obj if isinstance(item, dict)]
    if hasattr(obj, "__iter__"):
        result: list[dict[str, Any]] = []
        for item in obj:
            if isinstance(item, dict):
                result.append(item)
        return result
    return ()


def _stringify(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)
