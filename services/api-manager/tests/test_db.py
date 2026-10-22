"""Tests for database module (galera, database, init_db).

Covers MariaDB Galera support, penguin-dal initialization, and SQLAlchemy schema setup.
"""

import os
import pytest
import tempfile
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone

# ============================================================================
# Tests for app/db/galera.py
# ============================================================================


class TestGaleraConfig:
    """Tests for GaleraConfig dataclass."""

    def test_galera_config_defaults(self):
        """Test GaleraConfig with default values."""
        from app.db.galera import GaleraConfig

        config = GaleraConfig()
        assert config.enabled is False
        assert config.wsrep_sync_wait == 1
        assert config.deadlock_retry_count == 3
        assert config.deadlock_retry_delay == 0.1
        assert config.auto_increment_offset == 1
        assert config.auto_increment_increment == 1

    def test_galera_config_custom_values(self):
        """Test GaleraConfig with custom values."""
        from app.db.galera import GaleraConfig

        config = GaleraConfig(
            enabled=True,
            wsrep_sync_wait=3,
            deadlock_retry_count=5,
            deadlock_retry_delay=0.2,
            auto_increment_offset=2,
            auto_increment_increment=2,
        )
        assert config.enabled is True
        assert config.wsrep_sync_wait == 3
        assert config.deadlock_retry_count == 5
        assert config.deadlock_retry_delay == 0.2
        assert config.auto_increment_offset == 2
        assert config.auto_increment_increment == 2

    def test_galera_config_from_env(self, monkeypatch):
        """Test GaleraConfig.from_env() with environment variables."""
        from app.db.galera import GaleraConfig

        monkeypatch.setenv('DB_GALERA_ENABLED', 'true')
        monkeypatch.setenv('DB_GALERA_WSREP_SYNC_WAIT', '3')
        monkeypatch.setenv('DB_GALERA_DEADLOCK_RETRIES', '5')
        monkeypatch.setenv('DB_GALERA_DEADLOCK_DELAY', '0.2')
        monkeypatch.setenv('DB_GALERA_AUTO_INCREMENT_OFFSET', '2')
        monkeypatch.setenv('DB_GALERA_AUTO_INCREMENT_INCREMENT', '3')

        config = GaleraConfig.from_env()
        assert config.enabled is True
        assert config.wsrep_sync_wait == 3
        assert config.deadlock_retry_count == 5
        assert config.deadlock_retry_delay == 0.2
        assert config.auto_increment_offset == 2
        assert config.auto_increment_increment == 3

    def test_galera_config_from_env_defaults(self, monkeypatch):
        """Test GaleraConfig.from_env() with defaults when env vars not set."""
        from app.db.galera import GaleraConfig

        # Clear any existing env vars
        monkeypatch.delenv('DB_GALERA_ENABLED', raising=False)
        monkeypatch.delenv('DB_GALERA_WSREP_SYNC_WAIT', raising=False)

        config = GaleraConfig.from_env()
        assert config.enabled is False
        assert config.wsrep_sync_wait == 1


