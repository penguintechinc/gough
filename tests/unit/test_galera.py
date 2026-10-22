"""Unit tests for Galera cluster module (api-manager/app/db/galera.py).

All tests use unittest.mock; no real database connection required.
"""
import sys

_API_MANAGER_PATH = '/home/penguin/code/gough/services/api-manager'
if _API_MANAGER_PATH not in sys.path:
    sys.path.insert(0, _API_MANAGER_PATH)
# Evict any stale 'app' module cached from another path (e.g. penguincode/app.py)
for _mod in list(sys.modules.keys()):
    if _mod == 'app' or _mod.startswith('app.'):
        del sys.modules[_mod]

import pytest
from unittest.mock import MagicMock, patch, call
from app.db.galera import (
    GaleraConfig,
    is_galera_enabled,
    get_galera_config,
    is_deadlock_error,
    handle_galera_deadlock,
    is_cluster_ready,
    get_cluster_status,
    set_wsrep_sync_wait,
    set_auto_increment_config,
    wait_for_cluster_ready,
    init_galera_session,
    GaleraTransaction,
)


class TestGaleraConfig:
    """Tests for the GaleraConfig dataclass."""

    def test_default_values(self):
        """GaleraConfig has sensible defaults."""
        config = GaleraConfig()
        assert config.enabled is False
        assert config.wsrep_sync_wait == 1
        assert config.deadlock_retry_count == 3
        assert config.deadlock_retry_delay == 0.1
        assert config.auto_increment_offset == 1
        assert config.auto_increment_increment == 1

    def test_from_env_defaults_when_vars_absent(self):
        """GaleraConfig.from_env() returns defaults when env vars not set."""
        env = {k: v for k, v in __import__('os').environ.items()
               if not k.startswith('DB_GALERA')}
        with patch.dict('os.environ', env, clear=True):
            config = GaleraConfig.from_env()
        assert config.enabled is False
        assert config.wsrep_sync_wait == 1
        assert config.deadlock_retry_count == 3
        assert config.deadlock_retry_delay == 0.1

    def test_from_env_reads_enabled(self):
        """GaleraConfig.from_env() parses DB_GALERA_ENABLED=true."""
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            config = GaleraConfig.from_env()
        assert config.enabled is True

    def test_from_env_false_string(self):
        """GaleraConfig.from_env() treats 'false' as disabled."""
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'false'}):
            config = GaleraConfig.from_env()
        assert config.enabled is False

    def test_from_env_reads_wsrep_sync_wait(self):
        """GaleraConfig.from_env() reads DB_GALERA_WSREP_SYNC_WAIT."""
        with patch.dict('os.environ', {
            'DB_GALERA_ENABLED': 'true',
            'DB_GALERA_WSREP_SYNC_WAIT': '3',
        }):
            config = GaleraConfig.from_env()
        assert config.wsrep_sync_wait == 3

    def test_from_env_reads_deadlock_retries(self):
        """GaleraConfig.from_env() reads DB_GALERA_DEADLOCK_RETRIES."""
        with patch.dict('os.environ', {'DB_GALERA_DEADLOCK_RETRIES': '5'}):
            config = GaleraConfig.from_env()
        assert config.deadlock_retry_count == 5

    def test_from_env_reads_deadlock_delay(self):
        """GaleraConfig.from_env() reads DB_GALERA_DEADLOCK_DELAY."""
        with patch.dict('os.environ', {'DB_GALERA_DEADLOCK_DELAY': '0.5'}):
            config = GaleraConfig.from_env()
        assert config.deadlock_retry_delay == 0.5

    def test_from_env_reads_auto_increment_offset(self):
        """GaleraConfig.from_env() reads DB_GALERA_AUTO_INCREMENT_OFFSET."""
        with patch.dict('os.environ', {'DB_GALERA_AUTO_INCREMENT_OFFSET': '2'}):
            config = GaleraConfig.from_env()
        assert config.auto_increment_offset == 2

    def test_from_env_reads_auto_increment_increment(self):
        """GaleraConfig.from_env() reads DB_GALERA_AUTO_INCREMENT_INCREMENT."""
        with patch.dict('os.environ', {'DB_GALERA_AUTO_INCREMENT_INCREMENT': '3'}):
            config = GaleraConfig.from_env()
        assert config.auto_increment_increment == 3


