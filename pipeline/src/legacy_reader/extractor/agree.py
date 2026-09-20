"""Second-family agreement (PRD §8.2, stage 4): two readings of one page, compared value by value.

The rule, so it can be argued with:

* **Same value** means the same field (a `hole_name` is a `hole_id`), the same analyte for a grade, and the
  same quote box on the page: the two boxes come from locating each reader's quote against the same OCR
  words, so when both quotes locate, the boxes of one printed cell overlap and the boxes of two different
  cells do not. A value whose own quote did not locate (the usual case when the two readers read a digit
  differently: the OCR sides with one of them) is paired by its printed row instead, the row band the
  locator found for the row quote, within the same field; a value with neither box is matched by its
  printed text, which can only ever count it as agreed, never as a disagreement it did not earn.
* **Equal** means, for a numeric field, the same qualifier and numbers equal within the precision the first
  reader printed (half a unit in the last printed decimal: `478.6` agrees with `478.60`, not with `478.5`);
  for a text field, equality after normalisation (case, whitespace, separators and punctuation folded, the
  same folding the locator applies to both readers).
* Every value one reader found and the other did not is a disagreement too: a dropped row is the failure
  the row counts exist for, and the review queue is where it goes.

The agreement rate of a page is the agreed share of the union of values either reader found; the rate over
matched pairs alone is reported beside it, because the two answer different questions (did the readers
find the same cells; did they read them the same way). Both are tallied per field type.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field as dc_field
from typing import Any, Iterable

from ..locate import Located, PageLocator, bare
from ..normalise import parse_number

AGREE_VERSION = "agree/v1"

#: a cell whose quote located: the two boxes must cover at least this share of the smaller one
BOX_OVERLAP_MIN = 0.5
#: a value paired by its printed row: the two row bands must share at least this much of the shorter one
ROW_OVERLAP_MIN = 0.5

#: field -> the type its agreement is tallied under. Numeric types compare by number; the rest by text.
FIELD_TYPES: dict[str, str] = {
    "from_depth": "depth", "to_depth": "depth", "sample_from_depth": "depth", "sample_to_depth": "depth",
    "interval_width": "depth", "sample_width": "depth", "total_depth": "depth", "elevation": "depth",
    "grade": "grade", "sulfides": "grade",
    "core_recovery": "recovery", "sample_recovery": "recovery",
    "easting": "coordinate", "northing": "coordinate", "latitude": "coordinate", "longitude": "coordinate",
    "grid_x": "coordinate", "grid_y": "coordinate",
    "azimuth": "angle", "dip": "angle",
    "hole_id": "identifier", "hole_name": "identifier", "sample_id": "identifier",
    "description": "text", "lith_code": "text", "date": "text", "other": "text",
    "utm_zone": "page_level", "datum": "page_level", "depth_unit": "page_level", "unit_notes": "page_level",
    "species": "page_level", "basis": "page_level", "method": "page_level",
}
NUMERIC_TYPES = frozenset({"depth", "grade", "recovery", "coordinate", "angle"})
STATUSES = ("agreed", "disagreed", "only_a", "only_b")

_DECIMALS = re.compile(r"\d+\.(\d+)")


def field_type(field: str | None) -> str:
    return FIELD_TYPES.get(field or "", "text")


def is_numeric(field: str | None) -> bool:
    return field_type(field) in NUMERIC_TYPES


# --------------------------------------------------------------------------------- candidates

@dataclass(frozen=True)
class Candidate:
    """One printed value as one reader saw it, with the box the locator gave its quote."""

    field: str
    scope: str                       # cell | page_level | hole
    as_printed: str
    unit_as_printed: str | None = None
    analyte: str | None = None
    quote: str | None = None
    bbox: list[float] | None = None          # the value's own box, when its quote located
    row_bbox: list[float] | None = None      # the printed row's band, when the row quote located
    table_index: int | None = None
    row_index: int | None = None
    value_id: str | None = None      # the store's id when the reading was assembled; None for a fresh read
    model: str | None = None
    prompt_version: str | None = None
    run_id: str | None = None
    uncertain: bool = False
    extra: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def key_field(self) -> str:
        return "hole_id" if self.field == "hole_name" else self.field

    @property
    def analyte_key(self) -> str:
        return bare(self.analyte) if self.field == "grade" else ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _box_of(loc: PageLocator, value: str, row: Located | None, column_x: tuple[float, float] | None = None,
            multiline: bool = False) -> list[float] | None:
    located = loc.locate_value(value, row if row and row.bbox else None, column_x=column_x,
                               allow_multiline=multiline)
    return located.bbox if located.located else None


def candidates_from_wire(result: dict[str, Any], loc: PageLocator, model: str | None = None,
                         prompt_version: str | None = None, run_id: str | None = None) -> list[Candidate]:
    """The printed values of one wire result, each located the way the assembler locates it (row first,
    then the cell inside the row), so a fresh reading and an assembled one carry comparable boxes."""
    out: list[Candidate] = []
    page_level = result.get("page_level") or {}
    for key in ("hole_id", "datum", "utm_zone", "depth_unit", "unit_notes", "species", "basis", "method"):
        f = page_level.get(key) or {}
        if not isinstance(f, dict) or f.get("printed") != "printed" or not f.get("value_as_printed"):
            continue
        value = str(f["value_as_printed"])
        quote = f.get("quote") or value
        row = loc.locate_row(quote)
        out.append(Candidate(field=key, scope="page_level", as_printed=value, unit_as_printed=f.get("unit_as_printed"),
                             quote=quote, bbox=_box_of(loc, value, row), model=model,
                             prompt_version=prompt_version, run_id=run_id))
    for table in result.get("tables") or []:
        t_index = int(table.get("table_index") or 0)
        for row in table.get("rows") or []:
            r_index = int(row.get("row_index") or 0)
            row_quote = row.get("row_quote")
            row_loc = loc.locate_row(row_quote)
            cells = row.get("cells") or []
            name = row.get("hole_id_as_printed")
            row_box = row_loc.bbox if row_loc.bbox else None
            if name and not any(c.get("field") == "hole_id" and c.get("value_as_printed") for c in cells):
                out.append(Candidate(field="hole_id", scope="hole", as_printed=str(name).strip(), quote=row_quote,
                                     bbox=_box_of(loc, str(name), row_loc), row_bbox=row_box, table_index=t_index,
                                     row_index=r_index, model=model, prompt_version=prompt_version, run_id=run_id))
            for cell in cells:
                if cell.get("printed") != "printed" or not cell.get("value_as_printed"):
                    continue
                fieldname = cell.get("field") or "other"
                value = str(cell["value_as_printed"])
                multiline = fieldname in ("description", "lith_code", "other")
                out.append(Candidate(
                    field=fieldname, scope="cell", as_printed=value, unit_as_printed=cell.get("unit_as_printed"),
                    analyte=cell.get("analyte_as_printed"), quote=cell.get("quote") or row_quote,
                    bbox=_box_of(loc, value, row_loc, multiline=multiline), row_bbox=row_box, table_index=t_index,
                    row_index=r_index, model=model, prompt_version=prompt_version, run_id=run_id,
                    uncertain=bool(cell.get("uncertain")),
                ))
    return out


def candidates_from_assembled(doc: dict[str, Any], page_no: int) -> list[Candidate]:
    """The printed values the assembler filed for one page, with the ids the store knows them by.

    The assembler mints a `hole_name` value for every hole beside the cell or header it read the name from,
    so one printed name is filed twice; here it counts once, as the cell (or the page-level field) the second
    reader will also transcribe, and the name value stands in only when no such cell exists."""
    out: list[Candidate] = []
    meta_all = doc.get("value_meta") or {}
    names: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for vid, v in (doc.get("values") or {}).items():
        if v.get("kind") != "extracted":
            continue
        m = meta_all.get(vid) or {}
        if int(m.get("page") or 0) != int(page_no) or m.get("printed") != "printed" or v.get("as_printed") is None:
            continue
        if m.get("field") == "hole_name":
            if m.get("hole_id_source") in ("page_header", "carried"):
                continue   # the assembler's copy of the page-level hole id: one printed value, not two
            names.append((vid, v, m))
            continue
        out.append(_assembled_candidate(vid, v, m))
    cells = {(bare(c.as_printed), c.row_index) for c in out if c.field == "hole_id"}
    for vid, v, m in names:
        if (bare(str(v["as_printed"])), m.get("row")) not in cells:
            out.append(_assembled_candidate(vid, v, m))
    return out


def _assembled_candidate(vid: str, v: dict[str, Any], m: dict[str, Any]) -> Candidate:
    lin = v.get("lineage") or {}
    row_loc = m.get("row_locate") or {}
    table_id = m.get("table_id")
    t_index = int(str(table_id).rsplit(":", 1)[-1]) if table_id and str(table_id).rsplit(":", 1)[-1].isdigit() else None
    return Candidate(
        field=str(m.get("field") or "other"), scope=str(m.get("scope") or "cell"), as_printed=str(v["as_printed"]),
        unit_as_printed=v.get("unit_as_printed"), analyte=m.get("analyte_as_printed"),
        quote=lin.get("quote"), bbox=list(lin["bbox"]) if lin.get("quote_located") and lin.get("bbox") else None,
        row_bbox=list(row_loc["bbox"]) if row_loc.get("bbox") else None,
        table_index=t_index, row_index=m.get("row"), value_id=vid,
        model=lin.get("model"), prompt_version=lin.get("prompt_version"), run_id=lin.get("run_id"),
        uncertain=bool(m.get("uncertain")),
    )


# --------------------------------------------------------------------------------- equality

def printed_decimals(text: str) -> int:
    """How many decimals the first reader printed: the precision a numeric comparison is held to."""
    m = _DECIMALS.search(text.replace(",", ""))
    return len(m.group(1)) if m else 0


def values_equal(a: str, b: str, numeric: bool) -> tuple[bool, str]:
    """(equal, why). Numbers within half a unit of the first reader's last printed decimal, with the same
    qualifier; anything else after the locator's normalisation."""
    if numeric:
        pa, pb = parse_number(a), parse_number(b)
        if pa.value is not None and pb.value is not None:
            if (pa.qualifier or "") != (pb.qualifier or ""):
                return False, f"qualifier {pa.qualifier or 'none'} against {pb.qualifier or 'none'}"
            tol = 0.5 * 10 ** (-printed_decimals(a))
            if abs(pa.value - pb.value) <= tol + 1e-9:
                return True, f"within {tol:g} of each other"
            return False, f"{pa.value:g} against {pb.value:g}, more than {tol:g} apart"
        # a qualifier alone (tr, nil, n.d.) or an unparsable number: the printed text has to match
    if bare(a) == bare(b):
        return True, "the same after normalisation"
    return False, "different text after normalisation"


