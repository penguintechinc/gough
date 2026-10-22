"""Add the columns and table three endpoints were already writing to.

Revision ID: 20260821_1900_missing_cols
Revises: 20260821_1600_merge_cloud_schema
Create Date: 2026-08-21 19:00:00.000000

Same defect class as the cloud-table merge in the previous revision, found by
auditing every table that test fixtures hand-define with ``define_table()``
against the schema ``create_all_tables()`` actually builds. Where a fixture
invents a column the real table lacks, the tests pass and the endpoint 500s on
a real database -- the fixture is describing what the code assumes, not what
deployments have.

Two endpoints were dead for this reason (a third, POST /biomes/{id}/upgrade,
had a related but distinct fault -- upgrade_runs existed only in the alembic
path, not in the create_all() path init_db uses, and its app-supplied UUID
primary key was never populated; that is fixed by app.models_m1.UpgradeRun and
the id now passed in app/api/biomes.py, not by DDL here):

    biomes.biome_type       written by app/api/biomes.py:547
    biomes.lxd_image_url    written by app/api/biomes.py:555
                            -> every POST /api/v1/biomes raised
                            CompileError("Unconsumed column names")
    access_agents.enrolled_at
                            written by app/api/agents.py:277
                            -> every agent enrollment raised the same

Additive only: nothing is dropped or renamed. ``lxd_image_url`` sits alongside
the existing ``lxd_image_alias`` (an alias names an image in a remote, a URL
points at one directly -- create_biome writes both), and ``enrolled_at``
alongside ``enrollment_completed`` (a timestamp beside the boolean), so no
existing column's meaning changes.

Guarded by reflection like the previous revision: the baseline builds the
schema from live ORM metadata, so a database created after this change already
has these and a blind add_column would fail with "duplicate column".
"""

import sqlalchemy as sa

from alembic import op

revision = '20260821_1900_missing_cols'
down_revision = '20260821_1600_merge_cloud_schema'
branch_labels = None
depends_on = None


def _columns(table: str) -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    biome_columns = _columns('biomes')
    if 'lxd_image_url' not in biome_columns:
        op.add_column('biomes', sa.Column('lxd_image_url', sa.String(1024), nullable=True))
    if 'biome_type' not in biome_columns:
        op.add_column('biomes', sa.Column('biome_type', sa.String(64), nullable=True))

    if 'enrolled_at' not in _columns('access_agents'):
        op.add_column('access_agents', sa.Column('enrolled_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    if 'enrolled_at' in _columns('access_agents'):
        op.drop_column('access_agents', 'enrolled_at')

    biome_columns = _columns('biomes')
    if 'biome_type' in biome_columns:
        op.drop_column('biomes', 'biome_type')
    if 'lxd_image_url' in biome_columns:
        op.drop_column('biomes', 'lxd_image_url')
