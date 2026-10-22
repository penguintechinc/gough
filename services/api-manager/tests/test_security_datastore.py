"""Tests for app/security_datastore.py PyDAL user datastore."""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — build mock DB objects
# ---------------------------------------------------------------------------

def _row(**kwargs) -> MagicMock:
    """Simulate a penguin-dal Row-like object."""
    row = MagicMock()
    row.__iter__ = MagicMock(return_value=iter(kwargs.items()))
    for k, v in kwargs.items():
        setattr(row, k, v)
    # Make dict() work on the row
    row.items = MagicMock(return_value=kwargs.items())
    row.keys = MagicMock(return_value=kwargs.keys())
    # Support dict(row) via __getitem__
    row.__getitem__ = lambda self, k: kwargs[k]
    return row


def _make_user_row(
    id: int = 1,
    email: str = "test@example.com",
    active: bool = True,
    **kwargs,
) -> MagicMock:
    return _row(id=id, email=email, active=active, **kwargs)


def _make_role_row(id: int = 10, name: str = "admin") -> MagicMock:
    return _row(id=id, name=name, description="Admin role", permissions=None)


def _make_db(
    *,
    user_row=None,
    role_row=None,
    user_roles_rows=None,
) -> MagicMock:
    """Build a minimal fake penguin-dal DB object."""
    db = MagicMock()

    # auth_user table
    user_query = MagicMock()
    user_query.select.return_value.first.return_value = user_row
    db.auth_user = MagicMock()
    db.auth_user.__eq__ = MagicMock(return_value=user_query)
    db.return_value = user_query  # db(query) → user_query

    # auth_role table
    role_query = MagicMock()
    role_query.select.return_value.first.return_value = role_row
    db.auth_role = MagicMock()

    # auth_user_roles table
    roles_query = MagicMock()
    roles_rows = user_roles_rows or []
    roles_query.select.return_value = iter(roles_rows)
    db.auth_user_roles = MagicMock()

    # When db(query).select().first() is called, route based on context
    # We make __call__ return a flexible query mock
    def _db_call(query):
        return query

    db.side_effect = _db_call
    db.commit = MagicMock()
    return db


# ---------------------------------------------------------------------------
# RoleMixin
# ---------------------------------------------------------------------------

class TestRoleMixin:
    def test_eq_with_same_name_role(self):
        from app.security_datastore import PyDALRole
        r1 = PyDALRole(name="admin")
        r2 = PyDALRole(name="admin")
        assert r1 == r2

    def test_eq_with_string(self):
        from app.security_datastore import PyDALRole
        r = PyDALRole(name="viewer")
        assert r == "viewer"

    def test_neq_different_name(self):
        from app.security_datastore import PyDALRole
        r = PyDALRole(name="admin")
        assert r != "viewer"

    def test_hash_by_name(self):
        from app.security_datastore import PyDALRole
        r = PyDALRole(name="admin")
        assert hash(r) == hash("admin")

    def test_eq_non_role_non_str_returns_false(self):
        from app.security_datastore import PyDALRole
        r = PyDALRole(name="admin")
        assert r != 42


# ---------------------------------------------------------------------------
# UserMixin
# ---------------------------------------------------------------------------

