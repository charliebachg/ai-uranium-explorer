"""Stage 3, the mechanical verifier: the free gate every node passes before a model sees it.

Six rules, none of them judgement:

1. **Every number resolves.** `memo.check_claims` binds each number in the text to a cited value id, with
   the same leniency for quoted strings a memo gets and no more.
2. **Cell identity.** Every cited id names the cell being assessed (or a declared neighbour) or is grid-wide
   (a coverage share, a passage page, a model metric). A value from another cell is somebody else's evidence.
3. **Polarity.** A criterion node saying `met` must cite its own feature value or membership, and that value
   must lie on the favourable side of the criterion's threshold; `not_met` the reverse. The favourable side
   is the criteria table's own membership rule (`criteria.membership` at or above 0.5), so the gate and the
   breakdown's `state` can never disagree. Percentile criteria have no absolute threshold, so for them only
   the membership id counts. A node may state the opposite if it says so ("however", "but", "despite").
4. **Unknown only where unmeasured.** A node may say `unknown` when its feature has no value here or it
   cites no value for that feature; citing a present value for the feature and calling it unknown is a
   dodge. Strength is 0 when unknown. A feature whose layer was never mapped around the cell (rule 6) counts
   as unmeasured here, whatever value the distance feature carries.
5. **No arithmetic.** An equals sign, a plus, a "≈", or "sum", "average", "total of", "per cent of" next
   to a number: the model computed, and the model never computes.
6. **Absence of mapping is not absence of features.** A `not_met` node is refused when the staged `nearby`
   (or `crosscheck`) result for its layer says `in_footprint` is 0: nothing of that layer was mapped or
   sampled around the cell, so an empty radius there is unknown, and the feedback names the layer and the
   cell and says to record unknown. A `met` node is never refused this way, because a feature inside the
   radius is evidence whatever the footprint says. The layer is the plan's own `FEATURE_LAYER` for a
   criterion and `CROSSCHECK_LAYERS` for a pair; a node whose registry holds no flag for its layer is not
   held to a footprint nobody computed.

A rejected node goes back to the executor with `feedback` that escalates the way STA-CoT's rule controller
does (their Appendix C.2): the reasons; then the reasons and the ids the node may cite; then the protocol in
three lines and permission to answer unknown. A node that fails the third time is recorded by
`record_unknown`, never dropped, so the verifier and the deciders see that the segment was tried.
"""

from __future__ import annotations

import re
from dataclasses import replace

import numpy as np

from ..prospect.criteria import CriteriaSet, Criterion, membership
from ..prospect.memo import NUMBER, NUMBER_LABEL, NUMBER_UNIT, check_claims
from .plan import FEATURE_LAYER
from .wire import MAX_ATTEMPTS, TEXT_MAX, Node, parse_id

__all__ = ["MAX_ATTEMPTS", "check_node", "feedback", "record_unknown"]

#: a node may cite a value against its status when it says so
HEDGES = re.compile(r"\b(however|but|despite)\b", re.I)
#: the evidence layers each cross-check pair combines, for rule 6: the same layers `crosscheck` flags
CROSSCHECK_LAYERS: dict[str, tuple[str, ...]] = {
    "conductor_fault": ("em_conductors", "faults_250k"),
    "sediment_sampling": ("lake_sediment_gsc",),
}
#: the last part of a footprint flag's id, from either tool: `c:nb:<cell>:<layer>:<radius>:in_footprint` and
#: `c:x:<cell>:<pair>:<layer>:in_footprint` both name the layer before it
IN_FOOTPRINT = "in_footprint"
#: the forms arithmetic takes in prose; a dash is not here because "1975-1978" is a survey period
ARITHMETIC: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\d\s*=\s*\d"), "an equals sign between numbers"),
    (re.compile(r"\d\s*[+×÷]\s*\d"), "an operator between numbers"),
    (re.compile(r"≈"), "an approximation sign"),
    (re.compile(r"\b(sum|average|total of|per ?cent of|percent of)\b[^.;]{0,24}\d|"
                r"\d[^.;]{0,24}\b(sum|average|total of|per ?cent of|percent of)\b", re.I),
     "a word for a computation next to a number"),
)
MEMBERSHIP_MET = 0.5


def _cited(node: Node, values: dict[str, dict], c: Criterion) -> tuple[str | None, str | None]:
    """(feature value id, membership id) among what the node cites, when the values registry has them."""
    feature_id = membership_id = None
    for vid in node.value_ids:
        if vid not in values:
            continue
        _cell, kind, suffix = parse_id(vid)
        if kind == "cell" and suffix == (c.feature,):
            feature_id = vid
        elif kind == "crit" and suffix == (c.key,):
            membership_id = vid
    return feature_id, membership_id


