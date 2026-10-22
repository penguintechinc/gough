"""Plan compiler — Phase 1→2 handoff orchestration.

Implements the ``PlanCompiler`` class described in the Gough spec sections:
  - Phase 1→2 Handoff: Plan Compilation
  - Biome Model → Dependency Resolution
  - Biome Dependency Graphs (Deep Dive)
  - Joiner / Enrollment Secrets

All validation errors are collected (never raised early); ``compile()`` raises
``PlanCompilationError`` only after all validation is complete and errors exist.

Performance targets (enforced by CI benchmark):
  - 50-biome plan: p95 < 1 s
  - 250-biome plan: p95 < 5 s

Author: Gough api-manager (generated per spec)
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..catalog import CILIUM_IMAGE_DIGESTS, CILIUM_VERSION, canonical_biome_name
from ..mirrors import MirrorConfig, MirrorConfigError, load_mirror_config


# ---------------------------------------------------------------------------
# Public error types
# ---------------------------------------------------------------------------


class PlanCompilationError(Exception):
    """Raised when plan compilation has one or more validation errors.

    ``errors`` contains all collected ``PlanValidationError`` instances.
    """

    def __init__(self, errors: list[PlanValidationError]) -> None:
        """Initialise with a list of validation errors."""
        self.errors = errors
        super().__init__(f"Plan compilation failed with {len(errors)} error(s)")


class LUKSValidationError(ValueError):
    """Raised when LUKS sealing tier parameters are invalid or unsupported."""

    pass


# ---------------------------------------------------------------------------
# Input/output models (Pydantic v2)
# ---------------------------------------------------------------------------


class PlanValidationError(BaseModel):
    """A single structured validation error collected during plan compilation."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class BiomeAssignmentRef(BaseModel):
    """Reference to a biome to include in this plan."""

    biome_id: int
    instance_alias: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _remap_egg_fields(cls, data: Any) -> Any:
        """Backward-compat: accept egg_id and remap to biome_id."""
        if isinstance(data, dict) and "egg_id" in data and "biome_id" not in data:
            data = dict(data)
            data["biome_id"] = data.pop("egg_id")
        return data


# Backward-compat alias
EggAssignmentRef = BiomeAssignmentRef


class DiskPlanInput(BaseModel):
    """Disk partitioning intent provided by the operator at plan time."""

    disk_device: str = Field(default="/dev/sda", description="e.g. /dev/nvme0n1")
    luks_enabled: bool = True
    partitions: list[dict[str, Any]] = Field(default_factory=list)


class PlanRequest(BaseModel):
    """Operator-submitted plan request for a node."""

    biome_assignments: list[BiomeAssignmentRef]
    disk_plan: DiskPlanInput = Field(default_factory=DiskPlanInput)
    reason: str = ""
    cluster_id: str = ""
    actor_sub: str = "system"

    @model_validator(mode="before")
    @classmethod
    def _remap_egg_fields(cls, data: Any) -> Any:
        """Backward-compat: accept egg_assignments and remap to biome_assignments."""
        if isinstance(data, dict) and "egg_assignments" in data and "biome_assignments" not in data:
            data = dict(data)
            data["biome_assignments"] = data.pop("egg_assignments")
        return data


class CompiledPlan(BaseModel):
    """Fully compiled deployment plan ready for Phase-2 handoff."""

    plan_id: str = Field(description="UUIDv7 plan identifier")
    biome_order: list[int] = Field(description="Biome IDs in topological order")
    cloud_init_bundle: str = Field(description="Base64-encoded merged cloud-init YAML")
    disk_script: str = Field(description="sgdisk/sfdisk shell script for Phase 2")
    luks_key_sealed: bytes = Field(description="LUKS key sealed per tier dispatch")
    luks_key_provenance: dict[str, Any] = Field(description="Tier, key name, version")
    lxd_join_token: str = Field(description="Single-use LXD cluster join token")
    phase2_ipxe_script: str = Field(description="iPXE script pointing to deploy image")
    joiner_secret_refs: list[str] = Field(
        description="joiner_secrets UUIDs consumed by this plan"
    )
    compile_warnings: list[str] = Field(description="Non-fatal warnings")

    model_config = {"arbitrary_types_allowed": True}

    @property
    def egg_order(self) -> list[int]:
        """Backward-compat alias for biome_order."""
        return self.biome_order


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_NUMERIC_TAG_RE = re.compile(
    r"^(?P<prefix>(?:mem|nic|cpu|disk|psu|power-budget):[\w:-]+:)(?P<val>\d+)(?P<suffix>[a-z]*)$"
)

# Phase ordering — earlier phases cannot depend on later phases
_PHASE_ORDER: dict[str, int] = {
    "phase1_helper": 0,
    "phase2_initial": 1,
    "post_deploy": 2,
}


def _phase_rank(phase: str) -> int:
    return _PHASE_ORDER.get(phase, 99)


