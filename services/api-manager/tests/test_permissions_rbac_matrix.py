"""Behavioural tests for app/permissions.py RBAC helpers.

Covers the full team-role hierarchy (owner > admin > member > viewer), the
resource-permission decision tree, and the ``require_team_permission`` /
``require_resource_permission`` decorators exercised through a real Quart
request context (no skipped decorator tests).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from quart import Quart, g, jsonify

import app.permissions as perms
from app.permissions import (
    TEAM_ROLES,
    check_resource_permission,
    check_shell_access,
    check_team_access,
    require_resource_permission,
    require_team_permission,
)


def _seq_db(*results: Any) -> MagicMock:
    """Build a fake penguin-dal handle whose successive query calls return ``results``.

    Each ``db(...)`` call pops the next result; ``.select(...).first()`` yields it
    (``None`` means no row). Lets a test script the exact query order that the
    permission helpers issue.
    """
    remaining = list(results)
    db = MagicMock()

    def _query(*_args: Any, **_kwargs: Any) -> MagicMock:
        row = remaining.pop(0)
        query = MagicMock()
        query.select.return_value.first.return_value = row
        return query

    db.side_effect = _query
    db.__getitem__.return_value = MagicMock()  # any resource table
    return db


def _membership(role: Any) -> SimpleNamespace:
    """Return a team_members row carrying ``role``."""
    return SimpleNamespace(role=role)


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: MagicMock) -> None:
    """Point app.permissions.get_db at ``db`` for the duration of a test."""
    monkeypatch.setattr(perms, "get_db", lambda: db)


# ---------------------------------------------------------------------------
# check_team_access: full role hierarchy matrix
# ---------------------------------------------------------------------------

_MATRIX = [
    (user_role, required)
    for user_role in TEAM_ROLES
    for required in TEAM_ROLES
]


@pytest.mark.parametrize(("user_role", "required"), _MATRIX)
def test_team_role_matrix(monkeypatch: pytest.MonkeyPatch, user_role: str, required: str) -> None:
    """Each member role is granted exactly when its rank is at or above the requirement."""
    _patch_db(monkeypatch, _seq_db(_membership(user_role)))

    granted = check_team_access(1, 10, required)

    expected = TEAM_ROLES.index(user_role) <= TEAM_ROLES.index(required)
    assert granted is expected, f"{user_role} vs required {required}"


def test_team_access_default_requirement_is_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default required role is member: viewer denied, member and admin allowed."""
    _patch_db(monkeypatch, _seq_db(_membership("viewer")))
    assert check_team_access(1, 10) is False

    _patch_db(monkeypatch, _seq_db(_membership("member")))
    assert check_team_access(1, 10) is True


@pytest.mark.parametrize("user_role", TEAM_ROLES)
def test_team_access_unknown_required_role_fails_closed(
    monkeypatch: pytest.MonkeyPatch, user_role: str
) -> None:
    """An unrecognised required role is denied for every member, without raising."""
    _patch_db(monkeypatch, _seq_db(_membership(user_role)))

    assert check_team_access(1, 10, "superuser") is False


@pytest.mark.parametrize("bad_role", [None, "", "OWNER", "root"])
def test_team_access_unknown_membership_role_fails_closed(
    monkeypatch: pytest.MonkeyPatch, bad_role: Any
) -> None:
    """A membership row with a None/empty/unknown role is denied, never granted."""
    _patch_db(monkeypatch, _seq_db(_membership(bad_role)))

    assert check_team_access(1, 10, "viewer") is False


def test_team_access_no_membership_row_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user with no row in team_members is denied even for the lowest role."""
    _patch_db(monkeypatch, _seq_db(None))

    assert check_team_access(1, 10, "viewer") is False



# ---------------------------------------------------------------------------
# check_resource_permission: decision tree
# ---------------------------------------------------------------------------


def _resource(**attrs: Any) -> SimpleNamespace:
    """Return a fake resource row with the given attributes."""
    return SimpleNamespace(**attrs)


def test_resource_missing_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resource id that does not exist is denied."""
    _patch_db(monkeypatch, _seq_db(None))

    assert check_resource_permission(1, "widgets", 99, "read") is False


