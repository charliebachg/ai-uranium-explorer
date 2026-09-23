# AI Uranium Explorer — Product Requirements

**What this document is.** The product in a page, the requirements per area, the standing constraints, and
the backlog. How each piece works, control by control and stage by stage, is in GUIDE.md; this document says
what the product is for, what it must do, and what is left. Cross-references name GUIDE sections.

---

## 1. The product

**AI Uranium Explorer turns public uranium exploration records for the Athabasca Basin into scored 2 km
cells, and lets a person interrogate any score through an agent that can only speak from the evidence that
produced it.** Every number on screen resolves to a stored value, every model is read against a null that
encodes exploration effort, and every claim an agent makes is checked by code before it is shown.

It is for two things. First, to show how much of a score over this ground is explained by where people
already drilled, so that a ranking discloses its own exploration history instead of presenting it as geology.
Second, to show a way of putting a language model beside geological evidence without letting it invent a
number: tools compute, the model argues, a gate checks. It is a prototype on public data. It gives no drilling
advice and makes no geological judgement of its own; verdicts top out at "supports a closer look" (GUIDE:
What this is, in one paragraph).

## 2. Who it is for

| User | Their job here | What they need from the system |
|---|---|---|
| Exploration geologist | decide whether a cell deserves a closer look | the evidence itself, unknown kept apart from absent, and the one observation that would change the reading |
| Exploration manager | spend a finite programme across ground | search-space reduction beside capture rate, and a ranking that says how much of it is drilling history |
| AI engineer | build, validate and scale the system | reproducible runs, benchmarks with denominators, a tool contract any client can use, an architecture that survives ten times the data |

## 3. The prototype's acceptance story

One person opens the dashboard and:

1. sees the data and its readiness: which sources are present, licensed, covering, servable and versioned,
   and which gaps are recorded (the data page);
2. inspects the model results on the Eval page: the three scores beside the effort null under spatial folds,
   the dated hindcast, and the analyst arms with their per-stage columns;
3. opens an enabled cell and reads the analyst's chains on it, node by node, every number an id, with the
   verifier's verdict and the one observation that would change it;
4. talks to the interface agent about that cell, is answered from tool values or refused with a reason, and
   records an insight of their own into the expert tier;
5. has the agent call the analyst on the cell as a background job, and reads the verdict beside the stored
   chain without the insight.

Every model call in that story runs on a cheap pay-per-token model. The benchmark (the analyst arms on the
frozen subset, the interface agent on the mechanical tiers) runs afterwards, once the story works end to end.

"Working" means a person unfamiliar with the code can do the five steps, click any number through to its
source, and be told plainly when the system does not know; and an engineer can reproduce every score and
every agent answer from a versioned input and start the whole thing on a fresh machine from one command.

## 4. The shape of the system

