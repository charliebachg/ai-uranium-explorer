# AI Uranium Explorer: the guide

This is the detailed explanation of the system: what each part is, what it does, how to run it, and where
its numbers come from. The README says the same things briefly and points here; the PRD carries the product
(who it is for, what it must do, what is deliberately out). Every command, flag, variable and page named
below exists in the code as it stands; where the code stops, the guide says so.

The repository has two halves. `pipeline/` is Python (managed with `uv`; the command is `uv run ue ...`
run from `pipeline/`), package `uranium_explorer`, environment prefix `UE_`, and its one store is the DuckDB
file `pipeline/data/ue.duckdb`. `web/` is a Vite, React and MapLibre app. `gold/` holds the held-out lock and
the gold pages when a person keys them.

---

## 1. What the system is

It reads public Saskatchewan uranium records, the provincial GIS layers and the scanned assessment reports
filed with the province, into a checked, traceable store; scores 2 km cells over the Athabasca Basin three
ways; and puts agents beside the record that can only speak from the same deterministic tools the scores were
computed from. The argument it makes is not "here is good ground". It is: here is how much of a
prospectivity score is explained by where people already drilled, and here is a way to let a model talk
about evidence without letting it invent a number.

Three rules run through everything:

- **Every number on screen resolves to a stored value with an id.** The web app prints stored numbers through
  one component, the agents may state a number only by citing its id, and a check that the id was returned by
  a tool in that session stands between every model and the page.
- **Unknown is not absent.** A feature nobody measured at a cell is null with an observation count of zero,
  never a low value; a layer that was never mapped around a cell makes an empty radius unknown, not empty.
- **Provenance never mixes.** What a public service published, what a model read off a page, what the pipeline
  computed, what a model argued and what a geologist stated live in five separate schemas, each checked.

**The acceptance story** is what the dashboard's guided tour walks through (section 3): a person opens the
dashboard, sees the data and how ready it is, reads the three scores against the null model they must beat,
inspects the model results on the Eval page and the analyst's stored chain on an enabled cell, talks to the
interface agent about that cell, and has it call the analyst as a background job whose verdict comes back
beside the stored chain.

**What it is not.** It is not a targeting tool. It makes no geological judgement, no geologist has reviewed
any of it, and every score on the map is a retrospective fit to public labels. The strongest verdict any agent
here can give is "supports a closer look". The Limits page says the rest.

---

## 2. Running it

### 2.1 With Docker

`docker-compose.yml` defines four services and one optional one:

| Service | What it is |
|---|---|
| `app` | One image: the built site and the API, on port 8787. `pipeline/data` is mounted from the host, never copied into the image. |
| `db` | Postgres 16 with PostGIS, the serving database (`ue store migrate`, `ue store sync-pg`). |
| `minio` | S3-compatible object store, where a seed pack can be pushed to and pulled from. |
| `redis` | Started for the serving stack; nothing in the pipeline reads it yet. |
| `jaeger` | Commented out: an OTLP trace backend for machines with room for one. |

The `Dockerfile` builds the web app with Node (`npm run build` with `VITE_SERVICE_ROOT` set to the empty
string, so the site calls the API on its own origin), installs the pipeline with `uv sync --frozen --no-dev`,
copies `src`, `knowledge`, `configs`, the Alembic files and the built site, and starts with:

    ue store seed ensure && ue prospect serve --host 0.0.0.0 --port 8787 --backend auto --web-dist /app/web/dist

`ue store seed ensure` is the container's first step: when `pipeline/data/ue.duckdb` is missing it unpacks
the pack at `UE_SEED_DIR` (the compose file points it at `/app/pipeline/data/seed/latest`, the mounted
`pipeline/data/seed/latest`), pulling one from `UE_SEED_URL` first when there is none on disk (the compose
file sets `s3://seeds/latest` on the compose MinIO); when a store is present it does nothing. Compose hands
the container the repository's `.env` and `pipeline/.env` as env files (both optional), which is where the model keys and their spend ceilings go
for the chat.

    docker compose up -d --build          # everything
    docker compose up -d db               # the serving database alone, for `ue store migrate` and `sync-pg`

**The seed pack.** The store is a gitignored DuckDB file nothing publishes. A pack is the same rows as one
Parquet file per table under `pipeline/data/seed/<hash>/`, named by the hash of its `manifest.json`, which
records the store's sha256, the store and pipeline versions, every table's row count and file hash, and the
licence decision that put each table in or left it out. `pipeline/data/seed/latest` is a symlink to the
newest pack.

| Command | What it does |
|---|---|
| `ue store seed pack [--private] [--out DIR]` | Write a pack. The public pack holds only tables whose every source the inventory marks redistributable; `--private` holds every table the store contract covers. |
| `ue store seed verify SRC` | Check every table's sha256 and row count against the manifest, and the manifest against its own address. Exit 1 on a mismatch. |
| `ue store seed unpack SRC [--into FILE]` | Rebuild a store from a pack: verify, apply `schema.sql`, load, audit the tiers, recount. Refuses an existing file. |
| `ue store seed push SRC --to s3://bucket/prefix` | Verify, upload under `<prefix>/<hash>/` with the manifest last, then point `<prefix>/manifest.json` at it, so a partial upload is never taken for a pack. |
| `ue store seed pull URL [--into DIR]` | `s3://` or `https://`: fetch the manifest, then every file with its sha256 checked; refuses on a mismatch and leaves nothing behind. |
| `ue store seed ensure` | The container's first step, described above. |

