# Findings

Measured results that changed what the project claims. Each entry names the run that produced it, so every
number here walks back to a stored metric. Newest first.

## F6 · Analyst_v0: a single closed-book call reads the evidence as well as the fitted model, and no better (2026-09-20)

**The question.** UraniumBench v1, analyst track: 114 open scored cells (50 labelled: 18 deposit, 32
occurrence; 64 drilled, unlabelled negatives matched to the positives' drilling profile) plus 16 never-drilled
probe cells, closed book, no names, no coordinates, no effort features, no label context, no fitted scores.
One call per cell with the map card and the evidence pack; the gate on every answer. Manifest
`fa09da9a2230`; the answer key never staged.

| Row, same 114 cells | F1 | PR-AUC | ROC-AUC | ECE | Abstain | Gate rejected | Cost |
|---|---|---|---|---|---|---|---|
| **v0** (Opus 5, medium) | 0.531 [0.452, 0.605] | 0.523 [0.423, 0.649] | 0.586 | 0.084 | 24% | 1 of 130 | $18.46, $0.14 a cell |
| **v0-sonnet** (Sonnet 5, medium) | 0.521 [0.424, 0.605] | 0.484 [0.390, 0.609] | 0.541 | 0.166 | 33% | 25 of 129 | $8.87, $0.07 a cell |
| effort null, out of fold | 0.568 [0.439, 0.689] | 0.659 [0.556, 0.776] | 0.667 | 0.170 | — | — | free |
| criteria, unfitted | 0.551 [0.485, 0.615] | 0.469 [0.394, 0.580] | 0.513 | 0.199 | — | — | free |
| learned, out of fold | 0.226 [0.097, 0.358] | 0.497 [0.405, 0.609] | 0.508 | 0.212 | — | — | free |
| random | 0.496 [0.389, 0.581] | 0.424 [0.369, 0.524] | 0.494 | 0.275 | — | — | free |

**Reading it.** On effort-matched ground, a strong model reading the map and the numbers ranks cells about
as well as the fitted geology model does (PR-AUC 0.52 against 0.50, intervals overlapping) and better than
random, and nowhere near the effort null (0.66), which it never saw. Its probability is well calibrated (ECE
0.084, the best row on the table). Its failure is the same as the fitted model's: it calls 39 of the 64
drilled negatives "supports a closer look", because on the geology they look like the positives; deposits it
gets right 78% of the time, occurrences 63%, negatives 8%. Recall 0.68, precision 0.44.

**Sonnet against Opus.** Sonnet lands inside Opus's intervals on F1 and PR-AUC, abstains more (33% against
24%) and is worse calibrated (0.166 against 0.084). The difference that matters for the loop design is the
gate: 25 of Sonnet's 129 answers were refused, 20 of them for citing an observation-count id for a feature
the pack marks unmeasured, an id it guessed by pattern; Opus did that once in 130. A cheap executor is
viable, but only behind the mechanical verifier that already exists.

**What it decides for the staged loop.** The single-call floor is 0.52 PR-AUC, not the baselines' chance
level, so the loop has something to improve on and a clear target: the negatives. The executor can be the
cheap model with the gate on every node. Two must-run arms remain before the design is frozen: v0-text
(does the executor need the card) and v0-features (does the model reason from raw values or echo the
criteria memberships).

**Probes.** On the 16 never-drilled cells v0 abstained on 38% and Sonnet on 56%; the rest were mostly
"evidence against", which is a reasonable reading of empty ground and an honest one.

**What this is not.** Fifty positives and 64 negatives: every interval on the table is wide and printed. The
labels are a drilling map; a "negative" is drilled ground with nothing recorded. Both runs at medium
reasoning effort, recorded in every row.

## F5 · The reading pass over the enabled cells is complete: text-indexed in full, the gate green, 208 of 212 pages read on Opus (2026-09-20)

**What was done, with no model calls.** Every object the 18 enabled files carry was fetched (858 MB of
reports, appendices, assay spreadsheets and certificates; the one download the store had cut off was resumed
and finished). Their 8,853 rendered pages went through Apple Vision on this machine in 2.1 hours with no
failures. The corpus text index was rebuilt with the OCR pass as a second source: 966 documents and 13,945
pages of text across 35 files, 3,718 of those pages from the OCR where the text layer was missing or the
second reader had rejected it (a page whose scanner OCR reads "L2L.-7" now carries the Vision reading, and
says so in its `source` column). The assay spreadsheets filed with the digital submissions were read into the
native tier. The router then planned **212 pages over the 18 files**, 12 per file wherever twelve candidate
pages exist and fewer where a file is thin, with assay tables first.

