"""The learned score and the exploration-effort null model, scored the way the literature says to.

The comparison is the point. Two models are fitted on the same cells, the same labels and the same folds:

* **learned** - geological features only: distance to a conductor, to a fault, the mapped host, the
  unconformity depth, terrain and land cover.
* **effort** - where people looked: holes nearby, when drilling started, how many samples were taken, how many
  survey footprints cover the cell. No geology at all.

If the geological model cannot beat "where people already drilled" once folds are spatial, that is the finding,
and it is the one this whole build exists to be able to state with a number rather than an opinion.

Three rules, each from a published failure:

* **Unlabelled is not barren.** Nobody drilled most of this basin. Unlabelled cells are treated as negatives
  for fitting because something must be, and every metric is reported as positive-unlabelled, never as accuracy.
* **Folds are spatial and committed first.** Nearby cells share geology; a random split leaks near-copies and
  inflates every number. Both a 30 km block split and a leave-one-camp-out split are reported, beside the
  random split, so the gap is visible.
* **No score outside the data it was fitted on.** An area-of-applicability mask marks cells whose features sit
  far from anything in training; their score is published as null, because cross-validation error does not
  apply there.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..store import append_frame, connect

TOOL = "prospect/models"

#: geological features with enough coverage to fit on complete cases; the thin ones are named in the readiness
#: scorecard and deliberately left out rather than imputed
LEARNED_FEATURES = (
    "d_conductor_m", "d_fault_m", "fault_density", "graphitic_host", "unconformity_depth_m",
    "water_fraction", "vegetation_fraction", "bare_fraction", "elevation_m", "relief_m",
)
EFFORT_FEATURES = (
    "holes_n", "holes_first_year", "sed_samples_n", "boulder_samples_n",
    "airborne_surveys_n", "ground_surveys_n",
)
FEATURE_SETS: dict[str, tuple[str, ...]] = {"learned": LEARNED_FEATURES, "effort": EFFORT_FEATURES}

#: a cell this far (in standardised feature space) from its nearest training cell is outside the model's range
AOA_QUANTILE = 0.95
AOA_FACTOR = 2.0

#: how far around a camp is held out with it, in degrees (about 25 km at this latitude)
CAMP_HOLDOUT_DEG = 0.35


@dataclass(frozen=True)
class Fold:
    name: str
    groups: np.ndarray  # one group id per row; a fold holds out whole groups


def matrix(feature_set: str, grid_id: str | None = None) -> pd.DataFrame:
    """Complete cases only: one row per cell that has every feature in the set, plus label and folds.

    Imputing a missing feature would quietly turn "nobody measured here" into a number, and for the effort
    model it would erase exactly the signal being measured. A cell that cannot be scored is left unscored.
    """
    keys = FEATURE_SETS[feature_set]
    con = connect(read_only=True)
    try:
        if grid_id is None:
            row = con.execute("select grid_id from derived.grid order by built_at desc limit 1").fetchone()
            grid_id = row[0] if row else None
        placeholders = ",".join("?" * len(keys))
        wide = con.execute(
            f"select cell_id, feature_key, value from derived.cell_feature "
            f"where feature_key in ({placeholders})", list(keys)
        ).df().pivot_table(index="cell_id", columns="feature_key", values="value", dropna=False)
        labels = con.execute(
            "select l.cell_id, l.label_tier, l.camp_id, l.block_id, c.lon, c.lat "
            "from derived.cell_label l join derived.cell c using (cell_id) where c.grid_id = ?", [grid_id]
        ).df()
    finally:
        con.close()
    df = labels.merge(wide.reset_index(), on="cell_id", how="inner")
    return df.dropna(subset=list(keys)).reset_index(drop=True)


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(
        max_depth=4, max_iter=200, learning_rate=0.08, l2_regularization=1.0,
        class_weight="balanced", random_state=0,
    )


def folds(df: pd.DataFrame, kind: str, n_splits: int = 5, seed: int = 0) -> Fold:
    """Group assignment for cross-validation, from position alone and before any fit."""
    if kind == "random":
        rng = np.random.default_rng(seed)
        return Fold("random", rng.integers(0, n_splits, size=len(df)))
    if kind == "spatial":
        # whole 30 km blocks are held out together, so a test cell never sits beside its own training data
        blocks = df["block_id"].to_numpy()
        uniq = np.unique(blocks)
        rng = np.random.default_rng(seed)
        assign = dict(zip(uniq, rng.integers(0, n_splits, size=len(uniq)), strict=True))
        return Fold("spatial", np.array([assign[b] for b in blocks]))
    if kind == "camp":
        # Leave one camp out, and with it the ground around it. Holding out only the positive cells would
        # leave a test set with no negatives in it, which scores nothing; the fold has to be a region.
        from sklearn.neighbors import KDTree

        xy = np.column_stack([df["lon"].to_numpy(), df["lat"].to_numpy()])
        camps = df["camp_id"].to_numpy()
        groups = np.full(len(df), -1, dtype=int)
        for i, camp in enumerate(sorted({int(c) for c in camps if c >= 0})):
            members = xy[camps == camp]
            if len(members) == 0:
                continue
            tree = KDTree(members)
            # about 25 km at this latitude, in degrees: the fold is the camp and its surroundings
            d, _ = tree.query(xy, k=1)
            near = d[:, 0] <= CAMP_HOLDOUT_DEG
            groups[near & (groups < 0)] = i
        return Fold("camp", groups)
    raise ValueError(f"unknown fold kind {kind!r}")


def cross_val_scores(df: pd.DataFrame, feature_set: str, fold: Fold) -> np.ndarray:
    """Out-of-fold probabilities. A cell is only ever scored by a model that never saw its fold."""
    keys = list(FEATURE_SETS[feature_set])
    x = df[keys].to_numpy(dtype=float)
    y = (df["label_tier"] != "unlabelled").to_numpy().astype(int)
    out = np.full(len(df), np.nan)
    for g in np.unique(fold.groups):
        if g < 0:  # cells outside every hold-out group (unlabelled ground in a camp split) train only
            continue
        test = fold.groups == g
        train = ~test
        if y[train].sum() == 0 or y[test].sum() == 0:
            continue  # a fold with no positive on one side cannot be scored
        model = _model()
        model.fit(x[train], y[train])
        out[test] = model.predict_proba(x[test])[:, 1]
    return out


def area_of_applicability(df: pd.DataFrame, feature_set: str, train_mask: np.ndarray) -> np.ndarray:
    """True where a cell resembles the training data closely enough for the error estimate to mean anything."""
    from sklearn.neighbors import KDTree

    keys = list(FEATURE_SETS[feature_set])
    x = df[keys].to_numpy(dtype=float)
    mu, sd = x[train_mask].mean(axis=0), x[train_mask].std(axis=0)
    z = (x - mu) / np.where(sd > 0, sd, 1.0)
    tree = KDTree(z[train_mask])
    d, _ = tree.query(z, k=1)
    threshold = float(np.quantile(d[train_mask], AOA_QUANTILE)) * AOA_FACTOR
    return (d[:, 0] <= threshold)


# ---------------------------------------------------------------- metrics


def pr_auc(y: np.ndarray, p: np.ndarray) -> float:
    from sklearn.metrics import average_precision_score

    return float(average_precision_score(y, p))


def roc_auc(y: np.ndarray, p: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, p))


def capture_at(y: np.ndarray, p: np.ndarray, area: float = 0.10) -> float:
    """Share of positives falling in the top `area` of ranked ground: the number an explorer actually feels."""
    n = max(int(round(len(p) * area)), 1)
    order = np.argsort(-p)
    return float(y[order[:n]].sum() / max(y.sum(), 1))


def calibration(y: np.ndarray, p: np.ndarray, bins: int = 5) -> list[dict[str, float]]:
    edges = np.quantile(p, np.linspace(0, 1, bins + 1))
    out = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        m = (p >= lo) & (p <= hi if i == bins - 1 else p < hi)
        if m.sum() == 0:
            continue
        out.append({"bin": i, "predicted": float(p[m].mean()), "observed": float(y[m].mean()),
                    "n": int(m.sum())})
    return out


def evaluate(df: pd.DataFrame, feature_set: str, fold_kind: str) -> dict[str, Any]:
    fold = folds(df, fold_kind)
    p = cross_val_scores(df, feature_set, fold)
    scored = np.isfinite(p)
    y = (df["label_tier"] != "unlabelled").to_numpy().astype(int)[scored]
    ps = p[scored]
    if y.sum() == 0 or y.sum() == len(y):
        return {"feature_set": feature_set, "fold": fold_kind, "scored": int(scored.sum()),
                "positives": int(y.sum()), "note": "no usable split"}
    return {
        "feature_set": feature_set, "fold": fold_kind, "scored": int(scored.sum()),
        "positives": int(y.sum()),
        "pr_auc": round(pr_auc(y, ps), 4),
        "roc_auc": round(roc_auc(y, ps), 4),
        "capture_top10": round(capture_at(y, ps, 0.10), 4),
        "base_rate": round(float(y.mean()), 5),
        "calibration": calibration(y, ps),
    }


def evaluate_ranking(scores: np.ndarray, y: np.ndarray, name: str) -> dict[str, Any]:
    """Score a ranking that was not fitted to the labels, so it can stand beside the fitted models.

    The criteria score needs no cross-validation because nothing was learned from the labels. That does not
    make it unbiased: its depth window was drawn from deposits that were found, which is survival bias by
    another route. Reported so the comparison is possible, with that caveat attached.
    """
    ok = np.isfinite(scores)
    if y[ok].sum() == 0:
        return {"feature_set": name, "fold": "none", "note": "no positives to score against"}
    return {
        "feature_set": name, "fold": "none", "scored": int(ok.sum()), "positives": int(y[ok].sum()),
        "pr_auc": round(pr_auc(y[ok], scores[ok]), 4),
        "roc_auc": round(roc_auc(y[ok], scores[ok]), 4),
        "capture_top10": round(capture_at(y[ok], scores[ok], 0.10), 4),
        "base_rate": round(float(y[ok].mean()), 5),
        "caveat": "not fitted to the labels, so no fold is needed; but its depth window was drawn from "
                  "deposits that were found",
    }


def criteria_ranking(cell_ids: list[str]) -> np.ndarray:
    """The stored criteria score for the given cells, aligned to their order."""
    con = connect(read_only=True)
    try:
        rows = dict(con.execute(
            "select cell_id, score from derived.cell_score where model = 'criteria'"
        ).fetchall())
    finally:
        con.close()
    return np.array([rows.get(c, np.nan) if rows.get(c) is not None else np.nan for c in cell_ids],
                    dtype=float)


def fit_full(df: pd.DataFrame, feature_set: str) -> tuple[np.ndarray, np.ndarray]:
    """Fit on everything and predict every cell, with the applicability mask beside it."""
    keys = list(FEATURE_SETS[feature_set])
    x = df[keys].to_numpy(dtype=float)
    y = (df["label_tier"] != "unlabelled").to_numpy().astype(int)
    model = _model()
    model.fit(x, y)
    return model.predict_proba(x)[:, 1], area_of_applicability(df, feature_set, np.ones(len(df), dtype=bool))


def run(grid_id: str | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Fit both models, score every cell, and record the comparison that matters."""
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    results: list[dict[str, Any]] = []
    rows: list[pd.DataFrame] = []

    # The comparison is only fair on the same ground: PR-AUC moves with the base rate, so two models scored on
    # different cell sets cannot be read against each other. Both are evaluated on the cells where every
    # feature of both sets is present.
    frames = {fs: matrix(fs, grid_id) for fs in ("learned", "effort")}
    common = set(frames["learned"]["cell_id"]) & set(frames["effort"]["cell_id"])
    log(f"  complete cases: learned {len(frames['learned']):,}, effort {len(frames['effort']):,}, "
        f"both {len(common):,} (the comparison runs on these)")

    for feature_set in ("learned", "effort"):
        df = frames[feature_set]
        df = df[df["cell_id"].isin(common)].reset_index(drop=True)
        y = (df["label_tier"] != "unlabelled").sum()
        log(f"  {feature_set}: {len(df):,} cells, {y} positive")
        for kind in ("random", "spatial", "camp"):
            r = evaluate(df, feature_set, kind)
            results.append(r)
            if "pr_auc" in r:
                log(f"    {kind:8} PR-AUC {r['pr_auc']:.4f}  ROC-AUC {r['roc_auc']:.4f}  "
                    f"capture@10% {r['capture_top10']:.0%}  (base rate {r['base_rate']:.4f})")
            else:
                log(f"    {kind:8} {r.get('note')}")
        # the published per-cell score uses every cell the set can answer, not just the shared ones
        full = frames[feature_set]
        p, in_aoa = fit_full(full, feature_set)
        rows.append(pd.DataFrame({
            "cell_id": full["cell_id"], "model": feature_set, "score": p,
            "known_share": 1.0, "in_aoa": in_aoa, "computed_at": now,
            "params": json.dumps({
                "features": list(FEATURE_SETS[feature_set]),
                "model": "HistGradientBoostingClassifier",
                "labels": "positive-unlabelled; unlabelled treated as negative",
                "aoa_note": "fitted on every cell it scores, so the applicability mask is not a filter here; "
                            "it exists for predicting onto ground the fit never saw",
            }),
        }))
        outside = int((~in_aoa).sum())
        log(f"    outside the area of applicability: {outside:,} cells ({(~in_aoa).mean():.1%})"
            + ("" if outside else "  (the published model is fitted on every cell it scores, so the mask "
                                  "only bites when predicting onto ground the fit never saw)"))

    # put the knowledge-driven score on the same footing as the fitted models, on the same cells
    base = frames["learned"][frames["learned"]["cell_id"].isin(common)].reset_index(drop=True)
    y_base = (base["label_tier"] != "unlabelled").to_numpy().astype(int)
    crit = evaluate_ranking(criteria_ranking(list(base["cell_id"])), y_base, "criteria")
    results.append(crit)
    if "pr_auc" in crit:
        log(f"  criteria: PR-AUC {crit['pr_auc']:.4f}  ROC-AUC {crit['roc_auc']:.4f}  "
            f"capture@10% {crit['capture_top10']:.0%}  (not fitted to the labels; no fold needed)")

    con = connect()
    try:
        con.execute("delete from derived.cell_score where model in ('learned', 'effort')")
        append_frame(con, "derived", "cell_score", pd.concat(rows, ignore_index=True), "derived")
        metrics = []
        for r in results:
            for key in ("pr_auc", "roc_auc", "capture_top10", "base_rate"):
                if key in r:
                    metrics.append({
                        "metric_key": f"{r['feature_set']}.{r['fold']}.{key}", "run_id": now,
                        "value": float(r[key]), "fmt": "ratio3",
                        "note": f"{key} for the {r['feature_set']} model under {r['fold']} folds, "
                                f"{r['positives']} positives in {r['scored']} scored cells",
                        "computed_at": now,
                    })
        append_frame(con, "derived", "metric", pd.DataFrame(metrics), "derived")
    finally:
        con.close()

    spatial = {r["feature_set"]: r for r in results if r["fold"] == "spatial" and "pr_auc" in r}
    if len(spatial) == 2:
        gap = spatial["learned"]["pr_auc"] - spatial["effort"]["pr_auc"]
        log(f"  under spatial folds the geological model's PR-AUC is {gap:+.4f} against the effort model "
            f"({spatial['learned']['pr_auc']:.4f} vs {spatial['effort']['pr_auc']:.4f})")
    return {"results": results, "run_id": now}
