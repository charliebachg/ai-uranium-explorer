"""The Eval page's phase blocks: built from the run JSONs, every number a value, every row naming its run."""

from __future__ import annotations

import json
from pathlib import Path

from uranium_explorer.contract_check import Errors, _ref, check_registry
from uranium_explorer.prospect import export as X
from uranium_explorer.values import registry


def _write(d: Path, name: str, payload: dict) -> None:
    (d / name).write_text(json.dumps(payload))


def test_no_run_json_means_no_block(tmp_path: Path) -> None:
    vals: list[dict] = []
    assert X._phase_blocks(vals, tmp_path) == {}
    assert vals == []


def test_headline_rows_carry_intervals_the_run_id_and_the_verdict(tmp_path: Path) -> None:
    _write(tmp_path, "headline.json", {
        "run_id": "2026-09-19T11:56:38+00:00", "mlflow_run_id": "abc123", "store_sha256": "f" * 64, "snapshot": "f" * 12,
        "cells": 10183, "verdict": {"text": "effort ahead", "by_positives": {"all": "effort ahead"}},
        "rows": [
            {"config": "learned.all.matched+thinned.spatial", "feature_set": "learned", "positives": "all", "matched": True,
             "thinned": True, "fold": "spatial", "pr_auc": 0.111, "pr_auc_ci": [0.10, 0.13], "roc_auc": 0.6,
             "capture_top10": 0.2, "base_rate": 0.05},
            {"config": "effort.all.naive.camp", "feature_set": "effort", "positives": "all", "matched": False,
             "thinned": False, "fold": "camp", "note": "not measurable"},
        ],
        "minetrace": {"learned": {"roc_auc_mean": 0.661, "roc_auc_sd": 0.053, "protocol": "30/200"}},
    })
    vals: list[dict] = []
    b = X._phase_blocks(vals, tmp_path)["headline"]
    assert b["verdict"] == "effort ahead" and b["mlflow_run_id"] == "abc123" and b["snapshot"] == "f" * 12
    metrics = {r["metric"]: r for r in b["rows"]}
    assert set(metrics) == {"pr_auc", "roc_auc", "capture_top10", "base_rate"}   # the row without numbers is dropped
    pr = metrics["pr_auc"]
    assert pr["run_id"] == "abc123" and pr["matched"] and pr["thinned"] and pr["fold"] == "spatial"
    reg = registry(*vals)
    assert reg[pr["value_id"]]["value"] == 0.111
    assert [reg[v]["value"] for v in pr["ci"]] == [0.10, 0.13]
    assert b["minetrace"][0]["value_id"] in reg and reg["c:h:cells"]["value"] == 10183


def test_search_rows_keep_each_arm_s_own_run_id_and_the_decision(tmp_path: Path) -> None:
    _write(tmp_path, "modelsearch.json", {
        "run_id": "2026-09-19T12:00:00+00:00", "store_sha256": None, "snapshot": None, "quick": False, "cells": 10183,
        "rows": [
            {"arm": "random_forest.all.spatial", "name": "random_forest", "feature_set": "learned", "fold": "spatial",
             "positives": "all", "run_id": "d1f910f4", "pr_auc": 0.125, "pr_auc_ci": [0.112, 0.144], "roc_auc": 0.62,
             "capture_top10": 0.21, "base_rate": 0.05},
            {"arm": "effort.all.spatial", "name": "effort", "feature_set": "effort", "fold": "spatial",
             "positives": "all", "run_id": "3264dc93", "pr_auc": 0.178, "pr_auc_ci": [0.162, 0.199], "roc_auc": 0.7,
             "capture_top10": 0.3, "base_rate": 0.05},
        ],
        "decision": {"model": "random_forest", "stage": "candidate", "served": False, "run_id": "d1f910f4",
                     "version": 4, "reason": "not validated; nothing is served"},
    })
    vals: list[dict] = []
    b = X._phase_blocks(vals, tmp_path)["search"]
    by_arm = {(r["arm"], r["metric"]): r for r in b["rows"]}
    assert by_arm[("random_forest.all.spatial", "pr_auc")]["run_id"] == "d1f910f4"
    assert by_arm[("effort.all.spatial", "pr_auc")]["run_id"] == "3264dc93"
    card = b["decision"].pop("card")
    assert b["decision"] == {"model": "random_forest", "stage": "candidate", "served": False,
                             "reason": "not validated; nothing is served", "run_id": "d1f910f4", "version": 4}
    # the model card comes from the registered run's own row
    assert card["name"] == "random_forest" and card["fold"] == "spatial" and card["matched"] and card["thinned"]
    assert card["features"] == [] and card["n_pos"] is None
    assert registry(*vals)["c:s:effort.all.spatial.pr_auc.hi"]["value"] == 0.199


