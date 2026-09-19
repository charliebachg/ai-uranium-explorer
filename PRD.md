# AI Uranium Explorer — Product Requirements

**Status:** draft v0.1 · 2026-09-19 · owner: Charlie
**Scope:** turn the demo into a working prototype that a geologist could use and an engineer could scale.
**How to read:** §1–9 set the frame; §6 is the architecture in two diagrams, §7 the bias and leak register, §8 the
three agents, and §9 the boundary of what this prototype will actually run. §A–E are the five workstreams, each with *current state → requirement →
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

## 6. How the pieces fit

Three agents, one store, three scores and one control. The **extractor** turns pages, chips and text into
values that land in the store under their own tier. The **criteria**, **learned** and **effort** scores are
computed over the same rows under the same spatial folds; effort is the control the other two are read
against, and the served learned model is the best one that beats it — or none. The **analyst** reads a cell's
evidence and is scored against the same labels as the models, with retrieval blinded to the files that would
give the answer away. The **interface** talks to the geologist, calls tools over the store, and can invoke the
analyst. Every number that crosses a boundary passes the gate. Nothing an agent says is ever a source of
numbers, and nothing said in a session is ever written back to the served model, the labels or the map.

```mermaid
flowchart LR
  subgraph SRC["Public sources"]
    S1["Layers: conductors, faults,<br/>geochemistry, survey footprints"]
    S2["Assessment PDFs<br/>(scanned pages)"]
    S3["Imagery: Sentinel-2,<br/>DEM, bedrock map"]
    S4["Labels: deposits,<br/>occurrences"]
  end

  subgraph EXT["Extractor agent (role 1)"]
    X1["VLM page reading:<br/>value + page + box + quote"]
    X2["Chips and map units<br/>to features"]
    X3["Text embeddings:<br/>LLM-derived features"]
  end

  subgraph STORE["One store, tiered"]
    direction TB
    T1["native"]
    T2["read"]
    T3["derived"]
    T5["expert<br/>(recorded geologist input)"]
    T4["agent<br/>(never a source of numbers)"]
    HO["Held-out files and eval cells:<br/>never rendered, extracted or retrieved"]
  end

  subgraph SCORES["Scores on the same rows, same 30 km folds"]
    C["criteria<br/>(hand table, cited thresholds)"]
    L["learned<br/>(registry: best that beats the null, or none)"]
    E["effort null<br/>(the control)"]
  end

  subgraph ANA["Analyst agent (role 3)"]
    A1["evidence-only arm"]
    A2["+ out-of-fold scores arm"]
    A3["multimodal arm (chips)"]
  end

  GATE{"Gate at every handoff:<br/>every number resolves to a value id"}

  subgraph UI["Dashboard"]
    MAP["Map, score cells,<br/>evidence rail"]
    IFC["Interface agent (role 2)<br/>tools over the store"]
  end

  GEO(("Geologist"))
  BENCH["UraniumBench<br/>one tier per role"]

  S1 --> T1
  S4 --> T1
  S2 --> X1 --> T2
  S3 --> X2 --> T3
  S2 --> X3 --> T3
  T1 --> SCORES
  T2 --> SCORES
  T3 --> SCORES
  E -. control .- C
  E -. control .- L
  T1 --> A1
  T2 --> A1
  T3 --> A1
  SCORES -- "out-of-fold only" --> A2
  S3 --> A3
  HO -. "blinds retrieve for scored cells" .- ANA
  ANA --> GATE --> T4
  SCORES --> MAP
  T4 --> MAP
  GEO <--> IFC
  IFC --> MAP
  IFC -- "insight, recorded first" --> T5
  T5 --> ANA
  IFC -- "invoke" --> ANA
  BENCH -. scores .-> EXT
  BENCH -. scores .-> SCORES
  BENCH -. scores .-> ANA
  BENCH -. scores .-> IFC
```

**A live reading** is what happens when the geologist adds something the store does not hold. The insight
becomes a value first, so the analyst can cite it; the run is then the ordinary analyst over the evidence
pack plus the expert-tier values, gated like any other, and reported beside the run without the insight.

```mermaid
sequenceDiagram
  actor G as Geologist
  participant I as Interface agent
  participant S as Store
  participant A as Analyst agent
  participant X as Gate

  G->>I: "the conductor probably continues north-east of hole X"
  I->>S: record as expert-tier value (author, time, cell, text)
  S-->>I: value id
  I->>A: run this cell: evidence pack + out-of-fold scores + expert values
  A->>S: cell_features, nearby, score, retrieve (blinded to held-out files)
  S-->>A: values with ids
  A->>X: memo with claims
  X-->>I: pass, or reject with the unresolved number
  I-->>G: session assessment: verdict, inputs listed, diff against the run without the insight
  Note over S: written to the agent tier only. Never to the served model, the labels or the displayed score
```

---

## 7. Bias and leak register

Every way this system could look better than it is, written down so each one can be attacked in turn. An
entry moves to *handled* only when a test in the repository demonstrates the fix. **Status:** open · partly ·
handled.

**Labels and sampling**

