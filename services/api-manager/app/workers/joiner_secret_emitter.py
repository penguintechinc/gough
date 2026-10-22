"""Joiner secret emitter — post-deploy extractor flow.

Implements the Emit -> Store flow from the Gough spec
(Biome Model -> Joiner / Enrollment Secrets -> Emit -> Store Flow):

1. Emitter biome reaches ``ready`` on its target node.
2. api-manager opens a control-tunnel session to the node's agent.
3. For each ``joiner_emit_spec.extractors[]``:
   - Run extractor inside the LXD instance via control tunnel
   - Stream output back over mTLS as opaque bytes (never logged)
   - Generate per-row DEK, AES-256-GCM encrypt, wrap DEK via Vault transit
   - Insert ``joiner_secrets`` row inside a SERIALIZABLE Postgres tx
   - Append audit-chain row with action ``joiner.secret.emit``
   - Memory-zero raw bytes + DEK after persist
4. Publish NATS event ``gough.joiner.<cluster-id>.emitted`` with metadata only.

Four-layer protection (KEK, per-row DEK envelope, Postgres TDE row-encrypt
at rest, RLS) is enforced collectively by this module (Layers 1+2),
infrastructure (Layer 3), and migrations (Layer 4).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Protocol

from pydantic import BaseModel
from penguin_dal import DB, Row

from app.clients.vault import VaultClient
from app.db.rls import CROSS_TENANT_SENTINEL, get_current_tenant, set_current_tenant
from app.models_m1 import JoinerSecret
from app.security.audit_chain import (
    ZERO_HASH,
    compute_chain_hash,
    generate_uuidv7,
)
from app.security.joiner_envelope import encrypt_envelope, zero_bytes


VAULT_KEK_NAME = "gough-joiner-dek-wrap"

SOURCE_EXEC = "exec"
SOURCE_FILE = "file"
SOURCE_HTTP = "http_api"
SOURCE_LXD = "lxd_config"
VALID_SOURCES = frozenset({SOURCE_EXEC, SOURCE_FILE, SOURCE_HTTP, SOURCE_LXD})
VALID_SCOPES = frozenset({"cluster", "node", "tenant"})


def _mask(value: Any) -> str:
    """Render a non-sensitive reference for logs (length only, no bytes)."""
    if value is None:
        return "<none>"
    if isinstance(value, (bytes, bytearray)):
        return f"<bytes len={len(value)}>"
    if isinstance(value, str):
        return f"<str len={len(value)}>"
    return f"<{type(value).__name__}>"


class ExtractorSpecError(ValueError):
    """Raised when a joiner_emit_spec extractor is malformed or unsupported."""


class ControlTunnelExtractionError(RuntimeError):
    """Raised when a control-tunnel extractor invocation fails."""


class EmitterStateError(RuntimeError):
    """Raised when the emitter is invoked against an invalid biome-instance state."""


class ControlTunnelClient(Protocol):
    """Duck-typed interface for the per-node control tunnel.

    A real implementation establishes an mTLS gRPC session to the node's
    discovery-agent. For unit tests this is mocked.
    """

    def run_command(
        self,
        node_id: int,
        lxd_instance: str,
        command: list[str],
        timeout_seconds: int = 60,
    ) -> bytes: ...

    def read_file(
        self,
        node_id: int,
        lxd_instance: str,
        path: str,
        max_bytes: int = 65536,
    ) -> bytes: ...

    def http_api(
        self,
        node_id: int,
        lxd_instance: str,
        url: str,
        method: str = "GET",
        headers: Optional[dict[str, str]] = None,
        body: Optional[bytes] = None,
        timeout_seconds: int = 30,
    ) -> bytes: ...

    def lxd_config(
        self,
        node_id: int,
        lxd_instance: str,
        config_key: str,
    ) -> bytes: ...


class NatsPublisher(Protocol):
    """Duck-typed interface for the NATS publisher used by the emitter."""

    def publish(self, subject: str, payload: bytes) -> None: ...


@dataclass(slots=True)
class JoinerSecretMetadata:
    """Non-sensitive metadata returned to callers after a successful emit.

    Carries no ciphertext, no DEK, no plaintext bytes.
    """

    joiner_secret_id: str
    cluster_id: str
    biome_kind: str
    extractor_name: str
    scope: str
    rotation_class: Optional[str]
    ttl_seconds: Optional[int]
    expires_at: Optional[datetime]
    audit_event_id: str
    emitter_biome_id: int
    emitter_node_id: int


@dataclass(slots=True)
class ExtractedMaterial:
    """Plaintext material returned by a rotation provider callback."""

    extractor_name: str
    plaintext: bytes
    ttl_seconds: int = 0
    rotation_class: Optional[str] = None


@dataclass(slots=True)
class EmitResult:
    """Result of a successful joiner secret emission."""

    joiner_secret_id: object  # uuid.UUID
    audit_event_id: object    # uuid.UUID
    expires_at: object        # datetime


def persist_extracted_material(
    tx: Any,
    *,
    cluster_id: str,
    tenant_id: str,
    biome_kind: str,
    emitter_biome_id: int,
    emitter_node_id: Optional[int],
    extractor_name: str,
    scope: str,
    vault_client: VaultClient,
    material: ExtractedMaterial,
    actor_sub: str,
    actor_scope: list[str],
    action: str,
    resource_kind: str = "joiner_secret",
    vault_kek_name: str = VAULT_KEK_NAME,
    request_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> tuple[str, str, Optional[datetime]]:
    """Envelope-encrypt already-extracted material and persist it atomically.

    Callers that already hold plaintext (e.g. ``app.api.joiner_secrets
    .rotate_joiner_secret``, which gets it from
    ``JOINER_ROTATE_MATERIAL_PROVIDER`` rather than a fresh control-tunnel
    extraction) don't go through ``JoinerSecretEmitter.emit()`` -- that
    method's whole first half (load biome_instance context, validate
    joiner_emit_spec, run the extractor over the control tunnel) doesn't
    apply, and it returns ``list[JoinerSecretMetadata]`` from a
    ``biome_instance_id``, not a single result for an already-known secret.
    This function is the second half only (encrypt + insert + audit),
    extracted so both call sites share one persistence implementation.

    Takes a penguin-dal ``Tx`` (``DB.transaction()``), not a SQLAlchemy
    ``Session`` -- ``Tx`` only exposes raw driver-native-paramstyle
    ``executesql()``, so the audit-chain row is appended here via the pure
    hashing/id helpers from ``app.security.audit_chain`` (``generate_uuidv7``,
    ``compute_chain_hash``, ``canonicalize_record``, ``ZERO_HASH`` -- none of
    which need a ``db_session``) rather than ``AuditEventWriter``, which
    does and is therefore incompatible with a ``Tx``. This mirrors
    ``JoinerSecretEmitter._persist()``'s raw-SQL insert shape one-for-one,
    just re-pointed at ``Tx.executesql`` instead of
    ``Session.execute(text(...))``.

    Args:
        tx: Pinned-connection transaction handle from ``db.transaction()``.
        cluster_id: Cluster UUID (string form -- see the VARCHAR(36) note in
            ``app.api.joiner_secrets``).
        tenant_id: Tenant id owning the new secret.
        biome_kind: Biome kind label carried onto the audit record.
        emitter_biome_id: FK to ``biomes.id``.
        emitter_node_id: FK to ``nodes.id`` (nullable).
        extractor_name: Extractor name carried onto the new row + audit record.
        scope: One of ``VALID_SCOPES``.
        vault_client: Vault client used to wrap the per-row DEK.
        material: Already-extracted plaintext + rotation metadata.
        actor_sub: Audit actor subject.
        actor_scope: Audit actor scopes.
        action: Audit action string (e.g. ``"joiner.secret.rotate"``).
        resource_kind: Audit resource_kind (defaults to ``"joiner_secret"``).
        vault_kek_name: Vault transit key name for DEK wrapping.
        request_id: Optional request id carried onto the audit record.
        reason: Optional operator-supplied reason (e.g. rotate's required
            ``reason`` field), carried onto the audit record's after_json
            when given.

    Returns:
        ``(joiner_secret_id, audit_event_id, expires_at)``, all as the same
        types ``EmitResult`` carries (str ids, ``Optional[datetime]``).

    Raises:
        ExtractorSpecError: If ``scope`` is not in ``VALID_SCOPES``.
    """
    if scope not in VALID_SCOPES:
        raise ExtractorSpecError(f"Invalid scope: {scope!r}")

    envelope = encrypt_envelope(
        plaintext=material.plaintext,
        vault_client=vault_client,
        vault_kek_name=vault_kek_name,
    )

    joiner_secret_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    expires_at: Optional[datetime] = None
    if material.ttl_seconds and material.ttl_seconds > 0:
        expires_at = now + timedelta(seconds=material.ttl_seconds)

    prev_rows = tx.executesql(
        "SELECT hash FROM audit_events ORDER BY ts DESC, id DESC LIMIT 1"
    )
    prev_hash_raw = prev_rows[0][0] if prev_rows else None
    prev_hash: bytes = (
        bytes(prev_hash_raw)
        if isinstance(prev_hash_raw, (bytes, bytearray)) and len(prev_hash_raw) == 32
        else ZERO_HASH
    )

    event_id = generate_uuidv7()
    ts_utc = datetime.now(timezone.utc)
    after_json: dict[str, Any] = {
        "biome_kind": biome_kind,
        "extractor_name": extractor_name,
        "scope": scope,
        "rotation_class": material.rotation_class,
        "ttl_seconds": material.ttl_seconds if material.ttl_seconds else None,
        "vault_kek_name": vault_kek_name,
        "emitter_biome_id": emitter_biome_id,
        "emitter_node_id": emitter_node_id,
    }
    if reason is not None:
        after_json["reason"] = reason
    record_fields = {
        "id": str(event_id),
        "ts": ts_utc.isoformat(),
        "cluster_id": cluster_id,
        "tenant_id": tenant_id,
        "actor_sub": actor_sub,
        "actor_scope": actor_scope,
        "action": action,
        "resource_kind": resource_kind,
        "resource_id": str(joiner_secret_id),
        "before_json": None,
        "after_json": after_json,
        "request_id": request_id,
        "source_ip": None,
        "user_agent": None,
    }
    chain_hash = compute_chain_hash(prev_hash, record_fields)

    tx.executesql(
        """
        INSERT INTO audit_events
        (id, ts, cluster_id, tenant_id, actor_sub, actor_scope, action,
         resource_kind, resource_id, before_json, after_json, request_id,
         source_ip, user_agent, prev_hash, hash, signature)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(event_id),
            ts_utc,
            cluster_id,
            tenant_id,
            actor_sub,
            json.dumps(actor_scope),
            action,
            resource_kind,
            str(joiner_secret_id),
            None,
            json.dumps(after_json),
            request_id,
            None,
            None,
            prev_hash,
            chain_hash,
            None,
        ),
    )

    tx.executesql(
        """
        INSERT INTO joiner_secrets
          (id, cluster_id, tenant_id, biome_kind, emitter_biome_id,
           emitter_node_id, extractor_name, scope, ciphertext, iv,
           auth_tag, dek_wrapped, vault_kek_name, ttl_seconds,
           expires_at, rotation_class, created_at, audit_event_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(joiner_secret_id),
            cluster_id,
            tenant_id,
            biome_kind,
            emitter_biome_id,
            emitter_node_id,
            extractor_name,
            scope,
            envelope.ciphertext,
            envelope.iv,
            envelope.auth_tag,
            envelope.dek_wrapped,
            envelope.vault_kek_name,
            material.ttl_seconds if material.ttl_seconds else None,
            expires_at,
            material.rotation_class,
            now,
            str(event_id),
        ),
    )

    return str(joiner_secret_id), str(event_id), expires_at


class _ExtractorSpec(BaseModel):
    """Validated extractor entry from joiner_emit_spec.extractors[]."""

    name: str
    source: str
    command: Optional[list[str]] = None
    path: Optional[str] = None
    url: Optional[str] = None
    method: Optional[str] = None
    config_key: Optional[str] = None
    ttl_seconds: int = 0
    rotation: Optional[str] = None
    scope: str = "cluster"


class JoinerSecretEmitter:
    """Emits encrypted joiner secrets when a biome_instance reaches ready.

    Each ``emit(biome_instance_id)`` call:
      * Loads the emitter biome + node + assignment.
      * Validates the assignment's status is ``ready``.
      * Iterates ``joiner_emit_spec.extractors[]``, running the extractor
        inside the LXD instance via the injected control tunnel.
      * Envelope-encrypts each output, wraps the DEK via Vault transit,
        inserts a ``joiner_secrets`` row inside a SERIALIZABLE transaction,
        and appends a hash-chained audit-event row.
      * Publishes a metadata-only NATS notification per extractor.
      * Memory-zeros raw bytes after persist.

    The emitter never logs ciphertext, DEK, or raw extractor output.
    """

    def __init__(
        self,
        db: DB,
        vault_client: VaultClient,
        control_tunnel_client: ControlTunnelClient,
        nats_client: Optional[NatsPublisher] = None,
        cluster_id: Optional[str] = None,
        actor_sub: str = "system:api-manager",
        vault_kek_name: str = VAULT_KEK_NAME,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Initialize the emitter.

        Args:
            db: penguin-dal ``DB`` instance (the RLS-wired ``app.config["db"]``
                pool -- see ``app.models.get_db``). ``emit()`` pushes the RLS
                cross-tenant sentinel for its own duration (see ``emit()``
                docstring), so callers do not need to set a tenant GUC
                themselves before constructing this emitter.
            vault_client: Vault client used to wrap per-row DEKs.
            control_tunnel_client: mTLS control-tunnel client used to run
                extractors inside the biome's LXD instance.
            nats_client: Optional NATS publisher for ``gough.joiner.*`` events.
            cluster_id: Cluster UUID to scope joiner secrets to. If ``None``,
                the emitter resolves it lazily per-node.
            actor_sub: Subject for the audit chain (``system:api-manager``).
            vault_kek_name: Vault transit key name used to wrap each DEK.
            logger: Logger instance; defaults to module logger.
        """
        self.db = db
        self.vault_client = vault_client
        self.control_tunnel = control_tunnel_client
        self.nats = nats_client
        self._cluster_id = cluster_id
        self.actor_sub = actor_sub
        self.vault_kek_name = vault_kek_name
        self.logger = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #

    def emit(self, biome_instance_id: int) -> list[JoinerSecretMetadata]:
        """Run the post-deploy extractor flow for one biome_instance.

        Args:
            biome_instance_id: Primary key of the ``node_egg_assignments``
                row whose status has just transitioned to ``ready``.

        Returns:
            List of ``JoinerSecretMetadata`` (one per extractor, metadata only).

        Raises:
            EmitterStateError: When the assignment, biome, or node cannot be
                resolved, or the assignment is not in ``ready`` state.
            ExtractorSpecError: When ``joiner_emit_spec`` is malformed.
            ControlTunnelExtractionError: When a control-tunnel call fails.

        RLS note: this runs outside Quart's HTTP request pipeline (invoked
        by a background trigger on a ``biome_instance_id`` alone -- no
        request, no tenant bridge, so ``app.db.rls``'s tenant
        ContextVar is never set by anything upstream of this call). Worse,
        resolving *which* tenant owns this row is the first thing this
        method has to do (``_load_context`` looks up the assignment/biome/
        node by PK, before any tenant is known), so there is no tenant to
        scope to even if a caller wanted to. This pushes the RLS
        cross-tenant sentinel (``app.db.rls.CROSS_TENANT_SENTINEL``) for
        the duration of the whole call -- both the context-resolution reads
        and the eventual write, restoring whatever was previously set (if
        anything) on this task's context before returning. The write
        itself still uses the row's own real ``tenant_id`` for every
        inserted column; the sentinel only affects which existing rows are
        *visible*, not what gets written -- the ``tenant_isolation`` RLS
        policy in the baseline migration has no separate ``WITH CHECK``
        clause, so it reuses ``USING`` for inserts too, and the sentinel
        value satisfies that check for any tenant_id being written.
        """
        previous_tenant = get_current_tenant()
        set_current_tenant(CROSS_TENANT_SENTINEL)
        try:
            assignment, biome, node = self._load_context(biome_instance_id)
            cluster_id = self._resolve_cluster_id(node)
            spec_extractors = self._validate_spec(biome)

            if not spec_extractors:
                self.logger.info(
                    "Biome has emits_joiner_secrets=true but no extractors; skipping",
                    extra={"biome_id": biome.id, "biome_instance_id": biome_instance_id},
                )
                return []

            lxd_instance = self._derive_lxd_instance(node, biome, assignment)

            results: list[JoinerSecretMetadata] = []
            for extractor in spec_extractors:
                metadata = self._emit_one(
                    cluster_id=cluster_id,
                    biome=biome,
                    node=node,
                    lxd_instance=lxd_instance,
                    extractor=extractor,
                )
                results.append(metadata)

            return results
        finally:
            set_current_tenant(previous_tenant)

    # ------------------------------------------------------------------ #
    # Per-extractor pipeline                                             #
    # ------------------------------------------------------------------ #

    def _emit_one(
        self,
        *,
        cluster_id: str,
        biome: Row,
        node: Row,
        lxd_instance: str,
        extractor: _ExtractorSpec,
    ) -> JoinerSecretMetadata:
        """Run extractor + persist + audit + notify for a single extractor."""
        self.logger.info(
            "Joiner secret emit starting",
            extra={
                "cluster_id": cluster_id,
                "biome_kind": biome.biome_kind,
                "extractor": extractor.name,
                "scope": extractor.scope,
                "node_id": node.id,
            },
        )

        # Step 1: run extractor inside the biome's LXD instance.
        raw_bytes = self._extract_secret(
            node_id=node.id,
            lxd_instance=lxd_instance,
            extractor_spec=extractor,
        )
        if not raw_bytes:
            raise ControlTunnelExtractionError(
                f"Extractor {extractor.name!r} returned empty output"
            )

        # Step 2-4: envelope-encrypt + persist + audit (within SERIALIZABLE tx).
        try:
            joiner_secret_id, audit_event_id, expires_at = self._persist(
                extractor_name=extractor.name,
                scope=extractor.scope,
                raw_bytes=raw_bytes,
                ttl_seconds=extractor.ttl_seconds,
                rotation_class=extractor.rotation,
                emitter_biome_id=biome.id,
                emitter_node_id=node.id,
                biome_kind=biome.biome_kind,
                cluster_id=cluster_id,
                tenant_id=biome.tenant_id,
            )
        finally:
            # Step 5: zero raw bytes regardless of persist outcome.
            try:
                zero_bytes(raw_bytes)
            except Exception:  # pragma: no cover - best-effort
                self.logger.debug("zero_bytes(raw) failed; bytes will be GC'd")

        # Step 6: publish NATS metadata-only notification.
        self._publish_emitted_event(
            cluster_id=cluster_id,
            joiner_secret_id=joiner_secret_id,
            biome_kind=biome.biome_kind,
            extractor_name=extractor.name,
            scope=extractor.scope,
        )

        self.logger.info(
            "Joiner secret emit completed",
            extra={
                "cluster_id": cluster_id,
                "biome_kind": biome.biome_kind,
                "extractor": extractor.name,
                "joiner_secret_id": joiner_secret_id,
                "audit_event_id": audit_event_id,
                "raw_bytes_ref": _mask(raw_bytes),
            },
        )

        return JoinerSecretMetadata(
            joiner_secret_id=joiner_secret_id,
            cluster_id=cluster_id,
            biome_kind=biome.biome_kind,
            extractor_name=extractor.name,
            scope=extractor.scope,
            rotation_class=extractor.rotation,
            ttl_seconds=extractor.ttl_seconds if extractor.ttl_seconds else None,
            expires_at=expires_at,
            audit_event_id=audit_event_id,
            emitter_biome_id=biome.id,
            emitter_node_id=node.id,
        )

    # ------------------------------------------------------------------ #
    # Extractor invocation                                               #
    # ------------------------------------------------------------------ #

    def _extract_secret(
        self,
        node_id: int,
        lxd_instance: str,
        extractor_spec: _ExtractorSpec,
    ) -> bytes:
        """Run the extractor inside the LXD instance via the control tunnel.

        The control-tunnel adapter routes the call to one of four sources
        declared in ``joiner_emit_spec.extractors[].source``:

        * ``exec``       -> ``control_tunnel.run_command(...)``
        * ``file``       -> ``control_tunnel.read_file(...)``
        * ``http_api``   -> ``control_tunnel.http_api(...)``
        * ``lxd_config`` -> ``control_tunnel.lxd_config(...)``

        Args:
            node_id: Target node primary key.
            lxd_instance: LXD instance name hosting the biome.
            extractor_spec: Validated extractor spec.

        Returns:
            Raw extractor output bytes (caller must zero after use).

        Raises:
            ControlTunnelExtractionError: On any control-tunnel failure.
            ExtractorSpecError: When the spec lacks required fields.
        """
        try:
            if extractor_spec.source == SOURCE_EXEC:
                if not extractor_spec.command:
                    raise ExtractorSpecError(
                        f"Extractor {extractor_spec.name!r} source={SOURCE_EXEC} requires command"
                    )
                output = self.control_tunnel.run_command(
                    node_id=node_id,
                    lxd_instance=lxd_instance,
                    command=list(extractor_spec.command),
                )
            elif extractor_spec.source == SOURCE_FILE:
                if not extractor_spec.path:
                    raise ExtractorSpecError(
                        f"Extractor {extractor_spec.name!r} source={SOURCE_FILE} requires path"
                    )
                output = self.control_tunnel.read_file(
                    node_id=node_id,
                    lxd_instance=lxd_instance,
                    path=extractor_spec.path,
                )
            elif extractor_spec.source == SOURCE_HTTP:
                if not extractor_spec.url:
                    raise ExtractorSpecError(
                        f"Extractor {extractor_spec.name!r} source={SOURCE_HTTP} requires url"
                    )
                output = self.control_tunnel.http_api(
                    node_id=node_id,
                    lxd_instance=lxd_instance,
                    url=extractor_spec.url,
                    method=extractor_spec.method or "GET",
                )
            elif extractor_spec.source == SOURCE_LXD:
                if not extractor_spec.config_key:
                    raise ExtractorSpecError(
                        f"Extractor {extractor_spec.name!r} source={SOURCE_LXD} requires config_key"
                    )
                output = self.control_tunnel.lxd_config(
                    node_id=node_id,
                    lxd_instance=lxd_instance,
                    config_key=extractor_spec.config_key,
                )
            else:
                raise ExtractorSpecError(
                    f"Extractor {extractor_spec.name!r} unsupported source: {extractor_spec.source}"
                )
        except ExtractorSpecError:
            raise
        except Exception as e:
            # Sanitized: never log raw bytes / output / inner data.
            self.logger.error(
                "Control tunnel extractor failed",
                extra={
                    "node_id": node_id,
                    "extractor": extractor_spec.name,
                    "source": extractor_spec.source,
                    "error_type": type(e).__name__,
                },
            )
            raise ControlTunnelExtractionError(
                f"Extractor {extractor_spec.name!r} ({extractor_spec.source}) failed"
            ) from e

        if not isinstance(output, (bytes, bytearray)):
            raise ControlTunnelExtractionError(
                f"Extractor {extractor_spec.name!r} returned non-bytes output"
            )
        # Normalize to bytes so caller can zero deterministically.
        return bytes(output)

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #

    def _persist(
        self,
        *,
        extractor_name: str,
        scope: str,
        raw_bytes: bytes,
        ttl_seconds: Optional[int],
        rotation_class: Optional[str],
        emitter_biome_id: int,
        emitter_node_id: int,
        biome_kind: str,
        cluster_id: str,
        tenant_id: str,
    ) -> tuple[str, str, Optional[datetime]]:
        """Encrypt + insert + audit inside one penguin-dal transaction.

        Thin wrapper over the module-level ``persist_extracted_material``
        helper -- the same function ``app.api.joiner_secrets
        .rotate_joiner_secret`` and the gRPC ``JoinerSecrets`` servicer use,
        so there is one write path for "envelope-encrypt + insert
        joiner_secrets + append a hash-chained audit_events row", not two.
        Previously this method duplicated that whole sequence directly
        against a SQLAlchemy ``Session``.

        Isolation note: the former SQLAlchemy implementation opened its
        connection with ``execution_options={"isolation_level":
        "SERIALIZABLE"}``; penguin-dal's ``Tx`` (``DB.transaction()``) has
        no isolation-level control, so that guarantee is not carried
        forward here. This matches ``rotate_joiner_secret``'s existing
        penguin-dal-transaction implementation (task 6a) exactly -- same
        tradeoff, not a regression introduced by this conversion. Both
        still get one pinned connection with commit-on-clean-exit /
        rollback-on-exception, which is what makes the insert+audit pair
        atomic.

        Returns:
            ``(joiner_secret_id, audit_event_id, expires_at)``.
        """
        material = ExtractedMaterial(
            extractor_name=extractor_name,
            plaintext=raw_bytes,
            ttl_seconds=ttl_seconds or 0,
            rotation_class=rotation_class,
        )
        with self.db.transaction() as tx:
            return persist_extracted_material(
                tx,
                cluster_id=str(cluster_id),
                tenant_id=tenant_id,
                biome_kind=biome_kind,
                emitter_biome_id=emitter_biome_id,
                emitter_node_id=emitter_node_id,
                extractor_name=extractor_name,
                scope=scope,
                vault_client=self.vault_client,
                material=material,
                actor_sub=self.actor_sub,
                actor_scope=["system:api-manager"],
                action="joiner.secret.emit",
                vault_kek_name=self.vault_kek_name,
            )

    # ------------------------------------------------------------------ #
    # NATS                                                               #
    # ------------------------------------------------------------------ #

    def _publish_emitted_event(
        self,
        *,
        cluster_id: str,
        joiner_secret_id: str,
        biome_kind: str,
        extractor_name: str,
        scope: str,
    ) -> None:
        """Publish ``gough.joiner.<cluster-id>.emitted`` with metadata only."""
        if self.nats is None:
            return
        subject = f"gough.joiner.{cluster_id}.emitted"
        # Spec: NATS payload contains NO secret data — metadata only.
        payload = (
            "{"
            f'"joiner_secret_id":"{joiner_secret_id}",'
            f'"biome_kind":"{biome_kind}",'
            f'"extractor_name":"{extractor_name}",'
            f'"scope":"{scope}"'
            "}"
        ).encode("utf-8")
        try:
            self.nats.publish(subject, payload)
        except Exception as e:  # pragma: no cover - publisher errors logged, not raised
            self.logger.warning(
                "NATS publish failed; emit succeeded but downstream notify dropped",
                extra={
                    "subject": subject,
                    "error_type": type(e).__name__,
                },
            )

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _load_context(
        self, biome_instance_id: int
    ) -> tuple[Row, Row, Row]:
        """Load assignment + biome + node and validate ready state.

        Physical table is ``node_egg_assignments`` (``db.node_egg_assignments``)
        with an ``egg_id`` column -- ``NodeBiomeAssignment.biome_id`` was only
        ever a Python-side ORM ``synonym("egg_id")``, never a real column, so
        penguin-dal's reflected access (raw DB columns only) uses ``egg_id``
        directly, not ``biome_id``.
        """
        assignment = (
            self.db(self.db.node_egg_assignments.id == biome_instance_id)
            .select()
            .first()
        )
        if assignment is None:
            raise EmitterStateError(
                f"biome_instance {biome_instance_id} not found"
            )
        if assignment.status != "ready":
            raise EmitterStateError(
                f"biome_instance {biome_instance_id} status is "
                f"{assignment.status!r}, expected 'ready'"
            )

        biome = self.db(self.db.biomes.id == assignment.egg_id).select().first()
        if biome is None:
            raise EmitterStateError(
                f"biome {assignment.egg_id} for biome_instance {biome_instance_id} not found"
            )
        if not biome.emits_joiner_secrets:
            raise EmitterStateError(
                f"biome {biome.id} (kind={biome.biome_kind!r}) has emits_joiner_secrets=false"
            )

        node = self.db(self.db.nodes.id == assignment.node_id).select().first()
        if node is None:
            raise EmitterStateError(
                f"node {assignment.node_id} for biome_instance {biome_instance_id} not found"
            )

        return assignment, biome, node

    def _resolve_cluster_id(self, node: Row) -> str:
        """Resolve cluster UUID (constructor override or per-node lookup).

        The Node model in M1 does not carry a direct cluster_id column;
        callers either inject a cluster_id at construction time (typical
        when the emitter runs inside a per-cluster worker) or fall back
        to a lookup via storage_backends in the same tenant.
        """
        if self._cluster_id is not None:
            return self._cluster_id

        row = (
            self.db(self.db.storage_backends.tenant_id == node.tenant_id)
            .select(self.db.storage_backends.cluster_id, limitby=(0, 1))
            .first()
        )
        if row is None or row.cluster_id is None:
            raise EmitterStateError(
                f"cluster_id not resolvable for node {node.id}; "
                "construct JoinerSecretEmitter with cluster_id=..."
            )
        return str(row.cluster_id)

    @staticmethod
    def _derive_lxd_instance(
        node: Row, biome: Row, assignment: Row
    ) -> str:
        """Derive the LXD instance name for the biome's workload.

        Spec convention: workloads run in an LXD instance named
        ``<biome.biome_kind>-<assignment.id>`` on the target node; for
        ``host`` workloads the ``node.name`` is used directly.
        """
        if biome.workload_type == "host":
            return node.name
        return f"{biome.biome_kind}-{assignment.id}"

    def _validate_spec(self, biome: Row) -> list[_ExtractorSpec]:
        """Parse + validate ``joiner_emit_spec.extractors[]``."""
        spec = biome.joiner_emit_spec or {}
        if not isinstance(spec, dict):
            raise ExtractorSpecError(
                f"biome {biome.id}: joiner_emit_spec must be an object"
            )
        raw_extractors: Iterable[Any] = spec.get("extractors") or []
        if not isinstance(raw_extractors, list):
            raise ExtractorSpecError(
                f"biome {biome.id}: joiner_emit_spec.extractors must be a list"
            )

        validated: list[_ExtractorSpec] = []
        for entry in raw_extractors:
            if not isinstance(entry, dict):
                raise ExtractorSpecError(
                    f"biome {biome.id}: each extractor must be an object"
                )
            try:
                ex = _ExtractorSpec.model_validate(entry)
            except Exception as e:
                raise ExtractorSpecError(
                    f"biome {biome.id}: invalid extractor: {e}"
                ) from e
            if ex.source not in VALID_SOURCES:
                raise ExtractorSpecError(
                    f"biome {biome.id}: extractor {ex.name!r} unsupported source {ex.source!r}"
                )
            if ex.scope not in VALID_SCOPES:
                raise ExtractorSpecError(
                    f"biome {biome.id}: extractor {ex.name!r} unsupported scope {ex.scope!r}"
                )
            if ex.ttl_seconds < 0:
                raise ExtractorSpecError(
                    f"biome {biome.id}: extractor {ex.name!r} ttl_seconds must be >= 0"
                )
            validated.append(ex)
        return validated


__all__ = [
    "ControlTunnelClient",
    "ControlTunnelExtractionError",
    "EmitterStateError",
    "ExtractorSpecError",
    "JoinerSecret",
    "JoinerSecretEmitter",
    "JoinerSecretMetadata",
    "NatsPublisher",
    "VAULT_KEK_NAME",
]
