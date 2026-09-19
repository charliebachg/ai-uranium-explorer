"""Phase 3: the model search, tracked. Candidates against the same folds and the same null; ablations; the
spatial block size; every number an MLflow run the Eval page can cite.

Nothing here changes how a model is judged. The headline module fixed that: corrections shrink the training
set only, every configuration is scored out of fold on the same common cells, deposits are captured at fixed
area budgets, intervals are bootstraps over cells, and the effort-only null sits on every row. This module
adds candidates and asks the one question that decides what is served: does any of them beat the null under
spatial folds with intervals apart? Today's answer is recorded in the registry either way.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..store import append_frame, connect
from . import headline as H
from . import models as M
from . import tracking as TR

TOOL = "prospect/modelsearch"
Fit = Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]

#: geological feature groups for the ablations; each is removed in turn from the learned set
GROUPS: dict[str, tuple[str, ...]] = {
    "conductor": ("d_conductor_m",),
    "faults": ("d_fault_m", "fault_density"),
    "host": ("graphitic_host",),
    "depth": ("unconformity_depth_m",),
    "cover": ("water_fraction", "vegetation_fraction", "bare_fraction"),
    "terrain": ("elevation_m", "relief_m"),
}
BLOCK_KM = (20, 30, 50)


# ---------------------------------------------------------------- feature sets beyond the two originals


def register_feature_sets(df: pd.DataFrame) -> None:
    """Feature sets the candidates need, registered on the models module so the shared machinery sees them."""
    learned = M.LEARNED_FEATURES
    M.FEATURE_SETS["learned+xy"] = (*learned, "lon", "lat")
    M.FEATURE_SETS["learned+effort"] = (*learned, *M.EFFORT_FEATURES)
    if "criteria_score" in df.columns:
        M.FEATURE_SETS["learned+criteria"] = (*learned, "criteria_score")
    for name, cols in GROUPS.items():
        M.FEATURE_SETS[f"learned-{name}"] = tuple(c for c in learned if c not in cols)


def with_criteria(df: pd.DataFrame) -> pd.DataFrame:
    """The stored criteria score as a column, for the knowledge-constrained candidate."""
    scores = M.criteria_ranking(list(df["cell_id"]))
    out = df.copy()
    out["criteria_score"] = scores
    return out


# ---------------------------------------------------------------- the candidates


def fit_histgb(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray) -> np.ndarray:
    model = M._model()
    model.fit(x_train, y_train)
    return model.predict_proba(x_test)[:, 1]


def fit_random_forest(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray) -> np.ndarray:
    from sklearn.ensemble import RandomForestClassifier

    model = RandomForestClassifier(n_estimators=300, min_samples_leaf=5, class_weight="balanced_subsample",
                                   random_state=0, n_jobs=2)
    model.fit(x_train, y_train)
    return model.predict_proba(x_test)[:, 1]


def fit_logistic_spatial(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray) -> np.ndarray:
    """Logistic regression on standardised features plus quadratic terms of the last two columns (lon, lat)."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    def expand(x: np.ndarray) -> np.ndarray:
        lon, lat = x[:, -2], x[:, -1]
        return np.column_stack([x, lon * lon, lat * lat, lon * lat])

    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, class_weight="balanced", C=0.5))
    model.fit(expand(x_train), y_train)
    return model.predict_proba(expand(x_test))[:, 1]


def fit_bagging_pu(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray, bags: int = 10, ratio: int = 5,
                   seed: int = 0) -> np.ndarray:
    """Bagging PU (Mordelet and Vert): every positive with a fresh sample of unlabelled cells as negatives per
    bag, scores averaged. The unlabelled pool is never treated as one fixed negative set."""
    rng = np.random.default_rng(seed)
    pos = np.flatnonzero(y_train == 1)
    unl = np.flatnonzero(y_train == 0)
    take = min(len(unl), max(len(pos) * ratio, 50))
    out = np.zeros(len(x_test))
    for _ in range(bags):
        neg = rng.choice(unl, size=take, replace=False)
        idx = np.concatenate([pos, neg])
        model = M._model()
        model.fit(x_train[idx], y_train[idx])
        out += model.predict_proba(x_test)[:, 1]
    return out / bags


CANDIDATES: dict[str, tuple[str, Fit]] = {
    # name: (feature set, fit)
    "histgb": ("learned", fit_histgb),
    "random_forest": ("learned", fit_random_forest),
    "logistic_spatial": ("learned+xy", fit_logistic_spatial),
    "bagging_pu": ("learned", fit_bagging_pu),
    "criteria_prior": ("learned+criteria", fit_histgb),
    "effort": ("effort", fit_histgb),
}


