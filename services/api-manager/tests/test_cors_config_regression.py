"""Regression tests: CORS_ORIGINS must fail closed, never default to "*".

regression: audit cors-wildcard 2026-09-22

``app._resolve_cors_origins`` decides the ``allow_origin`` value passed to
``quart_cors.cors()``. Unset ``CORS_ORIGINS`` must resolve to a wildcard only
under DEBUG/TESTING (local dev, test suites); everywhere else it must resolve
to no cross-origin access at all. An operator-set value (including an
explicit ``*``) is always honored as-is -- that is a deliberate choice, not a
default.
"""

from __future__ import annotations

from quart import Quart

from app import _resolve_cors_origins
from app.config import Config


def _make_app(**config_overrides: object) -> Quart:
    app = Quart(__name__)
    app.config.from_object(Config)
    app.config.update(config_overrides)
    return app


class TestCORSFailClosedDefault:
    """SEE app/__init__.py::_resolve_cors_origins."""

    def test_config_class_default_is_not_wildcard(self):
        """Root-cause check: the Config class attribute itself must not be '*'."""
        assert Config.CORS_ORIGINS != "*"

    def test_unset_in_production_returns_no_origins(self):
        """CORS_ORIGINS unset + DEBUG/TESTING False -> fail-closed, not '*'."""
        app = _make_app(CORS_ORIGINS="", DEBUG=False, TESTING=False)
        assert _resolve_cors_origins(app) == []

    def test_unset_in_debug_allows_wildcard(self):
        """CORS_ORIGINS unset + DEBUG=True -> wildcard is fine for local dev."""
        app = _make_app(CORS_ORIGINS="", DEBUG=True, TESTING=False)
        assert _resolve_cors_origins(app) == ["*"]

    def test_unset_in_testing_allows_wildcard(self):
        """CORS_ORIGINS unset + TESTING=True -> wildcard so test suites keep booting."""
        app = _make_app(CORS_ORIGINS="", DEBUG=False, TESTING=True)
        assert _resolve_cors_origins(app) == ["*"]

    def test_explicit_wildcard_honored_even_in_production(self):
        """An operator explicitly setting CORS_ORIGINS=* gets it -- their call, not a default."""
        app = _make_app(CORS_ORIGINS="*", DEBUG=False, TESTING=False)
        assert _resolve_cors_origins(app) == ["*"]

    def test_explicit_origin_list_parsed_from_csv(self):
        """A comma-separated CORS_ORIGINS value is split into distinct origins."""
        app = _make_app(
            CORS_ORIGINS="https://a.example.com, https://b.example.com",
            DEBUG=False,
            TESTING=False,
        )
        assert _resolve_cors_origins(app) == [
            "https://a.example.com",
            "https://b.example.com",
        ]
