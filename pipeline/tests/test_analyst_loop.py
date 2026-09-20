"""The staged loop: a clean chain end to end, the gate's escalation, the verifier's repairs, K exhausted, the
switches, the budget stop, the spans, and triage - every one on a scripted backend and a fake tool world."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from legacy_reader import store as ST
from legacy_reader.analyst import chains as CH
from legacy_reader.analyst import loop as L
from legacy_reader.analyst import nodegate as G
from legacy_reader.analyst import wire as W
from legacy_reader.analyst.arms import Inputs, Switches
from legacy_reader.analyst.session import Session
from legacy_reader.analyst.v0 import ABSTAIN, NEGATIVE, POSITIVE
from legacy_reader.prospect import criteria as C
from legacy_reader.runtime.spend import BudgetExhausted
from legacy_reader.runtime.tracing import read_spans, trace

from fake_loop_world import (CELL, COND, LoopBackend, LoopWorld, adjudicator_answer, bad_adjudicator, bad_node,
                             loop_registry, node_answer, verifier_answer, vid)

CS = C.load()
ALL_ON = Switches(drillholes=True, label_context=True, oof_scores=True, effort_features=True, criteria=True)
INPUTS = Inputs(card=False, pack=True, passages=False)
V0_ROW_KEYS = {"bench_id", "answer", "problems", "published", "cost_usd", "duration_s", "model_resolved", "cache_key", "from_cache"}
#: the template over the real criteria table: eight counted criteria, then the two cross-checks
SEGMENTS = {"s01": "conductor_proximity", "s02": "graphitic_host", "s03": "fault_proximity", "s04": "structural_density",
            "s05": "unconformity_depth", "s06": "lake_sediment_uranium", "s07": "lake_water_uranium",
            "s08": "boulder_train", "s09": "conductor_fault", "s10": "sediment_sampling"}


@pytest.fixture
def weights_stub(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """The weights module as an interface: a version and a fixed score, so nothing here depends on the fit."""
    stub = types.ModuleType("legacy_reader.analyst.weights")
    stub.criteria_weights = lambda criteria: SimpleNamespace(version="stub/v1")  # type: ignore[attr-defined]
    stub.score = lambda nodes, weights: 0.7  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "legacy_reader.analyst.weights", stub)
    return stub


def config(**over: Any) -> L.LoopConfig:
    base = dict(executor_model="m-small", verifier_model="m-large", adjudicator_model="m-large",
                planner_model="m-large", effort="low", segment_workers=2)
    base.update(over)
    return L.LoopConfig(**base)


def run(tmp_path: Path, backend: LoopBackend, world: LoopWorld | None = None, con: Any = None,
        **over: Any) -> tuple[dict[str, Any], Session]:
    """One cell on a dashboard session (real ids, no scrub) with the chains file beside the stage."""
    session = Session.open(CELL, "dashboard", fold=None, switches=ALL_ON, stage=tmp_path / "cell" / "stage",
                           tools=loop_registry(world or LoopWorld()))
    row = L.run_cell_v1(backend, session, None, INPUTS, ALL_ON, config(**over), CS, chain_id="ch-1", run_id="run-1",
                        con=con, stage=session.stage)
    return row, session


def chains_file(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "cell" / "chains" / f"{CELL}.json").read_text())


# ---------------------------------------------------------------- 1. a clean chain


def test_a_clean_chain_publishes_with_every_stage_counted_and_the_store_takes_it(tmp_path: Path, weights_stub) -> None:
    backend = LoopBackend()
    con = ST.connect(tmp_path / "s.duckdb")
    try:
        row, session = run(tmp_path, backend, con=con)
        assert V0_ROW_KEYS <= set(row) and {"chain", "stages"} <= set(row)
        assert row["bench_id"] == CELL and row["published"] is True and row["problems"] == []
        a = row["answer"]
        assert a["verdict"] == POSITIVE and a["probability"] == 0.7, "the weighted score is the probability under 'both'"
        assert a["adjudicator_probability"] == 0.8 and a["weighted_score"] == 0.7 and a["weights_version"] == "stub/v1"
        assert a["majority_label"] is None and a["verifier_label"] == POSITIVE
        assert a["claims"] == adjudicator_answer()["claims"] and a["next_observation"]
        assert row["model_resolved"] == "m-large-resolved" and row["cache_key"] and row["from_cache"] is False
        assert row["cost_usd"] == pytest.approx(12 * 0.05) and row["duration_s"] == pytest.approx(12.0), "10 executors, 1 verifier, 1 adjudicator"

        tasks = [t for t, _ in backend.calls]
        assert tasks.count(L.TASK_EXECUTE) == 10 and tasks.count(L.TASK_VERIFY) == 1 and tasks.count(L.TASK_ADJUDICATE) == 1
        assert {seg for t, seg in backend.calls if t == L.TASK_EXECUTE} == set(SEGMENTS)
        st = row["stages"]
        assert st["n_segments"] == 10 and st["n_nodes"] == 10 and st["attempts_total"] == 10
        assert st["n_gate_rejections"] == 0 and st["n_recorded_unknown"] == 0 and st["rounds"] == 1 and st["valid"] is True
        assert st["n_faulty_total"] == 0 and st["n_reexecuted"] == 0 and st["shallow"] is False
        assert st["n_refusals_by_rule"] == {"B17": 0, "B18": 0, "B30": 0, "switch": 0, "purpose": 0}
        assert st["verifier_agreement"] is True and st["decider_agreement"] is True

        chain = W.Chain.from_dict(row["chain"]).validate()
        assert chain.published and chain.decision is not None and chain.decision.final_verdict == POSITIVE
        assert [n.node_id for n in chain.nodes] == [f"n{i:02d}" for i in range(1, 11)]
        assert {n.segment_id: n.criterion for n in chain.nodes} == SEGMENTS
        assert all(n.published and n.round == 0 and n.attempt == 1 and n.model == "m-small-resolved" for n in chain.nodes)
        by_seg = {n.segment_id: n for n in chain.nodes}
        assert by_seg["s09"].depends_on == ["n01", "n03"] and by_seg["s10"].depends_on == ["n06"]
        assert by_seg["s07"].status == "unknown" and by_seg["s07"].strength == 0
        assert chains_file(tmp_path) == row["chain"]
        # every executor read its own segment's staged files and nothing else
        for req, (task, seg) in zip(backend.requests, backend.calls):
            if task == L.TASK_EXECUTE:
                names = [name for _path, name in req.stage_files]
                assert names and all((session.stage / n).is_file() for n in names)
                assert all(f"{{STAGE_DIR}}/{n}" in req.user_prompt for n in names)
                assert req.model == "m-small" and req.effort == "low" and req.schema == W.NODE_SCHEMA
        verify = next(r for r, (t, _) in zip(backend.requests, backend.calls) if t == L.TASK_VERIFY)
        assert verify.model == "m-large" and "n09 | s09 crosscheck conductor_fault" in verify.user_prompt
        assert f"0.8 [c:score:{CELL}:effort]" in verify.user_prompt, "the effort null, with its id"

        stored = CH.load_chain(con, "ch-1")
        assert stored["chain"]["published"] is True and stored["chain"]["final_verdict"] == POSITIVE
        assert stored["chain"]["models_json"] == {"executor": "m-small", "verifier": "m-large", "adjudicator": "m-large"}
        assert stored["chain"]["cell_id"] == CELL and stored["chain"]["bench_id"] is None and stored["chain"]["rounds"] == 1
        assert len(stored["nodes"]) == 10 and len(stored["verdicts"]) == 1 and stored["decision"]["published"] is True
        assert stored["decision"]["values_json"] == {COND: session.values[COND]}
    finally:
        con.close()


# ---------------------------------------------------------------- 2. the gate's escalation


def test_a_node_the_gate_rejects_three_times_is_recorded_unknown_and_the_chain_stays_unpublished(tmp_path: Path, weights_stub) -> None:
    backend = LoopBackend(executor={"s01": [bad_node(), bad_node(), bad_node()]})
    row, session = run(tmp_path, backend)
    reqs = backend.executor_requests("s01")
    assert len(reqs) == 3
    problem = "the number 2.4 is not backed by any value this claim cites"
    assert problem not in reqs[0].user_prompt
    assert problem in reqs[1].user_prompt and COND in reqs[1].user_prompt, "attempt 2: the reason and the ids it may cite"
    assert problem in reqs[2].user_prompt and "last attempt" in reqs[2].user_prompt and "unknown_reason" in reqs[2].user_prompt
    assert len({r.cache_key("claude_cli") for r in reqs}) == 3, "each attempt is its own call"
    chain = W.Chain.from_dict(row["chain"]).validate()
    n01 = next(n for n in chain.nodes if n.segment_id == "s01")
    assert (n01.status, n01.strength, n01.published, n01.attempt, n01.value_ids) == ("unknown", 0, False, 3, [])
    assert n01.text.startswith("gate: ids: the number # is not backed") and n01.problems
    assert len(chain.nodes) == 10 and not chain.published and row["published"] is False
    st = row["stages"]
    assert st["attempts_total"] == 12 and st["n_gate_rejections"] == 3 and st["n_recorded_unknown"] == 1
    assert st["valid"] is True, "the verifier accepted the chain; the node gate is what refused publication"
    assert row["problems"] == [], "the adjudicator's answer itself passed its gate"
    cross = next(n for n in chain.nodes if n.segment_id == "s09")
    assert cross.depends_on == ["n01", "n03"] and n01.line() in backend.executor_requests("s09")[0].user_prompt, \
        "the cross-check saw the unknown record, never a dropped node"


# ---------------------------------------------------------------- 3. refinement


def test_a_faulted_node_and_its_dependents_are_re_executed_in_the_next_round(tmp_path: Path, weights_stub) -> None:
    reason = "the conductor node rests on a single short trace"
    backend = LoopBackend(verifier=[verifier_answer(False, [{"node_id": "n01", "reason": reason}], feedback="Re-read the conductor evidence."),
                                    verifier_answer(True)])
    row, session = run(tmp_path, backend, rounds=2)
    redone = [seg for t, seg in backend.calls[10:] if t == L.TASK_EXECUTE]
    assert sorted(redone) == ["s01", "s09"], "the faulted node and the cross-check that depends on it, nothing else"
    s01, s09 = backend.executor_requests("s01")[1], backend.executor_requests("s09")[1]
    assert s01.user_prompt.startswith("The chain verifier faulted your earlier node n01") and reason in s01.user_prompt
    assert "Re-read the conductor evidence." in s01.user_prompt
    assert "n01" in s09.user_prompt.splitlines()[0] and "faulted" in s09.user_prompt.splitlines()[0]
    assert "n11 | s01 criterion conductor_proximity" in s09.user_prompt, "the cross-check builds on the new node"
    chain = W.Chain.from_dict(row["chain"]).validate()
    second = [n for n in chain.nodes if n.round == 1]
    assert [(n.node_id, n.segment_id, n.attempt) for n in second] == [("n11", "s01", 1), ("n12", "s09", 1)]
    assert second[1].depends_on == ["n11", "n03"]
    assert [n.node_id for n in chain.current_nodes()] == ["n11", "n02", "n03", "n04", "n05", "n06", "n07", "n08", "n12", "n10"]
    assert [v.valid for v in chain.verdicts] == [False, True] and chain.verdicts[0].faulty_ids() == ["n01"]
    st = row["stages"]
    assert st["rounds"] == 2 and st["n_reexecuted"] == 2 and st["n_faulty_total"] == 1 and st["valid"] is True
    assert st["attempts_total"] == 12 and row["published"] is True
    verify_prompts = [r.user_prompt for r, (t, _) in zip(backend.requests, backend.calls) if t == L.TASK_VERIFY]
    assert "n01 |" in verify_prompts[0] and "n11 |" in verify_prompts[1] and "n01 |" not in verify_prompts[1]


# ---------------------------------------------------------------- 4. K exhausted


def test_k_exhausted_takes_the_majority_over_rounds_or_abstains_without_one(tmp_path: Path, weights_stub) -> None:
    def invalid(label: str) -> dict[str, Any]:
        return verifier_answer(False, [{"node_id": "n01", "reason": "still rests on one trace"}], label=label)

    row, _ = run(tmp_path, LoopBackend(verifier=[invalid(POSITIVE), invalid(POSITIVE)]), rounds=2)
    d = W.Chain.from_dict(row["chain"]).decision
    assert d is not None and d.majority_label == POSITIVE and d.final_verdict == POSITIVE and d.abstained_reason is None
    assert row["answer"]["verdict"] == POSITIVE and row["answer"]["majority_label"] == POSITIVE
    assert row["stages"]["valid"] is False and row["stages"]["rounds"] == 2 and row["published"] is False
    assert row["stages"]["verifier_agreement"] is True

    row, _ = run(tmp_path / "b", LoopBackend(verifier=[invalid(POSITIVE), invalid(NEGATIVE)]), rounds=2)
    d = W.Chain.from_dict(row["chain"]).decision
    assert d is not None and d.majority_label is None and d.final_verdict == ABSTAIN
    assert d.abstained_reason == "no round validated and no majority"
    assert row["answer"]["verdict"] == ABSTAIN and row["answer"]["probability"] == 0.7, "the weighted score still scores it"
    assert row["stages"]["verifier_agreement"] is False and row["published"] is False


# ---------------------------------------------------------------- 5. no verifier


def test_verifier_none_skips_the_call_and_the_adjudicator_decides(tmp_path: Path, weights_stub) -> None:
    backend = LoopBackend(adjudicator=[adjudicator_answer(NEGATIVE, 0.2)])
    row, _ = run(tmp_path, backend, verifier="none", decider="adjudicator")
    assert not any(t == L.TASK_VERIFY for t, _ in backend.calls)
    assert row["chain"]["verdicts"] == [] and row["stages"]["valid"] is True and row["stages"]["rounds"] == 1
    assert row["answer"]["verdict"] == NEGATIVE and row["answer"]["probability"] == 0.2
    assert row["answer"]["weighted_score"] is None and row["stages"]["decider_agreement"] is None
    assert row["stages"]["verifier_agreement"] is None and row["answer"]["verifier_label"] is None
    assert row["published"] is True


# ---------------------------------------------------------------- 6. executor context


def test_independent_executors_see_only_their_dependencies_and_cumulative_ones_the_chain_so_far(tmp_path: Path, weights_stub) -> None:
    backend = LoopBackend()
    run(tmp_path, backend)
    s02 = backend.executor_requests("s02")[0].user_prompt
    s09 = backend.executor_requests("s09")[0].user_prompt
    assert "Prior nodes" not in s02
    assert "n01 | s01" in s09 and "n03 | s03" in s09 and "n02 | s02" not in s09

    # cumulative: s02 answers about the wrong criterion three times, so its record is unknown and unpublished
    backend = LoopBackend(executor={"s02": [bad_node()] * 3})
    row, _ = run(tmp_path / "cumulative", backend, executor_context="cumulative")
    order = [seg for _, seg in backend.calls if seg is not None]
    assert order == ["s01", "s02", "s02", "s02", *sorted(SEGMENTS)[2:]], "one at a time, in plan order"
    s01 = backend.executor_requests("s01")[0].user_prompt
    s08 = backend.executor_requests("s08")[0].user_prompt
    assert "Prior nodes" not in s01
    assert "Prior nodes" in s08 and "n01 | s01" in s08 and "n06 | s06" in s08
    assert "n07 | s07 criterion lake_water_uranium | unknown" in s08, "a published unknown is a node like any other"
    assert "n02 | s02" not in s08, "the gate's unpublished record is not carried as context"
    n02 = next(n for n in W.Chain.from_dict(row["chain"]).nodes if n.segment_id == "s02")
    assert not n02.published and n02.text.startswith("gate: criterion: the model answered about 'conductor_proximity'")
    assert row["stages"]["n_recorded_unknown"] == 1 and row["stages"]["n_gate_rejections"] == 3


# ---------------------------------------------------------------- 7. the budget


def test_a_budget_stop_propagates_and_leaves_the_partial_chain_on_disk(tmp_path: Path, weights_stub) -> None:
    backend = LoopBackend(raise_on_executor_call=4, error=BudgetExhausted("the run's budget is spent"))
    with pytest.raises(BudgetExhausted, match="budget"):
        run(tmp_path, backend, segment_workers=1)
    assert backend.n_executor == 4 and not any(t == L.TASK_VERIFY for t, _ in backend.calls)
    partial = W.Chain.from_dict(chains_file(tmp_path)).validate()
    assert [n.node_id for n in partial.nodes] == ["n01", "n02", "n03"] and len(partial.plan.segments) == 10
    assert partial.decision is None and partial.published is False and partial.verdicts == []


# ---------------------------------------------------------------- 8. spans


def test_every_stage_run_is_a_span_and_no_attribute_carries_the_cell(tmp_path: Path, weights_stub) -> None:
    reason = "rests on one trace"
    backend = LoopBackend(executor={"s02": [bad_node(), node_answer("graphitic_host")]},
                          verifier=[verifier_answer(False, [{"node_id": "n01", "reason": reason}]), verifier_answer(True)])
    with trace("R1", "bench", run_dir=tmp_path / "R1"):
        row, _ = run(tmp_path, backend, rounds=2)
    spans = read_spans(tmp_path / "R1")
    stages = [sp for sp in spans if sp["name"].startswith("stage:")]
    counts = {name: sum(1 for sp in stages if sp["name"] == name) for name in {sp["name"] for sp in stages}}
    assert counts == {"stage:plan": 1, "stage:execute": 2, "stage:gate": row["stages"]["attempts_total"],
                      "stage:verify": 2, "stage:decide": 1, "stage:publish": 1}
    assert row["stages"]["attempts_total"] == 13 and all(sp["status"] == "ok" for sp in stages)
    by_name = {sp["name"]: sp for sp in stages}
    assert by_name["stage:decide"]["attrs"]["verdict"] == POSITIVE and by_name["stage:publish"]["attrs"]["published"] is True
    execute = [sp for sp in stages if sp["name"] == "stage:execute"]
    assert execute[0]["attrs"]["n_segments"] == 10 and execute[0]["attrs"]["n_gate_rejections"] == 1
    assert execute[1]["attrs"]["round"] == 1 and execute[1]["attrs"]["n_segments"] == 2
    gates = [sp for sp in stages if sp["name"] == "stage:gate"]
    parents = {sp["span_id"] for sp in execute}
    assert all(sp["parent_id"] in parents and sp["kind"] == "gate" for sp in gates), "the gate nests in the execute span, pool threads included"
    assert sum(1 for sp in gates if not sp["attrs"]["published"]) == 1
    tools = [sp for sp in spans if sp["name"].startswith("tool:")]
    assert tools and all(sp["parent_id"] in {s["span_id"] for s in stages} for sp in tools), "tool calls nest in a stage"
    assert CELL not in json.dumps([sp["attrs"] for sp in spans]) and CELL not in json.dumps([sp["name"] for sp in spans])


# ---------------------------------------------------------------- 9. triage


def test_triage_takes_the_shallow_path_when_the_scores_agree_and_coverage_is_full(tmp_path: Path, weights_stub) -> None:
    world = LoopWorld(unmeasured=set())
    backend = LoopBackend()
    row, session = run(tmp_path, backend, world=world, triage=True)
    assert [t for t, _ in backend.calls] == [L.TASK_ADJUDICATE]
    assert row["stages"]["shallow"] is True and row["stages"]["n_segments"] == 0 and row["stages"]["n_nodes"] == 0
    assert row["chain"]["plan"]["segments"] == [] and row["chain"]["nodes"] == [] and row["chain"]["verdicts"] == []
    d = W.Chain.from_dict(row["chain"]).validate().decision
    assert d is not None and d.abstained_reason is None and d.final_verdict == POSITIVE and d.weighted_score is None
    assert row["answer"]["probability"] == 0.8, "with no chain there is no weighted score; the adjudicator's stands"
    req = backend.requests[0]
    staged = [name for _p, name in req.stage_files]
    assert staged == [c["file"] for c in session.calls] and staged[0] == "tool_01_cell_scores.json" and len(staged) == 3
    assert "There is no chain" in req.user_prompt and f"[c:score:{CELL}:effort]" in req.user_prompt
    assert row["published"] is True and row["stages"]["valid"] is True

    world = LoopWorld(scores={"learned": 0.7, "effort": 0.3, "criteria": 0.65}, unmeasured=set())
    backend = LoopBackend()
    row, _ = run(tmp_path / "full", backend, world=world, triage=True)
    assert row["stages"]["shallow"] is False and sum(1 for t, _ in backend.calls if t == L.TASK_EXECUTE) == 10

    world = LoopWorld()  # scores agree, but lake water is unmeasured here
    backend = LoopBackend()
    row, _ = run(tmp_path / "gap", backend, world=world, triage=True)
    assert row["stages"]["shallow"] is False and backend.n_executor == 10


def test_triage_falls_back_to_the_full_path_when_the_scores_are_switched_off(tmp_path: Path, weights_stub) -> None:
    session = Session.open(CELL, "dashboard", fold=None, switches=Switches(True, True, False, True, True),
                           stage=tmp_path / "cell" / "stage", tools=loop_registry(LoopWorld(unmeasured=set())))
    backend = LoopBackend()
    row = L.run_cell_v1(backend, session, None, INPUTS, session.switches, config(triage=True), CS, chain_id="ch-1",
                        run_id="run-1", stage=session.stage)
    assert row["stages"]["shallow"] is False and backend.n_executor == 10
    assert row["stages"]["n_refusals_by_rule"]["switch"] == 2, "triage's scores, then the verifier's effort null"
    verify = next(r for r, (t, _) in zip(backend.requests, backend.calls) if t == L.TASK_VERIFY)
    assert L.WITHHELD in verify.user_prompt


# ---------------------------------------------------------------- the planner and the adjudicator's gate


def test_a_model_plan_that_fails_its_check_falls_back_to_the_template_and_says_why(tmp_path: Path, weights_stub) -> None:
    folklore = {"segment_id": "s01", "kind": "criterion", "criterion": "em_bright_spot", "purpose": "Establish the bright spot.",
                "tool_calls": [{"tool": "cell_features", "args": {"cell_id": "$cell"}}], "depends_on": []}
    backend = LoopBackend(planner={"segments": [folklore]})
    row, _ = run(tmp_path, backend, planner="model")
    assert backend.calls[0][0] == L.TASK_PLAN and backend.requests[0].model == "m-large"
    assert "Coverage of each feature" in backend.requests[0].user_prompt and "water_u_max_ppm: unmeasured here" not in backend.requests[0].user_prompt
    assert row["stages"]["planner_fallback"] == ["s01: em_bright_spot is folklore; it may be named by the verifier, never executed"]
    assert row["chain"]["plan"]["planner"] == "template" and row["stages"]["n_segments"] == 10 and row["published"] is True

    short = {"segment_id": "s01", "kind": "criterion", "criterion": "conductor_proximity", "purpose": "Establish the conductor.",
             "tool_calls": [{"tool": "cell_features", "args": {"cell_id": "$cell"}}, {"tool": "criteria_breakdown", "args": {"cell_id": "$cell"}}],
             "depends_on": []}
    backend = LoopBackend(planner={"segments": [short]})
    row, _ = run(tmp_path / "ok", backend, planner="model")
    assert "planner_fallback" not in row["stages"] and row["chain"]["plan"]["planner"] == "model"
    assert row["stages"]["n_segments"] == 1 and backend.n_executor == 1 and row["published"] is True


def test_a_rejected_adjudicator_answer_abstains_with_the_first_problem_as_the_reason(tmp_path: Path, weights_stub) -> None:
    row, _ = run(tmp_path, LoopBackend(adjudicator=[bad_adjudicator()] * 3))   # every attempt refused
    assert row["published"] is False and row["problems"] and "2.4" in row["problems"][0]
    d = W.Chain.from_dict(row["chain"]).validate().decision
    assert d is not None and d.final_verdict == ABSTAIN and d.abstained_reason == f"adjudicator rejected: {row['problems'][0]}"
    assert d.adjudicator is not None and d.adjudicator["verdict"] == POSITIVE, "the answer is kept beside its problems"
    assert row["answer"]["verdict"] == ABSTAIN and row["answer"]["adjudicator_probability"] == 0.8


def test_the_weighted_only_arm_thresholds_the_score_and_needs_no_adjudicator(tmp_path: Path, weights_stub) -> None:
    backend = LoopBackend()
    row, _ = run(tmp_path, backend, decider="weighted")
    assert not any(t == L.TASK_ADJUDICATE for t, _ in backend.calls)
    assert row["answer"]["verdict"] == POSITIVE and row["answer"]["probability"] == 0.7 and row["answer"]["claims"] == []
    assert row["model_resolved"] is None and row["cache_key"] is None and row["published"] is True
    assert row["chain"]["decision"]["adjudicator"] is None and row["stages"]["decider_agreement"] is True
    weights_stub.score = lambda nodes, weights: None
    row, _ = run(tmp_path / "none", LoopBackend(), decider="weighted")
    assert row["answer"]["verdict"] == ABSTAIN and row["answer"]["probability"] == 0.5 and row["published"] is False


def test_loop_config_refuses_a_switch_outside_its_arms() -> None:
    with pytest.raises(ValueError, match="^decider"):
        config(decider="coin")
    with pytest.raises(ValueError, match="^rounds"):
        config(rounds=0)
    with pytest.raises(ValueError, match="^skip_unmeasured"):
        config(skip_unmeasured="yes")
    with pytest.raises(ValueError, match="^executor_batch"):
        config(executor_batch=1)
    assert config().models() == {"executor": "m-small", "verifier": "m-large", "adjudicator": "m-large"}
    assert (config().skip_unmeasured, config().executor_batch) == (False, False), "both cost switches are arms, off by default"


# ---------------------------------------------------------------- the two cost switches


def test_skip_unmeasured_settles_an_unmeasured_criterion_without_an_executor_call(tmp_path: Path, weights_stub) -> None:
    world = LoopWorld(nearest={"water_u_max_ppm": 6200.0})
    backend = LoopBackend()
    row, session = run(tmp_path, backend, world=world, skip_unmeasured=True)
    assert backend.executor_requests("s07") == [] and backend.n_executor == 9
    assert {"feature_key": "water_u_max_ppm"} in world.received["coverage"], "the skipped segment's tools are still staged"
    chain = W.Chain.from_dict(row["chain"]).validate()
    n07 = next(n for n in chain.nodes if n.segment_id == "s07")
    near = f"{vid('water_u_max_ppm')}:nearest_m"
    assert (n07.node_id, n07.status, n07.strength, n07.published, n07.model) == ("n07", "unknown", 0, True, "deterministic")
    assert (n07.round, n07.attempt, n07.cost_usd, n07.duration_s, n07.depends_on, n07.problems) == (0, 1, 0.0, 0.0, [], [])
    assert n07.value_ids == [near], "the nearest observation is cited by id, never as a bare distance"
    assert n07.text == f"unmeasured here: water_u_max_ppm has no value at this cell; nearest observation {near}"
    assert n07.unknown_reason == "no value for water_u_max_ppm at this cell"
    assert G.check_node(n07, session.values, session.context, CS, {CELL}, {"water_u_max_ppm"}) == []
    st = row["stages"]
    assert st["n_skipped_unmeasured"] == 1 and st["attempts_total"] == 9 and st["n_nodes"] == 10
    assert st["n_gate_rejections"] == 0 and st["n_recorded_unknown"] == 0 and st["n_batch_calls"] == 0
    assert row["published"] is True and chain.published and row["cost_usd"] == pytest.approx(11 * 0.05)
    verify = next(r for r, (t, _) in zip(backend.requests, backend.calls) if t == L.TASK_VERIFY)
    assert n07.line() in verify.user_prompt, "the verifier reads the harness's node like any other"

    # nothing to cite when the grid knows no nearest observation; the text says so without a number
    backend = LoopBackend()
    row, _ = run(tmp_path / "bare", backend, skip_unmeasured=True)
    n07 = next(n for n in W.Chain.from_dict(row["chain"]).validate().nodes if n.segment_id == "s07")
    assert n07.value_ids == [] and n07.text == "unmeasured here: water_u_max_ppm has no value at this cell" and n07.published
    assert backend.n_executor == 9 and row["stages"]["n_skipped_unmeasured"] == 1

    # a cross-check is never settled this way, even when the criterion it conditions was
    backend = LoopBackend()
    row, _ = run(tmp_path / "cross", backend, world=LoopWorld(unmeasured={"water_u_max_ppm", "sed_u_max_ppm"}),
                 skip_unmeasured=True)
    assert backend.executor_requests("s06") == [] and len(backend.executor_requests("s10")) >= 1
    assert row["stages"]["n_skipped_unmeasured"] == 2 and backend.n_executor >= 8
    assert "n06 | s06 criterion lake_sediment_uranium | unknown" in backend.executor_requests("s10")[0].user_prompt

    # a fully measured cell skips nothing
    backend = LoopBackend()
    row, _ = run(tmp_path / "full", backend, world=LoopWorld(unmeasured=set()), skip_unmeasured=True)
    assert backend.n_executor == 10 and row["stages"]["n_skipped_unmeasured"] == 0


def test_executor_batch_answers_every_criterion_in_one_call_and_retries_only_what_the_gate_refuses(tmp_path: Path, weights_stub) -> None:
    backend = LoopBackend()
    with trace("R1", "bench", run_dir=tmp_path / "R1"):
        row, session = run(tmp_path, backend, executor_batch=True)
    tasks = [t for t, _ in backend.calls]
    assert tasks.count(L.TASK_EXECUTE_BATCH) == 1 and tasks.count(L.TASK_EXECUTE) == 2
    assert tasks.count(L.TASK_VERIFY) == 1 and tasks.count(L.TASK_ADJUDICATE) == 1
    assert sorted(seg for t, seg in backend.calls if t == L.TASK_EXECUTE) == ["s09", "s10"], "the cross-checks run per segment"
    criteria = [sid for sid in sorted(SEGMENTS) if sid not in ("s09", "s10")]
    batch = backend.requests[tasks.index(L.TASK_EXECUTE_BATCH)]
    assert batch.schema == W.NODES_SCHEMA and batch.model == "m-small" and batch.effort == "low"
    assert all(f"Segment {sid}: criterion {SEGMENTS[sid]}." in batch.user_prompt for sid in criteria)
    assert "Segment s09" not in batch.user_prompt and "Prior nodes" not in batch.user_prompt
    names = [name for _path, name in batch.stage_files]
    assert names and len(names) == len(set(names)) and names == [c["file"] for c in session.calls[:len(names)]]
    assert not any("crosscheck" in n for n in names) and all(f"{{STAGE_DIR}}/{n}" in batch.user_prompt for n in names)
    st = row["stages"]
    assert st["n_batch_calls"] == 1 and st["n_batch_extra"] == 0 and st["attempts_total"] == 10
    assert st["n_gate_rejections"] == 0 and st["n_nodes"] == 10 and st["n_skipped_unmeasured"] == 0
    chain = W.Chain.from_dict(row["chain"]).validate()
    by_seg = {n.segment_id: n for n in chain.nodes}
    for sid in criteria:
        n = by_seg[sid]
        assert (n.published, n.attempt, n.round, n.model, n.cost_usd) == (True, 1, 0, "m-small-resolved", 0.0), sid
    assert by_seg["s09"].cost_usd == 0.05 and by_seg["s09"].depends_on == ["n01", "n03"]
    assert "n01 | s01" in backend.executor_requests("s09")[0].user_prompt
    assert row["published"] is True and chain.published
    assert row["cost_usd"] == pytest.approx(5 * 0.05), "one batch call, two cross-checks, the verifier, the adjudicator"
    gates = [sp for sp in read_spans(tmp_path / "R1") if sp["name"] == "stage:gate"]
    assert len(gates) == st["attempts_total"] and sum(1 for sp in gates if sp["attrs"].get("batch")) == 8

    # one batch node the gate refuses, one the reply leaves out, one it was never asked for
    extra = {**node_answer("boulder_train"), "criterion": "em_bright_spot"}
    backend = LoopBackend(batch={"conductor_proximity": bad_node(), "graphitic_host": None, "em_bright_spot": extra})
    row, _ = run(tmp_path / "retry", backend, executor_batch=True)
    assert [t for t, _ in backend.calls].count(L.TASK_EXECUTE_BATCH) == 1
    assert sorted(seg for t, seg in backend.calls if t == L.TASK_EXECUTE) == ["s01", "s02", "s09", "s10"]
    s01, s02 = backend.executor_requests("s01"), backend.executor_requests("s02")
    assert len(s01) == 1 and len(s02) == 1, "only the refused criteria cost another call, one each"
    assert s01[0].task == L.TASK_EXECUTE and s01[0].schema == W.NODE_SCHEMA
    assert "rejected by the mechanical gate" in s01[0].user_prompt and "the number 2.4 is not backed" in s01[0].user_prompt
    assert COND in s01[0].user_prompt, "attempt 2: the reason and the ids the node may cite"
    assert "batch: the reply has no node for graphitic_host" in s02[0].user_prompt
    st = row["stages"]
    assert st["n_batch_calls"] == 1 and st["n_batch_extra"] == 1 and st["n_gate_rejections"] == 2 and st["attempts_total"] == 12
    chain = W.Chain.from_dict(row["chain"]).validate()
    by_seg = {n.segment_id: n for n in chain.nodes}
    assert (by_seg["s01"].attempt, by_seg["s01"].published, by_seg["s01"].cost_usd) == (2, True, 0.05)
    assert (by_seg["s02"].attempt, by_seg["s02"].published) == (2, True)
    assert len(chain.nodes) == 10 and row["published"] is True and row["cost_usd"] == pytest.approx(7 * 0.05)

    # a batch node refused on every retry is recorded unknown, as a per-segment node is
    backend = LoopBackend(batch={"conductor_proximity": bad_node()}, executor={"s01": [bad_node(), bad_node()]})
    row, _ = run(tmp_path / "exhausted", backend, executor_batch=True)
    assert len(backend.executor_requests("s01")) == 2 and "last attempt" in backend.executor_requests("s01")[1].user_prompt
    n01 = next(n for n in W.Chain.from_dict(row["chain"]).validate().nodes if n.segment_id == "s01")
    assert (n01.status, n01.published, n01.attempt) == ("unknown", False, 3) and n01.text.startswith("gate:")
    assert row["stages"]["n_recorded_unknown"] == 1 and row["stages"]["n_gate_rejections"] == 3 and row["published"] is False


def test_the_batch_leaves_out_the_criteria_skip_unmeasured_settled(tmp_path: Path, weights_stub) -> None:
    backend = LoopBackend()
    row, _ = run(tmp_path, backend, executor_batch=True, skip_unmeasured=True)
    batch = next(r for r in backend.requests if r.task == L.TASK_EXECUTE_BATCH)
    assert "lake_water_uranium" not in batch.user_prompt and "Segment s07" not in batch.user_prompt
    assert sum(1 for line in batch.user_prompt.splitlines() if line.startswith("Segment ")) == 7
    st = row["stages"]
    assert st["n_skipped_unmeasured"] == 1 and st["n_batch_calls"] == 1 and st["attempts_total"] == 9
    assert backend.n_executor == 2 and row["published"] is True and row["cost_usd"] == pytest.approx(5 * 0.05)


def test_the_adjudicator_gets_the_gates_feedback_and_a_second_attempt(tmp_path: Path, weights_stub) -> None:
    """Opus never needed it; a cheap adjudicator cited a feature name where the id belongs and lost the chain
    on one strike. The retry carries the gate's reasons and, on the second attempt, the ids it may cite."""
    backend = LoopBackend(adjudicator=[bad_adjudicator(), adjudicator_answer()])
    row, _ = run(tmp_path, backend)
    prompts = [r.user_prompt for r in backend.requests if r.task == L.TASK_ADJUDICATE]
    assert len(prompts) == 2 and "rejected by the mechanical gate" in prompts[1] and COND in prompts[1]
    assert row["published"] is True and row["problems"] == [] and row["stages"]["adjudicator_attempts"] == 2
    backend = LoopBackend(adjudicator=[bad_adjudicator(), bad_adjudicator(), bad_adjudicator()])
    row, _ = run(tmp_path / "again", backend)
    assert row["published"] is False and row["stages"]["adjudicator_attempts"] == 3 and row["problems"]