# --------------------------------------------------------------------------------- alignment

def row_overlap(a: list[float] | None, b: list[float] | None) -> float:
    """Vertical overlap of two row bands over the shorter one: a row is a band, not a box."""
    if not a or not b:
        return 0.0
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return iy / max(min(a[3] - a[1], b[3] - b[1]), 1e-9)


def box_overlap(a: list[float] | None, b: list[float] | None) -> float:
    """Intersection over the smaller box: 1.0 when one box sits inside the other, 0.0 when they miss."""
    if not a or not b:
        return 0.0
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    area_a = max((a[2] - a[0]) * (a[3] - a[1]), 1e-9)
    area_b = max((b[2] - b[0]) * (b[3] - b[1]), 1e-9)
    return inter / min(area_a, area_b)


@dataclass(frozen=True)
class Pair:
    status: str                      # agreed | disagreed | only_a | only_b
    a: Candidate | None
    b: Candidate | None
    matched_by: str                  # box | row | text | none
    detail: str = ""

    @property
    def field(self) -> str:
        return (self.a or self.b).key_field  # type: ignore[union-attr]

    @property
    def field_type(self) -> str:
        return field_type(self.field)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "field": self.field, "field_type": self.field_type,
                "matched_by": self.matched_by, "detail": self.detail,
                "a": self.a.as_dict() if self.a else None, "b": self.b.as_dict() if self.b else None}


