"""Datum and coordinate-system strings as printed: NAD27, NAD83, WGS84, UTM zones, local grid stations.

Used twice: on text layers right after download (to check the "at least 3 NAD27 or no datum" selection
constraint without looking at pages), and on OCR rows during routing (per page, with a box). A hit is a
signal that the page prints something datum-like, never a decision about which datum a collar uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# OCR-tolerant spellings: "NAD 27", "NAD27", "N.A.D. 27", "NAD-27", "NAD 1927", "North American Datum of 1927"
_NAD = r"(?:\bN\.?\s?A\.?\s?D\.?\s?[-\s]?|\bNorth\s+American\s+Datum\s+(?:of\s+)?)"
PATTERNS: dict[str, re.Pattern[str]] = {
    "nad27": re.compile(_NAD + r"(?:19)?27\b", re.IGNORECASE),
    "nad83": re.compile(_NAD + r"(?:19)?83\b", re.IGNORECASE),
    "wgs84": re.compile(r"\bWGS\s?-?\s?(?:19)?84\b", re.IGNORECASE),
    "utm_zone": re.compile(r"\b(?:UTM\s+)?zone\s?:?\s?(1[1-4])\s?[NUVW]?\b", re.IGNORECASE),
    "utm": re.compile(r"\bU\.?T\.?M\.?\b"),
    # grid stations: "11+00N", "1+60W", "L 4+00 E", "BL 0+00N"
    "local_grid": re.compile(r"(?<![\w+])\d{1,3}\s?\+\s?\d{2}\s?[NSEW](?![a-z])"),
}


@dataclass(frozen=True)
class DatumHit:
    kind: str
    text: str
    start: int
    end: int


def find_hits(text: str) -> list[DatumHit]:
    hits: list[DatumHit] = []
    for kind, pat in PATTERNS.items():
        for m in pat.finditer(text or ""):
            if kind == "utm_zone" and not re.search(r"utm|zone\s?:?\s?1[23]", m.group(0), re.IGNORECASE):
                continue
            hits.append(DatumHit(kind=kind, text=m.group(0), start=m.start(), end=m.end()))
    hits.sort(key=lambda h: (h.start, h.kind))
    return hits


def count_hits(text: str) -> dict[str, int]:
    counts = {k: 0 for k in PATTERNS}
    for h in find_hits(text):
        counts[h.kind] += 1
    return counts


def merge_counts(*counts: dict[str, int] | None) -> dict[str, int]:
    out = {k: 0 for k in PATTERNS}
    for c in counts:
        for k, v in (c or {}).items():
            out[k] = out.get(k, 0) + v
    return out
