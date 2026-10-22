"""Unit tests for models/__init__.py - Database initialization and validation.

Tests for:
- validate_database_schema() - SQLAlchemy schema validation
- init_db() - Database connection initialization
- get_db() - Request-scoped database access
"""

import os
import sys
from unittest.mock import MagicMock, patch, Mock, call
from datetime import datetime

import pytest

_API_MANAGER_PATH = '/home/penguin/code/gough/services/api-manager'
if _API_MANAGER_PATH not in sys.path:
    sys.path.insert(0, _API_MANAGER_PATH)
for _mod in list(sys.modules.keys()):
    if _mod == 'app' or _mod.startswith('app.'):
        del sys.modules[_mod]

from app.models import (
    validate_database_schema,
    init_db,
    get_db,
    VALID_ROLES,
    CLOUD_PROVIDER_TYPES,
    SECRETS_BACKEND_TYPES,
    JOB_STATUSES,
    MACHINE_STATUSES,
)


class TestValidateDatabaseSchema:
    """Tests for validate_database_schema() function."""

    @patch('app.models_sqlalchemy.get_sqlalchemy_engine')
    def test_validate_schema_success(self, mock_get_engine):
        """validate_database_schema returns True when all tables and columns exist."""
        # Setup mock engine and inspector
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        # Mock the inspector to return expected tables and columns
        mock_inspector = MagicMock()
        with patch('sqlalchemy.inspect', return_value=mock_inspector):
            mock_inspector.get_table_names.return_value = [
                'auth_user', 'auth_role', 'auth_user_roles', 'other_table'
            ]
            mock_inspector.get_columns.return_value = [
                {'name': 'id'},
                {'name': 'email'},
                {'name': 'password'},
                {'name': 'active'},
                {'name': 'fs_uniquifier'},
                {'name': 'extra_column'},
            ]

            # Mock engine.connect() to return admin user
            mock_conn = MagicMock()
            mock_result = MagicMock()
            mock_result.scalar.return_value = 1
            mock_conn.execute.return_value = mock_result
            mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

            result = validate_database_schema('postgresql://localhost/test')

            assert result is True
            mock_inspector.get_table_names.assert_called_once()
            mock_inspector.get_columns.assert_called_once_with('auth_user')

    @patch('app.models_sqlalchemy.get_sqlalchemy_engine')
    def test_validate_schema_missing_table(self, mock_get_engine):
        """validate_database_schema returns False when a critical table is missing."""
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        mock_inspector = MagicMock()
        with patch('sqlalchemy.inspect', return_value=mock_inspector):
            # Missing auth_role table
            mock_inspector.get_table_names.return_value = [
                'auth_user', 'auth_user_roles'
            ]

            with patch('builtins.print') as mock_print:
                result = validate_database_schema('postgresql://localhost/test')

            assert result is False
            mock_print.assert_called_with('Missing table: auth_role')

    @patch('app.models_sqlalchemy.get_sqlalchemy_engine')
    def test_validate_schema_missing_column(self, mock_get_engine):
        """validate_database_schema returns False when required columns are missing."""
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        mock_inspector = MagicMock()
        with patch('sqlalchemy.inspect', return_value=mock_inspector):
            mock_inspector.get_table_names.return_value = [
                'auth_user', 'auth_role', 'auth_user_roles'
            ]
            # Missing 'fs_uniquifier' column
            mock_inspector.get_columns.return_value = [
                {'name': 'id'},
                {'name': 'email'},
                {'name': 'password'},
                {'name': 'active'},
            ]

            with patch('builtins.print') as mock_print:
                result = validate_database_schema('postgresql://localhost/test')

            assert result is False
            # Verify that the missing column message was printed
            printed_calls = [str(call) for call in mock_print.call_args_list]
            assert any('fs_uniquifier' in str(c) for c in printed_calls)

    @patch('app.models_sqlalchemy.get_sqlalchemy_engine')
    def test_validate_schema_no_admin_user(self, mock_get_engine):
        """validate_database_schema returns False when default admin user is missing."""
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        mock_inspector = MagicMock()
        with patch('sqlalchemy.inspect', return_value=mock_inspector):
            mock_inspector.get_table_names.return_value = [
                'auth_user', 'auth_role', 'auth_user_roles'
            ]
            mock_inspector.get_columns.return_value = [
                {'name': 'id'},
                {'name': 'email'},
                {'name': 'password'},
                {'name': 'active'},
                {'name': 'fs_uniquifier'},
            ]

            # Mock engine.connect() - admin user count = 0
            mock_conn = MagicMock()
            mock_result = MagicMock()
            mock_result.scalar.return_value = 0
            mock_conn.execute.return_value = mock_result
            mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

            with patch('builtins.print') as mock_print:
                result = validate_database_schema('postgresql://localhost/test')

            assert result is False
            mock_print.assert_called_with('Default admin user not found')

    @patch('app.models_sqlalchemy.get_sqlalchemy_engine')
    def test_validate_schema_checks_all_expected_tables(self, mock_get_engine):
        """validate_database_schema checks all three expected tables."""
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        mock_inspector = MagicMock()
        with patch('sqlalchemy.inspect', return_value=mock_inspector):
            mock_inspector.get_table_names.return_value = []

            with patch('builtins.print') as mock_print:
                validate_database_schema('postgresql://localhost/test')

            # Should attempt to find all three tables
            print_calls = [call[0][0] for call in mock_print.call_args_list]
            assert any('auth_user' in str(c) for c in print_calls)

    @patch('app.models_sqlalchemy.get_sqlalchemy_engine')
    def test_validate_schema_prints_success(self, mock_get_engine):
        """validate_database_schema prints success message when validation passes."""
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        mock_inspector = MagicMock()
        with patch('sqlalchemy.inspect', return_value=mock_inspector):
            mock_inspector.get_table_names.return_value = [
                'auth_user', 'auth_role', 'auth_user_roles'
            ]
            mock_inspector.get_columns.return_value = [
                {'name': 'id'},
                {'name': 'email'},
                {'name': 'password'},
                {'name': 'active'},
                {'name': 'fs_uniquifier'},
            ]

            mock_conn = MagicMock()
            mock_result = MagicMock()
            mock_result.scalar.return_value = 1
            mock_conn.execute.return_value = mock_result
            mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

            with patch('builtins.print') as mock_print:
                result = validate_database_schema('postgresql://localhost/test')

            assert result is True
            mock_print.assert_called_with('Database schema validation passed')


