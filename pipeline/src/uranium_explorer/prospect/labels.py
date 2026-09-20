"""Labels, camps and folds: what counts as a positive, and how ground is held out.

Three decisions live here, and each one changes the numbers more than the choice of model does.

**An unlabelled cell is not barren.** Nobody has drilled most of this basin. Cells without a deposit or
occurrence are *unlabelled*, and the learned model treats them that way (positive-unlabelled), because random
negatives create "label crossover, which is the condition that otherwise true positives would be reversed in
class designation" (Parsa and Cumani 2025, via research report 02 section 4.2.2).

**Positives come in two tiers.** A mapped deposit footprint is a deposit. An occurrence in the provincial index
is a place uranium was observed, which is weaker, and the two are kept apart so a run can say which it used.

**Folds are spatial, and committed before any fit.** Nearby cells share geology, so a random split leaks
near-copies into training: in ecology a random-forest model scored R2 0.53 under random and 0.14 under spatial
cross-validation. Positives here cluster in camps, so the harder and more honest test is to hold a whole camp
out, which `camps()` exists to make possible.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

import geopandas as gpd
import numpy as np
import pandas as pd

from ..paths import PATHS
from ..store import append_frame, connect
from .grid import GRID_EPSG, load_cells

TOOL = "prospect/labels"

#: a cell within this distance of an occurrence point counts as carrying it
OCCURRENCE_RADIUS_M = 1000.0
#: single-linkage distance for grouping positives into camps
CAMP_EPS_KM = 25.0
#: spatial cross-validation block size
BLOCK_KM = 30.0


def _layer(key: str) -> gpd.GeoDataFrame:
    path = PATHS.index / f"{key}.geojson"
    if not path.is_file():
        raise RuntimeError(f"{key}.geojson missing; run `ue index pull --only {key}`")
    return gpd.read_file(path).to_crs(epsg=GRID_EPSG)


def label_cells(grid_id: str | None = None, log: Callable[[str], None] = print) -> pd.DataFrame:
    """One row per positive cell, with the tier that made it positive and the name behind it."""
    cells = load_cells(grid_id)
    out: dict[str, dict[str, Any]] = {}

    deposits = _layer("uranium_deposit_footprints")
    pairs = gpd.sjoin(
        gpd.GeoDataFrame(cells[["cell_id"]], geometry=cells.geometry, crs=cells.crs),
        deposits[[c for c in ("DEPOSIT", "NAME_ZONE") if c in deposits.columns] + ["geometry"]],
        how="inner", predicate="intersects",
    )
    cell_geom = dict(zip(cells["cell_id"], cells.geometry, strict=True))
    dep_geom = deposits.geometry.to_numpy()
    for cell_id, idx in zip(pairs["cell_id"].to_numpy(), pairs["index_right"].to_numpy(), strict=True):
        if cell_geom[cell_id].intersection(dep_geom[idx]).area <= 0:
            continue  # a footprint that merely abuts the cell is not in it
        name = None
        for col in ("DEPOSIT", "NAME_ZONE"):
            if col in deposits.columns:
                name = deposits.iloc[idx][col] or name
        out[cell_id] = {"cell_id": cell_id, "label_tier": "deposit", "label_name": str(name or "")}

    occ = _layer("mineral_deposits_uranium")
    occ = occ[~occ.geometry.is_empty & occ.geometry.notna()]
    if len(occ):
        from sklearn.neighbors import KDTree

        xy = np.column_stack([occ.geometry.x.to_numpy(), occ.geometry.y.to_numpy()])
        tree = KDTree(xy)
        centres = np.column_stack([cells.geometry.centroid.x.to_numpy(), cells.geometry.centroid.y.to_numpy()])
        hits = tree.query_radius(centres, r=OCCURRENCE_RADIUS_M)
        names = occ["NAME"].to_numpy() if "NAME" in occ.columns else np.array([""] * len(occ))
        for i, hit in enumerate(hits):
            if len(hit) == 0:
                continue
            cell_id = cells["cell_id"].iloc[i]
            if cell_id in out:
                continue  # a deposit outranks an occurrence
            out[cell_id] = {"cell_id": cell_id, "label_tier": "occurrence",
                            "label_name": str(names[hit[0]] or "")}

    df = pd.DataFrame(list(out.values()))
    if df.empty:
        return pd.DataFrame(columns=["cell_id", "label_tier", "label_name"])
    log(f"  positives: {(df['label_tier'] == 'deposit').sum()} deposit cells, "
        f"{(df['label_tier'] == 'occurrence').sum()} occurrence cells, of {len(cells)}")
    return df


def camps(positives: pd.DataFrame, cells: gpd.GeoDataFrame, eps_km: float = CAMP_EPS_KM) -> pd.Series:
    """Group positives into camps by single-linkage distance, so a whole camp can be held out.

    Known deposits cluster where people looked, so holding out one random positive leaves its neighbours in
    training and the test is far too easy. A camp is the unit that makes a spatial hold-out mean something.
    """
    from sklearn.cluster import DBSCAN

    if positives.empty:
        return pd.Series(dtype="int64")
    pos = positives.merge(
        pd.DataFrame({"cell_id": cells["cell_id"], "x": cells.geometry.centroid.x,
                      "y": cells.geometry.centroid.y}),
        on="cell_id", how="left",
    )
    labels = DBSCAN(eps=eps_km * 1000, min_samples=1).fit_predict(pos[["x", "y"]].to_numpy())
    return pd.Series(labels, index=pos["cell_id"])


def blocks(cells: gpd.GeoDataFrame, block_km: float = BLOCK_KM) -> pd.Series:
    """Spatial block id per cell, assigned from position alone and before any model is fitted."""
    x = cells.geometry.centroid.x.to_numpy()
    y = cells.geometry.centroid.y.to_numpy()
    bx = np.floor((x - x.min()) / (block_km * 1000)).astype(int)
    by = np.floor((y - y.min()) / (block_km * 1000)).astype(int)
    return pd.Series(bx * 10_000 + by, index=cells["cell_id"])


def build(grid_id: str | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Write `derived.cell_label`: positives, their camp, and every cell's spatial block."""
    cells = load_cells(grid_id)
    positives = label_cells(grid_id, log=log)
    camp = camps(positives, cells)
    block = blocks(cells)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    df = pd.DataFrame({"cell_id": cells["cell_id"]})
    df["block_id"] = df["cell_id"].map(block).astype("int64")
    df = df.merge(positives, on="cell_id", how="left")
    df["label_tier"] = df["label_tier"].fillna("unlabelled")
    df["label_name"] = df["label_name"].fillna("")
    df["camp_id"] = df["cell_id"].map(camp).fillna(-1).astype("int64")
    df["computed_at"] = now

    con = connect()
    try:
        con.execute("delete from derived.cell_label")
        append_frame(con, "derived", "cell_label", df, "derived")
        counts = con.execute(
            "select label_tier, count(*) from derived.cell_label group by 1 order by 2 desc"
        ).fetchall()
        n_camps = con.execute(
            "select count(distinct camp_id) from derived.cell_label where camp_id >= 0"
        ).fetchone()[0]
        n_blocks = con.execute("select count(distinct block_id) from derived.cell_label").fetchone()[0]
    finally:
        con.close()
    log(f"  {dict(counts)}; {n_camps} camps, {n_blocks} spatial blocks of {BLOCK_KM:.0f} km")
    return {"counts": dict(counts), "camps": int(n_camps), "blocks": int(n_blocks)}
