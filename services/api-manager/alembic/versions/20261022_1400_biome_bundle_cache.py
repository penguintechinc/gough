"""Add the hub biome bundle cache: import ledger and verified file store.

Revision ID: 20261022_1400_biome_bundle_cache
Revises: 20261022_1300_rename_k8s_primary_biome
Create Date: 2026-10-22 14:00:00.000000

Additive only, guarded by reflection. A row is written only after the bundle's
sha256 (and cosign signature when configured) verified, so the tables never hold
unverified content.

- biome_bundle_imports: one row per pinned biomes release (unique version).
- biome_bundle_files: verified file contents served to instances, keyed by
  (import, biome, path). Content is stored in the database so the hub stays stateless.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261022_1400_biome_bundle_cache"
down_revision = "20261022_1300_rename_k8s_primary_biome"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the bundle import and file tables when absent."""
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())

    if "biome_bundle_imports" not in existing:
        op.create_table(
            "biome_bundle_imports",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("public_id", sa.String(36), nullable=False, unique=True),
            sa.Column("version", sa.String(64), nullable=False, unique=True),
            sa.Column("source_sha256", sa.String(64), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=False),
            # 0 = not required (no cosign key configured), 1 = cosign verified.
            sa.Column("signature_status", sa.Integer(), nullable=False),
            sa.Column("file_count", sa.Integer(), nullable=False),
            sa.Column("imported_by", sa.String(255), nullable=False),
            sa.Column("imported_at", sa.DateTime(), nullable=False),
        )

    if "biome_bundle_files" not in existing:
        op.create_table(
            "biome_bundle_files",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "import_id",
                sa.Integer(),
                sa.ForeignKey("biome_bundle_imports.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("biome", sa.String(64), nullable=False),
            sa.Column("path", sa.String(512), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("content", sa.LargeBinary(), nullable=False),
            sa.UniqueConstraint("import_id", "biome", "path", name="uq_bundle_file_path"),
        )


def downgrade() -> None:
    """Drop the bundle cache tables (file table first for the foreign key)."""
    op.drop_table("biome_bundle_files")
    op.drop_table("biome_bundle_imports")
