-- The store's five provenance tiers. A fact never changes tier, and no table mixes tiers:
--
--   native   exactly what a public service returned (ArcGIS REST, STAC). As published.
--   read     what OCR and the vision model read off document pages. Unvalidated until a check says so.
--   derived  anything this pipeline computed, recording its inputs.
--   agent    model-written argument. Never a source of numbers.
--   expert   what a geologist stated, recorded before any agent may cite it (PRD §8.3, B19).
--
-- Every fact table carries a `tier` column defaulted and CHECK-constrained to its schema, so a row can only
-- be inserted into the tier it belongs to. Cross-tier questions go through the views at the bottom, which
-- carry the tier column through rather than hiding it.

create schema if not exists native;
create schema if not exists read;
create schema if not exists derived;
create schema if not exists agent;
create schema if not exists expert;

-- ---------------------------------------------------------------- tier A: native

-- One row per pull. `payload_sha256` is the hash of the exact response body the service returned, so a later
-- pull that changes the data is visible rather than silent.
create table if not exists native.layer (
  layer_key       text primary key,
  title           text not null,
  service_url     text not null,
  layer_id        integer,
  where_clause    text,
  out_sr          integer,
  record_count    bigint not null,
  payload_sha256  text,
  licence         text not null,
  licence_url     text,
  redistributable boolean not null,
  retrieved_at    text not null,
  bears_on        text,                 -- the deposit-model element this layer speaks to
  role            text not null,        -- feature | label | context
  notes           text,
  tier            text not null default 'native' check (tier = 'native')
);

-- Features verbatim: geometry as WKB in the CRS it was requested in, attributes as the service's own JSON.
-- Typed access is through the views at the bottom, so nothing here is reshaped or renamed on the way in.
create table if not exists native.feature (
  layer_key  text not null,
  record_id  bigint not null,
  geom_wkb   blob,
  geom_type  text,
  epsg       integer,
  minx       double, miny double, maxx double, maxy double,
  attrs      json not null,
  tier       text not null default 'native' check (tier = 'native'),
  primary key (layer_key, record_id)
);

-- Raster provenance: one row per STAC asset actually read, so every per-cell statistic can name its scene.
create table if not exists native.scene (
  collection   text not null,
  stac_id      text not null,
  asset_key    text not null,
  href         text not null,
  datetime     text,
  cloud_cover  double,
  epsg         integer,
  gsd          double,
  licence      text not null,
  retrieved_at text not null,
  tier         text not null default 'native' check (tier = 'native'),
  primary key (collection, stac_id, asset_key)
);

-- One row per assessment file in the searchable corpus: what the province's own index says about it, and
-- where the holes it reported actually are. No document has been opened to write this row.
create table if not exists native.corpus_file (
  file_num         text primary key,
  company          text,
  property         text,
  work_period      text,
  nts              text,
  work_description text,
  lon              double,
  lat              double,
  n_holes          integer not null default 0,
  hole_names       text,
  retrieved_at     text not null,
  tier             text not null default 'native' check (tier = 'native')
);

-- ---------------------------------------------------------------- tier B: read

-- A page of a document, as text. This is tier B even though no model was asked to read it: the text layer of
-- a scanned report is usually somebody's OCR pass, and this project has already seen one turn 121.7 into
-- "L2L.-7". It is searchable evidence, not a measurement, and anything quoted from it says which page it
-- came from.
create table if not exists read.corpus_page (
  file_num     text not null,
  doc_name     text not null,
  doc_sha256   text not null,
  page         integer not null,
  chars        integer not null,
  text         text not null,
  extracted_at text not null,
  source       text,               -- text_layer | ocr: which reader the page text came from
  tier         text not null default 'read' check (tier = 'read'),
  primary key (doc_sha256, page)
);

-- ---------------------------------------------------------------- tier C: derived

-- The analysis grid. Cells are addressed by a stable id built from the grid definition, not by row order.
create table if not exists derived.grid (
  grid_id     text primary key,
  cell_m      integer not null,
  epsg        integer not null,
  buffer_m    integer not null,
  extent_wkt  text not null,
  n_cells     bigint not null,
  built_at    text not null,
  tier        text not null default 'derived' check (tier = 'derived')
);

