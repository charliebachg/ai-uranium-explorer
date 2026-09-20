"""Stage 1, the template planner: the fixed plan a chain follows when no model plans it.

The plan is a list of segments over the criteria table, in the table's order: one criterion segment per
counted criterion, then the two cross-checks the handbook reads together, then a retrieval pass when the arm
asks for one. It is the "without planner" arm of PRD 8.5, so it must be dull on purpose: the same criteria
set and switches always give byte-identical JSON, and nothing in it depends on the cell. That is what lets a
run over a benchmark be compared with a model-planned run segment for segment.

**The `$cell` placeholder.** A plan is built once and executed over many cells, so a tool call's arguments
cannot carry a cell id. Every argument whose value is exactly `CELL` (`"$cell"`) is substituted by the harness
with the cell it is executing (`bind`). No other placeholder exists; every other argument is a literal the
tool takes as written (a layer name, a feature key, a radius in metres, a query string).

**Folklore is never executed.** A folklore criterion carries weight zero and has no published test; the
verifier may name it when a chain leans on it, but no executor is ever asked to establish it, because a node
with a strength is a number the deciders would read.

**Switches remove tool calls.** An arm with `effort_features` off may not have the executor shown an effort
layer or feature, so the template drops any call that names one; `criteria` off drops `criteria_breakdown`;
`label_context` and `oof_scores` off drop their tools (the template never calls them, and `check_plan`
holds a model planner to the same rule).
"""

from __future__ import annotations

from typing import Any

from ..prospect import models as M
from ..prospect.criteria import CriteriaSet, Criterion
from .arms import Switches
from .wire import ALLOWED_TOOLS, CROSSCHECKS, Plan, Segment

TEMPLATE_VERSION = "analyst/v1/template/v1"
CELL = "$cell"

#: the evidence layer each criterion feature is built from, for `nearby`; a feature not listed here (the
#: interpolated unconformity depth, which comes from drillhole collars) gets no neighbourhood call
FEATURE_LAYER: dict[str, str] = {
    "d_conductor_m": "em_conductors",
    "conductor_density": "em_conductors",
    "d_fault_m": "faults_250k",
    "fault_density": "faults_250k",
    "sed_u_max_ppm": "lake_sediment_gsc",
    "water_u_max_ppm": "lake_water_sgs",
    "boulder_max_cps": "radioactive_boulders",
    "graphitic_host": "bedrock_250k",
}
#: layers that exist only because somebody explored: collars and survey footprints
EFFORT_LAYERS = ("compilation", "survey_footprints_airborne", "survey_footprints_ground")
#: the point features are built within 5 km, so that is the neighbourhood a criterion is asked about unless
#: its own threshold reaches further
DEFAULT_RADIUS_M = 5000.0
RETRIEVAL_K = 6
#: a closed-book query: what the handbook says to look for, naming no ground
RETRIEVAL_QUERY = ("graphitic conductor, basement fault, unconformity depth, clay alteration, lake sediment "
                   "uranium anomaly, radioactive boulders")

#: what each cross-check establishes and which criterion keys it combines
_CROSSCHECK_PURPOSE: dict[str, tuple[str, tuple[str, ...]]] = {
    "conductor_fault": ("Establish whether the conductor and the fault corridor coincide here: a graphitic "
                        "conductor along a fault is the pathway-and-trap pairing the deposit model requires, "
                        "and either alone is weaker than the two together.",
                        ("conductor_proximity", "fault_proximity")),
    "sediment_sampling": ("Establish whether the lake-sediment reading rests on enough sampling to mean "
                          "anything: one lake's maximum is not a survey, and a high value from thin sampling "
                          "says less than the same value from dense sampling.",
                          ("lake_sediment_uranium",)),
}


def bind(args: dict[str, Any], cell_id: str) -> dict[str, Any]:
    """The tool call's arguments for one cell: `$cell` replaced, everything else as written."""
    return {k: (cell_id if v == CELL else v) for k, v in args.items()}


def _names_effort(tool: str, args: dict[str, Any]) -> bool:
    return any(v in EFFORT_LAYERS or v in M.EFFORT_FEATURES for v in args.values() if isinstance(v, str))


def allowed_call(tool: str, args: dict[str, Any], sw: Switches) -> bool:
    """Whether the switches let this tool call reach an executor. Off means the call is not made, so the
    model is never shown what it must not use."""
    if not sw.effort_features and _names_effort(tool, args):
        return False
    if not sw.criteria and tool == "criteria_breakdown":
        return False
    if not sw.label_context and tool == "label_context":
        return False
    if not sw.oof_scores and tool == "cell_scores":
        return False
    return True


