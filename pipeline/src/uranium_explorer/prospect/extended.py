"""The fitted model given more of the store's evidence.

The learned model reads ten features, one number per layer, complete in every cell it scores. The layers carry
more: thorium, loss on ignition, lead and nickel beside every lake-sediment uranium; a rock type on every
boulder; a trend on every fault; a tectonic domain on every bedrock polygon. `features` builds that into
features, and this module asks whether the fitted model gets better when it can read them.

It is the first step of a comparison between readers. Before an LLM is handed the raw evidence and compared
with the fitted model, the fitted model is handed the same evidence in the one form it can take, engineered
features, so that if the LLM later wins it wins on reading, not on having been shown more.

The protocol is the headline's, unchanged: spatial folds, matched background and thinned positives in
training only, every feature set scored out of fold on the same cells, intervals by bootstrap over cells, and
each set's PR-AUC compared with the ten-feature model's on the same cells as a paired difference. The new
features are missing wherever nothing was sampled, and the gradient-boosted trees send a missing value down
its own branch. That is how "where somebody sampled" can stand in for geology, so a model that sees nothing
but which values are missing is scored beside the others as the check.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..paths import PATHS
from ..store import append_frame, connect
from ..store import snapshot as SN
from . import headline as H
from . import models as M
from . import modelsearch as MS
from . import tracking as TR
from .features import DOMAIN_LEVELS, TRENDS, slug

TOOL = "prospect/extended"

#: the evidence families, each a group of features the learned model does not read. The first three of the
#: geochemistry family existed before this module and were left out of the learned model for their coverage.
FAMILIES: dict[str, tuple[str, ...]] = {
    "geochem": ("sed_u_max_ppm", "sed_u_max_ppm_sgs", "water_u_max_ppm", "sed_u_th_max", "sed_u_loi_max",
                "sed_pb_max_ppm", "sed_ni_max_ppm", "sed_u_anomaly_share"),
    "boulders": ("boulder_max_cps", "boulder_sst_max_cps", "boulder_downice_max_cps", "boulder_upice_max_cps"),
    "structure": (*(f"fault_len_{t}_km" for t in TRENDS), "fault_crossings_n", "fault_conductor_crossings_n"),
    "setting": ("d_basin_edge_m", "d_domain_boundary_m", "lith_groups_n",
                *(f"domain_{slug(level)}" for level in DOMAIN_LEVELS)),
}
EXTENDED_FEATURES: tuple[str, ...] = tuple(k for keys in FAMILIES.values() for k in keys)
MISSING_PREFIX = "missing:"

#: the sets scored, in the order the table prints them. `learned` is the reference every paired difference
#: is taken against; `effort` is the null; `missing-only` is the leakage check.
SETS = ("learned", "extended", *(f"learned+{f}" for f in FAMILIES), "missing-only", "effort")
POSITIVES = ("all", "deposits")


def register_feature_sets(df: pd.DataFrame) -> None:
    """The extended sets, registered on the models module so the shared fold and fit machinery sees them."""
    M.FEATURE_SETS["extended"] = (*M.LEARNED_FEATURES, *EXTENDED_FEATURES)
    for fam, keys in FAMILIES.items():
        M.FEATURE_SETS[f"learned+{fam}"] = (*M.LEARNED_FEATURES, *keys)
    M.FEATURE_SETS["missing-only"] = tuple(c for c in df.columns if c.startswith(MISSING_PREFIX))


def with_missing(df: pd.DataFrame) -> pd.DataFrame:
    """One 0/1 column per extended feature that is ever missing: 1 where it is. Nothing else about the cell."""
    out = df.copy()
    for k in EXTENDED_FEATURES:
        if k in out.columns and out[k].isna().any():
            out[f"{MISSING_PREFIX}{k}"] = out[k].isna().astype(float)
    return out


def load(grid_id: str | None = None, con: Any = None) -> pd.DataFrame:
    """The bench frame (learned features complete, effort features with nulls) plus the extended features with
    their nulls, the missingness columns, and every set registered."""
    from ..bench.frame import load_frame

    df = with_missing(load_frame(grid_id, con=con, extra=EXTENDED_FEATURES))
    register_feature_sets(df)
    return df


# ---------------------------------------------------------------- scoring


def oof(df: pd.DataFrame, feature_set: str, positives: str, fold: M.Fold, seed: int = 0) -> np.ndarray:
    """Out-of-fold probabilities for one set, trained under both corrections, with the histogram-gradient-boosted
    trees (the one learner here that takes a missing value without imputing it)."""
    y = H.labels(df, positives)
    train_ok = H.training_mask(df, y, matched=True, thinned=True, seed=seed)
    return H._oof(df, feature_set, y, fold, MS.fit_histgb, train_ok=train_ok)


def paired_difference(y: np.ndarray, p_a: np.ndarray, p_b: np.ndarray, stat: Callable[[np.ndarray, np.ndarray], float],
                      n: int = H.BOOT, seed: int = 0) -> dict[str, float]:
    """stat(a) minus stat(b) on the same cells, with a percentile interval from resampling cells (positives and
    negatives apart, as every interval here is) and the share of draws where a did not beat b."""
    ok = np.isfinite(p_a) & np.isfinite(p_b)
    y, p_a, p_b = y[ok], p_a[ok], p_b[ok]
    point = stat(y, p_a) - stat(y, p_b)
    rng = np.random.default_rng(seed)
    pos, neg = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    draws = []
    for _ in range(n):
        idx = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
        draws.append(stat(y[idx], p_a[idx]) - stat(y[idx], p_b[idx]))
    arr = np.asarray(draws, dtype=float)
    return {"diff": float(point), "diff_ci": [float(np.nanpercentile(arr, 2.5)), float(np.nanpercentile(arr, 97.5))],
            "p_not_better": float(np.mean(arr <= 0)), "cells": int(ok.sum())}


def _ranking_row(y: np.ndarray, p: np.ndarray, boot: int, seed: int) -> dict[str, Any]:
    ok = np.isfinite(p)
    y_ok, p_ok = y[ok], p[ok]
    row: dict[str, Any] = {"scored": int(ok.sum()), "n_pos": int(y_ok.sum()),
                           "base_rate": float(y_ok.mean()) if len(y_ok) else float("nan")}
    if y_ok.sum() == 0 or y_ok.sum() == len(y_ok):
        return row | {"note": "no usable split"}
    row["pr_auc"] = M.pr_auc(y_ok, p_ok)
    row["roc_auc"] = M.roc_auc(y_ok, p_ok)
    row["pr_auc_ci"] = H.bootstrap_ci(y_ok, p_ok, M.pr_auc, n=boot, seed=seed)
    row["roc_auc_ci"] = H.bootstrap_ci(y_ok, p_ok, M.roc_auc, n=boot, seed=seed)
    return row


def grid_rows(df: pd.DataFrame, seed: int = 0, boot: int = H.BOOT, log: Callable[[str], None] = print,
              sets: tuple[str, ...] = SETS) -> tuple[list[dict[str, Any]], dict[tuple[str, str], np.ndarray]]:
    """Every set on every positive set over the whole grid, the headline's way, with deposits captured at the
    fixed area budgets and each set's paired difference from `learned`."""
    if "learned" not in sets:
        raise ValueError("the learned model is the reference every paired difference is taken against")
    fold = M.folds(df, "spatial", seed=seed)
    y_dep_all = (df["label_tier"] == "deposit").to_numpy().astype(int)
    scores: dict[tuple[str, str], np.ndarray] = {}
    rows: list[dict[str, Any]] = []
    for positives in POSITIVES:
        y = H.labels(df, positives)
        for fs in sets:
            p = oof(df, fs, positives, fold, seed=seed)
            scores[(fs, positives)] = p
            row = {"scope": "grid", "feature_set": fs, "positives": positives, "fold": "spatial",
                   "features": list(M.FEATURE_SETS[fs]), "cells": int(len(df))} | _ranking_row(y, p, boot, seed)
            ok = np.isfinite(p)
            if "pr_auc" in row:
                row.update(H.capture_at_budgets(y_dep_all[ok], p[ok]))
            if fs != "learned" and "pr_auc" in row:
                row["vs_learned"] = paired_difference(y, p, scores[("learned", positives)], M.pr_auc, n=boot, seed=seed)
            rows.append(row)
            _log_row(row, log)
    return rows, scores


