"""The tools the agent panel may call, and the only numbers it may use.

Every tool is deterministic Python against the store. The model never computes geometry or arithmetic: that
boundary exists because GIS agents are documented to skip reprojection steps and because a prospectivity
assistant that was restricted to tool-computed scores still fabricated a number in 1 of 150 rated responses.

Every number a tool returns arrives inside a `Val` with an id. The memo may only print numbers by citing those
ids, and the fabrication gate re-checks each one against this registry before a memo is published. A number
that is not in the registry cannot reach the page, whatever the model writes.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable

import shapely
from pyproj import Transformer
from shapely.geometry import Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

from .. import index as IX
from ..bench.card import DRILLHOLE_LAYER, GRID_EPSG, VALUE_FIELD, reproject_feature, within_window
from ..bench.spec import LABEL_LAYERS
from ..runtime.tracing import set_attrs, span
from ..store import connect
from ..values import stat
from .criteria import load as load_criteria
from .inventory import load as load_inventory
from .retrieve import retrieve as retrieve_passages

TOOL_VERSION = "prospect/tools/v1"


@dataclass
class ToolResult:
    """What one tool call returns: prose rows for the model, and the values it may cite."""

    tool: str
    args: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    note: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "note": self.note, "rows": self.rows,
                "values": self.values}


def _vals(*items: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["id"]: v for v in items}


def _fmt_for(unit: str | None, value: float) -> str:
    if unit in ("m", "km2", "cps", "ppm"):
        return "m1"
    if float(value).is_integer():
        return "int"
    return "m2"


# ---------------------------------------------------------------- tools


def cell_features(cell_id: str) -> ToolResult:
    """Every feature computed for a cell, with its observation count and what it was built from."""
    con = connect(read_only=True)
    try:
        rows = con.execute(
            "select f.feature_key, f.value, f.value_text, f.unit, f.n_obs, f.nearest_m, s.title, "
            "s.bears_on, s.is_effort, s.notes "
            "from derived.cell_feature f left join derived.feature_spec s using (feature_key) "
            "where f.cell_id = ? order by s.is_effort, f.feature_key", [cell_id]
        ).fetchall()
    finally:
        con.close()
    out = ToolResult("cell_features", {"cell_id": cell_id})
    for key, value, text, unit, n_obs, nearest, title, bears_on, is_effort, notes in rows:
        row: dict[str, Any] = {
            "feature": key, "title": title, "bears_on": bears_on, "is_effort": bool(is_effort),
            "observations": int(n_obs or 0), "note": notes,
        }
        if n_obs:
            obs_vid = f"c:cell:{cell_id}:{key}:n_obs"
            out.values |= _vals(stat(obs_vid, int(n_obs),
                                     note=f"observations behind {title or key} at cell {cell_id}"))
            row["observations_id"] = obs_vid
        if value is not None:
            vid = f"c:cell:{cell_id}:{key}"
            out.values |= _vals(stat(vid, round(float(value), 4), fmt=_fmt_for(unit, value),
                                     unit=unit, note=f"{title or key} for cell {cell_id}"))
            row["value_id"] = vid
            row["value"] = round(float(value), 4)
            row["unit"] = unit
        elif text:
            # a class, not a measurement (the dominant surficial environment): citable like a number, so a claim
            # that names it has an id to cite rather than one it has to guess
            vid = f"c:cell:{cell_id}:{key}"
            out.values |= _vals(stat(vid, str(text), fmt="text", note=f"{title or key} for cell {cell_id}"))
            row["value_id"] = vid
            row["value"] = None
        else:
            row["value"] = None
            row["missing"] = "no observation; this is not a low value"
        if text:
            row["text"] = text
        if nearest is not None and value is None:
            vid = f"c:cell:{cell_id}:{key}:nearest_m"
            out.values |= _vals(stat(vid, round(float(nearest), 1), fmt="m1", unit="m",
                                     note=f"distance from cell {cell_id} to the nearest {key} observation"))
            row["nearest_observation_id"] = vid
        out.rows.append(row)
    out.note = ("A null value means nobody measured it here, which is not the same as a low reading. "
                "Effort features describe where people looked, not what is in the rock.")
    return out


def cell_scores(cell_id: str) -> ToolResult:
    """The three scores for a cell, with how much of each is actually known."""
    con = connect(read_only=True)
    try:
        rows = con.execute(
            "select model, score, known_share, in_aoa, params from derived.cell_score where cell_id = ? "
            "order by model", [cell_id]
        ).fetchall()
        metrics = con.execute(
            "select metric_key, value from derived.metric where metric_key like '%.spatial.%' "
            "order by metric_key"
        ).fetchall()
    finally:
        con.close()
    out = ToolResult("cell_scores", {"cell_id": cell_id})
    for model, score, known, in_aoa, params in rows:
        row: dict[str, Any] = {"model": model, "in_area_of_applicability": bool(in_aoa)}
        if score is not None:
            vid = f"c:score:{cell_id}:{model}"
            out.values |= _vals(stat(vid, round(float(score), 4), fmt="ratio3",
                                     note=f"{model} score for cell {cell_id}"))
            row["score_id"] = vid
            row["score"] = round(float(score), 4)
        else:
            row["score"] = None
            row["missing"] = "too little is known here to score honestly"
        if known is not None:
            known_vid = f"c:score:{cell_id}:{model}:known"
            out.values |= _vals(stat(known_vid, round(float(known), 3), fmt="ratio3",
                                     note=f"share of the {model} score's inputs actually measured here"))
            row["known_share_id"] = known_vid
            row["known_share"] = round(float(known), 3)
        try:
            row["model_note"] = json.loads(params or "{}").get("aoa_note") or ""
        except json.JSONDecodeError:
            pass
        out.rows.append(row)
    for key, value in metrics:
        vid = f"c:metric:{key}"
        out.values |= _vals(stat(vid, round(float(value), 4), fmt="ratio3",
                                 note=f"{key}, measured under spatial folds"))
    out.note = ("Under spatial folds the exploration-effort model outscores the geological one on this grid. "
                "A high learned score is therefore weak evidence about rock and strong evidence about where "
                "people have already worked.")
    return out


def criteria_breakdown(cell_id: str) -> ToolResult:
    """What each targeting criterion contributed here, with its evidence, its caveat and its status."""
    cs = load_criteria()
    by_key = {c.key: c for c in cs.criteria}
    con = connect(read_only=True)
    try:
        rows = con.execute(
            "select criterion, membership, weight, contribution from derived.cell_criterion "
            "where cell_id = ? order by weight desc, criterion", [cell_id]
        ).fetchall()
    finally:
        con.close()
    out = ToolResult("criteria_breakdown", {"cell_id": cell_id})
    for key, membership, weight, _contribution in rows:
        c = by_key.get(key)
        row: dict[str, Any] = {
            "criterion": key, "title": c.title if c else key, "element": c.element if c else "",
            "status": c.status if c else "", "weight": float(weight),
            "weight_id": f"c:crit:{cell_id}:{key}:weight",
            "evidence": c.evidence if c else "", "caveat": c.caveat if c else "",
        }
        out.values |= _vals(stat(f"c:crit:{cell_id}:{key}:weight", round(float(weight), 2), fmt="m2",
                                 note=f"weight carried by {key}"))
        # The thresholds are what the argument is actually about ("met, but only against an assumed 500 m
        # cut-off"), so they are values like any other rather than prose the memo has to quote from a file.
        for param, pval in sorted((c.params if c else {}).items()):
            pvid = f"c:crit:{cell_id}:{key}:{param}"
            out.values |= _vals(stat(pvid, round(float(pval), 4), fmt=_fmt_for(None, float(pval)),
                                     note=f"{param} threshold for {key}, status {c.status if c else ''}"))
            row.setdefault("thresholds", {})[param] = {"value": round(float(pval), 4), "value_id": pvid}
        if membership is None:
            row["state"] = "unknown"
            row["note"] = "no value for this criterion here: unknown, not absent"
        else:
            vid = f"c:crit:{cell_id}:{key}"
            out.values |= _vals(stat(vid, round(float(membership), 3), fmt="ratio3",
                                     note=f"{key} membership for cell {cell_id}"))
            row["membership_id"] = vid
            row["membership"] = round(float(membership), 3)
            row["state"] = "met" if membership >= 0.5 else "not met"
        if c and c.status == "folklore":
            row["note"] = ("recorded as personal communication with no published test; carried at weight zero "
                           "and may be named but never counted")
        out.rows.append(row)
    out.note = "Unknown and absent are different answers, and the adjudicator is required to tell them apart."
    return out


def label_context(cell_id: str, radius_km: float = 25.0, mask_cell: str | None = None) -> ToolResult:
    """How close the nearest known deposit or occurrence is: the first thing a skeptic should ask.

    `mask_cell` leaves that cell's own label row out before the ranking (leakage rule B30, for scored runs).
    The rest are then ranked from 1 as if the cell had no label, because a gap at rank 1 would itself say the
    cell is labelled; the note says a mask is in force whether or not anything was removed, for the same
    reason. Without `mask_cell` the tool behaves exactly as it always did."""
    mask = " and l.cell_id <> ?" if mask_cell else ""
    con = connect(read_only=True)
    try:
        here = con.execute("select lon, lat from derived.cell where cell_id = ?", [cell_id]).fetchone()
        if not here:
            return ToolResult("label_context", {"cell_id": cell_id}, note="no such cell")
        rows = con.execute(
            f"""
            select l.label_tier, l.label_name, c.lon, c.lat,
                   6371.0 * 2 * asin(sqrt(
                     pow(sin(radians(c.lat - ?) / 2), 2) +
                     cos(radians(?)) * cos(radians(c.lat)) * pow(sin(radians(c.lon - ?) / 2), 2))) as km
            from derived.cell_label l join derived.cell c using (cell_id)
            where l.label_tier <> 'unlabelled'{mask}
            order by km limit 5
            """,
            [here[1], here[1], here[0], *([mask_cell] if mask_cell else [])],
        ).fetchall()
    finally:
        con.close()
    args: dict[str, Any] = {"cell_id": cell_id, "radius_km": radius_km}
    if mask_cell:
        args["mask_cell"] = mask_cell
    out = ToolResult("label_context", args)
    for i, (tier, name, _lon, _lat, km) in enumerate(rows):
        vid = f"c:near:{cell_id}:{i}"
        out.values |= _vals(stat(vid, round(float(km), 2), fmt="m2", unit="km",
                                 note=f"distance from cell {cell_id} to {tier} {name or '(unnamed)'}"))
        out.rows.append({"rank": i + 1, "tier": tier, "name": name or "(unnamed)",
                         "distance_km_id": vid, "distance_km": round(float(km), 2)})
    out.note = ("A cell beside a known deposit will score well for reasons that have nothing to do with its "
                "own evidence. This is the leakage check, not a recommendation.")
    if mask_cell:
        out.note += " The evaluated cell's own label is masked."
    return out


def coverage(feature_key: str | None = None) -> ToolResult:
    """How much of the grid each feature actually covers: what a silent absence really means."""
    from .readiness import table as readiness_table

    df = readiness_table()
    if feature_key:
        df = df[df["feature_key"] == feature_key]
    out = ToolResult("coverage", {"feature_key": feature_key})
    for _, r in df.iterrows():
        key = str(r["feature_key"])
        vid = f"c:cov:{key}"
        out.values |= _vals(stat(vid, round(float(r["coverage"] or 0), 3), fmt="ratio3",
                                 note=f"share of cells with an observation behind {key}"))
        out.rows.append({"feature": key, "coverage_id": vid, "coverage": round(float(r["coverage"] or 0), 3),
                         "thin": bool(r["thin"]), "is_effort": bool(r["is_effort"])})
    out.note = "A thin feature cannot carry a basin-wide argument on its own."
    return out


def retrieve(query: str, cell_id: str | None = None, k: int = 6, radius_km: float = 40.0,
             exclude_files: list[str] | None = None) -> ToolResult:
    """Passages from the assessment corpus about this ground, most trustworthy tier first.

    `exclude_files` keeps named assessment files out of every tier (a benchmark's blind-list); empty, the
    tool behaves as it always did and the argument is not echoed."""
    lon = lat = None
    if cell_id:
        con = connect(read_only=True)
        try:
            row = con.execute("select lon, lat from derived.cell where cell_id = ?", [cell_id]).fetchone()
        finally:
            con.close()
        if row:
            lon, lat = float(row[0]), float(row[1])
    passages = retrieve_passages(query, lon=lon, lat=lat, radius_km=radius_km, k=k, exclude_files=exclude_files)
    args: dict[str, Any] = {"query": query, "cell_id": cell_id, "k": k, "radius_km": radius_km}
    if exclude_files:
        args["exclude_files"] = list(exclude_files)
    out = ToolResult("retrieve", args)
    for i, p in enumerate(passages):
        row: dict[str, Any] = {
            "tier": p.tier, "citation": p.cite(), "file": p.file_num, "page": p.page,
            "distance_km": p.distance_km, "text": p.text,
            "quotable": p.quotable, "numbers_allowed": p.carries_numbers,
            "value_id": p.value_id,
        }
        if p.page is not None:
            row["page_id"] = f"c:pass:{i}:page"
            out.values |= _vals(stat(f"c:pass:{i}:page", int(p.page),
                                     note=f"page of file {p.file_num} this passage is on"))
        if p.distance_km is not None:
            row["distance_km_id"] = f"c:pass:{i}:km"
            out.values |= _vals(stat(f"c:pass:{i}:km", round(float(p.distance_km), 2), fmt="m2", unit="km",
                                     note=f"distance from the cell to file {p.file_num}"))
        out.rows.append(row)
    out.note = ("Tier `extracted` carries a page, a box and a located quote, and is the only tier a number may "
                "be taken from. Tier `page` is the text layer of a scan, usually somebody's OCR pass, so it is "
                "evidence of what the page says and not that any number in it is right. Tier `metadata` is the "
                "provincial index row and is context only.")
    return out


# ---------------------------------------------------------------- the analyst's spatial tools

#: the native evidence layers `nearby` may open. `value` is the attribute that is a feature's reading and
#: `field` the unit-named key it is reported under; `text` is the attribute that says what was mapped (a rock
#: unit, a surficial class, a conductor type). Nothing that names ground, a file or a company is listed, and
#: the label and context layers are not here on purpose: a tool that counted deposits round a cell would be
#: the answer key with a radius on it. The sediment fields are the card's own, so the two agree.
NEARBY_LAYERS: dict[str, dict[str, str]] = {
    "em_conductors": {"text": "CONDUCTOR_TYPE"},
    "faults_250k": {"text": "FEAT_TYPE"},
    "radioactive_boulders": {"value": "CPS", "field": "cps", "text": "LITHOLOGY"},
    "lake_sediment_gsc": {"value": VALUE_FIELD["lake_sediment_gsc"], "field": "ppm"},
    "lake_sediment_sgs": {"value": VALUE_FIELD["lake_sediment_sgs"], "field": "ppm"},
    "lake_water_sgs": {"value": "U_PPM", "field": "ppm"},
    "bedrock_250k": {"text": "LITHOLOGY"},
    "surficial_250k": {"text": "MAIN_ENVIRONMENT"},
    DRILLHOLE_LAYER: {"text": "COMMODITY_OF_INTEREST"},
}
#: a radius below the cell size asks about less than one cell; one above 20 km is a regional question
NEARBY_RADIUS_M = (500.0, 20_000.0)
NEARBY_MAX_K = 50
#: how far `crosscheck` looks from the cell centre: the reach the point features are built with
CROSSCHECK_RADIUS_M = 5000.0
#: fewer lake-sediment samples than this within reach is thin sampling: one lake's reading is not a survey
MIN_SED_SAMPLES = 3


@lru_cache(maxsize=None)
def layer_features(layer: str) -> list[dict[str, Any]]:
    """One evidence layer as the card sees it: read once per process from the pulled file and reprojected
    into the grid's CRS by the card's own routine, so a distance here is a distance on the card. The reading
    and the mapped-unit text ride along under `properties`; nothing else from the file does."""
    spec = NEARBY_LAYERS[layer]
    tr = Transformer.from_crs(4326, GRID_EPSG, always_xy=True)
    out: list[dict[str, Any]] = []
    for f in IX.read_features(layer):
        moved = reproject_feature(f, tr, spec.get("value"))
        if moved is None:
            continue
        text = str((f.get("properties") or {}).get(spec["text"]) or "").strip() if "text" in spec else ""
        if text:
            moved["properties"]["text"] = text[:80]
        out.append(moved)
    return out


# ---------------------------------------------------------------- the layer footprint

#: how far one feature vouches for the ground around it, in metres, by the kind of geometry a layer holds. A
#: layer's footprint is the union of these halos over every feature, and a polygon layer's is the union of its
#: polygons. The union of halos was chosen over a concave hull because a hull draws one outline round the
#: layer and fills every gap inside it, so an unsampled hole inside a survey or an unflown corridor between
#: two survey blocks would read as mapped; the halos leave both open, and the one parameter is a distance a
#: geologist can check rather than a hull ratio.
#:   point  regional lake-sediment surveys sample about one lake per 6 to 13 km2, a sample every 2.4 to 3.6 km
#:          (in the GSC layer here the median spacing between samples is 2.4 km and the 99th percentile
#:          4.1 km), so 5 km joins one survey's samples into a patch and leaves a hole open where no lake was
#:          sampled for 10 km. Boulder records and drillhole collars are placed, not gridded, and for them the
#:          halo says somebody worked within 5 km. It is also the radius the point features are built with.
#:   line   EM surveys are flown in blocks and the conductor picks are joined along strike, so one block's
#:          traces lie a few km apart across strike (the 99th percentile of the spacing between traces here is
#:          1.6 km): 5 km joins a block's traces and leaves the gap between blocks open, where 10 km bridges
#:          neighbouring blocks. The faults are drawn on the same map sheets as the bedrock polygons, and 5 km
#:          leaves out only ground the sheets show without a fault for 5 km, which is the direction the error
#:          is allowed to go.
FOOTPRINT_HALO_M: dict[str, float] = {"point": 5000.0, "line": 5000.0}
_GEOMETRY_KIND = {"Point": "point", "MultiPoint": "point", "LineString": "line", "MultiLineString": "line",
                  "Polygon": "polygon", "MultiPolygon": "polygon"}


@dataclass(frozen=True)
class Footprint:
    """The ground a layer says anything about: one geometry in the grid's CRS, the kind of geometry it was
    built from and the halo it used (None for a polygon layer). A cell inside it is one the layer could have
    said something about; a cell outside it is one nobody mapped or sampled, and an empty radius there is not
    an absence."""

    layer: str
    geometry: BaseGeometry
    kind: str
    halo_m: float | None

    @property
    def rule(self) -> str:
        """What the footprint is, in one clause, for the value note and the summary row."""
        if self.kind == "empty":
            return f"nothing, because the {self.layer} layer has no features"
        if self.halo_m is None:
            return f"the union of the {self.layer} polygons"
        return f"the ground within {self.halo_m:g} m of any {self.layer} feature"

    def contains(self, centre: tuple[float, float]) -> int:
        """1 when the point lies inside the footprint, 0 outside it: a 0/1 statistic like the counts, so it
        is a value with an id and not a flag the model reads and forgets."""
        return int(self.geometry.intersects(Point(*centre)))


@lru_cache(maxsize=None)
def layer_footprint(layer: str) -> Footprint:
    """One layer's footprint, computed once per process from the layer's own features the way `layer_features`
    is read once: the store holds no per-cell footprint, and the survey footprints it does hold cover airborne
    and ground surveys only. The kind is the layer's commonest geometry, so a stray multi-part feature does
    not change the rule. The geometry is prepared, because every `nearby` call asks it one point-in-polygon
    question."""
    geoms = [shape(f["geometry"]) for f in layer_features(layer)]
    if not geoms:
        return Footprint(layer, Polygon(), "empty", None)   # nothing mapped anywhere: no cell is inside
    kind = Counter(_GEOMETRY_KIND.get(g.geom_type, "polygon") for g in geoms).most_common(1)[0][0]
    halo = FOOTPRINT_HALO_M.get(kind)
    geometry = shapely.union_all(geoms if halo is None else shapely.buffer(geoms, halo))
    shapely.prepare(geometry)
    return Footprint(layer, geometry, kind, halo)


def _footprint_value(vid: str, fp: Footprint, cell_id: str, centre: tuple[float, float]) -> tuple[int, dict[str, Any]]:
    """The 0/1 footprint statistic for one cell and layer, with the value the registry holds for it."""
    inside = fp.contains(centre)
    return inside, stat(vid, inside, note=f"1 when cell {cell_id} lies inside the mapped extent of {fp.layer}, "
                                          f"{fp.rule}; 0 when nothing of the layer was mapped or sampled "
                                          f"around the cell")


def _check_layer(layer: str) -> None:
    """Refuse anything that is not an evidence layer, saying why: a label is the answer, context is not evidence."""
    if layer in NEARBY_LAYERS:
        return
    role = next((s.role for s in load_inventory().sources if s.key == layer), None)
    if layer in LABEL_LAYERS or role == "label":
        raise ToolError(f"{layer} is a label layer: the labels are the answer, never evidence")
    if role == "context":
        raise ToolError(f"{layer} is a context layer, not evidence")
    raise ToolError(f"no evidence layer named {layer!r}; available: {', '.join(NEARBY_LAYERS)}")


def _cell_centre(cell_id: str) -> tuple[float, float] | None:
    con = connect(read_only=True)
    try:
        row = con.execute("select cx, cy from derived.cell where cell_id = ?", [cell_id]).fetchone()
    finally:
        con.close()
    return (float(row[0]), float(row[1])) if row else None


def _around(layer: str, centre: tuple[float, float], radius_m: float
            ) -> list[tuple[float, dict[str, Any], BaseGeometry]]:
    """The layer's features within `radius_m` of the centre, nearest first: a bbox pass over the window, then
    the true distance. Ties keep the file's order, so the same inputs give the same rows."""
    cx, cy = centre
    here = Point(cx, cy)
    hits: list[tuple[float, dict[str, Any], BaseGeometry]] = []
    for f in within_window(layer_features(layer), (cx - radius_m, cy - radius_m, cx + radius_m, cy + radius_m)):
        geom = shape(f["geometry"])
        d = float(geom.distance(here))
        if d <= radius_m:
            hits.append((d, f, geom))
    hits.sort(key=lambda hit: hit[0])
    return hits


def nearby(cell_id: str, layer: str, radius_m: float = 5000.0, k: int = 5) -> ToolResult:
    """What one evidence layer holds around the cell: a count inside the radius and the k nearest features,
    each with its true distance from the cell centre and, where the layer carries one, its reading.

    This is the analyst's box maker. The window is the cell centre plus or minus the radius in the grid's
    CRS, the features are the card's own reprojected copies, and the distances are shapely's in metres, so
    the model never measures anything off the card. Zero inside the radius is returned as a value like any
    other, because "nothing mapped here" is an answer and not a gap, but only where the layer reaches: the
    summary also says whether the cell lies inside the layer's footprint (`layer_footprint`), as a 0/1
    value with an id, and when it does not the note says that nothing of the layer was mapped or sampled
    around the cell, so an empty radius there is unknown. Drillhole collars are flagged `is_effort` on every
    row, so an arm that hides effort can drop them whole."""
    _check_layer(layer)
    lo, hi = NEARBY_RADIUS_M
    if not lo <= float(radius_m) <= hi:
        raise ToolError(f"radius_m must be between {lo:g} and {hi:g} m, not {radius_m}")
    if not 0 <= int(k) <= NEARBY_MAX_K:
        raise ToolError(f"k must be between 0 and {NEARBY_MAX_K}, not {k}")
    radius_m, k = float(radius_m), int(k)
    args: dict[str, Any] = {"cell_id": cell_id, "layer": layer, "radius_m": radius_m, "k": k}
    centre = _cell_centre(cell_id)
    if centre is None:
        return ToolResult("nearby", args, note="no such cell")
    hits = _around(layer, centre, radius_m)
    spec = NEARBY_LAYERS[layer]
    effort = layer == DRILLHOLE_LAYER
    base = f"c:nb:{cell_id}:{layer}:{radius_m:g}"
    out = ToolResult("nearby", args)
    summary: dict[str, Any] = {"layer": layer, "is_effort": effort, "n_within": len(hits),
                               "n_within_id": f"{base}:n_within"}
    out.values |= _vals(stat(f"{base}:n_within", len(hits),
                             note=f"{layer} features within {radius_m:g} m of cell {cell_id}"))
    if hits:
        nearest = round(hits[0][0], 1)
        out.values |= _vals(stat(f"{base}:nearest_m", nearest, fmt="m1", unit="m",
                                 note=f"distance from cell {cell_id} to the nearest {layer} feature"))
        summary["nearest_m"], summary["nearest_m_id"] = nearest, f"{base}:nearest_m"
    # the footprint rides under the same base as the counts: the same 0/1 for every radius, but a value the
    # node can cite beside the count it qualifies
    fp = layer_footprint(layer)
    inside, flag = _footprint_value(f"{base}:in_footprint", fp, cell_id, centre)
    out.values |= _vals(flag)
    summary["in_footprint"], summary["in_footprint_id"] = inside, f"{base}:in_footprint"
    if fp.halo_m is not None:
        # the halo is the rule's one parameter, so it is a value with an id like the crosscheck's thresholds
        out.values |= _vals(stat(f"{base}:footprint_halo_m", fp.halo_m, fmt="m1", unit="m",
                                 note=f"how far one {layer} feature vouches for the ground around it; the "
                                      f"layer's footprint is the union of these halos"))
        summary["footprint_halo_m"], summary["footprint_halo_m_id"] = fp.halo_m, f"{base}:footprint_halo_m"
    if inside:
        summary["footprint_note"] = (f"the cell lies inside the mapped extent of {layer} ({fp.rule}), so an "
                                     f"empty radius here is an absence")
    else:
        summary["footprint_note"] = (f"nothing of {layer} was mapped or sampled around this cell (its footprint "
                                     f"is {fp.rule}), so an empty radius here is unknown, not an absence")
    out.rows.append(summary)
    for i, (d, f, _geom) in enumerate(hits[:k]):
        vid = f"{base}:{i}:dist_m"
        row: dict[str, Any] = {"layer": layer, "is_effort": effort, "dist_m": round(d, 1), "dist_m_id": vid}
        out.values |= _vals(stat(vid, round(d, 1), fmt="m1", unit="m",
                                 note=f"distance from cell {cell_id} to {layer} feature {i}"))
        props = f.get("properties") or {}
        field_key = spec.get("field")
        if field_key:
            reading = props.get("value")
            if reading is None:
                row["missing"] = "no reading recorded for this feature"
            else:
                rid = f"{base}:{i}:{field_key}"
                out.values |= _vals(stat(rid, round(float(reading), 4), fmt=_fmt_for(field_key, reading),
                                         unit=field_key, note=f"{layer} reading of feature {i} near cell {cell_id}"))
                row[field_key], row[f"{field_key}_id"] = round(float(reading), 4), rid
        if props.get("text"):
            row["text"] = props["text"]
        out.rows.append(row)
    out.note = ("Counts say what was mapped or sampled inside the radius, not what is in the rock. A layer with "
                "nothing inside the radius is an absence only where in_footprint is 1; where it is 0 nothing "
                "of the layer was mapped or sampled around the cell, and the criterion is unknown, not not met.")
    return out


def _echo(out: ToolResult, row: dict[str, Any], feats: ToolResult, key: str) -> None:
    """One cell feature copied into a crosscheck row under the ids and values `cell_features` gave it, so a
    registry that holds both tools' results holds one copy. A null value is unknown, and the row then carries
    the nearest-observation id the way `cell_features` does."""
    src = next((r for r in feats.rows if r.get("feature") == key), None)
    if src is None or src.get("value") is None:
        row[key] = None
        nid = (src or {}).get("nearest_observation_id")
        if nid:
            row[f"{key}_nearest_m_id"] = nid
            out.values[nid] = feats.values[nid]
        return
    row[key], row[f"{key}_id"] = src["value"], src["value_id"]
    out.values[src["value_id"]] = feats.values[src["value_id"]]


def crosscheck(cell_id: str) -> ToolResult:
    """The conjunctions the handbook reads together, computed rather than reasoned: one row per pair.

    `conductor_fault`: among the conductors and faults within `CROSSCHECK_RADIUS_M` of the cell centre, the
    smallest separation between any conductor and any fault, and how many such pairs cross (a shapely
    intersects). Zero crossings is a real answer. A side with nothing inside the radius makes the pair
    absent when that layer's footprint reaches the cell, and unknown when it does not: the line maps are
    basin-wide, but their surveys are not. Each side's `in_footprint` rides on the row as a 0/1 value with
    an id, the same statistic `nearby` returns. The cell's own `d_conductor_m` and `d_fault_m` are echoed
    with the ids and values `cell_features` gives them.

    `sediment_sampling`: the lake-sediment anomaly beside the sampling it rests on. `sed_u_max_ppm` and
    `sed_samples_n` are echoed the same way, and `thin_sampling` is a fixed rule: fewer than
    `MIN_SED_SAMPLES` samples within 5 km. The threshold is a value with an id, so a memo can say which rule
    it applied. A null anomaly means nobody sampled within reach, which is unknown and not low; a null count
    leaves `thin_sampling` null rather than guessed. The sediment layer's `in_footprint` rides on the row
    too."""
    args: dict[str, Any] = {"cell_id": cell_id}
    centre = _cell_centre(cell_id)
    if centre is None:
        return ToolResult("crosscheck", args, note="no such cell")
    out = ToolResult("crosscheck", args)
    feats = cell_features(cell_id)
    radius = CROSSCHECK_RADIUS_M

    pair = "conductor_fault"
    row: dict[str, Any] = {"pair": pair, "radius_m": radius, "radius_m_id": f"c:x:{cell_id}:{pair}:radius_m"}
    out.values |= _vals(stat(f"c:x:{cell_id}:{pair}:radius_m", radius, fmt="m1", unit="m",
                             note=f"reach of the conductor-fault check around cell {cell_id}"))
    _echo(out, row, feats, "d_conductor_m")
    _echo(out, row, feats, "d_fault_m")
    sides = (("conductor", "em_conductors"), ("fault", "faults_250k"))
    hits_by = {side: _around(layer, centre, radius) for side, layer in sides}
    conductors, faults = hits_by["conductor"], hits_by["fault"]
    crossings = sum(1 for _, _, c in conductors for _, _, f in faults if c.intersects(f))
    row["crossings_n"], row["crossings_n_id"] = crossings, f"c:x:{cell_id}:{pair}:crossings_n"
    out.values |= _vals(stat(f"c:x:{cell_id}:{pair}:crossings_n", crossings,
                             note=f"conductor-fault crossings within {radius:g} m of cell {cell_id}"))
    # each side's footprint, under the pair's own ids: the node gate reads the flag from either tool
    mapped: dict[str, int] = {}
    for side, layer in sides:
        vid = f"c:x:{cell_id}:{pair}:{layer}:in_footprint"
        mapped[side], flag = _footprint_value(vid, layer_footprint(layer), cell_id, centre)
        out.values |= _vals(flag)
        row[f"{side}_in_footprint"], row[f"{side}_in_footprint_id"] = mapped[side], vid
    if conductors and faults:
        sep = round(min(float(c.distance(f)) for _, _, c in conductors for _, _, f in faults), 1)
        row["min_sep_m"], row["min_sep_m_id"] = sep, f"c:x:{cell_id}:{pair}:min_sep_m"
        out.values |= _vals(stat(f"c:x:{cell_id}:{pair}:min_sep_m", sep, fmt="m1", unit="m",
                                 note=f"smallest conductor-fault separation within {radius:g} m of cell {cell_id}"))
        row["state"] = "known"
    else:
        missing = [side for side, _layer in sides if not hits_by[side]]
        unmapped = [side for side in missing if not mapped[side]]
        if unmapped:
            # an empty side outside its footprint was never surveyed here: the conjunction is unknown
            row["state"] = "unknown"
            row["missing"] = (f"no {' or '.join(unmapped)} was mapped around this cell at all, so the pair is "
                              f"unknown, not absent")
        else:
            row["state"] = "absent"
            row["absent"] = f"no mapped {' or '.join(missing)} within the radius"
    out.rows.append(row)

    pair = "sediment_sampling"
    row = {"pair": pair}
    _echo(out, row, feats, "sed_u_max_ppm")
    _echo(out, row, feats, "sed_samples_n")
    row["min_samples"], row["min_samples_id"] = MIN_SED_SAMPLES, f"c:x:{cell_id}:{pair}:min_samples"
    out.values |= _vals(stat(f"c:x:{cell_id}:{pair}:min_samples", MIN_SED_SAMPLES,
                             note="fewer lake-sediment samples than this within reach counts as thin sampling"))
    vid = f"c:x:{cell_id}:{pair}:lake_sediment_gsc:in_footprint"
    sampled, flag = _footprint_value(vid, layer_footprint("lake_sediment_gsc"), cell_id, centre)
    out.values |= _vals(flag)
    row["sediment_in_footprint"], row["sediment_in_footprint_id"] = sampled, vid
    samples = row.get("sed_samples_n")
    row["thin_sampling"] = None if samples is None else bool(samples < MIN_SED_SAMPLES)
    if row.get("sed_u_max_ppm") is None:
        row["state"] = "unknown"
        row["missing"] = ("no lake-sediment sample within reach; unknown, not low" if sampled else
                          "no lake-sediment sample within reach and none around this cell at all; unknown, not low")
    else:
        row["state"] = "known"
    out.rows.append(row)
    out.note = ("Unknown and absent are different answers: a conjunction with one unknown side is unknown, and "
                "one with a side that is mapped but empty inside the radius is absent. A side whose layer has "
                "in_footprint 0 was never mapped or sampled around the cell, so an empty radius there is unknown.")
    return out


REGISTRY: dict[str, Callable[..., ToolResult]] = {
    "cell_features": cell_features,
    "cell_scores": cell_scores,
    "criteria_breakdown": criteria_breakdown,
    "label_context": label_context,
    "coverage": coverage,
    "retrieve": retrieve,
    "nearby": nearby,
    "crosscheck": crosscheck,
}

#: what the model is told it may call, in the prompt
TOOL_HELP = {
    "cell_features": "cell_features(cell_id) - every measured feature here, with its observation count",
    "cell_scores": "cell_scores(cell_id) - the criteria, learned and effort scores, and the model metrics",
    "criteria_breakdown": "criteria_breakdown(cell_id) - what each targeting criterion contributed, with its "
                          "evidence, caveat and whether it is published, assumed or folklore",
    "label_context": "label_context(cell_id) - distance to the nearest known deposit or occurrence",
    "coverage": "coverage(feature_key=None) - how much of the grid a feature covers",
    "retrieve": "retrieve(query, cell_id, k) - passages from the assessment corpus about this ground",
    "nearby": "nearby(cell_id, layer, radius_m, k) - the count and the k nearest features of one evidence "
              "layer around the cell, with true distances, and whether the cell lies inside the layer's "
              "mapped footprint (an empty radius outside it is unknown, not an absence)",
    "crosscheck": "crosscheck(cell_id) - conductor-fault separation and crossings, and the lake-sediment "
                  "anomaly beside its sampling density",
}


class ToolError(Exception):
    """The model asked for a tool that does not exist, or asked for it wrongly."""


def call(tool: str, args: dict[str, Any]) -> ToolResult:
    """Run one tool call. Unknown tools and bad arguments fail loudly rather than returning nothing."""
    fn = REGISTRY.get(tool)
    if fn is None:
        raise ToolError(f"no tool named {tool!r}; available: {', '.join(sorted(REGISTRY))}")
    # a tool span when a run is being traced, nothing otherwise: the arguments' names, never their values
    with span(f"tool:{tool}", kind="tool", tool=tool, arg_keys=sorted(args)):
        try:
            result = fn(**args)
        except TypeError as err:
            raise ToolError(f"{tool}: {err}") from err
        set_attrs(n_values=len(result.values), n_rows=len(result.rows))
        return result
