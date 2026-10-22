"""Real-Postgres tests for app.db.database's reworked accessor. Regression: gh-22.

Proves the two things the DB pool consolidation exists to fix, for THIS
specific pool (``test_rls_isolation.py`` only ever exercises ``pg_db_scoped``,
a ``DB`` instance built directly from ``pg_url`` by the fixture -- never
through ``app.db.database.get_db()``'s own ``init_db()``/
``Config.get_db_uri()`` resolution path, so it can't prove this module's own
wiring is correct):

1. ``app.db.database.get_db()`` works with NO Quart app context active at
   all -- the entire point of the consolidation (see that module's
   docstring): background workers can't rely on ``quart.g``/``current_app``.
2. The engine it builds is genuinely RLS-wired (``install_rls_events()`` is
   now called INSIDE ``init_db()`` itself, not left to the caller like the
   old ``app.models.init_db()`` pattern) -- proven with the same two-tenant
   isolation pattern ``test_rls_isolation.py`` uses, through this module's
   own ``get_db()``/``_thread_local`` path.

``app.config.Config`` resolves its ``DB_*`` class attributes from
``os.environ`` ONCE, at first import of that module -- by the time these
tests run, something else in the session has almost certainly already
imported ``app.config`` under whatever default env was present then, so
simply calling ``monkeypatch.setenv(...)`` here would not reliably change
what ``Config.get_db_uri()`` returns. ``tests/pg_fixtures.py``'s
``_upgrade_head()`` sidesteps the identical problem for Alembic by importing
a *separate*, never-before-imported bare ``config`` module rather than
``app.config`` -- not an option here, since ``app.db.database`` imports the
real ``app.config.Config`` object directly. Monkeypatching ``Config``'s
class attributes directly is the reliable equivalent, regardless of import
order.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from quart import has_app_context

from app.config import Config
from app.db import database
from app.db.rls import CROSS_TENANT_SENTINEL, set_current_tenant

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


@pytest.fixture
def scoped_db_env(pg_url, _pg_scoped_role_password, monkeypatch):
    """Point ``Config.get_db_uri()`` at the test Postgres instance, connecting
    as the non-owner ``api-manager-rw`` role so RLS is genuinely enforced
    (see ``tests/pg_fixtures.py``'s ``pg_db_scoped`` docstring for why the
    table-OWNER ``pg_db`` fixture can't prove this), and clears
    ``app.db.database``'s thread-local cache before and after so no state
    leaks to/from other tests sharing this worker thread.
    """
    parsed = urlparse(pg_url)
    monkeypatch.setattr(Config, "DB_TYPE", "postgres")
    monkeypatch.setattr(Config, "DB_HOST", parsed.hostname or "localhost")
    monkeypatch.setattr(Config, "DB_PORT", str(parsed.port or 5432))
    monkeypatch.setattr(Config, "DB_NAME", (parsed.path or "/").lstrip("/"))
    monkeypatch.setattr(Config, "DB_USER", "api-manager-rw")
    monkeypatch.setattr(Config, "DB_PASS", _pg_scoped_role_password)
    # DATABASE_URL, if set in this environment, would short-circuit init_db()
    # before it ever consults Config -- must be cleared for this fixture's
    # whole point (proving the Config.get_db_uri() fallback path) to hold.
    monkeypatch.delenv("DATABASE_URL", raising=False)

    database._thread_local.db = None
    yield
    if getattr(database._thread_local, "db", None) is not None:
        database._thread_local.db.close()
    database._thread_local.db = None


def _seed_biome(owner_db, *, name: str, tenant_id: str) -> int:
    """Insert a minimal ``biomes`` row as the table OWNER (bypasses RLS on insert)."""
    return owner_db.biomes.insert(name=name, tenant_id=tenant_id)


@pytest.fixture
def two_tenant_biomes(pg_db) -> tuple[int, int]:
    id_a = _seed_biome(pg_db, name="pool-consolidation-a", tenant_id=TENANT_A)
    id_b = _seed_biome(pg_db, name="pool-consolidation-b", tenant_id=TENANT_B)
    return id_a, id_b


def test_get_db_works_without_app_context(
    scoped_db_env, two_tenant_biomes: tuple[int, int]
) -> None:
    """The core consolidation proof: get_db() must not raise with no Quart
    app context pushed at all, and must return a thread-local-cached
    instance (same object on a second call, no re-init)."""
    assert not has_app_context()

    db = database.get_db()
    assert db is database.get_db()  # thread-local cache -- same instance

    set_current_tenant(TENANT_A)
    try:
        rows = db(db.biomes.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A}
    assert len(rows) == 1


def test_get_db_tenant_b_sees_only_own_row(
    scoped_db_env, two_tenant_biomes: tuple[int, int]
) -> None:
    """Symmetric check, same pool: tenant B's GUC value returns only tenant
    B's row, never tenant A's."""
    assert not has_app_context()
    db = database.get_db()

    set_current_tenant(TENANT_B)
    try:
        rows = db(db.biomes.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_B}
    assert len(rows) == 1


def test_get_db_unset_tenant_is_fail_closed(
    scoped_db_env, two_tenant_biomes: tuple[int, int]
) -> None:
    """No tenant on the ContextVar -> zero rows (fail closed), proving
    install_rls_events() is genuinely wired on THIS pool's engine -- if it
    weren't, the scoped role would see nothing set on app.current_tenant at
    all in exactly the same way, so this alone doesn't distinguish "wired
    but unset" from "never wired"; paired with
    test_get_db_works_without_app_context's non-empty, tenant-scoped result
    above, the pair together proves the wiring is both present and correct.
    """
    assert not has_app_context()
    db = database.get_db()

    set_current_tenant(None)
    rows = db(db.biomes.id > 0).select()

    assert len(rows) == 0


def test_get_db_cross_tenant_sentinel_sees_all_rows(
    scoped_db_env, two_tenant_biomes: tuple[int, int]
) -> None:
    """The cross-tenant sentinel (used by background workers that
    intentionally sweep every tenant -- e.g. app.workers.smart_sweeper)
    bypasses tenant filtering on this pool exactly as it does on the
    request-path pool."""
    assert not has_app_context()
    db = database.get_db()

    set_current_tenant(CROSS_TENANT_SENTINEL)
    try:
        rows = db(db.biomes.id > 0).select()
    finally:
        set_current_tenant(None)

    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {TENANT_A, TENANT_B}
    assert len(rows) == 2


def test_get_db_falls_back_to_config_when_database_url_unset(
    scoped_db_env,
) -> None:
    """Regression: gh-22. Proves get_db() actually resolved its connection
    via Config.get_db_uri() (the DB_* attributes scoped_db_env patched onto
    Config) rather than silently reusing some other cached URL -- connects
    successfully and reports the expected database name back from Postgres
    itself, not just "didn't raise"."""
    db = database.get_db()
    rows = db.executesql("SELECT current_database()")
    assert rows[0][0] == Config.DB_NAME
