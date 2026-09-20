"""expert.insight: the expert tier, a geologist's recorded statement (PRD §8.3, §E.3 `record_insight`, B19).

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
create schema if not exists expert;
create table if not exists expert.insight (
  expert_id       text primary key,
  cell_id         text not null,
  author          text not null,
  text            text not null,
  value_ids_json  text not null,
  values_json     text not null,
  session_id      text,
  run_id          text,
  principal       text,
  recorded_at     text not null,
  tier            text not null default 'expert' check (tier = 'expert')
);
create index if not exists insight_cell_idx on expert.insight (cell_id, recorded_at);
""")


def downgrade() -> None:
    op.execute("drop table if exists expert.insight; drop schema if exists expert;")
