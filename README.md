# AI Uranium Explorer

Reads public Saskatchewan uranium assessment reports into drillhole tables where every value carries its
provenance (file hash, page, box, verbatim quote), and measures how often the reading is wrong.

It is a demo built from the research in `../research/` (see `05-demo-scope.md`, the spec this follows), on public
data only. It proposes no drill targets and makes no geological judgement.

## What it does not claim

- No drill targets, prospectivity scores or "high potential" wording. Colour on the map shows extraction status
  or data source, never grade or potential.
- No interpretation of geology. Values are shown as printed, with the unit and basis the page states.
- No accuracy figure without the gold-set size, who labelled it and the split.
- Coverage is always stated as "12 of 5,822 uranium-tagged files", never "the Saskatchewan archive".
- Assessment report PDFs carry no named licence: they are read locally and never redistributed. Page images stay
  out of git, and a public build ships without them.

## Layout

    pipeline/   Python 3.13 (uv). Pulls provincial data, selects and downloads reports, renders and OCRs pages,
                extracts values with a vision model, validates, transforms coordinates, cross-checks, scores,
                and exports the web data contract.
    gold/       Gold labels (append-only event log) and the held-out lock.
    web/        Vite + React + MapLibre app: the map, the evidence panel and the eval page.

## Running it

    # a fresh clone: the analytics store (pipeline/data/ue.duckdb) is gitignored, so first pull the seed pack
    # from the URL the team publishes it at (an s3://bucket/prefix or an https:// directory; the URL is a
    # deployment choice, ask for it), into pipeline/data/seed/<hash>/ with data/seed/latest pointed at it:
    cd pipeline && uv sync && uv run ue store seed pull <url>    # reads manifest.json then every file, sha256 checked; refuses a mismatch
    cd .. && docker compose up -d --build     # the app's first start runs `ue store seed ensure`: unpacks the pack into
                                              # pipeline/data/ue.duckdb, verifying every table's hash and row count, and serves on :8787
    # (with UE_SEED_URL set for the app, as the compose file sets it to the compose MinIO, ensure pulls by itself)
    # or without Docker
    cd pipeline && uv run ue store seed unpack data/seed/latest && uv run ue prospect serve
    # whoever publishes: pack the store, then push it (the compose MinIO with UE_S3_ENDPOINT=http://127.0.0.1:9000, or any S3)
    uv run ue store seed pack && uv run ue store seed push data/seed/latest --to s3://seeds/latest
    # the public pack (9 MB) rebuilds the derived tier, the layer registry and the scenes; the read and agent
    # tiers come back as empty tables, so there are no document readings or chains until the private pack
    # (team only, 22 MB: page text and quotes never leave the team) is unpacked instead

    # pipeline
    cd pipeline
    uv sync
    uv run ue doctor                 # environment checks (claude CLI, poppler, Apple Vision, NTv2 grid)
    uv run ue crs fetch-grid         # download and pin the NAD27 to NAD83 grid
    uv run ue crs shift-grid         # export the shift grid used by the map's datum lens
    uv run ue index pull             # provincial layers (paced, cached)
    uv run ue export-web             # write the provincial part of the web data contract
    uv run ue export-layers          # thin the evidence layers (conductors, faults, host, geochemistry) for the map
    uv run ue run phase1             # select 12 files, fetch, lock held-out, render, OCR, route
    uv run pytest -q

    # data ownership and the tracked evaluations (Phases 2 and 3 of the PRD)
    uv run ue store register-layers  # every pulled layer in native.layer with the hash of its payload
    uv run ue store lineage          # every layer, feature and verified source walks back to a hashed pull; exit 1 on a break
    uv run ue store snapshot         # a hashed manifest of the store: row counts, layer hashes, feature quantiles
    uv run ue store seed pack        # the store as Parquet under data/seed/<hash>/ with a manifest, and data/seed/latest pointed at it; public: only tables whose every source the inventory marks redistributable
    uv run ue store seed pack --private   # every table, the read and agent tiers included: page text and quotes never leave the team
    uv run ue store seed verify data/seed/latest        # every sha256 and row count against the manifest, and the manifest against its own address; exit 1 on a mismatch
    uv run ue store seed unpack <pack> --into <file>    # rebuild a store from a pack: schema.sql, load, tier audit, recount; refuses an existing file
    uv run ue store seed push <pack> --to s3://bucket/prefix   # verify, then upload under <prefix>/<hash>/ with the manifest last, and point <prefix>/manifest.json at it (UE_S3_ENDPOINT for MinIO; credentials from the AWS env names)
    uv run ue store seed pull <url> [--into DIR]        # s3:// or https://: the manifest, then every file with its sha256 checked; refuses on a mismatch and leaves nothing behind
    UE_OTLP_ENDPOINT=http://127.0.0.1:4318 uv run ue arm run ...   # any traced run also exports its spans over OTLP/HTTP to that backend (Jaeger, Tempo, a hosted one); unset, spans.jsonl and the MLflow mirror are all there is
    UE_REAL_DATA=1 uv run pytest -m real_data           # the quick model search against the store, read-only, each arm against its stored interval (about a minute); CI unpacks the public pack first
    uv run ue prospect gate          # the five-column readiness gate (present, licensed, covers, servable, versioned); exit 1 when red
    uv run ue prospect drift         # feature distributions now against the latest snapshot; exit 1 on drift
    uv run ue prospect headline --snapshot <hash>     # Phase 0 re-test, pinned to the store it names
    uv run ue prospect modelsearch --snapshot <hash>  # candidates, ablations, block sizes; every arm an MLflow run
    uv run ue prospect hindcast --snapshot <hash>     # later discoveries ranked by models frozen at a cutoff
    uv run ue prospect export        # the data page and the Eval page's blocks, every number a stored value
    zsh scripts/read-enabled.sh      # the Opus-only read of the enabled cells' drilling files, through to the dashboard; resumable, run it in a plain terminal
    uv run mlflow ui --backend-store-uri sqlite:///data/mlflow.db   # the runs and the registry (installed with --extra mlflow)

    # the analyst benchmark and the staged analyst (PRD §8.4, §9.4)
    uv run ue bench build --version v2       # the frozen benchmark (v2: the same 163 cells as v1 on the corrected store); `ue bench audit` re-hashes and scans it
    uv run ue bench interface build --version v1   # the interface track's mechanical tiers (PRD §D.3.1 role 2): tier 1 exact answers and abstentions, tier 3 observed failures; gold by code, no model; `audit` regenerates every item from the store, `show` prints the counts
    uv run ue arm run --arm v0 --version v2  # one arm over the open cells: cached, budgeted, traced, scored; arms live in configs/arms/
    uv run ue arm run --arm v1               # the staged loop: template plan, executor per criterion, gate, verifier, both deciders
    uv run ue arm run --arm v1-scoped        # the same loop with each executor staged only its own criterion's rows of the tables (v1-skipunmeasured, v1-batch and v1-scoped-batch are the other cost switches)
    uv run ue arm chain --enabled --arm v1   # chains for the enabled cells into the agent tier, shown on the dashboard's evidence panel
    uv run ue arm chain --enabled --arm v1-cheap --backend auto   # a vendor/model id in an arm goes to OpenRouter (OPENROUTER_API_KEY in .env), a claude-* id to the CLI
    uv run ue arm run --arm v1-anthropic-or --backend auto --workers 20 --budget-usd 60 --resume <run id>   # the same two models pay-per-token, many cells at once, carrying a stopped run forward
    uv run ue arm table                      # every arm's latest run beside the random, learned, effort and criteria baselines, with the staged loop's per-stage columns for the v1 arms

    # the extractor as an agent (PRD §8.2): locate → read → validate → agree → file, one page at a time, resumable
    uv run ue extract agent --config opus1-assay --agree-only --backend auto --budget-usd 0.50 --page 74H16-0034:27   # a second family (z-ai/glm-5.3-flash on OpenRouter) over pages already read on Opus; disagreements go to the review queue in read.review_item
    uv run ue extract agent --files 74H16-0034 --budget-usd 5 --store /tmp/copy.duckdb   # fresh pages too (the reader on the CLI); --store files the queue somewhere other than data/ue.duckdb, --no-store keeps it in the run directory, --resume <run id> carries finished stages forward
    uv run ue gold key 74H16-0034 27         # an empty gold skeleton under gold/pages/ for a person to key from the page image; never prefilled from a model
    uv run ue gold score --run <run id>      # precision and recall against every keyed gold page the run read, per field type, with the denominators; reports zero gold pages while none is keyed

    # web
    cd web
    npm install
    npm run dev                      # http://localhost:5173
    npm test                         # unit tests (contract, style honesty, wording, URL, datum grid)
    npx playwright test              # map, evidence loop and spike checks in headless Chromium

    # the agent (optional: the map, the scores and the walkthrough all work without it)
    cp .env.example .env             # then put an OpenAI key in it; .env is gitignored
    cd pipeline
    uv run ue openai models          # free: what the key can reach, before spending anything
    uv run ue openai budget          # cumulative spend and what is left of the ceiling
    uv run ue prospect serve         # localhost:8787, the evidence record and the live chat; /docs for the OpenAPI page
    uv run ue prospect serve --backend auto   # the interface agent on its cheap OpenRouter model (UE_INTERFACE_MODEL)
    uv run ue interface ask --cell 0201_0072 -q "which criteria are unknown here?" --budget-usd 0.50   # one routed turn from the terminal
    UE_MCP_KEYS=<key>:read,record uv run ue prospect serve   # with a key register the API needs a key too: X-Api-Key or a bearer; see "Keys, roles and jobs"

    # the same tools over MCP (PRD §E.3): a geologist's own Claude Code or Cursor session as the client
    uv run ue mcp tools              # the catalogue as served: 15 tools, their kind and scope; --json for the tools/list document
    uv run ue mcp serve --stdio      # what a client launches: the server on stdin and stdout (see the .mcp.json below)
    uv run ue mcp serve --http       # streamable HTTP on :8788/mcp when the API is not running; `ue prospect serve` mounts it at :8787/mcp
    uv run ue mcp key --scopes read  # mint a key and print its UE_MCP_KEYS entry; without a register only local clients are served