def test_resource_unknown_table_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resource_type that is not a known table is denied (KeyError fails closed)."""
    db = MagicMock()
    db.__getitem__.side_effect = KeyError("nope")
    _patch_db(monkeypatch, db)

    assert check_resource_permission(1, "not_a_table", 1, "read") is False


def test_resource_creator_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The creator of a resource is allowed every permission, without a team lookup."""
    for permission in ("read", "write", "execute", "admin", "shell"):
        _patch_db(monkeypatch, _seq_db(_resource(created_by=5, team_id=10)))
        assert check_resource_permission(5, "widgets", 1, permission) is True


def test_resource_without_team_or_creator_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-team-scoped resource not created by the user is denied."""
    _patch_db(monkeypatch, _seq_db(SimpleNamespace(created_by=99)))

    assert check_resource_permission(1, "widgets", 1, "read") is False


def test_explicit_permission_grant_allows_listed_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit resource_permissions row grants each comma-listed permission."""
    resource = _resource(team_id=10)
    grant = SimpleNamespace(permission="read,shell")
    _patch_db(monkeypatch, _seq_db(resource, grant))

    assert check_resource_permission(1, "widgets", 1, "shell") is True


def test_explicit_grant_without_permission_falls_through_to_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant for read does not allow shell; shell falls through to a denial."""
    resource = _resource(team_id=10)
    grant = SimpleNamespace(permission="read")
    _patch_db(monkeypatch, _seq_db(resource, grant))

    assert check_resource_permission(1, "widgets", 1, "shell") is False


def test_explicit_grant_with_empty_permission_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """A grant row with a NULL permission column is treated as no grant."""
    resource = _resource(team_id=10)
    grant = SimpleNamespace(permission=None)
    # No team role for the user -> denied after the grant check fails.
    _patch_db(monkeypatch, _seq_db(resource, grant, None))

    assert check_resource_permission(1, "widgets", 1, "read") is False


@pytest.mark.parametrize(
    ("team_role", "permission", "expected"),
    [
        # admin/write: requires team admin or higher
        ("owner", "write", True),
        ("admin", "write", True),
        ("member", "write", False),
        ("viewer", "write", False),
        ("owner", "admin", True),
        ("admin", "admin", True),
        ("member", "admin", False),
        # read/execute: any team member or higher
        ("member", "read", True),
        ("viewer", "read", False),
        ("member", "execute", True),
        ("viewer", "execute", False),
        ("owner", "read", True),
    ],
)
def test_team_role_drives_resource_permission(
    monkeypatch: pytest.MonkeyPatch, team_role: str, permission: str, expected: bool
) -> None:
    """Team membership role decides write/admin and read/execute on team-scoped resources."""
    resource = _resource(team_id=10)
    _patch_db(monkeypatch, _seq_db(resource, None, _membership(team_role)))

    assert check_resource_permission(1, "widgets", 1, permission) is expected


def test_shell_permission_never_inherited_from_team_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shell is only granted explicitly; a team-scoped resource with no grant is denied."""
    resource = _resource(team_id=10)
    _patch_db(monkeypatch, _seq_db(resource, None))

    assert check_shell_access(1, "widgets", 1) is False


def test_check_shell_access_delegates_explicit_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_shell_access returns True when an explicit shell grant exists."""
    resource = _resource(team_id=10)
    grant = SimpleNamespace(permission="shell")
    _patch_db(monkeypatch, _seq_db(resource, grant))

    assert check_shell_access(1, "widgets", 1) is True


# ---------------------------------------------------------------------------
# Decorators via a real Quart request context
# ---------------------------------------------------------------------------


@pytest.fixture()
def rbac_app() -> Quart:
    """Quart app with team- and resource-guarded routes.

    The caller identity comes from ``X-Test-User-Id`` so each test controls it.
    """
    app = Quart(__name__)

    @app.before_request
    async def _identity() -> None:
        from quart import request

        raw = request.headers.get("X-Test-User-Id")
        if raw is not None:
            g.current_user = {"id": int(raw)}

    @app.route("/teams/<int:team_id>/ping")
    @require_team_permission("admin")
    async def team_route(team_id: int) -> Any:
        return jsonify({"ok": True, "team_id": team_id})

    @app.route("/team-body", methods=["POST"])
    @require_team_permission("member")
    async def team_body_route() -> Any:
        return jsonify({"ok": True})

    @app.route("/resource", methods=["POST"])
    @require_resource_permission("write")
    async def resource_route() -> Any:
        return jsonify({"ok": True})

    return app


async def test_team_decorator_denies_without_user(
    rbac_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No authenticated user -> 403, and the role check is never consulted."""
    called = MagicMock(return_value=True)
    monkeypatch.setattr(perms, "check_team_access", called)

    resp = await rbac_app.test_client().get("/teams/3/ping")

    assert resp.status_code == 403
    assert (await resp.get_json()) == {"error": "Forbidden"}
    called.assert_not_called()


