"""Parsing and normalisation rules, each one logged so it can be argued with.

Nothing here ever overwrites what was printed. `value_as_printed` and `unit_as_printed` survive
untouched into the export; every rule below produces a *separate* derived value or a label, and says
which rule fired. The U to U3O8 factor is used only to cross-check two printed numbers, never to
rewrite one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

FT_TO_M = 0.3048
U_TO_U3O8 = 1.17924          # mass factor; used by V02 as a cross-check only, never to rewrite a value
PPM_PER_PERCENT = 10_000.0

_NUM = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d*\.?\d+")
_QUALIFIER = re.compile(r"^\s*(<=|>=|<|>|~|≈|ca\.?)\s*", re.IGNORECASE)
_NON_NUMERIC = {"tr", "trace", "nil", "nd", "n.d.", "n/d", "na", "n/a", "-", "--", "—", "not detected",
                "below detection", "bd", "bdl", "ins", "insuff", "insufficient"}

DEPTH_UNITS = {
    "ft": "ft", "ft.": "ft", "feet": "ft", "foot": "ft", "'": "ft", "ftg": "ft", "footage": "ft",
    "m": "m", "m.": "m", "metre": "m", "metres": "m", "meter": "m", "meters": "m", "mtr": "m",
}
GRADE_UNITS = {
    "%": "%", "percent": "%", "pct": "%", "% u3o8": "%", "wt%": "%", "wt %": "%",
    "ppm": "ppm", "ppm u": "ppm", "ppm u3o8": "ppm", "mg/kg": "ppm", "g/t": "g/t", "gpt": "g/t",
    "ppb": "ppb", "ug/g": "ppm", "µg/g": "ppm",
    "cps": "cps", "counts": "cps", "c/s": "cps", "counts/second": "cps", "cps/": "cps",
    "lbs/ton": "lbs/ton", "lb/ton": "lbs/ton", "oz/ton": "oz/ton",
}

# Species as printed -> the contract's closed set. Only ever applied to printed text.
_SPECIES_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\be?\s?u\s?3\s?o\s?8\b", re.I), "U3O8", "matched U3O8 in the printed text"),
    (re.compile(r"\bequivalent\s+u3o8\b|\beu3o8\b|\be-?u3o8\b", re.I), "eU3O8", "printed as equivalent U3O8"),
    (re.compile(r"\beu\b|\bequivalent\s+u\b", re.I), "eU", "printed as equivalent uranium"),
    (re.compile(r"\bu\s*\(?ppm\)?\b|\buranium\b|(?<![a-z])u(?![a-z0-9])", re.I), "U", "printed as elemental U"),
)
_PROBE_WORDS = re.compile(r"\bprobe\b|\bprobed\b|\bgamma\b|\bradiometric\b|\bcps\b|\bcounts?\b|\bscintillometer\b"
                          r"|\bequivalent\b|\bgr\b|\bgamma[- ]ray\b|\bdown\s?hole\b", re.I)
_CHEM_WORDS = re.compile(r"\bassay\b|\bchemical\b|\bfluorimetr|\bicp\b|\bxrf\b|\bdelayed\s+neutron\b|\binaa\b"
                         r"|\blaborator|\bgeochem", re.I)


@dataclass(frozen=True)
class ParsedNumber:
    value: float | None
    qualifier: str | None          # "<", ">", "~" as printed
    non_numeric: str | None        # "tr", "nil", ... as printed
    rule: str

    @property
    def below_detection(self) -> bool:
        return self.qualifier in ("<", "<=") or (self.non_numeric or "").lower() in {
            "tr", "trace", "nd", "n.d.", "n/d", "bd", "bdl", "not detected", "below detection", "nil"}


def parse_number(text: str | None) -> ParsedNumber:
    """Parse a printed value. Qualifiers and non-numeric answers are preserved, never turned into 0."""
    if text is None:
        return ParsedNumber(None, None, None, "no printed value")
    raw = str(text).strip()
    if not raw:
        return ParsedNumber(None, None, None, "empty printed value")
    if raw.lower().strip(".") in _NON_NUMERIC or raw.lower() in _NON_NUMERIC:
        return ParsedNumber(None, None, raw, "printed as a non-numeric qualifier; kept as printed, not zero")
    m = _QUALIFIER.match(raw)
    qualifier = None
    rest = raw
    if m:
        qualifier = m.group(1)
        rest = raw[m.end():]
    nm = _NUM.search(rest.replace(" ", ""))
    if not nm:
        return ParsedNumber(None, qualifier, raw, "no number found in the printed value")
    try:
        value = float(nm.group(0).replace(",", ""))
    except ValueError:
        return ParsedNumber(None, qualifier, raw, "number could not be parsed")
    rule = "parsed the printed digits"
    if qualifier:
        rule = f"parsed the printed digits; the printed qualifier {qualifier!r} is kept and the value is a bound"
    return ParsedNumber(value, qualifier, None, rule)


def depth_unit(text: str | None) -> tuple[str | None, str]:
    """Map a printed depth unit onto ft/m. Returns (unit, rule); None means nothing recognisable was printed."""
    if not text:
        return None, "no depth unit printed"
    t = str(text).strip().lower().strip("()[]")
    if t in DEPTH_UNITS:
        return DEPTH_UNITS[t], f"printed depth unit {text!r}"
    for key, unit in DEPTH_UNITS.items():
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", t):
            return unit, f"depth unit {unit} found inside the printed text {text!r}"
    return None, f"printed text {text!r} is not a depth unit this pipeline recognises"


def grade_unit(text: str | None) -> tuple[str | None, str]:
    if not text:
        return None, "no grade unit printed"
    t = str(text).strip().lower().strip("()[]")
    if t in GRADE_UNITS:
        return GRADE_UNITS[t], f"printed grade unit {text!r}"
    for key in sorted(GRADE_UNITS, key=len, reverse=True):
        if key in t:
            return GRADE_UNITS[key], f"grade unit {GRADE_UNITS[key]} found inside the printed text {text!r}"
    return None, f"printed text {text!r} is not a grade unit this pipeline recognises"


def species(*printed: str | None) -> tuple[str, str]:
    """Closed-set species from printed text only. `not_printed` when nothing states it."""
    joined = " ".join(p for p in printed if p)
    if not joined.strip():
        return "not_printed", "no species printed on the page or in the column header"
    if re.search(r"\beu\s?3\s?o\s?8\b|equivalent\s+u3o8", joined, re.I):
        return "eU3O8", "printed as equivalent U3O8"
    for pattern, out, rule in _SPECIES_PATTERNS:
        if pattern.search(joined):
            return out, rule
    return "other", f"printed analyte {joined.strip()[:40]!r} is not a uranium species"


def basis(*printed: str | None) -> tuple[str, str]:
    """chemical | probe_equivalent | not_printed, from printed words only."""
    joined = " ".join(p for p in printed if p)
    if not joined.strip():
        return "not_printed", "no basis printed"
    if re.search(r"\beu\s?3?\s?o?\s?8?\b|equivalent", joined, re.I) or _PROBE_WORDS.search(joined):
        return "probe_equivalent", "printed words indicate a probe or radiometric equivalent, not a chemical assay"
    if _CHEM_WORDS.search(joined):
        return "chemical", "printed words indicate a laboratory assay"
    return "not_printed", "printed text does not state whether grades are chemical or probe equivalents"


def to_metres(value: float | None, unit: str | None) -> tuple[float | None, str, dict[str, Any]]:
    """(metres, op, params). `identity` when the printed unit is already metres or nothing was printed."""
    if value is None:
        return None, "identity", {"reason": "no numeric value"}
    if unit == "ft":
        return round(value * FT_TO_M, 3), "ft_to_m", {"factor": FT_TO_M, "from_unit": "ft", "to_unit": "m"}
    if unit == "m":
        return round(value, 3), "identity", {"from_unit": "m", "to_unit": "m"}
    return round(value, 3), "identity", {"from_unit": "not_printed", "to_unit": "m",
                                         "assumption": "none: the number is carried through unchanged and flagged by V01"}


def normalise_hole_name(name: str | None) -> str:
    """Hole names for matching only. Never stored as the name, and never shown.

    Separators go, and each printed group of digits loses its leading zeros, so 'R-78-27', 'R 78 027'
    and 'R-78-027' all match, and so do 'KL-5' and 'KL005'. Two names that differ only in how their
    digits are grouped ('R78027') stay different: this function does not guess where a group ends.
    """
    if not name:
        return ""
    parts = re.findall(r"[a-z]+|\d+", str(name).lower())
    out = []
    for part in parts:
        out.append((part.lstrip("0") or "0") if part.isdigit() else part)
    return "".join(out).upper()
