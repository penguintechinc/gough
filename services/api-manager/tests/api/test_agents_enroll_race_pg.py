"""Real-Postgres concurrency regression for ``app.api.agents.enroll_agent``.

# regression: gh-22 (fix-round)

Before the fix-round: the enrollment-key ``is_used`` check and the
insert-agent + mark-used write were split across two separate ``run_db()``
thread hops. ``run_db()``/``asyncio.to_thread`` removed the accidental
single-process serialization a purely-synchronous handler used to provide
(see ``app/db/run_db.py``), so two concurrent requests presenting the same
single-use enrollment key could both read ``is_used=False`` before either
wrote, and both would insert an agent + mark the key used -- consuming a
single-use key twice.

The fix makes the claim a conditional ``UPDATE ... WHERE is_used = False``
whose rowcount is checked in the same closure as the write, before any
agent row is inserted -- Postgres serializes concurrent UPDATEs to the same
row, so at most one of two truly-concurrent requests can ever claim the
key. This test fires two enroll requests at the same key *concurrently*
(``asyncio.gather``, not sequentially) against a real Postgres connection
and asserts exactly one winner.

Schema note: ``access_agents``/``enrollment_keys``/``ssh_ca_config`` come
from ``app.models_sqlalchemy``'s declarative models via the baseline
migration's blanket ``MainBase.metadata.create_all()`` (not individual
``op.create_table()`` calls, so they don't show up in a text search of the
migration file itself). ``AccessAgent`` is missing a real column
(``enrolled_at``) that ``enroll_agent()`` has always unconditionally
inserted -- a genuine, pre-existing, unrelated bug (the endpoint 500s on
every real call against the declared schema, independent of this race
fix). Patched here with a plain ``ALTER TABLE`` against a private
connection, applied *before* this module's own ``DB(..., reflect=True)``
construction so the reflected metadata picks it up -- test-local, does not
touch ``app/models_sqlalchemy.py`` (the real schema authority) or add a
new Alembic migration, both of which are out of scope for this fix-round.
See the module docstring in ``tests/pg_fixtures.py`` for why these tables
aren't part of the baseline's explicit ``op.create_table()`` calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from penguin_dal import DB
from quart import Quart, g

pytestmark = pytest.mark.asyncio


def _passthrough_decorator(*dargs: Any, **dkwargs: Any) -> Any:
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        return dargs[0]
    return lambda fn: fn


def _seed_auth_user(db: Any, *, email: str) -> int:
    """Insert a minimal real auth_user row (enrollment_keys.created_by is a
    real FK to auth_user.id in the live schema -- app/models_sqlalchemy.py)."""
    user_id = db.auth_user.insert(
        email=email,
        fs_uniquifier=str(uuid.uuid4()),
        active=True,
    )
    db.commit()
    return int(user_id)


@pytest.fixture()
def agent_pg_db(pg_url: str, _pg_schema: None) -> Iterator[DB]:
    """Like tests/pg_fixtures.py's ``pg_db``, plus the ``enrolled_at``
    schema patch described in this module's docstring, applied before this
    fixture's own DB(..., reflect=True) construction so reflection sees it.
    """
    patch_db = DB(pg_url, pool_size=1, reflect=False)
    try:
        patch_db.executesql(
            "ALTER TABLE access_agents ADD COLUMN IF NOT EXISTS enrolled_at TIMESTAMP",
            check_injection=False,
        )
    finally:
        patch_db.close()

    db = DB(pg_url, pool_size=2, reflect=True)
    try:
        table_names = [name for name in db.tables if name != "alembic_version"]
        if table_names:
            quoted = ", ".join(f'"{name}"' for name in table_names)
            db.executesql(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE", check_injection=False)
        yield db
        table_names = [name for name in db.tables if name != "alembic_version"]
        if table_names:
            quoted = ", ".join(f'"{name}"' for name in table_names)
            db.executesql(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE", check_injection=False)
    finally:
        db.close()


@pytest.fixture()
def agents_pg_client(agent_pg_db: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Quart test client for agents_bp, backed by a real Postgres connection."""
    import app.middleware as mw_mod

    monkeypatch.setattr(mw_mod, "auth_required", _passthrough_decorator)
    monkeypatch.setattr(mw_mod, "roles_required", _passthrough_decorator)
    monkeypatch.setattr(mw_mod, "get_current_user", lambda: {
        "id": "test-user-id",
        "username": "test-user",
        "role": "admin",
    })

    import app.audit as audit_mod
    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: None)

    import app.api.agents as agents_mod
    agents_mod = importlib.reload(agents_mod)
    monkeypatch.setattr(agents_mod, "get_db", lambda: agent_pg_db)

    app = Quart(__name__)
    app.config["TESTING"] = True
    app.config["JWT_SECRET_KEY"] = "test-secret-key"
    app.url_map.strict_slashes = False
    app.register_blueprint(agents_mod.agents_bp)

    @app.before_request
    async def _inject_auth() -> None:
        g.current_user = {"id": "test-user-id", "username": "test-user", "role": "admin"}
        g.tenant_context = SimpleNamespace(tenant_id="default")

    return app.test_client()


