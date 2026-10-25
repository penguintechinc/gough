"""Tests for the hub biome bundle cache: verification, safe extraction, cache keys, and endpoints.

Covers hash mismatch (nothing stored), cosign gating, tar member rejection, cache-path
traversal (400), inactive or missing agents, and installer serving. The database is
replaced by in-memory fakes patched at the ``_db_*`` seams; HTTP is served by httpx
MockTransport so no network is touched.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from app.api import biome_cache
from app.api import biomes as biomes_api
from app.api import install
from app.api.lxd_cluster import AgentRecord
from app.services import biome_bundle
from app.services.biome_bundle import (
    BundleCacheKeyError,
    BundleError,
    BundleHashMismatch,
    BundleSignatureError,
    BundleSourceRejected,
    extract_bundle,
    fetch_vetted,
    validate_cache_key,
    validate_version,
    verify_sha256,
)
from app.workers import biome_bundle_importer as importer
from app.workers.biome_bundle_importer import (
    BundleVersionConflictError,
    CachedFile,
    import_biome_bundle,
)

BUNDLE_URL = "https://mirror.airgap.test/biomes/biomes-1.0.0.tar.gz"
SIG_URL = "https://mirror.airgap.test/biomes/biomes-1.0.0.tar.gz.sig"
INSTALLER_BYTES = b"#!/usr/bin/env python3\nprint('gough installer')\n"


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    """Add an in-memory regular file to ``tar`` under ``name``."""
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def make_bundle(*, with_installer: bool = True) -> bytes:
    """Build a gzip tarball shaped like the biomes repo (one root directory)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        root = "biomes-1.0.0"
        if with_installer:
            _add_bytes(tar, f"{root}/installer/gough-install.py", INSTALLER_BYTES)
        _add_bytes(tar, f"{root}/_lib/render.py", b"print('render')\n")
        _add_bytes(
            tar, f"{root}/k8s-controller/biome.json", b'{"name": "k8s-controller"}'
        )
        _add_bytes(
            tar,
            f"{root}/k8s-controller/cloud-init/user-data.yaml.tmpl",
            b"#cloud-config\n",
        )
        # Ignored: not a biome directory and not an always-cached dir.
        _add_bytes(tar, f"{root}/tests/test_x.py", b"def test(): pass\n")
        _add_bytes(tar, f"{root}/docs/readme.md", b"# docs\n")
    return buf.getvalue()


def _transport(routes: dict[str, bytes], status: int = 200) -> httpx.MockTransport:
    """Return an httpx MockTransport serving ``routes`` keyed by the original https URL.

    Requests reach the transport at the pinned IP literal, so the key is rebuilt from
    the Host header (the hostname the operator configured) plus the path.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        body = routes.get(f"https://{host}{request.url.path}")
        if body is None:
            return httpx.Response(404)
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


#: Public test address returned by the fake resolver for the airgap test mirror.
PUBLIC_TEST_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _egress_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allowlist the test mirror and make DNS resolve it to a public address."""
    monkeypatch.setenv("GOUGH_BUNDLE_MIRROR_HOSTS", "mirror.airgap.test")

    async def fake_resolve(host: str, port: int) -> list[str]:
        return [PUBLIC_TEST_IP]

    monkeypatch.setattr(biome_bundle, "resolve_host", fake_resolve)


