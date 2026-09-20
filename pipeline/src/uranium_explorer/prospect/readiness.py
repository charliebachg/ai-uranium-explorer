"""How much of the basin each feature actually covers.

This runs before any model, and it is meant to be read before any score is believed. A feature that exists for
a tenth of the cells is not a feature yet, whatever its correlation with the labels looks like: the model will
simply have learned the shape of the sampled ground. The same table is what the web page shows.

Coverage here means "this cell has a real observation behind its value", which is not the same as "this cell has
a value": a distance to the nearest conductor exists everywhere, because absence of a conductor within 60 km is
itself a measurement, while a lake-sediment maximum exists only where somebody sampled a lake.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

import pandas as pd

from ..paths import PATHS
from ..store import connect
from .features import SPECS
from .inventory import load as load_inventory

#: coverage at or below this is called thin on the page: a model leaning on it is learning the sampling frame
THIN_COVERAGE = 0.40


def table(grid_id: str | None = None) -> pd.DataFrame:
    """One row per feature: coverage, observation counts and the spread of the values that exist."""
    con = connect(read_only=True)
    try:
        if grid_id is None:
            row = con.execute("select grid_id from derived.grid order by built_at desc limit 1").fetchone()
            grid_id = row[0] if row else None
        n_cells = con.execute(
            "select count(*) from derived.cell where grid_id = ?", [grid_id]
        ).fetchone()[0]
        df = con.execute(
            """
            select f.feature_key,
                   count(*)                                             as cells,
                   count(*) filter (where f.n_obs > 0)                   as cells_with_obs,
                   count(*) filter (where f.value is not null)           as cells_with_value,
                   count(*) filter (where f.value_text is not null)      as cells_with_text,
                   sum(f.n_obs)                                          as obs_cell_pairs,
                   median(f.n_obs) filter (where f.n_obs > 0)             as median_obs,
                   median(f.value)                                       as median_value,
                   min(f.value)                                          as min_value,
                   max(f.value)                                          as max_value,
                   median(f.nearest_m)                                   as median_nearest_m
            from derived.cell_feature f
            join derived.cell c using (cell_id)
            where c.grid_id = ?
            group by 1 order by 1
            """,
            [grid_id],
        ).df()
        specs = con.execute(
            "select feature_key, title, unit, bears_on, is_effort, is_count, from_tier, source_keys, notes "
            "from derived.feature_spec"
        ).df()
    finally:
        con.close()
    out = specs.merge(df, on="feature_key", how="right")
    # a right join leaves these as objects or NaN for any feature whose spec row predates a column
    for flag in ("is_effort", "is_count"):
        out[flag] = out[flag].fillna(False).astype(bool) if flag in out.columns else False
    out["n_cells"] = n_cells
    # a value backed by an observation, or (for a distance) a value that exists at all
    out["covered"] = out[["cells_with_obs", "cells_with_text"]].max(axis=1)
    out.loc[out["covered"] == 0, "covered"] = out["cells_with_value"]
    # a count is answerable wherever it was computed: zero samples nearby is an answer, not a gap
    out.loc[out["is_count"], "covered"] = out["cells_with_value"]
    out["coverage"] = (out["covered"] / n_cells).round(3)
    out["thin"] = out["coverage"] <= THIN_COVERAGE
    return out.sort_values(["is_effort", "coverage"], ascending=[True, False]).reset_index(drop=True)


def report(grid_id: str | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Print the scorecard and write it beside the other stage outputs."""
    inv = load_inventory()
    df = table(grid_id)
    n_cells = int(df["n_cells"].iloc[0]) if len(df) else 0
    log(f"  {n_cells:,} cells; {len(df)} features")
    log(f"  {'feature':24} {'bears on':10} {'coverage':>9}  {'obs per cell':>12}")
    for _, r in df.iterrows():
        flag = "thin" if r["thin"] else ""
        kind = "effort" if r["is_effort"] else str(r["bears_on"])
        med = r["median_obs"]
        med_txt = f"{med:.0f}" if pd.notna(med) else "-"
        log(f"  {r['feature_key']:24} {kind:10} {r['coverage']:>8.1%}  {med_txt:>12}  {flag}")

    thin = df[df["thin"] & ~df["is_effort"]]
    if len(thin):
        log(f"  {len(thin)} geological feature(s) cover {THIN_COVERAGE:.0%} of the basin or less: "
            + ", ".join(thin["feature_key"]))
    out = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "cells": n_cells,
        "thin_coverage_threshold": THIN_COVERAGE,
        "features": json.loads(df.drop(columns=["n_cells"]).to_json(orient="records")),
        "gaps": [
            {"key": g.key, "title": g.title, "status": g.status, "why_it_matters": g.why_it_matters,
             "evidence": g.evidence, "workaround": g.workaround}
            for g in inv.gaps
        ],
    }
    path = PATHS.out / "prospect" / "readiness.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1) + "\n")
    log(f"  wrote {path.relative_to(PATHS.pipeline)}")
    return out