A client connects by launching the stdio server. Claude Code reads `.mcp.json` in the project root, Cursor
`.cursor/mcp.json`; the same shape works in both:

    {"mcpServers": {"ai-uranium-explorer": {"command": "uv", "args": ["run", "--directory", "pipeline", "ue", "mcp", "serve", "--stdio"]}}}

With no `UE_MCP_KEYS` register a stdio client and a loopback HTTP client have every scope. To hand out
scopes, set `UE_MCP_KEYS=<key>:<scope>[,<scope>];<key>:...` in the server's environment (a key is any string
without `:`, `;`, `,` or white space; `ue mcp key` mints one); an HTTP client then sends
`Authorization: Bearer <key>` (or `X-Api-Key: <key>`), and a stdio client puts its key in `UE_MCP_KEY`. Scopes:
`read` (open a session, the eight reads, `hole_crosscheck`, `check_claims`, `abstain`, `job_status`, every
resource and prompt), `record` (`record_insight`, which writes the expert tier), `run` (`run_analyst`, which
starts an analyst job on the API's runner and returns its id; only in the API process, where `/mcp` has the
runner). `tools/list` shows a key only the tools its scopes carry. `--public-safe` (or `UE_MCP_PUBLIC=1`) never
serves report text or a layer the inventory marks non-redistributable.

