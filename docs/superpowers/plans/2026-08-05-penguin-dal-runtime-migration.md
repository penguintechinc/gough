# penguin-dal Runtime Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move gough's ~73 direct-SQLAlchemy runtime call sites onto the penguin-dal overlay, add the two overlay primitives that makes that possible (`executesql()` + `transaction()`), stand up real-Postgres tests, and enforce RLS tenant isolation (#7a).

**Architecture:** Three stacked phases across two repos. Phase 1 adds `executesql()` and a `transaction()` context manager to penguin-dal (`~/code/penguin-libs`, branch `release/python-dal/v0.4.x`, released as 0.4.0). Phase 2 (gough, branch `feature/penguin-dal-runtime-migration`) converts runtime queries — relational → `db(...).select()`, single-statement raw → `db.executesql()`, multi-statement raw units → `db.transaction()` — adds a real-Postgres pytest fixture, and wires RLS. Phase 3 publishes penguin-dal to PyPI (human/CI) and repins gough. Gough work develops against the **editable** penguin-dal install so it isn't blocked on the publish.

**Tech Stack:** Python 3.12/3.13, Quart, penguin-dal (PyDAL-over-SQLAlchemy), SQLAlchemy 2.0 + Alembic (schema only), psycopg2, pytest, testcontainers-python, Postgres 16.

## Global Constraints

- **Schema authority stays SQLAlchemy+Alembic** — never move model definitions or migrations to penguin-dal. Runtime queries only.
- **Sync + thread-local** DB pattern preserved; blocking DB calls in async handlers stay wrapped in `asyncio.to_thread`. No async DB rearchitecture.
- **Parameterized SQL only** — raw SQL binds via `placeholders`/bound params; never f-string/`%`/`.format` of caller input.
- **penguin-dal `executesql` uses DRIVER-NATIVE paramstyle** — `%(name)s` (psycopg2/Postgres, the runtime + test DB), `%s` positional, or `?` (sqlite). NEVER SQLAlchemy `:name`. Every converted raw-SQL string and every `Tx.executesql` call uses `%(name)s` for the Postgres runtime. `executesql` already exists on `release/python-dal/v0.4.x` — do not reimplement it.
- **penguin-dal target version: 0.4.0** on branch `release/python-dal/v0.4.x`; gough repins `penguin-dal==0.3.0` → `0.4.0` in Phase 3.
- **Every commit runs its task's tests green** before moving on. Backup-push each branch after every commit.
- Python invoked as `python3` everywhere (never bare `python`).
- Type hints on new/changed functions; `mypy --strict` clean for touched files.
- gough feature branch: `feature/penguin-dal-runtime-migration` (already created, off `release/v1.0.X`).
- penguin-libs work: branch off `release/python-dal/v0.4.x` as `feature/executesql` (PR back into the release line).

---

## Phase 1 — penguin-dal primitives (repo: `~/code/penguin-libs`)

### Task 1: Validate existing `executesql` + lock the paramstyle contract

`executesql` **already ships** on `release/python-dal/v0.4.x` (`class DB` in `db.py`) with a
superset signature — **do NOT reimplement it.** This task validates it in the editable env and
ensures the driver-native paramstyle contract the gough conversion relies on is covered by a test.

**Files:**
- Test: `packages/python-dal/tests/test_executesql.py` (extend ONLY if a listed case is missing)

**Interfaces:**
- Consumes (pre-existing, do not modify): `DB.executesql(query, placeholders=None, as_dict=False, fields=None, colnames=None, as_ordered_dict=False, return_rowcount=False, check_injection=True)` — uses `conn.exec_driver_sql`, DRIVER-NATIVE paramstyle (`%(name)s`/`%s` psycopg2, `?` sqlite; NEVER `:name`). Returns `list[tuple]` for SELECT (dict/OrderedDict/`Rows` per flags), `None` for writes (or `cursor.rowcount` if `return_rowcount=True`).

- [ ] **Step 1: Run the existing suite**

Run: `cd ~/code/penguin-libs/packages/python-dal && python3 -m pytest tests/test_executesql.py -v`
Expected: PASS.

