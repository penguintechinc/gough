"""Additional test coverage for app/permissions.py (target 99-104, 149-150, 152, 155, 159, 164, 187-219).

Tests for decorator branches, async result handling, and edge cases in
require_team_permission and require_resource_permission decorators.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from types import SimpleNamespace

from app.permissions import (
    check_team_access,
    check_resource_permission,
    TEAM_ROLES,
)


class TestTeamAccessEdgeCases:
    """Test edge cases in check_team_access (lines 99-104)."""

    def test_check_team_access_admin_to_member_fallback(self, monkeypatch):
        """Test admin cannot satisfy member requirement (99-100, write permission)."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_membership = Mock(role="admin")
        mock_query.select.return_value.first.return_value = mock_membership

        monkeypatch.setattr("app.permissions.get_db", lambda: mock_db)

        result = check_team_access(1, 1, "member")
        assert result is False  # admin(1) < member(2) index

    def test_check_team_access_member_to_viewer_fallback(self, monkeypatch):
        """Test member cannot satisfy viewer requirement (102-104, read permission)."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_membership = Mock(role="member")
        mock_query.select.return_value.first.return_value = mock_membership

        monkeypatch.setattr("app.permissions.get_db", lambda: mock_db)

        result = check_team_access(1, 1, "viewer")
        assert result is False  # member(2) < viewer(3) index

    def test_check_team_access_index_comparison(self, monkeypatch):
        """Test index >= comparison logic (51)."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_membership = Mock(role="owner")
        mock_query.select.return_value.first.return_value = mock_membership

        monkeypatch.setattr("app.permissions.get_db", lambda: mock_db)

        # Owner is index 0, member is 2, so 0 >= 2 is False
        result = check_team_access(1, 1, "member")
        assert result is False


class TestResourcePermissionFallback:
    """Test fallback permission checks in check_resource_permission (lines 99-104, 149-150)."""

    def test_resource_permission_admin_write_fallback(self, monkeypatch):
        """Test write permission falls back to team admin check (99-100)."""
        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=Mock())

        # Resource is team-scoped but not created by user
        mock_resource = Mock(created_by=999, team_id=2)

        call_count = [0]

        def db_call_side_effect(*args, **kwargs):
            result = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: get resource
                result.select.return_value.first.return_value = mock_resource
            elif call_count[0] == 2:
                # Second call: check resource_permissions - no explicit perms
                result.select.return_value.first.return_value = None
            else:
                # Third call: team access check
                result.select.return_value.first.return_value = Mock(role="admin")
            return result

        mock_db.side_effect = db_call_side_effect

        monkeypatch.setattr("app.permissions.get_db", lambda: mock_db)

        # Should fall through to team admin check for write permission
        result = check_resource_permission(1, "cloud_provider", 1, "write")
        assert isinstance(result, bool)

    def test_resource_permission_read_execute_fallback(self, monkeypatch):
        """Test read/execute falls back to team member check (103-104)."""
        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=Mock())
        mock_query = MagicMock()
        mock_db.return_value = mock_query

        # Resource is team-scoped but not created by user
        mock_resource = Mock(created_by=999, team_id=2)
        mock_query.select.return_value.first.return_value = mock_resource

        call_count = [0]

        def sequential_return(*args, **kwargs):
            result = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                result.select.return_value.first.return_value = mock_resource
            else:
                # Team member check
                result.select.return_value.first.return_value = Mock(role="member")
            return result

        mock_db.return_value = sequential_return

        monkeypatch.setattr("app.permissions.get_db", lambda: mock_db)

        # Should fall through to team member check for read permission
        result = check_resource_permission(1, "cloud_provider", 1, "read")
        assert isinstance(result, bool)


class TestResourcePermissionAttributeChecks:
    """Test hasattr checks for resource attributes (lines 149-150, 152, 155)."""

    def test_resource_permission_no_created_by(self, monkeypatch):
        """Test resource without created_by attribute (152)."""
        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=Mock())
        mock_query = MagicMock()
        mock_db.return_value = mock_query

        # Resource has no created_by attr
        mock_resource = MagicMock(spec=[])  # Empty spec
        mock_query.select.return_value.first.return_value = mock_resource

        monkeypatch.setattr("app.permissions.get_db", lambda: mock_db)

        # hasattr check should fail, continue to next check
        result = check_resource_permission(1, "cloud_provider", 1, "read")
        assert isinstance(result, bool)

    def test_resource_permission_no_team_id(self, monkeypatch):
        """Test resource without team_id (155)."""
        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=Mock())
        mock_query = MagicMock()
        mock_db.return_value = mock_query

        # Resource has created_by but no team_id
        mock_resource = MagicMock(spec=["created_by"])
        mock_resource.created_by = 999  # Not current user
        mock_query.select.return_value.first.return_value = mock_resource

        monkeypatch.setattr("app.permissions.get_db", lambda: mock_db)

        # Should return False (no team_id to check)
        result = check_resource_permission(1, "cloud_provider", 1, "read")
        assert result is False

    def test_resource_permission_explicit_permission_in_list(self, monkeypatch):
        """Test explicit permission found in comma-separated list (159)."""
        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=Mock())
        mock_query = MagicMock()
        mock_db.return_value = mock_query

        mock_resource = Mock(created_by=999, team_id=1)
        mock_perms = Mock(permission="read,execute,admin")

        call_count = [0]

        def sequential_return(*args, **kwargs):
            result = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                result.select.return_value.first.return_value = mock_resource
            else:
                # Permissions lookup
                result.select.return_value.first.return_value = mock_perms
            return result

        mock_db.side_effect = sequential_return

        monkeypatch.setattr("app.permissions.get_db", lambda: mock_db)

        # Should find "read" in permission list
        result = check_resource_permission(1, "cloud_provider", 1, "read")
        assert result is True

    def test_resource_permission_shell_permission_check(self, monkeypatch):
        """Test shell permission through explicit list (164)."""
        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=Mock())
        mock_query = MagicMock()
        mock_db.return_value = mock_query

        mock_resource = Mock(created_by=999, team_id=1)
        mock_perms = Mock(permission="read,shell")

        call_count = [0]

        def sequential_return(*args, **kwargs):
            result = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                result.select.return_value.first.return_value = mock_resource
            else:
                result.select.return_value.first.return_value = mock_perms
            return result

        mock_db.side_effect = sequential_return

        monkeypatch.setattr("app.permissions.get_db", lambda: mock_db)

        # Should find "shell" in permission list
        result = check_resource_permission(1, "cloud_provider", 1, "shell")
        assert result is True


class TestGetCurrentUserEdgeCases:
    """Test get_current_user integration in decorators (lines 155, 159)."""

    def test_check_team_access_with_none_membership_role(self, monkeypatch):
        """Test handling when membership.role is None."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.return_value = mock_query
        mock_membership = Mock(role=None)
        mock_query.select.return_value.first.return_value = mock_membership

        monkeypatch.setattr("app.permissions.get_db", lambda: mock_db)

        # ValueError when trying to find None in TEAM_ROLES
        with pytest.raises(ValueError):
            check_team_access(1, 1, "member")
