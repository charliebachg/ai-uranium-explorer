"""`ue ocr`: Apple Vision OCR per rendered page, rotation choice, text-layer comparison, words.parquet.

Engines (both through ocrmac 1.0.1's Apple frameworks, no network):
- "livetext" (VisionKit's VKCImageAnalyzer): word tokens with tight boxes and line grouping, but no
  confidence (ocrmac reports 1.0). We call the analyzer directly (the same calls ocrmac makes) so each token
  keeps its line id, and we wait for completion instead of ocrmac's fixed 10 s run loop.
- "vision" (VNRecognizeTextRequest, accurate): line boxes with real confidences. Each livetext token also
  gets the confidence of the vision line it overlaps most, as a usable proxy.

Rotation: a page is read at 0 degrees first. If the vision lines look wrong (mean confidence below 0.8,
more than a quarter of the characters in vertical boxes, or almost no text), the page is also read turned
90 and 270 degrees counter-clockwise and the reading with the most confident horizontal text wins (it
must beat 0 degrees clearly). Boxes are stored on the upright page, top-left origin, normalised 0..1;
`rotation_ccw` in the pages table says how to turn the rendered PNG to get that upright page.

Cache: one JSON per page keyed by sha256(image sha256, engine version); a re-run only reads the cache.
"""

from __future__ import annotations

import io
import json
import multiprocessing as mp
import platform
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from .ids import sha256_json
from .paths import PATHS
from .textlayer import agreement, pdftotext_words, rotate_box, trusted

OCR_VERSION = "ocr/v1"
ROTATION_TRY = (90, 270)
LOW_CONF = 0.8
VERTICAL_SHARE = 0.25
FEW_CHARS = 30
SWITCH_MARGIN = 1.15


def engine_version() -> str:
    try:
        from importlib.metadata import version

        ocrmac_v = version("ocrmac")
    except Exception:  # pragma: no cover
        ocrmac_v = "?"
    return f"{OCR_VERSION}|ocrmac {ocrmac_v}|macOS {platform.mac_ver()[0]}"


def vision_available() -> bool:
    try:
        import Vision  # noqa: F401
        from ocrmac import ocrmac  # noqa: F401

        return platform.system() == "Darwin"
    except Exception:
        return False


# ---------------------------------------------------------------- engines


def vision_lines(image: Image.Image) -> list[dict[str, Any]]:
    from ocrmac import ocrmac as om

    out = []
    for i, (text, conf, (x, y, w, h)) in enumerate(om.text_from_image(image, recognition_level="accurate")):
        # Vision boxes have a bottom-left origin
        out.append({"text": text, "conf": float(conf), "x0": float(x), "y0": float(1 - y - h),
                    "x1": float(x + w), "y1": float(1 - y), "line_id": i})
    return out


def livetext_tokens(image: Image.Image, timeout_s: float = 90.0) -> list[dict[str, Any]]:
    import objc
    from AppKit import NSData, NSImage
    from CoreFoundation import CFRunLoopGetCurrent, CFRunLoopRunInMode, CFRunLoopStop, kCFRunLoopDefaultMode
    from ocrmac import ocrmac as om  # noqa: F401  (registers the analyzer's block signatures)

    tokens: list[dict[str, Any]] = []
    state: dict[str, Any] = {"done": False, "error": None}
    with objc.autorelease_pool():
        buf = io.BytesIO()
        image.save(buf, format="TIFF")
        data = buf.getvalue()
        ns_image = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(data, len(data)))
        analyzer = objc.lookUpClass("VKCImageAnalyzer").alloc().init()
        request = objc.lookUpClass("VKCImageAnalyzerRequest").alloc().initWithImage_requestType_(ns_image, 1)

        def handler(analysis, error):  # noqa: ANN001
            if error:
                state["error"] = str(error)
            else:
                for li, line in enumerate(analysis.allLines() or []):
                    for child in line.children():
                        bb = child.quad().boundingBox()
                        # LiveText boxes have a top-left origin (ocrmac flips them to match Vision)
                        x, y, w, h = bb.origin.x, bb.origin.y, bb.size.width, bb.size.height
                        tokens.append({"text": str(child.string()), "x0": float(x), "y0": float(y),
                                       "x1": float(x + w), "y1": float(y + h), "line_id": li})
            state["done"] = True
            CFRunLoopStop(CFRunLoopGetCurrent())

        analyzer.processRequest_progressHandler_completionHandler_(request, lambda p: None, handler)
        t0 = time.monotonic()
        while not state["done"] and time.monotonic() - t0 < timeout_s:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.25, False)
    if state["error"]:
        raise RuntimeError(f"LiveText failed: {state['error']}")
    if not state["done"]:
        raise TimeoutError(f"LiveText did not finish in {timeout_s} s")
    return tokens


