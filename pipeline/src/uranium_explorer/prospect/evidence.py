"""Raw evidence around a cell, for a reader that can take it raw.

`features` reduces each layer to one number per cell for the fitted model, and `extended` gives the fitted
model more of those numbers. This module hands over the same evidence unreduced: every lake-sediment sample in
reach with its elements, every radioactive boulder with its rock type and where it lies against the ice flow,
every lineament and conductor with its trend, every bedrock unit as the map describes it, and where the cell
sits in the region. It is what the analyst is shown when the benchmark asks whether a model that reads the
evidence can rank cells better than one fitted to features built from it.

Four tools, one per evidence family, and a fifth for the regional setting. Three rules hold for all of them:

* **Every number has a value id, and this module computes every derived number** (a ratio, a bearing, a
  distance, a share), so the model never does arithmetic and the fabrication gate can bind every claim.
* **Nothing names ground.** Domain names, stratigraphic names, survey system, file numbers, companies, years
  and assay results are left out: a domain is a letter and a description of its rocks; a boulder is its rock
  type and its reading. The benchmark's scrub runs over the text that remains.
* **Unknown stays unknown.** A family with nothing in reach says so; a reading under detection is written as
  such, not as a number.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..values import stat
from . import features as F
from .tools import ToolResult, _cell_centre

TOOL_VERSION = "prospect/evidence/v1"
FAMILIES = ("geochem", "boulders", "structure", "bedrock")
TOOLS = (*(f"evidence_{f}" for f in FAMILIES), "region")

#: how far each family looks: the sediment and boulder features reach 5-10 km, and a boulder train is read
#: over kilometres; the bedrock units are the ground under and beside the cell; the region is 50 km round
RADIUS_M = 10_000.0
UNIT_RADIUS_M = 5_000.0
CROSSING_RADIUS_M = 5_000.0
REGION_RADIUS_M = 50_000.0
#: rows per kind, nearest first. A cap bounds the prompt; the note says when it cut rows
CAPS = {"sediment": 40, "water": 10, "boulder": 40, "lineament": 30, "conductor": 15, "unit": 20,
        "surficial": 8, "domain": 6}
#: a bearing within this many degrees of down-ice is down-ice, as in the cone features
ICE_HALF_ANGLE = 30.0
#: below these denominators a ratio is a division, not a measurement (the features use the same floors)
TH_FLOOR_PPM, LOI_FLOOR_PCT = 0.5, 5.0

_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
_TREND_NAMES = {"ns": "N-S", "ne": "NE-SW", "ew": "E-W", "nw": "NW-SE"}
BELOW_DETECTION = "below detection"


def compass(deg: float) -> str:
    return _COMPASS[int(((float(deg) % 360.0) + 22.5) // 45.0) % 8]


def trend_name(axis_deg: float) -> str:
    a = float(axis_deg) % 180.0
    for key, (lo, hi) in F.TRENDS.items():
        if (lo > hi and (a >= lo or a < hi)) or (lo <= a < hi):
            return _TREND_NAMES[key]
    return _TREND_NAMES["ns"]


def ice_relation(bearing_deg: float, down_deg: float) -> str:
    off = abs((float(bearing_deg) - float(down_deg) + 180.0) % 360.0 - 180.0)
    if off <= ICE_HALF_ANGLE:
        return "down-ice"
    if off >= 180.0 - ICE_HALF_ANGLE:
        return "up-ice"
    return "across the ice flow"


def boulder_group(text: Any) -> str:
    low = str(text or "").strip().lower()
    if not low or low in ("none", "undefined"):
        return "not recorded"
    if "pitchblende" in low or "uraninite" in low:
        return "ore mineral"
    if any(w in low for w in F.SANDSTONE_WORDS) or any(w in low for w in ("siltstone", "mudstone")):
        return "cover (sandstone family)"
    return "basement"


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _fmt(unit: str | None, v: float) -> str:
    if unit in ("m", "cps", "ppm", "km"):
        return "m1"
    return "int" if float(v).is_integer() else "m2"


class _Rows:
    """Rows and the values they cite, built together so no number reaches a row without an id."""

    def __init__(self, tool: str, cell_id: str, args: dict[str, Any]):
        self.out = ToolResult(tool, args)
        self.base = f"c:ev:{cell_id}"
        self.cell_id = cell_id

    def num(self, row: dict[str, Any], key: str, tag: str, value: float | None, unit: str | None = None,
            note: str = "", digits: int = 4) -> None:
        if value is None or not np.isfinite(value):
            return
        vid = f"{self.base}:{tag}:{key}"
        v = round(float(value), digits)
        self.out.values[vid] = stat(vid, v, fmt=_fmt(unit, v), unit=unit, note=note or f"{key} ({tag})")
        row[key], row[f"{key}_id"] = v, vid

    def reading(self, row: dict[str, Any], key: str, tag: str, raw: Any, unit: str, note: str) -> None:
        """A laboratory reading: a negative is the survey's code for under detection, written as words."""
        v = _num(raw)
        if v is None:
            return
        if v < 0:
            row[key] = BELOW_DETECTION
            return
        self.num(row, key, tag, v, unit, note)


