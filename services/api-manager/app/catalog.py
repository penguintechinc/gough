"""Seeds the four built-in biomes if they don't already exist.

This module provides idempotent seeding of the standard Gough built-in biomes:
- k8s-primary: Infrastructure biome for Kubernetes primary control plane
- k8s-worker: Infrastructure biome for Kubernetes worker nodes
- nest-agent: Infrastructure agent for cluster observability
- longhorn-agent: Application agent for distributed storage

All built-in biomes are pre-marked as signature_verified=True since they are
shipped with the product and validated at build time.
"""

from __future__ import annotations


BUILTIN_BIOMES = [
    {
        "name": "k8s-primary",
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
            "version": "1.19.1",
            "chart_repo": "https://helm.cilium.io/",
            "image_digests": {
                "cilium": "quay.io/cilium/cilium@sha256:41f1f74a0000de8656f1de4088ea00c8f2d49d6edea579034c73c5fd5fe01792",
                "operator_generic": "quay.io/cilium/operator-generic@sha256:e7278d763e448bf6c184b0682cf98cdca078d58a27e1b2f3c906792670aa211a",
                "hubble_relay": "quay.io/cilium/hubble-relay@sha256:d8c4e13bc36a56179292bb52bc6255379cb94cb873700d316ea3139b1bdb8165",
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
                "enableCiliumEndpointSlice": True,
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
    from datetime import datetime, timezone

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
