"""Report text about the cell itself, for benchmark v3: the information the fitted model lacks, with the outcome
taken out.

Benchmark v2's passages could not test the hypothesis that an LLM reading the assessment record ranks cells
better than a fitted model: the 10 km blind-list removed every file near the cell, so all 1,254 passages came
from 10 to 40 km away and described the region, never the ground. Here the text comes from the reports on the
cell, and the blind-list is replaced by redaction:

1. **Files.** The report files whose reported holes lie within `radius_km` of the cell centre (5 km where there
   are none), ranked by the number of those holes, then by the nearest; at most `files_per_cell`. The same rule
   for every stratum. Deposit cells have about four times the reports of their matched negatives, so volume is
   capped, not proportional.
2. **Pages.** The first `max_pages` pages of each report document (the body: summary, geology, the drilling
   described; appendices, logs and certificates come later), from the text layer where it has one and from
   Apple Vision on a rendered page where it has none. Cached per document.
3. **Redaction.** Split into sentences; drop every sentence that states an outcome (mineralisation, U3O8, ore,
   grade, intercepts, radioactivity, anomalies, discoveries, deposits, resources, assays); replace every number
   with `[n]`; scrub company, property, deposit, place and hole names. What is left is description: alteration,
   structure, host rocks, the cover.
4. **Retrieval.** Sentences are grouped into passages of about `passage_chars` characters; one BM25 query per
   evidence family (alteration, structure, host rock, cover) picks passages in turn until `passages` are chosen.
   Retrieval runs on the redacted text, so it cannot prefer a passage for what was taken out of it.

Two views are written: **redacted**, the one arms read, and **raw** (names scrubbed, nothing else), kept for the
leak audit only. A third, **swapped** (each cell given another cell's redacted passages), is made at run time.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..paths import PATHS
from .spec import TextSpec

PAGES_DIR = PATHS.data / "celltext" / "pages"

#: a sentence that states or points at an outcome is dropped whole; description survives
OUTCOME = re.compile(
    r"(?i)\b(?:mineraliz\w*|mineralis\w*|u3o8|u308|u3 ?o8|pitchblende\w*|uraninite\w*|coffinite\w*|yellowcake|"
    r"ores?|orebod\w*|ore-bod\w*|grades?|graded|high-grade|low-grade|intersect\w*|intercept\w*|radioactiv\w*|"
    r"radiometric\w*|anomal\w*|cps|counts? per|scintillomet\w*|spectromet\w*|gamma\w*|probed?|probing|geiger|"
    r"discover\w*|deposits?|showings?|occurrences?|economic\w*|resources?|reserves?|assay\w*|enrich\w*|"
    r"elevated|significant|encouraging|hot)\b")
#: any token with a digit in it: a number, a grid line or conductor name (L4, A2), a hole (2O7), a figure (3A),
#: an OCR slip (Fal1s). All become [n]; a digit is a number the gate cannot check or a label for ground
NUMBER = re.compile(r"[A-Za-z]*\d[\w.,:/+'-]*")
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")
WORD = re.compile(r"[a-z]{3,}")

FAMILY_QUERIES: dict[str, str] = {
    "alteration": "alteration altered clay illite chlorite kaolinite dravite sudoite bleached bleaching hematite "
                  "limonite silicification desilicification sericite argillic clay-altered",
    "structure": "fault faulted shear sheared fracture fractured breccia brecciated graphite graphitic conductor "
                 "structure structural offset displacement mylonite gouge",
    "host": "basement pelitic pelite gneiss paragneiss metasediment pegmatite granite calc-silicate graphitic "
            "garnet cordierite regolith paleoweathered lithology",
    "cover": "sandstone unconformity conglomerate quartz arenite overburden till glacial drift bleached sandstone "
             "manitou falls read formation depth",
}


# ---------------------------------------------------------------- files per cell


def hole_table() -> Any:
    """Every provincial drillhole that names the assessment file it was reported in, in grid metres."""
    import pandas as pd
    from pyproj import Transformer

    holes = json.loads((PATHS.index / "geods_holes.geojson").read_text())["features"]
    tr = Transformer.from_crs(4326, 2957, always_xy=True)
    rows = []
    for f in holes:
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        fn = (p.get("TEMP_ASSMNT_FILE_NUM") or "").strip()
        if not fn or fn.upper() == "SEDAR" or g.get("type") != "Point":
            continue
        x, y = tr.transform(*g["coordinates"][:2])
        rows.append((fn, x, y, str(p.get("HOLE_NAME") or "").strip()))
    return pd.DataFrame(rows, columns=["file", "x", "y", "hole"])


def cell_files(centres: dict[str, tuple[float, float]], spec: TextSpec, holes: Any = None
               ) -> dict[str, list[tuple[str, float]]]:
    """Per cell id, its report files and the distance in km from the cell centre to each file's nearest hole:
    within `radius_km`, else `fallback_km`; ranked by holes in reach, then nearest; at most `files_per_cell`."""
    holes = hole_table() if holes is None else holes
    out: dict[str, list[tuple[str, float]]] = {}
    for cid, (cx, cy) in centres.items():
        d = np.hypot(holes["x"].to_numpy() - cx, holes["y"].to_numpy() - cy)
        picked: list[tuple[str, float]] = []
        for radius in (spec.radius_km, spec.fallback_km):
            near = holes[d <= radius * 1000].assign(d=d[d <= radius * 1000])
            if near.empty:
                continue
            g = near.groupby("file").agg(n=("d", "size"), dmin=("d", "min")).sort_values(
                ["n", "dmin"], ascending=[False, True])
            picked = [(str(f), round(float(r.dmin) / 1000, 1)) for f, r in g.head(spec.files_per_cell).iterrows()]
            break
        out[cid] = picked
    return out


# ---------------------------------------------------------------- pages


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _text_layer(pdf: Path, last: int) -> dict[int, str]:
    try:
        out = subprocess.run(["pdftotext", "-layout", "-f", "1", "-l", str(last), str(pdf), "-"],
                             capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return {i: chunk for i, chunk in enumerate(out.stdout.split("\f"), start=1) if chunk.strip()}


def _page_count(pdf: Path) -> int:
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.M)
    return int(m.group(1)) if m else 0


def _ocr_page(pdf: Path, page: int) -> str:
    """Apple Vision over one page rendered at 150 dpi, lines top to bottom."""
    from PIL import Image

    from ..ocr import vision_available, vision_lines

    if not vision_available():
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        stem = Path(tmp) / "p"
        subprocess.run(["pdftoppm", "-r", "150", "-gray", "-png", "-f", str(page), "-l", str(page), "-singlefile",
                        str(pdf), str(stem)], capture_output=True, timeout=180, check=False)
        png = stem.with_suffix(".png")
        if not png.is_file():
            return ""
        with Image.open(png) as im:
            lines = vision_lines(im.convert("RGB"))
    lines.sort(key=lambda w: (round(float(w.get("y0") or 0.0), 3), float(w.get("x0") or 0.0)))
    return "\n".join(str(w.get("text") or "").strip() for w in lines).strip()


def document_pages(pdf: Path, max_pages: int, root: Path = PAGES_DIR) -> dict[str, Any]:
    """The first `max_pages` pages of one PDF as text, each marked text_layer or ocr; cached by document hash."""
    sha = _sha256(pdf)
    cache = root / f"{sha}.json"
    if cache.is_file():
        got = json.loads(cache.read_text())
        if int(got.get("max_pages", 0)) >= max_pages:
            return got
    n = min(_page_count(pdf), max_pages)
    layer = _text_layer(pdf, n) if n else {}
    pages = []
    for i in range(1, n + 1):
        text = layer.get(i, "")
        if len(re.sub(r"\s", "", text)) >= 200:
            pages.append({"page": i, "source": "text_layer", "text": text})
            continue
        ocr = _ocr_page(pdf, i)
        if len(re.sub(r"\s", "", ocr)) >= 40:
            pages.append({"page": i, "source": "ocr", "text": ocr})
    out = {"doc_sha256": sha, "name": pdf.name, "max_pages": max_pages, "pages": pages}
    root.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out


# ---------------------------------------------------------------- redaction and passages


def sentences(text: str) -> list[str]:
    flat = re.sub(r"[ \t]+", " ", re.sub(r"-\n(?=[a-z])", "", text))
    flat = re.sub(r"\s*\n\s*", " ", flat).strip()
    return [s.strip() for s in SENTENCE.split(flat) if s.strip()]


#: the small words a sentence has and a table of contents, a plate list or a log header does not
FUNCTION_WORDS = frozenset("the a an of and or in on at to from by with is are was were be been as that this "
                           "which these those it its into over under between within for".split())


def prose(s: str) -> bool:
    """A sentence, not a table row, a heading, a plate list or OCR noise: enough words, mostly letters, few
    digits, and a real share of the small words sentences are made of."""
    letters = sum(c.isalpha() for c in s)
    digits = sum(c.isdigit() for c in s)
    words = re.findall(r"[A-Za-z]+", s)
    if len(words) < 8 or letters < 0.6 * max(len(s), 1) or digits > 0.1 * max(len(s), 1):
        return False
    return sum(w.lower() in FUNCTION_WORDS for w in words) >= 0.15 * len(words)


def redact(sentence: str, scrub: Callable[[str], str]) -> str | None:
    """A sentence as an arm may read it, or None when it states an outcome."""
    # a unit glued to a number ("1000cps") is only a word once the digits are blanked
    if OUTCOME.search(sentence) or OUTCOME.search(re.sub(r"\d+", " ", sentence)):
        return None
    return scrub(NUMBER.sub("[n]", sentence))


def chunks(sents: list[str], size: int) -> list[str]:
    """Consecutive sentences grouped into passages of about `size` characters."""
    out, cur = [], ""
    for s in sents:
        if cur and len(cur) + 1 + len(s) > size:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def cell_passages(docs: list[dict[str, Any]], spec: TextSpec, scrub: Callable[[str], str]
                  ) -> dict[str, list[dict[str, Any]]]:
    """Candidate passages for one cell, in both views, before retrieval: each doc is (pages, distance_km).
    Every candidate carries its redacted and its raw text, its page and its distance."""
    out: list[dict[str, Any]] = []
    for doc in docs:
        for page in doc["pages"]:
            raw_sents = [s for s in sentences(page["text"]) if prose(s)]
            kept = [(s, redact(s, scrub)) for s in raw_sents]
            red = [r for _s, r in kept if r]
            for text in chunks(red, spec.passage_chars):
                if len(text) >= spec.min_chars:
                    out.append({"page": page["page"], "distance_km": doc["distance_km"], "text": text,
                                "source": {"file": doc["file"], "doc_sha256": doc["doc_sha256"],
                                           "page": page["page"], "ocr": page["source"] == "ocr"}})
            for text in chunks([scrub(s) for s in raw_sents], spec.passage_chars):
                if len(text) >= spec.min_chars:
                    out.append({"page": page["page"], "distance_km": doc["distance_km"], "text": text, "raw": True,
                                "source": {"file": doc["file"], "doc_sha256": doc["doc_sha256"],
                                           "page": page["page"], "ocr": page["source"] == "ocr"}})
    return {"redacted": [c for c in out if not c.get("raw")], "raw": [c for c in out if c.get("raw")]}


class BM25:
    """Okapi BM25 over a fixed collection, k1 1.2, b 0.75: small enough to read."""

    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75) -> None:
        self.tokens = [WORD.findall(d.lower()) for d in docs]
        self.k1, self.b = k1, b
        self.avg = float(np.mean([len(t) for t in self.tokens])) if self.tokens else 0.0
        df: Counter[str] = Counter()
        for t in self.tokens:
            df.update(set(t))
        n = len(docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def scores(self, query: str) -> np.ndarray:
        q = WORD.findall(query.lower())
        out = np.zeros(len(self.tokens))
        for i, t in enumerate(self.tokens):
            tf = Counter(t)
            norm = self.k1 * (1 - self.b + self.b * len(t) / (self.avg or 1.0))
            out[i] = sum(self.idf.get(w, 0.0) * tf[w] * (self.k1 + 1) / (tf[w] + norm) for w in q if tf[w])
        return out


def select(candidates: list[dict[str, Any]], bm25: BM25, offset: int, spec: TextSpec) -> list[dict[str, Any]]:
    """Passages in turn from each family's query, best first, until `passages` are chosen; a passage scoring
    nothing for any family is never chosen, and the same text is never chosen twice."""
    if not candidates:
        return []
    ranked = {}
    for fam, q in FAMILY_QUERIES.items():
        s = bm25.scores(q)[offset:offset + len(candidates)]
        ranked[fam] = [int(i) for i in np.argsort(-s, kind="mergesort") if s[i] > 0]
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    while len(chosen) < spec.passages and any(ranked.values()):
        for fam in FAMILY_QUERIES:
            while ranked[fam]:
                i = ranked[fam].pop(0)
                key = candidates[i]["text"]
                if key in seen:
                    continue
                seen.add(key)
                chosen.append({**candidates[i], "family": fam})
                break
            if len(chosen) >= spec.passages:
                break
    return chosen


def passage_rows(chosen: list[dict[str, Any]], numbers_allowed: bool) -> tuple[list[dict[str, Any]],
                                                                              list[dict[str, Any]]]:
    """The passages an arm reads (no file, no document) and their sources, kept apart for the audit."""
    rows, sources = [], []
    for i, c in enumerate(chosen, start=1):
        pid = f"p-{i:02d}"
        rows.append({"passage_id": pid, "family": c["family"], "tier": "page", "page": c["page"],
                     "distance_km": c["distance_km"], "quotable": True, "numbers_allowed": numbers_allowed,
                     "text": c["text"]})
        sources.append({"passage_id": pid, **c["source"]})
    return rows, sources


def swapped(passages_by_bench: dict[str, list[dict[str, Any]]], seed: int) -> dict[str, str]:
    """A fixed derangement among the cells that have passages: which cell's passages each cell is given in the
    placebo view. No cell keeps its own."""
    ids = sorted(b for b, p in passages_by_bench.items() if p)
    if len(ids) < 2:
        return {}
    rng = np.random.default_rng(seed)
    perm = list(ids)
    while True:
        rng.shuffle(perm)
        if all(a != b for a, b in zip(ids, perm, strict=True)):
            return dict(zip(ids, perm, strict=True))


def build_texts(cells: list[tuple[str, str]], spec: TextSpec, con: Any, forbidden: set[str],
                log: Callable[[str], None] = print) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Per bench id: `redacted` and `raw` passage rows and their `sources`, for (bench id, cell id) pairs.

    Documents come from what is on disk (`prospect.corpus.documents_on_disk`); a cell whose files have no
    document on disk gets no passages, and the build reports how many. BM25's document frequencies are taken
    over every cell's candidates together, so a rare word counts the same everywhere."""
    from ..prospect.corpus import documents_on_disk
    from .pack import hole_names, scrub_text

    centres = {cid: (float(x), float(y)) for cid, x, y in con.execute(
        "select cell_id, cx, cy from derived.cell where cell_id in (" + ",".join("?" * len(cells)) + ")",
        [c for _b, c in cells]).fetchall()}
    holes = hole_table()
    files = cell_files(centres, spec, holes)
    on_disk = documents_on_disk(log=lambda _m: None)
    names = set(forbidden) | hole_names(con)
    names |= {h for h in holes["hole"] if len(h) >= 4}
    scrub = lambda t: scrub_text(t, names)  # noqa: E731
    pages_cache: dict[str, dict[str, Any]] = {}
    cands: dict[str, dict[str, list[dict[str, Any]]]] = {}
    no_docs = 0
    for bid, cid in cells:
        docs = []
        for file_num, dist in files.get(cid, []):
            for d in on_disk.get(file_num, []):
                key = str(d["path"])
                if key not in pages_cache:
                    pages_cache[key] = document_pages(Path(d["path"]), spec.max_pages)
                got = pages_cache[key]
                docs.append({"file": file_num, "doc_sha256": got["doc_sha256"], "pages": got["pages"],
                             "distance_km": dist})
        no_docs += bool(files.get(cid)) and not docs
        cands[bid] = cell_passages(docs, spec, scrub)
    log(f"  cell text: {sum(1 for f in files.values() if f)} of {len(cells)} cells have report files in reach, "
        f"{no_docs} of those have no document on disk")
    out: dict[str, dict[str, list[dict[str, Any]]]] = {bid: {"redacted": [], "raw": [], "sources": []}
                                                      for bid, _c in cells}
    for view, numbers in (("redacted", False), ("raw", True)):
        flat = [c for bid, _c in cells for c in cands[bid][view]]
        bm25 = BM25([c["text"] for c in flat])
        offset = 0
        for bid, _c in cells:
            n = len(cands[bid][view])
            rows, sources = passage_rows(select(cands[bid][view], bm25, offset, spec), numbers)
            out[bid][view] = rows
            if view == "redacted":
                out[bid]["sources"] = sources
            offset += n
    return out


__all__ = ["BM25", "build_texts", "hole_table", "FAMILY_QUERIES", "OUTCOME", "TextSpec", "cell_files", "cell_passages", "chunks",
           "document_pages", "passage_rows", "redact", "select", "sentences", "swapped"]