**What the public pack holds and what only the private one does.** The rule is derived from
`pipeline/knowledge/data_inventory.toml`, not from a list of table names: a table goes in the public pack
only when every source it carries is marked redistributable there. Sources are resolved from the rows
themselves (a `layer_key` column names pulled layers, a `collection` column names STAC collections, and the
derived tier's upstream is what `derived.feature_spec` and `derived.cell_feature` declare plus the label and
study-area layers). The layer registry `native.layer` is public (service URLs, counts, hashes and licence
flags, no payloads). A table keyed by assessment file (`file_num`, `doc_sha256`) holds material from
documents that carry no named licence and goes only in the private pack; so does every table in the `read`
tier (page text, readings, agreement marks, the review queue), the `agent` tier (memos, conversations,
chains, jobs) and the `expert` tier (a named geologist's statements). The `geods_holes` layer states no
licence, so its features stay private too. In practice the public pack rebuilds the derived tier, the layer
registry and the raster scenes; the read and agent tiers come back as empty tables, so there are no document
readings, no memos and no chains until the private pack is unpacked instead. The decision and its reason are
written per table into the manifest.

The S3 client reads the standard AWS names (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_DEFAULT_REGION`); when those are unset the `UE_S3_KEY` and `UE_S3_SECRET` pair (what the compose file
starts MinIO with) stands in, and `UE_S3_ENDPOINT` points the client at MinIO. Any S3 works with the
endpoint unset. Pushing to the compose MinIO from the host uses `UE_S3_ENDPOINT=http://127.0.0.1:9000`.

### 2.2 Without Docker

    cd pipeline && uv sync
    uv run ue doctor                                  # environment checks (claude CLI, poppler, Apple Vision, NTv2 grid)
    uv run ue store seed pull <url>                   # a fresh clone: the pack from wherever the team publishes it
    uv run ue store seed unpack data/seed/latest      # rebuild pipeline/data/ue.duckdb from it
    uv run ue prospect serve                          # the evidence record, the chat, the MCP route and the jobs, on 127.0.0.1:8787
    cd ../web && npm install && npm run dev           # http://localhost:5173

The map, the scores, the layers, the Data, Eval and Limits pages and the guided tour are static files under
`web/public/data` and work with nothing running. Only the evidence panel, the chat, the jobs strip and the
Review page need `ue prospect serve`; when it is down each says so and names the command rather than
erroring. Without a seed pack the store is built from the public services with the pipeline commands in
section 12, in the order the data section describes.

`ue prospect serve` picks the chat's backend with `--backend`: `auto` (the default, API first: a
`vendor/model` id goes to OpenRouter, a bare `claude-*` id to OpenRouter's Anthropic listing, a `gpt-*` id
to OpenAI, each adapter built on first use so the service starts with no key and only a call names the
key it lacks), `openai` (the OpenAI adapter alone) or `claude` (the local Claude CLI, a developer's option
that needs a login; never chosen by `auto`). `--model` overrides the model, `--effort` the reasoning
effort, `--host 0.0.0.0` opens it inside a container, `--web-dist web/dist` serves the built site from the
same process. Check `uv run ue openai models` (free) before choosing an OpenAI model and `uv run ue openai
budget` or `uv run ue spend show` for what has been spent.

The published site can also be built public-safe: `npm run build:public` builds with `VITE_PUBLIC_SAFE=1`,
which hides the chat and serves no report text.
non-redistributable data and page images.

### 2.3 Environment variables

Every `UE_*` variable the code reads, with its default and meaning. None of their values is ever logged.

| Variable | Default | Meaning |
|---|---|---|
| `UE_ROOT` | the repository root, found from the package | Overrides where `pipeline/`, `pipeline/data`, `gold/` and `web/public/data` are looked for. The Docker image sets it to `/app`. |
| `UE_STORE_RW` | unset | `1` puts the process in one-connection mode: every store open is read-write, because DuckDB refuses to open one file read-only and read-write at once from one process. `ue prospect serve`, `ue arm chain` and `ue prospect record --job` set it for themselves; batch commands leave it unset and keep their read-only handles. |
| `UE_MAX_SPEND_USD` | 300 | The cumulative ceiling across every backend family, checked before each live call against the on-disk ledger `pipeline/data/spend.jsonl`. |
| `UE_INTERFACE_MODEL` | `z-ai/glm-5.3-flash` | The interface agent's model under `--backend auto` (the chat, `ue interface ask`, `ue prospect record`). |
| `UE_MCP_KEYS` | unset (no register) | The key register shared by the MCP server and the API: `<key>:<scope>[,<scope>];<key>:...`. Unset, local clients hold every scope. |
| `UE_MCP_KEY` | unset | The key a stdio MCP client's process presents when a register is configured. |
| `UE_MCP_PUBLIC` | unset | `1` turns the MCP server's public-safe build on (the same as `ue mcp serve --public-safe`). |
| `UE_JOB_SESSION_BUDGET_USD` | 5 | The job runner's ceiling across every job in one process; a submission that would cross it is refused. |
| `UE_JOB_WORKERS` | 2 | Threads in the job runner's pool. |
| `UE_ANALYST_JOB_BUDGET_USD` | 0.50 | What one analyst invocation from the chat may spend. |
| `UE_ANALYST_SESSION_BUDGET_USD` | 2.00 | What one conversation may commit over all its analyst invocations. |
| `UE_BENCH_KNOWLEDGE_DIR` | `pipeline/knowledge/bench/` | Where the benchmark dataset lives (section 10); the default is a symlink on the machine that builds it and absent in a clone. |
| `UE_MLFLOW_URI` | `sqlite:///pipeline/data/mlflow.db` | The MLflow tracking store, which also holds the model registry. |
| `UE_TRACING_MLFLOW` | `1` | `0` turns the MLflow mirror of every span off. |
| `UE_OTLP_ENDPOINT` | unset | An OTLP/HTTP endpoint every span is also exported to; a bare host gets `/v1/traces` appended. Unset, the OpenTelemetry exporter is not imported. |
| `UE_OTLP_HEADERS` | unset | `name=value[,name=value]`: headers for a hosted trace backend. |
| `UE_PG_DSN` | `postgresql://ue:ue@127.0.0.1:5432/ue` | The serving database; `ue store migrate`, `sync-pg` and `pg-audit`, and the API's candidate list when it is reachable. |
| `UE_SEED_DIR` | `pipeline/data/seed/latest` | The pack `ue store seed ensure` unpacks. |
| `UE_SEED_URL` | unset | Where `ensure` pulls a pack from when there is no store and no pack on disk. |
| `UE_S3_ENDPOINT` | unset (AWS) | The S3 endpoint for push, pull and ensure. |
| `UE_S3_KEY`, `UE_S3_SECRET` | unset | S3 credentials used when the AWS names are unset. |

Two are read only by the tests: `UE_REAL_DATA=1` enables the tests marked `real_data` (they read the live
store), and `UE_PG_TEST=1` enables the Postgres test against the compose stack. `UE_PG_USER`,
`UE_PG_PASSWORD`, `UE_PG_DB`, `UE_PG_PORT` and `UE_REDIS_URL` are compose substitutions and example-file
entries; no Python reads them.

The model keys are read by name only: `OPENAI_API_KEY` for the OpenAI backend and `OPENROUTER_API_KEY` for
OpenRouter. Beside them the OpenAI adapter reads `OPENAI_MODEL` (default `gpt-5-mini`),
`OPENAI_MAX_SPEND_USD` (2.00, that family's own ceiling), `OPENAI_PRICE_IN_PER_MTOK` and
`OPENAI_PRICE_OUT_PER_MTOK` (0.25 and 2.00; the ledger's dollars for this family are arithmetic over these,
because a price the code guessed would be a fabricated number), `OPENAI_MAX_OUTPUT_TOKENS` (1400) and
`OPENAI_VISION_MODELS`: the adapter sends page images only to models it knows take them (`gpt-5*`,
`gpt-4o*`, `gpt-4.1*`, `o1`, `o3`, `o4`), this comma-separated list adds others, and a request carrying an
image for any other model is refused before it is sent. The OpenRouter adapter reads
`OPENROUTER_MAX_SPEND_USD` (25.00) and `OPENROUTER_MAX_OUTPUT_TOKENS` (0: the reasoning budget for the
effort plus the answer room); it takes the call's cost from the provider's own usage block, with the model's
published prices as the fallback.

The pipeline finds these in the nearest `.env` walking up from the working directory, so from `pipeline/`
either `pipeline/.env` or a `.env` at the repository root is read, without overwriting anything already set.
Two example files are checked in: `.env.example` at the root (the model keys and their settings) and
`pipeline/.env.example` (the serving stack, the seed transport and the OTLP endpoint). Both `.env` files are
gitignored and never copied into the image.

### 2.4 Keys and roles

**One register.** `UE_MCP_KEYS` is the key register for the MCP server and the API alike. A key is any
string without `:`, `;`, `,` or white space; `uv run ue mcp key --scopes read,record` mints a random one and
prints the register entry. A key never appears in a log, a span, a manifest or an error: a principal is named
by `key:` and the first eight hex characters of the key's sha256.

The MCP server speaks in three scopes and the API in three roles, each role a set of scopes:

| Role | Scopes | What it opens |
|---|---|---|
| `viewer` | `read` | the evidence record, the stored conversations, a job's row |
| `geologist` | `read`, `record` | the chat and the review decisions; every turn records who asked |
| `admin` | `read`, `record`, `run` | submitting and cancelling an `analyst` job |

A role is all of its scopes or none of it: a key minted with `run` but not `record` holds no role above
viewer. A caller presents the key as `Authorization: Bearer <key>` or `X-Api-Key: <key>` over HTTP; a stdio
MCP client puts it in `UE_MCP_KEY`. `GET /api/whoami` says what a key holds.

**The local rule.** With no register configured, an HTTP client on the machine's own network (loopback, or a private address such as the Docker gateway the container sees a browser through) and a stdio or in-process client are
the `local` principal with every scope and every role, and a client from any other address gets nothing. So a
local prototype needs no key at all, and the site's Key button (section 3) is only needed when the service has
a register. A refusal is a 401 (nobody could be resolved) or a 403 (a principal without the role) with a plain
reason.

---

## 3. The dashboard

### 3.1 Pages

The app has five views, all under the same top bar: **Map** (`/`), **Data** (`/data`), **Eval** (`/eval`),
**Limits** (`/limits`) and **Review** (`/review`). The top bar carries the coverage counter (the
`m:files_read` and `m:uranium_files` stats from the manifest, read "of ... uranium-tagged files read"; at
the current export, 22 of 5,822), the view links, **Search**, **Tour** and **Key**.

**The map.** The layer rail on the left is grouped the way MineTRACE groups evidence, with one deliberate
addition. At the top, *Scores* holds the *Prospect scores* layer and a four-way picker: **Criteria**,
**Learned**, **Effort** and **Learned − effort**. Then *Pathway and trap* (EM conductors; faults and
lineaments; graphitic or pelitic host), *Geochemistry* (lake sediment samples; lake water samples;
radioactive boulders), *Where people already looked* (airborne and ground survey footprints, GeoDS
drillholes, compilation collars; the group exists because separating the rock from the exploration history is
the argument this system makes), *Known uranium* (uranium deposit footprints; uranium occurrences) and *Base*
(the Athabasca Basin outline, NTS map sheets, relief shading). *Geophysics* is a group with no layers:
magnetics, gravity and radiometrics are listed struck through with "no public grid for Saskatchewan", because
a gap you can see beats a smooth map that quietly omits it. The footer holds the basemap picker and the theme
toggle. Each evidence layer is fetched the first time it is switched on. A cell the chosen model could not
score is drawn as an outline with no fill, a hole in the picture rather than a low score. Clicking a cell
opens it in the agent rail; hovering names whatever is under the cursor. The score legend sits bottom-left
whenever the cells are on. The datum tools (`D`, `M`) and the timeline (`T`) are side exhibits from the
reading pipeline: the NAD27 to NAD83 shift and where collars would land if it were skipped, and where people
drilled by year.

**Data** (`/data`) is the readiness scorecard: where the observations are (one dot per grid cell, from the
exported cell file, a coverage map and not a prospectivity map), the totals (sources, verified features,
redistributable sources, recorded gaps), coverage by feature with the thin ones named, the sources with their
licences and verification dates, the five-column readiness gate as `ue prospect gate` last scored it, and what
is missing.

**Eval** (`/eval`) opens on the reading pipeline's run statistics and says at the top that they are run
statistics, not accuracy, because no gold page has been keyed: what was read, values with a quote on the page,
the second reader (OCR), what the checks flagged, where the holes came from, the cross-check against
provincial records, per file, what the run cost, and what would turn these into accuracy. Below sit the score
models: what the map's scores are worth (the fold metrics), the re-test, the model search with its registry
decision and model card, the dated hindcast, the analyst benchmark with the staged loop's per-stage columns,
and the fabrication gate put to the test. Section 6.7 says where each table's numbers come from.

**Limits** (`/limits`) is one row per claim (a grade or log means something; the map ranks ground; drill here;
an LLM judge checked it; anything about a company's own ground): what public data can support, what it cannot,
and what each would take, each line cited to its source.

**Review** (`/review`) is the extractor's review queue (section 5.3).

### 3.2 The evidence panel with chains

The evidence tab of the agent rail holds everything the local service knows about one cell, read through the
same four tool calls the chat opens with: the three scores with their known share and applicability; the
criteria breakdown, where a criterion that is unknown here is drawn with no bar and named as not measured,
while one that was measured and fell short gets a bar and a number; the nearest labelled deposits and
occurrences; the memos the three-role panel wrote about the cell, a rejected memo shown with its verdict
struck out and its claims withheld; and the analyst chains stored for the cell, newest first (the record
carries up to five), with a picker between them.

A chain is shown as the staged analyst produced it: one node per criterion and cross-check with its status in
words (met, not met, unknown), its strength, the value ids it cites (an expert-tier id labelled as such), and
its one sentence; each verifier round with what it faulted; and the decision with its verdict, probability,
claims, the criteria it calls unknown and absent, and the one observation that would change it. A node or a
decision that failed its gate is marked withheld with the objection beside it; a chain that never validated
says why it abstained. A verdict is words in the page's plain ink, never a colour.

### 3.3 The agent rail: the chat and the jobs strip

The rail opens on a selected cell. Its header shows the cell id and its centre; under it the three scores
from the exported row, so the rail still says something with the service down. Between the scores and the
tabs sits the **jobs strip**: the cell's background jobs, each a status in words (queued, running, done,
failed, cancelled), the stage a running one has reached, a finished one's chain id, verdict and cost, a
failed one's reason; **Run analyst** submits an `analyst` job on an enabled cell (any other cell is refused
with the reason), and when it finishes the evidence record is re-read so the chain appears below. The strip
polls a job's row only while something is queued or running.

The **chat tab** is a conversation bound to that cell; selecting another cell starts a fresh one. Question on
the right, answer on the left, the composer pinned at the bottom, with three suggested questions. Under each
answer: the route line (the kind the question was read as and the tools its plan fetched), the tool calls as
they happened, the cited values as chips (each a stored value with an id, printed through the same component as
every other number on the site), and what the turn cost. An answer that fails the check is kept in the transcript with its text withheld and the
checker's objection in its place. A refusal shows its reason and its abstain id; an insight shows the expert
id it was recorded under; an invoked analyst is a card that polls its job and prints the stages as words, and
the turn that finds it finished carries the verdict and the diff against the cell's stored chain. The chat
streams the turn's events (`opened`, `route`, `thinking`, `delta`, `reasoning`, `tool`, `tool_error`,
`checking`, `refused`, `abstain`, `insight`, `job`, `done`, `error`) so the panel shows the work rather than a
spinner.
Earlier conversations on the cell are listed from the store and can be resumed.

### 3.4 The key dialog

The **Key** button in the top bar opens a dialog for the API key, needed only when the service has a key
register. The key is kept in this browser's `localStorage` and sent as `X-Api-Key` on every call the typed
client makes; before it is kept it is checked against `GET /api/whoami`, and the roles it holds are shown in
words. With no register the dialog says the service answers a local page as `local` with every role.

### 3.5 The tour

