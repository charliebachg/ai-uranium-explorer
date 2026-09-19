# AI Uranium Explorer — Product Requirements

**Status:** draft v0.1 · 2026-09-19 · owner: Charlie
**Scope:** turn the demo into a working prototype that a geologist could use and an engineer could scale.
**How to read:** §1–5 set the frame. §A–E are the five workstreams, each with *current state → requirement →
how we will know → open questions*. §12 is the order we do them in. We go deeper into one section at a time
after this document is agreed.

---

## 1. The product, in one sentence

An evidence-grounded system that turns public uranium exploration records into scored ground, and lets a
geologist interrogate any score through an agent that can only speak from the evidence that produced it —
with every number traceable, every model validated against a null, and every agent claim mechanically checked.

## 2. Demo → prototype: what actually changes

| | Demo (today) | Working prototype (this PRD) |
|---|---|---|
| Serving | static JSON exports + a stdlib HTTP server on localhost | a real API and database; multi-user; deployable |
| Data | 20 registered sources, 4 gaps, one-off pulls | owned catalogue with lineage, refresh, versioning, and a decided position on every gap |
| ML | three scores, one validation run, one learner | an experiment programme: tracked runs, a model registry, bias-corrected validation, reported with intervals |
| Agent | one configuration, one synthetic eval | a benchmark over our own data; several configurations compared; the best one chosen by measurement |
| Tools | six Python functions behind a custom loop | a tool contract exposed over MCP, usable from our UI *and* from a geologist's own client |
| Trust | a fabrication gate measured on synthetic corruption | the gate at every boundary, measured on organic outputs, with a human-adjudicated denominator |

**Definition of "working":** a geologist unfamiliar with the code can open the app, pick a cell, read why it
scored what it scored, ask three follow-up questions, get answers whose every number they can click through
to its source, and be told plainly when the system does not know. An engineer can reproduce every score and
every agent answer from a versioned input, and can deploy the whole thing on a fresh machine from one command.

## 3. Who it is for

| User | Their job here | What they need from us |
|---|---|---|
| **Exploration geologist** | decide whether a cell deserves a closer look | evidence they can see, unknown kept apart from absent, the next observation that would change the answer |
| **Exploration manager** | allocate a finite programme across ground | search-space reduction paired with capture rate; a ranking that discloses how much of it is exploration history |
| **AI/ML engineer** | build, validate and scale the LLM system | reproducibility, benchmarks with denominators, an architecture that survives 10× the data |

## 4. Standing constraints — carried over unchanged

- Public data only; no logged-in sites, no paid data, no scraping behind logins; dataset licences respected;
  report PDFs (no named licence) read locally and never redistributed.
- The mineral dispositions layer is never loaded. Held-out files are never rendered, extracted or labelled.
- No drill targets, no prospectivity language, no grade or tonnage estimates. Verdicts top out at
  *supports a closer look*.
- **Every number on screen resolves to a stored value id.** Colour encodes source or status, with the one
  declared exception for score cells.
- Provenance tiers stay: `native` (as published) / `read` (model-read, unvalidated) / `derived` (computed,
  inputs recorded) / `agent` (argued, never a source of numbers).
- A model never computes: no arithmetic, distances, conversions or percentages outside a tool.
- Nothing invented to fill a gap. A missing dataset is named on screen, not smoothed over.

## 5. Where we are — the facts the plan starts from

- **Store:** 30,534 cells · 732,816 feature values across 24 features (18 geological, 6 exploration-effort) ·
  30,534 labels (60 deposit, 620 occurrence, 29,854 unlabelled) · 64,131 scores · 305,340 criterion
  memberships · 116 metrics · 27 memos with 188 claims.
- **Corpus:** 1,534 in-area assessment files at metadata level, 1,846 pages read, 22 of 60 target PDFs fetched.
- **Sources:** 20 registered, 18 redistributable, 4 recorded gaps (aeromagnetic grids, discovery dates, EM
  conductor attributes, alteration measurements).
