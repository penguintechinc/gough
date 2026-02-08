#!/bin/bash
# Database migration helper script for Gough API Manager

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Check if alembic is installed
if ! command -v alembic &> /dev/null; then
    echo "Error: alembic not found. Please install: pip install alembic"
    exit 1
fi

# Function to display usage
usage() {
    echo "Usage: $0 {init|migrate|upgrade|downgrade|current|history}"
    echo ""
    echo "Commands:"
    echo "  init              - Initialize database schema (first time setup)"
    echo "  migrate <msg>     - Create new migration from model changes"
    echo "  upgrade [target]  - Apply migrations (default: head)"
    echo "  downgrade [target]- Rollback migrations (default: -1)"
    echo "  current           - Show current migration version"
    echo "  history           - Show migration history"
    echo ""
    echo "Examples:"
    echo "  $0 init"
    echo "  $0 migrate 'add user table'"
    echo "  $0 upgrade head"
    echo "  $0 downgrade -1"
    exit 1
}

# Check for command
if [ $# -lt 1 ]; then
    usage
fi

COMMAND="$1"
shift

case "$COMMAND" in
    init)
        echo "Initializing database schema with SQLAlchemy..."
        python3 -c "
from app.config import Config
from app.models_sqlalchemy import create_all_tables
db_uri = Config.get_db_uri()
print(f'Creating tables for: {db_uri}')
create_all_tables(db_uri)
print('Database initialized successfully!')
"
        ;;

    migrate)
        if [ $# -lt 1 ]; then
            echo "Error: Migration message required"
            echo "Usage: $0 migrate 'migration message'"
            exit 1
        fi
        MESSAGE="$1"
        echo "Creating migration: $MESSAGE"
        alembic revision --autogenerate -m "$MESSAGE"
        ;;

    upgrade)
        TARGET="${1:-head}"
        echo "Upgrading database to: $TARGET"
        alembic upgrade "$TARGET"
        ;;

    downgrade)
        TARGET="${1:--1}"
        echo "Downgrading database to: $TARGET"
        alembic downgrade "$TARGET"
        ;;

    current)
        echo "Current database version:"
        alembic current
        ;;

    history)
        echo "Migration history:"
        alembic history --verbose
        ;;

    *)
        echo "Error: Unknown command '$COMMAND'"
        usage
        ;;
esac
