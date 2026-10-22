"""Unit tests for PyDAL user datastore (api-manager/app/security_datastore.py).

All tests use unittest.mock; no real database connection required.
"""
import sys

_API_MANAGER_PATH = '/home/penguin/code/gough/services/api-manager'
if _API_MANAGER_PATH not in sys.path:
    sys.path.insert(0, _API_MANAGER_PATH)
# Evict any stale 'app' module cached from another path (e.g. penguincode/app.py)
for _mod in list(sys.modules.keys()):
    if _mod == 'app' or _mod.startswith('app.'):
        del sys.modules[_mod]

import pytest
from unittest.mock import MagicMock, patch, call
from datetime import datetime
from app.security_datastore import (
    RoleMixin,
    UserMixin,
    PyDALRole,
    PyDALUser,
    PyDALUserDatastore,
)


# ---------------------------------------------------------------------------
# RoleMixin
# ---------------------------------------------------------------------------

class ConcreteRole(RoleMixin):
    """Minimal concrete RoleMixin for testing the abstract base."""
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name


class TestRoleMixin:
    """Tests for the RoleMixin base class."""

    def test_equality_two_roles_same_name(self):
        """Two roles with the same name are equal."""
        r1 = ConcreteRole('admin')
        r2 = ConcreteRole('admin')
        assert r1 == r2

    def test_inequality_different_names(self):
        """Roles with different names are not equal."""
        assert ConcreteRole('admin') != ConcreteRole('viewer')

    def test_equality_with_string(self):
        """A role equals a string with the same value."""
        role = ConcreteRole('admin')
        assert role == 'admin'
        assert role != 'viewer'

    def test_equality_with_unrelated_type(self):
        """A role does not equal an unrelated type."""
        assert ConcreteRole('admin') != 42
        assert ConcreteRole('admin') != None  # noqa: E711

    def test_hash_equals_hash_of_name(self):
        """hash(role) equals hash(role.name)."""
        role = ConcreteRole('maintainer')
        assert hash(role) == hash('maintainer')

    def test_hash_consistent_across_instances(self):
        """Two roles with the same name produce the same hash."""
        assert hash(ConcreteRole('admin')) == hash(ConcreteRole('admin'))

    def test_name_not_implemented_raises(self):
        """RoleMixin.name raises NotImplementedError when not overridden."""
        class Bare(RoleMixin):
            pass
        obj = Bare()
        with pytest.raises(NotImplementedError):
            _ = obj.name


# ---------------------------------------------------------------------------
# UserMixin
# ---------------------------------------------------------------------------

class ConcreteUser(UserMixin):
    """Minimal concrete UserMixin for testing the abstract base."""
    def __init__(self, id=None, active=True, roles=None):
        self.id = id
        self.active = active
        self.roles = roles or []


class TestUserMixin:
    """Tests for the UserMixin base class."""

    def test_is_active_true(self):
        """is_active returns True when active=True."""
        user = ConcreteUser(active=True)
        assert user.is_active is True

    def test_is_active_false(self):
        """is_active returns False when active=False."""
        user = ConcreteUser(active=False)
        assert user.is_active is False

    def test_is_authenticated_always_true(self):
        """is_authenticated is always True for concrete users."""
        assert ConcreteUser().is_authenticated is True

    def test_is_anonymous_always_false(self):
        """is_anonymous is always False for concrete users."""
        assert ConcreteUser().is_anonymous is False

    def test_get_id_returns_string(self):
        """get_id() returns the user's id as a string."""
        user = ConcreteUser(id=42)
        assert user.get_id() == '42'

    def test_get_id_when_no_id(self):
        """get_id() returns empty string when id is None."""
        user = ConcreteUser(id=None)
        assert user.get_id() == ''

    def test_has_role_by_name_found(self):
        """has_role() returns True for a role the user holds."""
        role = ConcreteRole('admin')
        user = ConcreteUser(roles=[role])
        assert user.has_role('admin') is True

    def test_has_role_by_name_not_found(self):
        """has_role() returns False for a role the user does not hold."""
        user = ConcreteUser(roles=[ConcreteRole('viewer')])
        assert user.has_role('admin') is False

    def test_has_role_by_object(self):
        """has_role() accepts a RoleMixin object as argument."""
        role = ConcreteRole('admin')
        user = ConcreteUser(roles=[role])
        assert user.has_role(role) is True

    def test_has_role_empty_roles(self):
        """has_role() returns False when the user has no roles."""
        user = ConcreteUser(roles=[])
        assert user.has_role('admin') is False


