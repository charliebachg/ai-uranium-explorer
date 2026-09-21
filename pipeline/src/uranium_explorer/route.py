"""`ue route`: what kind of page is this, from OCR geometry and words only. No model calls in Phase 1.

Classes: collar_table, lith_log, assay_table, probe_log, certificate, other, uncertain.

How it decides (all heuristics, all recorded per page in data/out/routing_report.json):
- geometry: tokens are grouped into rows by their vertical centres; numeric tokens are clustered by their
  left edge into columns; rows holding an ascending depth pair ("152.2 153.7", "157-173.5") are counted.
- words: keyword banks per class, matched with an edit distance of at most 1 for words of five or more
  letters (OCR noise) and exactly for short ones; header hits (top quarter of the page) count 1.5 times.
- a class needs both its keywords and the right shape: assay, lithology and probe tables need aligned
  numeric columns or depth-pair rows, a collar page needs either that or four collar labels (a summary log
  is a key-value form), and a certificate needs either that or three lab-identity markers. A page of prose
  about assays is therefore not an assay table.
- two classes within 10% and 2 points of each other, or a table-shaped page that no class fits, come out
  as `uncertain` and are left for the model triage stage in Phase 2. The one resolved pair is certificate
  against assay_table: a lab certificate's own table stays a certificate.
- continuation chains: consecutive pages of the same class whose numeric column signatures overlap, or
  that print "continued", get one chain id; the first page's header row is kept as carry text.
- datum signals: NAD27 / NAD83 / WGS84 / UTM / zone / local-grid station patterns per row, with the row's
  box, so the web app can jump to the phrase later.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable

from rapidfuzz.distance import Levenshtein

from . import datum as datum_mod
from .paths import PATHS

ROUTE_VERSION = "route/v1"

KEYWORDS: dict[str, tuple[str, ...]] = {
    "collar_table": (
        "collar", "northing", "easting", "elevation", "azimuth", "bearing", "dip", "coordinates",
        "co-ordinates", "location", "grid", "utm", "latitude", "longitude", "collar survey", "total depth",
        "hole length", "casing", "overburden", "unconformity", "summary log", "drill hole summary",
        "hole no", "ddh", "inclination", "date started", "date completed", "logged by", "claim",
    ),
    "lith_log": (
        "lithology", "lithological", "description", "geological log", "core", "recovery", "rqd", "sandstone",
        "pegmatite", "granite", "gneiss", "pelite", "metapelite", "graphitic", "chloritic", "hematite",
        "limonite", "altered", "alteration", "conglomerate", "quartz", "regolith", "basement", "mudstone",
        "siltstone", "breccia", "clay", "weathered", "foliated", "interbedded", "from", "to", "depth",
    ),
    "assay_table": (
        "assay", "assays", "u3o8", "u308", "ppm", "sample", "samples", "sample no", "analysis", "analyses",
        "results", "interval", "width", "grade", "percent", "combined", "geochemical", "lithogeochemical",
        "from", "to", "cu", "ni", "pb", "zn", "co", "as", "au", "ag", "mo", "u",
    ),
    "probe_log": (
        "probe", "probed", "gamma", "cps", "counts", "count rate", "radiometric", "scintillometer",
        "spp2", "sopris", "peaks", "probe log", "gamma log", "total count", "c/s", "eu3o8", "equivalent",
        "borehole probe", "gamma ray",
    ),
    "certificate": (
        "certificate", "certificate of analysis", "laboratory", "laboratories", "geoanalytical", "src",
        "report no", "date received", "date of report", "analyst", "signed", "certified", "method",
        "icp", "detection limit", "digestion", "client", "attention", "batch", "lab",
    ),
}
# Markers that identify a class on their own; each distinct hit adds a point (at most 4).
STRONG: dict[str, tuple[str, ...]] = {
    "collar_table": ("collar", "northing", "easting", "azimuth", "coordinates", "co-ordinates", "summary log",
                     "drill hole summary", "collar survey"),
    "lith_log": ("lithology", "lithological", "geological log", "rqd", "recovery", "regolith"),
    "assay_table": ("assay", "assays", "u3o8", "u308", "grade", "lithogeochemical"),
    "probe_log": ("probe", "probed", "gamma", "cps", "scintillometer", "probe log", "gamma log"),
    "certificate": ("certificate", "certificate of analysis", "geoanalytical", "laboratory", "laboratories",
                    "report no", "date received", "analyst", "detection limit"),
}
MAX_STRONG = 4.0

_SHORT_EXACT = 4  # words up to this length must match exactly; longer ones allow one edit

_NUMERIC = re.compile(r"^[<>~]?\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?[%]?$")
_DEPTH = re.compile(r"^(\d{1,4}(?:\.\d{1,2})?)$")
_DEPTH_PAIR_TOKEN = re.compile(r"^(\d{1,4}(?:\.\d{1,2})?)\s*[-–]\s*(\d{1,4}(?:\.\d{1,2})?)(?:\s?m)?$")
_CONTINUED = re.compile(r"cont(?:'?d|inued|\.)", re.IGNORECASE)
_UTM_LIKE = re.compile(r"^\d{6,7}$")
# Front matter of a modern digital report: the phrase sits in the top quarter of the page and the page lists
# section titles that mention assays, samples and drill holes, which is exactly what fools the keyword banks.
_FRONT_MATTER = re.compile(r"\b(table\s+of\s+contents|list\s+of\s+(tables|figures|appendices|plates|maps))\b", re.IGNORECASE)

MAX_DEPTH_M = 3000.0
MAX_INTERVAL_M = 500.0


@dataclass
class PageFeatures:
    n_tokens: int = 0
    n_rows: int = 0
    numeric_tokens: int = 0
    numeric_ratio: float = 0.0
    numeric_columns: int = 0
    column_signature: list[float] = field(default_factory=list)
    depth_pair_rows: int = 0
    depth_pair_ratio: float = 0.0
    utm_like_tokens: int = 0
    keyword_hits: dict[str, list[str]] = field(default_factory=dict)
    keyword_scores: dict[str, float] = field(default_factory=dict)
    table_like: bool = False
    strong_hits: dict[str, list[str]] = field(default_factory=dict)
    continued_marker: bool = False
    front_matter: bool = False
    header_text: str = ""
    engine: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["keyword_hits"] = {k: v for k, v in self.keyword_hits.items() if v}
        return d


# ---------------------------------------------------------------- geometry


def choose_words(words: list[dict[str, Any]], prefer_text_layer: bool) -> tuple[list[dict[str, Any]], str]:
    """Which reading to route from: a trusted text layer, else LiveText tokens, else Vision lines."""
    by_engine: dict[str, list[dict[str, Any]]] = {}
    for w in words:
        by_engine.setdefault(w["engine"], []).append(w)
    if prefer_text_layer and len(by_engine.get("pdftotext", [])) >= 5:
        return by_engine["pdftotext"], "pdftotext"
    lt = by_engine.get("livetext", [])
    vis = by_engine.get("vision", [])
    if len(lt) >= 5 or len(lt) >= len(vis):
        return lt, "livetext"
    return vis, "vision"


def group_rows(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Cluster tokens into text rows by vertical centre (tolerance: 0.6 of the median token height)."""
    if not words:
        return []
    heights = [w["y1"] - w["y0"] for w in words if w["y1"] > w["y0"]]
    tol = 0.6 * (statistics.median(heights) if heights else 0.01)
    rows: list[list[dict[str, Any]]] = []
    for w in sorted(words, key=lambda w: ((w["y0"] + w["y1"]) / 2, w["x0"])):
        yc = (w["y0"] + w["y1"]) / 2
        if rows and abs(yc - statistics.fmean([(t["y0"] + t["y1"]) / 2 for t in rows[-1]])) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def row_text(row: list[dict[str, Any]]) -> str:
    return " ".join(w["text"] for w in row).strip()