class TestInitDb:
    """Tests for init_db() function."""

    @patch('app.models.create_all_tables')
    @patch('app.models.validate_database_schema')
    @patch('app.models.DB')
    def test_init_db_success_with_valid_schema(self, mock_db_cls,
                                                mock_validate, mock_create_all):
        """init_db returns DB instance when schema is valid."""
        from app.config import Config

        # Create a mock config with get_db_uri method
        with patch.object(Config, 'get_db_uri', return_value='postgresql://localhost/test'):
            with patch.object(Config, 'DB_POOL_SIZE', 10):
                mock_validate.return_value = True
                mock_db_instance = MagicMock()
                mock_db_cls.return_value = mock_db_instance

                mock_app = MagicMock()

                with patch('builtins.print') as mock_print:
                    result = init_db(mock_app)

                assert result is mock_db_instance
                mock_validate.assert_called_once_with('postgresql://localhost/test')
                mock_create_all.assert_not_called()  # Schema already valid, don't create
                mock_db_cls.assert_called_once_with('postgresql://localhost/test', pool_size=10)
                mock_app.config.__setitem__.assert_called_once_with('db', mock_db_instance)

    @patch('app.models.create_all_tables')
    @patch('app.models.validate_database_schema')
    @patch('app.models.DB')
    def test_init_db_creates_schema_when_invalid(self, mock_db_cls,
                                                   mock_validate, mock_create_all):
        """init_db creates schema if validation fails."""
        from app.config import Config

        with patch.object(Config, 'get_db_uri', return_value='postgresql://localhost/test'):
            with patch.object(Config, 'DB_POOL_SIZE', 10):
                mock_validate.return_value = False  # Schema invalid
                mock_db_instance = MagicMock()
                mock_db_cls.return_value = mock_db_instance

                mock_app = MagicMock()

                with patch('builtins.print') as mock_print:
                    result = init_db(mock_app)

                assert result is mock_db_instance
                mock_validate.assert_called_once()
                mock_create_all.assert_called_once_with('postgresql://localhost/test')
                mock_db_cls.assert_called_once()

    @patch('app.models.create_all_tables')
    @patch('app.models.validate_database_schema')
    @patch('app.models.DB')
    def test_init_db_stores_db_in_app_config(self, mock_db_cls,
                                              mock_validate, mock_create_all):
        """init_db stores database instance in app.config['db']."""
        from app.config import Config

        with patch.object(Config, 'get_db_uri', return_value='sqlite:///:memory:'):
            with patch.object(Config, 'DB_POOL_SIZE', 5):
                mock_validate.return_value = True
                mock_db_instance = MagicMock()
                mock_db_cls.return_value = mock_db_instance

                mock_app = MagicMock()

                init_db(mock_app)

                mock_app.config.__setitem__.assert_called_once_with('db', mock_db_instance)

    @patch('app.models.create_all_tables')
    @patch('app.models.validate_database_schema')
    @patch('app.models.DB')
    def test_init_db_uses_correct_pool_size(self, mock_db_cls,
                                             mock_validate, mock_create_all):
        """init_db passes DB_POOL_SIZE from Config to DB constructor."""
        from app.config import Config

        with patch.object(Config, 'get_db_uri', return_value='postgresql://localhost/test'):
            with patch.object(Config, 'DB_POOL_SIZE', 20):
                mock_validate.return_value = True
                mock_db_cls.return_value = MagicMock()

                mock_app = MagicMock()

                init_db(mock_app)

                call_kwargs = mock_db_cls.call_args[1]
                assert call_kwargs['pool_size'] == 20

    @patch('app.models.create_all_tables')
    @patch('app.models.validate_database_schema')
    @patch('app.models.DB')
    def test_init_db_prints_status_messages(self, mock_db_cls,
                                             mock_validate, mock_create_all):
        """init_db prints appropriate status messages."""
        from app.config import Config

        with patch.object(Config, 'get_db_uri', return_value='postgresql://localhost/test'):
            with patch.object(Config, 'DB_POOL_SIZE', 10):
                mock_validate.return_value = True
                mock_db_cls.return_value = MagicMock()

                mock_app = MagicMock()

                with patch('builtins.print') as mock_print:
                    init_db(mock_app)

                # Should print that schema already exists
                mock_print.assert_called_with('Database schema already exists and is valid')

    @patch('app.models.create_all_tables')
    @patch('app.models.validate_database_schema')
    @patch('app.models.DB')
    def test_init_db_prints_creation_message(self, mock_db_cls,
                                              mock_validate, mock_create_all):
        """init_db prints creation message when schema is created."""
        from app.config import Config

        with patch.object(Config, 'get_db_uri', return_value='postgresql://localhost/test'):
            with patch.object(Config, 'DB_POOL_SIZE', 10):
                mock_validate.return_value = False  # Will trigger creation
                mock_db_cls.return_value = MagicMock()

                mock_app = MagicMock()

                with patch('builtins.print') as mock_print:
                    init_db(mock_app)

                # Should print creation message
                printed = [call[0][0] for call in mock_print.call_args_list]
                assert any('schema created' in str(p).lower() for p in printed)


