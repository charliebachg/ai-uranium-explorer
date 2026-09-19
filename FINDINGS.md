# Findings

Measured results that changed what the project claims. Each entry names the run that produced it, so every
number here walks back to a stored metric. Newest first.

## F1 · Phase 0: effort beats geology, and the sampling confound does not explain it (2026-09-19)

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