def _criterion_calls(c: Criterion) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = [
        {"tool": "cell_features", "args": {"cell_id": CELL}},
        {"tool": "criteria_breakdown", "args": {"cell_id": CELL}},
        {"tool": "coverage", "args": {"feature_key": c.feature}},
    ]
    layer = FEATURE_LAYER.get(c.feature)
    if layer:
        radius = c.params.get("hi", DEFAULT_RADIUS_M) if c.shape == "falling" else DEFAULT_RADIUS_M
        calls.append({"tool": "nearby", "args": {"cell_id": CELL, "layer": layer, "radius_m": float(radius)}})
    return calls


def _criterion_purpose(c: Criterion) -> str:
    return (f"Establish whether the {c.element} criterion {c.key} holds here: {c.title.rstrip('.')}. Decide "
            f"met, not_met or unknown from its own feature ({c.feature}) and the coverage behind it.")


def template_plan(criteria: CriteriaSet, switches: Switches, retrieval: bool = False) -> Plan:
    """One segment per counted criterion in table order, the two cross-checks, and a retrieval pass when
    asked. Deterministic: the segment ids are positional, and nothing here reads the clock or the store."""
    segments: list[Segment] = []
    by_criterion: dict[str, str] = {}

    def add(kind: str, criterion: str | None, purpose: str, calls: list[dict[str, Any]],
            depends_on: list[str]) -> Segment:
        seg = Segment(segment_id=f"s{len(segments) + 1:02d}", kind=kind, criterion=criterion, purpose=purpose,
                      tool_calls=[c for c in calls if allowed_call(c["tool"], c["args"], switches)],
                      depends_on=depends_on)
        segments.append(seg)
        return seg

    for c in criteria.criteria:
        if c.status == "folklore":
            continue
        by_criterion[c.key] = add("criterion", c.key, _criterion_purpose(c), _criterion_calls(c), []).segment_id
    for pair in CROSSCHECKS:
        purpose, keys = _CROSSCHECK_PURPOSE[pair]
        deps = [by_criterion[k] for k in keys if k in by_criterion]
        if not deps:
            continue  # nothing to combine: the criteria this pair reads are not in the table
        calls: list[dict[str, Any]] = [{"tool": "crosscheck", "args": {"cell_id": CELL}}]
        if pair == "sediment_sampling":
            sed = next(c for c in criteria.criteria if c.key == "lake_sediment_uranium")
            calls += [{"tool": "cell_features", "args": {"cell_id": CELL}},
                      {"tool": "coverage", "args": {"feature_key": sed.feature}}]
        add("crosscheck", pair, purpose, calls, deps)
    if retrieval:
        add("retrieval", None,
            "Retrieve what the assessment record says about this ground, most trustworthy tier first, so a "
            "reading no feature carries (alteration, drilling results) can be named with its page.",
            [{"tool": "retrieve", "args": {"query": RETRIEVAL_QUERY, "cell_id": CELL, "k": RETRIEVAL_K}}], [])
    return Plan(planner="template", segments=segments, prompt_version=TEMPLATE_VERSION).validate()


def check_plan(plan: Plan, criteria: CriteriaSet, switches: Switches) -> list[str]:
    """What a model planner may not do, checked mechanically: name a tool outside the list, execute a
    criterion the table does not have or marks folklore, call a switched-off tool, or depend on a segment
    that is not there. Structural problems come from the plan's own `check`."""
    problems = list(plan.check())
    known = {c.key: c for c in criteria.criteria}
    for s in plan.segments:
        if s.kind == "criterion":
            c = known.get(s.criterion or "")
            if c is None:
                problems.append(f"{s.segment_id}: criterion {s.criterion!r} is not in the criteria table")
            elif c.status == "folklore":
                problems.append(f"{s.segment_id}: {s.criterion} is folklore; it may be named by the verifier, "
                                f"never executed")
        for call in s.tool_calls:
            tool, args = call.get("tool", ""), call.get("args") or {}
            if tool not in ALLOWED_TOOLS:
                problems.append(f"{s.segment_id}: tool {tool!r} is not one of {ALLOWED_TOOLS}")
            elif not allowed_call(tool, args, switches):
                problems.append(f"{s.segment_id}: {tool}({args}) is switched off for this arm")
    return list(dict.fromkeys(problems))