**The Opus read, complete.** 208 of the 212 planned pages were read; every one of the 211 live calls
resolved to `claude-opus-5`. Four pages are on the give-up list (74H04-0097 p5; 74H04-0110 p70, p71, p73):
each stalled past the call timeout on three attempts and is skipped until `--retry-failed` asks for it. A
retry pass on 2026-09-20 gave the same result on three of them (nine more timeouts, no cost) and tripped the
circuit breaker before the fourth; they are recorded as gaps, not retried again.

| Usage, from `calls.jsonl` across 17 runs | |
|---|---|
| Live calls | 211 (30 further attempts failed and were retried) |
| List-price cost | $78.99; mean $0.37 a page; a 30 to 40 row assay table $0.45 to $1.51, a contents page $0.08 |
| Model time | 6.31 hours; mean 108 s a page, longest successful 484 s |
| Tokens | 2.64 M output, 3.04 M cache read, 1.11 M cache creation, 850 uncached input |

The 2026-09-19 estimate of $0.27 a page was low because the assay-first priority puts the densest pages
first; the per-call ceiling had to rise from $1.50 to $2.50 for one page that legitimately cost $1.51.

**What is in the store now.** 22 reports read: the four of Phase 2 and all 18 enabled files. 252 pages,
271 tables, 1,969 records, **28,169 field values** with 5,141 validator outcomes, 393 placed collars and
612 matches against provincial records (median offsets 3 to 5 cm where coordinates were printed; 1.26 to
1.28 m for the two files whose collars were transformed from NAD27). All 22 are on the dashboard with their
page images. The validators earned their keep: 74G07-0064 drew 1,140 findings (346 V01, a depth unit read as
metres where the page says feet; 532 V16, the OCR second reader disagreeing on digits), MAW00193 740 (511
V02), MAW00509 704, 74H04-0094 601; every one of these stays flagged rather than dropped, which is the class
of page the review queue in PRD §8.2 exists for. Placement is where the reading is weakest: 393 of 508
holes are plottable, and most of the rest print no coordinates on the pages the router chose, so they sit
on the provincial position instead.

**The five-column gate is green.** At snapshot `073bd46408b7` every feature layer and scene, both label
layers, the read-tier records and all 18 enabled files are green on present, licensed, covers, servable and
versioned. This is the Phase 2 exit condition in §12, met.

**How it ran, and what it changed in the pipeline.** Claude Code's low-memory watchdog killed the reading
chain three times on this 9 GB machine (2.5 to 3.4 GB of swap in use by the browser and other sessions),
twice before the first live call of a resumed run. The kills exposed a real fault: the extractor wrote its
results file only at the end of a run, so 80 paid pages sat in the call cache and never reached assembly.
Three changes followed, each committed with a test: the results file is rewritten after every page; a page
that exhausts its attempts lands on a give-up list and is skipped until asked for again, instead of costing
three retries per resume; and the call timeout and ceiling were tuned (600 s, $2.50). The read then finished
in foreground slices of 3 to 40 pages, one to three workers as memory allowed. Nothing was read on any
model but Opus.

**What it changes.** The reading bound in PRD §9.3 is met on both halves: everything in the enabled cells
is fetched and text-indexed at zero model cost, and the top drilling files are model-read at box level. The
analyst's evidence for every enabled cell now has a deep tier to quote from.

## F4 · The dated hindcast: with drilling frozen, geology ranks later discoveries and effort cannot (2026-09-19)

**Claim under test.** The other tests ask whether a model separates known deposits from the rest of the grid,
and effort wins them (F1). This one asks what a company would: run in year C, where would each model have
ranked the ground where the deposits found *after* C turned out to be?

**What was run.** `lr prospect hindcast`, MLflow run `f0d9d16d1b37`; re-run pinned to store snapshot `f18b5ad059d0` as run `5f44e924ed0a`, every row reproduced exactly. Sixteen dated discoveries in
`knowledge/discoveries.toml`, each cited to a public document with a confidence; medium and high used. At each
cutoff the labels keep only deposits dated at or before it (undated and later deposits masked from training,
occurrences left unlabelled), and every cell first drilled after the cutoff has its hole count set to zero.
The learned, effort and criteria scores are computed for the whole grid and each later discovery's cell is
reported as the share of basin area scoring at least as well. Smaller is better; 5% is a target a programme
drills, 50% is a coin toss.

