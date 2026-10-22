"""Verified biomes release bundles: fetch over https, check sha256/cosign, extract safely.

A bundle is a gzip tarball with exactly one top-level directory (the biomes repo
root). Only ``_lib``, ``installer`` and directories containing ``biome.json`` are
cached; everything else (tests, docs, tools) is ignored. Extraction uses
``tarfile`` with ``filter="data"``; any unsafe member is rejected.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..api import biomes as _biomes

MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 2000
FETCH_TIMEOUT_SECONDS = 60.0

#: Top-level directories cached even without a ``biome.json`` manifest.
ALWAYS_CACHED_DIRS: frozenset[str] = frozenset({"_lib", "installer"})
#: The installer served at ``/api/v1/install/gough-install.py``; required in every bundle.
INSTALLER_BIOME = "installer"
INSTALLER_PATH = "gough-install.py"

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_BIOME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")


class BundleError(Exception):
    """Base error for bundle import and cache lookups; carries a client-safe message."""


class BundleHashMismatch(BundleError):
    """Raised when the downloaded bundle does not match the pinned sha256."""


class BundleSignatureError(BundleError):
    """Raised when cosign verification of the bundle fails or is misconfigured."""


class BundleCacheKeyError(BundleError):
    """Raised for an invalid version, biome, or file path in a cache request."""


@dataclass(slots=True, frozen=True)
class BundleFile:
    """One verified file from a bundle, addressed by biome directory and relative path."""

    biome: str
    path: str
    sha256: str
    content: bytes


@dataclass(slots=True, frozen=True)
class VerifiedBundle:
    """A bundle that passed hash (and optional signature) checks, with its cached files."""

    version: str
    sha256: str
    signature_verified: bool
    files: tuple[BundleFile, ...]


def validate_version(version: str) -> str:
    """Return ``version`` if it is a safe release label, else raise BundleCacheKeyError."""
    if not _VERSION_RE.fullmatch(version or ""):
        raise BundleCacheKeyError(f"invalid biomes version {version!r}")
    return version


def validate_cache_key(biome: str, path: str) -> tuple[str, str]:
    """Validate a cache lookup's biome and relative path against traversal and malformed input.

    Rejects ``..``, absolute paths, backslashes, NUL/control characters and empty
    segments before any lookup happens.
    """
    if not _BIOME_RE.fullmatch(biome or ""):
        raise BundleCacheKeyError(f"invalid biome name {biome!r}")
    if not path or "\\" in path or "\x00" in path or path.startswith("/"):
        raise BundleCacheKeyError("invalid file path")
    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", "..") or not _SEGMENT_RE.fullmatch(segment):
            raise BundleCacheKeyError("invalid file path")
    return biome, "/".join(segments)


def verify_sha256(data: bytes, expected: str) -> str:
    """Return the sha256 of ``data``; raise BundleHashMismatch unless it equals ``expected``."""
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, expected.lower()):
        raise BundleHashMismatch(
            f"bundle sha256 mismatch: expected {expected.lower()}, got {actual}"
        )
    return actual


async def fetch_https(
    url: str,
    *,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """Download an https URL with a size cap; redirects and http are refused.

    ``transport`` exists so tests can inject an httpx MockTransport.
    """
    if not url.startswith("https://"):
        raise BundleError("bundle URLs must use https")
    timeout = httpx.Timeout(FETCH_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise BundleError(f"fetch returned HTTP {response.status_code}")
            buf = bytearray()
            async for chunk in response.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise BundleError("download exceeds the size limit")
    return bytes(buf)


async def verify_cosign_blob(blob_path: Path, signature: bytes, key_id: str, workdir: Path) -> None:
    """Verify a bundle with ``cosign verify-blob``; raise BundleSignatureError on any failure.

    Reuses the biome signing pipeline's key resolution and subprocess boundary
    (``app.api.biomes``), so the same key scheme and fail-closed contract apply.
    """
    sig_path = workdir / "bundle.sig"
    sig_path.write_bytes(signature)
    argv = [
        _biomes._COSIGN_BIN,
        "verify-blob",
        "--key",
        _biomes._cosign_key_ref(key_id),
        "--signature",
        str(sig_path),
        "--",
        str(blob_path),
    ]
    try:
        await _biomes._run_subprocess(argv, timeout=_biomes._COSIGN_TIMEOUT_SECONDS)
    except _biomes.CosignError as exc:
        raise BundleSignatureError(f"bundle signature verification failed: {exc}") from exc


def _check_member_names(names: list[str]) -> str:
    """Return the single top-level directory shared by all members, or raise BundleError."""
    normalized = [name[2:] if name.startswith("./") else name for name in names]
    roots = {name.split("/", 1)[0] for name in normalized if name not in ("", ".")}
    if len(roots) != 1:
        raise BundleError("bundle must contain exactly one top-level directory")
    (root,) = roots
    if not _SEGMENT_RE.fullmatch(root):
        raise BundleError(f"unsafe top-level directory name {root!r}")
    return root


def extract_bundle(data: bytes, workdir: Path) -> tuple[BundleFile, ...]:
    """Extract a verified bundle into ``workdir`` and return the cacheable files.

    Raises BundleError for non-gzip input, unsafe members (links, devices, traversal
    rejected by ``filter="data"``), size or count limits, or a missing installer.
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise BundleError(f"bundle is not a gzip tarball: {exc}") from exc

    with tar:
        members = tar.getmembers()
        if not members:
            raise BundleError("bundle is empty")
        root = _check_member_names([m.name for m in members])
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise BundleError(f"unsupported bundle member type: {member.name!r}")
        if sum(m.size for m in members if m.isfile()) > MAX_EXTRACTED_BYTES:
            raise BundleError("bundle exceeds the extracted size limit")
        try:
            tar.extractall(workdir, filter="data")
        except tarfile.TarError as exc:
            raise BundleError(f"unsafe bundle member rejected: {exc}") from exc

    root_dir = workdir / root
    files: list[BundleFile] = []
    for biome_dir in sorted(p for p in root_dir.iterdir() if p.is_dir()):
        if biome_dir.name not in ALWAYS_CACHED_DIRS and not (biome_dir / "biome.json").is_file():
            continue
        if not _BIOME_RE.fullmatch(biome_dir.name):
            raise BundleError(f"unsafe biome directory name {biome_dir.name!r}")
        for file_path in sorted(p for p in biome_dir.rglob("*") if not p.is_dir()):
            if file_path.is_symlink() or not file_path.is_file():
                raise BundleError(f"unsupported entry in biome {biome_dir.name!r}")
            content = file_path.read_bytes()
            if len(content) > MAX_FILE_BYTES:
                raise BundleError(f"file exceeds the size limit: {file_path.name}")
            rel = file_path.relative_to(biome_dir).as_posix()
            validate_cache_key(biome_dir.name, rel)
            files.append(
                BundleFile(
                    biome=biome_dir.name,
                    path=rel,
                    sha256=hashlib.sha256(content).hexdigest(),
                    content=content,
                )
            )
        if len(files) > MAX_FILES:
            raise BundleError("bundle exceeds the file count limit")

    if not any(f.biome == INSTALLER_BIOME and f.path == INSTALLER_PATH for f in files):
        raise BundleError(f"bundle is missing {INSTALLER_BIOME}/{INSTALLER_PATH}")
    return tuple(files)
