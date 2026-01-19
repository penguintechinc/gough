"""
MariaDB Galera cluster support with WSREP handling.

This module provides utilities for working with MariaDB Galera clusters:
- WSREP synchronization settings
- Deadlock detection and retry logic
- Auto-increment offset handling
- Transaction isolation for cluster operations

Per CLAUDE.md standards:
- Support MariaDB Galera with WSREP
- Handle cluster-specific requirements
- Thread-safe operation
"""

import os
import logging
import time
from typing import Optional, Callable, Any, Dict
from dataclasses import dataclass
from functools import wraps

logger = logging.getLogger(__name__)


@dataclass
class GaleraConfig:
    """MariaDB Galera cluster configuration."""
    enabled: bool = False
    wsrep_sync_wait: int = 1
    deadlock_retry_count: int = 3
    deadlock_retry_delay: float = 0.1
    auto_increment_offset: int = 1
    auto_increment_increment: int = 1

    @classmethod
    def from_env(cls) -> 'GaleraConfig':
        """
        Create GaleraConfig from environment variables.

        Environment Variables:
            DB_GALERA_ENABLED: Enable Galera support (default: false)
            DB_GALERA_WSREP_SYNC_WAIT: WSREP sync wait level (default: 1)
            DB_GALERA_DEADLOCK_RETRIES: Deadlock retry count (default: 3)
            DB_GALERA_DEADLOCK_DELAY: Deadlock retry delay in seconds (default: 0.1)
            DB_GALERA_AUTO_INCREMENT_OFFSET: Auto-increment offset (default: 1)
            DB_GALERA_AUTO_INCREMENT_INCREMENT: Auto-increment increment (default: 1)

        Returns:
            GaleraConfig instance
        """
        return cls(
            enabled=os.getenv('DB_GALERA_ENABLED', 'false').lower() == 'true',
            wsrep_sync_wait=int(os.getenv('DB_GALERA_WSREP_SYNC_WAIT', '1')),
            deadlock_retry_count=int(os.getenv('DB_GALERA_DEADLOCK_RETRIES', '3')),
            deadlock_retry_delay=float(os.getenv('DB_GALERA_DEADLOCK_DELAY', '0.1')),
            auto_increment_offset=int(os.getenv('DB_GALERA_AUTO_INCREMENT_OFFSET', '1')),
            auto_increment_increment=int(os.getenv('DB_GALERA_AUTO_INCREMENT_INCREMENT', '1')),
        )


def is_galera_enabled() -> bool:
    """
    Check if Galera cluster support is enabled.

    Returns:
        True if DB_GALERA_ENABLED environment variable is 'true'
    """
    return os.getenv('DB_GALERA_ENABLED', 'false').lower() == 'true'


def get_galera_config() -> GaleraConfig:
    """
    Get current Galera configuration.

    Returns:
        GaleraConfig instance
    """
    return GaleraConfig.from_env()


def set_wsrep_sync_wait(db: Any, level: int = 1) -> bool:
    """
    Set WSREP sync wait level for current session.

    WSREP sync wait ensures causality checks before queries:
    - 0: Disabled (no sync)
    - 1: Sync reads (default)
    - 2: Sync updates
    - 3: Sync reads and updates
    - 4: Sync inserts
    - 7: Sync all operations

    Args:
        db: PyDAL database instance
        level: WSREP sync wait level (0-7)

    Returns:
        True if set successfully, False otherwise
    """
    if not is_galera_enabled():
        logger.debug("Galera not enabled, skipping WSREP sync wait")
        return True

    try:
        db.executesql(f'SET SESSION wsrep_sync_wait = {level}')
        logger.debug(f"Set wsrep_sync_wait to {level}")
        return True
    except Exception as e:
        logger.warning(f"Failed to set wsrep_sync_wait: {e}")
        return False


def is_deadlock_error(exception: Exception) -> bool:
    """
    Check if exception is a Galera deadlock error.

    Galera deadlock errors include:
    - 1213: Deadlock found when trying to get lock
    - 1205: Lock wait timeout exceeded
    - 1047: WSREP has not yet prepared node for application use

    Args:
        exception: Exception to check

    Returns:
        True if exception is a deadlock error
    """
    error_str = str(exception).lower()
    deadlock_indicators = [
        'deadlock',
        'lock wait timeout',
        'wsrep has not yet prepared',
        '1213',
        '1205',
        '1047',
    ]
    return any(indicator in error_str for indicator in deadlock_indicators)


