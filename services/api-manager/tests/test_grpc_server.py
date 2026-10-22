"""Real-Postgres tests for ``app.grpc_server`` (JoinerSecretsServicer,
AuditServicer, IdentityServicer.VerifyOTPN, BiomesServicer.DeployBiome).

Covers the penguin-dal conversion of these servicers: filter + empty-result
cases for reads, the atomic revoke+persist transaction for ``Rotate``, and a
dedicated RLS proof that ``_cross_tenant_scope()`` -- not an app-level filter
-- is what makes these gRPC methods see rows at all under Row Level
Security (see ``app.grpc_server._cross_tenant_scope`` docstring).

``app.grpc_server`` imports ``gough.identity_pb2`` at module level (for
``IdentityServicer``). That generated file was hand-fabricated rather than
buf-generated and its embedded descriptor was internally inconsistent
("TypeError: Couldn't parse file content!") -- this masked the fact that
every servicer in this module, not just Identity, was silently failing to
start in every deployment (``app/__init__.py``'s ``_start_grpc`` swallowed
the whole ``grpc_runner`` import into a warning log). Fixed by regenerating
all stubs from ``proto/v1/gough/*.proto`` via ``buf generate`` (gh-22); the
real ``gough.identity_pb2``/``identity_pb2_grpc`` now import cleanly, so
this module imports ``app.grpc_server`` directly with no stand-in shim.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest
from quart import Quart

from app.grpc_server import (
    AuditServicer,
    BiomesServicer,
    IdentityServicer,
    JoinerSecretsServicer,
)
from app.security.joiner_envelope import EnvelopeCiphertext, encrypt_envelope

pytestmark = pytest.mark.asyncio


# =============================================================================
# Fake gRPC context
# =============================================================================


class _AbortCalled(Exception):
    """Raised by ``FakeGrpcContext.abort`` -- mirrors real ``grpc.aio``
    semantics (``ServicerContext.abort`` always raises to terminate the
    RPC; see its docstring: "Raises: Exception: An exception is always
    raised"). Tests assert on ``context.aborted`` after catching this (or
    letting it propagate, matching what a real caller would see)."""

    def __init__(self, code: Any, details: str) -> None:
        self.code = code
        self.details = details
        super().__init__(f"{code}: {details}")


class FakeGrpcContext:
    """Minimal stand-in for ``grpc.aio.ServicerContext``."""

    def __init__(self) -> None:
        self.aborted: tuple[Any, str] | None = None

    async def abort(self, code: Any, details: str = "") -> None:
        self.aborted = (code, details)
        raise _AbortCalled(code, details)


# =============================================================================
# App-context helper
# =============================================================================


def _make_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db: Any,
    vault_client: Any = None,
    material_provider: Any = None,
    cluster_id: str | None = None,
) -> Quart:
    """Build a bare Quart app, and point the servicers' DB accessor at ``db``.

    Regression: gh-22 (DB pool consolidation). Servicers used to resolve
    their DB via ``app.models.get_db()`` (``current_app.config["db"]``),
    which is why this helper used to set ``app.config["db"] = db`` and every
    call site wrapped the servicer call in ``async with app.app_context():``
    -- that combination is what made ``current_app.config.get("db")``
    resolve to ``db`` at all. Servicers now use
    ``app.db.database.get_db()`` instead (RLS-wired, app-context-free --
    see that module's docstring) precisely because the OLD assumption
    baked into this helper's previous docstring -- that a single
    app-context push around ``asyncio.ensure_future(grpc_runner.serve())``
    in ``app/__init__.py``'s ``_start_grpc`` stays "active" for every
    subsequent gRPC call for the life of the process -- was wrong: Quart
    pops that app context the moment the ``before_serving`` hook coroutine
    returns (immediately after scheduling the task), so every real
    servicer call ran with NO app context and would ``RuntimeError`` on
    ``app.models.get_db()`` (never caught here because this helper always
    supplied one). ``app.db.database.get_db()`` is thread-local, not
    request/app-context-scoped, so redirecting it to ``db`` requires
    patching the function itself (each servicer method does its own local
    ``from app.db.database import get_db``, a fresh attribute lookup on
    ``app.db.database`` every call -- patching that module's ``get_db``
    attribute is what every subsequent local import picks up, for the
    life of this test via ``monkeypatch``'s teardown).

    ``app.config["db"] = db`` and the ``app_context()`` wrap at call sites
    are kept (harmless, no longer load-bearing for these tests) rather than
    ripped out repo-wide -- any future servicer still using
    ``app.models.get_db()`` (e.g. a request-path helper reused as-is) would
    still need them.
    """
    import app.db.database as db_database_mod

    monkeypatch.setattr(db_database_mod, "get_db", lambda: db)

    app = Quart(__name__)
    app.config["db"] = db
    # CLUSTER_ID must be a valid UUID string -- app.grpc_server.JoinerSecretsServicer
    # .Emit/.Rotate parse it via uuid.UUID(...) (pre-existing, unchanged by this
    # conversion). Default to a fresh UUID rather than a human-readable label.
    app.config["CLUSTER_ID"] = cluster_id or str(uuid.uuid4())
    if vault_client is not None:
        app.config["VAULT_CLIENT"] = vault_client
    if material_provider is not None:
        app.config["JOINER_ROTATE_MATERIAL_PROVIDER"] = material_provider
    return app


@pytest.fixture
def mock_vault_client() -> Any:
    """Same round-trip mock as ``tests/workers/test_joiner_secret_emitter.py``."""
    client = Mock()
    client._wrapped_keys: dict[str, bytes] = {}

    def mock_encrypt(key_name: str, plaintext: bytes) -> Any:
        from app.clients.vault import VaultTransitEncryptResponse

        wrapped = f"vault:v1:{uuid.uuid4().hex}"
        client._wrapped_keys[wrapped] = plaintext
        return VaultTransitEncryptResponse(ciphertext=wrapped, key_version=1)

    def mock_decrypt(key_name: str, ciphertext: str) -> Any:
        from app.clients.vault import VaultTransitDecryptResponse

        if ciphertext not in client._wrapped_keys:
            raise ValueError(f"Unknown wrapped key: {ciphertext}")
        return VaultTransitDecryptResponse(plaintext=client._wrapped_keys[ciphertext])

    client.transit_encrypt = mock_encrypt
    client.transit_decrypt = mock_decrypt
    return client


# =============================================================================
# Seed helpers (real penguin-dal inserts -- pg_db is the owner role, RLS-exempt)
# =============================================================================


def _seed_node(dal_db: Any, **overrides: Any) -> int:
    """created_at/updated_at have no server-side DEFAULT (ORM-side
    ``default=`` only) -- supplied explicitly, matching every other
    direct-insert seed helper in this test suite (see
    ``tests/api/test_primary_dal_conversion.py``)."""
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        name=f"node-{uuid.uuid4().hex[:8]}",
        state="ready",
        tenant_id="acme",
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.nodes.insert(**base))


def _seed_biome(dal_db: Any, **overrides: Any) -> int:
    base: dict[str, Any] = dict(name=f"biome-{uuid.uuid4().hex[:8]}", tenant_id="acme")
    base.update(overrides)
    return int(dal_db.biomes.insert(**base))


def _seed_assignment(dal_db: Any, *, node_id: int, egg_id: int, **overrides: Any) -> int:
    """``phase``/``assigned_at``/``created_at``/``updated_at`` are all NOT
    NULL with no (or Python-only) defaults -- supplied explicitly."""
    now = datetime.now(timezone.utc)
    base: dict[str, Any] = dict(
        node_id=node_id,
        egg_id=egg_id,
        tenant_id="acme",
        phase="post_deploy",
        status="ready",
        assigned_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return int(dal_db.node_egg_assignments.insert(**base))


def _seed_joiner_secret(
    dal_db: Any,
    *,
    vault_client: Any,
    plaintext: bytes = b"secret-plaintext",
    emitter_biome_id: int | None = None,
    **overrides: Any,
) -> tuple[str, bytes]:
    """Insert a real, genuinely-decryptable ``joiner_secrets`` row.

    Returns ``(id, plaintext)`` so callers can assert Consume/VerifyOTPN
    return the exact bytes that went in -- proving real decryption, not
    just "a response came back".
    """
    if emitter_biome_id is None:
        emitter_biome_id = _seed_biome(dal_db)
    envelope: EnvelopeCiphertext = encrypt_envelope(
        plaintext=plaintext, vault_client=vault_client, vault_kek_name="gough-joiner-dek-wrap"
    )
    base: dict[str, Any] = dict(
        id=str(uuid.uuid4()),
        cluster_id=str(uuid.uuid4()),
        tenant_id="acme",
        biome_kind="vault",
        emitter_biome_id=emitter_biome_id,
        emitter_node_id=None,
        extractor_name="root-token",
        scope="cluster",
        ciphertext=envelope.ciphertext,
        iv=envelope.iv,
        auth_tag=envelope.auth_tag,
        dek_wrapped=envelope.dek_wrapped,
        vault_kek_name=envelope.vault_kek_name,
        ttl_seconds=3600,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        rotation_class="vault-unseal",
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    base["id"] = str(base["id"])
    base["cluster_id"] = str(base["cluster_id"])
    new_id = str(dal_db.joiner_secrets.insert(**base))
    return new_id, plaintext


def _seed_audit_event(dal_db: Any, **overrides: Any) -> str:
    """Matches ``tests/api/test_audit.py``'s helper of the same name."""
    base: dict[str, Any] = dict(
        id=str(uuid.uuid4()),
        ts=datetime.now(timezone.utc),
        cluster_id="test-cluster",
        tenant_id="acme",
        actor_sub="alice@acme",
        actor_scope=["gough.audit.read"],
        action="joiner.secret.emit",
        resource_kind="joiner_secret",
        resource_id=str(uuid.uuid4()),
        prev_hash=b"\x00" * 32,
        hash=b"\x11" * 32,
    )
    base.update(overrides)
    base["id"] = str(base["id"])
    return str(dal_db.audit_events.insert(**base))


# =============================================================================
# JoinerSecretsServicer.Emit
# =============================================================================


class TestEmit:
    async def test_emit_persists_secret_for_ready_node(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db, biome_kind="k8s-primary", emits_joiner_secrets=True)
        _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id, status="ready")

        material = Mock()
        material.extractor_name = "grpc-emit"
        material.plaintext = b"fresh-token"
        material.ttl_seconds = 3600
        material.rotation_class = "single_use"
        provider = Mock(return_value=material)

        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client, material_provider=provider)
        request = Mock(node_id=str(node_id), rotation_class="k8s-primary", key_version=1)
        context = FakeGrpcContext()

        async with app.app_context():
            response = await JoinerSecretsServicer().Emit(request, context)

        assert context.aborted is None
        assert response.secret_id
        row = pg_db(pg_db.joiner_secrets.id == response.secret_id).select().first()
        assert row is not None
        assert row.emitter_biome_id == biome_id
        assert row.tenant_id == "acme"

    async def test_emit_skips_non_emitting_biome_when_multiple_ready(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        """Regression (fix round 1): a node with two ``ready`` assignments --
        one pointing at a non-emitting biome, one at an emitting biome --
        must attribute the secret to the EMITTING biome, never whichever
        assignment happens to be most recent. Parity with
        ``JoinerSecretEmitter._load_context``'s ``emits_joiner_secrets``
        enforcement."""
        node_id = _seed_node(pg_db)
        non_emitting_id = _seed_biome(
            pg_db, biome_kind="sidecar", emits_joiner_secrets=False
        )
        emitting_id = _seed_biome(
            pg_db, biome_kind="k8s-primary", emits_joiner_secrets=True
        )
        # Non-emitting assignment inserted (and thus higher id) AFTER the
        # emitting one, so a naive "most recent ready row" pick would
        # select the wrong (non-emitting) biome.
        _seed_assignment(pg_db, node_id=node_id, egg_id=emitting_id, status="ready")
        _seed_assignment(pg_db, node_id=node_id, egg_id=non_emitting_id, status="ready")

        material = Mock()
        material.extractor_name = "grpc-emit"
        material.plaintext = b"fresh-token"
        material.ttl_seconds = 3600
        material.rotation_class = "single_use"
        provider = Mock(return_value=material)

        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client, material_provider=provider)
        request = Mock(node_id=str(node_id), rotation_class="k8s-primary", key_version=1)
        context = FakeGrpcContext()

        async with app.app_context():
            response = await JoinerSecretsServicer().Emit(request, context)

        assert context.aborted is None
        row = pg_db(pg_db.joiner_secrets.id == response.secret_id).select().first()
        assert row is not None
        assert row.emitter_biome_id == emitting_id

    async def test_emit_not_found_when_ready_assignment_biome_does_not_emit(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        """Regression (fix round 1): a node whose only ``ready`` assignment
        points at a non-emitting biome must abort NOT_FOUND, the same as
        having no ready assignment at all -- never persist a secret
        attributed to a biome that doesn't emit joiner secrets."""
        node_id = _seed_node(pg_db)
        biome_id = _seed_biome(pg_db, biome_kind="sidecar", emits_joiner_secrets=False)
        _seed_assignment(pg_db, node_id=node_id, egg_id=biome_id, status="ready")

        provider = Mock()
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client, material_provider=provider)
        request = Mock(node_id=str(node_id), rotation_class="sidecar", key_version=1)
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await JoinerSecretsServicer().Emit(request, context)

        assert context.aborted is not None
        code, _details = context.aborted
        assert "NOT_FOUND" in str(code)
        provider.assert_not_called()

    async def test_emit_not_found_when_no_ready_assignment(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        node_id = _seed_node(pg_db)
        provider = Mock()
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client, material_provider=provider)
        request = Mock(node_id=str(node_id), rotation_class="k8s-primary", key_version=1)
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await JoinerSecretsServicer().Emit(request, context)

        assert context.aborted is not None
        code, _details = context.aborted
        assert "NOT_FOUND" in str(code)
        provider.assert_not_called()

    async def test_emit_invalid_argument_on_bad_node_id(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client, material_provider=Mock())
        request = Mock(node_id="not-an-int", rotation_class="x", key_version=1)
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await JoinerSecretsServicer().Emit(request, context)

        code, _ = context.aborted
        assert "INVALID_ARGUMENT" in str(code)


