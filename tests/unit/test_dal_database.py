"""Unit tests for penguin-dal database module (api-manager/app/db/database.py).

All tests use unittest.mock to avoid requiring a real database connection.
Real-Postgres coverage for this module's app-context-free RLS wiring lives
in ``services/api-manager/tests/test_db_database_rls.py`` (gh-22).
"""
import os
import sys

_API_MANAGER_PATH = '/home/penguin/code/gough/services/api-manager'
if _API_MANAGER_PATH not in sys.path:
    sys.path.insert(0, _API_MANAGER_PATH)
# Evict any stale 'app' module cached from another path (e.g. penguincode/app.py)
for _mod in list(sys.modules.keys()):
    if _mod == 'app' or _mod.startswith('app.'):
        del sys.modules[_mod]

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime


class TestInitDb:
    """Tests for init_db() function.

    Regression: gh-22 (DB pool consolidation). ``init_db()`` no longer
    raises when ``DATABASE_URL`` is unset -- it falls back to
    ``Config.get_db_uri()`` (the same ``DB_*``-env-var source
    ``app.models.init_db()`` uses), which always resolves to a URI rather
    than raising. It also now installs RLS GUC wiring
    (``app.db.rls.install_rls_events``) on the engine it builds.
    """

    def test_init_db_returns_db_instance(self):
        """init_db() with explicit URL returns a DB instance."""
        with patch('app.db.database.DB') as mock_db_cls, \
                patch('app.db.database.install_rls_events'):
            mock_db_cls.return_value = MagicMock()
            from app.db.database import init_db
            result = init_db(database_url='sqlite:///:memory:', pool_size=5)
            mock_db_cls.assert_called_once_with('sqlite:///:memory:', pool_size=5)
            assert result is mock_db_cls.return_value

    def test_init_db_installs_rls_events(self):
        """init_db() wires RLS GUC events onto the new DB's engine."""
        with patch('app.db.database.DB') as mock_db_cls, \
                patch('app.db.database.install_rls_events') as mock_install:
            mock_instance = MagicMock()
            mock_db_cls.return_value = mock_instance
            from app.db.database import init_db
            init_db(database_url='sqlite:///:memory:', pool_size=5)
            mock_install.assert_called_once_with(mock_instance.engine)

    def test_init_db_falls_back_to_config_when_no_url(self):
        """init_db() falls back to Config.get_db_uri() (converted to
        SQLAlchemy format) when neither an explicit URL nor DATABASE_URL is
        given -- never raises."""
        import importlib
        import app.db.database as db_mod
        from app.config import Config

        env = {k: v for k, v in os.environ.items() if k != 'DATABASE_URL'}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(Config, 'DB_TYPE', 'postgres'), \
                patch.object(Config, 'DB_HOST', 'fallback-host'), \
                patch.object(Config, 'DB_PORT', '5432'), \
                patch.object(Config, 'DB_NAME', 'fallback_db'), \
                patch.object(Config, 'DB_USER', 'fallback_user'), \
                patch.object(Config, 'DB_PASS', 'fallback_pass'), \
                patch('app.db.database.DB') as mock_db_cls, \
                patch('app.db.database.install_rls_events'):
            mock_db_cls.return_value = MagicMock()
            db_mod.init_db()
            call_args = mock_db_cls.call_args
            assert call_args[0][0] == (
                'postgresql://fallback_user:fallback_pass@fallback-host:5432/fallback_db'
            )

    def test_init_db_uses_env_var(self):
        """init_db() reads DATABASE_URL from environment when no explicit URL given."""
        with patch('app.db.database.DB') as mock_db_cls, \
                patch('app.db.database.install_rls_events'):
            mock_db_cls.return_value = MagicMock()
            with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://localhost/test'}):
                from app.db.database import init_db
                result = init_db()
                mock_db_cls.assert_called_once()
                call_args = mock_db_cls.call_args
                assert call_args[0][0] == 'postgresql://localhost/test'

    def test_init_db_uses_default_pool_size(self):
        """init_db() uses Config.DB_POOL_SIZE (10) by default."""
        with patch('app.db.database.DB') as mock_db_cls, \
                patch('app.db.database.install_rls_events'):
            mock_db_cls.return_value = MagicMock()
            with patch.dict(os.environ, {'DATABASE_URL': 'sqlite:///:memory:'}):
                from app.db.database import init_db
                init_db()
                call_kwargs = mock_db_cls.call_args[1]
                assert call_kwargs.get('pool_size') == 10

    def test_init_db_explicit_pool_size(self):
        """init_db() passes explicit pool_size to DB constructor."""
        with patch('app.db.database.DB') as mock_db_cls, \
                patch('app.db.database.install_rls_events'):
            mock_db_cls.return_value = MagicMock()
            from app.db.database import init_db
            init_db(database_url='sqlite:///:memory:', pool_size=20)
            call_kwargs = mock_db_cls.call_args[1]
            assert call_kwargs.get('pool_size') == 20


class TestGetDb:
    """Tests for get_db() function."""

    def test_get_db_initialises_on_first_call(self):
        """get_db() calls init_db when thread-local DB is not set."""
        import app.db.database as db_mod
        # Clear any cached thread-local state
        if hasattr(db_mod._thread_local, 'db'):
            db_mod._thread_local.db = None

        mock_db = MagicMock()
        with patch('app.db.database.init_db', return_value=mock_db) as mock_init:
            result = db_mod.get_db()
            mock_init.assert_called_once()
            assert result is mock_db

    def test_get_db_returns_cached_instance(self):
        """get_db() returns the same DB instance when already initialised."""
        import app.db.database as db_mod
        existing_db = MagicMock()
        db_mod._thread_local.db = existing_db

        with patch('app.db.database.init_db') as mock_init:
            result = db_mod.get_db()
            mock_init.assert_not_called()
            assert result is existing_db

        # Clean up
        db_mod._thread_local.db = None


class TestCloseDb:
    """Tests for close_db() function."""

    def test_close_db_calls_close_and_sets_none(self):
        """close_db() calls db.close() and sets thread-local to None."""
        import app.db.database as db_mod
        mock_db = MagicMock()
        db_mod._thread_local.db = mock_db

        db_mod.close_db()

        mock_db.close.assert_called_once()
        assert db_mod._thread_local.db is None

    def test_close_db_safe_when_no_connection(self):
        """close_db() does not raise when no connection exists."""
        import app.db.database as db_mod
        db_mod._thread_local.db = None
        # Should not raise
        db_mod.close_db()

    def test_close_db_handles_close_exception(self):
        """close_db() sets thread-local to None even when close() raises."""
        import app.db.database as db_mod
        mock_db = MagicMock()
        mock_db.close.side_effect = RuntimeError("connection already closed")
        db_mod._thread_local.db = mock_db

        # Should not propagate exception
        db_mod.close_db()
        assert db_mod._thread_local.db is None


class TestGetDbContext:
    """Tests for get_db_context() context manager."""

    def test_context_yields_db_and_commits(self):
        """get_db_context() yields the DB and commits on exit."""
        mock_db = MagicMock()
        with patch('app.db.database.get_db', return_value=mock_db):
            from app.db.database import get_db_context
            with get_db_context() as db:
                assert db is mock_db
            mock_db.commit.assert_called_once()