def _group_key(c: Candidate) -> tuple[str, str]:
    return c.key_field, c.analyte_key


def _cx(c: Candidate) -> float | None:
    return (c.bbox[0] + c.bbox[2]) / 2 if c.bbox else None


def align(a_values: list[Candidate], b_values: list[Candidate]) -> list[Pair]:
    """Match the two readings within a field: by the value's own box first, then by the printed row for a
    value whose quote did not locate, then by printed text for what has neither."""
    groups: dict[tuple[str, str], tuple[list[Candidate], list[Candidate]]] = {}
    for c in a_values:
        groups.setdefault(_group_key(c), ([], []))[0].append(c)
    for c in b_values:
        groups.setdefault(_group_key(c), ([], []))[1].append(c)
    pairs: list[Pair] = []
    for key in sorted(groups):
        ga, gb = groups[key]
        numeric = is_numeric(key[0])
        taken_a: set[int] = set()
        taken_b: set[int] = set()
        # boxes: every overlapping pair, best first, each value used once
        scored = sorted(((box_overlap(x.bbox, y.bbox), i, j) for i, x in enumerate(ga) for j, y in enumerate(gb)),
                        key=lambda t: (-t[0], t[1], t[2]))
        for score, i, j in scored:
            if score < BOX_OVERLAP_MIN:
                break
            if i in taken_a or j in taken_b:
                continue
            taken_a.add(i)
            taken_b.add(j)
            equal, why = values_equal(ga[i].as_printed, gb[j].as_printed, numeric)
            pairs.append(Pair("agreed" if equal else "disagreed", ga[i], gb[j], "box", why))
        # a value whose own quote did not locate, but whose row did: the same row and field is the same cell.
        # Two located cells that did not overlap are two different cells and are never paired this way.
        rows = sorted(((row_overlap(x.row_bbox, y.row_bbox), abs((_cx(x) or 0) - (_cx(y) or 0)), i, j)
                       for i, x in enumerate(ga) for j, y in enumerate(gb)
                       if not (x.bbox and y.bbox)),
                      key=lambda t: (-t[0], t[1], t[2], t[3]))
        for score, _dx, i, j in rows:
            if score < ROW_OVERLAP_MIN:
                break
            if i in taken_a or j in taken_b:
                continue
            taken_a.add(i)
            taken_b.add(j)
            equal, why = values_equal(ga[i].as_printed, gb[j].as_printed, numeric)
            pairs.append(Pair("agreed" if equal else "disagreed", ga[i], gb[j], "row", why))
        # neither box on one side: the same printed text is the same value, and only that. A page-level
        # field is one statement per page by construction, so for it the text stands even when the locator
        # anchored the same printed words at two different places (a unit printed in every column header).
        one_per_page = field_type(key[0]) == "page_level"
        for i, x in enumerate(ga):
            if i in taken_a:
                continue
            for j, y in enumerate(gb):
                if j in taken_b:
                    continue
                if x.bbox and y.bbox and not one_per_page:
                    continue   # both located and did not overlap: two different cells, not a text match
                if values_equal(x.as_printed, y.as_printed, numeric)[0]:
                    taken_a.add(i)
                    taken_b.add(j)
                    pairs.append(Pair("agreed", x, y, "text", "the same printed text; a quote did not locate"))
                    break
        pairs.extend(Pair("only_a", x, None, "none", "the second reader did not find this value")
                     for i, x in enumerate(ga) if i not in taken_a)
        pairs.extend(Pair("only_b", None, y, "none", "the first reader did not find this value")
                     for j, y in enumerate(gb) if j not in taken_b)
    return pairs


