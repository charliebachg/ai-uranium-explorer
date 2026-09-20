"""National Topographic System sheet ids to geographic bounds.

Why compute instead of looking up: the NTS layer we pulled covers only the region of interest, while file
index rows name sheets anywhere in the province, in several spellings ("64-L-04", "064L04", "64L04",
"74H"). The grid is regular south of 68 N, so bounds follow from the id:

- Series "XY": X is the longitude band (east edge 48 + 8X degrees W), Y the latitude band (south edge 40 + 4Y N);
  one series is 8 x 4 degrees.
- Map area letter A to P: a 4 x 4 grid of 2 x 1 degree areas, lettered in a boustrophedon from the
  south-east corner (row 1: D C B A west to east; row 2: E F G H; row 3: L K J I; row 4: M N O P).
- 50k sheet 1 to 16: the same boustrophedon inside a letter area, 0.5 x 0.25 degrees.

Longitudes are returned as negative degrees east (the western hemisphere).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (row from south, column from west) for letters and for 50k sheet numbers
_LETTER_POS = {
    "D": (0, 0), "C": (0, 1), "B": (0, 2), "A": (0, 3),
    "E": (1, 0), "F": (1, 1), "G": (1, 2), "H": (1, 3),
    "L": (2, 0), "K": (2, 1), "J": (2, 2), "I": (2, 3),
    "M": (3, 0), "N": (3, 1), "O": (3, 2), "P": (3, 3),
}
_SHEET_POS = {
    4: (0, 0), 3: (0, 1), 2: (0, 2), 1: (0, 3),
    5: (1, 0), 6: (1, 1), 7: (1, 2), 8: (1, 3),
    12: (2, 0), 11: (2, 1), 10: (2, 2), 9: (2, 3),
    13: (3, 0), 14: (3, 1), 15: (3, 2), 16: (3, 3),
}

_ID = re.compile(r"^0*(\d{1,2})\s*-?\s*([A-P])(?:\s*-?\s*(\d{1,2}))?$")


class NtsError(ValueError):
    pass


@dataclass(frozen=True)
class NtsSheet:
    series: int
    letter: str
    sheet: int | None

    @property
    def id(self) -> str:
        return f"{self.series}{self.letter}" + (f"{self.sheet:02d}" if self.sheet is not None else "")

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) in degrees, longitudes negative."""
        return nts_bounds(self.id)


def parse_nts(value: str) -> NtsSheet:
    s = str(value).strip().upper()
    m = _ID.match(s)
    if not m:
        raise NtsError(f"not an NTS 250k or 50k sheet id: {value!r}")
    series = int(m.group(1))
    sheet = int(m.group(3)) if m.group(3) else None
    if sheet is not None and not 1 <= sheet <= 16:
        raise NtsError(f"50k sheet number out of range in {value!r}")
    if series % 10 > 6:
        raise NtsError(f"series {series} lies north of 68 N, where the grid is not regular: {value!r}")
    return NtsSheet(series=series, letter=m.group(2), sheet=sheet)


def nts_bounds(value: str) -> tuple[float, float, float, float]:
    """(west, south, east, north) for an NTS 250k ("74H") or 50k ("64L04") sheet.

    >>> nts_bounds("74H")
    (-106.0, 57.0, -104.0, 58.0)
    >>> nts_bounds("64-L-04")
    (-104.0, 58.0, -103.5, 58.25)
    """
    s = parse_nts(value)
    east_w = 48.0 + 8.0 * (s.series // 10)  # east edge in degrees west
    south = 40.0 + 4.0 * (s.series % 10)
    row, col = _LETTER_POS[s.letter]
    area_south = south + row * 1.0
    area_west_w = east_w + 8.0 - col * 2.0  # west edge of the letter area, degrees west
    if s.sheet is None:
        return (-area_west_w, area_south, -(area_west_w - 2.0), area_south + 1.0)
    r2, c2 = _SHEET_POS[s.sheet]
    sh_south = area_south + r2 * 0.25
    sh_west_w = area_west_w - c2 * 0.5
    return (-sh_west_w, sh_south, -(sh_west_w - 0.5), sh_south + 0.25)


def norm_nts(value: str) -> str:
    return parse_nts(value).id


def parse_nts_list(text: str | None) -> list[str]:
    """Split an index field such as "63-D-14; 64-L-04" or "064L04; 064L05" into normalised ids.

    Unparseable parts are skipped (the caller records the raw field)."""
    if not text:
        return []
    out: list[str] = []
    for part in re.split(r"[;,/]", text):
        part = part.strip()
        if not part:
            continue
        try:
            sid = norm_nts(part)
        except NtsError:
            continue
        if sid not in out:
            out.append(sid)
    return out


def point_in_sheet(lon: float, lat: float, sheet: str, tol_deg: float = 0.0) -> bool:
    w, s, e, n = nts_bounds(sheet)
    return (w - tol_deg) <= lon <= (e + tol_deg) and (s - tol_deg) <= lat <= (n + tol_deg)
