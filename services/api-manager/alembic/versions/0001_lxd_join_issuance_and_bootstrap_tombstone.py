"""Add LXD join token issuance log and bootstrap token tombstone tables.

Idempotent: each table is created only when absent. The repo has no prior alembic
baseline and existing databases skip create_all (validation checks auth tables only),
so this migration is the only path that adds these tables to an existing database.

Revision ID: 0001_lxd_join
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_lxd_join"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create lxd_join_token_issuances and bootstrap_token_seeds when absent."""
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "lxd_join_token_issuances" not in existing:
        op.create_table(
            "lxd_join_token_issuances",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("agent_id", sa.String(255), nullable=False, index=True),
            sa.Column("member_name", sa.String(255), nullable=False, index=True),
            sa.Column("status", sa.Integer, nullable=False, server_default="0"),
            sa.Column("issued_at", sa.DateTime, nullable=False),
            sa.Column("expires_at", sa.DateTime, nullable=False),
        )
    if "bootstrap_token_seeds" not in existing:
        op.create_table(
            "bootstrap_token_seeds",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("seeded_at", sa.DateTime, nullable=False),
        )


def downgrade() -> None:
    """No-op: these tables hold issuance and tombstone evidence and are never dropped."""