class TestIsGaleraEnabled:
    """Tests for is_galera_enabled()."""

    def test_disabled_by_default(self):
        """is_galera_enabled() returns False when env var is absent."""
        env = {k: v for k, v in __import__('os').environ.items()
               if k != 'DB_GALERA_ENABLED'}
        with patch.dict('os.environ', env, clear=True):
            assert is_galera_enabled() is False

    def test_enabled_when_set_true(self):
        """is_galera_enabled() returns True when DB_GALERA_ENABLED=true."""
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            assert is_galera_enabled() is True

    def test_case_insensitive_true(self):
        """is_galera_enabled() is case-insensitive for 'TRUE'."""
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'TRUE'}):
            assert is_galera_enabled() is True

    def test_disabled_for_non_true_value(self):
        """is_galera_enabled() returns False for values other than 'true'."""
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'yes'}):
            assert is_galera_enabled() is False


class TestGetGaleraConfig:
    """Tests for get_galera_config()."""

    def test_returns_galera_config_instance(self):
        """get_galera_config() returns a GaleraConfig instance."""
        result = get_galera_config()
        assert isinstance(result, GaleraConfig)


class TestIsDeadlockError:
    """Tests for is_deadlock_error()."""

    def test_detects_deadlock_keyword(self):
        """Recognises 'deadlock' in the error message."""
        assert is_deadlock_error(Exception("deadlock found when trying to get lock")) is True

    def test_detects_lock_wait_timeout(self):
        """Recognises 'lock wait timeout' in the error message."""
        assert is_deadlock_error(Exception("lock wait timeout exceeded")) is True

    def test_detects_wsrep_not_prepared(self):
        """Recognises 'wsrep has not yet prepared' in the error message."""
        assert is_deadlock_error(Exception("wsrep has not yet prepared node")) is True

    def test_detects_error_code_1213(self):
        """Recognises error code 1213 (deadlock) in the error string."""
        assert is_deadlock_error(Exception("(1213) Deadlock")) is True

    def test_detects_error_code_1205(self):
        """Recognises error code 1205 (lock timeout) in the error string."""
        assert is_deadlock_error(Exception("error 1205 lock wait timeout")) is True

    def test_detects_error_code_1047(self):
        """Recognises error code 1047 (WSREP) in the error string."""
        assert is_deadlock_error(Exception("error 1047")) is True

    def test_returns_false_for_connection_error(self):
        """Returns False for unrelated connection errors."""
        assert is_deadlock_error(Exception("connection refused")) is False

    def test_returns_false_for_syntax_error(self):
        """Returns False for SQL syntax errors."""
        assert is_deadlock_error(Exception("you have an error in your SQL syntax")) is False

    def test_case_insensitive_matching(self):
        """Deadlock detection is case-insensitive."""
        assert is_deadlock_error(Exception("DEADLOCK FOUND")) is True


class TestHandleGaleraDeadlock:
    """Tests for handle_galera_deadlock() wrapper factory."""

    def test_succeeds_on_first_try(self):
        """Wrapped function returns normally when no deadlock occurs."""
        def my_func():
            return "success"

        wrapped = handle_galera_deadlock(my_func, max_retries=2, retry_delay=0)
        assert wrapped() == "success"

    def test_retries_on_deadlock_then_succeeds(self):
        """Wrapped function is retried after deadlock and returns on later attempt."""
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise Exception("deadlock found")
            return "ok"

        wrapped = handle_galera_deadlock(flaky, max_retries=3, retry_delay=0)
        result = wrapped()
        assert result == "ok"
        assert call_count[0] == 3

    def test_raises_after_max_retries_exceeded(self):
        """Raises the last deadlock exception when max_retries is exhausted."""
        def always_deadlock():
            raise Exception("deadlock found")

        wrapped = handle_galera_deadlock(always_deadlock, max_retries=2, retry_delay=0)
        with pytest.raises(Exception, match="deadlock"):
            wrapped()

    def test_non_deadlock_error_raised_immediately(self):
        """Non-deadlock exceptions propagate immediately without retry."""
        call_count = [0]

        def fails_immediately():
            call_count[0] += 1
            # Use an error message that contains none of the deadlock indicators
            raise ValueError("connection timeout: host unreachable")

        wrapped = handle_galera_deadlock(fails_immediately, max_retries=3, retry_delay=0)
        with pytest.raises(ValueError):
            wrapped()
        assert call_count[0] == 1

    def test_passes_args_and_kwargs(self):
        """Wrapped function receives its positional and keyword arguments."""
        def add(a, b, multiplier=1):
            return (a + b) * multiplier

        wrapped = handle_galera_deadlock(add, max_retries=1, retry_delay=0)
        assert wrapped(3, 4, multiplier=2) == 14

    def test_preserves_function_name(self):
        """handle_galera_deadlock preserves __name__ via functools.wraps."""
        def my_func():
            pass

        wrapped = handle_galera_deadlock(my_func, max_retries=1, retry_delay=0)
        assert wrapped.__name__ == 'my_func'

    def test_retry_count_respects_config_when_not_overridden(self):
        """Uses config defaults when max_retries/retry_delay are not supplied."""
        call_count = [0]

        def always_deadlock():
            call_count[0] += 1
            raise Exception("deadlock found")

        with patch.dict('os.environ', {'DB_GALERA_DEADLOCK_RETRIES': '2', 'DB_GALERA_DEADLOCK_DELAY': '0'}):
            wrapped = handle_galera_deadlock(always_deadlock)
            with pytest.raises(Exception):
                wrapped()
        # 1 initial attempt + 2 retries = 3 total
        assert call_count[0] == 3