| Discovery (year) | Cutoff | Learned | Effort | Criteria |
|---|---|---|---|---|
| Shea Creek, Kianna (2004) | 2000 | 0.1% | 0.8% | 5.1% |
| Centennial (2004) | 2000 | 18.9% | 99.3% | 26.4% |
| Patterson Lake South, Triple R (2013) | 2000 | 2.4% | 1.0% | 51.4% |
| Patterson Lake South, R1620E (2014) | 2000 | 13.0% | 1.7% | 85.6% |
| Spitfire (2015) | 2000 | 19.1% | 100% | 3.0% |
| Hurricane (2018) | 2000 | 8.8% | 100% | 3.0% |
| Triple R (2013) | 2010 | 9.8% | 2.2% | 51.4% |
| R1620E (2014) | 2010 | 48.0% | 99.4% | 85.6% |
| Spitfire (2015) | 2010 | 10.7% | 99.7% | 3.0% |
| Hurricane (2018) | 2010 | 13.0% | 2.7% | 3.0% |

Median area share over the ten rows: **learned 12%, criteria 16%, effort 51%**. Four of ten later discoveries
sit in the learned model's top 10% of area, five in the criteria score's, five in effort's.

**Reading it.** Effort's rows are bimodal for a reason: where a discovery sits on ground drilled before the
cutoff (Patterson Lake South, Kianna, Hurricane at 2010), drilling history ranks it near the top; where the
ground had never been drilled by the cutoff (Spitfire, Hurricane at 2000, Centennial), effort has nothing and
puts it last. That is the effort model's ceiling made visible: it can only ever point back at where people
went. The geology model ranks the undrilled discoveries in the top 9 to 19% of the basin. Kianna's 0.1% at the
2000 cutoff is proximity, not prediction: Colette, 1 km away, was known in 1995 and trained the model.

**What cannot be frozen, stated.** The conductor, fault and geochemistry layers are compilations as they stand
today, so surveys flown after the cutoff are in the features; survey footprints carry no year; holes drilled
after the cutoff in a cell first drilled before it stay in the count; occurrences carry no date. Ten rows
from six discoveries is a small table; nine of the sixteen entries are cited at medium confidence. This is the
honest version of the test, not a clean one, and it is the first version.

**What it changes.** F1 stands: on the known labels, effort explains more than geology. F4 is the reason the
geology model is still worth building: the labels are a map of drilling, and a model that predicts drilling
cannot find ground nobody drilled. Both tables belong on the Eval page side by side.

## F3 · The model search: no geology candidate beats the effort null, and the conductor is the feature that matters (2026-09-19)

**What was run.** `lr prospect modelsearch`, 23 arms, every one an MLflow run (re-run 2026-09-19 pinned to store snapshot `49cd5dac1147`; every number below reproduced exactly, the fits are seeded): six candidates under spatial and
camp folds, six single-group ablations, geology plus effort, and 20 and 50 km blocks; matched background and
thinned positives for training, out-of-fold scoring on the same 10,183 cells, bootstrap intervals.

| Arm, spatial folds | PR-AUC | 95% interval |
|---|---|---|
| effort null | 0.178 | 0.162 to 0.199 |
| random forest | 0.125 | 0.112 to 0.144 |
| bagging PU | 0.122 | 0.112 to 0.140 |
| HistGB + criteria score as a prior | 0.118 | 0.105 to 0.140 |
| logistic with spatial terms | 0.117 | 0.104 to 0.135 |
| HistGB | 0.111 | 0.101 to 0.128 |
| geology + effort | 0.190 | 0.172 to 0.214 |

**Decision, by the rule and not by preference.** The best geology-only candidate, the random forest, does not
beat the null: intervals overlap and its point lies below. The registry holds it at stage *candidate*, served
false. Geology on top of effort reaches 0.190 against 0.178, intervals overlapping: geology adds something,
and not enough to separate.

**Ablations.** Removing the conductor distance drops HistGB from 0.111 to 0.086, intervals apart; removing
any other group moves it within its interval. One feature carries the geological signal. Block size: at 20 km
the gap is 0.126 against 0.164, at 50 km 0.112 against 0.160; the ordering does not depend on the block.

## F2 · The magnetics gap was overstated: a national 200 m grid is published under an open licence (2026-09-19)

**Claim under test.** The data inventory recorded the airborne magnetic grids as *not addressable*: the federal
repository served them only through an interactive portal, with no file URL, no WMS and no WCS (checked
2026-09-18). The PRD asked for this to be re-verified before any external claim.

**What was checked.** The Open Government Portal record for "Canada - Aeromagnetic survey data compilation"
(dataset 752fe3fc-d871-5ae1-9bd9-2b6f65880a8d, Natural Resources Canada, modified 2020-10-05) and where its
resource links resolve.

**Result.** The Canadian Aeromagnetic Data Base compilation is published under the **Open Government Licence -
Canada** with named resources: 200 m and 1 km residual total field grids (ZIP), both first vertical derivatives,
a CAGDB WMS, and the Open File 7799 anomaly map. The resource links on the old host redirect to the new portal
at geophysical-data.canada.ca, so the file is reachable in principle and not yet pulled by us. Radiometrics and
gravity: no value grid found; unchanged.

