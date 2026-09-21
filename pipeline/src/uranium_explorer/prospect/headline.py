"""Phase 0: does effort really beat geology, once the sampling confound is removed?

The headline (effort PR-AUC 0.347 against learned 0.133 under spatial folds) was measured with every
unlabelled cell treated as a negative. The Geological Survey of Canada's own paper on positive-unlabelled
prospectivity names what that does: label crossover shrinks the positive area and inflates variance, and it
does so asymmetrically, hurting the geology model most while effort features are measured cleanly on every
cell. So the claim is re-tested here under the two corrections with published support, and reported under
all three fold schemes, with intervals, and beside the numbers a manager and a reviewer actually read.

Every configuration is a pure function of the feature matrix and a seed, so the table is reproducible from
the store alone. Nothing here writes a served score; it writes metrics.

Corrections (each on or off, all four combinations run):
* `matched_background` — target-group background sampling: negatives are drawn so that their distribution
  over an effort index matches the positives' (decile-matched), instead of being every unlabelled cell.
  What is left for the learned model to find is geology *given* effort.
* `thinned_positives` — systematic subsampling: at most one positive per 10 km block, so a camp of forty
  adjacent deposit cells counts once, not forty times.

Beside PR-AUC and ROC-AUC:
* area-budget capture: of the deposit cells (not occurrences), how many fall in the top 5 / 10 / 15 % of
  ranked area. The number a manager reads, and robust to the confound in a way discrimination metrics are not.
* MineTRACE's protocol: 30 positives held out, 200 negatives sampled, fit on the rest, ROC-AUC on the 230.
  Reported so the two systems can be placed on one axis, and stated to be a different measurement.
* bootstrap intervals: cells resampled with replacement within the out-of-fold predictions, stratified.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..store import append_frame, connect
from ..store import snapshot as SN
from . import models as M
from . import tracking as TR

TOOL = "prospect/headline"
THIN_BLOCK_M = 10_000.0
BOOT = 200
MINETRACE_POS, MINETRACE_NEG, MINETRACE_REPEATS = 30, 200, 20
AREA_BUDGETS = (0.05, 0.10, 0.15)


# ---------------------------------------------------------------- the two corrections


def effort_index(df: pd.DataFrame) -> np.ndarray:
    """One number per cell for how much exploration it has seen: the mean rank of the six effort features.

    Counts are ranked, not summed, so a cell with 400 holes and none of the rest does not dominate; a
    missing effort feature ranks as zero, which is what "nobody looked" means for a count."""
    cols = [c for c in M.EFFORT_FEATURES if c in df.columns]
    if not cols:
        raise ValueError("no effort feature in the frame")
    ranks = np.column_stack([df[c].fillna(0.0).rank(method="average").to_numpy() for c in cols])
    return ranks.mean(axis=1) / len(df)


def matched_background(df: pd.DataFrame, y: np.ndarray, seed: int = 0, per_positive: int = 20,
                       bins: int = 10) -> np.ndarray:
    """Which cells to keep: every positive, plus negatives whose effort-index deciles match the positives'.

    Returns a boolean mask over df. Negatives are sampled without replacement inside each effort decile in
    proportion to the positives in that decile; a decile short of negatives contributes what it has."""
    rng = np.random.default_rng(seed)
    e = effort_index(df)
    edges = np.quantile(e, np.linspace(0, 1, bins + 1))
    decile = np.clip(np.searchsorted(edges, e, side="right") - 1, 0, bins - 1)
    keep = y.astype(bool).copy()
    pos_counts = np.bincount(decile[y == 1], minlength=bins)
    for b in range(bins):
        want = int(pos_counts[b] * per_positive)
        pool = np.flatnonzero((decile == b) & (y == 0))
        if want == 0 or len(pool) == 0:
            continue
        take = rng.choice(pool, size=min(want, len(pool)), replace=False)
        keep[take] = True
    return keep


def thinned_positives(df: pd.DataFrame, y: np.ndarray, block_m: float = THIN_BLOCK_M, seed: int = 0) -> np.ndarray:
    """Which cells to keep: all negatives, and one positive per coarse block, chosen at random with a seed."""
    rng = np.random.default_rng(seed)
    if "cx" in df.columns and "cy" in df.columns:
        bx = np.floor(df["cx"].to_numpy() / block_m)
        by = np.floor(df["cy"].to_numpy() / block_m)
    else:  # degrees: about 10 km at this latitude
        bx = np.floor(df["lon"].to_numpy() / 0.17)
        by = np.floor(df["lat"].to_numpy() / 0.09)
    keep = ~y.astype(bool)
    pos = np.flatnonzero(y == 1)
    blocks = pd.Series([(bx[i], by[i]) for i in pos])
    for _, positions in blocks.groupby(blocks).indices.items():
        keep[pos[rng.choice(np.asarray(positions))]] = True
    return keep


# ---------------------------------------------------------------- metrics beside the usual ones


def capture_at_budgets(y_dep: np.ndarray, p: np.ndarray, budgets: tuple[float, ...] = AREA_BUDGETS) -> dict[str, float]:
    """Share of deposit cells inside the top k% of ranked area, for each budget. NaN-safe on p."""
    ok = np.isfinite(p)
    y_dep, p = y_dep[ok], p[ok]
    n_dep = int(y_dep.sum())
    out = {}
    order = np.argsort(-p)
    for b in budgets:
        k = max(1, int(round(b * len(p))))
        out[f"capture_top{int(round(b * 100))}"] = float(y_dep[order[:k]].sum() / n_dep) if n_dep else float("nan")
    return out


def bootstrap_ci(y: np.ndarray, p: np.ndarray, stat: Callable[[np.ndarray, np.ndarray], float], n: int = BOOT,
                 seed: int = 0) -> tuple[float, float]:
    """Percentile interval by resampling cells with replacement, positives and negatives separately."""
    rng = np.random.default_rng(seed)
    pos = np.flatnonzero(y == 1)
    neg = np.flatnonzero(y == 0)
    vals = []
    for _ in range(n):
        idx = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
        vals.append(stat(y[idx], p[idx]))
    return float(np.nanpercentile(vals, 2.5)), float(np.nanpercentile(vals, 97.5))


def minetrace_protocol(df: pd.DataFrame, feature_set: str, y: np.ndarray, repeats: int = MINETRACE_REPEATS,
                       n_pos: int = MINETRACE_POS, n_neg: int = MINETRACE_NEG, seed: int = 0,
                       fit: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray] | None = None) -> dict[str, Any]:
    """Hold out n_pos positives and n_neg sampled negatives, fit on the rest, ROC-AUC on the held-out 230.

    The held-out positives are whole spatial blocks, so a held-out deposit never has its neighbour in
    training. Repeated with fresh draws; mean and spread reported."""
    keys = list(M.FEATURE_SETS[feature_set])
    x = df[keys].to_numpy(dtype=float)
    blocks = df["block_id"].to_numpy()
    rng = np.random.default_rng(seed)
    aucs = []
    for _ in range(repeats):
        pos_blocks = np.unique(blocks[y == 1])
        rng.shuffle(pos_blocks)
        held = np.zeros(len(df), dtype=bool)
        for b in pos_blocks:
            if held[y == 1].sum() >= n_pos:
                break
            held |= (blocks == b) & (y == 1)
        neg_pool = np.flatnonzero((y == 0) & ~held)
        held_neg = rng.choice(neg_pool, size=min(n_neg, len(neg_pool)), replace=False)
        test = held.copy()
        test[held_neg] = True
        train = ~test
        if y[train].sum() == 0:
            continue
        p = (fit or _fit_predict)(x[train], y[train], x[test])
        aucs.append(M.roc_auc(y[test], p))
    return {"protocol": f"{n_pos} positives held out by block, {n_neg} negatives sampled, {len(aucs)} repeats",
            "roc_auc_mean": float(np.mean(aucs)) if aucs else float("nan"),
            "roc_auc_sd": float(np.std(aucs)) if aucs else float("nan"),
            "roc_auc_min": float(np.min(aucs)) if aucs else float("nan"),
            "roc_auc_max": float(np.max(aucs)) if aucs else float("nan")}


def _fit_predict(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray) -> np.ndarray:
    model = M._model()
    model.fit(x_train, y_train)
    return model.predict_proba(x_test)[:, 1]


# ---------------------------------------------------------------- one configuration


@dataclass(frozen=True)
class Config:
    feature_set: str
    positives: str            # "all" (deposit + occurrence) | "deposits"
    matched: bool
    thinned: bool
    fold: str

    @property
    def key(self) -> str:
        corr = "+".join(c for c, on in (("matched", self.matched), ("thinned", self.thinned)) if on) or "naive"
        return f"{self.feature_set}.{self.positives}.{corr}.{self.fold}"


def labels(df: pd.DataFrame, positives: str) -> np.ndarray:
    if positives == "deposits":
        return (df["label_tier"] == "deposit").to_numpy().astype(int)
    return (df["label_tier"] != "unlabelled").to_numpy().astype(int)


def evaluate_config(df: pd.DataFrame, cfg: Config, seed: int = 0, boot: int = BOOT,
                    fit: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray] | None = None) -> dict[str, Any]:
    """Out-of-fold metrics for one configuration, with intervals. Deposit capture is always on deposits."""
    y = labels(df, cfg.positives)
    train_ok = training_mask(df, y, matched=cfg.matched, thinned=cfg.thinned, seed=seed)
    fold = M.folds(df, cfg.fold, seed=seed)
    p = _oof(df, cfg.feature_set, y, fold, fit, train_ok=train_ok)
    ok = np.isfinite(p)
    y_ok, p_ok = y[ok], p[ok]
    y_dep = (df["label_tier"] == "deposit").to_numpy().astype(int)[ok]
    row: dict[str, Any] = {"config": cfg.key, "feature_set": cfg.feature_set, "positives": cfg.positives,
                           "matched": cfg.matched, "thinned": cfg.thinned, "fold": cfg.fold,
                           "cells": int(len(df)), "train_cells": int(train_ok.sum()),
                           "train_pos": int(y[train_ok].sum()), "scored": int(ok.sum()), "n_pos": int(y_ok.sum()),
                           "n_deposits": int(y_dep.sum()), "base_rate": float(y_ok.mean()) if len(y_ok) else float("nan")}
    if y_ok.sum() == 0 or y_ok.sum() == len(y_ok):
        row["note"] = "no usable split"
        return row
    row["pr_auc"] = M.pr_auc(y_ok, p_ok)
    row["roc_auc"] = M.roc_auc(y_ok, p_ok)
    row.update(capture_at_budgets(y_dep, p_ok))
    row["pr_auc_ci"] = bootstrap_ci(y_ok, p_ok, M.pr_auc, n=boot, seed=seed)
    row["roc_auc_ci"] = bootstrap_ci(y_ok, p_ok, M.roc_auc, n=boot, seed=seed)
    row["capture_top10_ci"] = bootstrap_ci(y_dep, p_ok, lambda yy, pp: capture_at_budgets(yy, pp, (0.10,))["capture_top10"],
                                           n=boot, seed=seed)
    return row


def training_mask(df: pd.DataFrame, y: np.ndarray, matched: bool, thinned: bool, seed: int = 0) -> np.ndarray:
    """Which cells a model may be fitted on. The corrections shrink the training set and nothing else:
    every configuration is still scored on every common cell, so the metrics stay comparable."""
    keep = np.ones(len(df), dtype=bool)
    if thinned:
        keep &= thinned_positives(df, y, seed=seed)
    if matched:
        idx = np.flatnonzero(keep)
        inner = matched_background(df.iloc[idx].reset_index(drop=True), y[idx], seed=seed)
        keep = np.zeros(len(df), dtype=bool)
        keep[idx[inner]] = True
    return keep


def _oof(df: pd.DataFrame, feature_set: str, y: np.ndarray, fold: M.Fold,
         fit: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray] | None,
         train_ok: np.ndarray | None = None) -> np.ndarray:
    """Out-of-fold scores for every cell: a fold's model is fitted on the other folds' *training-eligible*
    cells and scores all of the fold's cells, eligible or not."""
    keys = list(M.FEATURE_SETS[feature_set])
    x = df[keys].to_numpy(dtype=float)
    eligible = np.ones(len(df), dtype=bool) if train_ok is None else train_ok
    out = np.full(len(df), np.nan)
    for g in np.unique(fold.groups):
        if g < 0:
            continue
        test = fold.groups == g
        train = ~test & eligible
        if y[train].sum() == 0 or y[test].sum() == 0:
            continue
        out[test] = (fit or _fit_predict)(x[train], y[train], x[test])
    return out


