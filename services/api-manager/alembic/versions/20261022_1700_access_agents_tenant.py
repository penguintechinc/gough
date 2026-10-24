"""Add access_agents.tenant_id (tenant of the enrolling key; NULL for legacy agents).

Revision ID: 20261022_1700_access_agents_tenant
Revises: 20261022_1600_agent_node_link_reservation_confirm
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261022_1700_access_agents_tenant"
down_revision = "20261022_1600_agent_node_link_reservation_confirm"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    """Return the existing column names of access_agents."""
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("access_agents")}


def upgrade() -> None:
    """Add the nullable tenant column and its index (legacy rows stay NULL)."""
    if "tenant_id" not in _columns():
        op.add_column("access_agents", sa.Column("tenant_id", sa.String(255), nullable=True))
        op.create_index("ix_access_agents_tenant_id", "access_agents", ["tenant_id"])


def downgrade() -> None:
    """Drop the tenant column and index."""
    if "tenant_id" in _columns():
        op.drop_index("ix_access_agents_tenant_id", table_name="access_agents")
        op.drop_column("access_agents", "tenant_id")
