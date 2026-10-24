"""Egress policy for biome bundle sources: allowlist, https-only, blocked addresses, pinning, redirects.

Reuses the SSRF blocklist from app.clients.cluster_discovery (commit 517cf1e) and its
resolve-once, connect-to-pinned-IP pattern. DNS and HTTP are faked; no network is touched.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from typing import Any

import httpx
import pytest

from app.services import biome_bundle
from app.services.biome_bundle import (
    BundleSourceRejected,
    allowed_bundle_hosts,
    fetch_vetted,
    vet_source,
)
from app.workers import biome_bundle_importer as importer

MIRROR = "https://mirror.airgap.test/biomes/biomes-1.0.0.tar.gz"
GENERIC = "bundle source rejected by egress policy"


def _fixed_resolver(*addresses: str) -> Any:
    """Return a resolver that always answers with ``addresses`` and counts its calls."""
    calls: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append((host, port))
        return list(addresses)

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _ok_transport(seen: list[httpx.Request], body: bytes = b"data") -> httpx.MockTransport:
    """Return a transport that records each request and answers 200 with ``body``."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Allowlist and scheme
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_not_on_allowlist_is_rejected_before_resolution() -> None:
    """A host outside the operator allowlist is refused and DNS is never consulted."""
    resolver = _fixed_resolver("93.184.216.34")
    with pytest.raises(BundleSourceRejected) as info:
        await vet_source(
            "https://evil.example.net/b.tar.gz",
            allowed=frozenset({"mirror.airgap.test"}),
            resolver=resolver,
        )
    assert str(info.value) == GENERIC
    assert resolver.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_empty_allowlist_rejects_every_url_import() -> None:
    """With no allowlist configured, URL imports are refused outright."""
    with pytest.raises(BundleSourceRejected):
        await vet_source(MIRROR, allowed=frozenset(), resolver=_fixed_resolver("93.184.216.34"))


@pytest.mark.asyncio
async def test_http_scheme_rejected_even_when_allowlisted() -> None:
    """Only https is accepted, regardless of the allowlist."""
    with pytest.raises(BundleSourceRejected):
        await vet_source(
            "http://mirror.airgap.test/b.tar.gz",
            allowed=frozenset({"mirror.airgap.test"}),
            resolver=_fixed_resolver("93.184.216.34"),
        )


@pytest.mark.asyncio
async def test_allowlist_match_is_exact_not_suffix() -> None:
    """An allowlisted parent domain does not admit a sibling or a suffix-spoofed host."""
    for url in ("https://airgap.test/x", "https://mirror.airgap.test.evil.example/x"):
        with pytest.raises(BundleSourceRejected):
            await vet_source(url, allowed=frozenset({"mirror.airgap.test"}), resolver=_fixed_resolver("93.184.216.34"))


def test_default_allowlist_derives_from_mirror_urls() -> None:
    """Unset GOUGH_BUNDLE_MIRROR_HOSTS falls back to the hosts of the configured mirror URLs."""
    env = {
        "GOUGH_CHART_MIRROR_URL": "https://charts.airgap.test/cilium",
        "GOUGH_FILE_MIRROR_URL": "https://files.airgap.test:8443/",
    }
    assert allowed_bundle_hosts(env) == frozenset({"charts.airgap.test", "files.airgap.test"})


def test_explicit_blank_allowlist_overrides_mirror_defaults() -> None:
    """A blank GOUGH_BUNDLE_MIRROR_HOSTS means reject all, even when mirror URLs are set."""
    env = {"GOUGH_BUNDLE_MIRROR_HOSTS": "  ", "GOUGH_CHART_MIRROR_URL": "https://charts.airgap.test"}
    assert allowed_bundle_hosts(env) == frozenset()


# ---------------------------------------------------------------------------
# Blocked addresses (always refused, even for allowlisted hosts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked",
    ["169.254.169.254", "169.254.10.10", "127.0.0.1", "fe80::1", "::1"],
)
async def test_blocked_address_rejected_even_when_host_allowlisted(blocked: str) -> None:
    """Loopback, link-local (incl. 169.254/16 and fe80::/10) and metadata are refused always."""
    with pytest.raises(BundleSourceRejected) as info:
        await vet_source(
            "https://imds.airgap.test/b.tar.gz",
            allowed=frozenset({"imds.airgap.test"}),
            resolver=_fixed_resolver(blocked),
        )
    assert str(info.value) == GENERIC


@pytest.mark.asyncio
async def test_one_blocked_address_among_many_rejects_the_whole_host() -> None:
    """A mixed answer containing any blocked address is refused, not filtered."""
    with pytest.raises(BundleSourceRejected):
        await vet_source(
            MIRROR,
            allowed=frozenset({"mirror.airgap.test"}),
            resolver=_fixed_resolver("93.184.216.34", "169.254.169.254"),
        )


