# Findings

Measured results that changed what the project claims. Each entry names the run that produced it, so every
number here walks back to a stored metric. Newest first.

## F8 · The staged loop on a cheap stack: the gates hold, the cost falls a hundredfold, and the verifier is where the capacity is missed (2026-09-21)

**The question.** Can the staged loop run off the Claude subscription, on cheap models through OpenRouter,
at many cells in parallel, and what does that buy? The arm `v1-openrouter`: GLM 5.3 Flash as the executor
(it reads the card; $0.09 in, $0.30 out per million tokens), Qwen 3.8 Flash as verifier and adjudicator
($0.15, $0.47), medium effort, K = 3, the template plan, thirty cells in parallel, the same 130 open cells
and the same gates as every other arm. Final run `20260920T181356Z-bench` (MLflow `4cbd562b`) resumed from
`20260920T180317Z-bench`; the single closed-book call on Qwen 3.8 Flash (`v0-qwen38`) is in F6's addendum.

| Same 114 labelled cells | F1 | PR-AUC | Abstain | Chains validated | Cost |
|---|---|---|---|---|---|
| v1-openrouter, GLM executes, Qwen verifies | 0.286 | 0.556 on the 21 published | 82% | 63 of 130 | about $0.05 a cell |
| v1, Sonnet executes, Opus verifies (35 cells before the stop) | 0.480 | 0.716 [0.39, 0.95] | 45% | 31 of 35 | $1.37 a cell |
| v0, Opus single call | 0.531 | 0.523 [0.42, 0.65] | 24% | | $0.14 a cell |
| v0-qwen38, Qwen single call | 0.444 | 0.496 [0.37, 0.67] | 46% | | $0.005 a cell |

**Where the cheap chains die.** All 130 cells answered on the final pass. The node gate refused 9% of
executor attempts (Sonnet: 5 to 10%). 67 chains were never validated: the Qwen verifier refuses GLM's nodes
on substance (single bedrock observations read as host, thin coverage read as measurement, the withheld
effort null not acknowledged) and the nodes GLM re-executes on that feedback do not satisfy it, so after three
rounds the chain abstains. Ten more chains validated but the adjudicator's claims failed the gate on all three
attempts (bare feature names where the id belongs). 53 published. The Opus verifier validated 87% of Sonnet's
chains on the enabled cells and 89% on the benchmark cells it reached; Qwen validates 48% of GLM's. The loop
cannot tell from inside whether the cheap verifier is over-strict or the cheap executor's repairs are poor;
either way the pairing is where the capacity is missed, which is STA-CoT's finding again from the other side.

**What it took to run at thirty in parallel.** Five things, each found by a failed run and fixed in code:
the reasoning these models do is billed as completion tokens and unbounded it ate the allowance and returned
nothing (a verifier call took twelve minutes at low and at medium effort alike, so the effort is now sent as
a hard `reasoning.max_tokens` budget, which took the same call to 35 seconds); the Z.ai endpoint refuses to
disable reasoning and ignores the budget, so a cut reply is retried with a low effort hint and a wide
completion room; OpenRouter takes an effort or a budget, never both; a reply with a second object after the
first is read as the first; and forty workers parsing the same map layer on their first call took the
runner to 6.9 GB, so the layers are preloaded once. The final pass ran 130 cells in about 35 minutes with no
failure; the whole day of attempts cost about $15.

**Addendum, 2026-09-21: the same two models through OpenRouter's Anthropic listing.** `v1-anthropic-or` is
v1 with `anthropic/claude-sonnet-5` and `anthropic/claude-opus-5` through OpenRouter instead of the Claude
CLI: pay per token, no subscription limit, twenty cells in parallel. Chained onto the stopped v1 run and
parked at 76 of 130 cells (run `20260920T190845Z-bench`, MLflow `f98e3ffc`; 35 cells via the CLI, 41 via
OpenRouter). Per call: Sonnet executor $0.047 and 6 s median, Opus verifier $0.076 and 30 s, adjudicator
$0.081; $1.02 a cell over the two OpenRouter batches. On the 67 labelled cells reached: F1 0.360 [0.18,
0.51], PR-AUC 0.539 [0.36, 0.85], ROC-AUC 0.612, ECE 0.116, 61% abstaining; v0 on the same cells F1 0.500,
PR-AUC 0.432 [0.35, 0.62]; the effort null 0.614 [0.50, 0.75]; the learned model 0.473. The first 35 cells
had put the loop at 0.716; with 67 the ranking still sits above the single call and the learned model and
below the effort null, with every interval overlapping, and the price is abstention: the OpenRouter batches
validated 23 of 40 chains against 31 of 35 for the CLI batch, which could be the later cells being harder or
the transport (Opus through the API thinks under a 4,000-token budget; the CLI's medium effort is its own
setting), and the run cannot separate the two. Stopped by the user on cost; the remaining 54 cells resume
from that run id at about $1 a cell.