# ---------------------------------------------------------------- where things are


@lru_cache(maxsize=1)
def _ice_table() -> pd.DataFrame:
    from .grid import load_cells

    return F.down_ice_frame(load_cells()).set_index("cell_id")


def down_ice(cell_id: str) -> tuple[float, bool]:
    """The cell's down-ice bearing and whether it is the cell's own grain (else the regional axis)."""
    t = _ice_table()
    if cell_id not in t.index:
        return F.REGIONAL_ICE_FLOW_DEG, False
    row = t.loc[cell_id]
    return float(row["down_ice_deg"]), bool(row["from_grain"])


def _points(layer_key: str, centre: tuple[float, float], radius_m: float) -> pd.DataFrame:
    """The layer's points within `radius_m`, nearest first, with distance and compass bearing from the centre."""
    pts = F._layer(layer_key)
    pts = pts[~pts.geometry.is_empty & pts.geometry.notna()]
    x, y = pts.geometry.x.to_numpy(), pts.geometry.y.to_numpy()
    dx, dy = x - centre[0], y - centre[1]
    d = np.hypot(dx, dy)
    keep = d <= radius_m
    out = pd.DataFrame(pts.drop(columns="geometry")[keep]).reset_index(drop=True)
    out["_d"] = d[keep]
    out["_b"] = np.mod(np.degrees(np.arctan2(dx[keep], dy[keep])), 360.0)
    return out.sort_values("_d", kind="stable").reset_index(drop=True)


def _place(R: _Rows, row: dict[str, Any], tag: str, d: float, b: float, down: float | None) -> None:
    R.num(row, "dist_m", tag, d, "m", f"distance from the cell centre to record {tag}", digits=0)
    row["bearing"] = compass(b)
    if down is not None:
        row["ice"] = ice_relation(b, down) if d >= 1.0 else "at the cell centre"


def _capped(note: str, shown: int, total: int, what: str) -> str:
    return note + (f" Only the nearest {shown} of the {what} in reach are listed." if total > shown else "")


# ---------------------------------------------------------------- the families