class TestGetClusterStatus:
    """Tests for get_cluster_status()."""

    def test_returns_none_when_galera_disabled(self):
        """Returns None without querying DB when Galera is disabled."""
        mock_db = MagicMock()
        env = {k: v for k, v in __import__('os').environ.items()
               if k != 'DB_GALERA_ENABLED'}
        with patch.dict('os.environ', env, clear=True):
            result = get_cluster_status(mock_db)
        assert result is None
        mock_db.engine.connect.assert_not_called()

    def test_returns_status_dict_when_enabled(self):
        """Returns dict of WSREP status variables when Galera is enabled."""
        mock_conn = MagicMock()
        mock_result = [
            ('wsrep_ready', 'ON'),
            ('wsrep_connected', 'ON'),
            ('wsrep_cluster_status', 'Primary'),
            ('wsrep_cluster_size', '3'),
            ('wsrep_local_state_comment', 'Synced'),
        ]
        mock_conn.execute.return_value = iter(mock_result)
        mock_db = MagicMock()
        mock_db.engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            result = get_cluster_status(mock_db)

        assert isinstance(result, dict)
        assert 'wsrep_ready' in result
        assert result['wsrep_ready'] == 'ON'

    def test_returns_none_on_exception(self):
        """Returns None when the database query raises an exception."""
        mock_db = MagicMock()
        mock_db.engine.connect.side_effect = RuntimeError("connection failed")

        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            result = get_cluster_status(mock_db)

        assert result is None


class TestIsClusterReady:
    """Tests for is_cluster_ready()."""

    def test_returns_true_when_galera_not_enabled(self):
        """Returns True immediately when Galera support is off."""
        mock_db = MagicMock()
        env = {k: v for k, v in __import__('os').environ.items()
               if k != 'DB_GALERA_ENABLED'}
        with patch.dict('os.environ', env, clear=True):
            assert is_cluster_ready(mock_db) is True

    def test_returns_false_when_status_unavailable(self):
        """Returns False when get_cluster_status returns None."""
        import app.db.galera as galera_mod
        mock_db = MagicMock()
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'get_cluster_status', return_value=None):
                assert is_cluster_ready(mock_db) is False

    def test_returns_true_when_all_good(self):
        """Returns True when wsrep_ready, wsrep_connected, and cluster_status are correct."""
        import app.db.galera as galera_mod
        from app.db.galera import is_cluster_ready as is_cluster_ready_fresh
        mock_db = MagicMock()
        good_status = {
            'wsrep_ready': 'on',
            'wsrep_connected': 'on',
            'wsrep_cluster_status': 'primary',
        }
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'get_cluster_status', return_value=good_status):
                assert is_cluster_ready_fresh(mock_db) is True

    def test_returns_false_when_not_ready(self):
        """Returns False when wsrep_ready is OFF."""
        import app.db.galera as galera_mod
        mock_db = MagicMock()
        bad_status = {
            'wsrep_ready': 'off',
            'wsrep_connected': 'on',
            'wsrep_cluster_status': 'primary',
        }
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'get_cluster_status', return_value=bad_status):
                assert is_cluster_ready(mock_db) is False

    def test_returns_false_when_not_connected(self):
        """Returns False when wsrep_connected is OFF."""
        import app.db.galera as galera_mod
        mock_db = MagicMock()
        bad_status = {
            'wsrep_ready': 'on',
            'wsrep_connected': 'off',
            'wsrep_cluster_status': 'primary',
        }
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'get_cluster_status', return_value=bad_status):
                assert is_cluster_ready(mock_db) is False

    def test_returns_false_when_cluster_status_not_primary(self):
        """Returns False when cluster is not in Primary component."""
        import app.db.galera as galera_mod
        mock_db = MagicMock()
        bad_status = {
            'wsrep_ready': 'on',
            'wsrep_connected': 'on',
            'wsrep_cluster_status': 'non-primary',
        }
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'get_cluster_status', return_value=bad_status):
                assert is_cluster_ready(mock_db) is False