# ---------------------------------------------------------------- the whole table


def configs(feature_sets: tuple[str, ...] = ("learned", "effort"), positives: tuple[str, ...] = ("all", "deposits"),
            folds: tuple[str, ...] = ("random", "spatial", "camp")) -> list[Config]:
    return [Config(fs, pos, m, t, f) for fs, pos, (m, t), f in
            itertools.product(feature_sets, positives, [(False, False), (True, False), (False, True), (True, True)], folds)]


def run(grid_id: str | None = None, seed: int = 0, boot: int = BOOT, log: Callable[[str], None] = print,
        write: bool = True, fit: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray] | None = None,
        cfgs: list[Config] | None = None, track: bool = True, snapshot: str | None = None) -> dict[str, Any]:
    """Every configuration on the common complete cases, the MineTRACE protocol, and the verdict.

    The run names the store it ran on: `snapshot` pins it to a taken snapshot and refuses any other store."""
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    pinned = SN.pin(snapshot, log=log)
    frames = {fs: M.matrix(fs, grid_id) for fs in ("learned", "effort")}
    common = set(frames["learned"]["cell_id"]) & set(frames["effort"]["cell_id"])
    extra = [c for c in M.EFFORT_FEATURES if c not in frames["learned"].columns]
    both = frames["learned"].merge(frames["effort"][["cell_id", *extra]], on="cell_id") if extra else frames["learned"]
    both = both[both["cell_id"].isin(common)].reset_index(drop=True)
    log(f"  common complete cases: {len(both):,} cells, {(both['label_tier'] != 'unlabelled').sum()} positives, "
        f"{(both['label_tier'] == 'deposit').sum()} deposits")
    rows = []
    for cfg in (cfgs or configs()):
        r = evaluate_config(both, cfg, seed=seed, boot=boot, fit=fit)
        rows.append(r)
        if "pr_auc" in r:
            log(f"    {cfg.key:<40} PR-AUC {r['pr_auc']:.3f} [{r['pr_auc_ci'][0]:.3f},{r['pr_auc_ci'][1]:.3f}]  "
                f"ROC {r['roc_auc']:.3f}  dep@10% {r['capture_top10']:.0%}  n={r['scored']} pos={r['n_pos']}")
        else:
            log(f"    {cfg.key:<40} {r.get('note')}")
    mt = {}
    for fs in ("learned", "effort"):
        mt[fs] = minetrace_protocol(both, fs, labels(both, "deposits"), seed=seed, fit=fit)
        log(f"  MineTRACE protocol, {fs}: ROC-AUC {mt[fs]['roc_auc_mean']:.3f} ± {mt[fs]['roc_auc_sd']:.3f} "
            f"(min {mt[fs]['roc_auc_min']:.3f}, max {mt[fs]['roc_auc_max']:.3f})")
    verdict = _verdict(rows)
    log("  verdict: " + verdict["text"])
    out: dict[str, Any] = {"run_id": now, "mlflow_run_id": None, "store_sha256": pinned["store_sha256"],
                           "snapshot": pinned["snapshot"], "seed": seed, "boot": boot, "cells": int(len(both)),
                           "verdict": verdict, "rows": rows, "minetrace": mt}
    if track:
        out["mlflow_run_id"] = TR.log_run(
            "headline",
            params={"seed": seed, "boot": boot, "cells": out["cells"], "store_sha256": pinned["store_sha256"]},
            metrics=_tracked_metrics(rows),
            tags={"phase": "0", "kind": "headline", "snapshot": pinned["snapshot"], "verdict": verdict["text"]},
            artifacts={"rows.json": rows})
    if write:
        _write_metrics(out, out["mlflow_run_id"] or now)
        TR.write_json("headline.json", out)
    return out