# =============================================================================
# JoinerSecretsServicer.Consume
# =============================================================================


class TestConsume:
    async def test_consume_returns_real_plaintext(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        secret_id, plaintext = _seed_joiner_secret(
            pg_db, vault_client=mock_vault_client, plaintext=b"the-actual-secret"
        )
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client)
        request = Mock(secret_id=secret_id, node_id="")
        context = FakeGrpcContext()

        async with app.app_context():
            response = await JoinerSecretsServicer().Consume(request, context)

        assert context.aborted is None
        # The whole point of the fix: decrypted_secret must be the REAL
        # plaintext, never the raw envelope ciphertext (see docstring on
        # JoinerSecretsServicer.Consume).
        assert response.decrypted_secret == plaintext
        assert response.decrypted_secret != b""

    async def test_consume_not_found(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any) -> None:
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client)
        request = Mock(secret_id=str(uuid.uuid4()), node_id="")
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await JoinerSecretsServicer().Consume(request, context)

        code, _ = context.aborted
        assert "NOT_FOUND" in str(code)

    async def test_consume_revoked_denied(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any) -> None:
        secret_id, _ = _seed_joiner_secret(
            pg_db, vault_client=mock_vault_client, revoked_at=datetime.now(timezone.utc)
        )
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client)
        request = Mock(secret_id=secret_id, node_id="")
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await JoinerSecretsServicer().Consume(request, context)

        code, _ = context.aborted
        assert "PERMISSION_DENIED" in str(code)


