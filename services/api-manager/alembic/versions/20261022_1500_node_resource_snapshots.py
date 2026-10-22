"""Add node resource snapshots and capacity reservations (capacity guard).

Revision ID: 20261022_1500_node_resource_snapshots
Revises: 20261022_1400_biome_bundle_cache
Create Date: 2026-10-22 15:00:00.000000

Additive only, guarded by reflection.

- node_resource_snapshots: one row per node, the latest host resource snapshot
  reported by the access agent (gough.host_resources.v1).
- capacity_reservations: placement holds checked against a node's allocatable
  capacity; status uses int codes (1 active, 2 released, 3 expired).
"""

import sqlalchemy as sa

from alembic import op

revision = "20261022_1500_node_resource_snapshots"
down_revision = "20261022_1400_biome_bundle_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the snapshot and reservation tables when absent."""
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())

    if "node_resource_snapshots" not in existing:
        op.create_table(
            "node_resource_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "node_id",
                sa.Integer(),
                sa.ForeignKey("nodes.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("schema_version", sa.String(64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("logical_cores", sa.Integer(), nullable=False),
            sa.Column("cpu_usage_pct", sa.Float(), nullable=True),
            sa.Column("memory_total_bytes", sa.BigInteger(), nullable=False),
            sa.Column("memory_available_bytes", sa.BigInteger(), nullable=False),
            sa.Column("committed_cpu_cores", sa.Float(), nullable=False),
            sa.Column("committed_memory_bytes", sa.BigInteger(), nullable=False),
            sa.Column("instances_without_limits", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("psi_available", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index(
            "ix_node_resource_snapshots_received_at",
            "node_resource_snapshots",
            ["received_at"],
        )

    if "capacity_reservations" not in existing:
        op.create_table(
            "capacity_reservations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("public_id", sa.String(36), nullable=False, unique=True),
            sa.Column(
                "node_id",
                sa.Integer(),
                sa.ForeignKey("nodes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("cpu_cores", sa.Float(), nullable=False),
            sa.Column("memory_bytes", sa.BigInteger(), nullable=False),
            sa.Column("disk_bytes", sa.BigInteger(), nullable=False),
            sa.Column("pool", sa.String(255), nullable=False),
            sa.Column("status", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_capacity_reservations_node_status",
            "capacity_reservations",
            ["node_id", "status"],
        )


def downgrade() -> None:
    """Drop the capacity guard tables."""
    op.drop_index("ix_capacity_reservations_node_status", table_name="capacity_reservations")
    op.drop_table("capacity_reservations")
    op.drop_index("ix_node_resource_snapshots_received_at", table_name="node_resource_snapshots")
    op.drop_table("node_resource_snapshots")
