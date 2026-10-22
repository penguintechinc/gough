# Design: Runtime DB migration to penguin-dal (remove direct SQLAlchemy)

- **Date:** 2026-08-04
- **Status:** Approved (design) — pending spec review
- **Owner:** Justin Bowen
- **Repos touched:** `gough` (primary), `penguin-libs` (`packages/python-dal`)
- **Tracks:** remediation item #23 (runtime SQLAlchemy → penguin-dal) + #7a (RLS tenant enforcement)

## Problem

`gough`'s api-manager runs ~200+ relational queries through penguin-dal already, but **~73 runtime call sites still hit SQLAlchemy directly** — via `db.engine.connect()`, `db.query(Model)` (SQLAlchemy ORM), or `text(...)` executed on the raw engine. penguin-dal is a PyDAL-compatible overlay *over* SQLAlchemy; the intent is that **all runtime DB access flows through the overlay**, with SQLAlchemy+Alembic retained only as the schema/migration authority.

Two facts block a clean migration:

1. **No raw-SQL path exists through the overlay.** penguin-dal `DB` (through the 0.4.0-dev working tree) exposes only `define_table / engine / metadata / register_model / register_validators / tables / commit / close`. There is no PyDAL-style `executesql()` and no `rollback()`. The ~25-30 backend-specific statements (Galera session vars, Postgres advisory locks, RLS `set_config`, audit hash-chain) have no expressible `.select()` form, so today they can *only* run on `db.engine`.
2. **The pin is broken.** Both requirements files pin `penguin-dal==0.3.0` with hashes, but PyPI has only 0.1.0 (0.2.x/0.3.0 were git-tagged, never published). CI cannot reproducibly install the pinned artifact — the root cause of the version-mismatch test-module failures.

## Goals

- Zero direct-SQLAlchemy runtime call sites in gough (`db.engine` / `db.query` / `text()` for runtime queries all removed).
- All raw/backend-specific SQL runs through a new penguin-dal `executesql()` on the overlay's own pooled/thread-local connection.
- penguin-dal published as a real, installable release; gough repinned to it.
- DB-touching tests run against a **real Postgres** spun up during the test session — finally exercising RLS / advisory-lock / Galera paths that SQLite cannot.
- **#7a**: RLS tenant GUC (`app.current_tenant`) enforced on the actual request connection, proven by a two-tenant cross-isolation test.

## Non-goals

