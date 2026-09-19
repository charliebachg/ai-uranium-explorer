"""The dated hindcast: prediction before the claim (PRD C.2.3).

Every other test in this project asks whether a model separates known deposits from the rest of the grid. This
one asks the question a company would: had the model been run in year C, where would it have ranked the ground
where the deposits found *after* C turned out to be? The datable inputs are frozen at C — the labels drop every
discovery made after it, and the drilling features drop every cell first drilled after it — the grid is
scored, and each later discovery's cell is reported as a share of the basin's area that scored at least as
well. A rank in the top 5% is a target a programme would have drilled; a rank at 60% is a coin toss.

What cannot be frozen is stated on every row: the geological layers (conductors, faults, geochemistry) are
compilations as they stand today, survey footprints carry no year, and the discovery years come from a
hand-built table with a confidence per entry. This is the honest version of the test, not a clean one.
"""

from __future__ import annotations

import datetime as dt
import json
import tomllib
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..paths import PATHS
from ..store import append_frame, connect
from . import headline as H
from . import models as M
from . import tracking as TR

TOOL = "prospect/hindcast"
DISCOVERIES = PATHS.pipeline / "knowledge" / "discoveries.toml"
CUTOFFS = (2000, 2010)
CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


def load_discoveries(path: Path | None = None) -> list[dict[str, Any]]:
    raw = tomllib.loads((path or DISCOVERIES).read_text())
    return list(raw["discovery"])


def map_to_cells(discoveries: list[dict[str, Any]], labels: pd.DataFrame) -> list[dict[str, Any]]:
    """Each discovery with the cell ids that carry its label name; a name with no cell is kept and flagged."""
    out = []
    for d in discoveries:
        cells = labels.loc[labels["label_name"] == d["label_name"], "cell_id"].tolist()
        out.append({**d, "cells": cells})
    return out


def frozen_labels(df: pd.DataFrame, dated: list[dict[str, Any]], cutoff: int) -> tuple[np.ndarray, np.ndarray]:
    """Training labels as they would have stood in `cutoff`, and the mask of cells that may be trained on.

    Positives: deposit cells whose discovery year is at or before the cutoff. Deposit cells with no dated
    entry, and every cell of a later discovery, are masked out of training: they are neither a known deposit
    nor known ground. Occurrences carry no date and are treated as unlabelled, which is a stated simplification."""
    year_by_cell: dict[str, int] = {}
    for d in dated:
        for c in d["cells"]:
            year_by_cell[c] = min(d["year"], year_by_cell.get(c, 10_000))
    cell_ids = df["cell_id"].to_numpy()
    is_dep = (df["label_tier"] == "deposit").to_numpy()
    years = np.array([year_by_cell.get(c, -1) for c in cell_ids])
    y = np.where(is_dep & (years > 0) & (years <= cutoff), 1, 0)
    train_ok = ~(is_dep & ((years < 0) | (years > cutoff)))
    return y, train_ok


def frozen_effort(df: pd.DataFrame, cutoff: int) -> pd.DataFrame:
    """Drilling features as they stood at the cutoff: a cell first drilled after it had no holes then.
    Holes drilled after the cutoff in a cell first drilled before it cannot be removed from the count; stated."""
    out = df.copy()
    later = out["holes_first_year"].to_numpy() > cutoff
    out.loc[later, "holes_n"] = 0.0
    out.loc[later, "holes_first_year"] = np.nan
    out["holes_first_year"] = out["holes_first_year"].fillna(float(cutoff))
    return out


def share_at_least(scores: np.ndarray, value: float, in_basin: np.ndarray) -> float:
    """The share of basin cells scoring at least `value`: the area a programme ranking by this score would have
    had to cover before reaching the discovery. Smaller is better."""
    s = scores[in_basin]
    s = s[np.isfinite(s)]
    return float((s >= value).mean()) if len(s) else float("nan")