# ---------------------------------------------------------------------------
# PyDALRole
# ---------------------------------------------------------------------------

class TestPyDALRole:
    """Tests for the PyDALRole model wrapper."""

    def test_name_property_from_kwargs(self):
        """name property returns value passed as keyword argument."""
        role = PyDALRole(name='admin')
        assert role.name == 'admin'

    def test_id_property_from_kwargs(self):
        """id property returns value passed as keyword argument."""
        role = PyDALRole(id=3, name='admin')
        assert role.id == 3

    def test_description_property(self):
        """description property returns the stored description."""
        role = PyDALRole(name='admin', description='Full access')
        assert role.description == 'Full access'

    def test_permissions_property(self):
        """permissions property returns the stored permissions."""
        role = PyDALRole(name='admin', permissions='all')
        assert role.permissions == 'all'

    def test_name_setter(self):
        """name can be updated via the setter."""
        role = PyDALRole(name='viewer')
        role.name = 'editor'
        assert role.name == 'editor'

    def test_description_setter(self):
        """description can be updated via the setter."""
        role = PyDALRole(name='admin', description='old')
        role.description = 'new description'
        assert role.description == 'new description'

    def test_permissions_setter(self):
        """permissions can be updated via the setter."""
        role = PyDALRole(name='admin')
        role.permissions = 'read,write'
        assert role.permissions == 'read,write'

    def test_equality_by_name(self):
        """Two PyDALRole instances with the same name are equal."""
        r1 = PyDALRole(name='admin')
        r2 = PyDALRole(name='admin')
        assert r1 == r2

    def test_equality_with_string(self):
        """A PyDALRole instance equals a string matching its name."""
        role = PyDALRole(name='viewer')
        assert role == 'viewer'

    def test_hash_equals_hash_of_name(self):
        """hash(PyDALRole) == hash(role.name)."""
        role = PyDALRole(name='maintainer')
        assert hash(role) == hash('maintainer')

    def test_repr(self):
        """repr includes the role name."""
        role = PyDALRole(name='admin')
        assert 'admin' in repr(role)

    def test_from_row(self):
        """PyDALRole can be constructed from a DB Row-like object."""
        mock_row = MagicMock()
        mock_row.__iter__ = MagicMock(return_value=iter(
            [('id', 1), ('name', 'admin'), ('description', 'Full access'), ('permissions', None)]
        ))
        # Simulate dict(row) behaviour
        mock_row.keys = MagicMock(return_value=['id', 'name', 'description', 'permissions'])
        mock_row.__getitem__ = lambda self, key: {
            'id': 1, 'name': 'admin', 'description': 'Full access', 'permissions': None
        }[key]
        # dict(MagicMock) is tricky; use a real dict as the row instead
        row_dict = {'id': 1, 'name': 'admin', 'description': 'Full access', 'permissions': None}
        role = PyDALRole(**row_dict)
        assert role.name == 'admin'
        assert role.id == 1

    def test_defaults_when_no_data(self):
        """id is None and name is '' when no data is provided."""
        role = PyDALRole()
        assert role.id is None
        assert role.name == ''
        assert role.description == ''
        assert role.permissions is None


# ---------------------------------------------------------------------------
# PyDALUser
# ---------------------------------------------------------------------------

