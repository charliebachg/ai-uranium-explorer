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
    negative_is_zero: bool = False,
    text_field: str | None = None,
    text_words: tuple[str, ...] = (),
) -> pd.DataFrame:
    """A statistic over points within `radius_m` of the cell centre, with the sample count beside it.

    A cell with no sample in range gets a null value and n_obs 0: the sampling frame here is lakes, so "no
    anomaly" and "never sampled" are different facts and must stay different. `zero_is_null` reads a value
    of exactly 0 as a missing measurement: the radioactive-boulder layer carries 2,127 records at 0.0 cps
    among 6,591, a filled-in blank, and the staged analyst's verifier refused a node that read it as a
    measured absence: a filled-in blank is no reading, and unknown and absent stay distinct (the PRD's standing
    constraints). A record so dropped still counts for the effort features, which count records, not readings.

    `negative_is_zero` reads a negative value as below detection: the 1975-1978 survey writes a result under
    its detection limit as a negative number (468 of 3,001 lead values), which is a low reading, not a missing
    one. `text_field` and `text_words` keep only the records whose text names one of the words, lower-cased,
    so a statistic can be taken over one kind of record (the sandstone boulders, say).
    """
    pts = _layer(layer_key)
    if value_field not in pts.columns:
        raise RuntimeError(f"{layer_key}: no field {value_field!r} (have {sorted(pts.columns)[:12]})")
    pts = pts[pts[value_field].notna()]
    if text_field:
        pts = pts[_names_any(pts[text_field], text_words)]
    if zero_is_null:
        pts = pts[pd.to_numeric(pts[value_field], errors="coerce").fillna(0) != 0]
    if negative_is_zero:
        pts = pts.assign(**{value_field: pd.to_numeric(pts[value_field], errors="coerce").clip(lower=0.0)})
    return _point_stat(cells, pts, value_field, radius_m, stat, extra_field)


def _names_any(text: pd.Series, words: tuple[str, ...]) -> np.ndarray:
    """True where the lower-cased text contains any of the words."""
    low = text.fillna("").astype(str).str.lower()
    return low.apply(lambda t: any(w in t for w in words)).to_numpy(dtype=bool)


def _point_stat(cells: gpd.GeoDataFrame, pts: gpd.GeoDataFrame, value_field: str, radius_m: float,
                stat: str = "max", extra_field: str | None = None) -> pd.DataFrame:
    """The statistic itself, over points already filtered."""
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


# ---------------------------------------------------------------- the extended evidence
#
# The builders above take one number per layer. The store holds more: every lake-sediment sample carries
# thorium, loss on ignition, lead or nickel beside its uranium; every boulder a rock type; every fault a trend;
# every bedrock polygon a tectonic domain. The builders below turn that into features, so the fitted model is
# given what the analyst is shown raw, and a comparison between the two is a comparison of readers.

#: boulder rock types that come from the Athabasca sandstone itself. A radioactive sandstone boulder was carried
#: from altered cover over a source; a radioactive granite or pegmatite boulder usually was not.
SANDSTONE_WORDS = ("sandstone", "conglomerat", "conglomerit", "athabasca", "arenite")

#: lithology groups for the 338 bedrock descriptions, first match wins. The graphitic-pelitic group comes before
#: gneiss because "pelitic gneiss" is a host, not a gneiss.
LITH_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sandstone", ("sandstone", "arenite", "conglomerat", "conglomerit")),
    ("graphitic_pelite", ("graphit", "pelit")),
    ("quartzite_psammite", ("quartzit", "psammit", "arkos")),
    ("calc_silicate", ("calc", "marble", "dolom", "carbonat")),
    ("mafic", ("amphibolit", "gabbr", "diabas", "basalt", "mafic", "norit")),
    ("granitoid", ("granit", "tonalit", "monzon", "syenit", "pegmat", "diorit", "charnock")),
    ("gneiss", ("gneiss", "migmatit", "schist")),
)

#: the tectonic domains, as the 1:250,000 map names them, that cover at least 1.5% of the grid. A cell in any
#: other domain is zero on all of them; an unmapped cell is null on all of them.
DOMAIN_LEVELS = ("Athabasca Basin", "Mudjatik", "Wollaston", "Zemlak", "Beaverlodge", "Taltson", "Tantato",
                 "Phanerozoic Basin")