def test_hindcast_rows_are_percent_shares_named_by_discovery(tmp_path: Path) -> None:
    _write(tmp_path, "hindcast.json", {
        "run_id": "f0d9d16d", "store_sha256": "a" * 64, "snapshot": "a" * 12,
        "rows": [{"cutoff": 2000, "discovery": "Shea Creek, Kianna", "year": 2004, "confidence": "high",
                  "cells": ["0031_0080"], "model": "learned", "area_share": 0.00058, "in_top5": True, "in_top10": True},
                 {"cutoff": 2000, "discovery": "Spitfire", "year": 2015, "confidence": "medium",
                  "cells": ["0145_0018"], "model": "effort", "area_share": 1.0}],
        "summary": {"learned.median_area_share": 0.118, "learned.n_in_top10": 4, "learned.n": 10},
    })
    vals: list[dict] = []
    b = X._phase_blocks(vals, tmp_path)["hindcast"]
    assert b["run_id"] == "f0d9d16d"
    kianna = b["rows"][0]
    assert kianna["discovery"] == "shea_creek_kianna" and kianna["title"] == "Shea Creek, Kianna"
    assert kianna["year"] == 2004 and kianna["cells"] == ["0031_0080"] and kianna["run_id"] == "f0d9d16d"
    reg = registry(*vals)
    assert reg[kianna["value_id"]]["fmt"] == "pct1" and reg[kianna["value_id"]]["value"] == 0.0006
    summary = {(s["model"], s["key"]): s for s in b["summary"]}
    assert reg[summary[("learned", "n_in_top10")]["value_id"]] ["value"] == 4
    assert reg[summary[("learned", "median_area_share")]["value_id"]]["fmt"] == "pct1"


def test_every_reference_in_the_blocks_resolves(tmp_path: Path) -> None:
    _write(tmp_path, "hindcast.json", {"run_id": "r", "rows": [
        {"cutoff": 2010, "discovery": "Hurricane", "year": 2018, "confidence": "high", "cells": [], "model": "criteria",
         "area_share": 0.03}], "summary": {"criteria.n": 1}})
    vals: list[dict] = []
    blocks = X._phase_blocks(vals, tmp_path)
    e = Errors()
    known = check_registry(e, "values", registry(*vals))
    for r in blocks["hindcast"]["rows"] + blocks["hindcast"]["summary"]:
        _ref(e, "row", r["value_id"], known)
    assert list(e) == []


def test_the_readiness_gate_block_carries_every_column_with_its_note(tmp_path: Path) -> None:
    _write(tmp_path, "gate.json", {
        "generated_at": "t", "store_sha256": "a" * 64, "snapshot": None, "green": False,
        "rows": [{"dataset": "faults_250k", "title": "Faults", "kind": "layer",
                  "present": {"ok": True, "note": "17,571 rows"}, "licensed": {"ok": True, "note": "SK; redistributable"},
                  "covers": {"ok": True, "note": "1 feature(s); max 100%"}, "servable": {"ok": True, "note": "tiles:faults"},
                  "versioned": {"ok": False, "note": "no snapshot names the store as it is now"}}],
        "failures": ["faults_250k: versioned: no snapshot names the store as it is now"],
    })
    b = X._gate_columns_block(tmp_path)
    assert b and b["green"] is False and b["rows"][0]["versioned"] == {"ok": False, "note": "no snapshot names the store as it is now"}
    assert b["failures"] == ["faults_250k: versioned: no snapshot names the store as it is now"]
    assert X._gate_columns_block(tmp_path / "nowhere") is None


