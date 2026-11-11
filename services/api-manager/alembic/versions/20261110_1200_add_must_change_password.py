"""Add auth_user.must_change_password (forced first-login password change).

Revision ID: 20261110_1200_must_change_pw
Revises: 20260821_1900_missing_cols
Create Date: 2026-11-10 12:00:00.000000

Security fix (HIGH): the api-manager no longer seeds a default admin password.
A generated one-time bootstrap password, or one an admin sets for another user,
flags the account ``must_change_password``; while set, every authenticated
route except change-password and logout answers 403 (app.middleware).

Additive, NOT NULL with a ``false`` server default so existing rows backfill
to "no change required" (their passwords were chosen before this flag
existed). Guarded by reflection like the previous revisions: a database built
by ``create_all_tables()`` after this change already has the column.
``seed_initial_admin`` refuses to start against a database missing it.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261110_1200_must_change_pw"
down_revision = "20260821_1900_missing_cols"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    """Return the live column names of ``table``."""
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    """Add the forced-password-change flag to auth_user."""
    if "must_change_password" not in _columns("auth_user"):
        op.add_column(
            "auth_user",
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    """Drop the forced-password-change flag."""
    if "must_change_password" in _columns("auth_user"):
        op.drop_column("auth_user", "must_change_password")
