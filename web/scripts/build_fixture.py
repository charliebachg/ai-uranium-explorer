"""Build the UI development fixture (web/public/fixtures) from the probed 1980 drill log (file 64L04-0075).

Run with the pipeline environment:  cd pipeline && uv run python ../web/scripts/build_fixture.py

The fixture exists so the evidence UI can be built before the extraction pipeline delivers real reports:
- page images are real pages of the public assessment file (kept local: web/public/fixtures/pages is git-ignored)
- boxes come from Apple Vision OCR on those pages
- the values were keyed by hand from the pages (lineage.model = "fixture"), not produced by the evaluated model
- manifest-level flag: every screen showing it carries a FIXTURE watermark
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from ocrmac import ocrmac
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "pipeline" / "data" / "probe" / "drilllog_1980s.pdf"
OUT = ROOT / "web" / "public" / "fixtures"
FILE = "64L04-0075"
RUN = "fixture"
NOW = "2026-09-18T00:00:00Z"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def render(page: int, dpi: int = 200) -> Path:
    tmp = ROOT / "pipeline" / "data" / "probe" / "render"
    tmp.mkdir(parents=True, exist_ok=True)
    prefix = tmp / f"p{page:04d}"
    subprocess.run(["pdftoppm", "-gray", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page), "-singlefile",
                    str(PDF), str(prefix)], check=True)
    return prefix.with_suffix(".png")


CONFUSABLE = str.maketrans({"O": "0", "o": "0", "Q": "0", "D": "0", "l": "1", "I": "1", "|": "1", "†": "+",
                             "S": "5", "B": "8", "°": "0", "#": "", ":": "", ",": ""})


def fold(t: str) -> str:
    return t.translate(CONFUSABLE).replace(" ", "").upper()


def ocr_tokens(img: Path) -> list[tuple[str, list[float]]]:
    """Live Text word tokens as top-left normalised [x0, y0, x1, y1]."""
    res = ocrmac.OCR(str(img), framework="livetext").recognize()
    out = []
    for text, _conf, (x, y, w, h) in res:
        top = 1 - y - h
        out.append((text, [x, top, x + w, top + h]))
    return out


def union(boxes: list[list[float]], pad: float = 0.003) -> list[float]:
    return [round(max(0, min(b[0] for b in boxes) - pad), 4), round(max(0, min(b[1] for b in boxes) - pad), 4),
            round(min(1, max(b[2] for b in boxes) + pad), 4), round(min(1, max(b[3] for b in boxes) + pad), 4)]


def find_anchor(tokens, anchor: str) -> list[float] | None:
    for n in (1, 2, 3):
        for i in range(len(tokens) - n + 1):
            seq = tokens[i:i + n]
            if fold("".join(t for t, _ in seq)) == fold(anchor):
                return union([b for _, b in seq], pad=0)
    return None


def locate(tokens, needle: str, row: str | None = None, col: str | None = None) -> list[float] | None:
    """Token sequences matching the needle (confusables folded), nearest to the row anchor's y and column anchor's x."""
    target = fold(needle)
    rb = find_anchor(tokens, row) if row else None
    cb = find_anchor(tokens, col) if col else None
    cands = []
    for n in range(1, 5):
        for i in range(len(tokens) - n + 1):
            seq = tokens[i:i + n]
            joined = fold("".join(t for t, _ in seq))
            if joined == target:  # exact after folding confusables: a fuzzy match can land on the wrong words
                box = union([b for _, b in seq])
                cy, cx = (box[1] + box[3]) / 2, (box[0] + box[2]) / 2
                d = 0.0
                if rb:
                    d += abs(cy - (rb[1] + rb[3]) / 2) * 3
                    if cx < rb[0]:
                        d += 1  # values sit to the right of their row label
                if cb:
                    d += abs(cx - (cb[0] + cb[2]) / 2)
                cands.append((d, n, box))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1]))
    return cands[0][2]


def cell_box(tokens, row_value: str, row_label: str, col: str) -> list[float] | None:
    """Box of an empty table cell: the column header's x-range on the row of a known value."""
    rb = locate(tokens, row_value, row=row_label)
    cb = find_anchor(tokens, col)
    if not rb or not cb:
        return None
    w = max(cb[2] - cb[0], 0.05)
    cx = (cb[0] + cb[2]) / 2
    return [round(cx - w * 0.75, 4), rb[1], round(cx + w * 0.75, 4), rb[3]]


