# AI Uranium Explorer — what it is and how it works

A working guide to the dashboard: every control on screen, and what actually happens to a cell when you click
it. Numbers here are the ones the current export measured, not round figures.

---

## 1. What this is, in one paragraph

It reads public Saskatchewan uranium records — provincial GIS layers and scanned assessment reports — turns
them into a checked, traceable table, scores 2 km cells over the Athabasca Basin three different ways, and puts
an LLM agent beside each cell that can only speak from the same tools the scores were computed from. The
argument it makes is not "here is good ground". It is: **here is how much of a prospectivity score is explained
by where people already drilled**, and here is a way to let a model talk about evidence without letting it
invent numbers.

**What it is not.** It is not a targeting tool, it makes no geological judgement, and no geologist has reviewed
any of it. Every score on the map is retrospective. See §8.

---

## 2. Running it

    cd pipeline && uv sync
    uv run lr store seed unpack data/seed/latest   # a fresh clone only: rebuild the analytics store from the seed pack, every table's hash verified (the README says where the pack comes from)
    uv run lr prospect serve          # localhost:8787 — the evidence record and the live chat
    cd ../web && npm install && npm run dev    # http://localhost:5173

Or, with Docker, `docker compose up -d --build` from `legacy-reader/`: the app unpacks the seed pack itself on
first start when `pipeline/data/lr.duckdb` is missing, and serves the built site and the API on :8787.

The map, the scores, the layers and the guided walkthrough are **static files** and work with nothing running.
Only the evidence panel and the live chat need `lr prospect serve`; when it is down both panels say so and name
the command, rather than erroring.

For the chat on OpenAI, put a key in `legacy-reader/.env` (gitignored) and restart the service — it defaults to
`--backend openai`. Check `uv run lr openai models` first (free) and `uv run lr openai budget` any time; spend
is capped cumulatively on disk, default $2.00.

---

## 3. The dashboard, control by control

### Top bar

| Item | What it does |
|---|---|
| `4 of 5,822 uranium-tagged files read` | The honest coverage figure: how many assessment reports have actually been read end to end, against how many exist. |
| **Map / Data / Eval / Limits** | The dashboard, then three reference pages (§7). |
| **Search** (`⌘K`) | Command palette: reports, holes, layers, basemaps, tools. Highlighting a report peeks at it on the map and restores the camera when you leave. |
| **Tour** (`G`) | The six-step guided walkthrough, ending on a recorded conversation with the agent. |

### The honesty banner (top right)

Normally it says colour encodes extraction status, not prospectivity. **The moment you switch on the score
cells it changes**, because that sentence stops being true — those cells are the one layer allowed to colour by
a value. This is enforced in code, not by convention (§5.4).

### Layer rail (left)

Grouped the way MineTRACE groups evidence, with one deliberate addition.

- **Scores** — *Prospect scores* plus a four-way picker: **Criteria / Learned / Effort / Learned − effort**.
- **Pathway and trap** — EM conductors, faults and lineaments, graphitic or pelitic host.
- **Geochemistry** — lake sediment samples, lake water samples, radioactive boulders.
- **Where people already looked** — compilation collars, GeoDS drillholes, airborne and ground survey
  footprints. *This group exists because separating rock from exploration history is the whole argument.*
- **Known uranium** — deposit footprints, occurrences.
- **Base** — Athabasca basin outline, NTS sheets, relief shading.
- **Geophysics** — **empty on purpose.** Magnetics, gravity and radiometrics are struck through and labelled
  "no public grid". They are the three layers a prospectivity map usually leans on, and none is published as a
  grid for Saskatchewan. A gap you can see beats a smooth map that quietly omits it.
- **Basemap** (footer) — Ink / Streets / **Satellite** / None, and *Data sources and licences*.

  *Satellite* is a Sentinel-2 cloudless mosaic from EOX — the same satellite this project computes its cover
  features from, so the picture under the cells is genuinely one of the inputs rather than decoration. It costs
  almost nothing: a whole-basin view fetches **24 tiles, 0.08 MB**, against 4.9 MB for the score cells alone.
  A dark scrim sits between the imagery and the data so the score ramp, which runs dark to pale, stays
  readable. Licence is CC BY-NC-SA 4.0 (attribution required, non-commercial); nothing is redistributed —
  tiles are fetched by the viewer's browser — and the terms are stated in *Data sources and licences*.