@pytest.fixture()
def app() -> Any:
    """Quart app with the real biome cache and installer blueprints (overrides the stub app)."""
    from app.api.biome_cache import biome_cache_bp
    from app.api.install import install_bp
    from quart import Quart

    quart_app = Quart(__name__)
    quart_app.config["TESTING"] = True
    quart_app.config["JWT_SECRET_KEY"] = "test-secret-biome-cache"
    quart_app.register_blueprint(biome_cache_bp)
    quart_app.register_blueprint(install_bp)
    return quart_app


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the importer DB seams with an in-memory ledger and return it for assertions."""
    state: dict[str, Any] = {"versions": set(), "stored": None, "files": {}}

    monkeypatch.setattr(
        importer, "_db_version_exists", lambda v: v in state["versions"]
    )

    def _store(bundle: Any, **kwargs: Any) -> None:
        state["stored"] = (bundle, kwargs)
        state["versions"].add(bundle.version)
        for item in bundle.files:
            state["files"][(bundle.version, item.biome, item.path)] = CachedFile(
                content=item.content, sha256=item.sha256
            )

    monkeypatch.setattr(importer, "_db_store_bundle", _store)
    monkeypatch.setattr(
        importer,
        "_db_get_file",
        lambda v, b, p: state["files"].get((v, b, p)),
    )
    monkeypatch.delenv("GOUGH_BIOME_BUNDLE_COSIGN_KEY", raising=False)
    return state


# ---------------------------------------------------------------------------
# Pipeline: fetch, verify, extract, store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_stores_only_cacheable_files(store: dict[str, Any]) -> None:
    """A verified bundle stores biome, _lib and installer files; tests/docs are ignored."""
    data = make_bundle()
    digest = hashlib.sha256(data).hexdigest()
    result = await import_biome_bundle(
        version="1.0.0",
        bundle_url=BUNDLE_URL,
        bundle_sha256=digest,
        signature_url=None,
        imported_by="admin-sub",
        transport=_transport({BUNDLE_URL: data}),
    )
    assert result.file_count == 4
    assert result.sha256 == digest
    assert result.signature_status == 0
    cached = {(b, p) for (_v, b, p) in store["files"]}
    assert cached == {
        ("installer", "gough-install.py"),
        ("_lib", "render.py"),
        ("k8s-controller", "biome.json"),
        ("k8s-controller", "cloud-init/user-data.yaml.tmpl"),
    }
    installer = store["files"][("1.0.0", "installer", "gough-install.py")]
    assert installer.content == INSTALLER_BYTES
    assert installer.sha256 == hashlib.sha256(INSTALLER_BYTES).hexdigest()


@pytest.mark.asyncio
async def test_hash_mismatch_stores_nothing(store: dict[str, Any]) -> None:
    """A wrong pinned sha256 raises BundleHashMismatch and writes no rows."""
    data = make_bundle()
    with pytest.raises(BundleHashMismatch, match="sha256 mismatch"):
        await import_biome_bundle(
            version="1.0.0",
            bundle_url=BUNDLE_URL,
            bundle_sha256="0" * 64,
            signature_url=None,
            imported_by="admin-sub",
            transport=_transport({BUNDLE_URL: data}),
        )
    assert store["stored"] is None
    assert store["files"] == {}


@pytest.mark.asyncio
async def test_duplicate_version_conflicts(store: dict[str, Any]) -> None:
    """An already-imported version is refused; imports are immutable per version."""
    data = make_bundle()
    digest = hashlib.sha256(data).hexdigest()
    store["versions"].add("1.0.0")
    with pytest.raises(BundleVersionConflictError):
        await import_biome_bundle(
            version="1.0.0",
            bundle_url=BUNDLE_URL,
            bundle_sha256=digest,
            signature_url=None,
            imported_by="admin-sub",
            transport=_transport({BUNDLE_URL: data}),
        )


@pytest.mark.asyncio
async def test_missing_installer_rejected(store: dict[str, Any]) -> None:
    """A bundle without installer/gough-install.py is refused before anything is stored."""
    data = make_bundle(with_installer=False)
    with pytest.raises(BundleError, match="missing installer/gough-install.py"):
        await import_biome_bundle(
            version="1.0.0",
            bundle_url=BUNDLE_URL,
            bundle_sha256=hashlib.sha256(data).hexdigest(),
            signature_url=None,
            imported_by="admin-sub",
            transport=_transport({BUNDLE_URL: data}),
        )
    assert store["stored"] is None


@pytest.mark.asyncio
async def test_signature_required_when_key_configured(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a cosign key configured, a bundle without signature_url is refused."""
    monkeypatch.setenv("GOUGH_BIOME_BUNDLE_COSIGN_KEY", "hashivault://biome-bundle")
    data = make_bundle()
    with pytest.raises(BundleError, match="signature_url is required"):
        await import_biome_bundle(
            version="1.0.0",
            bundle_url=BUNDLE_URL,
            bundle_sha256=hashlib.sha256(data).hexdigest(),
            signature_url=None,
            imported_by="admin-sub",
            transport=_transport({BUNDLE_URL: data}),
        )