create table if not exists derived.cell (
  cell_id  text not null,
  grid_id  text not null,
  col      integer not null,
  row      integer not null,
  cx       double not null,          -- centre, in the grid's own CRS
  cy       double not null,
  lon      double not null,          -- centre in WGS84, for the web export
  lat      double not null,
  geom_wkb blob not null,
  in_basin boolean not null,
  tier     text not null default 'derived' check (tier = 'derived'),
  primary key (cell_id)
);

-- One row per cell per feature, with the coverage that produced it. A null value with n_obs 0 means "we do
-- not know", never zero: the readiness scorecard reads this table directly.
create table if not exists derived.cell_feature (
  cell_id     text not null,
  feature_key text not null,
  value       double,
  value_text  text,
  unit        text,
  n_obs       integer not null default 0,
  nearest_m   double,
  from_tier   text not null,          -- which tier the inputs came from: native | read | derived
  op          text not null,
  tool        text not null,
  params      json,
  inputs      json,                   -- layer keys, scene ids or value ids behind this number
  computed_at text not null,
  tier        text not null default 'derived' check (tier = 'derived'),
  primary key (cell_id, feature_key)
);

create table if not exists derived.feature_spec (
  feature_key text primary key,
  title       text not null,
  unit        text,
  from_tier   text not null,
  source_keys json not null,
  bears_on    text,
  is_effort   boolean not null,       -- true: an exploration-effort feature, for the null model
  is_label    boolean not null,       -- true: never allowed into a feature set
  is_count    boolean not null default false,  -- true: zero is a real answer, so the feature is known everywhere
  notes       text,
  tier        text not null default 'derived' check (tier = 'derived')
);

-- One row per cell per model: the criteria score, the learned score, the exploration-effort null model.
-- `known_share` and `in_aoa` are how a cell says it should not be read: a score computed from a fraction of
-- the criteria, or one extrapolated outside the data the model was fitted on, is not the same number.
-- Labels, camps and folds. A cell is positive, or unlabelled: never barren, because nobody has drilled most
-- of this basin. `block_id` and `camp_id` are assigned from position alone, before any model is fitted.
create table if not exists derived.cell_label (
  cell_id     text not null,
  label_tier  text not null,          -- deposit | occurrence | unlabelled
  label_name  text,
  camp_id     bigint not null,        -- -1 where the cell is unlabelled
  block_id    bigint not null,
  computed_at text not null,
  tier        text not null default 'derived' check (tier = 'derived'),
  primary key (cell_id)
);

create table if not exists derived.cell_score (
  cell_id     text not null,
  model       text not null,          -- criteria | learned | effort
  score       double,                 -- null where the cell cannot honestly be scored
  known_share double,
  in_aoa      boolean not null default true,
  params      json,
  computed_at text not null,
  tier        text not null default 'derived' check (tier = 'derived'),
  primary key (cell_id, model)
);

-- Out-of-fold scores: each cell scored by the fold model that never saw it, under the headline corrections.
-- This is what a benchmark baseline is read from; `derived.cell_score` is fitted on every cell it scores and
-- would hand a benchmark its own answer key. `fold_kind` names the split (spatial), `fold` the held-out group.
create table if not exists derived.cell_score_oof (
  cell_id     text not null,
  model       text not null,          -- learned | effort | criteria
  fold_kind   text not null,          -- spatial
  fold        integer not null,
  score       double,                 -- null where no fold model could score the cell
  run_id      text not null,
  computed_at text not null,
  tier        text not null default 'derived' check (tier = 'derived'),
  primary key (cell_id, model, fold_kind)
);

-- What each criterion contributed to a cell's criteria score, so the number can be argued with rather than
-- just shown. Folklore criteria appear here with weight zero and a contribution of zero.
create table if not exists derived.cell_criterion (
  cell_id      text not null,
  criterion    text not null,
  membership   double,
  weight       double not null,
  contribution double,
  computed_at  text not null,
  tier         text not null default 'derived' check (tier = 'derived'),
  primary key (cell_id, criterion)
);

create table if not exists derived.metric (
  metric_key  text not null,
  run_id      text not null,
  value       double,
  fmt         text,
  note        text,
  computed_at text not null,
  tier        text not null default 'derived' check (tier = 'derived'),
  primary key (metric_key, run_id)
);

-- ---------------------------------------------------------------- tier D: agent

