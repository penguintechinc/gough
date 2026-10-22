"""Import a pinned biomes release bundle into the hub's verified cache.

Pipeline (fail-closed, nothing is stored until every check passes):
fetch over https (size-capped) -> sha256 against the pinned value -> cosign
verify-blob when ``GOUGH_BIOME_BUNDLE_COSIGN_KEY`` is set -> safe extraction ->
one database transaction storing the import row and every file.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from ..db.run_db import run_db
from ..models import get_db
from ..services.biome_bundle import (
    MAX_BUNDLE_BYTES,
    BundleError,
    VerifiedBundle,
    extract_bundle,
    fetch_https,
    validate_version,
    verify_cosign_blob,
    verify_sha256,
)

log = logging.getLogger(__name__)

SIGNATURE_NOT_REQUIRED = 0
SIGNATURE_VERIFIED = 1
_COSIGN_KEY_ENV = "GOUGH_BIOME_BUNDLE_COSIGN_KEY"


class BundleVersionConflictError(BundleError):
    """Raised when a version is already imported; imports are immutable per version."""


@dataclass(slots=True, frozen=True)
class ImportResult:
    """Outcome of a successful bundle import, safe to return to the caller."""

    import_public_id: str
    version: str
    sha256: str
    file_count: int
    signature_status: int


@dataclass(slots=True, frozen=True)
class CachedFile:
    """A file served from the cache with its verified digest."""

    content: bytes
    sha256: str


def _db() -> Any:
    """Return the live DAL or fail loudly; bundle reads and writes never run degraded."""
    db = get_db()
    if db is None:
        raise RuntimeError("database unavailable; the biome bundle cache requires it")
    return db


def _db_version_exists(version: str) -> bool:
    """Return True when a bundle with this version is already imported."""
    db = _db()
    return db(db.biome_bundle_imports.version == version).select().first() is not None


def _db_store_bundle(
    bundle: VerifiedBundle,
    *,
    public_id: str,
    source_url: str,
    imported_by: str,
    imported_at: datetime,
) -> None:
    """Insert the import row and all file rows in one transaction; roll back on any error."""
    db = _db()
    try:
        import_id = db.biome_bundle_imports.insert(
            public_id=public_id,
            version=bundle.version,
            source_sha256=bundle.sha256,
            source_url=source_url,
            signature_status=SIGNATURE_VERIFIED if bundle.signature_verified else SIGNATURE_NOT_REQUIRED,
            file_count=len(bundle.files),
            imported_by=imported_by,
            imported_at=imported_at,
        )
        for item in bundle.files:
            db.biome_bundle_files.insert(
                import_id=import_id,
                biome=item.biome,
                path=item.path,
                sha256=item.sha256,
                size_bytes=len(item.content),
                content=item.content,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise


def _db_get_file(version: str, biome: str, path: str) -> Optional[CachedFile]:
    """Return the verified file for (version, biome, path), or None when not cached."""
    db = _db()
    imp = db(db.biome_bundle_imports.version == version).select().first()
    if imp is None:
        return None
    row = db(
        (db.biome_bundle_files.import_id == imp.id)
        & (db.biome_bundle_files.biome == biome)
        & (db.biome_bundle_files.path == path)
    ).select().first()
    if row is None:
        return None
    return CachedFile(content=bytes(row.content), sha256=str(row.sha256))


def _cosign_key() -> Optional[str]:
    """Return the configured bundle cosign key id, or None when signature checks are off."""
    value = (os.getenv(_COSIGN_KEY_ENV) or "").strip()
    return value or None


async def import_biome_bundle(
    *,
    version: str,
    bundle_url: str,
    bundle_sha256: str,
    signature_url: Optional[str],
    imported_by: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ImportResult:
    """Fetch, verify and store one biomes release; raises BundleError subclasses on failure.

    Raises BundleHashMismatch on a sha256 mismatch, BundleSignatureError when cosign
    verification fails or a signature is required but absent, and
    BundleVersionConflictError when the version is already imported.
    """
    validate_version(version)
    data = await fetch_https(bundle_url, max_bytes=MAX_BUNDLE_BYTES, transport=transport)
    digest = verify_sha256(data, bundle_sha256)

    key_id = _cosign_key()
    if signature_url and key_id is None:
        raise BundleError(f"signature_url given but {_COSIGN_KEY_ENV} is not configured")
    if key_id is not None and not signature_url:
        raise BundleError(f"signature_url is required when {_COSIGN_KEY_ENV} is configured")

    if await run_db(lambda: _db_version_exists(version)):
        raise BundleVersionConflictError(f"biomes version {version!r} is already imported")

    with tempfile.TemporaryDirectory(prefix="biome-bundle-") as tmp:
        workdir = Path(tmp)
        signature_verified = False
        if key_id is not None and signature_url:
            blob_path = workdir / "bundle.tar.gz"
            blob_path.write_bytes(data)
            signature = await fetch_https(signature_url, max_bytes=64 * 1024, transport=transport)
            await verify_cosign_blob(blob_path, signature, key_id, workdir)
            signature_verified = True
        extract_dir = workdir / "extract"
        extract_dir.mkdir()
        files = extract_bundle(data, extract_dir)

    bundle = VerifiedBundle(
        version=version,
        sha256=digest,
        signature_verified=signature_verified,
        files=files,
    )
    public_id = str(uuid.uuid4())
    await run_db(
        lambda: _db_store_bundle(
            bundle,
            public_id=public_id,
            source_url=bundle_url,
            imported_by=imported_by,
            imported_at=datetime.utcnow(),
        )
    )
    log.info(
        "biome bundle imported",
        extra={"version": version, "file_count": len(files), "sha256": digest[:12]},
    )
    return ImportResult(
        import_public_id=public_id,
        version=version,
        sha256=digest,
        file_count=len(files),
        signature_status=SIGNATURE_VERIFIED if signature_verified else SIGNATURE_NOT_REQUIRED,
    )


async def read_cached_file(version: str, biome: str, path: str) -> Optional[CachedFile]:
    """Return a cached file for a validated (version, biome, path) key, or None."""
    return await run_db(lambda: _db_get_file(version, biome, path))
