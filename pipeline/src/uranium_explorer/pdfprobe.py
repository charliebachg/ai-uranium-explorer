"""Cheap PDF facts from poppler (pdfinfo, pdffonts, pdfimages -list, pdftotext) before any rendering.

What later stages need to know per PDF and per page:
- page sizes in inches, and which pages are large format (maps, strip logs) so rendering skips them;
- whether a page is a scan (a page-covering image) and at what resolution;
- what kind of text layer it has: "none" (image only), "ocr_layer" (text laid over a scan by a scanner or
  OCR tool, e.g. the 1980 drill log rescanned in 2016 whose text layer reads "L2L.-7" for 121.7), or
  "born_digital" (text drawn by the authoring program). Only born-digital text can ever be trusted, and
  only after it agrees with OCR (see textlayer.py).

Poppler conventions found on the probe PDFs (poppler 26.04): pdfinfo page sizes are unrotated MediaBox
sizes with a separate /Rotate; pdftoppm applies /Rotate; pdftotext -bbox word boxes are in the rotated
frame while its <page> element reports the unrotated size.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .datum import count_hits
from .ids import sha256_file

PROBE_VERSION = "pdfprobe/v1"

# Producers and creators of scanners and OCR tools seen on assessment files, plus common ones.
_SCANNER = re.compile(
    r"canon|kofax|abbyy|finereader|omnipage|nuance|paper\s*capture|scansnap|xerox|ricoh|konica|kyocera|"
    r"sharp|epson|fujitsu|readiris|tesseract|ocrmypdf|\bocr\b|scan|image\s*products|capture|hp\s*digital|"
    r"lexmark|panasonic|bizhub|imagerunner|iris",
    re.IGNORECASE,
)
_OCR_FONTS = re.compile(r"glyphless|ocr|invisible", re.IGNORECASE)

RENDER_DPI = 200            # the render stage's base resolution
MAX_RENDER_PX = 4600        # longest side we render and OCR as one page
MAX_RENDER_MP = 15e6        # 17 x 22 in at 200 dpi
MIN_RENDER_DPI = 36


def _run(argv: list[str], timeout: float = 300) -> str:
    p = subprocess.run(argv, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed ({p.returncode}): {p.stderr.decode(errors='replace')[:400]}")
    return p.stdout.decode("utf-8", errors="replace")


def pdfinfo(pdf: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    head = _run(["pdfinfo", str(pdf)])
    info: dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    n = int(info.get("Pages", "0"))
    pages: dict[int, dict[str, Any]] = {i: {"page_no": i} for i in range(1, n + 1)}
    if n:
        body = _run(["pdfinfo", "-f", "1", "-l", str(n), str(pdf)])
        for line in body.splitlines():
            m = re.match(r"Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", line)
            if m:
                pages[int(m.group(1))].update(width_pt=float(m.group(2)), height_pt=float(m.group(3)))
            m = re.match(r"Page\s+(\d+)\s+rot:\s+(-?\d+)", line)
            if m:
                pages[int(m.group(1))]["rotate"] = int(m.group(2)) % 360
    for p in pages.values():
        p.setdefault("rotate", 0)
    return info, [pages[i] for i in sorted(pages)]


def pdffonts(pdf: Path) -> list[dict[str, Any]]:
    out = _run(["pdffonts", str(pdf)])
    fonts = []
    for line in out.splitlines()[2:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        # name type... encoding emb sub uni object ID; the type can contain spaces, so read from the right
        emb, sub, uni = parts[-5], parts[-4], parts[-3]
        fonts.append({"name": parts[0], "type": " ".join(parts[1:-6]), "encoding": parts[-6],
                      "embedded": emb == "yes", "subset": sub == "yes", "unicode": uni == "yes"})
    return fonts


def pdfimages_list(pdf: Path) -> list[dict[str, Any]]:
    out = _run(["pdfimages", "-list", str(pdf)])
    rows = []
    for line in out.splitlines()[2:]:
        parts = line.split()
        if len(parts) < 14:
            continue
        try:
            rows.append({"page": int(parts[0]), "type": parts[2], "width": int(parts[3]), "height": int(parts[4]),
                         "color": parts[5], "bpc": int(parts[7]), "enc": parts[8],
                         "x_ppi": float(parts[12]), "y_ppi": float(parts[13])})
        except ValueError:
            continue
    return rows


def page_text_chars(pdf: Path, n_pages: int) -> tuple[list[int], dict[int, dict[str, int]]]:
    """Non-space characters of the text layer per page, and datum-string hit counts per page."""
    text = _run(["pdftotext", "-layout", str(pdf), "-"])
    chunks = text.split("\f")
    chars, hits = [], {}
    for i in range(n_pages):
        t = chunks[i] if i < len(chunks) else ""
        chars.append(len(re.sub(r"\s", "", t)))
        h = count_hits(t)
        if any(h.values()):
            hits[i + 1] = h
    return chars, hits


def render_dpi_for_page(scan_ppi: int | None, base: int = RENDER_DPI) -> int:
    """Rendering above a scan's own resolution only interpolates, so a full-page scan is rendered at its
    native resolution when that is below the base dpi.

    This also repairs a producer quirk seen on 74H09-0039 (adultpdf.com): the page box is the image's
    pixel count in points, so the page claims 23.7 x 30.6 inches at 72 ppi while the scan is an ordinary
    2200 x 1705 px sheet. At 72 dpi it renders pixel for pixel; at 200 dpi it would be a blurry 28 MP."""
    if scan_ppi and scan_ppi < base:
        return max(MIN_RENDER_DPI, int(scan_ppi))
    return base


def large_format(width_pt: float, height_pt: float, dpi: int = RENDER_DPI) -> str | None:
    """Reason string if the page is too large to render and OCR as one page (maps, strip logs).

    Judged on the pixels the render would produce, not on the page box alone: an oversized box with a
    letter-sized scan inside is an ordinary page, while a 44-inch radiometric strip log is not."""
    short_in, long_in = sorted((width_pt / 72.0, height_pt / 72.0))
    px_short, px_long = short_in * dpi, long_in * dpi
    size = f"{short_in:.1f} x {long_in:.1f} in at {dpi} dpi = {px_short:.0f} x {px_long:.0f} px"
    if px_long > MAX_RENDER_PX:
        return f"{size}: longer than {MAX_RENDER_PX} px"
    if px_short * px_long > MAX_RENDER_MP:
        return f"{size}: more than {MAX_RENDER_MP / 1e6:.0f} megapixels"
    if long_in / max(short_in, 0.1) > 2.2 and px_long > 3000:
        return f"{size}: strip format (aspect {long_in / short_in:.1f})"
    return None


def classify_page(chars: int, coverage: float, ocr_signature: bool, real_fonts: bool) -> str:
    if chars < 10:
        return "none"
    if coverage >= 0.6:
        # text over a page-sized image: a scanner or OCR layer unless the file has real embedded fonts
        # and no scanner signature (a born-digital page with a full-page figure)
        return "born_digital" if (real_fonts and not ocr_signature) else "ocr_layer"
    return "born_digital"


def probe_pdf(pdf: Path, sha256: str | None = None) -> dict[str, Any]:
    info, pages = pdfinfo(pdf)
    fonts = pdffonts(pdf)
    images = pdfimages_list(pdf)
    chars, hits = page_text_chars(pdf, len(pages))
    producer = f"{info.get('Producer', '')} | {info.get('Creator', '')} | {info.get('Author', '')}"
    ocr_fonts = [f["name"] for f in fonts if _OCR_FONTS.search(f["name"])]
    real_fonts = any(f["embedded"] and not _OCR_FONTS.search(f["name"]) for f in fonts)
    ocr_signature = bool(ocr_fonts) or bool(_SCANNER.search(producer))
    by_page: dict[int, list[dict[str, Any]]] = {}
    for im in images:
        if im["type"] == "image":
            by_page.setdefault(im["page"], []).append(im)
    kinds: Counter[str] = Counter()
    dpis = []
    for p, n_chars in zip(pages, chars):
        w_in, h_in = p.get("width_pt", 612) / 72.0, p.get("height_pt", 792) / 72.0
        area = w_in * h_in
        ims = by_page.get(p["page_no"], [])
        cov = 0.0
        biggest = None
        for im in ims:
            if im["x_ppi"] <= 0 or im["y_ppi"] <= 0:
                continue
            a = (im["width"] / im["x_ppi"]) * (im["height"] / im["y_ppi"])
            cov += a / area
            if biggest is None or a > biggest[0]:
                biggest = (a, im)
        cov = min(1.0, cov)
        p["text_chars"] = n_chars
        p["image_coverage"] = round(cov, 3)
        p["n_images"] = len(ims)
        p["scan_ppi"] = round(biggest[1]["x_ppi"]) if (biggest and cov >= 0.6) else None
        p["scan_bitonal"] = bool(biggest and cov >= 0.6 and biggest[1]["bpc"] == 1)
        p["scanned"] = cov >= 0.6
        p["render_dpi"] = render_dpi_for_page(p["scan_ppi"])
        p["large_format"] = large_format(p.get("width_pt", 612), p.get("height_pt", 792), p["render_dpi"])
        p["page_size_in"] = [round(p.get("width_pt", 612) / 72, 2), round(p.get("height_pt", 792) / 72, 2)]
        p["text_layer_kind"] = classify_page(n_chars, cov, ocr_signature, real_fonts)
        p["datum_hits"] = hits.get(p["page_no"], {})
        kinds[p["text_layer_kind"]] += 1
        if p["scan_ppi"]:
            dpis.append(p["scan_ppi"])
    n = len(pages) or 1
    text_pages = kinds["ocr_layer"] + kinds["born_digital"]
    if text_pages < 0.1 * n:
        pdf_kind = "none"
    elif kinds["ocr_layer"] >= kinds["born_digital"]:
        pdf_kind = "ocr_layer"
    else:
        pdf_kind = "born_digital"
    doc_hits: dict[str, int] = {}
    for h in hits.values():
        for k, v in h.items():
            doc_hits[k] = doc_hits.get(k, 0) + v
    return {
        "version": PROBE_VERSION,
        "pdf": pdf.name,
        "sha256": sha256 or sha256_file(pdf),
        "bytes": pdf.stat().st_size,
        "producer": info.get("Producer"),
        "creator": info.get("Creator"),
        "creation_date": info.get("CreationDate"),
        "pdf_version": info.get("PDF version"),
        "n_pages": len(pages),
        "fonts": fonts,
        "ocr_fonts": ocr_fonts,
        "ocr_signature": ocr_signature,
        "real_embedded_fonts": real_fonts,
        "text_layer_kind": pdf_kind,
        "page_kinds": dict(sorted(kinds.items())),
        "scanned_pages": sum(1 for p in pages if p["scanned"]),
        "scanned": sum(1 for p in pages if p["scanned"]) >= 0.5 * n,
        "scan_dpi_estimate": int(statistics.median(dpis)) if dpis else None,
        "large_format_pages": [p["page_no"] for p in pages if p["large_format"]],
        "render_dpi": sorted({p["render_dpi"] for p in pages}),
        "text_datum_hits": doc_hits,
        "pages": pages,
    }


def probe_cached(pdf: Path, sha256: str, cache_dir: Path) -> dict[str, Any]:
    path = cache_dir / f"{sha256}.json"
    if path.is_file():
        rec = json.loads(path.read_text())
        if rec.get("version") == PROBE_VERSION:
            return rec
    rec = probe_pdf(pdf, sha256)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=1) + "\n")
    return rec