async def test_team_decorator_denies_when_team_id_missing(
    rbac_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route without a team_id (args, JSON, or path) -> 403 before any DB check."""
    called = MagicMock(return_value=True)
    monkeypatch.setattr(perms, "check_team_access", called)

    resp = await rbac_app.test_client().post(
        "/team-body", headers={"X-Test-User-Id": "1"}, json={}
    )

    assert resp.status_code == 403
    called.assert_not_called()


async def test_team_decorator_denies_insufficient_role(
    rbac_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Role check returning False -> 403 and the required role is forwarded."""
    called = MagicMock(return_value=False)
    monkeypatch.setattr(perms, "check_team_access", called)

    resp = await rbac_app.test_client().get(
        "/teams/3/ping", headers={"X-Test-User-Id": "8"}
    )

    assert resp.status_code == 403
    called.assert_called_once_with(8, 3, "admin")


async def test_team_decorator_allows_sufficient_role(
    rbac_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Role check passing -> handler runs and its body is returned."""
    called = MagicMock(return_value=True)
    monkeypatch.setattr(perms, "check_team_access", called)

    resp = await rbac_app.test_client().get(
        "/teams/3/ping", headers={"X-Test-User-Id": "8"}
    )

    assert resp.status_code == 200
    assert (await resp.get_json()) == {"ok": True, "team_id": 3}
    called.assert_called_once_with(8, 3, "admin")


async def test_team_decorator_reads_team_id_from_json_body(
    rbac_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """team_id supplied in the JSON body is used for the membership check."""
    called = MagicMock(return_value=True)
    monkeypatch.setattr(perms, "check_team_access", called)

    resp = await rbac_app.test_client().post(
        "/team-body",
        headers={"X-Test-User-Id": "2"},
        json={"team_id": 55},
    )

    assert resp.status_code == 200
    called.assert_called_once_with(2, 55, "member")


async def test_resource_decorator_denies_without_resource_identifiers(
    rbac_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing resource_type/resource_id -> 403 and no permission lookup."""
    called = MagicMock(return_value=True)
    monkeypatch.setattr(perms, "check_resource_permission", called)

    resp = await rbac_app.test_client().post(
        "/resource", headers={"X-Test-User-Id": "1"}, json={"resource_type": "widgets"}
    )

    assert resp.status_code == 403
    called.assert_not_called()


async def test_resource_decorator_forwards_identifiers_from_body(
    rbac_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resource_type/resource_id from the JSON body drive the permission check."""
    called = MagicMock(return_value=True)
    monkeypatch.setattr(perms, "check_resource_permission", called)

    resp = await rbac_app.test_client().post(
        "/resource",
        headers={"X-Test-User-Id": "4"},
        json={"resource_type": "widgets", "resource_id": 9},
    )

    assert resp.status_code == 200
    called.assert_called_once_with(4, "widgets", 9, "write")


async def test_resource_decorator_denies_when_check_fails(
    rbac_app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resource check returning False -> 403 with the standard error body."""
    monkeypatch.setattr(perms, "check_resource_permission", MagicMock(return_value=False))

    resp = await rbac_app.test_client().post(
        "/resource",
        headers={"X-Test-User-Id": "4"},
        json={"resource_type": "widgets", "resource_id": 9},
    )

    assert resp.status_code == 403
    assert (await resp.get_json()) == {"error": "Forbidden"}
