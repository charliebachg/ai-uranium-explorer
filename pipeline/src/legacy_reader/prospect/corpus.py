"""`lr prospect corpus`: a searchable corpus of assessment reports, built without a single model call.

"Retrieval over heterogeneous corpora" needs a corpus. Four files read page by page is not one. This builds a
broad, shallow index beside the deep one:

* **Every uranium-tagged file with linked drillholes** (2,381 of the 5,822) gets a row from the province's own
  index: company, property, work period, the work description it filed, and the centroid of the holes it
  reported. No document is opened, so this costs nothing but a join, and it already answers "who worked this
  ground, when, and on what".
* **A few hundred of those files** are downloaded and split into page text, so a passage can be quoted with the
  file and page it came from.
* The handful already extracted page by page keep their boxes and quotes, and stay the only tier a number may
  be read from.

The page text is tier B. Not because a model produced it, but because on a scanned report the text layer is
usually somebody's OCR pass, and this project has already seen one turn 121.7 into "L2L.-7". It is searchable
evidence, never a measurement.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from ..arcgis import ArcGisClient
from ..ocr import read_words
from ..paths import PATHS
from ..select import classify_listing, probe_listing
from ..store import append_frame, connect

TOOL = "prospect/corpus"
CORPUS_DIR = PATHS.data / "corpus"
USER_AGENT = "legacy-reader/0.1 (research demo; public data only)"

#: a page with less text than this is a scan with no usable text layer; it is counted, not indexed
MIN_PAGE_CHARS = 40
#: per file, and per document, so one enormous report cannot eat the whole budget
MAX_FILE_MB = 12.0
MAX_DOC_MB = 8.0
PACE_S = 1.0


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def linked_files(log: Callable[[str], None] = print) -> pd.DataFrame:
    """Every assessment file with drillholes in the provincial layer, with where those holes are.

    The join is the province's own: GeoDS records the assessment file a hole was reported in, so a file gets a
    position without anybody reading it.
    """
    holes_path = PATHS.index / "geods_holes.geojson"
    index_path = PATHS.index / "file_index_uranium.json"
    if not holes_path.is_file() or not index_path.is_file():
        raise RuntimeError("run `lr index pull` first (geods_holes, file_index_uranium)")

    holes = json.loads(holes_path.read_text())["features"]
    by_file: dict[str, dict[str, Any]] = {}
    for f in holes:
        p = f.get("properties") or {}
        fn = (p.get("TEMP_ASSMNT_FILE_NUM") or "").strip()
        geom = f.get("geometry") or {}
        if not fn or fn.upper() == "SEDAR" or geom.get("type") != "Point":
            continue
        lon, lat = geom["coordinates"][:2]
        rec = by_file.setdefault(fn, {"file_num": fn, "lon": 0.0, "lat": 0.0, "n_holes": 0, "names": []})
        rec["lon"] += lon
        rec["lat"] += lat
        rec["n_holes"] += 1
        if p.get("HOLE_NAME") and len(rec["names"]) < 40:
            rec["names"].append(str(p["HOLE_NAME"]))

    idx = json.loads(index_path.read_text())
    records = idx if isinstance(idx, list) else idx.get("features") or idx.get("records") or []
    meta: dict[str, dict[str, Any]] = {}
    for r in records:
        a = r.get("attributes") or r
        for key in (a.get("ASSMNT_FILE_NUM"), a.get("SYSTEM_ASSMNT_FILE_NUM")):
            if key:
                meta[str(key).strip()] = a

    rows = []
    for fn, rec in by_file.items():
        a = meta.get(fn) or {}
        rows.append({
            "file_num": fn,
            "company": (a.get("CMPNY") or "").strip() or None,
            "property": (a.get("PROPRTY_PROJCT") or "").strip() or None,
            "work_period": (a.get("WRK_PERIOD") or "").strip() or None,
            "nts": (a.get("NTS_SHEET") or "").strip() or None,
            "work_description": (a.get("HSTRC_ASSMNT_WRK_DESCRPTN") or "").strip() or None,
            "lon": rec["lon"] / rec["n_holes"],
            "lat": rec["lat"] / rec["n_holes"],
            "n_holes": rec["n_holes"],
            "hole_names": ", ".join(rec["names"]) or None,
            "in_index": fn in meta,
            "retrieved_at": _now(),
        })
    df = pd.DataFrame(rows).sort_values("n_holes", ascending=False).reset_index(drop=True)
    log(f"  {len(df)} files carry drillholes; {int(df['in_index'].sum())} of them are uranium-tagged")
    return df


def in_region(df: pd.DataFrame, bbox: tuple[float, float, float, float]) -> pd.DataFrame:
    w, s, e, n = bbox
    return df[(df["lon"] >= w) & (df["lon"] <= e) & (df["lat"] >= s) & (df["lat"] <= n)].copy()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]


def page_texts(pdf: Path) -> list[tuple[int, str]]:
    """Page number and text for each page with a usable text layer, via poppler."""
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, timeout=180, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pages = []
    for i, chunk in enumerate(out.stdout.split("\f"), start=1):
        text = chunk.strip()
        if len(re.sub(r"\s", "", text)) >= MIN_PAGE_CHARS:
            pages.append((i, text))
    return pages


def ocr_page_texts(pdf_sha256: str, root: Path | None = None) -> list[tuple[int, str]]:
    """Page text from the OCR pass (Apple Vision lines, top to bottom, left to right) for a document whose text
    layer is missing or not to be trusted. Only pages the OCR actually covered come back, and only when they
    hold enough characters to be a page of text rather than a drawing with a title."""
    lines: dict[int, list[dict[str, Any]]] = {}
    for w in read_words(pdf_sha256, root=root):
        if w.get("engine") != "vision" or not w.get("text"):
            continue
        lines.setdefault(int(w["page_no"]), []).append(w)
    out = []
    for page, ws in sorted(lines.items()):
        ws.sort(key=lambda w: (round(float(w.get("y0") or 0.0), 3), float(w.get("x0") or 0.0)))
        text = "\n".join(str(w["text"]).strip() for w in ws).strip()
        if len(re.sub(r"\s", "", text)) >= MIN_PAGE_CHARS:
            out.append((page, text))
    return out


def merge_page_texts(text_layer: list[tuple[int, str]], ocr: list[tuple[int, str]],
                     untrusted: set[int] | None = None) -> list[tuple[int, str, str]]:
    """One text per page, each saying where it came from: the text layer where it exists and is trusted,
    the OCR pass for every other page it covers. A page's text layer is untrusted when the second reader
    disagreed with it (`text_layer_trusted` false in the page table)."""
    untrusted = untrusted or set()
    by_ocr = dict(ocr)
    rows: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    for page, text in text_layer:
        if page in untrusted and page in by_ocr:
            rows.append((page, by_ocr[page], "ocr"))
        else:
            rows.append((page, text, "text_layer"))
        seen.add(page)
    rows += [(page, text, "ocr") for page, text in ocr if page not in seen]
    return sorted(rows)


def untrusted_pages() -> dict[str, set[int]]:
    """Pages whose text layer the OCR comparison rejected, by document hash; empty when no page table exists."""
    try:
        from ..render import read_pages
    except ImportError:  # pragma: no cover
        return {}
    out: dict[str, set[int]] = {}
    try:
        rows = read_pages()
    except Exception:  # noqa: BLE001 - no pages table yet
        return {}
    for r in rows:
        if r.get("text_layer_trusted") is False:
            out.setdefault(str(r["pdf_sha256"]), set()).add(int(r["page_no"]))
    return out


def fetch_documents(
    file_nums: Iterable[str], client: Any = None, max_file_mb: float = MAX_FILE_MB,
    log: Callable[[str], None] = print,
) -> dict[str, list[dict[str, Any]]]:
    """List each file's report PDFs, download what fits the budget, and return what is on disk.

    Resumable: a document already downloaded with the right size is not fetched again.
    """
    import httpx

    client = client or ArcGisClient(cache_dir=PATHS.cache / "arcgis")
    http = httpx.Client(timeout=httpx.Timeout(60.0, read=180.0), follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        for i, fn in enumerate(file_nums, 1):
            try:
                listing = classify_listing(probe_listing(client, fn))
            except Exception as err:
                log(f"    {fn}: listing failed ({type(err).__name__}); skipped")
                continue
            budget = max_file_mb
            got: list[dict[str, Any]] = []
            for doc in listing.get("report_pdfs", []):
                size = float(doc.get("size_mb") or 0.0)
                if not doc.get("url") or size > MAX_DOC_MB or size <= 0 or size > budget:
                    continue
                dest = CORPUS_DIR / _safe(fn) / _safe(doc["name"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.is_file() or dest.stat().st_size < 1024:
                    try:
                        time.sleep(PACE_S)
                        with http.stream("GET", doc["url"]) as r:
                            r.raise_for_status()
                            with dest.open("wb") as fh:
                                for chunk in r.iter_bytes(1 << 16):
                                    fh.write(chunk)
                    except Exception as err:
                        log(f"    {fn}: {doc['name'][:40]} failed ({type(err).__name__})")
                        dest.unlink(missing_ok=True)
                        continue
                budget -= size
                got.append({"name": doc["name"], "path": dest, "size_mb": size})
            if got:
                out[fn] = got
            if i % 10 == 0:
                log(f"    {i} files probed, {len(out)} with documents, "
                    f"{sum(len(v) for v in out.values())} PDFs on disk")
    finally:
        http.close()
    return out


def documents_on_disk(log: Callable[[str], None] = print) -> dict[str, list[dict[str, Any]]]:
    """PDFs already fetched by earlier stages, indexed without touching the network.

    The provincial file store serves at about 20 KB/s, so downloading a wide corpus takes hours. Everything
    already on disk is indexed first, which makes the page tier real immediately; the download widens it in
    the background afterwards.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for root in (PATHS.raw, CORPUS_DIR):
        if not root.is_dir():
            continue
        for pdf in sorted(root.rglob("*.pdf")):
            file_num = pdf.relative_to(root).parts[0]
            out.setdefault(file_num, []).append(
                {"name": pdf.name, "path": pdf, "size_mb": round(pdf.stat().st_size / 1e6, 3)}
            )
    log(f"  {sum(len(v) for v in out.values())} PDFs already on disk across {len(out)} files")
    return out


