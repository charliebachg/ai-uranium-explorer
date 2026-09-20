"""`ue crosscheck`: the extracted collars against the province's two independent compilations.

This is not a gold set. Both provincial layers are compilations with their own transcription history, so
a disagreement is a question, not a verdict: every one of them lands in an adjudication queue with the
numbers that raised it.

The one inference worth making is the **datum-shift signature**: when an extracted collar sits the local
NAD27-to-NAD83 distance away from its provincial twin, in the local shift's direction, the likely cause
is that one of the two readings ignored the datum. That is recorded per match, never auto-corrected.

Provincial lithology (GeoDS drilling layer 4) is fetched for matched hole names only, paced through the
ArcGIS client, and stored as `p:` source values so the strip log can show it beside what we read.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

from rapidfuzz import fuzz

from . import crs
from .arcgis import ArcGisClient
from .assemble import assembled_files, read_assembled
from .ids import sha256_json, short
from .index import GEODS
from .normalise import normalise_hole_name
from .paths import PATHS
from .position import read_positions
from .values import PROJ_TOOL, TOOL, derived, source

CROSSCHECK_VERSION = "crosscheck/v1"
GEODS_LITH_LAYER = f"{GEODS}/P_GeoDS_DrillingPage_EM/FeatureServer/4"

NAME_FUZZ_MIN = 90.0
NEAR_NAME_KM = 5.0
NEAREST_CANDIDATE_M = 250.0
ADJUDICATE_OFFSET_M = 100.0
SHIFT_DISTANCE_TOL_M = 8.0
SHIFT_BEARING_TOL_DEG = 20.0

# Field names on P_GeoDS_DrillingPage_EM/FeatureServer/4 ("Public Query Lithology"), read from the
# layer's own metadata on 2026-09-18. The layer publishes depths in both metres and feet; metres are
# taken, since the province has already done that conversion and says so.
_LITH_FIELDS = {
    "from": ("DPTH_FROM_M", "DPTH_FROM_FT"),
    "to": ("DPTH_TO_M", "DPTH_TO_FT"),
    "code": ("MATRL_TYPE", "MATRL_NAME_REVSD"),
    "description": ("MATRL_NAME_ORIGNL", "MATRL_NAME_REVSD", "MATRL_OTHER_NAME", "CMNT"),
    "hole": ("HOLE_NAME", "ORIGINAL_ID_NUMBER", "SYSTEM_WELBORE_UWI_TEMP_HOLE_ID"),
}


def crosscheck_path(file_num: str):
    return PATHS.out / "crosscheck" / f"{file_num}.json"


def read_crosscheck(file_num: str) -> dict[str, Any]:
    p = crosscheck_path(file_num)
    return json.loads(p.read_text()) if p.is_file() else {}


def name_match_kind(extracted: str, provincial: str) -> tuple[str | None, float]:
    """exact | normalised | fuzzy, with the score that decided it."""
    if not extracted or not provincial:
        return None, 0.0
    if extracted.strip() == provincial.strip():
        return "exact", 100.0
    a, b = normalise_hole_name(extracted), normalise_hole_name(provincial)
    if a and a == b:
        return "normalised", 100.0
    score = float(fuzz.ratio(a, b))
    return ("fuzzy", score) if score >= NAME_FUZZ_MIN else (None, score)


def _shift_signature(lonlat: list[float], prov_lonlat: list[float], tr: crs.Nad27ToNad83 | None
                     ) -> tuple[bool, dict[str, Any]]:
    """Does the offset look like a missed datum transformation at this location?"""
    off = crs.geodesic_shift(lonlat[0], lonlat[1], prov_lonlat[0], prov_lonlat[1])
    details: dict[str, Any] = {"offset_m": round(off.dist_m, 2), "bearing_deg": round(off.bearing_deg, 1)}
    if tr is None:
        return False, details
    local = tr.shift(lonlat[0], lonlat[1])
    details["local_datum_shift_m"] = round(local.dist_m, 2)
    details["local_datum_shift_bearing_deg"] = round(local.bearing_deg, 1)
    dist_ok = abs(off.dist_m - local.dist_m) <= SHIFT_DISTANCE_TOL_M
    delta = abs((off.bearing_deg - local.bearing_deg + 180.0) % 360.0 - 180.0)
    reverse = abs((off.bearing_deg - (local.bearing_deg + 180.0) + 180.0) % 360.0 - 180.0)
    details["bearing_difference_deg"] = round(min(delta, reverse), 1)
    details["distance_tolerance_m"] = SHIFT_DISTANCE_TOL_M
    details["bearing_tolerance_deg"] = SHIFT_BEARING_TOL_DEG
    return bool(dist_ok and min(delta, reverse) <= SHIFT_BEARING_TOL_DEG), details


def _candidates(hole: dict[str, Any], pos: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    name = hole.get("name_as_printed") or ""
    lonlat = pos.get("lonlat")
    out: list[dict[str, Any]] = []
    for dataset, key in (("geods", "geods_holes"), ("compilation", "compilation_holes")):
        for h in ctx.get(key, []):
            kind, score = name_match_kind(name, h.get("name") or "")
            distance_m = None
            if lonlat and h.get("lonlat"):
                distance_m = crs.geodesic_shift(lonlat[0], lonlat[1], h["lonlat"][0], h["lonlat"][1]).dist_m
            if kind is None:
                # name within 5 km, then nearest within 250 m as a candidate only
                if distance_m is not None and distance_m <= NEAR_NAME_KM * 1000 and score >= 70:
                    kind = "fuzzy"
                elif distance_m is not None and distance_m <= NEAREST_CANDIDATE_M:
                    kind = "nearest"
                else:
                    continue
            out.append({"dataset": dataset, "record": h, "name_match": kind, "name_score": round(score, 1),
                        "distance_m": None if distance_m is None else round(distance_m, 2)})
    out.sort(key=lambda c: ({"exact": 0, "normalised": 1, "fuzzy": 2, "nearest": 3}[c["name_match"]],
                            c["distance_m"] if c["distance_m"] is not None else 1e9))
    return out


def _numeric_diff(a: Any, b: Any) -> float | None:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return round(float(a) - float(b), 3)
    return None


def crosscheck_file(file_num: str, doc: dict[str, Any], positions: dict[str, Any], ctx: dict[str, Any],
                    tr: crs.Nad27ToNad83 | None) -> dict[str, Any]:
    pos_by_hole = {p["hole_id"]: p for p in positions.get("holes", [])}
    values: dict[str, dict[str, Any]] = {}
    matches_out: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    matched_provincial: set[tuple[str, int]] = set()

    for hole in doc.get("holes", []):
        pos = pos_by_hole.get(hole["hole_id"], {})
        cands = _candidates(hole, pos, ctx)
        if not cands:
            queue.append({"reason": "extracted hole with no provincial match", "hole_id": hole["hole_id"],
                          "name_as_printed": hole.get("name_as_printed"),
                          "position_source": pos.get("position_source"), "status": pos.get("status")})
            continue
        strong = [c for c in cands if c["name_match"] in ("exact", "normalised")]
        if len(strong) > 1:
            queue.append({"reason": "more than one provincial record matches this name",
                          "hole_id": hole["hole_id"],
                          "candidates": [{"dataset": c["dataset"], "id": c["record"].get("id"),
                                          "name": c["record"].get("name")} for c in strong]})
        seen_datasets: set[str] = set()
        for cand in cands:
            if cand["dataset"] in seen_datasets:
                continue
            seen_datasets.add(cand["dataset"])
            rec = cand["record"]
            matched_provincial.add((cand["dataset"], int(rec.get("id") or 0)))
            lonlat = pos.get("lonlat")
            prov_lonlat = rec.get("lonlat")
            signature, shift_details = (False, {})
            offset_vid = bearing_vid = None
            if lonlat and prov_lonlat:
                signature, shift_details = _shift_signature(lonlat, prov_lonlat, tr)
                key = short(sha256_json([file_num, hole["hole_id"], cand["dataset"], rec.get("id")]), 8)
                offset_vid = f"d:{file_num}:off_{key}"
                bearing_vid = f"d:{file_num}:brg_{key}"
                inputs = [v for v in (pos.get("lon_vid"), pos.get("lat_vid")) if v]
                # An offset only measures the reading when the collar came off the page. A hole placed
                # by a provincial name match is being compared with the record that placed it, so its
                # offset is zero by construction and says nothing about extraction accuracy.
                independent = str(pos.get("position_source") or "").startswith("extracted")
                note = (f"distance from this extracted collar to the {cand['dataset']} record for the "
                        "same hole")
                if not independent:
                    note = (f"this collar was placed by a provincial record ({pos.get('position_source')}), "
                            f"so its distance to the {cand['dataset']} record is not a measure of how well "
                            "the page was read")
                values[offset_vid] = derived(
                    offset_vid, shift_details["offset_m"], "m1", "geodesic_offset", inputs, tool=PROJ_TOOL,
                    unit="m", params={"dataset": cand["dataset"], "record_id": rec.get("id"),
                                      "ellipsoid": "GRS80", "independent_of_the_provincial_record": independent},
                    note=note)
                values[bearing_vid] = derived(
                    bearing_vid, shift_details["bearing_deg"], "deg1", "geodesic_offset", inputs,
                    tool=PROJ_TOOL, unit="deg", params={"dataset": cand["dataset"]},
                    note="bearing from this collar to the provincial record")
            diffs = {
                "total_depth_m": _numeric_diff(_hole_total_depth_m(doc, hole), rec.get("total_depth_m")),
                "dip_deg": _numeric_diff(_hole_number(doc, hole, "dip"), rec.get("inclination_deg")),
                "azimuth_deg": _numeric_diff(_hole_number(doc, hole, "azimuth"), rec.get("azimuth_deg")),
            }
            adjudication = "none"
            reasons = []
            if shift_details.get("offset_m", 0.0) > ADJUDICATE_OFFSET_M:
                reasons.append(f"offset {shift_details['offset_m']} m is over {ADJUDICATE_OFFSET_M} m")
            if signature:
                reasons.append("the offset matches the local NAD27 to NAD83 shift in size and direction")
            if reasons:
                adjudication = "needed"
                queue.append({"reason": "; ".join(reasons), "hole_id": hole["hole_id"],
                              "dataset": cand["dataset"], "feature_id": rec.get("id"),
                              "offset_m": shift_details.get("offset_m"), "details": shift_details})
            matches_out.append({
                "hole_id": hole["hole_id"], "dataset": cand["dataset"], "feature_id": int(rec.get("id") or 0),
                "provincial_name": rec.get("name"), "lonlat": prov_lonlat,
                "offset_m": offset_vid, "bearing_deg": bearing_vid,
                "offset_m_value": shift_details.get("offset_m"),
                "name_match": cand["name_match"], "name_score": cand["name_score"],
                "offset_independent": str(pos.get("position_source") or "").startswith("extracted"),
                "position_source": pos.get("position_source"),
                "datum_shift_signature": signature, "shift_details": shift_details,
                "differences": diffs, "adjudication": adjudication,
            })

    for dataset, key in (("geods", "geods_holes"), ("compilation", "compilation_holes")):
        for h in ctx.get(key, []):
            if (dataset, int(h.get("id") or 0)) not in matched_provincial:
                queue.append({"reason": f"{dataset} links this hole to the file but no collar was extracted "
                                        "for it", "dataset": dataset, "feature_id": h.get("id"),
                              "provincial_name": h.get("name")})

    return {"version": CROSSCHECK_VERSION, "file_num": file_num,
            "computed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "matches": matches_out, "values": values, "adjudication_queue": queue,
            "provincial_lith": {}, "provincial_lith_values": {}}


def _hole_number(doc: dict[str, Any], hole: dict[str, Any], field: str) -> float | None:
    vid = hole.get("collar", {}).get(field)
    v = (doc.get("values", {}).get(vid or "") or {}).get("value")
    return float(v) if isinstance(v, (int, float)) else None


def _hole_total_depth_m(doc: dict[str, Any], hole: dict[str, Any]) -> float | None:
    from .normalise import FT_TO_M

    vid = hole.get("collar", {}).get("total_depth")
    v = (doc.get("values", {}).get(vid or "") or {}).get("value")
    if not isinstance(v, (int, float)):
        depths = [i.get("to_m_value") for i in hole.get("lith", []) + hole.get("assays", [])
                  if isinstance(i.get("to_m_value"), (int, float))]
        return max(depths) if depths else None
    unit = (doc.get("value_meta", {}).get(vid) or {}).get("unit_norm")
    return round(float(v) * FT_TO_M, 2) if unit == "ft" else float(v)


# ------------------------------------------------------------------ provincial lithology

def _pick(attrs: dict[str, Any], names: tuple[str, ...]) -> tuple[Any, str | None]:
    """(value, the field it came from) so the stored source reference names a real column."""
    for n in names:
        if n in attrs and attrs[n] not in (None, ""):
            return attrs[n], n
    return None, None


def fetch_provincial_lith(names: list[str], client: ArcGisClient,
                          log: Callable[[str], None] = print) -> dict[str, list[dict[str, Any]]]:
    """GeoDS drilling layer 4, one hole name per query (the WAF blocks compound filters)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for name in sorted({n for n in names if n}):
        safe = name.replace("'", "''")
        try:
            feats = client.fetch_all(GEODS_LITH_LAYER, where=f"HOLE_NAME='{safe}'", out_fields="*",
                                     geometry=False, fmt="json")
        except Exception as e:  # a WAF block must not lose the run
            log(f"    provincial lithology for {name!r} failed: {type(e).__name__}: {str(e)[:120]}")
            continue
        rows = []
        for f in feats:
            a = f.get("attributes", {})
            row: dict[str, Any] = {"record_id": a.get("OBJECTID") or a.get("ObjectID"),
                                   "hole_name": _pick(a, _LITH_FIELDS["hole"])[0] or name,
                                   "source_fields": {}}
            for key in ("from", "to", "code", "description"):
                value, field = _pick(a, _LITH_FIELDS[key])
                row[{"from": "from_m", "to": "to_m"}.get(key, key)] = value
                row["source_fields"][key] = field
            if row.get("from_m") is not None and row["source_fields"]["from"] == "DPTH_FROM_FT":
                row["from_m"] = row["to_m"] = None  # only feet published: do not silently convert
                row["source_fields"]["note"] = "the province published this interval in feet only"
            rows.append(row)
        if rows:
            out[name] = rows
            log(f"    provincial lithology {name}: {len(rows)} intervals")
    return out


