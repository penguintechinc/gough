"""Tests for ``app.api.audit`` blueprint.

Covers:
- list events with tenant + filter scoping (since/until/actor/action/etc)
- format=json vs format=jsonl
- cursor pagination + invalid cursor
- cluster_id filter requires gough.cluster.superadmin
- verify endpoint: clean chain returns 0 breaks; tampered chain
  triggers Prometheus counter increment
- export endpoint: scope + MFA enforcement, JSONL stream format,
  Vault-transit footer signature, ``audit.log.export`` self-audit, and
  ``gough.audit.exported`` NATS publish
"""

from __future__ import annotations

import base64
import importlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
from quart import Quart, g

from app.api import audit as audit_module
from app.api.audit import (
    DEFAULT_MFA_REQUIRED_LANES,
    _serialize_audit_event,
    _stream_audit_events_jsonl,
    audit_bp,
    audit_chain_break_total,
)
from app.security.credentials import CredentialType, Principal
from app.security.tenant import TenantContext

# =============================================================================
# Fixtures
# =============================================================================


@dataclass
class FakeAuditRow:
    """Stand-in for SQLAlchemy AuditEvent ORM row."""

    id: uuid.UUID
    ts: datetime
    cluster_id: str
    tenant_id: Optional[str]
    actor_sub: str
    actor_scope: list[str]
    action: str
    resource_kind: str
    resource_id: Optional[str] = None
    before_json: Optional[dict] = None
    after_json: Optional[dict] = None
    request_id: Optional[str] = None
    source_ip: Optional[str] = None
    user_agent: Optional[str] = None
    prev_hash: bytes = field(default=b"\x00" * 32)
    hash: bytes = field(default=b"\x11" * 32)
    signature: Optional[bytes] = None


def _make_row(**overrides: Any) -> FakeAuditRow:
    base = dict(
        id=uuid.uuid4(),
        ts=datetime.now(timezone.utc),
        cluster_id="test-cluster",
        tenant_id="acme",
        actor_sub="alice@acme",
        actor_scope=["gough.audit.read"],
        action="joiner.secret.emit",
        resource_kind="joiner_secret",
        resource_id=str(uuid.uuid4()),
    )
    base.update(overrides)
    return FakeAuditRow(**base)


def _seed_audit_event(dal_db: Any, **overrides: Any) -> str:
    """Insert a real ``audit_events`` row via penguin-dal; return its id (str).

    Used by tests exercising the penguin-dal-converted read paths
    (``list_audit_events`` / ``_stream_audit_events_jsonl``), as opposed to
    ``_make_row``, which only builds an in-memory stand-in for the
    still-SQLAlchemy-session-based paths (``verify_audit_chain`` / the
    ``AuditEventWriter`` self-audit inside ``export_audit_log``).
    """
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


class FakeQuery:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def filter(self, *_: Any) -> "FakeQuery":
        return self

    def order_by(self, *_: Any) -> "FakeQuery":
        return self

    def limit(self, n: int) -> "FakeQuery":
        self._rows = self._rows[:n]
        return self

    def yield_per(self, _n: int) -> "FakeQuery":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeTx:
    """Minimal penguin-dal ``Tx`` stand-in for ``AuditEventWriter.append()``.

    Only implements what ``AuditEventWriter._append_on_tx`` actually calls:
    a chain-head ``SELECT`` (always answered "no prior rows" -- these tests
    don't assert anything about the writer's own self-audit-log row, only
    about the export stream/footer/NATS behavior) and the subsequent
    ``INSERT`` (recorded, not persisted anywhere real).
    """

    def __init__(self, session: "FakeSession") -> None:
        self._session = session

    def executesql(self, query: str, placeholders: Any = None, **_: Any) -> Any:
        stripped = query.strip().upper()
        if stripped.startswith("SELECT"):
            return []
        self._session.inserted.append((query, placeholders))
        return None