# =============================================================================
# JoinerSecretsServicer.Rotate
# =============================================================================


class TestRotate:
    async def test_rotate_atomic_success(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any) -> None:
        secret_id, _ = _seed_joiner_secret(pg_db, vault_client=mock_vault_client)

        material = Mock()
        material.extractor_name = "root-token"
        material.plaintext = b"new-material"
        material.ttl_seconds = 3600
        material.rotation_class = "vault-unseal"
        provider = Mock(return_value=material)

        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client, material_provider=provider)
        request = Mock(secret_id=secret_id, new_key_version=2)
        context = FakeGrpcContext()

        async with app.app_context():
            response = await JoinerSecretsServicer().Rotate(request, context)

        assert context.aborted is None
        assert response.new_secret_id
        assert response.new_secret_id != secret_id

        old_row = pg_db(pg_db.joiner_secrets.id == secret_id).select().first()
        new_row = pg_db(pg_db.joiner_secrets.id == response.new_secret_id).select().first()
        assert old_row.revoked_at is not None
        assert new_row is not None
        assert new_row.revoked_at is None

    async def test_rotate_rollback_on_persist_failure(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        """Forced Vault failure mid-transaction must NOT leave the old
        secret revoked with nothing to replace it (same regression class
        task 6a's fix-round-1 guarded for ``rotate_joiner_secret``)."""
        secret_id, _ = _seed_joiner_secret(pg_db, vault_client=mock_vault_client)

        material = Mock()
        material.extractor_name = "root-token"
        material.plaintext = b"new-material"
        material.ttl_seconds = 3600
        material.rotation_class = "vault-unseal"
        provider = Mock(return_value=material)

        failing_vault = Mock()
        failing_vault.transit_encrypt = Mock(side_effect=Exception("Vault sealed"))

        app = _make_app(monkeypatch, db=pg_db, vault_client=failing_vault, material_provider=provider)
        request = Mock(secret_id=secret_id, new_key_version=2)
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await JoinerSecretsServicer().Rotate(request, context)

        code, _ = context.aborted
        assert "INTERNAL" in str(code)
        old_row = pg_db(pg_db.joiner_secrets.id == secret_id).select().first()
        assert old_row.revoked_at is None, "revoke must roll back with the failed persist"

    async def test_rotate_not_found(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any) -> None:
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client, material_provider=Mock())
        request = Mock(secret_id=str(uuid.uuid4()), new_key_version=1)
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await JoinerSecretsServicer().Rotate(request, context)

        code, _ = context.aborted
        assert "NOT_FOUND" in str(code)

    async def test_rotate_already_revoked(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any) -> None:
        secret_id, _ = _seed_joiner_secret(
            pg_db, vault_client=mock_vault_client, revoked_at=datetime.now(timezone.utc)
        )
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client, material_provider=Mock())
        request = Mock(secret_id=secret_id, new_key_version=1)
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await JoinerSecretsServicer().Rotate(request, context)

        code, _ = context.aborted
        assert "FAILED_PRECONDITION" in str(code)