**What it decides.** The gates and the harness carry a hundredfold cheaper stack without change: every number
still resolves or the node is refused, a chain no round validates still abstains, and a run of 130 cells
costs six dollars. The verdict quality does not survive the swap of the *verifier*: F1 0.29 against 0.48, and
82% abstention, is the verifier refusing what the executor gives it. The next arm is the one the pairing
grid says: the cheap executor under a strong verifier, with the verifier on Opus through OpenRouter's own
Anthropic listing ($5 and $25 per million tokens, about $0.12 a call), which keeps the run off the
subscription limit and puts the capacity where it pays. The single-call number for the cheap model (F6
addendum) stays the floor to beat.

## F7 · Analyst v1 on the enabled cells: the loop runs end to end, and the verifier finds the data's faults before ours (2026-09-20)

**The question.** Does the staged loop (PRD §8.4) run over real cells, store what it decides, and earn its
verifier? Fifteen enabled cells, dashboard purpose (nothing blinded, the label kept off the pack by the arm's
switches), the v1 arm: template plan of eight criteria and two cross-checks, Sonnet 5 executor per segment,
the mechanical gate, one Opus 5 verifier round (K = 1), both deciders, chains into the agent tier. Run
`20260920T122617Z-chain`, MLflow `ad5a0491`; the first attempt stopped at its $15 budget after eleven cells
and the second replayed those eleven from the cache for nothing and ran the last four live.

| | |
|---|---|
| Chains | 15 stored, 8 published, 7 never validated |
| Nodes | 150: 74 met, 40 not met, 36 unknown; every one on `claude-sonnet-5` |
| Node gate | 3 rejections in 152 executor attempts (2%); none recorded unknown after three attempts |
| Verifier | 15 verdicts on `claude-opus-5`; its recorded label agreed with the final verdict on 14 of 15 |
| Deciders | weighted score and adjudicator on the same side of 0.5 on 8 of 14; the adjudicator's probability lower on 13 of 15 |
| Cost | $17.85 for 15 cells, $1.19 a cell; 12 to 13 calls a cell |

**What the verifier objected to, and it was right every time.**
- *Cover masks the host* (six cells: 0015_0065, 0033_0047, 0037_0049, 0145_0019, 0185_0058, and a note on
  0145_0018). The executor read the 1:250k bedrock polygon as "not met" for graphitic host where the cell
  sits under basin sandstone, so the polygon describes the cover and says nothing about the basement. The
  verifier's instruction each time: unknown, not not met. The criteria score makes the same error for every
  covered cell, silently. A cover mask on `graphitic_host` is now on the backlog ahead of 5c.
- *Absence of mapping read as absence of features* (0000_0053). The cell sits outside the geological map's
  footprint, every nearest feature about 30 km away; the executor scored conductors, faults, density and the
  cross-check as absent. The verifier called four nodes unmeasured, and one number cited to nothing.
- *A zero-filled count field* (0145_0018, 0145_0019). Fifteen boulders within 5 km at 0.0 cps is a null in the
  source layer, not a measured negative.

With K = 1 none of this could be repaired inside the run: the chains are stored unpublished with the
verifier's reasons, which is what the dashboard shows beside the memos. On these cells the K3 arm would have
re-executed one node with that feedback for about one more executor call and one more verifier call a cell.

**What the loop read on the labelled cells.** Seven deposit cells: six "supports a closer look", one
"insufficient evidence" (0030_0081, valid chain, adjudicator at 0.22 against a weighted 0.52). Both
occurrence cells: supports a closer look. Six unlabelled cells: two insufficient, one evidence against, three
supports a closer look. Not a benchmark: the cells were chosen, and this purpose does not blind them.