**Tour** (or `G`) starts the ten-step walkthrough. Each step sets up its own screen, carries the line to say
out loud and a note on what is happening, and has a time budget the HUD counts against without ever
advancing on its own (about eight minutes in all): *title* (the opening statement), *data* (the Data page and
the readiness gate), *map* (the evidence layers on, the enabled cell open in the rail), *scores* (the cells
coloured by the criteria score; the banner changes and the legend appears), *nullmodel* (learned minus
effort), *eval* (the model search, the hindcast and the analyst benchmark), *chains* (the evidence tab on the
enabled cell with its stored chain; needs the local service, otherwise the offline notice), *ask* (the
recorded conversation replayed up to the question the system must refuse), *analyst* (the recorded job card
with its stages, verdict and cost, and the follow-up turn's diff) and *limits*. `ArrowRight` and `ArrowLeft`
move between steps, `Escape` leaves.

**The recorded session.** The agent exchange is prerecorded, so the room never waits on a model call, and
the panel labels it as recorded. `ue prospect record` (section 7.3) asks the tour's five questions of the
interface agent for real in one conversation on one enabled cell: what is measured and what only assumed; why
the criteria score is what it is and whether the learned score is explained by the drilling history; which
single unmeasured criterion would move the score most; what grade a hole would intersect (declined out of
scope, with its reason and id); and "run the analyst", which submits a job through the same runner the API
installs. The recording waits for the job in-process, keeps the runner's row as it ended on the job card,
and asks one follow-up so the transcript carries the verdict and the chain diff. In the current recording the
follow-up turn's own answer declined with `not_measured`, because the agent's answer call does not yet see
the finished job it is handed; the card on that turn carries the verdict and the diff regardless, and the
tour says out loud whether the verifier published or withheld the job's chain. A recording never writes an
insight: its conversation has no insight store.

### 3.6 Keyboard shortcuts and URL state

| Key | Does |
|---|---|
| `G` | start or leave the tour |
| `⌘K` / `Ctrl-K` | the command palette: views, reports, holes, layers, basemaps and tools; highlighting a report or hole peeks at it on the map and leaving restores the camera |
| `T` | the timeline: one bar per year by data source, a scrubber that sets the latest year drawn, and playback |
| `D` | the cursor datum lens |
| `M` | where NAD27 collars would land if read as NAD83 |
| `Esc` | one step back along the evidence trail (value, hole, report, selection) |

The URL carries the camera (`c`), basemap (`b`), theme (`t=light`), the visible layers (`L`), the
uranium-only filter (`u=1`), the selected collar (`s`), and the open report, hole, value and page (`r`, `h`,
`v`, `p`). A plain visit to `/` runs the intro flight; any query parameter (a deep link) skips it, and so does
a reduced-motion setting; `?intro=1` forces it and `?intro=0` suppresses it. `?motion=0` turns every animation
off, `?perf=lite` drops the backdrop blur, `?fixture=1` shows the evidence panel on a hand-keyed development
fixture (the app shows a FIXTURE badge whenever it is used), and `?spike` opens the spike harness.

### 3.7 The colour rule

Colour shows extraction status or data source, never prospectivity. The score cells are the one layer allowed
to colour by a value, and they carry their own legend. This is enforced in code: the layer declares itself
with `metadata["ue:score"]`, and `honestyViolations` in `web/src/map/style/composeStyle.ts` refuses both an
undeclared layer whose colour is driven by a computed field and a declared layer that colours by anything but
the score fields; `tests/unit/composeStyle.test.ts` runs it over the composed style. Source and status hues
(compilation, GeoDS, pass, flag, miss) never change with the theme; the score ramp flips between the dark and
light themes so that a high score is always more ink. App copy is scanned by `tests/unit/wording.test.ts` for
phrases that must never appear.

### 3.8 The value-id contract and the digit walk

`web/src/data/contract.ts` is the schema for everything under `web/public/data`, checked by `npm run
validate:data` before the app loads any of it. Its first rule: no bare numbers. Every numeric value shown to a
person is a value id pointing at a `Val` record, except geometry coordinates, page boxes, array indices and the
datum-grid arrays. Its second: the browser does no evidential maths; offsets, shifts, misread positions,
feet-to-metre geometry, counts and metrics all arrive from the pipeline as values. Ids are namespaced by
kind: `x` extracted, `d` derived, `p` provincial lithology, `s` bulk source property, `m` manifest stat, `e`
eval metric, `c` cell and coverage value, `g` datum grid node, `h` histogram bin. An extracted value carries
its lineage (file hash, page, box, verbatim quote, whether the quote was located, model, prompt version, run,
validator outcomes); a derived value carries its derivation (operation, inputs, tool); a source value its
source record.

`<V id>` is the only way the app prints a stored number. It marks the element with `data-vid`, and fails
loudly for an unknown id: it throws in development and renders an UNBACKED chip in a build, counted on
`window.__ue.unbacked`. **The digit walk** is the end-to-end check that closes the loop: the Playwright specs
(the tour, the Eval page, the evidence panel, the HUD) walk every text node inside a `[data-strict]` region,
and any digit that is not inside an element marked `data-vid` (a stored value), `data-ident` (an identifier
such as a cell id or hole name), `data-instrument` (a readout such as the cursor position), `data-axis`,
`data-chrome` (a keyboard hint) or `data-source-text` fails the test. The tour's lines are digit-free apart
from `{value-id}` tokens the HUD resolves through the registry.

---

## 4. Data

### 4.1 Sources and licences: the inventory

`pipeline/knowledge/data_inventory.toml` is the register of every source the project may use, validated on
load. Each source carries its tier, its role (`feature`, `label` or `context`; a label may never enter a
feature set), what it bears on in the mineral-systems vocabulary the research supports for this basin
(pathway, trap, detection, dispersal, cover, effort, label, study_area), how it is accessed (ArcGIS REST,
STAC, a file, internal), its licence, whether it was confirmed by a live call and when, and its caveats. The
licences: the Government of Saskatchewan Standard Unrestricted Use Data Licence v2.0 for every provincial
layer (commercial use with attribution, redistributable); the Copernicus Sentinel Data Licence for Sentinel-2
and the Copernicus DEM; USGS public domain for Landsat; the Open Government Licence - Canada for the NTS grid
and the national aeromagnetic compilation; and "no licence stated" for the GeoDS drilling layer and for the
assessment reports themselves, which are used locally and never redistributed. The inventory also records the
gaps so nothing pretends otherwise: the airborne magnetic, radiometric and gravity grids (magnetics is
published nationally under the open licence and not yet pulled; radiometrics and gravity have no public value
grid), discovery dates (not published), conductance on the EM conductor layer (unverified) and alteration
measurements at depth (not public). `ue prospect inventory` prints it; the Data page and the MCP resource
`ue://reading/inventory` serve it.

The provincial layers are pulled by `ue index pull` (paced, cached, single-clause queries into
`pipeline/data/index`): the drillhole compilation, the GeoDS drilling layer, the uranium file index, the
assessment-file information layers, the basin outline, the deposit footprints, the uranium-primary mineral
deposit index, the EM conductors, the 1:250,000 faults, bedrock and surficial geology, the survey footprints,
the two lake-sediment surveys, lake water, the radioactive boulders and the NTS sheets. The rasters (Sentinel-2
L2A and the Copernicus DEM) are read straight from cloud-optimised GeoTIFFs through STAC by `ue prospect
rasters`, at a decimated resolution through the files' own overviews, with every scene recorded in
`native.scene`.

### 4.2 The tiered store

The store is one DuckDB file with five schemas, one provenance tier each (`pipeline/src/uranium_explorer/store/schema.sql`):

| Tier | What lives there |
|---|---|
| `native` | Exactly what a public service returned: the layer registry with each pull's payload hash and licence flag, the features verbatim, the raster scenes, the corpus file index, the assay spreadsheets filed with digital submissions. Trusted as far as the publisher is. |
| `read` | What OCR and the vision model read off document pages: page text, the readings, the agreement marks and the review queue. Unvalidated until a check says otherwise; never a feature or a label. |
| `derived` | What the pipeline computed, recording its inputs: the grid and cells, the features with their observation counts, the feature specs, the labels with camps and folds, the three scores, the out-of-fold scores, the criteria contributions, the metrics. |
| `agent` | What a model argued: the panel memos with their claims and checks, the chat conversations turn by turn, the analyst chains with their nodes, verdicts and decisions, and the job rows. Never a source of numbers. |
| `expert` | What a geologist stated, recorded with author, time and cell before any agent may cite it; the numbers in it carry expert-tier ids. |

Every table carries a `tier` column defaulted and CHECK-constrained to its schema, `insert_frame` and
`append_frame` refuse a frame whose tier does not match, and `ue store audit` (and the test suite) runs the
tier audit: every table in a tiered schema must carry a tier column holding only that schema's tier. Cross-tier
questions go through views that keep the tier column. **Migrations**: `schema.sql` is applied on every
read-write open, and columns added after a table first shipped are added with `alter table ... add column if
not exists` from a fixed list in `store/__init__.py` (DuckDB cannot add a constrained column to a live table).
The serving database is the same schema in Postgres with PostGIS, derived by a mechanical translation and
applied by Alembic (`ue store migrate`, migrations `0001` to `0007`), filled by `ue store sync-pg` and audited
by `ue store pg-audit`. `ue store rebuild` flattens the reading pipeline's stage outputs into the store from
scratch every time.

### 4.3 The grid and the features

The grid (`ue prospect grid`) is the Athabasca Supergroup outline buffered outward by 30 km, cut into 2 km
square cells in the province's own projected system, NAD83 UTM zone 13N (EPSG:2957), so a distance in a
feature is a distance on the ground: 30,534 cells, 18,898 of them over the sandstone, 122,136 km². A cell is a
search area, never a target; a deposit is far smaller than any cell.

`ue prospect features` and `ue prospect rasters` compute 25 features per cell, each stored in
`derived.cell_feature` with the count of observations behind it, the nearest observation's distance where
there is one, and the layers or scenes it was built from. Nineteen are geological: distance to the nearest EM
conductor and conductor density; a graphitic or pelitic host under the cover rule and the same map read at the
surface; distance to the nearest fault or lineament and fault density; the interpolated unconformity depth
(inverse-distance from holes that reached the contact, null beyond 25 km of the nearest); the dominant
surficial class; the highest lake-sediment uranium within 5 km from each of the two surveys and the highest
lake-water uranium; the highest radioactive-boulder count rate; and from the rasters the water, vegetation
and bare-ground fractions, elevation, relief, landform grain and grain coherence. Six are exploration-effort
features: collars within 2 km, the earliest drilling year within 5 km, lake-sediment and boulder records
within 5 km, and airborne and ground survey footprints covering the cell. These describe where people looked,
not what is in the rock; they are marked `is_effort` and are the null model's whole world. Features are
computed from `native`-tier data only, and a builder that reads a label layer is refused
(`test_feature_leakage`).

Three rules the builders enforce:

- **Zero is not a reading.** A statistic over points needs at least one observation and stays null without
  one; only a count answers everywhere (no samples within 5 km is zero, not unknown). The radioactive-boulder
  layer carries thousands of records at exactly 0 cps, a filled-in blank, so a value of exactly zero is read
  as a missing measurement for the count-rate feature while the record still counts for the effort feature.
- **The cover rule for the graphitic host.** Inside the basin outline the 1:250,000 map shows the Athabasca
  sandstone, which is the cover, not the basement the criterion asks about, so a non-host polygon there says
  nothing: `graphitic_host` is 1 where the map names a host, 0 where it is mapped as something else outside
  the basin, and null where it is unmapped or under the cover. The criteria score and the analyst read this
  one. `graphitic_host_surface` is the same map read as a statement about the surface (0 under the cover is
  true of the surface), kept under an honest name for the learned model, which fits on complete cases and
  would otherwise lose every covered cell.
- **Layer footprints.** A layer's footprint is the ground it says anything about: the union of 5 km halos
  round its points or lines, or the union of its polygons, computed once per process from the layer's own
  features. The `nearby` and `crosscheck` tools return whether the cell lies inside it as a 0/1 value with an
  id, so an empty radius outside the footprint reads as unknown, not absent, and the node gate holds a chain
  to that.

### 4.4 Readiness and the gate command

`ue prospect readiness` reports how much of the basin each feature actually covers, meaning the share of
cells with a real observation behind their value (a distance to the nearest conductor exists everywhere; a
lake-sediment maximum exists only where somebody sampled a lake); a feature at or under 40% is flagged thin.
`ue prospect drift` compares the feature distributions now against a snapshot's and exits 1 on drift.

`ue prospect gate` is the five-column data readiness gate: one row per dataset the focused tasks depend on
(every source layer or scene collection a feature is built from, every label source, and every enabled file
of the reading corpus), each scored on **present** (in the store under its tier with a non-zero count; for a
file, every listed object on disk), **licensed** (the licence named and either redistributable or used
locally and never exported), **covers** (a coverage number stated for every feature built on it; for a file,
the share of its rendered pages that have text), **servable** (in an export, or local only by licence) and
**versioned** (a payload hash, the pull log, and a snapshot manifest that names the store as it is now). It
prints the table, writes `pipeline/data/out/prospect/gate.json`, exits 1 when any cell is red, and the Data
page, the MCP resource `ue://readiness/gate` and the readiness export carry the result. The rule it stands
for: no agent phase starts until every row is green.

### 4.5 Snapshots and lineage

`ue store snapshot` writes a hashed manifest of the store at a moment: the file's hash, every tiered table's
row count, the pipeline version and git commit, every native layer's pull hash and retrieval time, and the
feature quantiles. It is not a copy; it is the record a run cites, and `--snapshot <hash>` on `ue prospect
headline`, `modelsearch` and `hindcast` refuses to score a store that is not the one named. `ue store
register-layers` writes every pulled layer into `native.layer` with the hash of its payload; `ue store
lineage` checks that every layer row's hash matches the pull log, every feature spec's sources resolve to a
layer or a scene, and every verified inventory source has a store row or says it is unpulled, exiting 1 on a
break. Every run manifest (section 7.1) names the store hash and the snapshot it is.

---

## 5. Reading legacy assessment files

### 5.1 The batch reader

The reading pipeline turns scanned assessment reports into drillhole tables where every value carries its
file hash, page, box and verbatim quote. `ue run phase1` runs the first half end to end and idempotently:
`ue select shortlist`, `probe` and `final` choose the files (twelve, four per era, with a held-out split
proposed); `ue fetch` downloads their report, appendix, assay and certificate objects (resumable, paced);
`ue lock-heldout` writes `gold/heldout.lock` with the held-out files' hashes and never overwrites it; `ue
render` makes 200 dpi grey page images (never of a held-out PDF); `ue ocr` runs Apple Vision over each page
and compares it with the PDF's text layer; `ue route` classifies pages from the OCR words. `ue enable-files`
registers files outside the shortlist (the enabled cells' drilling files) as dev files, never a held-out one.

`ue run phase2` runs the second half: `ue extract` reads one routed page per model call under a fixed schema
(`--config` names the reader model, effort and page classes under `configs/<id>.toml`; `--max-calls` caps a
run; `--dry-run` prints the plan; `--replay` serves recorded calls only; a usage limit pauses the run with
exit 75 and a re-run resumes from the on-disk cache; `--split heldout` needs `--unlock-heldout` and every
locked hash to match); `ue assemble` wires rows into collar, lithology and assay records and locates every
quote against the OCR words (`ue locate` re-runs the locating alone and reports how well it went); `ue
validate` runs the twenty validators V01 to V20, whose findings flag values and never drop them; `ue crs
transform` places each collar or refuses to and records why (the NTv2 grid from `ue crs fetch-grid`, pinned
in `grids.lock`; `ue crs shift-grid` writes the shift field the map's datum lens draws); `ue crosscheck`
matches extracted collars to the two provincial compilations and builds the adjudication queue; `ue store
rebuild` files it all under the `read` tier; `ue export-reports` writes the report index, per-report records
and page images for the web. `ue assay-sheets` reads the spreadsheets filed with digital submissions into the
native tier with no model call. `ue run-usage` prints a run's per-page duration, tokens and cost from its own
records. `pipeline/scripts/read-enabled.sh` runs this chain over the enabled cells' drilling files, through to
the dashboard exports.

### 5.2 The extractor as an agent

`ue extract agent` runs the same pieces as a loop, one page at a time through five typed stages, each written
to the run directory as it finishes (`pages.jsonl`, one record per page per stage) so a stopped run resumes
where it was with `--resume <run id>`:

1. **locate**: the page's OCR words become the locator both readings are held to; no model call.
2. **read**: the first reading, carried in from the batch reader's results when the page was already read
   under the same prompt and schema versions (at no cost), else one call to the reader model.
3. **validate**: the file assembled and V01 to V20 run; the findings that touch this page are recorded.
4. **agree**: a second read of the same page by a different model family (`--second-model`, default
   `z-ai/glm-5.3-flash` through OpenRouter; the run refuses a model the provider's catalogue says cannot see
   images), compared with the first value by value.
5. **file**: a fresh reading lands in the batch reader's results and the file's assembled document, and the
   agreement marks and queue rows go into `read.agreement` and `read.review_item`, beside the values and never
   rewriting them.

**Second-family agreement** (`extractor/agree.py`). The same value means the same field, the same analyte for
a grade, and the same located box on the page; a value whose own quote did not locate (what happens when the
two readers read a digit differently and the OCR sides with one) is paired by its printed row within the same
field; a value with neither box is matched by its printed text, which can only count it as agreed. Equal
means, for a number, the same qualifier and equality within half a unit of the last decimal the first reader
printed; for text, equality after the locator's normalisation. Values both agree on are marked `agreed`; a
disagreement, and any value only one reader found, goes to the review queue. The agreement rate is the agreed
share of the union of values either reader found, per page and per field type, with the rate over matched
pairs beside it; both are in the run summary.

The budget is checked before every live call (`--budget-usd` is a hard stop; exit 3 on the budget, 75 on a
usage limit; the page in flight is marked stopped and the rest stay pending), every stage is a span under the
run's trace, and the manifest names both models, the prompt and schema hashes and the budget. `--agree-only`
never calls the reader, so the second family can run over pages already read; `--no-agree` runs without a
second family; `--page <file>:<page>` names exact pages; `--store` points the filing at another DuckDB file
(the serving process holds the live store's one read-write connection), `--no-store` keeps it in the run
directory; `--dry-run` prints the plan.

### 5.3 The review queue, its API and page

`read.review_item` holds every disagreement and every value only one reader found, open until a person
decides: both readings as JSON with the model that made each, the reason (`disagreed`, `only_a`, `only_b`), and
after a decision the status (`accepted_a`, `accepted_b`, `rejected`, `edited`), who decided (the key's label,
never the key), when, and the accepted reading. Rows are keyed by a hash of what they are about, so filing the
same comparison twice is a no-op. The two readings and the original `read.field_value` rows are never
rewritten, so a decision can always be seen beside what it decided between.

`GET /api/review` lists items (open by default; `status`, `file`, `limit`, `offset`), with counts; anyone who
can reach the API may look. `POST /api/review/{queue_id}` records a decision (`accepted_a`, `accepted_b`,
`rejected` or `edited` with the keyed reading) and needs the geologist role. The **Review** page lists the
open items oldest first, filterable by file: both readings side by side, the page cropped around the located
box (the crop needs the page images, which a public build ships without), and the three decisions; without a
geologist key it says so.

### 5.4 Gold tooling

The reading track's own ground truth is a page a person keyed by hand. `ue gold key <file> <page>` writes
the empty skeleton under `gold/pages/<file>/p<NNNN>.json` in the wire schema's own vocabulary, every value
null, with instructions in the file; it never overwrites and nothing in it is ever prefilled from a model,
because a gold set that started as model output would score the model against itself. A skeleton counts for
nothing until the person sets `status` to `keyed` and their name in `keyed_by`. `ue gold score --run <id>`
(`--which a` for the reader's readings, `b` for the second family's) reports precision and recall against
every keyed page the run read, per field type, with the denominators beside every rate. **No page is keyed
yet**, so the score reports zero gold pages and no number, and the Eval page's reading section is run
statistics, not accuracy.

### 5.5 The corpus text index and retrieval

`ue prospect corpus` builds a broad, shallow index beside the deep one with no model call: every
uranium-tagged file with linked drillholes gets a `native.corpus_file` row from the province's own index
(company, property, work period, the work description filed, the centroid of the holes reported); a few
hundred of those files (`--files`, default 60; `--region-only`) are downloaded and split into page text in
`read.corpus_page`, which is read tier because the text layer of a scan is usually somebody's OCR pass; and
the files read page by page keep their boxes and quotes. `ue prospect retrieve "<query>" --lon --lat
--radius-km -k` searches it, spatial filter first (the corpus is cut to the files whose holes sit near the
place) and then BM25 over the passages, with no embedding model, no key and no vector store, so anything it
returns can be checked by eye. Three tiers come back and a passage always says which: `metadata` (context,
never evidence), `page` (quotable with a file and page, evidence of what the page says and not that any
number in it is right) and `extracted` (a value with its box, quote and validator outcomes, the only tier a
number may be taken from). The `retrieve` tool wraps this for the agents.

---

## 6. Scores and models

### 6.1 The criteria score

`pipeline/knowledge/criteria.toml` is the knowledge-driven score written so every threshold can be argued
with: ten criteria, each with the feature it reads, a membership shape (`falling` between two distances,
`band` for a depth window, `binary`, `percentile_rising` with thresholds taken from this grid's own
distribution and recorded with the score), a weight, a status, the evidence quoted from its source and the
caveat the skeptic is expected to use. The status is `published` (the association is stated in a
peer-reviewed or agency source), `assumed` (the association is published, the numeric threshold is ours) or
`folklore` (recorded in the literature as personal communication with no published test). Eight criteria
count: conductor proximity, the graphitic host, fault proximity, structural density, the unconformity depth
window, lake-sediment uranium, lake-water uranium and the boulder train. Two are folklore, conductor strength
and EM bright spots, carried at **weight zero**: the loader refuses to start if folklore ever gains weight, the
agents may name them and never count them. A cell is scored only when criteria covering at least half the
total weight (`min_known_weight`) have a value; otherwise its score is null and drawn as a gap. `ue prospect
score --model criteria` writes `derived.cell_score` and each criterion's membership and contribution to
`derived.cell_criterion`. Nothing in it is fitted to the labels.

### 6.2 The learned model and the effort null

`ue prospect labels` marks the positive cells from the deposit footprints and the uranium-primary
occurrences, groups them into camps, and assigns spatial blocks from position alone, before any fit. An
unlabelled cell is not barren, because nobody has drilled most of the basin; the models are
positive-unlabelled, and every metric is reported as such.

`ue prospect score --model learned` and `--model effort` (`--model all` runs the three) fit the same
learner, scikit-learn's `HistGradientBoostingClassifier` (`max_depth=4, max_iter=200, learning_rate=0.08,
l2_regularization=1.0, class_weight="balanced", random_state=0`; shallow on purpose), on two feature sets:
**learned** reads geology only (distance to a conductor and to a fault, fault density, the surface host, the
unconformity depth, the water, vegetation and bare fractions, elevation and relief), and **effort** reads the
six exploration-effort features and nothing about rock. Both fit on complete cases only (no imputation, because
imputing a missing reading invents evidence) and are compared on the cells where every feature of both sets is
present. The effort model is the null: it exists to be beaten. An area-of-applicability mask marks cells whose
features sit far from anything in training, whose score is published as null. The published per-cell score is
fitted on every cell it scores; the out-of-fold scores a benchmark reads are a different table (section 10).

The fourth view on the map, **Learned − effort**, is not a model: it is the arithmetic difference between the
two scores, computed in the export so the number the map colours and the number a panel prints come from one
subtraction. Blue is where the learned score leads, pink where effort leads.

### 6.3 Folds

Three fold schemes, all committed before any fit: `random`; `spatial`, where whole 30 km blocks are held
out together so a test cell never sits beside its own training data; and `camp`, leave one camp out with the
ground around it (about 25 km), because holding out only the positive cells would leave a test set with no
negatives. Nearby cells share geology, so a random split leaks near-copies and inflates every number; the
spatial and camp rows are the ones to read.

### 6.4 Metrics with intervals

`ue prospect score` writes PR-AUC, ROC-AUC, capture in the top 10% of ranked ground and the base rate per
model and fold to `derived.metric`, and the criteria score is evaluated on the same cells with no fold (nothing
was learned from the labels). `ue prospect headline` is the re-test with the two published corrections,
each on or off, all four combinations: matched background (negatives drawn so their distribution over an
effort index matches the positives') and thinned positives (at most one per 10 km block, so a camp of adjacent
deposit cells counts once); under all three fold schemes; with bootstrap intervals over cells, positives and
negatives resampled separately; with deposit capture at fixed area budgets (5, 10 and 15% of ranked area);
and with MineTRACE's own protocol reported beside them as a different measurement. Every configuration is a
pure function of the feature matrix and a seed.

### 6.5 The model search and the registry rule

`ue prospect modelsearch` puts candidates through the same folds against the same null, each an MLflow run:
the gradient-boosting learner, a random forest, a logistic regression with position, a bagging
positive-unlabelled learner, the learner with the criteria score as a prior, and the effort null; ablations;
and spatial block sizes of 20, 30 and 50 km (`--quick` runs four candidates under spatial folds only, the CI
regression). The registry rule is applied in one place: the best geology-only candidate is *validated* only if
it beats the effort null under spatial folds with the bootstrap intervals apart, and it is *served* only if it
is validated. The decision is registered in the MLflow model registry with its stage (`validated` or
`candidate`), `served` false, the reason and the store hash. Today nothing is served, and the dashboard shows
no learned predictor as such. `uv run mlflow ui --backend-store-uri sqlite:///data/mlflow.db` browses the runs
and the registry (MLflow is installed with `uv sync --extra mlflow`).

### 6.6 The hindcast

`ue prospect hindcast` asks the question a company would: had the model been run in year C, where would it
have ranked the ground where the deposits found after C turned out to be? The dated discoveries live in
`pipeline/knowledge/discoveries.toml`, each cited to a public technical report with a confidence
(`--min-confidence high|medium`). At each cutoff (`--cutoff`, default 2000 and 2010) the labels drop every
later discovery and the drilling features drop every cell first drilled later; the grid is scored by the
criteria, learned and effort models; and each later discovery's cell is reported as the share of the basin's
area that scored at least as well, with a summary per model. What cannot be frozen is stated on every row: the
geological layers are compilations as they stand today and the survey footprints carry no year.

### 6.7 The Eval page's tables and where their numbers come from

Every table on the Eval page is read from `web/public/data`, and every number in it is a value with an id:

| Table | Written by | From |
|---|---|---|
| The reading run statistics and checks | `ue export-eval` | the extraction runs, the assembled documents and the validators (`gold` stays null until a scorecard exists) |
| What the map's scores are worth | `ue prospect export` | `derived.metric` rows `ue prospect score` wrote |
| The re-test | `ue prospect export` | `pipeline/data/out/prospect/headline.json`, each row naming its MLflow run and the store snapshot |
| The model search and its decision | `ue prospect export` | `pipeline/data/out/prospect/modelsearch.json`, each arm naming its run, the decision and the model card from the registry |
| The dated hindcast | `ue prospect export` | `pipeline/data/out/prospect/hindcast.json` |
| The analyst benchmark | `ue prospect export` | `pipeline/data/out/bench/<version>/table.json`, which `ue arm table` writes from each arm's latest run summary and the baselines; the highest version that has a table, with the per-stage columns re-derived from each run's own cell rows |
| The fabrication gate, put to the test | `ue prospect export` | `pipeline/data/exports/gate_eval.json`, which `ue prospect gate-eval` writes |

None of these reads the benchmark dataset directory, so `npm run validate:data` passes without it. The
readiness export (`web/public/data/prospect/readiness.json`, with `coverage.geojson` and `scores.geojson`)
carries the Data page and these blocks together; a block is absent, not empty, until its command has run.

---

## 7. The agents

Three agents share one runtime: the analyst (a reasoning loop over the evidence record), the interface agent
(the chat) and the extractor (the reading loop of section 5.2). Every model call any of them makes goes
through the same backends, the same cache, the same ledger and the same tracing.

### 7.1 The runtime

**Backends.** `backends/claude_cli.py` runs one `claude -p` subprocess per call with the page image staged in
a fresh temporary directory outside the repository and the API-key variables removed from the child;
`backends/openai_api.py` and `backends/openrouter.py` are API adapters that inline staged files and send
images as parts; `backends/replay.py` serves recorded envelopes and raises on anything not recorded, and is
what every test runs on; `backends/router.py` sends each request to the adapter its model id belongs to
(`--backend auto`: a `vendor/model` id to OpenRouter, a bare `claude-*` id to OpenRouter's Anthropic listing, a `gpt-*` id to OpenAI; never the CLI), so a cheap executor beside
a strong verifier is one backend with two ledgers.

**Cache keys.** `backends/cache.py` keeps every successful call under `pipeline/data/cache/calls/` and every
failure under `failures/`, never served. The key is the hash of the task, the backend family, the image and
staged-file hashes, the render parameters, the prompt and schema versions, the model, the effort, the carry
context, and the sha256 of the system prompt, the user prompt and the schema themselves, so an edited prompt
is never served an older prompt's answer (leak register B22) and two backend families never share an answer.
`ue cache stats` counts the records; `ue cache rekey` moves records written under the older key to the one a
live request computes now, checked two ways before anything is written. A cache hit costs nothing and charges
no budget.

**Budgets.** One ledger, `pipeline/data/spend.jsonl`, every family writes to; two cumulative ceilings on disk
(the total, `UE_MAX_SPEND_USD`, and a family's own where it has one) checked before each live call, so a
restart never hands back a fresh budget; and one run budget in memory (`--budget-usd`) that the cached
backend refuses to cross by the worst case of the call about to be made. Dollars are recorded as the provider
reported them where it does (the CLI's envelope, OpenRouter's usage block) and as arithmetic over configured
prices where it does not (OpenAI). `ue spend show` prints spend per family against its ceiling.

**Runs and manifests.** Every run has an id (a UTC second plus its kind) and a directory under
`pipeline/data/runs/<id>/` claimed atomically, holding `manifest.json`, the call log, the staged tool
results and `spans.jsonl`. The manifest names everything the answer depended on (the prompt and schema
hashes, the model behind each role, the store hash and snapshot, the git commit, the benchmark manifest and
the score versions the agent saw, the retrieval blind-list, the seed) and everything the bill depended on (the
budget and what was spent), written before the first call and finished after the last, so an aborted run
still says what it was.

**Tracing.** A run is a trace; every cell, tool call, model call and gate check inside it is a span with a
parent, a duration, its attributes (argument names and hashes, never their values, because a real cell id in
a benchmark trace is a leak) and whether it failed. `spans.jsonl` is the source of truth. The same spans are
mirrored to MLflow Tracing beside the experiment runs unless `UE_TRACING_MLFLOW=0`, and exported over
OTLP/HTTP when `UE_OTLP_ENDPOINT` is set (Jaeger, Grafana Tempo, a hosted backend with `UE_OTLP_HEADERS`),
with the same ids and attributes. Both are best-effort: batched, flushed at the end of the run, never in the
way of a model call, and a backend that is down costs one warning. One run at a time is traced per process
(a pool thread's span attaches to the process's active run); a registry keyed by run is on the PRD's backlog.

### 7.2 The analyst

**Before the analyst: the memo panel.** `ue prospect memo` runs three roles over one cell, each a short tool
loop: the proponent argues from the tools, the skeptic attacks coverage gaps, effort artefacts, folklore and
proximity to known deposits, and the adjudicator rules ("supports a closer look", "insufficient evidence",
"evidence against"), names which criteria are unknown rather than absent, and states the one observation that
would change the verdict. Every memo passes the fabrication gate before it reaches `agent.memo`; a rejected
memo is kept as the record of a rejection. The panel's memos are what the evidence tab shows above the chains.

**The eight tools** (`prospect/tools.py`) are deterministic Python over the store, and the only numbers any
agent may use: `cell_features`, `cell_scores`, `criteria_breakdown`, `label_context`, `coverage`, `retrieve`,
`nearby` (what one evidence layer holds around the cell: a count, the nearest features with true distances and
readings, and whether the cell lies inside the layer's footprint) and `crosscheck` (the two conjunctions the
handbook reads together, computed: the smallest conductor-fault separation and the crossings within 5 km, and
the lake-sediment anomaly beside its sampling density with a fixed thin-sampling rule). Every number a tool
returns arrives as a value with an id (`c:cell:<cell>:<feature>`, `c:crit:<cell>:<criterion>`,
`c:nb:<cell>:<layer>:<radius>:...`, `c:x:<cell>:<pair>:...`, `c:cov:<feature>`, `c:metric:<key>`), a
criterion's thresholds and weights included, so the argument is never about a number the model has to quote
from a file. The model never computes anything. `nearby` refuses a label layer (the labels are the answer) and
a context layer.

**The fabrication gate** (`memo.check_claims`) is the one rule every answer passes: every number in a claim
must resolve to a value the claim cites by id, in any of the forms the gate can verify itself (a rounding, a
thousands separator, a ratio spoken as a percentage, metres as kilometres), or appear verbatim in text a tool
returned (a hole name, a map scale, a quoted passage; only the string leaves count, so a number inside a cell
id is not a quotation). An answer that fails is not shown.

#### Analyst v0: the single call

`analyst/v0.py` is the floor: everything the model may know is handed over at once (the map card as an
image, the evidence pack as text, retrieved passages when the arm allows them), it answers once with a
verdict, a probability that the public record labels the cell as a deposit or occurrence (not that ore is
present), claims with value ids, the unknown and absent criteria and the next observation, and the answer is
gated. Closed book: the prompt names no place, and it refuses to build if the handbook material it draws on
ever does. Switches remove rows from the pack before it is rendered rather than asking the model to ignore
them, and the cache key carries them.

#### The staged analyst

`analyst/loop.py` reasons in stages, after STA-CoT (a planner, an executor per segment, a mechanical rule
check, a model chain verifier with node-level feedback, refinement rounds, a majority over rounds) adapted to
structured evidence.

**The session and the leak register.** `analyst/session.py` is the only path from a tool to a model, and the
leakage rules live in it as code because they are properties of which rows reach the model. A session has a
purpose: `dashboard` (unblinded, the label kept), `scored` or `benchmark` (blinded). In a blinded session:
**B17**, retrieval leakage: `retrieve` is given the cell's blind-list (the files whose reported holes sit
within 10 km, plus any read file whose collars fall inside) and a passage that still names a blind-listed file
is dropped and counted; **B18**, score leakage: `cell_scores` never serves the fitted table, which saw every
label, but the out-of-fold scores for the session's fold, recording which model versions and fold the model
saw; **B30**, the cell's own label: `label_context` is asked with the evaluated cell masked and a row at
0.0 km dropped; **B19**, expert anchoring: every expert-tier value is remembered by id and the row that cites
it marked, so a node that leans on a geologist's insight says so. An arm's switches are enforced the same way
(a part that is off is refused, not hidden behind prompt wording), a benchmark session anonymises every id to
`b:<bench id>:...` and scrubs every name that places the ground, a scored session keeps the cell id and
scrubs the names, and every refusal is charged to its rule so a run counts refusals per rule. The unscrubbed
result is kept under `stage/private/` for the dashboard and never staged to a model.

**The template plan** (`analyst/plan.py`): one segment per counted criterion in table order, each with the
calls it needs (`cell_features`, `criteria_breakdown`, `coverage` for its feature, and `nearby` on its
feature's layer out to the criterion's own threshold), then the two cross-checks with the criteria they
depend on, then a retrieval pass when the arm asks for one. It is dull on purpose: the same criteria and
switches give byte-identical JSON, `$cell` stands where the cell id goes, folklore is never executed, and a
switched-off tool is dropped rather than shown. A model planner (`planner = "model"`) is held to the same
rules by `check_plan` and falls back to the template with its problems recorded.

**The node protocol** (`analyst/wire.py`): a node is a claim about one criterion, not a sentence. Its status
is `met`, `not_met` or `unknown` (a feature nobody measured here is unknown; measured and not met is not_met),
its strength an integer from 0 to 5 that must be 0 when unknown, its `value_ids` the list the gate binds the
text's numbers to, its `expert_ids` the expert-tier ids among them, its `depends_on` the chain, and its text
one sentence. The schemas cover only what a model returns; the harness fills the ids, rounds, attempts, model
and cost, so a model can never claim to have been published.

#### The node gate

`analyst/nodegate.py` is the mechanical verifier every node passes before a model sees it, six rules and no
judgement: (1) every number in the text resolves to a cited id, with the same leniency a memo gets; (2) cell
identity: every cited id names the cell being assessed or is grid-wide, so a value from another cell is
refused; (3) polarity: a `met` node must cite its own feature value or membership on the favourable side of
the criterion's threshold (the criteria table's own membership rule, at or above 0.5) and a `not_met` node
the reverse, unless it says so with "however", "but" or "despite"; (4) unknown only where unmeasured: a node
may say unknown only when its feature has no value here, and a feature whose layer was never mapped around
the cell counts as unmeasured whatever the distance feature carries; (5) no arithmetic: an equals sign, an
operator, an approximation sign or a word for a computation next to a number is refused, because the model
never computes; (6) absence of mapping is not absence of features: a `not_met` node is refused when the
staged `nearby` or `crosscheck` result for its layer says `in_footprint` is 0, with feedback naming the layer,
the cell and the flag and saying to record unknown; a `met` node is never refused this way. A place name in
the text is refused too. A rejected node goes back to the executor with feedback that escalates over three
attempts (the reasons; the reasons and the ids the node may cite; the protocol in three lines and permission
to answer unknown), and a node that fails the third time is recorded as unknown with strength 0 and its
problems kept, never dropped, so the verifier sees the segment was tried.

**The verifier and its rounds.** With `verifier = "skeptic"` a second model reads the whole chain with the
skeptic's brief and returns valid or not, the faulty nodes with reasons, chain-level feedback and its own
candidate label. Its word is checked too: faulty ids the chain never had are dropped and counted, and a
verdict that fails its structural check is recorded as invalid with the harness's reason. **The re-ask
rule**: a verdict that rejects the chain but names no node breaks the protocol the way an uncited node does
(nothing could be re-executed and the chain would be withheld on prose alone), so the verifier is asked once
more with that rule as feedback, and a second such verdict stands. When nodes are faulted, they and
everything that depends on them are re-executed with the reason as a note, and the chain is verified again,
up to `rounds` (K, the first construction included; three in the `v1` arm). A reason that names a place is
withheld rather than rendered.

**The deciders.** Two always run under `decider = "both"`: (a) `analyst/weights.py`, a weighted sum over
node strengths (met is +strength/5, not met is −strength/5, unknown is 0 with a known flag), with the
criteria table's own weights by default, null when too little is known; or fitted weights, a logistic
regression fitted per spatial fold on the other folds' chains, versioned by a hash of its training rows; and
(b) the adjudicator, one call over the chain and the effort note, gated exactly as v0's answer, asked again
with the gate's feedback up to three times. The final label is a lookup: the adjudicator's verdict when a
round validated, else the majority over the rounds' candidate labels, else an abstention with its reason.
Triage (`triage = true`) is a deterministic shallow path: when the three out-of-fold scores agree and every
counted criterion is measured and not thin, the adjudicator rules over the staged results alone.

**Publishing.** A chain is published only when a round validated, the decision passed its gate and every
current node passed the node gate; `analyst/chains.py` refuses to store a chain marked published when any
part did not, re-running the claim check on the decision, and a refused chain is stored unpublished as the
record of the refusal (which the benchmark counts as an abstention). Every attempt of every node, every
verdict and the decision land in `agent.chain`, `agent.chain_node`, `agent.chain_verdict` and
`agent.chain_decision`, with the cited values carried so a chain can be re-scored without the store it ran
against. The chain file is written under the run's `chains/` whatever happens, so a budget stop leaves the
partial chain behind.

**The switches.** The evidence switches (`drillholes`, `label_context`, `oof_scores`, `effort_features`,
`criteria`) cut rows before rendering; every v1 arm keeps `oof_scores` off so v0 and v1 are compared on the
same evidence, and the verifier and adjudicator are then told the effort null is withheld (an arm with it on
is on the PRD's backlog). Three loop switches cut cost without loosening the gate, each off by default and
each an arm: `skip_unmeasured` lets the harness write the unknown node for a criterion whose feature has no
value here (citing the nearest observation by id), still through the gate; `executor_batch` asks one
executor call for every criterion's node in the first construction, gates the reply node by node and sends
only the refused criteria down the per-segment path at attempt 2; `segment_scoped` stages each executor only
its own criterion's rows of the feature, criteria, coverage and cross-check tables, with the gate holding its
node to what its segment saw plus the prior nodes it was shown.

**Arms as files.** `pipeline/configs/arms/<name>.toml` is everything that may differ between two runs of the
analyst over the same benchmark: the agent (`v0` or `v1`), the model and effort, the per-call ceiling and
timeout, the workers, the inputs (card, pack, passages), every evidence switch stated, a `notes` line naming
the one question the arm answers, and for a v1 arm the `[arm.loop]` table (the executor, verifier,
adjudicator and planner models, the planner, the verifier, the rounds, triage, the executor context
`independent` or `cumulative`, the decider, the segment workers, retrieval, and the three cost switches). A
file that forgets a switch is not an arm, and the file's `name` must match its stem. The arms checked in:
`v0` and its ablations (`v0-card`, `v0-text`, `v0-features`, `v0-holes`, `v0-labels`, `v0-scores`,
`v0-retrieval`, `v0-sonnet`, `v0-qwen38`), and `v1` with `v1-noverify`, `v1-K1`, `v1-modelplanner`,
`v1-cumulative`, `v1-triage`, `v1-strong`, `v1-cheap`, `v1-openrouter`, `v1-anthropic-or`,
`v1-skipunmeasured`, `v1-batch`, `v1-scoped` and `v1-scoped-batch`.

**Running it.** `ue arm run --arm <name> --version <bench>` runs one arm over the open cells of a frozen
benchmark: cached, budgeted, traced, scored against the key, one MLflow run per arm; `--cells` names bench
ids, `--workers` cells in parallel, `--backend claude|auto|replay`, `--resume <run id>` carries finished
cells forward, exit 3 on the budget and 75 on a usage limit. `ue arm chain --enabled` (or `--cells`) runs the
staged loop over real cells for the dashboard in one-connection mode and publishes the chains into the agent
tier. `ue arm score --run` scores a run's cells against the key, `ue arm regate --run` re-applies the current
gate to stored v0 answers and re-scores with no model call, `ue arm table` merges every arm's latest run with
the baselines into `pipeline/data/out/bench/<version>/table.json` and `derived.metric`, and `ue arm
baselines` prints the baseline rows alone.

#### The dashboard's scope rule

The analyst runs only on the **enabled cells** frozen in `pipeline/knowledge/enabled_cells.toml`: five demo
cells chosen for their score behaviour, the cells of the reports read in the reading pipeline, and two deposit
cells per camp with the fewest attached files that still carry drilling. For each enabled cell every
assessment file inside it is fetched and text-indexed, the two files with the most drillholes are model-read,
and the analyst chain is computed offline (`ue arm chain --enabled`) and served as-is. A live reading is
available only on them, as a job within a per-session budget; any other cell shows its scores and its
evidence, and a request to run the analyst on it is refused with that reason.

### 7.3 The chat: the interface agent

The chat behind the panel is the interface agent (`uranium_explorer.interface`). It adds no signal: it finds
(the eight tools), explains (one answer call over what the tools returned), records (a geologist's insight
into the expert tier) and invokes (the analyst, as a job), on a cheap model, because the value ids and the gate
carry the correctness rather than the model. Every turn goes:

1. **Route.** One structured call at low effort with no evidence attached classifies the question into a
   fixed kind: `lookup`, `compare`, `explain_score`, `what_is_unknown`, `what_would_change`,
   `record_insight`, `run_analyst` or `other`; names the topic (features, scores, criteria, labels, coverage,
   passages, nearby, crosscheck, sensitivity), the cells and the things the question is about; and says
   whether it is out of scope (a company's holdings, a grade or tonnage, an ore body, where or whether to
   drill, ground outside the grid). The model never answers here, and a reply that cannot be read is routed
   as `other`, so a router failure costs a few tool calls and never an answer.
2. **Plan.** Each kind has a plan written in Python: a lookup goes to the one tool that holds its topic;
   compare runs the same tools on both cells; explain_score reads `cell_scores` and `criteria_breakdown`;
   what_is_unknown reads `criteria_breakdown` and `coverage`; what_would_change runs the **sensitivity**, the
   one computation the agent has (`interface/sensitivity.py`): for every criterion unknown at the cell, the
   criteria score if it were measured and met, if measured and not met, and the move from now, from the
   weights and memberships the store holds, ranked by the move, every figure a value under `c:sens:<cell>:...`.
   A read already staged in the conversation with the same arguments is reused. `other` falls back to the
   plain tool loop (`interface/loop.py`), capped at five model calls, which may call a tool, answer or abstain.
3. **Answer, gated.** One answer call on the same model over the staged evidence, with the handbook and the
   criteria file staged for context. The gate holds every number in the claims and in the prose to an id a
   tool returned in this conversation; an answer that fails is sent back once with the objections and asked
   again, and one that fails twice is withheld with the objections in its place. A reply with no answer object
   is refused with a reason the retry can act on. A claim that cites an expert-tier id is labelled (B19).
4. **Or abstain.** The answer call may decline instead, with a reason from the fixed set `not_measured`,
   `outside_grid`, `no_value` or `out_of_scope`, recorded under an abstain id through the MCP server's own
   handler so a refusal is something the benchmark counts. An out-of-scope question goes straight to it from
   the router with no answer call. The model's own words for the refusal are shown only if they pass the gate.

Two kinds are actions rather than reads. **Record insight** writes the person's statement to the `expert`
tier through the MCP server's `record_insight` handler (author = the caller's label, `local` with no
register), returns its expert id, and from then on any claim in the conversation may cite the values minted
from its numbers, labelled as the geologist's statement rather than a measurement. **Run analyst** hands the
cell, its out-of-fold score ids, the expert ids recorded in the conversation and the reason to the job runner
(`api.jobs`), only on an enabled cell and within the per-invocation and per-session budgets
(`UE_ANALYST_JOB_BUDGET_USD`, `UE_ANALYST_SESSION_BUDGET_USD`), and returns a job id; on a later turn the
agent reports every job that finished since, with its verdict and the **diff** (`interface/diff.py`) against
the cell's newest published dashboard chain that leans on no expert id: node statuses and the verdict, computed
and never reasoned.

`ue interface ask --cell <id> -q "<question>" [-q ...]` runs one or more turns in one conversation from the
terminal under a hard budget (`--budget-usd`, default 0.50; `--backend auto|openai|claude`; `--model`;
`--effort`; `--refresh` to ignore cached replies; `--json` for one line per turn); an insight is written only
to the DuckDB file named by `--insight-store`, under the author label `--requested-by` (default `local`),
and refused without one. `ue prospect record` (section 3.5) records the tour's session: `--cell` (an enabled
cell), `--backend`, `--model`, `--effort`, `--budget-usd` for the whole recording with `--job-budget-usd` the
job's share, `--no-job` to record the read questions only, `--refresh`, `--out` (default
`web/public/data/prospect/recorded_chat.json`); with `--backend auto` it refuses to start without
`OPENROUTER_API_KEY` (exit 2) and exits 3 on the budget without writing.