#: domains that are cover, not basement: their edges are not boundaries between basement domains
COVER_DOMAINS = ("Athabasca Basin", "Phanerozoic Basin")

#: fault trend sectors on the compass axis (0-180 from north), 45 degrees wide
TRENDS: dict[str, tuple[float, float]] = {"ns": (157.5, 22.5), "ne": (22.5, 67.5), "ew": (67.5, 112.5),
                                          "nw": (112.5, 157.5)}

#: the direction the last ice sheet moved over the basin, as a compass bearing: toward the south-west. An
#: assumption, used only to choose which end of a landform axis is down-ice (see `down_ice_bearing`).
REGIONAL_ICE_FLOW_DEG = 225.0
#: a grain weaker than this says nothing, and the cell takes the regional axis instead
MIN_GRAIN_COHERENCE = 0.5
#: a share of samples above a threshold needs this many samples before it is a share
MIN_SHARE_SAMPLES = 3

#: per-build memo of polygon classes, so the eight domain levels read the map once. Only `build` sets it.
_MEMO: dict[tuple[Any, ...], pd.DataFrame] | None = None


def slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text.strip().lower()).strip("_")


def lith_group(text: Any) -> str | None:
    """The lithology group a bedrock description falls in, or None for an empty description."""
    low = str(text or "").strip().lower()
    if not low:
        return None
    for group, words in LITH_GROUPS:
        if any(w in low for w in words):
            return group
    return "other"


def point_ratio(cells: gpd.GeoDataFrame, layer_key: str, num_field: str, den_field: str, radius_m: float,
                den_floor: float, stat: str = "max") -> pd.DataFrame:
    """A statistic over a ratio taken sample by sample, within `radius_m` of the cell centre.

    A negative numerator is below detection and reads as zero. A sample whose denominator is under `den_floor`
    is left out, because a ratio over a near-zero denominator is a division, not a measurement: uranium over a
    thorium of 0.1 ppm, or over 1% loss on ignition, would be the largest number in the basin for no reason."""
    pts = _layer(layer_key)
    for f in (num_field, den_field):
        if f not in pts.columns:
            raise RuntimeError(f"{layer_key}: no field {f!r}")
    num = pd.to_numeric(pts[num_field], errors="coerce").clip(lower=0.0)
    den = pd.to_numeric(pts[den_field], errors="coerce")
    ok = (num.notna() & den.notna() & (den >= den_floor)).to_numpy()
    sub = pts[ok].assign(_ratio=(num[ok] / den[ok]).to_numpy(dtype=float))
    return _point_stat(cells, sub, "_ratio", radius_m, stat)


def point_share_above(cells: gpd.GeoDataFrame, layer_key: str, value_field: str, radius_m: float,
                      quantile: float = 0.95, min_obs: int = MIN_SHARE_SAMPLES) -> pd.DataFrame:
    """The share of samples within `radius_m` whose value is above the layer's own `quantile`.

    A share and not a count, because a count grows with how many lakes were sampled, which is effort. Fewer
    than `min_obs` samples is no share: one lake above the line is one lake."""
    pts = _layer(layer_key)
    vals = pd.to_numeric(pts[value_field], errors="coerce")
    threshold = float(np.nanquantile(vals.to_numpy(dtype=float), quantile))
    sub = pts[vals.notna().to_numpy()].assign(_above=(vals[vals.notna()] > threshold).astype(float).to_numpy())
    out = _point_stat(cells, sub, "_above", radius_m, "mean")
    out.loc[out["n_obs"] < min_obs, "value"] = np.nan
    return out


def grain_bearing(grain_deg: Any) -> np.ndarray:
    """The landform grain as a compass axis, 0-180 degrees from north.

    `landform_grain_deg` is measured from east and turns clockwise, because the DEM's rows run south
    (`rasters._landform_grain`): 0 is an east-west grain, 90 a north-south one. The compass axis is 90 on."""
    return np.mod(90.0 + np.asarray(grain_deg, dtype=float), 180.0)