**What the gate found in us, again.** The second chain was withheld at the store for quoting a map scale:
"1:250,000" scanned as the token "000" once the colon stopped the match at "250,000", and the store's
publish check had no tool transcript to see the scale was a tool's own text. The scanner no longer starts a
number after a digit and a comma, the store now re-checks with the transcript the loop's gate used, the
fabrication suite is unchanged at 228 of 232 refused and 0 of 128 honest claims refused, and the replay
published the cell. Before the run, staging one benchmark cell through a real session with no model found
the criteria rows carrying their literature lines, which name the deposits each threshold came from; blinded
sessions now drop that line and scrub the handbook's own place names.

**K = 3, same cells, same day.** With three verifier rounds the same fifteen cells validated 13 of 15 (run
`20260920T130034Z-chain`, MLflow `a62ca4a4`): the five graphitic-host chains were repaired in round 2 or 3,
the executor re-reading its node as unknown on the verifier's reason, at $0.35 to $0.75 a cell more. The two
that still never validated are the two data faults: the footprint cell spent $2.59 over three rounds with the
executor scoring distances to features outside the map's reach as "not met" and the verifier refusing it
each time, and the boulder cell could not mark a count unknown while the source layer hands it a value of
0.0. Neither is repairable from what the store holds; both are on the backlog. The node gate's rejection rate
rose to 10% (re-executed nodes face it again); live spend for the K = 3 pass was $5.25 on top of the cached
executor calls.

**What it decides.** The cover mask and the zero-fill rule land before 5c, because the verifier will keep
failing chains on them and the criteria baseline carries the same errors. K = 1 discards the verifier's most
useful output; K = 3 is now the v1 default and K = 1 the ablation arm. A chain costs $1.19, so a 114-cell arm is
about $135 and the seven arms about $950; the arm set is sized against usage, not run wholesale. The two
deciders disagree on the threshold for four chains in ten, with the adjudicator the conservative one; 5c
reports both and the ranking decides.

## F6 · Analyst_v0: a single closed-book call reads the evidence as well as the fitted model, and no better (2026-09-20)