class TestSetWsrepSyncWait:
    """Tests for set_wsrep_sync_wait()."""

    def test_returns_true_when_galera_disabled(self):
        """Returns True immediately (no-op) when Galera is disabled."""
        mock_db = MagicMock()
        env = {k: v for k, v in __import__('os').environ.items()
               if k != 'DB_GALERA_ENABLED'}
        with patch.dict('os.environ', env, clear=True):
            result = set_wsrep_sync_wait(mock_db, level=1)
        assert result is True
        mock_db.engine.connect.assert_not_called()

    def test_executes_sql_when_galera_enabled(self):
        """Executes SET SESSION wsrep_sync_wait when Galera is enabled."""
        mock_conn = MagicMock()
        mock_db = MagicMock()
        mock_db.engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            result = set_wsrep_sync_wait(mock_db, level=3)

        assert result is True
        mock_conn.execute.assert_called_once()

    def test_returns_false_on_exception(self):
        """Returns False (and does not re-raise) when SQL execution fails."""
        mock_db = MagicMock()
        mock_db.engine.connect.side_effect = RuntimeError("SQL error")

        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            result = set_wsrep_sync_wait(mock_db, level=1)

        assert result is False


class TestSetAutoIncrementConfig:
    """Tests for set_auto_increment_config()."""

    def test_returns_true_when_galera_disabled(self):
        """Returns True immediately when Galera is disabled."""
        mock_db = MagicMock()
        env = {k: v for k, v in __import__('os').environ.items()
               if k != 'DB_GALERA_ENABLED'}
        with patch.dict('os.environ', env, clear=True):
            result = set_auto_increment_config(mock_db)
        assert result is True
        mock_db.engine.connect.assert_not_called()

    def test_executes_sql_when_enabled(self):
        """Executes SET SESSION auto_increment_* statements when enabled."""
        mock_conn = MagicMock()
        mock_db = MagicMock()
        mock_db.engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict('os.environ', {
            'DB_GALERA_ENABLED': 'true',
            'DB_GALERA_AUTO_INCREMENT_OFFSET': '2',
            'DB_GALERA_AUTO_INCREMENT_INCREMENT': '3',
        }):
            result = set_auto_increment_config(mock_db)

        assert result is True
        assert mock_conn.execute.call_count == 2

    def test_uses_explicit_offset_and_increment(self):
        """Uses explicitly supplied offset and increment rather than config values."""
        mock_conn = MagicMock()
        mock_db = MagicMock()
        mock_db.engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            result = set_auto_increment_config(mock_db, offset=5, increment=10)

        assert result is True


class TestWaitForClusterReady:
    """Tests for wait_for_cluster_ready()."""

    def test_returns_true_immediately_when_galera_disabled(self):
        """Returns True without waiting when Galera is disabled."""
        mock_db = MagicMock()
        env = {k: v for k, v in __import__('os').environ.items()
               if k != 'DB_GALERA_ENABLED'}
        with patch.dict('os.environ', env, clear=True):
            result = wait_for_cluster_ready(mock_db, timeout=5)
        assert result is True

    def test_returns_true_when_cluster_immediately_ready(self):
        """Returns True on the first check when cluster is ready."""
        import app.db.galera as galera_mod
        from app.db.galera import wait_for_cluster_ready as wait_for_cluster_ready_fresh
        mock_db = MagicMock()
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'is_cluster_ready', return_value=True):
                with patch.object(galera_mod.time, 'sleep') as mock_sleep:
                    result = wait_for_cluster_ready_fresh(mock_db, timeout=10)
        assert result is True
        mock_sleep.assert_not_called()

    def test_returns_false_on_timeout(self):
        """Returns False when cluster never becomes ready within the timeout."""
        import app.db.galera as galera_mod
        mock_db = MagicMock()
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'is_cluster_ready', return_value=False):
                with patch.object(galera_mod.time, 'sleep'):
                    with patch.object(galera_mod.time, 'time', side_effect=[0, 0, 31]):
                        result = wait_for_cluster_ready(mock_db, timeout=30)
        assert result is False

    def test_retries_until_ready(self):
        """Returns True after cluster becomes ready on a subsequent check."""
        import app.db.galera as galera_mod
        from app.db.galera import wait_for_cluster_ready as wait_for_cluster_ready_fresh
        mock_db = MagicMock()
        ready_results = [False, False, True]

        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'is_cluster_ready', side_effect=ready_results):
                with patch.object(galera_mod.time, 'sleep'):
                    with patch.object(galera_mod.time, 'time', side_effect=[0, 1, 2, 3]):
                        result = wait_for_cluster_ready_fresh(mock_db, timeout=30)
        assert result is True


