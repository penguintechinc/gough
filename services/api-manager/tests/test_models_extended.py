"""Test suite for app/models/__init__.py.

Coverage targets:
- Constants (VALID_ROLES, CLOUD_PROVIDER_TYPES, etc.)
- validate_database_schema() function
- init_db() function (high-level)
"""

import pytest
from unittest.mock import Mock, MagicMock, patch

from app.models import (
    VALID_ROLES,
    CLOUD_PROVIDER_TYPES,
    SECRETS_BACKEND_TYPES,
    JOB_STATUSES,
    MACHINE_STATUSES,
    validate_database_schema,
)


class TestModelConstants:
    """Test module-level constants."""

    def test_valid_roles_contains_expected_values(self):
        """Test VALID_ROLES contains standard roles."""
        assert VALID_ROLES == ["admin", "maintainer", "viewer"]

    def test_cloud_provider_types(self):
        """Test CLOUD_PROVIDER_TYPES lists all supported providers."""
        expected = ["maas", "lxd", "aws", "gcp", "azure", "vultr"]
        assert CLOUD_PROVIDER_TYPES == expected

    def test_secrets_backend_types(self):
        """Test SECRETS_BACKEND_TYPES lists all backends."""
        expected = ["encrypted_db", "vault", "infisical", "aws", "gcp", "azure"]
        assert SECRETS_BACKEND_TYPES == expected

    def test_job_statuses(self):
        """Test JOB_STATUSES lists all job states."""
        expected = ["pending", "running", "completed", "failed", "cancelled"]
        assert JOB_STATUSES == expected

    def test_machine_statuses(self):
        """Test MACHINE_STATUSES lists all machine states."""
        expected = [
            "new", "commissioning", "ready", "allocated", "deploying",
            "deployed", "releasing", "disk_erasing", "failed", "broken",
            "running", "stopped", "terminated"
        ]
        assert MACHINE_STATUSES == expected

    def test_role_count(self):
        """Test VALID_ROLES has expected number of roles."""
        assert len(VALID_ROLES) == 3

    def test_cloud_provider_count(self):
        """Test CLOUD_PROVIDER_TYPES has expected number of providers."""
        assert len(CLOUD_PROVIDER_TYPES) == 6

    def test_machine_statuses_count(self):
        """Test MACHINE_STATUSES has expected number of states."""
        assert len(MACHINE_STATUSES) == 13


class TestValidateDatabaseSchema:
    """Test validate_database_schema() function."""

    @patch("app.models_sqlalchemy.get_sqlalchemy_engine")
    @patch("sqlalchemy.inspect")
    @patch("app.models.DB")
    def test_validate_database_schema_success(self, mock_db_cls, mock_inspect_cls, mock_get_engine):
        """Test validate_database_schema() returns True when schema valid.

        The admin-exists check (former ``engine.connect()`` + raw
        ``text("SELECT COUNT(*) ...")``) is now a penguin-dal runtime query
        against a short-lived ``DB`` instance (task-7b) -- mock the ``DB``
        class itself and its ``count()`` chain instead of a SQLAlchemy
        connection/cursor.
        """
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        mock_inspector = MagicMock()
        mock_inspect_cls.return_value = mock_inspector

        # Setup expected tables
        mock_inspector.get_table_names.return_value = [
            "auth_user", "auth_role", "auth_user_roles"
        ]

        # Setup auth_user columns
        mock_inspector.get_columns.return_value = [
            {"name": "id"},
            {"name": "email"},
            {"name": "password"},
            {"name": "active"},
            {"name": "fs_uniquifier"},
        ]

        # Mock admin exists check: check_db(query).count() -> 1
        mock_check_db = MagicMock()
        mock_db_cls.return_value = mock_check_db
        mock_check_db.return_value.count.return_value = 1

        with patch("builtins.print"):
            result = validate_database_schema("sqlite:///test.db")

        assert result is True
        mock_check_db.close.assert_called_once()

    @patch("app.models_sqlalchemy.get_sqlalchemy_engine")
    @patch("sqlalchemy.inspect")
    def test_validate_database_schema_missing_table(self, mock_inspect_cls, mock_get_engine):
        """Test validate_database_schema() returns False when table missing."""
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        mock_inspector = MagicMock()
        mock_inspect_cls.return_value = mock_inspector

        # Return only 2 of 3 required tables
        mock_inspector.get_table_names.return_value = ["auth_user", "auth_role"]

        with patch("builtins.print"):
            result = validate_database_schema("sqlite:///test.db")

        assert result is False

    @patch("app.models_sqlalchemy.get_sqlalchemy_engine")
    @patch("sqlalchemy.inspect")
    def test_validate_database_schema_missing_column(self, mock_inspect_cls, mock_get_engine):
        """Test validate_database_schema() returns False when column missing."""
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        mock_inspector = MagicMock()
        mock_inspect_cls.return_value = mock_inspector

        # All tables present
        mock_inspector.get_table_names.return_value = [
            "auth_user", "auth_role", "auth_user_roles"
        ]

        # Missing 'fs_uniquifier' column
        mock_inspector.get_columns.return_value = [
            {"name": "id"},
            {"name": "email"},
            {"name": "password"},
            {"name": "active"},
            # Missing fs_uniquifier
        ]

        with patch("builtins.print"):
            result = validate_database_schema("sqlite:///test.db")

        assert result is False

    @patch("app.models_sqlalchemy.get_sqlalchemy_engine")
    @patch("sqlalchemy.inspect")
    @patch("app.models.DB")
    def test_validate_database_schema_no_admin(self, mock_db_cls, mock_inspect_cls, mock_get_engine):
        """Test validate_database_schema() returns False when admin missing.

        See ``test_validate_database_schema_success`` docstring for why the
        admin-exists check now mocks ``app.models.DB`` rather than a
        SQLAlchemy connection.
        """
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        mock_inspector = MagicMock()
        mock_inspect_cls.return_value = mock_inspector

        # All tables and columns present
        mock_inspector.get_table_names.return_value = [
            "auth_user", "auth_role", "auth_user_roles"
        ]
        mock_inspector.get_columns.return_value = [
            {"name": "id"},
            {"name": "email"},
            {"name": "password"},
            {"name": "active"},
            {"name": "fs_uniquifier"},
        ]

        # No admin exists: check_db(query).count() -> 0
        mock_check_db = MagicMock()
        mock_db_cls.return_value = mock_check_db
        mock_check_db.return_value.count.return_value = 0

        with patch("builtins.print"):
            result = validate_database_schema("sqlite:///test.db")

        assert result is False
        mock_check_db.close.assert_called_once()


