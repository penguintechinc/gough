"""gRPC servicer implementations for Plan 2 (Deployment).

Thin wrappers over the same service/dal functions used by the REST endpoints.
No business logic lives here — all logic is in the REST layer functions.

mTLS is Plan 4 scope; the runner uses an insecure channel until then.

Regression: gh-22 (DB pool consolidation). Servicer methods use
``app.db.database.get_db()`` -- the RLS-wired, app-context-free accessor
(see that module's docstring) -- NOT ``app.models.get_db()``. The gRPC
server runs as a background ``asyncio.ensure_future()`` task started from
``app.__init__``'s ``_start_grpc`` ``before_serving`` hook: Quart pushes an
app context only for the duration of that hook coroutine's own execution,
not for tasks it schedules and returns from immediately, so every real
servicer call (once the gRPC listener actually starts accepting traffic --
previously it silently failed to start at all, see the ``_start_grpc``
docstring) runs with NO Quart app context. ``app.models.get_db()`` would
``RuntimeError`` on every such call; ``app.db.database.get_db()`` doesn't
need one. Each servicer's cross-tenant sentinel wrap (``_cross_tenant_scope``
below) and the closure+``asyncio.to_thread()`` idiom are unaffected by this
switch -- ``app.db.rls``'s tenant ``ContextVar`` propagates through
``to_thread()`` regardless of which accessor resolved the connection pool.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import string
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import grpc

# The protoc-generated *_pb2_grpc.py files use bare `from gough import …`.
# Adding app/grpc to sys.path makes the `gough` package importable by that name.
_GRPC_PKG_DIR = os.path.join(os.path.dirname(__file__), "grpc")
if _GRPC_PKG_DIR not in sys.path:
    sys.path.insert(0, _GRPC_PKG_DIR)

from gough import ipxe_pb2, ipxe_pb2_grpc  # noqa: E402
from gough import joiner_pb2, joiner_pb2_grpc  # noqa: E402
from gough import biomes_pb2, biomes_pb2_grpc  # noqa: E402
from gough import audit_pb2, audit_pb2_grpc  # noqa: E402
from gough import identity_pb2, identity_pb2_grpc  # noqa: E402

from app.db.rls import CROSS_TENANT_SENTINEL, get_current_tenant, set_current_tenant  # noqa: E402

log = logging.getLogger(__name__)


@contextmanager
def _cross_tenant_scope() -> Iterator[None]:
    """Push the RLS cross-tenant sentinel for one gRPC handler's DB work.

    gRPC servicers run outside Quart's HTTP request pipeline (see module
    docstring: mTLS/SPIFFE identity is Plan 4 scope, the runner is still an
    insecure channel), so the tenant bridge never runs for these calls
    and ``app.db.rls``'s tenant ContextVar is never set. A scoped Postgres
    role would therefore see zero rows on every RLS-protected table (fail
    closed) -- and every query these RPCs make is *already* untenanted at
    the app level too: lookups are keyed by secret id / node id, never
    tenant_id, because this is a system/agent-facing trust boundary, not a
    per-tenant HTTP caller (see ``app.api.joiner_secrets`` module
    docstring: "Plaintext extraction is reserved for the gRPC ``Joiner
    .Consume`` RPC on a different trust boundary"). This explicitly opts
    into the documented cross-tenant sentinel instead of silently returning
    zero rows, and restores whatever tenant (if any) was set on this
    task's context before returning -- writes made under this scope still
    carry each row's own real ``tenant_id`` (the sentinel only affects
    which existing rows are *visible* to reads/updates, not what gets
    written).
    """
    previous = get_current_tenant()
    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        yield
    finally:
        set_current_tenant(previous)


# ---------------------------------------------------------------------------
# IPXE servicer
# ---------------------------------------------------------------------------


class IPXEServicer(ipxe_pb2_grpc.IPXEServicer):
    """gRPC handlers for IPXE service — delegates to ipxe.py helpers."""

    async def MintBootstrapToken(
        self,
        request: ipxe_pb2.MintBootstrapTokenRequest,
        context: grpc.aio.ServicerContext,
    ) -> ipxe_pb2.MintBootstrapTokenResponse:
        from app.api.ipxe import _find_node_or_machine_by_mac, _mint_bootstrap_jwt
        from app.db.database import get_db

        try:
            db = get_db()
            node = None
            if request.node_id:
                node = await asyncio.to_thread(
                    lambda: db(db.nodes.id == request.node_id).select().first()
                    if hasattr(db, "nodes") else None
                )
            if node is None:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"Node {request.node_id} not found")
                return ipxe_pb2.MintBootstrapTokenResponse()

            mac: str = getattr(node, "primary_nic_mac", None) or ""
            dmi_uuid: str = getattr(node, "dmi_uuid", None) or ""

            token, _nonce = await asyncio.to_thread(
                lambda: _mint_bootstrap_jwt(mac, phase="deploy", dmi_uuid_hint=dmi_uuid)
            )

            now_ts = int(datetime.now(timezone.utc).timestamp())
            from app.api.ipxe import _BOOTSTRAP_JWT_TTL_SECONDS
            expiration = now_ts + _BOOTSTRAP_JWT_TTL_SECONDS

            return ipxe_pb2.MintBootstrapTokenResponse(
                token=token,
                expiration=expiration,
            )
        except Exception as exc:
            log.exception("MintBootstrapToken gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return ipxe_pb2.MintBootstrapTokenResponse()

    async def ResolveBootScript(
        self,
        request: ipxe_pb2.ResolveBootScriptRequest,
        context: grpc.aio.ServicerContext,
    ) -> ipxe_pb2.ResolveBootScriptResponse:
        from app.api.ipxe import (
            _normalize_mac,
            _find_node_or_machine_by_mac,
            _render_helper_ipxe_script,
            _render_deploy_ipxe_script,
        )
        import os

        try:
            mac = request.mac_address or ""
            normalized = await asyncio.to_thread(_normalize_mac, mac)
            if not normalized:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"Invalid MAC: {mac}")
                return ipxe_pb2.ResolveBootScriptResponse()

            record: dict[str, Any] | None = await asyncio.to_thread(
                _find_node_or_machine_by_mac, normalized
            )
            if record is None:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"MAC not found: {normalized}")
                return ipxe_pb2.ResolveBootScriptResponse()

            primary_url = os.getenv("PRIMARY_BASE_URL", "https://localhost")

            from app.api.ipxe import _mint_bootstrap_jwt
            token, _nonce = await asyncio.to_thread(
                lambda: _mint_bootstrap_jwt(
                    normalized,
                    phase="deploy",
                    dmi_uuid_hint=record.get("dmi_uuid"),
                )
            )

            script = await asyncio.to_thread(
                _render_deploy_ipxe_script, normalized, token, primary_url
            )

            return ipxe_pb2.ResolveBootScriptResponse(boot_script=script)
        except Exception as exc:
            log.exception("ResolveBootScript gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return ipxe_pb2.ResolveBootScriptResponse()

    async def BindMac(
        self,
        request: ipxe_pb2.BindMacRequest,
        context: grpc.aio.ServicerContext,
    ) -> ipxe_pb2.BindMacResponse:
        from app.api.ipxe import _normalize_mac
        from app.db.database import get_db

        try:
            normalized = await asyncio.to_thread(_normalize_mac, request.mac_address or "")
            if not normalized:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"Invalid MAC: {request.mac_address}",
                )
                return ipxe_pb2.BindMacResponse()

            db = get_db()
            node_id = request.node_id

            def _do_bind() -> tuple[bool, str]:
                if not hasattr(db, "nodes"):
                    raise RuntimeError("nodes table not available")

                node = None
                if node_id:
                    node = db(db.nodes.id == node_id).select().first()
                    if not node:
                        raise KeyError(f"Node not found: {node_id}")

                if node:
                    db(db.nodes.id == node.id).update(
                        primary_nic_mac=normalized,
                        updated_at=datetime.now(timezone.utc),
                    )
                    db.commit()
                    return True, str(node.id)

                new_id = db.nodes.insert(
                    tenant_id="__default__",
                    name=f"node-{normalized.replace(':', '')}",
                    state="new",
                    primary_nic_mac=normalized,
                )
                db.commit()
                return True, str(new_id)

            bound, result_node_id = await asyncio.to_thread(_do_bind)
            return ipxe_pb2.BindMacResponse(bound=bound, node_id=result_node_id)

        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            return ipxe_pb2.BindMacResponse()
        except Exception as exc:
            log.exception("BindMac gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return ipxe_pb2.BindMacResponse()

    async def FetchCloudInit(
        self,
        request: ipxe_pb2.FetchCloudInitRequest,
        context: grpc.aio.ServicerContext,
    ) -> ipxe_pb2.FetchCloudInitResponse:
        from app.db.database import get_db

        _VALID_BASELINES = {"native", "hybrid", "full-virtual"}

        try:
            baseline = (request.baseline or "native").lower()
            if baseline not in _VALID_BASELINES:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"Invalid baseline; must be one of: {', '.join(sorted(_VALID_BASELINES))}",
                )
                return ipxe_pb2.FetchCloudInitResponse()

            node_id = request.node_id

            def _fetch() -> tuple[str, str]:
                db = get_db()
                node = db.nodes(node_id) if hasattr(db, "nodes") else None
                if not node:
                    raise KeyError(f"Node {node_id} not found")

                template_row = None
                if hasattr(db, "cloud_init_templates"):
                    template_row = db(
                        db.cloud_init_templates.is_default == True  # noqa: E712
                    ).select().first()

                if template_row is None:
                    raise LookupError("No default cloud-init template configured")

                hostname = getattr(node, "name", None) or str(node_id)
                content = getattr(template_row, "template_content", None) or ""
                rendered = string.Template(content).safe_substitute(
                    node_id=str(node_id),
                    hostname=hostname,
                    baseline=baseline,
                )
                return rendered, "text/plain; charset=utf-8"

            user_data, content_type = await asyncio.to_thread(_fetch)
            return ipxe_pb2.FetchCloudInitResponse(
                user_data=user_data,
                content_type=content_type,
            )

        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            return ipxe_pb2.FetchCloudInitResponse()
        except LookupError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            return ipxe_pb2.FetchCloudInitResponse()
        except Exception as exc:
            log.exception("FetchCloudInit gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return ipxe_pb2.FetchCloudInitResponse()


# ---------------------------------------------------------------------------
# JoinerSecrets servicer
# ---------------------------------------------------------------------------


class JoinerSecretsServicer(joiner_pb2_grpc.JoinerSecretsServicer):
    """gRPC handlers for JoinerSecrets service — delegates to joiner_secrets.py helpers.

    A system/agent-facing trust boundary, not a per-tenant HTTP caller (see
    ``app.api.joiner_secrets`` module docstring: "Plaintext extraction is
    reserved for the gRPC ``Joiner.Consume`` RPC on a different trust
    boundary"). None of these RPCs carried a tenant filter in the original
    SQLAlchemy implementation either -- every lookup below is keyed by
    ``secret_id``/``node_id`` alone -- so DB reads/writes run under
    ``_cross_tenant_scope()`` (module-level helper above), not a per-request
    tenant. mTLS/SPIFFE caller identity is Plan 4 scope (see module
    docstring); until then there is no caller identity to scope to anyway.
    """

    async def Emit(
        self,
        request: joiner_pb2.EmitRequest,
        context: grpc.aio.ServicerContext,
    ) -> joiner_pb2.EmitResponse:
        """Persist externally-extracted joiner material for a node.

        Does NOT go through ``JoinerSecretEmitter.emit()`` -- that method's
        contract (``biome_instance_id`` -> run a fresh control-tunnel
        extraction -> ``list[JoinerSecretMetadata]``) is a different flow
        than "persist material a ``JOINER_ROTATE_MATERIAL_PROVIDER`` already
        extracted", the same distinction ``app.api.joiner_secrets
        .rotate_joiner_secret`` makes (task 6a). The previous implementation
        called ``JoinerSecretEmitter(db_session=..., audit_writer=...)``
        (kwargs the real constructor has never accepted) then
        ``emitter.emit(cluster_id=..., ...)`` (kwargs the real ``emit()``
        has never accepted either -- it takes only ``biome_instance_id``) --
        this RPC could never have succeeded, on any version of this file.
        Uses the shared ``persist_extracted_material`` helper instead,
        inside one ``db.transaction()`` -- the same pattern ``Rotate``
        below and ``rotate_joiner_secret`` use.

        NOTE (pre-existing gap, not fully resolved here): ``joiner_secrets
        .emitter_biome_id`` is a NOT NULL FK to ``biomes.id``, but
        ``EmitRequest`` carries no biome reference of its own (only
        ``node_id``/``rotation_class``/``key_version``). Resolved here via
        the node's current ``ready`` ``node_egg_assignments`` row, matching
        this module's own documented flow ("Emitter biome reaches ready on
        its target node" -- ``joiner_secret_emitter`` module docstring,
        step 1); aborts NOT_FOUND if there is none, rather than guessing a
        value. ``request.key_version`` has no column on ``joiner_secrets``
        at all (only ``rotation_class`` is tracked) and is not persisted --
        also pre-existing, not introduced by this conversion.

        FIX ROUND 1 (data-integrity gap): ``_resolve_ready_biome`` must
        only attribute the secret to a biome whose ``emits_joiner_secrets``
        is true -- a node can have more than one ``ready`` assignment
        (e.g. one joiner-emitting, one not), and picking the most-recent
        ``ready`` row regardless of its biome's ``emits_joiner_secrets``
        flag could attribute the persisted secret to the wrong biome. This
        must have parity with the pre-existing
        ``JoinerSecretEmitter._load_context``, which enforces the same
        invariant (``if not biome.emits_joiner_secrets: raise
        EmitterStateError(...)``) once it has resolved a specific biome.
        penguin-dal's query builder has no join support (single ``Query``
        tracks one table -- see ``penguin_dal.query.Query``), so this
        can't be expressed as one filtered ``SELECT`` the way a real join
        could; instead walks the node's ``ready`` assignments
        most-recent-first and returns the first whose biome actually
        emits, mirroring ``_load_context``'s per-assignment check rather
        than trusting whichever assignment happens to be newest. No
        qualifying assignment (none ready at all, or none with an
        emitting biome) is treated identically -- both are "nothing to
        emit for this node" and abort NOT_FOUND (the caller already maps
        ``_resolve_ready_biome() is None`` to that), rather than
        introducing a second, distinguishable failure mode.
        """
        from app.db.database import get_db
        from app.workers.joiner_secret_emitter import ExtractedMaterial, persist_extracted_material
        from app.security.joiner_envelope import zero_bytes
        from quart import current_app
        import uuid as _uuid

        try:
            node_id = int(request.node_id)
        except (TypeError, ValueError):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, f"Invalid node_id: {request.node_id!r}"
            )
            return joiner_pb2.EmitResponse()

        db = get_db()
        vault_client = current_app.config.get("VAULT_CLIENT")
        material_provider = current_app.config.get("JOINER_ROTATE_MATERIAL_PROVIDER")
        cluster_id = str(_uuid.UUID(current_app.config.get("CLUSTER_ID", str(_uuid.uuid4()))))

        def _resolve_ready_biome() -> tuple[int, str, str] | None:
            with _cross_tenant_scope():
                assignments = db(
                    (db.node_egg_assignments.node_id == node_id)
                    & (db.node_egg_assignments.status == "ready")
                ).select(orderby=~db.node_egg_assignments.id)

                for assignment in assignments:
                    biome = db(db.biomes.id == assignment.egg_id).select().first()
                    if biome is not None and biome.emits_joiner_secrets:
                        return int(biome.id), str(biome.tenant_id), str(biome.biome_kind)

            return None

        try:
            if material_provider is None:
                raise RuntimeError("JOINER_ROTATE_MATERIAL_PROVIDER not configured")

            resolved = await asyncio.to_thread(_resolve_ready_biome)
            if resolved is None:
                raise LookupError(f"No ready biome assignment for node {node_id}")
            emitter_biome_id, tenant_id, biome_kind = resolved

            material: ExtractedMaterial = await asyncio.to_thread(material_provider, None)

            def _persist() -> tuple[str, Any]:
                with _cross_tenant_scope(), db.transaction() as tx:
                    new_id, _audit_event_id, expires_at = persist_extracted_material(
                        tx,
                        cluster_id=cluster_id,
                        tenant_id=tenant_id,
                        biome_kind=biome_kind,
                        emitter_biome_id=emitter_biome_id,
                        emitter_node_id=node_id,
                        extractor_name=request.rotation_class or "grpc-emit",
                        scope="node",
                        vault_client=vault_client,
                        material=material,
                        actor_sub="grpc",
                        actor_scope=[],
                        action="joiner.secret.emit",
                    )
                    return new_id, expires_at

            try:
                new_id, expires_at = await asyncio.to_thread(_persist)
            finally:
                try:
                    zero_bytes(material.plaintext)
                except Exception:  # pragma: no cover - best-effort
                    log.debug("zero_bytes(material.plaintext) failed; bytes will be GC'd")

            return joiner_pb2.EmitResponse(
                secret_id=new_id,
                expires_at=expires_at.isoformat() if expires_at else "",
            )

        except LookupError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            return joiner_pb2.EmitResponse()
        except RuntimeError as exc:
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
            return joiner_pb2.EmitResponse()
        except Exception as exc:
            log.exception("JoinerSecrets.Emit gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return joiner_pb2.EmitResponse()

    async def Consume(
        self,
        request: joiner_pb2.ConsumeRequest,
        context: grpc.aio.ServicerContext,
    ) -> joiner_pb2.ConsumeResponse:
        """Decrypt and return a joiner secret's plaintext.

        Fixes a pre-existing bug found while making this reachable for the
        first time: the response field is named ``decrypted_secret`` but
        the previous implementation returned the raw envelope ciphertext
        verbatim -- it never called ``decrypt_envelope`` at all, so every
        (theoretical) caller of this RPC received AES-256-GCM ciphertext +
        auth tag labeled as plaintext. Mirrors
        ``IdentityServicer.VerifyOTPN``'s existing correct decrypt pattern
        in this same file.
        """
        from app.db.database import get_db
        from app.security.joiner_envelope import EnvelopeCiphertext, decrypt_envelope
        from quart import current_app
        import uuid as _uuid

        try:
            secret_id = str(_uuid.UUID(request.secret_id))
        except (TypeError, ValueError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"Invalid secret_id: {exc}")
            return joiner_pb2.ConsumeResponse()

        db = get_db()
        vault_client = current_app.config.get("VAULT_CLIENT")

        def _fetch_and_decrypt() -> tuple[bytes, str]:
            with _cross_tenant_scope():
                row = db(db.joiner_secrets.id == secret_id).select().first()
            if row is None:
                raise KeyError(f"Secret not found: {request.secret_id}")
            if row.revoked_at is not None:
                raise PermissionError(f"Secret {request.secret_id} is revoked")

            envelope = EnvelopeCiphertext(
                ciphertext=bytes(row.ciphertext),
                iv=bytes(row.iv),
                auth_tag=bytes(row.auth_tag),
                dek_wrapped=bytes(row.dek_wrapped),
                vault_kek_name=row.vault_kek_name,
            )
            plaintext = decrypt_envelope(
                envelope, vault_client, joiner_secret_id=_uuid.UUID(secret_id)
            )
            return plaintext, row.rotation_class or ""

        try:
            plaintext, rotation_class = await asyncio.to_thread(_fetch_and_decrypt)
            return joiner_pb2.ConsumeResponse(
                decrypted_secret=plaintext,
                rotation_class=rotation_class,
            )

        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            return joiner_pb2.ConsumeResponse()
        except PermissionError as exc:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
            return joiner_pb2.ConsumeResponse()
        except Exception as exc:
            log.exception("JoinerSecrets.Consume gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return joiner_pb2.ConsumeResponse()

    async def Rotate(
        self,
        request: joiner_pb2.RotateRequest,
        context: grpc.aio.ServicerContext,
    ) -> joiner_pb2.RotateResponse:
        """Revoke + persist a fresh joiner secret atomically.

        Same ``db.transaction()`` + ``persist_extracted_material`` pattern
        as ``app.api.joiner_secrets.rotate_joiner_secret`` (task 6a) -- one
        write path, not two -- adapted for this RPC's lookup-by-``secret_id``
        -only contract (no tenant claim on this trust boundary; see class
        docstring). Does not go through ``JoinerSecretEmitter.emit()`` for
        the same reason ``Emit`` above doesn't -- that call was never valid
        (constructor and ``.emit()`` kwargs neither one matches the real
        class, same finding task 6a made for ``app/api/joiner_secrets.py``).
        """
        from app.db.database import get_db
        from app.workers.joiner_secret_emitter import persist_extracted_material
        from app.security.joiner_envelope import zero_bytes
        from quart import current_app
        import uuid as _uuid

        try:
            secret_id = str(_uuid.UUID(request.secret_id))
        except (TypeError, ValueError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"Invalid secret_id: {exc}")
            return joiner_pb2.RotateResponse()

        db = get_db()

        def _fetch_row() -> Any:
            with _cross_tenant_scope():
                return db(db.joiner_secrets.id == secret_id).select().first()

        try:
            row = await asyncio.to_thread(_fetch_row)
            if row is None:
                raise KeyError(f"Secret not found: {request.secret_id}")
            if row.revoked_at is not None:
                raise LookupError("Cannot rotate a revoked secret")

            material_provider = current_app.config.get("JOINER_ROTATE_MATERIAL_PROVIDER")
            if material_provider is None:
                raise RuntimeError("JOINER_ROTATE_MATERIAL_PROVIDER not configured")

            material = await asyncio.to_thread(material_provider, row)
            now = datetime.now(timezone.utc)
            cluster_id = str(row.cluster_id)
            vault_client = current_app.config.get("VAULT_CLIENT")

            def _persist() -> tuple[str, Any]:
                with _cross_tenant_scope(), db.transaction() as tx:
                    tx.executesql(
                        "UPDATE joiner_secrets SET revoked_at = %s, rotated_at = %s "
                        "WHERE id = %s",
                        (now, now, secret_id),
                    )
                    new_id, _audit_event_id, expires_at = persist_extracted_material(
                        tx,
                        cluster_id=cluster_id,
                        tenant_id=row.tenant_id,
                        biome_kind=row.biome_kind,
                        emitter_biome_id=row.emitter_biome_id,
                        emitter_node_id=row.emitter_node_id,
                        extractor_name=row.extractor_name,
                        scope=row.scope,
                        vault_client=vault_client,
                        material=material,
                        actor_sub="grpc",
                        actor_scope=[],
                        action="joiner.secret.rotate",
                    )
                    return new_id, expires_at

            try:
                new_id, expires_at = await asyncio.to_thread(_persist)
            finally:
                try:
                    zero_bytes(material.plaintext)
                except Exception:  # pragma: no cover - best-effort
                    log.debug("zero_bytes(material.plaintext) failed; bytes will be GC'd")

            return joiner_pb2.RotateResponse(
                new_secret_id=new_id,
                expires_at=expires_at.isoformat() if expires_at else "",
            )

        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            return joiner_pb2.RotateResponse()
        except LookupError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            return joiner_pb2.RotateResponse()
        except RuntimeError as exc:
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
            return joiner_pb2.RotateResponse()
        except Exception as exc:
            log.exception("JoinerSecrets.Rotate gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return joiner_pb2.RotateResponse()

    async def Revoke(
        self,
        request: joiner_pb2.RevokeRequest,
        context: grpc.aio.ServicerContext,
    ) -> joiner_pb2.RevokeResponse:
        """Mark a joiner secret revoked (never hard-deletes; see class docstring)."""
        from app.db.database import get_db
        import uuid as _uuid

        try:
            secret_id = str(_uuid.UUID(request.secret_id))
        except (TypeError, ValueError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"Invalid secret_id: {exc}")
            return joiner_pb2.RevokeResponse()

        db = get_db()

        def _do_revoke() -> bool:
            with _cross_tenant_scope():
                row = db(db.joiner_secrets.id == secret_id).select().first()
                if row is None:
                    raise KeyError(f"Secret not found: {request.secret_id}")
                if row.revoked_at is not None:
                    return False
                db(db.joiner_secrets.id == secret_id).update(
                    revoked_at=datetime.now(timezone.utc)
                )
                return True

        try:
            revoked = await asyncio.to_thread(_do_revoke)
            return joiner_pb2.RevokeResponse(revoked=revoked)

        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            return joiner_pb2.RevokeResponse()
        except Exception as exc:
            log.exception("JoinerSecrets.Revoke gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return joiner_pb2.RevokeResponse()

    async def List(
        self,
        request: joiner_pb2.JoinerListRequest,
        context: grpc.aio.ServicerContext,
    ) -> joiner_pb2.JoinerListResponse:
        """List joiner secrets for a node, keyset-paginated.

        Fixes a pre-existing bug found while making this reachable for the
        first time: ``JoinerSecret.node_id`` in the proto response is a
        ``string`` field, but the previous implementation assigned
        ``r.emitter_node_id`` (an ``int``) to it directly -- protobuf raises
        ``TypeError`` assigning an int to a string field, so this RPC could
        never have returned a non-empty list even with a working DB
        session. Cast to ``str`` here. Preserves the original's
        ``created_at DESC`` ordering paired with an ``id >`` cursor filter
        verbatim (not internally consistent as keyset pagination, but not
        this task's call to redesign).
        """
        from app.db.database import get_db
        import uuid as _uuid
        import base64
        import json

        db = get_db()
        limit = request.limit if request.limit > 0 else 50
        cursor = request.cursor or None

        try:
            node_id = int(request.node_id)
        except (TypeError, ValueError):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, f"Invalid node_id: {request.node_id!r}"
            )
            return joiner_pb2.JoinerListResponse()

        def _fetch() -> tuple[list[Any], str]:
            query = db.joiner_secrets.emitter_node_id == node_id

            if cursor:
                try:
                    decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                    after_id = str(_uuid.UUID(decoded["id"]))
                    query = query & (db.joiner_secrets.id > after_id)
                except Exception:
                    pass

            with _cross_tenant_scope():
                rows = list(
                    db(query).select(
                        orderby=~db.joiner_secrets.created_at,
                        limitby=(0, limit + 1),
                    )
                )

            next_cur = ""
            if len(rows) > limit:
                rows = rows[:limit]
                last = rows[-1]
                next_cur = base64.urlsafe_b64encode(
                    json.dumps({"id": str(last.id)}).encode()
                ).decode()

            return rows, next_cur

        try:
            rows, next_cursor = await asyncio.to_thread(_fetch)

            secrets = [
                joiner_pb2.JoinerSecret(
                    id=str(r.id),
                    node_id=str(r.emitter_node_id) if r.emitter_node_id is not None else "",
                    rotation_class=r.rotation_class or "",
                    key_version=0,
                    created_at=r.created_at.isoformat() if r.created_at else "",
                    expires_at=r.expires_at.isoformat() if r.expires_at else "",
                )
                for r in rows
            ]

            return joiner_pb2.JoinerListResponse(
                secrets=secrets,
                next_cursor=next_cursor,
            )

        except Exception as exc:
            log.exception("JoinerSecrets.List gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return joiner_pb2.JoinerListResponse()


# ---------------------------------------------------------------------------
# Biomes servicer
# ---------------------------------------------------------------------------


class BiomesServicer(biomes_pb2_grpc.BiomesServicer):
    """gRPC handlers for Biomes service — delegates to biomes.py helpers."""

    async def DeployBiome(
        self,
        request: biomes_pb2.DeployBiomeRequest,
        context: grpc.aio.ServicerContext,
    ) -> biomes_pb2.DeployBiomeResponse:
        from app.db.database import get_db

        try:
            def _do_deploy() -> tuple[int, str]:
                db = get_db()

                # Validate biome exists
                if not hasattr(db, "biomes"):
                    raise RuntimeError("biomes table not available")

                biome = db(db.biomes.id == request.biome_id).select().first()
                if not biome:
                    raise KeyError(f"Biome not found: {request.biome_id}")

                # Validate node exists
                if not hasattr(db, "nodes"):
                    raise RuntimeError("nodes table not available")

                node = db(db.nodes.id == request.node_id).select().first()
                if not node:
                    raise KeyError(f"Node not found: {request.node_id}")

                # Create the assignment row on the real, baseline-created
                # ``node_egg_assignments`` table (gh-21: ``node_biome_assignments``
                # was a phantom name that never existed as a table -- this
                # insert unconditionally raised RuntimeError before this fix).
                # Physical columns are ``node_id``/``egg_id``, not
                # ``node_id``/``biome_id``; ``request.config``/``request.params``
                # have no matching column on this table and are intentionally
                # not persisted (same gap the phantom table would have had --
                # normalizing to the real table doesn't invent new columns).
                now = datetime.now(timezone.utc)
                assignment_id = db.node_egg_assignments.insert(
                    node_id=request.node_id,
                    egg_id=request.biome_id,
                    tenant_id=getattr(node, "tenant_id", None) or "__default__",
                    phase=getattr(biome, "phase", None) or "post_deploy",
                    status="pending",
                    readiness_probe_state="not_started",
                    assigned_at=now,
                    created_at=now,
                    updated_at=now,
                )
                db.commit()
                return assignment_id, "pending"

            assignment_id, status = await asyncio.to_thread(_do_deploy)
            return biomes_pb2.DeployBiomeResponse(
                deployment_id=str(assignment_id),
                status=status,
            )

        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            return biomes_pb2.DeployBiomeResponse()
        except Exception as exc:
            log.exception("Biomes.DeployBiome gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return biomes_pb2.DeployBiomeResponse()

    async def GetBiome(
        self,
        request: biomes_pb2.BiomeGetRequest,
        context: grpc.aio.ServicerContext,
    ) -> biomes_pb2.BiomeGetResponse:
        from app.db.database import get_db

        try:
            def _fetch() -> dict[str, Any]:
                db = get_db()

                if not hasattr(db, "biomes"):
                    raise RuntimeError("biomes table not available")

                biome = db(db.biomes.id == request.biome_id).select().first()
                if not biome:
                    raise KeyError(f"Biome not found: {request.biome_id}")

                return {
                    "id": biome.id,
                    "name": getattr(biome, "name", ""),
                    "version": getattr(biome, "version", ""),
                    "state": getattr(biome, "state", ""),
                    "created_at": int(getattr(biome, "created_at", datetime.now(timezone.utc)).timestamp()),
                }

            biome_data = await asyncio.to_thread(_fetch)
            biome_proto = biomes_pb2.Biome(
                id=biome_data["id"],
                name=biome_data["name"],
                version=biome_data["version"],
                state=biome_data["state"],
                created_at=biome_data["created_at"],
            )
            return biomes_pb2.BiomeGetResponse(biome=biome_proto)

        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            return biomes_pb2.BiomeGetResponse()
        except Exception as exc:
            log.exception("Biomes.GetBiome gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return biomes_pb2.BiomeGetResponse()

    async def VerifySignature(
        self,
        request: biomes_pb2.VerifySignatureRequest,
        context: grpc.aio.ServicerContext,
    ) -> biomes_pb2.VerifySignatureResponse:
        from app.db.database import get_db

        try:
            def _verify() -> tuple[bool, str]:
                db = get_db()

                if not hasattr(db, "biomes"):
                    raise RuntimeError("biomes table not available")

                biome = db(db.biomes.id == request.biome_id).select().first()
                if not biome:
                    raise KeyError(f"Biome not found: {request.biome_id}")

                verified = getattr(biome, "signature_verified", False)
                reason = "Signature verified" if verified else "Signature not verified"
                return verified, reason

            verified, reason = await asyncio.to_thread(_verify)
            return biomes_pb2.VerifySignatureResponse(
                verified=verified,
                reason=reason,
            )

        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            return biomes_pb2.VerifySignatureResponse()
        except Exception as exc:
            log.exception("Biomes.VerifySignature gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return biomes_pb2.VerifySignatureResponse()


# ---------------------------------------------------------------------------
# Audit servicer
# ---------------------------------------------------------------------------


class AuditServicer(audit_pb2_grpc.AuditServicer):
    """gRPC Audit service — wraps AuditEventWriter for chain-append + verify/export.

    ``Stream``/``ExportRange`` carried no tenant filter in the original
    implementation either -- read under ``_cross_tenant_scope()`` (see that
    helper's docstring, and the identical reasoning on
    ``JoinerSecretsServicer``).
    """

    async def AppendEvent(
        self,
        request: audit_pb2.AppendEventRequest,
        context: grpc.aio.ServicerContext,
    ) -> audit_pb2.AppendEventResponse:
        """Append one hash-chained audit event via ``AuditEventWriter``.

        ``AuditEventWriter``/``writer.append()`` require a real SQLAlchemy
        ``Session`` (``app.security.audit_chain`` -- a third file, out of
        this conversion's scope; see task 6a's identical boundary decision
        for ``app/api/joiner_secrets.py``). Accessor fix only:
        ``current_app.db_session`` is never set anywhere in this codebase
        (grepped ``app/__init__.py`` and every ``app.db_session =``
        assignment -- there are none), so this always raised
        ``AttributeError`` before reaching ``AuditEventWriter`` at all.
        Swapped for ``app.api.audit._get_db_session()``, the same
        session-acquisition helper the REST audit endpoints use -- it still
        depends on ``DB_SESSION_FACTORY`` being wired into
        ``app/__init__.py``'s ``create_app()``, which task 6a found is
        *also* never done, so this now raises a documented, precedented
        ``RuntimeError`` instead of an undocumented ``AttributeError``.
        Genuinely fixing this endpoint needs that wiring -- out of scope for
        both this file and that one.
        """
        from app.api.audit import _get_db_session
        from app.security.audit_chain import AuditEventWriter
        from app.models_m1 import AuditEvent as AuditEventModel
        import json

        try:
            event_data = json.loads(request.event_data)

            db = _get_db_session()
            cluster_id = event_data.get("cluster_id", "grpc")

            def _do_append() -> AuditEventModel:
                writer = AuditEventWriter(db_session=db, cluster_id=cluster_id, signer=None)
                event = writer.append(
                    actor_sub=event_data.get("actor_sub", "unknown"),
                    action=event_data.get("action", "unknown"),
                    resource_kind=event_data.get("resource_kind", "unknown"),
                    resource_id=event_data.get("resource_id"),
                    tenant_id=event_data.get("tenant_id"),
                    actor_scope=event_data.get("actor_scope"),
                    before=event_data.get("before_json"),
                    after=event_data.get("after_json"),
                    request_id=event_data.get("request_id"),
                    source_ip=event_data.get("source_ip"),
                    user_agent=event_data.get("user_agent"),
                )
                db.commit()
                return event

            event = await asyncio.to_thread(_do_append)
            return audit_pb2.AppendEventResponse(
                event_id=str(event.id),
                timestamp=int(event.ts.timestamp()),
            )

        except json.JSONDecodeError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "event_data must be JSON")
            return audit_pb2.AppendEventResponse()
        except Exception as exc:
            log.exception("Audit.AppendEvent gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return audit_pb2.AppendEventResponse()

    async def Stream(
        self,
        request: audit_pb2.StreamRequest,
        context: grpc.aio.ServicerContext,
    ):
        """Stream audit events since ``start_offset`` as JSON payloads.

        Fixes a pre-existing bug found while converting this query off
        SQLAlchemy: the original ``SELECT`` named a column,
        ``event_payload``, that has never existed on ``audit_events`` (see
        ``app.models_m1.AuditEvent`` -- the real columns are
        ``actor_sub``/``action``/``before_json``/``after_json``/etc, no
        single combined ``event_payload``) -- this would have raised
        ``UndefinedColumn`` from Postgres on every call, independent of
        this conversion. Selects the real columns instead and assembles
        the same payload shape ``ExportRange`` below already builds, which
        is clearly what was intended (a JSON event payload per stream
        entry). No tenant filter in the original query either -- see class
        docstring on ``_cross_tenant_scope``.
        """
        from app.db.database import get_db
        from typing import cast
        import json

        db = get_db()

        def _fetch_rows() -> list[dict[str, Any]]:
            start_offset = request.start_offset if request.start_offset > 0 else 0
            with _cross_tenant_scope():
                return cast(
                    "list[dict[str, Any]]",
                    db.executesql(
                        """
                        SELECT id, ts, cluster_id, tenant_id, actor_sub, actor_scope,
                               action, resource_kind, resource_id, before_json,
                               after_json, request_id, source_ip, user_agent
                        FROM audit_events
                        WHERE ts >= to_timestamp(%s)
                        ORDER BY ts ASC
                        """,
                        (start_offset,),
                        as_dict=True,
                    ),
                )

        try:
            rows = await asyncio.to_thread(_fetch_rows)

            for row in rows:
                payload = {
                    "cluster_id": row["cluster_id"],
                    "tenant_id": row["tenant_id"],
                    "actor_sub": row["actor_sub"],
                    "actor_scope": row["actor_scope"],
                    "action": row["action"],
                    "resource_kind": row["resource_kind"],
                    "resource_id": row["resource_id"],
                    "before_json": row["before_json"],
                    "after_json": row["after_json"],
                    "request_id": row["request_id"],
                    "source_ip": row["source_ip"],
                    "user_agent": row["user_agent"],
                }

                if request.filter:
                    try:
                        filter_obj = json.loads(request.filter)
                        after_json = payload.get("after_json") or {}
                        if isinstance(after_json, dict) and not all(
                            after_json.get(k) == v for k, v in filter_obj.items()
                        ):
                            continue
                    except (json.JSONDecodeError, TypeError):
                        pass

                yield audit_pb2.StreamResponse(
                    event_id=str(row["id"]),
                    event_payload=json.dumps(payload, default=str),
                    timestamp=int(row["ts"].timestamp()),
                )

        except Exception as exc:
            log.exception("Audit.Stream gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

    async def Verify(
        self,
        request: audit_pb2.VerifyRequest,
        context: grpc.aio.ServicerContext,
    ) -> audit_pb2.VerifyResponse:
        """Verify the audit hash chain has no breaks.

        ``verify_chain`` requires a real SQLAlchemy ``Session``
        (``app.security.audit_chain`` -- out of this conversion's scope,
        same boundary as ``AppendEvent`` above). Accessor fix only -- see
        ``AppendEvent`` docstring for why ``current_app.db_session`` never
        worked and what ``_get_db_session()`` still needs before this is a
        full functional fix.
        """
        from app.api.audit import _get_db_session
        from app.security.audit_chain import verify_chain

        try:
            db = _get_db_session()

            def _do_verify() -> dict:
                result = verify_chain(db, since=None, to=None)
                return result

            result = await asyncio.to_thread(_do_verify)
            verified = result["breaks"] == 0
            msg = "ok" if verified else f"Chain breaks: {result['breaks']}"

            return audit_pb2.VerifyResponse(
                verified=verified,
                verification_result=msg,
            )

        except Exception as exc:
            log.exception("Audit.Verify gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return audit_pb2.VerifyResponse()

    async def ExportRange(
        self,
        request: audit_pb2.ExportRangeRequest,
        context: grpc.aio.ServicerContext,
    ) -> audit_pb2.ExportRangeResponse:
        """Export audit events in a time range as newline-delimited JSON.

        No tenant filter in the original query -- see class docstring on
        ``_cross_tenant_scope``.
        """
        from app.db.database import get_db
        from typing import cast
        import json
        import uuid as _uuid

        db = get_db()

        def _do_export() -> bytes:
            with _cross_tenant_scope():
                rows = cast(
                    "list[dict[str, Any]]",
                    db.executesql(
                        """
                        SELECT id, ts, cluster_id, tenant_id, actor_sub, actor_scope, action,
                               resource_kind, resource_id, before_json, after_json, request_id,
                               source_ip, user_agent, prev_hash, hash, signature
                        FROM audit_events
                        WHERE ts BETWEEN to_timestamp(%s) AND to_timestamp(%s)
                        ORDER BY ts ASC
                        """,
                        (request.start_time, request.end_time),
                        as_dict=True,
                    ),
                )

            lines = []
            for row in rows:
                record = {
                    "id": str(row["id"]),
                    "ts": row["ts"].isoformat(),
                    "cluster_id": row["cluster_id"],
                    "tenant_id": row["tenant_id"],
                    "actor_sub": row["actor_sub"],
                    "actor_scope": row["actor_scope"],
                    "action": row["action"],
                    "resource_kind": row["resource_kind"],
                    "resource_id": row["resource_id"],
                    "before_json": row["before_json"],
                    "after_json": row["after_json"],
                    "request_id": row["request_id"],
                    "source_ip": row["source_ip"],
                    "user_agent": row["user_agent"],
                }
                lines.append(json.dumps(record))

            return "\n".join(lines).encode("utf-8")

        try:
            export_data = await asyncio.to_thread(_do_export)
            export_id = str(_uuid.uuid4())

            return audit_pb2.ExportRangeResponse(
                export_data=export_data,
                export_id=export_id,
            )

        except Exception as exc:
            log.exception("Audit.ExportRange gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return audit_pb2.ExportRangeResponse()


# ---------------------------------------------------------------------------
# Identity servicer
# ---------------------------------------------------------------------------


class IdentityServicer(identity_pb2_grpc.IdentityServicer):
    """gRPC Identity service — SVID issuance and OTPN verification."""

    async def IssueSVID(
        self,
        request: identity_pb2.IssueSVIDRequest,
        context: grpc.aio.ServicerContext,
    ) -> identity_pb2.IssueSVIDResponse:
        from cryptography import x509
        from cryptography.x509.oid import NameOID, ExtensionOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from datetime import datetime, timezone, timedelta
        import base64

        try:
            service_name = request.service_name or "gough-service"
            ttl_seconds = request.ttl_seconds if request.ttl_seconds > 0 else 3600

            def _issue_svid() -> tuple[str, str, int, str]:
                ca_cert_pem = os.getenv("GOUGH_CA_CERT_PEM")
                ca_key_pem = os.getenv("GOUGH_CA_KEY_PEM")

                if request.csr_pem:
                    csr = x509.load_pem_x509_csr(
                        request.csr_pem.encode(), backend=None
                    )
                    public_key = csr.public_key()
                else:
                    private_key = ec.generate_private_key(ec.SECP256R1())
                    public_key = private_key.public_key()

                if not ca_cert_pem or not ca_key_pem:
                    ca_private_key = ec.generate_private_key(ec.SECP256R1())
                    ca_public_key = ca_private_key.public_key()
                else:
                    ca_private_key = serialization.load_pem_private_key(
                        ca_key_pem.encode(), password=None, backend=None
                    )
                    ca_public_key = serialization.load_pem_public_key(
                        ca_cert_pem.encode(), backend=None
                    )

                now = datetime.now(timezone.utc)
                expires_at = now + timedelta(seconds=ttl_seconds)

                subject_name = x509.Name([
                    x509.NameAttribute(NameOID.COMMON_NAME, service_name)
                ])
                issuer_name = x509.Name([
                    x509.NameAttribute(NameOID.COMMON_NAME, "gough-ca")
                ])

                cert = x509.CertificateBuilder().subject_name(
                    subject_name
                ).issuer_name(
                    issuer_name
                ).public_key(
                    public_key
                ).serial_number(
                    x509.random_serial_number()
                ).not_valid_before(
                    now
                ).not_valid_after(
                    expires_at
                ).add_extension(
                    x509.SubjectAlternativeName([
                        x509.UniformResourceIdentifier(
                            f"spiffe://penguintech.io/gough/{service_name}"
                        )
                    ]),
                    critical=False,
                ).sign(ca_private_key, hashes.SHA256())

                cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()

                if request.csr_pem:
                    key_pem = ""
                else:
                    key_pem = private_key.private_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PrivateFormat.PKCS8,
                        encryption_algorithm=serialization.NoEncryption(),
                    ).decode()

                spiffe_id = f"spiffe://penguintech.io/gough/{service_name}"
                expires_ts = int(expires_at.timestamp())

                return cert_pem, key_pem, expires_ts, spiffe_id

            cert_pem, key_pem, expires_ts, spiffe_id = await asyncio.to_thread(_issue_svid)

            return identity_pb2.IssueSVIDResponse(
                certificate_pem=cert_pem,
                private_key_pem=key_pem,
                expires_at=expires_ts,
                spiffe_id=spiffe_id,
            )

        except Exception as exc:
            log.exception("Identity.IssueSVID gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return identity_pb2.IssueSVIDResponse()

    async def VerifyOTPN(
        self,
        request: identity_pb2.VerifyOTPNRequest,
        context: grpc.aio.ServicerContext,
    ) -> identity_pb2.VerifyOTPNResponse:
        """Verify a one-time-per-node secret and return its decrypted plaintext.

        No tenant claim on this trust boundary (a node verifying its own
        OTPN, before it has any other identity -- see
        ``JoinerSecretsServicer`` class docstring for the same reasoning);
        reads under ``_cross_tenant_scope()``. Preserves the original's
        "return ``valid=False`` for every unverifiable case, never a gRPC
        error" design -- not-found, revoked, malformed ``node_id``, and
        decrypt failure are all folded into the same false result rather
        than distinguishable abort codes, which avoids giving a caller an
        oracle on *why* verification failed.
        """
        from app.db.database import get_db
        from app.security.joiner_envelope import EnvelopeCiphertext, decrypt_envelope
        from quart import current_app

        try:
            db = get_db()
            vault_client = current_app.config.get("VAULT_CLIENT")

            def _verify_otp() -> tuple[bool, bytes, str, str]:
                try:
                    node_id = int(request.node_id)
                except (TypeError, ValueError):
                    return False, b"", "", ""

                with _cross_tenant_scope():
                    row = (
                        db(
                            (db.joiner_secrets.emitter_node_id == node_id)
                            & (db.joiner_secrets.rotation_class == request.rotation_class)
                        )
                        .select()
                        .first()
                    )

                if row is None:
                    return False, b"", "", ""

                if row.revoked_at is not None:
                    return False, b"", "", ""

                envelope = EnvelopeCiphertext(
                    ciphertext=bytes(row.ciphertext),
                    iv=bytes(row.iv),
                    auth_tag=bytes(row.auth_tag),
                    dek_wrapped=bytes(row.dek_wrapped),
                    vault_kek_name=row.vault_kek_name,
                )

                try:
                    plaintext = decrypt_envelope(
                        envelope, vault_client, joiner_secret_id=row.id
                    )
                except Exception as e:
                    log.exception("Failed to decrypt envelope: %s", e)
                    return False, b"", "", ""

                return True, plaintext, row.rotation_class or "", str(request.node_id)

            valid, secret_bytes, rotation_class, node_id = await asyncio.to_thread(_verify_otp)

            return identity_pb2.VerifyOTPNResponse(
                valid=valid,
                decrypted_secret=secret_bytes,
                rotation_class=rotation_class,
                node_id=node_id,
            )

        except Exception as exc:
            log.exception("Identity.VerifyOTPN gRPC handler failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return identity_pb2.VerifyOTPNResponse()
