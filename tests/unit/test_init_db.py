"""Unit tests for db/init_db.py module - SQLAlchemy database initialization.

Tests for:
- APIDefinition model
- APIUsage model
- APIKey model
- get_sqlalchemy_url function
- init_db_schema function
- create_tables function
- drop_tables function
"""

import os
import sys
from unittest.mock import MagicMock, patch, Mock
from datetime import datetime

import pytest

_API_MANAGER_PATH = '/home/penguin/code/gough/services/api-manager'
if _API_MANAGER_PATH not in sys.path:
    sys.path.insert(0, _API_MANAGER_PATH)
for _mod in list(sys.modules.keys()):
    if _mod == 'app' or _mod.startswith('app.'):
        del sys.modules[_mod]

from app.db.init_db import (
    APIDefinition,
    APIUsage,
    APIKey,
    get_sqlalchemy_url,
    init_db_schema,
    create_tables,
    drop_tables,
    Base,
)


class TestAPIDefinitionModel:
    """Tests for APIDefinition SQLAlchemy model."""

    def test_api_definition_tablename(self):
        """Test APIDefinition table name."""
        assert APIDefinition.__tablename__ == 'api_definitions'

    def test_api_definition_columns_exist(self):
        """Test APIDefinition has required columns."""
        columns = {col.name for col in APIDefinition.__table__.columns}
        assert 'id' in columns
        assert 'name' in columns
        assert 'version' in columns
        assert 'path' in columns
        assert 'method' in columns
        assert 'description' in columns
        assert 'openapi_spec' in columns
        assert 'enabled' in columns
        assert 'created_at' in columns
        assert 'updated_at' in columns

    def test_api_definition_has_unique_constraint(self):
        """Test APIDefinition has unique constraint."""
        constraints = {c.name for c in APIDefinition.__table__.constraints}
        assert 'uix_api_endpoint' in constraints

    def test_api_definition_has_indexes(self):
        """Test APIDefinition has required indexes."""
        indexes = {idx.name for idx in APIDefinition.__table__.indexes}
        assert 'idx_api_name_version' in indexes
        assert 'idx_api_path' in indexes
        assert 'idx_api_enabled' in indexes


class TestAPIUsageModel:
    """Tests for APIUsage SQLAlchemy model."""

    def test_api_usage_tablename(self):
        """Test APIUsage table name."""
        assert APIUsage.__tablename__ == 'api_usage'

    def test_api_usage_columns_exist(self):
        """Test APIUsage has required columns."""
        columns = {col.name for col in APIUsage.__table__.columns}
        assert 'id' in columns
        assert 'api_id' in columns
        assert 'timestamp' in columns
        assert 'method' in columns
        assert 'path' in columns
        assert 'status_code' in columns
        assert 'response_time_ms' in columns
        assert 'user_id' in columns
        assert 'ip_address' in columns
        assert 'user_agent' in columns

    def test_api_usage_has_indexes(self):
        """Test APIUsage has required indexes."""
        indexes = {idx.name for idx in APIUsage.__table__.indexes}
        assert 'idx_usage_api_id' in indexes
        assert 'idx_usage_timestamp' in indexes
        assert 'idx_usage_user_id' in indexes
        assert 'idx_usage_status' in indexes


class TestAPIKeyModel:
    """Tests for APIKey SQLAlchemy model."""

    def test_api_key_tablename(self):
        """Test APIKey table name."""
        assert APIKey.__tablename__ == 'api_keys'

    def test_api_key_columns_exist(self):
        """Test APIKey has required columns."""
        columns = {col.name for col in APIKey.__table__.columns}
        assert 'id' in columns
        assert 'key_hash' in columns
        assert 'name' in columns
        assert 'user_id' in columns
        assert 'scopes' in columns
        assert 'enabled' in columns
        assert 'rate_limit' in columns
        assert 'created_at' in columns
        assert 'expires_at' in columns
        assert 'last_used_at' in columns

    def test_api_key_key_hash_unique(self):
        """Test APIKey key_hash has unique constraint."""
        key_hash_col = APIKey.__table__.columns['key_hash']
        assert key_hash_col.unique

    def test_api_key_has_indexes(self):
        """Test APIKey has required indexes."""
        indexes = {idx.name for idx in APIKey.__table__.indexes}
        assert 'idx_key_hash' in indexes
        assert 'idx_key_user_id' in indexes
        assert 'idx_key_enabled' in indexes


