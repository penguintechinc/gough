"""Regression guard: no direct-SQLAlchemy RUNTIME queries in app/.

Runtime DB access must go through the penguin-dal overlay. Only the
schema-authority + engine-construction + RLS files may touch SQLAlchemy
directly. If this test fails, you reintroduced a direct-SQLAlchemy runtime
call -- route it through penguin-dal (db(...).select()/.executesql()/.transaction()).
"""
import pathlib
import re

# Files legitimately allowed to use SQLAlchemy directly:
#  - schema authority (models + Alembic), engine construction, and RLS pool wiring.
ALLOW_FILES = {
    "models_sqlalchemy.py",   # declarative models (schema authority)
    "models_m1.py",           # declarative models
    "init_db.py",             # schema init / engine construction
    "rls.py",                 # RLS pool-checkout event (engine-level SQL, by design)
    "database.py",            # penguin-dal DB() engine construction
}
ALLOW_DIR_PARTS = {"alembic"}  # migrations
# app/models/__init__.py constructs the engine + startup COUNT; allow it too.
ALLOW_RELPATHS = {"models/__init__.py"}

BAD = re.compile(
    r"set_tenant_guc\(|db_session\.execute\(\s*text|\.engine\.connect\(|"
    r"\bdb\.query\(|\.execute\(\s*text\(|\bsessionmaker\("
)

def _is_in_docstring(lines: list[str], line_num: int) -> bool:
    """Heuristic: check if line_num is inside a triple-quoted docstring."""
    in_triple_double = False
    in_triple_single = False
    for i, line in enumerate(lines[:line_num]):
        in_triple_double ^= line.count('"""') % 2 == 1
        in_triple_single ^= line.count("'''") % 2 == 1
    return in_triple_double or in_triple_single

def _strip_comment(line: str) -> str:
    # drop everything after a '#'; skip docstring markers.
    stripped = line.split("#", 1)[0]
    if '"""' in stripped or "'''" in stripped:
        return ""
    return stripped

def test_no_direct_sqlalchemy_runtime():
    # tests/security/test_no_direct_sqlalchemy_runtime.py -> parents[2] is
    # the api-manager service root (parents[0]=security/, [1]=tests/).
    app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for p in app_root.rglob("*.py"):
        rel = p.relative_to(app_root)
        if p.name in ALLOW_FILES:
            continue
        if any(part in ALLOW_DIR_PARTS for part in rel.parts):
            continue
        if str(rel) in ALLOW_RELPATHS:
            continue
        lines = p.read_text().splitlines()
        for i, raw in enumerate(lines, 1):
            if _is_in_docstring(lines, i - 1):
                continue
            code = _strip_comment(raw)
            if BAD.search(code):
                offenders.append(f"{rel}:{i}: {raw.strip()}")
    assert not offenders, (
        "Direct-SQLAlchemy runtime usage found (route through penguin-dal):\n"
        + "\n".join(offenders)
    )