class TestUserMixin:
    def test_is_authenticated(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(id=1, email="a@b.com", active=True)
        assert u.is_authenticated is True

    def test_is_anonymous(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(id=1, email="a@b.com")
        assert u.is_anonymous is False

    def test_is_active_when_active_true(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(id=1, email="a@b.com", active=True)
        assert u.is_active is True

    def test_is_active_when_active_false(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(id=1, email="a@b.com", active=False)
        assert u.is_active is False

    def test_get_id_returns_string(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(id=5, email="a@b.com")
        assert u.get_id() == "5"

    def test_get_id_when_none(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="a@b.com")
        assert u.get_id() == ""

    def test_has_role_by_string(self):
        from app.security_datastore import PyDALUser, PyDALRole
        role = PyDALRole(name="admin")
        u = PyDALUser(id=1, email="a@b.com", roles=[role])
        assert u.has_role("admin") is True
        assert u.has_role("viewer") is False

    def test_has_role_by_role_object(self):
        from app.security_datastore import PyDALUser, PyDALRole
        role = PyDALRole(name="admin")
        u = PyDALUser(id=1, email="a@b.com", roles=[role])
        assert u.has_role(role) is True


# ---------------------------------------------------------------------------
# PyDALRole
# ---------------------------------------------------------------------------

class TestPyDALRole:
    def test_properties_from_kwargs(self):
        from app.security_datastore import PyDALRole
        r = PyDALRole(name="maintainer", description="Maintainer role", permissions="read")
        assert r.name == "maintainer"
        assert r.description == "Maintainer role"
        assert r.permissions == "read"
        assert r.id is None

    def test_repr(self):
        from app.security_datastore import PyDALRole
        r = PyDALRole(name="admin")
        assert "admin" in repr(r)

    def test_name_setter(self):
        from app.security_datastore import PyDALRole
        r = PyDALRole(name="viewer")
        r.name = "admin"
        assert r.name == "admin"

    def test_description_setter(self):
        from app.security_datastore import PyDALRole
        r = PyDALRole(name="x")
        r.description = "New desc"
        assert r.description == "New desc"

    def test_permissions_setter(self):
        from app.security_datastore import PyDALRole
        r = PyDALRole(name="x")
        r.permissions = "write"
        assert r.permissions == "write"


# ---------------------------------------------------------------------------
# PyDALUser
# ---------------------------------------------------------------------------

class TestPyDALUser:
    def test_properties_from_kwargs(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(id=7, email="u@example.com", password="hash", active=True,
                      fs_uniquifier="uid-123", full_name="Test User")
        assert u.id == 7
        assert u.email == "u@example.com"
        assert u.password == "hash"
        assert u.active is True
        assert u.fs_uniquifier == "uid-123"
        assert u.full_name == "Test User"

    def test_repr(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(id=1, email="test@test.com")
        assert "test@test.com" in repr(u)

    def test_email_setter(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="old@test.com")
        u.email = "new@test.com"
        assert u.email == "new@test.com"

    def test_password_setter(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="a@b.com")
        u.password = "newhash"
        assert u.password == "newhash"

    def test_active_setter(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="a@b.com", active=True)
        u.active = False
        assert u.active is False

    def test_roles_default_empty(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="a@b.com")
        assert u.roles == []

    def test_roles_setter(self):
        from app.security_datastore import PyDALUser, PyDALRole
        u = PyDALUser(email="a@b.com")
        roles = [PyDALRole(name="admin")]
        u.roles = roles
        assert u.roles == roles

    def test_get_security_payload(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(id=3, email="sec@test.com")
        payload = u.get_security_payload()
        assert payload == {"id": 3, "email": "sec@test.com"}

    def test_optional_properties_default_none(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="a@b.com")
        assert u.confirmed_at is None
        assert u.last_login_at is None
        assert u.current_login_at is None
        assert u.last_login_ip is None
        assert u.current_login_ip is None
        assert u.tf_totp_secret is None
        assert u.tf_primary_method is None

    def test_login_count_default_zero(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="a@b.com")
        assert u.login_count == 0

    def test_login_count_setter(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="a@b.com")
        u.login_count = 5
        assert u.login_count == 5

    def test_tf_setters(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="a@b.com")
        u.tf_totp_secret = "TOTP123"
        u.tf_primary_method = "authenticator"
        assert u.tf_totp_secret == "TOTP123"
        assert u.tf_primary_method == "authenticator"

    def test_login_ip_setters(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="a@b.com")
        u.last_login_ip = "1.2.3.4"
        u.current_login_ip = "5.6.7.8"
        assert u.last_login_ip == "1.2.3.4"
        assert u.current_login_ip == "5.6.7.8"

    def test_login_at_setters(self):
        from app.security_datastore import PyDALUser
        u = PyDALUser(email="a@b.com")
        now = datetime.utcnow()
        u.last_login_at = now
        u.current_login_at = now
        u.confirmed_at = now
        assert u.last_login_at == now
        assert u.current_login_at == now
        assert u.confirmed_at == now


# ---------------------------------------------------------------------------
# PyDALUserDatastore.find_user
# ---------------------------------------------------------------------------

def _make_db_with_row(row) -> MagicMock:
    """Make a MagicMock DB where any db(query).select().first() returns `row`."""
    db = MagicMock(spec=[])  # spec=[] so __call__ is defined by MagicMock
    db = MagicMock()
    # Set return_value so db(anything) returns the chain
    db.return_value.select.return_value.first.return_value = row
    db.auth_user = MagicMock()
    db.auth_user.id = MagicMock()
    db.auth_user.email = MagicMock()
    db.auth_user.email.lower = MagicMock(return_value=MagicMock())
    db.auth_user.fs_uniquifier = MagicMock()
    db.auth_user_roles = MagicMock()
    db.auth_role = MagicMock()
    db.commit = MagicMock()
    return db


class TestFindUser:
    def test_find_by_id_returns_user(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser

        user_row = MagicMock()
        user_row.id = 1
        user_row.email = "test@example.com"
        user_row.active = True
        dict_data = {"id": 1, "email": "test@example.com", "active": True}
        user_row.__iter__ = MagicMock(return_value=iter(dict_data.items()))

        db = _make_db_with_row(user_row)
        store = PyDALUserDatastore(db)

        with patch.object(store, "_get_user_roles", return_value=[]):
            result = store.find_user(id=1)

        assert result is not None
        assert isinstance(result, PyDALUser)

    def test_find_user_returns_none_when_not_found(self):
        from app.security_datastore import PyDALUserDatastore

        db = _make_db_with_row(None)
        store = PyDALUserDatastore(db)

        with patch.object(store, "_get_user_roles", return_value=[]):
            result = store.find_user(id=999)

        assert result is None

    def test_find_by_email(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser

        user_row = MagicMock()
        user_row.id = 2
        user_row.email = "find@example.com"
        user_row.active = True
        dict_data = {"id": 2, "email": "find@example.com", "active": True}
        user_row.__iter__ = MagicMock(return_value=iter(dict_data.items()))

        db = _make_db_with_row(user_row)
        store = PyDALUserDatastore(db)

        with patch.object(store, "_get_user_roles", return_value=[]):
            result = store.find_user(email="find@example.com")

        assert result is not None
        assert isinstance(result, PyDALUser)

    def test_find_by_email_case_insensitive(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser

        user_row = MagicMock()
        user_row.id = 3
        user_row.email = "UPPER@EXAMPLE.COM"
        user_row.active = True
        dict_data = {"id": 3, "email": "UPPER@EXAMPLE.COM", "active": True}
        user_row.__iter__ = MagicMock(return_value=iter(dict_data.items()))

        db = _make_db_with_row(user_row)
        db.auth_user.email.lower = MagicMock(return_value=MagicMock())
        store = PyDALUserDatastore(db)

        with patch.object(store, "_get_user_roles", return_value=[]):
            result = store.find_user(email="upper@example.com", case_insensitive=True)

        assert result is not None

    def test_find_by_fs_uniquifier(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser

        user_row = MagicMock()
        user_row.id = 4
        user_row.email = "uniq@example.com"
        user_row.active = True
        dict_data = {"id": 4, "email": "uniq@example.com", "active": True}
        user_row.__iter__ = MagicMock(return_value=iter(dict_data.items()))

        db = _make_db_with_row(user_row)
        store = PyDALUserDatastore(db)

        with patch.object(store, "_get_user_roles", return_value=[]):
            result = store.find_user(fs_uniquifier="some-uuid")

        assert result is not None

    def test_find_user_no_known_kwarg_returns_none(self):
        from app.security_datastore import PyDALUserDatastore

        db = _make_db_with_row(None)
        # Make hasattr(db.auth_user, key) return False for unknown key
        real_hasattr = hasattr

        def fake_hasattr(obj, name):
            if obj is db.auth_user and name == "unknown_field":
                return False
            return real_hasattr(obj, name)

        store = PyDALUserDatastore(db)
        with patch("builtins.hasattr", side_effect=fake_hasattr):
            result = store.find_user(unknown_field="value")
        assert result is None


# ---------------------------------------------------------------------------
# PyDALUserDatastore.find_role
# ---------------------------------------------------------------------------

class TestFindRole:
    def test_find_role_returns_role(self):
        from app.security_datastore import PyDALUserDatastore, PyDALRole

        role_row = MagicMock()
        role_row.id = 10
        role_row.name = "admin"
        dict_data = {"id": 10, "name": "admin"}
        role_row.__iter__ = MagicMock(return_value=iter(dict_data.items()))

        db = _make_db_with_row(role_row)
        store = PyDALUserDatastore(db)
        result = store.find_role("admin")
        assert isinstance(result, PyDALRole)

    def test_find_role_returns_none_when_missing(self):
        from app.security_datastore import PyDALUserDatastore

        db = _make_db_with_row(None)
        store = PyDALUserDatastore(db)
        result = store.find_role("nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# PyDALUserDatastore.activate_user / deactivate_user / toggle_active
# ---------------------------------------------------------------------------

class TestActivationMethods:
    def _make_store_and_user(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser
        db = MagicMock()
        db.auth_user = MagicMock()
        query = MagicMock()
        db.__call__ = MagicMock(return_value=query)
        store = PyDALUserDatastore(db)
        user = PyDALUser(id=1, email="a@b.com", active=True)
        return store, user, db, query

    def test_deactivate_user(self):
        from app.security_datastore import PyDALUser
        store, user, db, query = self._make_store_and_user()
        result = store.deactivate_user(user)
        assert result is True
        assert user.active is False
        db.commit.assert_called()

    def test_activate_user(self):
        store, user, db, query = self._make_store_and_user()
        user._data["active"] = False
        result = store.activate_user(user)
        assert result is True
        assert user.active is True
        db.commit.assert_called()

    def test_toggle_active_from_true_to_false(self):
        store, user, db, query = self._make_store_and_user()
        result = store.toggle_active(user)
        assert result is True
        assert user.active is False

    def test_toggle_active_from_false_to_true(self):
        from app.security_datastore import PyDALUser
        store, user, db, query = self._make_store_and_user()
        user._data["active"] = False
        result = store.toggle_active(user)
        assert result is True
        assert user.active is True


# ---------------------------------------------------------------------------
# PyDALUserDatastore.add_role_to_user / remove_role_from_user
# ---------------------------------------------------------------------------

class TestRoleManagement:
    def _make_store(self, existing_role_row=None):
        from app.security_datastore import PyDALUserDatastore
        db = MagicMock()
        db.auth_role = MagicMock()
        db.auth_user = MagicMock()
        db.auth_user_roles = MagicMock()
        # db(query).select().first() returns existing_role_row
        db.return_value.select.return_value.first.return_value = existing_role_row
        db.commit = MagicMock()
        store = PyDALUserDatastore(db)
        return store, db

    def test_add_role_to_user_by_role_object(self):
        from app.security_datastore import PyDALUser, PyDALRole
        store, db = self._make_store(existing_role_row=None)
        user = PyDALUser(id=1, email="a@b.com")
        role = PyDALRole(id=10, name="admin")
        result = store.add_role_to_user(user, role)
        assert result is True
        assert role in user.roles
        db.commit.assert_called()

    def test_add_role_to_user_by_string(self):
        from app.security_datastore import PyDALUser, PyDALRole
        store, db = self._make_store(existing_role_row=None)
        user = PyDALUser(id=1, email="a@b.com")
        role_obj = PyDALRole(id=10, name="admin")
        with patch.object(store, "find_role", return_value=role_obj):
            result = store.add_role_to_user(user, "admin")
        assert result is True

    def test_add_role_returns_false_when_role_not_found(self):
        from app.security_datastore import PyDALUser
        store, db = self._make_store()
        user = PyDALUser(id=1, email="a@b.com")
        with patch.object(store, "find_role", return_value=None):
            result = store.add_role_to_user(user, "nonexistent")
        assert result is False

    def test_add_role_returns_false_when_already_has_role(self):
        from app.security_datastore import PyDALUser, PyDALRole, PyDALUserDatastore
        db = MagicMock()
        db.auth_role = MagicMock()
        db.auth_user = MagicMock()
        db.auth_user_roles = MagicMock()
        existing = MagicMock()
        existing.select.return_value.first.return_value = MagicMock()  # Already exists
        db.__call__ = MagicMock(return_value=existing)
        store = PyDALUserDatastore(db)
        user = PyDALUser(id=1, email="a@b.com")
        role = PyDALRole(id=10, name="admin")
        result = store.add_role_to_user(user, role)
        assert result is False

    def test_remove_role_from_user(self):
        from app.security_datastore import PyDALUser, PyDALRole, PyDALUserDatastore
        db = MagicMock()
        db.auth_user_roles = MagicMock()
        db.auth_role = MagicMock()
        db.auth_user = MagicMock()
        db.return_value.delete.return_value = 1
        db.commit = MagicMock()
        store = PyDALUserDatastore(db)
        role = PyDALRole(id=10, name="admin")
        user = PyDALUser(id=1, email="a@b.com", roles=[role])
        result = store.remove_role_from_user(user, role)
        assert result is True
        assert role not in user.roles

    def test_remove_role_by_string(self):
        from app.security_datastore import PyDALUser, PyDALRole, PyDALUserDatastore
        db = MagicMock()
        db.auth_user_roles = MagicMock()
        db.auth_role = MagicMock()
        db.auth_user = MagicMock()
        db.return_value.delete.return_value = 1
        db.commit = MagicMock()
        store = PyDALUserDatastore(db)
        role = PyDALRole(id=10, name="admin")
        user = PyDALUser(id=1, email="a@b.com", roles=[role])
        with patch.object(store, "find_role", return_value=role):
            result = store.remove_role_from_user(user, "admin")
        assert result is True

    def test_remove_role_returns_false_when_not_deleted(self):
        from app.security_datastore import PyDALUser, PyDALRole, PyDALUserDatastore
        db = MagicMock()
        db.auth_user_roles = MagicMock()
        db.auth_role = MagicMock()
        db.auth_user = MagicMock()
        db.return_value.delete.return_value = 0
        store = PyDALUserDatastore(db)
        role = PyDALRole(id=10, name="viewer")
        user = PyDALUser(id=1, email="a@b.com")
        result = store.remove_role_from_user(user, role)
        assert result is False

    def test_remove_role_returns_false_when_role_not_found(self):
        from app.security_datastore import PyDALUser
        from app.security_datastore import PyDALUserDatastore
        db = MagicMock()
        store = PyDALUserDatastore(db)
        user = PyDALUser(id=1, email="a@b.com")
        with patch.object(store, "find_role", return_value=None):
            result = store.remove_role_from_user(user, "nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# PyDALUserDatastore.set_uniquifier
# ---------------------------------------------------------------------------

class TestSetUniquifier:
    def test_set_with_explicit_value(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser
        db = MagicMock()
        db.auth_user = MagicMock()
        query = MagicMock()
        db.__call__ = MagicMock(return_value=query)
        store = PyDALUserDatastore(db)
        user = PyDALUser(id=1, email="a@b.com")
        store.set_uniquifier(user, "custom-uid-456")
        assert user.fs_uniquifier == "custom-uid-456"
        db.commit.assert_called()

    def test_set_with_auto_generated_value(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser
        db = MagicMock()
        db.auth_user = MagicMock()
        query = MagicMock()
        db.__call__ = MagicMock(return_value=query)
        store = PyDALUserDatastore(db)
        user = PyDALUser(id=1, email="a@b.com")
        store.set_uniquifier(user)
        # Should have been set to a UUID
        assert user.fs_uniquifier is not None
        assert len(user.fs_uniquifier) > 0


# ---------------------------------------------------------------------------
# PyDALUserDatastore.put
# ---------------------------------------------------------------------------

class TestPut:
    def test_put_existing_user_updates(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser
        db = MagicMock()
        db.auth_user = MagicMock()
        query = MagicMock()
        db.__call__ = MagicMock(return_value=query)
        db.commit = MagicMock()
        store = PyDALUserDatastore(db)
        user = PyDALUser(id=1, email="upd@example.com", active=True)
        result = store.put(user)
        assert result is user
        db.commit.assert_called()

    def test_put_existing_role_updates(self):
        from app.security_datastore import PyDALUserDatastore, PyDALRole
        db = MagicMock()
        db.auth_role = MagicMock()
        query = MagicMock()
        db.__call__ = MagicMock(return_value=query)
        db.commit = MagicMock()
        store = PyDALUserDatastore(db)
        role = PyDALRole(id=10, name="admin", description="Admin")
        result = store.put(role)
        assert result is role
        db.commit.assert_called()


# ---------------------------------------------------------------------------
# PyDALUserDatastore.delete_user
# ---------------------------------------------------------------------------

class TestDeleteUser:
    def test_delete_user_removes_roles_and_user(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser
        db = MagicMock()
        db.auth_user = MagicMock()
        db.auth_user_roles = MagicMock()
        db.return_value.delete.return_value = 1
        db.commit = MagicMock()
        store = PyDALUserDatastore(db)
        user = PyDALUser(id=1, email="del@example.com")
        store.delete_user(user)
        assert db.return_value.delete.called
        db.commit.assert_called()

    def test_delete_user_noop_when_no_id(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser
        db = MagicMock()
        store = PyDALUserDatastore(db)
        user = PyDALUser(email="noid@example.com")
        store.delete_user(user)
        db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# PyDALUserDatastore.reset_user_access
# ---------------------------------------------------------------------------

class TestResetUserAccess:
    def test_reset_calls_set_uniquifier(self):
        from app.security_datastore import PyDALUserDatastore, PyDALUser
        db = MagicMock()
        store = PyDALUserDatastore(db)
        user = PyDALUser(id=1, email="a@b.com")
        with patch.object(store, "set_uniquifier") as mock_set:
            store.reset_user_access(user)
            mock_set.assert_called_once_with(user)