#: extra fold seeds for the spread on the benchmark cells: on 114 cells the draw of folds and background moves a
#: PR-AUC by several hundredths, so one seed's difference is read beside the others
SPREAD_SEEDS = (0, 1, 2, 3)


def bench_rows(df: pd.DataFrame, version: str, boot: int = H.BOOT, log: Callable[[str], None] = print,
               spread_seeds: tuple[int, ...] = SPREAD_SEEDS, sets: tuple[str, ...] = SETS) -> list[dict[str, Any]]:
    """Every set on the benchmark's open labelled cells, out of fold under the benchmark's own folds and seed,
    with paired differences from `learned` and from the effort null on the same cells.

    Each row also carries its spread over further fold seeds: the PR-AUC under each, and the PR-AUC of the
    probability averaged over all of them (the benchmark's seed included), with that average's paired
    difference from the learned model's average."""
    from ..analyst import frozen as FZ
    from ..analyst.score import LABEL_STRATA, POSITIVE_LABELS
    from ..bench.spec import load_spec

    spec = load_spec(version)
    bench = FZ.load_bench(version)
    cells = [c for c in bench.open_cells()
             if (bench.key.get(c["bench_id"], {}).get("stratum") or "") in LABEL_STRATA]
    y_by_cell = {str(c["cell_id"]): int(bench.key[c["bench_id"]].get("label") in POSITIVE_LABELS) for c in cells}
    fold = MS.spatial_fold(df, spec.fold_km, n_splits=spec.n_folds, seed=spec.seed)
    at = {c: i for i, c in enumerate(df["cell_id"].astype(str))}
    ids = [c for c in y_by_cell if c in at]
    missing = sorted(set(y_by_cell) - set(ids))
    rows_idx = np.array([at[c] for c in ids], dtype=int)
    y = np.array([y_by_cell[c] for c in ids], dtype=int)
    if "learned" not in sets:
        raise ValueError("the learned model is the reference every paired difference is taken against")
    scores: dict[str, np.ndarray] = {}
    rows = []
    for fs in sets:
        p = oof(df, fs, "all", fold, seed=spec.seed)[rows_idx]
        scores[fs] = p
        row = {"scope": f"bench:{version}", "feature_set": fs, "positives": "all", "fold": f"spatial{int(spec.fold_km)}",
               "features": list(M.FEATURE_SETS[fs]), "cells": int(len(y)), "not_in_frame": missing,
               "seed": spec.seed} | _ranking_row(y, p, boot, spec.seed)
        if fs != "learned" and "pr_auc" in row:
            row["vs_learned"] = paired_difference(y, p, scores["learned"], M.pr_auc, n=boot, seed=spec.seed)
        rows.append(row)
        _log_row(row, log)
    for row in rows:
        if row["feature_set"] != "effort" and "effort" in scores and "pr_auc" in row:
            row["vs_effort"] = paired_difference(y, scores[row["feature_set"]], scores["effort"], M.pr_auc, n=boot,
                                                 seed=spec.seed)
    if spread_seeds:
        seeds = (spec.seed, *[s for s in spread_seeds if s != spec.seed])
        per: dict[str, list[np.ndarray]] = {fs: [scores[fs]] for fs in sets}
        for s in seeds[1:]:
            fold_s = MS.spatial_fold(df, spec.fold_km, n_splits=spec.n_folds, seed=s)
            for fs in sets:
                per[fs].append(oof(df, fs, "all", fold_s, seed=s)[rows_idx])
        mean = {fs: np.nanmean(np.vstack(v), axis=0) for fs, v in per.items()}
        for row in rows:
            fs = row["feature_set"]
            vals = [M.pr_auc(y[np.isfinite(p)], p[np.isfinite(p)]) for p in per[fs]]
            ok = np.isfinite(mean[fs])
            row["seed_spread"] = {"seeds": list(seeds), "pr_auc": vals, "mean": float(np.mean(vals)),
                                  "sd": float(np.std(vals, ddof=1)), "averaged_pr_auc": M.pr_auc(y[ok], mean[fs][ok])}
            if fs != "learned":
                row["seed_spread"]["averaged_vs_learned"] = paired_difference(y, mean[fs], mean["learned"], M.pr_auc,
                                                                              n=boot, seed=spec.seed)
        log("    seed spread on the benchmark cells (PR-AUC mean ± sd over "
            f"{len(seeds)} fold seeds; seed-averaged score): "
            + "; ".join(f"{r['feature_set']} {r['seed_spread']['mean']:.3f}±{r['seed_spread']['sd']:.3f} "
                        f"({r['seed_spread']['averaged_pr_auc']:.3f})" for r in rows))
    return rows


