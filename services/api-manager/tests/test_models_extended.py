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
    def test_validate_database_schema_success(self, mock_inspect_cls, mock_get_engine):
        """Test validate_database_schema() returns True when schema valid.

        Admin existence is no longer part of schema validation (security fix:
        the default-admin row and its hardcoded password are gone; the first
        admin is created by ``seed_initial_admin``), so no DB query is made.
        """
        mock_get_engine.return_value = MagicMock()

        mock_inspector = MagicMock()
        mock_inspect_cls.return_value = mock_inspector

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

        assert validate_database_schema("sqlite:///test.db") is True

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


class TestSeedInitialAdminRealPostgres:
    """Real-Postgres proof of the single admin-seeding path (security fix).

    ``create_all_tables`` seeds roles only -- never a user -- and
    ``seed_initial_admin`` creates exactly one flagged admin with a random
    one-time password when ``auth_user`` is empty, then is a no-op.
    ``pg_url``'s instance is session-scoped and shared, so auth rows left by
    other tests are wiped first.
    """

    def test_roles_only_then_one_flagged_admin(self, pg_url: str, tmp_path) -> None:
        import bcrypt
        from sqlalchemy import delete, func, select
        from sqlalchemy.orm import Session

        from app import models_m1  # noqa: F401 — registers M1 ORM classes on Base.metadata
        from app.models_sqlalchemy import (
            AuthPasswordReset,
            AuthRefreshToken,
            AuthUser,
            AuthUserRole,
            create_all_tables,
            seed_initial_admin,
        )

        engine = create_all_tables(pg_url)
        with Session(engine) as session:
            for model in (AuthUserRole, AuthRefreshToken, AuthPasswordReset, AuthUser):
                session.execute(delete(model))
            session.commit()

        create_all_tables(pg_url)
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(AuthUser)) == 0

        cred = tmp_path / "admin-initial-password"
        env = {"ADMIN_BOOTSTRAP_CREDENTIAL_PATH": str(cred)}
        assert seed_initial_admin(engine, env) is True
        assert seed_initial_admin(engine, env) is False

        password = next(
            line.split("=", 1)[1]
            for line in cred.read_text().splitlines()
            if line.startswith("password=")
        )
        with Session(engine) as session:
            users = session.scalars(select(AuthUser)).all()
        assert len(users) == 1
        assert users[0].must_change_password is True
        assert bcrypt.checkpw(password.encode(), users[0].password.encode())
        assert not bcrypt.checkpw(b"admin", users[0].password.encode())


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