class TestGaleraFunctions:
    """Tests for Galera utility functions."""

    def test_is_galera_enabled_false(self, monkeypatch):
        """Test is_galera_enabled() returns False when not configured."""
        from app.db.galera import is_galera_enabled

        monkeypatch.setenv('DB_GALERA_ENABLED', 'false')
        assert is_galera_enabled() is False

    def test_is_galera_enabled_true(self, monkeypatch):
        """Test is_galera_enabled() returns True when configured."""
        from app.db.galera import is_galera_enabled

        monkeypatch.setenv('DB_GALERA_ENABLED', 'true')
        assert is_galera_enabled() is True

    def test_is_galera_enabled_case_insensitive(self, monkeypatch):
        """Test is_galera_enabled() is case-insensitive."""
        from app.db.galera import is_galera_enabled

        monkeypatch.setenv('DB_GALERA_ENABLED', 'TRUE')
        assert is_galera_enabled() is True

        monkeypatch.setenv('DB_GALERA_ENABLED', 'True')
        assert is_galera_enabled() is True

    def test_get_galera_config(self, monkeypatch):
        """Test get_galera_config() returns GaleraConfig instance."""
        from app.db.galera import get_galera_config

        monkeypatch.setenv('DB_GALERA_ENABLED', 'true')
        config = get_galera_config()
        assert config.enabled is True

    def test_is_deadlock_error_deadlock(self):
        """Test is_deadlock_error() detects deadlock errors."""
        from app.db.galera import is_deadlock_error

        exc = Exception("Deadlock found when trying to get lock")
        assert is_deadlock_error(exc) is True

    def test_is_deadlock_error_lock_timeout(self):
        """Test is_deadlock_error() detects lock timeout."""
        from app.db.galera import is_deadlock_error

        exc = Exception("Lock wait timeout exceeded")
        assert is_deadlock_error(exc) is True

    def test_is_deadlock_error_wsrep_not_ready(self):
        """Test is_deadlock_error() detects WSREP not ready."""
        from app.db.galera import is_deadlock_error

        exc = Exception("WSREP has not yet prepared node for application use")
        assert is_deadlock_error(exc) is True

    def test_is_deadlock_error_error_code_1213(self):
        """Test is_deadlock_error() detects error code 1213."""
        from app.db.galera import is_deadlock_error

        exc = Exception("Error 1213: Deadlock found")
        assert is_deadlock_error(exc) is True

    def test_is_deadlock_error_error_code_1205(self):
        """Test is_deadlock_error() detects error code 1205."""
        from app.db.galera import is_deadlock_error

        exc = Exception("Error 1205: Lock wait timeout")
        assert is_deadlock_error(exc) is True

    def test_is_deadlock_error_non_deadlock(self):
        """Test is_deadlock_error() returns False for non-deadlock errors."""
        from app.db.galera import is_deadlock_error

        exc = Exception("Connection refused")
        assert is_deadlock_error(exc) is False

    @patch('app.db.galera.is_galera_enabled')
    def test_set_wsrep_sync_wait_disabled(self, mock_enabled):
        """Test set_wsrep_sync_wait() skips when Galera is disabled."""
        from app.db.galera import set_wsrep_sync_wait

        mock_enabled.return_value = False
        mock_db = MagicMock()

        result = set_wsrep_sync_wait(mock_db, level=2)
        assert result is True
        mock_db.executesql.assert_not_called()

    @patch('app.db.galera.is_galera_enabled')
    def test_set_wsrep_sync_wait_success(self, mock_enabled):
        """Test set_wsrep_sync_wait() sets sync level successfully."""
        from app.db.galera import set_wsrep_sync_wait

        mock_enabled.return_value = True
        mock_db = MagicMock()

        result = set_wsrep_sync_wait(mock_db, level=3)
        assert result is True
        mock_db.executesql.assert_called_once_with(
            'SET SESSION wsrep_sync_wait = %(level)s', {'level': 3}
        )

    @patch('app.db.galera.is_galera_enabled')
    def test_set_wsrep_sync_wait_failure(self, mock_enabled):
        """Test set_wsrep_sync_wait() handles errors gracefully."""
        from app.db.galera import set_wsrep_sync_wait

        mock_enabled.return_value = True
        mock_db = MagicMock()
        mock_db.executesql.side_effect = Exception("Connection failed")

        result = set_wsrep_sync_wait(mock_db, level=1)
        assert result is False

    @patch('app.db.galera.is_galera_enabled')
    def test_set_auto_increment_config_disabled(self, mock_enabled):
        """Test set_auto_increment_config() skips when Galera is disabled."""
        from app.db.galera import set_auto_increment_config

        mock_enabled.return_value = False
        mock_db = MagicMock()

        result = set_auto_increment_config(mock_db)
        assert result is True
        mock_db.transaction.assert_not_called()

    @patch('app.db.galera.is_galera_enabled')
    @patch('app.db.galera.get_galera_config')
    def test_set_auto_increment_config_success(self, mock_config, mock_enabled):
        """Test set_auto_increment_config() sets auto-increment successfully."""
        from app.db.galera import set_auto_increment_config, GaleraConfig

        mock_enabled.return_value = True
        mock_gconf = GaleraConfig(auto_increment_offset=2, auto_increment_increment=3)
        mock_config.return_value = mock_gconf

        mock_tx = MagicMock()
        mock_db = MagicMock()
        mock_db.transaction.return_value.__enter__.return_value = mock_tx

        result = set_auto_increment_config(mock_db)
        assert result is True
        assert mock_tx.executesql.call_count == 2

    @patch('app.db.galera.is_galera_enabled')
    def test_set_auto_increment_config_custom_values(self, mock_enabled):
        """Test set_auto_increment_config() with custom offset/increment."""
        from app.db.galera import set_auto_increment_config

        mock_enabled.return_value = True
        mock_tx = MagicMock()
        mock_db = MagicMock()
        mock_db.transaction.return_value.__enter__.return_value = mock_tx

        result = set_auto_increment_config(mock_db, offset=5, increment=10)
        assert result is True
        mock_tx.executesql.assert_any_call(
            'SET SESSION auto_increment_offset = %(offset)s', {'offset': 5}
        )
        mock_tx.executesql.assert_any_call(
            'SET SESSION auto_increment_increment = %(increment)s', {'increment': 10}
        )

    @patch('app.db.galera.is_galera_enabled')
    def test_get_cluster_status_disabled(self, mock_enabled):
        """Test get_cluster_status() returns None when Galera is disabled."""
        from app.db.galera import get_cluster_status

        mock_enabled.return_value = False
        mock_db = MagicMock()

        result = get_cluster_status(mock_db)
        assert result is None

    @patch('app.db.galera.is_galera_enabled')
    def test_get_cluster_status_success(self, mock_enabled):
        """Test get_cluster_status() retrieves cluster status."""
        from app.db.galera import get_cluster_status

        mock_enabled.return_value = True
        mock_db = MagicMock()
        mock_db.executesql.return_value = [
            ('wsrep_cluster_size', '3'),
            ('wsrep_cluster_status', 'Primary'),
            ('wsrep_ready', 'ON'),
        ]

        result = get_cluster_status(mock_db)
        assert isinstance(result, dict)
        assert result['wsrep_cluster_size'] == '3'
        assert result['wsrep_cluster_status'] == 'Primary'

    @patch('app.db.galera.is_galera_enabled')
    def test_get_cluster_status_error(self, mock_enabled):
        """Test get_cluster_status() handles errors gracefully."""
        from app.db.galera import get_cluster_status

        mock_enabled.return_value = True
        mock_db = MagicMock()
        mock_db.executesql.side_effect = Exception("Query failed")

        result = get_cluster_status(mock_db)
        assert result is None

    @patch('app.db.galera.is_galera_enabled')
    @patch('app.db.galera.get_cluster_status')
    def test_is_cluster_ready_true(self, mock_status, mock_enabled):
        """Test is_cluster_ready() returns True when cluster is ready."""
        from app.db.galera import is_cluster_ready

        mock_enabled.return_value = True
        mock_status.return_value = {
            'wsrep_ready': 'ON',
            'wsrep_connected': 'ON',
            'wsrep_cluster_status': 'Primary',
        }
        mock_db = MagicMock()

        result = is_cluster_ready(mock_db)
        assert result is True

    @patch('app.db.galera.is_galera_enabled')
    def test_is_cluster_ready_false_no_status(self, mock_enabled):
        """Test is_cluster_ready() returns False when status unavailable."""
        from app.db.galera import is_cluster_ready

        mock_enabled.return_value = True
        mock_db = MagicMock()

        with patch('app.db.galera.get_cluster_status', return_value=None):
            result = is_cluster_ready(mock_db)
            assert result is False

    @patch('app.db.galera.is_galera_enabled')
    def test_wait_for_cluster_ready_disabled(self, mock_enabled):
        """Test wait_for_cluster_ready() returns True when Galera is disabled."""
        from app.db.galera import wait_for_cluster_ready

        mock_enabled.return_value = False
        mock_db = MagicMock()

        result = wait_for_cluster_ready(mock_db)
        assert result is True

    @patch('app.db.galera.is_galera_enabled')
    @patch('app.db.galera.is_cluster_ready')
    @patch('time.time')
    @patch('time.sleep')
    def test_wait_for_cluster_ready_success(self, mock_sleep, mock_time, mock_ready, mock_enabled):
        """Test wait_for_cluster_ready() returns True when cluster becomes ready."""
        from app.db.galera import wait_for_cluster_ready

        mock_enabled.return_value = True
        mock_ready.return_value = True
        mock_time.side_effect = [0, 1, 2]  # Simulate time progression
        mock_db = MagicMock()

        result = wait_for_cluster_ready(mock_db, timeout=30)
        assert result is True

    @patch('app.db.galera.is_galera_enabled')
    @patch('app.db.galera.is_cluster_ready')
    @patch('time.time')
    @patch('time.sleep')
    def test_wait_for_cluster_ready_timeout(self, mock_sleep, mock_time, mock_ready, mock_enabled):
        """Test wait_for_cluster_ready() returns False on timeout."""
        from app.db.galera import wait_for_cluster_ready

        mock_enabled.return_value = True
        mock_ready.return_value = False
        # Simulate timeout by advancing time
        mock_time.side_effect = [0, 5, 10, 15, 20, 25, 30, 35]
        mock_db = MagicMock()

        result = wait_for_cluster_ready(mock_db, timeout=30)
        assert result is False

    @patch('app.db.galera.is_galera_enabled')
    @patch('app.db.galera.set_wsrep_sync_wait')
    @patch('app.db.galera.set_auto_increment_config')
    def test_init_galera_session_disabled(self, mock_ai, mock_wsrep, mock_enabled):
        """Test init_galera_session() returns True when Galera is disabled."""
        from app.db.galera import init_galera_session

        mock_enabled.return_value = False
        mock_db = MagicMock()

        result = init_galera_session(mock_db)
        assert result is True
        mock_wsrep.assert_not_called()
        mock_ai.assert_not_called()

    @patch('app.db.galera.is_galera_enabled')
    @patch('app.db.galera.set_wsrep_sync_wait')
    @patch('app.db.galera.set_auto_increment_config')
    def test_init_galera_session_success(self, mock_ai, mock_wsrep, mock_enabled):
        """Test init_galera_session() initializes both settings successfully."""
        from app.db.galera import init_galera_session

        mock_enabled.return_value = True
        mock_wsrep.return_value = True
        mock_ai.return_value = True
        mock_db = MagicMock()

        result = init_galera_session(mock_db)
        assert result is True

    @patch('app.db.galera.is_galera_enabled')
    def test_handle_galera_deadlock_success_first_try(self, mock_enabled):
        """Test handle_galera_deadlock() wrapper succeeds on first try."""
        from app.db.galera import handle_galera_deadlock

        mock_enabled.return_value = True

        @handle_galera_deadlock
        def test_func():
            return "success"

        result = test_func()
        assert result == "success"

    @patch('app.db.galera.is_galera_enabled')
    @patch('app.db.galera.is_deadlock_error')
    @patch('time.sleep')
    def test_handle_galera_deadlock_retries(self, mock_sleep, mock_deadlock, mock_enabled):
        """Test handle_galera_deadlock() wrapper retries on deadlock."""
        from app.db.galera import handle_galera_deadlock

        mock_enabled.return_value = True
        mock_deadlock.return_value = True

        call_count = 0

        @handle_galera_deadlock
        def test_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Deadlock")
            return "success"

        result = test_func()
        assert result == "success"
        assert call_count == 3

    def test_galera_transaction_context_manager(self):
        """Test GaleraTransaction context manager initialization."""
        from app.db.galera import GaleraTransaction, GaleraConfig

        mock_db = MagicMock()
        tx = GaleraTransaction(mock_db, max_retries=5, retry_delay=0.2)

        assert tx.db is mock_db
        assert tx.max_retries == 5
        assert tx.retry_delay == 0.2

    @patch('app.db.galera.is_galera_enabled')
    @patch('app.db.galera.init_galera_session')
    def test_galera_transaction_enter(self, mock_init, mock_enabled):
        """Test GaleraTransaction.__enter__() initializes session."""
        from app.db.galera import GaleraTransaction

        mock_enabled.return_value = True
        mock_init.return_value = True
        mock_db = MagicMock()

        tx = GaleraTransaction(mock_db)
        result = tx.__enter__()

        assert result is mock_db
        mock_init.assert_called_once_with(mock_db)

    def test_galera_transaction_exit_no_exception(self):
        """Test GaleraTransaction.__exit__() with no exception."""
        from app.db.galera import GaleraTransaction

        mock_db = MagicMock()
        tx = GaleraTransaction(mock_db)

        result = tx.__exit__(None, None, None)
        assert result is False

    @patch('app.db.galera.is_galera_enabled')
    @patch('app.db.galera.is_deadlock_error')
    def test_galera_transaction_exit_deadlock(self, mock_deadlock, mock_enabled):
        """Test GaleraTransaction.__exit__() with deadlock exception."""
        from app.db.galera import GaleraTransaction

        mock_enabled.return_value = True
        mock_deadlock.return_value = True
        mock_db = MagicMock()

        tx = GaleraTransaction(mock_db)
        exc = Exception("Deadlock")
        result = tx.__exit__(Exception, exc, None)

        assert result is False


