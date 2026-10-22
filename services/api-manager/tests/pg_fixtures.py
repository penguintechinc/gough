"""Real-Postgres test fixtures.

Reuses ``$DATABASE_URL`` when present (CI service container); otherwise spins
an ephemeral ``postgres:16-bookworm`` container via testcontainers for the
duration of the pytest session.

Schema is built exactly once per session by running the real Alembic
migration chain (``alembic upgrade head``, currently a single baseline --
``alembic/versions/20260805_1000_baseline_full_schema.py``) against the
target database. This is the same migration that builds production schema
(62 tables, 4 views, RLS policies, per-service DB roles), so this fixture and
production share one schema authority instead of drifting via a hand-rolled
``Base.metadata.create_all()``. Function-scoped ``pg_db`` gets per-test
isolation by TRUNCATE-ing every data table between tests rather than
re-running migrations per test, which would be both slow and unnecessary --
the schema itself never changes mid-session.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from penguin_dal import DB

SERVICE_ROOT = Path(__file__).resolve().parent.parent

# Non-owner role the baseline migration creates for the api-manager service
# (``CREATE ROLE "api-manager-rw" WITH LOGIN`` -- no password set there;
# production wires that in out-of-repo via a secrets-manager-driven ALTER
# ROLE). Postgres exempts table OWNERS from RLS entirely, so ``pg_db``
# (which connects as the migration/owner role) can never actually exercise
# RLS -- this is precisely why the FIX #7a GUC-wiring bug went unnoticed.
# ``pg_db_scoped`` below connects as this role instead so RLS is genuinely
# enforced.
_SCOPED_ROLE = "api-manager-rw"


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """Session-scoped Postgres connection URL (``postgresql://...``).

    Reuses ``$DATABASE_URL`` when set (CI service container). Otherwise spins
    an ephemeral ``postgres:16-bookworm`` container via testcontainers, kept
    alive for the whole test session and torn down at the end.
    """
    existing = os.getenv("DATABASE_URL")
    if existing:
        yield existing.replace("postgresql+psycopg2://", "postgresql://")
        return

    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    with PostgresContainer("postgres:16-bookworm") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


def _upgrade_head(pg_url: str) -> None:
    """Run ``alembic upgrade head`` against ``pg_url``.

    ``alembic/env.py`` resolves its connection string from
    ``app.config.Config.get_db_uri()``, which is built from ``DB_HOST`` /
    ``DB_PORT`` / ``DB_USER`` / ``DB_PASS`` / ``DB_NAME`` / ``DB_TYPE`` read
    at class-definition (first-import) time -- not from ``$DATABASE_URL``
    directly. Point Alembic at the test database by setting those env vars
    from the parsed ``pg_url`` before invoking it, then restore whatever was
    there beforehand so this doesn't leak into other tests/fixtures.

    ``env.py`` also calls ``logging.config.fileConfig()``, which -- with its
    default ``disable_existing_loggers=True`` -- permanently disables every
    ``logging.Logger`` already registered under a name not listed in
    ``alembic.ini``'s ``[loggers]`` section (only ``root``, ``sqlalchemy``,
    ``alembic`` are). A real, subprocess ``alembic upgrade head`` never
    surfaces this (nothing else shares that process' logging state), but
    running it in-process here would otherwise silently and permanently mute
    every other module's logger for the rest of the pytest session (caught
    empirically: it broke an unrelated ``caplog`` assertion elsewhere in the
    suite). Re-enable anything fileConfig newly disabled once the upgrade is
    done, leaving loggers that were already disabled beforehand alone.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig

    logger_dict = logging.Logger.manager.loggerDict
    already_disabled = {
        name
        for name, candidate in logger_dict.items()
        if isinstance(candidate, logging.Logger) and candidate.disabled
    }

    parsed = urlparse(pg_url)
    overrides = {
        "DB_TYPE": "postgresql",
        "DB_HOST": parsed.hostname or "localhost",
        "DB_PORT": str(parsed.port or 5432),
        "DB_NAME": (parsed.path or "/").lstrip("/"),
        "DB_USER": parsed.username or "",
        "DB_PASS": parsed.password or "",
    }
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        cfg = AlembicConfig(str(SERVICE_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(SERVICE_ROOT / "alembic"))
        command.upgrade(cfg, "head")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name, candidate in list(logger_dict.items()):
            if (
                isinstance(candidate, logging.Logger)
                and candidate.disabled
                and name not in already_disabled
            ):
                candidate.disabled = False


@pytest.fixture(scope="session")
def _pg_schema(pg_url: str) -> None:
    """Build the full schema exactly once per session via ``alembic upgrade head``.

    Only pulled in transitively by ``pg_db`` -- tests that never request
    ``pg_db``/``pg_url`` never spin up Postgres or need Docker at all.
    """
    _upgrade_head(pg_url)


def _truncate_all(db: DB) -> None:
    """TRUNCATE every reflected table except ``alembic_version``.

    Cheap per-test isolation: wipes data without re-running migrations, which
    only need to happen once per session since the schema itself is static.
    """
    table_names = [name for name in db.tables if name != "alembic_version"]
    if not table_names:
        return
    quoted = ", ".join(f'"{name}"' for name in table_names)
    db.executesql(
        f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE",
        check_injection=False,
    )


@pytest.fixture
def pg_db(pg_url: str, _pg_schema: None) -> Iterator[DB]:
    """Function-scoped penguin-dal ``DB`` bound to a real Postgres instance.

    The schema is built once per session (``_pg_schema``, via
    ``alembic upgrade head``). Each test gets isolated data by TRUNCATE-ing
    every table before and after it runs -- migrations are never re-run
    per test.
    """
    db = DB(pg_url, pool_size=2, reflect=True)
    try:
        _truncate_all(db)
        yield db
        _truncate_all(db)
    finally:
        db.close()


@pytest.fixture(scope="session")
def _pg_scoped_role_password(pg_url: str, _pg_schema: None) -> str:
    """Set a login password on the ``api-manager-rw`` role, once per session.

    The baseline migration creates the role with ``WITH LOGIN`` but no
    password -- production sets that separately (secrets-manager-driven, out
    of repo). Generated fresh per session and scoped only to the ephemeral
    test database, never persisted or reused across runs.
    """
    password = secrets.token_urlsafe(24)
    owner_db = DB(pg_url, pool_size=1, reflect=False)
    try:
        owner_db.executesql(
            f'ALTER ROLE "{_SCOPED_ROLE}" WITH LOGIN PASSWORD %s',
            (password,),
        )
    finally:
        owner_db.close()
    return password


def _scoped_pg_url(pg_url: str, password: str) -> str:
    """Build a connection URL for ``_SCOPED_ROLE``, reusing ``pg_url``'s host/port/db."""
    parsed = urlparse(pg_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    dbname = (parsed.path or "/").lstrip("/")
    return f"postgresql://{_SCOPED_ROLE}:{password}@{host}:{port}/{dbname}"


@pytest.fixture
def pg_db_scoped(pg_url: str, _pg_scoped_role_password: str) -> Iterator[DB]:
    """Function-scoped penguin-dal ``DB`` connected as the non-owner ``api-manager-rw`` role.

    Unlike ``pg_db`` (which connects as the migration/table OWNER -- exempt
    from RLS by Postgres by default), this connects as the actual scoped
    application role the baseline ``GRANT``s SELECT/INSERT/UPDATE to, so Row
    Level Security policies are genuinely evaluated on every query. Use this
    fixture -- never ``pg_db`` -- for any test that asserts RLS tenant
    isolation actually filters rows.

    Does NOT truncate tables (that's ``pg_db``'s job, and ``api-manager-rw``
    lacks the privileges to run it): seed data via ``pg_db`` (owner) first,
    then read it back through this fixture.
    """
    db = DB(_scoped_pg_url(pg_url, _pg_scoped_role_password), pool_size=2, reflect=True)
    try:
        yield db
    finally:
        db.close()
