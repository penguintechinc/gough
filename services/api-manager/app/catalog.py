"""Seeds the four built-in biomes if they don't already exist.

This module provides idempotent seeding of the standard Gough built-in biomes:
- k8s-controller: Infrastructure biome for Kubernetes control plane (formerly k8s-primary)
- k8s-worker: Infrastructure biome for Kubernetes worker nodes
- nest-agent: Infrastructure agent for cluster observability
- longhorn-agent: Application agent for distributed storage

All built-in biomes are pre-marked as signature_verified=True since they are
shipped with the product and validated at build time.

Airgap: the Cilium chart, CRD bundle and registry are NOT referenced here by
upstream URL. They come from operator mirrors configured in ``app.mirrors``.
The digests below are the 1.20.1 multi-arch index digests (verified with
``docker buildx imagetools inspect``) and are informational pins.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone

log = logging.getLogger(__name__)

#: Cilium release pinned by the built-in CNI. Matches the biomes repo (``cilium_version``).
CILIUM_VERSION = "1.20.1"

#: Deprecated biome names accepted on input for one release. Maps to the canonical name.
#: Airgap image digests for Cilium 1.20.1 (biomes repo airgap matrix, rendered with helm template).
#: Keys are mirror image names. The operator key is the repository WITHOUT the "-generic"
#: suffix: the chart appends it, so writing it twice would pull a non-existent image.
CILIUM_IMAGE_DIGESTS: Mapping[str, str] = {
    "cilium": "sha256:ae9ea21f7427fe24bc6ea7247eb552157a1b0a431744045d3f641545ca71d11b",
    "operator": "sha256:6c3885fc7b629099fdbe2a5c87869c86feb825fa18fae299eac0f61918d16ecf",
    "cilium-envoy": "sha256:75b8094c7127736a2ffd2dce3945e0931cb6df21b0372ff661940eca26730b91",
    "hubble-relay": "sha256:59be0ae7d475ab9011a5e954618c0f27b5778b17140381425b308b55ba4917f4",
    "certgen": "sha256:6213a4c54a1f36e14a9280765f058aaa2017550c28bc122f5b09ad146fd0da2b",
}

#: Envoy tag that matches the pinned digest (documentation; the digest is what is pulled).
CILIUM_ENVOY_TAG = "v1.37.5-1786810558-766ccfb37260a43e9d228837aa84ce3faf9f64e7"

LEGACY_BIOME_ALIASES: Mapping[str, str] = {"k8s-primary": "k8s-controller"}


def canonical_biome_name(name: str) -> str:
    """Return the canonical biome name, mapping deprecated aliases with a warning.

    Input that is not a known alias is returned unchanged. A deprecated name is
    accepted for one release so existing callers and stored rows keep working;
    the warning makes the remaining callers visible for removal.
    """
    canonical = LEGACY_BIOME_ALIASES.get(name)
    if canonical is None:
        return name
    log.warning(
        "deprecated biome name %r accepted; use %r (alias removed in the next release)",
        name,
        canonical,
    )
    return canonical


BUILTIN_BIOMES = [
    {
        "name": "k8s-controller",
        "biome_kind": "infrastructure",
        "phase": "post_deploy",
        "lock_to_host": True,
        "emits_joiner_secrets": True,
        "default_frontend": {
            "mode": "kube-vip",
            "kube_vip_image_digest": "sha256:840305b94ef2a89abb3b7fd2b09edfbde690d90052020da4dff90679fe892da2",
            "kube_vip_version": "v0.8.8",
        },
        "default_cni": {
            "mode": "cilium",
            "version": CILIUM_VERSION,
            # Chart, CRD bundle and checksums come from hub mirror settings
            # (GOUGH_CHART_MIRROR_URL, GOUGH_CILIUM_CHART_SHA256, GOUGH_CILIUM_CRDS_URL,
            # GOUGH_CILIUM_CRDS_SHA256); no upstream chart repository is referenced.
            "chart_source": "mirror",
            "image_digests": {
                "cilium": f"quay.io/cilium/cilium@{CILIUM_IMAGE_DIGESTS['cilium']}",
                "operator_generic": f"quay.io/cilium/operator-generic@{CILIUM_IMAGE_DIGESTS['operator']}",
                "cilium_envoy": f"quay.io/cilium/cilium-envoy@{CILIUM_IMAGE_DIGESTS['cilium-envoy']}",
                "hubble_relay": f"quay.io/cilium/hubble-relay@{CILIUM_IMAGE_DIGESTS['hubble-relay']}",
                "certgen": f"quay.io/cilium/certgen@{CILIUM_IMAGE_DIGESTS['certgen']}",
                "kubectl": "registry.k8s.io/kubectl@sha256:6e792b13c92ee7822f67e8dc22b51848af3ae0b43a1f0b8205bb727caa4ceefb",
            },
            "values": {
                "kubeProxyReplacement": True,
                "k8sServiceHost": "<templated>",
                "k8sServicePort": "6443",
                "ipam": {"mode": "kubernetes"},
                "ipv4": {"enabled": True},
                "ipv6": {"enabled": False},
                "l2announcements": {"enabled": True},
                "externalIPs": {"enabled": True},
                "loadBalancer": {"algorithm": "maglev", "mode": "snat"},
                "bpf": {"masquerade": True},
                "socketLB": {"enabled": True},
                "nodePort": {"enabled": True},
                "hostServices": {"enabled": True},
                "envoyConfig": {"enabled": True},
                "operator": {"replicas": 1},
                "hubble": {"enabled": True, "relay": {"enabled": True}},
                "gatewayAPI": {"enabled": True},
            },
        },
    },
    {
        "name": "k8s-worker",
        "biome_kind": "infrastructure",
        "phase": "post_deploy",
        "lock_to_host": False,
        "emits_joiner_secrets": False,
    },
    {
        "name": "nest-agent",
        "biome_kind": "infrastructure",
        "phase": "always_on",
        "lock_to_host": False,
        "emits_joiner_secrets": False,
    },
    {
        "name": "longhorn-agent",
        "biome_kind": "application",
        "phase": "always_on",
        "lock_to_host": False,
        "emits_joiner_secrets": False,
    },
]


def seed_builtin_biomes(db: object) -> None:
    """Insert built-in biomes that don't exist yet. Idempotent.

    Args:
        db: PyDAL database instance (penguin-dal DAL object).

    Each biome is inserted with signature_verified=True and published_at set
    to indicate they are pre-validated product components.

    Note: default_frontend and default_cni are intentionally not persisted to the
    biomes table; they are provided in BUILTIN_BIOMES for reference and will be
    resolved at runtime via biome-specific configuration or defaults.
    """
    for spec in BUILTIN_BIOMES:
        # Check if biome already exists by name
        existing = db(db.biomes.name == spec["name"]).select().first()
        if not existing:
            db.biomes.insert(
                name=spec["name"],
                biome_kind=spec["biome_kind"],
                phase=spec["phase"],
                lock_to_host=spec["lock_to_host"],
                emits_joiner_secrets=spec["emits_joiner_secrets"],
                signature_verified=True,
                published_at=datetime.now(timezone.utc),
            )
    db.commit()