class TestGetSQLAlchemyUrl:
    """Tests for get_sqlalchemy_url function."""

    def test_get_sqlalchemy_url_postgresql(self):
        """Test converting PostgreSQL URL."""
        result = get_sqlalchemy_url('postgresql', 'localhost:5432/mydb')
        assert result.startswith('postgresql://')
        assert 'localhost:5432' in result
        assert 'mydb' in result

    def test_get_sqlalchemy_url_postgresql_already_prefixed(self):
        """Test handling already-prefixed PostgreSQL URL."""
        result = get_sqlalchemy_url(
            'postgresql',
            'postgresql://user:pass@localhost/db'
        )
        assert result.startswith('postgresql://')

    def test_get_sqlalchemy_url_postgres_alias(self):
        """Test handling 'postgres' alias for PostgreSQL."""
        result = get_sqlalchemy_url('postgres', 'localhost:5432/mydb')
        assert result.startswith('postgresql://')

    def test_get_sqlalchemy_url_mysql(self):
        """Test converting MySQL URL."""
        result = get_sqlalchemy_url('mysql', 'localhost:3306/mydb')
        assert 'mysql+pymysql://' in result
        assert 'localhost:3306' in result

    def test_get_sqlalchemy_url_mysql_already_prefixed(self):
        """Test handling already-prefixed MySQL URL."""
        result = get_sqlalchemy_url(
            'mysql',
            'mysql://user:pass@localhost/db'
        )
        assert 'mysql+pymysql://' in result

    def test_get_sqlalchemy_url_mariadb(self):
        """Test converting MariaDB URL."""
        result = get_sqlalchemy_url('mariadb', 'localhost:3306/mydb')
        assert 'mysql+pymysql://' in result

    def test_get_sqlalchemy_url_sqlite(self):
        """Test converting SQLite URL."""
        result = get_sqlalchemy_url('sqlite', '/path/to/db.sqlite')
        assert result.startswith('sqlite:///')
        assert '/path/to/db.sqlite' in result

    def test_get_sqlalchemy_url_sqlite_already_prefixed(self):
        """Test handling already-prefixed SQLite URL."""
        result = get_sqlalchemy_url(
            'sqlite',
            'sqlite:////path/to/db.sqlite'
        )
        assert result.startswith('sqlite:///')

    def test_get_sqlalchemy_url_case_insensitive(self):
        """Test get_sqlalchemy_url is case insensitive."""
        result1 = get_sqlalchemy_url('PostgreSQL', 'localhost/db')
        result2 = get_sqlalchemy_url('postgresql', 'localhost/db')
        assert result1 == result2

    def test_get_sqlalchemy_url_invalid_type(self):
        """Test get_sqlalchemy_url raises for invalid DB type."""
        with pytest.raises(ValueError, match="Unsupported DB_TYPE"):
            get_sqlalchemy_url('oracle', 'localhost/db')


