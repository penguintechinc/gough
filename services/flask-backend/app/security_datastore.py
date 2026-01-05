"""PyDAL Datastore for Flask-Security-Too.

This module provides a custom datastore that bridges Flask-Security-Too
with PyDAL for database operations, as required by CLAUDE.md standards.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from flask_security import UserMixin, RoleMixin
from flask_security.datastore import UserDatastore

if TYPE_CHECKING:
    from pydal import DAL
    from pydal.objects import Row


class PyDALRole(RoleMixin):
    """Role model wrapper for Flask-Security-Too compatibility."""

    def __init__(self, row: Row | None = None, **kwargs: Any) -> None:
        self._row = row
        self._data = kwargs
        if row:
            self._data = dict(row)

    @property
    def id(self) -> int | None:
        return self._data.get("id")

    @property
    def name(self) -> str:
        return self._data.get("name", "")

    @name.setter
    def name(self, value: str) -> None:
        self._data["name"] = value

    @property
    def description(self) -> str:
        return self._data.get("description", "")

    @description.setter
    def description(self, value: str) -> None:
        self._data["description"] = value

    @property
    def permissions(self) -> str | None:
        return self._data.get("permissions")

    @permissions.setter
    def permissions(self, value: str) -> None:
        self._data["permissions"] = value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PyDALRole):
            return self.name == other.name
        if isinstance(other, str):
            return self.name == other
        return False

    def __hash__(self) -> int:
        return hash(self.name)

    def __repr__(self) -> str:
        return f"<Role {self.name}>"


class PyDALUser(UserMixin):
    """User model wrapper for Flask-Security-Too compatibility."""

    def __init__(
        self,
        row: Row | None = None,
        roles: list[PyDALRole] | None = None,
        **kwargs: Any,
    ) -> None:
        self._row = row
        self._data = kwargs
        self._roles = roles or []
        if row:
            self._data = dict(row)

    @property
    def id(self) -> int | None:
        return self._data.get("id")

    @property
    def email(self) -> str:
        return self._data.get("email", "")

    @email.setter
    def email(self, value: str) -> None:
        self._data["email"] = value

    @property
    def password(self) -> str:
        return self._data.get("password", "")

    @password.setter
    def password(self, value: str) -> None:
        self._data["password"] = value

    @property
    def active(self) -> bool:
        return self._data.get("active", True)

    @active.setter
    def active(self, value: bool) -> None:
        self._data["active"] = value

    @property
    def fs_uniquifier(self) -> str:
        return self._data.get("fs_uniquifier", "")

    @fs_uniquifier.setter
    def fs_uniquifier(self, value: str) -> None:
        self._data["fs_uniquifier"] = value

    @property
    def confirmed_at(self) -> datetime | None:
        return self._data.get("confirmed_at")

    @confirmed_at.setter
    def confirmed_at(self, value: datetime) -> None:
        self._data["confirmed_at"] = value

    @property
    def last_login_at(self) -> datetime | None:
        return self._data.get("last_login_at")

    @last_login_at.setter
    def last_login_at(self, value: datetime) -> None:
        self._data["last_login_at"] = value

    @property
    def current_login_at(self) -> datetime | None:
        return self._data.get("current_login_at")

    @current_login_at.setter
    def current_login_at(self, value: datetime) -> None:
        self._data["current_login_at"] = value

    @property
    def last_login_ip(self) -> str | None:
        return self._data.get("last_login_ip")

    @last_login_ip.setter
    def last_login_ip(self, value: str) -> None:
        self._data["last_login_ip"] = value

    @property
    def current_login_ip(self) -> str | None:
        return self._data.get("current_login_ip")

    @current_login_ip.setter
    def current_login_ip(self, value: str) -> None:
        self._data["current_login_ip"] = value

    @property
    def login_count(self) -> int:
        return self._data.get("login_count", 0)

    @login_count.setter
    def login_count(self, value: int) -> None:
        self._data["login_count"] = value

    @property
    def tf_totp_secret(self) -> str | None:
        return self._data.get("tf_totp_secret")

    @tf_totp_secret.setter
    def tf_totp_secret(self, value: str) -> None:
        self._data["tf_totp_secret"] = value

    @property
    def tf_primary_method(self) -> str | None:
        return self._data.get("tf_primary_method")

    @tf_primary_method.setter
    def tf_primary_method(self, value: str) -> None:
        self._data["tf_primary_method"] = value

    @property
    def roles(self) -> list[PyDALRole]:
        return self._roles

    @roles.setter
    def roles(self, value: list[PyDALRole]) -> None:
        self._roles = value

    @property
    def full_name(self) -> str:
        return self._data.get("full_name", "")

    @full_name.setter
    def full_name(self, value: str) -> None:
        self._data["full_name"] = value

    def has_role(self, role: str | PyDALRole) -> bool:
        """Check if user has the specified role."""
        if isinstance(role, str):
            return any(r.name == role for r in self._roles)
        return role in self._roles

    def get_security_payload(self) -> dict[str, Any]:
        """Return security payload for token generation."""
        return {"id": self.id, "email": self.email}

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class PyDALUserDatastore(UserDatastore):
    """PyDAL-based datastore for Flask-Security-Too.

    This datastore uses PyDAL for all database operations as required
    by CLAUDE.md standards.
    """

    def __init__(self, db: DAL) -> None:
        self.db = db
        self.user_model = PyDALUser
        self.role_model = PyDALRole

    def _get_user_roles(self, user_id: int) -> list[PyDALRole]:
        """Get all roles for a user."""
        db = self.db
        rows = db(
            (db.auth_user_roles.user_id == user_id)
            & (db.auth_user_roles.role_id == db.auth_role.id)
        ).select(db.auth_role.ALL)
        return [PyDALRole(row) for row in rows]

    def find_user(self, **kwargs: Any) -> PyDALUser | None:
        """Find a user by any attribute."""
        db = self.db

        # Handle case_insensitive option for email
        case_insensitive = kwargs.pop("case_insensitive", False)

        if "id" in kwargs:
            row = db(db.auth_user.id == kwargs["id"]).select().first()
        elif "email" in kwargs:
            email = kwargs["email"]
            if case_insensitive:
                row = db(db.auth_user.email.lower() == email.lower()).select().first()
            else:
                row = db(db.auth_user.email == email).select().first()
        elif "fs_uniquifier" in kwargs:
            row = db(
                db.auth_user.fs_uniquifier == kwargs["fs_uniquifier"]
            ).select().first()
        else:
            # Generic query for other attributes
            query = None
            for key, value in kwargs.items():
                if hasattr(db.auth_user, key):
                    q = getattr(db.auth_user, key) == value
                    query = q if query is None else query & q
            if query is None:
                return None
            row = db(query).select().first()

        if not row:
            return None

        roles = self._get_user_roles(row.id)
        return PyDALUser(row, roles=roles)

    def find_role(self, role: str) -> PyDALRole | None:
        """Find a role by name."""
        db = self.db
        row = db(db.auth_role.name == role).select().first()
        return PyDALRole(row) if row else None

    def create_user(self, **kwargs: Any) -> PyDALUser:
        """Create a new user."""
        db = self.db

        # Generate fs_uniquifier if not provided
        if "fs_uniquifier" not in kwargs:
            kwargs["fs_uniquifier"] = str(uuid.uuid4())

        # Extract roles before inserting user
        roles = kwargs.pop("roles", [])

        # Set default values
        kwargs.setdefault("active", True)
        kwargs.setdefault("created_at", datetime.utcnow())
        kwargs.setdefault("updated_at", datetime.utcnow())
        kwargs.setdefault("login_count", 0)

        # Insert user
        user_id = db.auth_user.insert(**kwargs)
        db.commit()

        # Add roles
        role_objects = []
        for role in roles:
            if isinstance(role, str):
                role_obj = self.find_role(role)
            else:
                role_obj = role
            if role_obj:
                db.auth_user_roles.insert(user_id=user_id, role_id=role_obj.id)
                role_objects.append(role_obj)
        db.commit()

        # Return the created user
        row = db(db.auth_user.id == user_id).select().first()
        return PyDALUser(row, roles=role_objects)

    def create_role(self, **kwargs: Any) -> PyDALRole:
        """Create a new role."""
        db = self.db
        kwargs.setdefault("created_at", datetime.utcnow())
        role_id = db.auth_role.insert(**kwargs)
        db.commit()
        row = db(db.auth_role.id == role_id).select().first()
        return PyDALRole(row)

    def add_role_to_user(self, user: PyDALUser, role: str | PyDALRole) -> bool:
        """Add a role to a user."""
        db = self.db

        if isinstance(role, str):
            role_obj = self.find_role(role)
        else:
            role_obj = role

        if not role_obj or not user.id:
            return False

        # Check if already has role
        existing = db(
            (db.auth_user_roles.user_id == user.id)
            & (db.auth_user_roles.role_id == role_obj.id)
        ).select().first()

        if existing:
            return False

        db.auth_user_roles.insert(user_id=user.id, role_id=role_obj.id)
        db.commit()

        # Update user's roles
        if role_obj not in user.roles:
            user.roles.append(role_obj)

        return True

    def remove_role_from_user(self, user: PyDALUser, role: str | PyDALRole) -> bool:
        """Remove a role from a user."""
        db = self.db

        if isinstance(role, str):
            role_obj = self.find_role(role)
        else:
            role_obj = role

        if not role_obj or not user.id:
            return False

        deleted = db(
            (db.auth_user_roles.user_id == user.id)
            & (db.auth_user_roles.role_id == role_obj.id)
        ).delete()
        db.commit()

        if deleted:
            user.roles = [r for r in user.roles if r.name != role_obj.name]
            return True

        return False

    def toggle_active(self, user: PyDALUser) -> bool:
        """Toggle the active status of a user."""
        db = self.db
        new_status = not user.active
        db(db.auth_user.id == user.id).update(
            active=new_status, updated_at=datetime.utcnow()
        )
        db.commit()
        user.active = new_status
        return True

    def deactivate_user(self, user: PyDALUser) -> bool:
        """Deactivate a user."""
        db = self.db
        db(db.auth_user.id == user.id).update(
            active=False, updated_at=datetime.utcnow()
        )
        db.commit()
        user.active = False
        return True

    def activate_user(self, user: PyDALUser) -> bool:
        """Activate a user."""
        db = self.db
        db(db.auth_user.id == user.id).update(
            active=True, updated_at=datetime.utcnow()
        )
        db.commit()
        user.active = True
        return True

    def set_uniquifier(self, user: PyDALUser, uniquifier: str | None = None) -> None:
        """Set the uniquifier for a user."""
        db = self.db
        if uniquifier is None:
            uniquifier = str(uuid.uuid4())
        db(db.auth_user.id == user.id).update(
            fs_uniquifier=uniquifier, updated_at=datetime.utcnow()
        )
        db.commit()
        user.fs_uniquifier = uniquifier

    def put(self, model: PyDALUser | PyDALRole) -> PyDALUser | PyDALRole:
        """Save a user or role to the database."""
        db = self.db

        if isinstance(model, PyDALUser):
            if model.id:
                # Update existing user
                update_data = {
                    k: v
                    for k, v in model._data.items()
                    if k not in ("id", "roles") and v is not None
                }
                update_data["updated_at"] = datetime.utcnow()
                db(db.auth_user.id == model.id).update(**update_data)
            else:
                # Create new user
                return self.create_user(**model._data)
        elif isinstance(model, PyDALRole):
            if model.id:
                # Update existing role
                update_data = {
                    k: v
                    for k, v in model._data.items()
                    if k != "id" and v is not None
                }
                db(db.auth_role.id == model.id).update(**update_data)
            else:
                # Create new role
                return self.create_role(**model._data)

        db.commit()
        return model

    def delete_user(self, user: PyDALUser) -> None:
        """Delete a user and their role associations."""
        db = self.db
        if user.id:
            # Delete role associations first
            db(db.auth_user_roles.user_id == user.id).delete()
            # Delete user
            db(db.auth_user.id == user.id).delete()
            db.commit()

    def reset_user_access(self, user: PyDALUser) -> None:
        """Reset user access by generating a new uniquifier."""
        self.set_uniquifier(user)
