"""Rows derived from runs already made, and the comparisons the benchmark was run to make.

Two kinds of row cost no model call. A **vote** combines the samples of one arm (`ue arm run --sample N`): its
probability for a cell is the mean over the samples that published one, and its verdict follows the rule fixed
before the run, abstain when most samples abstained, else the majority among the committed. With five samples
it is the single call at the compute of the five-call staged agent, the control the staged agent has to beat.
A **fitted decider** reads the four readings of an analyst v2 run (one strength per evidence family) and fits
their weights out of fold on the benchmark's own spatial folds, the lever that moved MineAgent's AUC most.

The **contrasts** are fixed here, before the table exists, so a table can only report them, not choose them:
each is a paired difference in the ranking PR-AUC over the same labelled cells, with a bootstrap interval, the
share of draws where the first did not beat the second, and McNemar's exact test on which cells each got right.

Every contrast is scored two ways. The **tie rule** puts a refused or failed answer at 0.5 on both sides. The
**own rule** scores every answer at the probability it stated, refused or not: a refusal is the gate's verdict on
the numbers an answer cited, not a ranking, and at 0.5 it sits above 86 to 91% of real answers, so under the tie
rule one arm's refusals can decide a contrast (benchmark v2's d2 minus d1 was +0.063 tied, +0.010 own). The own
rule carries the permutation test (within-cell ranks swapped at random, one-sided), Holm's adjustment across
the contrasts, and Spearman's rho between the two rankings, which says how much pairing can help. When an arm
has several samples its interval resamples runs as well as cells.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import frozen as F

NAN = float("nan")
POSITIVE, ABSTAIN, NEGATIVE = "supports_closer_look", "insufficient", "evidence_against"

#: (first, second, question). A contrast runs when both rows exist; `-vote5` and `-fitted` are derived rows
CONTRASTS: tuple[tuple[str, str, str], ...] = (
    ("v1", "v0", "multi-agent against single shot, on the basic information"),
    ("d2", "d1", "multi-agent against single shot, on the rich information"),
    ("d2", "d1-vote5", "multi-agent against single shot at the same compute (five calls a cell each)"),
    ("d1", "v0", "more information, single shot"),
    ("d2", "v1", "more information, multi-agent"),
    ("d1", "extended", "the single-shot LLM against the fitted model given the same evidence"),
    ("d1-vote5", "extended", "the voted single shot against the fitted model given the same evidence"),
    ("d2", "extended", "the multi-agent LLM against the fitted model given the same evidence"),
    ("d2-fitted", "d2", "fitted weights over the four readings against the model's own ranking"),
    ("d2", "effort", "the multi-agent LLM against where people drilled"),
    ("d1", "effort", "the single-shot LLM against where people drilled"),
)


def run_cells(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A run's rows by bench id, from its own directory."""
    from .score import read_cells
    from pathlib import Path

    rd = summary.get("run_dir")
    return read_cells(Path(rd)) if rd and (Path(rd) / "cells.jsonl").is_file() else {}


def cell_view(row: dict[str, Any]) -> tuple[float, str, bool]:
    """(probability, verdict, abstained) for one run row: a refused or failed answer has no probability and
    abstains, whatever it said."""
    a = row.get("answer") if isinstance(row.get("answer"), dict) else None
    if a is None or not row.get("published"):
        return NAN, "", True
    p = a.get("probability")
    p = float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else NAN
    v = str(a.get("verdict") or "")
    return p, v, v == ABSTAIN


def cell_view_own(row: dict[str, Any]) -> tuple[float, str, bool]:
    """(probability, verdict, abstained) under the own rule: the probability the answer stated, published or
    refused; NaN only where no answer came back at all."""
    a = row.get("answer") if isinstance(row.get("answer"), dict) else None
    if a is None:
        return NAN, "", True
    p = a.get("probability")
    p = float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else NAN
    v = str(a.get("verdict") or "")
    return p, v, v == ABSTAIN or not row.get("published")


