"""Coverage tests for small modules."""

from unittest.mock import Mock, patch
import pytest

from app.models import get_user_by_id, _get_user_role
from app.permissions import check_team_access, check_resource_permission, TEAM_ROLES
from app.rate_limit import RateLimitInfo, InMemoryStorage
from app.security_datastore import PyDALUser, PyDALRole, PyDALUserDatastore


class TestGetUserById:
    """Test get_user_by_id with various DB states."""

    def test_get_user_by_id_success(self):
        """Test successful user lookup."""
        # Mocks db(query).select().first() -- the real call shape used by
        # get_user_by_id (penguin_dal's TableProxy has no __call__, so a
        # ``db.auth_user(id)`` shortcut only appears to work against a bare
        # Mock and raises against the real DAL).
        mock_db = Mock()
        mock_row = Mock()
        mock_row.id = 123
        mock_row.email = "user@example.com"
        mock_row.full_name = "John Doe"
        mock_row.active = True
        mock_db.return_value.select.return_value.first.return_value = mock_row

        with patch("app.models.get_db", return_value=mock_db):
            with patch("app.models._get_user_role", return_value="admin"):
                result = get_user_by_id(123)
                assert result is not None
                assert result["id"] == 123
                assert result["email"] == "user@example.com"

    def test_get_user_by_id_not_found(self):
        """Test user not found returns None."""
        mock_db = Mock()
        mock_db.return_value.select.return_value.first.return_value = None

        with patch("app.models.get_db", return_value=mock_db):
            result = get_user_by_id(999)
            assert result is None

    def test_get_user_by_id_exception(self):
        """Test exception during lookup returns None."""
        mock_db = Mock()
        mock_db.return_value.select.side_effect = Exception("DB error")

        with patch("app.models.get_db", return_value=mock_db):
            result = get_user_by_id(123)
            assert result is None


class TestGetUserRole:
    """Test _get_user_role helper."""

    def test_get_user_role_exception(self):
        """Test exception during role lookup defaults to viewer."""
        mock_db = Mock()
        mock_query = Mock()
        mock_query.select.side_effect = Exception("DB error")
        mock_db.return_value = mock_query

        result = _get_user_role(mock_db, 123)
        assert result == "viewer"


class TestCheckTeamAccess:
    """Test team access checks."""

    def test_check_team_access_no_membership(self):
        """Test user with no team membership denied."""
        mock_db = Mock()
        mock_query = Mock()
        mock_query.select.return_value.first.return_value = None
        mock_db.return_value = mock_query

        with patch("app.permissions.get_db", return_value=mock_db):
            result = check_team_access(1, 10, "member")
            assert result is False

    def test_check_team_access_invalid_role(self):
        """Test with invalid role name."""
        mock_db = Mock()
        mock_membership = Mock()
        mock_membership.role = "invalid_role"
        mock_query = Mock()
        mock_query.select.return_value.first.return_value = mock_membership
        mock_db.return_value = mock_query

        with patch("app.permissions.get_db", return_value=mock_db):
            # Invalid role causes ValueError, which should be caught
            try:
                result = check_team_access(1, 10, "member")
                # If it returns False, that's correct
                assert result is False
            except ValueError:
                # If ValueError is raised, that's also valid behavior
                pass


class TestCheckResourcePermission:
    """Test resource permission checks."""

    def test_check_resource_permission_not_found(self):
        """Test resource not found returns False."""
        mock_db = Mock()
        mock_table = Mock()
        mock_query = Mock()
        mock_query.select.return_value.first.return_value = None
        mock_table.return_value = mock_query
        mock_db.__getitem__ = Mock(return_value=mock_table)
        mock_db.return_value = mock_query

        with patch("app.permissions.get_db", return_value=mock_db):
            result = check_resource_permission(100, "lxd_cluster", 5, "read")
            assert result is False

    def test_check_resource_permission_exception(self):
        """Test exception handling."""
        mock_db = Mock()
        mock_db.__getitem__ = Mock(side_effect=KeyError("Invalid table"))

        with patch("app.permissions.get_db", return_value=mock_db):
            result = check_resource_permission(100, "invalid_table", 5, "read")
            assert result is False


class TestRateLimitInfo:
    """Test RateLimitInfo data structure."""

    def test_rate_limit_info_creation(self):
        """Test RateLimitInfo creation."""
        from datetime import datetime, timedelta
        reset = datetime.utcnow() + timedelta(seconds=60)
        info = RateLimitInfo(limit=10, remaining=5, reset_at=reset)
        assert info.limit == 10
        assert info.remaining == 5


class TestInMemoryStorage:
    """Test in-memory rate limit storage."""

    def test_in_memory_storage_get_missing_key(self):
        """Test get missing key returns None."""
        storage = InMemoryStorage()
        result = storage.get("missing_key")
        assert result is None

    def test_in_memory_storage_set_and_get(self):
        """Test set and retrieve."""
        storage = InMemoryStorage()
        storage.set("key1", {"count": 1}, 60)
        result = storage.get("key1")
        assert result is not None
        assert "expires_at" in result

    def test_in_memory_storage_multiple_keys(self):
        """Test multiple keys."""
        storage = InMemoryStorage()
        storage.set("key1", {"count": 1}, 60)
        storage.set("key2", {"count": 2}, 60)
        result1 = storage.get("key1")
        result2 = storage.get("key2")
        assert result1 is not None
        assert result2 is not None

    def test_in_memory_storage_incr(self):
        """Test increment."""
        storage = InMemoryStorage()
        storage.set("counter", {"count": 5}, 60)
        storage.incr("counter", 3)
        result = storage.get("counter")
        assert result is not None
        assert "count" in result


class TestPyDALUser:
    """Test PyDALUser model."""

    def test_pydal_user_creation(self):
        """Test creating PyDALUser."""
        user = PyDALUser(id=1, email="test@example.com", active=True, roles=[])
        assert user.id == 1
        assert user.email == "test@example.com"
        assert user.active is True


class TestPyDALRole:
    """Test PyDALRole model."""

    def test_pydal_role_creation(self):
        """Test creating PyDALRole."""
        role = PyDALRole(id=1, name="admin", description="Admin role")
        assert role.id == 1
        assert role.name == "admin"


class TestPyDALUserDatastore:
    """Test PyDALUserDatastore."""

    def test_datastore_creation(self):
        """Test creating PyDALUserDatastore."""
        mock_db = Mock()
        datastore = PyDALUserDatastore(mock_db)
        assert datastore is not None


class TestEdgeCases:
    """Test edge cases across modules."""

    def test_permissions_with_none_db(self):
        """Test permissions functions handle None DB gracefully."""
        with patch("app.permissions.get_db", return_value=None):
            result = check_team_access(1, 10, "member")
            assert result is False

    def test_models_with_exception(self):
        """Test models functions handle exceptions."""
        mock_db = Mock()
        mock_db.return_value.select.side_effect = RuntimeError("Connection lost")

        with patch("app.models.get_db", return_value=mock_db):
            result = get_user_by_id(123)
            assert result is None
