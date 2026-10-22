"""NATS JetStream client for the Gough event stream.

Implements the standard Gough event envelope and JetStream durable consumers
per the spec section "Developer & Operator Experience -> Event Stream (NATS)".

Streams:
    gough_events  - interest retention, 24h max age (primary event stream)
    gough_audit   - file retention, 30d max age (compliance mirror)

Authentication:
    NATS NKEYS - credentials pulled from Vault at
    secret/gough/<cluster-id>/nats/credentials, expected to contain a
    `creds` field with NATS NKEY credentials in the standard
    "-----BEGIN NATS USER NKEY-----" format.

Event envelope (every published payload is wrapped in this structure):
    {
      "version": "1",
      "event": "<event-name>",
      "id": "<ULID>",
      "ts": "<RFC3339>",
      "cluster_id": "...",
      "tenant_id": "...",
      "actor_sub": "...",
      "trace_id": "...",
      "request_id": "...",
      "data": { ... }
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import nats
from nats.aio.client import Client as NatsAioClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription as NatsSubscription
from nats.errors import ConnectionClosedError, NoServersError, TimeoutError as NatsTimeoutError
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig, DeliverPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError as JsNotFoundError

from .vault import VaultClient, VaultError

logger = logging.getLogger(__name__)

# ULID alphabet - Crockford's base32, no I/L/O/U
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Standard stream names
EVENTS_STREAM = "gough_events"
AUDIT_STREAM = "gough_audit"

# Standard event-stream subject prefix (allows wildcard subscription)
EVENTS_SUBJECT_PREFIX = "gough.events"
# Audit mirror subject prefix - copied via subject mirror config
AUDIT_SUBJECT_PREFIX = "gough.audit"


class NatsClientError(Exception):
    """Base class for NATS client errors."""


class NatsCredentialError(NatsClientError):
    """Vault did not return usable NATS credentials."""


class NatsPublishError(NatsClientError):
    """Publish to JetStream failed."""


class NatsConnectionError(NatsClientError):
    """Could not connect to any NATS server."""


def _generate_ulid() -> str:
    """Generate a Crockford base32 ULID (timestamp + 80 random bits)."""
    ts_ms = int(time.time() * 1000)
    # 48-bit timestamp (10 base32 chars) + 80-bit random (16 base32 chars)
    ts_chars = ""
    for _ in range(10):
        ts_chars = _ULID_ALPHABET[ts_ms & 0x1F] + ts_chars
        ts_ms >>= 5
    rand_int = int.from_bytes(secrets.token_bytes(10), "big")
    rand_chars = ""
    for _ in range(16):
        rand_chars = _ULID_ALPHABET[rand_int & 0x1F] + rand_chars
        rand_int >>= 5
    return ts_chars + rand_chars


def _rfc3339_now() -> str:
    """Return current time in strict RFC 3339 / ISO 8601 with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


@dataclass(slots=True)
class EventEnvelope:
    """Gough standard event envelope."""

    version: str
    event: str
    id: str
    ts: str
    cluster_id: str
    tenant_id: str
    actor_sub: str
    trace_id: str
    request_id: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        """Serialize to canonical JSON bytes (UTF-8)."""
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=False).encode(
            "utf-8"
        )


@dataclass(slots=True)
class PublishResult:
    """Result of a JetStream publish."""

    stream: str
    sequence: int
    duplicate: bool


@dataclass(slots=True)
class Subscription:
    """Wrapped JetStream durable subscription."""

    subject: str
    durable_name: str
    _nats_subscription: NatsSubscription

    async def unsubscribe(self) -> None:
        """Unsubscribe and stop receiving messages."""
        await self._nats_subscription.unsubscribe()