def down_ice_bearing(axis_deg: Any, regional_deg: float = REGIONAL_ICE_FLOW_DEG) -> np.ndarray:
    """The end of a compass axis taken as down-ice: whichever end is nearer the regional flow direction.

    An axis cannot say which way the ice went (`rasters._landform_grain` says so, and the boulder criterion
    stays two-sided for it). The sense here is an assumption, not a measurement: the last ice sheet crossed the
    basin toward the south-west, and the grain agrees for most cells (median axis 41 to 221 degrees). The
    up-ice cone is built beside the down-ice one, so if the assumption is wrong the two features trade places
    in the model rather than failing silently."""
    a = np.mod(np.asarray(axis_deg, dtype=float), 180.0)
    b = a + 180.0

    def off(x: np.ndarray) -> np.ndarray:
        return np.abs(np.mod(x - regional_deg + 180.0, 360.0) - 180.0)

    return np.where(off(a) <= off(b), a, b)


def _regional_axis(axis: np.ndarray, weight: np.ndarray) -> float:
    """The mean of axes, averaged as doubled angles because 179 and 1 degrees are nearly the same axis."""
    ok = np.isfinite(axis) & np.isfinite(weight) & (weight > 0)
    if not ok.any():
        return float(np.mod(REGIONAL_ICE_FLOW_DEG, 180.0))
    t = np.radians(2.0 * axis[ok])
    return float(np.mod(np.degrees(np.arctan2((weight[ok] * np.sin(t)).sum(), (weight[ok] * np.cos(t)).sum())) / 2.0,
                        180.0))


def cell_down_ice(cells: gpd.GeoDataFrame) -> np.ndarray:
    """Each cell's down-ice bearing: its own grain where the grain is coherent, the regional axis elsewhere.

    A `down_ice_deg` column on the cells is used as it is (tests set one); otherwise the grain is read from the
    DEM features already in the store."""
    if "down_ice_deg" in cells.columns:
        return cells["down_ice_deg"].to_numpy(dtype=float)
    return down_ice_frame(cells)["down_ice_deg"].to_numpy(dtype=float)


def down_ice_frame(cells: gpd.GeoDataFrame) -> pd.DataFrame:
    """`cell_id`, `down_ice_deg` and `from_grain`: whether the bearing is the cell's own coherent grain (True)
    or the regional axis standing in for a weak or missing one (False)."""
    con = connect(read_only=True)
    try:
        rows = con.execute(
            "select cell_id, feature_key, value from derived.cell_feature "
            "where feature_key in ('landform_grain_deg', 'grain_coherence')"
        ).df()
    finally:
        con.close()
    wide = rows.pivot_table(index="cell_id", columns="feature_key", values="value") if len(rows) else pd.DataFrame()
    grain = cells["cell_id"].map(wide.get("landform_grain_deg", pd.Series(dtype=float))).to_numpy(dtype=float)
    coh = cells["cell_id"].map(wide.get("grain_coherence", pd.Series(dtype=float))).to_numpy(dtype=float)
    axis = grain_bearing(grain)
    good = np.isfinite(axis) & (np.nan_to_num(coh) >= MIN_GRAIN_COHERENCE)
    regional = _regional_axis(np.where(good, axis, np.nan), np.where(good, coh, 0.0))
    return pd.DataFrame({"cell_id": cells["cell_id"].to_numpy(),
                         "down_ice_deg": down_ice_bearing(np.where(good, axis, regional)), "from_grain": good})