def test_a_green_gate_with_a_failing_column_is_refused_by_the_contract() -> None:
    from uranium_explorer.contract_check import check_readiness

    doc = {"schema_version": "1.0.0", "grid": {}, "totals": {}, "caveats": ["x"], "features": [], "sources": [], "gaps": [],
           "values": {}, "readiness_gate": {"green": True, "rows": [
               {"dataset": "d", "title": "d", "kind": "layer", "present": {"ok": False, "note": "no store row"},
                "licensed": {"ok": True, "note": "ok"}, "covers": {"ok": True, "note": "ok"},
                "servable": {"ok": True, "note": "ok"}, "versioned": {"ok": True, "note": "ok"}}]}}
    assert any("green with a failing column" in err for err in check_readiness(doc))


# ---------------------------------------------------------------- the analyst benchmark table

ARM = {
    "name": "v0-retrieval", "kind": "arm", "model": "claude-opus-5", "n_cells": 40, "run_id": "20260920T061420Z-bench",
    "mlflow_run_id": "9c1e4b2a", "cost_usd": 12.5, "pending": 0, "n": 40, "n_pos": 20, "n_neg": 20, "base_rate": 0.5,
    "abstain_n": 8, "abstain_denominator": 40, "precision": 0.7, "recall": 0.6, "f1": 0.6462, "f1_ci": [0.51, 0.77],
    "abstain_rate": 0.2, "abstain_rate_ci": [0.1, 0.33], "pr_auc": 0.71, "pr_auc_ci": [0.6, 0.82], "roc_auc": 0.75,
    "pr_auc_all": 0.66, "roc_auc_all": 0.7, "ece": float("nan"), "ece_committed": 0.12,
    "gate_rejection_rate": 0.05, "gate_rejection_denominator": 40, "probe_abstain_rate": 0.8, "n_probe": 5,
    "n_rejected": 2, "n_failed": 0, "cost_usd_total": 12.5, "cost_usd_per_cell": 0.2778, "latency_s_per_cell": None,
    "from_cache_share": 0.0,
    "strata": {"deposit": {"n": 10, "accuracy": 0.8, "abstain_rate": 0.1},
               "occurrence": {"n": 10, "accuracy": 0.5, "abstain_rate": 0.3},
               "negative": {"n": 20, "accuracy": 0.7, "abstain_rate": 0.2}},
}
BASELINE = {
    "name": "random_expected", "kind": "baseline", "model": "random", "n_cells": 40, "run_id": None, "mlflow_run_id": None,
    "cost_usd": 0.0, "precision": 0.5, "recall": 0.5, "f1": 0.5, "pr_auc": 0.5, "roc_auc": 0.5, "pr_auc_all": 0.5,
    "roc_auc_all": 0.5, "ece": 0.25, "abstain_rate": 0.0,
    "note": "analytic expectation for a uniform random probability at a 0.5 threshold; no interval",
}


def _bench_table(root: Path, version: str, rows: list[dict]) -> None:
    (root / version).mkdir(parents=True)
    (root / version / "table.json").write_text(json.dumps(
        {"version": version, "computed_at": "2026-09-20T06:23:16+00:00", "manifest_sha256": "b" * 64, "rows": rows}))


def _readiness_with(bench: dict, vals: list[dict]) -> dict:
    return {"schema_version": "1.0.0", "grid": {}, "totals": {}, "caveats": ["x"], "features": [], "sources": [],
            "gaps": [], "values": registry(*vals), "bench": bench}


def test_no_bench_table_means_no_block(tmp_path: Path) -> None:
    vals: list[dict] = []
    assert X._bench_block(vals, tmp_path) is None
    assert X._bench_block(vals, tmp_path / "nowhere") is None
    assert vals == []