- [ ] **Step 2: Confirm paramstyle + shaping coverage**

Confirm `tests/test_executesql.py` covers, on sqlite (`?` paramstyle): a positional-placeholder SELECT returning rows, a write returning `None`, `as_dict=True`, and `return_rowcount=True`. If any is missing, ADD a minimal test for it (sqlite `?`; do NOT require a live Postgres here).

```python
# add only if missing — sqlite positional paramstyle:
def test_executesql_positional_and_rowcount(tmp_path):
    from penguin_dal import DB
    d = DB(f"sqlite:///{tmp_path/'t.db'}", pool_size=1, reflect=False)
    d.executesql("CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT)")
    assert d.executesql("INSERT INTO widget (name) VALUES (?)", ("a",)) is None
    assert d.executesql("SELECT name FROM widget WHERE name = ?", ("a",)) == [("a",)]
    assert d.executesql("SELECT name FROM widget", as_dict=True) == [{"name": "a"}]
    assert d.executesql("UPDATE widget SET name = ? WHERE name = ?", ("b", "a"),
                        return_rowcount=True) == 1
```

- [ ] **Step 3: Commit only if a test was added**

If Step 2 added a test: `git add tests/test_executesql.py && git commit -m "test(dal): cover executesql paramstyle + rowcount contract" && git push`.
If nothing was missing: report `DONE_WITH_CONCERNS` with "executesql already complete — no change" (no commit).

---

### Task 2: `DB.transaction()` context manager (+ shared result-shaping helper)

Net-new. Must be **consistent with the existing `executesql`**: driver-native paramstyle via `conn.exec_driver_sql`, same row shaping. Extract the shaping into a module helper and have BOTH `executesql` and `Tx.executesql` call it (DRY — the reviewer will flag duplicated shaping otherwise).

**Files:**
- Modify: `packages/python-dal/src/penguin_dal/db.py` (add module-level `_shape_result` helper + `Tx` class above `class DB`; add `transaction()` to `class DB`; refactor the existing `executesql` result-shaping block to call `_shape_result`)
- Modify: `packages/python-dal/pyproject.toml` (confirm version is `0.4.0`)
- Test: `packages/python-dal/tests/test_transaction.py` (create)

**Interfaces:**
- Consumes: existing `executesql` execution style (`conn.exec_driver_sql(query, placeholders)`).
- Produces: `DB.transaction()` — context manager yielding `Tx` with `Tx.executesql(query, placeholders=None, as_dict=False, return_rowcount=False) -> list | int | None`. All statements run on ONE pinned connection in ONE transaction; commit on clean exit, rollback on exception. Same driver-native paramstyle as `DB.executesql`.

- [ ] **Step 1: Write the failing tests** (sqlite, `?` paramstyle)

```python
# packages/python-dal/tests/test_transaction.py
import pytest
from penguin_dal import DB

@pytest.fixture
def db(tmp_path):
    d = DB(f"sqlite:///{tmp_path/'t.db'}", pool_size=1, reflect=False)
    d.executesql("CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT)")
    return d

def test_transaction_commits_on_clean_exit(db):
    with db.transaction() as tx:
        tx.executesql("INSERT INTO widget (name) VALUES (?)", ("a",))
        tx.executesql("INSERT INTO widget (name) VALUES (?)", ("b",))
    assert db.executesql("SELECT count(*) FROM widget") == [(2,)]

def test_transaction_rolls_back_on_exception(db):
    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            tx.executesql("INSERT INTO widget (name) VALUES (?)", ("a",))
            raise RuntimeError("boom")
    assert db.executesql("SELECT count(*) FROM widget") == [(0,)]

def test_transaction_statements_share_one_connection(db):
    with db.transaction() as tx:
        tx.executesql("CREATE TEMPORARY TABLE t (x INTEGER)")
        tx.executesql("INSERT INTO t VALUES (1)")
        assert tx.executesql("SELECT x FROM t") == [(1,)]
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_transaction.py -v`
Expected: FAIL — `AttributeError: 'DB' object has no attribute 'transaction'`

- [ ] **Step 3: Implement**

