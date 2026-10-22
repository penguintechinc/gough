"""Alembic environment configuration for Gough API Manager.

This module configures Alembic to work with the Gough database using
environment variables for connection configuration.
"""

from logging.config import fileConfig
import os

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Import the Base and all models
import sys
import types
from pathlib import Path

# Add app directory to path (the bare "config" import below needs this)
service_root = Path(__file__).parent.parent
app_path = service_root / "app"
sys.path.insert(0, str(app_path))

from config import Config  # noqa: E402

# Register (or reuse) "app" as a real dotted package so every model module
# that does a relative import resolves onto the SAME Base/MetaData objects
# that target_metadata below is built from -- a bare `import models_m1`
# cannot work here, since its `from .models_sqlalchemy import Base` relative
# import requires a genuine parent package (see task-3b-report.md). Reuse an
# already-imported real "app" package if one exists (e.g. running inside the
# pytest process, where app/__init__.py already ran for real) rather than
# clobbering it; otherwise register a lightweight stand-in so a plain
# `alembic upgrade head` doesn't have to boot the full Quart app factory
# (Vault/SPIRE/etc.) just to read model metadata. Mirrors the identical
# trick in alembic/versions/20260805_1000_baseline_full_schema.py.
if "app" not in sys.modules:
    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = [str(app_path)]
    sys.modules["app"] = app_pkg
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

from app.models_sqlalchemy import Base  # noqa: E402

# Importing models_m1 registers its classes (nodes, biomes,
# node_egg_assignments, joiner_secrets, audit_events, leader_leases, ...)
# onto Base.metadata -- models_m1 shares Base rather than declaring its own
# (see app.models_sqlalchemy.create_all_tables, which does the same import
# for exactly this reason). Without this, autogenerate only sees the tables
# declared directly in models_sqlalchemy.py and would propose DROPping every
# M1 table on the next `alembic revision --autogenerate`.
from app import models_m1  # noqa: E402,F401
from app.db.init_db import Base as InitBase  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Add your models' MetaData objects here for 'autogenerate' support. Two
# declarative bases are in play (see the baseline migration and
# tests/pg_fixtures.py): Base (models_sqlalchemy.py + models_m1.py) and
# InitBase (app/db/init_db.py -- api_definitions/api_usage/api_keys).
# Alembic accepts a sequence of MetaData objects for autogenerate.
target_metadata = [Base.metadata, InitBase.metadata]

# Override sqlalchemy.url from environment variables
config.set_main_option("sqlalchemy.url", Config.get_db_uri())


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    # Get database URI with SQLAlchemy format
    db_uri = Config.get_db_uri()

    # Convert PyDAL format to SQLAlchemy format
    if db_uri.startswith("postgres://"):
        db_uri = db_uri.replace("postgres://", "postgresql://", 1)
    elif db_uri.startswith("sqlite:") and "memory" not in db_uri:
        db_uri = db_uri.replace("sqlite://", "sqlite:///", 1)

    # Override the config
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = db_uri

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
