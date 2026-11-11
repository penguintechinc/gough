"""Route-coverage gate: every real route has a reviewed policy decision.

Enumerates routes from the REAL application (``app.create_app()`` and its
``url_map``, i.e. exactly the blueprints production registers) and fails when:

* the app factory cannot be built (any blueprint import failure propagates --
  ``create_app`` imports every blueprint directly, never via a skip-on-error
  loader);
* a route has no entry in ``SCOPE_POLICY``, ``ANONYMOUS_PATHS`` or the
  documented ``INTENTIONALLY_UNMAPPED`` allowlist;
* an entry in any of those three tables matches no real route (dead entries
  silently pre-authorise a path that a future route could land on);
* an ``INTENTIONALLY_UNMAPPED`` entry lacks a follow-up reason.

The pure helpers below take their inputs as arguments so the gate's ability
to fail is itself tested (``TestGateCanFail``).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Mapping

import pytest
from quart import Quart

from app.security.scope_policy import (
    ANONYMOUS_PATHS,
    INTENTIONALLY_UNMAPPED,
    SCOPE_POLICY,
)

_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})

#: Lower bounds on what the real app registers. Guards against the gate
#: passing vacuously because enumeration found (almost) nothing.
_MIN_BLUEPRINTS = 20
_MIN_ROUTES = 150

Pair = tuple[str, str]


def _canon(rule: str) -> str:
    """Canonicalise a rule the way enforcement matches: no trailing slash, converters erased."""
    rule = rule.rstrip("/") if len(rule) > 1 else rule
    return re.sub(r"<[^>]+>", "<>", rule)


def route_pairs(app: Quart) -> list[Pair]:
    """Return sorted (method, rule) pairs for every non-static route of ``app``."""
    pairs = {
        (method, rule.rule)
        for rule in app.url_map.iter_rules()
        if rule.endpoint != "static" and not rule.endpoint.endswith(".static")
        for method in rule.methods or ()
        if method not in _IGNORED_METHODS
    }
    return sorted(pairs)


def find_unmapped(routes: Iterable[Pair], *tables: Iterable[Pair]) -> list[str]:
    """Routes that appear in none of ``tables`` (canonical comparison)."""
    known = {(m, _canon(p)) for table in tables for m, p in table}
    return [f"{m} {r}" for m, r in routes if (m, _canon(r)) not in known]


def find_dead(routes: Iterable[Pair], table: Iterable[Pair]) -> list[str]:
    """Entries of ``table`` that match no real route (canonical comparison)."""
    real = {(m, _canon(r)) for m, r in routes}
    return sorted(f"{m} {p}" for m, p in table if (m, _canon(p)) not in real)


def find_undocumented(table: Mapping[Pair, str]) -> list[str]:
    """Allowlist entries whose reason does not name a follow-up."""
    return sorted(
        f"{m} {p}"
        for (m, p), reason in table.items()
        if not isinstance(reason, str) or "follow-up" not in reason.lower()
    )


@pytest.fixture(scope="module")
def real_app() -> Quart:
    """The production app factory output (blueprint import errors propagate)."""
    from app import create_app
    from app.config import TestingConfig

    return asyncio.run(create_app(TestingConfig))


@pytest.fixture(scope="module")
def real_routes(real_app: Quart) -> list[Pair]:
    """Every (method, rule) pair the real app serves."""
    return route_pairs(real_app)


class TestRealAppCoverage:
    """The gate itself, run against the real application."""

    def test_enumeration_is_not_vacuous(
        self, real_app: Quart, real_routes: list[Pair]
    ) -> None:
        """Enough blueprints and routes were examined for the gate to mean anything."""
        assert len(real_app.blueprints) >= _MIN_BLUEPRINTS, sorted(real_app.blueprints)
        assert len(real_routes) >= _MIN_ROUTES, len(real_routes)
        assert "vault" in real_app.blueprints, "vault_bp must be registered and gated"

    def test_every_route_has_a_decision(self, real_routes: list[Pair]) -> None:
        """Each route is scoped, anonymous, or documented as intentionally unmapped."""
        offenders = find_unmapped(
            real_routes, SCOPE_POLICY, ANONYMOUS_PATHS, INTENTIONALLY_UNMAPPED
        )
        assert not offenders, (
            f"{len(offenders)}/{len(real_routes)} routes lack a policy decision:\n"
            + "\n".join(offenders)
        )

    @pytest.mark.parametrize(
        ("name", "table"),
        [
            ("SCOPE_POLICY", SCOPE_POLICY),
            ("ANONYMOUS_PATHS", ANONYMOUS_PATHS),
            ("INTENTIONALLY_UNMAPPED", INTENTIONALLY_UNMAPPED),
        ],
    )
    def test_no_dead_entries(
        self, real_routes: list[Pair], name: str, table: Iterable[Pair]
    ) -> None:
        """No table entry pre-authorises (or excuses) a path with no real route."""
        dead = find_dead(real_routes, table)
        assert not dead, (
            f"{name}: {len(dead)} entries match no real route:\n" + "\n".join(dead)
        )

    def test_no_duplicate_policy_keys_in_source(self) -> None:
        """Dict-literal duplicates (silently last-wins) and canonical twins are rejected."""
        import ast
        import inspect

        import app.security.scope_policy as sp

        tree = ast.parse(inspect.getsource(sp))
        literal_keys: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and getattr(node.target, "id", "") == "SCOPE_POLICY"
            ):
                assert isinstance(node.value, ast.Dict)
                literal_keys = [
                    ast.unparse(k) for k in node.value.keys if k is not None
                ]
        assert len(literal_keys) == len(SCOPE_POLICY) > 50
        canon = [(m, _canon(p)) for m, p in SCOPE_POLICY]
        assert len(set(canon)) == len(canon), (
            "two policy entries canonicalise to one route"
        )

    def test_unmapped_entries_name_their_follow_up(self) -> None:
        """Every intentionally-unmapped route documents the fix it is waiting on."""
        assert INTENTIONALLY_UNMAPPED, "allowlist unexpectedly empty"
        assert not find_undocumented(INTENTIONALLY_UNMAPPED)

    def test_unmapped_disjoint_from_policy_and_anonymous(self) -> None:
        """An unmapped route can never also be granted a scope or anonymous access."""
        unmapped = {(m, _canon(p)) for m, p in INTENTIONALLY_UNMAPPED}
        granted = {(m, _canon(p)) for m, p in SCOPE_POLICY} | {
            (m, _canon(p)) for m, p in ANONYMOUS_PATHS
        }
        assert not unmapped & granted

    def test_vault_rotate_keys_is_superadmin(self, real_routes: list[Pair]) -> None:
        """vault_bp's only route is covered and superadmin-only."""
        assert ("POST", "/api/v1/vault/rotate-keys") in real_routes
        assert SCOPE_POLICY[("POST", "/api/v1/vault/rotate-keys")] == frozenset(
            {"gough.cluster.superadmin"}
        )


