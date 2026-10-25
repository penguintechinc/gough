"""Public installer distribution: serves gough-install.py from the verified biome cache.

Only the installer script and its sha256 are anonymous (see ``ANONYMOUS_PATHS`` in
``app.security.scope_policy``). Both are rate-limited. The installer is read from
the cached biomes release selected by ``?version=`` or ``GOUGH_BIOMES_INSTALLER_VERSION``.
"""

from __future__ import annotations

import os
from typing import Any

from quart import Blueprint, Response, request

from ..api._helpers import err_bad_request, err_not_found
from ..rate_limit import rate_limit
from ..services.biome_bundle import (
    INSTALLER_BIOME,
    INSTALLER_PATH,
    BundleCacheKeyError,
    validate_version,
)
from ..workers.biome_bundle_importer import read_cached_file

install_bp = Blueprint("install", __name__, url_prefix="/api/v1/install")

_INSTALLER_VERSION_ENV = "GOUGH_BIOMES_INSTALLER_VERSION"


def _installer_version() -> str | None:
    """Return the requested installer version, falling back to the configured pin."""
    requested = request.args.get("version") or os.getenv(_INSTALLER_VERSION_ENV) or None
    return requested


@install_bp.route("/gough-install.py", methods=["GET"])
@rate_limit("30/minute")
async def get_installer() -> Any:
    """Serve the installer script from the cached biomes release (anonymous, rate-limited)."""
    version = _installer_version()
    if not version:
        return err_not_found(
            f"installer version not configured ({_INSTALLER_VERSION_ENV})"
        )
    try:
        validate_version(version)
    except BundleCacheKeyError as exc:
        return err_bad_request(str(exc))
    cached = await read_cached_file(version, INSTALLER_BIOME, INSTALLER_PATH)
    if cached is None:
        return err_not_found("installer is not cached for this biomes version")
    response = Response(cached.content, mimetype="text/x-python")
    response.headers["X-Content-SHA256"] = cached.sha256
    return response


@install_bp.route("/gough-install.py.sha256", methods=["GET"])
@rate_limit("30/minute")
async def get_installer_sha256() -> Any:
    """Serve the sha256 of the installer in ``sha256sum`` format (anonymous, rate-limited)."""
    version = _installer_version()
    if not version:
        return err_not_found(
            f"installer version not configured ({_INSTALLER_VERSION_ENV})"
        )
    try:
        validate_version(version)
    except BundleCacheKeyError as exc:
        return err_bad_request(str(exc))
    cached = await read_cached_file(version, INSTALLER_BIOME, INSTALLER_PATH)
    if cached is None:
        return err_not_found("installer is not cached for this biomes version")
    return Response(f"{cached.sha256}  {INSTALLER_PATH}\n", mimetype="text/plain")
