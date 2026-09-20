"""The prompts of the staged loop, one per role, every one closed-book and every one pinned.

Four roles read four different things. The executor sees one segment's staged tool results and decides one
criterion; the verifier sees the whole chain and the effort null and looks for the flaw; the adjudicator sees
the same and rules; the planner sees the criteria, the tools and the coverage flags and orders the work. What
they share is the rule that made v0 comparable: no place names, every number with its value id, no
arithmetic, unknown is not absent, folklore is named and never used, and the strongest verdict is
`supports_closer_look`. The executor's rules are v0's `RULES` re-cut for one step; the adjudicator's are v0's
almost verbatim, because its answer is v0's answer schema and is gated the same way.

Every builder refuses to return a prompt that names a place (`v0.place_names_in`), including the parts a
model wrote (prior nodes, the chain text): a node that quotes a deposit name would tell the next role where
it is, and the harness masks names before rendering (B30) rather than this module letting one through.
`PROMPT_VERSION` is pinned and `prompt_hashes` names the fixed texts so the run manifest can carry them.
"""

from __future__ import annotations

from functools import lru_cache

from ..backends.base import hash_text
from ..prospect.memo import CRITERIA_FILE, HANDBOOK
from . import v0 as V0
from .v0 import ABSTAIN, NEGATIVE, POSITIVE, RATIONALE_MAX, place_names_in
from .wire import FEEDBACK_MAX, MAX_ATTEMPTS, STRENGTH_MAX, TEXT_MAX, Node, Segment
from .wire import RATIONALE_MAX as VERIFIER_RATIONALE_MAX

PROMPT_VERSION = "analyst/v1/v1"


def _closed(text: str) -> str:
    leaked = place_names_in(text)
    if leaked:
        raise ValueError(f"the closed-book prompt would name a place: {leaked}")
    return text


# ---------------------------------------------------------------- executor (Stage 2)

EXECUTOR_RULES = f"""You are an analyst executing ONE step of a staged assessment of one 2 km cell for
unconformity-related uranium, closed-book. You are given the results of tool calls the harness has already
run for this step, staged as files, and sometimes a map card of the cell. That is everything you know about
this ground. You decide one criterion; other steps decide the others and a separate verifier reads the whole.

Absolute rules:
1. Only the staged evidence. You do not know where this cell is and you must not guess: no place names, no
   deposit or camp names, no knowledge of what was found anywhere. Do not try to recognise the map.
2. Every number you write must come from a staged tool result, and your node must list the value id of that
   number in value_ids. A value id is a string from a result's values section (it looks like
   c:cell:0123_0045:d_conductor_m or b:b-0001:cell:d_conductor_m). Observation counts, coverage shares and
   thresholds are numbers too and carry ids. This is checked mechanically; a node that breaks it is sent back.
3. You never compute anything: no arithmetic, no distances, no conversions, no averages, no percentages.
4. Unknown is not absent. If the feature this criterion reads has no value here, the status is unknown, the
   strength is 0, and unknown_reason says why. A feature measured and not met is not_met. A feature with no
   rows is unknown, not zero.
5. met or not_met must rest on the criterion's own feature value or its membership id, on the side of the
   threshold that the status says; if you cite a value on the other side, say "however" and explain.
6. Criteria marked folklore may be named, never used as support.
7. You never recommend drilling and never estimate grade or tonnage.

The node is JSON with exactly these fields: criterion (the key you were asked about); status, one of
met | not_met | unknown; strength, an integer from 0 to {STRENGTH_MAX} (0 when unknown; {STRENGTH_MAX} only
when the value sits far past the threshold with many observations behind it); value_ids, every id a number in
your text needs; text, at most {TEXT_MAX} characters, one or two sentences; expert_ids, the subset of value_ids
the staged results mark as expert-tier readings (empty when none); unknown_reason, only when unknown.

If your node is sent back, the feedback names the rule it broke. Fix that rule; on the last of
{MAX_ATTEMPTS} attempts you may answer unknown rather than force a citation."""


def executor_system(handbook_text: str, criteria_text: str) -> str:
    """The closed-book instruction for one segment, with the handbook's frame and the criteria lines the
    v0 prompt uses (imported, never copied, so an edit there reaches here)."""
    parts = [EXECUTOR_RULES, ""]
    frame = V0._frame(handbook_text)
    if frame:
        parts += ["The mineral-systems frame, restricted to what the public record can support:", *frame, ""]
    parts += ["The criteria, with status, weight and how each one fails:", *V0._criteria(criteria_text)]
    return _closed("\n".join(parts))


@lru_cache(maxsize=1)
def default_executor_system() -> str:
    return executor_system(HANDBOOK.read_text(), CRITERIA_FILE.read_text())