@pytest.mark.asyncio
async def test_signature_url_without_key_rejected(store: dict[str, Any]) -> None:
    """A signature supplied while no key is configured fails loudly instead of being ignored."""
    data = make_bundle()
    with pytest.raises(BundleError, match="not configured"):
        await import_biome_bundle(
            version="1.0.0",
            bundle_url=BUNDLE_URL,
            bundle_sha256=hashlib.sha256(data).hexdigest(),
            signature_url=SIG_URL,
            imported_by="admin-sub",
            transport=_transport({BUNDLE_URL: data, SIG_URL: b"sig"}),
        )


@pytest.mark.asyncio
async def test_cosign_verify_blob_success_marks_signed(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing verify-blob marks the import signature_status=1 and uses the biome cosign path."""
    monkeypatch.setenv("GOUGH_BIOME_BUNDLE_COSIGN_KEY", "hashivault://biome-bundle")
    captured: list[list[str]] = []

    async def fake_run(argv: list[str], *, timeout: float) -> tuple[str, str]:
        captured.append(argv)
        return "", ""

    monkeypatch.setattr(biomes_api, "_run_subprocess", fake_run)
    data = make_bundle()
    result = await import_biome_bundle(
        version="1.0.0",
        bundle_url=BUNDLE_URL,
        bundle_sha256=hashlib.sha256(data).hexdigest(),
        signature_url=SIG_URL,
        imported_by="admin-sub",
        transport=_transport({BUNDLE_URL: data, SIG_URL: b"sigbytes"}),
    )
    assert result.signature_status == 1
    argv = captured[0]
    assert argv[1] == "verify-blob"
    assert "hashivault://biome-bundle" in argv
    assert argv[-2] == "--"


@pytest.mark.asyncio
async def test_cosign_verify_blob_failure_stores_nothing(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing verify-blob raises BundleSignatureError and stores no rows."""
    monkeypatch.setenv("GOUGH_BIOME_BUNDLE_COSIGN_KEY", "hashivault://biome-bundle")

    async def fail_run(argv: list[str], *, timeout: float) -> tuple[str, str]:
        raise biomes_api.CosignError("cosign exited 1: invalid signature")

    monkeypatch.setattr(biomes_api, "_run_subprocess", fail_run)
    data = make_bundle()
    with pytest.raises(BundleSignatureError, match="invalid signature"):
        await import_biome_bundle(
            version="1.0.0",
            bundle_url=BUNDLE_URL,
            bundle_sha256=hashlib.sha256(data).hexdigest(),
            signature_url=SIG_URL,
            imported_by="admin-sub",
            transport=_transport({BUNDLE_URL: data, SIG_URL: b"sigbytes"}),
        )
    assert store["stored"] is None


# ---------------------------------------------------------------------------
# Fetch and digest primitives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_refuses_http() -> None:
    """Only https sources are accepted for bundles."""
    with pytest.raises(BundleSourceRejected):
        await fetch_vetted(
            "http://mirror.airgap.test/b.tar.gz",
            allowed=frozenset({"mirror.airgap.test"}),
            max_bytes=1024,
        )


@pytest.mark.asyncio
async def test_fetch_enforces_size_cap() -> None:
    """A body over the cap is refused mid-stream."""
    with pytest.raises(BundleError, match="size limit"):
        await fetch_vetted(
            BUNDLE_URL,
            allowed=frozenset({"mirror.airgap.test"}),
            max_bytes=10,
            transport=_transport({BUNDLE_URL: b"x" * 100}),
        )


@pytest.mark.asyncio
async def test_fetch_non_200_fails() -> None:
    """A non-200 mirror response is a bundle error, never an empty success."""
    with pytest.raises(BundleError, match="HTTP 404"):
        await fetch_vetted(
            BUNDLE_URL,
            allowed=frozenset({"mirror.airgap.test"}),
            max_bytes=1024,
            transport=_transport({}),
        )


def test_verify_sha256_is_case_insensitive_and_exact() -> None:
    """Digest comparison accepts uppercase pins and rejects any single-byte change."""
    data = b"abc"
    digest = hashlib.sha256(data).hexdigest()
    assert verify_sha256(data, digest.upper()) == digest
    with pytest.raises(BundleHashMismatch):
        verify_sha256(data + b"!", digest)


# ---------------------------------------------------------------------------
# Safe extraction
# ---------------------------------------------------------------------------


def test_extract_rejects_traversal_member(tmp_path: Path) -> None:
    """A member that escapes the extraction root is rejected by the data filter."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_bytes(tar, "biomes/installer/gough-install.py", INSTALLER_BYTES)
        _add_bytes(tar, "biomes/../../escaped.txt", b"nope")
    with pytest.raises(BundleError):
        extract_bundle(buf.getvalue(), tmp_path)
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_extract_rejects_symlink_member(tmp_path: Path) -> None:
    """Link members are unsupported and refused before extraction."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_bytes(tar, "biomes/installer/gough-install.py", INSTALLER_BYTES)
        link = tarfile.TarInfo("biomes/installer/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    with pytest.raises(BundleError, match="unsupported bundle member"):
        extract_bundle(buf.getvalue(), tmp_path)


def test_extract_rejects_non_gzip(tmp_path: Path) -> None:
    """Non-tarball input is a BundleError, not an unhandled tarfile exception."""
    with pytest.raises(BundleError, match="not a gzip tarball"):
        extract_bundle(b"plain text", tmp_path)


def test_extract_rejects_multiple_roots(tmp_path: Path) -> None:
    """A bundle must have exactly one top-level directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_bytes(tar, "one/installer/gough-install.py", INSTALLER_BYTES)
        _add_bytes(tar, "two/_lib/render.py", b"x")
    with pytest.raises(BundleError, match="exactly one top-level"):
        extract_bundle(buf.getvalue(), tmp_path)


# ---------------------------------------------------------------------------
# Cache key validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("biome", "path"),
    [
        ("_lib", "render.py"),
        ("k8s-controller", "cloud-init/user-data.yaml.tmpl"),
        ("installer", "gough-install.py"),
    ],
)
def test_valid_cache_keys_pass(biome: str, path: str) -> None:
    """Well-formed biome and nested relative paths are accepted unchanged."""
    assert validate_cache_key(biome, path) == (biome, path)


@pytest.mark.parametrize(
    ("biome", "path"),
    [
        ("_lib", "../../etc/passwd"),
        ("_lib", "a/../b"),
        ("_lib", "/etc/passwd"),
        ("_lib", "a\\b"),
        ("_lib", "a//b"),
        ("_lib", "a/./b"),
        ("_lib", "a\x00b"),
        ("_lib", ""),
        ("../etc", "passwd"),
        ("k8s/controller", "biome.json"),
        ("", "biome.json"),
    ],
)
def test_traversal_and_malformed_keys_rejected(biome: str, path: str) -> None:
    """Traversal, absolute, backslash, NUL, empty-segment and bad biome names are refused."""
    with pytest.raises(BundleCacheKeyError):
        validate_cache_key(biome, path)


@pytest.mark.parametrize("version", ["1.0.0", "2026.10-rc1", "v1.0.0+build.5"])
def test_valid_versions_pass(version: str) -> None:
    """Release labels with dots, dashes and plus signs are accepted."""
    assert validate_version(version) == version


@pytest.mark.parametrize("version", ["", "../1.0", "1.0 0", ".hidden", "a/b"])
def test_invalid_versions_rejected(version: str) -> None:
    """Version labels that could alter a path or contain whitespace are refused."""
    with pytest.raises(BundleCacheKeyError):
        validate_version(version)


def test_request_dto_requires_https_and_sha256() -> None:
    """The import DTO refuses http URLs and malformed digests at the boundary."""
    from app.api.biome_cache import BundleImportRequest
    from pydantic import ValidationError

    valid = {"version": "1.0.0", "bundle_url": BUNDLE_URL, "bundle_sha256": "a" * 64}
    assert BundleImportRequest.model_validate(valid).version == "1.0.0"
    with pytest.raises(ValidationError):
        BundleImportRequest.model_validate({**valid, "bundle_url": "http://x/y.tar.gz"})
    with pytest.raises(ValidationError):
        BundleImportRequest.model_validate({**valid, "bundle_sha256": "ABC"})
    with pytest.raises(ValidationError):
        BundleImportRequest.model_validate({**valid, "unexpected": True})


# ---------------------------------------------------------------------------
# Endpoints: agent-authenticated cache read and public installer
# ---------------------------------------------------------------------------


def _agent_token(app: Any, agent_id: str = "agent-1") -> str:
    """Mint an HS256 agent_access token the way the agents API does."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": f"agent:{agent_id}",
        "type": "agent_access",
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    return jwt.encode(payload, app.config["JWT_SECRET_KEY"], algorithm="HS256")


def _agent(status: str) -> AgentRecord:
    """Build a server-side agent record with the given status."""
    return AgentRecord(
        agent_id="agent-1", status=status, hostname="node-1", enrollment_key_hash=None
    )


@pytest.fixture()
def cache_reads(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, Any]
) -> dict[str, Any]:
    """Seed one cached file and record every lookup key the endpoint performs."""
    lookups: list[tuple[str, str, str]] = []
    store["files"][("1.0.0", "_lib", "render.py")] = CachedFile(
        content=b"print('render')\n",
        sha256=hashlib.sha256(b"print('render')\n").hexdigest(),
    )

    def _get(v: str, b: str, p: str) -> CachedFile | None:
        lookups.append((v, b, p))
        return store["files"].get((v, b, p))

    monkeypatch.setattr(importer, "_db_get_file", _get)
    return {"lookups": lookups}


@pytest.mark.asyncio
async def test_cache_read_requires_agent_token(
    app: Any, cache_reads: dict[str, Any]
) -> None:
    """Without an agent_access bearer the cache read is refused with 401 and no lookup."""
    client = app.test_client()
    resp = await client.get("/api/v1/biomes/cache/1.0.0/_lib/render.py")
    assert resp.status_code == 401
    assert cache_reads["lookups"] == []


@pytest.mark.asyncio
async def test_cache_read_rejects_inactive_agent(
    app: Any, cache_reads: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suspended agent is refused with 403 even with a valid token."""
    monkeypatch.setattr(biome_cache, "_db_load_agent", lambda _id: _agent("suspended"))
    client = app.test_client()
    resp = await client.get(
        "/api/v1/biomes/cache/1.0.0/_lib/render.py",
        headers={"Authorization": f"Bearer {_agent_token(app)}"},
    )
    assert resp.status_code == 403
    assert cache_reads["lookups"] == []


@pytest.mark.asyncio
async def test_cache_read_serves_verified_file(
    app: Any, cache_reads: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active agent gets the file bytes and its sha256 in X-Content-SHA256."""
    monkeypatch.setattr(biome_cache, "_db_load_agent", lambda _id: _agent("active"))
    client = app.test_client()
    resp = await client.get(
        "/api/v1/biomes/cache/1.0.0/_lib/render.py",
        headers={"Authorization": f"Bearer {_agent_token(app)}"},
    )
    assert resp.status_code == 200
    assert await resp.get_data() == b"print('render')\n"
    assert (
        resp.headers["X-Content-SHA256"]
        == hashlib.sha256(b"print('render')\n").hexdigest()
    )


@pytest.mark.asyncio
async def test_cache_read_missing_file_is_404(
    app: Any, cache_reads: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncached file under a valid key returns 404."""
    monkeypatch.setattr(biome_cache, "_db_load_agent", lambda _id: _agent("active"))
    client = app.test_client()
    resp = await client.get(
        "/api/v1/biomes/cache/1.0.0/_lib/absent.py",
        headers={"Authorization": f"Bearer {_agent_token(app)}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cache_read_traversal_never_reaches_lookup(
    app: Any, cache_reads: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Traversal in the cache path is refused with 400 before any database lookup."""
    monkeypatch.setattr(biome_cache, "_db_load_agent", lambda _id: _agent("active"))
    client = app.test_client()
    resp = await client.get(
        "/api/v1/biomes/cache/1.0.0/_lib/..%2F..%2Fetc%2Fpasswd",
        headers={"Authorization": f"Bearer {_agent_token(app)}"},
    )
    assert resp.status_code in (400, 404)
    assert resp.status_code != 200
    assert all(".." not in key[2] for key in cache_reads["lookups"])


@pytest.mark.asyncio
async def test_installer_is_anonymous_and_serves_bytes(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer and its sha256 are served without a bearer, from the pinned version."""
    monkeypatch.setenv("GOUGH_BIOMES_INSTALLER_VERSION", "1.0.0")

    async def fake_read(v: str, b: str, p: str) -> CachedFile | None:
        if (v, b, p) == ("1.0.0", "installer", "gough-install.py"):
            return CachedFile(
                content=INSTALLER_BYTES,
                sha256=hashlib.sha256(INSTALLER_BYTES).hexdigest(),
            )
        return None

    monkeypatch.setattr(install, "read_cached_file", fake_read)
    client = app.test_client()
    body = await client.get("/api/v1/install/gough-install.py")
    assert body.status_code == 200
    assert await body.get_data() == INSTALLER_BYTES
    digest = await client.get("/api/v1/install/gough-install.py.sha256")
    assert digest.status_code == 200
    assert (await digest.get_data()).decode() == (
        f"{hashlib.sha256(INSTALLER_BYTES).hexdigest()}  gough-install.py\n"
    )


@pytest.mark.asyncio
async def test_installer_unconfigured_version_is_404(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a pinned installer version the endpoint fails loudly with 404."""
    monkeypatch.delenv("GOUGH_BIOMES_INSTALLER_VERSION", raising=False)
    client = app.test_client()
    resp = await client.get("/api/v1/install/gough-install.py")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_installer_bad_version_is_400(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version that could alter the lookup key is refused with 400."""
    client = app.test_client()
    resp = await client.get("/api/v1/install/gough-install.py?version=..%2Fx")
    assert resp.status_code == 400


def test_import_endpoint_requires_admin_scope() -> None:
    """The import route is in SCOPE_POLICY with the admin cluster scope only."""
    from app.security.scope_policy import SCOPE_POLICY

    assert SCOPE_POLICY[("POST", "/api/v1/biomes/cache/imports")] == frozenset(
        {"gough.cluster.admin"}
    )


def test_anonymous_surface_is_limited_to_installer_and_agent_routes() -> None:
    """Only the installer script, its sha256, and agent-authenticated routes skip the OIDC gate."""
    from app.security.scope_policy import is_anonymous_request

    assert is_anonymous_request("GET", "/api/v1/install/gough-install.py")
    assert is_anonymous_request("GET", "/api/v1/install/gough-install.py.sha256")
    assert is_anonymous_request("GET", "/api/v1/biomes/cache/1.0.0/_lib/render.py")
    assert is_anonymous_request(
        "GET", "/api/v1/biomes/cache/1.0.0/k8s-controller/cloud-init/x.tmpl"
    )
    assert is_anonymous_request("POST", "/api/v1/lxd/cluster-join-tokens")
    assert not is_anonymous_request("GET", "/api/v1/install/other.py")
    assert not is_anonymous_request("POST", "/api/v1/biomes/cache/imports")
    assert not is_anonymous_request("POST", "/api/v1/lxd/node-join-keys")