@pytest.mark.asyncio
async def test_private_rfc1918_allowed_only_for_allowlisted_host() -> None:
    """An airgapped mirror on RFC1918 space is accepted because its host is explicitly allowlisted."""
    source = await vet_source(
        MIRROR,
        allowed=frozenset({"mirror.airgap.test"}),
        resolver=_fixed_resolver("10.20.30.40"),
    )
    assert source.ip == "10.20.30.40"


# ---------------------------------------------------------------------------
# Pinning: DNS rebinding and single resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dns_rebinding_connects_to_first_resolved_ip_only() -> None:
    """Resolver answers public then private: exactly one lookup, connect to the public IP only."""
    answers = [["93.184.216.34"], ["169.254.169.254"]]
    calls: list[str] = []

    async def rebinding(host: str, port: int) -> list[str]:
        calls.append(host)
        return answers[min(len(calls), len(answers)) - 1]

    seen: list[httpx.Request] = []
    data = await fetch_vetted(
        MIRROR,
        allowed=frozenset({"mirror.airgap.test"}),
        max_bytes=1024,
        transport=_ok_transport(seen),
        resolver=rebinding,
    )
    assert data == b"data"
    assert len(calls) == 1
    assert len(seen) == 1
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "mirror.airgap.test"


@pytest.mark.asyncio
async def test_rebinding_to_metadata_never_connects() -> None:
    """If the single lookup already returns a blocked address, no connection is attempted."""
    seen: list[httpx.Request] = []
    with pytest.raises(BundleSourceRejected):
        await fetch_vetted(
            MIRROR,
            allowed=frozenset({"mirror.airgap.test"}),
            max_bytes=1024,
            transport=_ok_transport(seen),
            resolver=_fixed_resolver("169.254.169.254"),
        )
    assert seen == []


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_to_disallowed_host_is_rejected_not_followed() -> None:
    """A 302 to a non-allowlisted host is refused; the second hop is never requested."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example.net/steal"})

    with pytest.raises(BundleSourceRejected) as info:
        await fetch_vetted(
            MIRROR,
            allowed=frozenset({"mirror.airgap.test"}),
            max_bytes=1024,
            transport=httpx.MockTransport(handler),
            resolver=_fixed_resolver("93.184.216.34"),
        )
    assert str(info.value) == GENERIC
    assert len(seen) == 1
    assert all("evil.example.net" not in str(r.url) for r in seen)


@pytest.mark.asyncio
async def test_redirect_to_metadata_is_rejected() -> None:
    """A redirect toward the metadata address is refused the same way."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": "https://169.254.169.254/latest/meta-data/"})

    with pytest.raises(BundleSourceRejected):
        await fetch_vetted(
            MIRROR,
            allowed=frozenset({"mirror.airgap.test"}),
            max_bytes=1024,
            transport=httpx.MockTransport(handler),
            resolver=_fixed_resolver("93.184.216.34"),
        )


# ---------------------------------------------------------------------------
# Client-facing errors and the import pipeline
# ---------------------------------------------------------------------------


def _tiny_bundle() -> bytes:
    """Return a minimal valid bundle containing only the required installer."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"print('installer')\n"
        info = tarfile.TarInfo("biomes/installer/gough-install.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_import_refuses_non_allowlisted_url_without_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The importer applies the policy end to end: no request leaves for a disallowed host."""
    monkeypatch.setenv("GOUGH_BUNDLE_MIRROR_HOSTS", "mirror.airgap.test")
    monkeypatch.setattr(importer, "_db_version_exists", lambda v: False)
    seen: list[httpx.Request] = []
    with pytest.raises(BundleSourceRejected):
        await importer.import_biome_bundle(
            version="1.0.0",
            bundle_url="https://other.example.net/biomes.tar.gz",
            bundle_sha256=hashlib.sha256(_tiny_bundle()).hexdigest(),
            signature_url=None,
            imported_by="admin-sub",
            transport=_ok_transport(seen, _tiny_bundle()),
            resolver=_fixed_resolver("93.184.216.34"),
        )
    assert seen == []


@pytest.mark.asyncio
async def test_rejection_logs_detail_server_side_only(caplog: pytest.LogCaptureFixture) -> None:
    """The specific reason is logged for operators; the client sees only the generic message."""
    with caplog.at_level("ERROR", logger=biome_bundle.log.name):
        with pytest.raises(BundleSourceRejected) as info:
            await vet_source(
                "https://imds.airgap.test/b.tar.gz",
                allowed=frozenset({"imds.airgap.test"}),
                resolver=_fixed_resolver("169.254.169.254"),
            )
    assert str(info.value) == GENERIC
    assert "169.254.169.254" not in str(info.value)
    assert any("blocked address" in rec.getMessage() for rec in caplog.records)