**Light theme.** The toggle sits beside the basemap picker, and `?t=light` carries it in a link. It is not just
a palette swap: the map's colours are compiled into a MapLibre style, so switching rebuilds the style. Three
things move with it — the basemap pairs to a pale one (a light page over a black map reads as broken), the
score ramp **flips** so high scores are dark ink rather than pale, and the relief shading lightens (its dark
ramp painted sea level near-black and turned every lake navy). What does **not** move are the source and status
hues: compilation blue, GeoDS green, flag amber, miss pink stay put, because a data source that changed colour
with the theme would make the legend a lie.

Each evidence layer is **fetched the first time you switch it on**. Together they are 16 MB; a viewer who never
opens them never pays for them.

### The map

Click a cell to open it in the agent rail. Hovering names whatever is under the cursor. The score legend sits
bottom-left whenever the cells are on, and links to the fold tests.

**Datum tools** (`D`, `M`) are a side exhibit: the NAD27→NAD83 shift is 34.2 m in the eastern basin and 50.8 m
near Patterson Lake, and *Misread datum* shows where collars would land if that shift were skipped — a real
failure mode in legacy data, not a hypothetical.

**Timeline** (`T`) filters drillholes by year and says what it is hiding.

### Agent rail (right)

Opens on a selected cell. Header shows the cell id and its centre; under it, the three scores from the exported
row so the rail still says something with the service down.

- **Evidence tab** — the three model scores with their known-share and applicability; the criteria breakdown,
  where *unknown* is visually distinct from *not met* (no bar, amber, "not measured"); the nearest labelled
  deposit; and any memos three agents wrote about this cell, including rejected ones.
- **Chat tab** — a conversation about that same cell. Question right, answer left, citations under each answer
  as value chips, the tools it called, and what the turn cost. An answer that fails the check is **kept in the
  transcript, marked withheld, with the checker's objection in its place**.

---

## 4. Cells to try

| Cell | What it shows | Numbers |
|---|---|---|
| `0201_0072` | **The leakage cell.** It *is* the Horseshoe deposit. The null model that knows only where people drilled beats both real models. Three memos and the recorded conversation live here. | criteria 0.924 · learned 0.683 · **effort 0.976** |
| `0152_0083` | **The interesting one.** High knowledge-driven score with almost no exploration history. The panel disagrees with itself: proponent *supports a closer look*, skeptic *insufficient evidence*. | criteria 0.901 · learned 0.025 · effort 0.034 · known 0.667 |
| `0169_0116` | **Geology over effort.** 30 km from any labelled deposit. Switch to *Learned − effort* and this is where the map goes blue. | learned 0.830 · effort 0.041 |
| `0000_0053` | **The coverage gap.** No score at all; 16 of 24 features measured. Every memo says *insufficient evidence*. Ask "what is actually measured here?" | known share 0.40 |
| `0015_0065` | **The adjudicator overruling.** Well sampled, modest score; proponent says look closer, adjudicator says insufficient. | criteria 0.385 · known 0.933 |

Suggested order: `0201_0072` → `0152_0083` → `0169_0116` on the difference view → `0000_0053`.

---

## 5. How a cell is analysed

### 5.1 The grid

The Athabasca Basin outline plus a 30 km buffer, cut into **2 km cells in EPSG:2957** (UTM 13N, NAD83):
**30,534 cells over 122,136 km²**. A cell is a search area, never a target.

### 5.2 Features — 24 of them, and the split that matters

Each cell gets 24 features, each stored with the **count of observations behind it**, so "no reading" is never
confused with "a low reading".

- **18 geological features**: distance to the nearest EM conductor, conductor density, distance to a fault,
  fault density, mapped graphitic/pelitic host, interpolated unconformity depth, lake-sediment and lake-water
  uranium (two surveys), boulder counts-per-second, plus Sentinel-2 derived water/vegetation/bare fractions and
  Copernicus DEM elevation, relief, landform grain and grain coherence.
- **6 exploration-effort features**: drillholes in the cell, year of the first hole, airborne survey count,
  ground survey count, sediment samples, boulder samples. **These describe where people looked, not what is in
  the rock.**

Features are computed from `native`-tier data only — as the province published it. Nothing a model read off a
scanned page ever becomes a feature (§6.5).

### 5.3 Four views, three scores

