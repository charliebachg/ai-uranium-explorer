# Legacy Reader

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

    # pipeline
    cd pipeline
    uv sync
    uv run lr doctor                 # environment checks (claude CLI, poppler, Apple Vision, NTv2 grid)
    uv run lr crs fetch-grid         # download and pin the NAD27 to NAD83 grid
    uv run lr crs shift-grid         # export the shift grid used by the map's datum lens
    uv run lr index pull             # provincial layers (paced, cached)
    uv run lr export-web             # write the provincial part of the web data contract
    uv run lr export-layers          # thin the evidence layers (conductors, faults, host, geochemistry) for the map
    uv run lr run phase1             # select 12 files, fetch, lock held-out, render, OCR, route
    uv run pytest -q

    # web
    cd web
    npm install
    npm run dev                      # http://localhost:5173
    npm test                         # unit tests (contract, style honesty, wording, URL, datum grid)
    npx playwright test              # map, evidence loop and spike checks in headless Chromium

    # the agent (optional: the map, the scores and the walkthrough all work without it)
    cp .env.example .env             # then put an OpenAI key in it; .env is gitignored
    cd pipeline
    uv run lr openai models          # free: what the key can reach, before spending anything
    uv run lr openai budget          # cumulative spend and what is left of the ceiling
    uv run lr prospect serve         # localhost:8787, the evidence record and the live chat; /docs for the OpenAPI page

    # the serving stack (Postgres + PostGIS, MinIO, Redis) and the containerised app
    docker compose up -d db          # needs Docker; the PostGIS image is multi-arch
    cd pipeline
    uv run lr store migrate          # Alembic: the tiered schema in Postgres, plus the cell geometry
    uv run lr store sync-pg          # copy every tiered table from DuckDB, set geometries, audit the tiers
    uv run lr export-tiles           # PMTiles per evidence layer (needs tippecanoe); the map falls back to GeoJSON without them
    uv run lr api-spec               # write the OpenAPI document the web's typed client is generated from
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
status: the layer declares itself with `metadata["lr:score"]`, the honesty check in `composeStyle.ts` allows
that one exception and refuses any other, the banner at the top changes to say so, and a legend appears.

Click a cell and the rail on the right holds its evidence — the three scores, what each criterion contributed,
how close the nearest labelled deposit is — and a conversation about it. The agent reads the same tools the
panel is drawn from, and every number it states is checked against those tool values before the answer is
shown; an answer that fails is withheld, with the objection shown in its place. The chat needs
`lr prospect serve`; everything else on the map is static files.

Other views: `/data` (how much of the grid each feature actually covers), `/eval` (what the run read, what the
checks caught, and how the fabrication gate scores on an adversarial suite) and `/limits` (what this demo can
and cannot claim, carried from research report 05). `?motion=0` turns off every animation, `?perf=lite` drops
backdrop blur.

## Data sources

Provincial layers come from the Government of Saskatchewan under the Standard Unrestricted Use Data Licence
v2.0 (commercial use with attribution). The Geoscience Data System drillhole and assessment-file services state
no licence, so they are used but not redistributed. The NTS grid is Open Government Licence - Canada. Basemap
tiles are OpenFreeMap (MIT, OpenStreetMap data); relief is Mapzen Terrain Tiles. Every source, its licence and
whether this demo may redistribute it is listed in the app under "Data sources and licences".
