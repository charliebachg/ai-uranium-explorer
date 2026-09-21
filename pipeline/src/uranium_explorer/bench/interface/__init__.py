"""UraniumBench, interface track: the two mechanical tiers of the interface benchmark (the UraniumBench
requirement's role 2, the interface agent).

The interface agent adds no signal; it finds, explains and abstains. So it is not scored on AUC but on
whether what it says is what the store says, and on whether it declines when the store cannot answer. Two of
the three tiers need no model to build and no rater to score, and this package builds them:

* **Tier 1, deterministic.** Questions whose exact answer the store computes: the nearest deposit's distance,
  the criteria that are unknown here, the share of the grid a feature covers, the count of a layer's
  features within a radius, a cell's out-of-fold scores, how many holes it has. The gold is the value ids
  the tools return and their values, so an answer is checked by the ids it cites and never by matching a
  string. Unanswerable items sit beside them with abstention as the gold and a reason from the fixed set
  (`not_measured`, `outside_grid`, `no_value`, `out_of_scope`): the tier yields a refusal rate and a
  false-refusal rate with a denominator.
* **Tier 3, adversarial.** Questions engineered from failures actually observed: the corruptions the gate
  suite implements, a real number from a neighbouring cell, an observation count with no citable id, a file
  number cited as an id, a negated premise, a folklore criterion phrased as fact, a request for a grade, and
  absence of mapping read as absence of features. Every item names its source failure and the reference
  that recorded it. Nothing is invented beyond those sources: a source that yields fewer items than the spec
  asks for is a shortfall in the manifest, never padding.

Both tiers draw their cells from the frozen analyst benchmark (the same cells, strata, folds, blind-lists
and out-of-fold scores) and compute their gold through the same deterministic tools the agent will call,
with the analyst session's rules applied: the evaluated cell's own label masked (B30), out-of-fold scores
for the cell's fold in place of the served table (B18), and the frozen blind-list where a file is named
(B17). The build is deterministic, the manifest carries a content hash, and the audit regenerates every
item from its recorded choice and compares. Tier 2, the grounded-reasoning tier, needs a rubric and a rater
and is not here. Running any tier against an agent is a later, paid step; nothing in this package calls a
model.
"""