class TestGateCanFail:
    """The gate's helpers detect each failure class (a gate that cannot fail is not a gate)."""

    _ROUTES: tuple[Pair, ...] = (("GET", "/api/v1/a/<int:x>"), ("POST", "/api/v1/b/"))

    def test_unmapped_route_is_reported(self) -> None:
        """A real route absent from every table is an offender."""
        assert find_unmapped(self._ROUTES, [("GET", "/api/v1/a/<x>")]) == [
            "POST /api/v1/b/"
        ]

    def test_dead_entry_is_reported(self) -> None:
        """A table entry with no matching route is reported as dead."""
        table = [("GET", "/api/v1/a/<string:y>"), ("DELETE", "/api/v1/b")]
        assert find_dead(self._ROUTES, table) == ["DELETE /api/v1/b"]

    def test_method_mismatch_is_dead_and_unmapped(self) -> None:
        """Matching the path but not the method satisfies neither check."""
        table = [("PUT", "/api/v1/b")]
        assert find_dead(self._ROUTES, table) == ["PUT /api/v1/b"]
        assert "POST /api/v1/b/" in find_unmapped(self._ROUTES, table)

    def test_undocumented_allowlist_entry_is_reported(self) -> None:
        """An allowlist reason that names no follow-up is rejected."""
        table = {
            ("GET", "/x"): "because",
            ("GET", "/y"): "Follow-up: gh-1 tenant filter",
        }
        assert find_undocumented(table) == ["GET /x"]

    def test_clean_inputs_pass(self) -> None:
        """Fully-covered inputs yield no offenders (the gate is not always-red either)."""
        table = [("GET", "/api/v1/a/<x>"), ("POST", "/api/v1/b")]
        assert find_unmapped(self._ROUTES, table) == []
        assert find_dead(self._ROUTES, table) == []