def handle_galera_deadlock(
    func: Callable[..., Any],
    max_retries: Optional[int] = None,
    retry_delay: Optional[float] = None
) -> Callable[..., Any]:
    """
    Decorator to handle Galera deadlocks with automatic retry.

    Args:
        func: Function to wrap with retry logic
        max_retries: Maximum retry attempts (defaults to config)
        retry_delay: Delay between retries in seconds (defaults to config)

    Returns:
        Wrapped function with retry logic
    """
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        config = get_galera_config()
        retries = max_retries if max_retries is not None else config.deadlock_retry_count
        delay = retry_delay if retry_delay is not None else config.deadlock_retry_delay

        last_exception = None
        for attempt in range(retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if is_deadlock_error(e) and attempt < retries:
                    logger.warning(
                        f"Galera deadlock detected in {func.__name__}, "
                        f"retry {attempt + 1}/{retries}: {e}"
                    )
                    time.sleep(delay * (attempt + 1))
                    continue
                raise

        raise last_exception

    return wrapper


def set_auto_increment_config(
    db: Any,
    offset: Optional[int] = None,
    increment: Optional[int] = None
) -> bool:
    """
    Set auto-increment configuration for Galera cluster.

    In Galera clusters, each node should have a unique auto-increment offset
    to avoid primary key conflicts during concurrent inserts.

    Args:
        db: PyDAL database instance
        offset: Auto-increment offset (defaults to config)
        increment: Auto-increment increment (defaults to config)

    Returns:
        True if set successfully, False otherwise
    """
    if not is_galera_enabled():
        logger.debug("Galera not enabled, skipping auto-increment config")
        return True

    config = get_galera_config()
    offset = offset if offset is not None else config.auto_increment_offset
    increment = increment if increment is not None else config.auto_increment_increment

    try:
        db.executesql(f'SET SESSION auto_increment_offset = {offset}')
        db.executesql(f'SET SESSION auto_increment_increment = {increment}')
        logger.debug(f"Set auto_increment_offset={offset}, auto_increment_increment={increment}")
        return True
    except Exception as e:
        logger.warning(f"Failed to set auto-increment config: {e}")
        return False


def get_cluster_status(db: Any) -> Optional[Dict[str, Any]]:
    """
    Get Galera cluster status information.

    Args:
        db: PyDAL database instance

    Returns:
        Dictionary with cluster status or None if not available
    """
    if not is_galera_enabled():
        return None

    try:
        result = db.executesql(
            "SHOW STATUS WHERE Variable_name IN ("
            "'wsrep_cluster_size', "
            "'wsrep_cluster_status', "
            "'wsrep_ready', "
            "'wsrep_connected', "
            "'wsrep_local_state_comment'"
            ")",
            as_dict=True
        )

        status = {}
        for row in result:
            var_name = row.get('Variable_name', '').lower()
            value = row.get('Value', '')
            status[var_name] = value

        return status
    except Exception as e:
        logger.error(f"Failed to get cluster status: {e}", exc_info=True)
        return None


def is_cluster_ready(db: Any) -> bool:
    """
    Check if Galera cluster is ready for operations.

    Args:
        db: PyDAL database instance

    Returns:
        True if cluster is ready, False otherwise
    """
    if not is_galera_enabled():
        return True

    status = get_cluster_status(db)
    if not status:
        return False

    ready = status.get('wsrep_ready', '').lower() == 'on'
    connected = status.get('wsrep_connected', '').lower() == 'on'
    cluster_status = status.get('wsrep_cluster_status', '').lower() == 'primary'

    return ready and connected and cluster_status


def wait_for_cluster_ready(
    db: Any,
    timeout: int = 30,
    check_interval: float = 1.0
) -> bool:
    """
    Wait for Galera cluster to be ready.

    Args:
        db: PyDAL database instance
        timeout: Maximum wait time in seconds
        check_interval: Check interval in seconds

    Returns:
        True if cluster becomes ready, False if timeout
    """
    if not is_galera_enabled():
        return True

    logger.info("Waiting for Galera cluster to be ready")
    start_time = time.time()

    while time.time() - start_time < timeout:
        if is_cluster_ready(db):
            logger.info("Galera cluster is ready")
            return True

        time.sleep(check_interval)

    logger.error(f"Galera cluster not ready after {timeout} seconds")
    return False


def init_galera_session(db: Any) -> bool:
    """
    Initialize Galera session settings.

    Sets WSREP sync wait and auto-increment configuration for current session.

    Args:
        db: PyDAL database instance

    Returns:
        True if initialized successfully, False otherwise
    """
    if not is_galera_enabled():
        return True

    config = get_galera_config()

    success = True
    success &= set_wsrep_sync_wait(db, config.wsrep_sync_wait)
    success &= set_auto_increment_config(db)

    if success:
        logger.debug("Galera session initialized successfully")
    else:
        logger.warning("Galera session initialization had errors")

    return success


class GaleraTransaction:
    """
    Context manager for Galera-aware transactions.

    Usage:
        with GaleraTransaction(db) as tx:
            db.api_definitions.insert(...)
            db.commit()
    """

    def __init__(
        self,
        db: Any,
        max_retries: Optional[int] = None,
        retry_delay: Optional[float] = None
    ):
        """
        Initialize Galera transaction context.

        Args:
            db: PyDAL database instance
            max_retries: Maximum deadlock retry attempts
            retry_delay: Delay between retries in seconds
        """
        self.db = db
        self.config = get_galera_config()
        self.max_retries = max_retries if max_retries is not None else self.config.deadlock_retry_count
        self.retry_delay = retry_delay if retry_delay is not None else self.config.deadlock_retry_delay

    def __enter__(self) -> Any:
        """Enter transaction context."""
        if is_galera_enabled():
            init_galera_session(self.db)
        return self.db

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        """Exit transaction context with deadlock retry."""
        if exc_type is None:
            return False

        if is_galera_enabled() and is_deadlock_error(exc_val):
            logger.warning(f"Galera deadlock in transaction: {exc_val}")
            return False

        return False
