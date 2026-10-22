"""Merge the cloud API's field set into the cloud tables.

Revision ID: 20260821_1600_merge_cloud_schema
Revises: 20260805_1000_baseline
Create Date: 2026-08-21 16:00:00.000000

``app/api/clouds.py`` and ``models_sqlalchemy``'s cloud tables described the
same two entities with two different, non-overlapping vocabularies. The API
wrote ``cloud_id/name/state/region/image/size/public_ips/private_ips/extra``
and ``config/status/enabled``; the tables defined
``external_id/hostname/status/zone/os_image/machine_type/...`` and
``config_data/is_active``. Nine of the eleven columns ``create_machine``
inserted did not exist, so every provisioning call raised CompileError
("Unconsumed column names") and the whole ``/api/v1/clouds/*`` surface
returned 500 the moment its feature flag was switched on.

This resolves the split by union, not by choosing a winner. Where the two
sides named the same concept, the table's existing column keeps its name and
the API is pointed at it (see the companion change to app/api/clouds.py) --
one column per concept, no synonym pairs. Where the API could express
something the table had nowhere to put, that becomes a real column:

    cloud_machines.public_ips   a machine routinely has several addresses;
    cloud_machines.private_ips  ip_address/private_ip hold only one each and
                                are kept as denormalised primaries
    cloud_providers.status      connection state from the last authenticate(),
                                distinct from is_active (the operator's switch)

Three Text columns holding structured values are retyped to JSON so dicts and
lists round-trip without every reader re-parsing: ``cloud_providers.config_data``
(handed to get_cloud_provider), ``cloud_machines.tags`` (licensing reads the
gough-managed marker out of it on every allowance count) and
``cloud_machines.metadata``.

No model column is dropped. ``architecture``/``cpu_count``/``memory_mb``/
``storage_gb`` and the ``lxd_cluster_id``/``fleet_host_id`` links have no
reader today but are the schema side of planned capacity and LXD/FleetDM
features; removing them to match the API would have deleted those quietly.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '20260821_1600_merge_cloud_schema'
down_revision = '20260805_1000_baseline'
branch_labels = None
depends_on = None


#: Text->JSON retypes, as (table, column) pairs.
_JSON_RETYPES = (
    ('cloud_providers', 'config_data'),
    ('cloud_machines', 'tags'),
    ('cloud_machines', 'metadata'),
)


def _json_type(dialect: str):
    """JSON column type for this backend, JSONB on PostgreSQL."""
    return postgresql.JSONB() if dialect == 'postgresql' else sa.JSON()


def _existing_columns(table: str) -> dict:
    """Map column name -> reflected column for ``table``."""
    inspector = sa.inspect(op.get_bind())
    return {c['name']: c for c in inspector.get_columns(table)}


def upgrade() -> None:
    """Add the merged columns, skipping any the baseline already created.

    The baseline revision builds the schema by calling ``create_all()`` on the
    live ORM metadata rather than by spelling out DDL, so a database created
    *after* this change already has these columns and a blind ``add_column``
    would fail with "duplicate column name". A database stamped at the baseline
    *before* it does not. Both are legitimate states of the same revision, so
    every step here checks first -- which also makes the migration re-runnable.
    """
    dialect = op.get_bind().dialect.name
    json_type = _json_type(dialect)

    provider_columns = _existing_columns('cloud_providers')
    if 'status' not in provider_columns:
        op.add_column(
            'cloud_providers',
            sa.Column('status', sa.String(50), nullable=True,
                      server_default='disconnected'),
        )

    machine_columns = _existing_columns('cloud_machines')
    for name in ('public_ips', 'private_ips'):
        if name not in machine_columns:
            op.add_column('cloud_machines', sa.Column(name, json_type, nullable=True))

    # SQLite has no real column types to alter -- SQLAlchemy's JSON serialises
    # to TEXT there either way, so the existing columns already hold the right
    # thing and an ALTER would only rewrite the table for nothing.
    if dialect == 'sqlite':
        return

    for table, column in _JSON_RETYPES:
        current = _existing_columns(table).get(column)
        if current is None or isinstance(current['type'], (sa.JSON, postgresql.JSONB)):
            continue
        if dialect == 'postgresql':
            # An empty string is not valid JSON; existing rows may hold one.
            op.execute(
                f'ALTER TABLE {table} '
                f'ALTER COLUMN "{column}" TYPE jsonb '
                f'USING NULLIF("{column}", \'\')::jsonb'
            )
        else:
            op.alter_column(
                table, column, existing_type=sa.Text(), type_=json_type,
                existing_nullable=True,
            )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect != 'sqlite':
        for table, column in _JSON_RETYPES:
            if column not in _existing_columns(table):
                continue
            if dialect == 'postgresql':
                op.execute(
                    f'ALTER TABLE {table} '
                    f'ALTER COLUMN "{column}" TYPE text '
                    f'USING "{column}"::text'
                )
            else:
                op.alter_column(
                    table, column, existing_type=_json_type(dialect),
                    type_=sa.Text(), existing_nullable=True,
                )

    machine_columns = _existing_columns('cloud_machines')
    for name in ('private_ips', 'public_ips'):
        if name in machine_columns:
            op.drop_column('cloud_machines', name)
    if 'status' in _existing_columns('cloud_providers'):
        op.drop_column('cloud_providers', 'status')