# ============================================================================
# Tests for app/db/database.py
# ============================================================================


class TestDatabaseInit:
    """Tests for database initialization functions.

    Regression: gh-22 (DB pool consolidation). ``init_db()`` no longer
    raises when ``DATABASE_URL`` is unset -- it now falls back to
    ``Config.get_db_uri()`` (the same ``DB_*``-env-var source
    ``app.models.init_db()`` uses), which always resolves to a URI (backed
    by ``Config``'s own dev defaults) rather than raising. It also now
    installs RLS GUC wiring (``app.db.rls.install_rls_events``) on the
    engine it builds -- see ``app.db.database``'s module docstring.
    """

    def test_init_db_with_url(self, monkeypatch):
        """Test init_db() with explicit database URL (passed through unchanged)."""
        from app.db.database import init_db

        with patch('app.db.database.DB') as mock_db_class, \
                patch('app.db.database.install_rls_events') as mock_install:
            mock_instance = MagicMock()
            mock_db_class.return_value = mock_instance

            db = init_db(database_url='sqlite:///test.db', pool_size=5)

            mock_db_class.assert_called_once_with('sqlite:///test.db', pool_size=5)
            mock_install.assert_called_once_with(mock_instance.engine)
            assert db is mock_instance

    def test_init_db_with_env_var(self, monkeypatch):
        """Test init_db() uses DATABASE_URL environment variable, passed through unchanged."""
        from app.db.database import init_db

        monkeypatch.setenv('DATABASE_URL', 'postgresql://localhost/env')

        with patch('app.db.database.DB') as mock_db_class, \
                patch('app.db.database.install_rls_events'):
            mock_instance = MagicMock()
            mock_db_class.return_value = mock_instance

            db = init_db()

            mock_db_class.assert_called_once_with('postgresql://localhost/env', pool_size=10)

    def test_init_db_falls_back_to_config_get_db_uri(self, monkeypatch):
        """init_db() falls back to Config.get_db_uri() (converted to SQLAlchemy
        format) when neither an explicit URL nor DATABASE_URL is given --
        the same source app.models.init_db() uses, so both pools resolve to
        the same database.
        """
        from app.config import Config
        from app.db.database import init_db

        monkeypatch.delenv('DATABASE_URL', raising=False)
        monkeypatch.setattr(Config, 'DB_TYPE', 'postgres')
        monkeypatch.setattr(Config, 'DB_HOST', 'db-host')
        monkeypatch.setattr(Config, 'DB_PORT', '5432')
        monkeypatch.setattr(Config, 'DB_NAME', 'fallback_db')
        monkeypatch.setattr(Config, 'DB_USER', 'fallback_user')
        monkeypatch.setattr(Config, 'DB_PASS', 'fallback_pass')

        with patch('app.db.database.DB') as mock_db_class, \
                patch('app.db.database.install_rls_events'):
            mock_db_class.return_value = MagicMock()

            init_db()

            call_args = mock_db_class.call_args
            assert call_args[0][0] == (
                'postgresql://fallback_user:fallback_pass@db-host:5432/fallback_db'
            )

    def test_get_db_creates_instance(self, monkeypatch):
        """Test get_db() creates instance on first call."""
        from app.db.database import get_db, _thread_local

        # Clear thread-local state
        if hasattr(_thread_local, 'db'):
            delattr(_thread_local, 'db')

        monkeypatch.setenv('DATABASE_URL', 'sqlite:///test.db')

        with patch('app.db.database.init_db') as mock_init:
            mock_instance = MagicMock()
            mock_init.return_value = mock_instance

            db = get_db()

            assert db is mock_instance
            mock_init.assert_called_once()

    def test_get_db_returns_cached_instance(self, monkeypatch):
        """Test get_db() returns cached instance on subsequent calls."""
        from app.db.database import get_db, _thread_local

        monkeypatch.setenv('DATABASE_URL', 'sqlite:///test.db')
        mock_instance = MagicMock()
        _thread_local.db = mock_instance

        with patch('app.db.database.init_db') as mock_init:
            db = get_db()

            assert db is mock_instance
            mock_init.assert_not_called()

    def test_close_db_closes_connection(self, monkeypatch):
        """Test close_db() closes database connection."""
        from app.db.database import close_db, _thread_local

        mock_db = MagicMock()
        _thread_local.db = mock_db

        close_db()

        mock_db.close.assert_called_once()
        assert _thread_local.db is None

    def test_close_db_handles_error(self, monkeypatch):
        """Test close_db() handles errors gracefully."""
        from app.db.database import close_db, _thread_local

        mock_db = MagicMock()
        mock_db.close.side_effect = Exception("Close failed")
        _thread_local.db = mock_db

        # Should not raise exception
        close_db()
        assert _thread_local.db is None

    def test_close_db_safe_if_not_initialized(self):
        """Test close_db() is safe when db not initialized."""
        from app.db.database import close_db, _thread_local

        # Clear thread-local state
        if hasattr(_thread_local, 'db'):
            delattr(_thread_local, 'db')

        # Should not raise exception
        close_db()

    def test_get_db_context_manager(self, monkeypatch):
        """Test get_db_context() context manager."""
        from app.db.database import get_db_context

        monkeypatch.setenv('DATABASE_URL', 'sqlite:///test.db')
        mock_db = MagicMock()

        with patch('app.db.database.get_db', return_value=mock_db):
            with get_db_context() as db:
                assert db is mock_db

            mock_db.commit.assert_called_once()