class TestPyDALUser:
    """Tests for the PyDALUser model wrapper."""

    def test_is_active_default_true(self):
        """active defaults to True when not set."""
        user = PyDALUser(email='test@example.com')
        assert user.is_active is True

    def test_is_active_false(self):
        """is_active reflects the active=False kwarg."""
        user = PyDALUser(email='test@example.com', active=False)
        assert user.is_active is False

    def test_is_authenticated_always_true(self):
        """is_authenticated is always True."""
        assert PyDALUser().is_authenticated is True

    def test_is_anonymous_always_false(self):
        """is_anonymous is always False."""
        assert PyDALUser().is_anonymous is False

    def test_get_id(self):
        """get_id() returns the string representation of id."""
        user = PyDALUser(id=42, email='test@example.com')
        assert user.get_id() == '42'

    def test_email_property(self):
        """email property returns the stored email."""
        user = PyDALUser(email='user@example.com')
        assert user.email == 'user@example.com'

    def test_email_setter(self):
        """email can be updated via the setter."""
        user = PyDALUser(email='old@example.com')
        user.email = 'new@example.com'
        assert user.email == 'new@example.com'

    def test_password_property(self):
        """password property returns the stored password hash."""
        user = PyDALUser(password='hashed_pw')
        assert user.password == 'hashed_pw'

    def test_password_setter(self):
        """password can be updated via the setter."""
        user = PyDALUser(password='old')
        user.password = 'new_hash'
        assert user.password == 'new_hash'

    def test_active_setter(self):
        """active can be toggled via the setter."""
        user = PyDALUser(active=True)
        user.active = False
        assert user.active is False

    def test_fs_uniquifier_property(self):
        """fs_uniquifier property returns the stored uniquifier."""
        user = PyDALUser(fs_uniquifier='my-uid-123')
        assert user.fs_uniquifier == 'my-uid-123'

    def test_fs_uniquifier_setter(self):
        """fs_uniquifier can be updated via the setter."""
        user = PyDALUser(fs_uniquifier='old-uid')
        user.fs_uniquifier = 'new-uid'
        assert user.fs_uniquifier == 'new-uid'

    def test_login_count_default_zero(self):
        """login_count defaults to 0."""
        user = PyDALUser()
        assert user.login_count == 0

    def test_login_count_setter(self):
        """login_count can be updated via the setter."""
        user = PyDALUser(login_count=5)
        user.login_count = 10
        assert user.login_count == 10

    def test_full_name_property(self):
        """full_name property returns the stored full name."""
        user = PyDALUser(full_name='Jane Doe')
        assert user.full_name == 'Jane Doe'

    def test_roles_default_empty(self):
        """roles defaults to an empty list."""
        user = PyDALUser()
        assert user.roles == []

    def test_roles_from_constructor(self):
        """roles can be supplied to the constructor."""
        role = PyDALRole(name='admin')
        user = PyDALUser(roles=[role])
        assert len(user.roles) == 1
        assert user.roles[0].name == 'admin'

    def test_has_role_by_name(self):
        """has_role() returns True for a role the user holds."""
        role = PyDALRole(name='admin')
        user = PyDALUser(roles=[role])
        assert user.has_role('admin') is True
        assert user.has_role('viewer') is False

    def test_has_role_by_object(self):
        """has_role() accepts a PyDALRole object."""
        role = PyDALRole(name='admin')
        user = PyDALUser(roles=[role])
        assert user.has_role(role) is True

    def test_get_security_payload(self):
        """get_security_payload() returns dict with id and email."""
        user = PyDALUser(id=7, email='sec@example.com')
        payload = user.get_security_payload()
        assert payload == {'id': 7, 'email': 'sec@example.com'}

    def test_repr_includes_email(self):
        """repr includes the user email."""
        user = PyDALUser(email='repr@example.com')
        assert 'repr@example.com' in repr(user)

    def test_confirmed_at_property(self):
        """confirmed_at property returns the stored datetime."""
        dt = datetime(2024, 1, 1, 12, 0, 0)
        user = PyDALUser(confirmed_at=dt)
        assert user.confirmed_at == dt

    def test_last_login_ip_property(self):
        """last_login_ip property returns the stored IP."""
        user = PyDALUser(last_login_ip='10.0.0.1')
        assert user.last_login_ip == '10.0.0.1'

    def test_tf_totp_secret_property(self):
        """tf_totp_secret property returns the stored secret."""
        user = PyDALUser(tf_totp_secret='JBSWY3DPEHPK3PXP')
        assert user.tf_totp_secret == 'JBSWY3DPEHPK3PXP'

    def test_tf_primary_method_property(self):
        """tf_primary_method property returns the stored MFA method."""
        user = PyDALUser(tf_primary_method='authenticator')
        assert user.tf_primary_method == 'authenticator'


