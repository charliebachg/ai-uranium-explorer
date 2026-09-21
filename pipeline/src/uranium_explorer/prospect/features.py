"""Per-cell features, each with the coverage that produced it.

Two rules run through this module.

**A missing measurement is not a zero.** Every feature returns a value *and* an observation count. A cell with
no lake has no lake-sediment number, which is not the same as a low one, and the readiness scorecard reads the
counts to say how much of the basin each feature actually covers.

**Features and labels never mix.** Deposit footprints and occurrences are the positive class; they are declared
`is_label` in the inventory and no builder here may read them. `test_feature_leakage` enforces it.

Effort features (where people drilled, where surveys were flown, where samples were taken) are built the same
way as the geological ones but marked `is_effort`, because the null model is trained on them alone: if geology
adds nothing over "where people already looked", that is the finding.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import from_wkb  # noqa: F401  (used by grid.load_cells)

from ..paths import PATHS
from ..store import append_frame, connect
from .grid import GRID_EPSG, load_cells
from .inventory import Inventory, load as load_inventory

TOOL = "prospect/features"

#: bedrock lithology words that mark the graphitic-pelitic hosts Athabasca conductors come from
GRAPHITIC_WORDS = ("graphit", "pelit", "metapelit", "gneiss")


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    title: str
    bears_on: str
    op: str
    source_keys: tuple[str, ...]
    unit: str | None = None
    from_tier: str = "native"
    is_effort: bool = False
    #: a count: zero is a real answer, so the feature is known everywhere rather than only where it is non-zero
    is_count: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@lru_cache(maxsize=None)
def _layer(key: str, epsg: int = GRID_EPSG) -> gpd.GeoDataFrame:
    """Read a pulled layer once per process: several features share the same source."""
    path = PATHS.index / f"{key}.geojson"
    if not path.is_file():
        raise RuntimeError(f"{key}.geojson missing; run `ue index pull --only {key}`")
    return gpd.read_file(path).to_crs(epsg=epsg)


def _empty(cells: gpd.GeoDataFrame, n_obs: int = 0) -> pd.DataFrame:
    return pd.DataFrame({"cell_id": cells["cell_id"], "value": np.nan, "n_obs": n_obs, "nearest_m": np.nan})


# ---------------------------------------------------------------- builders


def distance_to_lines(cells: gpd.GeoDataFrame, layer_key: str, where: str | None = None) -> pd.DataFrame:
    """Metres from each cell centre to the nearest line. Absence of a line is a real answer, not a gap."""
    lines = _layer(layer_key)
    if where:
        lines = lines.query(where)
    lines = lines[~lines.geometry.is_empty & lines.geometry.notna()]
    if lines.empty:
        return _empty(cells)
    centres = gpd.GeoDataFrame(cells[["cell_id"]], geometry=cells.geometry.centroid, crs=cells.crs)
    joined = gpd.sjoin_nearest(centres, lines[["geometry"]], how="left", distance_col="d")
    d = joined.groupby("cell_id", sort=False)["d"].min()
    out = pd.DataFrame({"cell_id": cells["cell_id"]})
    out["value"] = out["cell_id"].map(d).to_numpy()
    # one observation backs a distance: the nearest line. (The count of lines in the layer is not a
    # per-cell observation count, and summing it across cells would invent millions of them.)
    out["n_obs"] = out["value"].notna().astype(int)
    out["nearest_m"] = out["value"]
    return out


def line_density(cells: gpd.GeoDataFrame, layer_key: str, where: str | None = None) -> pd.DataFrame:
    """Kilometres of line per square kilometre of cell, from the clipped intersection length."""
    lines = _layer(layer_key)
    if where:
        lines = lines.query(where)
    lines = lines[~lines.geometry.is_empty & lines.geometry.notna()]
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": 0.0, "n_obs": 0, "nearest_m": np.nan})
    if lines.empty:
        return out
    pairs = gpd.sjoin(
        gpd.GeoDataFrame(cells[["cell_id"]], geometry=cells.geometry, crs=cells.crs),
        lines[["geometry"]], how="inner", predicate="intersects",
    )
    if pairs.empty:
        return out
    cell_geom = dict(zip(cells["cell_id"], cells.geometry, strict=True))
    line_geom = lines.geometry.to_numpy()
    lengths: dict[str, float] = {}
    counts: dict[str, int] = {}
    for cell_id, idx in zip(pairs["cell_id"].to_numpy(), pairs["index_right"].to_numpy(), strict=True):
        piece = cell_geom[cell_id].intersection(line_geom[idx])
        if piece.is_empty or piece.length <= 0:
            continue
        lengths[cell_id] = lengths.get(cell_id, 0.0) + float(piece.length)
        counts[cell_id] = counts.get(cell_id, 0) + 1
    area_km2 = (cells.geometry.area.iloc[0] / 1e6) if len(cells) else 1.0
    out["value"] = out["cell_id"].map(lambda c: lengths.get(c, 0.0) / 1000.0 / area_km2)
    out["n_obs"] = out["cell_id"].map(lambda c: counts.get(c, 0))
    return out


def point_stat(
    cells: gpd.GeoDataFrame,
    layer_key: str,
    value_field: str,
    radius_m: float,
    stat: str = "max",
    extra_field: str | None = None,
    zero_is_null: bool = False,
) -> pd.DataFrame:
    """A statistic over points within `radius_m` of the cell centre, with the sample count beside it.

    A cell with no sample in range gets a null value and n_obs 0: the sampling frame here is lakes, so "no
    anomaly" and "never sampled" are different facts and must stay different. `zero_is_null` reads a value
    of exactly 0 as a missing measurement: the radioactive-boulder layer carries 2,127 records at 0.0 cps
    among 6,591, a filled-in blank, and the staged analyst's verifier refused a node that read it as a
    measured absence: a filled-in blank is no reading, and unknown and absent stay distinct (the PRD's standing
    constraints). A record so dropped still counts for the effort features, which count records, not readings.
    """
    pts = _layer(layer_key)
    if value_field not in pts.columns:
        raise RuntimeError(f"{layer_key}: no field {value_field!r} (have {sorted(pts.columns)[:12]})")
    pts = pts[pts[value_field].notna()]
    if zero_is_null:
        pts = pts[pd.to_numeric(pts[value_field], errors="coerce").fillna(0) != 0]
    pts = pts[~pts.geometry.is_empty & pts.geometry.notna()]
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": np.nan, "n_obs": 0, "nearest_m": np.nan})
    if pts.empty:
        return out
    from sklearn.neighbors import KDTree

    xy = np.column_stack([pts.geometry.x.to_numpy(), pts.geometry.y.to_numpy()])
    tree = KDTree(xy)
    centres = np.column_stack([cells.geometry.centroid.x.to_numpy(), cells.geometry.centroid.y.to_numpy()])
    idx = tree.query_radius(centres, r=radius_m)
    vals = pd.to_numeric(pts[value_field], errors="coerce").to_numpy(dtype=float)
    extra = pd.to_numeric(pts[extra_field], errors="coerce").to_numpy(dtype=float) if extra_field else None
    nearest_d, _ = tree.query(centres, k=1)

    # A count is answerable everywhere: "no samples within 5 km" is zero, not unknown. Every other statistic
    # needs at least one observation, and stays null without one.
    values = np.zeros(len(cells)) if stat == "count" else np.full(len(cells), np.nan)
    counts = np.zeros(len(cells), dtype=int)
    for i, hit in enumerate(idx):
        if len(hit) == 0:
            continue
        v = vals[hit]
        v = v[np.isfinite(v)]
        counts[i] = len(v)
        if len(v) == 0:
            continue
        if stat == "max":
            values[i] = float(np.max(v))
        elif stat == "p90":
            values[i] = float(np.percentile(v, 90))
        elif stat == "mean":
            values[i] = float(np.mean(v))
        elif stat == "count":
            values[i] = float(len(v))
        elif stat == "min_extra" and extra is not None:
            e = extra[hit]
            e = e[np.isfinite(e)]
            values[i] = float(np.min(e)) if len(e) else np.nan
        else:
            raise RuntimeError(f"unknown stat {stat!r}")
    out["value"] = values
    out["n_obs"] = counts
    out["nearest_m"] = nearest_d[:, 0]
    return out


def point_year(
    cells: gpd.GeoDataFrame, layer_key: str, date_field: str, radius_m: float, stat: str = "min"
) -> pd.DataFrame:
    """Earliest or latest year among records within `radius_m`, read from a free-text date field.

    This is an effort feature: when someone first drilled nearby says nothing about the rock, which is exactly
    why the null model is allowed to use it and the geological model is not.
    """
    from ..export_web import _year_from_text

    pts = _layer(layer_key)
    if date_field not in pts.columns:
        raise RuntimeError(f"{layer_key}: no field {date_field!r}")
    years = pts[date_field].map(lambda v: _year_from_text(str(v)) if v is not None else None)
    keep = years.notna() & pts.geometry.notna() & ~pts.geometry.is_empty
    pts = pts[keep]
    years = years[keep].astype(float)
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": np.nan, "n_obs": 0, "nearest_m": np.nan})
    if pts.empty:
        return out
    from sklearn.neighbors import KDTree

    xy = np.column_stack([pts.geometry.x.to_numpy(), pts.geometry.y.to_numpy()])
    tree = KDTree(xy)
    centres = np.column_stack([cells.geometry.centroid.x.to_numpy(), cells.geometry.centroid.y.to_numpy()])
    hits = tree.query_radius(centres, r=radius_m)
    nearest_d, _ = tree.query(centres, k=1)
    yr = years.to_numpy()
    values = np.full(len(cells), np.nan)
    counts = np.zeros(len(cells), dtype=int)
    for i, hit in enumerate(hits):
        if len(hit) == 0:
            continue
        counts[i] = len(hit)
        values[i] = float(np.min(yr[hit]) if stat == "min" else np.max(yr[hit]))
    out["value"] = values
    out["n_obs"] = counts
    out["nearest_m"] = nearest_d[:, 0]
    return out


def idw(
    cells: gpd.GeoDataFrame, layer_key: str, value_field: str, k: int = 8, max_m: float = 25_000,
    power: float = 2.0,
) -> pd.DataFrame:
    """Inverse-distance interpolation from the k nearest observations, and null beyond `max_m`.

    Used for the unconformity depth, which only exists where a hole reached the contact. Outside the drilled
    ground the honest answer is "not known", so the cap is enforced rather than extrapolated through.
    """
    pts = _layer(layer_key)
    if value_field not in pts.columns:
        raise RuntimeError(f"{layer_key}: no field {value_field!r}")
    pts = pts[pd.to_numeric(pts[value_field], errors="coerce").notna()]
    pts = pts[~pts.geometry.is_empty & pts.geometry.notna()]
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": np.nan, "n_obs": 0, "nearest_m": np.nan})
    if pts.empty:
        return out
    from sklearn.neighbors import KDTree

    xy = np.column_stack([pts.geometry.x.to_numpy(), pts.geometry.y.to_numpy()])
    vals = pd.to_numeric(pts[value_field], errors="coerce").to_numpy(dtype=float)
    tree = KDTree(xy)
    centres = np.column_stack([cells.geometry.centroid.x.to_numpy(), cells.geometry.centroid.y.to_numpy()])
    kk = min(k, len(xy))
    dist, ind = tree.query(centres, k=kk)
    dist = np.atleast_2d(dist)
    ind = np.atleast_2d(ind)
    within = dist <= max_m
    weights = np.where(within, 1.0 / np.maximum(dist, 1.0) ** power, 0.0)
    got = weights.sum(axis=1)
    values = np.where(got > 0, (weights * vals[ind]).sum(axis=1) / np.where(got > 0, got, 1.0), np.nan)
    # an exact hit should return the observation itself, not a weighted blur of its neighbours
    exact = dist[:, 0] < 1.0
    values[exact] = vals[ind[exact, 0]]
    out["value"] = values
    out["n_obs"] = within.sum(axis=1)
    out["nearest_m"] = dist[:, 0]
    return out


def polygon_class(cells: gpd.GeoDataFrame, layer_key: str, field_name: str) -> pd.DataFrame:
    """The class of the polygon covering most of the cell, as text. Unmapped ground stays null."""
    polys = _layer(layer_key)
    if field_name not in polys.columns:
        raise RuntimeError(f"{layer_key}: no field {field_name!r}")
    polys = polys[~polys.geometry.is_empty & polys.geometry.notna()]
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": np.nan, "value_text": None, "n_obs": 0,
                        "nearest_m": np.nan})
    if polys.empty:
        return out
    pairs = gpd.sjoin(
        gpd.GeoDataFrame(cells[["cell_id"]], geometry=cells.geometry, crs=cells.crs),
        polys[[field_name, "geometry"]], how="inner", predicate="intersects",
    )
    if pairs.empty:
        return out
    cell_geom = dict(zip(cells["cell_id"], cells.geometry, strict=True))
    poly_geom = polys.geometry.to_numpy()
    best: dict[str, tuple[float, str]] = {}
    counts: dict[str, int] = {}
    for cell_id, idx, cls in zip(
        pairs["cell_id"].to_numpy(), pairs["index_right"].to_numpy(), pairs[field_name].to_numpy(), strict=True
    ):
        area = float(cell_geom[cell_id].intersection(poly_geom[idx]).area)
        if area <= 0:
            continue  # a polygon that only abuts the cell edge covers none of it
        counts[cell_id] = counts.get(cell_id, 0) + 1
        if area > best.get(cell_id, (0.0, ""))[0]:
            best[cell_id] = (area, str(cls) if cls is not None else "")
    out["value_text"] = out["cell_id"].map(lambda c: best.get(c, (0.0, None))[1] or None)
    out["n_obs"] = out["cell_id"].map(lambda c: counts.get(c, 0))
    return out


def graphitic_host(cells: gpd.GeoDataFrame) -> pd.DataFrame:
    """1 where the mapped bedrock names a graphitic or pelitic host; 0 where it is mapped as something else
    outside the basin; null if unmapped, and null inside the basin unless the map names a host there.

    With no public magnetic grid, the mapped lithology is the only route to the element the magnetic low is a
    proxy for: "pelitic-psammopelitic gneiss, the host of graphitic conductors" (Thomas and McHardy 2007, GSC
    Bulletin 588).
    Inside the basin outline the 1:250,000 map shows the Athabasca sandstone, which is the cover, not the
    basement the criterion asks about, so a non-host polygon there says nothing: unknown, not absent. The
    verifier of the staged analyst refused that reading on six of the first fifteen cells it saw, which is where
    the rule "absence of mapping is not absence of features" came from, and before this rule the criteria score counted it against 18,619 covered cells. A host unit mapped inside
    the outline (a basement window or the rim) still counts as 1.
    """
    cls = polygon_class(cells, "bedrock_250k", "LITHOLOGY")
    text = cls["value_text"].fillna("").str.lower()
    hit = text.apply(lambda t: any(w in t for w in GRAPHITIC_WORDS)).to_numpy()
    mapped = (cls["n_obs"] > 0).to_numpy()
    covered = cells["in_basin"].fillna(False).astype(bool).to_numpy() if "in_basin" in cells.columns \
        else np.zeros(len(cells), dtype=bool)
    value = np.where(hit & mapped, 1.0, np.where(mapped & ~covered, 0.0, np.nan))
    return pd.DataFrame({"cell_id": cls["cell_id"], "value": value, "n_obs": cls["n_obs"],
                         "nearest_m": np.nan})


def graphitic_host_surface(cells: gpd.GeoDataFrame) -> pd.DataFrame:
    """1 where the mapped bedrock at the surface names a graphitic or pelitic host, 0 where it is mapped as
    anything else (the sandstone cover included), null if unmapped.

    This is `graphitic_host` before the cover rule, kept under an honest name for the learned model, which
    fits on complete cases and imputes nothing: a feature that is null under the basin would drop every
    covered cell from its frame, and the deposits are the covered cells. Read as "a host is mapped at the
    surface" it is complete wherever the map is, and a 0 under the cover is a true statement about the
    surface. The criteria score and the analyst read `graphitic_host`, which says unknown there.
    """
    cls = polygon_class(cells, "bedrock_250k", "LITHOLOGY")
    text = cls["value_text"].fillna("").str.lower()
    hit = text.apply(lambda t: any(w in t for w in GRAPHITIC_WORDS))
    value = np.where(cls["n_obs"] > 0, hit.astype(float), np.nan)
    return pd.DataFrame({"cell_id": cls["cell_id"], "value": value, "n_obs": cls["n_obs"], "nearest_m": np.nan})


def polygon_overlap_count(cells: gpd.GeoDataFrame, layer_key: str) -> pd.DataFrame:
    """How many polygons cover the cell: for survey footprints, a direct measure of exploration effort."""
    polys = _layer(layer_key)
    polys = polys[~polys.geometry.is_empty & polys.geometry.notna()]
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": 0.0, "n_obs": 0, "nearest_m": np.nan})
    if polys.empty:
        return out
    pairs = gpd.sjoin(
        gpd.GeoDataFrame(cells[["cell_id"]], geometry=cells.geometry, crs=cells.crs),
        polys[["geometry"]], how="inner", predicate="intersects",
    )
    if pairs.empty:
        return out
    cell_geom = dict(zip(cells["cell_id"], cells.geometry, strict=True))
    poly_geom = polys.geometry.to_numpy()
    counts: dict[str, int] = {}
    for cell_id, idx in zip(pairs["cell_id"].to_numpy(), pairs["index_right"].to_numpy(), strict=True):
        # a footprint that only abuts the cell edge did not survey it
        if cell_geom[cell_id].intersection(poly_geom[idx]).area > 0:
            counts[cell_id] = counts.get(cell_id, 0) + 1
    out["value"] = out["cell_id"].map(lambda c: float(counts.get(c, 0)))
    out["n_obs"] = out["value"].astype(int)
    return out


# ---------------------------------------------------------------- the register of features

SPECS: tuple[FeatureSpec, ...] = (
    # ---- pathway
    FeatureSpec("d_conductor_m", "Distance to nearest EM conductor", "pathway", "distance_to_lines",
                ("em_conductors",), unit="m",
                notes="Graphitic basement units are the key empirical locators for Athabasca uranium; a "
                      "conductor trace plots up-dip of blind ore, so proximity is a hint, not a position."),
    FeatureSpec("conductor_density", "EM conductor length per km2", "pathway", "line_density",
                ("em_conductors",), unit="km/km2"),
    FeatureSpec("graphitic_host", "Mapped bedrock names a graphitic or pelitic host", "pathway",
                "graphitic_host", ("bedrock_250k",),
                notes="Null where the 1:250,000 map has no polygon, and null inside the basin outline unless the "
                      "map names a host there: the sandstone cover says nothing about the basement. Unmapped "
                      "and covered are not the same as absent. The criteria score and the analyst read this."),
    FeatureSpec("graphitic_host_surface", "A graphitic or pelitic host is mapped at the surface", "pathway",
                "graphitic_host_surface", ("bedrock_250k",),
                notes="The same map read as a statement about the surface: 0 under the sandstone cover is true "
                      "of the surface and says nothing about the basement. The learned model, which fits on "
                      "complete cases, reads this one so that covered cells stay in its frame."),
    # ---- trap
    FeatureSpec("d_fault_m", "Distance to nearest mapped fault or lineament", "trap", "distance_to_lines",
                ("faults_250k",), unit="m",
                params={"where": "FEAT_TYPE == 'Fault' or FEAT_TYPE == 'Lineament' or FEAT_TYPE.isna()"}),
    FeatureSpec("fault_density", "Fault and lineament length per km2", "trap", "line_density",
                ("faults_250k",), unit="km/km2"),
    # ---- cover
    FeatureSpec("unconformity_depth_m", "Interpolated depth to the base of the Athabasca sandstone", "cover",
                "idw", ("compilation",), unit="m",
                params={"value_field": "BASE_OF_ATHABASCA_SG_DEPTH_M", "k": 8, "max_m": 25000},
                notes="Only holes that reached the contact carry this depth, so the interpolation is capped "
                      "at 25 km from the nearest observation and null beyond it."),
    FeatureSpec("surficial_class", "Dominant surficial environment", "cover", "polygon_class",
                ("surficial_250k",), params={"field_name": "MAIN_ENVIRONMENT"}),
    # ---- detection
    FeatureSpec("sed_u_max_ppm", "Highest lake-sediment uranium within 5 km", "detection", "point_stat",
                ("lake_sediment_gsc",), unit="ppm",
                params={"value_field": "U", "radius_m": 5000, "stat": "max"},
                notes="A dispersion signal: it says something upstream sheds uranium, not that ore is here."),
    FeatureSpec("sed_u_max_ppm_sgs", "Highest lake-sediment uranium within 5 km (1975-1978 survey)",
                "detection", "point_stat", ("lake_sediment_sgs",), unit="ppm",
                params={"value_field": "U_PPM", "radius_m": 5000, "stat": "max"}),
    FeatureSpec("water_u_max_ppm", "Highest lake-water uranium within 5 km", "detection", "point_stat",
                ("lake_water_sgs",), unit="ppm",
                params={"value_field": "U_PPM", "radius_m": 5000, "stat": "max"}),
    # ---- dispersal
    FeatureSpec("boulder_max_cps", "Highest radioactive-boulder count rate within 5 km", "dispersal",
                "point_stat", ("radioactive_boulders",), unit="cps",
                params={"value_field": "CPS", "radius_m": 5000, "stat": "max", "zero_is_null": True},
                notes="A boulder train points up-ice: at Patterson Lake the discovery hole sat 3.8 km up-ice "
                      "of the boulders, so this is a vector, not a location. A record at exactly 0 cps is a "
                      "filled-in blank, not a reading, and is left out of the statistic."),
    # ---- effort: the null model's whole world
    FeatureSpec("holes_n", "Provincial drillhole collars within 2 km", "effort", "point_stat",
                ("compilation",), is_effort=True, is_count=True,
                params={"value_field": "OBJECTID", "radius_m": 2000, "stat": "count"}),
    FeatureSpec("holes_first_year", "Earliest drilling year within 5 km", "effort", "point_year",
                ("compilation",), is_effort=True, unit="year",
                params={"date_field": "DATE_DRILLED", "radius_m": 5000, "stat": "min"},
                notes="When someone first drilled nearby: exploration history, not geology."),
    FeatureSpec("sed_samples_n", "Lake-sediment samples within 5 km", "effort", "point_stat",
                ("lake_sediment_gsc",), is_effort=True, is_count=True,
                params={"value_field": "U", "radius_m": 5000, "stat": "count"}),
    FeatureSpec("boulder_samples_n", "Radioactive-boulder records within 5 km", "effort", "point_stat",
                ("radioactive_boulders",), is_effort=True, is_count=True,
                params={"value_field": "CPS", "radius_m": 5000, "stat": "count"}),
    FeatureSpec("airborne_surveys_n", "Airborne survey footprints covering the cell", "effort",
                "polygon_overlap_count", ("survey_footprints_airborne",), is_effort=True, is_count=True),
    FeatureSpec("ground_surveys_n", "Ground survey footprints covering the cell", "effort",
                "polygon_overlap_count", ("survey_footprints_ground",), is_effort=True, is_count=True),
)

BUILDERS: dict[str, Callable[..., pd.DataFrame]] = {
    "distance_to_lines": distance_to_lines,
    "line_density": line_density,
    "point_stat": point_stat,
    "idw": idw,
    "polygon_class": polygon_class,
    "graphitic_host": graphitic_host,
    "graphitic_host_surface": graphitic_host_surface,
    "polygon_overlap_count": polygon_overlap_count,
    "point_year": point_year,
}


def label_keys(inv: Inventory | None = None) -> set[str]:
    """Layer keys that are labels. No feature may read one, and the leakage test checks it."""
    inv = inv or load_inventory()
    return {s.key for s in inv.with_role("label")}


def build(
    only: list[str] | None = None, grid_id: str | None = None, log: Callable[[str], None] = print
) -> dict[str, Any]:
    """Build every feature into `derived.cell_feature`, recording coverage and inputs per value."""
    inv = load_inventory()
    labels = label_keys(inv)
    cells = load_cells(grid_id)
    log(f"  {len(cells)} cells, {len(SPECS)} features")
    specs = [s for s in SPECS if not only or s.key in only]
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    written: dict[str, dict[str, Any]] = {}
    frames: list[pd.DataFrame] = []

    for spec in specs:
        leaked = set(spec.source_keys) & labels
        if leaked:
            raise RuntimeError(f"{spec.key}: reads label layer(s) {sorted(leaked)}; labels are never features")
        builder = BUILDERS[spec.op]
        kwargs = dict(spec.params)
        if spec.op in ("distance_to_lines", "line_density", "point_stat", "idw", "polygon_class",
                       "polygon_overlap_count", "point_year"):
            kwargs["layer_key"] = spec.source_keys[0]
        df = builder(cells, **kwargs)
        df = df.assign(
            feature_key=spec.key, unit=spec.unit, from_tier=spec.from_tier, op=spec.op, tool=TOOL,
            params=json.dumps(spec.params), inputs=json.dumps(list(spec.source_keys)),
            computed_at=now,
        )
        if "value_text" not in df.columns:
            df["value_text"] = None
        frames.append(df[["cell_id", "feature_key", "value", "value_text", "unit", "n_obs", "nearest_m",
                          "from_tier", "op", "tool", "params", "inputs", "computed_at"]])
        # a count answers everywhere, a distance answers everywhere; everything else needs an observation
        have = (int(df["value"].notna().sum()) if spec.is_count or spec.op == "distance_to_lines"
                else int((df["n_obs"] > 0).sum()))
        written[spec.key] = {"cells_with_value": have, "share": round(have / max(len(cells), 1), 3)}
        log(f"    {spec.key:22} {written[spec.key]['share']:.1%} of cells")

    con = connect()
    try:
        keys = [s.key for s in specs]
        con.execute(
            f"delete from derived.cell_feature where feature_key in ({','.join('?' * len(keys))})", keys
        )
        append_frame(con, "derived", "cell_feature", pd.concat(frames, ignore_index=True), "derived")
        spec_rows = pd.DataFrame([{
            "feature_key": s.key, "title": s.title, "unit": s.unit, "from_tier": s.from_tier,
            "source_keys": json.dumps(list(s.source_keys)), "bears_on": s.bears_on,
            "is_effort": s.is_effort, "is_label": False, "is_count": s.is_count, "notes": s.notes,
        } for s in specs])
        con.execute(
            f"delete from derived.feature_spec where feature_key in ({','.join('?' * len(keys))})", keys
        )
        append_frame(con, "derived", "feature_spec", spec_rows, "derived")
        total = con.execute("select count(*) from derived.cell_feature").fetchone()
    finally:
        con.close()
    log(f"  derived.cell_feature: {total[0] if total else 0} rows")
    return {"cells": len(cells), "features": written}