def reconcile_effort(version: str, con: Any = None) -> dict[str, Any]:
    """The effort null's PR-AUC on the benchmark's open labelled cells from each place it is stored: the store's
    out-of-fold table, which `ue bench oof-scores --write` fills under its `--seed` (0 unless given), and the
    benchmark's own CSV, which `ue bench build` writes under the spec's seed. The same model under two draws of
    folds and background: their gap is seed noise, and the run ids say which draw each is."""
    from ..analyst import frozen as FZ
    from ..analyst.score import LABEL_STRATA, POSITIVE_LABELS

    bench = FZ.load_bench(version)
    cells = [c for c in bench.open_cells()
             if (bench.key.get(c["bench_id"], {}).get("stratum") or "") in LABEL_STRATA]
    y = {str(c["cell_id"]): int(bench.key[c["bench_id"]].get("label") in POSITIVE_LABELS) for c in cells}
    bid = {str(c["bench_id"]): str(c["cell_id"]) for c in cells}
    out: dict[str, Any] = {"cells": len(y)}
    own = con is None
    con = con or connect(read_only=True)
    try:
        st = con.execute("select cell_id, count(*) n, avg(score) s, min(run_id) run_id from derived.cell_score_oof "
                         "where model = 'effort' and fold_kind = 'spatial' group by 1").df()
    finally:
        if own:
            con.close()
    st = st[st["cell_id"].astype(str).isin(y)]
    if len(st):
        yy = np.array([y[str(c)] for c in st["cell_id"]])
        out["store"] = {"pr_auc": M.pr_auc(yy, st["s"].to_numpy()), "cells": int(len(st)),
                        "runs_per_cell": sorted({int(n) for n in st["n"]}),
                        "run_ids": sorted({str(r) for r in st["run_id"]})}
    csv = PATHS.data / "bench" / version / "oof_scores.csv"
    if csv.is_file():
        b = pd.read_csv(csv)
        b = b[(b["model"] == "effort") & b["bench_id"].astype(str).isin(bid)]
        b = b.assign(cell_id=b["bench_id"].astype(str).map(bid)).dropna(subset=["score"])
        yy = np.array([y[c] for c in b["cell_id"]])
        out["bench_csv"] = {"pr_auc": M.pr_auc(yy, b["score"].to_numpy()), "cells": int(len(b)),
                            "missing_as_half": M.pr_auc(np.array(list(y.values())),
                                                        np.array([b.set_index("cell_id")["score"].get(c, 0.5)
                                                                  for c in y]))}
    return out