- **Headline measurement:** under spatial folds, the effort-only model reaches PR-AUC 0.347 against 0.133
  for the geology-trained model. **A literature review found a mechanism that could produce this result as a
  sampling artefact** (naive unlabelled-as-negative, uncorrected). It is a hypothesis until §C tests it.
- **Gate:** 210 of 223 corrupted claims refused, 0 of 90 true claims wrongly refused — on synthetic corruption
  only. Two named holes: a correct value from the wrong cell passes; negated evidence passes.
- **Stack:** Python 3.13 / uv / typer / DuckDB / scikit-learn / rasterio / pystac; React 19 / Vite 8 / TS 6 /
  Tailwind 4 / MapLibre 6 / zod 4 / zustand; Claude Code headless and an OpenAI adapter with a spend ceiling.
- **Known defects** (from `research/06`): metric mismatch vs MineTRACE, gate only at publish, homogeneous
  panel, cache key omits the system prompt, thin page-tier retrieval, no gold labels.

---

## A. Web application — a real frontend and backend

### A.1 Current state
Static exports served by Vite; a `ThreadingHTTPServer` on `127.0.0.1:8787` for evidence and chat; in-memory
conversations; 16 MB of evidence layers as lazy GeoJSON; no auth, no persistence, no deploy story.

### A.2 Requirements

**Backend (must)**
- **FastAPI** + Pydantic v2 (already our validation layer) + uvicorn. Typed routes, OpenAPI generated, SSE
  streaming for the agent. Async where I/O-bound.
- **PostgreSQL 16 + PostGIS** as the *serving* database: cells, features, scores, labels, memos,
  conversations, users. Spatial queries (nearest label, cells in view) become SQL, not Python loops.
  **DuckDB stays** as the analytics engine for feature computation and evaluation — it is the right tool for
  that and the wrong tool for concurrent serving. The tiered schema (`native/read/derived/agent`) is preserved
  in Postgres with the same CHECK constraints and the same audit.
- **Vector tiles** for every map layer: `tippecanoe` → PMTiles, served from object storage. Replaces GeoJSON
  entirely; scales to millions of features and to the whole province. The score layer becomes a tile with the
  four score properties, so switching model is a style change, not a fetch.
- **Object storage** (S3-compatible; MinIO locally) for rasters, COGs, PDFs, tiles and model artefacts.
- **Background jobs** for anything over a second: panel runs, corpus builds, re-scoring. A queue with a
  worker (RQ on Redis is enough; graduate to Celery/Temporal only if fan-out demands it).
- **Conversations persisted** with every tool call, every value id cited, and the gate's verdict — the
  transcript *is* the benchmark data for §D.
- **Auth** minimal but real: API keys per user, roles (viewer / geologist / admin). Enough to record *who*
  adjudicated a memo.
- **Observability:** OpenTelemetry traces; every agent run a trace, every tool call a span carrying the value
  ids it returned. Cost and latency per turn recorded, not estimated.
- **One-command deploy:** `docker compose up` locally; a container image + migrations for anywhere else.
  Migrations via Alembic; the schema is code.

**Frontend (must)**
- Keep React 19 / Vite / TypeScript / Tailwind / MapLibre — it is sound and already tested. Add **TanStack
  Query** for server state (cells, evidence, conversations); keep zustand for UI state only.
- A generated, typed API client from the OpenAPI spec, so the frontend cannot drift from the backend contract.
- Keep the value-id contract and the DOM digit walk in e2e. This is the one thing the demo got exactly right.
- Conversation history, multiple cells side by side, a memo review queue for the geologist role.