# ---------------------------------------------------------------- rotation


def reading_quality(lines: list[dict[str, Any]], width_px: int, height_px: int) -> dict[str, float]:
    chars = sum(len(ln["text"].strip()) for ln in lines)
    vert = 0
    score = 0.0
    conf_w = 0.0
    for ln in lines:
        n = len(ln["text"].strip())
        wpx = (ln["x1"] - ln["x0"]) * width_px
        hpx = (ln["y1"] - ln["y0"]) * height_px
        conf_w += ln["conf"] * n
        if n >= 3 and hpx > 1.5 * wpx:
            vert += n
        elif n >= 2:
            score += ln["conf"] * n
    return {"chars": chars, "mean_conf": round(conf_w / chars, 4) if chars else 0.0,
            "vertical_share": round(vert / chars, 4) if chars else 0.0, "score": round(score, 2)}


def needs_rotation_check(q: dict[str, float]) -> bool:
    return q["chars"] < FEW_CHARS or q["mean_conf"] < LOW_CONF or q["vertical_share"] > VERTICAL_SHARE


def choose_rotation(image: Image.Image) -> tuple[int, list[dict[str, Any]], dict[str, Any]]:
    lines0 = vision_lines(image)
    q0 = reading_quality(lines0, image.width, image.height)
    tried = {"0": q0}
    best = (0, lines0, q0)
    if needs_rotation_check(q0):
        for rot in ROTATION_TRY:
            turned = image.rotate(rot, expand=True)
            lines = vision_lines(turned)
            q = reading_quality(lines, turned.width, turned.height)
            tried[str(rot)] = q
            if q["score"] > best[2]["score"] * SWITCH_MARGIN + 5:
                best = (rot, lines, q)
    return best[0], best[1], tried


# ---------------------------------------------------------------- per page (worker)


def _attach_conf(tokens: list[dict[str, Any]], lines: list[dict[str, Any]]) -> None:
    for t in tokens:
        cx, cy = (t["x0"] + t["x1"]) / 2, (t["y0"] + t["y1"]) / 2
        best, best_d = None, 1e9
        for ln in lines:
            if ln["x0"] - 0.005 <= cx <= ln["x1"] + 0.005 and ln["y0"] - 0.005 <= cy <= ln["y1"] + 0.005:
                d = abs(cy - (ln["y0"] + ln["y1"]) / 2)
                if d < best_d:
                    best, best_d = ln, d
        t["conf"] = best["conf"] if best else None


def ocr_image(image_path: Path, image_sha256: str, cache_path: Path) -> dict[str, Any]:
    """OCR one page image (both engines, rotation choice); cached as JSON. Runs in worker processes."""
    if cache_path.is_file():
        return json.loads(cache_path.read_text())
    t0 = time.monotonic()
    with Image.open(image_path) as im:
        image = im.convert("L")
    rot, lines, tried = choose_rotation(image)
    upright = image.rotate(rot, expand=True) if rot else image
    tokens = livetext_tokens(upright)
    _attach_conf(tokens, lines)
    rec = {
        "image_sha256": image_sha256, "engine_version": engine_version(), "rotation_ccw": rot,
        "rotation_tried": tried, "upright_px": [upright.width, upright.height],
        "vision_lines": lines, "livetext_tokens": tokens, "seconds": round(time.monotonic() - t0, 2),
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".part")
    tmp.write_text(json.dumps(rec))
    tmp.rename(cache_path)
    return rec


