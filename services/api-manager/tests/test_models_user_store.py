"""User and refresh-token persistence in ``app.models`` against a real penguin-dal DB.

Each function is exercised on a fresh SQLite database with the auth tables
defined, asserting returned dicts, role assignment rules, pagination, deletion
cascade, and refresh-token validity/revocation semantics. ``get_db`` is pointed
at the test DB; no mocks of the DAL are used.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from penguin_dal import DB, Field

import app.models as models


@pytest.fixture()
def auth_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> DB:
    """Fresh SQLite DB with the auth_* tables; ``app.models.get_db`` returns it."""
    db = DB(f"sqlite:///{tmp_path}/auth.db", pool_size=1, reflect=False, migrate=True)
    db.define_table(
        "auth_user",
        Field("email", "string"),
        Field("password", "string"),
        Field("full_name", "string"),
        Field("active", "boolean", default=True),
        Field("fs_uniquifier", "string"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=True,
    )
    db.define_table("auth_role", Field("name", "string"), Field("description", "string"), migrate=True)
    db.define_table(
        "auth_user_roles",
        Field("user_id", "integer"),
        Field("role_id", "integer"),
        migrate=True,
    )
    db.define_table(
        "auth_refresh_tokens",
        Field("user_id", "integer"),
        Field("token_hash", "string"),
        Field("expires_at", "datetime"),
        Field("revoked", "boolean", default=False),
        migrate=True,
    )
    db.define_table("auth_password_resets", Field("user_id", "integer"), migrate=True)
    monkeypatch.setattr(models, "get_db", lambda: db)
    return db


def _role(db: DB, name: str) -> int:
    """Insert a role row and return its id."""
    return int(db.auth_role.insert(name=name, description=name))


def test_create_user_lowercases_email_and_assigns_existing_role(auth_db: DB) -> None:
    """create_user stores a lowercased email, a uuid uniquifier, and the requested role."""
    _role(auth_db, "admin")

    user = models.create_user("Alice@Example.COM", "hash-1", full_name="Alice", role="admin")

    assert user is not None
    assert user["email"] == "alice@example.com"
    assert user["password_hash"] == "hash-1"
    assert user["full_name"] == "Alice"
    assert user["role"] == "admin"
    assert user["is_active"] is True
    fs = auth_db(auth_db.auth_user.id == user["id"]).select().first().fs_uniquifier
    assert len(fs) == 36  # uuid4 string


def test_create_user_with_unknown_role_gets_default_viewer(auth_db: DB) -> None:
    """An unknown role name creates the user without a role row; the reported role falls back to viewer."""
    user = models.create_user("bob@example.com", "h", role="ghost")

    assert user["role"] == "viewer"
    assert len(auth_db(auth_db.auth_user_roles.user_id == user["id"]).select()) == 0


def test_get_user_by_id_and_email_round_trip(auth_db: DB) -> None:
    """Lookup by id and by (case-insensitive) email return the same user record."""
    created = models.create_user("carol@example.com", "h2", role="viewer")

    by_id = models.get_user_by_id(created["id"])
    by_email = models.get_user_by_email("CAROL@example.com")

    assert by_id == by_email == created


def test_lookups_return_none_for_missing_users(auth_db: DB) -> None:
    """Unknown id or email yields None rather than raising."""
    assert models.get_user_by_id(999) is None
    assert models.get_user_by_email("nobody@example.com") is None


def test_update_user_maps_field_names_and_replaces_role(auth_db: DB) -> None:
    """update_user maps password_hash/is_active to DB columns and swaps the role assignment."""
    _role(auth_db, "admin")
    _role(auth_db, "maintainer")
    user = models.create_user("dan@example.com", "old", role="admin")

    updated = models.update_user(
        user["id"], password_hash="new", is_active=False, role="maintainer"
    )

    assert updated is not None
    assert updated["password_hash"] == "new"
    assert updated["is_active"] is False
    assert updated["role"] == "maintainer"
    assignments = auth_db(auth_db.auth_user_roles.user_id == user["id"]).select()
    assert len(assignments) == 1


def test_update_user_with_unknown_role_keeps_existing_assignment(auth_db: DB) -> None:
    """Changing to a role name that does not exist leaves the current assignment untouched."""
    _role(auth_db, "admin")
    user = models.create_user("erin@example.com", "h", role="admin")

    updated = models.update_user(user["id"], role="does-not-exist")

    assert updated["role"] == "admin"


def test_update_user_without_fields_is_a_noop_read(auth_db: DB) -> None:
    """An update with no fields returns the unchanged user."""
    user = models.create_user("fay@example.com", "h", role="viewer")

    assert models.update_user(user["id"]) == user


def test_delete_user_removes_tokens_roles_and_row(auth_db: DB) -> None:
    """Deleting a user cascades to role, refresh-token and reset rows, and reports success."""
    _role(auth_db, "viewer")
    user = models.create_user("gus@example.com", "h", role="viewer")
    models.store_refresh_token(user["id"], "tok-gus", datetime.utcnow() + timedelta(hours=1))
    auth_db.auth_password_resets.insert(user_id=user["id"])

    assert models.delete_user(user["id"]) is True

    assert models.get_user_by_id(user["id"]) is None
    assert len(auth_db(auth_db.auth_user_roles.user_id == user["id"]).select()) == 0
    assert len(auth_db(auth_db.auth_refresh_tokens.user_id == user["id"]).select()) == 0
    assert len(auth_db(auth_db.auth_password_resets.user_id == user["id"]).select()) == 0


def test_delete_missing_user_reports_false(auth_db: DB) -> None:
    """Deleting a non-existent user returns False."""
    assert models.delete_user(4242) is False


def test_list_users_paginates_with_total_and_roles(auth_db: DB) -> None:
    """list_users returns the requested page in id order along with the total count."""
    _role(auth_db, "viewer")
    for i in range(5):
        models.create_user(f"u{i}@example.com", "h", role="viewer")

    page1, total = models.list_users(page=1, per_page=2)
    page3, total3 = models.list_users(page=3, per_page=2)

    assert total == total3 == 5
    assert [u["email"] for u in page1] == ["u0@example.com", "u1@example.com"]
    assert [u["email"] for u in page3] == ["u4@example.com"]
    assert all(u["role"] == "viewer" for u in page1)


def test_list_users_past_last_page_is_empty_with_total(auth_db: DB) -> None:
    """A page beyond the data returns no rows but still reports the total."""
    models.create_user("solo@example.com", "h", role="viewer")

    rows, total = models.list_users(page=9, per_page=10)

    assert rows == []
    assert total == 1


def test_refresh_token_validity_lifecycle(auth_db: DB) -> None:
    """A stored unexpired token is valid; revoking it makes it invalid; revoking again reports no change."""
    user = models.create_user("hank@example.com", "h", role="viewer")
    token_id = models.store_refresh_token(user["id"], "hash-hank", datetime.utcnow() + timedelta(hours=1))

    assert token_id > 0
    assert models.is_refresh_token_valid("hash-hank") is True
    assert models.revoke_refresh_token("hash-hank") is True
    assert models.is_refresh_token_valid("hash-hank") is False
    assert models.revoke_refresh_token("hash-hank") is True  # row still matches; idempotent update
    assert models.revoke_refresh_token("never-stored") is False


def test_expired_refresh_token_is_invalid(auth_db: DB) -> None:
    """A token past its expiry is not valid even if never revoked."""
    user = models.create_user("ivy@example.com", "h", role="viewer")
    models.store_refresh_token(user["id"], "hash-ivy", datetime.utcnow() - timedelta(minutes=1))

    assert models.is_refresh_token_valid("hash-ivy") is False


def test_unknown_refresh_token_is_invalid(auth_db: DB) -> None:
    """A hash that was never stored is not valid."""
    assert models.is_refresh_token_valid("nope") is False


def test_revoke_all_user_tokens_counts_only_active_tokens(auth_db: DB) -> None:
    """Bulk revoke touches only the user's still-active tokens and returns that count."""
    user = models.create_user("jo@example.com", "h", role="viewer")
    other = models.create_user("kim@example.com", "h", role="viewer")
    future = datetime.utcnow() + timedelta(hours=1)
    models.store_refresh_token(user["id"], "jo-1", future)
    models.store_refresh_token(user["id"], "jo-2", future)
    models.store_refresh_token(other["id"], "kim-1", future)
    models.revoke_refresh_token("jo-1")

    revoked = models.revoke_all_user_tokens(user["id"])

    assert revoked == 1
    assert models.is_refresh_token_valid("jo-2") is False
    assert models.is_refresh_token_valid("kim-1") is True


def test_role_lookup_degrades_to_viewer_on_db_error(auth_db: DB, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB failure while resolving the role is swallowed and reported as the default viewer role."""

    class _Boom:
        def __call__(self, *_a: object, **_k: object) -> None:
            raise RuntimeError("db offline")

    assert models._get_user_role(_Boom(), 1) == "viewer"  # type: ignore[arg-type]