| Id | Bias or leak | Where it enters | What it would do | How we detect it | How we would fix it | Status |
|---|---|---|---|---|---|---|
| B1 | Exploration-effort confound | labels exist where people drilled; features are measured where people looked | any model learns *where people went*, and reports it as geology | the effort-only null on the same rows; capture stratified by effort decile | effort-adjusted metrics; PU negatives sampled at matched effort; report every score beside the null | partly |
| B2 | Label crossover from naive PU sampling | unlabelled cells treated as negatives | shrinks the positive area, inflates variance, depresses the geology model most | Phase 0 re-test (§C.2.1) against bagging PU and recursive reliable-negatives | reliable-negative selection; spatial negatives away from positives; bagging PU | open |
| B3 | Spatial autocorrelation leakage | random cross-validation | neighbouring cells in train and test; metrics inflate | gap between random, spatial and camp folds | spatial blocks fixed before the first fit; camp holdout; all three reported | handled |
| B4 | Label definition bias | deposit vs occurrence; compiler-dependent definitions in SMDI | "positives" mix ore bodies with a single radioactive boulder | metrics reported deposits-only and with occurrences | both label sets kept; base rate stated beside every number | partly |
| B5 | Camp clustering | deposits sit in a handful of camps | a model memorises camps and looks skilled | leave-one-camp-out fold | camp fold is one of the three standard folds | handled |
| B6 | Cell size and areal unit | the 2 km grid was chosen, not derived | results that only hold at 2 km | 1 km and 5 km sensitivity (§C.2.2) | report the sensitivity run beside the headline | open |
| B7 | Class-imbalance metric flattery | ROC-AUC at 1:500 | a model that ranks background well looks excellent | PR-AUC and capture at 10% of area with the base rate | never compare across protocols (MineTRACE's 30/200 is not ours); PR-AUC first | handled |

**Features, coverage and provenance**

| Id | Bias or leak | Where it enters | What it would do | How we detect it | How we would fix it | Status |
|---|---|---|---|---|---|---|
| B8 | Coverage is not random | surveys were flown where interest was; 9 of 17 features cover 40% or less | missingness itself predicts the label | missingness-vs-label correlation; readiness scorecard | missingness indicators counted as *effort* features; area-of-applicability mask; metrics per coverage stratum | partly |
| B9 | Survey vintage and method drift | EM systems, detection limits and sampling density changed over decades | old ground looks quiet; new ground looks anomalous | feature value vs survey year | survey year and system as effort features; per-survey normalisation | open |
| B10 | Censored geochemistry | below-detection values reported as half the limit or zero | false thresholds in the criteria; distorted anomalies | detection limit recorded per sample | censoring-aware summaries; detection limit stored beside the value | open |
| B11 | Folklore in the criteria table | thresholds and weights taken from textbooks written about the same camps that supply the labels | circular skill: the criteria describe the known deposits | criteria score on camp holdout; folklore rows at weight zero | every threshold cited; fitted-weights arm evaluated on held-out camps only | partly |
| B12 | Text-length bias | mineralised areas have longer, richer reports and map descriptions (Parsa et al. 2025) | LLM-derived features read effort through the text channel | text length vs label; a length-only null | length-normalised embeddings; length itself filed as effort | open |
| B13 | Corpus survivorship | assessment files are filed by holders who kept ground; the fetched subset was chosen by interest | the corpus over-represents success | fetch selection recorded; compare fetched vs unfetched by NTS sheet | sample files by sheet, not by interest; record the selection rule | open |
| B14 | Compilations as they stand today | deposit and occurrence compilations include post-discovery work | retrodiction sees the future | the dated hindcast with a cut-off (§C.2.3) | freeze datable inputs at the cut-off; state the leakage that cannot be removed | partly |

**Imagery and text**

| Id | Bias or leak | Where it enters | What it would do | How we detect it | How we would fix it | Status |
|---|---|---|---|---|---|---|
| B15 | Effort visible in imagery | cut lines, camps, drill roads and clearings in Sentinel-2 chips | a vision model learns to spot roads and calls it geology | chip-only model vs the effort null; saliency over cleared pixels | mask clearings and roads; ablation with and without; state as a limit if it cannot be removed | open |
| B16 | Cloud and season | cloud pixels once counted as valid ground; snow and leaf-on differ by scene | features that encode acquisition date | cloud mask audit; per-scene date recorded | scene date and cloud fraction stored; composite over a fixed window | partly |

**Agent, retrieval and gate**

| Id | Bias or leak | Where it enters | What it would do | How we detect it | How we would fix it | Status |
|---|---|---|---|---|---|---|
| B17 | Retrieval leakage | the assessment file for a deposit cell says mineralisation was intersected | the analyst reads the answer instead of the evidence | analyst with and without retrieval; citations checked against the held-out file list; a "read the label from the report" baseline | blind retrieve to held-out files for the scored cell and its neighbours; date cut for hindcast | open |
| B18 | Score leakage into the analyst | **live today**: `cell_scores` returns learned and effort scores fitted on every cell, as its own note says | the analyst inherits the fit and is scored on the same labels | the model version and fold the analyst saw are logged per run | out-of-fold scores only; the arm reported beside "copy the score" and the evidence-only arm | open |
| B19 | Expert anchoring | a geologist's insight steers the analyst; the geologist then sees agreement | a confirmation loop dressed as a live reading | diff between runs with and without expert-tier values; adversarial wrong-insight items | insight recorded first, labelled as expert-tier in the memo; never written back; reported with the diff | open |
| B20 | Homogeneous panel | all roles on one model family | correlated errors; a skeptic that agrees with itself | heterogeneous arm (§D.3.3) | different families per role; mechanical checks over LLM judges | open |
| B21 | Gate blind spots | right number from the wrong cell; negated evidence; small round numbers in quotable text | fabrication that resolves | adversarial tier 3; organic gate measurement | cell-identity binding; polarity and unit normalisation; gate at every handoff (§D.3.5) | partly |
| B22 | Stale cache | cache key omits the system prompt and schema | an old answer evaluated as a new configuration | byte-identical recordings after a prompt change | system prompt and schema in the key before any benchmark run | open |
| B30 | The cell's own label in the evidence pack | `label_context` returns "occurrence at 0.0 km" for a labelled cell | the analyst reads the answer off the pack | any scored run where the pack contains a label at 0 km | mask the evaluated cell's own label in scored runs; keep it in the dashboard | open |
| B23 | Compute asymmetry between configurations | a panel makes more tool calls than a single agent | more evidence, not better reasoning, wins | calls and cost per answer logged | matched compute budget per configuration (§D.3.3) | partly |

**Evaluation and people**

| Id | Bias or leak | Where it enters | What it would do | How we detect it | How we would fix it | Status |
|---|---|---|---|---|---|---|
| B24 | Benchmark authorship | we write the questions, the rubric and the configurations | a benchmark tuned to what we already do well | benchmark frozen before configurations are tuned; a held-out question split | tier 3 grows only from production failures; the frozen version is the one reported | open |
| B25 | Single rater who built the system | the geologist-day is one person, and the reasoning tiers are rated by the author | ratings drift toward the design | configuration labels blinded; order randomised; reported as one rater | the geologist-day (§D.3.4) with chance-corrected agreement | open |
| B26 | Judge shares a family with the judged | LLM-as-judge on rubric items | the judge forgives its own habits | judge and judged from different families; mechanical items first | rubric items that can be checked by code are; the rest rated by a person | open |

**Interface and dashboard**

| Id | Bias or leak | Where it enters | What it would do | How we detect it | How we would fix it | Status |
|---|---|---|---|---|---|---|
| B27 | Score before evidence | the map shows a coloured cell before the geologist sees why | anchoring on the colour | usage: evidence opened before or after the score | evidence-first mode; the effort score one click from every learned score | open |
| B28 | Colour ramp | low cells invisible or nudged toward attention | the ramp editorialises | the declared colour exception and its test | ramp reviewed with the geologist; the exception stays the only one | partly |
| B29 | Wording drift | "prediction", "target", "potential" creep into copy and names | the product claims skill it has not shown | the forbidden-phrases test | verdicts top out at "supports a closer look"; live readings are readings, not predictions | handled |


---

## 8. The agentic system — three agents, one runtime

Three agents, one runtime, one gate. Each agent is a loop over the same tool contract, and the gate is
middleware that runs at every handoff, not a check at the end. The analyst design starts from MineAgent and
STA-CoT, translated from images to a structured evidence record; neither is ground truth, both are the
only measured starting points.

### 8.1 Shared runtime (must)

- **One tool contract** over MCP (§E.3): every tool returns values with ids, every call is traced with its
  arguments, result ids, cost and latency. A model never computes; a tool always does.
- **Gate as middleware**: every message that crosses an agent boundary (tool → agent, agent → agent,
  agent → store, agent → screen) is checked for number-to-id resolution, cell identity and polarity before it
  is passed on. A rejected message goes back to its producer with the unresolved number, never forward.
- **Run manifest** per invocation: prompt versions, model per role, fold and model version of every score the
  agent saw, retrieval blind-list, seed, budget. A run is replayable from its manifest and the cache; the cache
  key covers system prompt and schema (closes B22).
- **Budgets** per run and per configuration, checked before each call, with the spend ledger already built.
- **Tracing** with OpenTelemetry so the benchmark can compute per-stage metrics from the traces, not from logs.

### 8.2 Extractor agent — reading loop (must, with §B and §C)

For each page or chip: locate → read under a schema → validate → file under `read`.

1. **Locate**: layout and OCR where a text layer is missing; page, box and verbatim quote recorded for every
   candidate value (already the contract for the 1,846 pages read).
2. **Read**: a VLM call constrained to the value schema for that page type (collar, assay interval,
   lithology log, legend, map unit). Output is a set of candidate values, each with quote and box.
3. **Validate**: deterministic checks — CRS and datum, units, physical ranges, the quote located on the page,
   collar within the claim block. A value that fails stays flagged and never becomes a feature.
4. **Agree**: a second read by a different model family; values the two readings disagree on go to a review
   queue with both readings shown. Agreement rate is a tracked metric.
5. **File** under `read` with reader model, prompt version and validator results, so §C can choose which
   readings feed a feature.

Scored by Tier 4 against hand-keyed gold; the published bars are in §D.3.1.

### 8.3 Interface agent — router (must, with §E)

The chat panel. It adds no signal; it finds, explains, records and invokes.

- **Intent router** over a fixed set of question kinds: lookup, compare, explain a score, what is unknown
  here, what would change the reading, record an insight, run the analyst. Unrouted questions go to a plain
  tool loop capped at a small number of calls.
- **Explicit abstain tool.** GeoBenchX showed refusal is only measurable when there is a tool to call; ours
  returns the reason (not measured here, outside the grid, would need a value the store lacks).
- **Record insight**: writes a geologist's statement to the `expert` tier with author, time and cell, and
  returns its id. Only then can any agent cite it.
- **Invoke analyst**: hands over the cell, the out-of-fold score ids, the expert-tier ids and the reason for
  invoking; receives a gated chain and reports the verdict with the diff against the run without the insight.
- Scored on tiers 1–3 and by the MineTRACE rating protocol (§D.3.1, §D.3.4).

### 8.4 Analyst agent — multi-step reasoning over the evidence record (must)

MineAgent judges each *image* and aggregates; STA-CoT plans, executes each step against a *target area*, and
verifies the chain. We judge each **criterion**, execute each step against the **cell and its neighbourhood**,
and verify the chain twice: once mechanically, once with a model.

```mermaid
flowchart TB
  IN["Cell id + fold + blind-list"] --> S0

  S0["Stage 0 · Triage (deterministic)<br/>criteria, learned, effort scores<br/>coverage flags, score disagreement"]
  S0 -- "shallow" --> S6
  S0 -- "full" --> S1

  S1["Stage 1 · Plan<br/>segments from the handbook criteria:<br/>one per criterion, cross-checks, retrieval passes"]
  S1 --> S2

  S2["Stage 2 · Execute, per segment<br/>tool calls only: cell_features, nearby,<br/>coverage, crosscheck, retrieve<br/>node = status, strength, evidence ids, text"]
  S2 --> S3

  S3{"Stage 3 · Mechanical verify (gate)<br/>ids resolve · cell identity · polarity<br/>unknown vs absent · no arithmetic"}
  S3 -- "reject node" --> S2
  S3 -- "pass" --> S4

  S4{"Stage 4 · Chain verify (model)<br/>does the verdict follow from the nodes?<br/>contradictions · effort acknowledged<br/>returns isValid + faulty nodes + feedback"}
  S4 -- "faulty nodes, round < K" --> S2
  S4 -- "valid, or K rounds" --> S5

  S5["Stage 5 · Decide<br/>(a) weighted sum, weights fitted out-of-fold<br/>(b) model adjudicator over nodes + effort null<br/>majority vote over rounds if K reached"]
  S5 --> S6

  S6["Stage 6 · Publish<br/>chain + verdict + the one observation<br/>that would change it → gate → agent tier"]
```

**Stage 0 — Triage.** Deterministic and free. Computes the three scores, the effort null, coverage and the
disagreement between them for the cell. Decides depth: a cell where all scores agree and coverage is full may
take the shallow path (a single gated summary); disagreement, gaps or an expert-tier value force the full
chain. GeoDecider's design: numerical prediction first, tool-assisted reasoning only for the hard intervals.
Whether triage is used at all is a configuration, because it is also a place to hide cost.

**Stage 1 — Plan.** The handbook and the criteria table are K_domain; the tools are T_set. The plan is a
list of segments: one per criterion (conductor distance, conductor density, fault distance and intersections,
graphitic host, unconformity depth, lake sediment conditioned on sampling density, lake water with pH and Eh,
boulder field with up-ice corridor, alteration where measured), then cross-checks (conductor ∧ fault,
geochemistry ∧ sampling density), then retrieval passes with the queries to run. A fixed template is the
"without planner" arm; a model planner may reorder, add retrieval queries and drop criteria the coverage flags
mark unmeasurable.

**Stage 2 — Execute.** One call per segment on the cheap model. The executor may only call tools and must
return a **node** in the fixed protocol: criterion, status ∈ {met, not met, unknown}, strength 0–5, the value
ids that support it, one sentence. This is MineAgent's communication protocol, and the component their
ablation shows carries the gain (Pos.F1 32.5 without it, 61.2 with). `nearby(cell, layer, radius)` is our box
maker; `crosscheck` is our spatial-relationship explorer; both are deterministic.

**Stage 3 — Mechanical verify.** The gate on every node: every number resolves to an id returned in this
run, the id carries the cell in question or a declared neighbour, polarity matches (a "not met" node cannot
cite evidence the handbook lists as favourable without saying so), unknown is used only where coverage says
unmeasured, and no arithmetic appears. A rejected node goes back to Stage 2 with the reason. Free, so it runs
every time.

**Stage 4 — Chain verify.** One call per chain on the strong model, with the skeptic's brief: does the
verdict follow from the nodes, do any nodes contradict, is the effort null acknowledged, is a folklore
criterion carrying weight, is proximity to a known deposit doing the work. Returns isValid, the faulty node
ids and corrective feedback; faulty nodes are re-executed and the chain re-verified, up to K rounds. STA-CoT's
finding that the verifier is where capacity pays (strong verifier + weak executor beats the reverse) is why
the executor is the cheap model and the verifier the expensive one, and why that pairing is a configuration.

**Stage 5 — Decide.** Two deciders, always both, compared:
(a) a **weighted sum** over node strengths with weights fitted on training folds only — MineAgent's decision
module and MineTRACE's fitted expert weights, and the fitted-weights criteria arm of §C in one place;
(b) a **model adjudicator** reading the nodes and the effort null, producing the three-level verdict
(evidence against / insufficient / supports a closer look), the unknown-vs-absent list and the one observation
that would change the reading. For the benchmark's binary question, (a) supplies a calibrated score and (b)
supplies a label. If K rounds were exhausted, the label is the majority over rounds.

**Stage 6 — Publish.** The chain, the verdict and its inputs go through the gate once more as a whole and
land in the `agent` tier with the run manifest. Nothing here is a source of numbers.

**Leakage controls, enforced by tests, not policy**: the scores in Stage 0 and Stage 5 are out-of-fold for
the cell's block (B18); `retrieve` refuses files on the blind-list for the cell and its neighbours (B17);
expert-tier ids are labelled as such in every node that cites them (B19); `label_context` masks the
evaluated cell's own label and any label inside it in scored runs (B30), while the dashboard keeps showing
it, because the nearest known occurrence is the first thing a skeptic should ask.

### 8.5 Ablation matrix — the configurations §D compares (must)

Each row is one switch on the loop above, run on the same frozen benchmark at matched compute. The matrix
replaces the six configurations formerly listed in §D.3.3.

| Switch | Arms | What it tests |
|---|---|---|
| Triage | off / on | whether the shallow path loses correctness for the cost it saves |
| Planner | fixed template / model | STA-CoT's −14.6 Pos.F1 without a planner — does it hold on structured evidence |
| Executor | single shot over the whole pack / per segment | STA-CoT's −56 without an executor; ours is where the protocol lives |
| Mechanical gate | off / on | the free layer; the only arm allowed to publish is "on" |
| Chain verifier | none / neutral / skeptic brief | STA-CoT's −29 without a verifier; the skeptic brief is today's panel |
| Model pairing | cheap executor + strong verifier / strong both / reverse | the executor–verifier trade-off |
| Decider | weighted sum / adjudicator / both | fitted weights vs judgement |
| Rounds K | 1 / 3 | repair vs cost |
| Self-consistency | n = 1 / 5 with vote | majority vote as a fallback vs as a default |
| Retrieval | off / on / on with blind-list | B17, measured |
| Families | homogeneous / heterogeneous across roles | B20 |
| Baselines | random · copy the learned score · criteria · learned · effort null · single-shot model with no tools | the rows every arm must beat |

Per-stage metrics from the traces: node rejection rate at Stage 3, verifier catch rate and rounds-to-valid
at Stage 4, agreement between the two deciders at Stage 5, calls and cost per chain, and the correctness of
each arm against the labels with the effort null on the same row.


---

## 9. Scope boundary — what this prototype will actually run

Data first, then two focused tasks, then a backlog. The extractor cannot read the corpus and the analyst
cannot read the grid: the numbers below say why, and what fits instead.

### 9.1 Data readiness gate (must, before any agent phase)

No agent phase starts until every dataset the focused tasks depend on is green on all five columns. The
checklist lives in §B's catalogue; this is the rule and the current picture.

| Column | Meaning | Where we are (2026-09-19) |
|---|---|---|
| **Present** | in the store under its tier, row counts known | 11 layers, 56 scenes registered, 1,534 corpus files at metadata level, 24 features over 30,534 cells |
| **Licensed** | redistributable, or read locally and never served | 18 of 20 sources redistributable; report PDFs local only |
| **Covers** | share of the grid with a value, stated per feature | 9 of 17 geological features cover 40% or less |
| **Servable** | exportable to the dashboard within its licence and size | 8 evidence layers exported; scores as a layer; imagery features not yet built |
| **Versioned** | a hashed pull that a value walks back to | pulls cached and hashed; no snapshot versioning yet (§B) |

**What has been read so far, and whether it is enough.** Reading is a pre-processing step done offline on
the Claude Code headless backend, cached and resumable; it is never done at request time.

| Read tier today | Count | Where it sits |
|---|---|---|
| Reports fully read (VLM, every value with page, box, quote) | 4 — Uranerz 1979, Cree Lake 2005, Conwest 1978, Cameco Park Creek 1998 | all `dev` split; 2 in occurrence cells, 2 in unlabelled cells |
| Pages the router sent to the model | 42 of the 4 reports' 691 pages (**6%**) | collar, assay and lithology tables |
| Values read | 2,175 field values; 40 collars, 43 assay intervals, 126 lithology intervals | 176 flagged, 11 failed by validators — the `read` tier is unvalidated by design |
| Text-indexed pages (no model) | 1,846 pages from 8 files (38 documents, 9.1 M characters) | 3 occurrence cells, 5 unlabelled cells — **0 of the 60 deposit cells** |

Verdict: **enough to prove the mechanism, not enough to serve the dashboard's cells.** Every score is
computed from `native` layers alone (all 24 features), so the deterministic and learned analyses need no
reading at all. Reading matters only for the evidence the analyst and the dashboard show on the enabled
cells, and none of those cells is a deposit cell today. The bounded pre-read in §9.3 fixes that.

The four recorded gaps (aeromagnetic grids, discovery dates, EM conductor attributes, alteration measurements)
are decided in Phase 2, each as *filled*, *substituted* or *stated as absent on screen*. Sentinel-2 and DEM
features are **not** required by the focused tasks below and move to the backlog with the multimodal arm.

### 9.2 Why the whole corpus and the whole grid are out

Measured on this machine, from the run manifests and the store:

| Quantity | Measured | Full scale | Verdict |
|---|---|---|---|
| Reading, pages per hour | 42 pages in 3,012 s of model time, about **73 s per page**, in 41 calls | 1,534 files at 30–100 pages each is 50,000–150,000 pages, so 1,000–3,000 hours | **out** |
| Reading, usage per page | the run's own accounting: **$0.14-equivalent per page**, 391,592 output tokens for 42 pages | 50,000 pages is $7,000-equivalent of subscription usage | **out** |
| Analyst, calls per cell | 3 per cell today (panel); the §8.4 loop is about **13** (10 segments, verifier, adjudicator, publish) | 30,534 cells is 400,000 calls per configuration | **out** |
| Analyst, wall time | chat-length calls run 15–30 s on the headless backend | 120 cells × 13 calls is 1,560 calls, **6–13 hours per configuration**, unattended | in, bounded |
| OpenAI | $0.00 of the $2.00 test ceiling spent | reserved for the chat UI, as agreed | unchanged |

### 9.3 Focused task 1 — Reading, pre-done offline for the enabled cells (extractor)

The corpus is already positioned where it matters: **184 assessment files sit inside 53 of the 60 deposit
cells** (5,035 drillholes), 314 inside 189 of the 620 occurrence cells, and 294 within 3 km of a deposit cell.
The current fetch rule (the 60 files with the most holes) lands 27 in deposit cells and 7 in occurrence cells,
which is exactly the survivorship bias B13 names.

- **Enabled cells first.** The dashboard's analyst-enabled set (§9.4) is chosen before any reading, and the
  reading is bounded to the files inside those cells. Proposed list of **15**: the five cells the panel and
  recorded chat already use (Horseshoe 0201_0072 and four unlabelled cells chosen for score disagreement,
  gap and background); the four cells of the reports already read; and two deposit cells per camp with the
  fewest files that still carry drilling — Colette 0030_0081 and Dominique-Peter 0031_0088 in the west,
  0037_0049 and R780E 0033_0047 at Patterson Lake South, Deilmann 0145_0019 and Horseshoe 0200_0072 in the
  east. Frozen and versioned with its selection rule in Phase 2.
- **Fetch and index everything in the enabled cells, read almost nothing.** All 47 files inside the 15
  enabled cells are downloaded and text-indexed with no model calls, so the analyst has the page tier
  (quotable, numbers not allowed) and the metadata tier ("six files exist here, two were read") for every
  file. Only the **top two drilling files per cell** are model-read: 21 files, of which 4 are done, about 230
  routed pages, under 5 hours. Four enabled cells have no file at all, and "no report exists" is their honest
  state.
- **Why not more.** A few reports prove the mechanism (four exist). One or two per cell make each enabled
  cell's verdict honest. Reliable negatives for the learned model need hundreds of cells, which tens of files
  do not reach either, so that is backlog whatever we read now. There is no useful middle.
- **Route, then read — one page image per call, never a report.** Every fetched page is text-indexed or
  OCR'd for free; the router classifies each page by keywords and table shape with no model call; at most 12
  routed table pages per file are then sent **one page image at a time** with the schema for their class, and
  every returned quote is located on the page and validated before it is filed. The router already sends
  about 6% of pages to the model (42 of 691). This is a batch job on the headless backend, not an agent:
  no tool loop, no conversation. The interface and analyst agents never open a PDF; `retrieve` reads the
  index and the filed values.
- **Prototype on Claude Code headless, production on an API.** Every call already goes through the
  `Backend` protocol, so the swap is configuration — except that the OpenAI backend has no image input
  yet, so the extractor cannot run on it (backlog, §9.6). At API prices the bounded read is cheap: about
  9,300 output tokens per page measured, roughly $0.02–0.03 per page on gpt-5-mini, so 230 pages is under $7.
  Whether the cheaper reader is good enough is what the 30-page gold measures.
- **Hand-key 30 pages** from those files as the first Tier 4 gold, chosen blind to the model; score the read by
  precision and recall against them and by second-family agreement (§8.2).
- Everything else the corpus holds is **backlog**, and the readiness scorecard says how much of it there is.

### 9.4 Focused task 2 — Analyst, bounded to the enabled cells in the dashboard

- **In the dashboard, the analyst runs only on the enabled cells** (§9.3, about 12). Their chains are
  computed offline with the §8.4 loop, stored in the `agent` tier with run manifests, and served as-is. Every
  other cell shows its three scores, the effort null and the evidence rail, and a plain statement that no
  analyst reading exists for it. A live reading (§6) is available only on enabled cells, within a per-session
  budget.
- **The benchmark subset is sized later.** The four-arm design and the baseline rows (§8.5) stand; the number
  of cells, the class balance and the nights of compute are decided when Phase 5a starts, against the usage
  actually available then. The earlier sketch (120 cells, four arms, about 1,560 calls per arm) is the
  starting point for that discussion, not a commitment.
- **Deterministic and learned analyses are unbounded.** They run over all 30,534 cells from existing `native`
  data; the cost is machine time only. Deep learning (a CNN over chips, a transformer over the feature table)
  is **backlog**: no chips yet, and 60 positives is too few to fit one honestly.

### 9.5 Focused task 3 — Interface, the mechanical tiers

Tiers 1 and 3 are generated by code and need no model to build; running them is 400–800 calls, one arm,
a few hours. Tier 2 is bounded by the rater, not the machine: **30 questions × the top 2 arms**, the
MineTRACE protocol, one day.

### 9.6 Backlog, stated as such

| Item | Why not now | Unblocks when |
|---|---|---|
| The other eight switches of the §8.5 matrix | each is another 6–13 hours per arm | the four-arm table shows where the variance is |
| Multimodal arm and Tier 4 chip agreement | Sentinel-2 and DEM features not built; effort mask (B15) not designed | §B.2 chips exist |
| Deep learning models (§C.2.2) | no chips; 60 positives | §B.2 chips exist and the PU re-test says the labels support it |
| Image input on the OpenAI backend, so the extractor can run on an API | prototype reads on Claude Code headless | Phase 4a, before any production reading |
| Analyst benchmark subset sizing (§9.4) | depends on usage available at Phase 5a | Phase 5a, discussed then |
| Reading beyond two drilling files per enabled cell | tens of files cannot reach the hundreds of cells reliable negatives need | a page-type classifier picks only table pages, or a cheaper reader is measured against the gold |
| Analyst on demand for any cell outside the subset | correct design (§6), but every call is unbudgeted until 4a's manifests exist | Phase 4a |
| Heterogeneous families across roles | needs a third backend | an open model adapter |
| Embedding retrieval over the corpus | lexical first, by design | a measured recall gap on the text index |
| 1 km and 5 km sensitivity | compute and a second grid | Phase 3 |
| LLM-derived text features (§C.2.2) | needs the 200 fetched files first | Focused task 1 done |


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
- **Deep learning is backlog** (§9.6): a CNN over Sentinel-2 and DEM chips needs chips that do not exist yet,
  and a transformer over the feature table needs more than 60 positives to be fitted honestly.
- **Ablations**: each geological feature group removed in turn; effort features added to the learned model
  (does geology add *anything* on top of effort?).
- **LLM-derived features arm** (the extractor role, §D.1): text embeddings of the assessment-report passages
  and bedrock-unit descriptions attached to each cell, added to the learned model and run with and without
  under the same spatial folds — QueryPlot's design, where the added layer moved balanced accuracy 72.4 → 78.4
  on one classifier. Parsa et al. 2025 note that mineralised areas carry longer, richer text; that is effort
  leaking through the text channel, so this arm is only read beside the effort null.
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

### D.1 The stance, and where the literature actually is
Every published use of an LLM in mineral exploration fits one of three **roles**, cross-cut by what the model
reads. The grid was checked on 2026-09-19 by five independent sweeps (139 searches, English and Chinese; every
source fetched before it was placed):

| Role of the LLM | Text and tables | Images |
|---|---|---|
| **1. Extractor** — makes features or structured records; a separate model judges | Parsa et al. 2025 (BERT embeddings into a transformer; 87% search-space cut). Chen et al. 2026 (LLM-extracted priors; ROC-AUC 0.83). QueryPlot 2026 (the one clean ablation: balanced accuracy 72.4 → 78.4 with the LLM-derived layer). GeoChemAD 2026 (LLM element selection; AUC 0.74 vs 0.64 manual). | DIGMAPPER (GPT-4o legend extraction, F1 0.88 on >100 USGS maps). Gans Combe 2026 — the only uranium-specific LLM paper anywhere: Gemini 2.5 Pro sorting 973 scanned Athabasca and Namibia filings, 92% / 60% by level. |
| **2. Interface** — gathers data and explains a deterministic model through tools; adds no signal | MineTRACE (92% Good, one fabrication in 150). GISclaw (orchestrates a random forest). China Geological Survey's 智能找矿 (200+ algorithms, no published score). | GeoMap-Agent (tools and databases over geologic maps; 0.811 vs GPT-4o 0.369). ThinkGeo (tool choice 74% right, arguments 37%). Both let the LLM compose the answer, so neither is a pure interface, and none sits over a scoring model. |
| **3. Analyst** — makes the prospectivity call itself, and the call is scored | **Nothing published.** Two independent 33-search attempts to fill this cell failed. Nearest: GeoDecider 2026, an LLM agent over numeric well logs that emits a lithology label. No paper compares an LLM head-to-head with a random forest on a prospectivity feature table. | MineAgent and STA-CoT on MineBench — the only two. |

Three things follow. **The analyst-over-structured-data cell is empty**, so the experiment in D.3.1 has no
external comparator and its comparators must be internal. **The general tabular prior is unfavourable** —
XGBoost 0.94 against TabLLM 0.92 on a standard benchmark, with LLMs competitive only below roughly 256
labels — which is exactly why the learned score and the effort null sit beside the agent on the same rows.
And **every number in this section will be the first of its kind for uranium**.

The literature also reports that *some* multi-agent configurations do not beat a single agent on *some*
tasks. That is not a finding about ours. Nobody has measured an LLM adjudicating grounded geological evidence
behind a mechanical gate, because nobody has built one. **The prototype's job is to measure it.** We may find
our panel wins, loses, or wins only in one configuration — all three are results worth having.

### D.2 Current state
One configuration (three roles on one backend, custom tool loop, gate at publish). One synthetic gate eval.
Fourteen memos and a handful of chats. No task-level correctness measurement at all.

### D.3 Requirements

**D.3.1 UraniumBench — one benchmark, three roles (must)**
The agent plays all three roles in this system, and each role is scored the way its literature scores it.
Role 1 is shared with §C; roles 2 and 3 are this section's.

*Role 3 — analyst.* Two arms, kept separate because their comparators differ.
- **Structured arm (first).** The agent reads a cell's evidence record — the 17 features with their
  observation counts, the criteria memberships, the coverage flags, the located quotes — and answers "does
  this cell hold a deposit?". Scored by Pos.F1, ROC-AUC and MCC against the label layers over a stratified
  subset (known deposit / high score far from labels / coverage gap / background). No external comparator
  exists, so the comparators are internal and on the same rows under the same 30 km spatial folds: the
  criteria score, the learned score, and **the effort-only null**. An agent that beats the effort null has
  read geology; one that merely matches it has read drilling history. Class balance is stated beside every
  number: 1:44 with occurrences, ~1:500 on deposits alone.
- **Multimodal arm (after §B.2 chips exist).** The same question with the cell's Sentinel-2 chip and
  geology-map tile attached. This is MineBench for uranium. MineBench (Yu et al., arXiv 2412.17339) is a
  **copper** benchmark over Western Australia: per 12 × 12 km cell the model sees 2, 4 or 9 ASTER-derived
  alteration and geology images by difficulty tier, no tables, 73 positive / 539 negative, labels from deposit
  records. On GPT-4o, Pos.F1 goes 34.9 → 61.2 with MineAgent's scaffolding → 63.1 with STA-CoT; on Gemini 2.0,
  37.5 → 60.2 → 66.7; STA-CoT loses 25.6 Avg.F1 without its verifier. Those are the numbers a reviewer will
  hold beside ours, so this arm reports the same metrics and states its class balance next to their 1:9.

*Role 2 — interface.* The chat panel. Not scored on AUC — the LLM is not supposed to add signal here — but
on faithfulness, abstention and rating, in three tiers over *our* data:
1. **Deterministic** (~300 items): questions with exact answers computable from the store — "how far is the
   nearest deposit", "which criteria are unknown here", "how much of the grid does this feature cover". Gold is
   generated by code, so correctness is exact and free. Stratified over cell types. Includes unanswerable
   items, where the gold is abstention: GeoBenchX is the only geoscience benchmark that publishes a
   correct-refusal rate (0.17 to 0.90 across eight models on 79 unsolvable tasks) and nothing in mineral
   exploration does, so this tier yields a refusal rate *and* a false-refusal rate with a denominator.
2. **Grounded reasoning** (~100 items): questions whose answer is a judgement over evidence — "is this score
   explained by drilling", "what is unknown versus not met", "what single observation would change the
   reading". Gold is a rubric written from the handbook; most required elements are mechanically checkable
   (the answer must cite the effort and learned scores by id, must name the unknown criteria), and only the
   judgement itself needs a rater.
3. **Adversarial** (~100 items): questions engineered from failures actually observed — a real number from a
   neighbouring cell (4 of 40 escaped the gate), an observation count with no citable id, a filename cited as
   an id, a negated premise, a folklore criterion phrased as fact, a request for a grade. Gold is the expected
   behaviour. The tier grows only from production failures, never from imagination.

*Role 1 — extractor (backlog, shared with §C).* The pipeline already reads images — scanned assessment pages
through a VLM, Sentinel-2 and DEM chips into features, the bedrock map into the graphitic-host criterion — so
this is in scope, not another modality. **Tier 4, Reading**, scores value extraction from scanned pages against
hand-keyed gold, legend and map-unit reading, and chip-to-feature agreement. The published bars: GPT-4o legend
extraction at F1 0.88 (DIGMAPPER, >100 annotated USGS maps); BERT at F1 87% on drillhole results from 50 ASX
reports with a company-level split (Dimeski & Rahimi 2022); an LLM at 100% on clean well-record PDFs and 70%
on scanned ones (Ma et al. 2024). Nobody has published precision and recall for grade or tonnage extraction
from NI 43-101 or assessment files — MinMod, at 680,000 sites, publishes scale and time saved, not accuracy —
so §C's hand-keyed gold set would produce the first such number. Backlog because that gold does not exist yet.
The **LLM-derived-features arm** (text embeddings of report and bedrock descriptions as inputs to the learned
model, with and without, under spatial folds — QueryPlot's design) lives in §C.2 and is scored there.

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

**D.3.3 Configurations to compare (must)** — same frozen benchmark, matched compute budget
The configurations are the ablation matrix in §8.5: each switch on the analyst loop (triage, planner,
executor, mechanical gate, chain verifier, model pairing, decider, rounds, self-consistency, retrieval,
families) plus the baseline rows every arm must beat. Report the full matrix. The prototype ships whichever
wins on tier 2 correctness at acceptable tier 3 faithfulness, and beats the effort null on the analyst rows —
chosen by the table, not by preference.

**D.3.4 Human adjudication (must, bounded)**
One geologist-day: 30 questions × the top 2 configurations, rated on a fixed rubric (MineTRACE's protocol,
copied), reported with chance-corrected agreement. This converts the benchmark from internal to comparable.

**D.3.5 Gate hardening (must)** — done before the benchmark runs, so the benchmark measures the real gate
- Cell-identity binding: a cited id must carry the cell in question.
- Polarity and unit normalisation.
- Gate at every handoff (proponent → skeptic → adjudicator → publish).
- Organic evaluation: the benchmark's adversarial tier *is* the organic gate measurement.

### D.4 How we will know
- A results table: 6 configurations × (role 3 structured arm + role 2 tiers 1–3) × 7 metrics, with intervals,
  on the Eval page; the role 3 rows carry the criteria, learned and effort scores as their comparators.
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
| **2 · Data ownership** | 1–2 | §B: gap re-verification (magnetics first); freeze the 15 enabled cells with their selection rule (§9.3); fetch and text-index their 47 files; pre-read the top two drilling files per cell with the existing pipeline (17 files, about 230 routed pages, unattended — **can start now, in parallel with Phase 0**); versioned snapshots | Every on-screen value walks to a hashed source pull; **the §9.1 readiness checklist is green for the focused tasks** |
| **3 · ML programme** | 2 | §C.2.2–C.2.4: MLflow, registry, candidate models, ablations, sensitivity, CI regression | Eval page links every number to a run |
| **4a · Runtime and tool contract** | 1 | §8.1, §E.3: MCP server over the six tools plus abstain, record-insight and run-analyst; gate as middleware at every handoff; run manifests; tracing; cache key covers prompt and schema | A stock client gets a gated answer; a run replays from its manifest; B22 closed |
| **4b · Extractor agent** | 1 | §8.2: schema-constrained reading loop, validators, second-family agreement, review queue; `expert` tier schema | The Phase 2 pre-read re-run through the new loop with second-family agreement; 30 hand-keyed pages as the first Tier 4 gold; precision and recall measured against them |
| **4c · Interface agent** | 1 | §8.3: intent router, abstain tool, record-insight, invoke-analyst, session assessment with diff | Tier 1 pass rate, refusal and false-refusal rates measured with a denominator; the 30 rating questions drafted |
| **4d · Analyst agent v1** | 2 | §8.4: stages 0–6 on the structured arm; both deciders; out-of-fold scores and blind-list enforced by tests | Chains computed and stored for every enabled cell (§9.4); every node gated; B17 and B18 have failing-then-passing tests |
| **5a · Freeze UraniumBench v1** | 1 | §D.3.1: tier 1 generated (~300), tier 3 from observed failures (~100), tier 2 rubric (~100), the analyst subset sized at this point (§9.4) with fold ids; blind-list in the store; benchmark hashed and versioned before any tuning | No model calls yet; the frozen hash is the one every later table cites (B24) |
| **5b · Baselines** | ½ | §8.5 baseline rows: random, copy-the-score, criteria, learned, effort null, single-shot model | The baseline row of the table, with intervals |
| **5c · Four arms** | 2 | §8.5: the four arms and the baseline rows at matched compute on the subset sized in 5a; per-stage metrics from traces; the other eight switches stay in §9.6 | The four-arm table with intervals, on the Eval page |
| **5d · Human adjudication and decision** | 1 | §D.3.4: one geologist-day on the top two configurations, MineTRACE protocol, chance-corrected agreement; the written finding | The table decides the shipped configuration; the finding names the number that decided it |
| **5e · Multimodal arm** | after §B.2 chips | §8.4 with the chip and map tile attached; §D.3.1 MineBench-for-uranium numbers | Backlog until chips and their effort mask exist (B15) |

Phase 0 is first because it is the only one that can change what the rest of the document is *for*. Phases 4 and
5 total about nine weeks, up from three to four in v0.1, because a loop with a verifier, two deciders and a
frozen benchmark is what separates a measured agent from a demo.

## 13. Risks

| Risk | Consequence | Mitigation |
|---|---|---|
| The headline does not survive corrected sampling | the central story changes | Phase 0 finds out first; a retraction on public data is itself a credible result |
| Aeromagnetic grid does exist and we said it did not | credibility | re-verify in Phase 2 before any external claim |
| Postgres migration stalls the demo | nothing to show mid-project | static-export path stays alive until parity; feature-flag the API |
| Geologist-day never happens | benchmark stays internal | tiers 1 and 3 are fully mechanical and stand alone |
| LLM spend | budget | ceilings per backend already exist; the benchmark is cost-capped per configuration |
| Framework sprawl in the agent layer | more places to invent a number | §E.4: in-house runtime, revisit only on measured need |
| A bias in §7 goes unattacked | the number that decides the prototype is an artefact | every §7 entry has an owner section and a detection test; the register is reviewed at each phase gate |

## 14. Deliberately out of scope for the prototype

Fine-tuning any generator; a fourth critic role; LLM-judge refinement loops over extracted tables; evaluation
against text-recall geology benchmarks; any commercial or company data; any claim about ground a company holds.

---

*Next: pick a section and go deeper. Recommended order is §12's — but §A (the platform) is the one with the
most decisions to make together, and §C Phase 0 is the one that can start today.*
