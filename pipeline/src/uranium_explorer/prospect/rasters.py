"""Per-cell statistics from public imagery and terrain, read straight from cloud-optimised GeoTIFFs.

What imagery can and cannot do here is settled before any of it is used. Athabasca deposits sit 100 to 900 m
down under 1 to 2 km of sandstone, so no optical sensor sees one, and nothing in this module is an ore
detector. Three honest jobs remain:

* **The sampling frame.** 16,000 lake-sediment and lake-water samples exist only where there is a lake. The
  water fraction per cell says whether a cell could ever have been sampled that way, which is what separates
  "no anomaly" from "never looked".
* **Cover versus outcrop.** Vegetation and bare-ground fractions say whether a surface observation in a cell
  means anything about the rock beneath it.
* **Terrain.** Elevation and local relief, and the landform grain that carries glacial transport direction,
  which is what turns a radioactive boulder into a search corridor.

Everything is read at a decimated resolution through the COG's own overviews: a 2 km cell does not need 10 m
pixels, and the whole basin at full resolution would be hundreds of gigabytes for a number that fits in a byte.
Scenes are recorded in `native.scene` so any per-cell statistic can name the images behind it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..store import append_frame, connect
from .grid import GRID_EPSG, load_cells

STAC_URL = "https://earth-search.aws.element84.com/v1"
TOOL = "prospect/rasters"

#: Sentinel-2 scene classification classes we count (the L2A SCL band)
SCL = {"shadow": 3, "vegetation": 4, "bare": 5, "water": 6, "cloud_med": 8, "cloud_high": 9, "cirrus": 10,
       "snow": 11}
CLOUDY = (SCL["shadow"], SCL["cloud_med"], SCL["cloud_high"], SCL["cirrus"])

#: read the 20 m SCL band at about 200 m, and the 30 m DEM at about 120 m
S2_DECIMATE = 10
DEM_DECIMATE = 4

#: a cell whose pixels are mostly cloud is recorded as unobserved rather than as land
MAX_CLOUD_SHARE = 0.35


def _gdal_env() -> None:
    """Public buckets, no credentials, and no directory listing on open."""
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
    os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")
    os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")


@dataclass(frozen=True)
class Grid:
    """Where the grid's cells sit, so a pixel can be put in one by arithmetic rather than a spatial join."""

    x0: float
    y0: float
    cell_m: float
    n_cols: int
    n_rows: int
    index: np.ndarray  # (n_cols, n_rows) -> position in `cell_ids`, or -1
    cell_ids: list[str]

    @classmethod
    def from_cells(cls, cells: Any) -> "Grid":
        cell_m = float(cells["cell_m"].iloc[0])
        col = cells["col"].to_numpy()
        row = cells["row"].to_numpy()
        x0 = float(cells["cx"].iloc[0]) - (float(col[0]) + 0.5) * cell_m
        y0 = float(cells["cy"].iloc[0]) - (float(row[0]) + 0.5) * cell_m
        n_cols, n_rows = int(col.max()) + 1, int(row.max()) + 1
        index = np.full((n_cols, n_rows), -1, dtype=np.int32)
        index[col, row] = np.arange(len(cells), dtype=np.int32)
        return cls(x0, y0, cell_m, n_cols, n_rows, index, list(cells["cell_id"]))

    def cell_of(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Position in `cell_ids` for each projected coordinate, or -1 outside the grid."""
        col = np.floor((x - self.x0) / self.cell_m).astype(np.int64)
        row = np.floor((y - self.y0) / self.cell_m).astype(np.int64)
        ok = (col >= 0) & (col < self.n_cols) & (row >= 0) & (row < self.n_rows)
        out = np.full(col.shape, -1, dtype=np.int32)
        out[ok] = self.index[col[ok], row[ok]]
        return out


def _rfc3339(day: str) -> str:
    """The API rejects a bare date: 2024-07-01 has to arrive as 2024-07-01T00:00:00Z."""
    return day if "T" in day else f"{day}T00:00:00Z"


def _post(body: dict[str, Any]) -> dict[str, Any]:
    import urllib.request

    req = urllib.request.Request(
        f"{STAC_URL}/search", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as fh:
        return json.load(fh)


def _search(
    collection: str, bbox: tuple[float, float, float, float], max_features: int = 4000, **query: Any
) -> list[dict[str, Any]]:
    """Every matching item, following the paging token. A basin-wide search matches far more than one page."""
    body: dict[str, Any] = {"collections": [collection], "bbox": list(bbox), "limit": 100, **query}
    out: list[dict[str, Any]] = []
    while len(out) < max_features:
        page = _post(body)
        feats = page.get("features", [])
        out += feats
        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if not feats or not nxt:
            break
        body = {**body, **(nxt.get("body") or {})}
    return out


def region_bbox(cells: Any) -> tuple[float, float, float, float]:
    return (
        float(cells["lon"].min()), float(cells["lat"].min()),
        float(cells["lon"].max()), float(cells["lat"].max()),
    )


def pick_scenes(
    bbox: tuple[float, float, float, float], start: str, end: str, max_cloud: float = 15.0
) -> list[dict[str, Any]]:
    """The best Sentinel-2 scene per MGRS tile in the window: mostly-full first, then least cloudy.

    One scene per tile, not a composite: a composite needs many reads per tile for a statistic a single clear
    summer scene already answers, and every cell records how much cloud its own pixels carried. Granules at the
    edge of a swath can be almost entirely no-data, which is why emptiness is ranked before cloud — a pristine
    but 90%-empty granule leaves a hole in the map.
    """
    feats = _search(
        "sentinel-2-l2a", bbox, datetime=f"{_rfc3339(start)}/{_rfc3339(end)}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
    )

    def rank(f: dict[str, Any]) -> tuple[float, float]:
        p = f["properties"]
        nodata = float(p.get("s2:nodata_pixel_percentage") or 0.0)
        cloud = float(p.get("eo:cloud_cover") or 100.0)
        # anything under a fifth empty counts as full, so the choice among those comes down to cloud
        return (round(nodata / 20.0), cloud)

    best: dict[str, dict[str, Any]] = {}
    for f in feats:
        tile = f["properties"].get("grid:code") or f["id"]
        if tile not in best or rank(f) < rank(best[tile]):
            best[tile] = f
    return sorted(best.values(), key=lambda f: str(f["properties"].get("grid:code")))


def _read_decimated(href: str, factor: int) -> tuple[np.ndarray, Any, Any]:
    import rasterio
    from rasterio.enums import Resampling

    with rasterio.open(href) as src:
        h, w = max(1, src.height // factor), max(1, src.width // factor)
        arr = src.read(1, out_shape=(h, w), resampling=Resampling.nearest)
        transform = src.transform * src.transform.scale(src.width / w, src.height / h)
        return arr, transform, src.crs


def _pixel_cells(arr: np.ndarray, transform: Any, crs: Any, grid: Grid) -> np.ndarray:
    """Which cell each pixel centre falls in, as a flat array of positions (-1 outside the grid)."""
    from pyproj import Transformer

    h, w = arr.shape
    cols, rows = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    xs = transform.c + transform.a * cols + transform.b * rows
    ys = transform.f + transform.d * cols + transform.e * rows
    if crs.to_epsg() != GRID_EPSG:
        tr = Transformer.from_crs(crs.to_epsg(), GRID_EPSG, always_xy=True)
        xs, ys = tr.transform(xs.ravel(), ys.ravel())
    else:
        xs, ys = xs.ravel(), ys.ravel()
    return grid.cell_of(np.asarray(xs), np.asarray(ys))


def sentinel2_fractions(
    scenes: list[dict[str, Any]], grid: Grid, log: Callable[[str], None] = print
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Water, vegetation and bare-ground fractions per cell, plus the cloud share behind them."""
    _gdal_env()
    n = len(grid.cell_ids)
    counts = {k: np.zeros(n, dtype=np.int64) for k in ("water", "vegetation", "bare", "cloud", "valid")}
    scene_rows: list[dict[str, Any]] = []
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    for i, f in enumerate(scenes, 1):
        href = (f["assets"].get("scl") or {}).get("href")
        if not href:
            continue
        try:
            arr, transform, crs = _read_decimated(href, S2_DECIMATE)
        except Exception as err:  # a single unreadable scene must not lose the run
            log(f"    scene {f['id']}: unreadable ({type(err).__name__}); skipped")
            continue
        pos = _pixel_cells(arr, transform, crs, grid)
        flat = arr.ravel()
        inside = pos >= 0
        p = pos[inside]
        v = flat[inside]
        counts["water"] += np.bincount(p[v == SCL["water"]], minlength=n)
        counts["vegetation"] += np.bincount(p[v == SCL["vegetation"]], minlength=n)
        counts["bare"] += np.bincount(p[v == SCL["bare"]], minlength=n)
        cloudy = np.isin(v, CLOUDY)
        counts["cloud"] += np.bincount(p[cloudy], minlength=n)
        # valid means "the ground was seen": not no-data, not saturated, and not under cloud, so a cloudy
        # pixel never lands in both the numerator and the denominator of a fraction
        counts["valid"] += np.bincount(p[(v > 1) & ~cloudy], minlength=n)
        scene_rows.append({
            "collection": "sentinel-2-l2a", "stac_id": f["id"], "asset_key": "scl", "href": href,
            "datetime": f["properties"].get("datetime"),
            "cloud_cover": float(f["properties"].get("eo:cloud_cover") or 0.0),
            "epsg": int(f["properties"].get("proj:epsg") or 0) or None,
            "gsd": 20.0 * S2_DECIMATE, "licence": "Copernicus Sentinel Data Licence", "retrieved_at": now,
        })
        log(f"    {i}/{len(scenes)} {f['properties'].get('grid:code')} "
            f"cloud {f['properties'].get('eo:cloud_cover'):.1f}%  cells touched {int((np.bincount(p, minlength=n) > 0).sum())}")

    valid = counts["valid"].astype(float)
    total = valid + counts["cloud"]
    cloud_share = np.divide(counts["cloud"], np.maximum(total, 1), out=np.zeros(n), where=total > 0)
    seen = (valid > 0) & (cloud_share <= MAX_CLOUD_SHARE)

    def frac(key: str) -> np.ndarray:
        out = np.full(n, np.nan)
        out[seen] = counts[key][seen] / valid[seen]
        return out

    df = pd.DataFrame({
        "cell_id": grid.cell_ids,
        "water_fraction": frac("water"),
        "vegetation_fraction": frac("vegetation"),
        "bare_fraction": frac("bare"),
        "pixels": valid.astype(int),
        "cloud_share": np.where(total > 0, cloud_share, np.nan),
    })
    return df, scene_rows


def _landform_grain(
    arr: np.ndarray, transform: Any, lat_deg: float, geographic: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    """The axis of the terrain's grain, and how well defined it is, from the elevation structure tensor.

    Glacial landforms (drumlins, flutings, eskers) are elongate along the direction the ice moved, so their
    axis is measurable from a DEM. The *sense* is not: an axis of 45 degrees cannot say whether the ice came
    from the northeast or the southwest. That is why this returns an orientation in 0-180 and the criterion
    that uses it treats the corridor as two-sided until a geologist sets the sense.

    Returned as (orientation degrees, coherence 0-1). Coherence near zero means there is no grain to speak of,
    and the caller stores null rather than an arbitrary angle.
    """
    z = arr.astype(float)
    if geographic:
        # a degree of longitude shortens with latitude; a degree of latitude does not
        dy_m = abs(transform.e) * 111_320.0
        dx_m = abs(transform.a) * 111_320.0 * max(np.cos(np.radians(lat_deg)), 0.1)
    else:
        dy_m, dx_m = abs(transform.e), abs(transform.a)
    nan = np.full(z.shape, np.nan)
    if min(z.shape) < 5:
        return nan, np.zeros_like(z)

    def box(a: np.ndarray, width: int) -> np.ndarray:
        """A moving average with reflected edges: zero padding would invent a cliff at every border."""
        width = max(3, min(width, (min(a.shape) - 1) | 1))
        kk = np.ones(width) / width
        pad = width // 2
        out = np.pad(a, ((0, 0), (pad, pad)), mode="reflect")
        out = np.apply_along_axis(lambda r: np.convolve(r, kk, mode="valid"), 1, out)
        out = np.pad(out, ((pad, pad), (0, 0)), mode="reflect")
        return np.apply_along_axis(lambda c: np.convolve(c, kk, mode="valid"), 0, out)

    # Remove the regional slope first. Without this the tensor is dominated by the basin's broad tilt, which is
    # the same direction over hundreds of kilometres, and every cell reports a confident grain that is really
    # just "downhill". Subtracting a 15 km mean leaves the landform-scale relief the ice actually shaped.
    detrend_px = max(3, int(round(15_000.0 / max(dx_m, 1.0))) | 1)
    if detrend_px > min(z.shape) // 3:
        # the tile is too small to tell a regional trend from a landform; saying nothing beats saying "downhill"
        return nan, np.zeros_like(z)
    z = z - box(z, detrend_px)
    gy, gx = np.gradient(z, dy_m, dx_m)
    # smooth the tensor over a few kilometres so the grain, not the pixel noise, is what is measured;
    # the window can never exceed the array, or a convolution in "same" mode would change its shape
    win = max(3, int(round(5000.0 / max(dx_m, 1.0))) | 1)

    def smooth(a: np.ndarray) -> np.ndarray:
        return box(a, win)

    jxx, jyy, jxy = smooth(gx * gx), smooth(gy * gy), smooth(gx * gy)
    trace = jxx + jyy
    diff = np.sqrt((jxx - jyy) ** 2 + 4 * jxy**2)
    # Below a real slope there is nothing to orient. A floor of 1e-8 is a gradient of 0.1 m per kilometre
    # squared: under that the ratio would be numerical noise divided by numerical noise, which flat ground
    # would otherwise report as a perfectly confident direction.
    solid = trace > 1e-8
    coherence = np.divide(diff, trace, out=np.zeros_like(trace), where=solid)
    # the gradient is steepest across a ridge, so the ridge axis is perpendicular to it
    grad_dir = 0.5 * np.arctan2(2 * jxy, jxx - jyy)
    axis = np.degrees(grad_dir) + 90.0
    return np.where(solid, np.mod(axis, 180.0), np.nan), coherence


def dem_stats(
    bbox: tuple[float, float, float, float], grid: Grid, log: Callable[[str], None] = print
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Mean elevation and local relief per cell from Copernicus DEM GLO-30."""
    _gdal_env()
    feats = _search("cop-dem-glo-30", bbox)
    n = len(grid.cell_ids)
    total = np.zeros(n)
    count = np.zeros(n, dtype=np.int64)
    lo = np.full(n, np.inf)
    hi = np.full(n, -np.inf)
    # the grain is averaged as a doubled-angle vector, because 179 degrees and 1 degree are nearly the same axis
    grain_x = np.zeros(n)
    grain_y = np.zeros(n)
    grain_w = np.zeros(n)
    rows: list[dict[str, Any]] = []
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    for i, f in enumerate(feats, 1):
        href = (f["assets"].get("data") or {}).get("href", "")
        # the STAC record points at s3://; the same object is public over HTTPS
        if href.startswith("s3://"):
            href = href.replace("s3://copernicus-dem-30m/", "https://copernicus-dem-30m.s3.amazonaws.com/")
        if not href:
            continue
        try:
            arr, transform, crs = _read_decimated(href, DEM_DECIMATE)
        except Exception as err:
            log(f"    dem {f['id']}: unreadable ({type(err).__name__}); skipped")
            continue
        pos = _pixel_cells(arr, transform, crs, grid)
        flat = arr.ravel().astype(float)
        ok = (pos >= 0) & np.isfinite(flat) & (flat > -1000)
        p = pos[ok]
        v = flat[ok]
        if len(p) == 0:
            continue
        total += np.bincount(p, weights=v, minlength=n)
        count += np.bincount(p, minlength=n)
        np.minimum.at(lo, p, v)
        np.maximum.at(hi, p, v)

        lat_mid = float(transform.f + transform.e * arr.shape[0] / 2)
        axis, coh = _landform_grain(arr, transform, lat_mid, geographic=crs.to_epsg() == 4326)
        a2 = np.radians(2.0 * np.nan_to_num(axis.ravel()[ok]))
        w = np.nan_to_num(coh.ravel()[ok])
        grain_x += np.bincount(p, weights=w * np.cos(a2), minlength=n)
        grain_y += np.bincount(p, weights=w * np.sin(a2), minlength=n)
        grain_w += np.bincount(p, weights=w, minlength=n)
        rows.append({
            "collection": "cop-dem-glo-30", "stac_id": f["id"], "asset_key": "data", "href": href,
            "datetime": f["properties"].get("datetime"), "cloud_cover": None,
            "epsg": 4326, "gsd": 30.0 * DEM_DECIMATE, "licence": "Copernicus DEM free licence",
            "retrieved_at": now,
        })
        if i % 5 == 0:
            log(f"    dem {i}/{len(feats)} tiles")

    have = count > 0
    mean = np.full(n, np.nan)
    mean[have] = total[have] / count[have]
    relief = np.full(n, np.nan)
    relief[have] = hi[have] - lo[have]
    #: a cell whose grain vectors cancel out has no consistent landform direction to report
    MIN_GRAIN = 0.05
    strength = np.divide(np.hypot(grain_x, grain_y), np.maximum(grain_w, 1e-12),
                         out=np.zeros(n), where=grain_w > 0)
    grain = np.full(n, np.nan)
    ok_grain = have & (grain_w > 0) & (strength >= MIN_GRAIN)
    grain[ok_grain] = np.mod(np.degrees(np.arctan2(grain_y[ok_grain], grain_x[ok_grain])) / 2.0, 180.0)
    coherence_out = np.full(n, np.nan)
    coherence_out[have & (grain_w > 0)] = strength[have & (grain_w > 0)]

    df = pd.DataFrame({
        "cell_id": grid.cell_ids, "elevation_m": mean, "relief_m": relief,
        "landform_grain_deg": grain, "grain_coherence": coherence_out, "pixels": count.astype(int)
    })
    return df, rows


#: features that come from the terrain model rather than from imagery
DEM_KEYS = frozenset({"elevation_m", "relief_m", "landform_grain_deg", "grain_coherence"})

#: (feature key, source column, unit, bears_on, notes) for everything this module writes
RASTER_FEATURES = (
    ("water_fraction", "water_fraction", None, "detection",
     "Share of the cell classified as water. The lake-sediment and lake-water surveys can only sample where "
     "there is a lake, so this says whether a cell could ever have carried one of those measurements."),
    ("vegetation_fraction", "vegetation_fraction", None, "cover",
     "Share classified as vegetation: how much of the ground is obscured from any surface observation."),
    ("bare_fraction", "bare_fraction", None, "cover",
     "Share classified as bare ground, the nearest public proxy for where rock is exposed rather than covered."),
    ("elevation_m", "elevation_m", "m", "cover", "Mean elevation from Copernicus DEM GLO-30."),
    ("landform_grain_deg", "landform_grain_deg", "deg", "dispersal",
     "Orientation of the terrain's grain, 0 to 180 degrees, from the structure tensor of a detrended DEM. It "
     "is an axis, not a direction: which end the ice came from cannot be read from an axis. Measured on this "
     "grid it peaks sharply between 120 and 150 degrees, and nothing here establishes that this is glacial "
     "rather than structural or drainage grain, so no criterion may use it as an ice-flow direction until a "
     "geologist sets both the sense and whether it is ice flow at all."),
    ("grain_coherence", "grain_coherence", None, "dispersal",
     "How rank-one the structure tensor is, 0 to 1. On this terrain it sits above 0.98 for three quarters of "
     "the cells, so it does not separate cells with a grain from cells without one and must not be used as a "
     "quality filter. It is published because the measurement was made, not because it discriminates."),
    ("relief_m", "relief_m", "m", "dispersal",
     "Local relief within the cell: the terrain grain that carries glacial landforms, which is how a boulder "
     "train is turned into a direction."),
)


def build(
    start: str = "2023-06-15", end: str = "2024-09-20", max_cloud: float = 15.0,
    only: str | None = None, grid_id: str | None = None, log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Build the raster features. `only` may be "s2" or "dem" to rebuild one half without re-reading the other."""
    if only not in (None, "s2", "dem"):
        raise ValueError(f"only must be 's2', 'dem' or omitted, not {only!r}")
    cells = load_cells(grid_id)
    grid = Grid.from_cells(cells)
    bbox = region_bbox(cells)
    log(f"  {len(cells)} cells; region {bbox[0]:.2f},{bbox[1]:.2f} to {bbox[2]:.2f},{bbox[3]:.2f}")

    blank = pd.DataFrame({"cell_id": grid.cell_ids})
    if only == "dem":
        s2, s2_scenes = blank.assign(water_fraction=np.nan, vegetation_fraction=np.nan, bare_fraction=np.nan,
                                     pixels=0, cloud_share=np.nan), []
    else:
        scenes = pick_scenes(bbox, start, end, max_cloud)
        log(f"  Sentinel-2: {len(scenes)} tiles under {max_cloud:.0f}% cloud between {start} and {end}")
        s2, s2_scenes = sentinel2_fractions(scenes, grid, log=log)
    if only == "s2":
        dem, dem_scenes = blank.assign(elevation_m=np.nan, relief_m=np.nan, landform_grain_deg=np.nan,
                                       grain_coherence=np.nan, pixels=0), []
    else:
        dem, dem_scenes = dem_stats(bbox, grid, log=log)

    merged = s2.merge(dem, on="cell_id", how="outer", suffixes=("_s2", "_dem"))
    wanted = [f for f in RASTER_FEATURES if only is None
              or (only == "dem") == (f[0] in DEM_KEYS)]
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    frames = []
    for key, col, unit, _bears_on, _note in wanted:
        pixels = merged["pixels_dem"] if key in DEM_KEYS else merged["pixels_s2"]
        frames.append(pd.DataFrame({
            "cell_id": merged["cell_id"], "feature_key": key, "value": merged[col], "value_text": None,
            "unit": unit, "n_obs": pixels.fillna(0).astype(int), "nearest_m": np.nan,
            "from_tier": "native", "op": "raster_zonal", "tool": TOOL,
            "params": json.dumps({"decimate_s2": S2_DECIMATE, "decimate_dem": DEM_DECIMATE,
                                  "window": [start, end], "max_cloud": max_cloud}),
            "inputs": json.dumps(["cop_dem_glo_30" if key in DEM_KEYS else "sentinel2_l2a"]),
            "computed_at": now,
        }))

    con = connect()
    try:
        keys = [k for k, *_ in wanted]
        con.execute(f"delete from derived.cell_feature where feature_key in ({','.join('?' * len(keys))})", keys)
        append_frame(con, "derived", "cell_feature", pd.concat(frames, ignore_index=True), "derived")
        spec_rows = pd.DataFrame([{
            "feature_key": key, "title": key.replace("_", " ").capitalize(), "unit": unit,
            "from_tier": "native",
            "source_keys": json.dumps(["cop_dem_glo_30"] if key in DEM_KEYS else ["sentinel2_l2a"]),
            "bears_on": bears_on, "is_effort": False, "is_label": False, "notes": note,
        } for key, _col, unit, bears_on, note in wanted])
        con.execute(f"delete from derived.feature_spec where feature_key in ({','.join('?' * len(keys))})", keys)
        append_frame(con, "derived", "feature_spec", spec_rows, "derived")
        if s2_scenes or dem_scenes:
            collections = {r["collection"] for r in s2_scenes + dem_scenes}
            for c in collections:
                con.execute("delete from native.scene where collection = ?", [c])
            append_frame(con, "native", "scene", pd.DataFrame(s2_scenes + dem_scenes), "native")
    finally:
        con.close()

    got = {key: int(merged[col].notna().sum()) for key, col, *_ in wanted}
    for key, have in got.items():
        log(f"    {key:22} {have / max(len(cells), 1):.1%} of cells")
    log(f"  {len(s2_scenes)} Sentinel-2 scenes, {len(dem_scenes)} DEM tiles recorded in native.scene")
    return {"cells": len(cells), "scenes": len(s2_scenes) + len(dem_scenes), "coverage": got}
