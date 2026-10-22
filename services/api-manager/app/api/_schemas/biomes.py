"""Pydantic v2 request/response schemas for the Biomes Blueprint.

Defines models for biome-group endpoints to enable detailed OpenAPI documentation.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class BiomeMemberRef(BaseModel):
    """Reference to a biome within a biome-group membership list."""

    biome_id: int = Field(..., description="ID of the biome")
    order: int = Field(default=0, description="Sort order in the group")


class BiomeGroupCreateRequest(BaseModel):
    """Request body for creating a biome group."""

    name: str = Field(..., description="Unique identifier for the group")
    display_name: str = Field(..., description="Human-readable display name")
    description: Optional[str] = Field(None, description="Group description")
    biomes: list[BiomeMemberRef] = Field(..., description="Array of biomes in the group")
    is_default: Optional[bool] = Field(False, description="Whether this is a default group")


class BiomeGroupUpdateRequest(BaseModel):
    """Request body for updating a biome group."""

    name: Optional[str] = Field(None, description="Unique identifier for the group")
    display_name: Optional[str] = Field(None, description="Human-readable display name")
    description: Optional[str] = Field(None, description="Group description")
    biomes: Optional[list[BiomeMemberRef]] = Field(None, description="Array of biomes in the group")
    is_default: Optional[bool] = Field(None, description="Whether this is a default group")


class BiomeGroupResponse(BaseModel):
    """Response body for biome group queries."""

    id: int = Field(..., description="Biome group ID")
    name: str = Field(..., description="Unique identifier for the group")
    display_name: str = Field(..., description="Human-readable display name")
    description: Optional[str] = Field(None, description="Group description")
    biomes: list[BiomeMemberRef] = Field(default_factory=list, description="Array of biomes in the group")
    is_default: bool = Field(False, description="Whether this is a default group")
    created_at: Optional[str] = Field(None, description="Creation timestamp (ISO 8601)")
    updated_at: Optional[str] = Field(None, description="Last update timestamp (ISO 8601)")


class BiomeGroupListResponse(BaseModel):
    """Response body for listing biome groups."""

    groups: list[BiomeGroupResponse] = Field(default_factory=list, description="List of biome groups")