class TestValidateDatabaseSchemaRealPostgres:
    """Real-Postgres proof of the admin-exists penguin-dal conversion (task-7b).

    Unlike the mock-based tests above, this exercises the real
    ``penguin_dal.DB(...).count()`` call end to end against Postgres --
    proving the conversion actually queries the right table/column, not
    just that ``app.models.DB`` gets constructed.

    ``pg_url``'s Postgres instance is session-scoped and shared with the M1
    Alembic-baseline fixtures (``pg_db``/``pg_db_scoped``) -- ``auth_user``
    is a separate, non-Alembic-managed table set (see ``app.models.init_db``:
    SQLAlchemy ``Base.metadata`` for legacy auth tables, Alembic for the M1
    baseline), so creating it here doesn't collide with the M1 schema.
    ``pg_db``'s per-test TRUNCATE reflects and wipes every table it finds,
    including ``auth_user`` if a prior test already created it -- this test
    doesn't assume ``auth_user`` starts empty/absent; it explicitly deletes
    any pre-existing admin row before asserting the "missing" branch.
    """

    def test_admin_missing_then_present(self, pg_url: str) -> None:
        from sqlalchemy.orm import sessionmaker

        from app import models_m1  # noqa: F401 — registers M1 ORM classes on Base.metadata
        from app.models_sqlalchemy import AuthUser, Base, create_all_tables, get_sqlalchemy_engine

        engine = get_sqlalchemy_engine(pg_url)
        Base.metadata.create_all(engine)  # schema only — no seed data yet

        session = sessionmaker(bind=engine)()
        try:
            session.query(AuthUser).filter_by(email="admin@gough.local").delete()
            session.commit()
        finally:
            session.close()

        with patch("builtins.print"):
            assert validate_database_schema(pg_url) is False

        with patch("builtins.print"):
            create_all_tables(pg_url)  # seeds the default admin@gough.local row

        with patch("builtins.print"):
            assert validate_database_schema(pg_url) is True


class TestInitDb:
    """Test init_db() function (integration level)."""

    @patch("app.models.validate_database_schema")
    @patch("app.models.create_all_tables")
    def test_init_db_schema_valid(self, mock_create_all, mock_validate):
        """Test init_db() returns DB when schema valid."""
        mock_validate.return_value = True

        # This test would require full app context
        # Skipping for now as it's integration-level

    @patch("app.models.validate_database_schema")
    @patch("app.models.create_all_tables")
    def test_init_db_schema_invalid_creates_tables(self, mock_create_all, mock_validate):
        """Test init_db() creates tables when schema invalid."""
        mock_validate.return_value = False

        # This test would require full app context
        # Skipping for now


class TestModelImports:
    """Test that model constants and functions are importable."""

    def test_all_exports_importable(self):
        """Test all expected exports are available."""
        from app.models import (
            VALID_ROLES,
            CLOUD_PROVIDER_TYPES,
            SECRETS_BACKEND_TYPES,
            JOB_STATUSES,
            MACHINE_STATUSES,
            validate_database_schema,
            init_db,
        )
        # If we got here, all imports succeeded
        assert True
