# Database Architecture Migration

This document describes the migration from PyDAL-only to SQLAlchemy + PyDAL architecture.

## Architecture Overview

### Before (PyDAL-only)
- PyDAL handled both schema creation and runtime operations
- Migrations tracked in `/tmp/gough_migrations/`
- Single library for everything

### After (SQLAlchemy + PyDAL)
- **SQLAlchemy**: Database initialization, schema creation, migrations
- **PyDAL**: Runtime database operations (queries, CRUD)
- **Alembic**: Migration management
- Follows CLAUDE.md standards for database management

## File Changes

### New Files

1. **`/home/penguin/code/Gough/services/api-manager/app/models_sqlalchemy.py`**
   - SQLAlchemy ORM models for all tables
   - Defines database schema using SQLAlchemy declarative base
   - Function `create_all_tables()` creates schema

2. **`/home/penguin/code/Gough/services/api-manager/alembic.ini`**
   - Alembic configuration file
   - Database connection settings (overridden by environment variables)

3. **`/home/penguin/code/Gough/services/api-manager/alembic/env.py`**
   - Alembic environment configuration
   - Reads database URI from Config class
   - Handles offline and online migrations

4. **`/home/penguin/code/Gough/services/api-manager/alembic/script.py.mako`**
   - Template for generating migration files

5. **`/home/penguin/code/Gough/services/api-manager/alembic/README.md`**
   - Documentation for using Alembic migrations

6. **`/home/penguin/code/Gough/services/api-manager/scripts/db_migrate.sh`**
   - Helper script for common migration tasks
   - Commands: init, migrate, upgrade, downgrade, current, history

### Modified Files

1. **`/home/penguin/code/Gough/services/api-manager/app/models.py`**
   - Updated module docstring to reflect new architecture
   - Modified `init_db()` function:
     - Step 1: Create schema with SQLAlchemy
     - Step 2: Connect PyDAL with `migrate=False, fake_migrate=True`
   - All table definitions remain unchanged (PyDAL still uses them)
   - All helper functions remain unchanged (PyDAL for runtime operations)

2. **`/home/penguin/code/Gough/services/api-manager/requirements.txt`**
   - Added `SQLAlchemy==2.0.36`
   - Added `alembic==1.14.0`
   - Kept all existing dependencies

## How It Works

### Database Initialization (First Time)

```python
# In init_db() function:

# Step 1: SQLAlchemy creates tables
create_all_tables(db_uri)  # Creates all tables from SQLAlchemy models

# Step 2: PyDAL connects to existing tables
db = DAL(
    db_uri,
    migrate=False,        # Don't modify schema
    fake_migrate=True,    # Use existing tables
    folder=None          # No migration folder needed
)
```

### Schema Changes (Migrations)

1. **Modify SQLAlchemy models** in `app/models_sqlalchemy.py`
2. **Generate migration**: `alembic revision --autogenerate -m "description"`
3. **Apply migration**: `alembic upgrade head`
4. PyDAL automatically works with updated schema (no changes needed)

### Runtime Operations

All existing code continues to work unchanged:
- `get_user_by_id()`, `create_user()`, etc. use PyDAL
- All API endpoints use PyDAL for queries
- All database operations use PyDAL

## Migration Commands

### Using the Helper Script

```bash
# Initialize database (first time)
./scripts/db_migrate.sh init

# Create migration from model changes
./scripts/db_migrate.sh migrate "add new column to users"

# Apply migrations
./scripts/db_migrate.sh upgrade

# Rollback one migration
./scripts/db_migrate.sh downgrade

# Show current version
./scripts/db_migrate.sh current

# Show migration history
./scripts/db_migrate.sh history
```

### Using Alembic Directly

```bash
cd /home/penguin/code/Gough/services/api-manager

# Create migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Rollback
alembic downgrade -1

# Show status
alembic current
alembic history
```

## Removed Components

- **`/tmp/gough_migrations/`**: No longer created or used
- PyDAL migration tracking: Disabled with `migrate=False`

## Benefits

1. **Clear Separation of Concerns**:
   - SQLAlchemy handles schema/structure
   - PyDAL handles data/operations

2. **Better Migration Management**:
   - Alembic provides robust migration tracking
   - Version control for schema changes
   - Auto-generation of migrations from model changes

3. **CLAUDE.md Compliance**:
   - Follows project standards for database management
   - SQLAlchemy for initialization and migrations
   - PyDAL for runtime operations

4. **No Breaking Changes**:
   - All existing PyDAL code continues to work
   - All helper functions unchanged
   - All API endpoints unchanged

## Environment Variables

Same as before - no changes needed:

```bash
DB_TYPE=postgres
DB_HOST=localhost
DB_PORT=5432
DB_NAME=gough_db
DB_USER=gough_user
DB_PASS=gough_pass
DB_POOL_SIZE=10
```

## Testing

All existing tests should continue to work without modification. PyDAL operations are unchanged.

## Rollback Plan

If issues arise, you can temporarily revert `init_db()` to the old behavior by:

1. Comment out SQLAlchemy schema creation
2. Change PyDAL to `migrate=True`
3. Re-enable migrations folder

However, this is not recommended as it goes against CLAUDE.md standards.
