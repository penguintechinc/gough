"""Pydantic v2 models for the Gough ``biome.yaml`` schema (Sprint 2).

Keep this module dependency-free besides Pydantic so it can be reused by
the gRPC plane and the OpenAPI export later.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BiomeKind = Literal[
    "infrastructure",
    "k8s",
    "monitoring",
    "storage",
    "user_workload",
    "custom",
]

BiomePhase = Literal[
    "phase1_helper",
    "phase2_initial",
    "post_deploy",
    "always",
]

WorkloadType = Literal["lxc", "vm"]

UpgradeStrategy = Literal["rolling", "one-at-a-time", "canary", "blue-green"]


class StorageRequirements(BaseModel):
    model_config = ConfigDict(extra="allow")
    min_ram_mb: Annotated[int, Field(ge=0)] = 0
    min_disk_gb: Annotated[int, Field(ge=0)] = 0
    needs_gpu: bool = False
    needs_nested_virt: bool = False
    dark_drive_count: Annotated[int, Field(ge=0)] = 0


class HttpGetProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    port: Annotated[int, Field(ge=1, le=65535)]
    scheme: Literal["HTTP", "HTTPS"] = "HTTP"
    headers: Optional[dict[str, str]] = None


class GrpcProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str
    port: Annotated[int, Field(ge=1, le=65535)]


class ExecProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(min_length=1)


class LxdExecProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance: str
    command: list[str] = Field(min_length=1)


class ReadinessProbe(BaseModel):
    """Mutually exclusive among ``httpGet``, ``grpc``, ``exec``, ``lxdExec``.

    Defaults from spec: ``timeoutSeconds=5``, ``periodSeconds=2``,
    ``failureThreshold=30``, ``successThreshold=1``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    http_get: Optional[HttpGetProbe] = Field(default=None, alias="httpGet")
    grpc: Optional[GrpcProbe] = None
    exec_: Optional[ExecProbe] = Field(default=None, alias="exec")
    lxd_exec: Optional[LxdExecProbe] = Field(default=None, alias="lxdExec")
    timeout_seconds: Annotated[int, Field(ge=1, le=600)] = Field(5, alias="timeoutSeconds")
    period_seconds: Annotated[int, Field(ge=1, le=600)] = Field(2, alias="periodSeconds")
    failure_threshold: Annotated[int, Field(ge=1, le=1000)] = Field(30, alias="failureThreshold")
    success_threshold: Annotated[int, Field(ge=1, le=100)] = Field(1, alias="successThreshold")

    @model_validator(mode="after")
    def _exactly_one_probe(self):
        kinds = [
            self.http_get,
            self.grpc,
            self.exec_,
            self.lxd_exec,
        ]
        if sum(1 for k in kinds if k is not None) != 1:
            raise ValueError(
                "readiness_probe must declare exactly one of httpGet, grpc, exec, lxdExec"
            )
        return self


class BiomeCreate(BaseModel):
    """Body for ``POST /api/v1/biomes`` — full ``biome.yaml`` (Sprint 2).

    Mirrors ``Biome Catalog → Common Authoring Conventions``. Fields not
    yet stored verbatim are accepted (``extra=allow``) so the row can
    capture forward-compatible authoring data without a migration.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: Annotated[str, Field(min_length=1, max_length=255)]
    display_name: Annotated[str, Field(min_length=1, max_length=255)]
    description: Optional[str] = None
    biome_type: Literal["snap", "cloud_init", "lxd_container", "lxd_vm"]
    version: Annotated[str, Field(min_length=1, max_length=64)]

    # M1 extension columns
    biome_kind: BiomeKind = "custom"
    phase: BiomePhase = "post_deploy"
    workload_type: WorkloadType = "lxc"
    lock_to_host: bool = False
    auto_join_cluster: bool = False
    upgrade_strategy: UpgradeStrategy = "rolling"

    # Hardware tag declarations
    requires_hardware_tags: list[str] = Field(default_factory=list)
    prefers_hardware_tags: list[str] = Field(default_factory=list)
    forbids_hardware_tags: list[str] = Field(default_factory=list)

    # Resource and joiner declarations
    storage_requirements: Optional[StorageRequirements] = None
    readiness_probe: Optional[ReadinessProbe] = None
    emits_joiner_secrets: bool = False
    joiner_emit_spec: Optional[dict[str, Any]] = None
    consumes_joiner_secrets_from: Optional[list[str]] = None
    joiner_consume_spec: Optional[dict[str, Any]] = None
    snapshot_schedule: Optional[dict[str, Any]] = None
    required_interfaces: Optional[list[dict[str, Any]]] = None

    # Existing legacy columns (preserved; some authors still use them)
    category: Optional[str] = None
    snap_name: Optional[str] = None
    snap_channel: Optional[str] = "stable"
    snap_classic: bool = False
    cloud_init_content: Optional[str] = None
    lxd_image_alias: Optional[str] = None
    lxd_image_url: Optional[str] = None
    lxd_profiles: Optional[list[str]] = None
    is_hypervisor_config: bool = False
    dependencies: Optional[list[int]] = None
    min_ram_mb: Annotated[int, Field(ge=0)] = 0
    min_disk_gb: Annotated[int, Field(ge=0)] = 0
    required_architecture: Literal["amd64", "arm64", "any"] = "any"
    is_active: bool = True
    is_default: bool = False

    # Signing-related metadata (set by the sign endpoint, but accepted
    # here for already-signed authoring uploads).
    signing_key_id: Optional[str] = None
    sbom_url: Optional[str] = None
    registry_url: Optional[str] = None


class BiomeSignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: Annotated[str, Field(min_length=1, max_length=255)]
    reason: Annotated[str, Field(min_length=1, max_length=2048)]


class BiomeUpgradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_version: Annotated[str, Field(min_length=1, max_length=64)]
    approval_token: Optional[str] = None
    # Either a named strategy ("auto"/"canary") or an explicit orchestration
    # config dict (e.g. {"batch_size": 2}) consumed by
    # ``_execute_upgrade_orchestration`` -- see app/api/biomes.py.
    rollout_plan: Literal["auto", "canary"] | dict[str, Any] = "auto"


class NodeBiomeAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    biome_id: Annotated[int, Field(gt=0)]
    phase: BiomePhase = "post_deploy"
    depends_on_biome_instance_id: Optional[Annotated[int, Field(gt=0)]] = None

    @model_validator(mode='before')
    @classmethod
    def _remap_egg_fields(cls, data: Any) -> Any:
        """Backward-compat: accept egg_* fields and remap to biome_* fields."""
        if isinstance(data, dict):
            data = dict(data)
            if 'egg_id' in data and 'biome_id' not in data:
                data['biome_id'] = data.pop('egg_id')
            if 'depends_on_egg_instance_id' in data and 'depends_on_biome_instance_id' not in data:
                data['depends_on_biome_instance_id'] = data.pop('depends_on_egg_instance_id')
        return data
