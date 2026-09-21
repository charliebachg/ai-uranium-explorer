# AI Uranium Explorer

Uranium prospectivity over public Saskatchewan data, with agents that can only cite stored values. Every
number an agent states resolves to a value in the store, or the answer is withheld.

Status: prototype on public data. It gives no drilling advice. No geologist has reviewed it.

![The dashboard: score cells over the basin, one cell selected, the analyst's chain in the evidence rail](docs/screenshots/dashboard.png)

## What it does

- Reads public GIS layers and scanned assessment reports into a store with five tiers: native, read, derived,
  agent, expert. Every read value keeps its page, box and quote.
- Scores 2 km cells over the Athabasca Basin three ways: a criteria score, a learned model, and an effort model
  built from where people already drilled. Measured on this grid under spatial folds, with negatives matched to
  the positives' drilling effort and positives thinned to one per block, the effort model scores PR-AUC 0.178
  and the geology model 0.111, intervals apart; the run is on the Eval page.
- Runs an analyst agent on a cell: plan, execute per criterion, gate every node, verify with repair rounds,
  decide, publish.
- Answers questions through an interface agent. It routes each question to a fixed kind, refuses with a reason
  when the store cannot answer, and can start an analyst job.
- Checks every number before it is shown. A number without a stored value id is withheld, and the objection is
  shown instead.
- Serves the same tools over MCP, an API with roles and background jobs, and a dashboard with a guided tour.

![The interface agent: a routed question, cited values as chips, a refusal with its reason](docs/screenshots/interface-agent.png)

## Agentic design

![What runs, and how it is judged](docs/figures/evaluation-flow.svg)

The single-call arms, the cheap staged arm and part of the strong-stack run are scored on the Eval page; the
ablation matrix is not complete. Top row: what runs for one cell. Bottom row: what every answer is judged against.

- A frozen benchmark: stratified cells, labels masked, the cell's own files blind-listed, one hashed manifest.
- Baselines on the same cells: random, criteria, learned, effort null. An agent that only matches the effort
  null has read drilling history, not geology.
- Scores with intervals: PR-AUC, F1, abstention rate, gate-rejection rate, cost per chain. An abstention is
  never a positive.
- Per-stage metrics from traces: gate refusals, verifier catch rate, rounds to valid, decider agreement.
- Ablations as arm files: single call against the staged loop, planner, verifier, rounds, model pairing.
- A gate suite with no model in the loop, and interface tiers with gold computed by code.
- No human rater yet.

## Quick start

Needs Docker, or Python 3.13 with uv and Node 22. The analytics store is not in git and no public seed pack
is published yet, so a fresh clone runs the static site (the map, the scores, the Data, Eval and Limits
pages, the tour) and both test suites; the evidence panel, the chat, the jobs and the analyst need a pack
placed at `pipeline/data/seed/latest`.

    cd pipeline && uv sync && uv run ue store seed pull <url>

With Docker:

    docker compose up -d --build        # http://localhost:8787

Without Docker:

    cd pipeline && uv run ue store seed unpack data/seed/latest && uv run ue prospect serve
    cd web && npm install && npm run dev    # http://localhost:5173

The chat needs an OpenRouter key in `.env` (see `.env.example`). The map, the scores and the tour work with
nothing running.

## Repository

    pipeline/             Python: store, features, scores, agents, API, MCP server. Command: uv run ue
    pipeline/knowledge/   criteria table, handbook, data inventory, enabled cells, dated discoveries
    pipeline/configs/     analyst arms, benchmark specs, reader configurations
    pipeline/migrations/  serving-database schema
    web/                  Vite + React + MapLibre dashboard
    docs/                 screenshots and figures

GUIDE.md explains how it works. PRD.md states what it is, what it must do, and the backlog.

## Data and licences

Every source and its licence is listed in `pipeline/knowledge/data_inventory.toml` and on the app's data
page. Provincial layers: Government of Saskatchewan open data licence. NTS grid: Open Government Licence,
Canada. Imagery: Copernicus and USGS. Basemap: OpenFreeMap on OpenStreetMap data. Assessment report PDFs have
no named licence: they are read locally, never served, and their page images stay out of git. The public seed
pack holds only redistributable tables.

## Licence

MIT, see LICENSE. The data keeps the licences of its sources.
