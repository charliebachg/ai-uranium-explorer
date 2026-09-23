"""Rows made from runs already paid for (votes, a fitted decider) and the contrasts fixed before the table."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from uranium_explorer.analyst import arms as A
from uranium_explorer.analyst import compare as CMP
from uranium_explorer.analyst import run as RUN
from uranium_explorer.analyst import score as SC
from uranium_explorer.store import connect
from fake_bench import install_runtime, make_bench
from test_analyst_families import ReadingBackend


def row(verdict: str, p: float, published: bool = True) -> dict:
    return {"answer": {"verdict": verdict, "probability": p}, "published": published}


def test_the_vote_abstains_when_most_abstain_and_else_takes_the_committed_majority() -> None:
    s = [{"a": row("supports_closer_look", 0.8), "b": row("insufficient", 0.4), "c": row("supports_closer_look", 0.6)},
         {"a": row("evidence_against", 0.4), "b": row("insufficient", 0.3), "c": row("evidence_against", 0.2)},
         {"a": row("supports_closer_look", 0.6), "b": row("evidence_against", 0.2, published=False),
          "c": row("insufficient", 0.5)}]
    v = CMP.vote(s)
    assert v["a"] == (pytest.approx(0.6), "supports_closer_look", False), "two of three committed say closer look"
    assert v["b"][1] == "insufficient" and v["b"][2], "every sample abstained (a refusal is an abstention)"
    assert v["b"][0] == pytest.approx(0.35), "the refused answer's probability is not averaged in"
    assert v["c"][1] == "evidence_against", "one committed each way: the tie goes to the mean probability, 0.433"


def test_a_tied_vote_goes_to_the_side_of_the_mean_probability() -> None:
    s = [{"a": row("supports_closer_look", 0.3)}, {"a": row("evidence_against", 0.2)}]
    assert CMP.vote(s)["a"][1] == "evidence_against"


def test_the_fitted_decider_is_out_of_fold_and_learns_the_informative_reading() -> None:
    rng = np.random.default_rng(0)
    ids = [f"b{i:03d}" for i in range(200)]
    y = {b: int(rng.random() < 0.4) for b in ids}
    strengths = {b: [0.8 if y[b] else 0.2, rng.random(), 0.5, rng.random()] for b in ids}
    folds = {b: i % 5 for i, b in enumerate(ids)}
    p = CMP.fitted_decider(strengths, y, folds)
    assert set(p) == set(ids)
    pos = np.mean([p[b] for b in ids if y[b]])
    neg = np.mean([p[b] for b in ids if not y[b]])
    assert pos > 0.6 > 0.4 > neg


def test_mcnemar_counts_the_discordant_cells_and_is_exact() -> None:
    a = np.array([True] * 9 + [False] * 1 + [True] * 5)
    b = np.array([False] * 9 + [True] * 1 + [True] * 5)
    m = CMP.mcnemar(a, b)
    assert (m["b"], m["c"]) == (9, 1) and m["p"] == pytest.approx(2 * 11 / 1024)
    assert CMP.mcnemar(a, a) == {"b": 0, "c": 0, "p": 1.0}


def test_the_table_adds_the_vote_and_the_fitted_decider_and_only_contrasts_rows_that_exist(monkeypatch, tmp_path) -> None:
    rt = install_runtime(monkeypatch, tmp_path)
    bench = make_bench(tmp_path)
    arm = replace(A.load_arm("d2"), switches=A.load_arm("v0").switches)
    backend = ReadingBackend()
    kw = dict(budget_usd=5.0, log=lambda *_: None, workers=1, track=False, cache_root=rt.cache, boot=5)
    for sample in (0, 1):
        RUN.run_arm(bench.version, arm, lambda _arm: backend, sample=sample, **kw)
    con = connect(tmp_path / "t.duckdb")
    try:
        out = SC.table(bench.version, con=con, boot=5, write=False)
    finally:
        con.close()
    names = [r["name"] for r in out["rows"]]
    assert {"d2", "d2~s1", "d2-vote2", "d2-fitted"} <= set(names)
    vote = next(r for r in out["rows"] if r["name"] == "d2-vote2")
    assert vote["kind"] == "derived" and vote["n_cells"] == 4 and vote["cost_usd"] == pytest.approx(2.5), "both samples paid"
    # of the fixed pairs only the fitted decider against its own arm exists here; nothing is chosen afterwards
    assert [(c["first"], c["second"]) for c in out["contrasts"]] == [("d2-fitted", "d2")]
    c = out["contrasts"][0]
    assert {"diff", "diff_ci", "p_not_better", "mcnemar"} <= set(c) and c["cells"] == 4


def test_average_precision_matches_scikit_learn_with_ties() -> None:
    from sklearn.metrics import average_precision_score

    rng = np.random.default_rng(3)
    for _ in range(20):
        y = (rng.random(60) < 0.4).astype(int)
        s = np.round(rng.random(60), 1)  # coarse, so many ties, as LLM probabilities are
        assert CMP.average_precision(y, s) == pytest.approx(average_precision_score(y, s))


def test_the_permutation_test_holds_its_level_and_finds_a_real_gap() -> None:
    rng = np.random.default_rng(0)
    y = np.array([1] * 30 + [0] * 40)
    noise = rng.random(70)
    assert CMP.rank_permutation(y, noise, noise, n=500) == 1.0, "identical rankings: no evidence either way"
    good = y + 0.8 * rng.random(70)
    assert CMP.rank_permutation(y, good, noise, n=500) < 0.01
    assert CMP.rank_permutation(y, noise, good, n=500) > 0.9


def test_holm_steps_down_in_the_order_given() -> None:
    assert CMP.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def test_the_own_rule_keeps_a_refused_answers_probability() -> None:
    refused = {"published": False, "answer": {"probability": 0.2, "verdict": "insufficient"}}
    assert math.isnan(CMP.cell_view(refused)[0])
    p, _v, abstained = CMP.cell_view_own(refused)
    assert p == 0.2 and abstained
    assert math.isnan(CMP.cell_view_own({"published": False, "answer": None})[0]), "no answer, no probability"


def test_the_incremental_test_finds_added_information_and_not_its_absence() -> None:
    rng = np.random.default_rng(1)
    y = np.array([1] * 50 + [0] * 64)
    fitted = rng.random(114) + 0.3 * y
    adds = 0.8 * y + rng.random(114)
    assert CMP.incremental(y, adds, fitted)["p"] < 0.01
    noise = rng.random(114)
    assert CMP.incremental(y, noise, fitted)["p"] > 0.05
    assert CMP.incremental(y, 1 - adds, fitted)["p"] > 0.5, "one-sided: an LLM that ranks backwards adds nothing"