### 7.4 The extractor, as the third agent

The extractor's loop is section 5.2: the same runtime (a manifest before the first call, a span per stage, the
cached backend's budget check before every live call, resumable state on disk), two model families over one
page, and a queue for what they disagree on.

---

## 8. The MCP server

The eight tools are also served over the Model Context Protocol, so a geologist's own Claude Code or Cursor
session, the dashboard and the benchmark harness read the same contract from the same code. The server wraps;
it does not reimplement: the session handle carries the leakage rules of section 7.2, the gate binds every
number to the id the tools gave it, and nothing here computes a number or samples a model.

**Tools.** `ue mcp tools` prints the catalogue as served (`--json` for the `tools/list` document), fifteen
tools under tool-contract version `prospect/tools/v2`:

| Tool | Kind | Scope | What it does |
|---|---|---|---|
| `open_session(cell_id, purpose, fold?, bench_id?)` | action | read | Opens a session over one cell and returns the handle every other tool takes. `dashboard` shows the record as it is; `scored` and `benchmark` serve out-of-fold scores only, mask the cell's own label and blind-list the files within 10 km; a benchmark session shows the cell under a bench id and refuses any real cell id. |
| `cell_features`, `cell_scores`, `criteria_breakdown`, `label_context`, `nearby`, `coverage`, `crosscheck`, `retrieve` | read | read | The eight reads, over the session. A blinded session reads its own cell only. |
| `hole_crosscheck(cell_id?, hole_id?, file_num?, radius_km?)` | read | read | The extraction crosscheck over hole positions from `ue crosscheck`'s outputs: each extracted collar against the two provincial compilations, name match, offset and bearing with their ids, the datum-shift signature and whether the pair is queued. Dashboard sessions only, because it names files, holes and positions. |
| `check_claims(claims)` | read | read | The fabrication gate as a callable over what this session returned: the problems, the ids that resolved and those that did not, so a stock client checks itself before answering. |
| `abstain(reason, detail?)` | action | read | Records a refusal with a reason from the fixed set on the session, so refusal is measurable. |
| `record_insight(text, author, cell_id?)` | action | record | A geologist's statement into `expert.insight`; every number in the text is minted an expert-tier value id. |
| `run_analyst(config?, budget_usd?, reason?, expert_ids?)` | task | run | The staged analyst over the session's cell as a background job on the API's runner; returns the job id at once. Dashboard sessions on an enabled cell only. |
| `job_status(job_id)` | read | read | The job's row: status, who asked, the stages reached, and when done the chain id, the verdict and the cost as a value with an id. |