class TestGetDb:
    """Tests for get_db() function."""

    @pytest.mark.asyncio
    async def test_get_db_returns_db_when_in_g(self):
        """get_db returns database from g object when already set."""
        from quart import Quart
        app = Quart(__name__)
        mock_db = MagicMock()

        async with app.app_context():
            from quart import g
            g.db = mock_db

            result = get_db()

            assert result is mock_db

    @pytest.mark.asyncio
    async def test_get_db_fetches_from_app_config_when_missing(self):
        """get_db gets g.db from app.config['db'] when g.db doesn't exist."""
        from quart import Quart
        app = Quart(__name__)
        mock_db = MagicMock()
        app.config['db'] = mock_db

        async with app.app_context():
            result = get_db()

            assert result is mock_db

    @pytest.mark.asyncio
    async def test_get_db_sets_g_db_from_app_config(self):
        """get_db populates g.db from app.config['db']."""
        from quart import Quart, g
        app = Quart(__name__)
        mock_db = MagicMock()
        app.config['db'] = mock_db

        async with app.app_context():
            # First, g.db shouldn't exist
            assert 'db' not in g.__dict__

            result = get_db()

            # After calling get_db, g.db should be set
            assert g.db is mock_db
            assert result is mock_db

    @pytest.mark.asyncio
    async def test_get_db_returns_same_instance_on_multiple_calls(self):
        """get_db returns the same database instance on multiple calls."""
        from quart import Quart
        app = Quart(__name__)
        mock_db = MagicMock()
        app.config['db'] = mock_db

        async with app.app_context():
            result1 = get_db()
            result2 = get_db()

            assert result1 is result2
            assert result1 is mock_db


class TestConstants:
    """Tests for module-level constants."""

    def test_valid_roles_constant(self):
        """VALID_ROLES constant has expected values."""
        assert VALID_ROLES == ["admin", "maintainer", "viewer"]

    def test_cloud_provider_types_constant(self):
        """CLOUD_PROVIDER_TYPES constant has expected values."""
        expected = ["maas", "lxd", "aws", "gcp", "azure", "vultr"]
        assert CLOUD_PROVIDER_TYPES == expected

    def test_secrets_backend_types_constant(self):
        """SECRETS_BACKEND_TYPES constant has expected values."""
        expected = ["encrypted_db", "vault", "infisical", "aws", "gcp", "azure"]
        assert SECRETS_BACKEND_TYPES == expected

    def test_job_statuses_constant(self):
        """JOB_STATUSES constant has expected values."""
        expected = ["pending", "running", "completed", "failed", "cancelled"]
        assert JOB_STATUSES == expected

    def test_machine_statuses_constant(self):
        """MACHINE_STATUSES constant has expected values."""
        assert "new" in MACHINE_STATUSES
        assert "deployed" in MACHINE_STATUSES
        assert "failed" in MACHINE_STATUSES
        assert len(MACHINE_STATUSES) == 13

    def test_constants_are_lists(self):
        """All constants are lists."""
        assert isinstance(VALID_ROLES, list)
        assert isinstance(CLOUD_PROVIDER_TYPES, list)
        assert isinstance(SECRETS_BACKEND_TYPES, list)
        assert isinstance(JOB_STATUSES, list)
        assert isinstance(MACHINE_STATUSES, list)

    def test_constants_contain_strings(self):
        """All constant list items are strings."""
        for const_list in [VALID_ROLES, CLOUD_PROVIDER_TYPES,
                           SECRETS_BACKEND_TYPES, JOB_STATUSES,
                           MACHINE_STATUSES]:
            for item in const_list:
                assert isinstance(item, str)