def evidence_geochem(cell_id: str, radius_m: float = RADIUS_M) -> ToolResult:
    """Every lake-sediment and lake-water sample within reach, nearest first, with its elements."""
    R = _Rows("evidence_geochem", cell_id, {"cell_id": cell_id, "radius_m": radius_m})
    centre = _cell_centre(cell_id)
    if centre is None:
        R.out.note = "no such cell"
        return R.out
    down, _ = down_ice(cell_id)
    notes = []
    gsc = F._layer("lake_sediment_gsc")
    p95 = float(np.nanquantile(pd.to_numeric(gsc["U"], errors="coerce").to_numpy(dtype=float), 0.95))
    head: dict[str, Any] = {"kind": "survey threshold"}
    R.num(head, "u_p95_ppm", "s", p95, "ppm", "the regional survey's 95th percentile of lake-sediment uranium")
    R.out.rows.append(head)
    for layer, tag, survey in (("lake_sediment_gsc", "s", "regional survey"),
                               ("lake_sediment_sgs", "t", "1975-1978 survey")):
        hits = _points(layer, centre, radius_m)
        for i, r in hits.head(CAPS["sediment"]).iterrows():
            t = f"{tag}{i}"
            row: dict[str, Any] = {"kind": "lake sediment", "survey": survey}
            _place(R, row, t, r["_d"], r["_b"], down)
            u = _num(r.get("U") if layer == "lake_sediment_gsc" else r.get("U_PPM"))
            R.reading(row, "u_ppm", t, u, "ppm", f"lake-sediment uranium, sample {t}")
            if layer == "lake_sediment_gsc":
                if u is not None and u >= 0:
                    row["regional_rank"] = "in the top 5% of the survey" if u > p95 else "below the top 5%"
                ui, th = _num(r.get("U_INA")), _num(r.get("TH_INA"))
                R.reading(row, "th_ppm", t, th, "ppm", f"lake-sediment thorium, sample {t}")
                if ui is not None and th is not None and th >= TH_FLOOR_PPM:
                    R.num(row, "u_th", t, max(ui, 0.0) / th, None,
                          f"uranium over thorium by neutron activation, sample {t}, computed by the tool")
                loi = _num(r.get("LOI"))
                R.num(row, "lake_depth_m", t, _num(r.get("LK_DEPTH")), "m", f"lake depth at sample {t}")
            else:
                R.reading(row, "pb_ppm", t, r.get("PB_PPM"), "ppm", f"lake-sediment lead, sample {t}")
                R.reading(row, "ni_ppm", t, r.get("NI_PPM"), "ppm", f"lake-sediment nickel, sample {t}")
                loi = _num(r.get("LOI_PERC"))
            R.reading(row, "loi_pct", t, loi, "%", f"loss on ignition (organic content), sample {t}")
            if u is not None and loi is not None and loi >= LOI_FLOOR_PCT:
                R.num(row, "u_per_loi", t, max(u, 0.0) / loi, "ppm/%",
                      f"uranium per percent loss on ignition, sample {t}, computed by the tool")
            R.out.rows.append(row)
        notes.append(_capped("", min(len(hits), CAPS["sediment"]), len(hits), f"{survey} samples").strip())
    water = _points("lake_water_sgs", centre, radius_m)
    for i, r in water.head(CAPS["water"]).iterrows():
        t = f"w{i}"
        row = {"kind": "lake water"}
        _place(R, row, t, r["_d"], r["_b"], down)
        R.reading(row, "u_ppm", t, r.get("U_PPM"), "ppm", f"lake-water uranium, sample {t} (reported to 0.1 ppm)")
        R.reading(row, "ph", t, r.get("PH"), None, f"lake-water pH, sample {t}")
        R.reading(row, "eh_mv", t, r.get("EH_MV"), "mV", f"lake-water redox potential, sample {t}")
        R.out.rows.append(row)
    if len(R.out.rows) == 1:
        R.out.rows.append({"kind": "none", "note": f"no lake sample within {radius_m / 1000:g} km: unknown, not low"})
    R.out.note = " ".join(n for n in [
        "Lake sediment records what drains into the lake: a high uranium says something up-drainage sheds "
        "uranium, not that ore lies under the lake. Organic matter scavenges uranium, so uranium per loss on "
        "ignition corrects for how organic the lake is; thorium travels with detrital grains, so a high "
        "uranium over thorium is uranium beyond what the sediment's own minerals explain. Lead and nickel are "
        "pathfinders. A reading under detection is written as words. Ratios are computed by the tool.", *notes]
        if n)
    return R.out


def evidence_boulders(cell_id: str, radius_m: float = RADIUS_M) -> ToolResult:
    """The cell's ice-flow direction, then every radioactive boulder within reach with its rock type."""
    R = _Rows("evidence_boulders", cell_id, {"cell_id": cell_id, "radius_m": radius_m})
    centre = _cell_centre(cell_id)
    if centre is None:
        R.out.note = "no such cell"
        return R.out
    down, own = down_ice(cell_id)
    ice: dict[str, Any] = {"kind": "ice flow", "down_ice": compass(down),
                           "source": "the cell's own landform grain" if own else
                           "the regional direction (the landform grain here is weak or missing)"}
    R.num(ice, "down_ice_deg", "ice", down, "deg", "the compass bearing ice moved toward over this cell", digits=0)
    R.out.rows.append(ice)
    hits = _points("radioactive_boulders", centre, radius_m)
    cps = pd.to_numeric(hits.get("CPS"), errors="coerce") if len(hits) else pd.Series(dtype=float)
    hits = hits[(cps.fillna(0) > 0).to_numpy()] if len(hits) else hits
    for j, (_, r) in enumerate(hits.head(CAPS["boulder"]).iterrows()):
        t = f"b{j}"
        row: dict[str, Any] = {"kind": "boulder"}
        _place(R, row, t, r["_d"], r["_b"], down)
        rock = str(r.get("LITHOLOGY") or "").strip()
        row["rock"] = rock.lower() if rock and rock.lower() not in ("none", "undefined") else "not recorded"
        row["rock_group"] = boulder_group(rock)
        R.num(row, "cps", t, _num(r.get("CPS")), "cps", f"scintillometer count rate on boulder {t}")
        bg = _num(r.get("BACKGROUND"))
        if bg is not None and bg > 0:
            R.num(row, "background_cps", t, bg, "cps", f"background count rate recorded beside boulder {t}")
        R.out.rows.append(row)
    if not len(hits):
        R.out.rows.append({"kind": "none", "note": f"no radioactive boulder recorded within {radius_m / 1000:g} km: "
                                                   "boulders are recorded where someone prospected, so this is "
                                                   "unknown, not absent"})
    R.out.note = _capped(
        "Ice carried boulders from up-ice. A boulder down-ice of the cell may have come from it; one up-ice cannot "
        "have. Radioactive sandstone and conglomerate boulders come from altered cover over a source; granite and "
        "pegmatite boulders are often radioactive and barren. A count rate is a field reading, not an assay. "
        "Records at 0 cps are blanks in the source and are left out.", min(len(hits), CAPS["boulder"]), len(hits),
        "boulders")
    return R.out


