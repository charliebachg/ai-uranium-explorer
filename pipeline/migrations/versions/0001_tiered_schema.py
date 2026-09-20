"""The tiered schema, translated from the DuckDB store's schema.sql, plus PostGIS and the cell geometry.

Revision ID: 0001
Revises: None
"""
from alembic import op

from uranium_explorer.store.pg import geometry_ddl, postgres_ddl, postgres_views

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(postgres_ddl())
    op.execute(geometry_ddl())
    op.execute(postgres_views())


def downgrade() -> None:
    op.execute("drop schema if exists agent cascade; drop schema if exists derived cascade; "
               "drop schema if exists read cascade; drop schema if exists native cascade;")
