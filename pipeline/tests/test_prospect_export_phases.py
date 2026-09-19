"""The Eval page's phase blocks: built from the run JSONs, every number a value, every row naming its run."""

from __future__ import annotations

import json
from pathlib import Path

from legacy_reader.contract_check import Errors, _ref, check_registry
from legacy_reader.prospect import export as X
from legacy_reader.values import registry


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
    assert b["decision"] == {"model": "random_forest", "stage": "candidate", "served": False,
                             "reason": "not validated; nothing is served", "run_id": "d1f910f4", "version": 4}
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