```python
# near top of db.py imports:
from contextlib import contextmanager
from collections.abc import Iterator

# module-level, above class DB — shared by DB.executesql and Tx.executesql:
def _shape_result(result, as_dict=False, as_ordered_dict=False,
                  fields=None, colnames=None, return_rowcount=False):
    from collections import OrderedDict
    from penguin_dal.query import Row, Rows
    if not result.returns_rows:
        if return_rowcount:
            return result.rowcount if result.rowcount is not None else -1
        return None
    rows = result.fetchall()
    col_names = list(result.keys())
    field_list = fields or colnames
    if as_ordered_dict:
        return [OrderedDict(zip(col_names, r)) for r in rows]
    if as_dict:
        return [dict(zip(col_names, r)) for r in rows]
    if field_list:
        custom = [getattr(f, "name", None) or str(f) for f in field_list]
        return Rows([Row(dict(zip(custom, r))) for r in rows])
    return [tuple(r) for r in rows]

class Tx:
    """Raw-SQL executor bound to one open connection inside a transaction."""
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def executesql(self, query: str, placeholders=None, as_dict: bool = False,
                   return_rowcount: bool = False):
        if placeholders is not None:
            result = self._conn.exec_driver_sql(query, placeholders)
        else:
            result = self._conn.exec_driver_sql(query)
        return _shape_result(result, as_dict=as_dict, return_rowcount=return_rowcount)

# in class DB:
@contextmanager
def transaction(self) -> Iterator[Tx]:
    """Pin one connection for a multi-statement raw-SQL unit.

    Commits on clean exit, rolls back on exception. Use for advisory locks,
    hash-chain writes, and any raw unit whose statements must share session
    state (a naive per-call executesql would drop that state).
    """
    conn = self._engine.connect()
    trans = conn.begin()
    try:
        yield Tx(conn)
        trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()
```

Then **refactor the existing `executesql`**: replace its result-shaping block (from `if not result.returns_rows:` through the final `return [tuple(row) for row in rows]`) with `return _shape_result(result, as_dict=as_dict, as_ordered_dict=as_ordered_dict, fields=fields, colnames=colnames, return_rowcount=return_rowcount)`. Behavior is unchanged — existing `test_executesql.py` must still pass.

- [ ] **Step 4: Run to verify pass + confirm version**

Run: `python3 -m pytest tests/test_executesql.py tests/test_transaction.py -v && grep '^version' pyproject.toml`
Expected: PASS (existing executesql tests + 3 transaction tests); version shows `0.4.0`

- [ ] **Step 5: Commit**

```bash
git add packages/python-dal/src/penguin_dal/db.py packages/python-dal/tests/test_transaction.py packages/python-dal/pyproject.toml
git commit -m "feat(dal): add DB.transaction() multi-statement raw-SQL context (0.4.0)"
git push
```

- [ ] **Step 6: Open PR into the release line**

```bash
gh pr create --base release/python-dal/v0.4.x --head feature/executesql \
  --title "feat(dal): executesql() + transaction() raw-SQL primitives (0.4.0)" \
  --body "Adds PyDAL-compatible raw-SQL primitives needed by gough's runtime migration. Autocommit executesql() + pinned-connection transaction() for advisory-lock/hash-chain units."
```

---

## Phase 2 — gough conversion (repo: `~/code/gough`, branch `feature/penguin-dal-runtime-migration`)

> Develop against the editable penguin-dal (0.4.0 working tree). Confirm `python3 -c "from penguin_dal import DB; DB.executesql; DB.transaction"` succeeds before starting.

### Task 3: Real-Postgres pytest fixture

**Files:**
- Create: `services/api-manager/tests/pg_fixtures.py`
- Modify: `services/api-manager/tests/conftest.py` (add a `pg_db` fixture; keep existing sqlite fixtures for pure-logic tests)
- Modify: `services/api-manager/requirements.in` + recompile `requirements.txt` (add `testcontainers`)
- Test: `services/api-manager/tests/test_pg_fixture_smoke.py` (create)

