"""Operator mirror configuration for airgapped Cilium and registry installs.

Every artifact the plan compiler renders is fetched from an operator-controlled
mirror and verified by sha256 before it is applied. Nothing is fetched from
public upstream hosts at runtime.
Settings come from environment variables so the hub stays stateless.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# https only, and no characters that could break out of a shell-quoted argument.
_HTTPS_URL_RE = re.compile(r"^https://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+$")
DEFAULT_MIRRORED_REGISTRIES: tuple[str, ...] = (
    "docker.io",
    "quay.io",
    "registry.k8s.io",
    "ghcr.io",
)


class MirrorConfigError(ValueError):
    """Raised when a required airgap mirror setting is missing or malformed."""


@dataclass(slots=True, frozen=True)
class MirrorConfig:
    """Validated operator mirror settings used to render Cilium and registry install steps."""

    chart_mirror_url: str
    cilium_chart_sha256: str
    cilium_crds_url: str
    cilium_crds_sha256: str
    gateway_api_crds_url: str
    gateway_api_crds_sha256: str
    registry_mirror: str
    mirrored_registries: tuple[str, ...]

    def cilium_chart_url(self, version: str) -> str:
        """Return the chart tarball URL on the mirror for ``version``."""
        return f"{self.chart_mirror_url.rstrip('/')}/cilium-{version}.tgz"

    @property
    def registry_host(self) -> str:
        """Return the mirror's ``host[:port]``, used to build digest-pinned image references."""
        return urlsplit(self.registry_mirror).netloc


def _require(env: Mapping[str, str], key: str) -> str:
    """Return a non-empty env value or raise MirrorConfigError naming the key."""
    value = (env.get(key) or "").strip()
    if not value:
        raise MirrorConfigError(f"{key} is required for airgap installs")
    return value


def _https(env: Mapping[str, str], key: str) -> str:
    """Return a required https URL from env, rejecting anything else."""
    value = _require(env, key)
    if not _HTTPS_URL_RE.fullmatch(value):
        raise MirrorConfigError(f"{key} must be an https URL without spaces or quotes")
    return value


def _sha256(env: Mapping[str, str], key: str) -> str:
    """Return a required lowercase 64-hex sha256 from env."""
    value = _require(env, key).lower()
    if not _SHA256_RE.fullmatch(value):
        raise MirrorConfigError(f"{key} must be a 64-character hex sha256")
    return value


def load_mirror_config(env: Mapping[str, str] | None = None) -> MirrorConfig:
    """Load and validate mirror settings from the environment (or ``env`` for tests).

    Raises MirrorConfigError naming the first missing or malformed key. There are
    no upstream fallbacks: an unconfigured mirror fails the render loudly.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    registries_raw = (source.get("GOUGH_MIRRORED_REGISTRIES") or "").strip()
    registries = (
        tuple(r.strip() for r in registries_raw.split(",") if r.strip())
        if registries_raw
        else DEFAULT_MIRRORED_REGISTRIES
    )
    return MirrorConfig(
        chart_mirror_url=_https(source, "GOUGH_CHART_MIRROR_URL"),
        cilium_chart_sha256=_sha256(source, "GOUGH_CILIUM_CHART_SHA256"),
        cilium_crds_url=_https(source, "GOUGH_CILIUM_CRDS_URL"),
        cilium_crds_sha256=_sha256(source, "GOUGH_CILIUM_CRDS_SHA256"),
        gateway_api_crds_url=_https(source, "GOUGH_GATEWAY_API_CRDS_URL"),
        gateway_api_crds_sha256=_sha256(source, "GOUGH_GATEWAY_API_CRDS_SHA256"),
        registry_mirror=_https(source, "GOUGH_REGISTRY_MIRROR"),
        mirrored_registries=registries,
    )
