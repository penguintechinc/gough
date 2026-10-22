"""``run_db()`` -- the house idiom for running a blocking penguin-dal unit off
the event loop.

Regression: gh-22 (event-loop-unblocking sweep). Quart route handlers are
``async def``, but penguin-dal is a synchronous SQLAlchemy wrapper -- every
``db(...).select()``/``.insert()``/``.update()`` call blocks the calling
thread on network I/O. ``app.grpc_server``'s servicers already worked around
this for gRPC (see that module's docstring): define a local closure
containing the WHOLE database unit of work -- every statement of one logical
transaction, never split across separate ``to_thread`` calls, since a
penguin-dal transaction/connection checkout is not safe to resume from a
different thread hop -- then hand that closure to ``run_db()``. This service
runs single-process, single-loop (``run.py``'s ``app.run_task``) with gRPC
sharing the same loop, so a handler that blocks synchronously stalls every
other request and every gRPC call until it returns; ``run_db()`` is the fix.

``app.db.rls``'s tenant ``ContextVar`` propagates through
``asyncio.to_thread()`` automatically (it copies the current
``contextvars.Context`` onto the worker thread -- see that module's
docstring), so RLS/tenant scoping set by the tenant middleware before a
handler calls ``run_db()`` is still in effect inside the closure.

Fix-round (gh-22): a closure boundary is a thread boundary, NOT a
transaction boundary. Only ``db.transaction()`` is atomic; a bare
``db.commit()`` after a sequence of auto-committing ``.update()``/
``.insert()`` calls is a no-op as far as cross-request atomicity goes --
each individual statement already committed itself the moment it ran.
Before this sweep, a synchronous handler blocked the *entire* single-loop
process for its whole duration, which had the side effect of serializing
every "read a value, decide, write based on it" sequence against every
other concurrent request -- accidentally, not by design. ``run_db()``
removes that accidental serialization: two concurrent requests' closures
can now genuinely interleave against the database (real OS threads via
``asyncio.to_thread``, potentially true DB-level concurrency). Any
predicate that *gates* a write -- "only insert if this key isn't already
used", "only revoke if not already revoked", "only rename if no other row
already has this name" -- MUST be re-checked by the write itself, in the
SAME closure, not by a separate read earlier (whether that read is a
prior statement in the same closure with a hop in between, or worse, a
whole separate ``run_db()`` call). Two shapes both work:

* A conditional ``UPDATE ... WHERE <predicate>`` whose rowcount you check
  before proceeding -- Postgres serializes concurrent UPDATEs to the same
  row, so at most one concurrent caller's UPDATE actually matches.
* The whole read-decide-write sequence inside one ``db.transaction()``
  block, relying on a real DB-level constraint (unique index, etc.) to
  reject the losing writer -- catch the resulting exception and translate
  it to the same conflict response, same as the constraint were checked
  up front.

A plain "SELECT to check, then UPDATE unconditionally" -- even entirely
within one closure -- is not enough on its own unless one of the above
backs it: the SELECT and the UPDATE are two round trips, and nothing
stops a second closure (on a second thread) from running its own SELECT
before the first closure's UPDATE commits. See ``app/api/agents.py``'s
``enroll_agent`` (conditional-UPDATE-on-rowcount claiming a single-use
enrollment key), ``app/api/joiner_secrets.py``'s ``rotate_joiner_secret``
(conditional UPDATE inside a ``db.transaction()``), and
``app/api/nodes.py``'s ``patch_node`` (fast-path SELECT + a real unique
constraint as the authoritative guard) for the three shapes this class of
bug takes in practice.

Usage::

    def _do_update() -> tuple[bool, str]:
        # ALL statements of one transaction go here -- never split a unit
        # of work across two run_db()/to_thread() calls.
        row = db(db.nodes.id == node_id).select().first()
        db(db.nodes.id == node_id).update(state="ready")
        db.commit()
        return True, str(row.id)

    updated, node_id = await run_db(_do_update)
"""

from __future__ import annotations

import asyncio
from typing import Callable, TypeVar

T = TypeVar("T")


async def run_db(fn: Callable[[], T]) -> T:
    """Run a blocking penguin-dal unit of work off the event loop.

    ``fn`` must be a zero-argument closure wrapping the WHOLE DB unit (every
    statement of one logical transaction) -- never split a single transaction
    across multiple ``run_db()``/``to_thread()`` calls, since the thread a
    penguin-dal connection is checked out on cannot be resumed later.
    """
    return await asyncio.to_thread(fn)


__all__ = ["run_db"]