def vote(samples: list[dict[str, dict[str, Any]]]) -> dict[str, tuple[float, str, bool]]:
    """Per bench id: the mean published probability and the voted verdict (abstain when most samples
    abstained, else the majority among the committed, a tie going to the side the mean probability is on)."""
    out = {}
    for b in sorted({b for s in samples for b in s}):
        views = [cell_view(s[b]) for s in samples if b in s]
        ps = [p for p, _v, _a in views if math.isfinite(p)]
        p = float(np.mean(ps)) if ps else NAN
        abst = sum(1 for _p, _v, a in views if a)
        if abst * 2 > len(views):
            out[b] = (p, ABSTAIN, True)
            continue
        pos = sum(1 for _p, v, a in views if not a and v == POSITIVE)
        neg = sum(1 for _p, v, a in views if not a and v == NEGATIVE)
        verdict = POSITIVE if pos > neg else NEGATIVE if neg > pos else (POSITIVE if p >= 0.5 else NEGATIVE)
        out[b] = (p, verdict, False)
    return out


def reading_strengths(rows: dict[str, dict[str, Any]], families: tuple[str, ...]) -> dict[str, list[float]]:
    """Per bench id, the strength of each family's reading, 0.5 where the reading was refused or missing."""
    out = {}
    for b, r in rows.items():
        rd = r.get("readings") or {}
        out[b] = [float(rd[f]["strength"]) if isinstance(rd.get(f), dict) and rd[f].get("published")
                  and isinstance(rd[f].get("strength"), (int, float)) else 0.5 for f in families]
    return out


def fitted_decider(strengths: dict[str, list[float]], y: dict[str, int], folds: dict[str, int]) -> dict[str, float]:
    """Out-of-fold probabilities from a logistic regression over the reading strengths: each fold's cells are
    scored by weights fitted on the labelled cells of the other folds, never on their own."""
    from sklearn.linear_model import LogisticRegression

    ids = [b for b in strengths if b in y and b in folds]
    x = np.array([strengths[b] for b in ids], dtype=float)
    t = np.array([y[b] for b in ids], dtype=int)
    g = np.array([folds[b] for b in ids], dtype=int)
    out: dict[str, float] = {}
    for k in np.unique(g):
        test, train = g == k, g != k
        if len(np.unique(t[train])) < 2:
            continue
        model = LogisticRegression(C=1.0, max_iter=1000).fit(x[train], t[train])
        for b, p in zip(np.array(ids)[test], model.predict_proba(x[test])[:, 1], strict=True):
            out[str(b)] = float(p)
    return out


def average_precision(y: np.ndarray, s: np.ndarray) -> float:
    """Average precision as scikit-learn computes it (a step at each distinct score, ties taken together), in
    numpy, for the ten thousand draws of a permutation test."""
    order = np.argsort(-s, kind="mergesort")
    ss, yy = s[order], y[order]
    last = np.r_[np.flatnonzero(np.diff(ss)), len(ss) - 1]
    tp = np.cumsum(yy)[last]
    precision = tp / (last + 1)
    recall = tp / max(int(y.sum()), 1)
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


def rank_permutation(y: np.ndarray, pa: np.ndarray, pb: np.ndarray, n: int = 10_000, seed: int = 0) -> float:
    """One-sided p that the first ranks better: each cell's two normalised ranks swapped at random, so a
    probability and a fitted score are exchangeable (Bandos, Rockette and Gur 2005, for ROC areas)."""
    from scipy.stats import rankdata

    ra, rb = rankdata(pa) / len(pa), rankdata(pb) / len(pb)
    observed = average_precision(y, ra) - average_precision(y, rb)
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(n):
        swap = rng.random(len(y)) < 0.5
        if average_precision(y, np.where(swap, rb, ra)) - average_precision(y, np.where(swap, ra, rb)) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (n + 1)


def holm(ps: list[float]) -> list[float]:
    """Holm's step-down adjustment, in the order given."""
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    out, running = [1.0] * len(ps), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(ps) - rank) * ps[i]))
        out[i] = running
    return out


