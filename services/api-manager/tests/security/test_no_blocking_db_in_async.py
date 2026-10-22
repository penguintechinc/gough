"""Regression guard: gh-22 (event-loop-unblocking sweep, Parts 1-4).

No direct, synchronous penguin-dal execution may appear directly inside an
``async def`` function body -- every blocking penguin-dal call must run
inside a ``run_db()`` (or bare ``asyncio.to_thread()``) closure, per the
house pattern documented in ``app/db/run_db.py``. This service is
single-process/single-loop (gRPC shares it -- see ``app/grpc_server.py``),
so a synchronous multi-millisecond DB call sitting directly in a coroutine
stalls every other request and RPC until it returns.

Detection strategy (AST, not line-count/regex):

Walk every ``async def`` in ``app/`` (at any nesting depth, including
decorator-produced wrapper coroutines such as ``require_auth``'s
``decorated_function`` or ``_require_cluster_tenant``'s ``_wrapper``).
Within each async function's own statements -- but *not* descending into a
nested ``def``/``lambda`` (that's precisely the run_db()/to_thread()
closure the house pattern hands off to a worker thread) -- track which
local names are penguin-dal handles:

* ``db`` and ``get_db`` are always treated as handles -- ``db`` by the
  codebase's universal convention for the value returned by ``get_db()``,
  and ``get_db`` itself so a chained ``get_db()(...).select()`` (skipping
  the usual ``db = get_db()`` assignment) is still caught.
* Any name assigned ``= get_db()``.
* Any name assigned a bare call on an already-tracked handle, e.g.
  ``query = db(...)`` (PyDAL's ``db(query)`` returns a lazy ``Set`` --
  itself non-blocking -- so the *assignment* doesn't blindly get flagged;
  what matters is whether a terminal method is later invoked on it,
  in or out of a closure).
* The ``as`` target of ``with <handle>.transaction() as tx:``.

Deliberately *not* tracked: ``table = db.storage_config`` (a Table handle)
is not propagated as a tracked name, even though ``db.table.insert(...)``
chained directly off ``db`` in one expression is still caught. Table
objects expose both a genuinely blocking ``.insert()`` and entirely
non-blocking field/query-builder methods (``table.column.belongs([...])``,
``table.column.contains(...)``, comparisons) -- the ``table = db.X``
pattern is used throughout this codebase exclusively for the latter (see
``app/api/biomes.py``'s ``list_biomes``), so tracking it would flag pure
query-condition construction as a false positive far more often than it
would ever catch a real violation.

Then flag any call of the shape ``<tracked-handle>.<anything>(...)``
appearing directly in the async body (not inside a nested def/lambda) --
that covers ``db(...).select()/.update()/.delete()/.count()/.first()``,
``db.table.insert(...)``, ``db.table(id)`` (PyDAL's non-lazy row-by-id
shorthand), ``db.commit()/.rollback()/.executesql()/.transaction()``, and
the same on a tracked alias (``query.select()``, ``tx.executesql()``).

Known, accepted limitation: this is same-function, same-statement-tree
analysis, not cross-function dataflow. A handle threaded through an
*external* helper function's return value (e.g.
``assign_tbl, _ = _get_assignments_table(db)`` in ``app/api/nodes.py``)
is not traced -- exactly like ``test_no_direct_sqlalchemy_runtime.py``'s
regex approach, this test does not attempt full semantic analysis. In
every case audited during the Part 1-4 sweep, calls reached that way are
already inside a run_db()-wrapped closure at their point of use; if a
future change moves one out, this test's coverage of the *directly*
reachable 95% of call sites is still what catches the overwhelming
majority of regressions, and code review covers the rest.

No allowlist: the Part 4 sweep (gh-22) found zero call sites that
genuinely can't be expressed as a run_db()/to_thread() closure.
"""

from __future__ import annotations

import ast
import pathlib

# gRPC generated stubs (*_pb2.py / *_pb2_grpc.py) are third-party-generated
# code, not hand-written application logic -- never touch them.
_SKIP_DIR_PARTS = {"grpc", "__pycache__"}


def _root_name(node: ast.AST) -> str | None:
    """Walk a Call/Attribute/Subscript chain down to its root ``Name``."""
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Name):
            return node.id
        else:
            return None


def _is_handle_producing_expr(value: ast.AST, handles: set[str]) -> bool:
    """True iff ``value`` produces a *new* penguin-dal handle (DB/Set/Tx),
    as opposed to a row, list, count, or other already-executed result."""
    if isinstance(value, ast.Call):
        func = value.func
        if isinstance(func, ast.Name):
            if func.id == "get_db" or func.id in handles:
                return True
        if isinstance(func, ast.Attribute) and func.attr == "transaction":
            if _root_name(func.value) in handles:
                return True
        return False
    return False


class _AsyncBlockingCallFinder(ast.NodeVisitor):
    """Collects (lineno, description) violations for one module."""

    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scan_scope(node, {"db", "get_db"})
        # Keep descending so async defs nested inside a *sync* wrapper
        # (e.g. a decorator factory's inner `async def wrapper(...)`) are
        # still found -- generic_visit reaches them via the normal walk.
        self.generic_visit(node)

    def _scan_scope(self, func_node: ast.AST, handles: set[str]) -> None:
        for child in ast.iter_child_nodes(func_node):
            self._walk(child, handles)

    def _walk(self, node: ast.AST, handles: set[str]) -> None:
        # Nested (sync) def/lambda -- and nested async def, handled by its
        # own top-level visit_AsyncFunctionDef call -- are opaque here:
        # this is precisely the run_db()/to_thread() closure boundary.
        if isinstance(node, (ast.FunctionDef, ast.Lambda, ast.AsyncFunctionDef)):
            return

        if isinstance(node, ast.Assign) and _is_handle_producing_expr(node.value, handles):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    handles.add(tgt.id)

        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name) and _is_handle_producing_expr(
                    item.context_expr, handles
                ):
                    handles.add(item.optional_vars.id)

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                root = _root_name(func.value)
                if root in handles:
                    self.violations.append(
                        (node.lineno, f"{root}....{func.attr}(...) outside run_db()/to_thread()")
                    )

        for child in ast.iter_child_nodes(node):
            self._walk(child, handles)


def _scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    finder = _AsyncBlockingCallFinder()
    finder.visit(tree)
    return finder.violations


def test_no_blocking_db_in_async() -> None:
    # tests/security/test_no_blocking_db_in_async.py -> parents[2] is the
    # api-manager service root (parents[0]=security/, [1]=tests/).
    app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders: list[str] = []

    for path in sorted(app_root.rglob("*.py")):
        rel = path.relative_to(app_root)
        if any(part in _SKIP_DIR_PARTS for part in rel.parts):
            continue
        for lineno, desc in _scan_file(path):
            offenders.append(f"{rel}:{lineno}: {desc}")

    assert not offenders, (
        "Direct (unwrapped) penguin-dal execution found inside an async def "
        "body -- wrap it in a run_db()/asyncio.to_thread() closure (see "
        "app/db/run_db.py):\n" + "\n".join(offenders)
    )