def _favourable(c: Criterion, feature_id: str | None, membership_id: str | None,
                values: dict[str, dict]) -> tuple[bool | None, str]:
    """Whether what the node cites favours the criterion, and the id that decided it. None when nothing
    cited can decide (a percentile criterion with only its feature value)."""
    if membership_id is not None:
        m = values[membership_id].get("value")
        if m is not None:
            return float(m) >= MEMBERSHIP_MET, membership_id
    if feature_id is not None and c.shape != "percentile_rising":
        x = values[feature_id].get("value")
        if isinstance(x, (int, float)):
            m = membership(c, np.asarray([float(x)]))[0][0]
            return bool(m >= MEMBERSHIP_MET), feature_id
    return None, ""


def _polarity(node: Node, values: dict[str, dict], c: Criterion) -> list[str]:
    feature_id, membership_id = _cited(node, values, c)
    if feature_id is None and membership_id is None:
        return [f"polarity: a {node.status} node for {c.key} must cite its feature value "
                f"({c.feature}) or its membership id"]
    favourable, by = _favourable(c, feature_id, membership_id, values)
    if favourable is None:
        return []  # a percentile criterion judged from its raw value: nothing absolute to check against
    says_met = node.status == "met"
    if says_met != favourable and not HEDGES.search(node.text):
        side = "favourable" if favourable else "unfavourable"
        rule = f"membership at or above {MEMBERSHIP_MET:g}" if by == membership_id else \
            f"{c.shape} {c.params}" if c.params else c.shape
        return [f"polarity: says {node.status} but cites {by} = {values[by].get('value')}, which is {side} "
                f"for {c.key} ({rule}); say so with 'however' or change the status"]
    return []


def _unknown(node: Node, values: dict[str, dict], c: Criterion, coverage_unknown: set[str]) -> list[str]:
    problems: list[str] = []
    if node.strength != 0:
        problems.append(f"strength: must be 0 when the status is unknown, not {node.strength}")
    feature_id, membership_id = _cited(node, values, c)
    present = [v for v in (feature_id, membership_id) if v is not None and values[v].get("value") is not None]
    if present and c.feature not in coverage_unknown:
        problems.append(f"unknown: cites {present[0]}, a measured value for {c.feature}; unknown is only for "
                        f"a feature with no value here (measured and not met is not_met)")
    return problems


def _layers(node: Node, c: Criterion | None) -> tuple[str, ...]:
    """The evidence layers a node's answer rests on: the criterion feature's layer, or a pair's layers."""
    if node.kind == "criterion":
        layer = FEATURE_LAYER.get(c.feature) if c else None
        return (layer,) if layer else ()
    if node.kind == "crosscheck":
        return CROSSCHECK_LAYERS.get(node.criterion or "", ())
    return ()


def _unmapped(values: dict[str, dict], cell_ids_allowed: set[str], layers: tuple[str, ...]
              ) -> dict[str, tuple[str, str]]:
    """Per layer whose staged footprint flag says 0, the (cell, id) that says so: the registry's word on
    where nothing was mapped or sampled. A layer with no flag in the registry is absent here, because a
    footprint nobody computed refuses nothing."""
    out: dict[str, tuple[str, str]] = {}
    for vid, v in sorted(values.items()):
        cell, kind, suffix = parse_id(vid)
        if kind not in ("nb", "x") or cell not in cell_ids_allowed or not suffix or suffix[-1] != IN_FOOTPRINT:
            continue
        layer = next((layer for layer in layers if layer in suffix[:-1]), None)
        if layer is not None and layer not in out and v.get("value") == 0:
            out[layer] = (cell, vid)
    return out


def _footprint(unmapped: dict[str, tuple[str, str]]) -> list[str]:
    """Rule 6, for a `not_met` node: one line per layer whose footprint does not reach the cell, naming the
    layer, the cell and the flag, and saying what to return instead."""
    return [f"footprint: nothing of {layer} was mapped or sampled around cell {cell} ({vid} is 0), so an empty "
            f"radius there is unknown, not not_met; return status unknown with strength 0 and say so in "
            f"unknown_reason" for layer, (cell, vid) in unmapped.items()]