**1 — Criteria (knowledge-driven).** A fuzzy-membership table written by hand from the research, with every
threshold and weight recorded beside its citation in `knowledge/criteria.toml`. It is **not fitted to the
labels at all**.

| Criterion | Weight | Status | Element | Shape |
|---|---|---|---|---|
| conductor_proximity | 3.0 | assumed | pathway | falling 500 m → 5000 m |
| graphitic_host | 2.0 | **published** | pathway | binary |
| fault_proximity | 2.0 | assumed | trap | falling 500 m → 5000 m |
| unconformity_depth | 2.0 | assumed | cover | band 50–120–700–1000 m |
| lake_sediment_uranium | 2.0 | assumed | detection | percentile 75 → 97 |
| boulder_train | 2.0 | assumed | dispersal | percentile 50 → 95 |
| structural_density | 1.0 | assumed | trap | percentile 50 → 90 |
| lake_water_uranium | 1.0 | assumed | detection | percentile 75 → 97 |
| conductor_strength | **0.0** | **folklore** | pathway | may be named, never counted |
| em_bright_spot | **0.0** | **folklore** | pathway | may be named, never counted |

Two claims the research found repeated but never tested are carried at **weight zero** and the loader refuses
to start if folklore ever gains weight. A cell is only scored when criteria covering at least **half the total
weight** are actually known — otherwise it has *no score*, which is drawn as a gap, not as zero.

**2 — Learned.** Positive-unlabelled learning: positives are the 60 deposit cells and 620 occurrence cells;
the other 29,854 are unlabelled, not negative. Complete-case only — no imputation, because imputing a missing
geochemical reading invents evidence. Folds are fixed **before** fitting.

**3 — Effort (the null model).** The same learner given *only* the six exploration-effort features. It knows
nothing about rock. **It exists to be beaten**, and the headline result is that it is not.

**4 — Learned − effort (the fourth view).** Not a fourth model: the arithmetic difference between the two
above, computed in the export so the number the map colours and the number a panel prints come from the same
subtraction. It is the only view that answers the question the whole project is about — *where does the
geology say something that exploration history does not already say?* Blue is where the learned score leads,
pink where effort leads. Most of the basin is pink, which is the finding. `0169_0116` is the clearest cell
where it goes the other way: learned 0.830 against effort 0.041, 30 km from any labelled deposit.

It is also the honest one to demo, because it cannot be gamed by the leakage: a cell only shows blue if the
geological model beats the "where people drilled" model *on that cell*.

### 5.4 The machine learning: what, why, and what everyone else uses

**What we use.** `HistGradientBoostingClassifier` from scikit-learn — histogram-based gradient-boosted decision
trees, the same family as LightGBM. Settings, all in `prospect/models.py`:

    max_depth=4, max_iter=200, learning_rate=0.08,
    l2_regularization=1.0, class_weight="balanced", random_state=0

**Why this and not something else:**

- **Tabular, small-n, mixed types.** 680 positives against 30,534 cells, with features on wildly different
  scales (metres, ppm, counts, fractions) and several categorical. Boosted trees are the default winner on
  tabular data of this size; a neural network has nothing to learn from 680 examples that trees will not.
- **It tolerates missing values natively.** That matters here, where 9 features cover under 40% of the grid.
  We still run complete-case only (see below), but the algorithm not needing imputation removes a whole class
  of quiet invention.
- **`class_weight="balanced"`** because the base rate is 5.4%; without it the model learns to say "no".
- **Shallow trees, few iterations, L2.** With 680 positives a deep forest memorises the camps. `max_depth=4`
  is a deliberate underfit — we are trying to measure whether geology carries signal, not to win a leaderboard.
- **`random_state=0` and folds fixed before fitting**, so the number is reproducible and the split cannot be
  chosen after seeing the result.

**What we deliberately did not do:**

- **No imputation.** Complete-case only. Imputing a missing lake-sediment reading invents a measurement, and
  this project's whole discipline is that it does not do that. The cost is honest: the learned and effort
  models score only the 10,183 cells where every input exists.
- **No SMOTE or synthetic positives.** Generating fake deposits to balance classes would be fabricating the
  very thing being predicted.
- **No hyperparameter search against the test folds.** There is no held-out tuning budget here, so tuning
  against the folds would leak.

**What the field actually uses.** From this project's research (report 02):