**Interfaces:**
- Produces: session-scoped `pg_url` fixture (str `postgresql://...`) and function-scoped `pg_db` fixture (a penguin-dal `DB` bound to a real Postgres with the Alembic/SQLAlchemy schema created). Reuses `DATABASE_URL` when set (CI service container); otherwise spins an ephemeral container via testcontainers.

- [ ] **Step 1: Write the fixture module**

```python
# services/api-manager/tests/pg_fixtures.py
"""Real-Postgres test fixtures. Reuses $DATABASE_URL (CI service container)
when present; otherwise spins an ephemeral Postgres via testcontainers."""
import os
import pytest

@pytest.fixture(scope="session")
def pg_url():
    existing = os.getenv("DATABASE_URL")
    if existing:
        yield existing.replace("postgresql+psycopg2://", "postgresql://")
        return
    from testcontainers.postgres import PostgresContainer
    with PostgresContainer("postgres:16-bookworm") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")

@pytest.fixture
def pg_db(pg_url):
    """penguin-dal DB on real PG with the SQLAlchemy schema created."""
    from penguin_dal import DB
    from app.models_sqlalchemy import Base
    from sqlalchemy import create_engine, text
    eng = create_engine(pg_url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    Base.metadata.create_all(eng)          # schema authority = SQLAlchemy
    eng.dispose()
    db = DB(pg_url, pool_size=2, reflect=True)
    yield db
    db.close()
```

- [ ] **Step 2: Write a smoke test**

```python
# services/api-manager/tests/test_pg_fixture_smoke.py
from tests.pg_fixtures import pg_url, pg_db  # noqa: F401

def test_pg_db_is_real_postgres(pg_db):
    assert pg_db.executesql("SELECT 1") == [(1,)]
    # a reflected app table exists (schema built from SQLAlchemy Base)
    assert "nodes" in pg_db.tables
```

- [ ] **Step 3: Add dependency + recompile**

```bash
cd ~/code/gough/services/api-manager
grep -q '^testcontainers' requirements.in || echo 'testcontainers==4.8.1' >> requirements.in
uv pip compile requirements.in --generate-hashes -o requirements.txt
python3 -m pip install --require-hashes -r requirements.txt
```
(If `testcontainers==4.8.1` is unavailable, pin the current latest and note it.)

- [ ] **Step 4: Run the smoke test**

Run: `cd ~/code/gough/services/api-manager && python3 -m pytest tests/test_pg_fixture_smoke.py -v`
Expected: PASS (needs Docker locally, or `DATABASE_URL` exported)

- [ ] **Step 5: Commit**

```bash
git add services/api-manager/tests/pg_fixtures.py services/api-manager/tests/conftest.py \
        services/api-manager/tests/test_pg_fixture_smoke.py \
        services/api-manager/requirements.in services/api-manager/requirements.txt
git commit -m "test(api-manager): real-Postgres pytest fixture (testcontainers + DATABASE_URL reuse)"
git push
```

---

### Task 4: Convert `db/database.py` + `db/galera.py` raw SQL → `executesql`

**Files:**
- Modify: `services/api-manager/app/db/database.py:126-127` (`execute_query`)
- Modify: `services/api-manager/app/db/galera.py:108-109,215-217,240-241`
- Test: `services/api-manager/tests/test_db_layer_pg.py` (create)

**Interfaces:**
- Consumes: `DB.executesql` (Task 1).
- Produces: `execute_query(query, params=None)` delegates to `db.executesql`. Galera helpers issue `SET`/`SHOW STATUS` via `db.executesql`.

**Pattern (before → after):**
```python
# before
with db.engine.connect() as conn:
    result = conn.execute(text(query), params or {})
    return result.fetchall()
# after
return db.executesql(query, params, as_dict=False)
```
Paramstyle: `db.executesql` uses driver-native placeholders. Convert each raw query string and any callers of `execute_query` to `%(name)s` (Postgres) — never `:name`. Audit `execute_query` callers and update their SQL + params in the same task.

- [ ] **Step 1: Failing test**