# ---------------------------------------------------------------- folds at other block sizes


def spatial_fold(df: pd.DataFrame, km: float, n_splits: int = 5, seed: int = 0) -> M.Fold:
    """Whole blocks of the given size held out together, from the cell's projected coordinates."""
    size = km * 1000.0
    bx = np.floor(df["cx"].to_numpy() / size).astype(int)
    by = np.floor(df["cy"].to_numpy() / size).astype(int)
    blocks = bx * 100_000 + by
    uniq = np.unique(blocks)
    rng = np.random.default_rng(seed)
    assign = dict(zip(uniq, rng.integers(0, n_splits, size=len(uniq)), strict=True))
    return M.Fold(f"spatial{int(km)}", np.array([assign[b] for b in blocks]))


# ---------------------------------------------------------------- one evaluation


@dataclass(frozen=True)
class Arm:
    name: str          # the candidate or ablation name
    feature_set: str
    fit: Fit
    fold: str          # "spatial" | "camp" | "spatial20" ...
    positives: str = "all"
    matched: bool = True
    thinned: bool = True

    @property
    def key(self) -> str:
        return f"{self.name}.{self.positives}.{self.fold}"


def evaluate_arm(df: pd.DataFrame, arm: Arm, seed: int = 0, boot: int = H.BOOT) -> dict[str, Any]:
    y = H.labels(df, arm.positives)
    train_ok = H.training_mask(df, y, matched=arm.matched, thinned=arm.thinned, seed=seed)
    fold = spatial_fold(df, float(arm.fold[len("spatial"):]), seed=seed) if arm.fold.startswith("spatial") and arm.fold != "spatial" \
        else M.folds(df, arm.fold, seed=seed)
    p = H._oof(df, arm.feature_set, y, fold, arm.fit, train_ok=train_ok)
    ok = np.isfinite(p)
    y_ok, p_ok = y[ok], p[ok]
    y_dep = (df["label_tier"] == "deposit").to_numpy().astype(int)[ok]
    row: dict[str, Any] = {"arm": arm.key, "name": arm.name, "feature_set": arm.feature_set, "fold": arm.fold,
                           "positives": arm.positives, "matched": arm.matched, "thinned": arm.thinned,
                           "cells": int(len(df)), "train_cells": int(train_ok.sum()), "scored": int(ok.sum()),
                           "n_pos": int(y_ok.sum()), "n_deposits": int(y_dep.sum()),
                           "base_rate": float(y_ok.mean()) if len(y_ok) else float("nan"),
                           "features": list(M.FEATURE_SETS[arm.feature_set])}
    if y_ok.sum() == 0 or y_ok.sum() == len(y_ok):
        row["note"] = "no usable split"
        return row
    row["pr_auc"] = M.pr_auc(y_ok, p_ok)
    row["roc_auc"] = M.roc_auc(y_ok, p_ok)
    row.update(H.capture_at_budgets(y_dep, p_ok))
    row["pr_auc_ci"] = H.bootstrap_ci(y_ok, p_ok, M.pr_auc, n=boot, seed=seed)
    row["roc_auc_ci"] = H.bootstrap_ci(y_ok, p_ok, M.roc_auc, n=boot, seed=seed)
    return row


# ---------------------------------------------------------------- the programme


def arms(quick: bool = False) -> list[Arm]:
    folds = ("spatial",) if quick else ("spatial", "camp")
    out: list[Arm] = []
    names = ["histgb", "effort", "random_forest", "bagging_pu"] if quick else list(CANDIDATES)
    for name in names:
        fs, fit = CANDIDATES[name]
        for fold in folds:
            out.append(Arm(name, fs, fit, fold))
    if not quick:
        for group in GROUPS:
            out.append(Arm(f"ablation-{group}", f"learned-{group}", fit_histgb, "spatial"))
        out.append(Arm("learned+effort", "learned+effort", fit_histgb, "spatial"))
        for km in BLOCK_KM:
            if km != 30:
                out.append(Arm("histgb", "learned", fit_histgb, f"spatial{km}"))
                out.append(Arm("effort", "effort", fit_histgb, f"spatial{km}"))
    return out


