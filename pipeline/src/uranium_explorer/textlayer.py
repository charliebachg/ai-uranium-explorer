"""Embedded text layers: read them with boxes, and decide whether a page's layer can be trusted.

Rule from the plan: embedded text is never trusted without an OCR comparison. The 1980 drill log's
scanner OCR layer reads "L2L.-7" where the page prints 121.7; a born-digital lab certificate's layer is
exact. So per page:

- agreement: token similarity between the text layer and our own OCR, restricted to tokens that matter
  (anything with a digit, and words of at least 4 letters), matched by box overlap, counted in both
  directions so a layer that drops numbers scores as badly as one that garbles them. Numbers must match
  exactly; words use a character-level ratio.
- trusted: agreement >= 0.90 AND a born-digital signature (text drawn by the authoring program, no
  scanner or OCR producer, no page-covering scan image). An OCR layer that happens to agree is still not
  trusted, because its errors are silent elsewhere on the page.

Boxes are normalised 0..1 on the upright page (top-left origin), the same frame as OCR words.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

TRUST_THRESHOLD = 0.90
MIN_ELIGIBLE = 5

def rotate_box(box: tuple[float, float, float, float], rot_ccw: int) -> tuple[float, float, float, float]:
    """Map a normalised box (x0, y0, x1, y1; top-left origin) through a counter-clockwise image rotation.

    PIL's `Image.rotate(angle, expand=True)` turns the picture counter-clockwise; a point (x, y) moves to
    (y, 1 - x) for 90, (1 - x, 1 - y) for 180 and (1 - y, x) for 270."""
    rot = rot_ccw % 360
    x0, y0, x1, y1 = box
    if rot == 0:
        return box
    if rot == 90:
        pts = [(y0, 1 - x0), (y1, 1 - x1)]
    elif rot == 180:
        pts = [(1 - x0, 1 - y0), (1 - x1, 1 - y1)]
    elif rot == 270:
        pts = [(1 - y0, x0), (1 - y1, x1)]
    else:
        raise ValueError(f"rotation must be a multiple of 90, got {rot_ccw}")
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def pdftotext_words(pdf: Path, page_rotations: dict[int, int] | None = None,
                    first: int | None = None, last: int | None = None) -> dict[int, list[dict[str, Any]]]:
    """Words with boxes from `pdftotext -tsv`, per page number, normalised to the rendered page.

    Why TSV and not -bbox-layout: poppler 26.04 aborts (std::out_of_range while writing the HTML meta
    header) on some born-digital PDFs, e.g. the 2022 SRC certificate; the TSV writer is unaffected and
    carries block, line and word numbers. Poppler reports the unrotated page size but places words in the
    rotated frame, so width and height are swapped for pages with /Rotate 90 or 270."""
    argv = ["pdftotext", "-tsv"]
    if first:
        argv += ["-f", str(first)]
    if last:
        argv += ["-l", str(last)]
    out = subprocess.run([*argv, str(pdf), "-"], capture_output=True, timeout=900)
    if out.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {out.stderr.decode(errors='replace')[:300]}")
    pages: dict[int, list[dict[str, Any]]] = {}
    sizes: dict[int, tuple[float, float]] = {}
    line_ids: dict[int, dict[tuple[str, str, str], int]] = {}
    for raw in out.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        parts = raw.split("\t")
        if len(parts) < 12:
            continue
        level, page_s = parts[0], parts[1]
        try:
            page_no = int(page_s)
            left, top, width, height = (float(v) for v in parts[6:10])
        except ValueError:
            continue
        if level == "1":
            w, h = width, height
            if (page_rotations or {}).get(page_no, 0) % 180 == 90:
                w, h = h, w
            sizes[page_no] = (w, h)
            pages.setdefault(page_no, [])
            continue
        if level != "5" or page_no not in sizes:
            continue
        text = "\t".join(parts[11:])
        if not text.strip():
            continue
        w, h = sizes[page_no]
        ids = line_ids.setdefault(page_no, {})
        key = (parts[2], parts[3], parts[4])
        if key not in ids:
            ids[key] = len(ids)
        words = pages[page_no]
        words.append({
            "text": text,
            "x0": max(0.0, left / w), "y0": max(0.0, top / h),
            "x1": min(1.0, (left + width) / w), "y1": min(1.0, (top + height) / h),
            "line_id": ids[key], "word_no": len(words),
        })
    return pages


# ---------------------------------------------------------------- agreement


def token_kind(text: str) -> str | None:
    """Which tokens count: anything with a digit and at least two characters, and words of 4+ letters.

    Single characters are excluded because neither Apple engine reliably detects an isolated "7" in a
    sparse table (measured on the 2022 SRC certificate), so scoring them would blame the text layer for
    an OCR limitation. Corrupted layers garble multi-character tokens too ("L2L.-7", "LZ.5")."""
    t = text.strip()
    if any(ch.isdigit() for ch in t) and len(t.strip(".,;:()[]|'\"")) >= 2:
        return "num"
    if sum(ch.isalpha() for ch in t) >= 4:
        return "word"
    return None


def ocr_garbage(text: str) -> bool:
    """OCR output outside the Latin range: these documents are English, so such a token is an OCR failure."""
    return any(ord(ch) > 0x2E7F for ch in text)


def _norm_num(t: str) -> str:
    t = t.strip().strip(".,;:()[]{}|'\"")
    return re.sub(r"[^0-9A-Za-z.+\-/%<>]", "", t)


def _norm_word(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    ix = max(0.0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]))
    iy = max(0.0, min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    area = min((a["x1"] - a["x0"]) * (a["y1"] - a["y0"]), (b["x1"] - b["x0"]) * (b["y1"] - b["y0"]))
    return inter / area if area > 0 else 0.0


def _similarity(text: str, kind: str, candidates: list[dict[str, Any]]) -> float:
    if not candidates:
        return 0.0
    ordered = sorted(candidates, key=lambda c: c["x0"])
    options = [c["text"] for c in ordered] + ["".join(c["text"] for c in ordered)]
    for c in ordered:  # a vision line may hold several words: each counts as an option
        parts = c["text"].split()
        if len(parts) > 1:
            options.extend(parts)
    if kind == "num":
        target = _norm_num(text)
        return 1.0 if any(_norm_num(o) == target for o in options) else 0.0
    target = _norm_word(text)
    return max(fuzz.ratio(target, _norm_word(o)) for o in options) / 100.0


def _near(a: dict[str, Any], b: dict[str, Any], min_overlap: float) -> bool:
    if _overlap(a, b) >= min_overlap:
        return True
    cx, cy = (a["x0"] + a["x1"]) / 2, (a["y0"] + a["y1"]) / 2
    return b["x0"] <= cx <= b["x1"] and b["y0"] <= cy <= b["y1"]


def split_line_words(line: dict[str, Any]) -> list[dict[str, Any]]:
    """Approximate word boxes inside a Vision line box, proportional to character positions."""
    text = line["text"]
    n = max(len(text), 1)
    w = line["x1"] - line["x0"]
    return [{**line, "text": m.group(0), "x0": line["x0"] + w * m.start() / n, "x1": line["x0"] + w * m.end() / n}
            for m in re.finditer(r"\S+", text)]


def _score(src: list[dict[str, Any]], dst: list[dict[str, Any]],
           min_overlap: float) -> tuple[list[float], list[float], int]:
    sims, num_sims, missing = [], [], 0
    for w in src:
        kind = token_kind(w["text"])
        if kind is None:
            continue
        cands = [d for d in dst if _near(w, d, min_overlap)]
        if not cands:
            missing += 1
            continue
        s = _similarity(w["text"], kind, cands)
        sims.append(s)
        if kind == "num":
            num_sims.append(s)
    return sims, num_sims, missing


def agreement(layer_words: list[dict[str, Any]], ocr_words: list[dict[str, Any]],
              vision_lines: list[dict[str, Any]] | None = None, min_overlap: float = 0.3) -> dict[str, Any]:
    """Agreement between a text layer and OCR on one page (all boxes normalised, upright frame).

    - Both engines count: a layer token agrees if either LiveText or the Vision line over it matches, and
      the OCR side is Vision's words plus LiveText tokens where Vision read nothing. The engines miss
      different cells on sparse tables, so neither alone is a fair reference.
    - Scored both ways over matched positions only. A token the other side has nothing for is counted in
      `layer_missing` / `ocr_missing` instead of scored as a disagreement, because an OCR miss is not
      evidence about the layer (and OCR cannot prove the layer invented text).
    - Numbers must match exactly; words score on a character ratio.
    """
    vision_lines = [ln for ln in (vision_lines or []) if not ocr_garbage(ln["text"])]
    ocr_words = [t for t in ocr_words if not ocr_garbage(t["text"])]
    a, an, a_missing = _score(layer_words, ocr_words + vision_lines, min_overlap)
    vision_words = [vw for ln in vision_lines for vw in split_line_words(ln)]
    extra = [t for t in ocr_words if not any(_near(t, ln, min_overlap) for ln in vision_lines)]
    b, bn, b_missing = _score(vision_words + extra, layer_words, min_overlap)
    sims, nums = a + b, an + bn
    n = len(sims)
    return {
        "agreement": round(sum(sims) / n, 4) if n >= MIN_ELIGIBLE else None,
        "numeric_agreement": round(sum(nums) / len(nums), 4) if len(nums) >= MIN_ELIGIBLE else None,
        "eligible_tokens": n,
        "numeric_tokens": len(nums),
        "layer_eligible": len(a),
        "ocr_eligible": len(b),
        "ocr_missing": a_missing,     # layer tokens no engine read: OCR misses, not layer errors
        "layer_missing": b_missing,   # text OCR read that the layer does not have
    }


def trusted(agree: float | None, page_kind: str, pdf_ocr_signature: bool) -> bool:
    return agree is not None and agree >= TRUST_THRESHOLD and page_kind == "born_digital" and not pdf_ocr_signature