class NatsClient:
    """Async NATS JetStream publisher/consumer for Gough events.

    Reads credentials from Vault, authenticates via NATS NKEYS, publishes to
    the `gough_events` interest-retention stream, and mirrors compliance
    events to `gough_audit` for 30 days.
    """

    def __init__(
        self,
        server_urls: list[str],
        vault_client: VaultClient,
        cluster_id: Optional[str] = None,
        tenant_id_default: str = "",
        events_max_age_seconds: int = 24 * 60 * 60,
        audit_max_age_seconds: int = 30 * 24 * 60 * 60,
        connect_timeout_seconds: float = 5.0,
        reconnect_time_wait_seconds: float = 2.0,
        max_reconnect_attempts: int = -1,
    ) -> None:
        """Initialize the NATS client.

        Args:
            server_urls: One or more nats://host:4222 URLs.
            vault_client: VaultClient to pull NKEY creds from.
            cluster_id: Cluster id. If None, reads GOUGH_CLUSTER_ID env.
            tenant_id_default: Default tenant id used in envelope when caller
                does not provide one (single-tenant deployments).
            events_max_age_seconds: Max age for the events stream (default 24h).
            audit_max_age_seconds: Max age for the audit stream (default 30d).
            connect_timeout_seconds: Per-connect timeout.
            reconnect_time_wait_seconds: Pause between reconnect attempts.
            max_reconnect_attempts: -1 = retry forever.
        """
        if not server_urls:
            raise ValueError("server_urls must contain at least one entry")
        self.server_urls: list[str] = list(server_urls)
        self.vault: VaultClient = vault_client
        self.cluster_id: str = cluster_id or os.getenv("GOUGH_CLUSTER_ID", "default")
        self.tenant_id_default: str = tenant_id_default
        self.events_max_age_seconds: int = events_max_age_seconds
        self.audit_max_age_seconds: int = audit_max_age_seconds
        self._connect_timeout: float = connect_timeout_seconds
        self._reconnect_wait: float = reconnect_time_wait_seconds
        self._max_reconnect: int = max_reconnect_attempts

        self._nc: Optional[NatsAioClient] = None
        self._js: Optional[JetStreamContext] = None
        self._lock = asyncio.Lock()

    # ---- credential handling -------------------------------------------------

    def _vault_creds_path(self) -> str:
        return f"gough/{self.cluster_id}/nats/credentials"

    def _load_creds_from_vault(self) -> str:
        """Pull NATS NKEY creds string from Vault.

        Returns:
            The NATS credentials string in the format produced by
            ``nsc generate creds``.

        Raises:
            NatsCredentialError: If Vault does not return usable creds.
        """
        try:
            resp = self.vault.kv_read(self._vault_creds_path())
        except VaultError as exc:
            raise NatsCredentialError(
                f"Vault read failed for {self._vault_creds_path()}: {exc}"
            ) from exc
        creds = resp.data.get("creds") if resp.data else None
        if not creds or not isinstance(creds, str):
            raise NatsCredentialError(
                f"No `creds` key found at vault path {self._vault_creds_path()}"
            )
        return creds

    # ---- connect / close -----------------------------------------------------

    async def connect(self) -> None:
        """Connect to NATS, authenticate, and ensure JetStream streams exist."""
        async with self._lock:
            if self._nc is not None and self._nc.is_connected:
                return
            creds = self._load_creds_from_vault()

            # Write creds to a private temp file - nats-py wants a file path.
            import tempfile

            creds_fd, creds_path = tempfile.mkstemp(prefix="gough-nats-", suffix=".creds")
            os.close(creds_fd)
            os.chmod(creds_path, 0o600)
            with open(creds_path, "w", encoding="utf-8") as fh:
                fh.write(creds)
            self._creds_path = creds_path  # type: ignore[attr-defined]

            try:
                self._nc = await nats.connect(
                    servers=self.server_urls,
                    user_credentials=creds_path,
                    connect_timeout=self._connect_timeout,
                    reconnect_time_wait=self._reconnect_wait,
                    max_reconnect_attempts=self._max_reconnect,
                    name=f"gough-{self.cluster_id}",
                )
            except NoServersError as exc:
                raise NatsConnectionError(
                    f"No NATS servers reachable from {self.server_urls}"
                ) from exc

            self._js = self._nc.jetstream()
            await self._ensure_streams()
            logger.info(
                "NATS connected cluster_id=%s servers=%s", self.cluster_id, self.server_urls
            )

    async def _ensure_streams(self) -> None:
        """Create or update the gough_events and gough_audit streams."""
        assert self._js is not None
        events_cfg = StreamConfig(
            name=EVENTS_STREAM,
            subjects=[f"{EVENTS_SUBJECT_PREFIX}.>"],
            retention=RetentionPolicy.INTEREST,
            max_age=self.events_max_age_seconds * 1_000_000_000,  # ns
            storage=StorageType.FILE,
        )
        audit_cfg = StreamConfig(
            name=AUDIT_STREAM,
            subjects=[f"{AUDIT_SUBJECT_PREFIX}.>"],
            retention=RetentionPolicy.LIMITS,
            max_age=self.audit_max_age_seconds * 1_000_000_000,
            storage=StorageType.FILE,
        )
        for cfg in (events_cfg, audit_cfg):
            try:
                await self._js.update_stream(config=cfg)
            except JsNotFoundError:
                await self._js.add_stream(config=cfg)

    async def close(self) -> None:
        """Drain and close the NATS connection."""
        async with self._lock:
            if self._nc is not None and self._nc.is_connected:
                try:
                    await self._nc.drain()
                except (ConnectionClosedError, NatsTimeoutError):
                    pass
            self._nc = None
            self._js = None
        creds_path = getattr(self, "_creds_path", None)
        if creds_path and os.path.exists(creds_path):
            try:
                os.remove(creds_path)
            except OSError:
                pass

    # ---- publish -------------------------------------------------------------

    def _build_envelope(
        self,
        event: str,
        data: dict[str, Any],
        *,
        tenant_id: Optional[str] = None,
        actor_sub: str = "",
        trace_id: str = "",
        request_id: str = "",
    ) -> EventEnvelope:
        return EventEnvelope(
            version="1",
            event=event,
            id=_generate_ulid(),
            ts=_rfc3339_now(),
            cluster_id=self.cluster_id,
            tenant_id=tenant_id if tenant_id is not None else self.tenant_id_default,
            actor_sub=actor_sub,
            trace_id=trace_id,
            request_id=request_id,
            data=data,
        )

    async def publish(
        self,
        subject: str,
        payload: dict[str, Any],
        headers: Optional[dict[str, str]] = None,
        *,
        event_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        actor_sub: str = "",
        trace_id: str = "",
        request_id: str = "",
        audit: bool = False,
    ) -> str:
        """Publish a Gough event to JetStream.

        Args:
            subject: Subject under ``gough.events.`` (or full subject if
                already prefixed). If audit=True, also published to
                ``gough.audit.<rest>``.
            payload: Caller's domain payload - placed in envelope.data.
            headers: Optional NATS headers. ``gough-event-id`` and
                ``gough-event-version`` are automatically added.
            event_name: Override envelope ``event`` field (defaults to subject).
            tenant_id: Tenant id; falls back to client default.
            actor_sub: Authenticated subject performing the action.
            trace_id: W3C trace id for distributed tracing.
            request_id: Per-request correlation id.
            audit: When True, also mirror to gough_audit stream (30-day
                retention) for compliance lanes.

        Returns:
            The JetStream sequence id (as string) of the primary publish.

        Raises:
            NatsPublishError: Publish failed.
            NatsConnectionError: Not connected.
        """
        if self._js is None or self._nc is None or not self._nc.is_connected:
            raise NatsConnectionError("NATS client is not connected; call connect() first")

        if not subject.startswith(EVENTS_SUBJECT_PREFIX + "."):
            full_subject = f"{EVENTS_SUBJECT_PREFIX}.{subject}"
        else:
            full_subject = subject

        envelope = self._build_envelope(
            event=event_name or subject,
            data=payload,
            tenant_id=tenant_id,
            actor_sub=actor_sub,
            trace_id=trace_id,
            request_id=request_id,
        )
        body = envelope.to_bytes()
        full_headers: dict[str, str] = {
            "gough-event-id": envelope.id,
            "gough-event-version": envelope.version,
            "gough-cluster-id": envelope.cluster_id,
            "gough-tenant-id": envelope.tenant_id,
        }
        if headers:
            full_headers.update(headers)

        try:
            ack = await self._js.publish(
                subject=full_subject, payload=body, headers=full_headers
            )
        except (ConnectionClosedError, NatsTimeoutError, NoServersError) as exc:
            raise NatsPublishError(f"JetStream publish failed: {exc}") from exc

        primary_seq = ack.seq

        if audit:
            audit_subject = full_subject.replace(
                EVENTS_SUBJECT_PREFIX, AUDIT_SUBJECT_PREFIX, 1
            )
            try:
                await self._js.publish(
                    subject=audit_subject, payload=body, headers=full_headers
                )
            except (ConnectionClosedError, NatsTimeoutError, NoServersError) as exc:
                # Audit failure is logged but does not roll back the primary
                # publish - the audit lane is a best-effort compliance mirror.
                logger.error("Audit mirror publish failed for %s: %s", audit_subject, exc)

        logger.info(
            "Published event subject=%s seq=%d id=%s audit=%s",
            full_subject,
            primary_seq,
            envelope.id,
            audit,
        )
        return str(primary_seq)

    # ---- subscribe -----------------------------------------------------------

    async def subscribe(
        self,
        subject: str,
        durable_name: str,
        handler: Callable[[Msg], Awaitable[None]],
        *,
        queue: Optional[str] = None,
        manual_ack: bool = True,
        deliver_policy: DeliverPolicy = DeliverPolicy.ALL,
        max_deliver: int = 5,
        ack_wait_seconds: float = 30.0,
    ) -> Subscription:
        """Create a JetStream durable consumer with at-least-once delivery.

        Sequence-id-based replay happens automatically on reconnect because
        the consumer is durable.

        Args:
            subject: Subject pattern, e.g. ``gough.events.nodes.*``.
            durable_name: Durable consumer name (must be stable across
                restarts to keep position).
            handler: Async callable taking a NATS Msg.
            queue: Optional queue group for load-balanced delivery.
            manual_ack: When True, the handler is responsible for calling
                ``msg.ack()`` (recommended for at-least-once semantics).
            deliver_policy: ALL (replay everything) or NEW.
            max_deliver: Max redelivery attempts before message is sent to
                the dead-letter (consumer) tier.
            ack_wait_seconds: How long to wait for ack before redelivering.

        Returns:
            Subscription wrapper - call ``unsubscribe()`` to stop.
        """
        if self._js is None or self._nc is None or not self._nc.is_connected:
            raise NatsConnectionError("NATS client is not connected; call connect() first")

        if not subject.startswith(EVENTS_SUBJECT_PREFIX + ".") and not subject.startswith(
            AUDIT_SUBJECT_PREFIX + "."
        ):
            full_subject = f"{EVENTS_SUBJECT_PREFIX}.{subject}"
        else:
            full_subject = subject

        config = ConsumerConfig(
            durable_name=durable_name,
            deliver_policy=deliver_policy,
            max_deliver=max_deliver,
            ack_wait=int(ack_wait_seconds * 1_000_000_000),
        )

        async def _wrap(msg: Msg) -> None:
            try:
                await handler(msg)
                if not manual_ack:
                    await msg.ack()
            except Exception:  # noqa: BLE001 - log + nak so JetStream can redeliver
                logger.exception(
                    "Handler raised on subject=%s seq=%s; sending NAK",
                    msg.subject,
                    getattr(getattr(msg, "metadata", None), "sequence", "?"),
                )
                try:
                    await msg.nak()
                except Exception:  # noqa: BLE001
                    pass

        sub = await self._js.subscribe(
            subject=full_subject,
            durable=durable_name,
            cb=_wrap,
            queue=queue,
            manual_ack=manual_ack,
            config=config,
        )
        logger.info(
            "Subscribed subject=%s durable=%s queue=%s", full_subject, durable_name, queue
        )
        return Subscription(
            subject=full_subject, durable_name=durable_name, _nats_subscription=sub
        )

    # ---- introspection -------------------------------------------------------

    async def stream_info(self, name: str) -> dict[str, Any]:
        """Return current stream info for ``name`` (events or audit)."""
        if self._js is None:
            raise NatsConnectionError("Not connected")
        info = await self._js.stream_info(name)
        return {
            "name": info.config.name,
            "subjects": list(info.config.subjects or []),
            "retention": info.config.retention.value
            if hasattr(info.config.retention, "value")
            else str(info.config.retention),
            "max_age_ns": info.config.max_age,
            "messages": info.state.messages,
            "bytes": info.state.bytes,
            "first_seq": info.state.first_seq,
            "last_seq": info.state.last_seq,
        }

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected
