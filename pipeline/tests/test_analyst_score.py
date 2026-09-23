"""Scoring: the metrics on a case worked by hand, a run read back, the baselines on a temporary store, and
the merged table."""

from __future__ import annotations

import json

import numpy as np
import pytest

from uranium_explorer.analyst import frozen as F
from uranium_explorer.analyst import score as SC
from uranium_explorer.store import connect

from fake_bench import LABELLED, install_runtime, make_bench

POS, NEG, ABS = "supports_closer_look", "evidence_against", "insufficient"


# ---------------------------------------------------------------- metrics by hand

def hand_case():
    y = np.array([1, 1, 1, 0, 0, 0, 1, 0])
    verdicts = [POS, POS, ABS, NEG, POS, NEG, NEG, ABS]
    p = np.array([0.9, 0.9, 0.7, 0.1, 0.9, 0.1, 0.1, 0.2])
    abstain = np.array([v == ABS for v in verdicts])
    return y, p, verdicts, abstain


def test_metrics_on_a_case_worked_by_hand() -> None:
    y, p, verdicts, abstain = hand_case()
    m = SC.metrics(y, p, verdicts, abstain, boot=100, seed=1,
                   strata=["deposit", "deposit", "occurrence", "negative", "negative", "negative", "occurrence", "negative"])
    # predicted positive: cells 0, 1, 4 -> TP 2, FP 1; positives not predicted: cells 2 (abstained), 6 -> FN 2
    assert m["precision"] == pytest.approx(2 / 3) and m["recall"] == pytest.approx(0.5) and m["f1"] == pytest.approx(4 / 7)
    assert m["abstain_rate"] == pytest.approx(0.25) and m["abstain_n"] == 2 and m["abstain_denominator"] == 8
    assert m["n"] == 8 and m["n_pos"] == 4 and m["base_rate"] == 0.5
    # calibration with abstentions at 0.5: bin 9 holds (1,1,0) at 0.9, bin 5 holds (1,0) at 0.5, bin 1 holds (0,0,1) at 0.1
    assert m["ece"] == pytest.approx(3 / 8 * abs(2 / 3 - 0.9) + 0 + 3 / 8 * abs(1 / 3 - 0.1))
    # over the committed cells the 0.9s hold (1,1,0) and the 0.1s (0,0,1)
    assert m["ece_committed"] == pytest.approx(0.5 * abs(2 / 3 - 0.9) + 0.5 * abs(1 / 3 - 0.1))
    assert 0 < m["roc_auc"] < 1 and 0 < m["pr_auc"] <= 1 and 0 < m["roc_auc_all"] < 1
    for k in ("precision", "recall", "f1", "pr_auc", "roc_auc", "ece", "abstain_rate"):
        lo, hi = m[f"{k}_ci"]
        assert lo <= m[k] <= hi, k
    assert m["strata"]["deposit"] == {"n": 2, "accuracy": 1.0, "abstain_rate": 0.0}
    assert m["strata"]["occurrence"] == {"n": 2, "accuracy": 0.0, "abstain_rate": 0.5}
    assert m["strata"]["negative"]["n"] == 4 and m["strata"]["negative"]["accuracy"] == pytest.approx(0.5)


def test_metrics_on_a_perfect_answer_and_on_nothing() -> None:
    y = np.array([1, 0, 1, 0])
    m = SC.metrics(y, y.astype(float), [POS, NEG, POS, NEG], np.zeros(4, bool), boot=0)
    assert m["f1"] == 1.0 and m["ece"] == 0.0 and m["pr_auc"] == 1.0 and m["roc_auc"] == 1.0
    assert "f1_ci" not in m, "no bootstrap when none is asked for"
    assert SC.metrics(np.array([]), np.array([]), [], np.array([], bool))["n"] == 0
    with pytest.raises(ValueError):
        SC.metrics(y, y[:2], [POS] * 4, np.zeros(4, bool))


