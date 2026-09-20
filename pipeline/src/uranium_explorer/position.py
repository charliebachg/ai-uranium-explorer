"""`ue crs transform`: put each extracted collar on the map, or refuse to and say why.

`crs.py` owns the transformation itself (explicit NTv2 pipeline, pinned grid sha, canary). This module
decides, per collar, whether a transformation is even allowed:

- **datum printed** -> UTM (same datum) then NAD27 -> NAD83 through the grid; the operation name, code,
  pipeline string and grid sha256 are logged on the row, and `misread_lonlat` records where the same
  printed numbers would plot if the datum were ignored (the demo's misread toggle).
- **no datum printed** -> never transformed. Status `not_transformable_no_datum`. Both candidate
  positions are kept (`lonlat` from the provincial match, `alt_lonlat` the other candidate) and the UI
  does not choose.
- **local grid** -> never transformed. Status `not_transformable_local_grid`; the hole is placed by a
  provincial name match and labelled `provincial_geods` or `provincial_compilation`.

A UTM zone that is not printed is inferred by trying zones 12 and 13 and keeping the one that lands in
the file's NTS sheets; `zone_inferred` is recorded and the hole is flagged.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable

from . import crs
from .assemble import assembled_files, read_assembled
from .ids import sha256_json, short
from .normalise import FT_TO_M, normalise_hole_name
from .nts import nts_bounds, point_in_sheet
from .paths import PATHS
from .values import PROJ_TOOL, derived

POSITION_VERSION = "position/v1"
CANDIDATE_ZONES = (12, 13)
FILE_BUFFER_KM = 2.0

_DATUM_WORDS = {
    "nad27": "NAD27", "nad 27": "NAD27", "nad-27": "NAD27", "north american datum 1927": "NAD27",
    "nad83": "NAD83", "nad 83": "NAD83", "nad-83": "NAD83", "north american datum 1983": "NAD83",
    "wgs84": "WGS84", "wgs 84": "WGS84",
}


def parse_utm_zone(value: Any) -> int | None:
    """A printed zone may be a number, "13", or "Zone 13V": take the number, never the latitude band."""
    if isinstance(value, (int, float)):
        return int(value) if 1 <= int(value) <= 60 else None
    m = re.search(r"(\d{1,2})", str(value or ""))
    if not m:
        return None
    zone = int(m.group(1))
    return zone if 1 <= zone <= 60 else None


def parse_datum(text: str | None) -> tuple[str | None, str]:
    if not text:
        return None, "no datum printed"
    t = " ".join(str(text).lower().split())
    for key, out in _DATUM_WORDS.items():
        if key in t.replace(".", ""):
            return out, f"printed datum {text!r}"
    if "27" in t and "nad" in t:
        return "NAD27", f"printed datum {text!r}"
    if "83" in t and "nad" in t:
        return "NAD83", f"printed datum {text!r}"
    return None, f"printed text {text!r} does not name a datum this pipeline knows"


def positions_path(file_num: str):
    return PATHS.out / "crs" / f"{file_num}.json"


def read_positions(file_num: str) -> dict[str, Any]:
    p = positions_path(file_num)
    return json.loads(p.read_text()) if p.is_file() else {}


def _num(doc: dict[str, Any], vid: str | None) -> float | None:
    v = (doc.get("values", {}).get(vid or "") or {}).get("value")
    return float(v) if isinstance(v, (int, float)) else None


def _printed(doc: dict[str, Any], vid: str | None) -> str | None:
    return (doc.get("values", {}).get(vid or "") or {}).get("as_printed")


def _provincial_position(ctx: dict[str, Any], hole: dict[str, Any]) -> tuple[list[float] | None, str, dict[str, Any]]:
    """Fall back to the province: a name match in GeoDS, then in the compilation."""
    key = normalise_hole_name(hole.get("name_as_printed"))
    for source, label in (("geods_holes", "provincial_geods"), ("compilation_holes", "provincial_compilation")):
        for h in ctx.get(source, []):
            if normalise_hole_name(h.get("name")) == key and h.get("lonlat"):
                lon, lat = h["lonlat"][0], h["lonlat"][1]
                return [round(lon, 6), round(lat, 6)], label, {"dataset": source, "record_id": h.get("id"),
                                                               "name": h.get("name")}
    return None, "none", {}


def _file_hole_points(ctx: dict[str, Any]) -> list[tuple[float, float]]:
    pts = []
    for source in ("geods_holes", "compilation_holes"):
        for h in ctx.get(source, []):
            if h.get("lonlat"):
                pts.append((float(h["lonlat"][0]), float(h["lonlat"][1])))
    return pts


def _checks(lon: float, lat: float, ctx: dict[str, Any]) -> dict[str, Any]:
    sheets = [s for s in (ctx.get("nts_sheets") or []) if s]
    inside_nts = None
    if sheets:
        inside_nts = any(_point_in_sheet_safe(lon, lat, s) for s in sheets)
    pts = _file_hole_points(ctx)
    nearest_km = None
    if pts:
        nearest_km = round(min(crs.geodesic_shift(lon, lat, p[0], p[1]).dist_m for p in pts) / 1000.0, 3)
    return {
        "nts_sheets": sheets, "inside_nts": inside_nts,
        "distance_to_file_holes_km": nearest_km,
        "inside_file_polygon": None if nearest_km is None else nearest_km <= FILE_BUFFER_KM,
        "file_buffer_km": FILE_BUFFER_KM,
    }


def _point_in_sheet_safe(lon: float, lat: float, sheet: str) -> bool:
    try:
        return point_in_sheet(lon, lat, sheet, tol_deg=0.05)
    except Exception:
        return False


def _zone_from_sheets(ctx: dict[str, Any]) -> int | None:
    lons = []
    for s in ctx.get("nts_sheets") or []:
        try:
            x0, _y0, x1, _y1 = nts_bounds(s)
            lons.append((x0 + x1) / 2)
        except Exception:
            continue
    if not lons:
        return None
    return crs.utm_zone_for_lon(sum(lons) / len(lons))


def transform_hole(doc: dict[str, Any], hole: dict[str, Any], ctx: dict[str, Any],
                   tr: crs.Nad27ToNad83 | None) -> dict[str, Any]:
    collar = hole.get("collar", {})
    value_ids = [v for v in collar.values() if v]
    easting, northing = _num(doc, collar.get("easting")), _num(doc, collar.get("northing"))
    lat, lon = _num(doc, collar.get("latitude")), _num(doc, collar.get("longitude"))
    grid_x, grid_y = _num(doc, collar.get("grid_x")), _num(doc, collar.get("grid_y"))
    datum_printed = _printed(doc, collar.get("datum"))
    datum, datum_rule = parse_datum(datum_printed)
    zone_printed = parse_utm_zone((doc.get("values", {}).get(collar.get("utm_zone") or "") or {}).get("value"))
    coord_kinds = {p.get("coordinate_kind") for p in doc.get("pages", [])
                   if p.get("page") in (hole.get("pages") or [])}

    out: dict[str, Any] = {
        "hole_id": hole["hole_id"], "name_as_printed": hole.get("name_as_printed"),
        "value_ids": value_ids, "datum_printed": datum_printed, "datum": datum, "datum_rule": datum_rule,
        "coord_kinds_on_page": sorted(k for k in coord_kinds if k),
        "easting": easting, "northing": northing, "lat_printed": lat, "lon_printed": lon,
        "grid_x": grid_x, "grid_y": grid_y,
        "status": "no_coordinates", "position_source": "none", "lonlat": None,
        "misread_lonlat": None, "alt_lonlat": None, "transform": None, "zone": None,
        "zone_inferred": False, "notes": [], "checks": {}, "provincial": {},
    }

    # Precedence: a hole that prints a plausible projected position is placed by it, even when the same
    # sheet also prints the local exploration grid line it sits on (the 2005 file prints both). Only a
    # hole with nothing projected to work from is a local-grid hole.
    projected = (lat is not None and lon is not None) or (
        easting is not None and northing is not None and not crs.looks_like_local_grid(easting, northing))
    local_grid = not projected and (
        ("local_grid" in coord_kinds) or (grid_x is not None or grid_y is not None)
        or (easting is not None and northing is not None and crs.looks_like_local_grid(easting, northing)))
    if projected and (grid_x is not None or grid_y is not None):
        out["notes"].append("this sheet also prints a local grid reference; the projected coordinates are "
                            "used and the grid reference is kept as printed")
    if local_grid:
        out["status"] = "not_transformable_local_grid"
        out["notes"].append("the printed coordinates are a local exploration grid, not a projected system: "
                            "no transformation is possible without the grid's own tie points")
    elif not projected and easting is None and northing is None and lat is None and lon is None:
        out["status"] = "no_coordinates"
        out["notes"].append("no collar coordinates are printed on the pages read")
    elif datum is None:
        out["status"] = "not_transformable_no_datum"
        out["notes"].append("no datum is printed: the same numbers plot in two places about 30 to 50 m apart "
                            "in this region, and this pipeline does not choose between them")
    else:
        out["status"] = "transformed"

    # ---- the two candidate readings of the printed numbers
    def to_geographic(d: str) -> tuple[float, float] | None:
        if lat is not None and lon is not None:
            return (lon if lon < 0 else -lon, lat)
        if easting is None or northing is None:
            return None
        zone = zone_printed
        if zone is None:
            zone = _infer_zone(easting, northing, d, ctx, out)
        if zone is None:
            return None
        out["zone"] = zone
        try:
            return crs.utm_to_geographic(easting, northing, zone, d)
        except crs.CrsError as e:
            out["notes"].append(f"UTM conversion failed on {d} zone {zone}: {e}")
            return None

    as_nad27: tuple[float, float] | None = None
    as_nad83: tuple[float, float] | None = None
    transformed: tuple[float, float] | None = None
    if out["status"] in ("transformed", "not_transformable_no_datum"):
        as_nad27 = to_geographic("NAD27")
        as_nad83 = to_geographic("NAD83") if (easting is not None and northing is not None) else \
            ((lon if lon < 0 else -lon, lat) if (lat is not None and lon is not None) else None)
        if as_nad27 and tr is not None:
            try:
                transformed = tr.transform(*as_nad27)
            except crs.OutsideGridError as e:
                out["notes"].append(f"outside the NTv2 grid: {e}")

    if out["status"] == "transformed" and datum == "NAD27":
        if transformed is None:
            out["status"] = "transform_failed"
            out["notes"].append("the NAD27 to NAD83 transformation could not be applied")
        else:
            shift = crs.geodesic_shift(as_nad27[0], as_nad27[1], transformed[0], transformed[1])
            misread_offset = None
            if as_nad83:
                # How far the collar lands if the datum is ignored. For printed *UTM* coordinates this is
                # much larger than the 34 m geographic datum shift: NAD27 and NAD83 UTM northings differ
                # by about 200 m in this region because the two ellipsoids measure the meridian
                # differently. For printed lat/lon it is the datum shift itself.
                misread_offset = round(crs.geodesic_shift(transformed[0], transformed[1],
                                                          as_nad83[0], as_nad83[1]).dist_m, 2)
            out.update({
                "lonlat": [round(transformed[0], 6), round(transformed[1], 6)],
                "position_source": "extracted_transformed",
                "misread_lonlat": [round(as_nad83[0], 6), round(as_nad83[1], 6)] if as_nad83 else None,
                "misread_offset_m": misread_offset,
                "misread_kind": "utm_read_on_the_wrong_datum" if (easting is not None) else
                                "geographic_read_on_the_wrong_datum",
                "shift_m": round(shift.dist_m, 2), "bearing_deg": round(shift.bearing_deg, 1),
                "transform": {
                    "name": tr.log.operation_name, "code": tr.log.operation_code,
                    "pipeline": tr.log.pipeline_definition, "grid_file": tr.log.grid_file,
                    "grid_sha256": tr.log.grid_sha256, "from": "EPSG:4267", "to": "EPSG:4269",
                    "accuracy_m": tr.log.accuracy_m, "proj_version": tr.log.proj_version,
                    "pyproj_version": tr.log.pyproj_version,
                },
            })
    elif out["status"] == "transformed" and datum in ("NAD83", "WGS84"):
        if as_nad83:
            out.update({"lonlat": [round(as_nad83[0], 6), round(as_nad83[1], 6)],
                        "position_source": "extracted_nad83", "shift_m": 0.0, "bearing_deg": 0.0,
                        "transform": None})
            out["notes"].append(f"the page prints {datum}: no datum transformation is needed")
        else:
            out["status"] = "transform_failed"
    elif out["status"] == "not_transformable_no_datum":
        prov_lonlat, prov_source, prov = _provincial_position(ctx, hole)
        out["provincial"] = prov
        out["lonlat"] = prov_lonlat
        out["position_source"] = prov_source
        if as_nad27 and transformed and as_nad83:
            out["alt_lonlat"] = [round(as_nad83[0], 6), round(as_nad83[1], 6)]
            out["candidates"] = {
                "read_as_nad27_then_transformed": [round(transformed[0], 6), round(transformed[1], 6)],
                "read_as_nad83": [round(as_nad83[0], 6), round(as_nad83[1], 6)],
                "separation_m": round(crs.geodesic_shift(transformed[0], transformed[1],
                                                         as_nad83[0], as_nad83[1]).dist_m, 1),
            }
            if prov_lonlat is None:
                out["lonlat"] = None
    if out["status"] in ("not_transformable_local_grid", "no_coordinates", "transform_failed") \
            and out["lonlat"] is None:
        prov_lonlat, prov_source, prov = _provincial_position(ctx, hole)
        out["provincial"] = prov
        out["lonlat"] = prov_lonlat
        out["position_source"] = prov_source
        if prov_lonlat is not None:
            out["notes"].append(f"placed by a provincial name match ({prov_source}), not by anything printed "
                                "in this report")

    if out["lonlat"]:
        out["checks"] = _checks(out["lonlat"][0], out["lonlat"][1], ctx)
    return out


def _infer_zone(easting: float, northing: float, datum: str, ctx: dict[str, Any],
                out: dict[str, Any]) -> int | None:
    """No zone printed: try 12 and 13, keep the one that lands inside the file's NTS sheets."""
    sheets = [s for s in (ctx.get("nts_sheets") or []) if s]
    fits: list[int] = []
    for zone in CANDIDATE_ZONES:
        try:
            lon, lat = crs.utm_to_geographic(easting, northing, zone, datum)
        except crs.CrsError:
            continue
        if sheets and any(_point_in_sheet_safe(lon, lat, s) for s in sheets):
            fits.append(zone)
    if len(fits) == 1:
        out["zone_inferred"] = True
        out["notes"].append(f"no UTM zone is printed: zone {fits[0]} is the only one of {list(CANDIDATE_ZONES)} "
                            f"that lands inside the file's NTS sheets {sheets}")
        return fits[0]
    fallback = _zone_from_sheets(ctx)
    if fallback:
        out["zone_inferred"] = True
        out["notes"].append(f"no UTM zone is printed and neither zone 12 nor 13 lands inside the file's sheets; "
                            f"zone {fallback} was taken from the sheet centre and the position is flagged")
        return fallback
    out["notes"].append("no UTM zone is printed and none could be inferred from the file's NTS sheets")
    return None