class FakeSession:
    """Legacy dual-interface test double.

    Predates the FIX #7a cleanup that removed ``set_tenant_guc`` and
    converted ``verify_audit_chain``/``export_audit_log``/
    ``_build_audit_writer`` to ``get_db()`` (penguin-dal, ContextVar-wired
    RLS) -- nothing in ``app.api.audit`` reads ``g.db_session`` anymore, so
    the ``execute()`` half of this double (originally there for
    ``set_tenant_guc``) is now unused. Left wired via
    ``DB_SESSION_FACTORY``/``g.db_session`` in ``_build_app`` (harmless) and
    the ``transaction()``/``executesql()`` half (``_FakeTx``) is kept in
    case a future test wants a lightweight penguin-dal stand-in that
    doesn't need a real Postgres instance.
    """

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
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

    def add(self, _obj: Any) -> None:
        pass

    def flush(self) -> None:
        pass


def _principal(
    *,
    tenant: str = "acme",
    scopes: Optional[set[str]] = None,
    mfa: bool = False,
    sub: str = "alice@acme",
) -> Principal:
    claims: dict[str, Any] = {"sub": sub, "tenant": tenant}
    if mfa:
        claims["amr"] = ["mfa"]
    return Principal(
        cred_type=CredentialType.USER_JWT,
        sub=sub,
        tenant_id=tenant,
        scopes=frozenset(scopes or set()),
        claims=claims,
    )


def _passthrough_decorator(*dargs, **dkwargs):
    """Stub that replaces require_scopes with a no-op."""
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def _wrap(fn):
        return fn

    return _wrap


def _build_app(
    *,
    rows: list[Any],
    principal: Principal,
    verify_result: Optional[dict[str, Any]] = None,
    mfa_lane: Optional[str] = None,
    nats_client: Optional[Any] = None,
    dal_db: Optional[Any] = None,
    monkeypatch: Optional[pytest.MonkeyPatch] = None,
    rls_scoped: bool = False,
) -> Quart:
    """Build a Quart app with ``audit_bp`` registered.

    ``rows``/``FakeSession``/``DB_SESSION_FACTORY``/``g.db_session`` are
    legacy scaffolding, predating the FIX #7a cleanup that removed
    ``set_tenant_guc`` and converted ``verify_audit_chain``/
    ``export_audit_log``/``_build_audit_writer`` to ``get_db()`` (the same
    penguin-dal overlay ``list_audit_events`` already used, ContextVar-wired
    RLS via ``app.db.rls``). Nothing in ``app.api.audit`` reads
    ``g.db_session``/``DB_SESSION_FACTORY`` anymore; left wired here
    (harmless, unused) rather than ripped out as a separate cleanup. Pass
    ``dal_db`` (a real ``pg_db``) + ``monkeypatch`` together to patch
    ``audit_module.get_db`` -- required for any test that reaches
    ``list_audit_events``, ``verify_audit_chain``, or ``export_audit_log``'s
    export-stream / self-audit-write logic.

    ``rls_scoped=True`` additionally wires ``app.db.rls``'s pool
    checkout/checkin GUC events onto ``dal_db.engine`` and pushes
    ``principal.tenant_id`` onto the ``set_current_tenant`` ContextVar for
    the duration of each request (mirroring what the real
    ``tenant_middleware`` does), so RLS is genuinely enforced against
    ``dal_db`` -- pass a ``pg_db_scoped`` fixture (connected as the
    non-owner ``api-manager-rw`` role) as ``dal_db`` when using this, since
    ``pg_db`` (the migration/table owner) is RLS-exempt and would prove
    nothing.
    """
    app = Quart(__name__)
    app.register_blueprint(audit_bp, url_prefix="/api/v1/audit")

    if dal_db is not None:
        assert monkeypatch is not None, "dal_db requires monkeypatch"
        monkeypatch.setattr(audit_module, "get_db", lambda: dal_db)

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
    vault_client.transit_sign.return_value = "vault:v1:export-signature"

    app.config["DB_SESSION_FACTORY"] = lambda: session
    app.config["VAULT_CLIENT"] = vault_client
    app.config["CLUSTER_ID"] = "test-cluster"
    app.config["AUDIT_EXPORT_SIGNING_KEY"] = "gough-audit-export"
    if mfa_lane is not None:
        app.config["TENANT_COMPLIANCE_LANE"] = {principal.tenant_id: mfa_lane}
    if nats_client is not None:
        app.config["NATS_CLIENT"] = nats_client

    @app.before_request
    async def _inject_ctx() -> None:
        g.principal = principal
        g.current_user = {
            "_jwt_payload": {
                "tenant": principal.tenant_id,
                "scope": " ".join(principal.scopes) if principal.scopes else "",
                "sub": principal.sub,
            }
        }
        g.tenant_context = TenantContext(tenant_id=principal.tenant_id)
        g.db_session = session

    if verify_result is not None:
        # Patch verify_chain at module level for the duration of the app
        app.config["_verify_result"] = verify_result

    app.config["_session"] = session
    app.config["_vault"] = vault_client
    return app


