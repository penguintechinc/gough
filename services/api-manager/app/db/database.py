"""
PyDAL database configuration for runtime operations.

USAGE: PyDAL handles ALL runtime database operations and migrations.
SQLAlchemy is only used for initial schema creation (see init_db.py).

Per CLAUDE.md standards:
- PyDAL: ALL runtime database operations and migrations (migrate=True)
- SQLAlchemy: Database initialization and schema creation only

Thread Safety:
- Thread-local storage for database connections
- Connection pooling via PyDAL
- Safe for use with asyncio.to_thread() for blocking operations
"""

import os
import logging
import threading
from typing import Optional, Dict, Any, List
from contextlib import contextmanager
from pydal import DAL, Field
from datetime import datetime

logger = logging.getLogger(__name__)

# Thread-local storage for database connections
_thread_local = threading.local()


def get_pydal_uri(db_type: str, database_url: str) -> str:
    """
    Convert DB_TYPE and DATABASE_URL to PyDAL connection string.

    Args:
        db_type: Database type (postgres, mysql, mariadb, sqlite)
        database_url: Database connection URL

    Returns:
        PyDAL-compatible connection string
    """
    db_type_lower = db_type.lower()

    if db_type_lower in ['postgres', 'postgresql']:
        if database_url.startswith('postgresql://') or database_url.startswith('postgres://'):
            return database_url.replace('postgres://', 'postgres://')
        return f'postgres://{database_url}'

    elif db_type_lower in ['mysql', 'mariadb']:
        if database_url.startswith('mysql://'):
            return database_url
        return f'mysql://{database_url}'

    elif db_type_lower == 'sqlite':
        if database_url.startswith('sqlite://'):
            return database_url
        return f'sqlite://{database_url}'

    else:
        raise ValueError(f"Unsupported DB_TYPE: {db_type}. Supported: postgres, mysql, mariadb, sqlite")


def define_tables(db: DAL) -> None:
    """
    Define PyDAL table schemas with migrations enabled.

    Args:
        db: PyDAL database instance
    """
    db.define_table(
        'api_definitions',
        Field('name', 'string', length=255, notnull=True),
        Field('version', 'string', length=50, notnull=True),
        Field('path', 'string', length=500, notnull=True),
        Field('method', 'string', length=10, notnull=True),
        Field('description', 'text'),
        Field('openapi_spec', 'json'),
        Field('enabled', 'boolean', default=True, notnull=True),
        Field('created_at', 'datetime', default=datetime.utcnow, notnull=True),
        Field('updated_at', 'datetime', default=datetime.utcnow, update=datetime.utcnow, notnull=True),
    )

    db.define_table(
        'api_usage',
        Field('api_id', 'integer', notnull=True),
        Field('timestamp', 'datetime', default=datetime.utcnow, notnull=True),
        Field('method', 'string', length=10, notnull=True),
        Field('path', 'string', length=500, notnull=True),
        Field('status_code', 'integer', notnull=True),
        Field('response_time_ms', 'integer', notnull=True),
        Field('user_id', 'string', length=255),
        Field('ip_address', 'string', length=45),
        Field('user_agent', 'string', length=500),
    )

    db.define_table(
        'api_keys',
        Field('key_hash', 'string', length=255, notnull=True, unique=True),
        Field('name', 'string', length=255, notnull=True),
        Field('user_id', 'string', length=255, notnull=True),
        Field('scopes', 'json', notnull=True),
        Field('enabled', 'boolean', default=True, notnull=True),
        Field('rate_limit', 'integer', default=1000, notnull=True),
        Field('created_at', 'datetime', default=datetime.utcnow, notnull=True),
        Field('expires_at', 'datetime'),
        Field('last_used_at', 'datetime'),
    )

    db.commit()


def init_pydal(
    database_url: Optional[str] = None,
    db_type: Optional[str] = None,
    pool_size: int = 10,
    migrate: bool = True,
    fake_migrate: bool = False
) -> DAL:
    """
    Initialize PyDAL database connection with migrations enabled.

    Args:
        database_url: Database connection URL (defaults to DATABASE_URL env var)
        db_type: Database type (defaults to DB_TYPE env var)
        pool_size: Connection pool size
        migrate: Enable automatic migrations
        fake_migrate: Enable fake migrations (for manual schema management)

    Returns:
        PyDAL DAL instance
    """
    db_type = db_type or os.getenv('DB_TYPE', 'postgres')
    database_url = database_url or os.getenv('DATABASE_URL')

    if not database_url:
        raise ValueError("DATABASE_URL environment variable not set")

    pydal_uri = get_pydal_uri(db_type, database_url)

    logger.info(f"Initializing PyDAL with DB_TYPE={db_type}, pool_size={pool_size}")

    db = DAL(
        pydal_uri,
        pool_size=pool_size,
        migrate=migrate,
        fake_migrate=fake_migrate,
        check_reserved=['all'],
        folder=os.getenv('PYDAL_MIGRATIONS_FOLDER', 'databases'),
    )

    define_tables(db)

    logger.info("PyDAL initialized successfully")
    return db