def _generate_uuidv7() -> str:
    """Generate a UUIDv7 (RFC 9562): 48-bit ms timestamp + version + random bits."""
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    # Pack into 128-bit integer
    # bits 127–80: 48-bit timestamp
    # bits 79–76: version 0b0111
    # bits 75–64: rand_a (12 bits)
    # bits 63–62: variant 0b10
    # bits 61–0: rand_b (62 bits)
    int_val = (
        (ts_ms << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | (rand_b & 0x3FFFFFFFFFFFFFFF)
    )
    return str(uuid.UUID(int=int_val))


def _parse_numeric_tag(tag: str) -> tuple[str, int] | None:
    """Return (prefix_without_value, numeric_value) for bucketed tags, else None."""
    m = _NUMERIC_TAG_RE.match(tag)
    if not m:
        return None
    prefix = m.group("prefix")
    val = int(m.group("val"))
    suffix = m.group("suffix")
    return (prefix + ("" if not suffix else "{v}" + suffix), val)  # normalised key


def _tag_base(tag: str) -> str:
    """Strip trailing numeric value for bucketed tags to get the 'key prefix'."""
    m = _NUMERIC_TAG_RE.match(tag)
    if m:
        return m.group("prefix")
    return tag


def _node_satisfies_tag(node_tag: str, required_tag: str) -> bool:
    """Return True if *node_tag* satisfies *required_tag*.

    For numeric-bucketed tags (e.g., ``mem:total-gb:``) the node value must be
    ≥ the required value.  All other tags require exact string equality.
    """
    if node_tag == required_tag:
        return True
    rn = _NUMERIC_TAG_RE.match(required_tag)
    nn = _NUMERIC_TAG_RE.match(node_tag)
    if rn and nn:
        r_prefix = rn.group("prefix")
        n_prefix = nn.group("prefix")
        if r_prefix == n_prefix:
            return int(nn.group("val")) >= int(rn.group("val"))
    return False


def _node_tag_set_satisfies(
    node_tags: list[str], required: list[str]
) -> tuple[bool, list[str]]:
    """Check whether *node_tags* satisfies all *required* tags.

    Returns (satisfied: bool, missing: list[str]) where missing is the list of
    required tags not satisfied by any node tag.
    """
    missing = []
    for req in required:
        if not any(_node_satisfies_tag(nt, req) for nt in node_tags):
            missing.append(req)
    return (len(missing) == 0, missing)


def _node_has_forbidden_tag(node_tags: list[str], forbidden: list[str]) -> list[str]:
    """Return any *forbidden* tags that are present in *node_tags*."""
    present = []
    for f in forbidden:
        if any(_node_satisfies_tag(nt, f) for nt in node_tags):
            present.append(f)
    return present


# ---------------------------------------------------------------------------
# LUKS key sealing
# ---------------------------------------------------------------------------

_LUKS_KEY_NAME_DEV = "gough-luks-dev"


def _seal_luks_key_dev(raw_key: bytes, vault_client: Any) -> tuple[bytes, dict[str, Any]]:
    """Seal a LUKS key via Vault transit (dev tier — M1)."""
    resp = vault_client.transit_encrypt(_LUKS_KEY_NAME_DEV, raw_key)
    sealed = resp.ciphertext.encode() if isinstance(resp.ciphertext, str) else resp.ciphertext
    provenance: dict[str, Any] = {
        "tier": "dev",
        "vault_key_name": _LUKS_KEY_NAME_DEV,
        "key_version": resp.key_version,
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }
    return sealed, provenance


def _seal_luks_key_tpm2(raw_key: bytes, vault_client: Any) -> tuple[bytes, dict[str, Any]]:
    """Seal a LUKS key via Vault transit + TPM2 unsealing.

    TPM2-only tier uses Clevis binding to PCR7 (secure boot state).
    The sealed key is stored in Vault; unsealing requires the key
    and TPM2 attestation at boot time.
    """
    resp = vault_client.transit_encrypt(_LUKS_KEY_NAME_DEV, raw_key)
    sealed = resp.ciphertext.encode() if isinstance(resp.ciphertext, str) else resp.ciphertext
    provenance: dict[str, Any] = {
        "tier": "tpm2",
        "method": "clevis-luks-bind",
        "clevis_config": {"hash": "sha256", "key": "rsa", "pcr_ids": "7"},
        "vault_key_name": _LUKS_KEY_NAME_DEV,
        "key_version": resp.key_version,
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }
    return sealed, provenance


def _seal_luks_key_tpm2_pin(raw_key: bytes, vault_client: Any) -> tuple[bytes, dict[str, Any]]:
    """Seal a LUKS key via Vault transit + TPM2+PIN unsealing.

    TPM2+PIN tier requires both TPM2 attestation (PCR7) and an operator-supplied PIN.
    The sealed key is stored in Vault; unsealing requires the PIN at boot time.
    """
    resp = vault_client.transit_encrypt(_LUKS_KEY_NAME_DEV, raw_key)
    sealed = resp.ciphertext.encode() if isinstance(resp.ciphertext, str) else resp.ciphertext
    provenance: dict[str, Any] = {
        "tier": "tpm2-pin",
        "method": "clevis-luks-bind-with-pin",
        "clevis_config": {"pcr_ids": "7", "tpm_pin": True},
        "vault_key_name": _LUKS_KEY_NAME_DEV,
        "key_version": resp.key_version,
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }
    return sealed, provenance


def _seal_luks_key_tang(raw_key: bytes, vault_client: Any) -> tuple[bytes, dict[str, Any]]:
    """Seal a LUKS key via Vault transit + Tang (network-bound) unsealing.

    Network-bound tier uses Clevis + Tang for unsealing. The sealed key is stored
    in Vault; unsealing requires network access to the Tang server at boot time.
    Tang server URL and thumbprint are typically configured in the cluster identity
    plane and passed via joiner secrets.
    """
    resp = vault_client.transit_encrypt(_LUKS_KEY_NAME_DEV, raw_key)
    sealed = resp.ciphertext.encode() if isinstance(resp.ciphertext, str) else resp.ciphertext
    provenance: dict[str, Any] = {
        "tier": "tang",
        "method": "clevis-luks-bind-tang",
        "clevis_config": {
            "url": "https://tang.<cluster-domain>",
            "thp": "<to-be-resolved-from-cluster-config>",
        },
        "vault_key_name": _LUKS_KEY_NAME_DEV,
        "key_version": resp.key_version,
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }
    return sealed, provenance


def _seal_luks_key_cloud_kms(raw_key: bytes, vault_client: Any) -> tuple[bytes, dict[str, Any]]:
    """Seal a LUKS key via the cloud VM's native KMS (AWS/GCP/Azure).

    Cloud VMs have no TPM, so the TPM2/TPM2+PIN/Tang Clevis-binding tiers
    above don't apply to them. This tier envelope-encrypts the raw LUKS
    key directly with the cloud provider's own KMS -- selected via
    ``GOUGH_CLOUD_KMS_PROVIDER``/``GOUGH_CLOUD_KMS_KEY_ID`` -- instead of
    the weaker "dev" (Vault-transit-only) tier cloud VMs previously fell
    back to. ``vault_client`` is accepted (unused) purely for dispatch-
    signature symmetry with the other ``_seal_luks_key_*`` tiers -- the
    cloud KMS is the sealing authority for this tier, not Vault transit.

    Raises:
        LUKSValidationError: If cloud-kms is misconfigured or the
            provider API call fails.
    """
    from .. import metrics as _metrics
    from ..clients.cloud_kms import CloudKMSClient, CloudKMSError

    try:
        client = CloudKMSClient.from_env()
    except CloudKMSError as exc:
        _metrics.cloud_kms_luks_seal_total.labels(
            provider=os.getenv("GOUGH_CLOUD_KMS_PROVIDER", "unconfigured") or "unconfigured",
            result="error",
        ).inc()
        raise LUKSValidationError(f"cloud-kms LUKS sealing failed: {exc}") from exc

    try:
        result = client.encrypt(raw_key)
    except CloudKMSError as exc:
        _metrics.cloud_kms_luks_seal_total.labels(
            provider=client.provider, result="error"
        ).inc()
        raise LUKSValidationError(f"cloud-kms LUKS sealing failed: {exc}") from exc

    _metrics.cloud_kms_luks_seal_total.labels(
        provider=result.provider, result="success"
    ).inc()

    provenance: dict[str, Any] = {
        "tier": "cloud-kms",
        "method": f"{result.provider}-kms-envelope-encrypt",
        "cloud_kms_provider": result.provider,
        "cloud_kms_key_id": result.key_id,
        "key_version": result.key_version,
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }
    return result.ciphertext, provenance


def _seal_luks_key(tier: str, raw_key: bytes, vault_client: Any) -> tuple[bytes, dict[str, Any]]:
    """Dispatch to the correct tier sealing function.

    Supported tiers:
      - dev: Vault transit only (development/bootstrap)
      - tpm2: Vault + TPM2 unsealing via Clevis (PCR7)
      - tpm2-pin: Vault + TPM2+PIN unsealing via Clevis
      - tang: Vault + network-bound Tang server unsealing
      - cloud-kms: AWS KMS / GCP Cloud KMS / Azure Key Vault envelope
        encryption (cloud VMs, which have no TPM)

    Args:
        tier: Sealing tier name
        raw_key: Raw LUKS key bytes to seal
        vault_client: Vault client for transit operations

    Returns:
        (sealed_key, provenance_dict) tuple

    Raises:
        LUKSValidationError: If tier is unknown or unsupported
    """
    dispatch: dict[str, Any] = {
        "dev": _seal_luks_key_dev,
        "tpm2": _seal_luks_key_tpm2,
        "tpm2-pin": _seal_luks_key_tpm2_pin,
        "tang": _seal_luks_key_tang,
        "cloud-kms": _seal_luks_key_cloud_kms,
    }
    if tier not in dispatch:
        raise LUKSValidationError(f"Unknown LUKS sealing tier: {tier!r}")
    fn = dispatch[tier]
    return fn(raw_key, vault_client)


# ---------------------------------------------------------------------------
# CNI install rendering (Cilium or none)
# ---------------------------------------------------------------------------


def _image_values(indent: str, mirror_image: str, registry: str) -> list[str]:
    """Return chart values pinning one Cilium image to the mirror by digest.

    ``mirror_image`` is the repository name under ``<registry>/cilium/``; the digest
    comes from ``CILIUM_IMAGE_DIGESTS`` so chart values and catalog share one source.
    """
    return [
        f"{indent}image:",
        f"{indent}  repository: {registry}/cilium/{mirror_image}",
        f"{indent}  useDigest: true",
        f"{indent}  digest: {CILIUM_IMAGE_DIGESTS[mirror_image]}",
    ]


def _render_cilium_values(service_host: str, mirrors: MirrorConfig) -> str:
    """Render the Cilium helm values: every image from the mirror, pinned by digest.

    No upstream registry name is emitted. Envoy, certgen and hubble-relay are
    included because the chart pulls them by default or when Hubble is enabled.
    ``enableCiliumEndpointSlice`` is deliberately absent: chart 1.20.1 refuses it.
    """
    registry = mirrors.registry_host
    lines = [
        "kubeProxyReplacement: true",
        f"k8sServiceHost: {service_host}",
        "k8sServicePort: 6443",
        *_image_values("", "cilium", registry),
        "ipam:",
        "  mode: kubernetes",
        "ipv4:",
        "  enabled: true",
        "ipv6:",
        "  enabled: false",
        "l2announcements:",
        "  enabled: true",
        "externalIPs:",
        "  enabled: true",
        "loadBalancer:",
        "  algorithm: maglev",
        "  mode: snat",
        "bpf:",
        "  masquerade: true",
        "socketLB:",
        "  enabled: true",
        "nodePort:",
        "  enabled: true",
        "hostServices:",
        "  enabled: true",
        "envoyConfig:",
        "  enabled: true",
        "envoy:",
        *_image_values("  ", "cilium-envoy", registry),
        "operator:",
        "  replicas: 1",
        *_image_values("  ", "operator", registry),
        "certgen:",
        *_image_values("  ", "certgen", registry),
        "hubble:",
        "  enabled: true",
        "  relay:",
        "    enabled: true",
        *_image_values("    ", "hubble-relay", registry),
        "gatewayAPI:",
        "  enabled: true",
    ]
    return "\n".join(lines) + "\n"


def _fetch_verified_cmd(url: str, sha256: str, dest: str) -> str:
    """Return a shell step that downloads ``url`` to ``dest`` and fails unless sha256 matches.

    Verify-before-apply: the artifact is only applied after this step succeeds.
    Inputs are validated upstream (https-only, no quotes/spaces) and shell-quoted here.
    """
    q_url = shlex.quote(url)
    q_dest = shlex.quote(dest)
    return (
        f"mkdir -p {shlex.quote(os.path.dirname(dest))} && "
        f"curl -fsSL --proto '=https' --tlsv1.2 -o {q_dest} {q_url} && "
        f"echo {shlex.quote(sha256 + '  ' + dest)} | sha256sum -c - || exit 1"
    )


def _render_registry_mirror_files(mirrors: MirrorConfig) -> tuple[list[dict[str, Any]], list[str]]:
    """Render containerd hosts.toml files routing each upstream registry through the mirror.

    Returns (write_files, runcmds). The runcmd fails loudly if containerd does not
    read ``/etc/containerd/certs.d``, so pulls can never silently go upstream.
    """
    write_files: list[dict[str, Any]] = []
    for registry in mirrors.mirrored_registries:
        upstream = "registry-1.docker.io" if registry == "docker.io" else registry
        write_files.append({
            "path": f"/etc/containerd/certs.d/{registry}/hosts.toml",
            "content": (
                f'server = "https://{upstream}"\n\n'
                f'[host."{mirrors.registry_mirror}"]\n'
                '  capabilities = ["pull", "resolve"]\n'
            ),
            "permissions": "0644",
        })
    runcmds = [
        "# Fail loudly unless containerd reads per-registry mirror config",
        "grep -q 'config_path = \"/etc/containerd/certs.d\"' /etc/containerd/config.toml "
        "|| (echo 'FATAL: containerd config_path for certs.d is not set' && exit 1)",
    ]
    return (write_files, runcmds)


def _render_cni_install(
    cni_mode: str,
    cni_params: dict[str, Any],
    endpoint: str | None,
    mirror_config: MirrorConfig | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[PlanValidationError]]:
    """Render CNI install (Cilium) or skip if mode=none.

    Returns (write_files, runcmds, errors).

    On bootstrap with cni=cilium (airgap: every artifact from an operator mirror,
    sha256-verified before use; no public upstream host access):
      - Write cilium-values.yaml with templated k8sServiceHost
      - Write containerd hosts.toml routing upstream registries through the mirror
      - Fetch the Cilium CRD bundle and Gateway API CRDs, verify sha256, then apply
      - Helm install from the verified chart tarball
      - Wait for operator Ready + DaemonSet rolled out
      - Verify critical CRDs present (ciliumenvoyconfigs, ciliumclusterwideenvoyconfigs)

    On bootstrap with cni=none:
      - No install, no files, no commands (caller handles coredns install)

    Prevents dal2-beta operator deadlock: "Still waiting for Cilium Operator to register CRDs"
    by pre-applying CRDs before the operator starts.
    """
    errors: list[PlanValidationError] = []
    write_files: list[dict[str, Any]] = []
    runcmds: list[str] = []

    if cni_mode == "none":
        return (write_files, runcmds, errors)

    if cni_mode != "cilium":
        errors.append(
            PlanValidationError(
                code="cloud_init_validation_error",
                message=f"Unsupported CNI mode: {cni_mode!r}. Expected 'cilium' or 'none'.",
                details={"cni_mode": cni_mode},
            )
        )
        return (write_files, runcmds, errors)

    if not endpoint:
        errors.append(
            PlanValidationError(
                code="cloud_init_validation_error",
                message="CNI mode=cilium requires endpoint (control plane VIP)",
                details={},
            )
        )
        return (write_files, runcmds, errors)

    version = str(cni_params.get("version", CILIUM_VERSION))
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        errors.append(
            PlanValidationError(
                code="cloud_init_validation_error",
                message=f"Invalid Cilium version {version!r}; expected MAJOR.MINOR.PATCH",
                details={"cilium_version": version},
            )
        )
        return (write_files, runcmds, errors)

    try:
        mirrors = mirror_config or load_mirror_config()
    except MirrorConfigError as exc:
        errors.append(
            PlanValidationError(
                code="cloud_init_validation_error",
                message=f"airgap mirror configuration invalid: {exc}",
                details={},
            )
        )
        return (write_files, runcmds, errors)

    service_host = endpoint.split(":")[0]

    # Generate cilium-values.yaml with templated service host and mirror-pinned images
    cilium_yaml = _render_cilium_values(service_host, mirrors)

    write_files.append({
        "path": "/etc/gough/cilium-values.yaml",
        "content": cilium_yaml,
        "permissions": "0644",
    })

    # Airgap: route every upstream registry through the operator mirror.
    registry_files, registry_cmds = _render_registry_mirror_files(mirrors)
    write_files.extend(registry_files)
    runcmds.extend(registry_cmds)

    # Airgap: every artifact comes from an operator mirror and is sha256-verified
    # before it is applied. Nothing is fetched from upstream hosts.
    crd_bundle_path = "/var/lib/gough/airgap/cilium-crds.yaml"
    gateway_crd_path = "/var/lib/gough/airgap/gateway-api-crds.yaml"
    chart_path = f"/var/lib/gough/airgap/cilium-{version}.tgz"

    # Gateway API CRDs first: gatewayAPI is enabled in the values, so TLSRoute must be served.
    runcmds.append("# Fetch + verify Gateway API CRDs from the mirror (gatewayAPI is enabled)")
    runcmds.append(
        _fetch_verified_cmd(mirrors.gateway_api_crds_url, mirrors.gateway_api_crds_sha256, gateway_crd_path)
    )
    runcmds.append(f"kubectl apply -f {shlex.quote(gateway_crd_path)} || exit 1")
    runcmds.append(
        "kubectl get crd tlsroutes.gateway.networking.k8s.io -o jsonpath='{.spec.versions[?(@.name==\"v1alpha2\")].served}' | grep -q true "
        "|| (echo 'FATAL: TLSRoute v1alpha2 not served — Cilium operator will crash' && exit 1)"
    )

    # Pre-apply the Cilium CRD bundle (single verified file) before the operator starts.
    runcmds.append("# Fetch + verify + pre-apply Cilium CRD bundle (prevents operator stuck-on-CRDs deadlock)")
    runcmds.append(_fetch_verified_cmd(mirrors.cilium_crds_url, mirrors.cilium_crds_sha256, crd_bundle_path))
    runcmds.append(f"kubectl apply -f {shlex.quote(crd_bundle_path)} || exit 1")

    # Chart from the mirror, verified, installed from the local tarball (no helm repo).
    runcmds.append("# Fetch + verify Cilium chart from the mirror, then helm install from the local tarball")
    runcmds.append(_fetch_verified_cmd(mirrors.cilium_chart_url(version), mirrors.cilium_chart_sha256, chart_path))
    runcmds.append(
        f"helm install cilium {shlex.quote(chart_path)} "
        f"--namespace kube-system --create-namespace "
        f"-f /etc/gough/cilium-values.yaml "
        f"--wait --timeout 10m"
    )

    # Readiness probes
    runcmds.append("# Wait for Cilium Operator to become Ready")
    runcmds.append(
        "kubectl -n kube-system wait --for=condition=Available --timeout=300s deployment/cilium-operator || exit 1"
    )

    runcmds.append("# Wait for Cilium DaemonSet to roll out")
    runcmds.append(
        "kubectl -n kube-system rollout status daemonset/cilium --timeout=300s || exit 1"
    )

    # Final CRD verification (exact check from dal2-beta incident)
    runcmds.append("# Verify critical Cilium CRDs are present (dal2-beta deadlock prevention)")
    runcmds.append(
        "for crd in ciliumenvoyconfigs.cilium.io ciliumclusterwideenvoyconfigs.cilium.io; do "
        "kubectl get crd \"$crd\" > /dev/null || (echo \"Critical CRD $crd missing\" && exit 1); done"
    )

    # Apply CiliumLoadBalancerIPPool for L2 announcements (default: 10.2.0.50-10.2.0.250 for external baseline)
    lb_pool_cidr = cni_params.get("lb_pool_cidr", "10.2.0.50-10.2.0.250")
    lb_pool_name = "default-pool"
    pool_yaml = (
        "apiVersion: cilium.io/v2alpha1\n"
        "kind: CiliumLoadBalancerIPPool\n"
        "metadata:\n"
        f"  name: {lb_pool_name}\n"
        "spec:\n"
        f"  cidrs:\n"
        f"  - cidr: {lb_pool_cidr}\n"
    )
    write_files.append({
        "path": "/etc/gough/cilium-lb-pool.yaml",
        "content": pool_yaml,
        "permissions": "0644",
    })
    runcmds.append("# Apply CiliumLoadBalancerIPPool for type=LoadBalancer services (Cilium replaces MetalLB)")
    runcmds.append("kubectl apply -f /etc/gough/cilium-lb-pool.yaml || exit 1")

    return (write_files, runcmds, errors)


# ---------------------------------------------------------------------------
# Control-plane frontend rendering (kube-vip, external LB, none)
# ---------------------------------------------------------------------------


def _render_control_plane_frontend(
    params: dict[str, Any],
    biome_name: str,
    node_name: str,
    cluster_id: str,
    joiner_secrets_existing: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], list[PlanValidationError]]:
    """Render control-plane frontend (kube-vip, external, none) artifacts.

    Returns (write_files, runcmds, joiner_secrets_to_emit, validation_errors).

    On bootstrap (no prior joiner secrets):
      - kube-vip: write static pod + init + emit frontend joiner secrets
      - external: no pod, init + emit frontend joiner secrets
      - none: bare init, no secrets

    On join (joiner secrets exist):
      - kube-vip: write static pod + join command
      - external: no pod + join command
      - none: not applicable (only bootstrap)
    """
    errors: list[PlanValidationError] = []
    write_files: list[dict[str, Any]] = []
    runcmds: list[str] = []
    joiner_secrets: list[dict[str, Any]] = []

    if biome_name != "k8s-controller":
        errors.append(
            PlanValidationError(
                code="cloud_init_validation_error",
                message=f"control_plane_frontend params only valid on k8s-controller biome, not {biome_name!r}",
                details={"biome": biome_name},
            )
        )
        return (write_files, runcmds, joiner_secrets, errors)

    mode = params.get("mode", "kube-vip")
    if mode not in ("kube-vip", "external", "none"):
        errors.append(
            PlanValidationError(
                code="cloud_init_validation_error",
                message=f"Unknown control_plane_frontend mode: {mode!r}. Expected kube-vip, external, or none.",
                details={"mode": mode},
            )
        )
        return (write_files, runcmds, joiner_secrets, errors)

    is_bootstrap = len(joiner_secrets_existing) == 0

    if mode == "none":
        # Single-node lab: no VIP, no static pod, no endpoint
        if is_bootstrap:
            runcmds.append("kubeadm init --upload-certs")
        else:
            errors.append(
                PlanValidationError(
                    code="cloud_init_validation_error",
                    message="mode=none is only valid for bootstrap (no prior joiner secrets)",
                    details={"mode": mode, "is_bootstrap": False},
                )
            )
        return (write_files, runcmds, joiner_secrets, errors)

    # kube-vip or external: both require endpoint + baseline
    endpoint = params.get("endpoint")
    if not endpoint:
        errors.append(
            PlanValidationError(
                code="cloud_init_validation_error",
                message=f"control_plane_frontend mode={mode!r} requires 'endpoint' parameter",
                details={"mode": mode},
            )
        )
        return (write_files, runcmds, joiner_secrets, errors)

    if mode == "kube-vip":
        # kube-vip: render static pod manifest + populate init/join
        interface = params.get("interface", "eth0")
        kube_vip_digest = params.get("kube_vip_image_digest",
            "sha256:840305b94ef2a89abb3b7fd2b09edfbde690d90052020da4dff90679fe892da2")

        # Static pod manifest (ARP mode, single NIC)
        kube_vip_manifest = f"""apiVersion: v1
kind: Pod
metadata:
  name: kube-vip
  namespace: kube-system
spec:
  containers:
  - name: kube-vip
    image: ghcr.io/kube-vip/kube-vip@{kube_vip_digest}
    imagePullPolicy: IfNotPresent
    args:
      - manager
    env:
      - name: vip_arp
        value: "true"
      - name: vip_interface
        value: "{interface}"
      - name: vip_address
        value: "{endpoint.split(':')[0]}"
      - name: vip_leaderelection
        value: "true"
      - name: vip_leadernamespace
        value: "kube-system"
      - name: port_bgpnat
        value: "179"
      - name: vip_routerid
        value: "1"
    securityContext:
      capabilities:
        add:
          - NET_ADMIN
          - NET_RAW
          - SYS_TIME
    volumeMounts:
      - mountPath: /etc/kubernetes
        name: etc-kubernetes
  hostNetwork: true
  volumes:
  - hostPath:
      path: /etc/kubernetes
    name: etc-kubernetes
"""
        write_files.append({
            "path": "/etc/kubernetes/manifests/kube-vip.yaml",
            "content": kube_vip_manifest,
            "permissions": "0644",
        })

    # Both kube-vip and external: emit kubeadm init/join with endpoint
    if is_bootstrap:
        # Bootstrap: init with controlPlaneEndpoint, skip coredns (install after CNI) and kube-proxy (Cilium replaces it)
        kubeadm_init_cmd = (
            f"kubeadm init --control-plane-endpoint {endpoint} "
            "--upload-certs --skip-phases=addon/coredns --skip-phases=addon/kube-proxy"
        )
        runcmds.append(kubeadm_init_cmd)

        # Set KUBECONFIG for subsequent commands
        runcmds.append("export KUBECONFIG=/etc/kubernetes/admin.conf")

        # Install CNI (get mode from params, default: cilium)
        cni_mode = params.get("cni", "cilium")
        cni_params = params.get("cni_params", {})
        cni_write_files, cni_runcmds, cni_errors = _render_cni_install(cni_mode, cni_params, endpoint)
        write_files.extend(cni_write_files)
        runcmds.extend(cni_runcmds)
        errors.extend(cni_errors)

        # Install CoreDNS addon (now that CNI is ready)
        runcmds.append("kubeadm init phase addon coredns")

        # Install core-agents watchdog CronJob (self-healing for Cilium, CoreDNS, kube-vip)
        watchdog_yaml = """apiVersion: v1
kind: ServiceAccount
metadata:
  name: gough-core-agent-watchdog
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: gough-core-agent-watchdog
rules:
- apiGroups: ["apps"]
  resources: ["deployments", "daemonsets"]
  verbs: ["get", "list", "patch"]
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list"]
- apiGroups: ["apps"]
  resources: ["deployments/rollback"]
  verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: gough-core-agent-watchdog
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: gough-core-agent-watchdog
subjects:
- kind: ServiceAccount
  name: gough-core-agent-watchdog
  namespace: kube-system
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: gough-core-agent-watchdog
  namespace: kube-system
spec:
  schedule: "*/5 * * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: gough-core-agent-watchdog
          containers:
          - name: watchdog
            image: docker.io/bitnami/kubectl:1.30@sha256:5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5
            command:
            - /bin/bash
            - -c
            - |
              set +e
              LOG_FN() { echo "{\\\"ts\\\":\\\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\\\",\\\"component\\\":\\\"gough.core-agent.watchdog\\\",\\\"message\\\":\\\"$1\\\",\\\"status\\\":\\\"$2\\\"}"; }

              # Check Cilium Operator
              if [ "$(kubectl -n kube-system get deploy cilium-operator -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)" -lt 1 ]; then
                LOG_FN "cilium-operator not ready" "failure"
                kubectl -n kube-system rollout restart deployment/cilium-operator 2>/dev/null || true
              fi

              # Check Cilium DaemonSet (allow 1 node down)
              desired=$(kubectl -n kube-system get ds cilium -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null || echo 0)
              ready=$(kubectl -n kube-system get ds cilium -o jsonpath='{.status.numberReady}' 2>/dev/null || echo 0)
              if [ "$desired" -gt 0 ] && [ "$((desired - ready))" -gt 1 ]; then
                LOG_FN "cilium daemonset drift (desired=$desired, ready=$ready)" "failure"
                kubectl -n kube-system rollout restart daemonset/cilium 2>/dev/null || true
              fi

              # Check CoreDNS
              if [ "$(kubectl -n kube-system get deploy coredns -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)" -lt 1 ]; then
                LOG_FN "coredns not ready" "failure"
                kubectl -n kube-system rollout restart deployment/coredns 2>/dev/null || true
              fi

              LOG_FN "watchdog check complete" "ok"
          restartPolicy: OnFailure
"""
        write_files.append({
            "path": "/etc/gough/core-agent-watchdog-cronjob.yaml",
            "content": watchdog_yaml,
            "permissions": "0644",
        })
        runcmds.append("# Install gough core-agents watchdog CronJob (self-healing)")
        runcmds.append("kubectl apply -f /etc/gough/core-agent-watchdog-cronjob.yaml || exit 1")

        # Emit joiner secrets (placeholder extraction; real impl uses kubeadm hooks)
        joiner_secrets.append({
            "cluster_id": cluster_id,
            "egg_kind": "k8s-controller",
            "extractor_name": "control_plane_endpoint",
            "scope": "cluster",
            "value": endpoint,
        })
        joiner_secrets.append({
            "cluster_id": cluster_id,
            "egg_kind": "k8s-controller",
            "extractor_name": "kubeadm_certificate_key",
            "scope": "cluster",
            "value": f"<cert-key-from-init>",  # Real impl extracts from kubeadm
        })
    else:
        # Join-as-control-plane: consume token, ca-hash, cert-key from secrets
        token_secret = next(
            (s for s in joiner_secrets_existing
             if s.get("extractor_name") == "kubeadm_join_token"),
            None,
        )
        ca_hash_secret = next(
            (s for s in joiner_secrets_existing
             if s.get("extractor_name") == "kubeadm_ca_hash"),
            None,
        )
        cert_key_secret = next(
            (s for s in joiner_secrets_existing
             if s.get("extractor_name") == "kubeadm_certificate_key"),
            None,
        )

        if not (token_secret and ca_hash_secret and cert_key_secret):
            errors.append(
                PlanValidationError(
                    code="cloud_init_validation_error",
                    message="Join-as-control-plane requires kubeadm_join_token, kubeadm_ca_hash, and kubeadm_certificate_key joiner secrets",
                    details={"has_token": bool(token_secret), "has_ca_hash": bool(ca_hash_secret), "has_cert_key": bool(cert_key_secret)},
                )
            )
            return (write_files, runcmds, joiner_secrets, errors)

        kubeadm_join_cmd = (
            f"kubeadm join {endpoint} --control-plane "
            f"--certificate-key ${{KUBEADM_CERT_KEY}} "
            f"--token ${{KUBEADM_TOKEN}} "
            f"--discovery-token-ca-cert-hash ${{KUBEADM_CA_HASH}}"
        )
        runcmds.append(kubeadm_join_cmd)

        # Wait for local Cilium agent pod to be Ready on this node (bootstrap already installed Cilium)
        runcmds.append("export KUBECONFIG=/etc/kubernetes/admin.conf")
        runcmds.append(
            "kubectl -n kube-system wait --for=condition=Ready pod "
            "-l k8s-app=cilium "
            "--field-selector spec.nodeName=$(hostname) "
            "--timeout=300s || exit 1"
        )

    return (write_files, runcmds, joiner_secrets, errors)