# =============================================================================
# JoinerSecretsServicer.Revoke
# =============================================================================


class TestRevoke:
    async def test_revoke_marks_row(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any) -> None:
        secret_id, _ = _seed_joiner_secret(pg_db, vault_client=mock_vault_client)
        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(secret_id=secret_id)
        context = FakeGrpcContext()

        async with app.app_context():
            response = await JoinerSecretsServicer().Revoke(request, context)

        assert context.aborted is None
        assert response.revoked is True
        row = pg_db(pg_db.joiner_secrets.id == secret_id).select().first()
        assert row.revoked_at is not None

    async def test_revoke_already_revoked_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        secret_id, _ = _seed_joiner_secret(
            pg_db, vault_client=mock_vault_client, revoked_at=datetime.now(timezone.utc)
        )
        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(secret_id=secret_id)
        context = FakeGrpcContext()

        async with app.app_context():
            response = await JoinerSecretsServicer().Revoke(request, context)

        assert context.aborted is None
        assert response.revoked is False

    async def test_revoke_not_found(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(secret_id=str(uuid.uuid4()))
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await JoinerSecretsServicer().Revoke(request, context)

        code, _ = context.aborted
        assert "NOT_FOUND" in str(code)


# =============================================================================
# JoinerSecretsServicer.List
# =============================================================================


class TestList:
    async def test_list_filters_by_node_and_paginates(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        node_id = _seed_node(pg_db)
        other_node_id = _seed_node(pg_db)
        _seed_joiner_secret(pg_db, vault_client=mock_vault_client, emitter_node_id=node_id)
        _seed_joiner_secret(pg_db, vault_client=mock_vault_client, emitter_node_id=node_id)
        _seed_joiner_secret(
            pg_db, vault_client=mock_vault_client, emitter_node_id=other_node_id
        )

        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(node_id=str(node_id), limit=1, cursor="")
        context = FakeGrpcContext()

        async with app.app_context():
            first_page = await JoinerSecretsServicer().List(request, context)

        assert context.aborted is None
        assert len(first_page.secrets) == 1
        assert first_page.next_cursor
        assert all(s.node_id == str(node_id) for s in first_page.secrets)

    async def test_list_empty_result(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        node_id = _seed_node(pg_db)
        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(node_id=str(node_id), limit=50, cursor="")
        context = FakeGrpcContext()

        async with app.app_context():
            response = await JoinerSecretsServicer().List(request, context)

        assert context.aborted is None
        assert list(response.secrets) == []
        assert response.next_cursor == ""

    async def test_list_invalid_node_id(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(node_id="not-an-int", limit=50, cursor="")
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await JoinerSecretsServicer().List(request, context)

        code, _ = context.aborted
        assert "INVALID_ARGUMENT" in str(code)


# =============================================================================
# AuditServicer.Stream / ExportRange
# =============================================================================


class TestAuditStream:
    async def test_stream_yields_events_since_offset(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        old_ts = datetime.now(timezone.utc) - timedelta(days=1)
        new_ts = datetime.now(timezone.utc)
        _seed_audit_event(pg_db, ts=old_ts, action="stale")
        _seed_audit_event(pg_db, ts=new_ts, action="fresh")

        app = _make_app(monkeypatch, db=pg_db)
        cutoff = int((new_ts - timedelta(seconds=1)).timestamp())
        request = Mock(start_offset=cutoff, filter="")
        context = FakeGrpcContext()

        async with app.app_context():
            results = [r async for r in AuditServicer().Stream(request, context)]

        assert context.aborted is None
        assert len(results) == 1
        payload = json.loads(results[0].event_payload)
        assert payload["action"] == "fresh"

    async def test_stream_applies_after_json_filter(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        _seed_audit_event(
            pg_db, action="match", after_json={"biome_kind": "vault"}
        )
        _seed_audit_event(
            pg_db, action="no-match", after_json={"biome_kind": "other"}
        )

        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(
            start_offset=0, filter=json.dumps({"biome_kind": "vault"})
        )
        context = FakeGrpcContext()

        async with app.app_context():
            results = [r async for r in AuditServicer().Stream(request, context)]

        assert [json.loads(r.event_payload)["action"] for r in results] == ["match"]

    async def test_stream_empty_result(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(start_offset=int(datetime.now(timezone.utc).timestamp()) + 3600, filter="")
        context = FakeGrpcContext()

        async with app.app_context():
            results = [r async for r in AuditServicer().Stream(request, context)]

        assert results == []


class TestAuditExportRange:
    async def test_export_range_returns_jsonl(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        now = datetime.now(timezone.utc)
        _seed_audit_event(pg_db, ts=now, action="in-range")
        _seed_audit_event(pg_db, ts=now - timedelta(days=2), action="out-of-range")

        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(
            start_time=int((now - timedelta(hours=1)).timestamp()),
            end_time=int((now + timedelta(hours=1)).timestamp()),
            format="jsonl",
        )
        context = FakeGrpcContext()

        async with app.app_context():
            response = await AuditServicer().ExportRange(request, context)

        assert context.aborted is None
        lines = response.export_data.decode("utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["action"] == "in-range"

    async def test_export_range_empty_result(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        now = datetime.now(timezone.utc)
        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(
            start_time=int(now.timestamp()) + 3600,
            end_time=int(now.timestamp()) + 7200,
            format="jsonl",
        )
        context = FakeGrpcContext()

        async with app.app_context():
            response = await AuditServicer().ExportRange(request, context)

        assert response.export_data == b""


# =============================================================================
# BiomesServicer.DeployBiome
# =============================================================================


class TestDeployBiome:
    """Regression (gh-21): ``DeployBiome`` used to insert into
    ``db.node_biome_assignments``, a table that exists nowhere -- the
    ``hasattr`` guard in front of it was always False, so the insert
    unconditionally raised ``RuntimeError`` and every call aborted INTERNAL.
    These prove the real ``node_egg_assignments`` row actually lands, with
    the correct physical column mapping (``egg_id``, not ``biome_id``)."""

    async def test_deploy_biome_persists_assignment_row(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        node_id = _seed_node(pg_db, tenant_id="acme")
        biome_id = _seed_biome(pg_db, tenant_id="acme", phase="pre_deploy")

        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(biome_id=biome_id, node_id=node_id, config="", params={})
        context = FakeGrpcContext()

        async with app.app_context():
            response = await BiomesServicer().DeployBiome(request, context)

        assert context.aborted is None
        assert response.status == "pending"
        assert response.deployment_id

        row = (
            pg_db(pg_db.node_egg_assignments.id == int(response.deployment_id))
            .select()
            .first()
        )
        assert row is not None
        assert row.node_id == node_id
        assert row.egg_id == biome_id
        assert row.tenant_id == "acme"
        assert row.phase == "pre_deploy"
        assert row.status == "pending"
        assert row.assigned_at is not None
        assert row.created_at is not None
        assert row.updated_at is not None

    async def test_deploy_biome_defaults_phase_when_biome_has_none(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any
    ) -> None:
        """``phase`` is NOT NULL with no server default on the real table --
        falls back to ``post_deploy`` when the biome row's own phase is
        falsy, matching ``app.api.nodes.deploy_node``'s established
        convention."""
        node_id = _seed_node(pg_db, tenant_id="acme")
        biome_id = _seed_biome(pg_db, tenant_id="acme")
        pg_db(pg_db.biomes.id == biome_id).update(phase="")
        pg_db.commit()

        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(biome_id=biome_id, node_id=node_id, config="", params={})
        context = FakeGrpcContext()

        async with app.app_context():
            response = await BiomesServicer().DeployBiome(request, context)

        assert context.aborted is None
        row = (
            pg_db(pg_db.node_egg_assignments.id == int(response.deployment_id))
            .select()
            .first()
        )
        assert row.phase == "post_deploy"

    async def test_deploy_biome_not_found_biome(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        node_id = _seed_node(pg_db)
        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(biome_id=999999, node_id=node_id, config="", params={})
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await BiomesServicer().DeployBiome(request, context)

        code, _ = context.aborted
        assert "NOT_FOUND" in str(code)

    async def test_deploy_biome_not_found_node(self, monkeypatch: pytest.MonkeyPatch, pg_db: Any) -> None:
        biome_id = _seed_biome(pg_db)
        app = _make_app(monkeypatch, db=pg_db)
        request = Mock(biome_id=biome_id, node_id=999999, config="", params={})
        context = FakeGrpcContext()

        async with app.app_context():
            with pytest.raises(_AbortCalled):
                await BiomesServicer().DeployBiome(request, context)

        code, _ = context.aborted
        assert "NOT_FOUND" in str(code)


# =============================================================================
# IdentityServicer.VerifyOTPN
# =============================================================================


class TestVerifyOTPN:
    async def test_verify_otpn_success_returns_real_plaintext(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        node_id = _seed_node(pg_db)
        _seed_joiner_secret(
            pg_db,
            vault_client=mock_vault_client,
            plaintext=b"otpn-material",
            emitter_node_id=node_id,
            rotation_class="single_use",
        )

        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client)
        request = Mock(node_id=str(node_id), otp_token="unused", rotation_class="single_use")
        context = FakeGrpcContext()

        async with app.app_context():
            response = await IdentityServicer().VerifyOTPN(request, context)

        assert context.aborted is None
        assert response.valid is True
        assert response.decrypted_secret == b"otpn-material"

    async def test_verify_otpn_not_found_returns_false_not_error(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        node_id = _seed_node(pg_db)
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client)
        request = Mock(node_id=str(node_id), otp_token="x", rotation_class="single_use")
        context = FakeGrpcContext()

        async with app.app_context():
            response = await IdentityServicer().VerifyOTPN(request, context)

        assert context.aborted is None
        assert response.valid is False
        assert response.decrypted_secret == b""

    async def test_verify_otpn_revoked_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        node_id = _seed_node(pg_db)
        _seed_joiner_secret(
            pg_db,
            vault_client=mock_vault_client,
            emitter_node_id=node_id,
            rotation_class="single_use",
            revoked_at=datetime.now(timezone.utc),
        )
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client)
        request = Mock(node_id=str(node_id), otp_token="x", rotation_class="single_use")
        context = FakeGrpcContext()

        async with app.app_context():
            response = await IdentityServicer().VerifyOTPN(request, context)

        assert response.valid is False

    async def test_verify_otpn_invalid_node_id_returns_false_not_error(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        app = _make_app(monkeypatch, db=pg_db, vault_client=mock_vault_client)
        request = Mock(node_id="not-an-int", otp_token="x", rotation_class="single_use")
        context = FakeGrpcContext()

        async with app.app_context():
            response = await IdentityServicer().VerifyOTPN(request, context)

        assert context.aborted is None
        assert response.valid is False


# =============================================================================
# RLS proof: _cross_tenant_scope is what makes these RPCs see rows at all
# =============================================================================


class TestCrossTenantScopeRLS:
    async def test_consume_sees_row_under_scoped_role_only_via_cross_tenant_scope(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, pg_db_scoped: Any, mock_vault_client: Any
    ) -> None:
        """Proves ``_cross_tenant_scope()`` -- not an app-level filter, since
        Consume has none -- is what lets the scoped ``api-manager-rw`` role
        see this row at all under RLS.

        Negative control first (RLS genuinely active for this role, absent
        the scope): a direct scoped-role read with no tenant ContextVar set
        must return zero rows (fail closed, same proof style as
        ``tests/test_rls_isolation.py``). Positive: the real servicer
        method, which does apply the scope internally, finds it.
        """
        from app.db.rls import install_rls_events

        secret_id, plaintext = _seed_joiner_secret(
            pg_db, vault_client=mock_vault_client, tenant_id="acme"
        )
        install_rls_events(pg_db_scoped.engine)

        # Negative control: no tenant context set at all on this task ->
        # fail-closed sentinel -> RLS must filter this out for the scoped role.
        direct = pg_db_scoped(pg_db_scoped.joiner_secrets.id == secret_id).select().first()
        assert direct is None, "RLS did not filter the scoped role -- test is not proving anything"

        # Positive: the real servicer path applies _cross_tenant_scope() itself.
        app = _make_app(monkeypatch, db=pg_db_scoped, vault_client=mock_vault_client)
        request = Mock(secret_id=secret_id, node_id="")
        context = FakeGrpcContext()

        async with app.app_context():
            response = await JoinerSecretsServicer().Consume(request, context)

        assert context.aborted is None
        assert response.decrypted_secret == plaintext


# =============================================================================
# Regression: gh-22 -- servicer DB path works with NO Quart app context
# =============================================================================


class TestNoAppContextRequired:
    """The bug this whole conversion exists to fix: gRPC servicers run as a
    background ``asyncio.ensure_future()`` task (see
    ``app/__init__.py``'s ``_start_grpc``), which has NO Quart app context
    once the ``before_serving`` hook that scheduled it returns -- every
    servicer call in a real deployment ran (and, before the gRPC-stub-import
    fix landed in the same gh-22 task, would have run) with no app context
    at all. Every OTHER test in this file wraps its servicer call in
    ``async with app.app_context():`` -- harmless now, but it would have
    silently hidden this exact bug (``app.models.get_db()`` needs an app
    context; ``async with app.app_context():`` always supplies one, so a
    suite built entirely that way could never have caught servicers
    depending on it). This test is the one that actually proves the fix:
    no app, no app context, real Postgres.
    """

    async def test_list_secrets_succeeds_with_no_app_context_against_real_pg(
        self, monkeypatch: pytest.MonkeyPatch, pg_db: Any, mock_vault_client: Any
    ) -> None:
        """``JoinerSecretsServicer.List`` -- chosen because, unlike
        ``Emit``/``Consume``/``Rotate``, it touches no Vault-provided
        material and no ``CLUSTER_ID`` parsing, isolating the assertion to
        exactly the thing under test: the DB accessor itself.
        """
        from quart import has_app_context

        import app.db.database as db_database_mod

        node_id = _seed_node(pg_db)
        _seed_joiner_secret(pg_db, vault_client=mock_vault_client, emitter_node_id=node_id)

        monkeypatch.setattr(db_database_mod, "get_db", lambda: pg_db)

        assert not has_app_context(), "sanity: genuinely no app context in this test"

        request = Mock(node_id=str(node_id), limit=50, cursor="")
        context = FakeGrpcContext()

        # No `async with app.app_context():` anywhere -- the whole point.
        response = await JoinerSecretsServicer().List(request, context)

        assert context.aborted is None
        assert len(response.secrets) == 1
        assert response.secrets[0].node_id == str(node_id)