**Keys, roles and jobs (PRD §A.2).** The same `UE_MCP_KEYS` register is the API's: a role is a set of the MCP
scopes, so one key opens the same things over both.

| Role | Scopes | What it opens on the API |
|---|---|---|
| `viewer` | `read` | the record, the stored conversations, a job's row (`GET /api/jobs/{id}`, `GET /api/cell/{id}/jobs`) |
| `geologist` | `read`, `record` | the chat (`POST /api/chat`, `/api/chat/stream`); every turn records who asked |
| `admin` | `read`, `record`, `run` | `POST /api/jobs` with kind `analyst`, and cancelling one |

With no register a loopback client is `local` with every role, so a local prototype needs no key; with one,
every call that writes carries `X-Api-Key: <key>` (the site's Key button keeps it in the browser) or a bearer
header, and `GET /api/whoami` says what a key holds. A 401 or 403 carries a plain reason and never a key.
Anything over a second is a job with a durable row in `agent.job`: `POST /api/jobs {kind, cell_id, args}`
answers 202 with the row, `GET /api/jobs/{id}` polls it (status queued, running, done, failed or cancelled,
the stages reached, the result), `POST /api/jobs/{id}/cancel` stops it at its next model call. The `analyst`
kind runs the staged loop on one enabled cell (any other cell is refused with PRD §9.4's reason), arm
`v1-openrouter` through the router by default, `budget_usd` 0.50 by default and 2.00 at most inside a
per-process budget (`UE_JOB_SESSION_BUDGET_USD`, default 5), and publishes the chain exactly as
`ue arm chain` does; the chat's "run the analyst" intent submits the same job and the agent rail's jobs strip
polls it, refreshing the evidence panel's chains when it finishes. A job that was running when the process
restarted is marked failed with that reason.

    # the serving stack (Postgres + PostGIS, MinIO, Redis) and the containerised app
    docker compose up -d db          # needs Docker; the PostGIS image is multi-arch
    cd pipeline
    uv run ue store migrate          # Alembic: the tiered schema in Postgres, plus the cell geometry
    uv run ue store sync-pg          # copy every tiered table from DuckDB, set geometries, audit the tiers
    uv run ue export-tiles           # PMTiles per evidence layer (needs tippecanoe); the map falls back to GeoJSON without them
    uv run ue api-spec               # write the OpenAPI document the web's typed client is generated from
    cd .. && docker compose up -d --build app   # the site and the API in one container on :8787, store mounted from pipeline/data

`http://localhost:5173/?fixture=1` shows the evidence panel on a development fixture (values hand-keyed from
real public pages; the app shows a FIXTURE badge whenever it is used).

## Using the app

A plain visit to `/` opens on the globe, flies to the province and settles on three counters. Any query
parameter (a deep link, or `?intro=0`) skips that, and so does a reduced-motion setting.

| Key | Does |
|---|---|
| `G` | the walkthrough: six steps, each setting up its own screen, ending with a recorded conversation with the agent |
| `⌘K` / `Ctrl-K` | command palette: reports, holes, layers, basemaps, tools; highlighting a report or hole peeks at it on the map and leaving restores the camera |
| `T` | timeline: one bar per year by data source, a scrubber that sets the latest year drawn, and playback |
| `D` / `M` | cursor datum lens, and where NAD27 collars would land if read as NAD83 |
| `Esc` | one step back along the evidence trail (value, hole, report, selection) |

**The layer rail is grouped by what the evidence is for**, following MineTRACE: the prospectivity surface,
then pathway and trap (EM conductors, faults, the mapped graphitic host), then geochemistry (lake sediment,
lake water, radioactive boulders), then *where people already looked* (drillholes and survey footprints) kept
deliberately apart, because separating the rock from the exploration history is the argument this demo makes.
The geophysics group holds no layers at all: magnetics, gravity and radiometrics are not published as grids
for Saskatchewan, so they are listed as named gaps rather than quietly left out. Each evidence layer is
fetched the first time it is switched on — together they are 16 MB, and a reader who never opens them should
not pay for them.

**Scores and the agent are on the same map.** Turn on *Prospect scores* in the layer rail to draw the 2 km
analysis cells, coloured by whichever of the four scores is picked — criteria, learned, exploration effort, or
learned minus effort. That is the one place in this app where colour carries a value rather than a source or a
status: the layer declares itself with `metadata["ue:score"]`, the honesty check in `composeStyle.ts` allows
that one exception and refuses any other, the banner at the top changes to say so, and a legend appears.

Click a cell and the rail on the right holds its evidence — the three scores, what each criterion contributed,
how close the nearest labelled deposit is — and a conversation about it. The agent reads the same tools the
panel is drawn from, and every number it states is checked against those tool values before the answer is
shown; an answer that fails is withheld, with the objection shown in its place. Behind the panel is the
interface agent (PRD §8.3): a router classifies each question into a fixed kind and a plan written in Python
fetches its evidence, a refusal is recorded with its reason rather than guessed past, a geologist's own
statement can be recorded into the expert tier and cited from then on, and the analyst can be invoked on an
enabled cell as a job whose verdict comes back beside the stored chain without the insight. The chat needs
`ue prospect serve`; everything else on the map is static files.

Other views: `/data` (how much of the grid each feature actually covers), `/eval` (what the run read, what the
checks caught, and how the fabrication gate scores on an adversarial suite), `/limits` (what this demo can
and cannot claim, carried from research report 05) and `/review` (the extractor's review queue: every value the
second reader family disagreed with the first about, both readings beside the page crop, accepted or rejected
with the geologist key; the decision is recorded with the key's label and never rewrites a reading).
`?motion=0` turns off every animation, `?perf=lite` drops backdrop blur.

## Data sources

Provincial layers come from the Government of Saskatchewan under the Standard Unrestricted Use Data Licence
v2.0 (commercial use with attribution). The Geoscience Data System drillhole and assessment-file services state
no licence, so they are used but not redistributed. The NTS grid is Open Government Licence - Canada. Basemap
tiles are OpenFreeMap (MIT, OpenStreetMap data); relief is Mapzen Terrain Tiles. Every source, its licence and
whether this demo may redistribute it is listed in the app under "Data sources and licences".