def mcnemar(right_a: np.ndarray, right_b: np.ndarray) -> dict[str, Any]:
    """Cells only the first got right (b), only the second (c), and the exact two-sided binomial p."""
    b = int((right_a & ~right_b).sum())
    c = int((~right_a & right_b).sum())
    n = b + c
    if n == 0:
        return {"b": 0, "c": 0, "p": 1.0}
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return {"b": b, "c": c, "p": float(min(1.0, 2 * tail))}


def contrast(y: np.ndarray, a: dict[str, Any], b: dict[str, Any], boot: int, seed: int,
             a_own: dict[str, Any] | None = None, b_own: dict[str, Any] | None = None,
             perms: int = 10_000) -> dict[str, Any]:
    """The paired difference in ranking PR-AUC (a refused cell at 0.5 on both sides) and McNemar on verdicts;
    with the own-rule views, the same difference under the own rule, its permutation p and Spearman's rho."""
    from scipy.stats import spearmanr

    from ..prospect import models as M
    from ..prospect.extended import paired_difference

    pa = np.where(np.isfinite(a["p"]), a["p"], 0.5)
    pb = np.where(np.isfinite(b["p"]), b["p"], 0.5)
    d = paired_difference(y, pa, pb, M.pr_auc, n=boot, seed=seed)
    right = lambda v: np.array([(x == POSITIVE) == bool(t) and x != ABSTAIN for x, t in zip(v, y, strict=True)])  # noqa: E731
    out = {**d, "pr_auc_rank_first": M.pr_auc(y, pa), "pr_auc_rank_second": M.pr_auc(y, pb),
           "mcnemar": mcnemar(right(a["v"]), right(b["v"]))}
    if a_own is not None and b_own is not None:
        oa = np.where(np.isfinite(a_own["p"]), a_own["p"], 0.5)
        ob = np.where(np.isfinite(b_own["p"]), b_own["p"], 0.5)
        runs_a, runs_b = a_own.get("samples") or [oa], b_own.get("samples") or [ob]
        own = two_level(y, runs_a, runs_b, n=boot, seed=seed) if len(runs_a) > 1 or len(runs_b) > 1 \
            else paired_difference(y, oa, ob, M.pr_auc, n=boot, seed=seed)
        rho = spearmanr(oa, ob).statistic
        out["own"] = {"diff": own["diff"], "diff_ci": own["diff_ci"], "first": M.pr_auc(y, oa),
                      "second": M.pr_auc(y, ob), "perm_p": rank_permutation(y, oa, ob, n=perms, seed=seed),
                      "spearman": float(rho) if np.isfinite(rho) else None,
                      "runs": [len(runs_a), len(runs_b)]}
    return out


def two_level(y: np.ndarray, runs_a: list[np.ndarray], runs_b: list[np.ndarray], n: int, seed: int) -> dict[str, Any]:
    """The paired difference with a two-level bootstrap: each draw picks one run of each arm, then resamples
    cells (positives and negatives apart). The point is the difference of the run means."""
    from ..prospect import models as M

    point = float(np.mean([M.pr_auc(y, r) for r in runs_a]) - np.mean([M.pr_auc(y, r) for r in runs_b]))
    rng = np.random.default_rng(seed)
    pos, neg = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    draws = []
    for _ in range(n):
        ra, rb = runs_a[rng.integers(len(runs_a))], runs_b[rng.integers(len(runs_b))]
        idx = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
        draws.append(average_precision(y[idx], ra[idx]) - average_precision(y[idx], rb[idx]))
    arr = np.asarray(draws, dtype=float)
    return {"diff": point, "diff_ci": [float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5))]}


def labelled(version: str) -> tuple[list[str], np.ndarray, dict[str, int], dict[str, str]]:
    """The open labelled cells in bench-id order, their labels, their folds and their cell ids."""
    from .score import LABEL_STRATA, POSITIVE_LABELS

    bench = F.load_bench(version)
    cells = [c for c in bench.open_cells()
             if (bench.key.get(c["bench_id"], {}).get("stratum") or bench.key.get(c["bench_id"], {}).get("label"))
             in LABEL_STRATA]
    ids = [str(c["bench_id"]) for c in cells]
    y = np.array([1 if bench.key[b].get("label") in POSITIVE_LABELS else 0 for b in ids], dtype=int)
    folds = {str(c["bench_id"]): int(c.get("fold", bench.key[str(c["bench_id"])].get("fold", 0))) for c in cells}
    return ids, y, folds, {str(c["bench_id"]): str(c["cell_id"]) for c in cells}


