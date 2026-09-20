"""Retrieval over the assessment corpus: spatial filter first, then rank.

The order matters and is not an implementation detail. A lexical score alone will happily hand back a passage
about a property 400 km away that happens to share vocabulary, and in this domain the whole question is what is
known *here*. So a query carries a place, the corpus is cut to the files whose reported holes sit near it, and
only then are passages scored. That is the pattern the research recommends for spatial retrieval-augmented
generation, and it is what keeps an evidence memo about one cell from citing another district.

Three tiers come back, and a passage always says which it is:

* `metadata` - the province's own index row for a file: company, property, work period, work description. No
  document was opened. Context, never evidence.
* `page` - text from a page of a downloaded report. Quotable with a file and a page number, but the text layer
  of a scan is usually an OCR pass, so it is evidence of what the page says, not of any number being right.
* `extracted` - a value this pipeline read page by page, with its box, quote and validator outcomes. The only
  tier a number may be taken from.

BM25 is deliberate: no embedding model, no API key, no vector store. The corpus is tens of thousands of pages,
the ranking is reproducible from the text alone, and anything it returns can be checked by eye.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

from ..store import connect

#: BM25 with the usual defaults; k1 controls term saturation, b how much length normalises
K1 = 1.5
B = 0.75

TOKEN = re.compile(r"[a-z0-9][a-z0-9\-']*")
#: words that carry no signal in a corpus where every document is an assessment report about drilling
STOP = frozenset("""
a an and are as at be been but by for from had has have if in into is it its of on or that the their then
there these they this to was were which will with report assessment saskatchewan
""".split())


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP and len(t) > 1]


@dataclass
class Passage:
    """One retrieved passage, carrying where it came from and how far it can be trusted."""

    tier: str  # metadata | page | extracted
    file_num: str
    score: float
    text: str
    page: int | None = None
    doc_name: str | None = None
    doc_sha256: str | None = None
    value_id: str | None = None
    distance_km: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def quotable(self) -> bool:
        """Whether a memo may quote this with a page reference."""
        return self.tier in ("page", "extracted")

    @property
    def carries_numbers(self) -> bool:
        """Whether a number may be taken from this passage at all."""
        return self.tier == "extracted"

    def cite(self) -> str:
        if self.tier == "extracted":
            return f"{self.file_num} p.{self.page} (extracted, {self.value_id})"
        if self.tier == "page":
            return f"{self.file_num} p.{self.page} ({self.doc_name})"
        return f"{self.file_num} (provincial index entry)"


@dataclass
class Corpus:
    """An in-memory BM25 index over the corpus pages, built once and reused."""

    doc_ids: list[tuple[str, str, int]]  # (file_num, doc_sha256, page)
    lengths: list[int]
    postings: dict[str, list[tuple[int, int]]]  # term -> (doc position, count)
    avg_len: float
    n_docs: int

    def score(self, query: str, allowed: set[int] | None = None) -> list[tuple[int, float]]:
        terms = Counter(tokenize(query))
        if not terms:
            return []
        scores: dict[int, float] = {}
        for term, qf in terms.items():
            posting = self.postings.get(term)
            if not posting:
                continue
            idf = math.log(1 + (self.n_docs - len(posting) + 0.5) / (len(posting) + 0.5))
            for pos, tf in posting:
                if allowed is not None and pos not in allowed:
                    continue
                norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.lengths[pos] / max(self.avg_len, 1.0)))
                scores[pos] = scores.get(pos, 0.0) + idf * norm * min(qf, 3)
        return sorted(scores.items(), key=lambda kv: -kv[1])


@lru_cache(maxsize=1)
def load_corpus() -> Corpus:
    """Build the page index from the store. Cached: the corpus does not change inside one run."""
    con = connect(read_only=True)
    try:
        rows = con.execute(
            "select file_num, doc_sha256, page, text from read.corpus_page order by file_num, doc_sha256, page"
        ).fetchall()
    finally:
        con.close()
    doc_ids: list[tuple[str, str, int]] = []
    lengths: list[int] = []
    postings: dict[str, list[tuple[int, int]]] = {}
    for pos, (file_num, sha, page, text) in enumerate(rows):
        toks = tokenize(text)
        doc_ids.append((file_num, sha, page))
        lengths.append(len(toks))
        for term, tf in Counter(toks).items():
            postings.setdefault(term, []).append((pos, tf))
    avg = sum(lengths) / len(lengths) if lengths else 1.0
    return Corpus(doc_ids, lengths, postings, avg, len(doc_ids))


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def files_near(lon: float, lat: float, radius_km: float = 40.0) -> dict[str, float]:
    """Assessment files whose reported holes sit within `radius_km`, and how far away each is."""
    con = connect(read_only=True)
    try:
        rows = con.execute("select file_num, lon, lat from native.corpus_file").fetchall()
    finally:
        con.close()
    out: dict[str, float] = {}
    for file_num, flon, flat in rows:
        if flon is None or flat is None:
            continue
        d = _haversine_km(lon, lat, float(flon), float(flat))
        if d <= radius_km:
            out[file_num] = d
    return out


def _metadata_passages(near: dict[str, float], query: str, k: int) -> list[Passage]:
    if not near:
        return []
    con = connect(read_only=True)
    try:
        placeholders = ",".join("?" * len(near))
        rows = con.execute(
            f"select file_num, company, property, work_period, work_description, n_holes, hole_names "
            f"from native.corpus_file where file_num in ({placeholders})",
            list(near),
        ).fetchall()
    finally:
        con.close()
    terms = set(tokenize(query))
    out: list[Passage] = []
    for file_num, company, prop, period, desc, n_holes, names in rows:
        blob = " ".join(str(x) for x in (company, prop, period, desc, names) if x)
        hits = sum(1 for t in tokenize(blob) if t in terms)
        # proximity decides among index rows; the text only breaks ties, because these rows are near-identical
        score = 1.0 / (1.0 + near[file_num]) + 0.05 * hits
        text = (f"{company or 'unknown company'}, {prop or 'unnamed property'}, {period or 'undated'}: "
                f"{desc or 'no work description filed'} ({n_holes} holes reported)")
        out.append(Passage(tier="metadata", file_num=file_num, score=score, text=text,
                           distance_km=round(near[file_num], 1),
                           meta={"company": company, "property": prop, "work_period": period,
                                 "n_holes": n_holes}))
    return sorted(out, key=lambda p: -p.score)[:k]


def _page_passages(near: dict[str, float], query: str, k: int, snippet_chars: int,
                   excluded: frozenset[str] = frozenset()) -> list[Passage]:
    corpus = load_corpus()
    if not corpus.n_docs:
        return []
    allowed = ({i for i, (fn, _sha, _pg) in enumerate(corpus.doc_ids) if fn in near} if near
               else {i for i, (fn, _sha, _pg) in enumerate(corpus.doc_ids) if fn not in excluded} if excluded
               else None)
    ranked = corpus.score(query, allowed)[:k]
    if not ranked:
        return []
    con = connect(read_only=True)
    try:
        out: list[Passage] = []
        for pos, score in ranked:
            file_num, sha, page = corpus.doc_ids[pos]
            row = con.execute(
                "select doc_name, text from read.corpus_page where doc_sha256 = ? and page = ?", [sha, page]
            ).fetchone()
            if not row:
                continue
            doc_name, text = row
            out.append(Passage(
                tier="page", file_num=file_num, score=round(float(score), 3),
                text=_snippet(text, query, snippet_chars), page=int(page), doc_name=doc_name,
                doc_sha256=sha, distance_km=round(near.get(file_num, float("nan")), 1) if near else None,
            ))
        return out
    finally:
        con.close()


def _snippet(text: str, query: str, width: int) -> str:
    """The window around the first query term, so a passage reads as an answer rather than a page dump."""
    terms = [t for t in tokenize(query)]
    low = text.lower()
    at = min((low.find(t) for t in terms if low.find(t) >= 0), default=-1)
    if at < 0:
        return text[:width].strip()
    start = max(0, at - width // 3)
    return ("..." if start else "") + text[start:start + width].strip() + ("..." if start + width < len(text) else "")


def _extracted_passages(near: dict[str, float], query: str, k: int,
                        excluded: frozenset[str] = frozenset()) -> list[Passage]:
    """Values this pipeline read page by page: the only tier a number may come from."""
    con = connect(read_only=True)
    try:
        rows = con.execute(
            "select file_num, value_id, page, as_printed, unit_as_printed, quote, field, status "
            "from read.field_value where quote is not null and as_printed is not null"
        ).fetchall()
    finally:
        con.close()
    terms = set(tokenize(query))
    scored: list[Passage] = []
    for file_num, value_id, page, printed, unit, quote, field_name, status in rows:
        if near and file_num not in near:
            continue
        if file_num in excluded:
            continue
        hits = sum(1 for t in tokenize(f"{quote} {field_name or ''}") if t in terms)
        if not hits:
            continue
        scored.append(Passage(
            tier="extracted", file_num=file_num, score=float(hits), page=int(page) if page else None,
            text=f"{field_name or 'value'} = {printed}{(' ' + unit) if unit else ''}  (quote: \"{quote}\")",
            value_id=value_id, distance_km=round(near.get(file_num, float("nan")), 1) if near else None,
            meta={"status": status},
        ))
    return sorted(scored, key=lambda p: -p.score)[:k]


def retrieve(
    query: str, lon: float | None = None, lat: float | None = None, radius_km: float = 40.0,
    k: int = 8, snippet_chars: int = 420, tiers: Iterable[str] = ("extracted", "page", "metadata"),
    exclude_files: Iterable[str] | None = None,
) -> list[Passage]:
    """Passages about this ground, most trustworthy tier first.

    With no position the spatial filter is skipped and the whole corpus is ranked, which is right for a
    question about the basin and wrong for a question about a cell. `exclude_files` names assessment files
    no tier may return, which is how a benchmark keeps the reports about a cell's own ground out of its
    evidence; empty, it changes nothing.
    """
    excluded = frozenset(str(f) for f in (exclude_files or ()))
    near = files_near(lon, lat, radius_km) if lon is not None and lat is not None else {}
    if excluded:
        near = {f: d for f, d in near.items() if f not in excluded}
    out: list[Passage] = []
    tiers = tuple(tiers)
    if "extracted" in tiers:
        out += _extracted_passages(near, query, max(2, k // 3), excluded)
    if "page" in tiers:
        out += _page_passages(near, query, k, snippet_chars, excluded)
    if "metadata" in tiers:
        out += _metadata_passages(near, query, max(2, k // 3))
    return out


def summary() -> dict[str, Any]:
    """What the corpus holds, for the readiness page and `ue prospect retrieve --stats`."""
    con = connect(read_only=True)
    try:
        files = con.execute("select count(*) from native.corpus_file").fetchone()[0]
        pages = con.execute(
            "select count(distinct file_num), count(distinct doc_sha256), count(*), sum(chars) "
            "from read.corpus_page"
        ).fetchone()
    finally:
        con.close()
    corpus = load_corpus()
    return {
        "files_indexed": int(files or 0),
        "files_with_text": int(pages[0] or 0),
        "documents": int(pages[1] or 0),
        "pages": int(pages[2] or 0),
        "characters": int(pages[3] or 0),
        "terms": len(corpus.postings),
    }