**The question.** UraniumBench v1, analyst track: 114 open scored cells (50 labelled: 18 deposit, 32
occurrence; 64 drilled, unlabelled negatives matched to the positives' drilling profile) plus 16 never-drilled
probe cells, closed book, no names, no coordinates, no effort features, no label context, no fitted scores.
One call per cell; the gate on every answer; every arm at medium reasoning effort. Manifest `fa09da9a2230`;
the answer key never staged. Five arms, each differing from v0 in one way.

| Row, same 114 cells | F1 | PR-AUC | ROC-AUC | ECE | Abstain | Gate rejected | Cost |
|---|---|---|---|---|---|---|---|
| **v0** Opus 5, card + pack | 0.531 [0.452, 0.605] | **0.523** [0.423, 0.649] | 0.586 | **0.084** | 24% | 1 of 130 | $18.46 |
| v0-text, pack only | 0.553 [0.470, 0.634] | 0.463 [0.379, 0.585] | 0.514 | 0.118 | 27% | 0 of 130 | $15.29 |
| v0-features, no criteria table | 0.558 [0.476, 0.632] | 0.470 [0.384, 0.597] | 0.515 | 0.097 | 28% | 0 of 130 | $18.35 |
| v0-retrieval, passages, blind-listed | 0.552 [0.478, 0.625] | 0.472 [0.391, 0.586] | 0.540 | 0.093 | 21% | 8 of 129 | $23.99 |
| v0-sonnet, Sonnet 5 | 0.521 [0.424, 0.605] | 0.484 [0.390, 0.609] | 0.541 | 0.166 | 33% | 25 of 129 | $8.87 |
| effort null, out of fold | 0.568 [0.439, 0.689] | **0.659** [0.556, 0.776] | 0.667 | 0.170 | | | free |
| criteria, unfitted | 0.551 | 0.469 | 0.513 | 0.199 | | | free |
| learned, out of fold | 0.226 | 0.497 [0.405, 0.609] | 0.508 | 0.212 | | | free |
| random | 0.496 | 0.424 | 0.494 | 0.275 | | | free |

Precision of the top of each ranking (cells labelled among the top 10 / 20 / 30 by score): v0 0.60 / 0.55 /
0.60; learned 0.60 / 0.50 / 0.47; criteria 0.30 / 0.55 / 0.53; **effort 0.90 / 0.75 / 0.70**; v0-text 0.30 /
0.50 / 0.50; v0-features 0.50 / 0.50 / 0.43; v0-retrieval 0.30 / 0.40 / 0.50. Curves in
`deck/figures/v0-arms-pr-curves.png`.

**Reading it.** On effort-matched ground a strong model reading the map and the numbers ranks cells about
as well as the fitted geology model on the same cells (0.52 against 0.50, intervals overlapping), above
random, and nowhere near the effort null (0.66), which it never saw. It is the best-calibrated row. Its
failure is the fitted model's failure: it calls 39 of the 64 drilled negatives "supports a closer look",
because on the geology they look like the positives; deposits right 78%, occurrences 62%, negatives 8%. The
verdict threshold adds nothing over calling everything (its point sits on the base-rate line); the value,
such as it is, sits in the top of the ranking, where v0 and the learned model both reach 60% precision in
the first ten against a 44% base rate, and effort reaches 90%.

**What each arm decided.** Every interval overlaps every other, so these are directions, not verdicts.
- *The map is where the lift is.* Without the card (v0-text) the ranking falls to 0.46 and the top-ten
  precision to 0.30; F1 at the verdict does not move. The executor in the staged loop keeps the card.
- *The model is not echoing the criteria table.* Without it (v0-features) F1 and calibration hold and the
  ranking sits at 0.47, the criteria score's own level. The planner can hand the executor raw values and
  thresholds; the memberships are a convenience.
- *Retrieved text did not help, blind-listed.* Passages from files 10 to 40 km away (the cell's own files
  excluded) left the ranking at 0.47, raised the cost to $0.19 a cell, and produced the run's only real
  gate finding (below). The leak measurement, blind-list off, is still to run.
- *Sonnet is viable behind the gate, not in front of it.* Inside Opus's intervals on F1 and PR-AUC, worse
  calibrated (0.166), and 25 of 129 answers refused, 20 of them for citing an observation-count id on an
  unmeasured feature, an id it guessed by pattern. Opus did that once in 130.

**What the gate found in us.** The retrieval arm's first pass had 24 refusals; 19 were report text the
model quoted verbatim ("2310m", "2018:") that the gate's number scanner did not read as numbers. The scanner
now reads a number glued to its unit or ending a label on both sides of the check, `lr arm regate`
re-judged every stored answer of every arm with no model call (the four other arms did not change by one
row), and the fabrication suite went from 224 to 228 of 232 refused with still 0 of 128 honest claims
refused. Eight refusals remain in the retrieval arm, mostly depths quoted from hole labels.

**What it decides for the staged loop.** The single-call floor is 0.52 PR-AUC. The reasoning is not the
bottleneck on this pack; the evidence is: the same pack read by a gradient-boosted model, by Opus, by Opus
without the criteria table and by Opus with neighbouring passages all land within a few hundredths. The loop
earns its cost only where it brings evidence the pack does not carry, or where it turns a ranking into
abstention on thin ground. So: cheap executor behind the gate, card kept, criteria template as the plan,
verifier on Opus, and the deciders scored as a ranking, not a threshold.

**Probes.** On the 16 never-drilled cells the arms abstained 31 to 56% of the time and mostly said "evidence
against" otherwise, a reasonable reading of empty ground.

**Addendum, 2026-09-20 evening: the same single call on a cheap model.** `v0-qwen38`, Qwen 3.8 Flash through
OpenRouter at medium reasoning effort, the same card and pack, the same gate (runs `20260920T144047Z-bench`
and its resume `20260920T160038Z-bench`, MLflow `69c1da18`): F1 0.444 [0.33, 0.55], PR-AUC 0.496 [0.37,
0.67], ROC-AUC 0.565, ECE 0.093, abstain 46%, 26 of 129 answers refused by the gate, one cell failed on a
reply that reasoned past its length limit, $0.71 for 130 cells against $18.46 on Opus. The ranking sits
inside Opus's interval and above the learned model's 0.497; the verdict is worse, mostly because it abstains
twice as often; the gate refuses it as often as Sonnet. The calls take six minutes each at medium effort,
because the model reasons for thousands of tokens over a pack Opus answers in forty seconds; seven of 130
replies came back empty or malformed on the first pass, five of them recovered by a resume on a parser that
reads the first complete object. A twenty-sixth of the cost buys the same ranking and a weaker verdict.

**What this is not.** Fifty positives and 64 negatives: every interval is wide and printed. The labels are a
drilling map; a "negative" is drilled ground with nothing recorded. One cell of the retrieval arm failed its
call and counts as an abstention.

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