def main() -> None:
    pdf_sha = sha256(PDF)
    pages_out = OUT / "pages" / FILE
    pages_out.mkdir(parents=True, exist_ok=True)
    values: dict[str, dict] = {}
    boxes_missing = []

    def extracted(key: str, page: int, lines, as_printed: str | None, value, unit: str | None, quote: str,
                  status: str = "pass", validators=None, row: str | None = None, col: str | None = None,
                  unit_source: str | None = None, bbox: list[float] | None = None):
        vid = f"x:{FILE}:{key}"
        if bbox is None and as_printed:
            bbox = locate(lines, as_printed, row=row, col=col)
        if bbox is None:
            boxes_missing.append(vid)
        values[vid] = {
            "id": vid, "kind": "extracted", "as_printed": as_printed, "value": value, "unit_as_printed": unit,
            "status": status,
            "lineage": {
                "file_num": FILE, "file_sha256": pdf_sha, "page": page, "bbox": bbox, "quote": quote,
                "quote_located": bbox is not None, **({"unit_source": unit_source} if unit_source else {}),
                "model": "fixture", "prompt_version": "fixture", "run_id": RUN, "extracted_at": NOW,
                "validators": validators or [],
            },
        }
        return vid

    def derived(key: str, value, fmt: str, op: str, inputs: list[str], unit: str | None = None, note: str | None = None):
        vid = f"d:{FILE}:{key}"
        values[vid] = {"id": vid, "kind": "derived", "as_printed": None, "value": value, "unit_as_printed": None,
                       "fmt": fmt, **({"unit": unit} if unit else {}),
                       "derivation": {"op": op, "inputs": inputs, "tool": "fixture builder"},
                       **({"note": note} if note else {})}
        return vid

    def stat(key: str, value, note: str | None = None):
        vid = f"d:{FILE}:{key}"
        values[vid] = {"id": vid, "kind": "derived", "as_printed": None, "value": value, "unit_as_printed": None,
                       "fmt": "int", "derivation": {"op": "count", "inputs": [], "tool": "fixture builder"},
                       **({"note": note} if note else {})}
        return vid

    # ---------- page 1: summary log, hole Q9-5 ----------
    p1 = render(1)
    l1 = ocr_tokens(p1)
    ok = [{"id": "V05", "outcome": "pass"}]
    q95 = {
        "name": extracted("q95_name", 1, l1, "Q9-5", "Q9-5", None, "DDH# Q9-5", row="DDH#"),
        "elev": extracted("q95_elev", 1, l1, "434.38", 434.38, None, "ELEVATION: 434.38", row="ELEVATION:", unit_source="page_note",
                          validators=[{"id": "V01", "outcome": "flag", "severity": "warn",
                                       "message": "unit comes from the page note '(all figures in meters)', not the cell"}]),
        "coords": extracted("q95_coords", 1, l1, "L 11+00N, 1+60W", None, None, "CO-ORDINATES: L 11+00N, 1+60W",
                            status="flag", row="CO-ORDINATES:",
                            validators=[{"id": "V10", "outcome": "flag", "severity": "warn", "class_a": False,
                                         "message": "local grid Q-9: no datum or UTM coordinates printed; cannot be transformed"}]),
        "dip": extracted("q95_dip", 1, l1, "90", 90, "°", "DIP: 90°", row="DIP:"),
        "unc": extracted("q95_unc", 1, l1, "155.8", 155.8, None, "UNCONFORMITY: 155.8", row="UNCONFORMITY:", unit_source="page_note"),
    }
    peaks = [
        ("pk1", "250cps/ .7", "153.6", "250cps/ .7 153.6"),
        ("pk2", "150cps/", "174.1", "150cps/ 174.1"),
    ]

    # ---------- page 6: sample table, hole Q9-6 ----------
    p6 = render(6)
    l6 = ocr_tokens(p6)
    q96_name = extracted("q96_name", 6, l6, "Q9-6", "Q9-6", None, "HOLE # Q9-6", row="HOLE#")
    samples = [("D 256", "152.2", "153.7"), ("D 257", "153.7", "155.2"), ("D 258", "155.2", "155.5"), ("D 259", "155.5", "155.8")]

    def interval(key, page, lines, frm, to, sample_id):
        f = extracted(f"{key}_from", page, lines, frm, float(frm), None, f"{sample_id} {frm} {to}", row=sample_id, col="From")
        t = extracted(f"{key}_to", page, lines, to, float(to), None, f"{sample_id} {frm} {to}", row=sample_id, col="To")
        fm = derived(f"{key}_from_m", float(frm), "m1", "identity", [f], unit="m", note="page note: all figures in meters")
        tm = derived(f"{key}_to_m", float(to), "m1", "identity", [t], unit="m", note="page note: all figures in meters")
        return f, t, fm, tm

    q96_assays = []
    for i, (sid, frm, to) in enumerate(samples):
        key = f"q96_s{i + 1}"
        s = extracted(f"{key}_sample", 6, l6, sid, sid, None, f"{sid} {frm} {to}", col="Sample")
        f, t, fm, tm = interval(key, 6, l6, frm, to, sid)
        g = extracted(f"{key}_u3o8", 6, l6, None, None, None, "U3O8 (column empty on the page)", status="flag",
                      bbox=cell_box(l6, sid, "Sample", "43°8"),
                      validators=[{"id": "V03", "outcome": "flag", "severity": "warn",
                                   "message": "U3O8 column is printed but empty for this row: not printed"}])
        q96_assays.append({
            "id": key, "from": f, "to": t, "from_m": fm, "to_m": tm, "table_id": "t6", "row": i, "status": "flag",
            "depth_unit_as_printed": None, "sample_id": s,
            "grades": [{"value": g, "analyte_as_printed": "U3O8", "species": "U3O8", "basis": "not_printed",
                        "method_as_printed": None}],
        })

    q95_assays = []
    for i, (key, reading, depth, quote) in enumerate(peaks):
        r = extracted(f"q95_{key}_reading", 1, l1, reading, None, "cps", quote, status="flag", row="PROBE PEAKS",
                      validators=[{"id": "V04", "outcome": "flag", "severity": "warn",
                                   "message": "probe count rate, not a chemical grade: basis probe_equivalent"}])
        # Apple Vision merges the two printed depths (153.6 over 174.1) into one token "154.4": the readers disagree
        merged = locate(l1, "154.4")
        d = extracted(f"q95_{key}_depth", 1, l1, depth, float(depth), None, quote, unit_source="page_note", bbox=merged,
                      status="flag",
                      validators=[{"id": "V16", "outcome": "flag", "severity": "warn", "class_a": False,
                                   "message": f"OCR reads 154.4 where the page prints {depth}: the two readers disagree, check by eye"}])
        dm = derived(f"q95_{key}_depth_m", float(depth), "m1", "identity", [d], unit="m", note="page note: all figures in meters")
        q95_assays.append({
            "id": f"q95_{key}", "from": d, "to": d, "from_m": dm, "to_m": dm, "table_id": "t1", "row": i,
            "status": "flag", "depth_unit_as_printed": None, "sample_id": None,
            "grades": [{"value": r, "analyte_as_printed": "probe peak (counts/interval)", "species": "not_printed",
                        "basis": "probe_equivalent", "method_as_printed": "ra probe"}],
        })

    # ---------- page images (webp, 150 dpi equivalent) ----------
    pages_meta = []
    for page, png in ((1, p1), (6, p6)):
        im = Image.open(png).convert("L")
        w, h = im.size
        scale = 150 / 200
        big = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        big.save(pages_out / f"p{page:04d}.webp", "WEBP", quality=80)
        thumb = im.copy()
        thumb.thumbnail((160, 220))
        thumb.save(pages_out / f"t{page:04d}.webp", "WEBP", quality=70)
        n_vals = sum(1 for v in values.values() if v.get("lineage", {}).get("page") == page)
        pages_meta.append({"page": page, "width_px": big.width, "height_px": big.height,
                           "kind": "collar_table" if page == 1 else "assay_table",
                           "image": f"pages/{FILE}/p{page:04d}.webp", "thumb": f"pages/{FILE}/t{page:04d}.webp",
                           "n_values": n_vals})

    # ---------- summary, report ----------
    lon0, lat0, lon1, lat1 = -104.0, 58.0, -103.5, 58.25  # NTS 64L04 sheet bounds (fallback footprint)
    footprint = {"type": "Polygon", "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}
    year = derived("year", "1980", "text", "work_date", [], note="WRK_PERIOD 1980 and DATE STARTED Feb. 7/80")
    pages_n = derived("page_count", 51, "int", "pdfinfo", [])
    n_pass = stat("n_pass", 0)
    n_flag = stat("n_flag", 2)
    n_miss = stat("n_miss", 0)
    rows_t6 = stat("rows_t6", 4, note="rows stored from the page 6 sample table")

    summary = {
        "file_num": FILE, "company": "Asamera Oil Corporation Ltd", "property": "Grid Q-9", "era": "1980s-1990s",
        "year": year, "nts_sheets": ["64L04"], "page_count": pages_n, "scan_kind": "scanned", "file_sha256": pdf_sha,
        "source_url": "https://geoscience-data-system.saskatchewan.ca/", "split": "dev",
        "status_counts": {"pass": n_pass, "flag": n_flag, "miss": n_miss},
        "footprint": footprint, "centroid": [(lon0 + lon1) / 2, (lat0 + lat1) / 2],
        "holes": [
            {"hole_id": "Q9-5", "name": "Q9-5", "status": "flag", "lonlat": None, "position_source": "none", "datum_basis": "local_grid"},
            {"hole_id": "Q9-6", "name": "Q9-6", "status": "flag", "lonlat": None, "position_source": "none", "datum_basis": "none"},
        ],
    }
    empty_collar = {k: None for k in ("easting", "northing", "lat", "lon", "grid_x", "grid_y", "utm_zone", "datum_printed",
                                      "elevation", "azimuth", "dip", "total_depth")}
    report = {
        "summary": summary,
        "values": values,
        "tables": [
            {"table_id": "t1", "page": 1, "bbox": None, "kind": "probe", "rows_stored": stat("rows_t1", 2), "rows_printed": None, "continues_from": None},
            {"table_id": "t6", "page": 6, "bbox": None, "kind": "assay", "rows_stored": rows_t6, "rows_printed": None, "continues_from": None},
        ],
        "holes": [
            {"hole_id": "Q9-5", "name": q95["name"], "status": "flag",
             "collar": {**empty_collar, "coord_kind": "local_grid", "grid_x": q95["coords"], "elevation": q95["elev"], "dip": q95["dip"]},
             "position": None, "matches": [], "provincial_lith": [], "lith": [], "assays": q95_assays},
            {"hole_id": "Q9-6", "name": q96_name, "status": "flag",
             "collar": {**empty_collar, "coord_kind": "not_printed"},
             "position": None, "matches": [], "provincial_lith": [], "lith": [], "assays": q96_assays},
        ],
    }
    (OUT / "reports" / FILE).mkdir(parents=True, exist_ok=True)
    (OUT / "reports" / FILE / "report.json").write_text(json.dumps(report, indent=1))
    (OUT / "reports" / FILE / "pages.json").write_text(json.dumps({"file_num": FILE, "pages": pages_meta}, indent=1))
    index_values = {k: v for k, v in values.items() if k in {year, pages_n, n_pass, n_flag, n_miss}}
    (OUT / "reports" / "index.json").write_text(json.dumps({"reports": [summary], "values": index_values}, indent=1))
    print(f"fixture written to {OUT}; values {len(values)}; unlocated boxes: {boxes_missing}")


if __name__ == "__main__":
    main()
