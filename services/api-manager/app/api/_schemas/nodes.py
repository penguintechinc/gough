"""Pydantic v2 request/response schemas for the Nodes Blueprint.

All models use strict=False (Pydantic default) so that JSON integers
coerce cleanly to Python ints and strings coerce to expected types
where the spec allows it.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Discover endpoint
# ---------------------------------------------------------------------------


class NicInfo(BaseModel):
    """NIC descriptor reported by the discovery agent."""

    mac: str = Field(..., description="MAC address (colon-separated hex)")
    speed_mbps: Optional[int] = Field(None, description="Link speed in Mbit/s")
    pci_slot: Optional[str] = Field(None, description="PCI slot identifier")
    is_primary: bool = Field(False, description="True for the provisioning NIC")
    name: Optional[str] = Field(None, description="Kernel interface name, e.g. eth0")


class SmartAttribute(BaseModel):
    """Single SMART attribute entry for a disk."""

    id: int = Field(..., description="SMART attribute ID")
    name: str = Field(..., description="Attribute name")
    value: int = Field(..., description="Normalised value (0-255)")
    worst: int = Field(..., description="Worst recorded value")
    thresh: int = Field(..., description="Failure threshold")
    raw_value: Optional[int] = Field(None, description="Raw integer value")


class NumaTopology(BaseModel):
    """NUMA topology reported by numactl / lscpu."""

    nodes: int = Field(..., ge=1, description="Number of NUMA nodes")
    cpu_map: dict[str, list[int]] = Field(
        default_factory=dict,
        description="NUMA node → CPU list, e.g. {'0': [0,1,2,3]}",
    )
    memory_mb: dict[str, int] = Field(
        default_factory=dict,
        description="NUMA node → memory in MiB",
    )


class AcceleratorInfo(BaseModel):
    """GPU / accelerator descriptor."""

    bus_type: str = Field(..., description="pcie|integrated|usb|embedded")
    vendor: str = Field(..., description="nvidia|amd|intel|habana|other")
    model: str = Field(..., description="Human-readable model name")
    vram_mb: Optional[int] = Field(None, description="VRAM in MiB")
    pci_address: Optional[str] = Field(None, description="PCI BDF address")


class DiscoverRequest(BaseModel):
    """Body for POST /api/v1/nodes/discover."""

    dmi_uuid: str = Field(..., min_length=1, description="DMI system UUID")
    primary_nic_mac: str = Field(..., min_length=1, description="MAC of provisioning NIC")
    firmware_type: Literal["bios", "uefi"] = Field(
        ..., description="Firmware type detected by the agent"
    )
    lshw_json: Optional[dict[str, Any]] = Field(
        None, description="Full lshw -json output"
    )
    lsblk_json: Optional[dict[str, Any]] = Field(
        None, description="Full lsblk --json output"
    )
    smart_attributes: list[SmartAttribute] = Field(
        default_factory=list,
        description="SMART attributes for each discovered disk",
    )
    nics: list[NicInfo] = Field(
        default_factory=list,
        description="List of discovered NICs",
    )
    numa_topology: Optional[NumaTopology] = Field(
        None, description="NUMA topology from numactl"
    )
    accelerators: list[AcceleratorInfo] = Field(
        default_factory=list,
        description="GPU / accelerator descriptors",
    )
    hardware_tags: list[str] = Field(
        default_factory=list,
        description="Auto-discovered hardware tags from the agent",
    )


class DiscoverResponse(BaseModel):
    """Body returned by POST /api/v1/nodes/discover."""

    node_id: int
    state: str
    spire_join_token: Optional[str]
    control_tunnel_endpoint: str
    control_tunnel_spiffe_id: str


# ---------------------------------------------------------------------------
# List / filter
# ---------------------------------------------------------------------------


class NodeListResponse(BaseModel):
    """Response data for GET /api/v1/nodes."""

    nodes: list[dict[str, Any]]
    total: int
    next_cursor: Optional[str] = None


# ---------------------------------------------------------------------------
# Node detail patch (RFC 7396 JSON Merge Patch)
# ---------------------------------------------------------------------------


class NodePatchRequest(BaseModel):
    """Allowed fields for PATCH /api/v1/nodes/{id}.

    ``tenant_id`` is permitted only for super-admin (cross_tenant) callers;
    the handler enforces that separately.
    """

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    ipv4_static: Optional[str] = Field(None, description="Static IPv4 address")
    tenant_id: Optional[str] = Field(None, description="Super-admin only: move tenant")


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


class RejectRequest(BaseModel):
    """Body for POST /api/v1/nodes/{id}/reject."""

    reason: str = Field(..., min_length=1, max_length=1024)


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------


class BiomeAssignmentSpec(BaseModel):
    """Biome assignment within a deploy request."""

    biome_id: int = Field(..., gt=0)
    phase: Optional[str] = Field(None)
    depends_on_biome_instance_id: Optional[int] = Field(None)

    @model_validator(mode='before')
    @classmethod
    def _remap_egg_fields(cls, data: Any) -> Any:
        """Backward-compat: accept egg_id and remap to biome_id."""
        if isinstance(data, dict) and 'egg_id' in data and 'biome_id' not in data:
            data = dict(data)
            data['biome_id'] = data.pop('egg_id')
        return data


class DeployRequest(BaseModel):
    """Body for POST /api/v1/nodes/{id}/deploy."""

    plan_id: Optional[int] = Field(None, description="Optional existing plan ID to reuse")
    biome_assignments: list[BiomeAssignmentSpec] = Field(
        ..., min_length=1, description="Biomes to assign"
    )
    disk_plan: Optional[dict[str, Any]] = Field(
        None, description="Disk partitioning/filesystem plan JSON"
    )
    reason: Optional[str] = Field(None, max_length=1024)

    @model_validator(mode='before')
    @classmethod
    def _remap_egg_assignments(cls, data: Any) -> Any:
        """Backward-compat: accept egg_assignments and remap to biome_assignments."""
        if isinstance(data, dict) and 'egg_assignments' in data and 'biome_assignments' not in data:
            data = dict(data)
            data['biome_assignments'] = data.pop('egg_assignments')
        return data

    @field_validator("biome_assignments", mode="before")
    @classmethod
    def _not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("biome_assignments must contain at least one entry")
        return v


# ---------------------------------------------------------------------------
# Evacuate
# ---------------------------------------------------------------------------


class EvacuateRequest(BaseModel):
    """Body for POST /api/v1/nodes/{id}/evacuate."""

    reason: Optional[str] = Field(None, max_length=1024)
    force: bool = Field(False, description="Bypass safety-envelope check (audit-logged)")


# ---------------------------------------------------------------------------
# Node event (service→api-manager progress events)
# ---------------------------------------------------------------------------


class NodeEventRequest(BaseModel):
    """Body for POST /api/v1/nodes/{id}/events.

    Posted by the discovery-agent / cloud-init runcmd steps.  The
    ``stage`` field identifies the deployment phase; ``progress_pct``
    is 0-100 and optional.
    """

    stage: str = Field(
        ..., min_length=1, max_length=255,
        description="Machine-readable stage identifier, e.g. 'disk_partition_start'",
    )
    message: str = Field(..., min_length=1, max_length=4096)
    progress_pct: Optional[int] = Field(
        None, ge=0, le=100, description="0–100 completion percentage"
    )
    timestamp: Optional[str] = Field(
        None, description="RFC 3339 timestamp; defaults to server time if omitted"
    )


# ---------------------------------------------------------------------------
# Decommission / soft-delete
# ---------------------------------------------------------------------------


class DecommissionRequest(BaseModel):
    """Body for DELETE /api/v1/nodes/{id}."""

    reason: str = Field(..., min_length=1, max_length=1024)


# ---------------------------------------------------------------------------
# Tags patch
# ---------------------------------------------------------------------------


class TagEntry(BaseModel):
    """A single operator tag (key:value pair)."""

    tag_key: str = Field(..., min_length=1, max_length=255)
    tag_value: str = Field(..., min_length=1, max_length=255)


class TagsPatchRequest(BaseModel):
    """Body for PATCH /api/v1/nodes/{id}/tags."""

    add: list[TagEntry] = Field(default_factory=list)
    remove: list[TagEntry] = Field(default_factory=list)

    @field_validator("add", "remove", mode="before")
    @classmethod
    def _lists_are_lists(cls, v: Any) -> Any:
        if v is None:
            return []
        return v