def _tracked_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    """The numbers the verdict rests on: spatial folds under both corrections, each feature set on each positive set."""
    out: dict[str, float] = {}
    for r in rows:
        if r["matched"] and r["thinned"] and r["fold"] == "spatial" and "pr_auc" in r:
            for key in ("pr_auc", "roc_auc", "capture_top5", "capture_top10", "capture_top15"):
                out[f"{r['feature_set']}.{r['positives']}.{key}"] = r[key]
    return out


def _verdict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Does effort still beat geology under spatial folds after both corrections, on both label sets?"""
    def get(fs: str, pos: str) -> dict[str, Any] | None:
        return next((r for r in rows if r["feature_set"] == fs and r["positives"] == pos and r["matched"]
                     and r["thinned"] and r["fold"] == "spatial" and "pr_auc" in r), None)
    findings = {}
    for pos in ("all", "deposits"):
        le, ef = get("learned", pos), get("effort", pos)
        if not le or not ef:
            findings[pos] = "not measurable"
            continue
        gap = le["pr_auc"] - ef["pr_auc"]
        overlap = not (le["pr_auc_ci"][1] < ef["pr_auc_ci"][0] or ef["pr_auc_ci"][1] < le["pr_auc_ci"][0])
        findings[pos] = ("geology ahead" if gap > 0 else "effort ahead") + (" (intervals overlap)" if overlap else " (intervals separate)")
    text = "; ".join(f"{pos}: {v}" for pos, v in findings.items())
    return {"by_positives": findings, "text": f"after matched background and thinned positives, under spatial folds — {text}"}


def _write_metrics(out: dict[str, Any], run_id: str) -> None:
    """The store's metric rows, citing the MLflow run when there is one and the timestamp otherwise."""
    now = out["run_id"]
    metrics = []
    for r in out["rows"]:
        for key in ("pr_auc", "roc_auc", "capture_top5", "capture_top10", "capture_top15", "base_rate"):
            if key in r and np.isfinite(r[key]):
                metrics.append({"metric_key": f"headline.{r['config']}.{key}", "run_id": run_id, "value": float(r[key]),
                                "fmt": "ratio3", "computed_at": now,
                                "note": f"{key}; positives={r['positives']}, matched={r['matched']}, thinned={r['thinned']}, "
                                        f"fold={r['fold']}; {r['n_pos']} positives in {r['scored']} scored cells"
                                        + (f"; 95% CI {r[key + '_ci'][0]:.3f}-{r[key + '_ci'][1]:.3f}" if key + "_ci" in r else "")})
    for fs, mt in out["minetrace"].items():
        for key in ("roc_auc_mean", "roc_auc_sd"):
            if np.isfinite(mt[key]):
                metrics.append({"metric_key": f"headline.minetrace.{fs}.{key}", "run_id": run_id, "value": float(mt[key]),
                                "fmt": "ratio3", "computed_at": now, "note": mt["protocol"]})
    con = connect()
    try:
        con.execute("delete from derived.metric where metric_key like 'headline.%'")
        append_frame(con, "derived", "metric", pd.DataFrame(metrics), "derived")
    finally:
        con.close()
