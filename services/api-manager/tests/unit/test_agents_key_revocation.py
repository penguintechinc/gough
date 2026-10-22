"""Enrollment key revocation is a soft delete, and enrollment rejects revoked keys.

The agents endpoints are reached through their innermost undecorated coroutines (the auth
decorators are bypassed) and run against an in-memory DAL stand-in.
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from quart import Quart

from app.api import agents

ENROLL_KEY = "ENROLL-TEST-KEY"


class _Cond:
    """Conjunction of (table, field, value) equality predicates."""

    def __init__(self, preds: list[tuple[str, str, Any]]) -> None:
        self.preds = preds

    def __and__(self, other: "_Cond") -> "_Cond":
        return _Cond(self.preds + other.preds)


class _Field:
    """Column reference whose == produces a predicate."""

    def __init__(self, table: str, name: str) -> None:
        self.table = table
        self.name = name

    def __eq__(self, value: object) -> _Cond:  # type: ignore[override]
        return _Cond([(self.table, self.name, value)])

    __hash__ = object.__hash__


class _Table:
    """Table handle: attribute access yields fields; call yields a row by primary key."""

    def __init__(self, db: "FakeDAL", name: str) -> None:
        self._db = db
        self._name = name

    def __getattr__(self, field: str) -> _Field:
        if field.startswith("_"):
            raise AttributeError(field)
        return _Field(self._name, field)

    def __call__(self, key_id: int) -> Any:
        return next((r for r in self._db.rows[self._name] if r.id == key_id), None)


class _Query:
    """Selector over rows matching a conjunction of predicates."""

    def __init__(self, db: "FakeDAL", cond: _Cond) -> None:
        self._db = db
        self._cond = cond

    def _matches(self) -> list[Any]:
        table = self._cond.preds[0][0]
        return [r for r in self._db.rows[table]
                if all(getattr(r, f) == v for _, f, v in self._cond.preds)]

    def select(self, **_: Any) -> "_Query":
        return self

    def first(self) -> Any:
        matches = self._matches()
        return matches[0] if matches else None

    def update(self, **values: Any) -> int:
        matches = self._matches()
        for row in matches:
            for k, v in values.items():
                setattr(row, k, v)
        return len(matches)

    def delete(self) -> int:
        table = self._cond.preds[0][0]
        matches = self._matches()
        self._db.rows[table] = [r for r in self._db.rows[table] if r not in matches]
        return len(matches)


class FakeDAL:
    """Minimal stand-in for the release DAL: enrollment_keys rows plus commit/rollback."""

    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self.rows: dict[str, list[Any]] = {"enrollment_keys": [SimpleNamespace(**k) for k in keys]}
        self.enrollment_keys = _Table(self, "enrollment_keys")
        self.commits = 0

    def __call__(self, cond: _Cond) -> _Query:
        return _Query(self, cond)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


def _key_row(key_id: int = 1, revoked: bool = False) -> dict[str, Any]:
    """Build an unused, unexpired enrollment key row for ENROLL_KEY."""
    return {"id": key_id, "key_hash": hashlib.sha256(ENROLL_KEY.encode()).hexdigest(),
            "created_by": 1, "expires_at": datetime.utcnow() + timedelta(hours=1),
            "is_used": False, "revoked": revoked, "metadata": None}


def _innermost(handler: Any) -> Any:
    """Return the undecorated coroutine behind auth/role decorators."""
    fn = handler
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.fixture
def app() -> Quart:
    """Bare Quart app for request and app contexts."""
    application = Quart(__name__)
    application.config["JWT_SECRET_KEY"] = "test-secret"
    return application


@pytest.mark.asyncio
async def test_revoke_handler_is_a_soft_delete(app: Quart, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revoking sets revoked=True and keeps the row; nothing is deleted."""
    db = FakeDAL([_key_row(key_id=7)])
    monkeypatch.setattr(agents, "get_db", lambda: db)
    handler = _innermost(agents.revoke_enrollment_key)
    assert inspect.iscoroutinefunction(handler)

    async with app.app_context():
        response, status = await handler(key_id=7)

    assert status == 200
    rows = db.rows["enrollment_keys"]
    assert len(rows) == 1
    assert rows[0].revoked is True
    assert db.commits == 1


@pytest.mark.asyncio
async def test_revoke_unknown_key_is_404(app: Quart, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revoking a missing key returns 404 and does not create anything."""
    db = FakeDAL([])
    monkeypatch.setattr(agents, "get_db", lambda: db)
    handler = _innermost(agents.revoke_enrollment_key)
    async with app.app_context():
        _, status = await handler(key_id=99)
    assert status == 404


@pytest.mark.asyncio
async def test_enroll_rejects_revoked_key(app: Quart, monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked enrollment key is refused with 401 before any agent row is created."""
    db = FakeDAL([_key_row(revoked=True)])
    monkeypatch.setattr(agents, "get_db", lambda: db)
    handler = _innermost(agents.enroll_agent)
    assert inspect.iscoroutinefunction(handler)

    async def _call() -> tuple[Any, int]:
        async with app.test_request_context(
            "/api/v1/agents/enroll", method="POST",
            headers={"X-Enrollment-Key": ENROLL_KEY}, json={"hostname": "node-a"},
        ):
            response, status = await handler()
            return await response.get_json(), status

    body, status = await _call()
    assert status == 401
    assert body == {"error": "Enrollment key revoked"}
    assert db.rows["enrollment_keys"][0].is_used is False
