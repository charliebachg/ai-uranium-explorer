"""The template planner: deterministic, one segment per counted criterion, cross-checks that resolve, and
nothing a switch has turned off."""

from __future__ import annotations

import json

from uranium_explorer.analyst import plan as P
from uranium_explorer.analyst import wire as W
from uranium_explorer.analyst.arms import Switches, load_arm
from uranium_explorer.analyst.v0 import place_names_in
from uranium_explorer.prospect import criteria as C
from uranium_explorer.prospect.models import EFFORT_FEATURES

CS = C.load()
OFF = load_arm("v0").switches
ALL_ON = Switches(drillholes=True, label_context=True, oof_scores=True, effort_features=True, criteria=True)


def test_the_template_is_deterministic_and_its_json_is_stable() -> None:
    a = P.template_plan(CS, OFF).as_dict()
    b = P.template_plan(CS, OFF).as_dict()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["planner"] == "template" and a["prompt_version"] == P.TEMPLATE_VERSION
    assert [s["segment_id"] for s in a["segments"]] == [f"s{i:02d}" for i in range(1, len(a["segments"]) + 1)]
    assert W.Plan.from_dict(a) == P.template_plan(CS, OFF)


def test_one_criterion_segment_per_counted_criterion_in_table_order_and_none_for_folklore() -> None:
    plan = P.template_plan(CS, OFF)
    crit = [s for s in plan.segments if s.kind == "criterion"]
    assert [s.criterion for s in crit] == [c.key for c in CS.criteria if c.status != "folklore"]
    assert not {s.criterion for s in crit} & {c.key for c in CS.folklore}
    assert "conductor_strength" not in json.dumps(plan.as_dict()) and "em_bright_spot" not in json.dumps(plan.as_dict())
    for s in crit:
        assert s.depends_on == [] and s.purpose.endswith(".") and s.criterion in s.purpose
        tools = [c["tool"] for c in s.tool_calls]
        assert tools[:3] == ["cell_features", "criteria_breakdown", "coverage"]
        c = next(c for c in CS.criteria if c.key == s.criterion)
        assert s.tool_calls[2]["args"] == {"feature_key": c.feature}
        if c.feature in P.FEATURE_LAYER:
            assert tools[3] == "nearby"
            assert s.tool_calls[3]["args"]["layer"] == P.FEATURE_LAYER[c.feature]
            assert 500.0 <= s.tool_calls[3]["args"]["radius_m"] <= 20_000.0
        else:
            assert c.key == "unconformity_depth" and len(tools) == 3, "the interpolated depth has no layer to look around"
    conductor = next(s for s in crit if s.criterion == "conductor_proximity")
    assert conductor.tool_calls[3]["args"] == {"cell_id": "$cell", "layer": "em_conductors", "radius_m": 5000.0}


def test_crosschecks_depend_on_real_criterion_segments_and_call_crosscheck() -> None:
    plan = P.template_plan(CS, OFF)
    by_key = {s.criterion: s.segment_id for s in plan.segments if s.kind == "criterion"}
    xs = {s.criterion: s for s in plan.segments if s.kind == "crosscheck"}
    assert set(xs) == set(W.CROSSCHECKS) == {"conductor_fault", "sediment_sampling"}
    assert xs["conductor_fault"].depends_on == [by_key["conductor_proximity"], by_key["fault_proximity"]]
    assert xs["sediment_sampling"].depends_on == [by_key["lake_sediment_uranium"]]
    for s in xs.values():
        assert s.tool_calls[0] == {"tool": "crosscheck", "args": {"cell_id": "$cell"}}
        assert all(d in by_key.values() for d in s.depends_on)
    ids = [s.segment_id for s in plan.segments]
    assert all(ids.index(d) < ids.index(s.segment_id) for s in xs.values() for d in s.depends_on)


def test_retrieval_only_when_asked_and_its_query_names_no_place() -> None:
    without = P.template_plan(CS, OFF)
    assert not any(s.kind == "retrieval" for s in without.segments)
    assert not any(c["tool"] == "retrieve" for s in without.segments for c in s.tool_calls)
    with_it = P.template_plan(CS, OFF, retrieval=True)
    last = with_it.segments[-1]
    assert last.kind == "retrieval" and last.criterion is None and last.depends_on == []
    assert last.tool_calls == [{"tool": "retrieve", "args": {"query": P.RETRIEVAL_QUERY, "cell_id": "$cell", "k": 6}}]
    assert place_names_in(json.dumps(with_it.as_dict())) == []


def test_tool_names_are_within_the_allowed_list() -> None:
    for retrieval in (False, True):
        for sw in (OFF, ALL_ON):
            plan = P.template_plan(CS, sw, retrieval=retrieval)
            assert {c["tool"] for s in plan.segments for c in s.tool_calls} <= set(W.ALLOWED_TOOLS)
            assert plan.check() == [] and P.check_plan(plan, CS, sw) == []