Every number in a result is a value with an id in `structuredContent` and beside its id in the text block.
Unknown ("nobody measured it here", with the nearest observation when the tool gave one) and absent
("mapped, and nothing there") are two object types in every output schema, never a null, and the SDK's client
validates each result against the tool's `outputSchema` before a model reads it, so a bare number in a row
fails at the client. A refusal of any kind (a cell outside the grid, a handle that is not the caller's, a
blind-listed file, a tool the key's scope does not carry, an argument outside its schema) is a tool result
with `isError` and a reason the model can act on, never a protocol error.

**Resources** hold data: `ue://handbook` and `ue://criteria` (the two knowledge files every role stages),
`ue://cell/{id}/evidence` (the record the dashboard draws), `ue://cell/{id}/chains` (the stored chains with
the values they print through), `ue://run/{id}/manifest` (any run's manifest, an MCP session's included),
`ue://readiness/gate` and `ue://reading/inventory`. A resource carries no session, so the cell resources are
the dashboard's unblinded view and a benchmark harness must not read them for the cell it is scoring; a
session-scoped view is where this stops. **Prompts** are the six roles, each built from the text the in-house
loops use: `proponent`, `skeptic`, `adjudicator`, `analyst.executor`, `analyst.verifier` and
`interface.router`, each taking `cell_id` and `session_id` and ending with the same footer (read through the
tools with that handle, cite ids, run `check_claims` before answering, `abstain` when the tools cannot answer,
never compute a number). The server never asks a client's model for anything.

**Sessions.** A handle is opaque, bound to the key that opened it, and expires four hours after the open. One
call runs at a time per session (its store handle is one connection) and one traced call at a time across
sessions. Every session is a run: a directory under `data/runs/<id>-mcp/` with the runtime's manifest (the
store hash and snapshot, the prompt hashes, the purpose, fold and blind-list hash, the scores seen, every
abstention and insight, and at close the value ids served) and `spans.jsonl` with one span per call carrying
the arguments' hash, the result ids, the latency, the session and the run id, mirrored to MLflow and exported
over OTLP like any other run.