```python
# services/api-manager/tests/test_db_layer_pg.py
from tests.pg_fixtures import pg_url, pg_db  # noqa: F401
from app.db import database

def test_execute_query_uses_executesql(pg_db, monkeypatch):
    monkeypatch.setattr(database, "get_db", lambda: pg_db)
    assert database.execute_query("SELECT 1") == [(1,)]
```

- [ ] **Step 2: Run — expect FAIL** (`execute_query` still calls `db.engine`)

Run: `python3 -m pytest tests/test_db_layer_pg.py -v`

- [ ] **Step 3: Apply the pattern** to `execute_query` and each galera `SET`/`SHOW` site (read each file; replace `db.engine.connect()/text()` with `db.executesql(...)`, keeping the exact SQL and params).

- [ ] **Step 4: Run — expect PASS**, plus existing galera unit tests:

Run: `python3 -m pytest tests/test_db_layer_pg.py tests/ -k "galera or database" -v`

- [ ] **Step 5: Commit**

```bash
git add services/api-manager/app/db/database.py services/api-manager/app/db/galera.py services/api-manager/tests/test_db_layer_pg.py
git commit -m "refactor(db): route database.py/galera.py raw SQL through penguin-dal executesql"
git push
```

---

### Task 5: Convert health/ready routes → `executesql`

**Files:**
- Modify: `services/api-manager/app/__init__.py:231-232,248-249,311-312,329-330`
- Test: `services/api-manager/tests/test_health_pg.py` (create)

- [ ] **Step 1: Failing test**

```python
# services/api-manager/tests/test_health_pg.py
import pytest
from tests.pg_fixtures import pg_url, pg_db  # noqa: F401

@pytest.mark.asyncio
async def test_health_ok_with_real_db(pg_db, monkeypatch):
    from app import __init__ as appmod  # adjust import to the module exposing create_app
    app = await appmod.create_app("app.config.TestingConfig")
    app.config["db"] = pg_db
    client = app.test_client()
    resp = await client.get("/health")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run — expect FAIL** (routes still use `db.engine.connect()`)
- [ ] **Step 3: Replace each `SELECT 1` liveness check** with `app.config["db"].executesql("SELECT 1")`; keep the 503-when-db-None behavior.
- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit**

```bash
git add services/api-manager/app/__init__.py services/api-manager/tests/test_health_pg.py
git commit -m "refactor(health): route liveness SELECT 1 through executesql"
git push
```

---

### Task 6: Convert Category-A relational ORM — API layer

**Files:**
- Modify: `services/api-manager/app/api/audit.py:237,350`; `app/api/joiner_secrets.py:242,294,383,419`; `app/api/primary.py:759,916`; `app/api/webhooks.py:82,171,228,261,285`
- Test: extend `services/api-manager/tests/api/` (per-file, real PG via `pg_db`)

**Pattern (SQLAlchemy ORM → penguin-dal relational):**
```python
# before:  rows = db.query(JoinerSecret).filter(JoinerSecret.cluster_id == cid).all()
# after:   rows = db(db.joiner_secrets.cluster_id == cid).select()
# before:  db.query(AuditEvent).filter(...).yield_per(500)   # streaming export
# after:   db(db.audit_events.<cond>).select()  # if the set is bounded; else
#          paginate with .select(limitby=(start, start+500)) in a loop.
```
Notes: reflected table attribute is the SQL table name (`db.joiner_secrets`, `db.nodes`), not the model class. Webhook CRUD (`text()` INSERT/UPDATE/DELETE) becomes `db.webhooks.insert(**vals)` / `db(db.webhooks.id==x).update(**vals)` / `.delete()`.

- [ ] **Step 1: For each file, write/extend a real-PG test** asserting the endpoint's DB behavior (seed rows via `pg_db.<table>.insert(...)`, call the handler, assert result). Cover a filter + an empty-result case per the API-filter-coverage rule.
- [ ] **Step 2: Run — expect FAIL / current behavior**
- [ ] **Step 3: Read each file; apply the pattern** at the listed lines. Remove now-unused SQLAlchemy `text`/session imports.
- [ ] **Step 4: Run the touched api tests on real PG — expect PASS**
- [ ] **Step 5: Commit** (`refactor(api): route audit/joiner/primary/webhooks reads through penguin-dal`), push.

---

### Task 7: Convert Category-A relational ORM — gRPC + workers

**Files:**
- Modify: `services/api-manager/app/grpc_server.py:310,339,386,435,465,505,768,775,847,855,1021`; `app/workers/joiner_secret_emitter.py:545,559,631,646,660,682`; `app/workers/plan_compiler.py:1336,1628,1649`; `app/models/__init__.py:70`
- Test: extend `services/api-manager/tests/workers/` + a grpc servicer test (real PG)

**Pattern:** same as Task 6. The two `grpc_server` `text()` bulk statements (768/775 node, 847/855 biome) are multi-row reads → `db(<cond>).select()`; if they're genuine bulk upserts requiring one transaction, use `db.transaction()` (Task 2) instead.

- [ ] **Step 1: Real-PG tests** for the converted servicer methods and worker queries (seed, invoke, assert).
- [ ] **Step 2: Run — expect FAIL/current**
- [ ] **Step 3: Apply the pattern** per site; drop unused SQLAlchemy imports.
- [ ] **Step 4: Run touched tests on real PG — expect PASS**
- [ ] **Step 5: Commit** (`refactor(grpc,workers): route relational reads through penguin-dal`), push.

---

### Task 8: Convert Category-B multi-statement raw units → `transaction()` / `executesql`

**Files:**
- Modify: `services/api-manager/app/workers/leader_lease.py:123,135,158,182,224,260,284`; `app/workers/audit_chain_writer.py:291,301,501,535,601,616,630,645`; `app/security/audit_chain.py:136,169-180,252,303-304,325-334`; `app/workers/smart_sweeper.py:231,247`; `app/workers/webhook_dispatcher.py:224-225`; `app/api/vault.py:62,70`
- Test: `services/api-manager/tests/workers/test_leader_lease_pg.py`, `test_audit_chain_pg.py` (create, real PG)

**Pattern (advisory-lock unit must hold the lock across statements → one transaction):**
```python
# before (db_session.execute(text(...)) across several statements)
# after (Postgres paramstyle = %(name)s, NOT :name):
with db.transaction() as tx:
    tx.executesql("SELECT pg_advisory_xact_lock(%(k)s)", {"k": lock_key})
    rows = tx.executesql("SELECT holder, expires_at FROM leader_lease WHERE name=%(n)s", {"n": name})
    tx.executesql("INSERT INTO leader_lease (...) VALUES (%(a)s, %(b)s) ON CONFLICT ... DO UPDATE ...", {...})