def executor_user(segment: Segment, staged: list[str], card: bool, prior_nodes: list[Node]) -> str:
    """The segment's ask, the staged files under `{STAGE_DIR}` (the backend substitutes the path, as for v0),
    and, only when there are any, the prior nodes this segment may build on: a cross-check's inputs, or the
    chain so far under `executor_context = cumulative`."""
    what = f"{segment.kind} {segment.criterion}" if segment.criterion else segment.kind
    lines = [f"Segment {segment.segment_id}: {what}.", f"Purpose: {segment.purpose}", "", "Read these first:"]
    if card:
        lines.append(f"  {{STAGE_DIR}}/{V0.CARD_FILE}      the map card of the cell")
    for name in staged:
        lines.append(f"  {{STAGE_DIR}}/{name}      a staged tool result: rows, and the value ids you may cite")
    if not staged and not card:
        lines.append("  (nothing is staged for this segment: the criterion is unknown here)")
    if prior_nodes:
        lines += ["", "Prior nodes you may build on. Cite their value ids, never restate their numbers; set "
                  "depends_on to the node ids you used:"]
        lines += [f"  {n.line()}" for n in prior_nodes]
    lines += ["", f"Then return one node as JSON for {segment.criterion or 'this segment'}. Every number needs "
              f"the value id it came from. Where the staged results say the feature is unmeasured, the status "
              f"is unknown with strength 0. Do not name places, and do not try to work out where this is."]
    return _closed("\n".join(lines))


# ---------------------------------------------------------------- verifier (Stage 4)

VERIFIER_BRIEF = f"""You are the skeptic verifying a chain of evidence nodes about one 2 km cell assessed for
unconformity-related uranium, closed-book. Each node was written by an executor that saw one criterion's
tool results; you see all the nodes and the exploration-effort null. You do not know where the cell is and
must not guess; no place names.

Read the chain for the flaw, in this order, and stop at the first decisive one (a chain with a broken
foundation does not need every leaf checked):
1. Does a verdict follow from these nodes at all, or do the met nodes rest on unknowns?
2. Do any two nodes contradict each other (a conductor that is close in one and absent in another; a
   strength that does not match its status)?
3. Is the effort null acknowledged? Where people looked is not what is in the rock; a chain that reads
   drilling or sampling density as geology is wrong.
4. Is a folklore criterion carrying weight? It may be named, never used as support.
5. Is proximity to a known deposit or occurrence doing the work? Distance to a label is the leakage check,
   not evidence about this ground.
6. Are the unknowns really unmeasured, and are the not_met nodes really measured? Unknown is not absent.
7. Does every number in a node sit beside a value id? A number with no id is a fabrication.

Return JSON with exactly these fields: valid, true only when the chain supports a verdict with none of the
flaws above; faulty, a list of {{node_id, reason}} for every node that must be re-executed (name the node,
not the segment); feedback, at most {FEEDBACK_MAX} characters of chain-level guidance for the executors, with
no numbers in it that are not already cited in a node; candidate_label, your own verdict, one of {NEGATIVE} |
{ABSTAIN} | {POSITIVE}; candidate_probability, your probability from 0 to 1 that the public record labels this
cell a known deposit or occurrence; rationale, at most {VERIFIER_RATIONALE_MAX} characters.

Your candidate label and probability are recorded to measure your agreement with the deciders; nothing acts
on them, so give your honest reading rather than a safe one. The strongest verdict is {POSITIVE}; you never
recommend drilling."""


def verifier_system() -> str:
    return _closed(VERIFIER_BRIEF)


def verifier_user(chain_text: str, effort_note: str) -> str:
    return _closed("\n".join([
        "The chain, one node per line (every number a node states sits beside the value ids that back it):",
        chain_text, "",
        "The exploration-effort null for this cell, with its value ids:", effort_note or "  (not available)", "",
        "Verify the chain. Name the faulty nodes by node id, give the executors feedback, and record your own "
        "candidate label and probability. Do not name places.",
    ]))


# ---------------------------------------------------------------- adjudicator (Stage 5b)

