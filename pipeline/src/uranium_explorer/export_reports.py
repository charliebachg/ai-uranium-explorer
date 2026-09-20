"""`ue export-reports`: write the per-report half of the web contract, gated by the contract checker.

What it writes under `web/public/data`:
  reports/index.json              one ReportSummary per read file, plus the Vals they reference
  reports/<file>/report.json      values registry, tables, holes with collar, position, matches, intervals
  reports/<file>/pages.json       the pages the report points at
  pages/<file>/pNNNN.webp         150 dpi-equivalent page image, quality 80 (git-ignored, never public-safe)
  pages/<file>/tNNNN.webp         thumbnail
and patches exactly two things in the existing `manifest.json`: the `m:files_read` stat and the artifact
entries for the files written here. `export_web.py` (Phase 1, owned elsewhere) still owns the rest.

Nothing is written until `contract_check` passes on the staged copy: an invalid export never lands.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from PIL import Image
from shapely.geometry import MultiPoint, box as shapely_box, mapping
from shapely.ops import unary_union

from . import __version__, contract_check
from .assemble import assembled_files, read_assembled
from .crosscheck import read_crosscheck
from .ids import sha256_file, sha256_json, short
from .normalise import normalise_hole_name
from .nts import nts_bounds
from .paths import PATHS
from .position import read_positions
from .render import read_pages
from .values import registry, stat

EXPORT_VERSION = "export-reports/v1"
HOLE_BUFFER_M = 250.0
PAGE_DPI_SOURCE = 200
PAGE_DPI_TARGET = 150
PAGE_QUALITY = 80
THUMB_BOX = (160, 220)

_PAGE_KINDS = {"collar_table", "lith_log", "assay_table", "probe_log", "certificate", "other"}
_TABLE_KINDS = {"collar", "assay", "lith", "probe"}
_ROUTE_TO_TABLE = {"collar_table": "collar", "assay_table": "assay", "lith_log": "lith", "probe_log": "probe"}


def _deg_per_m(lat: float) -> tuple[float, float]:
    import math

    return 1.0 / 111_320.0, 1.0 / (111_320.0 * max(0.1, math.cos(math.radians(lat))))


def _dump(path: Path, obj: Any, compact: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(obj, separators=(",", ":")) if compact else json.dumps(obj, indent=1))
    tmp.replace(path)


# ------------------------------------------------------------------ file metadata

def file_metadata(file_num: str) -> dict[str, Any]:
    """Company, era, year, NTS sheets, source URL and page count, from the selection artifacts."""
    sel = json.loads((PATHS.index / "selection.json").read_text())
    meta: dict[str, Any] = {"file_num": file_num, "company": "", "property": None, "era": "1970s",
                            "year": None, "nts_sheets": [], "source_url": "", "pages": 0,
                            "split": "dev", "scanned": None}
    for cand in sel.get("shortlist", {}).get("candidates", []):
        if cand.get("file_num") == file_num:
            f = cand.get("features", {})
            meta.update(company=(f.get("company") or "").strip() or "(company not recorded)",
                        property=(f.get("property") or None), era=f.get("era") or "1970s",
                        year=f.get("year"), nts_sheets=f.get("nts_sheets") or [])
            break
    probe = (sel.get("probe", {}).get("files") or {}).get(file_num, {})
    reports = probe.get("report_pdfs") or []
    if reports:
        meta["source_url"] = reports[0].get("url") or ""
    post = (sel.get("post_fetch", {}).get("files") or {}).get(file_num, {})
    meta["pages"] = post.get("pages") or 0
    meta["scanned"] = post.get("scanned")
    meta["documents"] = post.get("documents") or []
    lock = PATHS.gold / "heldout.lock"
    if lock.is_file():
        heldout = {e["file_num"] for e in json.loads(lock.read_text()).get("heldout", [])}
        meta["split"] = "heldout" if file_num in heldout else "dev"
    return meta


def _scan_kind(pages: list[dict[str, Any]]) -> str:
    if not pages:
        return "scanned"
    trusted = sum(1 for p in pages if p.get("text_layer_trusted"))
    if trusted == 0:
        return "scanned"
    return "text" if trusted >= 0.9 * len(pages) else "mixed"


def _footprint(lonlats: list[list[float]], nts_sheets: list[str]) -> tuple[dict[str, Any], list[float], str]:
    """Hull of the plottable holes buffered 250 m; the file's NTS sheets when nothing is plottable."""
    if lonlats:
        lat0 = sum(p[1] for p in lonlats) / len(lonlats)
        dlat, dlon = _deg_per_m(lat0)
        geom = MultiPoint([(p[0], p[1]) for p in lonlats]).convex_hull
        buffered = geom.buffer(HOLE_BUFFER_M * max(dlat, dlon), quad_segs=8)
        note = (f"hull of the {len(lonlats)} plottable hole(s) in this file, buffered "
                f"{HOLE_BUFFER_M:.0f} m")
        poly = buffered
    else:
        boxes = []
        for sheet in nts_sheets:
            try:
                boxes.append(shapely_box(*nts_bounds(sheet)))
            except Exception:
                continue
        if not boxes:
            boxes = [shapely_box(-110.0, 56.0, -102.0, 60.0)]
            note = ("no hole in this file could be placed and its NTS sheets could not be resolved: the "
                    "footprint is the region of interest, not a measured extent")
        else:
            note = (f"no hole in this file could be placed, so the footprint is the union of its NTS sheets "
                    f"({', '.join(nts_sheets)}), not a measured extent")
        poly = unary_union(boxes).convex_hull
    m = mapping(poly)
    coords = [[[round(x, 6), round(y, 6)] for x, y in ring] for ring in m["coordinates"]]
    c = poly.centroid
    return {"type": "Polygon", "coordinates": coords}, [round(c.x, 6), round(c.y, 6)], note