def run(cutoffs: tuple[int, ...] = CUTOFFS, min_confidence: str = "medium", grid_id: str | None = None,
        log: Callable[[str], None] = print, write: bool = True, track: bool = True,
        fit: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray] | None = None,
        df: pd.DataFrame | None = None, discoveries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    if df is None:
        frames = {fs: M.matrix(fs, grid_id) for fs in ("learned", "effort")}
        common = set(frames["learned"]["cell_id"]) & set(frames["effort"]["cell_id"])
        extra = [c for c in M.EFFORT_FEATURES if c not in frames["learned"].columns]
        df = frames["learned"].merge(frames["effort"][["cell_id", *extra]], on="cell_id") if extra else frames["learned"]
        df = df[df["cell_id"].isin(common)].reset_index(drop=True)
        con = connect(read_only=True)
        try:
            df = df.merge(con.execute("select cell_id, label_name from derived.cell_label").df(), on="cell_id", how="left")
            basin = con.execute("select cell_id, in_basin from derived.cell").df()
            crit = dict(con.execute("select cell_id, score from derived.cell_score where model = 'criteria'").fetchall())
        finally:
            con.close()
        df = df.merge(basin, on="cell_id", how="left")
        df["criteria_score"] = [crit.get(c, np.nan) for c in df["cell_id"]]
    if "in_basin" not in df.columns:
        df["in_basin"] = True
    dated = map_to_cells(discoveries if discoveries is not None else load_discoveries(),
                         df[["cell_id", "label_name"]] if "label_name" in df.columns else pd.DataFrame(columns=["cell_id", "label_name"]))
    dated = [d for d in dated if CONFIDENCE_RANK.get(d.get("confidence", "low"), 0) >= CONFIDENCE_RANK[min_confidence]]
    unmapped = [d["name"] for d in dated if not d["cells"]]
    dated = [d for d in dated if d["cells"]]
    log(f"  {len(dated)} dated discoveries mapped to cells at confidence >= {min_confidence}"
        + (f"; not in the label layers: {unmapped}" if unmapped else ""))
    in_basin = df["in_basin"].fillna(False).to_numpy().astype(bool)
    rows: list[dict[str, Any]] = []
    fitter = fit or (lambda xt, yt, xs: _fit(xt, yt, xs))
    for cutoff in cutoffs:
        y, train_ok = frozen_labels(df, dated, cutoff)
        frozen = frozen_effort(df, cutoff)
        n_pos = int(y[train_ok].sum())
        later = [d for d in dated if d["year"] > cutoff]
        if n_pos == 0 or not later:
            log(f"  cutoff {cutoff}: {n_pos} positives before, {len(later)} discoveries after: nothing to test")
            continue
        scores: dict[str, np.ndarray] = {}
        for name, fs in (("learned", "learned"), ("effort", "effort")):
            keys = list(M.FEATURE_SETS[fs])
            x = frozen[keys].to_numpy(dtype=float)
            scores[name] = fitter(x[train_ok], y[train_ok], x)
        if "criteria_score" in df.columns:
            scores["criteria"] = df["criteria_score"].to_numpy(dtype=float)
        log(f"  cutoff {cutoff}: trained on {n_pos} dated deposits known by then; {len(later)} later discoveries ranked")
        for d in later:
            for model, s in scores.items():
                best = min(share_at_least(s, float(s[df["cell_id"] == c].max()), in_basin) for c in d["cells"]
                           if (df["cell_id"] == c).any())
                rows.append({"cutoff": cutoff, "discovery": d["name"], "year": d["year"], "confidence": d["confidence"],
                             "cells": d["cells"], "model": model, "area_share": best,
                             "in_top5": best <= 0.05, "in_top10": best <= 0.10})
            best_by = {r["model"]: r["area_share"] for r in rows if r["cutoff"] == cutoff and r["discovery"] == d["name"]}
            log("    " + f"{d['name']} ({d['year']}, {d['confidence']}): " +
                ", ".join(f"{m} top {v:.1%}" for m, v in best_by.items()))
    summary = _summary(rows)
    out = {"run_id": now, "rows": rows, "summary": summary, "unmapped": unmapped,
           "leakage": ["geological layers are compilations as they stand today", "survey footprints carry no year",
                       "holes drilled after the cutoff in cells first drilled before it stay in the count",
                       "occurrences carry no date and are treated as unlabelled"]}
    if track and rows:
        out["mlflow_run_id"] = TR.log_run("hindcast", {"cutoffs": list(cutoffs), "min_confidence": min_confidence,
                                                       "discoveries": len(dated)},
                                          {f"{k}": v for k, v in summary.items() if isinstance(v, (int, float))},
                                          tags={"phase": "3", "kind": "hindcast"}, artifacts={"rows.json": rows})
    if write and rows:
        _write_metrics(rows, summary, out.get("mlflow_run_id") or now)
    return out


def _fit(x_train: np.ndarray, y_train: np.ndarray, x_all: np.ndarray) -> np.ndarray:
    model = M._model()
    model.fit(x_train, y_train)
    return model.predict_proba(x_all)[:, 1]


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for model in sorted({r["model"] for r in rows}):
        sub = [r for r in rows if r["model"] == model]
        out[f"{model}.median_area_share"] = float(np.median([r["area_share"] for r in sub]))
        out[f"{model}.n_in_top10"] = int(sum(r["in_top10"] for r in sub))
        out[f"{model}.n"] = len(sub)
    return out


def _write_metrics(rows: list[dict[str, Any]], summary: dict[str, Any], run_id: str) -> None:
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    metrics = []
    for r in rows:
        slug = r["discovery"].lower().replace(" ", "_").replace(",", "").replace("(", "").replace(")", "")
        metrics.append({"metric_key": f"hindcast.{r['cutoff']}.{r['model']}.{slug}.area_share", "run_id": run_id,
                        "value": float(r["area_share"]), "fmt": "ratio3", "computed_at": now,
                        "note": f"share of basin area scoring at least the {r['discovery']} cell ({r['year']}, {r['confidence']} confidence) "
                                f"by the {r['model']} model frozen at {r['cutoff']}"})
    for k, v in summary.items():
        if isinstance(v, (int, float)):
            metrics.append({"metric_key": f"hindcast.summary.{k}", "run_id": run_id, "value": float(v), "fmt": "ratio3",
                            "computed_at": now, "note": "hindcast summary across cutoffs and discoveries"})
    con = connect()
    try:
        con.execute("delete from derived.metric where metric_key like 'hindcast.%'")
        append_frame(con, "derived", "metric", pd.DataFrame(metrics), "derived")
    finally:
        con.close()
    (PATHS.data / "out" / "prospect").mkdir(parents=True, exist_ok=True)
    (PATHS.data / "out" / "prospect" / "hindcast.json").write_text(json.dumps({"run_id": run_id, "rows": rows, "summary": summary}, indent=1) + "\n")