# ============================================================================
# Tests for app/db/init_db.py
# ============================================================================


class TestSQLAlchemyConversion:
    """Tests for SQLAlchemy URL conversion."""

    def test_get_sqlalchemy_url_postgres(self):
        """Test get_sqlalchemy_url() converts PostgreSQL URLs."""
        from app.db.init_db import get_sqlalchemy_url

        result = get_sqlalchemy_url('postgres', 'localhost:5432/mydb')
        assert result.startswith('postgresql://')

    def test_get_sqlalchemy_url_postgres_already_prefixed(self):
        """Test get_sqlalchemy_url() handles already-prefixed PostgreSQL URLs."""
        from app.db.init_db import get_sqlalchemy_url

        result = get_sqlalchemy_url('postgresql', 'postgresql://localhost:5432/mydb')
        assert result.startswith('postgresql://')

    def test_get_sqlalchemy_url_mysql(self):
        """Test get_sqlalchemy_url() converts MySQL URLs."""
        from app.db.init_db import get_sqlalchemy_url

        result = get_sqlalchemy_url('mysql', 'localhost:3306/mydb')
        assert 'mysql+pymysql://' in result

    def test_get_sqlalchemy_url_mariadb(self):
        """Test get_sqlalchemy_url() treats MariaDB as MySQL."""
        from app.db.init_db import get_sqlalchemy_url

        result = get_sqlalchemy_url('mariadb', 'localhost:3306/mydb')
        assert 'mysql+pymysql://' in result

    def test_get_sqlalchemy_url_sqlite(self):
        """Test get_sqlalchemy_url() handles SQLite URLs."""
        from app.db.init_db import get_sqlalchemy_url

        result = get_sqlalchemy_url('sqlite', ':memory:')
        assert result.startswith('sqlite:///')

    def test_get_sqlalchemy_url_unsupported(self):
        """Test get_sqlalchemy_url() raises for unsupported DB types."""
        from app.db.init_db import get_sqlalchemy_url

        with pytest.raises(ValueError, match="Unsupported DB_TYPE"):
            get_sqlalchemy_url('oracle', 'localhost:1521/db')


