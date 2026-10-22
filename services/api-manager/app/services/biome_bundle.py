"""Verified biomes release bundles: fetch over https, check sha256/cosign, extract safely.

A bundle is a gzip tarball with exactly one top-level directory (the biomes repo
root). Only ``_lib``, ``installer`` and directories containing ``biome.json`` are
cached; everything else (tests, docs, tools) is ignored. Extraction uses
``tarfile`` with ``filter="data"``; any unsafe member is rejected.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import ipaddress
import logging
import os
import re
import socket
import tarfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..api import biomes as _biomes
from ..clients.cluster_discovery import _is_blocked_ip  # shared SSRF blocklist (commit 517cf1e)

log = logging.getLogger(__name__)

BUNDLE_MIRROR_HOSTS_ENV = "GOUGH_BUNDLE_MIRROR_HOSTS"
_MIRROR_URL_ENVS: tuple[str, ...] = ("GOUGH_CHART_MIRROR_URL", "GOUGH_FILE_MIRROR_URL")

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


class BundleSourceRejected(BundleError):
    """Raised when a bundle URL fails the egress policy (allowlist, https, blocked address, redirect)."""


@dataclass(slots=True, frozen=True)
class PinnedSource:
    """A vetted bundle source: the hostname for Host/SNI and the one resolved IP to connect to."""

    host: str
    ip: str
    port: int
    path: str


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


#: Generic client-facing message for every egress-policy refusal; details go to the server log.
_SOURCE_REJECTED_MESSAGE = "bundle source rejected by egress policy"

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve_host(host: str, port: int) -> list[str]:
    """Resolve ``host`` to its IP addresses (one lookup per import; never repeated)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def allowed_bundle_hosts(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Return the operator allowlist of hosts bundles may be fetched from.

    ``GOUGH_BUNDLE_MIRROR_HOSTS`` (comma-separated) wins when set, even if blank
    (blank means reject every URL import). Otherwise the hosts of
    ``GOUGH_CHART_MIRROR_URL`` and ``GOUGH_FILE_MIRROR_URL`` are used when set.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(BUNDLE_MIRROR_HOSTS_ENV)
    if raw is not None:
        names = raw.split(",")
    else:
        names = [
            urlsplit(source[key]).hostname or ""
            for key in _MIRROR_URL_ENVS
            if source.get(key)
        ]
    return frozenset(name.strip().lower() for name in names if name.strip())


async def vet_source(
    url: str,
    *,
    allowed: frozenset[str],
    resolver: Resolver | None = None,
) -> PinnedSource:
    """Apply the bundle egress policy to ``url`` and pin one resolved address.

    Order: https only, host must be an exact allowlist entry (no wildcards),
    then a single DNS resolution. Any resolved address that is loopback,
    link-local or a cloud-metadata address is refused even when the host is
    allowlisted; private RFC1918 addresses are accepted only because the host
    matched the allowlist exactly. The first resolved address is pinned.
    Refusals log the detail server-side and raise a generic BundleError.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        log.error("bundle source refused: non-https or missing host", extra={"url_scheme": parts.scheme})
        raise BundleSourceRejected(_SOURCE_REJECTED_MESSAGE)
    if host not in allowed:
        log.error("bundle source refused: host not allowlisted", extra={"host": host})
        raise BundleSourceRejected(_SOURCE_REJECTED_MESSAGE)

    port = parts.port or 443
    lookup = resolver if resolver is not None else resolve_host
    try:
        addresses = await lookup(host, port)
    except OSError as exc:
        log.error("bundle source refused: resolution failed", extra={"host": host, "error": str(exc)})
        raise BundleSourceRejected(_SOURCE_REJECTED_MESSAGE) from exc
    if not addresses:
        log.error("bundle source refused: no addresses", extra={"host": host})
        raise BundleSourceRejected(_SOURCE_REJECTED_MESSAGE)

    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            log.error("bundle source refused: unparseable address", extra={"host": host})
            raise BundleSourceRejected(_SOURCE_REJECTED_MESSAGE) from exc
        if _is_blocked_ip(ip):
            log.error(
                "bundle source refused: resolves to blocked address",
                extra={"host": host, "address": str(ip)},
            )
            raise BundleSourceRejected(_SOURCE_REJECTED_MESSAGE)
        parsed.append(ip)

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return PinnedSource(host=host, ip=str(parsed[0]), port=port, path=path)


async def fetch_pinned(
    source: PinnedSource,
    *,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """Fetch from the pinned IP literal with the hostname kept for Host/SNI; never re-resolves.

    Redirects are not followed: any 3xx response is refused, so a redirect to a
    disallowed or private host can never be reached. The body is size-capped.
    """
    literal = f"[{source.ip}]" if ":" in source.ip else source.ip
    url = f"https://{literal}:{source.port}{source.path}"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
        follow_redirects=False,
        transport=transport,
    ) as client:
        async with client.stream(
            "GET",
            url,
            headers={"Host": source.host},
            extensions={"sni_hostname": source.host},
        ) as response:
            if 300 <= response.status_code < 400:
                log.error(
                    "bundle source refused: redirect not followed",
                    extra={"host": source.host, "status": response.status_code},
                )
                raise BundleSourceRejected(_SOURCE_REJECTED_MESSAGE)
            if response.status_code != 200:
                raise BundleError(f"fetch returned HTTP {response.status_code}")
            buf = bytearray()
            async for chunk in response.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise BundleError("download exceeds the size limit")
    return bytes(buf)


async def fetch_vetted(
    url: str,
    *,
    allowed: frozenset[str],
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
) -> bytes:
    """Vet ``url`` against the egress policy, then fetch it from the pinned address."""
    source = await vet_source(url, allowed=allowed, resolver=resolver)
    return await fetch_pinned(source, max_bytes=max_bytes, transport=transport)


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