def check_node(node: Node, values: dict[str, dict], context: str, criteria: CriteriaSet,
               cell_ids_allowed: set[str], coverage_unknown: set[str]) -> list[str]:
    """Every problem with a node, as feedback lines. `values` is the registry of what the tools returned for
    this segment, `context` what a claim may quote without citing (`v0.gate_context`), `cell_ids_allowed`
    the cell and its declared neighbours, `coverage_unknown` the features with no value here."""
    problems = list(node.check())
    # 1. every number resolves; the memo gate's wording, without its "claim 0:" prefix
    problems += ["ids: " + m.split(": ", 1)[-1] for m in
                 check_claims([{"text": node.text, "value_ids": node.value_ids}], values, context)]
    # 2. cell identity
    for vid in node.value_ids:
        cell, kind, _suffix = parse_id(vid)
        if not kind:
            problems.append(f"ids: {vid!r} is not a value id")
        elif cell is not None and cell not in cell_ids_allowed:
            problems.append(f"ids: {vid} cites an id from another cell ({cell})")
    c = next((c for c in criteria.criteria if c.key == node.criterion), None) if node.kind == "criterion" else None
    unmapped = _unmapped(values, cell_ids_allowed, _layers(node, c))
    if node.kind == "criterion":
        if c is None:
            problems.append(f"criterion: {node.criterion!r} is not in the criteria table")
        elif c.status == "folklore":
            problems.append(f"criterion: {node.criterion} is folklore and is never executed")
        elif node.status in ("met", "not_met"):
            problems += _polarity(node, values, c)     # 3
        elif node.status == "unknown":
            # 4: outside the layer's footprint the feature is unmeasured here, whatever value it carries
            problems += _unknown(node, values, c, coverage_unknown | ({c.feature} if unmapped else set()))
    elif node.status == "unknown" and node.strength != 0:
        problems.append(f"strength: must be 0 when the status is unknown, not {node.strength}")
    # 5. no arithmetic
    for pattern, what in ARITHMETIC:
        if pattern.search(node.text):
            problems.append(f"arithmetic: the text has {what}; the model never computes")
    # 6. absence of mapping is not absence of features
    if node.status == "not_met":
        problems += _footprint(unmapped)
    return list(dict.fromkeys(problems))


PROTOCOL = (
    "1. Every number in text has its value id in value_ids: no other number, no arithmetic, no place name.",
    "2. met or not_met only from the criterion's own feature value or membership id, on the side its "
    "threshold says; say 'however' if you cite the other side.",
    "3. unknown, with strength 0 and an unknown_reason, when the feature has no value here or nothing of its "
    "layer was mapped or sampled around the cell (in_footprint is 0).",
)


def feedback(problems: list[str], attempt: int, allowed_ids: list[str]) -> str:
    """What the executor is told when its node is rejected on `attempt` (1-based). Escalates: reasons; then
    reasons and the ids the node may cite; then the protocol and permission to answer unknown."""
    if not 1 <= attempt <= MAX_ATTEMPTS:
        raise ValueError(f"attempt: {attempt} is outside 1..{MAX_ATTEMPTS}")
    lines = ["Your node was rejected by the mechanical gate:", *(f"- {p}" for p in problems)]
    if attempt >= 2:
        lines += ["", "The only ids this node may cite:", *([f"  {vid}" for vid in allowed_ids] or ["  (none)"])]
    if attempt >= MAX_ATTEMPTS:
        lines += ["", "This is the last attempt. The protocol, in three lines:", *PROTOCOL, "",
                  "If you cannot satisfy it from what is staged, return status unknown with strength 0 and "
                  "say why in unknown_reason. Unknown is a respectable answer; a number from nowhere is not."]
    else:
        lines += ["", "Return the node again, corrected."]
    return "\n".join(lines)


def _mask_numbers(text: str) -> str:
    """A gate reason quotes the offending number; the record must not, or the verifier would read it."""
    for pattern in (NUMBER_UNIT, NUMBER_LABEL, NUMBER):
        text = pattern.sub("#", text)
    return text


def record_unknown(node: Node, problems: list[str]) -> Node:
    """The node the harness stores when the last attempt fails: unknown, strength 0, nothing cited, the
    gate's first reason as its text, unpublished, with every problem kept. Never dropped: the verifier sees
    that the segment was tried and why it has no answer."""
    if not problems:
        raise ValueError("problems: a node that passed the gate is published, not recorded as unknown")
    text = f"gate: {_mask_numbers(' '.join(problems[0].split()))}"
    return replace(node, status="unknown", strength=0, value_ids=[], expert_ids=[], text=text[:TEXT_MAX],
                   unknown_reason="rejected by the mechanical gate on every attempt", published=False,
                   problems=list(problems))