# =============================================================================
# Pure serializer
# =============================================================================


class TestSerializerAuditEvent:
    def test_serializer_renders_hash_as_b64(self) -> None:
        row = _make_row(hash=b"\xab" * 32, prev_hash=b"\xcd" * 32)
        out = _serialize_audit_event(row)
        assert out["hash_b64"] == base64.b64encode(b"\xab" * 32).decode("ascii")
        assert out["prev_hash_b64"] == base64.b64encode(b"\xcd" * 32).decode("ascii")
        assert out["signature_b64"] is None

    def test_serializer_includes_actor_scope(self) -> None:
        row = _make_row(actor_scope=["gough.audit.read", "gough.cluster.read"])
        out = _serialize_audit_event(row)
        assert "gough.audit.read" in out["actor_scope"]


# =============================================================================
# GET /events
# =============================================================================


# Formerly xfail(strict=True): the baseline migration
# (alembic/versions/20260805_1000_baseline_full_schema.py) only granted
# `api-manager-rw` INSERT on audit_events, never SELECT, so
# list_audit_events / export_audit_log's read queries hit `permission denied
# for table audit_events` against real Postgres. The migration now grants
# SELECT too (see the GRANT statement for audit_events); these tests run for
# real against pg_db_scoped.


@pytest.mark.asyncio
class TestListAuditEvents:
    """Read path -- converted to penguin-dal, exercised against real Postgres.

    ``test_list_invalid_format`` / ``test_list_requires_scope`` /
    ``test_list_cluster_id_filter_requires_superadmin`` /
    ``test_list_invalid_cursor`` all return before ``list_audit_events``
    ever calls ``get_db()`` (bad input / scope check short-circuits first),
    so they don't need ``pg_db`` and are unchanged.
    """

    async def test_list_happy_path(
        self, pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seeded via the owner role (``pg_db``, RLS-exempt); read back via
        the actual scoped ``api-manager-rw`` role (``pg_db_scoped``) with RLS
        genuinely enforced -- proves the converted ``db(...).select()``
        returns the tenant's own rows under RLS, not just under an
        RLS-exempt connection.
        """
        _seed_audit_event(pg_db, tenant_id="acme")
        _seed_audit_event(pg_db, tenant_id="acme", action="cluster_init")
        app = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.audit.read"}, tenant="acme"),
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        client = app.test_client()
        resp = await client.get("/api/v1/audit/events")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 2

    async def test_list_empty_result(
        self, pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No rows for this tenant -> 200 with an empty items list."""
        _seed_audit_event(pg_db, tenant_id="other-tenant")
        app = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.audit.read"}, tenant="acme"),
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        client = app.test_client()
        resp = await client.get("/api/v1/audit/events")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body == {
            "tenant_id": "acme",
            "count": 0,
            "next_cursor": None,
            "items": [],
        }

    async def test_list_rls_isolates_two_tenants(
        self, pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two tenants, one row each -- tenant A's request sees ONLY tenant
        A's row (never tenant B's), and vice versa, under real RLS
        enforcement (``pg_db_scoped``, not the RLS-exempt owner role).

        This is the direct regression guard for FIX #7a wired through the
        actual converted endpoint (``tests/test_rls_isolation.py`` proves
        the GUC-wiring mechanism in isolation against a raw ``biomes``
        query; this proves ``list_audit_events`` specifically benefits from
        it, now that it reads via ``app.models.get_db()``).
        """
        _seed_audit_event(pg_db, tenant_id="tenant-a", action="a.action")
        _seed_audit_event(pg_db, tenant_id="tenant-b", action="b.action")

        app_a = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.audit.read"}, tenant="tenant-a"),
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        resp_a = await app_a.test_client().get("/api/v1/audit/events")
        assert resp_a.status_code == 200
        body_a = await resp_a.get_json()
        assert body_a["count"] == 1
        assert body_a["items"][0]["action"] == "a.action"
        assert all(item["tenant_id"] == "tenant-a" for item in body_a["items"])

        app_b = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.audit.read"}, tenant="tenant-b"),
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        resp_b = await app_b.test_client().get("/api/v1/audit/events")
        assert resp_b.status_code == 200
        body_b = await resp_b.get_json()
        assert body_b["count"] == 1
        assert body_b["items"][0]["action"] == "b.action"
        assert all(item["tenant_id"] == "tenant-b" for item in body_b["items"])

    async def test_list_format_jsonl(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_audit_event(pg_db, tenant_id="acme")
        _seed_audit_event(pg_db, tenant_id="acme")
        app = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.audit.read"}),
            dal_db=pg_db,
            monkeypatch=monkeypatch,
        )
        client = app.test_client()
        resp = await client.get("/api/v1/audit/events?format=jsonl")
        assert resp.status_code == 200
        text = (await resp.get_data()).decode("utf-8").strip().splitlines()
        assert len(text) == 2
        for line in text:
            json.loads(line)  # each line must be valid JSON

    async def test_list_invalid_format(self) -> None:
        app = _build_app(rows=[], principal=_principal(scopes={"gough.audit.read"}))
        client = app.test_client()
        resp = await client.get("/api/v1/audit/events?format=xml")
        assert resp.status_code == 400

    async def test_list_requires_scope(self) -> None:
        app = _build_app(rows=[], principal=_principal(scopes=set()))
        client = app.test_client()
        resp = await client.get("/api/v1/audit/events")
        assert resp.status_code == 403

    async def test_list_cluster_id_filter_requires_superadmin(self) -> None:
        app = _build_app(rows=[], principal=_principal(scopes={"gough.audit.read"}))
        client = app.test_client()
        resp = await client.get("/api/v1/audit/events?cluster_id=other-cluster")
        assert resp.status_code == 403

    async def test_list_cluster_id_filter_with_superadmin(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_audit_event(pg_db, tenant_id="acme", cluster_id="other-cluster")
        app = _build_app(
            rows=[],
            principal=_principal(
                scopes={"gough.audit.read", "gough.cluster.superadmin"}
            ),
            dal_db=pg_db,
            monkeypatch=monkeypatch,
        )
        client = app.test_client()
        resp = await client.get("/api/v1/audit/events?cluster_id=other-cluster")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 1

    async def test_list_invalid_cursor(self) -> None:
        app = _build_app(rows=[], principal=_principal(scopes={"gough.audit.read"}))
        client = app.test_client()
        resp = await client.get("/api/v1/audit/events?cursor=not-a-uuid")
        assert resp.status_code == 400

    async def test_list_filters_dont_500(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_audit_event(
            pg_db,
            tenant_id="acme",
            action="x",
            actor_sub="bob@acme",
            resource_kind="joiner_secret",
            resource_id="abc",
            request_id="req-1",
        )
        app = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.audit.read"}),
            dal_db=pg_db,
            monkeypatch=monkeypatch,
        )
        client = app.test_client()
        resp = await client.get(
            "/api/v1/audit/events?since=2020-01-01T00:00:00Z"
            "&until=2099-01-01T00:00:00Z"
            "&actor_sub=bob@acme&action=x"
            "&resource_kind=joiner_secret&resource_id=abc"
            "&request_id=req-1"
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 1

    async def test_list_cursor_pagination_excludes_rows_at_or_before_cursor(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``cursor`` filters to ``id > cursor`` -- exercises the
        str(uuid)-cast keyset comparison against the VARCHAR(36) id column.

        Explicit, lexicographically-ordered ids (rather than random uuid4)
        so ``id > cursor`` has a deterministic result to assert on.
        """
        first_id = "00000000-0000-0000-0000-000000000001"
        second_id = "00000000-0000-0000-0000-000000000002"
        _seed_audit_event(pg_db, id=first_id, tenant_id="acme")
        _seed_audit_event(pg_db, id=second_id, tenant_id="acme")
        app = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.audit.read"}),
            dal_db=pg_db,
            monkeypatch=monkeypatch,
        )
        client = app.test_client()
        resp = await client.get(f"/api/v1/audit/events?cursor={first_id}")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 1
        assert body["items"][0]["id"] == second_id