def _bearings(dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
    """Compass bearings of offsets in the grid's projected coordinates (grid north; the few degrees of
    convergence from true north are small beside a 30-degree cone)."""
    return np.mod(np.degrees(np.arctan2(dx, dy)), 360.0)


def point_cone(cells: gpd.GeoDataFrame, layer_key: str, value_field: str, radius_m: float,
               half_angle_deg: float = 30.0, sense: str = "down", min_m: float = 1000.0,
               zero_is_null: bool = True) -> pd.DataFrame:
    """The highest reading among points in a cone from the cell centre, down-ice or up-ice of it.

    A boulder down-ice of a cell may have come from it; one up-ice of it cannot have. The cone opens from the
    cell centre along the cell's down-ice bearing (`cell_down_ice`), `half_angle_deg` either side, out to
    `radius_m`, and leaves out the points within `min_m` (the cell itself, which `boulder_max_cps` reads).
    No point in the cone is null, not zero: boulder records are placed where somebody looked."""
    if sense not in ("down", "up"):
        raise ValueError(f"sense must be 'down' or 'up', not {sense!r}")
    pts = _layer(layer_key)
    vals = pd.to_numeric(pts[value_field], errors="coerce")
    keep = vals.notna().to_numpy() & ~pts.geometry.is_empty.to_numpy() & pts.geometry.notna().to_numpy()
    if zero_is_null:
        keep &= vals.fillna(0).to_numpy() != 0
    pts, v = pts[keep], vals[keep].to_numpy(dtype=float)
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": np.nan, "n_obs": 0, "nearest_m": np.nan})
    if pts.empty:
        return out
    from sklearn.neighbors import KDTree

    px, py = pts.geometry.x.to_numpy(), pts.geometry.y.to_numpy()
    cx, cy = cells.geometry.centroid.x.to_numpy(), cells.geometry.centroid.y.to_numpy()
    idx, dist = KDTree(np.column_stack([px, py])).query_radius(np.column_stack([cx, cy]), r=radius_m,
                                                              return_distance=True)
    target = np.mod(cell_down_ice(cells) + (180.0 if sense == "up" else 0.0), 360.0)
    values, counts, nearest = np.full(len(cells), np.nan), np.zeros(len(cells), dtype=int), np.full(len(cells), np.nan)
    for i, (hit, d) in enumerate(zip(idx, dist, strict=True)):
        if len(hit) == 0 or not np.isfinite(target[i]):
            continue
        far = d >= min_m
        hit, d = hit[far], d[far]
        off = np.abs(np.mod(_bearings(px[hit] - cx[i], py[hit] - cy[i]) - target[i] + 180.0, 360.0) - 180.0)
        inside = off <= half_angle_deg
        if inside.any():
            values[i] = float(v[hit[inside]].max())
            counts[i] = int(inside.sum())
            nearest[i] = float(d[inside].min())
    out["value"], out["n_obs"], out["nearest_m"] = values, counts, nearest
    return out


def _segments(layer_key: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Every line cut at its vertices: midpoint x and y, length in metres, compass axis 0-180."""
    import shapely

    lines = _layer(layer_key)
    lines = lines[~lines.geometry.is_empty & lines.geometry.notna()].explode(index_parts=False)
    coords, part = shapely.get_coordinates(lines.geometry.to_numpy(), return_index=True)
    same = part[1:] == part[:-1]
    a, b = coords[:-1][same], coords[1:][same]
    d = b - a
    length = np.hypot(d[:, 0], d[:, 1])
    axis = np.mod(np.degrees(np.arctan2(d[:, 0], d[:, 1])), 180.0)
    mid = (a + b) / 2.0
    ok = length > 0
    return mid[ok, 0], mid[ok, 1], length[ok], axis[ok]


def line_length_by_trend(cells: gpd.GeoDataFrame, layer_key: str, radius_m: float, trend: str) -> pd.DataFrame:
    """Kilometres of line within `radius_m` of the cell centre whose trend falls in one 45-degree sector.

    A segment counts where its midpoint falls; at the map's vertex spacing that is a few hundred metres of
    rounding on a 5 km radius. Zero is an answer: the lineament map covers the whole study area."""
    lo, hi = TRENDS[trend]
    mx, my, length, axis = _segments(layer_key)
    sel = (axis >= lo) | (axis < hi) if lo > hi else (axis >= lo) & (axis < hi)
    mx, my, length = mx[sel], my[sel], length[sel]
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": 0.0, "n_obs": 0, "nearest_m": np.nan})
    if len(length) == 0:
        return out
    from sklearn.neighbors import KDTree

    centres = np.column_stack([cells.geometry.centroid.x.to_numpy(), cells.geometry.centroid.y.to_numpy()])
    idx = KDTree(np.column_stack([mx, my])).query_radius(centres, r=radius_m)
    out["value"] = [float(length[h].sum()) / 1000.0 for h in idx]
    out["n_obs"] = [len(h) for h in idx]
    return out


def crossing_points(layer_key: str, other_key: str | None = None) -> np.ndarray:
    """Points where the lines of one layer cross each other, or cross another layer's lines.

    Within one layer, two lines that only meet at an end are one lineament drawn in two pieces, not a
    crossing, so a point within a metre of either line's end is dropped. Overlapping stretches (a line result)
    are not crossings either."""
    import shapely

    a = _layer(layer_key)
    a = a[~a.geometry.is_empty & a.geometry.notna()].geometry.to_numpy()
    same = other_key is None or other_key == layer_key
    b = a if same else _layer(other_key)
    if not same:
        b = b[~b.geometry.is_empty & b.geometry.notna()].geometry.to_numpy()
    if len(a) == 0 or len(b) == 0:
        return np.zeros((0, 2))
    ia, ib = shapely.STRtree(b).query(a, predicate="intersects")
    if same:
        keep = ia < ib
        ia, ib = ia[keep], ib[keep]
    if len(ia) == 0:
        return np.zeros((0, 2))
    parts, which = shapely.get_parts(shapely.intersection(a[ia], b[ib]), return_index=True)
    is_point = shapely.get_type_id(parts) == 0
    parts, which = parts[is_point], which[is_point]
    if same and len(parts):
        ends = [shapely.get_point(a[ia[which]], 0), shapely.get_point(a[ia[which]], -1),
                shapely.get_point(b[ib[which]], 0), shapely.get_point(b[ib[which]], -1)]
        touching = np.any([shapely.distance(parts, e) < 1.0 for e in ends], axis=0)
        parts = parts[~touching]
    xy = shapely.get_coordinates(parts)
    return np.unique(np.round(xy, 0), axis=0) if len(xy) else np.zeros((0, 2))


def line_crossings(cells: gpd.GeoDataFrame, layer_key: str, radius_m: float,
                   other_key: str | None = None) -> pd.DataFrame:
    """How many line crossings lie within `radius_m` of the cell centre: fault on fault, or fault on conductor.
    A crossing is where two structures can make the dilation a deposit needs; zero is an answer."""
    xy = crossing_points(layer_key, other_key)
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": 0.0, "n_obs": 0, "nearest_m": np.nan})
    if len(xy) == 0:
        return out
    from sklearn.neighbors import KDTree

    tree = KDTree(xy)
    centres = np.column_stack([cells.geometry.centroid.x.to_numpy(), cells.geometry.centroid.y.to_numpy()])
    counts = np.array([len(h) for h in tree.query_radius(centres, r=radius_m)])
    out["value"] = counts.astype(float)
    out["n_obs"] = counts
    out["nearest_m"] = tree.query(centres, k=1)[0][:, 0]
    return out


@lru_cache(maxsize=None)
def _basin_outline() -> Any:
    from .grid import basin_outline

    return basin_outline(GRID_EPSG)


def basin_edge_distance(cells: gpd.GeoDataFrame) -> pd.DataFrame:
    """Metres from the cell centre to the edge of the Athabasca sandstone: positive inside, negative outside.
    Many deposits sit near the basin's rim, where the unconformity is shallow; the sign says which side."""
    import shapely

    basin = _basin_outline()
    centres = cells.geometry.centroid.to_numpy()
    d = shapely.distance(basin.boundary, centres)
    inside = shapely.contains(basin, centres)
    return pd.DataFrame({"cell_id": cells["cell_id"], "value": np.where(inside, d, -d), "n_obs": 1,
                         "nearest_m": d})


def _dominant(cells: gpd.GeoDataFrame, layer_key: str, field_name: str) -> pd.DataFrame:
    """`polygon_class`, read once per build for each layer and field."""
    if _MEMO is None:
        return polygon_class(cells, layer_key, field_name)
    key = (layer_key, field_name, len(cells))
    if key not in _MEMO:
        _MEMO[key] = polygon_class(cells, layer_key, field_name)
    return _MEMO[key]


def polygon_is(cells: gpd.GeoDataFrame, layer_key: str, field_name: str, level: str) -> pd.DataFrame:
    """1 where the polygon covering most of the cell is `level`, 0 where it is mapped as anything else, and
    null where the map has no polygon: one column of a one-hot encoding the fitted model can take."""
    cls = _dominant(cells, layer_key, field_name)
    text = cls["value_text"].fillna("").astype(str).str.strip()
    mapped = (cls["n_obs"] > 0).to_numpy()
    value = np.where(mapped, (text == level).astype(float), np.nan)
    return pd.DataFrame({"cell_id": cls["cell_id"], "value": value, "n_obs": cls["n_obs"], "nearest_m": np.nan})


def dissolved_domains(layer_key: str, field_name: str, skip: tuple[str, ...] = COVER_DOMAINS) -> dict[str, Any]:
    """Each basement domain the map names, dissolved into one geometry."""
    import shapely

    polys = _layer(layer_key)
    polys = polys[~polys.geometry.is_empty & polys.geometry.notna()]
    names = polys[field_name].fillna("").astype(str).str.strip()
    polys = polys[(names != "") & ~names.isin(skip)].assign(_d=names)
    return {d: shapely.union_all(g.geometry.to_numpy()) for d, g in polys.groupby("_d")}


def domain_boundary(layer_key: str, field_name: str, skip: tuple[str, ...] = COVER_DOMAINS,
                    tolerance_m: float = 50.0) -> Any:
    """The mapped boundaries between basement domains as one geometry, or None if the map has fewer than two."""
    import shapely

    grown = {d: shapely.buffer(g, tolerance_m) for d, g in dissolved_domains(layer_key, field_name, skip).items()}
    keys = sorted(grown)
    edges = [shapely.intersection(grown[p], grown[q]) for i, p in enumerate(keys) for q in keys[i + 1:]]
    edges = [e for e in edges if not e.is_empty]
    return shapely.union_all(edges) if edges else None


def distance_to_domain_boundary(cells: gpd.GeoDataFrame, layer_key: str, field_name: str,
                                skip: tuple[str, ...] = COVER_DOMAINS, tolerance_m: float = 50.0) -> pd.DataFrame:
    """Metres from the cell centre to the nearest mapped boundary between two basement domains.

    Each domain is dissolved and grown by `tolerance_m`; where two grown domains overlap is their shared
    boundary, which survives the small gaps and overlaps a digitised map has along a contact. Under the
    sandstone no domain is mapped, so a covered cell is measured to the nearest boundary outside the cover."""
    import shapely

    boundary = domain_boundary(layer_key, field_name, skip, tolerance_m)
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": np.nan, "n_obs": 0, "nearest_m": np.nan})
    if boundary is None:
        return out
    d = shapely.distance(boundary, cells.geometry.centroid.to_numpy())
    out["value"], out["n_obs"], out["nearest_m"] = d, 1, d
    return out


def lith_group_count(cells: gpd.GeoDataFrame, layer_key: str, field_name: str, radius_m: float) -> pd.DataFrame:
    """How many lithology groups (`LITH_GROUPS`) the bedrock map shows within `radius_m` of the cell centre.

    One for a uniform block, several where contacts crowd together. Read at the surface: under the sandstone
    the map shows the cover, so a covered cell is one group, which is true of the surface and says nothing
    about the basement. Null where the map has no polygon in range."""
    polys = _layer(layer_key)
    polys = polys[~polys.geometry.is_empty & polys.geometry.notna()]
    groups = polys[field_name].map(lith_group)
    polys = polys.assign(_g=groups)[groups.notna().to_numpy()]
    out = pd.DataFrame({"cell_id": cells["cell_id"], "value": np.nan, "n_obs": 0, "nearest_m": np.nan})
    if polys.empty:
        return out
    discs = gpd.GeoDataFrame(cells[["cell_id"]], geometry=cells.geometry.centroid.buffer(radius_m), crs=cells.crs)
    pairs = gpd.sjoin(discs, polys[["_g", "geometry"]], how="inner", predicate="intersects")
    per = pairs.groupby("cell_id")["_g"].agg(["nunique", "size"])
    out["value"] = out["cell_id"].map(per["nunique"]).astype(float)
    out["n_obs"] = out["cell_id"].map(per["size"]).fillna(0).astype(int)
    return out


# ---------------------------------------------------------------- the register of features

#: the extended evidence: what the layers carry beyond one number each (see `extended`). Kept as its own
#: block so a benchmark pack built before these features can leave them out and stay as it was.
EXTENDED_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec("sed_u_th_max", "Highest uranium-to-thorium ratio in lake sediment within 5 km", "detection",
                "point_ratio", ("lake_sediment_gsc",),
                params={"num_field": "U_INA", "den_field": "TH_INA", "radius_m": 5000, "den_floor": 0.5},
                notes="Both by neutron activation, on the 6,041 samples that have them. Thorium travels with the "
                      "detrital load and uranium also in solution, so a high ratio is uranium beyond what the "
                      "sediment's own minerals explain."),
    FeatureSpec("sed_u_loi_max", "Highest lake-sediment uranium per percent loss on ignition within 5 km",
                "detection", "point_ratio", ("lake_sediment_gsc",), unit="ppm/%",
                params={"num_field": "U", "den_field": "LOI", "radius_m": 5000, "den_floor": 5.0},
                notes="Organic matter scavenges uranium, so a raw maximum partly measures how organic the lake "
                      "is. Samples under 5% loss on ignition are left out rather than divided into."),
    FeatureSpec("sed_pb_max_ppm", "Highest lake-sediment lead within 5 km (1975-1978 survey)", "detection",
                "point_stat", ("lake_sediment_sgs",), unit="ppm",
                params={"value_field": "PB_PPM", "radius_m": 5000, "stat": "max", "negative_is_zero": True},
                notes="Radiogenic lead is a pathfinder for uranium that has sat in place a long time. A negative "
                      "result is the survey's way of writing below detection, read as zero."),
    FeatureSpec("sed_ni_max_ppm", "Highest lake-sediment nickel within 5 km (1975-1978 survey)", "detection",
                "point_stat", ("lake_sediment_sgs",), unit="ppm",
                params={"value_field": "NI_PPM", "radius_m": 5000, "stat": "max", "negative_is_zero": True},
                notes="Nickel accompanies the basement-hosted and unconformity ores of the eastern basin."),
    FeatureSpec("sed_u_anomaly_share", "Share of lake-sediment samples within 10 km above the basin's 95th "
                "percentile of uranium", "detection", "point_share_above", ("lake_sediment_gsc",),
                params={"value_field": "U", "radius_m": 10000, "quantile": 0.95},
                notes="A share, not a count, because a count grows with how many lakes were sampled. Null "
                      "under three samples."),
    FeatureSpec("boulder_sst_max_cps", "Highest count rate of a radioactive sandstone boulder within 5 km",
                "dispersal", "point_stat", ("radioactive_boulders",), unit="cps",
                params={"value_field": "CPS", "radius_m": 5000, "stat": "max", "zero_is_null": True,
                        "text_field": "LITHOLOGY", "text_words": SANDSTONE_WORDS},
                notes="Sandstone and conglomerate boulders only: radioactive cover was carried from over a source, "
                      "while radioactive granite and pegmatite boulders are common and mostly barren."),
    FeatureSpec("boulder_downice_max_cps", "Highest radioactive-boulder count rate 1-10 km down-ice", "dispersal",
                "point_cone", ("radioactive_boulders",), unit="cps",
                params={"value_field": "CPS", "radius_m": 10000, "sense": "down"},
                notes="A 60-degree cone from the cell along its down-ice bearing (the DEM's landform grain, with "
                      "the south-west taken as down-ice). A boulder here may have come from this cell."),
    FeatureSpec("boulder_upice_max_cps", "Highest radioactive-boulder count rate 1-10 km up-ice", "dispersal",
                "point_cone", ("radioactive_boulders",), unit="cps",
                params={"value_field": "CPS", "radius_m": 10000, "sense": "up"},
                notes="The mirror of the down-ice cone, kept as its control: a boulder up-ice cannot have come "
                      "from this cell, so if this carries the signal instead, the assumed ice-flow sense is wrong."),
    *[FeatureSpec(f"fault_len_{t}_km", f"Kilometres of {t.upper()}-trending lineament within 5 km", "trap",
                  "line_length_by_trend", ("faults_250k",), unit="km", is_count=True,
                  params={"radius_m": 5000, "trend": t},
                  notes="Trend measured from the line's own geometry; the map's direction field is blank on 57% "
                        "of its lines.")
      for t in TRENDS],
    FeatureSpec("fault_crossings_n", "Lineament crossings within 5 km", "trap", "line_crossings",
                ("faults_250k",), is_count=True, params={"radius_m": 5000},
                notes="Where two structures cross is where dilation and fluid focusing are most likely. Two lines "
                      "that only meet at an end are one lineament in two pieces and are not counted."),
    FeatureSpec("fault_conductor_crossings_n", "Places a lineament crosses an EM conductor within 5 km", "trap",
                "line_crossings", ("faults_250k", "em_conductors"), is_count=True,
                params={"radius_m": 5000, "other_key": "em_conductors"},
                notes="The pathway-and-trap pairing the deposit model asks for, as a count."),
    FeatureSpec("d_basin_edge_m", "Distance to the edge of the Athabasca sandstone (positive inside)", "cover",
                "basin_edge_distance", ("basin_geology",), unit="m"),
    FeatureSpec("d_domain_boundary_m", "Distance to the nearest mapped boundary between basement domains",
                "pathway", "distance_to_domain_boundary", ("bedrock_250k",), unit="m",
                params={"field_name": "DOMAIN"},
                notes="Domain boundaries are crustal-scale shear zones. Under the sandstone none is mapped, so a "
                      "covered cell is measured to the nearest boundary outside the cover."),
    FeatureSpec("lith_groups_n", "Lithology groups mapped within 5 km", "pathway", "lith_group_count",
                ("bedrock_250k",), params={"field_name": "LITHOLOGY", "radius_m": 5000},
                notes="Contacts between contrasting rocks focus strain. Read at the surface: a covered cell shows "
                      "one group, the sandstone."),
    *[FeatureSpec(f"domain_{slug(level)}", f"The cell lies in the {level} domain", "pathway", "polygon_is",
                  ("bedrock_250k",), params={"field_name": "DOMAIN", "level": level},
                  notes="The tectonic domain mapped at the surface, as one column of a one-hot encoding; null "
                        "where the map has no polygon.")
      for level in DOMAIN_LEVELS],
)
#: their keys, and the domain one-hots among them, whose keys name ground
EXTENDED_KEYS = frozenset(s.key for s in EXTENDED_SPECS)
DOMAIN_KEYS = frozenset(f"domain_{slug(level)}" for level in DOMAIN_LEVELS)

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
    *EXTENDED_SPECS,
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
    "point_ratio": point_ratio,
    "point_share_above": point_share_above,
    "point_cone": point_cone,
    "line_length_by_trend": line_length_by_trend,
    "line_crossings": line_crossings,
    "basin_edge_distance": basin_edge_distance,
    "polygon_is": polygon_is,
    "distance_to_domain_boundary": distance_to_domain_boundary,
    "lith_group_count": lith_group_count,
}