def _validate_frontend_params(
    params: dict[str, Any],
    biome_name: str,
    node_count: int,
) -> list[PlanValidationError]:
    """Validate control-plane frontend params."""
    errors: list[PlanValidationError] = []

    if not params:
        return errors

    if biome_name != "k8s-controller":
        return errors

    mode = params.get("mode", "kube-vip")
    if mode not in ("kube-vip", "external", "none"):
        errors.append(
            PlanValidationError(
                code="cloud_init_validation_error",
                message=f"Invalid mode: {mode!r}",
                details={"mode": mode},
            )
        )

    if mode in ("kube-vip", "external"):
        if not params.get("endpoint"):
            errors.append(
                PlanValidationError(
                    code="cloud_init_validation_error",
                    message=f"mode={mode!r} requires 'endpoint' parameter",
                    details={"mode": mode},
                )
            )

        # Warn if <3 primaries with mode != none (non-fatal)
        if node_count < 3 and mode != "none":
            # Note: warnings are collected separately, not as errors
            pass

    # Validate CNI mode (new)
    cni_mode = params.get("cni", "cilium")
    if cni_mode not in ("cilium", "none"):
        errors.append(
            PlanValidationError(
                code="cloud_init_validation_error",
                message=f"Invalid cni mode: {cni_mode!r}. Expected 'cilium' or 'none'.",
                details={"cni_mode": cni_mode},
            )
        )

    return errors


