"""agent.chain, chain_node, chain_verdict, chain_decision: the analyst loop's published chains (PRD §8.4, Stage 6).

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
create table if not exists agent.chain (
  chain_id          text primary key,
  cell_id           text not null,
  bench_id          text,
  purpose           text not null,
  run_id            text not null,
  arm               text not null,
  fold              integer,
  planner           text not null,
  rounds            integer not null,
  valid             boolean not null,
  final_verdict     text not null,
  final_probability double precision,
  weighted_score    double precision,
  weights_version   text,
  verifier_label    text,
  majority_label    text,
  abstained_reason  text,
  published         boolean not null,
  models_json       text not null,
  manifest_sha256   text,
  blind_list_hash   text,
  cost_usd          double precision,
  duration_s        double precision,
  created_at        text not null,
  tier              text not null default 'agent' check (tier = 'agent')
);
create table if not exists agent.chain_node (
  chain_id        text not null,
  node_id         text not null,
  round           integer not null,
  attempt         integer not null,
  segment_id      text not null,
  kind            text not null,
  criterion       text,
  status          text not null,
  strength        integer not null,
  value_ids_json  text not null,
  expert_ids_json text not null,
  depends_on_json text not null,
  text            text not null,
  published       boolean not null,
  problems_json   text not null,
  model           text not null,
  cost_usd        double precision,
  duration_s      double precision,
  created_at      text not null,
  tier            text not null default 'agent' check (tier = 'agent'),
  primary key (chain_id, node_id, round, attempt)
);
create table if not exists agent.chain_verdict (
  chain_id              text not null,
  round                 integer not null,
  valid                 boolean not null,
  faulty_json           text not null,
  feedback              text,
  candidate_label       text,
  candidate_probability double precision,
  rationale             text,
  model                 text not null,
  cost_usd              double precision,
  duration_s            double precision,
  created_at            text not null,
  tier                  text not null default 'agent' check (tier = 'agent'),
  primary key (chain_id, round)
);
create table if not exists agent.chain_decision (
  chain_id         text primary key,
  adjudicator_json text not null,
  claims_json      text not null,
  values_json      text not null,
  published        boolean not null,
  problems_json    text not null,
  model            text not null,
  cost_usd         double precision,
  duration_s       double precision,
  created_at       text not null,
  tier             text not null default 'agent' check (tier = 'agent')
);
create index if not exists chain_cell_idx on agent.chain (cell_id, created_at);
create index if not exists chain_run_idx on agent.chain (run_id);
""")


def downgrade() -> None:
    op.execute("drop table if exists agent.chain_decision; drop table if exists agent.chain_verdict; "
               "drop table if exists agent.chain_node; drop table if exists agent.chain;")
