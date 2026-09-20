"""The mechanical gate: each of the five rules with a node that passes and one that fails, the escalating
feedback, and the shape of a recorded unknown."""

from __future__ import annotations

import pytest

from uranium_explorer.analyst import nodegate as G
from uranium_explorer.analyst import wire as W
from uranium_explorer.prospect import criteria as C
from uranium_explorer.values import stat

CS = C.load()
CELL = "b01"
COND = f"b:{CELL}:cell:d_conductor_m"
COND_OBS = f"b:{CELL}:cell:d_conductor_m:n_obs"
FAULT = f"b:{CELL}:cell:d_fault_m"
DEPTH = f"b:{CELL}:cell:unconformity_depth_m"
SED = f"b:{CELL}:cell:sed_u_max_ppm"
SED_CRIT = f"b:{CELL}:crit:lake_sediment_uranium"
HOST = f"b:{CELL}:cell:graphitic_host"
COV = "c:cov:d_conductor_m"
OTHER = "b:b02:cell:d_conductor_m"
VALUES = {
    COND: stat(COND, 820.0, fmt="m1", unit="m"),
    COND_OBS: stat(COND_OBS, 3),
    FAULT: stat(FAULT, 4800.0, fmt="m1", unit="m"),
    DEPTH: stat(DEPTH, 512.7, fmt="m1", unit="m"),
    SED: stat(SED, 31.5, fmt="m1", unit="ppm"),
    SED_CRIT: stat(SED_CRIT, 0.2, fmt="ratio3"),
    HOST: stat(HOST, 1, fmt="int"),
    COV: stat(COV, 0.98, fmt="ratio3"),
    OTHER: stat(OTHER, 240.0, fmt="m1", unit="m"),
}
CONTEXT = "provincial survey 1975-1978\n"
ALLOWED = {CELL}
UNKNOWN_HERE = {"water_u_max_ppm", "boulder_max_cps"}


def node(criterion: str = "conductor_proximity", status: str = "met", strength: int = 4, ids: list[str] | None = None,
         text: str = "The nearest mapped conductor is 820 m away, from 3 observations.", **kw) -> W.Node:
    fields = dict(node_id="n01", segment_id="s01", kind="criterion", criterion=criterion, status=status,
                  strength=strength, value_ids=[COND, COND_OBS] if ids is None else ids, text=text)
    fields.update(kw)
    return W.Node(**fields)


def gate(n: W.Node, unknown: set[str] = UNKNOWN_HERE) -> list[str]:
    return G.check_node(n, VALUES, CONTEXT, CS, ALLOWED, unknown)


# ---------------------------------------------------------------- rule 1: every number resolves

def test_rule_1_every_number_resolves_to_a_cited_id() -> None:
    assert gate(node()) == []
    assert gate(node(text="The 1975-1978 survey found a conductor 820 m away.")) == [], "a quoted period is text"
    problems = gate(node(text="The conductor is 820 m away and grades reached 2.4% U3O8."))
    assert problems == ["ids: the number 2.4 is not backed by any value this claim cites"]
    problems = gate(node(ids=[COND, "b:b01:cell:no_such"]))
    assert any(p.startswith("ids: cites b:b01:cell:no_such, which no tool returned") for p in problems)


# ---------------------------------------------------------------- rule 2: cell identity

def test_rule_2_every_cited_id_names_this_cell_or_the_grid() -> None:
    assert gate(node(ids=[COND, COV], text="The conductor is 820 m away; the feature covers 0.98 of the grid.")) == []
    problems = gate(node(ids=[COND, OTHER], text="The conductor is 820 m away, or 240 m in the cell next door."))
    assert problems == [f"ids: {OTHER} cites an id from another cell (b02)"]
    assert G.check_node(node(ids=[COND, OTHER], text="Conductors at 820 m here and 240 m there."), VALUES, CONTEXT,
                        CS, {CELL, "b02"}, UNKNOWN_HERE) == [], "a declared neighbour may be cited"
    assert any("is not a value id" in p for p in gate(node(ids=[COND, "820"])))


# ---------------------------------------------------------------- rule 3: polarity

def test_rule_3_met_needs_a_favourable_value_and_not_met_an_unfavourable_one() -> None:
    assert gate(node()) == [], "820 m is inside the falling(500, 5000) window's favourable half"
    assert gate(node("fault_proximity", "not_met", 1, [FAULT], "The nearest fault is 4,800 m away.")) == []
    wrong = gate(node("fault_proximity", "met", 3, [FAULT], "The nearest fault is 4,800 m away."))
    assert len(wrong) == 1 and wrong[0].startswith("polarity: says met but cites " + FAULT) and "unfavourable" in wrong[0]
    wrong = gate(node(status="not_met", strength=1))
    assert len(wrong) == 1 and wrong[0].startswith("polarity: says not_met but cites " + COND) and "favourable" in wrong[0]
    hedged = node(status="not_met", strength=1, text="The conductor is 820 m away; however, it is a single short trace.")
    assert gate(hedged) == [], "citing the other side is allowed when the node says so"
    assert gate(node("unconformity_depth", "met", 3, [DEPTH], "The unconformity is 512.7 m down.")) == []
    assert gate(node("graphitic_host", "met", 2, [HOST], "The mapped bedrock is a graphitic gneiss.")) == []
    uncited = gate(node(ids=[COND_OBS], text="There are 3 observations."))
    assert uncited == ["polarity: a met node for conductor_proximity must cite its feature value (d_conductor_m) or its membership id"]


