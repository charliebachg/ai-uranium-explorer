"""derived.cell_score_oof: out-of-fold scores per cell and model, the benchmark baselines' table.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
create table if not exists derived.cell_score_oof (
  cell_id     text not null,
  model       text not null,
  fold_kind   text not null,
  fold        integer not null,
  score       double precision,
  run_id      text not null,
  computed_at text not null,
  tier        text not null default 'derived' check (tier = 'derived'),
  primary key (cell_id, model, fold_kind)
);
create index if not exists cell_score_oof_model_idx on derived.cell_score_oof (model, cell_id);
""")


def downgrade() -> None:
    op.execute("drop table if exists derived.cell_score_oof")
