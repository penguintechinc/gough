"""Hub biome bundle cache API.

- ``POST /api/v1/biomes/cache/imports`` (admin, scope ``gough.cluster.admin``) imports
  a pinned biomes release from an https mirror after sha256 (and optional cosign) checks.
- ``GET /api/v1/biomes/cache/{version}/{biome}/{path}`` serves one verified file to an
  enrolled agent. The bearer is an ``agent_access`` token validated in the handler
  (the same contract as ``/agents``), and the agent must be active.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from quart import Blueprint, Response, request

from ..api._helpers import (
    envelope_success,
    err_bad_request,
    err_conflict,
    err_forbidden,
    err_not_found,
    err_validation,
    validate_body,
)
from ..api.agents import _validate_agent_token
from ..api.lxd_cluster import _db_load_agent
from ..db.run_db import run_db
from ..middleware import admin_required, get_current_user
from ..rate_limit import rate_limit
from ..services.biome_bundle import (
    BundleCacheKeyError,
    BundleError,
    BundleHashMismatch,
    BundleSignatureError,
    validate_cache_key,
    validate_version,
)
from ..workers.biome_bundle_importer import (
    BundleVersionConflictError,
    import_biome_bundle,
    read_cached_file,
)

log = logging.getLogger(__name__)

biome_cache_bp = Blueprint("biome_cache", __name__, url_prefix="/api/v1/biomes/cache")

_AGENT_ACTIVE_STATUS = "active"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class BundleImportRequest(BaseModel):
    """Body of a bundle import: a pinned version, its https source, and its sha256."""

    model_config = ConfigDict(extra="forbid")

    version: Annotated[str, Field(min_length=1, max_length=64)]
    bundle_url: Annotated[str, Field(min_length=9, max_length=2048)]
    bundle_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    signature_url: str | None = Field(default=None, max_length=2048)

    @field_validator("bundle_url", "signature_url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        """Reject non-https and whitespace-bearing URLs at the boundary."""
        if value is None:
            return None
        if not value.startswith("https://") or any(c.isspace() for c in value):
            raise ValueError("URLs must be https and contain no whitespace")
        return value


def _actor() -> str:
    """Return the authenticated principal's subject for the import ledger."""
    user = get_current_user() or {}
    return str((user.get("_jwt_payload") or {}).get("sub", str(user.get("id", "unknown"))))


@biome_cache_bp.route("/imports", methods=["POST"])
@admin_required
@rate_limit("5/minute")
async def import_bundle() -> Any:
    """Import a pinned biomes release into the verified cache (admin only).

    Returns 201 with the import summary. Hash mismatch and failed signature checks
    return 422 and store nothing.
    """
    raw = await request.get_json(silent=True)
    if raw is None:
        return err_validation("JSON body required")
    body, err = validate_body(BundleImportRequest, raw)
    if err is not None:
        return err
    assert body is not None
    try:
        result = await import_biome_bundle(
            version=body.version,
            bundle_url=body.bundle_url,
            bundle_sha256=body.bundle_sha256,
            signature_url=body.signature_url,
            imported_by=_actor(),
        )
    except BundleVersionConflictError as exc:
        return err_conflict(str(exc))
    except BundleHashMismatch as exc:
        log.error("biome bundle hash mismatch", extra={"version": body.version})
        return err_bad_request(str(exc))
    except (BundleSignatureError, BundleError) as exc:
        return err_bad_request(str(exc))

    return envelope_success(
        {
            "import_id": result.import_public_id,
            "version": result.version,
            "sha256": result.sha256,
            "file_count": result.file_count,
            "signature_status": result.signature_status,
        },
        status_code=201,
    )


@biome_cache_bp.route("/<string:version>/<string:biome>/<path:path>", methods=["GET"])
@rate_limit("600/minute")
async def get_cached_file(version: str, biome: str, path: str) -> Any:
    """Serve one verified biome file to an active enrolled agent.

    Returns 401 without a valid agent token, 403 for a non-active agent, 400 for a
    malformed key or traversal attempt, and 404 when the file is not cached.
    """
    agent_id = await _validate_agent_token()
    if agent_id is None:
        return Response("authentication required", status=401, mimetype="text/plain")
    agent = await _load_agent(agent_id)
    if agent is None or agent.status != _AGENT_ACTIVE_STATUS:
        return err_forbidden("agent is not active")

    try:
        validate_version(version)
        biome, rel_path = validate_cache_key(biome, path)
    except BundleCacheKeyError as exc:
        return err_bad_request(str(exc))

    cached = await read_cached_file(version, biome, rel_path)
    if cached is None:
        return err_not_found("file is not cached for this biomes version")
    response = Response(cached.content, mimetype="application/octet-stream")
    response.headers["X-Content-SHA256"] = cached.sha256
    response.headers["Cache-Control"] = "private, no-store"
    return response


async def _load_agent(agent_id: str) -> Any:
    """Load the server-side agent record through the shared runner (sync DB, off-loop)."""
    return await run_db(lambda: _db_load_agent(agent_id))
