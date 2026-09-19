"""A Python mirror of `web/src/data/contract.ts`, used as the export gate.

The web repo's `npm run validate:data` is the other half; this one runs inside the pipeline so an export
can never be written in a shape the app would reject. Beyond the shapes it enforces the rule the zod
schema only implies: **every ValueId that appears anywhere resolves to a Val in a registry the same file
carries**, so no number reaches a screen without a quote, a derivation or a source.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0.0"
VALUE_ID = re.compile(r"^(x|d|p|s|m|e|c|g|h):[^\s]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BANNED_URL_FRAGMENT = "Mining/MapServer"

STATUS = {"pass", "flag", "miss", "corrected"}
HOLE_STATUS = {"pass", "flag", "miss"}
KINDS = {"extracted", "derived", "source", "stat", "metric"}
FMTS = {"int", "year", "m1", "m2", "deg1", "deg5", "pct1", "ratio3", "text"}
UNIT_SOURCES = {"cell", "column_header", "table_title", "page_note", "prev_page_header", "not_printed"}
POSITION_SOURCES = {"extracted_transformed", "extracted_nad83", "provincial_geods", "provincial_compilation"}
STUB_POSITION_SOURCES = POSITION_SOURCES | {"none"}
DATUM_BASIS = {"printed", "none", "local_grid"}
COORD_KINDS = {"utm", "geographic", "local_grid", "not_printed"}
TABLE_KINDS = {"collar", "assay", "lith", "probe"}
PAGE_KINDS = {"collar_table", "lith_log", "assay_table", "probe_log", "certificate", "other"}
ERAS = {"1970s", "1980s-1990s", "2000s+"}
SPECIES = {"U", "U3O8", "eU", "eU3O8", "other", "not_printed"}
BASES = {"chemical", "probe_equivalent", "not_printed"}
NAME_MATCHES = {"exact", "normalised", "fuzzy", "nearest"}
ADJUDICATIONS = {"none", "needed", "done"}
DATASETS = {"compilation", "geods", "geods_lith", "file_index", "deposits", "assessment_info"}


class Errors(list):
    def add(self, where: str, message: str) -> None:
        self.append(f"{where}: {message}")


def _enum(e: Errors, where: str, value: Any, allowed: set[str]) -> None:
    if value not in allowed:
        e.add(where, f"{value!r} is not one of {sorted(allowed)}")


def check_bbox(e: Errors, where: str, box: Any) -> None:
    if box is None:
        return
    if not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) for v in box):
        e.add(where, f"bbox must be four numbers, got {box!r}")
        return
    x0, y0, x1, y1 = box
    if not (0 <= x0 <= 1 and 0 <= y0 <= 1 and 0 <= x1 <= 1 and 0 <= y1 <= 1 and x1 >= x0 and y1 >= y0):
        e.add(where, f"bbox is not a normalised 0..1 top-left box: {box!r}")


def check_lonlat(e: Errors, where: str, ll: Any) -> None:
    if not isinstance(ll, list) or len(ll) != 2:
        e.add(where, f"lonlat must be [lon, lat], got {ll!r}")
        return
    lon, lat = ll
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        e.add(where, f"lonlat out of range: {ll!r}")


def check_val(e: Errors, where: str, val: Any) -> None:
    if not isinstance(val, dict):
        e.add(where, "value is not an object")
        return
    vid = val.get("id")
    if not isinstance(vid, str) or not VALUE_ID.match(vid):
        e.add(where, f"id {vid!r} is not a namespaced value id")
    _enum(e, f"{where}.kind", val.get("kind"), KINDS)
    for key in ("as_printed", "value", "unit_as_printed"):
        if key not in val:
            e.add(where, f"missing {key}")
    if val.get("status") is not None and val.get("status") not in STATUS:
        e.add(f"{where}.status", f"{val.get('status')!r} is not a status")
    if val.get("fmt") is not None and val["fmt"] not in FMTS:
        e.add(f"{where}.fmt", f"{val['fmt']!r} is not a formatter")
    kind = val.get("kind")
    if kind == "extracted" and not val.get("lineage"):
        e.add(where, "extracted value has no lineage")
    if kind == "derived" and not val.get("derivation"):
        e.add(where, "derived value has no derivation")
    if kind == "source" and not val.get("source"):
        e.add(where, "source value has no source reference")
    if kind != "extracted" and not val.get("fmt"):
        e.add(where, f"{kind} value needs fmt")
    lineage = val.get("lineage")
    if lineage is not None:
        check_lineage(e, f"{where}.lineage", lineage)
    src = val.get("source")
    if src is not None:
        _enum(e, f"{where}.source.dataset", src.get("dataset"), DATASETS)
        for key in ("record_id", "field", "retrieved_at"):
            if key not in src:
                e.add(f"{where}.source", f"missing {key}")
    der = val.get("derivation")
    if der is not None:
        if not isinstance(der.get("inputs"), list):
            e.add(f"{where}.derivation", "inputs must be a list")
        if not isinstance(der.get("op"), str) or not isinstance(der.get("tool"), str):
            e.add(f"{where}.derivation", "op and tool must be strings")


def check_lineage(e: Errors, where: str, lin: Any) -> None:
    if not isinstance(lin, dict):
        e.add(where, "lineage is not an object")
        return
    for key in ("file_num", "file_sha256", "page", "bbox", "quote", "quote_located", "model",
                "prompt_version", "run_id", "extracted_at", "validators"):
        if key not in lin:
            e.add(where, f"missing {key}")
    if not SHA256.match(str(lin.get("file_sha256", ""))):
        e.add(where, f"file_sha256 {lin.get('file_sha256')!r} is not 64 hex characters")
    if not isinstance(lin.get("page"), int) or lin["page"] < 1:
        e.add(where, f"page {lin.get('page')!r} must be a 1-based integer")
    check_bbox(e, f"{where}.bbox", lin.get("bbox"))
    if not isinstance(lin.get("quote_located"), bool):
        e.add(where, "quote_located must be a boolean")
    if lin.get("unit_source") is not None and lin["unit_source"] not in UNIT_SOURCES:
        e.add(f"{where}.unit_source", f"{lin['unit_source']!r} is not a unit source")
    for i, v in enumerate(lin.get("validators") or []):
        if v.get("outcome") not in {"pass", "flag", "fail", "na"}:
            e.add(f"{where}.validators[{i}]", f"outcome {v.get('outcome')!r} is invalid")
        if v.get("severity") is not None and v["severity"] not in {"info", "warn", "error"}:
            e.add(f"{where}.validators[{i}]", f"severity {v['severity']!r} is invalid")


def check_registry(e: Errors, where: str, reg: Any) -> set[str]:
    if not isinstance(reg, dict):
        e.add(where, "values registry is not an object")
        return set()
    for key, val in reg.items():
        if not isinstance(val, dict) or key != val.get("id"):
            e.add(f"{where}[{key}]", f"registry key does not match value id {val.get('id') if isinstance(val, dict) else val!r}")
        check_val(e, f"{where}[{key}]", val)
    return set(reg)


def _ref(e: Errors, where: str, vid: Any, known: set[str], nullable: bool = False) -> None:
    if vid is None:
        if not nullable:
            e.add(where, "value id is required but null")
        return
    if not isinstance(vid, str) or not VALUE_ID.match(vid):
        e.add(where, f"{vid!r} is not a namespaced value id")
        return
    if vid not in known:
        e.add(where, f"value id {vid} does not resolve to a stored value (unbacked number)")


def check_interval(e: Errors, where: str, interval: Any, known: set[str], assay: bool) -> None:
    for key in ("from", "to", "from_m", "to_m"):
        _ref(e, f"{where}.{key}", interval.get(key), known)
    if assay:
        _ref(e, f"{where}.sample_id", interval.get("sample_id"), known, nullable=True)
        for i, g in enumerate(interval.get("grades") or []):
            _ref(e, f"{where}.grades[{i}].value", g.get("value"), known)
            _enum(e, f"{where}.grades[{i}].species", g.get("species"), SPECIES)
            _enum(e, f"{where}.grades[{i}].basis", g.get("basis"), BASES)
            if "analyte_as_printed" not in g or "method_as_printed" not in g:
                e.add(f"{where}.grades[{i}]", "missing analyte_as_printed or method_as_printed")
    else:
        _ref(e, f"{where}.code", interval.get("code"), known, nullable=True)
        _ref(e, f"{where}.description", interval.get("description"), known, nullable=True)
    _enum(e, f"{where}.status", interval.get("status"), HOLE_STATUS)
    if "depth_unit_as_printed" not in interval:
        e.add(where, "missing depth_unit_as_printed")


def check_summary(e: Errors, where: str, s: Any, known: set[str]) -> None:
    for key in ("file_num", "company", "era", "year", "nts_sheets", "page_count", "scan_kind",
                "file_sha256", "source_url", "split", "status_counts", "footprint", "centroid", "holes"):
        if key not in s:
            e.add(where, f"missing {key}")
    _enum(e, f"{where}.era", s.get("era"), ERAS)
    _enum(e, f"{where}.scan_kind", s.get("scan_kind"), {"scanned", "text", "mixed"})
    _enum(e, f"{where}.split", s.get("split"), {"dev", "heldout"})
    if not SHA256.match(str(s.get("file_sha256", ""))):
        e.add(where, "file_sha256 is not 64 hex characters")
    if not str(s.get("source_url", "")).startswith(("http://", "https://")):
        e.add(where, f"source_url {s.get('source_url')!r} is not a URL")
    _ref(e, f"{where}.year", s.get("year"), known)
    _ref(e, f"{where}.page_count", s.get("page_count"), known)
    for key in ("pass", "flag", "miss"):
        _ref(e, f"{where}.status_counts.{key}", (s.get("status_counts") or {}).get(key), known)
    fp = s.get("footprint") or {}
    if fp.get("type") != "Polygon" or not isinstance(fp.get("coordinates"), list):
        e.add(f"{where}.footprint", "footprint must be a GeoJSON Polygon")
    else:
        for ring in fp["coordinates"]:
            for pt in ring:
                check_lonlat(e, f"{where}.footprint", pt)
    check_lonlat(e, f"{where}.centroid", s.get("centroid"))
    for i, stub in enumerate(s.get("holes") or []):
        _enum(e, f"{where}.holes[{i}].status", stub.get("status"), HOLE_STATUS)
        _enum(e, f"{where}.holes[{i}].position_source", stub.get("position_source"), STUB_POSITION_SOURCES)
        _enum(e, f"{where}.holes[{i}].datum_basis", stub.get("datum_basis"), DATUM_BASIS)
        if stub.get("lonlat") is not None:
            check_lonlat(e, f"{where}.holes[{i}].lonlat", stub["lonlat"])


def check_report(doc: Any, path: str = "report.json") -> Errors:
    e = Errors()
    if not isinstance(doc, dict):
        e.add(path, "report is not an object")
        return e
    known = check_registry(e, f"{path}.values", doc.get("values"))
    check_summary(e, f"{path}.summary", doc.get("summary") or {}, known)
    for i, t in enumerate(doc.get("tables") or []):
        w = f"{path}.tables[{i}]"
        _enum(e, f"{w}.kind", t.get("kind"), TABLE_KINDS)
        if not isinstance(t.get("page"), int) or t["page"] < 1:
            e.add(w, "page must be a 1-based integer")
        check_bbox(e, f"{w}.bbox", t.get("bbox"))
        _ref(e, f"{w}.rows_stored", t.get("rows_stored"), known)
        _ref(e, f"{w}.rows_printed", t.get("rows_printed"), known, nullable=True)
        if "continues_from" not in t:
            e.add(w, "missing continues_from")
    for i, h in enumerate(doc.get("holes") or []):
        w = f"{path}.holes[{i}]"
        _ref(e, f"{w}.name", h.get("name"), known)
        _enum(e, f"{w}.status", h.get("status"), HOLE_STATUS)
        collar = h.get("collar") or {}
        _enum(e, f"{w}.collar.coord_kind", collar.get("coord_kind"), COORD_KINDS)
        for key in ("easting", "northing", "lat", "lon", "grid_x", "grid_y", "utm_zone", "datum_printed",
                    "elevation", "azimuth", "dip", "total_depth"):
            if key not in collar:
                e.add(f"{w}.collar", f"missing {key}")
            else:
                _ref(e, f"{w}.collar.{key}", collar[key], known, nullable=True)
        pos = h.get("position")
        if pos is not None:
            check_lonlat(e, f"{w}.position.lonlat", pos.get("lonlat"))
            _enum(e, f"{w}.position.source", pos.get("source"), POSITION_SOURCES)
            _ref(e, f"{w}.position.lon", pos.get("lon"), known)
            _ref(e, f"{w}.position.lat", pos.get("lat"), known)
            for key in ("misread_lonlat", "alt_lonlat"):
                if key not in pos:
                    e.add(f"{w}.position", f"missing {key}")
                elif pos[key] is not None:
                    check_lonlat(e, f"{w}.position.{key}", pos[key])
            tr = pos.get("transform")
            if tr is not None:
                for key in ("name", "code", "pipeline", "grid_file", "grid_sha256", "from", "to",
                            "shift_m", "bearing_deg", "accuracy_m"):
                    if key not in tr:
                        e.add(f"{w}.position.transform", f"missing {key}")
                _ref(e, f"{w}.position.transform.shift_m", tr.get("shift_m"), known)
                _ref(e, f"{w}.position.transform.bearing_deg", tr.get("bearing_deg"), known)
        for j, m in enumerate(h.get("matches") or []):
            mw = f"{w}.matches[{j}]"
            _enum(e, f"{mw}.dataset", m.get("dataset"), {"geods", "compilation"})
            _enum(e, f"{mw}.name_match", m.get("name_match"), NAME_MATCHES)
            _enum(e, f"{mw}.adjudication", m.get("adjudication"), ADJUDICATIONS)
            _ref(e, f"{mw}.offset_m", m.get("offset_m"), known)
            _ref(e, f"{mw}.bearing_deg", m.get("bearing_deg"), known)
            check_lonlat(e, f"{mw}.lonlat", m.get("lonlat"))
            if not isinstance(m.get("feature_id"), int):
                e.add(mw, "feature_id must be an integer")
            if not isinstance(m.get("datum_shift_signature"), bool):
                e.add(mw, "datum_shift_signature must be a boolean")
        for group, assay in (("provincial_lith", False), ("lith", False), ("assays", True)):
            for j, interval in enumerate(h.get(group) or []):
                check_interval(e, f"{w}.{group}[{j}]", interval, known, assay)
    return e


def check_report_index(doc: Any, path: str = "reports/index.json") -> Errors:
    e = Errors()
    known = check_registry(e, f"{path}.values", doc.get("values"))
    for i, s in enumerate(doc.get("reports") or []):
        check_summary(e, f"{path}.reports[{i}]", s, known)
    return e


def check_pages_index(doc: Any, path: str = "pages.json", public_safe: bool = False) -> Errors:
    e = Errors()
    if not isinstance(doc.get("file_num"), str):
        e.add(path, "missing file_num")
    for i, p in enumerate(doc.get("pages") or []):
        w = f"{path}.pages[{i}]"
        if not isinstance(p.get("page"), int) or p["page"] < 1:
            e.add(w, "page must be a 1-based integer")
        for key in ("width_px", "height_px", "n_values"):
            if not isinstance(p.get(key), int):
                e.add(w, f"{key} must be an integer")
        _enum(e, f"{w}.kind", p.get("kind"), PAGE_KINDS)
        for key in ("image", "thumb"):
            if key not in p:
                e.add(w, f"missing {key}")
            elif public_safe and p[key] is not None:
                e.add(w, f"{key} must be null in a public-safe build")
    return e


def check_no_banned_url(doc: Any, path: str) -> Errors:
    e = Errors()
    if BANNED_URL_FRAGMENT in json.dumps(doc):
        e.add(path, f"contains the banned service fragment {BANNED_URL_FRAGMENT!r}")
    return e


def check_manifest(doc: Any, path: str = "manifest.json") -> Errors:
    e = Errors()
    if doc.get("schema_version") != SCHEMA_VERSION:
        e.add(path, f"schema_version must be {SCHEMA_VERSION}")
    check_registry(e, f"{path}.stats", doc.get("stats"))
    build = doc.get("build") or {}
    for key in ("id", "created_at", "pipeline_version", "page_images", "public_safe", "fixture"):
        if key not in build:
            e.add(f"{path}.build", f"missing {key}")
    for key, art in (doc.get("artifacts") or {}).items():
        if not SHA256.match(str(art.get("sha256", ""))):
            e.add(f"{path}.artifacts[{key}]", "sha256 is not 64 hex characters")
    e.extend(check_no_banned_url(doc, path))
    return e


PROSPECT_ROLES = {"feature", "label", "context"}
PROSPECT_TIERS = {"native", "read", "derived"}
GAP_STATUS = {"not_addressable", "not_published", "not_public", "unverified", "published_not_pulled"}


def check_readiness(doc: Any, path: str = "prospect/readiness.json") -> Errors:
    """The readiness scorecard: every number backed, every source licensed, labels never used as features."""
    e = Errors()
    if not isinstance(doc, dict):
        e.add(path, "not an object")
        return e
    if doc.get("schema_version") != SCHEMA_VERSION:
        e.add(path, f"schema_version is {doc.get('schema_version')!r}, expected {SCHEMA_VERSION!r}")
    known = check_registry(e, f"{path}.values", doc.get("values"))
    for key in ("cells", "cell_m", "in_basin", "area_km2", "buffer_km"):
        _ref(e, f"{path}.grid.{key}", (doc.get("grid") or {}).get(key), known)
    for key, vid in (doc.get("totals") or {}).items():
        _ref(e, f"{path}.totals.{key}", vid, known)
    if not doc.get("caveats"):
        e.add(path, "no caveats: coverage may not be published without stating what it is not")

    for i, f in enumerate(doc.get("features") or []):
        where = f"{path}.features[{i}]"
        _ref(e, f"{where}.coverage", f.get("coverage"), known)
        _ref(e, f"{where}.covered_cells", f.get("covered_cells"), known)
        _ref(e, f"{where}.median_obs", f.get("median_obs"), known, nullable=True)
        _ref(e, f"{where}.median_value", f.get("median_value"), known, nullable=True)
        if not isinstance(f.get("is_effort"), bool):
            e.add(where, "is_effort must say whether this measures exploration effort or geology")
        if not isinstance(f.get("thin"), bool):
            e.add(where, "thin must say whether coverage is too low to carry a model")

    label_sources = {s.get("key") for s in (doc.get("sources") or []) if s.get("role") == "label"}
    feature_sources: set[str] = set()
    for f in doc.get("features") or []:
        feature_sources |= set(f.get("sources") or [])
    leaked = label_sources & feature_sources
    if leaked:
        e.add(path, f"label layer(s) {sorted(leaked)} used as feature inputs: labels are never features")

    for i, src in enumerate(doc.get("sources") or []):
        where = f"{path}.sources[{i}]"
        if src.get("role") not in PROSPECT_ROLES:
            e.add(where, f"role {src.get('role')!r} not in {sorted(PROSPECT_ROLES)}")
        if src.get("tier") not in PROSPECT_TIERS:
            e.add(where, f"tier {src.get('tier')!r} not in {sorted(PROSPECT_TIERS)}")
        if not src.get("licence"):
            e.add(where, "no licence named")
        if not src.get("verified_at"):
            e.add(where, "no verification date")
    # the phase blocks: every row's number resolves, and every row names the tracker run it came from
    for block in ("headline", "search", "hindcast"):
        b = doc.get(block)
        if b is None:
            continue
        where = f"{path}.{block}"
        if not isinstance(b, dict) or not b.get("run_id"):
            e.add(where, "a phase block must name its run")
            continue
        for i, r in enumerate(b.get("rows") or []):
            _ref(e, f"{where}.rows[{i}].value_id", r.get("value_id"), known)
            if not r.get("run_id"):
                e.add(f"{where}.rows[{i}]", "no run id: a metric without a tracked run is not reportable")
            for j, bound in enumerate(r.get("ci") or []):
                _ref(e, f"{where}.rows[{i}].ci[{j}]", bound, known)
        for extra in ("minetrace", "summary"):
            for i, r in enumerate(b.get(extra) or []):
                _ref(e, f"{where}.{extra}[{i}].value_id", r.get("value_id"), known)
        _ref(e, f"{where}.cells", b.get("cells"), known, nullable=True)
    rg = doc.get("readiness_gate")
    if rg is not None:
        where = f"{path}.readiness_gate"
        if not isinstance(rg, dict) or not isinstance(rg.get("green"), bool):
            e.add(where, "the readiness gate must say whether it is green")
        else:
            for i, r in enumerate(rg.get("rows") or []):
                for c in ("present", "licensed", "covers", "servable", "versioned"):
                    cell = r.get(c)
                    if not isinstance(cell, dict) or not isinstance(cell.get("ok"), bool) or not cell.get("note"):
                        e.add(f"{where}.rows[{i}].{c}", "each gate column is ok/not ok with a note saying why")
            failed = any(not r[c]["ok"] for r in rg.get("rows") or [] for c in ("present", "licensed", "covers", "servable", "versioned")
                         if isinstance(r.get(c), dict) and isinstance(r[c].get("ok"), bool))
            if rg["green"] and failed:
                e.add(where, "the gate says green with a failing column")
    if isinstance(doc.get("metrics"), dict):
        for i, r in enumerate(doc["metrics"].get("rows") or []):
            _ref(e, f"{path}.metrics.rows[{i}].value_id", r.get("value_id"), known)

    for i, g in enumerate(doc.get("gaps") or []):
        where = f"{path}.gaps[{i}]"
        if g.get("status") not in GAP_STATUS:
            e.add(where, f"status {g.get('status')!r} not in {sorted(GAP_STATUS)}")
        if not g.get("evidence"):
            e.add(where, "a gap needs the evidence that it is a gap")
    e.extend(check_no_banned_url(doc, path))
    return e


def check_export(target: Path, public_safe: bool = False, files: Iterable[str] | None = None) -> Errors:
    """Validate everything this pipeline writes under web/public/data."""
    e = Errors()
    index_path = target / "reports" / "index.json"
    if not index_path.is_file():
        e.add(str(index_path), "missing")
        return e
    index = json.loads(index_path.read_text())
    e.extend(check_report_index(index))
    e.extend(check_no_banned_url(index, str(index_path)))
    for summary in index.get("reports", []):
        fn = summary["file_num"]
        if files and fn not in files:
            continue
        rpath = target / "reports" / fn / "report.json"
        ppath = target / "reports" / fn / "pages.json"
        if not rpath.is_file():
            e.add(str(rpath), "missing")
            continue
        report = json.loads(rpath.read_text())
        e.extend(check_report(report, f"reports/{fn}/report.json"))
        e.extend(check_no_banned_url(report, f"reports/{fn}/report.json"))
        if ppath.is_file():
            pages = json.loads(ppath.read_text())
            e.extend(check_pages_index(pages, f"reports/{fn}/pages.json", public_safe=public_safe))
            for p in pages.get("pages", []):
                for key in ("image", "thumb"):
                    rel = p.get(key)
                    if rel and not (target / rel).is_file():
                        e.add(f"reports/{fn}/pages.json", f"{key} {rel} is not on disk")
    mpath = target / "manifest.json"
    if mpath.is_file():
        e.extend(check_manifest(json.loads(mpath.read_text())))
    rpath = target / "prospect" / "readiness.json"
    if rpath.is_file():
        e.extend(check_readiness(json.loads(rpath.read_text())))
    return e