def test_an_all_abstain_run_has_no_ranking_metric_but_a_rate() -> None:
    y = np.array([1, 0, 1, 0])
    m = SC.metrics(y, np.full(4, np.nan), [ABS] * 4, np.ones(4, bool), boot=5)
    assert m["abstain_rate"] == 1.0 and np.isnan(m["pr_auc"]) and np.isnan(m["precision"])
    assert m["f1"] == 0.0 and m["recall"] == 0.0, "positives exist and none was found"
    assert m["roc_auc_all"] == 0.5, "every cell at 0.5 ranks nothing"


# ---------------------------------------------------------------- a run read back

def write_run(path, rows):
    path.mkdir(parents=True)
    (path / "cells.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def cell_row(bench_id, verdict=POS, p=0.8, published=True, answer=True, cost=0.05, duration=2.0, cache=False):
    return {"bench_id": bench_id, "cell_id": "x", "published": published and answer, "problems": [] if published else ["bad"],
            "answer": {"verdict": verdict, "probability": p} if answer else None,
            "cost_usd": cost, "duration_s": duration, "from_cache": cache}


def test_score_run_separates_probes_rejections_and_failures(tmp_path) -> None:
    key = {"b01": {"label": "deposit", "split": "open"}, "b02": {"label": "deposit", "split": "open"},
           "b03": {"label": "occurrence", "split": "open"}, "b04": {"label": "negative", "split": "open"},
           "b05": {"label": "probe", "split": "open"}, "b06": {"label": "negative", "split": "heldout"}}
    rd = tmp_path / "run"
    write_run(rd, [
        cell_row("b01"), cell_row("b02", published=False), cell_row("b03", verdict=ABS, p=0.4),
        cell_row("b04", verdict=NEG, p=0.1, cache=True), cell_row("b05", verdict=ABS, p=0.5),
        cell_row("b99", answer=False, cost=0.0, duration=None),  # a failed cell with no key entry
    ])
    s = SC.score_run(rd, key, boot=10)
    assert s["n_rows"] == 6 and s["n_labelled"] == 4 and s["n_probe"] == 1 and s["n_unknown_key"] == 1
    assert s["n_answered"] == 5 and s["n_rejected"] == 1 and s["n_failed"] == 1
    assert s["gate_rejection_rate"] == 0.2 and s["gate_rejection_denominator"] == 5
    assert s["probe_abstain_rate"] == 1.0
    # b01 positive right, b02 rejected -> abstain, b03 abstain, b04 negative right: TP 1, FP 0, FN 2
    assert s["precision"] == 1.0 and s["recall"] == pytest.approx(1 / 3) and s["abstain_n"] == 2
    assert s["cost_usd_total"] == pytest.approx(0.25) and s["cost_usd_per_cell"] == pytest.approx(0.25 / 6)
    assert s["latency_s_per_cell"] == 2.0 and s["from_cache_share"] == pytest.approx(1 / 6)
    # the last line for a cell wins, so a resumed run reads the same as a whole one
    with (rd / "cells.jsonl").open("a") as f:
        f.write(json.dumps(cell_row("b02")) + "\n")
    assert SC.score_run(rd, key, boot=0)["n_rejected"] == 0
    write_run(tmp_path / "sealed", [cell_row("b06")])
    with pytest.raises(PermissionError, match="held-out"):
        SC.score_run(tmp_path / "sealed", key, boot=0)


# ---------------------------------------------------------------- baselines and the table

@pytest.fixture
def store(tmp_path, monkeypatch):
    install_runtime(monkeypatch, tmp_path)
    bench = make_bench(tmp_path)
    con = connect(tmp_path / "store.duckdb")  # the schema declares derived.cell_score_oof
    # learned separates the labels perfectly, effort is inverted, criteria is flat; b03 has no learned score
    rows = []
    for bench_id in LABELLED:
        cell = bench.cells[bench_id]
        pos = bench.key[bench_id]["label"] != "negative"
        if bench_id != "b03":
            rows.append((cell, "learned", "spatial", 0, 0.9 if pos else 0.1))
        rows.append((cell, "effort", "spatial", 0, 0.2 if pos else 0.8))
        rows.append((cell, "criteria", "spatial", 0, 0.5))
        rows.append((cell, "learned", "random", 0, 0.0))  # another fold kind: never read
    con.executemany("insert into derived.cell_score_oof (cell_id, model, fold_kind, fold, score, run_id, computed_at) "
                    "values (?, ?, ?, ?, ?, 'oof-test', 'now')", rows)
    yield bench, con
    con.close()


def test_baselines_on_a_temporary_store(store) -> None:
    bench, con = store
    rows = SC.baselines(bench.version, con, boot=20)
    by = {r["name"]: r for r in rows}
    assert list(by) == ["random_expected", "random", "copy_learned", "learned", "effort", "criteria"]
    assert all(r["kind"] == "baseline" and r["n_cells"] == 4 and r["run_id"] is None for r in rows)
    pi = 3 / 4
    exp = by["random_expected"]
    assert exp["precision"] == pi and exp["recall"] == 0.5 and exp["roc_auc"] == 0.5 and exp["pr_auc"] == pi
    assert exp["f1"] == pytest.approx(2 * pi * 0.5 / (pi + 0.5)) and "f1_ci" not in exp
    assert exp["ece"] == pytest.approx(np.mean(np.abs(pi - (np.arange(10) + 0.5) / 10)))
    assert "f1_ci" in by["random"] and by["random"]["note"].startswith("one uniform draw")
    # copy_learned abstains where the learned score is missing; the learned row scores that cell at 0.5
    assert by["copy_learned"]["abstain_n"] == 1 and by["copy_learned"]["n_missing"] == 1
    assert by["copy_learned"]["precision"] == 1.0 and by["copy_learned"]["recall"] == pytest.approx(2 / 3)
    assert by["learned"]["abstain_n"] == 0 and by["learned"]["recall"] == 1.0 and by["learned"]["precision"] == 1.0
    assert by["effort"]["recall"] == 0.0 and by["criteria"]["recall"] == 1.0 and by["criteria"]["precision"] == pi
    assert by["learned"]["strata"]["negative"]["accuracy"] == 1.0


def test_baselines_with_no_oof_scores_give_only_chance(tmp_path, monkeypatch) -> None:
    install_runtime(monkeypatch, tmp_path)
    bench = make_bench(tmp_path)
    con = connect(tmp_path / "empty.duckdb")
    notes = []
    try:
        rows = SC.baselines(bench.version, con, boot=5, log=notes.append)
    finally:
        con.close()
    assert [r["name"] for r in rows] == ["random_expected", "random"]
    assert "oof_scores.csv is missing" in notes[0], "the benchmark's own file is read first"
    assert any("cell_score_oof" in n for n in notes), "then the store's table, and it says what it lacks"


def test_table_merges_arms_and_baselines_and_writes_metrics(store) -> None:
    bench, con = store
    d = SC.out_dir(bench.version) / "arms"
    d.mkdir(parents=True)
    score = SC.metrics(np.array([1, 1, 1, 0]), np.array([0.9, 0.8, 0.4, 0.2]), [POS, POS, ABS, NEG],
                       np.array([False, False, True, False]), boot=10)
    score |= {"cost_usd_total": 1.25, "cost_usd_per_cell": 0.25, "gate_rejection_rate": 0.0, "probe_abstain_rate": 1.0,
              "latency_s_per_cell": 30.0}
    (d / "v0.json").write_text(json.dumps({"arm": "v0", "model": "claude-opus-5", "run_id": "bench-007",
                                           "mlflow_run_id": "mlf-1", "pending": [], "score": score}))
    out = SC.table(bench.version, con, boot=10)
    names = [r["name"] for r in out["rows"]]
    assert names[:1] == ["v0"] and set(names[1:]) == {"random_expected", "random", "copy_learned", "learned", "effort", "criteria"}
    arm = out["rows"][0]
    assert arm["kind"] == "arm" and arm["model"] == "claude-opus-5" and arm["n_cells"] == 4
    assert arm["run_id"] == "bench-007" and arm["mlflow_run_id"] == "mlf-1" and arm["cost_usd"] == 1.25
    assert arm["f1"] == pytest.approx(0.8) and len(arm["f1_ci"]) == 2 and arm["abstain_rate"] == 0.25
    assert out["manifest_sha256"] == bench.manifest_sha256
    written = json.loads((SC.out_dir(bench.version) / "table.json").read_text())
    assert [r["name"] for r in written["rows"]] == names
    keys = {r[0]: r for r in con.execute("select metric_key, run_id, value, fmt from derived.metric").fetchall()}
    assert keys[f"bench.{bench.version}.v0.f1"][1] == "bench-007" and keys[f"bench.{bench.version}.v0.f1"][2] == pytest.approx(0.8)
    assert keys[f"bench.{bench.version}.v0.cost_usd_per_cell"][3] == "usd"
    assert keys[f"bench.{bench.version}.learned.recall"][2] == 1.0
    assert f"bench.{bench.version}.random_expected.f1" in keys and f"bench.{bench.version}.random_expected.abstain_rate" in keys
    n = len(keys)
    SC.table(bench.version, con, boot=10)
    assert con.execute("select count(*) from derived.metric").fetchone()[0] == n, "a rerun replaces, never duplicates"
    assert F.load_bench(bench.version).heldout == {"b06"}


def test_the_ranking_uses_every_published_probability_whatever_the_verdict() -> None:
    y = np.array([1, 1, 0, 0])
    p = np.array([0.9, 0.6, 0.4, np.nan])        # the last cell's answer was refused: no probability
    v = ["supports_closer_look", "insufficient", "insufficient", ""]
    ab = np.array([False, True, True, True])
    m = SC.metrics(y, p, v, ab, boot=0)
    assert m["pr_auc_rank"] == 1.0 and m["roc_auc_rank"] == 1.0, "the two insufficient cells still rank"
    assert m["coverage"] == 0.75
    assert m["brier"] == pytest.approx(np.mean([(0.9 - 1) ** 2, (0.6 - 1) ** 2, 0.4 ** 2]))


def test_the_calibration_slope_is_one_for_a_calibrated_probability_and_near_zero_for_noise() -> None:
    rng = np.random.default_rng(0)
    p = rng.uniform(0.05, 0.95, 4000)
    y = (rng.random(4000) < p).astype(int)
    assert SC.calibration_slope(y, p) == pytest.approx(1.0, abs=0.1)
    assert abs(SC.calibration_slope(y, rng.uniform(0.05, 0.95, 4000))) < 0.2
    squashed = 0.5 + (p - 0.5) * 0.5            # an underconfident model: slope well above one
    assert SC.calibration_slope(y, squashed) > 1.5
    assert np.isnan(SC.calibration_slope(np.array([1, 1]), np.array([0.2, 0.3])))


def test_the_baselines_are_read_from_the_benchmarks_own_out_of_fold_file(tmp_path, monkeypatch) -> None:
    install_runtime(monkeypatch, tmp_path)
    bench = make_bench(tmp_path)
    rows = ["bench_id,model,fold_kind,fold,score"]
    for b in bench.cells:
        for model, score in (("learned", 0.7), ("effort", 0.2), ("criteria", 0.5), ("extended", 0.6)):
            rows.append(f"{b},{model},spatial,0,{score}")
    (bench.dir / "oof_scores.csv").write_text("\n".join(rows) + "\n")
    con = connect(tmp_path / "empty.duckdb")
    try:
        out = SC.baselines(bench.version, con, boot=5)
    finally:
        con.close()
    names = [r["name"] for r in out]
    assert names[-4:] == ["learned", "effort", "criteria", "extended"], "the extended model rides along"


def test_the_table_rescores_every_arm_from_its_cells_with_the_scorer_as_it_stands(tmp_path, monkeypatch) -> None:
    """A run scored before a metric existed still gets it: the table recomputes, it does not copy."""
    from fake_bench import AnalystBackend

    from uranium_explorer.analyst import arms as A
    from uranium_explorer.analyst import run as RUN

    rt = install_runtime(monkeypatch, tmp_path)
    bench = make_bench(tmp_path)
    s = RUN.run_arm(bench.version, A.load_arm("v0"), lambda _a: AnalystBackend(), budget_usd=5.0,
                    log=lambda *_: None, workers=1, track=False, cache_root=rt.cache, boot=5)
    s["score"] = {k: v for k, v in s["score"].items() if not k.startswith("pr_auc_rank")}
    assert "pr_auc_rank" not in SC.arm_row(s)
    assert "pr_auc_rank" in SC.arm_row(s, bench.key, boot=5)