def stage_position(files: list[str] | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    from .validators import build_context

    tr: crs.Nad27ToNad83 | None
    try:
        tr = crs.Nad27ToNad83()
        log(f"  NTv2 grid {tr.log.grid_file} sha256 {tr.grid_sha256[:12]}, "
            f"operation {tr.log.operation_name} ({tr.log.operation_code}), canary ok")
    except crs.CrsError as e:
        tr = None
        log(f"  no usable NTv2 grid ({e}): no collar will be transformed")

    targets = [f for f in assembled_files() if not files or f in files]
    summary: dict[str, Any] = {"files": {}}
    for file_num in targets:
        doc = read_assembled(file_num)
        ctx = build_context(file_num)
        holes = [transform_hole(doc, h, ctx, tr) for h in doc.get("holes", [])]
        values: dict[str, dict[str, Any]] = {}
        for h in holes:
            if not h.get("lonlat"):
                continue
            key = short(sha256_json([file_num, h["hole_id"], h["lonlat"]]), 8)
            h["lon_vid"] = f"d:{file_num}:pos_{key}_lon"
            h["lat_vid"] = f"d:{file_num}:pos_{key}_lat"
            inputs = [v for v in h.get("value_ids", []) if v]
            values[h["lon_vid"]] = derived(h["lon_vid"], h["lonlat"][0], "ratio3", _op(h), inputs,
                                           tool=PROJ_TOOL, params=_params(h), unit="deg",
                                           note="longitude, WGS84/NAD83")
            values[h["lat_vid"]] = derived(h["lat_vid"], h["lonlat"][1], "ratio3", _op(h), inputs,
                                           tool=PROJ_TOOL, params=_params(h), unit="deg",
                                           note="latitude, WGS84/NAD83")
            if h.get("transform"):
                h["shift_vid"] = f"d:{file_num}:pos_{key}_shift"
                h["bearing_vid"] = f"d:{file_num}:pos_{key}_bearing"
                values[h["shift_vid"]] = derived(h["shift_vid"], h["shift_m"], "m1", "ntv2_shift", inputs,
                                                 tool=PROJ_TOOL, unit="m",
                                                 params={"operation": h["transform"]["name"],
                                                         "grid_sha256": h["transform"]["grid_sha256"]},
                                                 note="how far this collar moves from NAD27 to NAD83")
                values[h["bearing_vid"]] = derived(h["bearing_vid"], h["bearing_deg"], "deg1", "ntv2_shift",
                                                   inputs, tool=PROJ_TOOL, unit="deg",
                                                   params={"operation": h["transform"]["name"]},
                                                   note="bearing of the datum shift")
        out = {
            "version": POSITION_VERSION, "file_num": file_num,
            "computed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "grid": {"file": tr.log.grid_file, "sha256": tr.grid_sha256,
                     "operation": tr.log.operation_name, "code": tr.log.operation_code,
                     "proj_version": tr.log.proj_version} if tr else None,
            "holes": holes, "values": values,
        }
        positions_path(file_num).parent.mkdir(parents=True, exist_ok=True)
        positions_path(file_num).write_text(json.dumps(out, separators=(",", ":"), default=str))
        by_status: dict[str, int] = {}
        for h in holes:
            by_status[h["status"]] = by_status.get(h["status"], 0) + 1
        plottable = sum(1 for h in holes if h.get("lonlat"))
        summary["files"][file_num] = {"holes": len(holes), "plottable": plottable, "status": by_status}
        log(f"  {file_num}: {len(holes)} holes, {plottable} plottable "
            f"({', '.join(f'{k}:{v}' for k, v in sorted(by_status.items()))})")
    return summary


def _op(h: dict[str, Any]) -> str:
    return {"extracted_transformed": "ntv2_nad27_to_nad83", "extracted_nad83": "utm_to_geographic",
            "provincial_geods": "provincial_position", "provincial_compilation": "provincial_position",
            }.get(h.get("position_source") or "", "provincial_position")


def _params(h: dict[str, Any]) -> dict[str, Any]:
    p: dict[str, Any] = {"status": h["status"], "position_source": h["position_source"]}
    if h.get("zone"):
        p["utm_zone"] = h["zone"]
        p["utm_zone_inferred"] = h["zone_inferred"]
    if h.get("transform"):
        p["operation"] = h["transform"]["name"]
        p["grid_sha256"] = h["transform"]["grid_sha256"]
    if h.get("provincial"):
        p["provincial_record"] = h["provincial"]
    return p


FEET_PER_METRE = 1 / FT_TO_M
