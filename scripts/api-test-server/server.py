"""Hermetic launcher for the REAL api-manager Quart app (contract tests).

Boots ``app.create_app`` unmodified with a test-only Config subclass: sqlite
file DB, in-memory ES256 keystore (TESTING), gRPC listener and rate limiter
off. Also offers ``--dump-routes`` / ``--dump-openapi`` introspection modes
that never bind a socket.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "services" / "api-manager"
sys.path.insert(0, str(API_DIR))

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402


class ContractConfig(Config):
    """Test-only config: TESTING enables the in-memory ES256 keystore."""

    TESTING = True
    GRPC_ENABLED = False
    RATE_LIMIT_ENABLED = False
    AUDIT_RECORDING_ENABLED = False


def dump_routes(app) -> list:
    """Return the url_map as sorted method/path/scope/anonymous records."""
    import re

    from app.security.scope_enforcement import lookup_required_scopes
    from app.security.scope_policy import is_anonymous_request

    def concrete(rule: str) -> str:
        """Fill Flask converters with sample values so the app's own lookup runs."""
        rule = re.sub(r"<int:[^>]+>", "1", rule)
        rule = re.sub(r"<uuid:[^>]+>", "00000000-0000-4000-8000-000000000001", rule)
        return re.sub(r"<[^>]+>", "x", rule)

    rows = []
    for rule in app.url_map.iter_rules():
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            anon = is_anonymous_request(method, concrete(rule.rule))
            scopes = None if anon else lookup_required_scopes(method, concrete(rule.rule))
            rows.append(
                {
                    "method": method,
                    "path": rule.rule,
                    "endpoint": rule.endpoint,
                    "anonymous": anon,
                    "in_scope_policy": scopes is not None,
                    "scopes": sorted(scopes) if scopes is not None else None,
                }
            )
    return sorted(rows, key=lambda r: (r["path"], r["method"]))


async def serve(port: int) -> None:
    """Serve the app on 127.0.0.1:port via hypercorn until terminated."""
    from hypercorn.asyncio import serve as hserve
    from hypercorn.config import Config as HConfig

    app = await create_app(ContractConfig)
    hc = HConfig()
    hc.bind = [f"127.0.0.1:{port}"]
    hc.accesslog = "-"
    await hserve(app, hc)


def main() -> None:
    """CLI entry point."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int)
    ap.add_argument("--dump-routes", metavar="FILE")
    args = ap.parse_args()
    if args.dump_routes:
        app = asyncio.run(create_app(ContractConfig))
        Path(args.dump_routes).write_text(json.dumps(dump_routes(app), indent=1))
        return
    if not args.port:
        sys.exit("--port required")
    asyncio.run(serve(args.port))


if __name__ == "__main__":
    main()