def _log_row(row: dict[str, Any], log: Callable[[str], None]) -> None:
    head = f"    {row['scope']:<9} {row['feature_set']:<18} {row['positives']:<8}"
    if "pr_auc" not in row:
        log(f"{head} {row.get('note')}")
        return
    diff = row.get("vs_learned")
    d = f"  vs learned {diff['diff']:+.3f} [{diff['diff_ci'][0]:+.3f},{diff['diff_ci'][1]:+.3f}]" if diff else ""
    log(f"{head} PR-AUC {row['pr_auc']:.3f} [{row['pr_auc_ci'][0]:.3f},{row['pr_auc_ci'][1]:.3f}]  "
        f"ROC {row['roc_auc']:.3f}  n={row['scored']} pos={row['n_pos']}{d}")


# ---------------------------------------------------------------- the run


def run(grid_id: str | None = None, seed: int = 0, boot: int = H.BOOT, version: str = "v2",
        log: Callable[[str], None] = print, write: bool = True, track: bool = True,
        snapshot: str | None = None) -> dict[str, Any]:
    """Every set over the grid and on the benchmark cells, the effort null reconciled, and the store's metrics.
    `snapshot` pins the run to a taken snapshot and refuses any other store."""
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    pinned = SN.pin(snapshot, log=log)
    df = load(grid_id)
    log(f"  frame: {len(df):,} cells, {(df['label_tier'] != 'unlabelled').sum()} positives, "
        f"{len(EXTENDED_FEATURES)} extended features, {len(M.FEATURE_SETS['missing-only'])} missingness columns")
    coverage = {k: round(float(df[k].notna().mean()), 3) for k in EXTENDED_FEATURES if k in df.columns}
    grid, _ = grid_rows(df, seed=seed, boot=boot, log=log)
    bench = bench_rows(df, version, boot=boot, log=log)
    effort = reconcile_effort(version)
    log(f"  effort null on the {effort['cells']} benchmark cells: "
        + ", ".join(f"{k} {v['pr_auc']:.3f}" for k, v in effort.items() if isinstance(v, dict)))
    out: dict[str, Any] = {"run_id": now, "tool": TOOL, "seed": seed, "boot": boot, "version": version,
                           "store_sha256": pinned["store_sha256"], "snapshot": pinned["snapshot"],
                           "cells": int(len(df)), "families": {k: list(v) for k, v in FAMILIES.items()},
                           "coverage": coverage, "grid": grid, "bench": bench, "effort_reconciled": effort}
    if write:
        TR.write_json("extended.json", out)   # first, so a tracking failure cannot lose the run
    if track:
        out["mlflow_run_id"] = TR.log_run(
            "extended", params={"seed": seed, "boot": boot, "version": version, "cells": out["cells"],
                                "store_sha256": pinned["store_sha256"]},
            metrics={metric_name(r) + ".pr_auc": r["pr_auc"] for r in grid + bench if "pr_auc" in r},
            tags={"kind": "extended", "snapshot": pinned["snapshot"]}, artifacts={"extended.json": out})
    if write:
        _write_metrics(grid + bench, out.get("mlflow_run_id") or now, now)
        TR.write_json("extended.json", out)
    return out