**Should**
- Vector tile styling by zoom (cells → hexbins at province scale).
- Offline-safe static fallback (today's behaviour) for the map and scores when the API is down.

### A.3 How we will know
- p95 API latency under 300 ms for evidence reads at 10 concurrent users on a laptop.
- A fresh clone reaches a running app in one command, with a seeded database, in under 15 minutes.
- Every e2e test that passes today passes against the API-backed app.

### A.4 Open questions
- Postgres-only, or Postgres for serving + DuckDB for analytics (recommended — the split is honest about
  what each is good at)?
- Hosting target: a single VM is enough for the prototype; do we design for Kubernetes now or leave that to a
  later ADR? (Recommendation: containers now, orchestration later.)
- Do conversations need to be shareable by link? Affects auth and storage design.

---

## B. Data and features — full ownership

### B.1 Current state
`knowledge/data_inventory.toml` registers 20 sources with licence, verification date and what each bears on;
4 gaps recorded with evidence. Features are computed once by `lr prospect features`; there is no versioning
of inputs, no refresh, and no lineage graph a reader can follow from a score back to a source pull.

### B.2 Requirements

**Catalogue (must)** — every dataset has: source URL, licence and redistributability, retrieval date and
content hash, refresh cadence, spatial/temporal coverage, the features it feeds, and the gap it would close.
Machine-readable, and rendered on the Data page.

**Lineage (must)** — a score → the model run → the feature snapshot → the source pulls → the licence. Dagster-
style *asset* semantics fit this exactly: each derived table is an asset with declared upstream assets.
Whether we adopt Dagster or record lineage ourselves is an open question; the requirement is the graph.

**Versioning (must)** — raw pulls and feature snapshots are content-addressed and immutable. A model run
names the snapshot it trained on. DVC or lakeFS; the choice matters less than the discipline.

**Feature store (should)** — one place where a feature's definition, its builder, its unit, its observation
count and its coverage live together, versioned with the code that made it. `feature_spec` is the seed.

**Gap register (must)** — every gap carries: what it would close, evidence it does not exist publicly, the
date that was last checked, and a workaround. Gaps are *re-verified*, not assumed. Specifically:
- **Aeromagnetic grids** — re-verify against the NRCan Geoscience Data Repository's national grids before
  the prototype claims absence. If a Canada-wide grid covers the basin at usable resolution, this is the single
  most valuable addition to the feature table (magnetics and radiometrics were the top two uranium predictors
  in the published SHAP analysis).
- **Discovery dates** — hand-build `discoveries.toml` from public technical reports; it unlocks retrodiction.
- **EM conductor attributes** — conductance is on the provincial layer as a field; check whether it is
  populated.
- **Alteration** — company data; stays a gap, stated.

**New sources to evaluate (should)**
- NRCan GDR: aeromagnetic and radiometric grids (verify coverage).
- Saskatchewan open-file geochemistry beyond the two lake surveys already used.
- USGS/GSC critical-minerals compilations for label cross-checks.
- Sentinel-1 SAR for surface-condition features (public, STAC-served, complements Sentinel-2).

**Corpus (must)** — finish the page-tier index over all 1,534 in-area files; run a two-stage extract → validate
→ restructure pipeline over the target reports with per-value confidence, routed by risk to review.

### B.3 How we will know
- Every value on screen can be walked to a source pull with a date and a hash, by clicking.
- A feature snapshot can be rebuilt byte-identically from its recorded inputs.
- The gap register has a "last verified" date under 90 days on every entry.

### B.4 Open questions
- Which lineage tool, if any — Dagster assets, or our own asset table in Postgres? (Recommendation: try
  Dagster on the feature DAG; it is the closest fit to the tier model, and the alternative is writing it.)
- Is the 2 km cell the right unit for the prototype, or do we carry 1 km and 5 km grids in parallel for the
  sensitivity study §C requires?

---

## C. Machine learning for prospectivity — rigorous, and MLOps-grade

### C.1 Current state
`HistGradientBoostingClassifier` (depth 4, 200 iters, lr 0.08, L2 1.0, balanced) on complete cases;
positive-unlabelled framing; random / spatial (30 km blocks) / leave-one-camp-out folds fixed before
fitting; metrics PR-AUC, ROC-AUC, capture@10%, calibration; one run, no tracking, no registry.

### C.2 Requirements — the experiment programme

**C.2.1 Settle the headline first (must)**
The effort-beats-geology result is the claim everything rests on and it has a named confound. Before any
other modelling:
- Re-run learned vs effort with **target-group background sampling** (background matched to the effort
  distribution) and with **systematic grid subsampling of positives** — the two corrections with published
  support.
- Report under **random, spatial and camp folds** side by side. If the ordering holds only under one fold
  scheme, say so.
- Report **area-budget capture** (top 5 / 10 / 15 % of area → N of 60 deposit cells) beside PR-AUC. It is the
  number a manager reads and it is robust to the confound in a way discrimination metrics are not.
- Report **ROC-AUC under MineTRACE's exact protocol** (30 positives held out, 200 negatives sampled) so the two
  systems can be placed on one axis — and state that PR-AUC on 30,534 cells and AUC on 30/200 are different
  measurements.
- **Confidence intervals** on every headline number, by bootstrap over folds.

**C.2.2 Model search, tracked (must)**
- **MLflow** for every run: parameters, fold assignments, metrics with intervals, the feature snapshot hash,
  the code commit. Nothing is reported that is not in the tracker.
- **Model registry** with stages (candidate → validated → served). The served model is the one the API reads;
  promotion is a recorded action with a reason.
- Candidates, each against the same folds and the same null: HistGB (baseline), Random Forest, logistic
  regression with spatial terms, a PU-specific learner (Elkan-Noto / bagging PU), a knowledge-constrained
  model (criteria as a prior). Report all; do not cherry-pick.
- **Ablations**: each geological feature group removed in turn; effort features added to the learned model
  (does geology add *anything* on top of effort?).
- **Sensitivity**: 1 km and 5 km cells; 20 / 30 / 50 km spatial blocks.
- **No imputation, no synthetic positives, no tuning against the test folds.** These are decisions, recorded.

**C.2.3 Retrodiction (should)**
With `discoveries.toml`: freeze datable inputs at a cutoff, score, report where each later discovery ranks.
The leakage (compilations drawn as they stand today) is stated.

**C.2.4 MLOps mechanics (must)**
- Reproducible training: `lr prospect score --snapshot <hash>` rebuilds a run exactly.
- CI runs the fold tests on every change to `prospect/models.py` and fails on a metric regression beyond the
  interval.
- Data drift check when a source is refreshed: feature distributions compared to the served snapshot.
- Model cards generated from the tracker, on the Eval page.

### C.3 How we will know
- The headline claim is either confirmed under corrected sampling with intervals, or retracted — in writing,
  on the Eval page.
- Every number on the Eval page links to an MLflow run.
- A second engineer can reproduce the served model's metrics to the third decimal from the registry entry.

### C.4 Open questions
- Is a deep model worth a run at all with 680 positives? (Recommendation: one CNN-on-rasters arm *only if*
  the magnetic grid materialises; otherwise no.)
- What is the acceptance threshold for "geology adds something"? Propose: learned+effort must beat effort
  alone by more than the interval width under spatial folds, or we say it does not.

---

## D. Agent reasoning — a benchmark, not a belief

### D.1 The stance
The literature reports that *some* multi-agent configurations do not beat a single agent on *some* tasks. That
is not a finding about ours. Nobody has measured an LLM adjudicating grounded geological evidence behind a
mechanical gate, because nobody has built one. **The prototype's job is to measure it.** We may find our
panel wins, loses, or wins only in one configuration — all three are results worth having.

### D.2 Current state
One configuration (three roles on one backend, custom tool loop, gate at publish). One synthetic gate eval.
Fourteen memos and a handful of chats. No task-level correctness measurement at all.

### D.3 Requirements

**D.3.1 UraniumBench — our own benchmark (must)**
A question set over *our* data, in three tiers:
1. **Deterministic** (~300 items): questions with exact answers computable from the store — "how far is the
   nearest deposit", "which criteria are unknown here", "how much of the grid does this feature cover". Gold is
   generated by code, so correctness is exact and free. Stratified over cell types (known deposit / high score
   far from labels / coverage gap / background).
2. **Grounded reasoning** (~100 items): questions whose answer is a judgement over evidence — "is this score
   explained by drilling", "what is unknown versus not met", "what single observation would change the
   reading". Gold is a rubric written from the handbook, scored by a mechanical checker where possible and a
   rater where not.
3. **Adversarial** (~100 items): questions engineered to invite fabrication, leakage or folklore — negated
   premises, a number from a neighbouring cell, a folklore criterion phrased as fact.

**D.3.2 Metrics (must)** — reported per tier, per configuration, with intervals
- Correctness against gold (tier 1 exact; tier 2 rubric).
- **Faithfulness**: gate pass rate; citation precision (cited ids actually support the claim) and citation
  coverage (fraction of numeric claims carrying any citation).
- Unknown-vs-absent discrimination: does the agent say "not measured" when it is not measured.
- Abstention quality: does it decline when the tools cannot answer, and not otherwise.
- Tool efficiency: calls per answer, and correctness as a function of calls (the literature reports a steep
  decay with call count; we should know our curve).
- Cost and latency per answer, measured.
- Self-consistency: agreement across 3 runs at temperature.

**D.3.3 Configurations to compare (must)** — same benchmark, matched compute budget
1. Single agent, no tools beyond the four opening ones.
2. Single agent + self-consistency (n=5, vote).
3. Panel, homogeneous (today's design).
4. Panel, heterogeneous (proponent and skeptic on different model families).
5. Panel + voting adjudicator over drafted positions (not consensus prose).
6. Any of the above ± retrieval over the corpus; ± the gate; ± reasoning/serialisation split.

Report the full matrix. The prototype ships whichever wins on tier 2 correctness at acceptable tier 3
faithfulness — chosen by the table, not by preference.

**D.3.4 Human adjudication (must, bounded)**
One geologist-day: 30 questions × the top 2 configurations, rated on a fixed rubric (MineTRACE's protocol,
copied), reported with chance-corrected agreement. This converts the benchmark from internal to comparable.

**D.3.5 Gate hardening (must)** — done before the benchmark runs, so the benchmark measures the real gate
- Cell-identity binding: a cited id must carry the cell in question.
- Polarity and unit normalisation.
- Gate at every handoff (proponent → skeptic → adjudicator → publish).
- Organic evaluation: the benchmark's adversarial tier *is* the organic gate measurement.

### D.4 How we will know
- A results table: 6 configurations × 3 tiers × 7 metrics, with intervals, on the Eval page.
- A written finding: which configuration the prototype uses and the number that decided it.
- Gate false-refusal rate stays at zero on tier 1 and tier 2; escape rate on tier 3 reported with its interval.

### D.5 Open questions
- Which two model families for the heterogeneous arm? (We have Claude and OpenAI adapters; a third — an open
  model — would make the heterogeneity claim stronger.)
- Should the benchmark be published as a standalone artefact? It is the most reusable thing we will make.

---

## E. Agentic configuration — tools, MCP, and what a geologist actually needs

### E.1 Current state
Six deterministic tools (`cell_features`, `cell_scores`, `criteria_breakdown`, `label_context`, `coverage`,
`retrieve`) behind a custom Python loop, reachable only through our own server.

### E.2 What a geologist needs from the tools (must) — derived from the user jobs in §3
- **Compare**, not just inspect: two or more cells side by side; a cell against its camp.
- **Provenance on demand**: "where did this number come from" as a tool, returning the page, box and quote.
- **The next observation**: a tool that ranks which missing measurement would most change a cell's reading
  (a sensitivity over the criteria table — deterministic, and the most actionable thing the system can say).
- **Spatial questions**: cells within N km sharing a signature; the nearest cell with a measurement of X.
- **Document questions**: which reports mention this ground, what did they conclude, quoted at page level.
- **Unknown-vs-absent everywhere**: every tool distinguishes "no observation" from "observed low", in its
  return type, not in prose.

### E.3 Why an MCP server (must)
Today the tools exist only inside our loop. Exposing them over the **Model Context Protocol** means:
- The **same** tools serve our UI, a geologist's own Claude or Cursor session, and the benchmark harness — one
  contract, one implementation, one place to fix a bug.
- A geologist can bring their own client. That is the difference between "a demo app" and "a capability
  their team can adopt."
- The tool contract becomes testable and versioned independently of any agent design.
- Configurations in §D become swappable clients of the same server, which is what makes the comparison fair.

Design: a FastMCP server over the FastAPI backend; each tool returns typed values with ids (the value-id
contract carried into the protocol); resources for the handbook and criteria; prompts for the three roles.
Auth by API key. The custom loop is retired in favour of the model's native tool calling against the server,
with the gate applied to the streamed tool results.

### E.4 Agent runtime (should)
Keep the orchestration in-house and small (a typed state machine over MCP calls) rather than adopting a large
framework: our loop is five steps, and every added abstraction is another place a number can be invented.
Revisit only if §D's winning configuration needs graph features we do not have.

### E.5 How we will know
- A geologist connects a stock MCP client to the server, asks about a cell, and gets a gated answer.
- The benchmark harness in §D runs against the MCP server, not against in-process functions.
- Tool contract tests: every tool's every number carries an id; unknown and absent are distinct types.

### E.6 Open questions
- MCP over stdio for local use and streamable HTTP for the deployed server — both, or HTTP only?
- Where does the gate live when the model calls tools natively — in the server (gate the result before it
  returns) or in the client (gate the answer)? (Recommendation: both; the server is the last line.)

---

## 12. Order of work

| Phase | Weeks | What ships | Gate to next phase |
|---|---|---|---|
| **0 · Settle the headline** | 1 | §C.2.1 corrected sampling, both folds, intervals, area-budget capture | The effort-vs-geology claim is confirmed or retracted, in writing |
| **1 · Platform** | 2–3 | §A: FastAPI + Postgres/PostGIS, vector tiles, jobs, persisted conversations, one-command deploy; §B catalogue + lineage | All current e2e pass against the API; fresh clone runs in one command |
| **2 · Data ownership** | 1–2 | §B: gap re-verification (magnetics first), corpus completed, versioned snapshots | Every on-screen value walks to a hashed source pull |
| **3 · ML programme** | 2 | §C.2.2–C.2.4: MLflow, registry, candidate models, ablations, sensitivity, CI regression | Eval page links every number to a run |
| **4 · Tools over MCP** | 1–2 | §E: MCP server, geologist tools, gate hardened at every boundary | A stock client gets a gated answer |
| **5 · Agent benchmark** | 2 | §D: UraniumBench, six configurations, one geologist-day | A results table decides the shipped configuration |

Phase 0 is first because it is the only one that can change what the rest of the document is *for*.

## 13. Risks

| Risk | Consequence | Mitigation |
|---|---|---|
| The headline does not survive corrected sampling | the central story changes | Phase 0 finds out first; a retraction on public data is itself a credible result |
| Aeromagnetic grid does exist and we said it did not | credibility | re-verify in Phase 2 before any external claim |
| Postgres migration stalls the demo | nothing to show mid-project | static-export path stays alive until parity; feature-flag the API |
| Geologist-day never happens | benchmark stays internal | tiers 1 and 3 are fully mechanical and stand alone |
| LLM spend | budget | ceilings per backend already exist; the benchmark is cost-capped per configuration |
| Framework sprawl in the agent layer | more places to invent a number | §E.4: in-house runtime, revisit only on measured need |

## 14. Deliberately out of scope for the prototype

Fine-tuning any generator; a fourth critic role; LLM-judge refinement loops over extracted tables; evaluation
against text-recall geology benchmarks; any commercial or company data; any claim about ground a company holds.

---

*Next: pick a section and go deeper. Recommended order is §12's — but §A (the platform) is the one with the
most decisions to make together, and §C Phase 0 is the one that can start today.*
