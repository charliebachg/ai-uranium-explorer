"""`ue locate`: turn a model quote into a word span and a box on the page image.

The model is asked for verbatim quotes; this module is the deterministic half that checks them against a
second reader (Apple Vision OCR) and produces the box the UI jumps to. Three honesty rules:

1. **Confusables fold on the OCR side only.** If OCR read `O` where the model transcribed `0`, that is a
   match; the reverse is not. So `fold()` never touches the model's characters.
2. **`quote_located` and `digit_agreement` are different facts.** A box can be right while the two
   readers disagree on a digit (Apple Vision merges two printed depths into one token on this corpus),
   and a value can match exactly while sitting on the wrong row. Both are recorded per value.
3. **A printed but empty cell still points at the page.** Its box is synthesised from the column
   header's x-range on the row's y, with `quote_located = false`, so "not printed" is inspectable.

Boxes are normalised 0..1 on the upright page image, top-left origin, matching the OCR words.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from rapidfuzz import fuzz

from .route import group_rows

MAX_CELL_WIDTH_FRAC = 0.60      # a cell box wider than this is a bad match, not a cell
CELL_SCORE_MIN = 82.0           # below this, the span is not the value
ROW_SCORE_MIN = 40.0            # rows are scored by token coverage against the band (see _row_score)
PAD = 0.003
MAX_WINDOW = 6                  # words per candidate span
MERGED_SCORE = 90.0             # OCR merged this value with its neighbour into one token
SEPARATOR_SCORE = 99.0          # only separators or punctuation differ
CONFUSABLE_SCORE = 98.0         # a confusable substitution on the OCR side makes it equal

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−⁃"), "-")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"'}
_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d")
_NUMBER = re.compile(r"\d[\d.,]*")

# Applied to the OCR reading only. Keys are what OCR may print for the value on the right.
CONFUSABLE = {
    "l": "1", "I": "1", "i": "1", "|": "1", "!": "1",
    "O": "0", "o": "0", "Q": "0", "D": "0", "°": "0", "º": "0",
    "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "G": "6",
    "†": "+", "‡": "+", "t": "+",
    ",": ".", "·": ".", "•": ".", "'": "", "`": "",
}
_CONFUSABLE_TABLE = str.maketrans({k: v for k, v in CONFUSABLE.items()})


def norm(text: str | None) -> str:
    """NFKC, unified dashes and quotes, collapsed whitespace, case-folded. Applied to both readers."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_QUOTES)
    return _WS.sub(" ", t).strip().casefold()


def squash(text: str | None) -> str:
    """Normalised with whitespace removed: OCR splits and joins words unpredictably."""
    return norm(text).replace(" ", "")


def fold(text: str | None) -> str:
    """OCR side only: squash plus the confusable map. Never applied to a model value."""
    if not text:
        return ""
    return squash(unicodedata.normalize("NFKC", text).translate(_CONFUSABLE_TABLE))


_PUNCT = re.compile(r"[^0-9a-z]+")


def bare(text: str | None) -> str:
    """Normalised with every separator and punctuation mark removed. Applied to both readers:
    whether a scan prints 1,753,200 or 1.753.200 is not a disagreement about the number."""
    return _PUNCT.sub("", squash(text))


def foldbare(text: str | None) -> str:
    """OCR side only: `bare` after the confusable map."""
    return _PUNCT.sub("", fold(text))


def digits_of(text: str | None) -> str:
    return "".join(_DIGITS.findall(norm(text)))


def digit_agreement(value: str | None, ocr_text: str | None) -> str:
    """exact | confusable | mismatch | na, comparing the digits of the value with the OCR reading."""
    dv = digits_of(value)
    if not dv:
        return "na"
    do = digits_of(ocr_text)
    if not do:
        return "mismatch"
    if dv == do or dv in do:
        return "exact"
    folded = "".join(_DIGITS.findall(fold(ocr_text)))
    if folded and (dv == folded or dv in folded):
        return "confusable"
    return "mismatch"


@dataclass(frozen=True)
class Located:
    bbox: list[float] | None
    method: str                 # exact | fuzzy | row_anchor | none
    score: float
    digit_agreement: str        # exact | confusable | mismatch | na
    ambiguous: bool = False
    ocr_text: str = ""
    n_words: int = 0
    lines: tuple[int, ...] = ()
    note: str = ""

    @property
    def located(self) -> bool:
        return self.bbox is not None and self.method in ("exact", "fuzzy")

    def as_dict(self) -> dict[str, Any]:
        return {"bbox": self.bbox, "locate_method": self.method, "locate_score": round(self.score, 1),
                "digit_agreement": self.digit_agreement, "locate_ambiguous": self.ambiguous,
                "locate_ocr_text": self.ocr_text, "locate_words": self.n_words,
                "locate_note": self.note}


