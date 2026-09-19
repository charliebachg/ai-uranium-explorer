"""`lr render`: page images for the Phase 1 dev files, and the pages table every later stage extends.

- Greyscale PNG per page (pdftoppm, which applies the PDF's /Rotate) at data/pages/<pdf_sha256>/p0001.png,
  plus a 72 dpi thumbnail t0001.png for routing views and the web app. 200 dpi by default, but a page that
  is one full-page scan renders at the scan's own resolution when that is lower, because anything above it
  is interpolation (see pdfprobe.render_dpi_for_page).
- Large-format pages (maps, strip logs; see pdfprobe.large_format) are not rendered, but they get a row
  with `rendered = false` and the reason, so nothing disappears silently.
- Held-out PDFs are never rendered: the guard reads gold/heldout.lock and refuses by file number and by
  PDF sha256, and it refuses everything if the split has not been locked yet.
- data/out/pages.parquet has one row per page; ocr and route add their columns to the same rows.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from .ids import sha256_file
from .paths import PATHS
from .pdfprobe import probe_cached

RENDER_DPI = 200
THUMB_DPI = 72
RENDER_VERSION = "render/v1"


class HeldOutError(Exception):
    """Raised whenever a held-out file or PDF would be rendered, OCR'd or routed."""


def pdftoppm_version() -> str:
    p = subprocess.run(["pdftoppm", "-v"], capture_output=True, text=True)
    return (p.stderr or p.stdout).splitlines()[0].strip()


def page_id(pdf_sha256: str, page_no: int) -> str:
    return f"pg:{pdf_sha256[:12]}:{page_no:04d}"


# ---------------------------------------------------------------- pages table


def pages_path() -> Path:
    return PATHS.out / "pages.parquet"


def read_pages(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or pages_path()
    if not path.is_file():
        return []
    return pq.read_table(path).to_pylist()


def write_pages(rows: list[dict[str, Any]], path: Path | None = None) -> Path:
    path = path or pages_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["file_num"], r["pdf_name"], r["page_no"]))
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    table = pa.Table.from_pylist([{k: r.get(k) for k in keys} for r in rows])
    tmp = path.with_suffix(".part")
    pq.write_table(table, tmp)
    tmp.rename(path)
    return path