#: builders that take no layer key: they read their source themselves
NO_LAYER_ARG = frozenset({"graphitic_host", "graphitic_host_surface", "basin_edge_distance"})
#: builders whose value is known for every cell, so coverage counts values rather than observations
ANSWERS_EVERYWHERE = frozenset({"distance_to_lines", "basin_edge_distance", "distance_to_domain_boundary"})


def _build_one(spec: FeatureSpec, cells: gpd.GeoDataFrame, now: str) -> pd.DataFrame:
    """One feature's rows, in the store's column order."""
    kwargs = dict(spec.params)
    if spec.op not in NO_LAYER_ARG:
        kwargs["layer_key"] = spec.source_keys[0]
    df = BUILDERS[spec.op](cells, **kwargs)
    df = df.assign(
        feature_key=spec.key, unit=spec.unit, from_tier=spec.from_tier, op=spec.op, tool=TOOL,
        params=json.dumps(spec.params), inputs=json.dumps(list(spec.source_keys)), computed_at=now,
    )
    if "value_text" not in df.columns:
        df["value_text"] = None
    return df[["cell_id", "feature_key", "value", "value_text", "unit", "n_obs", "nearest_m",
               "from_tier", "op", "tool", "params", "inputs", "computed_at"]]


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
    specs = [s for s in SPECS if not only or s.key in only]
    # the label rule is checked before the store is opened: a spec that reads a label layer is refused on
    # any machine, store or no store
    for spec in specs:
        leaked = set(spec.source_keys) & labels
        if leaked:
            raise RuntimeError(f"{spec.key}: reads label layer(s) {sorted(leaked)}; labels are never features")
    cells = load_cells(grid_id)
    log(f"  {len(cells)} cells, {len(SPECS)} features")
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    written: dict[str, dict[str, Any]] = {}
    frames: list[pd.DataFrame] = []

    global _MEMO
    _MEMO = {}
    try:
        for spec in specs:
            frames.append(_build_one(spec, cells, now))
            df = frames[-1]
            # a count answers everywhere, a distance answers everywhere; everything else needs an observation
            have = (int(df["value"].notna().sum()) if spec.is_count or spec.op in ANSWERS_EVERYWHERE
                    else int((df["n_obs"] > 0).sum()))
            written[spec.key] = {"cells_with_value": have, "share": round(have / max(len(cells), 1), 3)}
            log(f"    {spec.key:22} {written[spec.key]['share']:.1%} of cells")
    finally:
        _MEMO = None

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