# (pg_advisory_xact_lock auto-releases at transaction end — no manual unlock)
```
Single-statement raw sites (`vault.py`, `smart_sweeper.py`) use `db.executesql(...)` directly.

- [ ] **Step 1: Real-PG tests** — leader-lease acquire/renew/release across two `pg_db` handles proves mutual exclusion; audit-chain test proves hash links across two appended rows.
- [ ] **Step 2: Run — expect FAIL/current**
- [ ] **Step 3: Apply the transaction/executesql pattern** per site; remove `db_session`/`text`/`engine` runtime usage.
- [ ] **Step 4: Run — expect PASS on real PG**
- [ ] **Step 5: Commit** (`refactor(workers,security): route advisory-lock/hash-chain units through db.transaction`), push.

---

### Task 9: #7a — RLS tenant enforcement on the real connection

**Files:**
- Create: `services/api-manager/app/db/rls.py` (pool-event tenant GUC wiring + thread-local setter)
- Modify: `services/api-manager/app/security/tenant.py:129-130` (set the thread-local tenant instead of a throwaway `db.engine.connect()`)
- Modify: `services/api-manager/app/db/database.py` (call `install_rls_events(db.engine)` once at DB init)
- Create: `services/api-manager/app/alembic/versions/<rev>_rls_tenant_policies.py` (ENABLE ROW LEVEL SECURITY + policy `USING (tenant_id = current_setting('app.current_tenant', true))` on `biomes`, `clusters`, `nodes`)
- Test: `services/api-manager/tests/security/test_rls_two_tenant_pg.py` (create)

**Interfaces:**
- Produces: `install_rls_events(engine)` — registers SQLAlchemy pool `checkout`/`checkin` listeners that run `SELECT set_config('app.current_tenant', :t, false)` from a thread-local, and `RESET` on checkin. `set_current_tenant(tenant_id: str | None)` — sets the thread-local read by the checkout listener.

- [ ] **Step 1: Write the two-tenant failing test**

```python
# services/api-manager/tests/security/test_rls_two_tenant_pg.py
from tests.pg_fixtures import pg_url, pg_db  # noqa: F401
from app.db import rls