**Scopes** are section 2.4's: `read` opens a session, the reads, `hole_crosscheck`, `check_claims`,
`abstain`, `job_status`, and every resource and prompt; `record` opens `record_insight`; `run` opens
`run_analyst`. `tools/list` shows a key only the tools its scopes carry, in catalogue order with a cache hint,
and a call outside them is refused with a reason.

**Public-safe mode** (`ue mcp serve --public-safe`, or `UE_MCP_PUBLIC=1`) is the build for anyone outside
the team: a passage's text is withheld (its citation, page and ids stay), `hole_crosscheck` is not served
(the collars it compares are read off report pages and one compilation is not redistributable), and a
`nearby` layer the inventory marks non-redistributable is refused.

**Transports.** `ue prospect serve` mounts the server on the API at `http://127.0.0.1:8787/mcp` (streamable
HTTP, stateless JSON responses, with the job runner attached); `ue mcp serve --http` serves the same route
alone on `:8788/mcp` when the API is not running (`--host`, `--port`, `--runs-dir`); `ue mcp serve --stdio`
serves the client that launched the process on its stdin and stdout. Local only by default: with no register
the HTTP route answers loopback and private network addresses alone (the Compose file publishes the port on the host's loopback, so nothing beyond the machine reaches it) with DNS-rebinding protection on, and a stdio client holds
every scope.

**Connecting a stock client.** Put this in `.mcp.json` (Claude Code) or `.cursor/mcp.json` (Cursor) at the
project root:

    {"mcpServers": {"ai-uranium-explorer": {"command": "uv", "args": ["run", "--directory", "pipeline", "ue", "mcp", "serve", "--stdio"]}}}

With a register configured, add `UE_MCP_KEY` to that server's environment; an HTTP client sends
`Authorization: Bearer <key>`.

**Stated boundaries.** The standalone `ue mcp serve` has no job runner, so `run_analyst` and `job_status`
refuse with the reason and `ue prospect serve` is where the analyst runs as a job: a runner's recovery pass
marks every queued or running job in the store as failed on start, which is right for the one API process
that owns the pool and wrong for a second process beside it. Resources carry no session. One traced call runs
at a time across clients. Each of these is on the PRD's backlog. Contract tests run the whole server through
the SDK's in-memory client with no network and no model; the few that need the live store skip without it.

---

## 9. The API and jobs

### 9.1 Routes

`ue prospect serve` runs a FastAPI service (`api/app.py`) on `127.0.0.1:8787`, with `/docs` as its OpenAPI
page; `ue api-spec` writes the OpenAPI document into `web/src/api/openapi.json`, from which `npm run
api:types` generates the web's typed client.

| Route | Role | What it does |
|---|---|---|
| `GET /api/health` | none | ok, the model, effort and backend, where the candidate list came from (`postgis` or `duckdb`), the evidence cache's hits and misses |
| `GET /api/whoami` | any key | the principal the key resolves to: its label, scopes and roles |
| `GET /api/cells?limit&model` | none | candidate cells, highest score first (through PostGIS when `UE_PG_DSN` is reachable, else DuckDB) |
| `GET /api/cell/{id}` | none | the evidence record: the four tool results with their values, the memos and the chains (cached per cell, invalidated by any write to the store file) |
| `GET /api/cell/{id}/conversations` | none | the stored conversations on the cell |
| `GET /api/conversation/{id}` | none | a persisted transcript, turn by turn |
| `POST /api/chat` `{cell_id, question, conversation_id?}` | geologist | one gated turn; `POST /api/chat/stream` streams the same turn as NDJSON events (section 3.3), the loop running on a worker thread |
| `POST /api/jobs` `{kind, cell_id, args}` | the kind's own (analyst: admin) | queue a job; answers 202 with the row |
| `GET /api/jobs/{id}`, `GET /api/cell/{id}/jobs` | viewer | poll a job's row, or a cell's jobs newest first |
| `POST /api/jobs/{id}/cancel` | the kind's own | stop a job: queued, it never starts; running, it stops at its next model call |
| `GET /api/review`, `POST /api/review/{queue_id}` | none / geologist | the review queue (section 5.3) |
| `/mcp` | by scope | the MCP server (section 8) |
| everything else | none | the built site, when `--web-dist` names it |

**Persistence.** Conversations are written to `agent.conversation` and `agent.conversation_turn` turn by
turn as they happen: the question, the gated answer, its claims and value ids, the tool calls that produced
them, the cited values, the gate's objections, the cost and who asked. That is enough to replay and re-score
an answer later, which is the raw material of the interface benchmark. A known conversation id that is not
live is rehydrated from the store.

### 9.2 Long work is a job

**Long work is a job**, never a blocked request: anything over a second runs on a bounded pool of threads
inside the API process, with a durable row per job in `agent.job` (status `queued`, `running`, `done`,
`failed` or `cancelled`; who asked, as the key's label; the arguments; the stages as they happen in
`progress_json`; the result; the error; the run id the job claimed). The row is rewritten as the job's state
changes, so a poll sees the stages before the job ends, and a restart marks every job an earlier process left
queued or running as failed with `process restarted` rather than leaving it running for ever.

**Why in-process.** DuckDB allows one read-write connection per file and the serving process holds it (one-
connection mode), so a worker in another process could not publish a chain into the store the API is reading.
The pool starts on the first submission; `UE_JOB_WORKERS` sizes it.

### 9.3 Keys, roles and background jobs

A job kind is a function plus the role a submitter needs, how it checks its arguments before anything is
queued, and what it will spend. The first and only kind is `analyst`: the staged loop over one enabled cell,
through the same `run_cells` that `ue arm chain` runs, so the chain lands in the agent tier exactly as an
offline one does and the evidence panel lists it. Its arguments: `arm` (a v1 arm; default `v1-openrouter`,
whose every role is a `vendor/model` id, so under the job's `--backend auto` routing every call goes to
OpenRouter), `budget_usd` (default
0.50, at most 2.00, refused when under the arm's per-call ceiling because no call could then be made),
`reason` and `expert_ids`. The submission needs the admin role; a cell that is not enabled is refused with
the scope rule's reason. The runner has a per-process budget across jobs (`UE_JOB_SESSION_BUDGET_USD`) that a
submission may not take the total past, reserved under one lock so two submissions cannot both fit into the
same remainder; the interface agent has its own per-invocation and per-conversation ceilings on top. A running
job's stages are read from the run's `spans.jsonl` as the loop's stage spans close, so the row says where the
chain is without the loop knowing about jobs; a cancel sets a flag the job checks before every live model
call. The result names the chain id, the run, the arm, the verdict, whether the store published the chain,
its problems and the cost. The chat's "run the analyst" intent, the rail's **Run analyst** button, `POST
/api/jobs` and the MCP `run_analyst` tool all submit to this one runner; every row says who asked, and a
recording's rows say `tour`.

---

## 10. Evaluation

### 10.1 The fabrication gate suite

The honest problem with a gate is that a check that has only ever fired on false positives is an untested
check. `ue prospect gate-eval` (`prospect/gate_eval.py`, "the gate suite") tests the gate directly with no
model in the loop: it takes the real evidence pool of each assessed cell (the four opening tool results;
`--cells` names cells, default the cells with memos), writes claims that are true of it in every form a
careful writer might choose, then corrupts them the way a language model plausibly would: a digit slips, a
decimal moves, two digits transpose, precision is invented, a metres-to-kilometres conversion is done by hand
wrongly, a figure is invented whole, a real number is cited to the wrong value (nothing invented, the hardest
case, which only the id binding catches) and a number is stated with no citation. Two conditions are reported:
`cited` (the claim judged only against the values it cites; the rule as stated, and the ceiling) and
`production` (plus any number appearing in the text the tools returned; what ships). It writes
`pipeline/data/exports/gate_eval.json`, which the Eval page shows: fabrications caught and missed by kind,
honest claims wrongly refused, and the cases that escaped. Running it changed the gate three times, which is
the argument for having written it: the text allowance used to be a substring test over the whole payload,
which let every wrong-citation case through; a tool printed an observation count with no id, so a memo was
refused for a number it could not cite; and the handbook and the criteria file were part of the allowance,
making every threshold an uncited number.

**Its two known holes** (the gate record the interface benchmark's adversarial tier cites): a correct value
from the wrong cell passes, because the memo and chat gate binds a number to its id and does not ask which
cell the id belongs to (the node gate's cell-identity rule closes this for chains, not for memos or the chat);
and negated evidence passes, because the gate checks numbers and not the sense of the sentence around them, so
a claim that negates what a correctly cited value shows is not refused. What the suite does not measure is
whether a model ever writes such a claim; that needs the model, and is the benchmark's job.

### 10.2 The analyst benchmark

The analyst benchmark ("UraniumBench", the requirement's name) is a frozen, anonymised set of cells built by
`ue bench build --version <v>` from a spec under `pipeline/configs/bench/<v>.toml` and the store (read only,
resumable, every draw from the spec's seed). Four strata, each answering a different question about an
analyst: **deposit** cells thinned to one per 10 km block so a camp counts once; **occurrence** cells that
were drilled; **negative** cells, unlabelled with at least five holes, drawn to match the positives'
exploration-effort profile so that hole counts cannot stand in for geology; and **probe** cells, never
drilled, not scored, watched. Folds are 30 km spatial blocks committed before any model sees the cells, and a
share of every stratum is held out and sealed; `open_cells` excludes them and a run that names one is refused.
The build writes `pipeline/data/bench/<v>/`: `cells.jsonl`, an evidence pack per cell (the tool results
anonymised to `b:<bench id>:...` ids and scrubbed of coordinates, sheets, file numbers, company, property and
deposit names; effort features dropped unless the spec turns them on), a map card per cell (the evidence
layers in a square window with no basemap, labels or place names, and the deposit layers never drawn), a
blind-list per cell, scrubbed passages where retrieval returns any, the out-of-fold scores, the key (hashed on
its own and never inside a pack), the held-out ids and a manifest with every file's hash. `ue bench audit`
re-hashes everything and scans every pack and passage for anything that places or names the ground; `ue bench
show` prints the counts and shortfalls. `ue bench oof-scores --write` refits the learned and effort models
the way the model search judges them and writes `derived.cell_score_oof`, the table every baseline and every
blinded session reads instead of the fitted scores.

A run is blinded through the session (section 7.2): the model sees the bench id and the scrubbed evidence,
retrieval runs under the blind-list, and the scores it may see are out-of-fold. Scoring (`analyst/score.py`)
counts abstaining: `insufficient` is a verdict the analyst may give, so precision, recall and F1 treat an
abstained cell as not positive, the abstain rate is reported with its denominator, the ranking metrics are
given over the committed cells and over every cell with an abstention at 0.5, an answer the gate refused is
an abstention too with the rejection rate beside it, probes are reported only as an abstain rate, and every
interval is a bootstrap over cells. The baselines answer the question every arm row invites: chance (analytic
and drawn), an analyst that copies the out-of-fold learned score, and the three fitted scores at a 0.5
threshold. A staged arm adds the per-stage columns: chains counted, the node gate's rejection rate over
executor attempts, the share of chains a round validated, the share the verifier refused at least once, rounds
over the chains that validated, nodes re-executed on the verifier's feedback, and how often the verifier's
label and the weighted decider agreed with the final verdict.

**Where the dataset lives.** The small, citable part of the benchmark (the cell list, the held-out list, the
key, the manifest, the baseline table, and the interface tiers) is unfinished and not committed. On the
machine that builds it `pipeline/knowledge/bench/` is a symlink to where it lives; anywhere else
`UE_BENCH_KNOWLEDGE_DIR` names the directory, and a clone without it gets one plain line naming the command
that builds what it lacks (`ue bench build --version <v>`, then `ue bench interface build --version <v>`)
and exit 2, never a traceback. The built packs, cards and passages under `pipeline/data/bench/` are a separate,
gitignored tree; the dataset copy is enough to score a run against, not to run one.

### 10.3 The interface tiers

The chat is scored on its own track, not on AUC: whether what it says is what the store says, and whether it
declines when the store cannot answer. Two of its three tiers need no model to build. `ue bench interface
build --version <v>` (spec `pipeline/configs/bench/interface-<v>.toml`) draws cells from the frozen analyst
benchmark's open split under the session's leakage rules and writes `interface/<v>/` under the dataset
directory: **tier 1**, questions the tools answer exactly (the nearest deposit or occurrence, which criteria
are unknown or not met, a criterion's state, a coverage share, a nearby count or nearest feature, the nearest
conductor, a score, a hole count, a feature value, an observation count, the cross-check crossings, thin
sampling), each with its gold as value ids, plus questions whose right answer is a refusal with a reason (not
measured here, outside the grid, a value the store holds for no cell, out of scope); and **tier 3**,
adversarial questions engineered only from failures actually observed, each naming its source by a stable
name: the gate suite's corruptions, an observation count with no citable id, a file number cited as a value
id, a negated premise, a folklore criterion phrased as fact, a request for a grade, and absence of mapping
read as absence of features, with the behaviour a correct agent shows (correct the premise and cite the id,
cite the id, refuse the citation, say unknown rather than absent, decline the grade, abstain with a reason).
The build is deterministic, `ue bench interface audit` regenerates every item from its recorded choice
against the store and compares, and `ue bench interface show` prints the counts by tier, kind, stratum,
reason and source. Neither tier has been run against an agent yet. Tier 2, the grounded-reasoning tier, needs
a rubric and a rater and is not built.

### 10.4 The leak register

The bias and leak register lives in the PRD; the ids the code enforces are: **B17**, retrieval leakage (the
blind-list, enforced twice, in the session and on the rows that come back); **B18**, score leakage (out-of-fold
scores only in a blinded session, the fold recorded); **B19**, expert anchoring (expert-tier ids marked on
every node and claim that leans on one, and the diff reported); **B22**, prompt-cache leakage (the cache key
hashes the prompt and schema texts); **B30**, the cell's own label (masked in `label_context`). Every session
manifest records its refusals per rule, so a run counts them rather than trusting that they happened.

### 10.5 What "abstain" and "withheld" mean

In every table and panel the two words mean different things. **Abstain** is a choice the agent made: the
analyst's `insufficient` verdict, a chain no round validated and no majority carried, or the interface agent's
refusal with one of the four reasons; it is counted as not positive, never as wrong, and its rate is shown with
its denominator. **Withheld** is a decision the gate made: an answer, a node, a decision or a chain that
stated something it could not back, kept in the record with the objection in its place and never offered as
argument; in the benchmark a withheld answer is scored as an abstention, and its rate is the gate rejection
rate. A criterion that is **unknown** is a third thing again: nobody measured it here, which is not a low
value and not a failure.

---

## 11. Testing and development

**The pipeline suite.** `cd pipeline && uv run pytest -q` runs the whole suite on fakes: the replay backend
(recorded envelopes under `tests/fixtures/replay/`, a miss is a loud failure, never a live call), a fake tool
world for the analyst loop and the sessions (`tests/fake_loop_world.py`, `tests/fake_session_world.py`), a
synthetic store and in-memory MCP client for the server (`tests/mcp_world.py`), a fake benchmark
(`tests/fake_bench.py`), fake pages and documents for the extractor, and a fake S3 for the seed transport.
Three markers gate what the fakes cannot cover: `network` (a grid download, live endpoints), `backend` (the
real Claude CLI) and `real_data` (the live store; `UE_REAL_DATA=1 uv run pytest -m real_data` runs the quick
model search against the store, read-only, and the live MCP test). Contract tests assert that no table mixes
tiers, that every number a tool returns carries an id, that a bare number in an MCP row fails the schema, and
that the gate suite still catches what it caught.

**The web suites.** `cd web && npm test` runs the unit tests (the contract, including the committed recorded
session; the style honesty check; the wording scan; the URL codec; the datum grid; the chat and jobs strip
renders; the tour). `npm run e2e` (`npx playwright test`) drives the map, the evidence loop, the Eval page, the
readiness page, the HUD and the ten-step tour in headless Chromium against `npm run dev`, with the digit walk
at the end of each; the local service is optional by design, and the specs assert the chain when it is up and
the offline notice when it is not. `npm run typecheck`, `npm run lint` (Biome) and `npm run validate:data` are
the other gates; `npm run build:public` builds public-safe.

**How to add an arm.** Copy `pipeline/configs/arms/v1.toml` to a new name, change exactly one thing, state
what question it answers in `notes`, and keep every switch stated (a missing one is refused) and `name` equal
to the file stem. A v0 arm has no `[arm.loop]` table; a v1 arm needs one. Run it with `ue arm run --arm <name>
--version v2` over the frozen benchmark (or `ue arm chain --arm <name> --enabled` for the dashboard), then
`ue arm table` puts it beside the others. A new loop switch is a field on `LoopSpec` in `analyst/arms.py` and
`LoopConfig` in `analyst/loop.py`, off by default, so every existing arm runs exactly as before.

**How to add a tool.** A tool is a function in `prospect/tools.py` returning a `ToolResult` whose every
number sits in `values` under an id built from the cell and the thing measured, with the row carrying the
number beside its `<key>_id`; register it in `REGISTRY` and describe it in `TOOL_HELP`. To serve it over MCP,
add its row schema to `ROWS` and its spec to `CATALOGUE` in `mcp/contract.py` (unknown and absent as the two
types, never a null) and name it in `REGISTRY_TOOLS`; to let the template plan call it, add it to the
segment's calls in `analyst/plan.py` (`ALLOWED_TOOLS` is derived from `TOOL_HELP`); to let the chat route to
it, add a topic in `interface/router.py`. The contract tests fail on a number without an id, which is the
point.

---

## 12. The complete command reference

Every `ue` command, grouped as the CLI groups them; every flag is in that command's `--help`.

**Top level**

- `ue doctor`: check the environment (the claude CLI, poppler, Apple Vision, the NTv2 grid), read-only.
- `ue fetch [--phase1 | --all-selected] [--only FILE ...] [--check/--no-check] [--workers N]`: download the selected files' report, appendix, assay and certificate objects, resumable and paced.
- `ue export-layers`: write the evidence layers (conductors, faults, host, geochemistry, surveys) for the map.
- `ue enable-files FILES... [--reason TEXT] [--max-item-mb N]`: register files outside the shortlist as dev files, never a held-out one.
- `ue run-usage [RUN_ID] [--expect-model MODEL]`: per-page duration, tokens and cost of one extraction run, from its own records.
- `ue assay-sheets [--only FILE ...] [--write/--no-write]`: read the assay spreadsheets filed with digital submissions into the native tier; no model call.
- `ue export-tiles`: one PMTiles archive per evidence source with tippecanoe, and `tiles/manifest.json`; the map falls back to GeoJSON without them.
- `ue api-spec [--out PATH]`: write the service's OpenAPI document for the web's typed client.
- `ue lock-heldout`: write `gold/heldout.lock` (file numbers, PDF sha256s, seed, timestamp); never overwrites.
- `ue render [--workers N]`: render the dev files to 200 dpi grey page images and thumbnails; never a held-out PDF.
- `ue ocr [--workers N]`: Apple Vision OCR per rendered page, with the text-layer comparison.
- `ue route`: classify rendered pages from OCR words into `data/out/routing_report.json`.
- `ue locate [--files FILE ...]`: re-locate every quote against the OCR words and report how well it went.
- `ue assemble [--files FILE ...]`: wire rows into located values and collar, lithology and assay records.
- `ue validate [--files FILE ...] [--mode enforce|shadow]`: run V01 to V20; findings flag values, nothing is dropped.
- `ue crosscheck [--files FILE ...] [--fetch-lith/--no-fetch-lith]`: match extracted collars to the two provincial compilations and build the adjudication queue.
- `ue export-reports [--files FILE ...] [--public-safe]`: write the report index, per-report records and page images (none with `--public-safe`).
- `ue export-web [--public-safe]`: write the provincial part of the web data contract into `web/public/data`.
- `ue export-eval`: write the reading run statistics for the Eval page (not accuracy: no gold labels yet).
- `ue probe-backend [--pdf PATH] [--page N] [--model ID] [--effort E]`: one real CLI call on a rendered page, saved as a replay fixture.

**`ue crs`**: `fetch-grid` (download the NTv2 grid and pin its sha256 in `grids.lock`), `shift-grid` (write the NAD27 to NAD83 shift field for the map's datum lens), `transform [--files ...]` (place each extracted collar, or refuse to and record why).

**`ue index`**: `pull [--only KEY ...]` (the provincial layers, paced, cached, single-clause queries, into `data/index`).

**`ue select`**: `shortlist` (rank the uranium-tagged files and shortlist about ten per era), `probe` (query the file listing for the shortlisted files and apply the size filters), `final` (pick the twelve files, record the constraints met, propose the split and the first subset), `check` (re-run the post-fetch scanned and datum check from what is on disk).

**`ue cache`**: `rekey [--dry-run]` (move cached calls written under the old key to the current one), `stats` (records by task and version).

**`ue spend`**: `show` (spend per backend family against its ceiling, and the total against `UE_MAX_SPEND_USD`).

**`ue bench`**: `build [--version V]` (write `data/bench/<V>/` from the spec and the store, resumable), `audit [--version V]` (re-hash every file and scan every pack and passage for anything that places or names the ground), `show [--version V]` (counts per stratum and split, shortfalls, hashes), `oof-scores [--seed N] [--write/--no-write]` (out-of-fold learned, effort and criteria scores for every scorable cell under 30 km spatial folds), and `interface build|audit|show [--version V]` (the interface track's tiers 1 and 3).

**`ue arm`**: `run [--version V] [--arm A] [--budget-usd N] [--workers N] [--cells ID ...] [--track/--no-track] [--backend claude|auto|replay] [--resume RUN]` (one arm over a frozen benchmark's open cells; exit 3 on the budget, 75 on a usage limit), `chain [--arm A] [--cells ID ... | --enabled] [--budget-usd N] [--workers N] [--track/--no-track] [--backend ...]` (the staged analyst over real cells for the dashboard, chains into the agent tier), `score --run RUN [--version V]` (score a run against the key), `regate --run RUN ... [--version V]` (re-apply the current gate to stored answers and re-score, no model call), `table [--version V]` (every arm's latest run beside the baselines into `table.json` and `derived.metric`), `baselines [--version V]` (the baseline rows alone).

**`ue run`**: `phase1 [--workers N] [--fetch-all/--no-fetch-all]` (select, fetch, lock, render, OCR, route; idempotent), `phase2 [--config ID] [--files ...] [--max-calls N] [--fetch-lith/--no-fetch-lith] [--public-safe]` (extract, assemble, validate, crs, crosscheck, store, export-reports).

**`ue replay`**: `pack --run RUN` (bundle a run's recorded envelopes into `tests/fixtures/replay/`).

**`ue store`**: `rebuild` (flatten every reading stage into the tiered store plus the two GeoParquet layers), `snapshot [--list]` (a hashed manifest of the store a run can cite), `register-layers` (every pulled layer in `native.layer` with its payload hash), `lineage` (every layer, feature and verified source walks back to a hashed pull; exit 1 on a break), `migrate [--dsn DSN]` (the Alembic migrations on the serving database), `sync-pg [--dsn DSN]` (copy every tiered table into Postgres, set the cell geometry, audit the tiers there), `pg-audit [--dsn DSN]` (the tier audit on the serving database), `audit` (no table mixes provenance tiers), and `seed pack|verify|unpack|push|pull|ensure` (section 2.1).

**`ue prospect`**: `inventory [--verbose]` (the data sources, what each bears on, what is missing), `grid [--cell-m N] [--buffer-m N]` (the analysis grid), `features [--only KEY ...]` (per-cell features with their coverage), `rasters [--start] [--end] [--max-cloud] [--only s2|dem]` (per-cell water, vegetation, bare ground and terrain from public COGs), `corpus [--files N] [--region-only/--province] [--download/--on-disk-only]` (index the assessment corpus), `retrieve [QUERY] [--lon] [--lat] [--radius-km] [-k] [--stats]` (search it, spatial filter first), `labels` (positive cells, camps and spatial folds), `score [--model criteria|learned|effort|all]` (score every cell), `headline [--seed] [--boot] [--write/--no-write] [--track/--no-track] [--snapshot HASH]` (the re-test with matched background and thinned positives, all folds, intervals), `modelsearch [--seed] [--boot] [--quick] [--write/--no-write] [--track/--no-track] [--snapshot HASH]` (candidates, ablations, block sizes, the served-model decision), `hindcast [--cutoff YEAR ...] [--min-confidence high|medium] [--write/--no-write] [--track/--no-track] [--snapshot HASH]` (later discoveries ranked by models frozen at a cutoff), `memo [--cell ID] [--model] [--effort] [--max-budget-usd] [--mode panel|oneshot] [--roles ...]` (the proponent, skeptic and adjudicator over one cell, gated), `serve [--port] [--model] [--effort] [--backend openai|claude|auto] [--host] [--web-dist DIR]` (the evidence record, the chat, the MCP route and the jobs), `record [--cell] [--backend] [--model] [--effort] [--budget-usd] [--job-budget-usd] [--job/--no-job] [--refresh] [--out]` (the tour's session, asked for real and written for the walkthrough), `gate-eval [--cells IDS] [--seed] [--per-cell N] [--out PATH]` (honest and corrupted claims put to the fabrication gate), `table` (write `knowledge/data_readiness_table.md`), `readiness` (how much of the basin each feature covers), `drift [--snapshot HASH]` (feature distributions now against a snapshot's; exit 1 on drift), `gate` (the five-column readiness gate; exit 1 when red), `export` (the readiness scorecard, the coverage layer, the scores and the Eval blocks into `web/public/data/prospect`).

**`ue openai`**: `models [--filter TEXT]` (what the key can reach; free), `budget` (what the chat has spent and what is left).

**`ue mcp`**: `serve [--stdio | --http] [--host] [--port] [--public-safe] [--runs-dir DIR]` (the tools, resources and prompts over one transport), `tools [--json]` (the catalogue as served), `key [--scopes read,record,run]` (mint a key and print its register entry; printed once, never stored).

**`ue interface`**: `ask --cell ID -q QUESTION [-q ...] [--backend] [--model] [--effort] [--budget-usd] [--insight-store FILE] [--requested-by LABEL] [--refresh] [--json]` (one conversation with the interface agent from the terminal).

**`ue extract`** `[--config ID] [--split dev|heldout] [--files ...] [--max-calls N] [--pages N] [--max-pages-per-file N] [--dry-run] [--unlock-heldout] [--replay] [--retry-failed]` (the batch reader), and `ue extract agent [--config] [--files ...] [--page FILE:PAGE ...] [--pages N] [--max-pages-per-file N] [--second-model ID] [--second-effort E] [--backend auto|claude|replay] [--budget-usd N] [--agree-only] [--no-agree] [--store FILE] [--no-store] [--resume RUN] [--second-timeout-s N] [--dry-run]` (the reading loop: locate, read, validate, agree, file).

**`ue gold`**: `key FILE PAGE` (the empty gold skeleton for one page, never prefilled from a model), `score --run RUN [--which a|b] [--json]` (precision and recall against every keyed gold page the run read; zero pages while none is keyed).