def test_bench_rows_carry_every_metric_as_a_value_and_name_their_run(tmp_path: Path) -> None:
    _bench_table(tmp_path, "v1", [ARM, BASELINE])
    vals: list[dict] = []
    b = X._bench_block(vals, tmp_path)
    assert b is not None
    assert b["version"] == "v1" and b["manifest_sha256"] == "b" * 64 and b["versions"] == []
    assert b["computed_at"] == "2026-09-20T06:23:16+00:00"
    arm, base = b["rows"]
    reg = registry(*vals)

    # the arm: counts, metrics, intervals, what only an arm reports, and the strata, each a value with an id
    assert arm["kind"] == "arm" and arm["model"] == "claude-opus-5" and arm["run_id"] == "20260920T061420Z-bench"
    assert arm["mlflow_run_id"] == "9c1e4b2a" and "note" not in arm
    assert arm["n"] == "c:bench:v1:v0-retrieval:n" and reg[arm["n"]] ["value"] == 40 and reg[arm["n"]]["fmt"] == "int"
    assert reg[arm["n_pos"]]["value"] == 20 and reg[arm["n_neg"]]["value"] == 20
    assert arm["metrics"]["f1"] == "c:bench:v1:v0-retrieval:f1"
    assert reg[arm["metrics"]["f1"]]["value"] == 0.6462 and reg[arm["metrics"]["f1"]]["fmt"] == "ratio3"
    assert set(arm["metrics"]) == {"f1", "precision", "recall", "pr_auc", "roc_auc", "pr_auc_all", "roc_auc_all",
                                   "abstain_rate"}   # ece is NaN in the table, so it is not a value
    assert arm["ci"] == {"f1": ["c:bench:v1:v0-retrieval:f1.lo", "c:bench:v1:v0-retrieval:f1.hi"],
                         "abstain_rate": ["c:bench:v1:v0-retrieval:abstain_rate.lo", "c:bench:v1:v0-retrieval:abstain_rate.hi"],
                         "pr_auc": ["c:bench:v1:v0-retrieval:pr_auc.lo", "c:bench:v1:v0-retrieval:pr_auc.hi"]}
    assert [reg[v]["value"] for v in arm["ci"]["f1"]] == [0.51, 0.77]
    # latency is null in the table, so the extras stop at the three that are numbers, each with its formatter
    assert arm["extra"] == {"gate_rejection_rate": "c:bench:v1:v0-retrieval:gate_rejection_rate",
                            "probe_abstain_rate": "c:bench:v1:v0-retrieval:probe_abstain_rate",
                            "cost_usd_per_cell": "c:bench:v1:v0-retrieval:cost_usd_per_cell"}
    assert reg[arm["extra"]["cost_usd_per_cell"]]["fmt"] == "m2" and reg[arm["extra"]["gate_rejection_rate"]]["fmt"] == "ratio3"
    assert arm["strata"]["deposit"] == {"n": "c:bench:v1:v0-retrieval:deposit:n",
                                        "accuracy": "c:bench:v1:v0-retrieval:deposit:accuracy",
                                        "abstain_rate": "c:bench:v1:v0-retrieval:deposit:abstain_rate"}
    assert reg["c:bench:v1:v0-retrieval:deposit:accuracy"]["value"] == 0.8

    # the baseline: no run, no interval, nothing an arm alone reports, and its note travels
    assert base["kind"] == "baseline" and base["run_id"] is None and base["n_pos"] is None
    assert base["note"].startswith("analytic expectation") and "ci" not in base and "extra" not in base
    assert base["metrics"]["ece"] == "c:bench:v1:random_expected:ece" and reg[base["metrics"]["ece"]]["value"] == 0.25

    # every id the block references resolves, and the contract agrees
    e = Errors()
    known = check_registry(e, "values", reg)
    for r in b["rows"]:
        for vid in [r["n"], r["n_pos"], r["n_neg"], *r["metrics"].values(), *r.get("extra", {}).values(),
                    *(bound for pair in r.get("ci", {}).values() for bound in pair),
                    *(vid for cell in r.get("strata", {}).values() for vid in cell.values())]:
            _ref(e, "row", vid, known, nullable=True)
    assert list(e) == []
    from uranium_explorer.contract_check import check_readiness

    assert [err for err in check_readiness(_readiness_with(b, vals)) if ".bench" in err] == []