# =============================================================================
# POST /verify
# =============================================================================


@pytest.mark.asyncio
class TestVerifyAuditChain:
    async def test_verify_clean_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            audit_module,
            "verify_chain",
            lambda *a, **k: {
                "rows_checked": 5,
                "breaks": 0,
                "first_break_id": None,
                "last_break_id": None,
            },
        )
        # Reset counter
        audit_chain_break_total._metrics.clear()
        app = _build_app(rows=[], principal=_principal(scopes={"gough.audit.read"}))
        client = app.test_client()
        resp = await client.post("/api/v1/audit/verify", json={})
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body == {
            "rows_checked": 5,
            "breaks": 0,
            "first_break_id": None,
            "last_break_id": None,
        }

    async def test_verify_tampered_increments_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken_id = uuid.uuid4()
        monkeypatch.setattr(
            audit_module,
            "verify_chain",
            lambda *a, **k: {
                "rows_checked": 10,
                "breaks": 3,
                "first_break_id": broken_id,
                "last_break_id": broken_id,
            },
        )
        audit_chain_break_total._metrics.clear()
        app = _build_app(rows=[], principal=_principal(scopes={"gough.audit.read"}))
        client = app.test_client()
        resp = await client.post(
            "/api/v1/audit/verify",
            json={"since": "2020-01-01T00:00:00Z", "to": "2099-01-01T00:00:00Z"},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["breaks"] == 3
        assert body["first_break_id"] == str(broken_id)

        # Counter must have been incremented (3 increments) for our cluster
        labelled = audit_chain_break_total.labels(cluster_id="test-cluster")
        assert labelled._value.get() == 3

    async def test_verify_requires_scope(self) -> None:
        app = _build_app(rows=[], principal=_principal(scopes=set()))
        client = app.test_client()
        resp = await client.post("/api/v1/audit/verify", json={})
        assert resp.status_code == 403

    async def test_verify_rls_scopes_to_requesting_tenant(
        self, pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for the ``set_tenant_guc`` removal (FIX #7a cleanup).

        ``verify_audit_chain`` used to call
        ``set_tenant_guc(_get_db_session(), tenant_id)`` before
        ``verify_chain`` -- ``_get_db_session()`` always raised
        ``RuntimeError`` in production (``DB_SESSION_FACTORY`` is never
        wired into ``create_app()``), so this endpoint never actually ran a
        real query outside of tests that monkeypatch ``verify_chain``
        entirely (see ``test_verify_clean_chain`` above). Exercised here
        for real against the non-owner ``api-manager-rw`` role
        (``pg_db_scoped``), with ``verify_chain`` NOT monkeypatched,
        proving RLS -- via the ContextVar the tenant middleware sets
        (``app.db.rls``) -- scopes the read to the requesting tenant's own
        rows: not another tenant's, and not zero (fail-closed) either.
        Chain-hash validity itself is covered separately by
        ``test_verify_tampered_increments_counter``; ``_seed_audit_event``'s
        dummy ``hash``/``prev_hash`` values aren't cryptographically linked,
        so ``breaks`` isn't asserted here -- only row-count tenant scoping.
        """
        _seed_audit_event(pg_db, tenant_id="tenant-a", action="a.action")
        _seed_audit_event(pg_db, tenant_id="tenant-a", action="a.action.2")
        _seed_audit_event(pg_db, tenant_id="tenant-b", action="b.action")

        app = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.audit.read"}, tenant="tenant-a"),
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        client = app.test_client()
        resp = await client.post("/api/v1/audit/verify", json={})
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["rows_checked"] == 2


# =============================================================================
# Streaming export internals
# =============================================================================


@pytest.mark.asyncio
class TestStreamAuditEventsJsonl:
    """``db`` is a real penguin-dal DB (pg_db) now, not a SQLAlchemy session --
    this helper does its own keyset-paginated SELECT (see the docstring on
    ``_stream_audit_events_jsonl``), so it needs real ``audit_events`` rows.

    Regression: gh-22. ``_stream_audit_events_jsonl`` is now an async
    generator (each batch fetch + the final Vault signature run via
    ``run_db()`` off the event loop) -- collect via ``async for`` instead of
    ``list(...)``. Assertions are unchanged: this proves the stream still
    yields every row, batch-wise, in the same shape as before.
    """

    async def _collect(self, gen: Any) -> list[bytes]:
        return [chunk async for chunk in gen]

    async def test_stream_yields_one_line_per_row_plus_signature(
        self, pg_db: Any
    ) -> None:
        _seed_audit_event(pg_db)
        _seed_audit_event(pg_db)
        vault = MagicMock()
        vault.transit_sign.return_value = "vault:v1:sig"

        chunks = await self._collect(
            _stream_audit_events_jsonl(
                pg_db,
                cluster_id_label="test-cluster",
                target_sub="auditor@acme",
                vault_client=vault,
                signing_key="gough-audit-export",
            )
        )
        # Last chunk is the footer JSON
        body = b"".join(chunks).decode("utf-8").strip().splitlines()
        assert len(body) == 3  # 2 rows + 1 footer
        for line in body[:-1]:
            obj = json.loads(line)
            # rows must serialize hash but never reveal envelope material
            assert "hash_b64" in obj
        footer = json.loads(body[-1])
        assert footer["_signature"] is True
        assert footer["rows_exported"] == 2
        assert footer["signature"] == "vault:v1:sig"
        assert footer["target_sub"] == "auditor@acme"

    async def test_stream_empty_table_yields_only_footer(self, pg_db: Any) -> None:
        chunks = await self._collect(
            _stream_audit_events_jsonl(
                pg_db,
                cluster_id_label="test-cluster",
                target_sub=None,
                vault_client=None,
                signing_key="gough-audit-export",
            )
        )
        body = b"".join(chunks).decode("utf-8").strip().splitlines()
        assert len(body) == 1  # footer only
        footer = json.loads(body[0])
        assert footer["rows_exported"] == 0

    async def test_stream_paginates_across_batch_boundary(self, pg_db: Any) -> None:
        """# regression: gh-22

        More rows than ``batch_size`` -- keyset loop must fetch every page.
        Each batch fetch now runs off the event loop via ``run_db()``; this
        proves the async-generator conversion still yields every row across
        multiple batch hops, not just within a single batch.
        """
        for _ in range(5):
            _seed_audit_event(pg_db)
        chunks = await self._collect(
            _stream_audit_events_jsonl(
                pg_db,
                cluster_id_label="test-cluster",
                target_sub=None,
                vault_client=None,
                signing_key="gough-audit-export",
                batch_size=2,
            )
        )
        body = b"".join(chunks).decode("utf-8").strip().splitlines()
        assert len(body) == 6  # 5 rows + footer
        footer = json.loads(body[-1])
        assert footer["rows_exported"] == 5

    async def test_stream_with_no_vault_emits_unsigned(self, pg_db: Any) -> None:
        _seed_audit_event(pg_db)
        chunks = await self._collect(
            _stream_audit_events_jsonl(
                pg_db,
                cluster_id_label="test-cluster",
                target_sub=None,
                vault_client=None,
                signing_key="gough-audit-export",
            )
        )
        body = b"".join(chunks).decode("utf-8").strip().splitlines()
        footer = json.loads(body[-1])
        assert footer["signature"] == "unsigned"

    async def test_stream_handles_vault_error_gracefully(self, pg_db: Any) -> None:
        _seed_audit_event(pg_db)
        vault = MagicMock()
        vault.transit_sign.side_effect = RuntimeError("vault sealed")
        chunks = await self._collect(
            _stream_audit_events_jsonl(
                pg_db,
                cluster_id_label="test-cluster",
                target_sub=None,
                vault_client=vault,
                signing_key="gough-audit-export",
            )
        )
        footer = json.loads(b"".join(chunks).decode("utf-8").strip().splitlines()[-1])
        assert footer["signature"].startswith("vault-sign-error:")


# =============================================================================
# GET /export
# =============================================================================


@pytest.mark.asyncio
class TestExportAuditLog:
    async def test_export_requires_superadmin(self) -> None:
        app = _build_app(rows=[], principal=_principal(scopes={"gough.audit.read"}))
        client = app.test_client()
        resp = await client.get("/api/v1/audit/export")
        assert resp.status_code == 403

    async def test_export_mfa_required_in_compliance_lane(self) -> None:
        app = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.cluster.superadmin"}, mfa=False),
            mfa_lane="fedramp",
        )
        client = app.test_client()
        resp = await client.get("/api/v1/audit/export")
        assert resp.status_code == 401

    async def test_export_streams_jsonl_and_signs(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """2 seeded rows + footer, PLUS the endpoint's own self-audit-log
        row (``action="audit.log.export"``) -- since FIX #7a's cleanup,
        ``_build_audit_writer`` writes that row via ``get_db()`` (patched
        to this same real ``pg_db``) *before* the export stream is built,
        so the stream (deliberately cross-tenant/unfiltered, see
        ``_stream_audit_events_jsonl``) now genuinely includes it. Before
        that cleanup this write silently went to a ``FakeSession`` double
        instead of real Postgres, so it never showed up here -- 4 lines is
        the corrected count, not a regression.
        """
        _seed_audit_event(pg_db)
        _seed_audit_event(pg_db)
        nats_client = MagicMock()
        nats_client.publish = MagicMock()
        app = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.cluster.superadmin"}, mfa=True),
            mfa_lane="fedramp",
            nats_client=nats_client,
            dal_db=pg_db,
            monkeypatch=monkeypatch,
        )
        client = app.test_client()
        resp = await client.get("/api/v1/audit/export?target_sub=auditor@acme")
        assert resp.status_code == 200
        body = (await resp.get_data()).decode("utf-8").strip().splitlines()
        assert len(body) == 4  # 2 seeded rows + self-audit-log row + footer
        actions = [json.loads(line)["action"] for line in body[:-1]]
        assert "audit.log.export" in actions
        footer = json.loads(body[-1])
        assert footer["_signature"] is True
        assert footer["rows_exported"] == 3
        assert footer["target_sub"] == "auditor@acme"
        assert footer["signature"] == "vault:v1:export-signature"
        # NATS publish was called with the right subject
        assert nats_client.publish.call_count == 1
        subject, payload = nats_client.publish.call_args[0]
        assert subject == "gough.audit.exported"
        ev = json.loads(payload.decode("utf-8"))
        assert ev["target_sub"] == "auditor@acme"
        # Content-Disposition for download
        assert "attachment" in resp.headers["Content-Disposition"]
        assert "gough-audit-test-cluster.jsonl" in resp.headers["Content-Disposition"]

    async def test_export_no_nats_client_does_not_fail(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_audit_event(pg_db)
        app = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.cluster.superadmin"}, mfa=True),
            dal_db=pg_db,
            monkeypatch=monkeypatch,
        )
        client = app.test_client()
        resp = await client.get("/api/v1/audit/export")
        assert resp.status_code == 200

    async def test_export_nats_publish_failure_does_not_break_export(
        self, pg_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_audit_event(pg_db)
        nats_client = MagicMock()
        nats_client.publish.side_effect = RuntimeError("nats down")
        app = _build_app(
            rows=[],
            principal=_principal(scopes={"gough.cluster.superadmin"}, mfa=True),
            nats_client=nats_client,
            dal_db=pg_db,
            monkeypatch=monkeypatch,
        )
        client = app.test_client()
        resp = await client.get("/api/v1/audit/export")
        assert resp.status_code == 200

    async def test_export_self_audit_write_succeeds_under_rls_and_is_tenant_scoped(
        self, pg_db: Any, pg_db_scoped: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for the ``set_tenant_guc`` removal (FIX #7a cleanup).

        The self-audit-log write inside ``export_audit_log``
        (``_build_audit_writer`` -> ``AuditEventWriter.append()``) used to
        run through ``set_tenant_guc(_get_db_session(), tenant_id)`` --
        ``_get_db_session()`` always raised ``RuntimeError`` in production
        (``DB_SESSION_FACTORY`` is never wired into ``create_app()``), so
        this write never actually happened outside of tests. Exercised here
        against the real, non-owner ``api-manager-rw`` role
        (``pg_db_scoped``) to prove the write succeeds under genuine RLS
        enforcement and is correctly scoped to the requesting tenant -- a
        different tenant's RLS-scoped connection cannot see the row.

        The export STREAM itself remaining single-tenant-scoped under a
        non-``cross_tenant`` token (rather than seeing every tenant) is a
        separate, pre-existing, already-flagged gap -- see
        ``app.security.audit_chain``'s module docstring -- not exercised
        here; this test is only about the self-audit-write's isolation.
        """
        principal = _principal(
            scopes={"gough.cluster.superadmin"}, mfa=True, tenant="tenant-a"
        )
        app = _build_app(
            rows=[],
            principal=principal,
            dal_db=pg_db_scoped,
            monkeypatch=monkeypatch,
            rls_scoped=True,
        )
        client = app.test_client()
        resp = await client.get("/api/v1/audit/export?target_sub=auditor@acme")
        assert resp.status_code == 200
        await resp.get_data()  # drain the stream

        written = (
            pg_db(
                (pg_db.audit_events.action == "audit.log.export")
                & (pg_db.audit_events.tenant_id == "tenant-a")
            )
            .select()
            .first()
        )
        assert written is not None

        from app.db.rls import set_current_tenant

        set_current_tenant("tenant-b")
        try:
            invisible = (
                pg_db_scoped(
                    (pg_db_scoped.audit_events.action == "audit.log.export")
                    & (pg_db_scoped.audit_events.id == written.id)
                )
                .select()
                .first()
            )
        finally:
            set_current_tenant(None)
        assert invisible is None


# =============================================================================
# Compliance defaults
# =============================================================================


class TestComplianceLaneConfig:
    def test_default_lanes(self) -> None:
        assert {"fedramp", "hipaa", "pci"} <= set(DEFAULT_MFA_REQUIRED_LANES)