def build(
    n_files: int = 60, bbox: tuple[float, float, float, float] | None = None,
    max_file_mb: float = MAX_FILE_MB, download: bool = True, log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Index the corpus: every linked file as a row, and page text for what is on disk (plus new downloads)."""
    files = linked_files(log=log)
    if bbox:
        files = in_region(files, bbox)
        log(f"  {len(files)} of them inside the study area")

    con = connect()
    try:
        con.execute("delete from native.corpus_file")
        append_frame(con, "native", "corpus_file",
                     files.drop(columns=["in_index"])[
                         ["file_num", "company", "property", "work_period", "nts", "work_description",
                          "lon", "lat", "n_holes", "hole_names", "retrieved_at"]
                     ], "native")
    finally:
        con.close()

    docs = documents_on_disk(log=log)
    if download:
        wanted = [f for f in files.head(n_files)["file_num"] if f not in docs]
        log(f"  downloading report PDFs for {len(wanted)} more files (the store is slow; this is resumable)")
        for fn, items in fetch_documents(wanted, max_file_mb=max_file_mb, log=log).items():
            docs.setdefault(fn, []).extend(items)

    rows: list[dict[str, Any]] = []
    now = _now()
    no_text = 0
    from_ocr = 0
    distrust = untrusted_pages()
    for fn, items in docs.items():
        for doc in items:
            sha = _sha256(doc["path"])
            pages = merge_page_texts(page_texts(doc["path"]), ocr_page_texts(sha), distrust.get(sha))
            if not pages:
                no_text += 1
                continue
            for page, text, source in pages:
                from_ocr += source == "ocr"
                rows.append({
                    "file_num": fn, "doc_name": doc["name"], "doc_sha256": sha, "page": page,
                    "chars": len(text), "text": text, "extracted_at": now, "source": source,
                })

    if rows:
        con = connect()
        try:
            shas = sorted({r["doc_sha256"] for r in rows})
            con.execute(
                f"delete from read.corpus_page where doc_sha256 in ({','.join('?' * len(shas))})", shas
            )
            append_frame(con, "read", "corpus_page", pd.DataFrame(rows), "read")
            totals = con.execute(
                "select count(distinct file_num), count(distinct doc_sha256), count(*) from read.corpus_page"
            ).fetchone()
        finally:
            con.close()
    else:
        totals = (0, 0, 0)

    log(f"  corpus: {len(files)} files indexed by metadata; "
        f"{totals[0]} files, {totals[1]} documents, {totals[2]} pages of text")
    log(f"  {from_ocr:,} page(s) of text came from the OCR pass; {no_text} document(s) have neither a text layer "
        f"nor OCR yet")
    return {"files_indexed": len(files), "files_with_text": totals[0], "documents": totals[1],
            "pages": totals[2], "documents_without_text": no_text}