def test_rule_3_percentile_criteria_are_judged_from_the_membership_id_only() -> None:
    by_value = node("lake_sediment_uranium", "met", 3, [SED], "Lake sediment reaches 31.5 ppm nearby.")
    assert gate(by_value) == [], "no absolute threshold exists, so the raw value cannot be checked"
    by_membership = node("lake_sediment_uranium", "met", 3, [SED, SED_CRIT], "Lake sediment reaches 31.5 ppm; membership 0.2.")
    problems = gate(by_membership)
    assert len(problems) == 1 and problems[0].startswith("polarity: says met but cites " + SED_CRIT)
    assert "membership at or above 0.5" in problems[0]
    assert gate(node("lake_sediment_uranium", "not_met", 1, [SED, SED_CRIT], "Only 31.5 ppm; membership 0.2.")) == []


# ---------------------------------------------------------------- rule 4: unknown only where unmeasured

def test_rule_4_unknown_only_where_the_feature_has_no_value() -> None:
    ok = node("lake_water_uranium", "unknown", 0, [], "No lake-water sample within reach.", unknown_reason="no rows")
    assert gate(ok) == []
    dodge = node(status="unknown", strength=0, text="The conductor distance of 820 m is hard to read.")
    problems = gate(dodge)
    assert problems == [f"unknown: cites {COND}, a measured value for d_conductor_m; unknown is only for a feature "
                        f"with no value here (measured and not met is not_met)"]
    assert gate(dodge, unknown={"d_conductor_m"}) == [], "when coverage says unmeasured, the harness's word holds"
    strong = node("lake_water_uranium", "unknown", 2, [], "No lake-water sample within reach.")
    assert gate(strong) == ["strength: must be 0 when the status is unknown, not 2"]
    crosscheck = node("conductor_fault", "unknown", 1, [], "Nothing to combine.", kind="crosscheck")
    assert gate(crosscheck) == ["strength: must be 0 when the status is unknown, not 1"]


# ---------------------------------------------------------------- rule 5: no arithmetic

@pytest.mark.parametrize("text", [
    "The conductor is 820 m away, so 820 = 0.82 km.",
    "Roughly ≈ 820 m to the conductor.",
    "The sum of 820 m and the fault distance is small.",
    "On average 820 m separates them.",
    "A total of 3 observations back the 820 m distance.",
    "About 3 + 3 lines cross the 820 m corridor.",
])
def test_rule_5_arithmetic_is_refused(text: str) -> None:
    problems = [p for p in gate(node(text=text)) if p.startswith("arithmetic")]
    assert len(problems) == 1, text


def test_rule_5_a_survey_period_and_a_unit_ratio_are_not_arithmetic() -> None:
    assert gate(node(text="The 1975-1978 survey mapped the conductor 820 m away.")) == []
    assert not any(p.startswith("arithmetic") for p in gate(node(text="The conductor is 820 m away, per the km/km2 layer.")))


def test_the_gate_also_reports_structural_problems_and_a_folklore_criterion() -> None:
    assert "status: 'maybe' is not one of ('met', 'not_met', 'unknown')" in gate(node(status="maybe"))
    assert gate(node("em_bright_spot", ids=[COND], text="Bright.")) == ["criterion: em_bright_spot is folklore and is never executed"]
    assert gate(node("moon_phase", ids=[COND], text="Full.")) == ["criterion: 'moon_phase' is not in the criteria table"]


# ---------------------------------------------------------------- feedback and the record

def test_feedback_escalates_over_three_attempts() -> None:
    problems = ["ids: the number 2.4 is not backed by any value this claim cites"]
    one = G.feedback(problems, 1, [COND, COND_OBS])
    two = G.feedback(problems, 2, [COND, COND_OBS])
    three = G.feedback(problems, 3, [COND, COND_OBS])
    assert problems[0] in one and COND not in one and "unknown" not in one.lower()
    assert problems[0] in two and COND in two and COND_OBS in two and "protocol" not in two.lower()
    assert problems[0] in three and COND in three and "protocol" in three.lower()
    assert "unknown" in three and "strength 0" in three and "unknown_reason" in three
    for line in G.PROTOCOL:
        assert line in three
    assert "(none)" in G.feedback(problems, 2, [])
    with pytest.raises(ValueError, match="^attempt"):
        G.feedback(problems, 4, [])
    assert G.MAX_ATTEMPTS == W.MAX_ATTEMPTS == 3


def test_record_unknown_keeps_the_node_and_masks_the_number_in_the_reason() -> None:
    failed = node(text="Grades reached 2.4% U3O8.", attempt=3, round=1, model="cheap", cost_usd=0.01)
    problems = ["ids: the number 2.4 is not backed by any value this claim cites", "arithmetic: x"]
    rec = G.record_unknown(failed, problems)
    assert (rec.status, rec.strength, rec.value_ids, rec.expert_ids, rec.published) == ("unknown", 0, [], [], False)
    assert rec.text == "gate: ids: the number # is not backed by any value this claim cites"
    assert rec.problems == problems and rec.unknown_reason
    assert (rec.node_id, rec.segment_id, rec.criterion, rec.attempt, rec.round, rec.model, rec.cost_usd) == \
        ("n01", "s01", "conductor_proximity", 3, 1, "cheap", 0.01)
    assert rec.check() == [] and gate(rec) == [], "the record itself passes the gate and validates"
    assert failed.status == "met", "the failed attempt is not mutated"
    with pytest.raises(ValueError, match="^problems"):
        G.record_unknown(failed, [])