def _line_axis(geom: Any) -> float:
    """A line's overall compass axis, 0-180: its segments' axes averaged as doubled angles, by length."""
    import shapely

    sx = sy = 0.0
    for part in shapely.get_parts(geom):
        d = np.diff(shapely.get_coordinates(part), axis=0)
        ln = np.hypot(d[:, 0], d[:, 1])
        ax = np.radians(2.0 * np.mod(np.degrees(np.arctan2(d[:, 0], d[:, 1])), 180.0))
        sx += float((ln * np.cos(ax)).sum())
        sy += float((ln * np.sin(ax)).sum())
    return float(np.mod(np.degrees(np.arctan2(sy, sx)) / 2.0, 180.0))


def _lines(R: _Rows, layer: str, tag: str, kind: str, centre: tuple[float, float], radius_m: float, cap: int
           ) -> int:
    import shapely
    from shapely.geometry import Point

    lines = F._layer(layer)
    here = Point(centre)
    disc = here.buffer(radius_m)
    idx = lines.sindex.query(disc, predicate="intersects")
    geoms = lines.geometry.to_numpy()[idx]
    d = shapely.distance(geoms, here)
    order = np.argsort(d, kind="stable")
    for j, k in enumerate(order[:cap]):
        t = f"{tag}{j}"
        g = geoms[k]
        row: dict[str, Any] = {"kind": kind}
        R.num(row, "dist_m", t, float(d[k]), "m", f"distance from the cell centre to {kind} {t}", digits=0)
        axis = _line_axis(g)
        row["trend"] = trend_name(axis)
        R.num(row, "trend_deg", t, axis, "deg", f"compass axis of {kind} {t}, from its geometry", digits=0)
        R.num(row, "length_in_reach_km", t, float(shapely.intersection(g, disc).length) / 1000.0, "km",
              f"length of {kind} {t} within {radius_m / 1000:g} km of the cell centre", digits=2)
        R.out.rows.append(row)
    return len(idx)


@lru_cache(maxsize=2)
def _crossings(other: str | None) -> np.ndarray:
    return F.crossing_points("faults_250k", other)


def evidence_structure(cell_id: str, radius_m: float = RADIUS_M) -> ToolResult:
    """Every lineament and the nearest conductors within reach, with trend and length, and the crossings."""
    R = _Rows("evidence_structure", cell_id, {"cell_id": cell_id, "radius_m": radius_m})
    centre = _cell_centre(cell_id)
    if centre is None:
        R.out.note = "no such cell"
        return R.out
    n_lin = _lines(R, "faults_250k", "f", "lineament", centre, radius_m, CAPS["lineament"])
    n_con = _lines(R, "em_conductors", "c", "conductor", centre, radius_m, CAPS["conductor"])
    for other, tag, what in ((None, "xf", "lineament crossing another lineament"),
                             ("em_conductors", "xc", "lineament crossing a conductor")):
        pts = _crossings(other)
        row: dict[str, Any] = {"kind": "crossings", "what": what}
        if len(pts):
            d = np.hypot(pts[:, 0] - centre[0], pts[:, 1] - centre[1])
            R.num(row, "within_5km", tag, float((d <= CROSSING_RADIUS_M).sum()), None,
                  f"places a {what} within {CROSSING_RADIUS_M / 1000:g} km of the cell centre")
            R.num(row, "nearest_m", tag, float(d.min()), "m", f"distance to the nearest {what}", digits=0)
        R.out.rows.append(row)
    if not n_lin and not n_con:
        R.out.rows.append({"kind": "none", "note": f"no lineament or conductor within {radius_m / 1000:g} km"})
    note = ("Lineaments are from the 1:250,000 map; trends are computed from each line's geometry. Conductors are "
            "electromagnetic survey picks, which trace graphitic basement units, and exist only where a survey "
            "was flown. A lineament crossing a conductor is the pathway-and-trap pairing the deposit model asks "
            "for.")
    note = _capped(note, min(n_lin, CAPS["lineament"]), n_lin, "lineaments")
    R.out.note = _capped(note, min(n_con, CAPS["conductor"]), n_con, "conductors")
    return R.out