def build_provincial_lith(file_num: str, lith: dict[str, list[dict[str, Any]]], retrieved_at: str
                          ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Provincial intervals as `p:` source values, matching the contract's Interval shape."""
    values: dict[str, dict[str, Any]] = {}
    out: dict[str, list[dict[str, Any]]] = {}
    for name, rows in lith.items():
        intervals = []
        for i, row in enumerate(sorted(rows, key=lambda r: (r.get("from_m") if isinstance(r.get("from_m"), (int, float)) else 0))):
            key = short(sha256_json([file_num, name, row.get("record_id"), i]), 8)

            def sv(suffix: str, value: Any, fmt: str, field: str, unit: str | None = None) -> str | None:
                if value is None:
                    return None
                vid = f"p:{file_num}:{key}_{suffix}"
                values[vid] = source(vid, value, fmt, "geods_lith", row.get("record_id") or 0, field,
                                     retrieved_at, unit=unit)
                return vid

            fields = row.get("source_fields") or {}
            frm = sv("from", row.get("from_m"), "m1", fields.get("from") or "DPTH_FROM_M", "m")
            to = sv("to", row.get("to_m"), "m1", fields.get("to") or "DPTH_TO_M", "m")
            if frm is None or to is None:
                continue
            intervals.append({
                "id": f"p:{name}:{i}", "from": frm, "to": to, "from_m": frm, "to_m": to,
                "code": sv("code", row.get("code"), "text", fields.get("code") or "MATRL_TYPE"),
                "description": sv("desc", row.get("description"), "text",
                                  fields.get("description") or "MATRL_NAME_ORIGNL"),
                "table_id": None, "row": i, "status": "pass", "depth_unit_as_printed": None,
            })
        if intervals:
            out[name] = intervals
    return out, values


# ------------------------------------------------------------------ stage

def stage_crosscheck(files: list[str] | None = None, fetch_lith: bool = True,
                     log: Callable[[str], None] = print) -> dict[str, Any]:
    from .validators import build_context

    try:
        tr: crs.Nad27ToNad83 | None = crs.Nad27ToNad83()
    except crs.CrsError as e:
        tr = None
        log(f"  no NTv2 grid ({e}): datum-shift signatures cannot be computed")

    targets = [f for f in assembled_files() if not files or f in files]
    client = ArcGisClient(cache_dir=PATHS.cache / "arcgis") if fetch_lith else None
    summary: dict[str, Any] = {"files": {}}
    try:
        for file_num in targets:
            doc = read_assembled(file_num)
            ctx = build_context(file_num)
            out = crosscheck_file(file_num, doc, read_positions(file_num), ctx, tr)
            if client is not None:
                names = [m["provincial_name"] for m in out["matches"]
                         if m["dataset"] == "geods" and m.get("provincial_name")]
                raw = fetch_provincial_lith(names, client, log=log)
                lith, lith_values = build_provincial_lith(
                    file_num, raw, dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
                out["provincial_lith"] = lith
                out["provincial_lith_values"] = lith_values
            crosscheck_path(file_num).parent.mkdir(parents=True, exist_ok=True)
            crosscheck_path(file_num).write_text(json.dumps(out, separators=(",", ":"), default=str))
            offsets = sorted(m["offset_m_value"] for m in out["matches"] if m.get("offset_m_value") is not None)
            summary["files"][file_num] = {
                "matches": len(out["matches"]),
                "signatures": sum(1 for m in out["matches"] if m["datum_shift_signature"]),
                "queue": len(out["adjudication_queue"]),
                "median_offset_m": offsets[len(offsets) // 2] if offsets else None,
                "max_offset_m": offsets[-1] if offsets else None,
                "provincial_lith_holes": len(out["provincial_lith"]),
                "provincial_lith_intervals": sum(len(v) for v in out["provincial_lith"].values()),
            }
            s = summary["files"][file_num]
            log(f"  {file_num}: {s['matches']} matches, median offset "
                f"{s['median_offset_m'] if s['median_offset_m'] is not None else 'n/a'} m, "
                f"{s['signatures']} datum-shift signatures, {s['queue']} to adjudicate, "
                f"{s['provincial_lith_intervals']} provincial lithology intervals")
    finally:
        if client is not None:
            client.close()
    return summary


TOOLS = {"pipeline": TOOL, "proj": PROJ_TOOL}