def test_the_contract_refuses_an_arm_without_a_run_and_an_unbacked_number(tmp_path: Path) -> None:
    from uranium_explorer.contract_check import check_readiness

    _bench_table(tmp_path, "v1", [{**ARM, "run_id": None, "mlflow_run_id": None}, BASELINE])
    vals: list[dict] = []
    b = X._bench_block(vals, tmp_path)
    assert b is not None
    errors = check_readiness(_readiness_with(b, vals))
    assert any("rows[0]: no run id" in err for err in errors)
    assert not any("rows[1]" in err for err in errors)   # a baseline is arithmetic, not a run
    b["rows"][1]["metrics"]["f1"] = "c:bench:v1:random_expected:nowhere"
    assert any("rows[1].metrics.f1" in err and "unbacked" in err for err in check_readiness(_readiness_with(b, vals)))


def test_bench_carries_every_version_highest_first_and_drops_a_row_scored_on_no_cells(tmp_path: Path) -> None:
    _bench_table(tmp_path, "v2", [BASELINE])
    _bench_table(tmp_path, "v10", [
        {"name": "v0", "kind": "arm", "model": "claude-opus-5", "n_cells": 0, "run_id": "r", "mlflow_run_id": None,
         "cost_usd": 0.0, "pending": 40},   # still running: nothing to print yet
        BASELINE,
    ])
    vals: list[dict] = []
    b = X._bench_block(vals, tmp_path)
    assert b is not None
    assert b["version"] == "v10" and b["versions"] == ["v2"]
    assert [r["name"] for r in b["rows"]] == ["random_expected"], "the arm still running has nothing to print"
    assert [e["version"] for e in b["earlier"]] == ["v2"], "an earlier version's table rides along in full"
    assert [r["name"] for r in b["earlier"][0]["rows"]] == ["random_expected"]
    assert {v["id"].split(":")[2] for v in vals} == {"v10", "v2"}, "each version's numbers are minted under its own ids"


def test_a_derived_row_and_the_fixed_contrasts_are_printed_as_values(tmp_path: Path) -> None:
    vote = {**BASELINE, "name": "d1-vote5", "kind": "derived", "model": "claude-opus-5", "pr_auc_rank": 0.61,
            "pr_auc_rank_ci": [0.5, 0.72], "coverage": 1.0, "brier": 0.23, "cal_slope": 0.8}
    (tmp_path / "v2").mkdir(parents=True)
    (tmp_path / "v2" / "table.json").write_text(json.dumps({
        "version": "v2", "computed_at": "2026-09-23T10:00:00+00:00", "manifest_sha256": "c" * 64, "rows": [vote],
        "contrasts": [{"first": "d1-vote5", "second": "extended", "question": "the voted single shot against the fitted model",
                       "diff": 0.09, "diff_ci": [-0.02, 0.2], "p_not_better": 0.06, "cells": 40,
                       "mcnemar": {"b": 21, "c": 12, "p": 0.163}},
                      {"first": "d2", "second": "d1", "question": "never computed", "diff": None, "cells": 0}]}))
    vals: list[dict] = []
    b = X._bench_block(vals, tmp_path)
    ids = {v["id"]: v for v in vals}
    row = b["rows"][0]
    assert row["kind"] == "derived" and ids[row["metrics"]["pr_auc_rank"]]["value"] == 0.61
    assert {"coverage", "brier", "cal_slope"} <= set(row["metrics"]) and "pr_auc_rank" in row["ci"]
    (c,) = b["contrasts"]
    assert c["first"] == "d1-vote5" and ids[c["diff"]]["value"] == 0.09 and len(c["diff_ci"]) == 2
    assert ids[c["mcnemar"]["b"]]["value"] == 21 and ids[c["mcnemar"]["p"]]["value"] == 0.163
    assert c["diff"] == "c:bench:v2:contrast:d1-vote5~extended:diff"