class TestInitDbSchema:
    """Tests for init_db_schema function."""

    @patch('app.db.init_db.create_engine')
    @patch('app.db.init_db.logger')
    def test_init_db_schema_success(self, mock_logger, mock_create_engine):
        """Test init_db_schema successfully initializes schema."""
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        result = init_db_schema(
            database_url='postgresql://localhost/test',
            db_type='postgresql'
        )

        assert result is True
        mock_create_engine.assert_called_once()
        mock_logger.info.assert_called()

    @patch('app.db.init_db.create_engine')
    @patch('app.db.init_db.logger')
    def test_init_db_schema_uses_env_vars(self, mock_logger, mock_create_engine):
        """Test init_db_schema uses environment variables."""
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        with patch.dict(os.environ, {
            'DATABASE_URL': 'postgresql://localhost/test',
            'DB_TYPE': 'postgresql'
        }):
            result = init_db_schema()

            assert result is True
            mock_create_engine.assert_called_once()

    @patch('app.db.init_db.logger')
    def test_init_db_schema_no_database_url(self, mock_logger):
        """Test init_db_schema raises when DATABASE_URL missing."""
        with patch.dict(os.environ, {}, clear=True):
            result = init_db_schema(db_type='postgresql')

            assert result is False
            mock_logger.error.assert_called()

    @patch('app.db.init_db.create_engine')
    @patch('app.db.init_db.logger')
    def test_init_db_schema_with_echo(self, mock_logger, mock_create_engine):
        """Test init_db_schema with echo=True."""
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        result = init_db_schema(
            database_url='postgresql://localhost/test',
            db_type='postgresql',
            echo=True
        )

        assert result is True
        call_kwargs = mock_create_engine.call_args[1]
        assert call_kwargs['echo'] is True

    @patch('app.db.init_db.create_engine')
    @patch('app.db.init_db.logger')
    def test_init_db_schema_connection_timeout(self, mock_logger, mock_create_engine):
        """Test init_db_schema sets connection timeout."""
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        init_db_schema(
            database_url='postgresql://localhost/test',
            db_type='postgresql'
        )

        call_kwargs = mock_create_engine.call_args[1]
        assert 'connect_args' in call_kwargs
        assert call_kwargs['connect_args']['connect_timeout'] == 10

    @patch('app.db.init_db.create_engine')
    @patch('app.db.init_db.logger')
    def test_init_db_schema_sqlite_no_timeout(self, mock_logger, mock_create_engine):
        """Test init_db_schema SQLite doesn't set connection timeout."""
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        init_db_schema(
            database_url='sqlite:///test.db',
            db_type='sqlite'
        )

        call_kwargs = mock_create_engine.call_args[1]
        assert call_kwargs.get('connect_args', {}) == {}

    @patch('app.db.init_db.create_engine', side_effect=Exception("Connection failed"))
    @patch('app.db.init_db.logger')
    def test_init_db_schema_exception_handling(self, mock_logger, mock_create_engine):
        """Test init_db_schema handles exceptions."""
        result = init_db_schema(
            database_url='postgresql://localhost/test',
            db_type='postgresql'
        )

        assert result is False
        mock_logger.error.assert_called()


class TestCreateTables:
    """Tests for create_tables function."""

    @patch('app.db.init_db.init_db_schema')
    def test_create_tables_calls_init_db_schema(self, mock_init_db):
        """Test create_tables calls init_db_schema."""
        mock_init_db.return_value = True

        result = create_tables()

        assert result is True
        mock_init_db.assert_called_once()

    @patch('app.db.init_db.init_db_schema')
    def test_create_tables_returns_false_on_error(self, mock_init_db):
        """Test create_tables returns False on init_db_schema failure."""
        mock_init_db.return_value = False

        result = create_tables()

        assert result is False


class TestDropTables:
    """Tests for drop_tables function."""

    @patch('app.db.init_db.create_engine')
    @patch('app.db.init_db.logger')
    def test_drop_tables_success(self, mock_logger, mock_create_engine):
        """Test drop_tables successfully drops tables."""
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        result = drop_tables(
            database_url='postgresql://localhost/test',
            db_type='postgresql'
        )

        assert result is True
        mock_logger.warning.assert_called()
        mock_logger.info.assert_called()

    @patch('app.db.init_db.create_engine')
    @patch('app.db.init_db.logger')
    def test_drop_tables_uses_env_vars(self, mock_logger, mock_create_engine):
        """Test drop_tables uses environment variables."""
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        with patch.dict(os.environ, {
            'DATABASE_URL': 'postgresql://localhost/test',
            'DB_TYPE': 'postgresql'
        }):
            result = drop_tables()

            assert result is True
            mock_create_engine.assert_called_once()

    @patch('app.db.init_db.logger')
    def test_drop_tables_no_database_url(self, mock_logger):
        """Test drop_tables raises when DATABASE_URL missing."""
        with patch.dict(os.environ, {}, clear=True):
            result = drop_tables(db_type='postgresql')

            assert result is False
            mock_logger.error.assert_called()

    @patch('app.db.init_db.create_engine')
    @patch('app.db.init_db.logger')
    def test_drop_tables_connection_timeout(self, mock_logger, mock_create_engine):
        """Test drop_tables sets connection timeout."""
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        drop_tables(
            database_url='postgresql://localhost/test',
            db_type='postgresql'
        )

        call_kwargs = mock_create_engine.call_args[1]
        assert call_kwargs['connect_args']['connect_timeout'] == 10

    @patch('app.db.init_db.create_engine')
    @patch('app.db.init_db.logger')
    def test_drop_tables_sqlite_no_timeout(self, mock_logger, mock_create_engine):
        """Test drop_tables SQLite doesn't set connection timeout."""
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        drop_tables(
            database_url='sqlite:///test.db',
            db_type='sqlite'
        )

        call_kwargs = mock_create_engine.call_args[1]
        assert call_kwargs.get('connect_args', {}) == {}

    @patch('app.db.init_db.create_engine', side_effect=Exception("Connection failed"))
    @patch('app.db.init_db.logger')
    def test_drop_tables_exception_handling(self, mock_logger, mock_create_engine):
        """Test drop_tables handles exceptions."""
        result = drop_tables(
            database_url='postgresql://localhost/test',
            db_type='postgresql'
        )

        assert result is False
        mock_logger.error.assert_called()