NOT_LOCATED = Located(None, "none", 0.0, "na")


def union(words: Iterable[dict[str, Any]], pad: float = PAD) -> list[float]:
    ws = list(words)
    return [round(max(0.0, min(w["x0"] for w in ws) - pad), 5),
            round(max(0.0, min(w["y0"] for w in ws) - pad), 5),
            round(min(1.0, max(w["x1"] for w in ws) + pad), 5),
            round(min(1.0, max(w["y1"] for w in ws) + pad), 5)]


def _windows(words: list[dict[str, Any]], max_window: int = MAX_WINDOW):
    n = len(words)
    for size in range(1, min(max_window, n) + 1):
        for i in range(n - size + 1):
            yield words[i:i + size]


@dataclass(frozen=True)
class _Target:
    """One value, pre-normalised. The model's characters are never confusable-folded."""

    raw: str
    squashed: str
    bare: str

    @staticmethod
    def of(value: str | None) -> "_Target":
        return _Target(value or "", squash(value), bare(value))


def _score(target: _Target, span: list[dict[str, Any]]) -> tuple[float, str, str, str]:
    """(score, method, ocr_text, note) for one candidate span, best interpretation first."""
    raw = " ".join(w["text"] for w in span)
    if squash(raw) == target.squashed:
        return 100.0, "exact", raw, ""
    rb, rf = bare(raw), foldbare(raw)
    if rb and rb == target.bare:
        return SEPARATOR_SCORE, "exact", raw, "separators differ between the two readings"
    if rf and rf == target.bare:
        return CONFUSABLE_SCORE, "fuzzy", raw, "matched after folding confusable characters in the OCR reading"
    if len(span) == 1 and len(target.bare) >= 2:
        for candidate in (rb, rf):
            if candidate and candidate != target.bare and (
                    candidate.startswith(target.bare) or candidate.endswith(target.bare)):
                return MERGED_SCORE, "fuzzy", raw, "OCR merged this value with an adjacent one into one token"
    if not target.bare:
        return 0.0, "fuzzy", raw, ""
    score = max(float(fuzz.ratio(rf, target.bare)), float(fuzz.ratio(rb, target.bare)))
    return score, "fuzzy", raw, ""


def _row_score(band: list[dict[str, Any]], tokens: list[str]) -> tuple[float, list[dict[str, Any]]]:
    """How well a printed band explains a row quote: F1 of token coverage against band coverage.

    Chosen over a plain edit ratio because a row quote covers part of a wide sheet (one column group of
    a landscape drill-log sheet), while a band holds the whole printed line across every column.
    """
    if not tokens or not band:
        return 0.0, []
    remaining = list(tokens)
    matched: list[dict[str, Any]] = []
    matched_chars = 0
    band_chars = 0
    for w in band:
        wb, wf = bare(w["text"]), foldbare(w["text"])
        band_chars += len(wb)
        if not wb:
            continue
        for t in list(remaining):
            if t in wb or t in wf or (len(wb) >= 2 and wb in t):
                matched.append(w)
                matched_chars += min(len(t), len(wb))
                remaining.remove(t)
                break
    recall = (len(tokens) - len(remaining)) / len(tokens)
    precision = min(1.0, matched_chars / band_chars) if band_chars else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return 100.0 * f1, matched