create table if not exists agent.memo (
  memo_id        text primary key,
  cell_id        text not null,
  role           text not null,       -- proponent | skeptic | adjudicator
  verdict        text,
  model          text not null,
  prompt_version text not null,
  run_id         text not null,
  cache_key      text,
  cost_usd       double,
  duration_s     double,
  created_at     text not null,
  published      boolean not null,    -- false: the fabrication gate rejected it
  tier           text not null default 'agent' check (tier = 'agent')
);

create table if not exists agent.memo_claim (
  memo_id   text not null,
  claim_no  integer not null,
  text      text not null,
  value_ids json not null,            -- every number in `text` must resolve to one of these
  tier      text not null default 'agent' check (tier = 'agent'),
  primary key (memo_id, claim_no)
);

create table if not exists agent.memo_check (
  memo_id text not null,
  check_id text not null,
  outcome text not null,              -- pass | fail
  detail  text,
  tier    text not null default 'agent' check (tier = 'agent'),
  primary key (memo_id, check_id)
);

-- ---------------------------------------------------------------- cross-tier views
--
-- The only sanctioned way to read across tiers. Each view keeps a tier column, so a consumer always knows
-- what it is holding.

create or replace view derived.v_feature_matrix as
  select c.cell_id, c.lon, c.lat, c.in_basin, f.feature_key, f.value, f.n_obs, f.from_tier, f.tier
  from derived.cell c left join derived.cell_feature f using (cell_id);

create or replace view native.v_layer_summary as
  select layer_key, title, role, record_count, licence, redistributable, retrieved_at, tier
  from native.layer;

-- Assay spreadsheets filed with modern digital submissions: as-published numbers, read by a parser with no
-- model, so they are tier A. One row per sample interval; the sheet table records what was and was not read.
create table if not exists native.assay_sheet (
  file_num     text not null,
  doc_sha256   text not null,
  doc_name     text not null,
  sheet        text not null,
  status       text not null,          -- ingested | no_header | unreadable | empty
  header_row   integer,
  n_rows       integer not null default 0,
  columns_json text,                   -- the header cells as printed, or the certificate's key-value block
  format       text,                   -- columns | certificate
  note         text,
  loaded_at    text not null,
  tier         text not null default 'native' check (tier = 'native'),
  primary key (doc_sha256, sheet)
);
create table if not exists native.assay_sheet_row (
  row_id           text primary key,   -- sha256 prefix of (doc_sha256, sheet, row_no)
  file_num         text not null,
  doc_sha256       text not null,
  doc_name         text not null,
  sheet            text not null,
  row_no           integer not null,   -- 0-based row in the sheet, header excluded
  hole_id          text,
  sample_id        text,
  from_m           double,
  to_m             double,
  interval_m       double,
  u3o8_pct         double,
  u3o8_as_printed  text,
  u3o8_column      text,
  u_ppm            double,
  u_ppm_as_printed text,
  u_ppm_column     text,
  below_detection  boolean not null default false,
  sample_type      text,               -- as printed: Basement, Standard, Blank ...
  kind             text not null,      -- interval | certificate
  loaded_at        text not null,
  tier             text not null default 'native' check (tier = 'native')
);

-- A conversation with the interface agent, persisted turn by turn. The transcript is benchmark data (PRD D):
-- every tool call, every value id cited and the gate's verdict are kept, so an answer can be re-scored later.
create table if not exists agent.conversation (
  conversation_id text primary key,
  cell_id         text not null,
  model           text not null,
  backend         text not null,
  created_at      text not null,
  requested_by    text,                -- the key label (never the key) that opened it; local with no register
  tier            text not null default 'agent' check (tier = 'agent')
);
create table if not exists agent.conversation_turn (
  turn_id          text primary key,   -- conversation_id:step
  conversation_id  text not null,
  step             integer not null,
  question         text not null,
  answer_text      text,               -- null when the gate refused it
  published        boolean not null,
  problems_json    text not null,      -- what the gate objected to, [] when it passed
  claims_json      text not null,      -- the claims with their value ids
  tool_calls_json  text not null,      -- tools called during this turn, with arguments
  values_json      text not null,      -- the cited values, so the turn is self-contained
  cost_usd         double,
  duration_s       double,
  created_at       text not null,
  requested_by     text,               -- who asked this turn (the key label, never the key)
  tier             text not null default 'agent' check (tier = 'agent')
);

