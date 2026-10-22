"""Rendered Cilium values for airgap installs: mirror-pinned images, chart-1.20.1 compatible.

Parses the real cilium-values.yaml the plan compiler writes and checks that no upstream
registry name appears, every image resolves to the operator mirror by digest, the
operator repository does not double the chart-appended -generic suffix, and
enableCiliumEndpointSlice (refused by chart 1.20.1) is absent.
"""

from __future__ import annotations

import yaml

from app import catalog
from app.mirrors import MirrorConfig
from app.workers.plan_compiler import _render_cni_install

MIRROR = MirrorConfig(
    chart_mirror_url="https://mirror.airgap.test/charts",
    cilium_chart_sha256="a" * 64,
    cilium_crds_url="https://mirror.airgap.test/cilium/cilium-crds-v1.20.1.yaml",
    cilium_crds_sha256="b" * 64,
    gateway_api_crds_url="https://mirror.airgap.test/gateway/standard-install.yaml",
    gateway_api_crds_sha256="c" * 64,
    registry_mirror="https://registry.airgap.test:5000",
    mirrored_registries=("docker.io", "quay.io", "registry.k8s.io", "ghcr.io"),
)


def _values_yaml() -> str:
    """Render the control-plane Cilium install and return the values file content."""
    write_files, _runcmds, errors = _render_cni_install(
        "cilium", {}, "10.2.0.10:6443", mirror_config=MIRROR
    )
    assert errors == []
    return next(f["content"] for f in write_files if f["path"] == "/etc/gough/cilium-values.yaml")


def test_values_have_no_upstream_registry_names() -> None:
    """Rendered values never name quay.io or registry.k8s.io; images come from the mirror."""
    values = _values_yaml()
    assert "quay.io" not in values
    assert "registry.k8s.io" not in values


def test_values_omit_endpoint_slice_key() -> None:
    """enableCiliumEndpointSlice is refused by chart 1.20.1 and must not be rendered or defaulted."""
    assert "enableCiliumEndpointSlice" not in _values_yaml()
    assert "enableCiliumEndpointSlice" not in catalog.BUILTIN_BIOMES[0]["default_cni"]["values"]


def test_every_image_pinned_by_digest_to_mirror_path() -> None:
    """Cilium, operator, envoy, certgen and hubble-relay resolve to mirror paths with digests."""
    parsed = yaml.safe_load(_values_yaml())
    registry = MIRROR.registry_host
    expected: dict[tuple[str, ...], tuple[str, str]] = {
        ("image",): ("cilium", "cilium"),
        ("operator", "image"): ("operator", "operator"),
        ("envoy", "image"): ("cilium-envoy", "cilium-envoy"),
        ("certgen", "image"): ("certgen", "certgen"),
        ("hubble", "relay", "image"): ("hubble-relay", "hubble-relay"),
    }
    for path, (digest_key, repo_name) in expected.items():
        node = parsed
        for key in path:
            node = node[key]
        assert node["repository"] == f"{registry}/cilium/{repo_name}"
        assert node["useDigest"] is True
        assert node["digest"] == catalog.CILIUM_IMAGE_DIGESTS[digest_key]


def test_operator_repository_does_not_double_generic_suffix() -> None:
    """The chart appends -generic, so the mirror repository must not already end with it."""
    operator_repo = yaml.safe_load(_values_yaml())["operator"]["image"]["repository"]
    assert not operator_repo.endswith("-generic")


def test_catalog_digests_match_airgap_matrix() -> None:
    """Catalog digests are the biomes airgap-matrix values, including envoy and certgen."""
    assert catalog.CILIUM_IMAGE_DIGESTS["cilium-envoy"] == (
        "sha256:75b8094c7127736a2ffd2dce3945e0931cb6df21b0372ff661940eca26730b91"
    )
    assert catalog.CILIUM_IMAGE_DIGESTS["certgen"] == (
        "sha256:6213a4c54a1f36e14a9280765f058aaa2017550c28bc122f5b09ad146fd0da2b"
    )
    cni = catalog.BUILTIN_BIOMES[0]["default_cni"]
    assert "cilium_envoy" in cni["image_digests"]
    assert "certgen" in cni["image_digests"]
