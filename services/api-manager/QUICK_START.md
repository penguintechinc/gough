# Quick Start - SQLAlchemy + PyDAL

## Installation

```bash
cd /home/penguin/code/Gough/services/api-manager
pip install -r requirements.txt
```

## Database Initialization

```bash
# Option 1: Use helper script
./scripts/db_migrate.sh init

# Option 2: Manual
python3 -c "from app.config import Config; from app.models_sqlalchemy import create_all_tables; create_all_tables(Config.get_db_uri())"

# Option 3: Start the application (auto-initializes)
python3 run.py
```

## Making Schema Changes

```bash
# 1. Edit SQLAlchemy models
nano app/models_sqlalchemy.py

# 2. Create migration
./scripts/db_migrate.sh migrate "add new column"

# 3. Apply migration
./scripts/db_migrate.sh upgrade
```

## Runtime Operations (No Changes)

All existing PyDAL code works as before:

```python
from app.models import get_user_by_id, create_user

# These work exactly as before
user = get_user_by_id(1)
new_user = create_user(email="test@example.com", password_hash="...")
```

## Migration Commands

```bash
./scripts/db_migrate.sh init         # Initialize database
./scripts/db_migrate.sh migrate MSG  # Create migration
./scripts/db_migrate.sh upgrade      # Apply migrations
./scripts/db_migrate.sh downgrade    # Rollback
./scripts/db_migrate.sh current      # Show version
./scripts/db_migrate.sh history      # Show history
```

## Key Files

- `app/models_sqlalchemy.py` - SQLAlchemy models (edit for schema changes)
- `app/models.py` - PyDAL definitions (runtime operations)
- `alembic/versions/` - Migration files
- `scripts/db_migrate.sh` - Helper script

## Documentation

- [MIGRATION_SUMMARY.md](MIGRATION_SUMMARY.md) - Complete summary
- [DATABASE_MIGRATION.md](DATABASE_MIGRATION.md) - Detailed architecture
- [alembic/README.md](alembic/README.md) - Alembic commands

## Architecture

```
┌─────────────────────────────────────┐
│     SQLAlchemy (Schema)             │
│  - Database initialization          │
│  - Schema creation                  │
│  - Migrations (Alembic)             │
└─────────────────────────────────────┘
                 ↓
┌─────────────────────────────────────┐
│       Database Tables               │
│  - auth_user, auth_role, etc.       │
│  - Created by SQLAlchemy            │
│  - Used by PyDAL                    │
└─────────────────────────────────────┘
                 ↓
┌─────────────────────────────────────┐
│      PyDAL (Runtime)                │
│  - Queries and CRUD operations      │
│  - All existing code                │
│  - migrate=False, fake_migrate=True │
└─────────────────────────────────────┘
```

## Environment Variables

```bash
DB_TYPE=postgres          # postgres, mysql, sqlite
DB_HOST=localhost
DB_PORT=5432
DB_NAME=gough_db
DB_USER=gough_user
DB_PASS=gough_pass
DB_POOL_SIZE=10
```

## Troubleshooting

**Problem**: "Table already exists" error
**Solution**: SQLAlchemy handles schema creation. If tables exist, it skips creation.

**Problem**: PyDAL validation errors
**Solution**: PyDAL definitions must match SQLAlchemy models. Keep both in sync.

**Problem**: Migration conflicts
**Solution**: Check `alembic current` and resolve conflicts manually in migration files.

## Testing

```bash
# Run tests (existing tests should work unchanged)
pytest

# Check database connection
python3 -c "from app.models import init_db; from quart import Quart; app = Quart(__name__); init_db(app)"
```

## Next Steps

1. Test database connection: `python3 run.py`
2. Verify PyDAL operations work
3. Create first migration (optional): `./scripts/db_migrate.sh migrate "initial schema"`
4. Update CI/CD pipelines to run migrations before deployment
