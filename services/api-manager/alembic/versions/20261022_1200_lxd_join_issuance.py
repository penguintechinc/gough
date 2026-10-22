"""Add LXD join issuance ledger, bootstrap token tombstones, and enrollment key revocation.

Revision ID: 20261022_1200_lxd_join_issuance
Revises: 20260821_1900_missing_cols
Create Date: 2026-10-22 12:00:00.000000

Additive only, guarded by reflection: the baseline builds the schema from live ORM
metadata, so a database created after these model changes already has them.

- lxd_join_token_issuances: one row per LXD join token grant. The agent_live, member_live
  and key_live columns are unique, so the database allows at most one live grant per
  agent, member, and key. A failed mint clears them (row status FAILED).
- bootstrap_token_seeds: tombstone per seeded bootstrap token hash. Never deleted.
  Backfilled here for every existing enrollment key whose metadata marks it as bootstrap.
- enrollment_keys.revoked: soft-delete flag. Revocation sets it instead of deleting the row.
"""

import ast
import json

import sqlalchemy as sa

from alembic import op

revision = "20261022_1200_lxd_join_issuance"
down_revision = "20260821_1900_missing_cols"
branch_labels = None
depends_on = None


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _metadata_purpose(raw: object) -> object:
    """Return the purpose recorded in enrollment key metadata (JSON or Python repr), else None."""
    if not isinstance(raw, str) or not raw:
        return None
    for loader in (json.loads, ast.literal_eval):
        try:
            value = loader(raw)
        except (ValueError, SyntaxError, TypeError):
            continue
        return value.get("purpose") if isinstance(value, dict) else None
    return None


def _backfill_bootstrap_tombstones() -> None:
    """Write a tombstone for every existing bootstrap-purpose key that has none."""
    bind = op.get_bind()
    keys = sa.table("enrollment_keys", sa.column("key_hash"), sa.column("metadata"))
    seeds = sa.table("bootstrap_token_seeds", sa.column("token_hash"), sa.column("seeded_at"))
    existing = {row[0] for row in bind.execute(sa.select(seeds.c.token_hash))}
    for key_hash, metadata in bind.execute(sa.select(keys.c.key_hash, keys.c.metadata)).fetchall():
        if _metadata_purpose(metadata) == "bootstrap" and key_hash not in existing:
            bind.execute(seeds.insert().values(token_hash=key_hash, seeded_at=sa.func.now()))


def upgrade() -> None:
    """Create the ledger and tombstone tables, add enrollment_keys.revoked, backfill tombstones."""
    tables = _tables()
    if "lxd_join_token_issuances" not in tables:
        op.create_table(
            "lxd_join_token_issuances",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("agent_id", sa.String(255), nullable=False, index=True),
            sa.Column("member_name", sa.String(255), nullable=False, index=True),
            sa.Column("key_hash", sa.String(255), nullable=False),
            sa.Column("status", sa.Integer, nullable=False, server_default="0"),
            sa.Column("issued_at", sa.DateTime, nullable=False),
            sa.Column("expires_at", sa.DateTime, nullable=False),
            sa.Column("agent_live", sa.String(255), nullable=True, unique=True),
            sa.Column("member_live", sa.String(255), nullable=True, unique=True),
            sa.Column("key_live", sa.String(255), nullable=True, unique=True),
        )
    if "bootstrap_token_seeds" not in tables:
        op.create_table(
            "bootstrap_token_seeds",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("seeded_at", sa.DateTime, nullable=False),
        )
    if "enrollment_keys" in tables and "revoked" not in _columns("enrollment_keys"):
        op.add_column(
            "enrollment_keys",
            sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.false()),
        )
    if "enrollment_keys" in tables:
        _backfill_bootstrap_tombstones()


def downgrade() -> None:
    """No-op: issuance and tombstone rows are audit evidence and are never dropped."""