def test_effort_off_names_no_effort_layer_or_feature_and_criteria_off_drops_the_breakdown() -> None:
    plan = P.template_plan(CS, OFF)
    dumped = json.dumps(plan.as_dict())
    assert "compilation" not in dumped and "survey_footprints" not in dumped
    assert not any(f in dumped for f in EFFORT_FEATURES)
    assert not any(c["tool"] in ("label_context", "cell_scores") for s in plan.segments for c in s.tool_calls)
    no_criteria = P.template_plan(CS, Switches(drillholes=False, label_context=False, oof_scores=False,
                                               effort_features=False, criteria=False))
    assert not any(c["tool"] == "criteria_breakdown" for s in no_criteria.segments for c in s.tool_calls)
    assert any(c["tool"] == "criteria_breakdown" for s in plan.segments for c in s.tool_calls)


def test_bind_substitutes_the_cell_placeholder_and_nothing_else() -> None:
    args = {"cell_id": "$cell", "layer": "em_conductors", "radius_m": 5000.0, "query": "$cell is not a query"}
    assert P.bind(args, "0001_0001") == {"cell_id": "0001_0001", "layer": "em_conductors", "radius_m": 5000.0,
                                         "query": "$cell is not a query"}
    assert P.CELL == "$cell"


def test_check_plan_holds_a_model_planner_to_the_rules() -> None:
    plan = P.template_plan(CS, OFF)
    plan.segments[0].criterion = "em_bright_spot"
    plan.segments[1].tool_calls.append({"tool": "label_context", "args": {"cell_id": "$cell"}})
    plan.segments[2].tool_calls.append({"tool": "nearby", "args": {"cell_id": "$cell", "layer": "compilation",
                                                                   "radius_m": 2000.0}})
    plan.segments[3].criterion = "moon_phase"
    plan.segments[4].tool_calls.append({"tool": "dig", "args": {}})
    problems = P.check_plan(plan, CS, OFF)
    assert any("folklore" in p and "s01" in p for p in problems)
    assert any("label_context" in p and "switched off" in p for p in problems)
    assert any("compilation" in p and "switched off" in p for p in problems)
    assert any("moon_phase" in p and "not in the criteria table" in p for p in problems)
    assert any("'dig'" in p for p in problems)
    assert P.check_plan(P.template_plan(CS, ALL_ON), CS, ALL_ON) == []
    # the same plan under a stricter arm is refused, so an arm cannot borrow another arm's plan
    assert any("switched off" in p for p in P.check_plan(P.template_plan(CS, ALL_ON), CS, OFF)) is False, \
        "the template calls nothing the headline arm turns off"


def test_segment_scope_names_each_segments_own_rows_and_nothing_else() -> None:
    plan = P.template_plan(CS, OFF, retrieval=True)
    by_key = {c.key: c for c in CS.criteria}
    for s in plan.segments:
        scope = P.segment_scope(s, CS)
        assert scope.key == s.segment_id
        if s.kind == "criterion":
            c = by_key[s.criterion]
            assert (scope.features, scope.criteria, scope.pairs) == ({c.feature}, {c.key}, set())
        elif s.kind == "crosscheck":
            keys = {"conductor_fault": {"conductor_proximity", "fault_proximity"},
                    "sediment_sampling": {"lake_sediment_uranium"}}[s.criterion]
            assert scope.criteria == keys and scope.features == {by_key[k].feature for k in keys} and scope.pairs == {s.criterion}
        else:
            assert (scope.features, scope.criteria, scope.pairs) == (set(), set(), set()), "a retrieval's passages are its own call"
    conductor = P.segment_scope(plan.segments[0], CS)
    assert conductor.keeps("cell_features", {"feature": "d_conductor_m"}) and not conductor.keeps("cell_features", {"feature": "d_fault_m"})
    assert conductor.keeps("criteria_breakdown", {"criterion": "conductor_proximity"}) and not conductor.keeps("crosscheck", {"pair": "conductor_fault"})
    assert conductor.keeps("nearby", {"layer": "faults_250k"}) and conductor.keeps("retrieve", {"tier": "page"}), \
        "a tool outside the tables is the segment's own call"
    rows = [{"feature": "d_conductor_m", "value_id": "c:cell:x:d_conductor_m"},
            {"feature": "d_fault_m", "value_id": "c:cell:x:d_fault_m", "thresholds": {"hi": {"value_id": "c:crit:x:fault_proximity:hi"}}}]
    values = {"c:cell:x:d_conductor_m": {}, "c:cell:x:d_fault_m": {}, "c:crit:x:fault_proximity:hi": {}, "c:orphan": {}}
    assert conductor.narrow("cell_features", rows, values) == (rows[:1], {"c:cell:x:d_conductor_m": {}})
    assert conductor.narrow("nearby", rows, values) == (rows, values)
    stray = W.Segment(segment_id="s99", kind="criterion", criterion="moon_phase", purpose="Establish the phase.")
    assert P.segment_scope(stray, CS) == P.Scope("s99"), "a criterion the table lacks sees nothing but the notes"