class TestInitGaleraSession:
    """Tests for init_galera_session()."""

    def test_returns_true_when_galera_disabled(self):
        """Returns True immediately (no-op) when Galera is disabled."""
        mock_db = MagicMock()
        env = {k: v for k, v in __import__('os').environ.items()
               if k != 'DB_GALERA_ENABLED'}
        with patch.dict('os.environ', env, clear=True):
            result = init_galera_session(mock_db)
        assert result is True

    def test_calls_wsrep_and_auto_increment_when_enabled(self):
        """Calls set_wsrep_sync_wait and set_auto_increment_config when enabled."""
        import app.db.galera as galera_mod
        from app.db.galera import init_galera_session as init_galera_session_fresh
        mock_db = MagicMock()
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'set_wsrep_sync_wait', return_value=True) as mock_wsrep:
                with patch.object(galera_mod, 'set_auto_increment_config', return_value=True) as mock_ai:
                    result = init_galera_session_fresh(mock_db)
        assert result is True
        mock_wsrep.assert_called_once_with(mock_db, 1)
        mock_ai.assert_called_once_with(mock_db)

    def test_returns_false_when_wsrep_fails(self):
        """Returns False when set_wsrep_sync_wait fails."""
        import app.db.galera as galera_mod
        from app.db.galera import init_galera_session as init_galera_session_fresh
        mock_db = MagicMock()
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'set_wsrep_sync_wait', return_value=False):
                with patch.object(galera_mod, 'set_auto_increment_config', return_value=True):
                    result = init_galera_session_fresh(mock_db)
        assert result is False


class TestGaleraTransaction:
    """Tests for GaleraTransaction context manager."""

    def test_enter_returns_db_when_galera_disabled(self):
        """__enter__ returns the db object when Galera is disabled."""
        mock_db = MagicMock()
        env = {k: v for k, v in __import__('os').environ.items()
               if k != 'DB_GALERA_ENABLED'}
        with patch.dict('os.environ', env, clear=True):
            tx = GaleraTransaction(mock_db)
            with tx as db:
                assert db is mock_db

    def test_enter_initialises_session_when_enabled(self):
        """__enter__ calls init_galera_session when Galera is enabled."""
        import app.db.galera as galera_mod
        from app.db.galera import GaleraTransaction as GaleraTransaction_fresh
        mock_db = MagicMock()
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            with patch.object(galera_mod, 'init_galera_session') as mock_init:
                tx = GaleraTransaction_fresh(mock_db)
                with tx:
                    pass
        mock_init.assert_called_once_with(mock_db)

    def test_exit_returns_false_on_no_exception(self):
        """__exit__ returns False (does not suppress) when no exception occurred."""
        mock_db = MagicMock()
        env = {k: v for k, v in __import__('os').environ.items()
               if k != 'DB_GALERA_ENABLED'}
        with patch.dict('os.environ', env, clear=True):
            tx = GaleraTransaction(mock_db)
            result = tx.__exit__(None, None, None)
        assert result is False

    def test_exit_returns_false_on_deadlock(self):
        """__exit__ returns False (does not suppress deadlock) to allow re-raise."""
        mock_db = MagicMock()
        with patch.dict('os.environ', {'DB_GALERA_ENABLED': 'true'}):
            tx = GaleraTransaction(mock_db)
            exc = Exception("deadlock found")
            result = tx.__exit__(type(exc), exc, None)
        assert result is False

    def test_custom_max_retries_stored(self):
        """GaleraTransaction stores custom max_retries."""
        mock_db = MagicMock()
        tx = GaleraTransaction(mock_db, max_retries=5)
        assert tx.max_retries == 5

    def test_custom_retry_delay_stored(self):
        """GaleraTransaction stores custom retry_delay."""
        mock_db = MagicMock()
        tx = GaleraTransaction(mock_db, retry_delay=0.05)
        assert tx.retry_delay == 0.05