def metric_name(row: dict[str, Any]) -> str:
    """`<scope>.<set>.<positives>` in the characters a metric name may hold: MLflow refuses a `+`."""
    return f"{row['scope'].split(':')[0]}.{row['feature_set'].replace('+', '_plus_')}.{row['positives']}"


def _write_metrics(rows: list[dict[str, Any]], run_id: str, now: str) -> None:
    metrics = []
    for r in rows:
        scope = r["scope"].replace(":", ".")
        for key in ("pr_auc", "roc_auc", "capture_top10"):
            if key in r and r[key] == r[key]:
                ci = r.get(f"{key}_ci")
                metrics.append({
                    "metric_key": f"extended.{scope}.{r['feature_set']}.{r['positives']}.{key}", "run_id": run_id,
                    "value": float(r[key]), "fmt": "ratio3", "computed_at": now,
                    "note": f"{key}; {r['feature_set']} ({len(r['features'])} features) under {r['fold']} folds, "
                            f"matched and thinned in training; {r['n_pos']} positives in {r['scored']} scored cells"
                            + (f"; 95% CI {ci[0]:.3f}-{ci[1]:.3f}" if ci else "")})
        diff = r.get("vs_learned")
        if diff:
            metrics.append({
                "metric_key": f"extended.{scope}.{r['feature_set']}.{r['positives']}.pr_auc_vs_learned",
                "run_id": run_id, "value": diff["diff"], "fmt": "ratio3", "computed_at": now,
                "note": f"PR-AUC of {r['feature_set']} minus the learned model's on the same {diff['cells']} cells; "
                        f"95% CI {diff['diff_ci'][0]:+.3f} to {diff['diff_ci'][1]:+.3f}"})
    con = connect()
    try:
        con.execute("delete from derived.metric where metric_key like 'extended.%'")
        append_frame(con, "derived", "metric", pd.DataFrame(metrics), "derived")
    finally:
        con.close()


def out_path() -> Path:
    return TR.OUT_DIR / "extended.json"
