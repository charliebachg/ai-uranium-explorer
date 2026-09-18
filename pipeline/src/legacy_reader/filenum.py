"""Assessment file numbers: one normal form for joining the provincial layers.

Why this exists: the same file appears under several spellings.
- GeoDS layer 20 `ASSMNT_FILE_NUM` says "MAW2110"; `SYSTEM_ASSMNT_FILE_NUM`, the assessment-information
  layers (`FILENUMBER`) and GeoDS drilling `TEMP_ASSMNT_FILE_NUM` say "MAW02110".
- Historic numbers ("64L04-0105", "74H-0012") are identical everywhere, but file names and page stamps
  carry a quadrant ("64L04-NW-0105") and a few index rows carry stray spaces or lower-case letters.
- The compilation's free-text `SOURCE` field embeds numbers among other words.

The normal form is the zero-padded system number (MAW + 5 digits), upper-case, no quadrant.
"""

from __future__ import annotations

import re

_MAW = re.compile(r"^MAW\s*0*(\d{1,5})$")
_HISTORIC_QUAD = re.compile(r"^(\d{2}[A-P]\d{2})\s*-\s*(?:NW|NE|SW|SE)\s*-\s*(\d{4})$")
_HISTORIC = re.compile(r"^(\d{2}[A-P](?:\d{2})?)\s*-\s*(\d{3,4})$")

# Patterns used to find file numbers inside free text. Order matters only for readability; matches are
# collected by position. The quadrant form is accepted because file names and page stamps use it.
_FIND = re.compile(
    r"(?<![A-Z0-9])("
    r"\d{2}[A-P]\d{2}\s?-\s?(?:(?:NW|NE|SW|SE)\s?-\s?)?\d{4}"  # 64L04-0105, 64L04-NW-0105
    r"|\d{2}[A-P]-\d{4}"                                       # 74H-0012
    r"|MAW\s?\d{2,5}"                                          # MAW2110, MAW02110, MAW 02110
    r")(?![0-9])",
    re.IGNORECASE,
)


def norm_file_num(value: str) -> str:
    """Return the join key for an assessment file number.

    >>> norm_file_num("MAW2110")
    'MAW02110'
    >>> norm_file_num("64L04-0105")
    '64L04-0105'
    """
    s = re.sub(r"\s+", " ", str(value).strip().upper())
    m = _MAW.match(s)
    if m:
        return f"MAW{int(m.group(1)):05d}"
    m = _HISTORIC_QUAD.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = _HISTORIC.match(s)
    if m:
        num = m.group(2)
        # One index row has a 3-digit serial ("74H04-021"); pad so it joins with its siblings.
        return f"{m.group(1)}-{int(num):04d}"
    return s.replace(" ", "")


def parse_file_nums(text: str | None) -> list[str]:
    """All assessment file numbers mentioned in free text, normalised, in order of first appearance."""
    if not text:
        return []
    out: list[str] = []
    for m in _FIND.finditer(text):
        n = norm_file_num(re.sub(r"\s", "", m.group(1)))
        if n not in out:
            out.append(n)
    return out


def display_file_num(system_num: str) -> str:
    """The GeoDS layer 20 / table 23 spelling ("MAW02110" -> "MAW2110"); historic numbers unchanged."""
    m = _MAW.match(system_num.upper())
    return f"MAW{int(m.group(1))}" if m else system_num


def nts_from_file_num(file_num: str) -> str | None:
    """Historic numbers start with their NTS sheet ("64L04-0105" -> "64L04", "74H-0012" -> "74H")."""
    m = re.match(r"^(\d{2}[A-P](?:\d{2})?)-", norm_file_num(file_num))
    return m.group(1) if m else None