-- Analyst chains (PRD §8.4, Stage 6): the staged analyst loop, as published. One row per chain, one per node
-- attempt, one per verifier round, one decision. A chain the gate refused is kept with `published` false as
-- the record of that refusal, never as argument. Nothing here is a source of numbers: a node cites values by
-- id, and the decision carries the cited values themselves (`values_json`, as `conversation_turn` does) so a
-- chain can be re-scored without the store it ran against.
create table if not exists agent.chain (
  chain_id          text primary key,
  cell_id           text not null,      -- the real cell
  bench_id          text,               -- the anonymised id, in benchmark runs
  purpose           text not null,      -- dashboard | scored | benchmark
  run_id            text not null,
  arm               text not null,
  fold              integer,
  planner           text not null,      -- template | model
  rounds            integer not null,   -- verifier rounds run
  valid             boolean not null,   -- true: a round validated the chain
  final_verdict     text not null,      -- evidence_against | insufficient | supports_closer_look
  final_probability double,
  weighted_score    double,             -- decider (a): the weighted sum over node strengths
  weights_version   text,               -- which out-of-fold fit the weights came from
  verifier_label    text,               -- the last verifier's candidate label: recorded, not acted on
  majority_label    text,               -- the vote over rounds, when K was exhausted
  abstained_reason  text,               -- why the chain publishes "insufficient" without a verdict of its own
  published         boolean not null,   -- false: the gate refused the chain as a whole
  models_json       text not null,      -- executor, verifier and adjudicator model ids
  manifest_sha256   text,               -- the run manifest this chain was published with
  blind_list_hash   text,               -- the retrieval blind-list in force (B17)
  cost_usd          double,
  duration_s        double,
  created_at        text not null,
  tier              text not null default 'agent' check (tier = 'agent')
);
create table if not exists agent.chain_node (
  chain_id        text not null,
  node_id         text not null,        -- n01, n02 ... in plan order
  round           integer not null,     -- the verifier round this attempt was executed in
  attempt         integer not null,     -- the mechanical gate's retry within the round
  segment_id      text not null,
  kind            text not null,        -- criterion | crosscheck | retrieval
  criterion       text,
  status          text not null,        -- met | not_met | unknown
  strength        integer not null,     -- 0..5
  value_ids_json  text not null,        -- the value ids the node rests on
  expert_ids_json text not null,        -- those of them that are expert-tier, labelled as such (B19)
  depends_on_json text not null,        -- the node ids this one builds on
  text            text not null,        -- the node's one sentence
  published       boolean not null,     -- true: passed the node gate
  problems_json   text not null,        -- what the node gate objected to, [] when it passed
  model           text not null,
  cost_usd        double,
  duration_s      double,
  created_at      text not null,
  tier            text not null default 'agent' check (tier = 'agent'),
  primary key (chain_id, node_id, round, attempt)
);
create table if not exists agent.chain_verdict (
  chain_id              text not null,
  round                 integer not null,
  valid                 boolean not null,
  faulty_json           text not null,  -- [{node_id, reason}]: the nodes sent back to Stage 2
  feedback              text,
  candidate_label       text,           -- the verifier's own label, for the per-stage agreement metric
  candidate_probability double,
  rationale             text,
  model                 text not null,
  cost_usd              double,
  duration_s            double,
  created_at            text not null,
  tier                  text not null default 'agent' check (tier = 'agent'),
  primary key (chain_id, round)
);
create table if not exists agent.chain_decision (
  chain_id         text primary key,
  adjudicator_json text not null,       -- the answer: verdict, probability, claims, unknown and absent criteria, next_observation, rationale
  claims_json      text not null,       -- the claims alone, with their value ids, for the digit scan
  values_json      text not null,       -- the cited values by id, so the chain is self-contained
  published        boolean not null,
  problems_json    text not null,
  model            text not null,
  cost_usd         double,
  duration_s       double,
  created_at       text not null,
  tier             text not null default 'agent' check (tier = 'agent')
);

