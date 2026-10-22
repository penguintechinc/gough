"""OpenAPI 3.1 spec emitter for the Gough API Manager.

Walks every Blueprint registered on the Quart app, extracts route metadata
(method, path, view function, docstring, Pydantic v2 request/response models
attached via attributes set by ``quart_schema``-style decorators or by the
internal ``app.api._helpers`` decorators), and emits a deterministic
OpenAPI 3.1 document at ``docs/api/openapi.{json,yaml}``.

Run as a module to regenerate the spec::

    python -m app.openapi_export

The output is sorted alphabetically (paths, methods, schema components, tags)
so re-running on an unchanged source tree produces a byte-stable diff. CI
gates on ``git diff --exit-code docs/api/openapi.json``.

Defensive imports: blueprints that fail to import (because their dependencies
are still being landed by sibling Wave 1 agents) are logged and skipped, not
fatal — the exporter must keep working even with partial Wave 1 coverage.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml
from pydantic import BaseModel
from quart import Blueprint, Quart

logger = logging.getLogger(__name__)


# ==============================================================================
# Configuration: blueprints to walk + mounting prefixes
# ==============================================================================
#
# Order is alphabetical for deterministic output. Each entry is
#     (module_path, attribute_name, url_prefix)
# url_prefix is the mounting prefix used in app/__init__.py (or "" when the
# blueprint embeds its own ``url_prefix=`` kwarg in Blueprint(...)).
#
# Wave 1 blueprints that are still landing are listed here too — defensive
# imports skip missing modules with a warning.
# ==============================================================================

BLUEPRINT_SPECS: tuple[tuple[str, str, str], ...] = (
    # Pre-existing Sprint 1 blueprints
    ("app.api.agents", "agents_bp", ""),
    ("app.api.clouds", "clouds_bp", "/api/v1/clouds"),
    ("app.api.biomes", "biomes_bp", ""),
    ("app.api.ipxe", "ipxe_bp", "/api/v1/ipxe"),
    ("app.api.secrets", "secrets_bp", "/api/v1/secrets"),
    ("app.api.shell", "shell_bp", ""),
    ("app.api.ssh_ca", "ssh_ca_bp", ""),
    ("app.api.storage", "storage_bp", "/api/v1/storage"),
    ("app.api.teams", "teams_bp", ""),
    ("app.auth", "auth_bp", "/api/v1/auth"),
    ("app.hello", "hello_bp", "/api/v1"),
    ("app.users", "users_bp", "/api/v1/users"),
    # Wave 1 blueprints (sibling agents). Defensive: skipped if missing.
    ("app.api.audit", "audit_bp", "/api/v1/audit"),
    ("app.api.capacity", "capacity_bp", "/api/v1/capacity"),
    ("app.api.clusters", "clusters_bp", "/api/v1/clusters"),
    ("app.api.disks", "disks_bp", "/api/v1/disks"),
    ("app.api.integrations", "integrations_bp", "/api/v1/integrations"),
    ("app.api.joiner_secrets", "joiner_secrets_bp", "/api/v1/joiner-secrets"),
    ("app.api.migration", "migration_bp", "/api/v1/migration"),
    ("app.api.nodes", "nodes_bp", "/api/v1/nodes"),
    ("app.api.primary", "primary_bp", "/api/v1/primary"),
    ("app.api.webhooks", "webhooks_bp", "/api/v1/webhooks"),
)


# ==============================================================================
# Standard OpenAPI envelope: security schemes, servers, info, error catalog
# ==============================================================================

API_VERSION: str = "1.0.0"
OPENAPI_VERSION: str = "3.1.0"

SECURITY_SCHEMES: dict[str, dict[str, Any]] = {
    "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "OIDC user/machine JWT. Tenant claim mandatory; scopes drive "
            "authorization decisions."
        ),
    },
    "mutualTLS": {
        "type": "mutualTLS",
        "description": (
            "SPIFFE SVID over mTLS for service-to-service traffic on the "
            "internal gRPC plane (also accepted on REST for cluster-internal "
            "calls)."
        ),
    },
    "oneTimeToken": {
        "type": "apiKey",
        "in": "header",
        "name": "X-Bootstrap-Token",
        "description": (
            "Vault-transit-signed JWT, 10-minute TTL, single-use Redis nonce. "
            "Used only during node joiner bootstrap before SVID issuance."
        ),
    },
}

# Standard error code catalog — keep in lockstep with the Standard Error Codes
# table in the Gough spec. Each entry becomes a discriminated example response.
ERROR_CODE_CATALOG: tuple[tuple[str, int, str], ...] = (
    ("invalid_request", 400, "Request payload failed validation."),
    ("unauthenticated", 401, "Missing or invalid credential."),
    ("tenant_claim_missing", 403, "JWT lacks the required 'tenant' claim."),
    ("forbidden_scope", 403, "Token scopes do not satisfy endpoint requirements."),
    ("not_found", 404, "Target resource does not exist."),
    ("conflict", 409, "Resource state conflicts with the request."),
    ("locked", 423, "Resource is locked by another in-flight operation."),
    ("rate_limited", 429, "Caller has exceeded the rate-limit budget."),
    ("internal_error", 500, "Unhandled server error; see audit log for trace."),
    ("dependency_unavailable", 503, "Vault/SPIRE/NATS/LXD dependency is unreachable."),
)


# ==============================================================================
# Internal data model
# ==============================================================================


@dataclass
class RouteMeta:
    """Captured metadata for a single Quart route, post-extraction."""

    method: str
    path: str
    view: Callable[..., Any]
    blueprint_name: str
    request_model: Optional[type[BaseModel]] = None
    response_model: Optional[type[BaseModel]] = None
    summary: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)


# ==============================================================================
# Blueprint discovery
# ==============================================================================


def import_blueprints(
    specs: Iterable[tuple[str, str, str]] = BLUEPRINT_SPECS,
) -> list[tuple[Blueprint, str]]:
    """Import each blueprint, skipping (with a warning) any that fail.

    Returns a list of ``(blueprint, mount_prefix)`` tuples in spec order.
    """
    imported: list[tuple[Blueprint, str]] = []
    for module_path, attr_name, mount_prefix in specs:
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 — defensive: any import error
            logger.warning(
                "openapi_export: skipping blueprint %s.%s — import failed: %s",
                module_path,
                attr_name,
                exc,
            )
            continue
        bp = getattr(module, attr_name, None)
        if not isinstance(bp, Blueprint):
            logger.warning(
                "openapi_export: skipping %s.%s — not a Quart Blueprint",
                module_path,
                attr_name,
            )
            continue
        imported.append((bp, mount_prefix))
    return imported


# ==============================================================================
# Route extraction
# ==============================================================================

# HTTP methods we expose in OpenAPI. HEAD/OPTIONS are auto-derived by tooling.
_OPENAPI_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE"}
)


def _model_attr(view: Callable[..., Any], *names: str) -> Optional[type[BaseModel]]:
    """Pull a Pydantic model off a view function via known attribute names.

    Supports attributes set by both ``quart_schema`` (``__quart_schema_request__``,
    ``__quart_schema_response__``) and the internal helpers in
    ``app.api._helpers`` (``_request_model``, ``_response_model``).
    """
    for name in names:
        candidate = getattr(view, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def _join_prefix(mount_prefix: str, bp_prefix: Optional[str], rule: str) -> str:
    """Combine app-side mount prefix, blueprint-internal prefix, and rule.

    Quart already prepends ``Blueprint(url_prefix=...)`` to the rule when the
    blueprint is registered with ``app.register_blueprint(bp)``; when registered
    with an extra ``url_prefix`` they stack. We mirror that join here without
    needing a real registration cycle, so the exporter is hermetic.
    """
    parts = []
    for segment in (mount_prefix or "", bp_prefix or "", rule or ""):
        if not segment:
            continue
        parts.append(segment if segment.startswith("/") else "/" + segment)
    raw = "".join(parts) or "/"
    # Collapse duplicate slashes that arise when segments already end with "/".
    collapsed = re.sub(r"/{2,}", "/", raw)
    if collapsed != "/" and collapsed.endswith("/"):
        collapsed = collapsed[:-1]
    return collapsed


def _flask_pattern_to_openapi(path: str) -> tuple[str, list[dict[str, Any]]]:
    """Convert a Flask-style URL pattern to an OpenAPI path + parameter list.

    ``/api/v1/biomes/<int:biome_id>`` → ``/api/v1/biomes/{biome_id}`` plus a parameter
    record describing ``biome_id`` as integer. Unknown converters fall back to
    ``string``.
    """
    type_map = {
        "int": ("integer", "int64"),
        "float": ("number", "float"),
        "uuid": ("string", "uuid"),
        "string": ("string", None),
        "path": ("string", None),
    }
    params: list[dict[str, Any]] = []

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if ":" in token:
            converter, name = token.split(":", 1)
        else:
            converter, name = "string", token
        otype, ofmt = type_map.get(converter, ("string", None))
        schema: dict[str, Any] = {"type": otype}
        if ofmt:
            schema["format"] = ofmt
        params.append(
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": schema,
            }
        )
        return "{" + name + "}"

    converted = re.sub(r"<([^>]+)>", _replace, path)
    return converted, params


def _extract_routes(
    blueprints: list[tuple[Blueprint, str]],
) -> list[RouteMeta]:
    """Walk each Blueprint's deferred functions and reconstruct route records.

    Quart Blueprints store registrations as ``deferred_functions`` (callbacks
    that re-issue ``app.add_url_rule`` against a state object). We replay them
    against a tiny capturing harness instead of a real Quart instance — the
    exporter never needs the app to be runnable.
    """
    routes: list[RouteMeta] = []

    for bp, mount_prefix in blueprints:
        captured: list[tuple[str, str, Callable[..., Any], list[str]]] = []

        class _Capture:
            url_prefix = bp.url_prefix or ""
            name = bp.name
            name_prefix = ""
            subdomain = None

            def add_url_rule(  # noqa: D401, PLR0913 — mimics Quart signature
                self,
                rule: str,
                endpoint: Optional[str] = None,
                view_func: Optional[Callable[..., Any]] = None,
                methods: Optional[Iterable[str]] = None,
                **kwargs: Any,
            ) -> None:
                if view_func is None:
                    return
                method_set = (
                    {m.upper() for m in methods}
                    if methods
                    else {"GET"}
                )
                method_set &= _OPENAPI_METHODS
                if not method_set:
                    return
                full_rule = (self.url_prefix or "") + rule
                captured.append(
                    (full_rule, endpoint or view_func.__name__, view_func, sorted(method_set))
                )

            # Several Quart helpers call these during deferred replay.
            def register_blueprint(self, *_a: Any, **_kw: Any) -> None:
                return None

        state = _Capture()
        for deferred in bp.deferred_functions:
            try:
                deferred(state)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "openapi_export: deferred replay failed in blueprint '%s': %s",
                    bp.name,
                    exc,
                )

        for rule, _endpoint, view, methods in captured:
            full_path = _join_prefix(mount_prefix, None, rule)
            request_model = _model_attr(
                view,
                "__quart_schema_request__",
                "_request_model",
                "openapi_request_model",
            )
            response_model = _model_attr(
                view,
                "__quart_schema_response__",
                "_response_model",
                "openapi_response_model",
            )
            doc = (view.__doc__ or "").strip()
            summary, _, description = doc.partition("\n")
            for method in methods:
                routes.append(
                    RouteMeta(
                        method=method,
                        path=full_path,
                        view=view,
                        blueprint_name=bp.name,
                        request_model=request_model,
                        response_model=response_model,
                        summary=summary.strip(),
                        description=description.strip(),
                        tags=[bp.name],
                    )
                )

    return routes


# ==============================================================================
# Schema component management
# ==============================================================================


class _SchemaRegistry:
    """Collects Pydantic schemas + dedupes them under ``components.schemas``."""

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}

    def register(self, model: type[BaseModel]) -> str:
        """Register ``model`` and return its component reference name."""
        json_schema = model.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        # Pydantic v2 emits nested $defs; hoist them to top-level components.
        defs = json_schema.pop("$defs", {})
        for name, sub_schema in defs.items():
            self._schemas.setdefault(name, sub_schema)
        component_name = model.__name__
        self._schemas[component_name] = json_schema
        return component_name

    def as_dict(self) -> dict[str, Any]:
        return OrderedDict(sorted(self._schemas.items()))


# ==============================================================================
# Spec assembly
# ==============================================================================


def _envelope_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["status", "data"],
        "properties": {
            "status": {"type": "string", "enum": ["success", "error"]},
            "data": {},
            "meta": {
                "type": "object",
                "properties": {
                    "version": {"type": "integer"},
                    "timestamp": {"type": "string", "format": "date-time"},
                    "request_id": {"type": "string"},
                },
            },
        },
    }


def _error_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["status", "error"],
        "properties": {
            "status": {"type": "string", "const": "error"},
            "error": {"type": "string"},
            "code": {
                "type": "string",
                "enum": [code for code, _, _ in ERROR_CODE_CATALOG],
            },
            "detail": {"type": "string"},
        },
    }


def _build_responses(
    route: RouteMeta, registry: _SchemaRegistry
) -> dict[str, Any]:
    responses: dict[str, Any] = OrderedDict()

    if route.response_model is not None:
        component = registry.register(route.response_model)
        success = {
            "description": "Successful response.",
            "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{component}"}
                }
            },
        }
    else:
        success = {
            "description": "Successful response.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/SuccessEnvelope"}
                }
            },
        }
    responses["200"] = success

    # Reference the standard error codes catalog for every protected endpoint.
    for code, status, description in ERROR_CODE_CATALOG:
        responses[str(status)] = {
            "description": f"{description} (code={code})",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/Error"},
                    "examples": {
                        code: {
                            "summary": code,
                            "value": {
                                "status": "error",
                                "error": description,
                                "code": code,
                            },
                        }
                    },
                }
            },
        }

    return responses


def _build_request_body(
    route: RouteMeta, registry: _SchemaRegistry
) -> Optional[dict[str, Any]]:
    if route.request_model is None:
        return None
    component = registry.register(route.request_model)
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": f"#/components/schemas/{component}"}
            }
        },
    }


def _route_security(route: RouteMeta) -> list[dict[str, list[str]]]:
    # Anonymous endpoints have no security requirements.
    try:
        from app.security.scope_policy import ANONYMOUS_PATHS
    except ImportError:
        # Scope policy not yet available (Wave 1 still landing); assume protected.
        ANONYMOUS_PATHS = frozenset()

    path_key = getattr(route, "path_template_for_policy", route.path)
    if (route.method, path_key) in ANONYMOUS_PATHS:
        return []
    return [{"bearerAuth": []}, {"mutualTLS": []}]


def _openapi_to_flask(openapi_path: str) -> str:
    """Convert OpenAPI path {param} to Flask pattern <param>."""
    return re.sub(r"\{([^}]+)\}", lambda m: "<" + m.group(1) + ">", openapi_path)


def build_spec(
    blueprints: Optional[list[tuple[Blueprint, str]]] = None,
) -> dict[str, Any]:
    """Build the full OpenAPI 3.1 document as a Python dict (sorted)."""
    if blueprints is None:
        blueprints = import_blueprints()
    routes = _extract_routes(blueprints)

    # Cache the policy-style path on each route for security lookup.
    for r in routes:
        r.path_template_for_policy = _openapi_to_flask(r.path)  # type: ignore[attr-defined]

    registry = _SchemaRegistry()
    registry._schemas["SuccessEnvelope"] = _envelope_schema()
    registry._schemas["Error"] = _error_schema()

    paths: dict[str, dict[str, Any]] = {}
    tags_seen: set[str] = set()

    # Sort routes deterministically: path, then method.
    for route in sorted(routes, key=lambda r: (r.path, r.method)):
        openapi_path, path_params = _flask_pattern_to_openapi(route.path)
        operation: dict[str, Any] = OrderedDict()
        if route.summary:
            operation["summary"] = route.summary
        if route.description:
            operation["description"] = route.description
        operation["tags"] = sorted(set(route.tags))
        tags_seen.update(route.tags)
        operation["operationId"] = f"{route.blueprint_name}_{route.view.__name__}_{route.method.lower()}"
        if path_params:
            operation["parameters"] = path_params
        body = _build_request_body(route, registry)
        if body:
            operation["requestBody"] = body
        operation["responses"] = _build_responses(route, registry)
        operation["security"] = _route_security(route)

        path_item = paths.setdefault(openapi_path, {})
        path_item[route.method.lower()] = operation

    sorted_paths = OrderedDict(
        (path, OrderedDict(sorted(methods.items())))
        for path, methods in sorted(paths.items())
    )

    spec: dict[str, Any] = OrderedDict()
    spec["openapi"] = OPENAPI_VERSION
    spec["info"] = OrderedDict(
        [
            ("title", "Gough API Manager"),
            ("version", API_VERSION),
            (
                "description",
                "Operator and SDK-facing REST API for the Gough platform. "
                "Cluster-internal RPCs use gRPC over mTLS on port 50051; this "
                "REST surface is reserved for human operators and SDK clients.",
            ),
            ("license", {"name": "Proprietary"}),
        ]
    )
    spec["servers"] = [
        {"url": "https://{host}", "variables": {"host": {"default": "gough.localhost.local"}}}
    ]
    spec["tags"] = [
        {"name": tag, "description": f"{tag} resource group"}
        for tag in sorted(tags_seen)
    ]
    spec["paths"] = sorted_paths
    spec["components"] = OrderedDict(
        [
            ("securitySchemes", OrderedDict(sorted(SECURITY_SCHEMES.items()))),
            ("schemas", registry.as_dict()),
        ]
    )
    spec["security"] = [{"bearerAuth": []}]

    return spec




# ==============================================================================
# Output writers (deterministic, sorted)
# ==============================================================================


def _to_plain(obj: Any) -> Any:
    """Convert nested OrderedDicts to plain dicts (already pre-sorted)."""
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


def write_spec(
    spec: dict[str, Any],
    out_dir: Path,
) -> tuple[Path, Path]:
    """Write spec to ``openapi.json`` and ``openapi.yaml``; return paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    plain = _to_plain(spec)
    json_path = out_dir / "openapi.json"
    yaml_path = out_dir / "openapi.yaml"
    json_text = json.dumps(plain, indent=2, sort_keys=False) + "\n"
    yaml_text = yaml.safe_dump(
        plain, sort_keys=False, default_flow_style=False, width=100
    )
    json_path.write_text(json_text, encoding="utf-8")
    yaml_path.write_text(yaml_text, encoding="utf-8")
    return json_path, yaml_path


# ==============================================================================
# CLI entrypoint
# ==============================================================================


def _default_out_dir() -> Path:
    """Locate ``<repo-root>/docs/api`` from this file."""
    here = Path(__file__).resolve()
    # services/api-manager/app/openapi_export.py → repo root is 4 levels up.
    return here.parents[3] / "docs" / "api"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit Gough OpenAPI 3.1 spec to docs/api/openapi.{json,yaml}"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_default_out_dir(),
        help="Destination directory (default: <repo-root>/docs/api)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if regenerated spec differs from on-disk copy.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging."
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    spec = build_spec()

    if args.check:
        existing_json = (args.out_dir / "openapi.json").read_text(encoding="utf-8") \
            if (args.out_dir / "openapi.json").exists() else ""
        new_json = json.dumps(_to_plain(spec), indent=2, sort_keys=False) + "\n"
        if existing_json != new_json:
            sys.stderr.write(
                "openapi_export: spec on disk is out of date — run "
                "`make openapi` and commit the result.\n"
            )
            return 1
        return 0

    json_path, yaml_path = write_spec(spec, args.out_dir)
    logger.info("openapi_export: wrote %s and %s", json_path, yaml_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
