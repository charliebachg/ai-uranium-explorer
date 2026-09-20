"""agent.job: background jobs with a durable row each (PRD §A.2), and who asked each conversation turn.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
create table if not exists agent.job (
  job_id        text primary key,
  kind          text not null,
  cell_id       text,
  status        text not null,
  requested_by  text not null,
  args_json     text not null,
  created_at    text not null,
  started_at    text,
  finished_at   text,
  progress_json text not null,
  result_json   text,
  error         text,
  run_id        text,
  tier          text not null default 'agent' check (tier = 'agent')
);
create index if not exists job_cell_idx on agent.job (cell_id, created_at);
alter table agent.conversation add column if not exists requested_by text;
alter table agent.conversation_turn add column if not exists requested_by text;
""")


def downgrade() -> None:
    op.execute("""
drop table if exists agent.job;
alter table agent.conversation_turn drop column if exists requested_by;
alter table agent.conversation drop column if exists requested_by;
""")