| Approach | Who uses it | Note |
|---|---|---|
| **Random Forest / gradient boosting on gridded features** | The workhorse of published mineral prospectivity mapping | What we use, and what MineTRACE's scorer is |
| **Weights of Evidence / fuzzy logic** | The classical knowledge-driven method, and the only peer-reviewed Athabasca precedent | This is essentially our **criteria** score |
| **Positive-unlabelled learning** | Increasingly standard, because "not a known deposit" ≠ "barren" | The framing we use for labels |
| **CNNs on geophysical grids** | Growing, where magnetics/gravity grids exist | **Not available to us** — no public grids for Saskatchewan (§3, Geophysics) |
| **Deep learning on drill-core imagery / hyperspectral** | Active research | Needs company core, not public data |

The one thing the literature does far less often, and which this project treats as the headline, is
**running an exploration-effort null model alongside the real one**. Published prospectivity papers typically
report AUC against known occurrences without asking how much of that AUC a model would get from drilling
density alone. When we asked, the answer was: most of it.

### 5.5 The colour exception

Everywhere else on this map, colour encodes a data source or an extraction status. Score cells break that, so
the exception is **declared and tested**: the layer carries `metadata["lr:score"]`, the honesty check in
`composeStyle.ts` allows only score fields on such a layer, and refuses both an undeclared layer that colours
by a computed key and a declared layer that colours by anything else. The banner changes and a legend appears.

### 5.6 What the scores are actually worth

Measured on the 10,183 cells all three models can score, base rate 5.4%:

| Model | Fold | PR-AUC | ROC-AUC | Capture @ top 10% |
|---|---|---|---|---|
| Criteria (unfitted) | none | 0.069 | 0.547 | 14.5% |
| Learned | random | 0.183 | 0.797 | 37.0% |
| **Effort (null)** | random | **0.386** | 0.879 | **57.1%** |
| Learned | **spatial** | 0.133 | 0.762 | 28.9% |
| **Effort (null)** | **spatial** | **0.347** | 0.852 | **53.1%** |

| Learned | camp | 0.080 | 0.641 | 15.6% |
| **Effort (null)** | camp | **0.241** | 0.773 | **38.8%** |

The Phase 0 re-test (FINDINGS.md, F1) repeated this with negatives matched to the positives' effort profile and
one positive per 10 km block: effort 0.178 against learned 0.111 under spatial folds, intervals separate. The
gap narrows because thinning stops one camp counting many times; it does not close.

Two more tests sit on the Eval page, each row naming the MLflow run behind it. The **model search** (F3) put
five candidates and six ablations through the same folds against the same null: the best geology-only model,
a random forest, reaches 0.125 against the null's 0.178 with the intervals apart, so the registry holds it as a
candidate and serves nothing; removing the conductor distance is the one ablation that moves the result. The
**dated hindcast** (F4) freezes labels and drilling at a cutoff and asks where each later discovery would have
ranked: the geology model puts the six discoveries after 2000 at a median 12% of basin area, the criteria score
at 16%, and effort at 51%, because effort cannot rank ground nobody had drilled. That last number is why the
geology model is still worth building even though it loses the first test.

**Read the spatial row.** Under folds drawn so nearby cells cannot leak between training and test, a model given
nothing but drilling history scores **0.347** against **0.133** for the model trained on geology. Under
leave-one-camp-out it is 0.241 against 0.080. The knowledge-driven criteria score, which nobody fitted, reaches
ROC-AUC 0.547 — near chance.

That is the finding this system was built to be able to report. Known deposits sit where people looked, so any
model trained on public labels partly learns exploration history.

---

## 6. The LLM layer

### 6.1 Eight tools, all deterministic Python

`cell_features`, `cell_scores`, `criteria_breakdown`, `label_context`, `coverage`, `retrieve`, and two the
staged analyst added: `nearby` (what one evidence layer holds around the cell: counts, nearest distances, the
nearest features, and whether the cell lies inside the layer's mapped footprint, so that an empty radius where
nothing was ever mapped or sampled reads as unknown rather than absent) and `crosscheck` (the conjunctions
the handbook names, computed: conductor with fault, sediment anomaly with sampling density, each side carrying
the same footprint flag). The footprint is built in memory from the layer's own geometry, the union of 5 km
halos round its points or lines or of its map polygons, and the halo is itself a value with an id.

