"""Every registered HTTP route must declare scopes or be explicitly anonymous.

Registers all blueprints (as the OpenAPI exporter does) and fails, listing the
offenders, when any (method, rule) is in neither SCOPE_POLICY nor ANONYMOUS_PATHS.
"""

from __future__ import annotations

import re

from quart import Quart

from app.openapi_export import import_blueprints
from app.security.scope_policy import ANONYMOUS_PATHS, SCOPE_POLICY

_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def _canon(rule: str) -> str:
    """Canonicalise a rule the way enforcement matches: no trailing slash, converters erased."""
    rule = rule.rstrip("/") if len(rule) > 1 else rule
    return re.sub(r"<[^>]+>", "<>", rule)


def _route_pairs() -> tuple[int, list[tuple[str, str]]]:
    """Return (blueprint count, sorted (method, rule) pairs) for every non-static route."""
    app = Quart(__name__)
    blueprints = import_blueprints()
    for blueprint, prefix in blueprints:
        app.register_blueprint(blueprint, url_prefix=prefix or None)
    pairs = {
        (method, rule.rule)
        for rule in app.url_map.iter_rules()
        if rule.endpoint != "static" and not rule.endpoint.endswith(".static")
        for method in rule.methods or ()
        if method not in _IGNORED_METHODS
    }
    return len(blueprints), sorted(pairs)


def test_every_route_has_scope_or_is_anonymous() -> None:
    """No route may be reachable without a declared scope or an explicit anonymous entry."""
    count, pairs = _route_pairs()
    assert count > 10 and len(pairs) > 100, f"examined too little: {count} blueprints, {len(pairs)} routes"
    known = {(m, _canon(r)) for m, r in SCOPE_POLICY} | {(m, _canon(r)) for m, r in ANONYMOUS_PATHS}
    offenders = [f"{method} {rule}" for method, rule in pairs if (method, _canon(rule)) not in known]
    assert not offenders, f"{len(offenders)}/{len(pairs)} routes lack scope policy:\n" + "\n".join(offenders)
