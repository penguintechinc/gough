"""Tests for ``app.api.joiner_secrets`` blueprint.

Covers:
- list/rotate/revoke happy paths
- response payload contract: NEVER ciphertext, dek_wrapped, iv, auth_tag
- tenant scoping (cross-tenant denied via filter)
- scope enforcement matrix
- MFA enforcement in compliance lanes
- not_found, already_revoked, missing_reason
- expiring-soon / active / revoked status filter

All DB and Vault interactions are mocked; the blueprint is exercised under a
ephemeral Quart app whose ``DB_SESSION_FACTORY`` returns a stub session.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
from quart import Quart, g

from app.api import joiner_secrets as joiner_module
from app.api.joiner_secrets import (
    DEFAULT_MFA_REQUIRED_LANES,
    EXPIRING_SOON_WINDOW,
    _serialize_joiner_secret,
    joiner_secrets_bp,
)
from app.security.credentials import CredentialType, Principal
from app.security.tenant import TenantContext
from app.workers.joiner_secret_emitter import ExtractedMaterial

SECRET_FIELDS_MUST_BE_ABSENT: frozenset[str] = frozenset(
    {"ciphertext", "dek_wrapped", "iv", "auth_tag"}
)


# =============================================================================
# Helpers / fixtures
# =============================================================================


@dataclass
class FakeJoinerSecret:
    """Mimics enough of the JoinerSecret ORM row for serialization tests."""

    id: uuid.UUID
    cluster_id: uuid.UUID
    tenant_id: str
    biome_kind: str
    emitter_biome_id: int
    emitter_node_id: Optional[int]
    extractor_name: str
    scope: str
    ciphertext: bytes
    iv: bytes
    auth_tag: bytes
    dek_wrapped: bytes
    vault_kek_name: str
    ttl_seconds: Optional[int]
    expires_at: Optional[datetime]
    rotation_class: Optional[str]
    created_at: datetime
    rotated_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    audit_event_id: Optional[uuid.UUID] = None


def _make_secret(**overrides: Any) -> FakeJoinerSecret:
    base = dict(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        tenant_id="acme",
        biome_kind="vault",
        emitter_biome_id=42,
        emitter_node_id=7,
        extractor_name="root-token",
        scope="cluster",
        ciphertext=b"CT-PLAINTEXT-MUST-NEVER-LEAK",
        iv=b"IIIIIIIIIIII",
        auth_tag=b"TTTTTTTTTTTTTTTT",
        dek_wrapped=b"vault:v1:DEK-MUST-NEVER-LEAK",
        vault_kek_name="gough-joiner-dek-wrap",
        ttl_seconds=3600,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        rotation_class="vault-unseal",
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return FakeJoinerSecret(**base)


def _seed_biome(dal_db: Any, **overrides: Any) -> int:
    """Insert a minimal real ``biomes`` row; ``joiner_secrets.emitter_biome_id``
    is a NOT NULL FK to it."""
    base: dict[str, Any] = dict(name="test-biome")
    base.update(overrides)
    return int(dal_db.biomes.insert(**base))


def _seed_joiner_secret(
    dal_db: Any, *, emitter_biome_id: Optional[int] = None, **overrides: Any
) -> str:
    """Insert a real ``joiner_secrets`` row via penguin-dal; return its id (str).

    Used by tests exercising the penguin-dal-converted read/write paths
    (``list_joiner_secrets`` / the lookup+revoke-mark in ``rotate_``/
    ``revoke_joiner_secret``), as opposed to ``_make_secret``, which only
    builds an in-memory stand-in for pure-serializer tests.
    """
    if emitter_biome_id is None:
        emitter_biome_id = _seed_biome(dal_db)
    base: dict[str, Any] = dict(
        id=str(uuid.uuid4()),
        cluster_id=str(uuid.uuid4()),
        tenant_id="acme",
        biome_kind="vault",
        emitter_biome_id=emitter_biome_id,
        emitter_node_id=None,
        extractor_name="root-token",
        scope="cluster",
        ciphertext=b"CT-PLAINTEXT-MUST-NEVER-LEAK",
        iv=b"IIIIIIIIIIII",
        auth_tag=b"TTTTTTTTTTTTTTTT",
        dek_wrapped=b"vault:v1:DEK-MUST-NEVER-LEAK",
        vault_kek_name="gough-joiner-dek-wrap",
        ttl_seconds=3600,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        rotation_class="vault-unseal",
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    base["id"] = str(base["id"])
    base["cluster_id"] = str(base["cluster_id"])
    return str(dal_db.joiner_secrets.insert(**base))


class FakeQuery:
    """Minimal SQLAlchemy-style query stub backed by a list."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def filter(self, *_: Any) -> "FakeQuery":
        return self

    def order_by(self, *_: Any) -> "FakeQuery":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def one_or_none(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None

    def one(self) -> Any:
        return self._rows[0]


class _FakeTx:
    """Minimal penguin-dal ``Tx`` stand-in for ``AuditEventWriter.append()``
    (Task 8a -- ``append()`` now needs ``.transaction()``/``Tx.executesql()``,
    not a SQLAlchemy Session; see ``FakeSession`` docstring)."""

    def __init__(self, session: "FakeSession") -> None:
        self._session = session

    def executesql(self, query: str, placeholders: Any = None, **_: Any) -> Any:
        stripped = query.strip().upper()
        if stripped.startswith("SELECT"):
            return []
        self._session.inserted.append((query, placeholders))
        return None


class FakeSession:
    """Stand-in for SQLAlchemy session — captures inserts and returns rows.

    Also implements penguin-dal's ``DB.transaction()``/``Tx.executesql()``
    surface (``_FakeTx``) -- ``AuditEventWriter.append()`` (Task 8a) needs
    that API, not ``Session.execute()``/``.add()``/``.flush()``. A real
    object can't be both; this dual-interface test double can, since these
    tests only assert on the endpoint's response contract, not on the
    writer's own self-audit-log row.
    """

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.added: list[Any] = []
        self.flushed = 0
        self.inserted: list[tuple[str, Any]] = []

    def execute(self, *_: Any, **__: Any) -> Any:
        return MagicMock()

    def executesql(self, query: str, placeholders: Any = None, **kwargs: Any) -> Any:
        return _FakeTx(self).executesql(query, placeholders, **kwargs)

    @contextmanager
    def transaction(self) -> Iterator["_FakeTx"]:
        yield _FakeTx(self)

    def query(self, _model: Any) -> FakeQuery:
        return FakeQuery(self.rows)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushed += 1


def _build_app(
    *,
    rows: list[Any],
    principal: Principal,
    mfa_lane: Optional[str] = None,
    rotation_provider: Optional[Any] = None,
    dal_db: Optional[Any] = None,
    monkeypatch: Optional[pytest.MonkeyPatch] = None,
    rls_scoped: bool = False,
) -> Quart:
    """Build a Quart app with ``joiner_secrets_bp`` registered.

    ``rows``/``FakeSession``/``DB_SESSION_FACTORY``/``g.db_session`` are
    legacy scaffolding from before FIX #7a's cleanup -- ``AuditEventWriter``
    (via ``_build_audit_writer``) now gets its ``DB`` from ``get_db()`` like
    every other penguin-dal call in this module, so nothing in production
    code reads ``g.db_session``/``DB_SESSION_FACTORY`` anymore. Left wired
    here (harmless, unused) rather than ripped out as a separate cleanup.
    Pass ``dal_db`` (a real ``pg_db``) + ``monkeypatch`` together to patch
    ``joiner_module.get_db`` for the penguin-dal-backed
    lookup/list/revoke-mark/audit-write logic -- required for any test that
    reaches ``revoke_joiner_secret``'s audit-writer call.

    ``rls_scoped=True`` additionally wires ``app.db.rls``'s pool
    checkout/checkin GUC events onto ``dal_db.engine`` and pushes
    ``principal.tenant_id`` onto the ``set_current_tenant`` ContextVar for
    the duration of each request (mirroring the real ``tenant_middleware``),
    so RLS is genuinely enforced against ``dal_db`` -- pass a
    ``pg_db_scoped`` fixture (the actual non-owner ``api-manager-rw`` role)
    as ``dal_db`` when using this; ``pg_db`` (the migration/table owner) is
    RLS-exempt and would prove nothing.
    """
    app = Quart(__name__)
    app.register_blueprint(joiner_secrets_bp, url_prefix="/api/v1")

    if dal_db is not None:
        assert monkeypatch is not None, "dal_db requires monkeypatch"
        monkeypatch.setattr(joiner_module, "get_db", lambda: dal_db)

        if rls_scoped:
            from app.db.rls import install_rls_events, set_current_tenant

            install_rls_events(dal_db.engine)

            @app.before_request
            async def _push_rls_tenant() -> None:
                set_current_tenant(principal.tenant_id)

            @app.after_request
            async def _pop_rls_tenant(response: Any) -> Any:
                set_current_tenant(None)
                return response

    session = FakeSession(rows)

    vault_client = MagicMock()
    vault_client.transit_sign.return_value = "vault:v1:fake-sig"
    enc_resp = MagicMock()
    enc_resp.ciphertext = "vault:v1:dek-wrapped"
    vault_client.transit_encrypt.return_value = enc_resp

    app.config["DB_SESSION_FACTORY"] = lambda: session
    app.config["VAULT_CLIENT"] = vault_client
    app.config["CLUSTER_ID"] = "test-cluster"
    if mfa_lane is not None:
        app.config["TENANT_COMPLIANCE_LANE"] = {principal.tenant_id: mfa_lane}
    if rotation_provider is not None:
        app.config["JOINER_ROTATE_MATERIAL_PROVIDER"] = rotation_provider

    @app.before_request
    async def _inject_ctx() -> None:
        g.principal = principal
        g.tenant_context = TenantContext(tenant_id=principal.tenant_id)
        g.db_session = session
        # Set g.current_user for require_scopes decorator
        g.current_user = {
            "_jwt_payload": {
                "scope": " ".join(sorted(principal.scopes)),
                "sub": principal.sub,
                "tenant": principal.tenant_id,
            }
        }

    app.config["_session"] = session
    app.config["_vault"] = vault_client
    return app


def _principal(
    *, tenant: str = "acme", scopes: Optional[set[str]] = None, mfa: bool = False
) -> Principal:
    claims: dict[str, Any] = {"sub": "user@acme", "tenant": tenant}
    if mfa:
        claims["amr"] = ["mfa"]
    return Principal(
        cred_type=CredentialType.USER_JWT,
        sub="user@acme",
        tenant_id=tenant,
        scopes=frozenset(scopes or set()),
        claims=claims,
    )


# =============================================================================
# Pure serializer contract — NEVER leak envelope material
# =============================================================================


class TestSerializerSecretContract:
    """The serializer must never expose envelope/DEK material."""

    def test_serializer_omits_ciphertext_iv_authtag_dek(self) -> None:
        row = _make_secret()
        out = _serialize_joiner_secret(row)
        for forbidden in SECRET_FIELDS_MUST_BE_ABSENT:
            assert forbidden not in out, f"Serializer leaked {forbidden!r}"

    def test_serializer_status_active(self) -> None:
        row = _make_secret(expires_at=datetime.now(timezone.utc) + timedelta(days=30))
        out = _serialize_joiner_secret(row)
        assert out["status"] == "active"
        assert out["expiring_soon"] is False

    def test_serializer_status_expiring_soon(self) -> None:
        row = _make_secret(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        out = _serialize_joiner_secret(row)
        assert out["status"] == "expiring-soon"
        assert out["expiring_soon"] is True

    def test_serializer_status_revoked_takes_precedence(self) -> None:
        row = _make_secret(
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            revoked_at=datetime.now(timezone.utc),
        )
        out = _serialize_joiner_secret(row)
        assert out["status"] == "revoked"
        assert out["expiring_soon"] is False

    def test_serializer_handles_no_expiry(self) -> None:
        row = _make_secret(expires_at=None, ttl_seconds=None)
        out = _serialize_joiner_secret(row)
        assert out["expires_at"] is None
        assert out["status"] == "active"

    def test_window_constant_is_24h(self) -> None:
        assert EXPIRING_SOON_WINDOW == timedelta(hours=24)


# =============================================================================
# GET /joiner-secrets
# =============================================================================


@pytest.mark.asyncio
class TestListJoinerSecrets:
    """Read path -- converted to penguin-dal, exercised against real Postgres.

    ``test_list_requires_scope`` returns before ``list_joiner_secrets`` ever
    calls ``get_db()`` (scope decorator short-circuits first), so it doesn't
    need ``pg_db`` and is unchanged.
    """

    async def test_list_happy_path_omits_envelope(
        self, pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seeded via the owner role (``pg_db``, RLS-exempt); read back via
        the actual scoped ``api-manager-rw`` role (``pg_db_scoped``) with
        RLS genuinely enforced.
        """
        cluster_id = uuid.uuid4()
        biome_id = _seed_biome(pg_db)
        _seed_joiner_secret(
            pg_db,
            cluster_id=cluster_id,
            biome_kind="vault",
            emitter_biome_id=biome_id,
            tenant_id="acme",
        )
        _seed_joiner_secret(
            pg_db,
            cluster_id=cluster_id,
            biome_kind="postgres",
            emitter_biome_id=biome_id,
            tenant_id="acme",
        )
        principal = _principal(scopes={"gough.joiner.read"}, tenant="acme")
        app = _build_app(
            rows=[],
            principal=principal,
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        client = app.test_client()
        resp = await client.get(f"/api/v1/clusters/{cluster_id}/joiner-secrets")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["count"] == 2
        for item in data["items"]:
            for forbidden in SECRET_FIELDS_MUST_BE_ABSENT:
                assert forbidden not in item

    async def test_list_empty_result(
        self, pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No secrets for this cluster -> 200 with an empty items list."""
        cluster_id = uuid.uuid4()
        _seed_joiner_secret(
            pg_db, cluster_id=uuid.uuid4(), tenant_id="acme"
        )  # different cluster, same tenant
        principal = _principal(scopes={"gough.joiner.read"}, tenant="acme")
        app = _build_app(
            rows=[],
            principal=principal,
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        client = app.test_client()
        resp = await client.get(f"/api/v1/clusters/{cluster_id}/joiner-secrets")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data == {
            "cluster_id": str(cluster_id),
            "tenant_id": "acme",
            "count": 0,
            "items": [],
        }

    async def test_list_rls_isolates_two_tenants(
        self, pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two tenants, one secret each on the SAME cluster_id -- tenant A's
        request sees ONLY tenant A's secret (never tenant B's), and vice
        versa, under real RLS enforcement (``pg_db_scoped``, not the
        RLS-exempt owner role). Same ``cluster_id`` on purpose: the
        app-level filter alone (``cluster_id == ... & tenant_id == ...``)
        would already exclude tenant B's row if it had a different
        cluster_id, so that wouldn't prove RLS is doing anything -- sharing
        the cluster_id isolates what only RLS (Layer 4) enforces from what
        the app-level filter (Layer 1/2) already would.
        """
        cluster_id = uuid.uuid4()
        biome_id = _seed_biome(pg_db)
        secret_a = _seed_joiner_secret(
            pg_db,
            cluster_id=cluster_id,
            tenant_id="tenant-a",
            emitter_biome_id=biome_id,
            extractor_name="secret-a",
        )
        secret_b = _seed_joiner_secret(
            pg_db,
            cluster_id=cluster_id,
            tenant_id="tenant-b",
            emitter_biome_id=biome_id,
            extractor_name="secret-b",
        )

        app_a = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.joiner.read"}, tenant="tenant-a"),
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        resp_a = await app_a.test_client().get(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets"
        )
        assert resp_a.status_code == 200
        data_a = await resp_a.get_json()
        assert data_a["count"] == 1
        assert data_a["items"][0]["id"] == secret_a
        assert all(item["tenant_id"] == "tenant-a" for item in data_a["items"])

        app_b = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.joiner.read"}, tenant="tenant-b"),
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        resp_b = await app_b.test_client().get(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets"
        )
        assert resp_b.status_code == 200
        data_b = await resp_b.get_json()
        assert data_b["count"] == 1
        assert data_b["items"][0]["id"] == secret_b
        assert all(item["tenant_id"] == "tenant-b" for item in data_b["items"])

    async def test_list_filter_biome_kind(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster_id = uuid.uuid4()
        biome_id = _seed_biome(pg_db)
        _seed_joiner_secret(
            pg_db, cluster_id=cluster_id, biome_kind="vault", emitter_biome_id=biome_id
        )
        _seed_joiner_secret(
            pg_db,
            cluster_id=cluster_id,
            biome_kind="postgres",
            emitter_biome_id=biome_id,
        )
        principal = _principal(scopes={"gough.joiner.read"})
        app = _build_app(
            rows=[], principal=principal, dal_db=pg_db, monkeypatch=monkeypatch
        )
        client = app.test_client()
        resp = await client.get(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets?biome_kind=vault"
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["count"] == 1
        assert data["items"][0]["biome_kind"] == "vault"

    async def test_list_filter_status_revoked(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster_id = uuid.uuid4()
        biome_id = _seed_biome(pg_db)
        _seed_joiner_secret(pg_db, cluster_id=cluster_id, emitter_biome_id=biome_id)
        _seed_joiner_secret(
            pg_db,
            cluster_id=cluster_id,
            emitter_biome_id=biome_id,
            revoked_at=datetime.now(timezone.utc),
        )
        principal = _principal(scopes={"gough.joiner.read"})
        app = _build_app(
            rows=[], principal=principal, dal_db=pg_db, monkeypatch=monkeypatch
        )
        client = app.test_client()
        resp = await client.get(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets?status=revoked"
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["count"] == 1
        assert all(item["status"] == "revoked" for item in data["items"])

    async def test_list_requires_scope(self) -> None:
        cluster_id = uuid.uuid4()
        principal = _principal(scopes=set())  # missing gough.joiner.read
        app = _build_app(rows=[], principal=principal)
        client = app.test_client()
        resp = await client.get(f"/api/v1/clusters/{cluster_id}/joiner-secrets")
        assert resp.status_code == 403

    async def test_list_filter_scope_field(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster_id = uuid.uuid4()
        biome_id = _seed_biome(pg_db)
        _seed_joiner_secret(
            pg_db, cluster_id=cluster_id, scope="tenant", emitter_biome_id=biome_id
        )
        principal = _principal(scopes={"gough.joiner.read"})
        app = _build_app(
            rows=[], principal=principal, dal_db=pg_db, monkeypatch=monkeypatch
        )
        client = app.test_client()
        resp = await client.get(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets?scope=tenant"
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["count"] == 1


# =============================================================================
# POST /rotate
# =============================================================================


@pytest.mark.asyncio
class TestRotateJoinerSecret:
    """The lookup + revoke-mark is converted to penguin-dal (needs ``pg_db``);
    ``AuditEventWriter``/``JoinerSecretEmitter`` stay on the SQLAlchemy
    session (``FakeSession``) and ``JoinerSecretEmitter`` itself is replaced
    by a ``FakeEmitter`` in every test that reaches it, same as before this
    conversion -- the real class's call signature is already out of sync
    with this call site independent of DB access (see task report).

    ``test_rotate_missing_reason`` / ``test_rotate_requires_rotate_scope`` /
    ``test_rotate_mfa_required_in_compliance_lane`` all return before the
    handler calls ``get_db()``, so they're unchanged.
    """

    async def test_rotate_happy_path(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exercises the REAL atomic persist path (``db.transaction()`` +
        ``persist_extracted_material``) -- no ``JoinerSecretEmitter``
        stand-in anymore, since ``rotate_joiner_secret`` no longer calls
        that class at all (see the handler + module docstrings). The
        ``vault_client`` mock ``_build_app`` already wires
        (``transit_encrypt``/``transit_sign``) is all the real persist path
        needs.
        """
        cluster_id = uuid.uuid4()
        original_id = _seed_joiner_secret(pg_db, cluster_id=cluster_id)

        def provider(_row: Any) -> ExtractedMaterial:
            return ExtractedMaterial(
                extractor_name=_row.extractor_name,
                plaintext=b"new-plaintext",
                ttl_seconds=600,
                rotation_class="vault-unseal",
            )

        principal = _principal(scopes={"gough.joiner.rotate", "gough.joiner.read"})
        app = _build_app(
            rows=[],
            principal=principal,
            rotation_provider=provider,
            dal_db=pg_db,
            monkeypatch=monkeypatch,
        )

        client = app.test_client()
        resp = await client.post(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{original_id}/rotate",
            json={"reason": "key compromised"},
        )

        assert resp.status_code == 201, await resp.get_data()
        data = await resp.get_json()
        for forbidden in SECRET_FIELDS_MUST_BE_ABSENT:
            assert forbidden not in data["rotated"]
            assert forbidden not in data["new_secret"]
        assert data["rotated"]["status"] == "revoked"
        assert data["new_secret"]["id"] != original_id
        assert data["new_secret"]["extractor_name"] == "root-token"
        assert data["new_secret"]["rotation_class"] == "vault-unseal"

        # The new row is genuinely committed and independently readable --
        # not an artifact of reading back inside the same transaction.
        reread = (
            pg_db(pg_db.joiner_secrets.id == data["new_secret"]["id"]).select().first()
        )
        assert reread is not None
        assert reread.revoked_at is None

        # A fresh audit_events row backs the new secret's audit_event_id
        # (the atomic persist path writes its own chain-hashed row rather
        # than going through AuditEventWriter -- see persist_extracted_material).
        audit_row = (
            pg_db(pg_db.audit_events.id == data["audit_event_id"]).select().first()
        )
        assert audit_row is not None
        assert audit_row.action == "joiner.secret.rotate"

    async def test_rotate_atomic_rollback_on_persist_failure(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If persisting the new secret fails mid-transaction (Vault error),
        the revoke of the OLD secret must roll back too -- this is the
        fix-round #3(b) regression guard: a naive auto-committing
        ``.update()`` before the persist step would leave the old secret
        permanently revoked with no replacement.
        """
        cluster_id = uuid.uuid4()
        original_id = _seed_joiner_secret(pg_db, cluster_id=cluster_id)

        def provider(_row: Any) -> ExtractedMaterial:
            return ExtractedMaterial(extractor_name="root-token", plaintext=b"x")

        principal = _principal(scopes={"gough.joiner.rotate"})
        app = _build_app(
            rows=[],
            principal=principal,
            rotation_provider=provider,
            dal_db=pg_db,
            monkeypatch=monkeypatch,
        )
        # Force the Vault wrap call inside encrypt_envelope to fail.
        app.config["VAULT_CLIENT"].transit_encrypt.side_effect = RuntimeError(
            "vault sealed"
        )

        client = app.test_client()
        resp = await client.post(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{original_id}/rotate",
            json={"reason": "key compromised"},
        )
        assert resp.status_code == 500

        # The original secret must NOT be left revoked -- the transaction
        # that would have revoked it rolled back along with the failed
        # persist.
        original_row = pg_db(pg_db.joiner_secrets.id == original_id).select().first()
        assert original_row is not None
        assert original_row.revoked_at is None
        assert original_row.rotated_at is None

    async def test_rotate_missing_reason(self) -> None:
        cluster_id = uuid.uuid4()
        secret = _make_secret(cluster_id=cluster_id)
        principal = _principal(scopes={"gough.joiner.rotate"})
        app = _build_app(rows=[secret], principal=principal)
        client = app.test_client()
        resp = await client.post(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret.id}/rotate",
            json={},
        )
        assert resp.status_code == 400

    async def test_rotate_not_found(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster_id = uuid.uuid4()
        principal = _principal(scopes={"gough.joiner.rotate"})
        app = _build_app(
            rows=[], principal=principal, dal_db=pg_db, monkeypatch=monkeypatch
        )
        client = app.test_client()
        resp = await client.post(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{uuid.uuid4()}/rotate",
            json={"reason": "rotate"},
        )
        assert resp.status_code == 404

    async def test_rotate_already_revoked(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster_id = uuid.uuid4()
        secret_id = _seed_joiner_secret(
            pg_db, cluster_id=cluster_id, revoked_at=datetime.now(timezone.utc)
        )
        principal = _principal(scopes={"gough.joiner.rotate"})
        app = _build_app(
            rows=[], principal=principal, dal_db=pg_db, monkeypatch=monkeypatch
        )
        client = app.test_client()
        resp = await client.post(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret_id}/rotate",
            json={"reason": "rotate"},
        )
        assert resp.status_code == 409

    async def test_rotate_requires_rotate_scope(self) -> None:
        cluster_id = uuid.uuid4()
        secret = _make_secret(cluster_id=cluster_id)
        principal = _principal(scopes={"gough.joiner.read"})
        app = _build_app(rows=[secret], principal=principal)
        client = app.test_client()
        resp = await client.post(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret.id}/rotate",
            json={"reason": "rotate"},
        )
        assert resp.status_code == 403

    async def test_rotate_mfa_required_in_compliance_lane(self) -> None:
        cluster_id = uuid.uuid4()
        secret = _make_secret(cluster_id=cluster_id)
        # principal w/o MFA, fedramp lane
        principal = _principal(scopes={"gough.joiner.rotate"}, mfa=False)
        app = _build_app(rows=[secret], principal=principal, mfa_lane="fedramp")
        client = app.test_client()
        resp = await client.post(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret.id}/rotate",
            json={"reason": "rotate"},
        )
        assert resp.status_code == 401
        body = await resp.get_json()
        assert body["error"] == "mfa_required"

    async def test_rotate_mfa_satisfied(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster_id = uuid.uuid4()
        secret_id = _seed_joiner_secret(pg_db, cluster_id=cluster_id)
        principal = _principal(scopes={"gough.joiner.rotate"}, mfa=True)

        def provider(_row: Any) -> ExtractedMaterial:
            return ExtractedMaterial(
                extractor_name=_row.extractor_name,
                plaintext=b"x",
                ttl_seconds=60,
            )

        app = _build_app(
            rows=[],
            principal=principal,
            mfa_lane="fedramp",
            rotation_provider=provider,
            dal_db=pg_db,
            monkeypatch=monkeypatch,
        )

        client = app.test_client()
        resp = await client.post(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret_id}/rotate",
            json={"reason": "scheduled"},
        )

        assert resp.status_code == 201

    async def test_rotate_provider_missing_returns_503(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster_id = uuid.uuid4()
        secret_id = _seed_joiner_secret(pg_db, cluster_id=cluster_id)
        principal = _principal(scopes={"gough.joiner.rotate"})
        app = _build_app(
            rows=[], principal=principal, dal_db=pg_db, monkeypatch=monkeypatch
        )
        # No rotation_provider configured
        client = app.test_client()
        resp = await client.post(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret_id}/rotate",
            json={"reason": "rotate"},
        )
        assert resp.status_code == 503


# =============================================================================
# DELETE /joiner-secrets
# =============================================================================


@pytest.mark.asyncio
class TestRevokeJoinerSecret:
    """Lookup + revoke-mark + the self-audit-log write (``AuditEventWriter``
    via ``_build_audit_writer``) all go through the penguin-dal overlay
    (``get_db()``, patched via ``dal_db=pg_db``) now (FIX #7a cleanup) --
    every non-error-path test here needs ``dal_db``/``monkeypatch``.
    ``test_revoke_missing_reason``/``test_revoke_requires_admin_scope``
    return before ``get_db()`` is called, so they're unchanged.
    """

    async def test_revoke_happy_path(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster_id = uuid.uuid4()
        secret_id = _seed_joiner_secret(pg_db, cluster_id=cluster_id)
        principal = _principal(scopes={"gough.cluster.admin"})
        app = _build_app(
            rows=[], principal=principal, dal_db=pg_db, monkeypatch=monkeypatch
        )
        client = app.test_client()
        resp = await client.delete(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret_id}",
            json={"reason": "manual revoke"},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        for forbidden in SECRET_FIELDS_MUST_BE_ABSENT:
            assert forbidden not in body["revoked"]
        assert body["revoked"]["status"] == "revoked"

    async def test_revoke_self_audit_write_succeeds_under_rls_and_is_tenant_scoped(
        self, pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for the ``set_tenant_guc`` removal (FIX #7a cleanup).

        The self-audit-log write inside ``revoke_joiner_secret``
        (``_build_audit_writer`` -> ``AuditEventWriter.append()``) used to
        run through ``set_tenant_guc(_get_db_session(), tenant_id)`` --
        ``_get_db_session()`` always raised ``RuntimeError`` in production
        (``DB_SESSION_FACTORY`` is never wired into ``create_app()``), so
        this write never actually happened outside of tests. Exercised here
        against the real, non-owner ``api-manager-rw`` role
        (``pg_db_scoped``, not the RLS-exempt ``pg_db`` owner) to prove two
        things at once: the write succeeds under genuine RLS enforcement
        (the ``tenant_isolation`` policy's ``USING`` clause doubles as its
        ``WITH CHECK`` since the baseline migration defines no separate one
        -- the requesting tenant's own INSERT must satisfy it), and a
        different tenant's RLS-scoped connection cannot see the row --
        i.e. removing ``set_tenant_guc`` did not weaken isolation, because
        the ContextVar pool-checkout event (``app.db.rls``) covers it.
        """
        cluster_id = uuid.uuid4()
        secret_id = _seed_joiner_secret(
            pg_db, cluster_id=cluster_id, tenant_id="tenant-a"
        )
        principal = _principal(scopes={"gough.cluster.admin"}, tenant="tenant-a")
        app = _build_app(
            rows=[],
            principal=principal,
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        client = app.test_client()
        resp = await client.delete(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret_id}",
            json={"reason": "rls regression check"},
        )
        assert resp.status_code == 200

        # Read back via the owner role (RLS-exempt) -- proves the row was
        # actually persisted, carrying the requesting tenant's own
        # tenant_id, not silently dropped or mis-scoped.
        written = (
            pg_db(
                (pg_db.audit_events.action == "joiner.secret.revoke")
                & (pg_db.audit_events.resource_id == secret_id)
            )
            .select()
            .first()
        )
        assert written is not None
        assert written.tenant_id == "tenant-a"

        # A different tenant's RLS-scoped connection must NOT see it.
        from app.db.rls import set_current_tenant

        set_current_tenant("tenant-b")
        try:
            invisible = (
                pg_db_scoped(
                    (pg_db_scoped.audit_events.action == "joiner.secret.revoke")
                    & (pg_db_scoped.audit_events.resource_id == secret_id)
                )
                .select()
                .first()
            )
        finally:
            set_current_tenant(None)
        assert invisible is None

    async def test_revoke_missing_reason(self) -> None:
        cluster_id = uuid.uuid4()
        secret = _make_secret(cluster_id=cluster_id)
        principal = _principal(scopes={"gough.cluster.admin"})
        app = _build_app(rows=[secret], principal=principal)
        client = app.test_client()
        resp = await client.delete(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret.id}",
            json={},
        )
        assert resp.status_code == 400

    async def test_revoke_not_found(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster_id = uuid.uuid4()
        principal = _principal(scopes={"gough.cluster.admin"})
        app = _build_app(
            rows=[], principal=principal, dal_db=pg_db, monkeypatch=monkeypatch
        )
        client = app.test_client()
        resp = await client.delete(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{uuid.uuid4()}",
            json={"reason": "rev"},
        )
        assert resp.status_code == 404

    async def test_revoke_idempotent_already_revoked_returns_409(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster_id = uuid.uuid4()
        secret_id = _seed_joiner_secret(
            pg_db, cluster_id=cluster_id, revoked_at=datetime.now(timezone.utc)
        )
        principal = _principal(scopes={"gough.cluster.admin"})
        app = _build_app(
            rows=[], principal=principal, dal_db=pg_db, monkeypatch=monkeypatch
        )
        client = app.test_client()
        resp = await client.delete(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret_id}",
            json={"reason": "rev"},
        )
        assert resp.status_code == 409

    async def test_revoke_requires_admin_scope(self) -> None:
        cluster_id = uuid.uuid4()
        secret = _make_secret(cluster_id=cluster_id)
        principal = _principal(scopes={"gough.joiner.rotate"})
        app = _build_app(rows=[secret], principal=principal)
        client = app.test_client()
        resp = await client.delete(
            f"/api/v1/clusters/{cluster_id}/joiner-secrets/{secret.id}",
            json={"reason": "rev"},
        )
        assert resp.status_code == 403


class TestComplianceLaneConfig:
    """The default lanes match the spec; overridable via app.config."""

    def test_default_lanes_includes_fedramp_hipaa_pci(self) -> None:
        assert "fedramp" in DEFAULT_MFA_REQUIRED_LANES
        assert "hipaa" in DEFAULT_MFA_REQUIRED_LANES
        assert "pci" in DEFAULT_MFA_REQUIRED_LANES