class TestEnrollAgentConcurrency:
    async def test_two_concurrent_enrolls_same_key_exactly_one_winner(
        self, agent_pg_db: Any, agents_pg_client: Any
    ) -> None:
        """# regression: gh-22

        Two enroll requests race on the SAME single-use enrollment key,
        fired concurrently (not sequentially) via asyncio.gather so their
        run_db() closures genuinely overlap on separate worker threads
        against the same real Postgres row. Exactly one must succeed
        (201) and the other must lose the race (409) -- never 2x201
        (double-spent key) and never 2x409 (nobody wins).
        """
        raw_key = "ENROLL-RACE-TEST-KEY-0001"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        admin_id = _seed_auth_user(agent_pg_db, email="race-admin@example.com")

        agent_pg_db.enrollment_keys.insert(
            key_hash=key_hash,
            created_by=admin_id,
            expires_at=expires_at,
            is_used=False,
            created_at=datetime.now(timezone.utc),
        )
        agent_pg_db.commit()

        async def _enroll(hostname: str) -> Any:
            return await agents_pg_client.post(
                "/api/v1/agents/enroll",
                headers={"X-Enrollment-Key": raw_key},
                json={"hostname": hostname, "capabilities": ["ssh"]},
            )

        resp_a, resp_b = await asyncio.gather(
            _enroll("race-host-a"), _enroll("race-host-b")
        )

        statuses = sorted([resp_a.status_code, resp_b.status_code])
        assert statuses == [201, 409], (
            f"expected exactly one 201 (winner) and one 409 (loser), got {statuses}"
        )

        # DB-level proof, not just the HTTP-layer proof above: the key was
        # consumed exactly once, and exactly one agent row exists for it.
        key_row = agent_pg_db(agent_pg_db.enrollment_keys.key_hash == key_hash).select().first()
        assert key_row.is_used is True

        agents = agent_pg_db(
            agent_pg_db.access_agents.enrollment_key_hash == key_hash
        ).select()
        assert len(agents) == 1
        assert key_row.used_by_agent == agents[0].id

    async def test_second_enroll_after_first_completes_is_rejected(
        self, agent_pg_db: Any, agents_pg_client: Any
    ) -> None:
        """Sequential (non-racing) control case: the second, later request
        against an already-consumed key must still get 409 -- the ordinary
        fast-path check, not just the race-path conditional UPDATE."""
        raw_key = "ENROLL-SEQUENTIAL-TEST-KEY-0002"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        admin_id = _seed_auth_user(agent_pg_db, email="sequential-admin@example.com")

        agent_pg_db.enrollment_keys.insert(
            key_hash=key_hash,
            created_by=admin_id,
            expires_at=expires_at,
            is_used=False,
            created_at=datetime.now(timezone.utc),
        )
        agent_pg_db.commit()

        first = await agents_pg_client.post(
            "/api/v1/agents/enroll",
            headers={"X-Enrollment-Key": raw_key},
            json={"hostname": "sequential-host-a", "capabilities": ["ssh"]},
        )
        assert first.status_code == 201

        second = await agents_pg_client.post(
            "/api/v1/agents/enroll",
            headers={"X-Enrollment-Key": raw_key},
            json={"hostname": "sequential-host-b", "capabilities": ["ssh"]},
        )
        assert second.status_code == 409
