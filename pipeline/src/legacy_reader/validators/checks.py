"""The twenty checks. Each one compares two things that were measured, and says which two.

Traps these were written against (from the spec's gold-set trap list): feet read as metres, ppm read as
percent, percent U stored as percent U3O8, a probe reading stored as a chemical assay, rows dropped off
the end of a scanned table, a datum the page never printed, swapped easting and northing, `<0.01`
turned into 0, a duplicated hole, and a value that is not inside its own quote.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable

from ..locate import bare, digits_of
from ..normalise import FT_TO_M, PPM_PER_PERCENT, U_TO_U3O8, normalise_hole_name
from . import Finding, finding, register

FEET_RATIO = 1 / FT_TO_M          # 3.2808: a hole logged in feet but stored as metres
FEET_RATIO_TOL = 0.12


def _values(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return doc.get("values", {})


def _meta(doc: dict[str, Any], vid: str | None) -> dict[str, Any]:
    return doc.get("value_meta", {}).get(vid or "", {})


def _val(doc: dict[str, Any], vid: str | None) -> dict[str, Any]:
    return _values(doc).get(vid or "", {})


def _num(doc: dict[str, Any], vid: str | None) -> float | None:
    v = _val(doc, vid).get("value")
    return float(v) if isinstance(v, (int, float)) else None


def _intervals(doc: dict[str, Any]) -> Iterable[tuple[dict[str, Any], str, dict[str, Any]]]:
    for hole in doc.get("holes", []):
        for group in ("lith", "assays"):
            for interval in hole.get(group, []):
                yield hole, group, interval


# ------------------------------------------------------------------ V01 depth units

@register("V01", "Depth unit is printed, and feet were not read as metres", severity="warn")
def v01(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for hole, group, interval in _intervals(doc):
        ids = [i for i in (interval.get("from"), interval.get("to")) if i]
        if not ids:
            continue
        if not interval.get("depth_unit"):
            yield finding("V01", "flag",
                          "no depth unit is printed for this interval: the number is stored as printed and "
                          "carried into metres unchanged",
                          value_ids=ids, scope="interval", scope_id=interval["id"],
                          details={"depth_unit_as_printed": interval.get("depth_unit_as_printed"),
                                   "from": interval.get("from_value"), "to": interval.get("to_value")})
    # feet read as metres: the deepest stored metre value against the province's total depth
    for hole in doc.get("holes", []):
        depths = [i.get("to_m_value") for i in hole.get("assays", []) + hole.get("lith", [])
                  if isinstance(i.get("to_m_value"), (int, float))]
        if not depths:
            continue
        deepest = max(depths)
        prov = _provincial_depth(ctx, hole)
        if prov and prov > 0:
            ratio = deepest / prov
            if abs(ratio - FEET_RATIO) <= FEET_RATIO_TOL:
                ids = [i.get("to") for i in hole.get("assays", []) + hole.get("lith", [])
                       if i.get("to_m_value") == deepest and i.get("to")]
                yield finding("V01", "fail",
                              f"deepest stored depth {deepest:.1f} m is {ratio:.2f} times the province's "
                              f"{prov:.1f} m for this hole: the printed feet were read as metres",
                              value_ids=ids, severity="error", class_a=True, scope="hole",
                              scope_id=hole["hole_id"],
                              details={"deepest_stored_m": round(deepest, 2), "provincial_total_depth_m": prov,
                                       "ratio": round(ratio, 3), "feet_per_metre": round(FEET_RATIO, 4)})


def _provincial_depth(ctx: dict[str, Any], hole: dict[str, Any]) -> float | None:
    key = normalise_hole_name(hole.get("name_as_printed"))
    for source in ("geods_holes", "compilation_holes"):
        for h in ctx.get(source, []):
            if normalise_hole_name(h.get("name")) == key and isinstance(h.get("total_depth_m"), (int, float)):
                return float(h["total_depth_m"])
    return None


# ------------------------------------------------------------------ V02 grade units

@register("V02", "Grade unit is printed and internally consistent", severity="warn")
def v02(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for hole, _group, interval in _intervals(doc):
        grades = interval.get("grades") or []
        for g in grades:
            vid = g["value"]
            v = _val(doc, vid)
            if _meta(doc, vid).get("printed") != "printed":
                continue
            unit = g.get("unit")
            if not unit:
                yield finding("V02", "flag",
                              "no grade unit is printed for this value: it cannot be compared with any other grade",
                              value_ids=[vid], scope="value", scope_id=vid,
                              details={"analyte_as_printed": g.get("analyte_as_printed"),
                                       "unit_as_printed": g.get("unit_as_printed")})
                continue
            value = g.get("grade_value")
            if unit == "%" and isinstance(value, (int, float)) and value > 100:
                yield finding("V02", "fail",
                              f"{v.get('as_printed')} is printed as a percentage but is greater than 100",
                              value_ids=[vid], severity="error", class_a=True, scope="value", scope_id=vid,
                              details={"value": value, "unit": unit})
            if unit == "ppm" and isinstance(value, (int, float)) and value > 1_000_000:
                yield finding("V02", "fail", f"{v.get('as_printed')} ppm is above one million parts per million",
                              value_ids=[vid], severity="error", class_a=True, scope="value", scope_id=vid,
                              details={"value": value, "unit": unit})
        # ppm U against % U3O8 on the same interval, where both are printed
        ppm_u = next((g for g in grades if g.get("unit") == "ppm" and g.get("species") == "U"
                      and isinstance(g.get("grade_value"), (int, float))), None)
        pct_u3o8 = next((g for g in grades if g.get("unit") == "%" and g.get("species") == "U3O8"
                         and isinstance(g.get("grade_value"), (int, float))), None)
        if ppm_u and pct_u3o8:
            as_u3o8 = ppm_u["grade_value"] / PPM_PER_PERCENT * U_TO_U3O8
            as_u = ppm_u["grade_value"] / PPM_PER_PERCENT
            got = pct_u3o8["grade_value"]
            details = {"ppm_u": ppm_u["grade_value"], "pct_u3o8_printed": got,
                       "pct_u3o8_implied": round(as_u3o8, 4), "pct_u_implied": round(as_u, 4),
                       "factor": U_TO_U3O8,
                       "note": "the factor is used only to compare two printed numbers, never to rewrite one"}
            near_u = as_u > 0 and abs(got - as_u) / as_u <= 0.03
            near_u3o8 = as_u3o8 > 0 and abs(got - as_u3o8) / as_u3o8 <= 0.05
            if near_u and not near_u3o8:
                yield finding("V02", "fail",
                              f"the column headed U3O8 % reads {got}, which is {ppm_u['grade_value']} ppm "
                              f"converted as elemental U; as U3O8 it implies {as_u3o8:.4f} % "
                              f"(factor {U_TO_U3O8}): a U grade may be stored as U3O8",
                              value_ids=[ppm_u["value"], pct_u3o8["value"]], severity="error",
                              class_a=True, scope="interval", scope_id=interval["id"], details=details)
            elif not near_u3o8 and as_u3o8 > 0 and abs(got - as_u3o8) / as_u3o8 > 0.25:
                yield finding("V02", "flag",
                              f"printed {ppm_u['grade_value']} ppm U implies about {as_u3o8:.4f} % U3O8 "
                              f"(factor {U_TO_U3O8}), but {got} % U3O8 is printed on the same interval",
                              value_ids=[ppm_u["value"], pct_u3o8["value"]], severity="warn",
                              scope="interval", scope_id=interval["id"], details=details)


# ------------------------------------------------------------------ V03 species

@register("V03", "Grade species is printed, with a quote", severity="warn")
def v03(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for hole, _group, interval in _intervals(doc):
        for g in interval.get("grades") or []:
            vid = g["value"]
            if g.get("species") == "not_printed":
                yield finding("V03", "flag",
                              "the page does not print which species this grade is (U, U3O8, eU3O8): "
                              "it is stored as printed and shown without a species",
                              value_ids=[vid], scope="value", scope_id=vid,
                              details={"analyte_as_printed": g.get("analyte_as_printed"),
                                       "rule": g.get("species_rule")})
            elif not _val(doc, vid).get("lineage", {}).get("quote"):
                yield finding("V03", "flag", f"species {g.get('species')} is recorded with no quote",
                              value_ids=[vid], scope="value", scope_id=vid,
                              details={"species": g.get("species")})


# ------------------------------------------------------------------ V04 basis

@register("V04", "A probe or radiometric reading is never stored as a chemical assay",
          severity="error", class_a=True)
def v04(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    probe_words = re.compile(r"probe|cps|count|gamma|radiometric|equivalent|\beu", re.I)
    for hole, _group, interval in _intervals(doc):
        for g in interval.get("grades") or []:
            vid = g["value"]
            printed = " ".join(str(x) for x in (g.get("analyte_as_printed"), g.get("unit_as_printed"),
                                                g.get("method_as_printed")) if x)
            if g.get("basis") == "chemical" and probe_words.search(printed):
                yield finding("V04", "fail",
                              f"printed as {printed.strip()!r} (a probe or equivalent reading) but stored with "
                              "basis 'chemical'",
                              value_ids=[vid], scope="value", scope_id=vid,
                              details={"printed": printed, "basis": g.get("basis"), "rule": g.get("basis_rule")})
            if g.get("basis") == "probe_equivalent" and g.get("species") in ("U", "U3O8"):
                yield finding("V04", "flag",
                              f"this is a probe or equivalent reading but the species is stored as "
                              f"{g.get('species')} rather than e{g.get('species')}",
                              value_ids=[vid], severity="warn", class_a=False, scope="value", scope_id=vid,
                              details={"species": g.get("species"), "basis": g.get("basis"),
                                       "analyte_as_printed": g.get("analyte_as_printed")})


# ------------------------------------------------------------------ V05 from < to

@register("V05", "Interval starts above where it ends", severity="error", class_a=False)
def v05(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for hole, _group, interval in _intervals(doc):
        a, b = interval.get("from_value"), interval.get("to_value")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        if a > b:
            yield finding("V05", "fail", f"interval runs from {a} to {b}: the start is below the end",
                          value_ids=[i for i in (interval.get("from"), interval.get("to")) if i],
                          scope="interval", scope_id=interval["id"], details={"from": a, "to": b})
        elif a == b:
            yield finding("V05", "flag", f"interval has zero length ({a} to {b})",
                          severity="warn",
                          value_ids=[i for i in (interval.get("from"), interval.get("to")) if i],
                          scope="interval", scope_id=interval["id"], details={"from": a, "to": b})


# ------------------------------------------------------------------ V06 monotonic and overlaps

@register("V06", "Depths run down the hole without unexplained overlaps", severity="warn")
def v06(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for hole in doc.get("holes", []):
        for group in ("lith", "assays"):
            rows = [i for i in hole.get(group, [])
                    if isinstance(i.get("from_m_value"), (int, float)) and isinstance(i.get("to_m_value"), (int, float))]
            rows = sorted(rows, key=lambda i: (i["from_m_value"], i["to_m_value"]))
            for prev, nxt in zip(rows, rows[1:], strict=False):
                if nxt["from_m_value"] < prev["to_m_value"] - 1e-6:
                    nested = nxt["to_m_value"] <= prev["to_m_value"] + 1e-6
                    if nested:
                        continue  # an "including" interval inside a wider one is normal practice
                    yield finding("V06", "flag",
                                  f"{group} interval {nxt['from_m_value']:.2f}-{nxt['to_m_value']:.2f} m overlaps "
                                  f"the previous {prev['from_m_value']:.2f}-{prev['to_m_value']:.2f} m",
                                  value_ids=[i for i in (nxt.get("from"), prev.get("to")) if i],
                                  scope="interval", scope_id=nxt["id"],
                                  details={"previous": [prev["from_m_value"], prev["to_m_value"]],
                                           "this": [nxt["from_m_value"], nxt["to_m_value"]], "group": group})


# ------------------------------------------------------------------ V07 printed length

@register("V07", "Printed interval length equals to minus from", severity="warn")
def v07(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for hole, _group, interval in _intervals(doc):
        width_vid = interval.get("width")
        if not width_vid:
            continue
        width = _num(doc, width_vid)
        a, b = interval.get("from_value"), interval.get("to_value")
        if width is None or not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        expected = b - a
        tol = max(0.05, abs(expected) * 0.01)
        if abs(width - expected) > tol:
            yield finding("V07", "flag",
                          f"printed width {width} does not equal the printed interval {a} to {b} ({expected})",
                          value_ids=[width_vid], scope="interval", scope_id=interval["id"],
                          details={"width_printed": width, "to_minus_from": round(expected, 4),
                                   "tolerance": round(tol, 4)})


# ------------------------------------------------------------------ V08 within total depth

@register("V08", "No interval runs past the hole's total depth", severity="error", class_a=False)
def v08(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for hole in doc.get("holes", []):
        rows = hole.get("lith", []) + hole.get("assays", [])
        depths = [(i.get("to_m_value"), i) for i in rows if isinstance(i.get("to_m_value"), (int, float))]
        if not depths:
            continue
        deepest, interval = max(depths, key=lambda t: t[0])
        td_vid = hole.get("collar", {}).get("total_depth")
        printed_td = _num(doc, td_vid)
        unit = _meta(doc, td_vid).get("unit_norm")
        td_m = printed_td * FT_TO_M if (printed_td is not None and unit == "ft") else printed_td
        source = "printed on the page"
        if td_m is None:
            td_m = _provincial_depth(ctx, hole)
            source = "the provincial record for this hole"
        if td_m is None or td_m <= 0:
            continue
        if deepest > td_m * 1.02 + 0.5:
            yield finding("V08", "fail",
                          f"deepest interval ends at {deepest:.1f} m but the total depth is {td_m:.1f} m "
                          f"({source})",
                          value_ids=[i for i in (interval.get("to"), td_vid) if i],
                          scope="hole", scope_id=hole["hole_id"],
                          details={"deepest_m": round(deepest, 2), "total_depth_m": round(td_m, 2),
                                   "total_depth_source": source})


# ------------------------------------------------------------------ V09 row count

@register("V09", "Rows stored, rows the model counted and rows the page geometry shows all agree",
          severity="error", class_a=True)
def v09(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    geometric = ctx.get("geometric_rows") or {}
    for table in doc.get("tables", []):
        stored = int(table.get("rows_stored") or 0)
        counted = int(table.get("rows_printed") or 0)
        geo = geometric.get(table["table_id"])
        counters = {"stored": stored, "model_count": counted}
        if geo is not None:
            counters["geometric"] = geo
        deficits = []
        if counted > stored:
            deficits.append("model_count")
        if geo is not None and geo > stored + 1:
            deficits.append("geometric")
        if table.get("truncated"):
            deficits.append("truncated_flag")
        value_ids = [vid for vid, m in doc.get("value_meta", {}).items() if m.get("table_id") == table["table_id"]]
        if len(deficits) >= 2:
            yield finding("V09", "fail",
                          f"{stored} rows stored but {counted} printed rows were counted"
                          + (f" and the page geometry shows {geo}" if geo is not None else "")
                          + ": rows are missing from this table",
                          value_ids=value_ids[:200], scope="table", scope_id=table["table_id"],
                          details={**counters, "agreed_by": deficits})
        elif deficits:
            yield finding("V09", "flag",
                          f"{stored} rows stored, {counted} counted"
                          + (f", {geo} from the page geometry" if geo is not None else "")
                          + ": one counter disagrees",
                          severity="warn", class_a=False, value_ids=value_ids[:200],
                          scope="table", scope_id=table["table_id"],
                          details={**counters, "disagreed": deficits})


# ------------------------------------------------------------------ V10 datum stated

@register("V10", "A collar position is never given a datum the page does not print",
          severity="error", class_a=True)
def v10(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    positions = {p["hole_id"]: p for p in (ctx.get("positions") or {}).get("holes", [])}
    for hole in doc.get("holes", []):
        collar = hole.get("collar", {})
        datum_vid = collar.get("datum")
        has_coords = any(collar.get(k) for k in ("easting", "northing", "latitude", "longitude"))
        pos = positions.get(hole["hole_id"], {})
        if datum_vid:
            lin = _val(doc, datum_vid).get("lineage", {})
            if not lin.get("quote_located"):
                yield finding("V10", "flag",
                              f"the datum {_val(doc, datum_vid).get('as_printed')!r} is recorded but its quote "
                              "could not be located on the page",
                              severity="warn", class_a=False, value_ids=[datum_vid],
                              scope="hole", scope_id=hole["hole_id"],
                              details={"quote": lin.get("quote"), "locate": _meta(doc, datum_vid).get("locate")})
            continue
        if has_coords:
            yield finding("V10", "flag",
                          "coordinates are printed for this hole but no datum is printed anywhere on the page: "
                          "the position is not transformed and both candidate positions are kept",
                          severity="warn", class_a=False,
                          value_ids=[v for v in (collar.get("easting"), collar.get("northing"),
                                                 collar.get("latitude"), collar.get("longitude")) if v],
                          scope="hole", scope_id=hole["hole_id"],
                          details={"position_source": pos.get("position_source"), "status": pos.get("status")})
        if pos.get("status") == "transformed" and not datum_vid:
            yield finding("V10", "fail",
                          "a datum was applied to this collar although the page prints none",
                          value_ids=[v for v in collar.values() if v], scope="hole", scope_id=hole["hole_id"],
                          details={"position": pos})


# ------------------------------------------------------------------ V11 inside the file's sheets

@register("V11", "A transformed collar lands inside the file's NTS sheets", severity="error", class_a=False)
def v11(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for pos in (ctx.get("positions") or {}).get("holes", []):
        if pos.get("lonlat") is None:
            continue
        checks = pos.get("checks") or {}
        if checks.get("inside_nts") is False:
            yield finding("V11", "fail",
                          f"transformed collar {pos['lonlat'][1]:.4f} N {abs(pos['lonlat'][0]):.4f} W falls "
                          f"outside the file's NTS sheets {checks.get('nts_sheets')}",
                          value_ids=pos.get("value_ids") or [], scope="hole", scope_id=pos["hole_id"],
                          details=checks)
        elif checks.get("inside_file_polygon") is False and checks.get("inside_nts"):
            yield finding("V11", "flag",
                          f"transformed collar is {checks.get('distance_to_file_holes_km')} km from the "
                          "provincial holes linked to this file (2 km buffer)",
                          severity="warn", value_ids=pos.get("value_ids") or [], scope="hole",
                          scope_id=pos["hole_id"], details=checks)


# ------------------------------------------------------------------ V12 coordinate plausibility

@register("V12", "Printed coordinates are plausible and not swapped", severity="error", class_a=False)
def v12(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for hole in doc.get("holes", []):
        collar = hole.get("collar", {})
        e, n = _num(doc, collar.get("easting")), _num(doc, collar.get("northing"))
        if e is not None and n is not None:
            if 150_000 <= n <= 850_000 and 5_400_000 <= e <= 6_700_000:
                yield finding("V12", "fail",
                              f"easting {e:.0f} and northing {n:.0f} look swapped: the northing is in the "
                              "easting's range and the easting in the northing's",
                              value_ids=[collar["easting"], collar["northing"]],
                              scope="hole", scope_id=hole["hole_id"], details={"easting": e, "northing": n})
            elif not (150_000 <= e <= 850_000 and 5_400_000 <= n <= 6_700_000):
                yield finding("V12", "flag",
                              f"easting {e:.0f} and northing {n:.0f} are outside the plausible UTM range for "
                              "Saskatchewan: treated as a local grid, not transformed",
                              severity="warn", value_ids=[collar["easting"], collar["northing"]],
                              scope="hole", scope_id=hole["hole_id"], details={"easting": e, "northing": n})
        lat, lon = _num(doc, collar.get("latitude")), _num(doc, collar.get("longitude"))
        if lat is not None and not (49.0 <= lat <= 60.5):
            yield finding("V12", "flag", f"latitude {lat} is outside Saskatchewan",
                          severity="warn", value_ids=[collar["latitude"]], scope="hole",
                          scope_id=hole["hole_id"], details={"lat": lat})
        if lon is not None and not (-111.0 <= lon <= -100.0 or 100.0 <= lon <= 111.0):
            yield finding("V12", "flag", f"longitude {lon} is outside Saskatchewan",
                          severity="warn", value_ids=[collar["longitude"]], scope="hole",
                          scope_id=hole["hole_id"], details={"lon": lon})


# ------------------------------------------------------------------ V13 text-layer disagreement

@register("V13", "The PDF's own text layer agrees with the transcribed digits", severity="warn")
def v13(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    layers = ctx.get("text_layer") or {}
    if not layers:
        return
    for vid, meta in doc.get("value_meta", {}).items():
        if meta.get("printed") != "printed":
            continue
        box = (meta.get("locate") or {}).get("bbox")
        page = meta.get("page")
        as_printed = _val(doc, vid).get("as_printed")
        want = digits_of(as_printed)
        if not box or not want or page is None:
            continue
        # Only a numeric cell can be compared token for token. A paragraph of description overlaps
        # whatever the text layer happens to put in that rectangle, which is noise, not disagreement.
        if len(want) < 0.5 * len(bare(as_printed)) or (box[2] - box[0]) > 0.25 or (box[3] - box[1]) > 0.04:
            continue
        words = layers.get((meta.get("pdf_sha256"), page)) or layers.get(page) or []
        overlapping = [w for w in words if _overlap(box, w) > 0.35 and digits_of(w["text"])]
        if not overlapping:
            continue
        got = "".join(digits_of(w["text"]) for w in sorted(overlapping, key=lambda w: w["x0"]))
        if want not in got and got not in want:
            yield finding("V13", "flag",
                          f"the PDF text layer reads {got!r} where the model transcribed {as_printed!r}",
                          value_ids=[vid], scope="value", scope_id=vid,
                          details={"text_layer": got, "transcribed": as_printed,
                                   "words": [w["text"] for w in overlapping][:6]})


def _overlap(box: list[float], word: dict[str, Any]) -> float:
    x0 = max(box[0], word["x0"])
    y0 = max(box[1], word["y0"])
    x1 = min(box[2], word["x1"])
    y1 = min(box[3], word["y1"])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area = max(1e-9, (word["x1"] - word["x0"]) * (word["y1"] - word["y0"]))
    return inter / area


# ------------------------------------------------------------------ V14 duplicate holes

@register("V14", "The same hole is not stored twice with different numbers", severity="warn")
def v14(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    seen: dict[str, list[dict[str, Any]]] = {}
    for hole in doc.get("holes", []):
        seen.setdefault(hole["hole_id"], []).append(hole)
    for key, group in seen.items():
        if len(group) > 1:
            yield finding("V14", "flag", f"hole {key} appears {len(group)} times in this file",
                          value_ids=[h.get("name_vid") for h in group if h.get("name_vid")],
                          scope="hole", scope_id=key,
                          details={"names_as_printed": [h.get("name_as_printed") for h in group]})
    # the same hole logged on two pages with conflicting collar numbers
    for hole in doc.get("holes", []):
        for fieldname in ("total_depth", "dip", "azimuth", "easting", "northing"):
            vids = [vid for vid, m in doc.get("value_meta", {}).items()
                    if m.get("hole_id") == hole["hole_id"] and m.get("field") == fieldname
                    and m.get("printed") == "printed"]
            numbers = {round(float(_val(doc, v)["value"]), 3) for v in vids
                       if isinstance(_val(doc, v).get("value"), (int, float))}
            if len(numbers) > 1:
                yield finding("V14", "flag",
                              f"hole {hole['hole_id']} has {len(numbers)} different printed values for "
                              f"{fieldname}: {sorted(numbers)}",
                              value_ids=vids, scope="hole", scope_id=hole["hole_id"],
                              details={"field": fieldname, "values": sorted(numbers)})


# ------------------------------------------------------------------ V15 value inside its own quote

@register("V15", "The printed value appears inside its own quote", severity="error", class_a=True)
def v15(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for vid, meta in doc.get("value_meta", {}).items():
        if meta.get("printed") != "printed":
            continue
        v = _val(doc, vid)
        as_printed = v.get("as_printed")
        quote = (v.get("lineage") or {}).get("quote")
        if not as_printed or not quote:
            continue
        want, have = bare(as_printed), bare(quote)
        if not want or want in have:
            continue
        # A long transcription whose quote is a shortened version of itself is a different problem from
        # a number that is simply not in the text it claims to come from. The model shortens two ways:
        # it stops early, and it elides the middle with an ellipsis.
        elided = "..." in str(quote) or "\u2026" in str(quote)
        if elided:
            head, _, tail = str(quote).replace("\u2026", "...").partition("...")
            head_ok = not bare(head) or bare(head) in want
            tail_ok = not bare(tail) or bare(tail) in want
            if head_ok and tail_ok:
                yield finding("V15", "flag",
                              "the quote for this value elides its middle with an ellipsis: the head and "
                              "tail match the transcription but the rest cannot be checked against it",
                              severity="warn", class_a=False, value_ids=[vid], scope="value", scope_id=vid,
                              details={"value_chars": len(want), "quote_chars": len(have),
                                       "head": head[:80], "tail": tail[-80:]})
                continue
        overlap = SequenceMatcher(None, want, have).find_longest_match(0, len(want), 0, len(have)).size
        if len(want) > 60 and overlap >= 0.8 * min(len(want), len(have)):
            yield finding("V15", "flag",
                          f"the quote for this value is shorter than the value itself "
                          f"({len(have)} characters against {len(want)}): the transcription cannot be "
                          "checked against it in full",
                          severity="warn", class_a=False, value_ids=[vid], scope="value", scope_id=vid,
                          details={"value_chars": len(want), "quote_chars": len(have),
                                   "longest_common_run": overlap})
            continue
        yield finding("V15", "fail",
                      f"the value {as_printed!r} does not appear in its own quote {quote[:120]!r}",
                      value_ids=[vid], scope="value", scope_id=vid,
                      details={"as_printed": as_printed, "quote": quote})


# ------------------------------------------------------------------ V16 OCR digits

@register("V16", "The second reader's digits match the transcribed digits", severity="warn")
def v16(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for vid, meta in doc.get("value_meta", {}).items():
        if meta.get("printed") != "printed":
            continue
        agreement = meta.get("digit_agreement")
        locate = meta.get("locate") or {}
        as_printed = _val(doc, vid).get("as_printed")
        if not digits_of(as_printed):
            continue
        if locate.get("locate_method") not in ("exact", "fuzzy", "row_anchor"):
            continue  # nothing was matched: that is a locator failure, not a disagreement about digits
        if agreement == "mismatch":
            yield finding("V16", "flag",
                          f"OCR reads {locate.get('locate_ocr_text')!r} where the model transcribed "
                          f"{as_printed!r}: the two readers disagree, check by eye",
                          value_ids=[vid], class_a=True, severity="warn", scope="value", scope_id=vid,
                          details={"transcribed": as_printed, "ocr": locate.get("locate_ocr_text"),
                                   "locate_method": locate.get("locate_method")})
        elif agreement == "confusable":
            yield finding("V16", "flag",
                          f"OCR reads {locate.get('locate_ocr_text')!r}: it matches {as_printed!r} only after "
                          "folding confusable characters",
                          severity="info", class_a=False, value_ids=[vid], scope="value", scope_id=vid,
                          details={"transcribed": as_printed, "ocr": locate.get("locate_ocr_text")})


# ------------------------------------------------------------------ V17 below-detection qualifiers

@register("V17", "Below-detection qualifiers are preserved, never turned into a number",
          severity="error", class_a=True)
def v17(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for vid, meta in doc.get("value_meta", {}).items():
        v = _val(doc, vid)
        as_printed = v.get("as_printed")
        if not as_printed:
            continue
        printed = str(as_printed).strip()
        qualifier = meta.get("qualifier")
        stored = v.get("value")
        if printed.startswith(("<", ">")) and not qualifier:
            yield finding("V17", "fail",
                          f"{printed!r} carries a detection-limit qualifier but none was recorded",
                          value_ids=[vid], scope="value", scope_id=vid,
                          details={"as_printed": printed, "stored": stored})
        elif qualifier and isinstance(stored, (int, float)) and stored == 0:
            yield finding("V17", "fail",
                          f"{printed!r} was stored as 0: a below-detection result is not zero",
                          value_ids=[vid], scope="value", scope_id=vid,
                          details={"as_printed": printed, "stored": stored, "qualifier": qualifier})
        elif meta.get("non_numeric") and isinstance(stored, (int, float)):
            yield finding("V17", "fail",
                          f"{printed!r} is not a number but was stored as {stored}",
                          value_ids=[vid], scope="value", scope_id=vid,
                          details={"as_printed": printed, "stored": stored})


# ------------------------------------------------------------------ V18 dip and azimuth

@register("V18", "Dip and azimuth are inside their ranges", severity="warn")
def v18(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for hole in doc.get("holes", []):
        collar = hole.get("collar", {})
        dip = _num(doc, collar.get("dip"))
        if dip is not None and not (-90.0 - 1e-6 <= dip <= 90.0 + 1e-6):
            yield finding("V18", "flag", f"dip {dip} is outside -90 to 90 degrees",
                          value_ids=[collar["dip"]], scope="hole", scope_id=hole["hole_id"],
                          details={"dip": dip})
        az = _num(doc, collar.get("azimuth"))
        if az is not None and not (0.0 <= az <= 360.0):
            yield finding("V18", "flag", f"azimuth {az} is outside 0 to 360 degrees",
                          value_ids=[collar["azimuth"]], scope="hole", scope_id=hole["hole_id"],
                          details={"azimuth": az})


# ------------------------------------------------------------------ V19 carried hole id

@register("V19", "A hole identifier taken from a header or a previous page is marked as carried",
          severity="info")
def v19(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    for hole in doc.get("holes", []):
        source = hole.get("hole_id_source")
        if source in ("page_header", "carried"):
            where = "the page header" if source == "page_header" else "the previous page of this table"
            yield finding("V19", "flag",
                          f"rows of this hole carry no hole identifier of their own: it was taken from {where}",
                          severity="info", value_ids=[hole["name_vid"]] if hole.get("name_vid") else [],
                          scope="hole", scope_id=hole["hole_id"],
                          details={"hole_id_source": source, "pages": hole.get("pages")})


# ------------------------------------------------------------------ V20 hole count

@register("V20", "The number of holes read matches the work description and the province", severity="warn")
def v20(doc: dict[str, Any], ctx: dict[str, Any]) -> Iterable[Finding]:
    extracted = len(doc.get("holes", []))
    work = ctx.get("work") or {}
    expected_work = work.get("hole_count")
    provincial = len({normalise_hole_name(h["name"]) for h in ctx.get("geods_holes", []) if h.get("name")})
    names_extracted = {normalise_hole_name(h.get("name_as_printed")) for h in doc.get("holes", [])}
    missing = sorted({normalise_hole_name(h["name"]) for h in ctx.get("geods_holes", []) if h.get("name")}
                     - names_extracted)
    if isinstance(expected_work, int) and expected_work > extracted:
        yield finding("V20", "flag",
                      f"the file's work description lists {expected_work} drillholes but {extracted} were read "
                      "from the pages called",
                      scope="file", scope_id=doc["file_num"],
                      details={"work_hole_count": expected_work, "extracted": extracted,
                               "work_hole_names": work.get("hole_names"),
                               "note": "only a subset of pages was called in this run"})
    if provincial and missing:
        yield finding("V20", "flag",
                      f"the province links {provincial} holes to this file; {len(missing)} of them have no "
                      "extracted collar",
                      scope="file", scope_id=doc["file_num"],
                      details={"provincial_holes": provincial, "extracted": extracted,
                               "missing_names": missing[:20]})


def geometric_row_counts(doc: dict[str, Any]) -> dict[str, int]:
    """Third row counter: printed bands with at least two numeric tokens inside each table's box."""
    from ..assemble import _page_locators

    counts: dict[str, int] = {}
    locators: dict[str, dict[int, Any]] = {}
    number = re.compile(r"^[<>~]?\(?-?\d{1,4}(?:[,.]\d+)*\)?%?$")
    for table in doc.get("tables", []):
        box = table.get("bbox")
        if not box:
            continue
        sha = table["pdf_sha256"]
        if sha not in locators:
            locators[sha] = _page_locators(sha)
        loc = locators[sha].get(int(table["page"]))
        if loc is None:
            continue
        n = 0
        for band in loc.bands:
            yc = sum((w["y0"] + w["y1"]) / 2 for w in band) / len(band)
            if not (box[1] - 0.005 <= yc <= box[3] + 0.005):
                continue
            numeric = sum(1 for w in band if number.match((w["text"] or "").strip()))
            if numeric >= 2:
                n += 1
        counts[table["table_id"]] = n
    return counts