# ------------------------------------------------------------------ report building

def build_report(file_num: str, log: Callable[[str], None] = print) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(report.json, pages.json, summary) for one file."""
    doc = read_assembled(file_num)
    positions = read_positions(file_num)
    cross = read_crosscheck(file_num)
    meta = file_metadata(file_num)
    page_rows = [r for r in read_pages() if r["file_num"] == file_num]
    by_page = {(r["pdf_sha256"], int(r["page_no"])): r for r in page_rows}

    values: dict[str, Any] = dict(doc.get("values", {}))
    values.update(positions.get("values", {}))
    values.update(cross.get("values", {}))
    values.update(cross.get("provincial_lith_values", {}))

    pos_by_hole = {p["hole_id"]: p for p in positions.get("holes", [])}
    matches_by_hole: dict[str, list[dict[str, Any]]] = {}
    for m in cross.get("matches", []):
        matches_by_hole.setdefault(m["hole_id"], []).append(m)
    prov_lith = cross.get("provincial_lith", {})

    # ---- tables
    tables_out = []
    for t in doc.get("tables", []):
        kind = t.get("kind")
        if kind not in _TABLE_KINDS:
            kind = _ROUTE_TO_TABLE.get(t.get("route_class") or "", "lith")
        rows_stored = f"d:{file_num}:rows_{short(sha256_json([t['table_id'], 'stored']), 8)}"
        values[rows_stored] = stat(rows_stored, int(t.get("rows_stored") or 0),
                                   note=f"rows stored from table {t['table_id']}")
        rows_printed = None
        if t.get("rows_printed"):
            rows_printed = f"d:{file_num}:rows_{short(sha256_json([t['table_id'], 'printed']), 8)}"
            values[rows_printed] = stat(rows_printed, int(t["rows_printed"]),
                                        note="rows the model counted as printed on the page before transcribing")
        tables_out.append({"table_id": t["table_id"], "page": int(t["page"]), "bbox": t.get("bbox"),
                           "kind": kind, "rows_stored": rows_stored, "rows_printed": rows_printed,
                           "continues_from": t.get("continues_from")})

    # ---- holes
    holes_out: list[dict[str, Any]] = []
    stubs: list[dict[str, Any]] = []
    dropped_intervals = 0
    for hole in doc.get("holes", []):
        collar_in = hole.get("collar", {})
        pos = pos_by_hole.get(hole["hole_id"], {})
        # Same precedence as the position stage: a printed projected position wins over a local grid
        # reference printed on the same sheet.
        coord_kind = "not_printed"
        if collar_in.get("latitude") or collar_in.get("longitude"):
            coord_kind = "geographic"
        elif (collar_in.get("easting") or collar_in.get("northing")) and \
                pos.get("status") != "not_transformable_local_grid":
            coord_kind = "utm"
        elif collar_in.get("grid_x") or collar_in.get("grid_y") or \
                pos.get("status") == "not_transformable_local_grid":
            coord_kind = "local_grid"
        collar = {
            "coord_kind": coord_kind,
            "easting": collar_in.get("easting"), "northing": collar_in.get("northing"),
            "lat": collar_in.get("latitude"), "lon": collar_in.get("longitude"),
            "grid_x": collar_in.get("grid_x"), "grid_y": collar_in.get("grid_y"),
            "utm_zone": collar_in.get("utm_zone"), "datum_printed": collar_in.get("datum"),
            "elevation": collar_in.get("elevation"), "azimuth": collar_in.get("azimuth"),
            "dip": collar_in.get("dip"), "total_depth": collar_in.get("total_depth"),
        }
        position = None
        if pos.get("lonlat") and pos.get("lon_vid") in values and pos.get("lat_vid") in values:
            transform = None
            if pos.get("transform") and pos.get("shift_vid") in values:
                tr = pos["transform"]
                transform = {"name": tr["name"], "code": tr["code"], "pipeline": tr["pipeline"],
                             "grid_file": tr["grid_file"], "grid_sha256": tr["grid_sha256"],
                             "from": tr["from"], "to": tr["to"], "shift_m": pos["shift_vid"],
                             "bearing_deg": pos["bearing_vid"], "accuracy_m": tr["accuracy_m"]}
            position = {"lonlat": pos["lonlat"], "source": pos["position_source"],
                        "lon": pos["lon_vid"], "lat": pos["lat_vid"], "transform": transform,
                        "misread_lonlat": pos.get("misread_lonlat"), "alt_lonlat": pos.get("alt_lonlat")}

        matches = []
        for m in matches_by_hole.get(hole["hole_id"], []):
            if not m.get("offset_m") or not m.get("lonlat"):
                continue
            matches.append({"dataset": m["dataset"], "feature_id": int(m["feature_id"]),
                            "lonlat": [round(m["lonlat"][0], 6), round(m["lonlat"][1], 6)],
                            "offset_m": m["offset_m"], "bearing_deg": m["bearing_deg"],
                            "name_match": m["name_match"],
                            "datum_shift_signature": bool(m["datum_shift_signature"]),
                            "adjudication": m["adjudication"]})

        def intervals(group: str, assay: bool) -> list[dict[str, Any]]:
            nonlocal dropped_intervals
            out = []
            for i in hole.get(group, []):
                if not all(i.get(k) for k in ("from", "to", "from_m", "to_m")):
                    dropped_intervals += 1
                    continue
                base = {"id": i["id"], "from": i["from"], "to": i["to"], "from_m": i["from_m"],
                        "to_m": i["to_m"], "table_id": i.get("table_id"), "row": i.get("row"),
                        "status": i.get("status", "pass"),
                        "depth_unit_as_printed": i.get("depth_unit_as_printed")}
                if assay:
                    base["sample_id"] = i.get("sample_id")
                    base["grades"] = [{"value": g["value"], "analyte_as_printed": g.get("analyte_as_printed"),
                                       "species": g.get("species", "not_printed"),
                                       "basis": g.get("basis", "not_printed"),
                                       "method_as_printed": g.get("method_as_printed")}
                                      for g in i.get("grades", []) if g.get("value") in values]
                else:
                    base["code"] = i.get("code")
                    base["description"] = i.get("description")
                out.append(base)
            return out

        prov_name = next((m["provincial_name"] for m in matches_by_hole.get(hole["hole_id"], [])
                          if m["dataset"] == "geods" and m.get("provincial_name")), None)
        status = hole.get("status", "pass")
        holes_out.append({
            "hole_id": hole["hole_id"], "name": hole.get("name_vid"), "status": status,
            "collar": collar, "position": position, "matches": matches,
            "provincial_lith": prov_lith.get(prov_name or "", []),
            "lith": intervals("lith", assay=False), "assays": intervals("assays", assay=True),
        })
        datum_basis = "printed" if collar_in.get("datum") else (
            "local_grid" if coord_kind == "local_grid" else "none")
        stub = {"hole_id": hole["hole_id"], "name": hole.get("name_as_printed") or hole["hole_id"],
                "status": status, "lonlat": pos.get("lonlat"),
                "position_source": pos.get("position_source") or "none", "datum_basis": datum_basis}
        for m in matches_by_hole.get(hole["hole_id"], []):
            if m["dataset"] == "geods":
                stub.setdefault("geods_id", int(m["feature_id"]))
            else:
                stub.setdefault("cmp_id", int(m["feature_id"]))
        stubs.append(stub)

    # ---- status counts and summary
    counts = {"pass": 0, "flag": 0, "miss": 0}
    for v in doc.get("values", {}).values():
        s = v.get("status")
        if s in counts:
            counts[s] += 1
    count_ids = {}
    for key, n in counts.items():
        vid = f"d:{file_num}:status_{key}"
        values[vid] = stat(vid, n, note=f"extracted values with status {key} in this file")
        count_ids[key] = vid
    year_vid = f"d:{file_num}:year"
    values[year_vid] = stat(year_vid, meta.get("year") or 0, fmt="year",
                            note="year of work, from the province's assessment file record")
    pages_vid = f"d:{file_num}:page_count"
    values[pages_vid] = stat(pages_vid, int(meta.get("pages") or len(page_rows)),
                             note="pages in the report PDFs of this assessment file")

    plottable = [s["lonlat"] for s in stubs if s.get("lonlat")]
    footprint, centroid, footprint_note = _footprint(plottable, meta["nts_sheets"])
    fp_vid = f"d:{file_num}:footprint_basis"
    values[fp_vid] = stat(fp_vid, len(plottable), note=footprint_note)

    file_sha = doc.get("file_sha256") or ""
    summary = {
        "file_num": file_num, "company": meta["company"], "era": meta["era"], "year": year_vid,
        "nts_sheets": meta["nts_sheets"], "page_count": pages_vid, "scan_kind": _scan_kind(page_rows),
        "file_sha256": file_sha, "source_url": meta["source_url"] or "https://geoscience-data-system.saskatchewan.ca/",
        "split": meta["split"], "status_counts": count_ids, "footprint": footprint, "centroid": centroid,
        "holes": stubs, "note": footprint_note,
    }
    if meta.get("property"):
        summary["property"] = meta["property"]

    # ---- pages index
    pages_with_values: dict[int, int] = {}
    for v in doc.get("values", {}).values():
        page = (v.get("lineage") or {}).get("page")
        if isinstance(page, int):
            pages_with_values[page] = pages_with_values.get(page, 0) + 1
    pages_out = []
    for p in sorted(doc.get("pages", []), key=lambda p: int(p["page"])):
        page_no = int(p["page"])
        row = by_page.get((p["pdf_sha256"], page_no), {})
        kind = p.get("route_class") or "other"
        if kind not in _PAGE_KINDS:
            kind = "other"
        pages_out.append({"page": page_no, "width_px": int(row.get("width_px") or 0),
                          "height_px": int(row.get("height_px") or 0), "kind": kind,
                          "image": None, "thumb": None,
                          "n_values": pages_with_values.get(page_no, 0),
                          "_image_path": row.get("image_path"), "_pdf_sha256": p["pdf_sha256"]})

    report = {"summary": summary, "values": values, "tables": tables_out, "holes": holes_out}
    pages_index = {"file_num": file_num, "pages": pages_out}
    if dropped_intervals:
        log(f"    {file_num}: {dropped_intervals} interval(s) had no printed depth pair and are not shown as "
            "intervals (their values stay in the registry)")
    return report, pages_index, summary


# ------------------------------------------------------------------ page images

def write_page_images(file_num: str, pages: list[dict[str, Any]], out_root: Path,
                      log: Callable[[str], None] = print) -> tuple[int, int]:
    """150 dpi-equivalent WebP plus a thumbnail, for pages carrying lineage or routed as tables."""
    out_dir = out_root / "pages" / file_num
    out_dir.mkdir(parents=True, exist_ok=True)
    scale = PAGE_DPI_TARGET / PAGE_DPI_SOURCE
    written = 0
    total_bytes = 0
    for p in pages:
        src_rel = p.pop("_image_path", None)
        p.pop("_pdf_sha256", None)
        wanted = p["n_values"] > 0 or p["kind"] in ("collar_table", "assay_table", "lith_log", "probe_log")
        if not src_rel or not wanted:
            continue
        src = PATHS.data / src_rel
        if not src.is_file():
            continue
        with Image.open(src) as im:
            im = im.convert("L")
            big = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
            big.save(out_dir / f"p{p['page']:04d}.webp", "WEBP", quality=PAGE_QUALITY)
            thumb = im.copy()
            thumb.thumbnail(THUMB_BOX)
            thumb.save(out_dir / f"t{p['page']:04d}.webp", "WEBP", quality=70)
        p["image"] = f"pages/{file_num}/p{p['page']:04d}.webp"
        p["thumb"] = f"pages/{file_num}/t{p['page']:04d}.webp"
        p["width_px"], p["height_px"] = big.width, big.height
        written += 1
        total_bytes += (out_dir / f"p{p['page']:04d}.webp").stat().st_size
        total_bytes += (out_dir / f"t{p['page']:04d}.webp").stat().st_size
    return written, total_bytes


# ------------------------------------------------------------------ manifest patch

def patch_manifest(target: Path, files_read: int, artifacts: dict[str, dict[str, Any]],
                   page_images: bool, log: Callable[[str], None] = print) -> bool:
    path = target / "manifest.json"
    if not path.is_file():
        log("  no manifest.json yet (run `ue export-web` first): the files_read stat was not patched")
        return False
    manifest = json.loads(path.read_text())
    stats = manifest.setdefault("stats", {})
    existing = stats.get("m:files_read")
    stats["m:files_read"] = stat("m:files_read", files_read,
                                 note="uranium-tagged assessment files read by this pipeline")
    if existing and existing.get("note"):
        stats["m:files_read"]["note"] = existing["note"]
    manifest.setdefault("artifacts", {}).update(artifacts)
    manifest.setdefault("build", {})["page_images"] = page_images
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(path)
    return True


def mark_read_holes(target: Path, ids: dict[str, set[int]], log: Callable[[str], None] = print
                    ) -> dict[str, dict[str, Any]]:
    """Set `rd` on the bulk features matched to read files, so nothing plots twice."""
    out: dict[str, dict[str, Any]] = {}
    for key, rel in (("compilation", "bulk/compilation_collars.geojson"),
                     ("geods", "bulk/geods_holes.geojson")):
        path = target / rel
        wanted = ids.get(key) or set()
        if not path.is_file() or not wanted:
            continue
        fc = json.loads(path.read_text())
        n = 0
        for f in fc.get("features", []):
            flag = 1 if int(f.get("id") or 0) in wanted else 0
            if f["properties"].get("rd") != flag:
                f["properties"]["rd"] = flag
                n += 1 if flag else 0
        tmp = path.with_suffix(".geojson.part")
        tmp.write_text(json.dumps(fc, separators=(",", ":")))
        tmp.replace(path)
        out[rel] = {"path": rel, "bytes": path.stat().st_size, "sha256": sha256_file(path),
                    "count": len(fc.get("features", []))}
        log(f"  {rel}: marked {n} feature(s) as belonging to a read file")
    return out


# ------------------------------------------------------------------ stage

def stage_export_reports(files: list[str] | None = None, public_safe: bool = False,
                         target: Path | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    target = target or PATHS.web_data
    targets = [f for f in assembled_files() if not files or f in files]
    if not targets:
        raise RuntimeError("nothing assembled; run `ue assemble` first")

    stage = Path(tempfile.mkdtemp(prefix="ue_reports_", dir=PATHS.data))
    artifacts: dict[str, dict[str, Any]] = {}
    summaries: list[dict[str, Any]] = []
    index_values: dict[str, Any] = {}
    read_ids: dict[str, set[int]] = {"compilation": set(), "geods": set()}
    image_stats = {"pages": 0, "bytes": 0}
    try:
        for file_num in targets:
            report, pages_index, summary = build_report(file_num, log=log)
            if public_safe:
                for p in pages_index["pages"]:
                    p.pop("_image_path", None)
                    p.pop("_pdf_sha256", None)
                    p["image"] = p["thumb"] = None
            else:
                n, nbytes = write_page_images(file_num, pages_index["pages"], stage, log=log)
                image_stats["pages"] += n
                image_stats["bytes"] += nbytes
            _dump(stage / "reports" / file_num / "report.json", report)
            _dump(stage / "reports" / file_num / "pages.json", pages_index)
            summaries.append(summary)
            for key in ("year", "page_count", "footprint_basis"):
                vid = f"d:{file_num}:{key}"
                if vid in report["values"]:
                    index_values[vid] = report["values"][vid]
            for vid in summary["status_counts"].values():
                index_values[vid] = report["values"][vid]
            for hole in report["holes"]:
                for m in hole["matches"]:
                    read_ids["compilation" if m["dataset"] == "compilation" else "geods"].add(m["feature_id"])
            log(f"  {file_num}: {len(report['holes'])} holes, {len(report['tables'])} tables, "
                f"{len(report['values'])} values")

        _dump(stage / "reports" / "index.json", {"reports": summaries, "values": index_values})

        errors = contract_check.check_export(stage, public_safe=public_safe)
        if errors:
            for msg in errors[:25]:
                log(f"  CONTRACT ERROR {msg}")
            raise ValueError(f"export refused: {len(errors)} contract violation(s); nothing was written")

        for rel_path in sorted(p.relative_to(stage) for p in stage.rglob("*") if p.is_file()):
            src = stage / rel_path
            dst = target / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            if str(rel_path).endswith(".json"):
                artifacts[str(rel_path)] = {"path": str(rel_path), "bytes": dst.stat().st_size,
                                            "sha256": sha256_file(dst)}
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    if not public_safe:
        artifacts.update(mark_read_holes(target, read_ids, log=log))
    patched = patch_manifest(target, len(targets), artifacts, page_images=not public_safe, log=log)

    final_errors = contract_check.check_export(target, public_safe=public_safe, files=targets)
    summary = {
        "version": EXPORT_VERSION, "pipeline_version": __version__, "public_safe": public_safe,
        "exported_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "files": targets, "reports": len(summaries),
        "holes": sum(len(s["holes"]) for s in summaries),
        "page_images": image_stats["pages"], "page_image_bytes": image_stats["bytes"],
        "artifacts": artifacts, "manifest_patched": patched,
        "contract_errors": list(final_errors),
    }
    log(f"exported {len(summaries)} report(s) to {target}; "
        f"{image_stats['pages']} page images ({image_stats['bytes'] / 1e6:.1f} MB); "
        f"contract {'ok' if not final_errors else f'{len(final_errors)} errors'}")
    return summary


REGISTRY_HELPERS = (registry, normalise_hole_name)