def test_tenant_cannot_read_other_tenants_rows(pg_db):
    rls.install_rls_events(pg_db.engine)
    pg_db.executesql(
        "ALTER TABLE biomes ENABLE ROW LEVEL SECURITY; "
        "CREATE POLICY t ON biomes USING (tenant_id = current_setting('app.current_tenant', true));"
    )
    pg_db.executesql("INSERT INTO biomes (id, tenant_id, name) VALUES (:i,:t,:n)",
                     {"i": "b1", "t": "tenantA", "n": "a"})
    pg_db.executesql("INSERT INTO biomes (id, tenant_id, name) VALUES (:i,:t,:n)",
                     {"i": "b2", "t": "tenantB", "n": "b"})
    rls.set_current_tenant("tenantA")
    assert pg_db(pg_db.biomes.id != "").select()  # only tenantA rows
    rows = pg_db(pg_db.biomes.tenant_id == "tenantB").select()
    assert rows == []          # cannot see tenantB even by explicit filter
    rls.set_current_tenant(None)
```

- [ ] **Step 2: Run — expect FAIL** (`app.db.rls` missing)

- [ ] **Step 3: Implement `rls.py`**

```python
# services/api-manager/app/db/rls.py
"""Postgres RLS wiring: apply a thread-local tenant GUC to every pooled
connection at checkout so RLS policies see the request's tenant regardless
of which connection a query lands on. Engine configuration, not a runtime query."""
import threading
from sqlalchemy import event

_local = threading.local()

def set_current_tenant(tenant_id: str | None) -> None:
    _local.tenant = tenant_id

def _current_tenant() -> str | None:
    return getattr(_local, "tenant", None)