- No async DB rearchitecture — penguin-dal 0.x is sync; keep the existing sync + thread-local pattern and `asyncio.to_thread` wrapping.
- No schema/model rewrites — SQLAlchemy declarative models + Alembic migrations stay exactly as the schema authority.
- Not the Go→Rust migration (separate sub-project, separate spec).
- Not the other deferred items (#24 gRPC `api_version`, plan-compiler deploy wiring) — separate work.

## Architecture — 3 phases, stacked across 2 repos

| Phase | Repo | Deliverable | Gated on |
|-------|------|-------------|----------|
| **1. Library** | `penguin-libs` @ `release/python-dal/v0.4.x` | `executesql()` + `rollback()` on `DB`, unit tests, version `0.4.0` | — |
| **2. Conversion** | `gough` @ `feature/penguin-dal-runtime-migration` | ~73 sites converted; real-PG test infra; #7a RLS fix | Phase 1 (developed against **editable** install) |
| **3. Release** | both | Publish penguin-dal 0.4.0 to PyPI (**human/CI action**); repin gough `0.3.0 → 0.4.0` with real hashes | Phases 1-2 green |

Phase 2 develops against the editable penguin-dal working tree, so gough work is not blocked on the PyPI publish. The repin (Phase 3) is the final step and is the only part requiring credentials I do not have.

## Phase 1 — penguin-dal `executesql()` / `rollback()`

PyDAL-compatible so it reads as completing the overlay, not a bespoke addition:

```python
def executesql(self, query, placeholders=None, as_dict=False, fields=None):
    """Execute raw SQL on the overlay's managed connection.
    Returns list of rows for SELECT-shaped results, else None.
    placeholders: sequence/dict bound as parameters (never string-interpolated)."""

def rollback(self):
    """Roll back the current transaction on the managed connection."""
```

- **Parameterized only** — `placeholders` bound through SQLAlchemy's parameter binding; no f-string/`%`/`.format` of user input. Ships with a SQL-injection regression test.
- Executes on the **same pooled/thread-local connection** the overlay manages, so raw and relational ops share one transaction/commit context.
- `as_dict` / `fields` mirror PyDAL semantics for row shaping.
- Lands on `release/python-dal/v0.4.x`; version bumped/tagged `penguin-dal-v0.4.0`.
- Full unit tests in penguin-libs (SELECT rows, non-SELECT `None`, params, rollback, injection attempt).

## Phase 2 — gough conversion (~73 sites, two categories)

Schema authority unchanged: penguin-dal constructs with `reflect=True` (already the case) and reflects the Alembic-built schema; no model rewrites.

**Category A — Relational → `db(...).select()/.insert()/.update()/.delete()`:**

| Site | Ref |
|------|-----|
| audit export ORM (`db.query(AuditEvent)`, `.yield_per`) | `api/audit.py:237,350` |
| joiner-secret ORM CRUD | `api/joiner_secrets.py:242,294,383,419` |
| primary node lookups | `api/primary.py:759,916` |
| webhook CRUD | `api/webhooks.py:82,171,228,261,285` |
| grpc joiner-secret ORM + bulk node/biome | `grpc_server.py:310,339,386,435,465,505,1021` |
| joiner-secret emitter ORM/inserts | `workers/joiner_secret_emitter.py:545,559,631,646,660,682` |
| plan-compiler node/biome SELECTs | `workers/plan_compiler.py:1336,1628,1649` |
| admin/schema count | `models/__init__.py:70` |

**Category B — Backend-specific → `db.executesql()`:**

| Site | Ref | Why raw |
|------|-----|---------|
| `execute_query` passthrough | `db/database.py:126` | becomes thin wrapper over `executesql` (or deleted) |
| Galera `SET`/`SHOW STATUS` | `db/galera.py:108,215,240` | session control, no relational form |
| health/ready `SELECT 1` | `app/__init__.py:231,248,311,329` | liveness probe |
| vault text SELECT | `api/vault.py:62,70` | evaluate: may be Category A |
| audit hash-chain | `security/audit_chain.py:136,169,252,303,325` | ordered integrity read/insert |
| RLS `set_config` (**#7a**) | `security/tenant.py:129` | session GUC — see #7a below |
| advisory locks + chain | `workers/audit_chain_writer.py:291,301,501,535,601,616,630,645` | `pg_advisory_lock` |
| leader lease | `workers/leader_lease.py:123,135,158,182,224,260,284` | advisory locks |
| smart-sweeper raw | `workers/smart_sweeper.py:231,247` | evaluate: may be Category A |
| webhook dispatcher text | `workers/webhook_dispatcher.py:224` | evaluate: may be Category A |

Final A/B assignment is confirmed per-site during implementation; "evaluate" sites become `.select()` if cleanly expressible, else `executesql`.

### #7a — RLS tenant enforcement

Today `security/tenant.py` sets `app.current_tenant` on a throwaway `db.engine.connect()` that closes before the request query runs, so Postgres RLS never applies. Fix: set the GUC via `executesql` on the **same managed connection** the request's queries use (thread-local), inside the request/transaction scope. Prove with a two-tenant real-PG test: tenant A cannot read tenant B's rows (0 rows / 403), and the GUC is reset/reapplied per request.

## Phase 2 — test infrastructure (real Postgres)

- DB-touching tests run against a **real Postgres**, created per session; schema built via SQLAlchemy `Base.metadata.create_all` (schema authority stays SQLAlchemy even in tests), then penguin-dal reflects it.
- Mechanism: **`testcontainers`-python** spawns an ephemeral Postgres locally via Docker; when `DATABASE_URL` is already set (CI's existing `postgres:16` service container), **detect and reuse it** instead of spawning. Rationale: no host pg binaries required, matches the org's testcontainers convention, and keeps CI fast. (`pytest-postgresql==5.0.0` is already pinned-but-unused; testcontainers is preferred but this is the one reversible tooling call.)
- Tests that genuinely need only pure logic may stay on SQLite; anything touching RLS / advisory locks / Galera / `executesql` runs on real PG.
- Playwright/artifact rules N/A (backend-only).

## Phase 3 — release coordination

1. Publish `penguin-dal 0.4.0` to PyPI — **human or CI action** (I cannot publish).
2. Regenerate gough requirements: `penguin-dal==0.4.0` in both `requirements.in` files; `uv pip compile --generate-hashes` → real `--hash=sha256` in both `requirements.txt`. Removes the phantom 0.3.0 hashes.
3. Confirm the 5 version-mismatch test modules pass once the real artifact is installed.

## Risks & edge cases

- **`yield_per` streaming export** (`audit.py:350`): penguin-dal `.select()` may not offer server-side streaming; if not, keep as `executesql` with a cursor, or paginate. Decide during implementation.
- **Transaction boundaries**: `executesql` must share the overlay's connection/commit — verified by a test that mixes a relational insert and a raw statement in one transaction and rolls back.
- **Galera path** is env-gated; real-PG tests won't exercise WSREP. Cover Galera `executesql` calls with unit tests asserting the emitted SQL, not live WSREP behavior.
- **Chicken-and-egg on the pin**: local/CI dev uses editable penguin-dal until 0.4.0 is on PyPI; document that Phase 3 repin is the gating handoff.

## Verification / done criteria

- penguin-libs: new `executesql`/`rollback` unit tests green, including a parameterization/injection regression test.
- gough: `grep` asserts **zero** runtime `db.engine` / `db.query` / raw `text()` call sites remain (allowlist only schema/Alembic files).
- gough: `make lint && make test` green **against real Postgres**.
- #7a: two-tenant cross-isolation test passes on real PG.
- Phase 3: gough installs cleanly from the repinned requirements; the 5 previously-failing test modules pass.

## Out of scope (tracked elsewhere)

- Go→Rust rewrite of `discovery-agent` + `cli/gough` (next sub-project).
- #24 gRPC `api_version`; plan-compiler deploy wiring (issue #19).