# --------------------------------------------------------------------------------- tallies

def _counts() -> dict[str, int]:
    return {s: 0 for s in STATUSES}


def _rates(c: dict[str, int]) -> dict[str, Any]:
    union = sum(c[s] for s in STATUSES)
    matched = c["agreed"] + c["disagreed"]
    return {**c, "n": union, "rate": round(c["agreed"] / union, 4) if union else None,
            "pair_rate": round(c["agreed"] / matched, 4) if matched else None}


def tally(pairs: Iterable[Pair]) -> dict[str, Any]:
    """Counts and rates over a page: overall and per field type. `rate` is over the union of values either
    reader found; `pair_rate` over the values both found."""
    total = _counts()
    by_type: dict[str, dict[str, int]] = {}
    for p in pairs:
        total[p.status] += 1
        by_type.setdefault(p.field_type, _counts())[p.status] += 1
    return {**_rates(total), "by_field_type": {k: _rates(v) for k, v in sorted(by_type.items())}}


def merge_tallies(tallies: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The run's agreement metric: the page tallies summed, rates recomputed over the sums."""
    total = _counts()
    by_type: dict[str, dict[str, int]] = {}
    n_pages = 0
    for t in tallies:
        n_pages += 1
        for s in STATUSES:
            total[s] += int(t.get(s) or 0)
        for k, v in (t.get("by_field_type") or {}).items():
            acc = by_type.setdefault(k, _counts())
            for s in STATUSES:
                acc[s] += int(v.get(s) or 0)
    return {"version": AGREE_VERSION, "pages": n_pages, **_rates(total),
            "by_field_type": {k: _rates(v) for k, v in sorted(by_type.items())}}


def compare(a_values: list[Candidate], b_values: list[Candidate]) -> dict[str, Any]:
    """One page: the pairs, the tally, and the pairs that belong in the review queue."""
    pairs = align(a_values, b_values)
    return {"version": AGREE_VERSION, "n_a": len(a_values), "n_b": len(b_values),
            "pairs": pairs, "tally": tally(pairs),
            "queue": [p for p in pairs if p.status != "agreed"]}