def install_rls_events(engine) -> None:
    @event.listens_for(engine, "checkout")
    def _set_tenant(dbapi_conn, conn_record, conn_proxy):
        t = _current_tenant()
        cur = dbapi_conn.cursor()
        try:
            cur.execute("SELECT set_config('app.current_tenant', %s, false)", (t or "",))
        finally:
            cur.close()
    @event.listens_for(engine, "checkin")
    def _reset_tenant(dbapi_conn, conn_record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("RESET app.current_tenant")
        finally:
            cur.close()
```

- [ ] **Step 4: Wire it** — call `install_rls_events(db.engine)` once in `db/database.py` `init_db()`; change `security/tenant.py` to `rls.set_current_tenant(<jwt tenant claim>)` (remove the throwaway `engine.connect()`); author the Alembic migration enabling RLS + policies on the three tenant tables.

- [ ] **Step 5: Run — expect PASS**

Run: `python3 -m pytest tests/security/test_rls_two_tenant_pg.py -v`

- [ ] **Step 6: Commit**

```bash
git add services/api-manager/app/db/rls.py services/api-manager/app/security/tenant.py \
        services/api-manager/app/db/database.py services/api-manager/app/alembic/versions/ \
        services/api-manager/tests/security/test_rls_two_tenant_pg.py
git commit -m "fix(security): enforce RLS tenant GUC on the request connection (#7a)"
git push
```

---

### Task 10: Guard test + lint/type gate

**Files:**
- Create: `services/api-manager/tests/test_no_direct_sqlalchemy_runtime.py`

**Interfaces:**
- Produces: a test that greps `app/` for runtime `db.engine.connect(` / `db.query(` / `db_session.execute(` / `.execute(text(` outside an allowlist (`models_sqlalchemy.py`, `db/init_db.py`, `alembic/`, `db/rls.py`), failing if any remain.

- [ ] **Step 1: Write the guard test**

```python
# services/api-manager/tests/test_no_direct_sqlalchemy_runtime.py
import pathlib, re
ALLOW = {"models_sqlalchemy.py", "init_db.py", "rls.py"}
BAD = re.compile(r"\.engine\.connect\(|db\.query\(|db_session\.execute\(|\.execute\(\s*text\(")

def test_no_direct_sqlalchemy_runtime():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for p in root.rglob("*.py"):
        if p.name in ALLOW or "/alembic/" in str(p):
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if BAD.search(line):
                offenders.append(f"{p.relative_to(root)}:{i}")
    assert not offenders, "Direct SQLAlchemy runtime usage remains:\n" + "\n".join(offenders)
```

- [ ] **Step 2: Run — fix any stragglers** it flags (convert or add to the allowlist only if genuinely schema/config code).
- [ ] **Step 3: Lint + types**

Run: `cd ~/code/gough && make lint && cd services/api-manager && python3 -m mypy --strict app/db/rls.py app/db/database.py`

- [ ] **Step 4: Commit** (`test: guard against direct-SQLAlchemy runtime usage`), push.

---

### Task 11: Full suite on real Postgres

- [ ] **Step 1:** Export a test DB URL (or rely on testcontainers/Docker) and run the whole api-manager suite:

Run: `cd ~/code/gough && make test` (ensure the api-manager tests bind to real PG)
Expected: green, including the 5 previously-failing modules once penguin-dal resolves.

- [ ] **Step 2:** Coverage ≥90% for touched modules; fix gaps.
- [ ] **Step 3: Commit** any test additions, push.

---

## Phase 3 — release coordination

### Task 12: Publish penguin-dal 0.4.0 + repin gough

- [ ] **Step 1 (human/CI):** Merge the penguin-libs PR (Task 2) and publish `penguin-dal 0.4.0` to PyPI via the library's release CI. **Claude cannot publish — this step is the human/CI handoff.**
- [ ] **Step 2:** Repin gough once 0.4.0 is on PyPI:

```bash
cd ~/code/gough
sed -i 's/^penguin-dal==0.3.0/penguin-dal==0.4.0/' requirements.in services/api-manager/requirements.in
uv pip compile requirements.in --generate-hashes -o requirements.txt
cd services/api-manager && uv pip compile requirements.in --generate-hashes -o requirements.txt
```

- [ ] **Step 3:** Verify a clean hashed install: `python3 -m pip install --require-hashes -r services/api-manager/requirements.txt` and re-run `make test`.
- [ ] **Step 4: Commit** (`build: repin penguin-dal 0.3.0 -> 0.4.0 with real hashes`), push.
- [ ] **Step 5:** Open the gough PR into `release/v1.0.X` (auto-merge when green per the release gate).

---

## Self-Review

**Spec coverage:** executesql (T1) ✓; transaction/rollback (T2) ✓; version 0.4.0 (T2) ✓; Category-A conversion (T6,T7) ✓; Category-B raw (T4,T5,T8) ✓; test infra (T3,T11) ✓; #7a RLS (T9) ✓; guard/zero-direct-SQLAlchemy done-criterion (T10) ✓; publish+repin (T12) ✓; the 5 failing modules (T11,T12) ✓. Risk "yield_per streaming" addressed in T6 pattern. No spec section unmapped.

**Placeholder scan:** Bulk-conversion tasks (T6–T8) intentionally give the transformation pattern + exact site list rather than fabricated per-site test bodies the author cannot know without reading each file — the per-task subagent reads the file and applies the shown pattern with a real-PG test. All novel/tricky code (executesql, transaction, fixture, rls) is concrete. No "TBD/handle-edge-cases" left.

**Type/name consistency:** `executesql(query, placeholders=None, as_dict=False)` and `transaction() -> Tx.executesql(...)` used identically across T1,T2,T4,T5,T8,T9. `install_rls_events`/`set_current_tenant` names match between T9 impl and test. Reflected-table access (`db.<tablename>`) consistent across T6–T9.
