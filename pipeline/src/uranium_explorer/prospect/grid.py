"""The analysis grid: square cells over the Athabasca Basin and its margins.

Cells are square and metric, in the province's own projected CRS (NAD83 UTM zone 13N, EPSG:2957), so a distance
in a feature is a distance on the ground rather than a degree. The grid is built from the basin outline buffered
outward, because the margin discoveries at Patterson Lake sit outside the sandstone itself.

The cell size is a parameter, not a fact about the world: an ore body is far smaller than any cell here
(Hurricane measures 375 m by 125 m by 12 m), so a cell says "this square kilometre or two" and the metrics that
depend on it are reported per cell size.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

import geopandas as gpd
import pandas as pd
from shapely.geometry import box
from shapely.ops import unary_union

from ..ids import sha256_json, short
from ..paths import PATHS
from ..store import append_frame, connect

#: the province's own projected CRS: metric, and the one the services return natively
GRID_EPSG = 2957
DEFAULT_CELL_M = 2000
DEFAULT_BUFFER_M = 30_000


@dataclass(frozen=True)
class GridSpec:
    cell_m: int = DEFAULT_CELL_M
    buffer_m: int = DEFAULT_BUFFER_M
    epsg: int = GRID_EPSG

    @property
    def grid_id(self) -> str:
        return short(sha256_json({"cell_m": self.cell_m, "buffer_m": self.buffer_m, "epsg": self.epsg}))


def basin_outline(epsg: int = GRID_EPSG) -> Any:
    """The Athabasca Supergroup polygons, dissolved, in the grid's CRS."""
    path = PATHS.index / "basin_geology.geojson"
    if not path.is_file():
        raise RuntimeError("basin_geology.geojson missing; run `ue index pull --only basin_geology`")
    gdf = gpd.read_file(path).to_crs(epsg=epsg)
    return unary_union(list(gdf.geometry))


def build(spec: GridSpec | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Create the cells and write them to `derived.grid` and `derived.cell`."""
    spec = spec or GridSpec()
    basin = basin_outline(spec.epsg)
    study = basin.buffer(spec.buffer_m)
    minx, miny, maxx, maxy = study.bounds
    # snap the origin to a multiple of the cell size, so the same spec always gives the same cells
    x0 = (minx // spec.cell_m) * spec.cell_m
    y0 = (miny // spec.cell_m) * spec.cell_m
    n_cols = int((maxx - x0) // spec.cell_m) + 1
    n_rows = int((maxy - y0) // spec.cell_m) + 1
    log(f"  study area {(maxx - minx) / 1000:.0f} by {(maxy - miny) / 1000:.0f} km; "
        f"grid {n_cols} x {n_rows} at {spec.cell_m} m")

    geoms, cols, rows = [], [], []
    for c in range(n_cols):
        for r in range(n_rows):
            geoms.append(box(x0 + c * spec.cell_m, y0 + r * spec.cell_m,
                             x0 + (c + 1) * spec.cell_m, y0 + (r + 1) * spec.cell_m))
            cols.append(c)
            rows.append(r)
    cells = gpd.GeoDataFrame({"col": cols, "row": rows}, geometry=geoms, crs=f"EPSG:{spec.epsg}")

    # keep only cells that touch the study area; spatial index makes this quick over ~100k candidates
    keep = cells.sindex.query(study, predicate="intersects")
    cells = cells.iloc[sorted(keep)].reset_index(drop=True)
    inside = cells.sindex.query(basin, predicate="intersects")
    cells["in_basin"] = False
    cells.loc[sorted(inside), "in_basin"] = True

    centres = cells.geometry.centroid
    wgs = gpd.GeoSeries(centres, crs=f"EPSG:{spec.epsg}").to_crs(epsg=4326)
    frame = pd.DataFrame({
        "cell_id": [f"{c:04d}_{r:04d}" for c, r in zip(cells["col"], cells["row"], strict=True)],
        "grid_id": spec.grid_id,
        "col": cells["col"].astype("int32"),
        "row": cells["row"].astype("int32"),
        "cx": centres.x.to_numpy(),
        "cy": centres.y.to_numpy(),
        "lon": wgs.x.to_numpy(),
        "lat": wgs.y.to_numpy(),
        "geom_wkb": [g.wkb for g in cells.geometry],
        "in_basin": cells["in_basin"].to_numpy(),
    })

    con = connect()
    try:
        con.execute("delete from derived.cell where grid_id = ?", [spec.grid_id])
        con.execute("delete from derived.grid where grid_id = ?", [spec.grid_id])
        append_frame(con, "derived", "grid", pd.DataFrame([{
            "grid_id": spec.grid_id, "cell_m": spec.cell_m, "epsg": spec.epsg, "buffer_m": spec.buffer_m,
            "extent_wkt": box(x0, y0, x0 + n_cols * spec.cell_m, y0 + n_rows * spec.cell_m).wkt,
            "n_cells": len(frame),
            "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }]), "derived")
        append_frame(con, "derived", "cell", frame, "derived")
        counts = con.execute(
            "select count(*), count(*) filter (where in_basin) from derived.cell where grid_id = ?",
            [spec.grid_id],
        ).fetchone()
    finally:
        con.close()

    total, in_basin = counts or (0, 0)
    area_km2 = total * (spec.cell_m / 1000) ** 2
    log(f"  grid {spec.grid_id}: {total} cells ({area_km2:,.0f} km2), {in_basin} over the sandstone")
    return {"grid_id": spec.grid_id, "cells": total, "in_basin": in_basin, "cell_m": spec.cell_m,
            "area_km2": area_km2}


def load_cells(grid_id: str | None = None) -> gpd.GeoDataFrame:
    """The cells as a GeoDataFrame in the grid's CRS, for feature building."""
    from shapely import from_wkb

    con = connect(read_only=True)
    try:
        if grid_id is None:
            row = con.execute("select grid_id from derived.grid order by built_at desc limit 1").fetchone()
            if not row:
                raise RuntimeError("no grid built; run `ue prospect grid`")
            grid_id = row[0]
        df = con.execute(
            "select c.cell_id, c.col, c.row, c.cx, c.cy, c.lon, c.lat, c.in_basin, c.geom_wkb, g.epsg, g.cell_m "
            "from derived.cell c join derived.grid g using (grid_id) where c.grid_id = ? order by c.cell_id",
            [grid_id],
        ).df()
    finally:
        con.close()
    epsg = int(df["epsg"].iloc[0]) if len(df) else GRID_EPSG
    # DuckDB hands a BLOB back as bytearray, which shapely will not take
    geom = [from_wkb(bytes(b)) for b in df["geom_wkb"]]
    return gpd.GeoDataFrame(df.drop(columns=["geom_wkb"]), geometry=geom, crs=f"EPSG:{epsg}")