Every number a tool returns arrives inside a **Val with an id**. The model never computes anything — no
arithmetic, no distances, no conversions — because that boundary is where documented GIS-agent failures happen.
If a tool shows the model a number, that number has an id it can cite; a test enforces it.

### 6.2 The panel — three roles per cell

**Proponent** argues from the tools. **Skeptic** attacks coverage gaps, effort-model artefacts, folklore
criteria and proximity to known deposits. **Adjudicator** rules: *supports a closer look* / *insufficient
evidence* / *evidence against*, names which criteria are **unknown** versus **absent**, and states the one
observation that would change the verdict. The verdict scale tops out at "supports a closer look" — there is no
"drill here".

The skeptic has already earned its place: on one run it noticed the criteria model had no validation metric
while the other two did. That is why §5.6 has a criteria row at all.

### 6.3 The chat

Same tools, same evidence record, same gate. A conversation is bound to one cell; selecting another starts a
fresh one rather than carrying stale context.

The chat is scored on its own track of the benchmark (PRD §D.3.1, role 2), not on AUC: whether what it says
is what the store says, and whether it declines when the store cannot answer. Two of its three tiers need no
model to build and `lr bench interface build` writes them under `knowledge/bench/interface/`: tier 1 asks
about 300 questions the tools answer exactly (a distance, a count, a share, a score, which criteria are
unknown), each with its gold as value ids, plus 44 questions whose right answer is a refusal with a reason
(not measured here, outside the grid, a value the store lacks, out of scope); tier 3 asks about 110 questions
built from failures actually observed, the gate suite's corruptions among them, each naming its source. Both
draw their cells from the frozen analyst benchmark under the session's leakage rules, `lr bench interface
audit` regenerates every item from the store and compares, and neither has been run against an agent yet.
Tier 2, the grounded-reasoning tier, needs a rubric and a rater and is not built.

### 6.4 The fabrication gate — and how it is measured

**The rule:** every number in an answer must resolve to a value the tools returned *and that the claim cites*,
or appear verbatim in text a tool returned.

Asserting that is easy; the honest problem was that the gate had rejected four things and **all four were false
positives** — a check that has only ever been wrong when it fired is an untested check. So `lr prospect
gate-eval` puts real claims and deliberately corrupted ones to it — digit slipped, decimal moved, precision
invented, conversion done by hand, a real number cited to the wrong value, a number cited to nothing — with no
model in the loop, so it is deterministic and free.

**Result (re-run 2026-09-20 on the 22-report evidence packs): 224 of 232 fabrications refused, 0 of 128 true claims wrongly refused; on the first, four-report packs it was 210 of 223 and 0 of 90.** The 8 that get through are
small round numbers that also appear in quotable text. The full breakdown is on the **Eval** page.

Running it changed the system three times: the allowance used to be a substring test over the whole tool
payload (so the value-id binding did nothing, and *none* of the 40 wrong-citation cases were caught); a tool
showed a number with no id, so a memo was refused for a number it could not cite; and the handbook and criteria
file were part of the allowance, making every threshold an uncited number.

### 6.5 Provenance tiers — the rule underneath everything

Five DuckDB schemas, enforced by a `tier` column with CHECK constraints and an audit that refuses a mixed table:

| Tier | What lives there |
|---|---|
| `native` | Data as published by a REST/STAC service. Trusted as far as the publisher is. |
| `read` | What a model read off a scanned page. **Unvalidated.** Never becomes a feature or a label. |
| `derived` | Anything computed — grid, features, scores, metrics. Records its inputs. |
| `agent` | What a model argued. **Never a source of numbers.** |
| `expert` | What a geologist stated, recorded with author, time and cell (`record_insight` over MCP) before any agent may cite it; the numbers in it carry expert-tier ids, so a claim that leans on one says so (B19). |

### 6.6 Backends, cost and caching

The chat runs on OpenAI (`family = "openai"`); reading, the memo panel and the evals stay on Claude Code. The
cache key carries the backend family, so one backend's answer can never be served for the other. Spend is
checked **before** each call against a cumulative on-disk ledger — a restart does not hand back a fresh budget.
Tokens are recorded as the API reports them; dollars are arithmetic over prices you set in `.env`, because a
price this code guessed would be exactly the kind of fabricated number the rest of the system exists to prevent.

**Known issue:** the cache key covers the user prompt but not the system prompt or schema, so editing a rule
re-serves the old answer until `PROMPT_VERSION` is bumped. This was observed, not theorised. Bump the version
when you change a prompt.

---

**OpenRouter.** Any OpenAI-compatible model can take an analyst role: an arm names a `vendor/model` id and
`--backend auto` sends that role to OpenRouter (key in `.env`, its own spend ceiling, the provider's own
cost figure on the ledger, images sent as parts) while `claude-*` ids stay on the CLI. Each adapter keys
the cache under its own family, so two models never share an answer.

### 6.7 The staged analyst (Analyst v1)

The panel of 6.2 reasons in one pass; the staged analyst reasons in stages, after STA-CoT (Findings of EMNLP
2025) adapted to structured evidence. A **session** is the only path from a tool to a model, and the leakage
rules live in it as code: out-of-fold scores only, blind-listed retrieval, the cell's own label masked, expert
values marked. A **plan** lists one segment per criterion and two cross-checks. An **executor** (Sonnet 5)
answers one segment from the tool results the session staged, as a node: met, not met or unknown, a strength,
the ids it cites, one sentence. A mechanical **gate** checks every node (ids resolve, the right cell, polarity,
unknown only where unmeasured, no arithmetic, and no "not met" where the layer's footprint does not reach the
cell) and sends it back with feedback that escalates over three
attempts. A **verifier** (Opus 5, the skeptic's brief) reads the whole chain and names the faulty nodes; those
and their dependents are re-executed, up to K rounds (three by default). Two **deciders** always run: a weighted sum over node
strengths and an adjudicator; a chain that never validates takes the majority over rounds or abstains. The
chain, its verdicts and its decision are stored in the agent tier and shown on the evidence panel under the
memos. Three switches, off by default and each an arm, cut the cost: `skip_unmeasured` lets the harness
write the unknown node for a criterion whose feature has no value here, `executor_batch` asks for every
criterion's node in one call and gates them one by one, and `segment_scoped` (`v1-scoped`, `v1-scoped-batch`)
stages each executor only its own criterion's rows of the feature, criteria, coverage and cross-check tables
instead of the whole tables, with the gate holding its node to what it saw. `lr arm chain --enabled` computes them for the enabled cells; `lr arm run --arm v1` runs the same loop
over the frozen benchmark, blinded, beside v0 and the baselines. The Eval page's benchmark table carries a
second group of columns for the staged arms, per chain and from the run's own rows: the chains counted, the
node gate's refusals over executor attempts, the share of chains a verifier round validated, the share the
verifier refused at least once, rounds over the chains that validated, nodes re-executed on the verifier's
feedback, and how often the verifier's own label and the weighted-sum decider agreed with the final verdict.
A single-call arm and a baseline have no stages and show a dash there.

### 6.8 The MCP server — the same tools for any client

The eight tools of 6.1 are also served over the Model Context Protocol (PRD §E.3), so a geologist's own
Claude Code or Cursor session, the dashboard and the benchmark harness read the same contract from the same
code. `open_session(cell_id, purpose)` returns a handle every other call carries: a `dashboard` session
shows the record as it is; `scored` and `benchmark` sessions reuse the staged analyst's session (6.7), so
they serve out-of-fold scores only, mask the cell's own label, blind-list the files within 10 km, and (in a
benchmark) show the cell under a bench id and refuse any real cell id. Every number in a result is a value
with an id in `structuredContent` and beside its id in the text; unknown ("nobody measured it here") and
absent ("mapped, nothing there") are two types in every output schema, never a null, and the SDK's client
validates each result against that schema before a model reads it. A refusal is a tool result with
`isError` and a reason to act on: a cell outside the grid, a layer that is a label, a file on the blind-list.

Beside the eight reads: `hole_crosscheck` (the extraction crosscheck over hole positions, dashboard sessions
only), `check_claims` (the fabrication gate of 6.4 as a callable over what this session returned, so a stock
client checks itself before answering), `abstain` (a reason from a fixed set, recorded on the session so refusal
is measurable), `record_insight` (a geologist's statement into the `expert` tier, its numbers minted
expert-tier ids) and `run_analyst` (a stub that says it is not available until Phase 4d). Resources hold data:
`lr://handbook`, `lr://criteria`, `lr://cell/{id}/evidence`, `lr://cell/{id}/chains`, `lr://run/{id}/manifest`,
`lr://readiness/gate`, `lr://reading/inventory`. Prompts are the six roles (`proponent`, `skeptic`,
`adjudicator`, `analyst.executor`, `analyst.verifier`, `interface.router`), each built from the text the
in-house loops use and taking `cell_id` and `session_id`. The server never asks a client's model for anything.