# ---------------------------------------------------------------------------
# Cloud-init merge helpers
# ---------------------------------------------------------------------------


def _merge_cloud_init(biomes_in_order: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Merge per-biome cloud-init fragments into a single bundle.

    Returns (base64-encoded YAML string, list of warning strings).

    Raises PlanCompilationError (via collected errors) on write_files collisions.
    """
    merged_write_files: dict[str, str] = {}  # path → biome name
    merged_runcmds: list[str] = []
    collision_errors: list[dict[str, Any]] = []
    warnings: list[str] = []

    for biome in biomes_in_order:
        ci = biome.get("cloud_init") or {}
        biome_name = biome.get("name", f"biome-{biome.get('id', '?')}")

        # Handle write_files
        for wf in (ci.get("write_files") or []):
            path = wf.get("path", "")
            if path in merged_write_files:
                collision_errors.append(
                    {
                        "path": path,
                        "first_egg": merged_write_files[path],
                        "second_egg": biome_name,
                    }
                )
            else:
                merged_write_files[path] = biome_name

        # Handle runcmd
        for cmd in (ci.get("runcmd") or []):
            merged_runcmds.append(cmd)

    if collision_errors:
        # Return the collision data — caller will convert to PlanValidationError
        return ("", [])  # caller checks for empty + raises

    # Produce merged YAML (minimal; Phase-2 expands via cloud-init merge keys)
    bundle: dict[str, Any] = {"write_files": [], "runcmd": merged_runcmds}
    for biome in biomes_in_order:
        ci = biome.get("cloud_init") or {}
        bundle["write_files"].extend(ci.get("write_files") or [])

    import yaml  # type: ignore[import-untyped]

    yaml_str = yaml.dump(bundle, default_flow_style=False)
    b64 = base64.b64encode(yaml_str.encode()).decode()
    return b64, warnings


def _merge_cloud_init_with_errors(
    biomes_in_order: list[dict[str, Any]],
) -> tuple[str, list[str], list[PlanValidationError]]:
    """Like _merge_cloud_init but returns validation errors instead of raising."""
    errors: list[PlanValidationError] = []
    merged_write_files: dict[str, str] = {}
    merged_runcmds: list[str] = []
    warnings: list[str] = []

    for biome in biomes_in_order:
        ci = biome.get("cloud_init") or {}
        biome_name = biome.get("name", f"biome-{biome.get('id', '?')}")

        for wf in (ci.get("write_files") or []):
            path = wf.get("path", "")
            if path in merged_write_files:
                errors.append(
                    PlanValidationError(
                        code="cloud_init_path_collision",
                        message=(
                            f"write_files path collision on {path!r}: claimed by "
                            f"{merged_write_files[path]!r} and {biome_name!r}"
                        ),
                        details={
                            "path": path,
                            "first_egg": merged_write_files[path],
                            "second_egg": biome_name,
                        },
                    )
                )
            else:
                merged_write_files[path] = biome_name

        for cmd in (ci.get("runcmd") or []):
            merged_runcmds.append(cmd)

    if errors:
        return ("", warnings, errors)

    import yaml  # type: ignore[import-untyped]

    bundle: dict[str, Any] = {"write_files": [], "runcmd": merged_runcmds}
    for biome in biomes_in_order:
        ci = biome.get("cloud_init") or {}
        bundle["write_files"].extend(ci.get("write_files") or [])

    yaml_str = yaml.dump(bundle, default_flow_style=False)
    b64 = base64.b64encode(yaml_str.encode()).decode()
    return b64, warnings, []


# ---------------------------------------------------------------------------
# Phase-2 iPXE script rendering
# ---------------------------------------------------------------------------


def _render_phase2_ipxe(node: Any, plan_id: str) -> str:
    """Render the Phase-2 iPXE script embedding the plan_id as a query param."""
    mac = getattr(node, "primary_nic_mac", "00:00:00:00:00:00") or "00:00:00:00:00:00"
    script = (
        "#!ipxe\n"
        f"set plan_id {plan_id}\n"
        f"chain https://${{next-server}}/ipxe/deploy/{mac}?plan_id={plan_id}\n"
    )
    return script


# ---------------------------------------------------------------------------
# Disk script rendering
# ---------------------------------------------------------------------------


def _render_disk_script(disk_plan: DiskPlanInput) -> str:
    """Render a minimal sgdisk-based disk partitioning script."""
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"DISK={disk_plan.disk_device}",
        "sgdisk --zap-all $DISK",
    ]
    for i, part in enumerate(disk_plan.partitions, start=1):
        size = part.get("size", "0")
        fs = part.get("fs_type", "ext4")
        lines.append(f"sgdisk -n {i}:0:{size} -t {i}:8300 $DISK  # {fs}")

    if disk_plan.luks_enabled:
        lines += [
            "# LUKS setup — key injected from sealed store at runtime",
            "cryptsetup luksFormat --batch-mode --key-file /run/gough/luks.key ${DISK}p1",
            "cryptsetup open --key-file /run/gough/luks.key ${DISK}p1 gough_crypt",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dependency graph (Kahn's algorithm)
# ---------------------------------------------------------------------------


@dataclass
class _BiomeNode:
    biome_id: int
    biome_name: str
    phase: str
    deps: list[int] = field(default_factory=list)  # hard deps by biome_id
    soft_deps: list[int] = field(default_factory=list)
    version_constraints: list[dict[str, Any]] = field(default_factory=list)
    requires_hardware_tags: list[str] = field(default_factory=list)
    prefers_hardware_tags: list[str] = field(default_factory=list)
    forbids_hardware_tags: list[str] = field(default_factory=list)
    consumes_joiner_secrets_from: list[dict[str, Any]] = field(default_factory=list)
    cloud_init: dict[str, Any] = field(default_factory=dict)
    storage_requirements: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _topo_sort(
    nodes: dict[int, _BiomeNode],
) -> tuple[list[int], list[PlanValidationError]]:
    """Kahn's BFS topological sort with cycle detection.

    Returns (sorted_ids, errors).  If a cycle exists, errors contains a single
    ``egg_dependency_cycle`` error with a bounded cycle path (first cycle found,
    max 20 nodes).
    """
    in_degree: dict[int, int] = {eid: 0 for eid in nodes}
    adj: dict[int, list[int]] = {eid: [] for eid in nodes}

    for eid, node in nodes.items():
        for dep_id in node.deps:
            if dep_id not in nodes:
                # External dep (already deployed) — treat as satisfied root
                continue
            in_degree[eid] += 1
            adj[dep_id].append(eid)

    queue: deque[int] = deque(
        eid for eid, deg in in_degree.items() if deg == 0
    )
    sorted_ids: list[int] = []

    while queue:
        eid = queue.popleft()
        sorted_ids.append(eid)
        for dep in adj[eid]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    if len(sorted_ids) < len(nodes):
        # Cycle exists — find one via DFS
        visited = set(sorted_ids)
        remaining = [eid for eid in nodes if eid not in visited]
        cycle = _find_cycle(remaining[0], nodes, max_len=20)
        return (
            [],
            [
                PlanValidationError(
                    code="egg_dependency_cycle",
                    message="Dependency cycle detected — plan compilation aborted",
                    details={
                        "cycle": [nodes[c].biome_name for c in cycle if c in nodes],
                        "cycle_ids": cycle,
                    },
                )
            ],
        )

    return sorted_ids, []


def _find_cycle(start: int, nodes: dict[int, _BiomeNode], max_len: int = 20) -> list[int]:
    """DFS cycle finder; returns the cycle path bounded to max_len entries."""
    path: list[int] = []
    on_stack: set[int] = set()
    found: list[list[int]] = []

    def dfs(eid: int) -> bool:
        if len(found) > 0:
            return True
        if eid in on_stack:
            cycle_start = path.index(eid)
            found.append(path[cycle_start:] + [eid])
            return True
        if eid not in nodes:
            return False
        on_stack.add(eid)
        path.append(eid)
        for dep in nodes[eid].deps:
            if dep in nodes and dfs(dep):
                return True
        path.pop()
        on_stack.discard(eid)
        return False

    # Try every remaining node as DFS start to handle disconnected cycles
    for eid in nodes:
        if found:
            break
        dfs(eid)

    if found:
        raw = found[0][:max_len]
        # Ensure the first entry appears at the end to show it's a cycle
        if raw and raw[0] != raw[-1]:
            raw.append(raw[0])
        return raw
    return [start]


# ---------------------------------------------------------------------------
# VRAM / GPU scheduling
# ---------------------------------------------------------------------------


def _check_vram_requirements(
    node: Any, biome_id: int, biome_name: str, needs_gpu: dict[str, Any]
) -> list[PlanValidationError]:
    """Validate VRAM and GPU constraints from storage_requirements_json.needs_gpu."""
    errors: list[PlanValidationError] = []
    hw: dict[str, Any] = getattr(node, "hardware_json", None) or {}
    accelerators: list[dict[str, Any]] = hw.get("accelerators", [])

    kind = needs_gpu.get("kind", "gpu")
    vendor = needs_gpu.get("vendor", "any")
    bus_type = needs_gpu.get("bus_type", "any")
    vram_per_device_min_gb = needs_gpu.get("vram_per_device_min_gb", 0)
    total_vram_min_gb = needs_gpu.get("total_vram_min_gb", 0)
    device_count_min = needs_gpu.get("device_count_min", 1)
    device_count_max = needs_gpu.get("device_count_max", 9999)
    shared_memory_acceptable = needs_gpu.get("shared_memory_acceptable", False)
    free_ram_gb: float = hw.get("free_ram_mb", 0) / 1024.0
    shared_memory_buffer_gb: float = hw.get("shared_memory_buffer_gb", 2.0)
    interconnect = needs_gpu.get("interconnect")
    min_compute = needs_gpu.get("min_compute_capability")

    def _matches_device(dev: dict[str, Any]) -> tuple[bool, str | None]:
        """Return (match, reason_for_skip)."""
        if dev.get("kind") != kind:
            return False, f"kind mismatch ({dev.get('kind')} != {kind})"
        if vendor != "any" and dev.get("vendor") != vendor:
            return False, f"vendor mismatch ({dev.get('vendor')} != {vendor})"
        dbus = dev.get("bus_type", "")
        if bus_type != "any" and dbus != bus_type:
            return False, f"bus_type mismatch ({dbus} != {bus_type})"
        # Integrated GPU: disqualify unless shared_memory_acceptable
        if dbus == "integrated":
            if not shared_memory_acceptable:
                return False, "integrated GPU not acceptable (shared_memory_acceptable=false)"
            # Check free RAM headroom
            needed = vram_per_device_min_gb + shared_memory_buffer_gb
            if free_ram_gb < needed:
                return (
                    False,
                    f"integrated GPU: free RAM {free_ram_gb:.1f} GB < needed {needed:.1f} GB",
                )
        # External GPU: disqualify by default
        if dbus == "external":
            if not needs_gpu.get("external_accelerator_allowed", False):
                return False, "external GPU not allowed (external_accelerator_allowed=false)"
        # VRAM check (skip for integrated with shared_memory_acceptable)
        if dbus != "integrated":
            dev_vram = dev.get("vram_gb", 0)
            if dev_vram < vram_per_device_min_gb:
                return (
                    False,
                    f"VRAM {dev_vram} GB < required {vram_per_device_min_gb} GB",
                )
        # Compute capability
        if min_compute and dev.get("compute_capability"):
            # Simple string comparison: sm-86 >= sm-80 etc.
            if dev["compute_capability"] < min_compute:
                return (
                    False,
                    f"compute_capability {dev['compute_capability']} < {min_compute}",
                )
        return True, None

    eligible: list[dict[str, Any]] = []
    elimination_reasons: dict[str, str] = {}
    for i, dev in enumerate(accelerators):
        ok, reason = _matches_device(dev)
        dev_key = f"device-{i}"
        if ok:
            eligible.append(dev)
        else:
            elimination_reasons[dev_key] = reason or "unknown"

    if len(eligible) < device_count_min:
        errors.append(
            PlanValidationError(
                code="no_eligible_node",
                message=(
                    f"Biome {biome_name!r} requires {device_count_min} GPU device(s) "
                    f"but only {len(eligible)} eligible device(s) found on node"
                ),
                details={
                    "biome_id": biome_id,
                    "biome_name": biome_name,
                    "device_count_min": device_count_min,
                    "eligible_count": len(eligible),
                    "elimination_reasons": elimination_reasons,
                },
            )
        )
        return errors

    # Aggregate VRAM check
    if total_vram_min_gb > 0:
        total_vram = sum(d.get("vram_gb", 0) for d in eligible[:device_count_max])
        if total_vram < total_vram_min_gb:
            errors.append(
                PlanValidationError(
                    code="no_eligible_node",
                    message=(
                        f"Biome {biome_name!r}: total VRAM across eligible devices "
                        f"{total_vram} GB < required {total_vram_min_gb} GB"
                    ),
                    details={
                        "biome_id": biome_id,
                        "biome_name": biome_name,
                        "total_vram_gb": total_vram,
                        "total_vram_min_gb": total_vram_min_gb,
                    },
                )
            )

    # Interconnect warning
    if device_count_min > 1 and not interconnect:
        pass  # Compiler emits warning, not error — handled in compile()

    return errors


# ---------------------------------------------------------------------------
# Joiner secret resolution helpers
# ---------------------------------------------------------------------------


def _resolve_joiner_secrets(
    db_session: Any,
    egg_node: _BiomeNode,
    cluster_id: str,
    now: datetime,
) -> tuple[list[str], list[PlanValidationError]]:
    """Query joiner_secrets table for each consumed secret reference.

    Returns (refs: list[str], errors: list[PlanValidationError]).

    ``db_session`` is a penguin-dal ``DB`` instance (see ``PlanCompiler``
    docstring for the request-scoped RLS reasoning -- this reuses whatever
    tenant is already ambient on the calling request/task, same as
    ``_load_node``/``_load_eggs``; no explicit tenant filter or cross-tenant
    sentinel here). ``joiner_secrets`` is one of the tables the baseline
    migration enables RLS on, so an unset tenant context fails closed (zero
    rows) here exactly as it would for ``nodes``/``biomes``.
    """
    refs: list[str] = []
    errors: list[PlanValidationError] = []

    tbl = db_session.joiner_secrets

    for consume_spec in egg_node.consumes_joiner_secrets_from:
        # Backward-compat: accept egg_kind or biome_kind
        biome_kind = consume_spec.get("biome_kind", consume_spec.get("egg_kind", ""))
        extractor = consume_spec.get("extractor_name", consume_spec.get("extractor", ""))
        scope = consume_spec.get("scope", "cluster")
        optional = consume_spec.get("mode", "hard") == "soft"

        query = (
            (tbl.cluster_id == cluster_id)
            & (tbl.biome_kind == biome_kind)
            & (tbl.extractor_name == extractor)
            & (tbl.scope == scope)
            & (tbl.revoked_at == None)  # noqa: E711 — DAL idiom
            & (
                (tbl.expires_at == None)  # noqa: E711 — DAL idiom
                | (tbl.expires_at > now)
            )
        )
        row = (
            db_session(query)
            .select(tbl.id, orderby=~tbl.created_at, limitby=(0, 1))
            .first()
        )

        if row is None:
            if optional:
                pass  # soft dep — warning only, not an error
            else:
                errors.append(
                    PlanValidationError(
                        code="joiner_secret_unavailable",
                        message=(
                            f"No valid joiner secret for biome_kind={biome_kind!r} "
                            f"extractor={extractor!r} scope={scope!r} in cluster"
                        ),
                        details={
                            "biome_kind": biome_kind,
                            "egg_kind": biome_kind,  # Backward-compat alias
                            "extractor": extractor,
                            "scope": scope,
                            "cluster_id": cluster_id,
                        },
                    )
                )
        else:
            refs.append(str(row.id))

    return refs, errors


# ---------------------------------------------------------------------------
# Version constraint helpers
# ---------------------------------------------------------------------------


def _parse_semver(version: str) -> tuple[int, ...]:
    """Parse a semver string like '1.29.3' into (1, 29, 3)."""
    parts = []
    for p in version.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _satisfies_version_range(
    actual: str, version_min: str | None, version_max: str | None
) -> bool:
    """Check if *actual* satisfies [version_min, version_max] range."""
    av = _parse_semver(actual)
    if version_min and av < _parse_semver(version_min):
        return False
    if version_max and av > _parse_semver(version_max):
        return False
    return True


# ---------------------------------------------------------------------------
# Main PlanCompiler class
# ---------------------------------------------------------------------------


class PlanCompiler:
    """Orchestrates Phase 1→2 plan compilation for a single node.

    Args:
        db_session: penguin-dal ``DB`` instance (task-7b runtime-DAL
            migration -- despite the name, this is no longer a SQLAlchemy
            ``Session``; kept as ``db_session`` because the sole call site,
            ``app.api.nodes.deploy_node``, constructs this class with
            ``db_session=db`` as a keyword argument). Reads in
            ``_load_node``/``_load_eggs``/``_resolve_joiner_secrets`` apply
            NO explicit tenant filter -- they rely entirely on ambient
            Postgres RLS. That is safe here specifically because
            ``PlanCompiler`` is constructed exactly once in this codebase,
            from inside a Quart request handler (``deploy_node``) *after*
            it has already called ``app.models.get_db()``, i.e. after
            the tenant bridge in ``app.middleware`` has set the request's tenant on
            ``app.db.rls``'s ContextVar. Per ``app.db.rls``'s own module
            docstring, that ContextVar propagates through
            ``asyncio.to_thread()`` (the intended way to invoke the
            synchronous ``compile()``/``validate()`` methods below without
            blocking the event loop) because ``copy_context()`` carries it
            onto the worker thread. Contrast with
            ``app.workers.joiner_secret_emitter.JoinerSecretEmitter`` and
            the gRPC servicers in ``app.grpc_server``, which run with no
            request/tenant context at all and must explicitly push
            ``app.db.rls.CROSS_TENANT_SENTINEL`` for the duration of their
            reads -- unnecessary (and would be *wrong*, widening visibility
            past the request's own tenant) here.
        vault_client: Configured VaultClient for transit encryption.
        lxd_client: Module reference exposing ``mint_join_token``.
        spire_client: SpireClient (currently unused in M1; reserved for M2).
    """

    def __init__(
        self,
        db_session: Any,
        vault_client: Any,
        lxd_client: Any,
        spire_client: Any = None,
    ) -> None:
        """Initialise compiler with injected dependencies."""
        self.db_session = db_session
        self.vault_client = vault_client
        self.lxd_client = lxd_client
        self.spire_client = spire_client

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def validate(self, plan_request: PlanRequest, node: Any) -> list[PlanValidationError]:
        """Pre-flight validation — collects ALL errors without raising.

        Checks:
        - Biome existence
        - Phase cross-dependency violations
        - Hardware tag satisfiability
        - VRAM constraints
        - Joiner-secret availability
        - Version range constraints
        - Dependency cycle

        Returns a (possibly empty) list of ``PlanValidationError``.
        """
        errors: list[PlanValidationError] = []

        # 1. Load biomes
        biome_nodes, load_errors = self._load_eggs(plan_request)
        errors.extend(load_errors)
        if load_errors:
            # Cannot proceed without valid biome set
            return errors

        # 2. Phase cross-dependency check
        errors.extend(self._check_phase_violations(biome_nodes))

        # 3. Hardware tag check per biome
        errors.extend(self._check_hardware_tags(biome_nodes, node))

        # 4. VRAM / GPU check per biome
        errors.extend(self._check_vram(biome_nodes, node))

        # 5. Joiner-secret availability
        now = datetime.now(timezone.utc)
        cluster_id = plan_request.cluster_id
        for egg_node in biome_nodes.values():
            _, js_errors = _resolve_joiner_secrets(
                self.db_session, egg_node, cluster_id, now
            )
            errors.extend(js_errors)

        # 6. Version range checks
        errors.extend(self._check_version_ranges(biome_nodes))

        # 7. Dependency cycle (Kahn)
        _, cycle_errors = _topo_sort(biome_nodes)
        errors.extend(cycle_errors)

        return errors

    def compile(self, node_id: int, plan_request: PlanRequest) -> CompiledPlan:
        """Compile a fully resolved plan for *node_id*.

        Steps (per spec):
        1. Load node
        2. Load biome set
        3. Kahn topo-sort + cycle detection
        4. Phase cross-dep validation
        5. Hardware tag + VRAM matching
        6. Cloud-init merge with path-collision detection
        7. LUKS key generation + sealing
        8. Joiner-secret availability check
        9. LXD join token minting
        10. Phase-2 iPXE script rendering

        Raises:
            PlanCompilationError: if any validation error is collected.
        """
        errors: list[PlanValidationError] = []
        warnings: list[str] = []

        # Step 1: load node
        node = self._load_node(node_id)
        if node is None:
            raise PlanCompilationError(
                [
                    PlanValidationError(
                        code="node_not_found",
                        message=f"Node {node_id} not found",
                        details={"node_id": node_id},
                    )
                ]
            )

        # Step 2: load biomes
        biome_nodes, load_errors = self._load_eggs(plan_request)
        errors.extend(load_errors)

        if not load_errors:
            # Step 3: Kahn topo-sort
            sorted_ids, cycle_errors = _topo_sort(biome_nodes)
            errors.extend(cycle_errors)

            if not cycle_errors:
                # Step 4: phase cross-dep validation
                errors.extend(self._check_phase_violations(biome_nodes))

                # Step 5: hardware tag + VRAM
                errors.extend(self._check_hardware_tags(biome_nodes, node))
                errors.extend(self._check_vram(biome_nodes, node))

                # Step 6: version range
                errors.extend(self._check_version_ranges(biome_nodes))

                # Soft dependency warnings
                for egg_node in biome_nodes.values():
                    for soft_dep in egg_node.soft_deps:
                        if soft_dep not in biome_nodes:
                            warnings.append(
                                f"Soft dependency biome_id={soft_dep} for biome "
                                f"{egg_node.biome_name!r} is not in plan — proceeding"
                            )
            else:
                sorted_ids = []
        else:
            sorted_ids = []

        if errors:
            raise PlanCompilationError(errors)

        # From here: no hard errors
        # Ordered list of biome dicts for cloud-init
        ordered_eggs = [biome_nodes[eid].raw for eid in sorted_ids]

        # Step 6: cloud-init merge
        cloud_init_b64, ci_warnings, ci_errors = _merge_cloud_init_with_errors(ordered_eggs)
        warnings.extend(ci_warnings)
        if ci_errors:
            raise PlanCompilationError(ci_errors)

        # Step 7: LUKS key generation + sealing
        raw_luks_key = secrets.token_bytes(32)
        luks_tier = self._resolve_luks_tier(node)
        luks_key_sealed, luks_key_provenance = _seal_luks_key(
            luks_tier, raw_luks_key, self.vault_client
        )
        del raw_luks_key  # zero-like: let GC handle it

        # Step 8: joiner-secret resolution
        now = datetime.now(timezone.utc)
        cluster_id = plan_request.cluster_id
        all_js_refs: list[str] = []
        for eid in sorted_ids:
            egg_node = biome_nodes[eid]
            refs, js_errors = _resolve_joiner_secrets(
                self.db_session, egg_node, cluster_id, now
            )
            if js_errors:
                raise PlanCompilationError(js_errors)
            all_js_refs.extend(refs)

        # Step 9: LXD join token
        lxd_token_obj = self.lxd_client.mint_join_token(
            cluster_id or "default", ttl_seconds=3600
        )
        lxd_join_token = lxd_token_obj.token

        # Step 10: plan ID + iPXE script
        plan_id = _generate_uuidv7()
        phase2_ipxe = _render_phase2_ipxe(node, plan_id)
        disk_script = _render_disk_script(plan_request.disk_plan)

        return CompiledPlan(
            plan_id=plan_id,
            biome_order=sorted_ids,
            cloud_init_bundle=cloud_init_b64,
            disk_script=disk_script,
            luks_key_sealed=luks_key_sealed,
            luks_key_provenance=luks_key_provenance,
            lxd_join_token=lxd_join_token,
            phase2_ipxe_script=phase2_ipxe,
            joiner_secret_refs=all_js_refs,
            compile_warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_node(self, node_id: int) -> Any | None:
        """Load node from DB; return None if not found.

        No app-level tenant filter -- relies on ambient RLS (see class
        docstring: this only ever runs request-scoped).
        """
        return self.db_session(self.db_session.nodes.id == node_id).select().first()

    def _load_eggs(
        self, plan_request: PlanRequest
    ) -> tuple[dict[int, _BiomeNode], list[PlanValidationError]]:
        """Load all requested biomes from DB and build _BiomeNode map.

        No app-level tenant filter -- relies on ambient RLS (see class
        docstring: this only ever runs request-scoped).
        """
        errors: list[PlanValidationError] = []
        biome_nodes: dict[int, _BiomeNode] = {}

        biome_ids = [ref.biome_id for ref in plan_request.biome_assignments]
        if not biome_ids:
            return {}, []

        rows = self.db_session(self.db_session.biomes.id.belongs(biome_ids)).select()

        found_ids = set()
        for row in rows:
            row_dict = row.as_dict()
            eid = row_dict["id"]
            found_ids.add(eid)
            # Rows stored before the k8s-controller rename still carry the legacy name.
            if isinstance(row_dict.get("name"), str):
                row_dict["name"] = canonical_biome_name(row_dict["name"])

            # Parse JSON fields
            def _j(col: str, default: Any = None) -> Any:
                v = row_dict.get(col)
                if v is None:
                    return default
                if isinstance(v, (dict, list)):
                    return v
                try:
                    return json.loads(v)
                except (TypeError, json.JSONDecodeError):
                    return default

            deps_raw = _j("dependencies", [])
            hard_deps: list[int] = []
            soft_deps: list[int] = []
            version_constraints: list[dict[str, Any]] = []
            for dep in deps_raw if isinstance(deps_raw, list) else []:
                if isinstance(dep, int):
                    hard_deps.append(dep)
                elif isinstance(dep, dict):
                    dep_id = dep.get("biome_id") or dep.get("id") or dep.get("egg_id")
                    mode = dep.get("mode", "hard")
                    if dep_id is not None:
                        if mode == "soft":
                            soft_deps.append(int(dep_id))
                        else:
                            hard_deps.append(int(dep_id))
                    # Version constraints
                    if dep.get("version_min") or dep.get("version_max"):
                        version_constraints.append(dep)

            consumes = _j("consumes_joiner_secrets_from", [])
            if isinstance(consumes, list):
                # May be a list of strings (biome_kind) or list of dicts
                consumes_list: list[dict[str, Any]] = []
                for c in consumes:
                    if isinstance(c, str):
                        consumes_list.append({"biome_kind": c, "extractor_name": "", "scope": "cluster"})
                    elif isinstance(c, dict):
                        # Backward-compat: remap egg_kind → biome_kind if needed
                        c_compat = dict(c)
                        if "egg_kind" in c_compat and "biome_kind" not in c_compat:
                            c_compat["biome_kind"] = c_compat.pop("egg_kind")
                        consumes_list.append(c_compat)
            else:
                consumes_list = []

            biome_nodes[eid] = _BiomeNode(
                biome_id=eid,
                biome_name=row_dict.get("name", f"biome-{eid}"),
                phase=row_dict.get("phase", "post_deploy"),
                deps=hard_deps,
                soft_deps=soft_deps,
                version_constraints=version_constraints,
                requires_hardware_tags=_j("requires_hardware_tags", []),
                prefers_hardware_tags=_j("prefers_hardware_tags", []),
                forbids_hardware_tags=_j("forbids_hardware_tags", []),
                consumes_joiner_secrets_from=consumes_list,
                cloud_init=_j("cloud_init", {}),
                storage_requirements=_j("storage_requirements_json", {}),
                raw={
                    **row_dict,
                    # Overwrite JSON-string fields with parsed versions
                    "id": eid,
                    "name": row_dict.get("name", f"biome-{eid}"),
                    "phase": row_dict.get("phase", "post_deploy"),
                    "cloud_init": _j("cloud_init", {}),
                    "requires_hardware_tags": _j("requires_hardware_tags", []),
                    "forbids_hardware_tags": _j("forbids_hardware_tags", []),
                    "storage_requirements_json": _j("storage_requirements_json", {}),
                },
            )

        # Report missing biomes
        for ref in plan_request.biome_assignments:
            if ref.biome_id not in found_ids:
                errors.append(
                    PlanValidationError(
                        code="egg_not_found",
                        message=f"Biome {ref.biome_id} not found in catalog",
                        details={"biome_id": ref.biome_id},
                    )
                )

        return biome_nodes, errors

    def _check_phase_violations(
        self, biome_nodes: dict[int, _BiomeNode]
    ) -> list[PlanValidationError]:
        """Detect cross-phase dependency violations.

        A biome of rank X cannot depend on a biome of rank > X.
        For example: phase1_helper (rank 0) cannot depend on phase2_initial (rank 1)
        or post_deploy (rank 2); phase2_initial (rank 1) cannot depend on post_deploy (rank 2).
        """
        errors: list[PlanValidationError] = []
        for eid, node in biome_nodes.items():
            node_rank = _phase_rank(node.phase)
            for dep_id in node.deps:
                dep = biome_nodes.get(dep_id)
                if dep is None:
                    continue
                dep_rank = _phase_rank(dep.phase)
                if dep_rank > node_rank:
                    errors.append(
                        PlanValidationError(
                            code="phase_dependency_violation",
                            message=(
                                f"Biome {node.biome_name!r} (phase={node.phase}, rank={node_rank}) "
                                f"depends on {dep.biome_name!r} (phase={dep.phase}, rank={dep_rank}); "
                                f"cannot depend on later phases"
                            ),
                            details={
                                "biome_id": eid,
                                "biome_name": node.biome_name,
                                "dep_biome_id": dep_id,
                                "dep_biome_name": dep.biome_name,
                                "egg_phase": node.phase,
                                "dep_phase": dep.phase,
                                "node_rank": node_rank,
                                "dep_rank": dep_rank,
                            },
                        )
                    )
        return errors

    def _check_hardware_tags(
        self, biome_nodes: dict[int, _BiomeNode], node: Any
    ) -> list[PlanValidationError]:
        """Check hardware tag requires/forbids for each biome against the node."""
        errors: list[PlanValidationError] = []

        # Build effective tag set: hardware_tags + operator tags
        hw_tags: list[str] = []
        hw_tags_raw = getattr(node, "hardware_tags", None)
        if hw_tags_raw:
            if isinstance(hw_tags_raw, list):
                hw_tags = hw_tags_raw
            elif isinstance(hw_tags_raw, dict):
                hw_tags = list(hw_tags_raw.keys())
            elif isinstance(hw_tags_raw, str):
                try:
                    parsed = json.loads(hw_tags_raw)
                    hw_tags = parsed if isinstance(parsed, list) else list(parsed.keys() if isinstance(parsed, dict) else [])
                except (json.JSONDecodeError, TypeError):
                    hw_tags = []

        node_id = getattr(node, "id", None) or (node[0] if hasattr(node, "__getitem__") else "?")

        for eid, egg_node in biome_nodes.items():
            required = egg_node.requires_hardware_tags
            forbidden = egg_node.forbids_hardware_tags

            satisfied, missing = _node_tag_set_satisfies(hw_tags, required)
            forbidden_present = _node_has_forbidden_tag(hw_tags, forbidden)

            if missing or forbidden_present:
                errors.append(
                    PlanValidationError(
                        code="tag_constraint_unsatisfiable",
                        message=(
                            f"Biome {egg_node.biome_name!r} cannot be scheduled on node {node_id}: "
                            f"missing_tags={missing}, forbidden_tags_present={forbidden_present}"
                        ),
                        details={
                            "biome_id": eid,
                            "biome_name": egg_node.biome_name,
                            "missing_tags": missing,
                            "forbidden_tags_present": forbidden_present,
                            "candidates_considered": 1,
                            "elimination_reasons": {
                                str(node_id): [
                                    *[f"missing:{t}" for t in missing],
                                    *[f"forbidden:{t}" for t in forbidden_present],
                                ]
                            },
                        },
                    )
                )

        return errors

    def _check_vram(
        self, biome_nodes: dict[int, _BiomeNode], node: Any
    ) -> list[PlanValidationError]:
        """Check VRAM requirements from storage_requirements_json.needs_gpu."""
        errors: list[PlanValidationError] = []
        for eid, egg_node in biome_nodes.items():
            needs_gpu = egg_node.storage_requirements.get("needs_gpu")
            if needs_gpu:
                errors.extend(
                    _check_vram_requirements(node, eid, egg_node.biome_name, needs_gpu)
                )
        return errors

    def _check_version_ranges(
        self, biome_nodes: dict[int, _BiomeNode]
    ) -> list[PlanValidationError]:
        """Validate version_min/max constraints for biome dependencies."""
        errors: list[PlanValidationError] = []
        for eid, egg_node in biome_nodes.items():
            for vc in egg_node.version_constraints:
                dep_id = vc.get("biome_id") or vc.get("id") or vc.get("egg_id")
                if dep_id is None:
                    continue
                dep_node = biome_nodes.get(int(dep_id))
                if dep_node is None:
                    # External dep — skip version check here
                    continue
                dep_version = dep_node.raw.get("version", "")
                version_min = vc.get("version_min")
                version_max = vc.get("version_max")
                if not dep_version:
                    continue
                if not _satisfies_version_range(dep_version, version_min, version_max):
                    errors.append(
                        PlanValidationError(
                            code="version_range_unsatisfied",
                            message=(
                                f"Biome {egg_node.biome_name!r} requires dep "
                                f"{dep_node.biome_name!r} version "
                                f"[{version_min or '*'}, {version_max or '*'}] "
                                f"but got {dep_version!r}"
                            ),
                            details={
                                "biome_id": eid,
                                "dep_biome_id": dep_id,
                                "dep_biome_name": dep_node.biome_name,
                                "dep_version": dep_version,
                                "version_min": version_min,
                                "version_max": version_max,
                            },
                        )
                    )
        return errors

    def _resolve_luks_tier(self, node: Any) -> str:
        """Determine LUKS sealing tier from node hardware/config."""
        hw: dict[str, Any] = {}
        hw_raw = getattr(node, "hardware_json", None)
        if isinstance(hw_raw, dict):
            hw = hw_raw
        elif isinstance(hw_raw, str):
            try:
                hw = json.loads(hw_raw)
            except (json.JSONDecodeError, TypeError):
                hw = {}

        # TPM2 available → tpm2 tier; cloud VM → dev (cloud KMS not yet implemented); else dev
        hw_tags_raw = getattr(node, "hardware_tags", None) or []
        if isinstance(hw_tags_raw, str):
            try:
                hw_tags_raw = json.loads(hw_tags_raw)
            except (json.JSONDecodeError, TypeError):
                hw_tags_raw = []
        hw_tags: list[str] = hw_tags_raw if isinstance(hw_tags_raw, list) else []

        if "tpm:2.0" in hw_tags:
            return "tpm2"
        if hw.get("platform") in ("cloud-vm", "cloud"):
            # Cloud VMs lack TPM; seal via the cloud provider's native KMS
            # (AWS KMS / GCP Cloud KMS / Azure Key Vault) instead of the
            # weaker "dev" (Vault-transit-only) tier.
            return "cloud-kms"
        return "dev"


# Backward-compat factory function
def _EggNode(egg_id: int, egg_name: str, **kwargs: Any) -> _BiomeNode:
    """Backward-compat factory: maps egg_id/egg_name → biome_id/biome_name."""
    return _BiomeNode(biome_id=egg_id, biome_name=egg_name, **kwargs)