**What changes.** The gap's status is now *published, not pulled*; a source entry `cagdb_mag_200m` is
registered as verified and unpulled; the risk "the grid exists and we said it did not" in PRD section 13 is
closed by this entry. The pull is Phase 2 work. A caveat stands: the national compilation is coarser than the
company surveys in the assessment files, and a residual field is not an interpretation.

## F1 · Phase 0: effort beats geology, and the sampling confound does not explain it (2026-09-19)

*Re-run 2026-09-19 pinned to store snapshot `77cd52b0e9ee` as MLflow run `5b74757a603d`: every number below reproduced exactly (seeded fits). The full table is on the Eval page, each row naming that run.*

**Claim under test.** Under spatial folds, a model given nothing but exploration effort (drillhole counts,
survey footprints, sample counts) predicts the deposit and occurrence labels better than a model given the
geology (PR-AUC 0.347 against 0.133). The literature review found a mechanism that could manufacture this:
treating every unlabelled cell as a negative shrinks the positive area asymmetrically and hurts the geology
model most (Zhang, Coutts, Parsa, Cumani, Thompson; *Natural Resources Research*, July 2025).

**What was run.** `lr prospect headline`, run `2026-09-19T11:56:38+00:00`, on the 10,183 cells where every
feature of both models is present: 546 positives (60 deposit cells, 486 occurrence cells). Every combination
of {learned, effort} × {all positives, deposits only} × {naive, matched background, thinned positives, both}
× {random, spatial 30 km blocks, leave-one-camp-out}, scored out of fold on the same cells. The corrections
change only which cells a fold's model may be fitted on. Intervals are 95% percentile bootstraps over cells.
Metrics are stored under `headline.*` in `derived.metric`.

- *Matched background*: negatives for training drawn so that their deciles on an effort index match the
  positives' deciles.
- *Thinned positives*: one positive per 10 km block, so a camp of forty adjacent deposit cells counts once.

**Result, spatial folds.**

| Positives | Correction | Learned PR-AUC | Effort PR-AUC | Learned deposits in top 10% | Effort deposits in top 10% |
|---|---|---|---|---|---|
| all | none | 0.133 [0.122, 0.148] | 0.346 [0.310, 0.388] | 27% | 100% |
| all | matched | 0.132 [0.119, 0.150] | 0.342 [0.310, 0.381] | 13% | 97% |
| all | thinned | 0.111 [0.100, 0.125] | 0.229 [0.203, 0.263] | 17% | 70% |
| all | both | 0.111 [0.101, 0.128] | 0.178 [0.162, 0.199] | 10% | 55% |
| deposits | none | 0.011 [0.007, 0.016] | 0.502 [0.402, 0.658] | 23% | 90% |
| deposits | both | 0.011 [0.006, 0.038] | 0.235 [0.165, 0.348] | 8% | 87% |

Base rates: 0.054 with all positives, 0.006 with deposits only. Random folds flatter both models; camp folds
(a whole camp and 25 km around it held out) give learned 0.119 against effort 0.198 after both corrections.

**MineTRACE's protocol** (30 positives held out by whole block, 200 sampled negatives, fit on the rest,
20 repeats): learned ROC-AUC 0.661 ± 0.053, effort 0.917 ± 0.084. The effort figure equals MineTRACE's
published headline for nickel, and it comes from a model that knows nothing but where people drilled.

**Verdict: confirmed.** After both corrections, under spatial folds, effort is ahead on both label sets and
the intervals do not overlap. The confound is real but it explains almost none of the gap: matching the
background moves the geology model by a thousandth. What the corrections do reveal is that about a third of
the effort model's apparent skill was counting the same camp many times: thinning cuts its PR-AUC from 0.346
to 0.229 and its top-10% capture from 100% to 70%.

**Three things this settles for the rest of the plan.**

1. The geology model has real but small signal beyond effort (ROC-AUC 0.71 to 0.76 under spatial folds), and
   it does not transfer across camps (0.64 to 0.69 with a camp held out). It has learned camp signatures.
2. Effort alone ranks the 60 deposit cells almost perfectly (ROC-AUC 0.90 to 0.95). The labels are, to first
   order, a map of drilling density. Any score reported without the effort row beside it is misleading.
3. The promotion rule in §C stands: nothing is served as a predictor until it beats the effort null under
   spatial folds, and today nothing does.

**Caveats.** One seed; the thinning block is 10 km and the matching uses 20 negatives per positive in each
decile, both defaults rather than tuned. Nine of seventeen geological features cover 40% of the grid or
less and are not in the learned set at all, so "geology" here means ten features. The occurrence labels
include single boulders and trenches; the deposits-only rows are the cleaner test and the harsher one.