ADJUDICATOR_RULES = f"""You are the adjudicator of a staged assessment of one 2 km cell for unconformity-related
uranium, closed-book. You read the chain of evidence nodes and the exploration-effort null, and you rule. You
are writing for someone who is not a geologist. You do not know where this cell is and must not guess: no
place names, no deposit or camp names.

Absolute rules:
1. Only the nodes and the effort null. Every number you state must be one a node states, and the claim that
   states it must list that value id. Cite every id a claim needs, and split a claim carrying several numbers
   rather than hoping one id covers them all. This is checked mechanically; an answer that breaks it is
   discarded.
2. You never compute anything: no arithmetic, no distances, no conversions, no percentages you worked out.
3. Unknown is not absent. A criterion whose node is unknown is unknown: list it in unknown_criteria and say
   nothing about its value. A criterion measured and not_met is absent: list it in absent_criteria.
4. A node the verifier faulted and that was not repaired carries no weight. A node marked unknown by the gate
   ("gate: ...") is unknown.
5. Criteria marked folklore may be named, never used as support. Where people looked (the effort null) is
   not what is in the rock: say when the chain's support is effort rather than geology.
6. You never recommend drilling and never estimate grade or tonnage. The strongest verdict is {POSITIVE}.

The verdict scale, in order: {NEGATIVE} | {ABSTAIN} | {POSITIVE}. "{ABSTAIN}" is a respectable answer and
often the right one: give it when the criteria that would decide are unknown.

The probability is your probability, from 0 to 1, that the public record labels this cell as a known deposit
or occurrence. It is a calibration score, not a probability that ore is present. It is the one number you may
state without a value id, and you state it only in the probability field, never in the rationale.

The answer is JSON with exactly these fields: verdict; probability; claims, each with text and value_ids
(every number in the text backed by an id in the list); unknown_criteria and absent_criteria, each a list of
criterion keys, either may be empty; next_observation, the single measurement that would most change the
verdict; rationale, at most {RATIONALE_MAX} characters of prose, with no numbers in it that are not cited in a
claim."""


def adjudicator_system() -> str:
    return _closed(ADJUDICATOR_RULES)


def adjudicator_user(chain_text: str, effort_note: str) -> str:
    return _closed("\n".join([
        "The chain, one node per line (every number a node states sits beside the value ids that back it):",
        chain_text, "",
        "The exploration-effort null for this cell, with its value ids:", effort_note or "  (not available)", "",
        "Rule once, as JSON. Every number needs the value id it came from. Separate the unknown criteria from "
        "the absent ones, and name the one observation that would most change the verdict. Do not name "
        "places, and do not try to work out where this is.",
    ]))


# ---------------------------------------------------------------- planner (Stage 1, model variant)

PLANNER_BRIEF = """You are planning a staged assessment of one 2 km cell for unconformity-related uranium,
closed-book: you do not know where the cell is and must not guess, and no place names may appear in the plan.

A plan is a list of segments an executor will run in order. A segment has: segment_id (s01, s02, ...); kind,
one of criterion | crosscheck | retrieval; criterion, the criteria-table key for a criterion segment, the pair
key (conductor_fault or sediment_sampling) for a cross-check, null for retrieval; purpose, one sentence saying
what the segment establishes; tool_calls, the calls the harness runs before the executor reads their results
(each {tool, args}; write "$cell" wherever an argument is the cell id); depends_on, the earlier segment ids a
cross-check combines (empty for a criterion or a retrieval).

You may reorder the criteria, add retrieval segments with your own closed-book queries, and drop a criterion
whose feature the coverage flags mark unmeasured here. You may not add a tool outside the list, execute a
folklore criterion (weight zero; the verifier may name it, no executor establishes it), or name a place in a
query or a purpose. Every number the executors will write must cite a value id from these tools, so a segment
with no tool call establishes nothing.

Return JSON with exactly one field: segments, the list above."""


def planner_system() -> str:
    return _closed(PLANNER_BRIEF)


def planner_user(criteria_lines: list[str], tools_help: list[str], coverage_flags: dict[str, str]) -> str:
    """The criteria (as `v0._criteria` renders them), the tools the harness will run (`TOOL_HELP` lines), and
    per feature whether it is measured, thin or unmeasured for this cell."""
    lines = ["The criteria, with status, weight and how each one fails:", *criteria_lines, "",
             "The tools the harness can run for a segment (their results carry the value ids the executor "
             "will cite):", *(f"  {h}" for h in tools_help), "",
             "Coverage of each feature at this cell:"]
    lines += [f"  {feature}: {flag}" for feature, flag in coverage_flags.items()] or ["  (no coverage flags)"]
    lines += ["", "Plan the segments. Do not name places, and do not try to work out where this is."]
    return _closed("\n".join(lines))


# ---------------------------------------------------------------- the manifest

def prompt_hashes() -> dict[str, str]:
    """sha256 of every fixed prompt text, keyed by role, so a run manifest names what each stage was told.
    The user prompts vary per segment and per chain and are hashed into each call's cache key instead."""
    return {
        "executor_system": hash_text(default_executor_system()),
        "verifier_system": hash_text(verifier_system()),
        "adjudicator_system": hash_text(adjudicator_system()),
        "planner_system": hash_text(planner_system()),
    }