def _unit_rows(R: _Rows, layer: str, field: str, tag: str, kind: str, centre: tuple[float, float],
               radius_m: float, cap: int, group: Callable[[Any], str | None] | None = None) -> int:
    import shapely
    from shapely.geometry import Point

    polys = F._layer(layer)
    here = Point(centre)
    disc = here.buffer(radius_m)
    idx = polys.sindex.query(disc, predicate="intersects")
    if not len(idx):
        return 0
    sub = polys.iloc[idx]
    text = sub[field].fillna("").astype(str).str.strip()
    area = shapely.area(shapely.intersection(sub.geometry.to_numpy(), disc)) / disc.area
    dist = shapely.distance(sub.geometry.to_numpy(), here)
    frame = pd.DataFrame({"text": text.to_numpy(), "share": area, "dist": dist})
    frame = frame[frame["text"] != ""]
    agg = frame.groupby("text", sort=False).agg(share=("share", "sum"), dist=("dist", "min")).reset_index()
    agg = agg.sort_values(["share", "text"], ascending=[False, True], kind="stable").reset_index(drop=True)
    for j, r in agg.head(cap).iterrows():
        t = f"{tag}{j}"
        row: dict[str, Any] = {"kind": kind, "description": str(r["text"]).lower()}
        if group is not None:
            row["group"] = group(r["text"]) or "other"
        row["under_cell_centre"] = "yes" if r["dist"] == 0 else "no"
        R.num(row, "share_of_area", t, float(r["share"]), None,
              f"share of the {radius_m / 1000:g} km disc round the cell the map gives to {kind} {t}", digits=3)
        if r["dist"] > 0:
            R.num(row, "dist_m", t, float(r["dist"]), "m", f"distance from the cell centre to {kind} {t}", digits=0)
        R.out.rows.append(row)
    return len(agg)


def evidence_bedrock(cell_id: str, radius_m: float = UNIT_RADIUS_M) -> ToolResult:
    """The bedrock units and surficial environments the maps show within reach, as they describe them."""
    R = _Rows("evidence_bedrock", cell_id, {"cell_id": cell_id, "radius_m": radius_m})
    centre = _cell_centre(cell_id)
    if centre is None:
        R.out.note = "no such cell"
        return R.out
    n_units = _unit_rows(R, "bedrock_250k", "LITHOLOGY", "u", "bedrock unit", centre, radius_m, CAPS["unit"],
                         group=F.lith_group)
    n_surf = _unit_rows(R, "surficial_250k", "MAIN_ENVIRONMENT", "q", "surficial environment", centre, radius_m,
                        CAPS["surficial"])
    if not n_units:
        R.out.rows.append({"kind": "none", "note": "no bedrock unit mapped within reach: unmapped, not absent"})
    note = ("Descriptions are the 1:250,000 maps' own words. Under the sandstone cover the bedrock map shows the "
            "cover, not the basement, so a covered cell says nothing about its basement rocks here. Surficial "
            "environments say how the ground was deposited: till carries boulders along the ice flow; "
            "glaciofluvial and glaciolacustrine deposits were moved by water.")
    note = _capped(note, min(n_units, CAPS["unit"]), n_units, "bedrock descriptions")
    R.out.note = _capped(note, min(n_surf, CAPS["surficial"]), n_surf, "surficial environments")
    return R.out


# ---------------------------------------------------------------- the region


@lru_cache(maxsize=1)
def _domains() -> dict[str, Any]:
    return F.dissolved_domains("bedrock_250k", "DOMAIN")