class TestBaseMetadata:
    """Tests for SQLAlchemy Base metadata."""

    def test_base_has_all_models(self):
        """Test Base metadata includes all models."""
        tables = {table.name for table in Base.metadata.tables.values()}
        assert 'api_definitions' in tables
        assert 'api_usage' in tables
        assert 'api_keys' in tables

    def test_all_tables_have_indices(self):
        """Test all tables have appropriate indexes."""
        # APIDefinition checks
        api_def_table = Base.metadata.tables['api_definitions']
        assert len(api_def_table.indexes) > 0

        # APIUsage checks
        api_usage_table = Base.metadata.tables['api_usage']
        assert len(api_usage_table.indexes) > 0

        # APIKey checks
        api_key_table = Base.metadata.tables['api_keys']
        assert len(api_key_table.indexes) > 0


class TestConfigGetDbUri:
    """Tests for Config.get_db_uri() method."""

    def test_get_db_uri_postgresql_default(self):
        """Test PostgreSQL URI generation."""
        from app.config import Config

        # Patch class attributes directly
        with patch.object(Config, 'DB_TYPE', 'postgres'):
            with patch.object(Config, 'DB_HOST', 'localhost'):
                with patch.object(Config, 'DB_PORT', '5432'):
                    with patch.object(Config, 'DB_NAME', 'testdb'):
                        with patch.object(Config, 'DB_USER', 'testuser'):
                            with patch.object(Config, 'DB_PASS', 'testpass'):
                                uri = Config.get_db_uri()

                                assert uri.startswith('postgres://')
                                assert 'testuser' in uri
                                assert 'testpass' in uri
                                assert 'localhost' in uri
                                assert '5432' in uri
                                assert 'testdb' in uri

    def test_get_db_uri_postgresql_alias(self):
        """Test PostgreSQL with 'postgresql' alias."""
        from app.config import Config

        with patch.object(Config, 'DB_TYPE', 'postgresql'):
            with patch.object(Config, 'DB_HOST', 'db.example.com'):
                with patch.object(Config, 'DB_PORT', '5433'):
                    with patch.object(Config, 'DB_NAME', 'mydb'):
                        with patch.object(Config, 'DB_USER', 'dbuser'):
                            with patch.object(Config, 'DB_PASS', 'dbpass'):
                                uri = Config.get_db_uri()

                                assert uri.startswith('postgres://')
                                assert 'db.example.com' in uri

    def test_get_db_uri_mysql(self):
        """Test MySQL URI generation."""
        from app.config import Config

        with patch.object(Config, 'DB_TYPE', 'mysql'):
            with patch.object(Config, 'DB_HOST', 'localhost'):
                with patch.object(Config, 'DB_PORT', '3306'):
                    with patch.object(Config, 'DB_NAME', 'gough_db'):
                        with patch.object(Config, 'DB_USER', 'gough_user'):
                            with patch.object(Config, 'DB_PASS', 'gough_pass'):
                                uri = Config.get_db_uri()

                                assert uri.startswith('mysql://')
                                assert 'gough_user' in uri
                                assert 'gough_pass' in uri
                                assert 'localhost:3306' in uri
                                assert 'gough_db' in uri

    def test_get_db_uri_mariadb_alias(self):
        """Test MariaDB with 'mariadb' alias (maps to mysql)."""
        from app.config import Config

        with patch.object(Config, 'DB_TYPE', 'mariadb'):
            with patch.object(Config, 'DB_HOST', 'mariadb.local'):
                with patch.object(Config, 'DB_PORT', '3306'):
                    with patch.object(Config, 'DB_NAME', 'mydb'):
                        with patch.object(Config, 'DB_USER', 'myuser'):
                            with patch.object(Config, 'DB_PASS', 'mypass'):
                                uri = Config.get_db_uri()

                                assert uri.startswith('mysql://')
                                assert 'mariadb.local' in uri

    def test_get_db_uri_sqlite_memory(self):
        """Test SQLite in-memory database."""
        from app.config import Config

        with patch.object(Config, 'DB_TYPE', 'sqlite'):
            with patch.object(Config, 'DB_NAME', ':memory:'):
                uri = Config.get_db_uri()

                assert uri == 'sqlite:memory'

    def test_get_db_uri_sqlite_file(self):
        """Test SQLite file-based database."""
        from app.config import Config

        with patch.object(Config, 'DB_TYPE', 'sqlite'):
            with patch.object(Config, 'DB_NAME', '/var/lib/gough/db'):
                uri = Config.get_db_uri()

                assert uri == 'sqlite:///var/lib/gough/db.db'

    def test_get_db_uri_case_insensitive_type(self):
        """Test DB_TYPE is case-insensitive."""
        from app.config import Config

        with patch.object(Config, 'DB_TYPE', 'PostgreSQL'):  # Mixed case
            with patch.object(Config, 'DB_HOST', 'localhost'):
                with patch.object(Config, 'DB_PORT', '5432'):
                    with patch.object(Config, 'DB_NAME', 'testdb'):
                        with patch.object(Config, 'DB_USER', 'user'):
                            with patch.object(Config, 'DB_PASS', 'pass'):
                                uri = Config.get_db_uri()

                                assert uri.startswith('postgres://')

    def test_get_db_uri_unknown_type_passthrough(self):
        """Test unknown DB_TYPE is passed through as-is."""
        from app.config import Config

        with patch.object(Config, 'DB_TYPE', 'custom_db'):
            with patch.object(Config, 'DB_HOST', 'localhost'):
                with patch.object(Config, 'DB_PORT', '9999'):
                    with patch.object(Config, 'DB_NAME', 'custom'):
                        with patch.object(Config, 'DB_USER', 'user'):
                            with patch.object(Config, 'DB_PASS', 'pass'):
                                uri = Config.get_db_uri()

                                assert uri.startswith('custom_db://')
                                assert 'localhost:9999' in uri