# ---------------------------------------------------------------------------
# PyDALUserDatastore
# ---------------------------------------------------------------------------

class TestPyDALUserDatastore:
    """Tests for the PyDALUserDatastore."""

    def _make_datastore(self):
        """Create a datastore backed by a MagicMock DB."""
        mock_db = MagicMock()
        ds = PyDALUserDatastore(mock_db)
        return ds, mock_db

    # -- find_user -----------------------------------------------------------

    def test_find_user_by_id_returns_user(self):
        """find_user(id=N) returns a PyDALUser when the row exists."""
        ds, mock_db = self._make_datastore()
        mock_row = MagicMock()
        mock_row.id = 1
        mock_select_result = MagicMock()
        mock_select_result.first.return_value = mock_row
        mock_select_result.__iter__ = MagicMock(return_value=iter([]))  # _get_user_roles returns []
        mock_db.return_value.select.return_value = mock_select_result

        result = ds.find_user(id=1)
        assert result is not None
        assert isinstance(result, PyDALUser)

    def test_find_user_returns_none_when_missing(self):
        """find_user() returns None when no row is found."""
        ds, mock_db = self._make_datastore()
        mock_db.return_value.select.return_value.first.return_value = None

        result = ds.find_user(id=999)
        assert result is None

    def test_find_user_by_email(self):
        """find_user(email=...) queries by email."""
        ds, mock_db = self._make_datastore()
        mock_row = MagicMock()
        mock_row.id = 2
        mock_select_result = MagicMock()
        mock_select_result.first.return_value = mock_row
        mock_select_result.__iter__ = MagicMock(return_value=iter([]))
        mock_db.return_value.select.return_value = mock_select_result

        result = ds.find_user(email='user@example.com')
        assert result is not None

    def test_find_user_by_fs_uniquifier(self):
        """find_user(fs_uniquifier=...) queries by uniquifier."""
        ds, mock_db = self._make_datastore()
        mock_row = MagicMock()
        mock_row.id = 3
        mock_select_result = MagicMock()
        mock_select_result.first.return_value = mock_row
        mock_select_result.__iter__ = MagicMock(return_value=iter([]))
        mock_db.return_value.select.return_value = mock_select_result

        result = ds.find_user(fs_uniquifier='some-uid')
        assert result is not None

    def test_find_user_returns_none_for_empty_kwargs(self):
        """find_user() with no known kwarg returns None."""
        ds, mock_db = self._make_datastore()
        result = ds.find_user()
        assert result is None

    # -- find_role -----------------------------------------------------------

    def test_find_role_returns_role_when_found(self):
        """find_role() returns a PyDALRole when the role exists."""
        ds, mock_db = self._make_datastore()
        mock_row = MagicMock()
        mock_db.return_value.select.return_value.first.return_value = mock_row

        result = ds.find_role('admin')
        assert result is not None
        assert isinstance(result, PyDALRole)

    def test_find_role_returns_none_when_not_found(self):
        """find_role() returns None when the role doesn't exist."""
        ds, mock_db = self._make_datastore()
        mock_db.return_value.select.return_value.first.return_value = None

        result = ds.find_role('nonexistent')
        assert result is None

    # -- toggle_active -------------------------------------------------------

    def test_toggle_active_from_true_to_false(self):
        """toggle_active() sets active=False on an active user."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1, email='test@example.com', active=True)

        ds.toggle_active(user)

        assert user.active is False
        mock_db.return_value.update.assert_called_once()

    def test_toggle_active_from_false_to_true(self):
        """toggle_active() sets active=True on an inactive user."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1, email='test@example.com', active=False)

        ds.toggle_active(user)

        assert user.active is True

    def test_toggle_active_commits(self):
        """toggle_active() commits the transaction."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1, active=True)
        ds.toggle_active(user)
        mock_db.commit.assert_called()

    # -- activate_user -------------------------------------------------------

    def test_activate_user_sets_active_true(self):
        """activate_user() sets active=True."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1, active=False)

        ds.activate_user(user)

        assert user.active is True

    def test_activate_user_commits(self):
        """activate_user() commits the transaction."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1, active=False)
        ds.activate_user(user)
        mock_db.commit.assert_called()

    # -- deactivate_user -----------------------------------------------------

    def test_deactivate_user_sets_active_false(self):
        """deactivate_user() sets active=False."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1, active=True)

        ds.deactivate_user(user)

        assert user.active is False

    def test_deactivate_user_commits(self):
        """deactivate_user() commits the transaction."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1, active=True)
        ds.deactivate_user(user)
        mock_db.commit.assert_called()

    # -- set_uniquifier ------------------------------------------------------

    def test_set_uniquifier_explicit_value(self):
        """set_uniquifier() stores the provided uniquifier on the user."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1, fs_uniquifier='old-uid')

        ds.set_uniquifier(user, 'new-uid-abc')

        assert user.fs_uniquifier == 'new-uid-abc'

    def test_set_uniquifier_generates_uuid_when_none(self):
        """set_uniquifier(user) generates a UUID when no value is given."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1)

        ds.set_uniquifier(user)

        assert user.fs_uniquifier != ''
        assert len(user.fs_uniquifier) > 10  # UUID-ish

    def test_set_uniquifier_commits(self):
        """set_uniquifier() commits the transaction."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1)
        ds.set_uniquifier(user, 'some-uid')
        mock_db.commit.assert_called()

    # -- delete_user ---------------------------------------------------------

    def test_delete_user_removes_roles_and_user(self):
        """delete_user() deletes role associations then the user record."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=5, email='delete@example.com')

        ds.delete_user(user)

        # Two delete calls: user_roles then auth_user
        assert mock_db.return_value.delete.call_count == 2

    def test_delete_user_commits(self):
        """delete_user() commits after deleting."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=5)
        ds.delete_user(user)
        mock_db.commit.assert_called()

    def test_delete_user_no_op_when_no_id(self):
        """delete_user() is a no-op when user has no id."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser()  # id is None
        ds.delete_user(user)
        mock_db.return_value.delete.assert_not_called()

    # -- reset_user_access ---------------------------------------------------

    def test_reset_user_access_calls_set_uniquifier(self):
        """reset_user_access() generates a new uniquifier for the user."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1, fs_uniquifier='old-uid')

        ds.reset_user_access(user)

        # uniquifier should be replaced
        assert user.fs_uniquifier != 'old-uid'
        assert len(user.fs_uniquifier) > 0

    # -- create_user ---------------------------------------------------------

    def test_create_user_inserts_and_returns_user(self):
        """create_user() inserts into auth_user and returns a PyDALUser."""
        ds, mock_db = self._make_datastore()
        mock_db.auth_user.insert.return_value = 10
        mock_row = MagicMock()
        mock_row.id = 10
        mock_select_result = MagicMock()
        mock_select_result.first.return_value = mock_row
        mock_select_result.__iter__ = MagicMock(return_value=iter([]))
        mock_db.return_value.select.return_value = mock_select_result

        result = ds.create_user(email='new@example.com', password='hashed')

        mock_db.auth_user.insert.assert_called_once()
        assert isinstance(result, PyDALUser)

    def test_create_user_generates_fs_uniquifier(self):
        """create_user() auto-generates fs_uniquifier when not supplied."""
        ds, mock_db = self._make_datastore()
        mock_db.auth_user.insert.return_value = 11
        mock_row = MagicMock()
        mock_row.id = 11
        mock_select_result = MagicMock()
        mock_select_result.first.return_value = mock_row
        mock_select_result.__iter__ = MagicMock(return_value=iter([]))
        mock_db.return_value.select.return_value = mock_select_result

        ds.create_user(email='new2@example.com')

        call_kwargs = mock_db.auth_user.insert.call_args[1]
        assert 'fs_uniquifier' in call_kwargs
        assert len(call_kwargs['fs_uniquifier']) > 0

    # -- create_role ---------------------------------------------------------

    def test_create_role_inserts_and_returns_role(self):
        """create_role() inserts into auth_role and returns a PyDALRole."""
        ds, mock_db = self._make_datastore()
        mock_db.auth_role.insert.return_value = 3
        mock_row = MagicMock()
        mock_db.return_value.select.return_value.first.return_value = mock_row

        result = ds.create_role(name='editor', description='Can edit')

        mock_db.auth_role.insert.assert_called_once()
        assert isinstance(result, PyDALRole)

    # -- add_role_to_user / remove_role_from_user ----------------------------

    def test_add_role_to_user_returns_true(self):
        """add_role_to_user() returns True when role is added."""
        ds, mock_db = self._make_datastore()
        role = PyDALRole(id=2, name='editor')
        user = PyDALUser(id=1, email='u@example.com')
        # No existing association
        mock_db.return_value.select.return_value.first.return_value = None

        result = ds.add_role_to_user(user, role)

        assert result is True
        mock_db.auth_user_roles.insert.assert_called_once()

    def test_add_role_to_user_returns_false_when_already_has_role(self):
        """add_role_to_user() returns False when the user already has the role."""
        ds, mock_db = self._make_datastore()
        role = PyDALRole(id=2, name='editor')
        user = PyDALUser(id=1, email='u@example.com')
        # Existing association found
        mock_db.return_value.select.return_value.first.return_value = MagicMock()

        result = ds.add_role_to_user(user, role)

        assert result is False
        mock_db.auth_user_roles.insert.assert_not_called()

    def test_remove_role_from_user_returns_true(self):
        """remove_role_from_user() returns True when role is removed."""
        ds, mock_db = self._make_datastore()
        role = PyDALRole(id=2, name='editor')
        user = PyDALUser(id=1, email='u@example.com', roles=[role])
        # find_role succeeds
        mock_db.return_value.select.return_value.first.return_value = MagicMock()
        mock_db.return_value.delete.return_value = 1

        result = ds.remove_role_from_user(user, role)

        assert result is True

    def test_remove_role_from_user_returns_false_when_not_found(self):
        """remove_role_from_user() returns False when association doesn't exist."""
        ds, mock_db = self._make_datastore()
        role = PyDALRole(id=2, name='editor')
        user = PyDALUser(id=1, email='u@example.com')
        mock_db.return_value.select.return_value.first.return_value = MagicMock()
        mock_db.return_value.delete.return_value = 0

        result = ds.remove_role_from_user(user, role)

        assert result is False

    # -- put -----------------------------------------------------------------

    def test_put_updates_existing_user(self):
        """put() updates an existing user (id set)."""
        ds, mock_db = self._make_datastore()
        user = PyDALUser(id=1, email='u@example.com')

        ds.put(user)

        mock_db.return_value.update.assert_called_once()
        mock_db.commit.assert_called()

    def test_put_updates_existing_role(self):
        """put() updates an existing role (id set)."""
        ds, mock_db = self._make_datastore()
        role = PyDALRole(id=1, name='admin')

        ds.put(role)

        mock_db.return_value.update.assert_called_once()
        mock_db.commit.assert_called()


