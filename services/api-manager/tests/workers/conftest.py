"""Worker-test conftest — isolates tests from the full Quart application stack.

The ``app/__init__.py`` factory imports Quart, quart-cors, penguin-aaa, and
several app-layer modules (middleware, models, etc.) that are either not
installed in the local dev environment or contain pre-existing import errors
(e.g. ``get_user_by_id`` missing from ``app.models``).  None of the worker
modules under test depend on the application factory.

This conftest installs lightweight ``sys.modules`` stubs *before* any
``app.*`` import resolves, allowing the test suite to import
``app.workers.plan_compiler`` (and sibling modules) without triggering the
full factory.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# Registers the pg_url / pg_db / pg_db_scoped real-Postgres fixtures
# (tests/pg_fixtures.py) for tests under tests/workers/ specifically.
# tests/conftest.py (one level up) already declares `pytest_plugins =
# ["tests.pg_fixtures"]`, but pytest only honors a conftest.py's
# `pytest_plugins` list when that conftest sits at the *rootdir* -- when
# pytest is invoked with a path scoped to tests/workers/ (e.g. the Makefile's
# `pytest tests/workers/` target), pytest can resolve its rootdir to
# tests/workers/ itself (it has its own pytest.ini), one level *below*
# tests/conftest.py, silently dropping that outer declaration (confirmed
# empirically: tests/conftest.py's own non-pytest_plugins fixtures, e.g.
# `dal`, still inherit normally in that case -- only the plugin-loading
# mechanism is affected). And when invoked from a path where rootdir climbs
# all the way to the monorepo root (e.g. `pytest tests/` from this service
# directory, which picks up ../../../pytest.ini), pytest 8 hard-errors on
# *any* non-rootdir conftest declaring `pytest_plugins` at all -- see
# https://docs.pytest.org/en/stable/deprecations.html#pytest-plugins-in-non-top-level-conftest-files
#
# Importing the fixtures directly sidesteps both failure modes: fixtures
# pulled into a conftest.py via a plain import are registered on that
# conftest's own scope regardless of rootdir, so pg_db et al. are available
# to tests/workers/ under every invocation path.
#
# Only do this when tests/conftest.py's own `pytest_plugins` declaration
# genuinely was NOT honored for this invocation (rootdir resolved to
# tests/workers/ itself, so tests/conftest.py -- one level above rootdir --
# never loads at all). Pytest processes `pytest_plugins` synchronously while
# loading the conftest that declares it, and ancestor conftests always load
# before descendant ones, so by the time *this* conftest is imported,
# `tests.pg_fixtures` is already in `sys.modules` in every invocation where
# tests/conftest.py did participate. Re-importing the names in that case
# would register a *second*, independent set of session-scoped fixturedefs
# local to this conftest -- pytest treats an imported `@pytest.fixture`
# object as a new override at the importing module's scope regardless of
# where it was originally defined -- spinning up a second, separate
# Testcontainers Postgres + full Alembic migration run purely for tests
# collected under tests/workers/, alongside the one tests/conftest.py's
# plugin registration already provides for the rest of the session.
if "tests.pg_fixtures" not in sys.modules:
    from tests.pg_fixtures import (  # noqa: F401
        _pg_schema,
        _pg_scoped_role_password,
        pg_db,
        pg_db_scoped,
        pg_url,
    )


def _stub(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__spec__ = None  # type: ignore[attr-defined]
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _install_stubs() -> None:
    """Pre-populate sys.modules with stubs before any app.* import."""
    # Only install if the full app package hasn't been imported cleanly yet.
    if "app" in sys.modules and not isinstance(sys.modules["app"], types.ModuleType):
        return

    # Heavy third-party deps that may not be installed in CI worker envs.
    # Stub only the ones genuinely missing here -- blindly stubbing anything
    # merely absent from sys.modules (rather than absent from the
    # environment) replaced real, installed packages (quart, hvac, nats,
    # penguin_aaa) with a permanent, session-wide MagicMock the instant this
    # conftest was imported during collection, corrupting unrelated tests
    # (e.g. tests/test_grpc_server.py's Vault/AESGCM decrypt path) that
    # import the real module later in the same pytest session -- sys.modules
    # mutations here are never undone, so the corruption outlives this file's
    # own tests entirely (regression: gh-41).
    import importlib

    for dep in (
        "quart",
        "quart.globals",
        "quart_cors",
        "hypercorn",
        "hypercorn.config",
        "penguin_aaa",
        "penguin_aaa.middleware",
        "penguin_aaa.middleware.asgi",
        "penguin_aaa.audit",
        "penguin_aaa.audit.emitter",
        "pyspiffe",
        "pyspiffe.workloadapi",
        "pyspiffe.workloadapi.default_workload_api_client",
        "hvac",
        "hvac.exceptions",
        "nats",
        "nats.aio",
        "nats.aio.client",
    ):
        if dep in sys.modules:
            continue
        try:
            importlib.import_module(dep)
        except ImportError:
            sys.modules[dep] = MagicMock()

    # Stub the app package itself so __init__.py never executes.
    # Must be a real package (has __path__) so sub-module imports work.
    if "app" not in sys.modules:
        import importlib.util
        import pathlib

        # Create a proper namespace package backed by the real app/ directory
        app_dir = str(pathlib.Path(__file__).parent.parent.parent / "app")
        app_spec = importlib.util.spec_from_file_location(
            "app",
            str(pathlib.Path(app_dir) / "__init__.py"),
            submodule_search_locations=[app_dir],
        )
        app_mod = types.ModuleType("app")
        app_mod.__path__ = [app_dir]  # type: ignore[attr-defined]
        app_mod.__package__ = "app"
        app_mod.__spec__ = app_spec  # type: ignore[attr-defined]
        sys.modules["app"] = app_mod

    # Stub sub-packages that get imported transitively
    for sub in (
        "app.config",
        "app.models",
        "app.models.ipxe",
        "app.security_datastore",
        "app.audit",
        "app.rate_limit",
        "app.ssh_ca",
        "app.websocket",
        "app.middleware",
        "app.auth",
        "app.auth.__init__",
    ):
        if sub not in sys.modules:
            sys.modules[sub] = MagicMock()


_install_stubs()


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

import os
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture()
def db_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path}/worker-test-{os.getpid()}-{threading.get_ident()}.db"


@pytest.fixture()
def dal(db_url, monkeypatch):
    """Fresh in-memory SQLite DAL with schema expected by worker tests."""
    from penguin_dal import DB, Field

    db = DB(db_url, pool_size=1, reflect=False, migrate=True)
    db.define_table(
        "nodes",
        Field("tenant_id", "string", default="__default__"),
        Field("name", "string"),
        Field("state", "string", default="new"),
        Field("dmi_uuid", "string"),
        Field("primary_nic_mac", "string"),
        Field("hardware_tags", "json"),
        migrate=True,
    )
    db.define_table(
        "disks",
        Field("node_id", "integer", notnull=True),
        Field("tenant_id", "string", default="__default__"),
        Field("device_path", "string", notnull=True),
        Field("serial", "string"),
        Field("capacity_bytes", "bigint"),
        Field("rotational", "boolean", default=False),
        Field("smart_status", "string", default="unknown"),
        Field("smart_attributes_json", "json"),
        Field("reserved_for_storage", "boolean", default=False),
        Field("storage_backend", "string"),
        Field("tier", "string", default="bulk"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )
    db.define_table(
        "leader_leases",
        Field("lease_name", "string", unique=True),
        Field("holder_id", "string"),
        Field("acquired_at", "datetime"),
        Field("expires_at", "datetime"),
        Field("version", "integer", default=0),
        migrate=True,
    )

    from app.db import database as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    import app.workers.smart_sweeper as sweeper_mod
    monkeypatch.setattr(sweeper_mod, "get_db", lambda: db)

    yield db
    try:
        db.close()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def mock_audit_chain_writer_metrics() -> None:
    """Mock Prometheus metrics in audit_chain_writer to prevent call_count errors.

    The production code calls COUNTER.inc() but tests expect .inc.call_count.
    This fixture patches the metrics module-level singletons with MagicMocks
    so tests can track invocation counts.
    """
    from app.workers import audit_chain_writer as acw_module

    for attr in [
        "CHAIN_BREAK_COUNTER",
        "MIRROR_LAG_GAUGE",
        "MIRROR_SHIPPED_COUNTER",
        "MIRROR_FAILURE_COUNTER",
        "APPEND_COUNTER",
        "NOT_LEADER_COUNTER",
    ]:
        if hasattr(acw_module, attr):
            metric = getattr(acw_module, attr)
            # Replace .inc / .set with MagicMock that tracks calls
            if hasattr(metric, "inc"):
                metric.inc = MagicMock()
            if hasattr(metric, "set"):
                metric.set = MagicMock()


def pytest_runtest_setup(item):
    """Print test name when it starts."""
    print(f"\n[START] {item.name}")


def pytest_runtest_teardown(item):
    """Print test name when it ends."""
    print(f"[END] {item.name}")


@pytest.fixture(autouse=True)
def clear_prometheus_registry() -> None:
    """Clear prometheus registry before each test to avoid duplicate metric errors.

    When multiple tests run, if they all import modules that register the same
    prometheus metrics, the registry will complain about duplicate timeseries.
    This fixture clears the registry before each test.
    """
    try:
        from prometheus_client import REGISTRY
        # Clear all collectors from the registry
        for collector in list(REGISTRY._collector_to_names.keys()):
            try:
                REGISTRY.unregister(collector)
            except Exception:
                pass
    except ImportError:
        pass
    yield
    # Also clear after the test
    try:
        from prometheus_client import REGISTRY
        for collector in list(REGISTRY._collector_to_names.keys()):
            try:
                REGISTRY.unregister(collector)
            except Exception:
                pass
    except ImportError:
        pass