def row_box(row: list[dict[str, Any]]) -> list[float]:
    return [round(min(w["x0"] for w in row), 5), round(min(w["y0"] for w in row), 5),
            round(max(w["x1"] for w in row), 5), round(max(w["y1"] for w in row), 5)]


def _is_numeric(text: str) -> bool:
    return bool(_NUMERIC.match(text.strip()))


def numeric_columns(rows: list[list[dict[str, Any]]], tol: float = 0.02,
                    min_rows: int = 4) -> tuple[int, list[float]]:
    """Columns of numbers: left edges clustered within `tol`, kept if they appear in `min_rows` rows."""
    points: list[tuple[float, int]] = []
    for i, row in enumerate(rows):
        for w in row:
            if _is_numeric(w["text"]):
                points.append((w["x0"], i))
    clusters: list[list[tuple[float, int]]] = []
    for x, i in sorted(points):
        if clusters and x - clusters[-1][0][0] <= tol:
            clusters[-1].append((x, i))
        else:
            clusters.append([(x, i)])
    sig = []
    n = 0
    for c in clusters:
        if len({i for _, i in c}) >= min_rows:
            n += 1
            sig.append(round(statistics.fmean([x for x, _ in c]), 3))
    return n, sig


def depth_pairs(rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Rows that carry a from/to depth pair, either as two numbers or one "from-to" token."""
    out = []
    for i, row in enumerate(rows):
        found = None
        for w in row:
            m = _DEPTH_PAIR_TOKEN.match(w["text"].strip())
            if m:
                a, b = float(m.group(1)), float(m.group(2))
                if 0 <= a < b <= MAX_DEPTH_M and b - a <= MAX_INTERVAL_M:
                    found = {"from": a, "to": b, "token": w["text"], "kind": "single_token"}
                    break
        if found is None:
            nums = [(float(m.group(1)), w) for w in row if (m := _DEPTH.match(w["text"].strip()))]
            for (a, wa), (b, wb) in zip(nums, nums[1:]):
                decimals = "." in wa["text"] or "." in wb["text"]
                if 0 <= a < b <= MAX_DEPTH_M and b - a <= MAX_INTERVAL_M and (decimals or b - a >= 1):
                    found = {"from": a, "to": b, "token": f"{wa['text']} {wb['text']}", "kind": "two_tokens"}
                    break
        if found:
            out.append({**found, "row": i, "box": row_box(row), "text": row_text(row)})
    return out


# ---------------------------------------------------------------- words


def _norm_token(text: str) -> str:
    return re.sub(r"[^a-z0-9/]", "", text.lower())


def keyword_hits(rows: list[list[dict[str, Any]]]) -> tuple[dict[str, list[str]], dict[str, float]]:
    tokens: list[tuple[str, float]] = []   # (normalised token, weight)
    texts: list[tuple[str, float]] = []    # (row text lower, weight)
    for row in rows:
        y = statistics.fmean([(w["y0"] + w["y1"]) / 2 for w in row])
        weight = 1.5 if y <= 0.25 else 1.0
        texts.append((row_text(row).lower(), weight))
        for w in row:
            t = _norm_token(w["text"])
            if t:
                tokens.append((t, weight))
    hits: dict[str, list[str]] = {}
    scores: dict[str, float] = {}
    for cls, bank in KEYWORDS.items():
        found: dict[str, float] = {}
        for kw in bank:
            k = kw.lower()
            if " " in k or "-" in k:
                for text, weight in texts:
                    if k in text:
                        found[kw] = max(found.get(kw, 0), weight)
                        break
                continue
            target = _norm_token(k)
            for t, weight in tokens:
                if t == target or (len(target) > _SHORT_EXACT and abs(len(t) - len(target)) <= 1
                                   and Levenshtein.distance(t, target) <= 1):
                    found[kw] = max(found.get(kw, 0), weight)
                    break
        hits[cls] = sorted(found)
        scores[cls] = round(sum(found.values()), 2)
    return hits, scores


def page_features(words: list[dict[str, Any]], engine: str = "") -> PageFeatures:
    rows = group_rows(words)
    f = PageFeatures(engine=engine)
    f.n_tokens = len(words)
    f.n_rows = len(rows)
    numeric = [w for w in words if _is_numeric(w["text"])]
    f.numeric_tokens = len(numeric)
    f.numeric_ratio = round(len(numeric) / len(words), 4) if words else 0.0
    f.numeric_columns, f.column_signature = numeric_columns(rows)
    pairs = depth_pairs(rows)
    f.depth_pair_rows = len(pairs)
    f.depth_pair_ratio = round(len(pairs) / len(rows), 4) if rows else 0.0
    f.utm_like_tokens = sum(1 for w in words if _UTM_LIKE.match(w["text"].strip()))
    f.keyword_hits, f.keyword_scores = keyword_hits(rows)
    # Prose mentioning depth ranges ("hematite from 157-173.5") is not a table: aligned columns or a high
    # share of depth-pair rows is required.
    f.table_like = (f.numeric_columns >= 2
                    or (f.depth_pair_rows >= 3 and (f.numeric_columns >= 1 or f.depth_pair_ratio >= 0.35))
                    or (f.numeric_ratio >= 0.35 and f.n_rows >= 5))
    f.strong_hits = {c: sorted(set(f.keyword_hits.get(c, [])) & set(STRONG[c])) for c in KEYWORDS}
    f.continued_marker = any(_CONTINUED.search(row_text(r)) for r in rows[:4])
    top = [r for r in rows if r and min(w.get("y0", 1.0) for w in r) < 0.25]
    f.front_matter = any(_FRONT_MATTER.search(row_text(r)) for r in top)
    f.header_text = " | ".join(row_text(r) for r in rows[:3])
    return f, rows, pairs


# ---------------------------------------------------------------- classification


def classify(f: PageFeatures) -> dict[str, Any]:
    """Score each class from keywords plus shape; return the class, the scores and why."""
    k = f.keyword_scores
    scores: dict[str, float] = {}
    why: dict[str, list[str]] = {c: [] for c in KEYWORDS}

    def add(cls: str, value: float, reason: str) -> None:
        scores[cls] = round(scores.get(cls, 0.0) + value, 2)
        why[cls].append(reason)

    if f.n_tokens < 15:
        return {"route_class": "other", "route_scores": {}, "route_candidates": [],
                "route_why": ["fewer than 15 tokens: blank or image-only page"]}
    if f.front_matter:
        return {"route_class": "other", "route_scores": {}, "route_candidates": [],
                "route_why": ["front matter: a table of contents or list of tables/figures/appendices in the top "
                              "quarter of the page; its section titles mention assays and holes without holding any"]}

    for cls in KEYWORDS:
        if k.get(cls, 0) > 0:
            add(cls, k[cls], f"keywords {k[cls]:g}")
        strong = f.strong_hits.get(cls) or []
        if strong:
            add(cls, min(len(strong), MAX_STRONG), f"distinctive markers {', '.join(strong[:4])}")
    if f.numeric_columns >= 2:
        for cls in ("assay_table", "lith_log", "probe_log", "collar_table", "certificate"):
            add(cls, 1.5, f"{f.numeric_columns} numeric columns")
    if f.depth_pair_rows >= 2:
        for cls in ("lith_log", "assay_table", "probe_log"):
            add(cls, 2.0, f"{f.depth_pair_rows} depth-pair rows")
    if f.utm_like_tokens >= 2:
        add("collar_table", 1.5, f"{f.utm_like_tokens} six- or seven-digit coordinates")
    if f.numeric_ratio >= 0.5:
        add("assay_table", 1.0, f"numeric ratio {f.numeric_ratio:.2f}")
        add("probe_log", 0.5, f"numeric ratio {f.numeric_ratio:.2f}")
    if f.numeric_ratio < 0.12:
        for cls in ("assay_table", "probe_log"):
            add(cls, -1.5, f"numeric ratio only {f.numeric_ratio:.2f}")

    # Keywords alone are not enough: a page of prose about assays is not an assay table. Each class has
    # to earn its shape as well. A collar summary may be a key-value form, and a lab certificate's front
    # page is plain text, so those two have their own gates.
    dropped: dict[str, str] = {}
    if not f.table_like:
        for cls in ("assay_table", "lith_log", "probe_log"):
            dropped[cls] = "no table shape (no aligned numeric columns, few depth-pair rows)"
    if not (f.table_like or (k.get("collar_table", 0) >= 4)):
        dropped["collar_table"] = "neither table shape nor at least four collar labels"
    if not (f.table_like or len(f.strong_hits.get("certificate") or []) >= 3):
        dropped["certificate"] = "neither table shape nor three lab-identity markers"
    for cls, reason in dropped.items():
        scores.pop(cls, None)
        why[cls].append(reason)

    ranked = sorted(((c, s) for c, s in scores.items() if s > 0), key=lambda cs: (-cs[1], cs[0]))
    result = {"route_scores": {c: s for c, s in sorted(scores.items())},
              "route_candidates": [c for c, _ in ranked[:3]]}
    result["route_dropped"] = dropped
    if not ranked or ranked[0][1] < 3.0:
        result["route_class"] = "uncertain" if f.table_like else "other"
        reasons = why.get(ranked[0][0], []) if ranked else []
        result["route_why"] = [*reasons, *dropped.values(), "no class reached the score threshold"]
        return result
    if len(ranked) > 1 and ranked[1][1] >= 0.9 * ranked[0][1] and ranked[0][1] - ranked[1][1] < 2.0:
        top2 = {ranked[0][0], ranked[1][0]}
        # A lab certificate's own assay table stays a certificate: the lab identity settles that pair.
        if top2 == {"certificate", "assay_table"} and len(f.strong_hits.get("certificate") or []) >= 3:
            result["route_class"] = "certificate"
            result["route_why"] = [*why["certificate"], "lab identity outranks the assay-table shape"]
            return result
        result["route_class"] = "uncertain"
        result["route_why"] = [f"{ranked[0][0]} {ranked[0][1]:g} and {ranked[1][0]} {ranked[1][1]:g} are too close",
                               *why[ranked[0][0]]]
        return result
    result["route_class"] = ranked[0][0]
    result["route_why"] = why[ranked[0][0]]
    return result


# ---------------------------------------------------------------- datum signals and chains


def datum_signals(rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for i, row in enumerate(rows):
        text = row_text(row)
        for hit in datum_mod.find_hits(text):
            out.append({"kind": hit.kind, "text": hit.text, "row": i, "box": row_box(row), "row_text": text})
    return out


def _signature_overlap(a: list[float], b: list[float], tol: float = 0.03) -> float:
    if not a or not b:
        return 0.0
    matched = sum(1 for x in a if any(abs(x - y) <= tol for y in b))
    return matched / max(len(a), len(b))


TABLE_CLASSES = ("collar_table", "lith_log", "assay_table", "probe_log", "certificate")


def continuation_chains(pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group consecutive same-class table pages into chains (column signatures or a "continued" marker)."""
    chains: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    prev: dict[str, Any] | None = None
    for p in sorted(pages, key=lambda p: p["page_no"]):
        cls = p["route_class"]
        f = p["features"]
        cont = False
        if prev is not None and cls in TABLE_CLASSES and prev["route_class"] == cls and p["page_no"] == prev["page_no"] + 1:
            sig = _signature_overlap(f["column_signature"], prev["features"]["column_signature"])
            cont = sig >= 0.6 or f["continued_marker"]
        if cont and current is not None:
            current["pages"].append(p["page_no"])
        elif cls in TABLE_CLASSES:
            cid = f"{p['pdf_sha256'][:12]}:c{p['page_no']:04d}"
            current = {"chain_id": cid, "route_class": cls, "pages": [p["page_no"]],
                       "header_text": f["header_text"]}
            chains[cid] = current
        else:
            current = None
        p["chain_id"] = current["chain_id"] if current else None
        p["chain_pos"] = len(current["pages"]) if current else None
        prev = p
    return chains


# ---------------------------------------------------------------- stage


def route_page(words: list[dict[str, Any]], prefer_text_layer: bool) -> dict[str, Any]:
    chosen, engine = choose_words(words, prefer_text_layer)
    f, rows, pairs = page_features(chosen, engine)
    cls = classify(f)
    return {**cls, "features": f.as_dict(), "depth_pairs": pairs[:40], "n_depth_pairs": len(pairs),
            "datum_signals": datum_signals(rows)}


def stage_route(log: Callable[[str], None] = print) -> dict[str, Any]:
    from .ocr import read_words
    from .render import guard_not_heldout, read_pages, update_pages

    pages = read_pages()
    for file_num, pdf_sha in {(p["file_num"], p["pdf_sha256"]) for p in pages}:
        guard_not_heldout(file_num, pdf_sha)
    by_pdf: dict[str, list[dict[str, Any]]] = {}
    for p in pages:
        by_pdf.setdefault(p["pdf_sha256"], []).append(p)
    updates: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {"version": ROUTE_VERSION, "files": {}}
    for sha, prows in sorted(by_pdf.items()):
        words = read_words(sha)
        by_page: dict[int, list[dict[str, Any]]] = {}
        for w in words:
            by_page.setdefault(w["page_no"], []).append(w)
        routed = []
        for p in sorted(prows, key=lambda r: r["page_no"]):
            if not p.get("rendered"):
                updates[p["page_id"]] = {"route_class": "not_rendered", "route_why": json.dumps([p.get("large_format_reason")]),
                                         "route_scores": None, "route_candidates": None, "route_features": None,
                                         "chain_id": None, "chain_pos": None, "datum_signals": None, "n_depth_pairs": None}
                continue
            r = route_page(by_page.get(p["page_no"], []), bool(p.get("text_layer_trusted")))
            routed.append({**r, "page_no": p["page_no"], "page_id": p["page_id"], "pdf_sha256": sha})
        chains = continuation_chains(routed)
        for r in routed:
            updates[r["page_id"]] = {
                "route_class": r["route_class"], "route_scores": json.dumps(r["route_scores"], sort_keys=True),
                "route_candidates": ",".join(r["route_candidates"]), "route_why": json.dumps(r["route_why"]),
                "route_features": json.dumps(r["features"], sort_keys=True), "route_engine": r["features"]["engine"],
                "chain_id": r.get("chain_id"), "chain_pos": r.get("chain_pos"),
                "n_depth_pairs": r["n_depth_pairs"],
                "datum_signals": json.dumps([{k: s[k] for k in ("kind", "text", "box")} for s in r["datum_signals"]]),
            }
        file_num = prows[0]["file_num"]
        entry = report["files"].setdefault(file_num, {"documents": [], "pages": 0, "by_class": {}, "uncertain_pages": [],
                                                      "datum_signals": {}, "scanned_pages": 0, "text_pages": 0,
                                                      "not_rendered": [], "chains": []})
        counts: dict[str, int] = {}
        for r in routed:
            counts[r["route_class"]] = counts.get(r["route_class"], 0) + 1
            if r["route_class"] == "uncertain":
                entry["uncertain_pages"].append({"pdf": prows[0]["pdf_name"], "page": r["page_no"],
                                                 "candidates": r["route_candidates"], "why": r["route_why"]})
            for s in r["datum_signals"]:
                entry["datum_signals"][s["kind"]] = entry["datum_signals"].get(s["kind"], 0) + 1
        entry["documents"].append({"pdf": prows[0]["pdf_name"], "pdf_sha256": sha, "pages": len(prows),
                                   "rendered": len(routed), "by_class": counts,
                                   "text_layer_kind": prows[0].get("pdf_text_layer_kind")})
        entry["pages"] += len(prows)
        for c, n in counts.items():
            entry["by_class"][c] = entry["by_class"].get(c, 0) + n
        entry["scanned_pages"] += sum(1 for p in prows if p.get("scanned"))
        entry["text_pages"] += sum(1 for p in prows if (p.get("text_chars") or 0) >= 10)
        entry["not_rendered"].extend([{"pdf": prows[0]["pdf_name"], "page": p["page_no"],
                                       "reason": p.get("large_format_reason")} for p in prows if not p.get("rendered")])
        entry["chains"].extend(chains.values())
        log(f"  {file_num:<12} {prows[0]['pdf_name'][:44]:<44} {counts}")
    update_pages(updates)
    out = PATHS.out / "routing_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n")
    log(f"wrote {out}")
    return report


def ocr_datum_hits(report: dict[str, Any] | None = None) -> dict[str, dict[str, int]]:
    """Per-file datum hit counts from a routing report, for the post-fetch selection check."""
    if report is None:
        path = PATHS.out / "routing_report.json"
        if not path.is_file():
            return {}
        report = json.loads(path.read_text())
    return {n: dict(v["datum_signals"]) for n, v in report["files"].items()}