class TestSchemaInitialization:
    """Tests for SQLAlchemy schema initialization."""

    @patch('app.db.init_db.create_engine')
    def test_init_db_schema_success(self, mock_engine_class):
        """Test init_db_schema() initializes schema successfully."""
        from app.db.init_db import init_db_schema

        mock_engine = MagicMock()
        mock_engine_class.return_value = mock_engine

        with patch('app.db.init_db.Base') as mock_base:
            result = init_db_schema(
                database_url='sqlite:///test.db',
                db_type='sqlite',
            )

            assert result is True
            mock_base.metadata.create_all.assert_called_once_with(mock_engine)

    @patch('app.db.init_db.create_engine')
    def test_init_db_schema_uses_env_var(self, mock_engine_class, monkeypatch):
        """Test init_db_schema() uses DATABASE_URL environment variable."""
        from app.db.init_db import init_db_schema

        monkeypatch.setenv('DATABASE_URL', 'sqlite:///env.db')
        monkeypatch.setenv('DB_TYPE', 'sqlite')

        mock_engine = MagicMock()
        mock_engine_class.return_value = mock_engine

        with patch('app.db.init_db.Base'):
            result = init_db_schema()

            assert result is True

    def test_init_db_schema_no_url_raises_error(self, monkeypatch):
        """Test init_db_schema() returns False when DATABASE_URL not set."""
        from app.db.init_db import init_db_schema

        monkeypatch.delenv('DATABASE_URL', raising=False)

        # The function catches the ValueError and returns False
        result = init_db_schema()
        assert result is False

    @patch('app.db.init_db.create_engine')
    def test_init_db_schema_handles_error(self, mock_engine_class):
        """Test init_db_schema() handles exceptions gracefully."""
        from app.db.init_db import init_db_schema

        mock_engine_class.side_effect = Exception("Connection failed")

        result = init_db_schema(
            database_url='sqlite:///test.db',
            db_type='sqlite',
        )

        assert result is False

    def test_create_tables(self):
        """Test create_tables() is wrapper around init_db_schema()."""
        from app.db.init_db import create_tables

        with patch('app.db.init_db.init_db_schema', return_value=True) as mock_init:
            result = create_tables()

            assert result is True
            mock_init.assert_called_once()

    @patch('app.db.init_db.create_engine')
    def test_drop_tables_success(self, mock_engine_class, monkeypatch):
        """Test drop_tables() drops all tables successfully."""
        from app.db.init_db import drop_tables

        monkeypatch.setenv('DATABASE_URL', 'sqlite:///test.db')
        monkeypatch.setenv('DB_TYPE', 'sqlite')

        mock_engine = MagicMock()
        mock_engine_class.return_value = mock_engine

        with patch('app.db.init_db.Base') as mock_base:
            result = drop_tables()

            assert result is True
            mock_base.metadata.drop_all.assert_called_once_with(mock_engine)

    def test_drop_tables_no_url_raises_error(self, monkeypatch):
        """Test drop_tables() returns False when DATABASE_URL not set."""
        from app.db.init_db import drop_tables

        monkeypatch.delenv('DATABASE_URL', raising=False)

        # The function catches the ValueError and returns False
        result = drop_tables()
        assert result is False

    @patch('app.db.init_db.create_engine')
    def test_drop_tables_handles_error(self, mock_engine_class):
        """Test drop_tables() handles exceptions gracefully."""
        from app.db.init_db import drop_tables

        mock_engine_class.side_effect = Exception("Connection failed")

        result = drop_tables(
            database_url='sqlite:///test.db',
            db_type='sqlite',
        )

        assert result is False