@lru_cache(maxsize=1)
def _domain_boundary() -> Any:
    return F.domain_boundary("bedrock_250k", "DOMAIN")


@lru_cache(maxsize=1)
def _domain_character() -> dict[str, list[tuple[str, float]]]:
    """Each domain's rocks: the lithology groups the map shows in it, by share of the domain's mapped area."""
    import shapely

    polys = F._layer("bedrock_250k")
    polys = polys[~polys.geometry.is_empty & polys.geometry.notna()]
    names = polys["DOMAIN"].fillna("").astype(str).str.strip()
    frame = pd.DataFrame({"domain": names.to_numpy(), "group": polys["LITHOLOGY"].map(F.lith_group).to_numpy(),
                          "area": shapely.area(polys.geometry.to_numpy())})
    frame = frame[frame["group"].notna() & (frame["domain"] != "")]
    out: dict[str, list[tuple[str, float]]] = {}
    for d, g in frame.groupby("domain"):
        shares = g.groupby("group")["area"].sum() / g["area"].sum()
        out[str(d)] = [(str(k), float(v)) for k, v in shares.sort_values(ascending=False).items()]
    return out


def region(cell_id: str, radius_m: float = REGION_RADIUS_M) -> ToolResult:
    """Where the cell sits: inside or outside the sandstone and how far from its edge, the basement domains in
    reach by letter with the rocks each is made of, and the distance to the nearest domain boundary."""
    import shapely
    from shapely.geometry import Point

    R = _Rows("region", cell_id, {"cell_id": cell_id, "radius_m": radius_m})
    centre = _cell_centre(cell_id)
    if centre is None:
        R.out.note = "no such cell"
        return R.out
    here = Point(centre)
    basin = F._basin_outline()
    inside = bool(shapely.contains(basin, here))
    cover: dict[str, Any] = {"kind": "cover", "under_sandstone_cover": "yes" if inside else "no"}
    R.num(cover, "dist_to_cover_edge_m", "cov", float(shapely.distance(basin.boundary, here)), "m",
          "distance from the cell centre to the edge of the sandstone cover", digits=0)
    R.out.rows.append(cover)
    doms = _domains()
    dist = {d: float(shapely.distance(g, here)) for d, g in doms.items()}
    near = sorted((d for d in dist if dist[d] <= radius_m), key=lambda d: (dist[d], d))[:CAPS["domain"]]
    character = _domain_character()
    for j, d in enumerate(near):
        label = chr(ord("A") + j)
        t = f"d{label}"
        row: dict[str, Any] = {"kind": "basement domain", "domain": label,
                               "contains_cell": "yes" if dist[d] == 0 else "no"}
        if dist[d] > 0:
            R.num(row, "dist_m", t, dist[d], "m", f"distance from the cell centre to basement domain {label}",
                  digits=0)
        for k, (group, share) in enumerate(character.get(d, [])[:4]):
            row[f"rock_{k + 1}"] = group
            R.num(row, f"rock_{k + 1}_share", t, share, None,
                  f"share of domain {label}'s mapped area that is {group}", digits=3)
        R.out.rows.append(row)
    boundary = _domain_boundary()
    if boundary is not None:
        row = {"kind": "domain boundary"}
        R.num(row, "dist_m", "bd", float(shapely.distance(boundary, here)), "m",
              "distance from the cell centre to the nearest mapped boundary between two basement domains", digits=0)
        R.out.rows.append(row)
    R.out.note = ("Basement domains are named here by letter, nearest first, and described by the rocks the map "
                  "shows across each; their names are withheld. Domain boundaries are crustal-scale shear zones. "
                  "Under the sandstone no domain is mapped, so a covered cell lies in none and is measured to the "
                  "nearest domain beyond the cover.")
    return R.out


def all_tools(cell_id: str) -> dict[str, ToolResult]:
    """Every family and the region for one cell, by tool name."""
    return {"evidence_geochem": evidence_geochem(cell_id), "evidence_boulders": evidence_boulders(cell_id),
            "evidence_structure": evidence_structure(cell_id), "evidence_bedrock": evidence_bedrock(cell_id),
            "region": region(cell_id)}


def clear_caches() -> None:
    """For tests that swap the layers under the cached geometry."""
    for f in (_ice_table, _crossings, _domains, _domain_boundary, _domain_character):
        f.cache_clear()
