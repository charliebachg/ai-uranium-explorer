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


def contrast(y: np.ndarray, a: dict[str, Any], b: dict[str, Any], boot: int, seed: int) -> dict[str, Any]:
    """The paired difference in ranking PR-AUC (a refused cell at 0.5 on both sides) and McNemar on verdicts."""
    from ..prospect import models as M
    from ..prospect.extended import paired_difference

    pa = np.where(np.isfinite(a["p"]), a["p"], 0.5)
    pb = np.where(np.isfinite(b["p"]), b["p"], 0.5)
    d = paired_difference(y, pa, pb, M.pr_auc, n=boot, seed=seed)
    right = lambda v: np.array([(x == POSITIVE) == bool(t) and x != ABSTAIN for x, t in zip(v, y, strict=True)])  # noqa: E731
    return {**d, "pr_auc_rank_first": M.pr_auc(y, pa), "pr_auc_rank_second": M.pr_auc(y, pb),
            "mcnemar": mcnemar(right(a["v"]), right(b["v"]))}


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


def extras(version: str, summaries: list[dict[str, Any]], boot: int, seed: int
           ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The derived rows (votes over an arm's samples, a fitted decider over a v2 arm's readings) and every
    contrast in `CONTRASTS` whose two rows exist."""
    from . import score as SC
    from .families import FAMILIES

    ids, y, folds, cell_of = labelled(version)
    bench = F.load_bench(version)
    strata = [str(bench.key[b].get("stratum") or bench.key[b].get("label")) for b in ids]
    views: dict[str, dict[str, Any]] = {}
    by_arm: dict[str, list[tuple[int, dict[str, Any], dict[str, dict[str, Any]]]]] = {}
    for s in summaries:
        rows = run_cells(s)
        sample = int(s.get("sample") or 0)
        views[SC.sample_name(s["arm"], sample)] = _arrays(ids, {b: cell_view(r) for b, r in rows.items()})
        by_arm.setdefault(s["arm"], []).append((sample, s, rows))
    scores, _note = SC.bench_oof_scores(version, list(cell_of.values()))
    for model, sc in scores.items():
        p = np.array([sc.get(cell_of[b], NAN) for b in ids], dtype=float)
        views[model] = {"p": p, "v": [POSITIVE if x >= 0.5 else NEGATIVE for x in np.nan_to_num(p, nan=0.5)],
                        "ab": ~np.isfinite(p)}
    derived: list[dict[str, Any]] = []
    for arm, samples in sorted(by_arm.items()):
        samples.sort(key=lambda t: t[0])
        if len(samples) >= 2:
            name = f"{arm}-vote{len(samples)}"
            views[name] = _arrays(ids, vote([rows for _s, _sum, rows in samples]))
            derived.append(_derived_row(
                name, "vote", samples[0][1].get("model"), views[name], y, strata,
                sum(float((s.get("score") or {}).get("cost_usd_total") or 0.0) for _n, s, _r in samples), boot, seed,
                f"samples {', '.join(str(n) for n, _s, _r in samples)} of {arm}: the mean published probability, "
                "abstaining when most samples abstained, else the majority among the committed"))
        first = next(((s, rows) for n, s, rows in samples if n == 0), None)
        if first and any(isinstance(r.get("readings"), dict) for r in first[1].values()):
            strengths = reading_strengths(first[1], tuple(f.name for f in FAMILIES))
            fitted = fitted_decider(strengths, dict(zip(ids, (int(v) for v in y), strict=True)), folds)
            p = np.array([fitted.get(b, NAN) for b in ids], dtype=float)
            name = f"{arm}-fitted"
            views[name] = {"p": p, "v": [POSITIVE if x >= 0.5 else NEGATIVE for x in np.nan_to_num(p, nan=0.5)],
                           "ab": ~np.isfinite(p)}
            derived.append(_derived_row(
                name, "fitted decider", first[0].get("model"), views[name], y, strata, 0.0, boot, seed,
                f"a logistic regression over the four reading strengths of {arm}, fitted out of fold on the "
                "benchmark's spatial folds; no model call"))
    contrasts = []
    for a, b, question in CONTRASTS:
        if a in views and b in views:
            contrasts.append({"first": a, "second": b, "question": question,
                              **contrast(y, views[a], views[b], boot, seed)})
    return derived, contrasts
