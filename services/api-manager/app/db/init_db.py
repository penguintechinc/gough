"""
Database schema initialization using SQLAlchemy.

USAGE: SQLAlchemy is used ONLY for initial schema creation.
All runtime operations use PyDAL (see database.py).

Per CLAUDE.md standards:
- SQLAlchemy: Database initialization and schema creation only
- PyDAL: ALL runtime database operations and migrations
"""

import os
import logging
from typing import Optional
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    JSON,
    Index,
    UniqueConstraint,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from datetime import datetime

logger = logging.getLogger(__name__)

Base = declarative_base()


class APIDefinition(Base):
    """API definition schema for SQLAlchemy initialization."""
    __tablename__ = 'api_definitions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    version = Column(String(50), nullable=False)
    path = Column(String(500), nullable=False)
    method = Column(String(10), nullable=False)
    description = Column(Text, nullable=True)
    openapi_spec = Column(JSON, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint('name', 'version', 'path', 'method', name='uix_api_endpoint'),
        Index('idx_api_name_version', 'name', 'version'),
        Index('idx_api_path', 'path'),
        Index('idx_api_enabled', 'enabled'),
    )


class APIUsage(Base):
    """API usage metrics schema for SQLAlchemy initialization."""
    __tablename__ = 'api_usage'

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_id = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    method = Column(String(10), nullable=False)
    path = Column(String(500), nullable=False)
    status_code = Column(Integer, nullable=False)
    response_time_ms = Column(Integer, nullable=False)
    user_id = Column(String(255), nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)

    __table_args__ = (
        Index('idx_usage_api_id', 'api_id'),
        Index('idx_usage_timestamp', 'timestamp'),
        Index('idx_usage_user_id', 'user_id'),
        Index('idx_usage_status', 'status_code'),
    )


class APIKey(Base):
    """API key management schema for SQLAlchemy initialization."""
    __tablename__ = 'api_keys'

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_hash = Column(String(255), nullable=False, unique=True)
    name = Column(String(255), nullable=False)
    user_id = Column(String(255), nullable=False)
    scopes = Column(JSON, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    rate_limit = Column(Integer, default=1000, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('idx_key_hash', 'key_hash'),
        Index('idx_key_user_id', 'user_id'),
        Index('idx_key_enabled', 'enabled'),
    )


def get_sqlalchemy_url(db_type: str, database_url: str) -> str:
    """
    Convert DB_TYPE and DATABASE_URL to SQLAlchemy connection string.

    Args:
        db_type: Database type (postgres, mysql, mariadb, sqlite)
        database_url: Database connection URL

    Returns:
        SQLAlchemy-compatible connection string
    """
    db_type_lower = db_type.lower()

    if db_type_lower in ['postgres', 'postgresql']:
        if not database_url.startswith('postgresql://') and not database_url.startswith('postgres://'):
            return f'postgresql://{database_url}'
        return database_url.replace('postgres://', 'postgresql://')

    elif db_type_lower in ['mysql', 'mariadb']:
        if not database_url.startswith('mysql://'):
            return f'mysql+pymysql://{database_url}'
        return database_url.replace('mysql://', 'mysql+pymysql://')

    elif db_type_lower == 'sqlite':
        if not database_url.startswith('sqlite://'):
            return f'sqlite:///{database_url}'
        return database_url

    else:
        raise ValueError(f"Unsupported DB_TYPE: {db_type}. Supported: postgres, mysql, mariadb, sqlite")


def init_db_schema(
    database_url: Optional[str] = None,
    db_type: Optional[str] = None,
    echo: bool = False
) -> bool:
    """
    Initialize database schema using SQLAlchemy.

    This function ONLY creates the schema structure.
    All runtime operations should use PyDAL (see database.py).

    Args:
        database_url: Database connection URL (defaults to DATABASE_URL env var)
        db_type: Database type (defaults to DB_TYPE env var)
        echo: Enable SQLAlchemy query logging

    Returns:
        True if schema initialized successfully, False otherwise
    """
    try:
        db_type = db_type or os.getenv('DB_TYPE', 'postgres')
        database_url = database_url or os.getenv('DATABASE_URL')

        if not database_url:
            raise ValueError("DATABASE_URL environment variable not set")

        sqlalchemy_url = get_sqlalchemy_url(db_type, database_url)

        logger.info(f"Initializing database schema with DB_TYPE={db_type}")

        engine = create_engine(
            sqlalchemy_url,
            echo=echo,
            poolclass=NullPool,
            connect_args={'connect_timeout': 10} if db_type != 'sqlite' else {}
        )

        Base.metadata.create_all(engine)

        logger.info("Database schema initialized successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to initialize database schema: {e}", exc_info=True)
        return False


def create_tables() -> bool:
    """
    Create all tables defined in SQLAlchemy models.

    Convenience wrapper around init_db_schema().

    Returns:
        True if tables created successfully, False otherwise
    """
    return init_db_schema()


def drop_tables(
    database_url: Optional[str] = None,
    db_type: Optional[str] = None
) -> bool:
    """
    Drop all tables (DANGEROUS - use only for testing).

    Args:
        database_url: Database connection URL
        db_type: Database type

    Returns:
        True if tables dropped successfully, False otherwise
    """
    try:
        db_type = db_type or os.getenv('DB_TYPE', 'postgres')
        database_url = database_url or os.getenv('DATABASE_URL')

        if not database_url:
            raise ValueError("DATABASE_URL environment variable not set")

        sqlalchemy_url = get_sqlalchemy_url(db_type, database_url)

        logger.warning("Dropping all database tables")

        engine = create_engine(
            sqlalchemy_url,
            poolclass=NullPool,
            connect_args={'connect_timeout': 10} if db_type != 'sqlite' else {}
        )

        Base.metadata.drop_all(engine)

        logger.info("All tables dropped successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to drop tables: {e}", exc_info=True)
        return False