**One store, five tiers.** Every value lands under a provenance tier and keeps it: `native` (as published by
the source), `read` (read from a scanned page by a model, with page, box and verbatim quote; unvalidated by
design), `derived` (computed, inputs recorded), `agent` (argued by a model; never a source of numbers) and
`expert` (a geologist's own statement, recorded with author, time and cell before any agent may cite it).
Held-out files and held-out benchmark cells are never rendered, extracted or retrieved (GUIDE: Provenance
tiers).

**Three scores and one control.** The `criteria` score is a hand-written table of cited thresholds with
folklore rows at weight zero; the `learned` score is the best model in a registry that beats the null under
spatial folds, or none; the `effort` score is a model over exploration-effort features only (drilling and
survey footprints) and is the control the other two are read against. All three are computed over the same
cells under the same spatial folds and shown with the base rate beside them (GUIDE: How a cell is analysed).

**The analyst is a staged loop.** Triage decides depth from the scores and coverage. A plan lists one segment
per criterion plus the cross-checks. An executor answers each segment with tool calls only and returns a
node: criterion, met / not met / unknown, strength, the value ids it rests on, one sentence, and the nodes it
depends on. A mechanical node gate refuses any node whose numbers do not resolve, whose ids belong to another
cell, whose polarity is wrong, or that reads an unmapped layer as absent, and sends it back with escalating
feedback. A chain verifier on a stronger model asks whether the verdict follows from the nodes and names the
faulty ones for re-execution, up to a fixed number of rounds. Two deciders, a weighted sum and a model
adjudicator, always both run and are compared. The chain publishes to the agent tier through the gate once
more, with the one observation that would change it (GUIDE: The staged analyst).

**The interface agent adds no signal.** A router classifies each question into a fixed kind (lookup, compare,
explain a score, what is unknown here, what would change the reading, record an insight, run the analyst,
other); each kind has a deterministic plan of tool calls; one gated answer call speaks from those values. It
has an explicit abstain tool that records the reason (not measured, outside the grid, no value, out of scope),
so refusal is measurable rather than guessed past. It records insights to the expert tier and invokes the
analyst as a job, then reports the verdict with the diff against the chain without the insight (GUIDE: The
chat).

**The extractor is a loop per page.** Locate, read under a schema for the page type, validate
deterministically, agree with a second read from a different model family, file under `read`. Values the two
families disagree on go to a review queue with both readings beside the page crop; a decision is recorded with
the reviewer's key and never rewrites a reading (GUIDE: The extractor as an agent).

**The MCP server** serves the same deterministic tools to the dashboard, to a geologist's own client and to a
benchmark harness: every number a value with an id, unknown and absent distinct types, errors as tool results,
sessions as handles so the gate can bind a claim to what was returned, scopes on keys, a public-safe mode, and
no sampling of the client's model (GUIDE: The MCP server).

**The API** has three roles (viewer, geologist, admin) as sets of the same scopes, records who asked on every
write, and runs anything longer than a second as a job with a durable row that a client polls or cancels
(GUIDE: Keys, roles and background jobs).

**The dashboard** is a map of the cells with the evidence layers grouped by what they are for, the score
views, an evidence rail with the chains, and the chat; plus the data, Eval, limits and review pages; and a
guided tour that walks the acceptance story with a recorded conversation (GUIDE: The dashboard, control by
control; The other three pages).

## 5. Requirements

Each area is a short list of musts and one line on how we will know.

### 5.1 Platform

- A typed API with an OpenAPI document the web client is generated from; conversations persisted turn by turn
  with every tool call, value id and gate verdict.
- A serving database with spatial queries beside the analytics store; the tiered schema and its audit hold in
  both.
- Vector tiles for every map layer, with a static fallback when the service is down.
- Roles by key; every write records who asked; a job for anything over a second, cancellable, and marked
  failed with the reason when the process restarts under it.
- One image, one compose file, one command from a fresh clone: the store arrives as a content-addressed seed
  pack whose every table is hash-checked on unpack, pulled from a URL when none is on disk; the public pack
  carries only redistributable tables.
- Tracing with OpenTelemetry on every agent run, tool call and model call; the backend is a configuration,
  not a dependency.

*How we will know:* a fresh clone reaches the running app in one command from a seed pack; the end-to-end
suite passes against the API; evidence reads stay inside their latency budget under concurrent readers in the
test.

### 5.2 Data

- An inventory of every source with licence, redistributability, verification date, what it bears on and the
  gap it would close, rendered on the data page. Gaps are re-verified, not assumed, and a recorded gap is
  stated on screen, not smoothed over.
- Lineage: a value walks back to a hashed pull; a feature snapshot rebuilds byte-identically from its
  recorded inputs; every run names the snapshot it read.
- A readiness gate with five columns (present, licensed, covers, servable, versioned) as a command and on the
  data page; no agent phase starts on a red row.
- Reading is a bounded offline job: every assessment file inside an enabled cell is fetched and text-indexed
  with no model calls; only the top drilling files per cell are model-read, one page image per call, every
  quote located on the page before it is filed. The agents never open a PDF.
- Labels in two tiers, deposit and occurrence, kept apart in every table.

*How we will know:* the gate is green for the enabled cells; the lineage check finds no break; the drift check
passes against the latest snapshot.

### 5.3 Machine learning

- Positive-unlabelled framing; spatial and leave-one-camp-out folds fixed before any fit; PR-AUC first, with
  capture at a fixed share of area and the base rate beside every number; intervals by bootstrap over folds.
- The effort null on the same rows as every model, always shown beside it.
- A tracked model search (every run with its parameters, folds, metrics, snapshot hash and commit) and a
  registry with a promotion rule: nothing is served that does not beat the null.
- The dated hindcast: datable inputs frozen at a cutoff, later discoveries ranked, and the leak in the
  compilations that cannot be dated stated beside the table.
- No imputation, no synthetic positives, no tuning on test folds.

*How we will know:* every number on the Eval page names its run and snapshot; a second engineer reproduces
the served model's metrics from the registry entry; the real-data regression test holds each arm inside its
stored interval.

### 5.4 UraniumBench: the agent benchmark

One benchmark, three roles, each scored the way its literature scores it. Frozen and hashed before any prompt
is tuned; a held-out share of cells stays sealed until the end.

- **Analyst.** A frozen subset of cells stratified over deposit, occurrence, drilled-unlabelled and
  never-drilled probes; per cell a pack with out-of-fold scores, the cell's own label masked, and a blind-list
  of the files that would give the answer away. Scored against the labels beside the criteria, learned and
  effort scores on the same rows: an agent that beats the effort null has read geology; one that matches it
  has read drilling history. Configurations are the switches on the staged loop (triage, planner, executor,
  node gate, verifier, model pairing, executor context, deciders, rounds, self-consistency, retrieval,
  families) at matched compute, plus the baseline rows every arm must beat. Per-stage metrics come from
  traces.
- **Interface.** Tier 1: deterministic questions with gold generated by code, including unanswerable ones
  whose gold is abstention with a reason, so the refusal rate and the false-refusal rate both carry a
  denominator. Tier 2: grounded-reasoning questions with a rubric. Tier 3: adversarial questions engineered
  only from failures actually observed, never from imagination: a real number from a neighbouring cell,
  an observation count with no citable id, a filename cited as an id, a negated premise,
  a folklore criterion phrased as fact, a request for a grade, absence of mapping read as absence.
- **Extractor.** Reading scored against hand-keyed gold pages by precision and recall per field type, and by
  agreement between two model families.
- Metrics per tier and configuration: correctness, gate pass rate, citation precision and coverage,
  unknown-versus-absent discrimination, abstention quality, calls per answer, cost and latency,
  self-consistency.

*How we will know:* an arms table on the Eval page with intervals, every row beside the effort null; tier 1
and tier 3 rates with denominators; a written line naming the configuration the prototype ships and the
number that decided it.

### 5.5 Tools and MCP

- A model never computes; a tool always does. Every tool returns values with ids; unknown and absent are
  distinct types in every output schema.
- The fabrication gate is middleware at every handoff (tool to agent, agent to agent, agent to store, agent
  to screen), and a rejected message goes back to its producer with the unresolved number. The gate record
  (GUIDE: The fabrication gate) names two holes the number check alone does not close:
  a correct value from the wrong cell passes; negated evidence passes. Cell-identity binding and polarity
  checks exist for those.
- One tool contract over MCP, on stdio and streamable HTTP, with resources for data and tools for actions,
  sessions as handles, scopes on keys, a public-safe mode that never serves non-redistributable text, and a
  span per call.
- A run manifest per invocation (prompt versions, model per role, fold and model version of every score seen,
  blind-list, seed, budget), so a run replays from its manifest and the cache; the cache key covers the system
  prompt and the schema.
- Budgets per run and per process, checked before each call.

*How we will know:* a stock MCP client connects, asks about a cell and gets a gated answer; the contract tests
pass; the benchmark harness can run against the server rather than against in-process functions.

## 6. Standing constraints

- Public data only: no logged-in sites, no paid data, no scraping behind logins; every dataset's licence
  respected; assessment report PDFs read locally and never redistributed. The mineral dispositions layer is
  never loaded.
- **A model never computes.** No arithmetic, distance, conversion or percentage outside a tool.
- **Every number on screen resolves to a stored value id, or is withheld.** An answer that fails the gate is
  replaced by the objection.
- **Unknown and absent are distinct** in every table, tool and sentence. A missing dataset is named on screen.
  **Absence of mapping is not absence of features:** a layer that was never mapped or sampled around a cell
  yields unknown there, never "not met".
- **The leak rules exist and are tested.** Each entry of the leak register below has a test in the
  repository; an entry is closed only by a test that demonstrates the fix.
- **No drilling advice.** No grade or tonnage estimate, no probability that ore is present, no ranking of
  ground a company holds. Verdicts top out at "supports a closer look"; a forbidden-phrases test guards the
  copy.
- **Colour encodes status or source only**, with one declared exception for the score cells, which announces
  itself in the banner.
- Nothing said in a session is ever written back to the served model, the labels or the displayed score.

### The leak register

Every way this system could look better than it is, with the id its tests carry.

| Id | Leak | Rule |
|---|---|---|
| B1 | labels exist where people drilled | the effort null on the same rows; every score reported beside it |
| B3, B5 | neighbouring cells and shared camps across train and test | spatial blocks and a leave-one-camp-out fold, fixed before any fit |
| B11 | criteria thresholds taken from the camps that supply the labels | every threshold cited; folklore at weight zero; criteria scored on held-out camps |
| B13 | files kept by holders who kept ground, fetched by interest | the selection rule recorded; files chosen by cell, not by interest |
| B14 | compilations drawn as they stand today | the dated hindcast; undatable inputs named beside the table |
| B15 | cut lines, camps and roads visible in imagery | no chip feature until an effort mask exists |
| B17 | the assessment file for a labelled cell says what was intersected | `retrieve` refuses blind-listed files for the scored cell and its neighbours |
| B18 | scores fitted on every cell reach the analyst | out-of-fold scores only in scored sessions |
| B19 | a geologist's insight steers the analyst, who then agrees | the insight recorded first, labelled expert-tier in every node that cites it, never written back; the diff reported |
| B21 | a right number from the wrong cell; negated evidence | cell-identity binding; polarity; the gate at every handoff |
| B22 | a stale cache serves an old answer to a new prompt | the system prompt and schema in the cache key |
| B24 | the benchmark is written by the people it scores | frozen and hashed before tuning; tier 3 grows only from observed failures |
| B26 | a judge from the family it judges | mechanical checks first; different families for judge and judged |
| B30 | the cell's own label in its evidence pack | masked in scored runs; kept on the dashboard |

## 7. Backlog

Everything not in the prototype, in one table. Nothing here is scheduled; each row says why it is not done
now and what would unblock it.

| Item | Why not now | What unblocks it |
|---|---|---|
| **Data** | | |
| Distance features outside a line layer's footprint (conductors, faults) are still read as measurements by the criteria score and the learned model; mask them as unknown the way the node gate does | the footprint rule was added at the gate with no store, snapshot or feature change, so the models were not re-run | a feature snapshot rebuild and a re-run of the model search |
| A range check that flags an as-published outlier in a native layer without changing it, shown beside the value and readable by the verifier | the native tier carries values as published and has no validator by design | before the arms re-run, so the verifier reads a flag rather than guessing |
| Pull the national aeromagnetic grid into the feature table | it was found under an open licence after the feature table was frozen | a snapshot rebuild; the criteria table gains its magnetic-low row |
| Further sources: radiometric grids, open-file geochemistry beyond the two lake surveys, deposit compilations for label cross-checks, Sentinel-1 | each needs the inventory's verification pass first | the readiness gate applied to each |
| Sentinel-2 and DEM chips as features, with a mask for cut lines, camps and roads | the mask is not designed, and an unmasked chip model would learn roads | the mask, then the multimodal arm |
| Reading beyond the top drilling files per enabled cell, and the page-tier index over every in-area file | tens of files cannot reach the hundreds of read cells that reliable negatives need | a page-type classifier that picks only table pages, or a cheaper reader measured against the gold |
| Cell-size sensitivity at 1 km and 5 km, and block sizes | compute and a second grid | a snapshot per grid |
| Text features from the reports and bedrock descriptions as an arm of the learned model, read only beside the effort null because text length leaks effort | needs the corpus read first | the reading row above |
| **Machine learning** | | |
| Reliable-negative selection for the positive-unlabelled learner | needs hundreds of read cells to say which unlabelled cells were tested | the reading row above |
| Deep models: a network over chips, a transformer over the feature table | no chips; too few positives to fit one honestly | chips, and a re-test that says the labels support it |
| **Analyst** | | |
| Run the benchmark: the analyst arms on the frozen subset and the interface agent on tiers 1 and 3 | the acceptance story is verified first, and every arm is a paid run | the story passing end to end |
| The segment-scoped and batched executor arms, built but not run | the first full arm has to show whether repeating the whole tables per segment costs anything but tokens | the first arm's cost per chain |
| An arm with the out-of-fold scores switched on, so the verifier reads the effort null rather than a withheld note | every arm so far keeps the single-call arm's evidence rules so the two agents are compared on the same evidence | one arm file and a run at matched compute |
| Model pairing as a switch: a cheap executor under a strong verifier, measured before it becomes a default | the router already routes by model id, so it is an arm file and a run | the same |
| Fitted decider weights per fold as an arm beside the criteria-weight decider | fitting needs node strengths from a finished arm on the open cells | that run |
| The retrieval leak run with the blind-list off | the builder must write a second passages set with the cell's own files included | that set, run beside the staged arms |
| The remaining switches of the matrix: triage, a model planner, no verifier, cumulative executor context, rounds, self-consistency, heterogeneous families | each is another paid arm | the first table showing where the variance is |
| Why the cheap verifier withholds every chain on one enabled cell where the strong arm publishes | arm tuning waits for the benchmark | the arms table |
| The analyst on any cell, not only the enabled ones | the enabled cells are the only ones with their files read, so a chain elsewhere would argue from layers alone | reading beyond the enabled cells |
| The multimodal arm with the chip and map tile attached | no chips | the chips row above |
| The chain diff counts a cross-check id as an expert id when flagging nodes that lean on an insight | the id namespace check lives in the diff and the node writer | a namespace test on expert ids |
| Run the information ladder on benchmark v2: `d1` and `d2`, two more seeds of `d1`, and `d1` voted at `d2`'s compute, beside the extended model | built and tested with scripted backends; every arm is a paid run | approval of the runs |
| The benchmark table's fitted rows read `derived.cell_score_oof`, written under seed 0, while the benchmark's own out-of-fold file uses the spec's seed; read the benchmark's own file, or a seed average | re-scoring moves the published baseline rows, so it goes with the scoring changes below | the next arms table |
| Score as selective prediction: coverage beside every metric, the ranking by the model's probability over every cell whatever its verdict, gate refusals apart from abstentions, Brier and calibration slope in place of ten-bin ECE | a scoring change with no model call, done once for every arm at the same time | the next arms table |
| The Eval page's extended-model table and the v2 benchmark table | the extended model is computed; its table waits for the v2 arms so both are shown together | the runs above |
| **Interface and tour** | | |
| The turn after a job cannot see the finished job in its answer call, so it abstains where the card shows the verdict | the finished row is not staged as a tool result before the answer call | staging it |
| Tier 2 of the interface track: grounded-reasoning items with a rubric | the judgement items need a rater and the project has none | a rater |
| The next-observation tool (`sensitivity`) in the MCP catalogue; today only the interface agent calls it in-process | it was written for the chat first | one catalogue entry and its contract test |
| The agent rail's tab in the store, so the tour sets state rather than clicking a button | local component state | a small refactor |
| A refresh passthrough into the job runner, so a re-recorded tour gets a fresh chain rather than a cache hit | the runner reuses the cache by design | a flag on the job |
| The recorded conversation checked by the data validator like every other data file | the check was not added with the file | one line in the validator |
| **MCP and runtime** | | |
| Concurrent traced calls and per-client quotas on the MCP server; today one traced call runs at a time across clients | the runtime keeps one process-wide trace, and serialising is what keeps span nesting strict | the trace registry below |
| A per-run trace registry in place of the process-wide active run, so two runs in one process trace apart | the scheduler, the job runner and the MCP handlers all hand a run to their threads through it | a design for handing runs to threads |
| A job runner for the standalone MCP server, so `run_analyst` works over stdio without the API | a runner's recovery pass marks every running job failed on start, right for the one API process and wrong beside it; the store allows one writer | a runner that knows it is not the owner |
| Session-scoped cell resources (evidence, chains), so a benchmark client cannot read the unblinded view | a resource read carries no session handle in the protocol | a session id in the URI, or cell resources refused to benchmark keys |
| The benchmark harness as an MCP client | the loops call the tools in-process | the two rows above |
| A spend approval before a job spends | the runner has budgets but no prompt | a client that wants to be asked |
| OAuth in place of keys; notifications on a changed tool list; MCP Apps; skills over MCP; header routing and caching; page crops as image content beside a passage | single-tenant, a fixed tool list, a single process; no client has asked | a second organisation, a version change, a proxy, a client asking |
| The tier audits and the schema map learning the expert tier | it was added with the MCP server as a fifth schema | the next audit change |
| Metrics, alerting on budget and gate-rejection rates, log shipping | one client | more than one client on the server |
| Evidence reads through the serving database; today the candidate list only, with evidence records from the analytics store cached per cell | the cache met the latency budget | a second process needing the reads |
| **Repository** | | |
| Whether the frozen benchmark dataset returns to the repository, and at which path; if it does, the dataset-directory override becomes optional | the dataset is unfinished, so it lives outside the tree with an override | a decision once the benchmark has run |
| A clearer refusal when only the dataset copy is present and not the built packs: name the build command instead of "drifted from its manifest" | the flag touches the benchmark object and the runner | a "dataset copy, not a build" flag |
| A public-safe web build that hides the chat and serves no report text (the MCP server's public-safe mode has no web counterpart) | the flag existed with nothing reading it, so it was removed rather than left as a claim | a deployment that serves the site to strangers |
| A CI workflow (GitHub Actions) running the pipeline suite and the web checks on every push, with its badge in the README | the repository is private and both suites run locally before every commit | the repository going public |

## 8. Decided out of scope

- **No human adjudication and no hand-keyed gold in the prototype**, because there is no geologist on the
  project. The mechanical tiers, the gate suite and the second-family agreement rate stand in, each with its
  denominator stated. The gold tooling exists (`ue gold key`, `ue gold score`) and reports zero pages until a
  person keys one; nothing a model writes may ever be used as gold.
- **No Langfuse**, and no vendor observability SDK in the code path: the instrumentation is OpenTelemetry and
  the backend is a deployment choice.
- **No fine-tuning** of any generator.
- **No company data**: nothing commercial, nothing behind a login.
- **No claim about ground a company holds**, and no comparison of one holder's ground with another's.
- No fourth critic role, no judge-refinement loops over extracted tables, no evaluation on text-recall
  geology benchmarks.

## 9. Risks

- The effort null wins outright: then the honest product is a disclosure tool, and the Eval page says so.
- A leak in the register goes unattacked: every entry carries a test, and the register is re-read before any
  benchmark run.
- The benchmark is tuned to what the system already does well: it is frozen and hashed before tuning, and
  tier 3 grows only from observed failures.
- One model family judges itself: heterogeneous arms, and mechanical checks before any model judge.
- Spend: ceilings per backend, per run and per process, checked before every call; every arm is cost-capped.
