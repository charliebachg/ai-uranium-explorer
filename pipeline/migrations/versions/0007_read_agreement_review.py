"""read.agreement and read.review_item: the extractor's second-family agreement and its review queue (PRD §8.2).

Revision ID: 0007
Revises: 0006
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
create table if not exists read.agreement (
  agreement_id   text primary key,
  file_num       text not null,
  page           integer not null,
  page_id        text,
  field          text not null,
  field_type     text not null,
  status         text not null,
  matched_by     text not null,
  detail         text,
  value_id       text,
  reading_a_json text,
  reading_b_json text,
  model_a        text,
  model_b        text,
  prompt_version text,
  run_id         text not null,
  compared_at    text not null,
  tier           text not null default 'read' check (tier = 'read')
);
create index if not exists agreement_page_idx on read.agreement (file_num, page, status);
create table if not exists read.review_item (
  queue_id        text primary key,
  file_num        text not null,
  page            integer not null,
  page_id         text,
  field           text not null,
  field_type      text,
  value_id        text,
  reading_a_json  text,
  reading_b_json  text,
  reason          text not null,
  status          text not null default 'open',
  run_id          text not null,
  model_a         text,
  model_b         text,
  created_at      text not null,
  resolved_by     text,
  resolved_at     text,
  resolution_json text,
  tier            text not null default 'read' check (tier = 'read')
);
create index if not exists review_item_open_idx on read.review_item (status, file_num, created_at);
""")


def downgrade() -> None:
    op.execute("drop table if exists read.review_item; drop table if exists read.agreement;")
