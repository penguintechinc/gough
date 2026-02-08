# Database Migrations with Alembic

This directory contains Alembic migration scripts for the Gough API Manager database.

## Architecture

- **SQLAlchemy**: Handles database schema creation and migrations
- **PyDAL**: Handles runtime database operations (queries, CRUD)

## Common Commands

### Create a new migration

```bash
# Auto-generate migration from model changes
cd /home/penguin/code/Gough/services/api-manager
alembic revision --autogenerate -m "description of changes"

# Create empty migration template
alembic revision -m "description of changes"
```

### Apply migrations

```bash
# Upgrade to latest version
alembic upgrade head

# Upgrade one version
alembic upgrade +1

# Downgrade one version
alembic downgrade -1

# Downgrade to specific revision
alembic downgrade <revision_id>
```

### View migration history

```bash
# Show current version
alembic current

# Show migration history
alembic history

# Show pending migrations
alembic history --verbose
```

## Environment Variables

Alembic reads database configuration from environment variables (same as the application):

- `DB_TYPE`: Database type (postgres, mysql, sqlite)
- `DB_HOST`: Database host
- `DB_PORT`: Database port
- `DB_NAME`: Database name
- `DB_USER`: Database user
- `DB_PASS`: Database password

## Migration Workflow

1. Modify SQLAlchemy models in `app/models_sqlalchemy.py`
2. Generate migration: `alembic revision --autogenerate -m "add new table"`
3. Review generated migration in `alembic/versions/`
4. Apply migration: `alembic upgrade head`
5. PyDAL automatically connects to updated schema (no PyDAL migration needed)

## Notes

- PyDAL is configured with `migrate=False` and `fake_migrate=True`
- All schema changes must go through SQLAlchemy/Alembic
- PyDAL only performs runtime operations on existing tables
- The `/tmp/gough_migrations` folder is no longer used
