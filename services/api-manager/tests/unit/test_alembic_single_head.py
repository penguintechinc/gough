"""Static check that the alembic revision graph has exactly one head.

Parses every revision file with ast (no alembic runtime or database needed) and fails
on zero or multiple heads, or on a broken chain.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _revisions() -> dict[str, object]:
    """Map each revision id to its down_revision, read from the module-level assignments."""
    found: dict[str, object] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in {"revision", "down_revision"}:
                    values[name] = ast.literal_eval(node.value)
        assert "revision" in values, f"{path.name} has no revision"
        found[str(values["revision"])] = values.get("down_revision")
    return found


def _heads(graph: dict[str, object]) -> set[str]:
    """Return revisions that no other revision names as its down_revision."""
    referenced: set[str] = set()
    for down in graph.values():
        if isinstance(down, (tuple, list)):
            referenced.update(str(d) for d in down)
        elif down is not None:
            referenced.add(str(down))
    return set(graph) - referenced


def test_exactly_one_head() -> None:
    """The revision graph has a single head, so `alembic upgrade head` is unambiguous."""
    graph = _revisions()
    assert graph, "no alembic revisions found"
    heads = _heads(graph)
    assert len(heads) == 1, f"expected one alembic head, got {sorted(heads)}"


def test_lxd_migration_is_the_head_on_top_of_missing_cols() -> None:
    """The LXD join migration chains directly onto 20260821_1900_missing_cols and is the head."""
    graph = _revisions()
    assert graph["20261022_1200_lxd_join_issuance"] == "20260821_1900_missing_cols"
    assert _heads(graph) == {"20261022_1200_lxd_join_issuance"}


@pytest.mark.parametrize("down", ["20260821_1900_missing_cols", "20260821_1600_merge_cloud_schema"])
def test_every_down_revision_resolves(down: str) -> None:
    """Known ancestors exist in the graph, so the chain is not dangling."""
    assert down in _revisions()