Every session is a run: a directory under `data/runs/<id>-mcp/` with the manifest of 6.6 (store hash,
prompt hashes, fold, blind-list hash, the scores seen, every abstention and insight) and `spans.jsonl`, one
span per call carrying the arguments' hash (never their values), the result ids, the latency, the session and
the run id, mirrored to MLflow Tracing when that is on. Handles expire after four hours and are bound to the
key that opened them.

**Connecting.** `lr mcp serve --stdio` is what a client launches; put this in `.mcp.json` (Claude Code) or
`.cursor/mcp.json` (Cursor) at the project root:

    {"mcpServers": {"legacy-reader": {"command": "uv", "args": ["run", "--directory", "pipeline", "lr", "mcp", "serve", "--stdio"]}}}

`lr prospect serve` mounts the same server on the API at `http://127.0.0.1:8787/mcp` (streamable HTTP), and
`lr mcp serve --http` serves it alone on `:8788/mcp`. Local only by default: with no key register a stdio
client and a loopback HTTP client hold every scope and any other address gets nothing.

**Keys and scopes.** `LR_MCP_KEYS=<key>:<scope>[,<scope>];<key>:...` in the server's environment turns the
register on; a key is any string without `:`, `;`, `,` or white space, and `lr mcp key --scopes read,record`
mints one. An HTTP client sends `Authorization: Bearer <key>`; a stdio client (which owns the process it
launched) names its key in `LR_MCP_KEY`. A key never appears in a log, a span or a manifest: a session is
recorded under the first eight hex characters of the key's hash.