-- Background jobs (PRD §A.2): anything over a second the API runs on its own worker pool, in this process,
-- because DuckDB allows one read-write connection per file and the serving process holds it. One row per job,
-- written as its state changes, so a restart shows every job's last state and marks the ones that were running
-- as failed with that reason. The first kind is `analyst`: the staged loop over one enabled cell (§9.4), whose
-- chain lands in agent.chain* exactly as `lr arm chain` publishes one. A job row is bookkeeping about a run,
-- never a source of numbers: the result names a chain id and the chain carries the values.
create table if not exists agent.job (
  job_id        text primary key,
  kind          text not null,         -- a registered job kind: analyst
  cell_id       text,                  -- the cell the job is about, when it is about one
  status        text not null,         -- queued | running | done | failed | cancelled
  requested_by  text not null,         -- the key label (never the key) that submitted it; local with no register
  args_json     text not null,         -- what was asked: arm, budget, reason, expert ids
  created_at    text not null,
  started_at    text,
  finished_at   text,
  progress_json text not null,         -- [{at, event, ...}]: the stages as they happen
  result_json   text,                  -- the kind's result; for analyst: chain_id, verdict, cost_usd, run_id
  error         text,                  -- why it failed or was cancelled; "process restarted" after a restart
  run_id        text,                  -- the run directory the job claimed, once it has one
  tier          text not null default 'agent' check (tier = 'agent')
);
create index if not exists job_cell_idx on agent.job (cell_id, created_at);

-- ---------------------------------------------------------------- tier E: expert

-- A geologist's own statement about a cell, recorded by the interface (`record_insight` over MCP, PRD §E.3)
-- with author, time and cell before any agent may cite it (§8.3). The numbers in the text are minted
-- expert-tier value ids by the server that recorded it, and the values themselves ride along so a chain that
-- cites one is self-contained; a node that leans on one is labelled expert-tier (B19). Never rewritten by a
-- model, never folded into a feature: it is the fifth tier of the §6 diagram, not a derived number and not an
-- agent's argument.
create table if not exists expert.insight (
  expert_id       text primary key,
  cell_id         text not null,
  author          text not null,
  text            text not null,
  value_ids_json  text not null,       -- the expert-tier value ids minted from the numbers in `text`
  values_json     text not null,       -- the values themselves, by id
  session_id      text,                -- the MCP session that recorded it
  run_id          text,                -- that session's run, whose manifest lists the insight
  principal       text,                -- the key label (never the key) the session was opened with
  recorded_at     text not null,
  tier            text not null default 'expert' check (tier = 'expert')
);
create index if not exists insight_cell_idx on expert.insight (cell_id, recorded_at);

-- ---------------------------------------------------------------- tier B, continued: agreement and review

-- The extractor's second-family agreement (PRD §8.2, stage 4). One row per value either reader found on a
-- compared page: agreed, disagreed, or found by one reader only, with both readings as JSON and the first
-- reading's `read.field_value` id when the store holds it. The values themselves are not touched: a
-- consumer that wants agreed values only joins here on `value_id`. Read tier because it is about readings,
-- and a reading is what it carries.
create table if not exists read.agreement (
  agreement_id   text primary key,     -- sha256 prefix of (run, page, field, the value id or the second reading)
  file_num       text not null,
  page           integer not null,
  page_id        text,
  field          text not null,
  field_type     text not null,        -- depth | grade | recovery | coordinate | angle | identifier | text | page_level
  status         text not null,        -- agreed | disagreed | only_a | only_b
  matched_by     text not null,        -- box | text | none: how the two readings were paired
  detail         text,
  value_id       text,                 -- the first reading's value id, when it was filed
  reading_a_json text,                 -- the first reading of this value (the reader model's), as the comparer saw it
  reading_b_json text,                 -- the second family's reading
  model_a        text,
  model_b        text,
  prompt_version text,
  run_id         text not null,
  compared_at    text not null,
  tier           text not null default 'read' check (tier = 'read')
);
create index if not exists agreement_page_idx on read.agreement (file_num, page, status);

-- The review queue (PRD §8.2, §A.2): every disagreement and every value only one reader found, open until a
-- person decides. The decision is recorded on the row and never rewrites a reading: `resolution_json` carries
-- the accepted reading (A's, B's, or the one the person typed) beside the two readings it chose between, and
-- `resolved_by` is the key label of whoever decided (never the key).
create table if not exists read.review_item (
  queue_id        text primary key,    -- sha256 prefix of (page, field, the value id or the second reading)
  file_num        text not null,
  page            integer not null,
  page_id         text,
  field           text not null,
  field_type      text,
  value_id        text,                -- the first reading's value id, when it was filed
  reading_a_json  text,
  reading_b_json  text,
  reason          text not null,       -- disagreed | only_a | only_b
  status          text not null default 'open',   -- open | accepted_a | accepted_b | rejected | edited
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