# ---------------------------------------------------------------------------
# iPXE constants (bonus coverage)
# ---------------------------------------------------------------------------

class TestIPXEConstants:
    """Test that the ipxe models module exports the expected constant lists."""

    def test_dhcp_modes(self):
        """DHCP_MODES contains full, proxy, disabled."""
        from app.models.ipxe import DHCP_MODES
        assert 'full' in DHCP_MODES
        assert 'proxy' in DHCP_MODES
        assert 'disabled' in DHCP_MODES

    def test_dhcp_modes_has_three_entries(self):
        """DHCP_MODES has exactly three entries."""
        from app.models.ipxe import DHCP_MODES
        assert len(DHCP_MODES) == 3

    def test_machine_statuses(self):
        """MACHINE_STATUSES contains the expected lifecycle states."""
        from app.models.ipxe import MACHINE_STATUSES
        for status in ('unknown', 'discovered', 'commissioning', 'ready',
                       'deploying', 'deployed', 'failed'):
            assert status in MACHINE_STATUSES

    def test_boot_modes(self):
        """BOOT_MODES contains bios, uefi, uefi_http."""
        from app.models.ipxe import BOOT_MODES
        assert 'bios' in BOOT_MODES
        assert 'uefi' in BOOT_MODES
        assert 'uefi_http' in BOOT_MODES

    def test_egg_types(self):
        """EGG_TYPES contains the four supported deployment types."""
        from app.models.ipxe import EGG_TYPES
        for t in ('snap', 'cloud_init', 'lxd_container', 'lxd_vm'):
            assert t in EGG_TYPES

    def test_architectures(self):
        """ARCHITECTURES contains amd64 and arm64."""
        from app.models.ipxe import ARCHITECTURES
        assert 'amd64' in ARCHITECTURES
        assert 'arm64' in ARCHITECTURES

    def test_architectures_has_two_entries(self):
        """ARCHITECTURES has exactly two entries."""
        from app.models.ipxe import ARCHITECTURES
        assert len(ARCHITECTURES) == 2

    def test_power_types(self):
        """POWER_TYPES contains ipmi, redfish, amt, wol, manual."""
        from app.models.ipxe import POWER_TYPES
        for pt in ('ipmi', 'redfish', 'amt', 'wol', 'manual'):
            assert pt in POWER_TYPES

    def test_image_types(self):
        """IMAGE_TYPES contains live, install, minimal."""
        from app.models.ipxe import IMAGE_TYPES
        assert 'live' in IMAGE_TYPES
        assert 'install' in IMAGE_TYPES
        assert 'minimal' in IMAGE_TYPES

    def test_deployment_statuses(self):
        """DEPLOYMENT_STATUSES contains the full deployment lifecycle."""
        from app.models.ipxe import DEPLOYMENT_STATUSES
        for s in ('pending', 'power_on', 'pxe_boot', 'os_install',
                  'egg_deploy', 'complete', 'failed'):
            assert s in DEPLOYMENT_STATUSES

    def test_boot_event_types(self):
        """BOOT_EVENT_TYPES contains all expected event names."""
        from app.models.ipxe import BOOT_EVENT_TYPES
        for et in ('dhcp_request', 'tftp_request', 'boot_start',
                   'os_installed', 'egg_started', 'egg_complete',
                   'deployment_complete', 'error'):
            assert et in BOOT_EVENT_TYPES

    def test_storage_providers(self):
        """STORAGE_PROVIDERS contains the expected provider names."""
        from app.models.ipxe import STORAGE_PROVIDERS
        for sp in ('minio', 'aws_s3', 'gcs', 'do_spaces', 'wasabi',
                   'backblaze', 'custom'):
            assert sp in STORAGE_PROVIDERS