def update_pages(updates: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    rows = read_pages(path)
    for r in rows:
        if r["page_id"] in updates:
            r.update(updates[r["page_id"]])
    write_pages(rows, path)


# ---------------------------------------------------------------- guard


def guard_not_heldout(file_num: str, pdf_sha256: str, lock_path: Path | None = None) -> None:
    lock_path = lock_path or (PATHS.gold / "heldout.lock")
    if not lock_path.is_file():
        raise HeldOutError(f"{lock_path} missing: lock the held-out split (`lr lock-heldout`) before rendering")
    lock = json.loads(lock_path.read_text())
    shas = {p["sha256"] for h in lock["heldout"] for p in h["report_pdfs"]}
    if file_num in set(lock["file_nums"]) or pdf_sha256 in shas:
        raise HeldOutError(f"{file_num} ({pdf_sha256[:12]}) is held out; it is not rendered before the config freeze")


# ---------------------------------------------------------------- rendering


def _ranges(pages: list[int], max_len: int = 8) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for p in sorted(pages):
        if out and p == out[-1][1] + 1 and out[-1][1] - out[-1][0] + 1 < max_len:
            out[-1] = (out[-1][0], p)
        else:
            out.append((p, p))
    return out


def _render_range(pdf: Path, first: int, last: int, out_dir: Path, dpi: int) -> dict[int, Path]:
    """Render pages first..last at one dpi; thumbnails are scaled down from the same bitmap."""
    tmp = Path(tempfile.mkdtemp(prefix="lr_render_", dir=out_dir))
    try:
        subprocess.run(["pdftoppm", "-gray", "-r", str(dpi), "-f", str(first), "-l", str(last), str(pdf),
                        str(tmp / "r")], check=True, capture_output=True, timeout=1800)
        done: dict[int, Path] = {}
        for pgm in sorted(tmp.glob("r-*.pgm")):
            n = int(pgm.stem.split("-")[-1])
            png = out_dir / f"p{n:04d}.png"
            with Image.open(pgm) as im:
                im = im.convert("L")
                im.save(png, format="PNG")
                scale = THUMB_DPI / dpi
                thumb = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
                thumb.save(out_dir / f"t{n:04d}.png", format="PNG")
            done[n] = png
        return done
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render_pdf(pdf: Path, pdf_sha256: str, file_num: str, out_root: Path, probe: dict[str, Any],
               dpi: int = RENDER_DPI, workers: int = 4, check_heldout: Callable[[str, str], None] | None = guard_not_heldout,
               log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """Render every normal-size page of one PDF; returns one pages-table row per page (rendered or not)."""
    if check_heldout is not None:
        check_heldout(file_num, pdf_sha256)
    out_dir = out_root / pdf_sha256
    out_dir.mkdir(parents=True, exist_ok=True)
    params = {"version": RENDER_VERSION, "base_dpi": dpi, "thumb_dpi": THUMB_DPI, "mode": "gray",
              "tool": pdftoppm_version(),
              "note": "full-page scans render at their native resolution when that is below the base dpi"}
    sidecar = out_dir / "render.json"
    if sidecar.is_file() and json.loads(sidecar.read_text()).get("params") != params:
        for f in out_dir.glob("[pt][0-9][0-9][0-9][0-9].png"):
            f.unlink()
    by_dpi: dict[int, list[int]] = {}
    for p in probe["pages"]:
        if p["large_format"] or (out_dir / f"p{p['page_no']:04d}.png").is_file():
            continue
        by_dpi.setdefault(int(p.get("render_dpi") or dpi), []).append(p["page_no"])
    chunks = [(page_dpi, *c) for page_dpi, pages in sorted(by_dpi.items()) for c in _ranges(pages)]
    if chunks:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda c: _render_range(pdf, c[1], c[2], out_dir, c[0]), chunks))
    sidecar.write_text(json.dumps({"params": params, "pdf": pdf.name, "pdf_sha256": pdf_sha256}, indent=1) + "\n")
    rows = []
    for p in probe["pages"]:
        n = p["page_no"]
        png = out_dir / f"p{n:04d}.png"
        row = {
            "page_id": page_id(pdf_sha256, n), "pdf_sha256": pdf_sha256, "file_num": file_num, "pdf_name": pdf.name,
            "page_no": n, "n_pages": probe["n_pages"], "width_pt": p.get("width_pt"), "height_pt": p.get("height_pt"),
            "pdf_rotate": p.get("rotate", 0), "dpi": int(p.get("render_dpi") or dpi),
            "page_size_in": p.get("page_size_in"), "large_format": bool(p["large_format"]),
            "large_format_reason": p["large_format"], "text_layer_kind": p["text_layer_kind"],
            "pdf_text_layer_kind": probe["text_layer_kind"], "scanned": p["scanned"], "scan_ppi": p["scan_ppi"],
            "text_chars": p["text_chars"], "rendered": png.is_file(),
        }
        if png.is_file():
            with Image.open(png) as im:
                row.update(width_px=im.width, height_px=im.height)
            row.update(image_path=str(png.relative_to(PATHS.data)) if png.is_relative_to(PATHS.data) else str(png),
                       image_sha256=sha256_file(png))
        else:
            row.update(width_px=None, height_px=None, image_path=None, image_sha256=None)
        rows.append(row)
    log(f"  {file_num:<12} {pdf.name[:60]:<60} {probe['n_pages']:>4} pages, rendered {sum(r['rendered'] for r in rows)}, "
        f"large-format skipped {sum(r['large_format'] for r in rows)}")
    return rows


def phase1_documents() -> list[dict[str, Any]]:
    from .fetch import documents_by_file
    from .select import read_selection, selected_files

    sel = read_selection()
    if not sel.get("locked"):
        raise HeldOutError("the split is not locked; run `lr lock-heldout` first")
    from .select import enabled_files

    docs = documents_by_file(PATHS.raw)
    enabled = set(enabled_files(sel))
    out = []
    for n in selected_files(sel, phase1_only=True):
        if n not in docs:
            if n in enabled:
                # an enabled file that is still downloading is skipped this pass, not an error: the next
                # `lr render` picks it up once its documents are on disk
                continue
            raise FileNotFoundError(f"{n} has no fetched documents; run `lr fetch`")
        out.extend(docs[n])
    return out


def stage_render(log: Callable[[str], None] = print, workers: int = 4) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in phase1_documents():
        pdf = PATHS.raw / d["path"]
        probe = probe_cached(pdf, d["sha256"], PATHS.out / "pdfprobe")
        rows.extend(render_pdf(pdf, d["sha256"], d["file_num"], PATHS.pages, probe, workers=workers, log=log))
    # keep columns other stages added to rows that still exist
    old = {r["page_id"]: r for r in read_pages()}
    merged = []
    for r in rows:
        prev = old.get(r["page_id"], {})
        if prev.get("image_sha256") == r.get("image_sha256"):
            merged.append({**prev, **r})
        else:
            merged.append(r)
    write_pages(merged)
    log(f"pages.parquet: {len(merged)} pages ({sum(r['rendered'] for r in merged)} rendered)")
    return merged