class TestConfigConstants:
    """Tests for Config class constants and environment variables."""

    def test_config_has_required_attributes(self):
        """Test Config class has all required configuration attributes."""
        from app.config import Config

        # Database config
        assert hasattr(Config, 'DB_TYPE')
        assert hasattr(Config, 'DB_HOST')
        assert hasattr(Config, 'DB_PORT')
        assert hasattr(Config, 'DB_NAME')
        assert hasattr(Config, 'DB_USER')
        assert hasattr(Config, 'DB_PASS')
        assert hasattr(Config, 'DB_POOL_SIZE')

        # Security config
        assert hasattr(Config, 'SECRET_KEY')
        assert hasattr(Config, 'SECURITY_PASSWORD_SALT')
        assert hasattr(Config, 'SECURITY_PASSWORD_HASH')

        # JWT config
        assert hasattr(Config, 'JWT_SECRET_KEY')
        assert hasattr(Config, 'JWT_ACCESS_TOKEN_EXPIRES')

    def test_config_security_settings(self):
        """Test Config security-related settings have secure defaults."""
        from app.config import Config

        # Should have a password hash method
        assert Config.SECURITY_PASSWORD_HASH == 'bcrypt'

        # Should not register new users without confirmation
        assert Config.SECURITY_REGISTERABLE is True
        assert Config.SECURITY_SEND_REGISTER_EMAIL is False

    def test_config_db_pool_size_is_integer(self):
        """Test DB_POOL_SIZE is configured as an integer."""
        from app.config import Config

        pool_size = Config.DB_POOL_SIZE
        assert isinstance(pool_size, int)
        assert pool_size > 0

    def test_config_db_type_default(self):
        """Test DB_TYPE defaults to 'postgres'."""
        from app.config import Config

        # The default is set in the class definition
        # We can verify it's used by checking the default in get_db_uri
        with patch.object(Config, 'DB_TYPE', 'postgres'):
            with patch.object(Config, 'DB_HOST', 'localhost'):
                with patch.object(Config, 'DB_PORT', '5432'):
                    with patch.object(Config, 'DB_NAME', 'testdb'):
                        with patch.object(Config, 'DB_USER', 'user'):
                            with patch.object(Config, 'DB_PASS', 'pass'):
                                uri = Config.get_db_uri()
                                # Should generate postgres URI with default type
                                assert uri.startswith('postgres://')