def run(grid_id: str | None = None, seed: int = 0, boot: int = H.BOOT, quick: bool = False,
        log: Callable[[str], None] = print, write: bool = True, track: bool = True,
        df: pd.DataFrame | None = None) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    if df is None:
        frames = {fs: M.matrix(fs, grid_id) for fs in ("learned", "effort")}
        common = set(frames["learned"]["cell_id"]) & set(frames["effort"]["cell_id"])
        extra = [c for c in M.EFFORT_FEATURES if c not in frames["learned"].columns]
        df = frames["learned"].merge(frames["effort"][["cell_id", *extra]], on="cell_id") if extra else frames["learned"]
        df = df[df["cell_id"].isin(common)].reset_index(drop=True)
        df = with_criteria(df)
    register_feature_sets(df)
    log(f"  common complete cases: {len(df):,} cells, {(df['label_tier'] != 'unlabelled').sum()} positives")
    rows: list[dict[str, Any]] = []
    run_ids: dict[str, str] = {}
    for arm in arms(quick):
        r = evaluate_arm(df, arm, seed=seed, boot=boot)
        rows.append(r)
        if "pr_auc" in r:
            log(f"    {arm.key:<34} PR-AUC {r['pr_auc']:.3f} [{r['pr_auc_ci'][0]:.3f},{r['pr_auc_ci'][1]:.3f}]  "
                f"ROC {r['roc_auc']:.3f}  dep@10% {r['capture_top10']:.0%}")
            if track:
                run_ids[arm.key] = TR.log_run(
                    arm.key,
                    params={"model": arm.name, "feature_set": arm.feature_set, "fold": arm.fold, "positives": arm.positives,
                            "matched": arm.matched, "thinned": arm.thinned, "seed": seed, "features": r["features"]},
                    metrics={k: r[k] for k in ("pr_auc", "roc_auc", "capture_top5", "capture_top10", "capture_top15", "base_rate")},
                    tags={"phase": "3", "kind": "modelsearch"}, artifacts={"row.json": r})
                r["run_id"] = run_ids[arm.key]
        else:
            log(f"    {arm.key:<34} {r.get('note')}")
    decision = decide(rows, log=log, track=track)
    out = {"run_id": now, "rows": rows, "decision": decision, "cells": int(len(df))}
    if write:
        _write_metrics(rows, now)
    return out


def decide(rows: list[dict[str, Any]], log: Callable[[str], None] = print, track: bool = True) -> dict[str, Any]:
    """The promotion rule over the spatial-fold rows: the best candidate is validated only if it beats the null."""
    null = next((r for r in rows if r["name"] == "effort" and r["fold"] == "spatial" and "pr_auc" in r), None)
    cands = [r for r in rows if r["fold"] == "spatial" and r["name"] != "effort" and not r["name"].startswith("ablation")
             and "pr_auc" in r]
    if not null or not cands:
        return {"served": False, "reason": "no spatial-fold rows to decide on"}
    best = max(cands, key=lambda r: r["pr_auc"])
    validated = TR.beats_null(best, null)
    reason = (f"best candidate {best['name']} PR-AUC {best['pr_auc']:.3f} [{best['pr_auc_ci'][0]:.3f},{best['pr_auc_ci'][1]:.3f}] "
              f"against the effort null {null['pr_auc']:.3f} [{null['pr_auc_ci'][0]:.3f},{null['pr_auc_ci'][1]:.3f}] under spatial folds: "
              + ("validated" if validated else "not validated; nothing is served"))
    log("  decision: " + reason)
    decision = {"best": best["name"], "validated": validated, "served": False, "reason": reason}
    if track and best.get("run_id"):
        decision |= TR.record_decision(best["run_id"], best["name"], validated, reason)
    return decision


def _write_metrics(rows: list[dict[str, Any]], now: str) -> None:
    metrics = []
    for r in rows:
        for key in ("pr_auc", "roc_auc", "capture_top5", "capture_top10", "capture_top15", "base_rate"):
            if key in r and r[key] == r[key]:
                metrics.append({"metric_key": f"search.{r['arm']}.{key}", "run_id": r.get("run_id") or now, "value": float(r[key]),
                                "fmt": "ratio3", "computed_at": now,
                                "note": f"{key}; {r['name']} on {r['feature_set']} under {r['fold']} folds; matched={r['matched']}, "
                                        f"thinned={r['thinned']}; {r['n_pos']} positives in {r['scored']} scored cells"
                                        + (f"; 95% CI {r[key + '_ci'][0]:.3f}-{r[key + '_ci'][1]:.3f}" if key + "_ci" in r else "")})
    con = connect()
    try:
        con.execute("delete from derived.metric where metric_key like 'search.%'")
        append_frame(con, "derived", "metric", pd.DataFrame(metrics), "derived")
    finally:
        con.close()
