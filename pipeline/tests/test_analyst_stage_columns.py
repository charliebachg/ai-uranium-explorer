"""The staged loop's per-stage columns on the bench table: derived from a run's own cells, null where an arm
has no stage, exported as values under `:stage:<key>`, and checked by the contract. Fixtures only, no store."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from uranium_explorer.analyst import score as SC
from uranium_explorer.contract_check import Errors, check_bench, check_registry, check_readiness
from uranium_explorer.prospect import export as X
from uranium_explorer.values import registry

from test_analyst_score import cell_row, write_run
from test_prospect_export_phases import ARM, BASELINE, _bench_table, _readiness_with


def stages(rounds, valid, **more):
    base = {"n_segments": 3, "n_nodes": 3, "attempts_total": 3, "n_gate_rejections": 0, "n_recorded_unknown": 0,
            "rounds": rounds, "valid": valid, "n_reexecuted": 0, "verifier_agreement": True,
            "decider_agreement": True, "shallow": False, "n_refusals_by_rule": {"B17": 0, "switch": 1}}
    return base | more


def staged_row(bench_id, rounds, valid, **more):
    return cell_row(bench_id, published=valid) | {"stages": stages(rounds, valid, **more)}


# ---------------------------------------------------------------- the two derived stage metrics

def test_the_verifier_caught_a_chain_its_first_round_did_not_validate() -> None:
    rows = {
        "a": staged_row("a", 1, True),                       # passed first time: not caught
        "b": staged_row("b", 3, True, n_reexecuted=4),       # caught, repaired by round 3
        "c": staged_row("c", 3, False, n_reexecuted=2),      # caught, never validated
        "d": staged_row("d", 1, False),                      # caught once, nothing to repair
        "e": staged_row("e", 0, False),                      # no verifier round at all: neither caught nor passed
    }
    m = SC.stage_metrics(rows)
    assert m["stage_n_chains"] == 5.0
    assert m["stage_verifier_catch_rate"] == pytest.approx(3 / 4)
    assert m["stage_rounds_to_valid_mean"] == pytest.approx(2.0), "over the chains that validated: 1 and 3"
    assert m["stage_rounds_mean"] == pytest.approx(8 / 5)
    assert m["stage_valid_rate"] == pytest.approx(2 / 5)
    assert m["stage_reexecuted_mean"] == pytest.approx(6 / 5)
    # nothing validated, nothing with a round: the rates are NaN, not zero
    m = SC.stage_metrics({"e": staged_row("e", 0, False)})
    assert math.isnan(m["stage_verifier_catch_rate"]) and math.isnan(m["stage_rounds_to_valid_mean"])
    assert SC.stage_metrics({"x": cell_row("x")}) == {}, "a v0 run has no stages"


# ---------------------------------------------------------------- the table rows

def test_arm_row_derives_the_stage_columns_from_the_run_and_keeps_the_score(tmp_path: Path) -> None:
    rd = tmp_path / "run"
    write_run(rd, [staged_row("a", 1, True), staged_row("b", 2, True, n_gate_rejections=1, attempts_total=4)])
    # a summary scored before the stage columns existed: the label metrics are kept, the stages come from the cells
    summary = {"arm": "v1", "model": "claude-opus-5", "run_id": "r1", "mlflow_run_id": "m1", "pending": [],
               "run_dir": str(rd), "score": {"n": 2, "f1": 0.5, "cost_usd_total": 1.0, "stage_n_chains": 99.0}}
    row = SC.arm_row(summary)
    assert row["f1"] == 0.5 and row["n_cells"] == 2 and row["cost_usd"] == 1.0
    assert row["stage_n_chains"] == 2.0, "the run's cells win over a stale summary"
    assert row["stage_valid_rate"] == 1.0 and row["stage_verifier_catch_rate"] == 0.5
    assert row["stage_rounds_to_valid_mean"] == 1.5 and row["stage_gate_rejection_rate"] == pytest.approx(1 / 7)
    assert set(SC.STAGE_COLUMNS) <= set(row) and all(isinstance(row[k], float) for k in SC.STAGE_COLUMNS)


def test_a_v0_arm_and_a_baseline_hold_null_under_every_stage(tmp_path: Path) -> None:
    rd = tmp_path / "v0"
    write_run(rd, [cell_row("a"), cell_row("b", verdict="evidence_against", p=0.2)])
    row = SC.arm_row({"arm": "v0", "model": "claude-opus-5", "run_id": "r0", "pending": [], "run_dir": str(rd),
                      "score": {"n": 2, "f1": 1.0}})
    assert all(row[k] is None for k in SC.STAGE_COLUMNS)
    # no run directory at all: the summary's own stage keys are used, the rest null
    row = SC.arm_row({"arm": "v1", "model": "m", "run_id": "r", "pending": [],
                      "score": {"n": 1, "stage_valid_rate": 0.75, "stage_rounds_to_valid_mean": float("nan")}})
    assert row["stage_valid_rate"] == 0.75 and row["stage_rounds_to_valid_mean"] is None and row["stage_n_chains"] is None
    assert all(SC.random_expected(0.5, 4)[k] is None for k in SC.STAGE_COLUMNS)
    assert all(k in SC.TABLE_METRICS for k in SC.STAGE_COLUMNS), "the store's metric rows carry the stages too"


# ---------------------------------------------------------------- the export and the contract

STAGED = {**ARM, "name": "v1-openrouter", "stage_n_chains": 130.0, "stage_gate_rejection_rate": 0.0907692,
          "stage_valid_rate": 0.4846153, "stage_verifier_catch_rate": 0.7307692, "stage_rounds_to_valid_mean": 1.6031746,
          "stage_reexecuted_mean": 3.6769230, "stage_verifier_agreement_rate": 0.6692307,
          "stage_decider_agreement_rate": 0.6153846, "stage_rounds_mean": 2.2153846, "stage_shallow_rate": 0.0}
SINGLE = {**ARM, "name": "v0", **dict.fromkeys(SC.STAGE_COLUMNS)}


def test_the_export_carries_the_stage_columns_for_a_staged_arm_only(tmp_path: Path) -> None:
    _bench_table(tmp_path, "v1", [STAGED, SINGLE, BASELINE])
    vals: list[dict] = []
    b = X._bench_block(vals, tmp_path)
    assert b is not None
    staged, single, base = b["rows"]
    reg = registry(*vals)
    pre = "c:bench:v1:v1-openrouter:stage"
    assert staged["stages"] == {key: f"{pre}:{key}" for key in X.BENCH_STAGES}
    assert reg[f"{pre}:n_chains"]["value"] == 130 and reg[f"{pre}:n_chains"]["fmt"] == "int"
    assert reg[f"{pre}:valid_rate"]["value"] == 0.4846 and reg[f"{pre}:valid_rate"]["fmt"] == "ratio3"
    assert reg[f"{pre}:rounds_to_valid_mean"]["value"] == 1.6032 and reg[f"{pre}:rounds_to_valid_mean"]["fmt"] == "m2"
    assert "over 130 chains" in reg[f"{pre}:valid_rate"]["note"] and "verifier round validated" in reg[f"{pre}:valid_rate"]["note"]
    assert "over 130 chains" not in reg[f"{pre}:n_chains"]["note"]
    # what is the same in every arm so far is not a column
    assert not any(v["id"].endswith(":stage:shallow_rate") or v["id"].endswith(":stage:rounds_mean") for v in vals)
    # a single-call arm and a baseline carry no stages at all, and the headline columns are as before
    assert "stages" not in single and "stages" not in base
    assert single["extra"]["gate_rejection_rate"] == "c:bench:v1:v0:gate_rejection_rate"
    assert set(staged["metrics"]) == set(single["metrics"])

    # the contract: every stage id resolves, and an unbacked one is refused where it sits
    e = Errors()
    known = check_registry(e, "values", reg)
    check_bench(e, "bench", b, known)
    assert list(e) == []
    assert [err for err in check_readiness(_readiness_with(b, vals)) if ".bench" in err] == []
    b["rows"][0]["stages"]["valid_rate"] = f"{pre}:nowhere"
    e = Errors()
    check_bench(e, "bench", b, known)
    assert any("rows[0].stages.valid_rate" in err and "unbacked" in err for err in e)


def test_a_stage_the_run_never_had_is_absent_from_the_export(tmp_path: Path) -> None:
    # an arm without a verifier: no rounds, so the verifier's columns are null in the table and absent on the web
    row = {**STAGED, "stage_verifier_catch_rate": None, "stage_rounds_to_valid_mean": None,
           "stage_verifier_agreement_rate": None}
    _bench_table(tmp_path, "v1", [row])
    vals: list[dict] = []
    b = X._bench_block(vals, tmp_path)
    assert b is not None
    assert set(b["rows"][0]["stages"]) == {"n_chains", "gate_rejection_rate", "valid_rate", "reexecuted_mean",
                                           "decider_agreement_rate"}
    assert json.dumps(b["rows"][0]["stages"])  # plain ids, serialisable as written


def test_a_derived_row_needs_no_run_and_a_comparison_must_resolve(tmp_path: Path) -> None:
    vote = {**BASELINE, "name": "d1-vote5", "kind": "derived", "model": "claude-opus-5", "pr_auc_rank": 0.61}
    (tmp_path / "v2").mkdir(parents=True)
    (tmp_path / "v2" / "table.json").write_text(json.dumps({
        "version": "v2", "computed_at": "2026-09-23T10:00:00+00:00", "manifest_sha256": "c" * 64, "rows": [vote],
        "contrasts": [{"first": "d1-vote5", "second": "extended", "question": "q", "diff": 0.09,
                       "diff_ci": [-0.02, 0.2], "cells": 40, "mcnemar": {"b": 21, "c": 12, "p": 0.16}}]}))
    vals: list[dict] = []
    b = X._bench_block(vals, tmp_path)
    e = Errors()
    known = check_registry(e, "values", registry(*vals))
    check_bench(e, "bench", b, known)
    assert list(e) == []
    b["contrasts"][0]["diff"] = "c:bench:v2:contrast:nowhere"
    e = Errors()
    check_bench(e, "bench", b, known)
    assert any("contrasts[0].diff" in err for err in e), "a comparison's number must resolve like any other"