# ---------------------------------------------------------------- the table's extra rows and its contrasts


def _arrays(ids: list[str], views: dict[str, tuple[float, str, bool]]) -> dict[str, Any]:
    got = [views.get(b, (NAN, "", True)) for b in ids]
    return {"p": np.array([g[0] for g in got], dtype=float), "v": [g[1] for g in got],
            "ab": np.array([g[2] for g in got], dtype=bool)}


def _derived_row(name: str, how: str, model: Any, view: dict[str, Any], y: np.ndarray, strata: list[str],
                 cost: float, boot: int, seed: int, note: str) -> dict[str, Any]:
    from .score import STAGE_COLUMNS, metrics

    m = metrics(y, view["p"], view["v"], view["ab"], boot=boot, seed=seed, strata=strata)
    return {"name": name, "kind": "derived", "model": model, "effort": None, "n_cells": int(len(y)),
            "run_id": None, "mlflow_run_id": None, "cost_usd": float(cost), "derived_from": how,
            **dict.fromkeys(STAGE_COLUMNS), **m, "note": note}


def extras(version: str, summaries: list[dict[str, Any]], boot: int, seed: int, perms: int = 10_000
           ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The derived rows (votes over an arm's samples, a fitted decider over a v2 arm's readings, and each LLM
    arm's rank average with the extended model) and every contrast in `CONTRASTS` whose two rows exist."""
    from . import score as SC
    from .families import FAMILIES

    ids, y, folds, cell_of = labelled(version)
    bench = F.load_bench(version)
    strata = [str(bench.key[b].get("stratum") or bench.key[b].get("label")) for b in ids]
    views: dict[str, dict[str, Any]] = {}
    own: dict[str, dict[str, Any]] = {}
    by_arm: dict[str, list[tuple[int, dict[str, Any], dict[str, dict[str, Any]]]]] = {}
    for s in summaries:
        rows = run_cells(s)
        sample = int(s.get("sample") or 0)
        name = SC.sample_name(s["arm"], sample)
        views[name] = _arrays(ids, {b: cell_view(r) for b, r in rows.items()})
        own[name] = _arrays(ids, {b: cell_view_own(r) for b, r in rows.items()})
        by_arm.setdefault(s["arm"], []).append((sample, s, rows))
    scores, _note = SC.bench_oof_scores(version, list(cell_of.values()))
    for model, sc in scores.items():
        p = np.array([sc.get(cell_of[b], NAN) for b in ids], dtype=float)
        views[model] = {"p": p, "v": [POSITIVE if x >= 0.5 else NEGATIVE for x in np.nan_to_num(p, nan=0.5)],
                        "ab": ~np.isfinite(p)}
        own[model] = views[model]
    derived: list[dict[str, Any]] = []
    for arm, samples in sorted(by_arm.items()):
        samples.sort(key=lambda t: t[0])
        if len(samples) >= 2:
            name = f"{arm}-vote{len(samples)}"
            views[name] = _arrays(ids, vote([rows for _s, _sum, rows in samples]))
            own[name] = views[name]
            derived.append(_derived_row(
                name, "vote", samples[0][1].get("model"), views[name], y, strata,
                sum(float((s.get("score") or {}).get("cost_usd_total") or 0.0) for _n, s, _r in samples), boot, seed,
                f"samples {', '.join(str(n) for n, _s, _r in samples)} of {arm}: the mean published probability, "
                "abstaining when most samples abstained, else the majority among the committed"))
            # the arm itself, compared with its runs as the outer level of the bootstrap
            own[arm] = {**own[arm], "samples": [np.where(np.isfinite(o["p"]), o["p"], 0.5) for o in (
                own[SC.sample_name(arm, n)] for n, _s, _r in samples)]}
        first = next(((s, rows) for n, s, rows in samples if n == 0), None)
        if first and any(isinstance(r.get("readings"), dict) for r in first[1].values()):
            strengths = reading_strengths(first[1], tuple(f.name for f in FAMILIES))
            fitted = fitted_decider(strengths, dict(zip(ids, (int(v) for v in y), strict=True)), folds)
            p = np.array([fitted.get(b, NAN) for b in ids], dtype=float)
            name = f"{arm}-fitted"
            views[name] = {"p": p, "v": [POSITIVE if x >= 0.5 else NEGATIVE for x in np.nan_to_num(p, nan=0.5)],
                           "ab": ~np.isfinite(p)}
            own[name] = views[name]
            derived.append(_derived_row(
                name, "fitted decider", first[0].get("model"), views[name], y, strata, 0.0, boot, seed,
                f"a logistic regression over the four reading strengths of {arm}, fitted out of fold on the "
                "benchmark's spatial folds; no model call"))
    if EXTENDED in views:
        for arm in sorted(by_arm):
            if arm not in own:
                continue
            name = f"{arm}+{EXTENDED}"
            p = rank_average(own[arm]["p"], views[EXTENDED]["p"])
            views[name] = own[name] = {"p": p, "v": [POSITIVE if x >= 0.5 else NEGATIVE for x in p],
                                       "ab": np.zeros(len(p), dtype=bool)}
            derived.append(_derived_row(
                name, "rank average", None, views[name], y, strata, 0.0, boot, seed,
                f"the mean of {arm}'s rank (each answer at its own probability) and the {EXTENDED} model's rank "
                "over the labelled cells; no model call, the control an LLM given the fitted score must beat"))
    contrasts = []
    for a, b, question in CONTRASTS:
        if a in views and b in views:
            contrasts.append({"first": a, "second": b, "question": question,
                              **contrast(y, views[a], views[b], boot, seed, own.get(a), own.get(b), perms=perms)})
    tested = [c for c in contrasts if "own" in c]
    for c, adj in zip(tested, holm([c["own"]["perm_p"] for c in tested]), strict=True):
        c["own"]["holm_p"] = adj
    return derived, contrasts


EXTENDED = "extended"


def rank_average(pa: np.ndarray, pb: np.ndarray) -> np.ndarray:
    """The mean of two scores' normalised ranks, a missing score ranked as a tie at the middle."""
    from scipy.stats import rankdata

    a = np.where(np.isfinite(pa), pa, np.nanmedian(pa) if np.isfinite(pa).any() else 0.5)
    b = np.where(np.isfinite(pb), pb, np.nanmedian(pb) if np.isfinite(pb).any() else 0.5)
    return (rankdata(a) / len(a) + rankdata(b) / len(b)) / 2


def own_columns(version: str, summaries: list[dict[str, Any]], boot: int, seed: int) -> dict[str, dict[str, Any]]:
    """Per arm row: its ranking PR-AUC under the own rule with an interval, and its refused answers by label."""
    from ..prospect import headline as H
    from ..prospect import models as M
    from . import score as SC

    ids, y, _folds, _cells = labelled(version)
    bench = F.load_bench(version)
    label = [str(bench.key[b].get("stratum") or bench.key[b].get("label")) for b in ids]
    out: dict[str, dict[str, Any]] = {}
    for s in summaries:
        rows = run_cells(s)
        o = _arrays(ids, {b: cell_view_own(r) for b, r in rows.items()})
        p = np.where(np.isfinite(o["p"]), o["p"], 0.5)
        refused: dict[str, int] = {}
        for b, lab in zip(ids, label, strict=True):
            r = rows.get(b)
            if r is not None and not r.get("published"):
                refused[lab] = refused.get(lab, 0) + 1
        out[SC.sample_name(s["arm"], int(s.get("sample") or 0))] = {
            "pr_auc_own": M.pr_auc(y, p), "pr_auc_own_ci": H.bootstrap_ci(y, p, M.pr_auc, n=boot, seed=seed),
            "refused_by_label": refused}
    return out