def get_db() -> DAL:
    """
    Get thread-local database connection.

    Returns:
        PyDAL DAL instance for current thread

    Raises:
        RuntimeError: If database not initialized for current thread
    """
    if not hasattr(_thread_local, 'db') or _thread_local.db is None:
        _thread_local.db = init_pydal()

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
def get_db_context():
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


def execute_query(
    query: str,
    params: Optional[Dict[str, Any]] = None,
    fetch: bool = True
) -> Optional[List[Dict[str, Any]]]:
    """
    Execute raw SQL query with parameter binding.

    Args:
        query: SQL query string
        params: Query parameters for binding
        fetch: Whether to fetch results

    Returns:
        List of row dictionaries if fetch=True, None otherwise
    """
    db = get_db()
    try:
        result = db.executesql(query, placeholders=params or {}, as_dict=True)
        if fetch:
            return result
        db.commit()
        return None
    except Exception as e:
        logger.error(f"Query execution failed: {e}", exc_info=True)
        db.rollback()
        raise


def get_connection_info() -> Dict[str, Any]:
    """
    Get current database connection information.

    Returns:
        Dictionary with connection details
    """
    db = get_db()
    return {
        'db_type': os.getenv('DB_TYPE', 'postgres'),
        'pool_size': db._pool_size,
        'migrate_enabled': db._migrate,
        'tables': list(db.tables),
        'adapter': str(type(db._adapter).__name__),
    }


def insert_api_definition(
    name: str,
    version: str,
    path: str,
    method: str,
    description: Optional[str] = None,
    openapi_spec: Optional[Dict[str, Any]] = None,
    enabled: bool = True
) -> int:
    """
    Insert new API definition.

    Args:
        name: API name
        version: API version
        path: API path
        method: HTTP method
        description: API description
        openapi_spec: OpenAPI specification
        enabled: Whether API is enabled

    Returns:
        ID of inserted record
    """
    db = get_db()
    record_id = db.api_definitions.insert(
        name=name,
        version=version,
        path=path,
        method=method,
        description=description,
        openapi_spec=openapi_spec,
        enabled=enabled,
    )
    db.commit()
    return record_id


def get_api_definitions(
    name: Optional[str] = None,
    version: Optional[str] = None,
    enabled: Optional[bool] = None
) -> List[Dict[str, Any]]:
    """
    Get API definitions with optional filters.

    Args:
        name: Filter by API name
        version: Filter by version
        enabled: Filter by enabled status

    Returns:
        List of API definition dictionaries
    """
    db = get_db()
    query = db.api_definitions.id > 0

    if name is not None:
        query &= (db.api_definitions.name == name)
    if version is not None:
        query &= (db.api_definitions.version == version)
    if enabled is not None:
        query &= (db.api_definitions.enabled == enabled)

    rows = db(query).select()
    return [row.as_dict() for row in rows]


def insert_api_usage(
    api_id: int,
    method: str,
    path: str,
    status_code: int,
    response_time_ms: int,
    user_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None
) -> int:
    """
    Insert API usage record.

    Args:
        api_id: API definition ID
        method: HTTP method
        path: Request path
        status_code: HTTP status code
        response_time_ms: Response time in milliseconds
        user_id: User ID
        ip_address: Client IP address
        user_agent: User agent string

    Returns:
        ID of inserted record
    """
    db = get_db()
    record_id = db.api_usage.insert(
        api_id=api_id,
        method=method,
        path=path,
        status_code=status_code,
        response_time_ms=response_time_ms,
        user_id=user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.commit()
    return record_id


def insert_api_key(
    key_hash: str,
    name: str,
    user_id: str,
    scopes: List[str],
    enabled: bool = True,
    rate_limit: int = 1000,
    expires_at: Optional[datetime] = None
) -> int:
    """
    Insert new API key.

    Args:
        key_hash: Hashed API key
        name: Key name/description
        user_id: User ID
        scopes: List of permission scopes
        enabled: Whether key is enabled
        rate_limit: Rate limit per hour
        expires_at: Expiration datetime

    Returns:
        ID of inserted record
    """
    db = get_db()
    record_id = db.api_keys.insert(
        key_hash=key_hash,
        name=name,
        user_id=user_id,
        scopes=scopes,
        enabled=enabled,
        rate_limit=rate_limit,
        expires_at=expires_at,
    )
    db.commit()
    return record_id


def get_api_key_by_hash(key_hash: str) -> Optional[Dict[str, Any]]:
    """
    Get API key by hash.

    Args:
        key_hash: Hashed API key

    Returns:
        API key dictionary or None if not found
    """
    db = get_db()
    row = db(db.api_keys.key_hash == key_hash).select().first()
    return row.as_dict() if row else None


def update_api_key_last_used(key_id: int) -> bool:
    """
    Update API key last used timestamp.

    Args:
        key_id: API key ID

    Returns:
        True if updated, False otherwise
    """
    db = get_db()
    updated = db(db.api_keys.id == key_id).update(last_used_at=datetime.utcnow())
    db.commit()
    return updated > 0
