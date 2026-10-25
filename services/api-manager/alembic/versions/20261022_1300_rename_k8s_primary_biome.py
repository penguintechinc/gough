"""Rename the built-in biome k8s-primary to k8s-controller.

Revision ID: 20261022_1300_rename_k8s_primary_biome
Revises: 20261022_1200_lxd_join_issuance
Create Date: 2026-10-22 13:00:00.000000

Idempotent data migration. Rows named ``k8s-primary`` (and ``egg_kind``
values) become ``k8s-controller``. If a ``k8s-controller`` row already exists the
legacy row is left untouched and a warning is logged, so no operator data is
overwritten or deleted. Input aliasing (``app.catalog.LEGACY_BIOME_ALIASES``) keeps
accepting the old name for one release.
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "20261022_1300_rename_k8s_primary_biome"
down_revision = "20261022_1200_lxd_join_issuance"
branch_labels = None
depends_on = None

_LEGACY = "k8s-primary"
_CANONICAL = "k8s-controller"
_log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    """Rename legacy biome rows to k8s-controller when the target name is free."""
    bind = op.get_bind()
    biomes = sa.table(
        "biomes",
        sa.column("name", sa.String),
        sa.column("egg_kind", sa.String),
    )
    legacy_rows = bind.execute(
        sa.select(sa.func.count()).select_from(biomes).where(biomes.c.name == _LEGACY)
    ).scalar_one()
    if legacy_rows == 0:
        return

    canonical_rows = bind.execute(
        sa.select(sa.func.count())
        .select_from(biomes)
        .where(biomes.c.name == _CANONICAL)
    ).scalar_one()
    if canonical_rows:
        _log.warning(
            "biome rename skipped: both %r and %r rows exist; resolve manually",
            _LEGACY,
            _CANONICAL,
        )
        return

    op.execute(
        sa.update(biomes).where(biomes.c.name == _LEGACY).values(name=_CANONICAL)
    )
    op.execute(
        sa.update(biomes)
        .where(biomes.c.egg_kind == _LEGACY)
        .values(egg_kind=_CANONICAL)
    )


def downgrade() -> None:
    """Restore the legacy name on rows that still use the canonical name."""
    biomes = sa.table(
        "biomes",
        sa.column("name", sa.String),
        sa.column("egg_kind", sa.String),
    )
    op.execute(
        sa.update(biomes).where(biomes.c.name == _CANONICAL).values(name=_LEGACY)
    )
    op.execute(
        sa.update(biomes)
        .where(biomes.c.egg_kind == _CANONICAL)
        .values(egg_kind=_LEGACY)
    )