| Scope | What it opens |
|---|---|
| `read` | `open_session`, the eight reads, `hole_crosscheck`, `check_claims`, `abstain`, every resource and prompt |
| `record` | `record_insight`: writes the `expert` tier |
| `run` | `run_analyst`: the task handle, a stub until Phase 4d |

`tools/list` shows a key only the tools its scopes carry, in a fixed order with a `ttlMs`, so a client can
cache it. `--public-safe` (or `LR_MCP_PUBLIC=1`) is the build for anyone outside the team: a passage's text is
withheld (its citation, page and ids stay), `hole_crosscheck` is not served, and a `nearby` layer the
inventory marks non-redistributable is refused. Contract tests (`tests/test_mcp_*.py`) run the whole thing
through the SDK's in-memory client with no network and no model; the few that need the live store skip
without it.

## 7. The other three pages

- **Data** — how much of the grid each feature actually covers, every source with its licence and verification
  date, the five-column readiness gate (present, licensed, covers, servable, versioned: one row per layer, label
  and enabled-cell file, as `lr prospect gate` last scored it), and four recorded gaps (the magnetic grid is
  now *published, not yet pulled*).
- **Eval** — what the reading run did, what the checks caught, the fold tests from §5.6, the Phase 0 re-test,
  the model search with its registry decision and model card, the dated hindcast, the analyst benchmark with
  the staged loop's per-stage columns (§6.7), and the gate scorecard from §6.4. Every row of the three tracked
  tables names its MLflow run and the store snapshot it read. It states at
  the top that these are run statistics, not accuracy, because no gold set has been labelled.
- **Limits** — what this demo can and cannot claim, carried from the project's research with each line cited.

---

## 8. What to say out loud

> This is a public-data engineering demo. It reads what old Saskatchewan assessment reports print and measures
> how often it reads them wrongly. It makes no geological judgement and proposes no drill targets. I am not a
> geologist.

And on the map specifically:

> Retrospective scoring of public data. No geologist has seen this, and the fold tests show how much of the
> ranking is explained by where people already drilled.

The strongest thing the system produces is not a score. It is **the next observation** — the single measurement
that would most change the reading of a cell. That is what a geologist can act on.

**To trust any of this for drilling** you would need geologist-defined error classes, a hand-labelled sample,
drill outcomes, and folds fixed before modelling. That is the first ask, not the last.