def cache_path_for(pdf_sha256: str, page_no: int, image_sha256: str, root: Path | None = None) -> Path:
    key = sha256_json({"image_sha256": image_sha256, "engine_version": engine_version()})
    return (root or PATHS.ocr) / pdf_sha256 / "pages" / f"p{page_no:04d}-{key[:16]}.json"


def _worker(args: tuple[str, str, str]) -> tuple[str, float]:
    image_path, image_sha, cache = args
    rec = ocr_image(Path(image_path), image_sha, Path(cache))
    return cache, rec.get("seconds", 0.0)


# ---------------------------------------------------------------- per PDF assembly


WORD_COLUMNS = ["page_no", "engine", "text", "x0", "y0", "x1", "y1", "conf", "line_id", "word_no", "rotation_ccw"]


def assemble_pdf(pdf: Path | None, pdf_sha256: str, page_rows: list[dict[str, Any]], pdf_ocr_signature: bool,
                 root: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Words for one PDF (livetext, vision, pdftotext) and the per-page OCR and text-layer columns."""
    words: list[dict[str, Any]] = []
    updates: dict[str, dict[str, Any]] = {}
    text_pages = [r for r in page_rows if r.get("rendered") and (r.get("text_chars") or 0) >= 10]
    layer: dict[int, list[dict[str, Any]]] = {}
    if pdf is not None and text_pages:
        rotations = {r["page_no"]: r.get("pdf_rotate") or 0 for r in page_rows}
        layer = pdftotext_words(pdf, rotations)
    for r in sorted(page_rows, key=lambda r: r["page_no"]):
        if not r.get("rendered"):
            continue
        cp = cache_path_for(pdf_sha256, r["page_no"], r["image_sha256"], root)
        if not cp.is_file():
            continue
        rec = json.loads(cp.read_text())
        rot = rec["rotation_ccw"]
        for i, t in enumerate(rec["livetext_tokens"]):
            words.append({"page_no": r["page_no"], "engine": "livetext", "text": t["text"], "x0": t["x0"], "y0": t["y0"],
                          "x1": t["x1"], "y1": t["y1"], "conf": t.get("conf"), "line_id": t["line_id"], "word_no": i,
                          "rotation_ccw": rot})
        for ln in rec["vision_lines"]:
            words.append({"page_no": r["page_no"], "engine": "vision", "text": ln["text"], "x0": ln["x0"], "y0": ln["y0"],
                          "x1": ln["x1"], "y1": ln["y1"], "conf": ln["conf"], "line_id": ln["line_id"],
                          "word_no": ln["line_id"], "rotation_ccw": rot})
        confs = [ln["conf"] for ln in rec["vision_lines"]]
        upd: dict[str, Any] = {
            "rotation_ccw": rot, "ocr_engine_version": rec["engine_version"], "ocr_tokens": len(rec["livetext_tokens"]),
            "ocr_lines": len(rec["vision_lines"]),
            "ocr_mean_conf": round(sum(confs) / len(confs), 4) if confs else None,
            "ocr_rotation_tried": json.dumps(rec["rotation_tried"], sort_keys=True),
            "text_layer_words": None, "text_layer_agreement": None, "text_layer_numeric_agreement": None,
            "text_layer_trusted": False,
        }
        tl = layer.get(r["page_no"], [])
        if tl:
            upright = []
            for i, w in enumerate(tl):
                x0, y0, x1, y1 = rotate_box((w["x0"], w["y0"], w["x1"], w["y1"]), rot)
                upright.append({**w, "x0": x0, "y0": y0, "x1": x1, "y1": y1})
                words.append({"page_no": r["page_no"], "engine": "pdftotext", "text": w["text"], "x0": x0, "y0": y0,
                              "x1": x1, "y1": y1, "conf": None, "line_id": w["line_id"], "word_no": i,
                              "rotation_ccw": rot})
            ag = agreement(upright, rec["livetext_tokens"], rec["vision_lines"])
            upd.update(text_layer_words=len(tl), text_layer_agreement=ag["agreement"],
                       text_layer_numeric_agreement=ag["numeric_agreement"],
                       text_layer_trusted=trusted(ag["agreement"], r.get("text_layer_kind") or "", pdf_ocr_signature))
        updates[r["page_id"]] = upd
    return words, updates


def write_words(words: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = {c: [w.get(c) for w in words] for c in WORD_COLUMNS}
    schema = pa.schema([("page_no", pa.int32()), ("engine", pa.string()), ("text", pa.string()),
                        ("x0", pa.float32()), ("y0", pa.float32()), ("x1", pa.float32()), ("y1", pa.float32()),
                        ("conf", pa.float32()), ("line_id", pa.int32()), ("word_no", pa.int32()),
                        ("rotation_ccw", pa.int16())])
    table = pa.Table.from_pydict(cols, schema=schema)
    tmp = path.with_suffix(".part")
    pq.write_table(table, tmp)
    tmp.rename(path)


def read_words(pdf_sha256: str, root: Path | None = None) -> list[dict[str, Any]]:
    path = (root or PATHS.ocr) / pdf_sha256 / "words.parquet"
    return pq.read_table(path).to_pylist() if path.is_file() else []


def stage_ocr(log: Callable[[str], None] = print, workers: int = 4) -> dict[str, Any]:
    from .fetch import documents_by_file
    from .pdfprobe import probe_cached
    from .render import guard_not_heldout, read_pages, update_pages

    if not vision_available():
        raise RuntimeError("Apple Vision (ocrmac) is not available on this machine")
    rows = [r for r in read_pages() if r.get("rendered")]
    for file_num, pdf_sha in {(r["file_num"], r["pdf_sha256"]) for r in rows}:
        guard_not_heldout(file_num, pdf_sha)
    tasks = []
    for r in rows:
        cp = cache_path_for(r["pdf_sha256"], r["page_no"], r["image_sha256"])
        if not cp.is_file():
            tasks.append((str(PATHS.data / r["image_path"]), r["image_sha256"], str(cp)))
    t0 = time.monotonic()
    log(f"OCR: {len(rows)} rendered pages, {len(tasks)} not cached, {workers} workers")
    failures: list[dict[str, str]] = []
    if tasks:
        done = 0
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            futs = {ex.submit(_worker, t): t for t in tasks}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:  # one unreadable page must not lose the whole run
                    failures.append({"image": futs[fut][0], "error": f"{type(e).__name__}: {e}"})
                    log(f"  FAILED {Path(futs[fut][0]).name}: {type(e).__name__}: {e}")
                done += 1
                if done % 25 == 0 or done == len(tasks):
                    el = time.monotonic() - t0
                    log(f"  {done}/{len(tasks)} pages, {el:.0f} s elapsed, ~{el / done * (len(tasks) - done):.0f} s left")
    docs = {d["sha256"]: d for ds in documents_by_file(PATHS.raw).values() for d in ds}
    all_updates: dict[str, dict[str, Any]] = {}
    by_pdf: dict[str, list[dict[str, Any]]] = {}
    for r in read_pages():
        by_pdf.setdefault(r["pdf_sha256"], []).append(r)
    for sha, prow in sorted(by_pdf.items()):
        d = docs.get(sha)
        pdf = PATHS.raw / d["path"] if d else None
        probe = probe_cached(pdf, sha, PATHS.out / "pdfprobe") if pdf else {"ocr_signature": True}
        words, updates = assemble_pdf(pdf, sha, prow, probe["ocr_signature"])
        write_words(words, PATHS.ocr / sha / "words.parquet")
        all_updates.update(updates)
        tl = [u for u in updates.values() if u["text_layer_agreement"] is not None]
        log(f"  {prow[0]['file_num']:<12} {prow[0]['pdf_name'][:50]:<50} words {len(words):>6}, rotated "
            f"{sum(1 for u in updates.values() if u['rotation_ccw'])}, text-layer pages {len(tl)}, trusted "
            f"{sum(1 for u in updates.values() if u['text_layer_trusted'])}")
    update_pages(all_updates)
    return {"pages": len(rows), "ocr_run": len(tasks), "failed": failures,
            "seconds": round(time.monotonic() - t0, 1)}
