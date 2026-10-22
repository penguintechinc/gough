"""Agent-to-node link and reservation confirmation timestamp.

Adds ``access_agents.node_id`` (server-verified agent<->node binding used by the
capacity guard's snapshot ingest) and ``capacity_reservations.confirmed_at``.
Idempotent: columns are only added when absent.

Revision ID: 20261022_1600_agent_node_link_reservation_confirm
Revises: 20261022_1500_node_resource_snapshots
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261022_1600_agent_node_link_reservation_confirm"
down_revision = "20261022_1500_node_resource_snapshots"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    """Return the existing column names of ``table``."""
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    """Add the agent node link and the reservation confirmation timestamp."""
    if "node_id" not in _columns("access_agents"):
        with op.batch_alter_table("access_agents") as batch:
            batch.add_column(sa.Column("node_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_access_agents_node_id", "nodes", ["node_id"], ["id"], ondelete="SET NULL"
            )
            batch.create_index("ix_access_agents_node_id", ["node_id"])
    if "confirmed_at" not in _columns("capacity_reservations"):
        op.add_column(
            "capacity_reservations",
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    """Drop the columns added by this revision (tolerates schemas built from the models)."""
    insp = sa.inspect(op.get_bind())
    if "confirmed_at" in _columns("capacity_reservations"):
        op.drop_column("capacity_reservations", "confirmed_at")
    if "node_id" in _columns("access_agents"):
        for index in insp.get_indexes("access_agents"):
            if index["column_names"] == ["node_id"]:
                op.drop_index(index["name"], table_name="access_agents")
        # The FK constraint (whatever its generated name) goes with the column.
        with op.batch_alter_table("access_agents") as batch:
            batch.drop_column("node_id")
