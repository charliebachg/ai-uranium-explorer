"""Out-of-fold scores for every scorable cell: the baselines the benchmark is read against.

The served `derived.cell_score` rows are fitted on every cell they score, which is right for a map and wrong
for a benchmark: a baseline that has seen the benchmark cell's label is not a baseline. So the learned and the
effort model are refitted here exactly the way the model search judges them — matched background, thinned
positives, whole 30 km blocks held out — and each cell is scored by the fold model that never saw it. The
criteria score is not fitted to the labels, so it needs no fold; it is carried through from the store and
stamped with the same fold number so the three sit in one table.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..prospect import headline as H
from ..prospect import modelsearch as MS
from ..store import append_frame
from .frame import load_frame

TABLE = ("derived", "cell_score_oof")
MODELS = ("learned", "effort", "criteria")
#: the learned model given the extended evidence, scored beside them when a benchmark asks for it
EXTENDED_MODEL = "extended"
FOLD_KIND = "spatial"
COLUMNS = ("cell_id", "model", "fold_kind", "fold", "score")

Fit = Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]


def oof_scores(seed: int, grid_id: str | None = None, df: pd.DataFrame | None = None, con: Any = None,
               fold_km: float = 30.0, n_folds: int = 5, fit: Fit | None = None, extended: bool = False) -> pd.DataFrame:
    """One row per cell per model: `cell_id, model, fold_kind, fold, score`.

    `learned` and `effort` come from `fit_histgb` under the headline corrections and `spatial_fold(fold_km)`;
    `criteria` is the stored score. With `extended`, the learned model given the extended evidence
    (`prospect.extended`) is scored the same way beside them. A fold whose test side has no positive cannot be
    scored and leaves NaN."""
    from ..prospect import extended as X

    if df is None:
        df = load_frame(grid_id, con=con, extra=X.EXTENDED_FEATURES if extended else ())
    MS.register_feature_sets(df)
    if extended:
        X.register_feature_sets(df)
    y = H.labels(df, "all")
    train_ok = H.training_mask(df, y, matched=True, thinned=True, seed=seed)
    fold = MS.spatial_fold(df, fold_km, n_splits=n_folds, seed=seed)
    frames = []
    for model in ("learned", "effort", *((EXTENDED_MODEL,) if extended else ())):
        p = H._oof(df, model, y, fold, fit or MS.fit_histgb, train_ok=train_ok)
        frames.append(_rows(df, model, fold, p))
    crit = df["criteria_score"].to_numpy(dtype=float) if "criteria_score" in df.columns else np.full(len(df), np.nan)
    frames.append(_rows(df, "criteria", fold, crit))
    return pd.concat(frames, ignore_index=True)


def _rows(df: pd.DataFrame, model: str, fold: Any, score: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({"cell_id": df["cell_id"].to_numpy(), "model": model, "fold_kind": FOLD_KIND,
                         "fold": fold.groups.astype(int), "score": np.asarray(score, dtype=float)})


def write_oof_scores(df: pd.DataFrame, con: Any, run_id: str | None = None) -> int:
    """Replace `derived.cell_score_oof` for the models in `df`, stamping run and time. Returns rows written."""
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    out = df[list(COLUMNS)].copy()
    out["run_id"] = run_id or now
    out["computed_at"] = now
    models = sorted(out["model"].unique())
    placeholders = ",".join("?" * len(models))
    con.execute(f"delete from derived.cell_score_oof where fold_kind = ? and model in ({placeholders})",
                [FOLD_KIND, *models])
    return append_frame(con, *TABLE, out, "derived")


def read_oof_scores(con: Any, cell_ids: list[str] | None = None) -> pd.DataFrame:
    """The stored table, or the rows for the given cells."""
    if cell_ids:
        placeholders = ",".join("?" * len(cell_ids))
        return con.execute(f"select cell_id, model, fold_kind, fold, score from derived.cell_score_oof "
                           f"where cell_id in ({placeholders}) order by cell_id, model", cell_ids).df()
    return con.execute("select cell_id, model, fold_kind, fold, score from derived.cell_score_oof "
                       "order by cell_id, model").df()