@dataclass
class PageLocator:
    """One page's OCR words, grouped into printed rows."""

    words: list[dict[str, Any]]
    engine: str = "livetext"
    bands: list[list[dict[str, Any]]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.words = [w for w in self.words if (w.get("text") or "").strip()]
        self.bands = group_rows(self.words)

    # ---------------------------------------------------------------- rows

    def locate_row(self, row_quote: str | None) -> Located:
        """Find the printed row a row_quote came from. A row can wrap, so bands are tried in pairs too."""
        tokens = [t for t in (bare(t) for t in norm(row_quote).split()) if t]
        if not tokens or not self.bands:
            return NOT_LOCATED
        best: tuple[float, list[dict[str, Any]], list[dict[str, Any]]] | None = None
        second = 0.0
        for i, band in enumerate(self.bands):
            groups = [band] if i + 1 >= len(self.bands) else [band, band + self.bands[i + 1]]
            for group in groups:
                score, matched = _row_score(group, tokens)
                if best is None or score > best[0]:
                    if best is not None:
                        second = best[0]
                    best = (score, group, matched)
                elif score > second:
                    second = score
        if best is None or best[0] < ROW_SCORE_MIN:
            return Located(None, "none", best[0] if best else 0.0, "na",
                           note="row quote not found in the OCR words")
        score, group, matched = best
        text = " ".join(w["text"] for w in group)
        box = union(matched or group)
        # a row's box spans the matched words horizontally but the whole printed band vertically
        band_box = union(group)
        box = [box[0], band_box[1], box[2], band_box[3]]
        return Located(
            bbox=box, method="exact" if score >= 99 else "fuzzy", score=score,
            digit_agreement=digit_agreement(row_quote, text),
            ambiguous=bool(second and score - second < 3.0),
            ocr_text=text[:400], n_words=len(matched or group),
            lines=tuple(sorted({int(w["line_id"]) for w in group})),
        )

    def row_words(self, row: Located, slack: float = 0.004) -> list[dict[str, Any]]:
        """Every word whose vertical centre sits inside the located row's band."""
        if row.bbox is None:
            return []
        y0, y1 = row.bbox[1] - slack, row.bbox[3] + slack
        return sorted((w for w in self.words if y0 <= (w["y0"] + w["y1"]) / 2 <= y1), key=lambda w: w["x0"])

    # ---------------------------------------------------------------- cells

    def locate_value(self, value: str | None, row: Located | None = None,
                     column_x: tuple[float, float] | None = None,
                     search: list[dict[str, Any]] | None = None,
                     allow_multiline: bool = False) -> Located:
        """Find one printed value. Restricted to the row's words when the row was located.

        `allow_multiline` is for a cell that really does wrap (a lithology description): it is matched
        by word coverage rather than as a contiguous span, and its box may cross printed lines.
        """
        target = _Target.of(value)
        if not target.squashed:
            return NOT_LOCATED
        pool = search if search is not None else (self.row_words(row) if row and row.bbox else self.words)
        if not pool:
            return NOT_LOCATED
        if allow_multiline and len(norm(value).split()) > MAX_WINDOW:
            return self._locate_long_text(value, pool, row)
        scored: list[tuple[float, str, list[dict[str, Any]], str, str]] = []
        for span in _windows(pool):
            if len({int(w["line_id"]) for w in span}) > 1 and len(span) > 1:
                # allow a multi-line span only when the words are vertically adjacent parts of one row
                ys = [(w["y0"] + w["y1"]) / 2 for w in span]
                if max(ys) - min(ys) > 0.012:
                    continue
            score, method, raw, note = _score(target, span)
            if score >= CELL_SCORE_MIN:
                scored.append((score, method, span, raw, note))
        if not scored:
            return self._not_found(value, row, column_x, pool)

        # prefer score, then the column hint, then the shortest span
        def rank(item: tuple[float, str, list[dict[str, Any]], str, str]) -> tuple:
            score, _method, span, _raw, _note = item
            cx = sum((w["x0"] + w["x1"]) / 2 for w in span) / len(span)
            col_dist = round(abs(cx - sum(column_x) / 2), 3) if column_x else 0.0
            return (-score, col_dist, len(span))

        scored.sort(key=rank)
        score, method, span, raw, note = scored[0]
        top = rank(scored[0])
        ambiguous = any(rank(s) == top and s[2] is not span for s in scored)
        box = union(span)
        if not allow_multiline and box[2] - box[0] > MAX_CELL_WIDTH_FRAC:
            return Located(None, "none", score, digit_agreement(value, raw),
                           note=f"span covers {box[2] - box[0]:.0%} of the page width: rejected")
        if not allow_multiline and len({int(w["line_id"]) for w in span}) > 1 and (box[3] - box[1]) > 0.03:
            return Located(None, "none", score, digit_agreement(value, raw),
                           note="span crosses more than one printed line: rejected")
        return Located(bbox=box, method=method, score=score, digit_agreement=digit_agreement(value, raw),
                       ambiguous=ambiguous, ocr_text=raw[:200], n_words=len(span),
                       lines=tuple(sorted({int(w["line_id"]) for w in span})), note=note)

    def _locate_long_text(self, value: str | None, pool: list[dict[str, Any]],
                          row: Located | None) -> Located:
        """A description cell: match by word coverage and allow the box to cross printed lines."""
        tokens = [t for t in (bare(t) for t in norm(value).split()) if len(t) > 2]
        if not tokens:
            return NOT_LOCATED
        matched: list[dict[str, Any]] = []
        remaining = set(tokens)
        for w in pool:
            wb, wf = bare(w["text"]), foldbare(w["text"])
            hit = next((t for t in remaining if t == wb or t == wf or (len(wb) > 3 and wb in t)), None)
            if hit:
                matched.append(w)
                remaining.discard(hit)
        recall = (len(tokens) - len(remaining)) / len(tokens)
        if recall < 0.45 or not matched:
            return self._not_found(value, row, None, pool)
        return Located(bbox=union(matched), method="fuzzy", score=round(100.0 * recall, 1),
                       digit_agreement="na", ocr_text=" ".join(w["text"] for w in matched)[:300],
                       n_words=len(matched),
                       lines=tuple(sorted({int(w["line_id"]) for w in matched})),
                       note=f"multi-line cell: {len(matched)} of {len(tokens)} printed words matched")

    def _nearest_numeric(self, value: str | None, pool: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The token on this row that looks like the value but reads differently: a real disagreement."""
        want = digits_of(value)
        if not want or len(want) < len(bare(value)) * 0.5:
            return None
        best: tuple[float, dict[str, Any]] | None = None
        for w in pool:
            got = digits_of(w["text"])
            if not got or abs(len(got) - len(want)) > 1:
                continue
            score = float(fuzz.ratio(got, want))
            if score >= 55.0 and (best is None or score > best[0]):
                best = (score, w)
        return best[1] if best else None

    def _not_found(self, value: str | None, row: Located | None, column_x: tuple[float, float] | None,
                   pool: list[dict[str, Any]]) -> Located:
        """No span matched. Fall back to the row band (and the column, when known), and say so.

        `digit_agreement` stays `na` unless a concrete rival token was found: not locating a value is a
        locator failure, not evidence that the two readers disagree about its digits.
        """
        if row is None or row.bbox is None:
            return Located(None, "none", 0.0, "na", note="value not found and no row anchor")
        rival = self._nearest_numeric(value, pool)
        if rival is not None:
            return Located(union([rival]), "row_anchor", 0.0, "mismatch", ocr_text=rival["text"],
                           lines=(int(rival["line_id"]),),
                           note="the value was not found; the closest numeric token on this row reads "
                                "differently")
        if column_x:
            box = self.synth_box(row, column_x)
            return Located(box, "row_anchor", 0.0, "na", ocr_text=" ".join(w["text"] for w in pool)[:200],
                           note="value not found in the OCR words: box is the column x-range on the row's y")
        return Located(list(row.bbox), "row_anchor", row.score, "na",
                       ocr_text=" ".join(w["text"] for w in pool)[:200],
                       note="value not found in the OCR words: box is the whole printed row")

    def synth_box(self, row: Located, column_x: tuple[float, float], min_width: float = 0.05) -> list[float] | None:
        """A printed but empty cell: the column header's x-range on the located row's y."""
        if row.bbox is None:
            return None
        cx = (column_x[0] + column_x[1]) / 2
        half = max((column_x[1] - column_x[0]) / 2, min_width / 2) * 1.5
        return [round(max(0.0, cx - half), 5), row.bbox[1], round(min(1.0, cx + half), 5), row.bbox[3]]

    def empty_cell(self, row: Located, column_x: tuple[float, float] | None) -> Located:
        """Box for a `not_printed` cell. quote_located stays false: nothing was matched."""
        if row.bbox is None:
            return Located(None, "none", 0.0, "na", note="empty cell and no row anchor")
        if column_x is None:
            return Located(list(row.bbox), "row_anchor", row.score, "na",
                           note="empty cell: box is the whole printed row (column header not located)")
        box = self.synth_box(row, column_x)
        return Located(box, "row_anchor", row.score, "na", note="empty cell: column x-range on the row's y")

    # ---------------------------------------------------------------- columns

    def column_x(self, header: str | None, above_y: float | None = None) -> tuple[float, float] | None:
        """x-range of a printed column header, searched above the row when a y is given."""
        target = _Target.of(header)
        if not target.squashed:
            return None
        pool = [w for w in self.words if above_y is None or (w["y0"] + w["y1"]) / 2 < above_y]
        best: tuple[float, list[dict[str, Any]]] | None = None
        for span in _windows(pool, max_window=4):
            if len({int(w["line_id"]) for w in span}) > 1:
                continue
            score, _method, _raw, _note = _score(target, span)
            if score >= 88.0 and (best is None or score > best[0] or
                                  (score == best[0] and (span[0]["y0"] > best[1][0]["y0"]))):
                best = (score, span)
        if best is None:
            return None
        span = best[1]
        return (min(w["x0"] for w in span), max(w["x1"] for w in span))
