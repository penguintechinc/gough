"""
RLS-wired, app-context-free penguin-dal accessor for background/worker code.

Regression: gh-22 (DB pool consolidation). Prior to this fix, gough ran TWO
independent penguin-dal connection pools:

* ``app.models``' ``init_db()``/``get_db()`` -- built from
  ``Config.get_db_uri()`` (``DB_*`` env vars), RLS-wired via
  ``install_rls_events`` at app startup, but only reachable through Quart's
  ``quart.g``/``current_app`` -- i.e. it REQUIRES an active app context.
* This module's ``init_db()``/``get_db()`` -- built from a bare
  ``DATABASE_URL`` env var (set only in CI, never in production Helm
  values), NOT RLS-wired at all, but usable with no app context.

Neither pool covered both requirements at once, so any code that needed
"no app context" AND "RLS-wired" (background workers: the SMART sweeper,
the audit chain writer's leader-lease/append/mirror/verify paths, any
future supercronic-scheduled job) had nowhere correct to turn -- and
``app.api.disks`` used this module's pool for ordinary REQUEST-path queries
against RLS-protected tables (``disks``, ``disk_plans``), which 500'd in
every real deployment (no ``DATABASE_URL`` there) and would have silently
returned zero rows under RLS even if it hadn't.

Consolidated design -- both pools now point at the same database and are
both RLS-wired, but remain two distinct pools (not merged into one engine
object; a request pool and a worker pool legitimately coexist -- see
``app.db.rls``'s checkout/checkin GUC wiring, which is per-engine):

* ``app.models.get_db()`` -- REQUEST-path accessor. Use from Quart route
  handlers (``app.api.*`` blueprints). Requires ``quart.g``/``current_app``;
  the request's tenant middleware has already pushed the tenant onto
  ``app.db.rls``'s ``ContextVar`` by the time a handler calls this.
* ``app.db.database.get_db()`` (this module) -- WORKER/no-context accessor.
  Use from anything that does NOT run inside Quart's request pipeline:
  leader-only background workers (``app.workers.smart_sweeper``), the audit
  chain writer's injected ``db_session`` (``app.workers.audit_chain_writer``),
  gRPC server handlers that aren't backed by a request context, supercronic
  entrypoints. Thread-local, built lazily on first ``get_db()`` call per
  thread -- no Quart machinery involved at all.

Both resolve the SAME underlying database: ``DATABASE_URL`` (SQLAlchemy-
format, e.g. ``postgresql://...``) if set -- CI's service-container jobs set
this -- otherwise ``Config.get_db_uri()`` (the ``DB_*`` env vars), exactly
the source ``app.models.init_db()`` uses. Both install RLS GUC wiring
(``app.db.rls.install_rls_events``) on their own engine at construction
time, so a query issued through either pool carries the tenant currently set
on ``app.db.rls``'s ``ContextVar`` (or fails closed to zero rows if unset --
see that module's docstring). Background workers that intentionally operate
across every tenant (the SMART sweeper, the audit chain writer) push
``app.db.rls.CROSS_TENANT_SENTINEL`` for the duration of their DB work
rather than relying on an ambient per-request tenant that will never be set
outside Quart's request pipeline.

Pick the accessor for your context, not for convenience -- both point at the
same data, so there is no functional reason to reach for one over the other
except which surface (Quart request vs. no context) is actually running.

Thread Safety:
- Thread-local storage for database connections (this module's ``get_db()``)
- Connection pooling via penguin-dal
- Safe for use with ``asyncio.to_thread()`` for blocking operations
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from penguin_dal import DB
from penguintechinc_utils import get_logger

from ..config import Config
from .rls import install_rls_events

logger = get_logger(__name__)

# Thread-local storage for database connections
_thread_local = threading.local()


def init_db(
    database_url: Optional[str] = None,
    pool_size: Optional[int] = None,
) -> DB:
    """Build a new RLS-wired penguin-dal ``DB`` instance, no app context required.

    Resolution order for the connection URI: the explicit ``database_url``
    argument, then the ``DATABASE_URL`` env var (SQLAlchemy-format,
    ``postgresql://...`` -- CI's service-container jobs set this), then
    ``Config.get_db_uri()`` (the ``DB_*`` env vars) -- the same source
    ``app.models.init_db()`` uses, converted from PyDAL to SQLAlchemy format
    exactly the way that module does. This guarantees both pools resolve to
    the same database whenever ``DATABASE_URL`` isn't explicitly overriding
    it for a given environment (e.g. CI).

    Args:
        database_url: Explicit SQLAlchemy URI override (``postgresql://``,
            ``mysql+pymysql://``, ``sqlite:///``). Defaults to the
            ``DATABASE_URL`` env var, then ``Config.get_db_uri()``.
        pool_size: Connection pool size. Defaults to ``Config.DB_POOL_SIZE``
            (same default the request-path pool uses).

    Returns:
        A penguin-dal ``DB`` instance with RLS GUC wiring installed on its
        engine (``app.db.rls.install_rls_events`` -- a no-op for non-Postgres
        dialects).
    """
    # database_url / DATABASE_URL are already SQLAlchemy-format (the CI
    # service-container jobs that set DATABASE_URL use `postgresql://...`
    # directly) -- only Config.get_db_uri()'s PyDAL-format output
    # (`postgres://`, `sqlite://file`) needs conversion.
    # convert_pydal_to_sqlalchemy_uri() is NOT idempotent for sqlite (PyDAL's
    # `sqlite://file` and SQLAlchemy's `sqlite:///file` differ by exactly one
    # slash), so it must never run on an input that might already be
    # SQLAlchemy-format -- running it unconditionally here would turn an
    # already-correct `sqlite:///file` into `sqlite:////file`.
    sa_uri = database_url or os.getenv("DATABASE_URL")
    if not sa_uri:
        from ..models_sqlalchemy import convert_pydal_to_sqlalchemy_uri

        sa_uri = convert_pydal_to_sqlalchemy_uri(Config.get_db_uri())
    resolved_pool_size = pool_size if pool_size is not None else Config.DB_POOL_SIZE

    logger.info(
        f"Initializing app.db.database penguin-dal pool "
        f"(pool_size={resolved_pool_size}, RLS-wired)"
    )

    db = DB(sa_uri, pool_size=resolved_pool_size)
    install_rls_events(db.engine)

    logger.info("app.db.database penguin-dal pool initialized successfully")
    return db


def get_db() -> DB:
    """Get the thread-local, RLS-wired database connection -- no Quart app context required.

    This is the accessor for code that cannot rely on ``quart.g``/
    ``current_app`` -- see this module's docstring for the full rule on
    which accessor (this one vs. ``app.models.get_db()``) to use where.

    Returns:
        penguin-dal ``DB`` instance for the current thread. Lazily built on
        first call per thread via ``init_db()``.
    """
    if not hasattr(_thread_local, 'db') or _thread_local.db is None:
        _thread_local.db = init_db()

    return _thread_local.db


def close_db() -> None:
    """
    Close thread-local database connection.

    Safe to call multiple times.
    """
    if hasattr(_thread_local, 'db') and _thread_local.db is not None:
        try:
            _thread_local.db.close()
            logger.debug("Database connection closed")
        except Exception as e:
            logger.error(f"Error closing database connection: {e}", exc_info=True)
        finally:
            _thread_local.db = None


@contextmanager
def get_db_context() -> Iterator[DB]:
    """
    Context manager for database connections.

    Usage:
        with get_db_context() as db:
            rows = db(db.api_definitions).select()
    """
    db = get_db()
    try:
        yield db
    finally:
        db.commit()
