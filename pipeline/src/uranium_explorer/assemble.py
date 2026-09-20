"""`ue assemble`: wire rows -> located values -> Collar / LithInterval / AssayInterval records.

Every printed value becomes one `x:<file>:<hash>` value carrying its lineage (page, box, quote,
`quote_located`, model, prompt version, run). Geometry the UI needs in metres becomes a *separate*
`d:<file>:<key>` derived value with its operation and factor, so nothing printed is ever overwritten.

Hole identity is explicit: a row's own hole id wins, then the page header's, then the id carried from
the previous page of a continued table. Which one was used is recorded on the hole (`hole_id_source`)
and checked by V19.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__
from .extract import read_results
from .ids import sha256_json, short
from .locate import Located, PageLocator
from .normalise import depth_unit as parse_depth_unit
from .normalise import grade_unit as parse_grade_unit
from .normalise import basis as parse_basis
from .normalise import normalise_hole_name, parse_number, species as parse_species, to_metres
from .ocr import read_words
from .paths import PATHS
from .render import read_pages
from .values import TOOL

ASSEMBLE_VERSION = "assemble/v1"

DEPTH_FIELDS = {"from_depth", "to_depth", "sample_from_depth", "sample_to_depth"}
COLLAR_FIELDS = ("easting", "northing", "latitude", "longitude", "grid_x", "grid_y", "utm_zone",
                 "datum", "elevation", "azimuth", "dip", "total_depth")
# Column headers to look for when synthesising the box of a printed but empty cell.
HEADER_GUESSES: dict[str, tuple[str, ...]] = {
    "from_depth": ("FROM", "From", "FROM (m)", "DEPTH FROM", "Depth From", "FOOTAGE"),
    "to_depth": ("TO", "To", "TO (m)", "DEPTH TO", "Depth To"),
    "sample_from_depth": ("FROM", "From", "FOOTAGE"),
    "sample_to_depth": ("TO", "To"),
    "interval_width": ("WIDTH", "Width", "LENGTH", "INTERVAL"),
    "core_recovery": ("RCVRY", "RECOVERY", "REC", "% RECOVERY"),
    "sample_id": ("SAMPLE", "Sample", "SAMPLE No", "No", "SAMPLE NO"),
    "description": ("DESCRIPTION", "Description", "LITHOLOGY", "Lithology"),
    "lith_code": ("CODE", "UNIT", "LITH"),
    "sulfides": ("% SULFIDES", "SULFIDES", "SULPHIDES"),
    "grade": ("ASSAYS", "ASSAY", "GRADE", "U3O8", "U"),
    "total_depth": ("LENGTH", "TOTAL DEPTH", "DEPTH", "TD"),
    "dip": ("DIP", "INCLINATION"),
    "azimuth": ("AZIMUTH", "BEARING"),
    "elevation": ("ELEVATION", "ELEV", "COLLAR ELEVATION"),
    "easting": ("EASTING", "EAST", "E"),
    "northing": ("NORTHING", "NORTH", "N"),
    "utm_zone": ("ZONE", "UTM ZONE"),
    "datum": ("DATUM",),
    "hole_id": ("HOLE", "HOLE NO", "DDH", "HOLE #"),
}


# --------------------------------------------------------------------------------- the artifact

def assemble_dir() -> Path:
    return PATHS.out / "assemble"


def assembled_path(file_num: str) -> Path:
    return assemble_dir() / f"{file_num}.json"


def read_assembled(file_num: str) -> dict[str, Any]:
    return json.loads(assembled_path(file_num).read_text())


def assembled_files() -> list[str]:
    return sorted(p.stem for p in assemble_dir().glob("*.json")) if assemble_dir().is_dir() else []


# --------------------------------------------------------------------------------- builders

@dataclass
class Builder:
    """Accumulates the value registry and the per-value metadata validators need."""

    file_num: str
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _vid(self, prefix: str, parts: list[Any]) -> str:
        base = f"{prefix}:{self.file_num}:{short(sha256_json(parts), 10)}"
        if base not in self.values:
            return base
        for n in range(2, 60):
            cand = f"{base}-{n}"
            if cand not in self.values:
                return cand
        raise RuntimeError(f"cannot allocate a value id for {parts}")

    def extracted(self, *, key_parts: list[Any], as_printed: str | None, value: Any,
                  unit_as_printed: str | None, lineage: dict[str, Any], meta: dict[str, Any],
                  status: str = "pass", note: str | None = None) -> str:
        vid = self._vid("x", key_parts)
        rec: dict[str, Any] = {
            "id": vid, "kind": "extracted", "as_printed": as_printed, "value": value,
            "unit_as_printed": unit_as_printed, "status": status, "lineage": {**lineage, "validators": []},
        }
        if note:
            rec["note"] = note
        self.values[vid] = rec
        self.meta[vid] = meta
        return vid

    def derived(self, *, key_parts: list[Any], value: Any, fmt: str, op: str, inputs: list[str],
                params: dict[str, Any] | None = None, unit: str | None = None,
                note: str | None = None) -> str:
        vid = self._vid("d", key_parts)
        rec: dict[str, Any] = {
            "id": vid, "kind": "derived", "as_printed": None, "value": value, "unit_as_printed": None,
            "fmt": fmt, "derivation": {"op": op, "inputs": inputs, "tool": TOOL,
                                       **({"params": params} if params else {})},
        }
        if unit:
            rec["unit"] = unit
        if note:
            rec["note"] = note
        self.values[vid] = rec
        self.meta[vid] = {"kind": "derived", "op": op, "inputs": inputs}
        return vid

    def stat(self, *, key_parts: list[Any], value: Any, fmt: str = "int", note: str | None = None) -> str:
        vid = self._vid("d", key_parts)
        rec: dict[str, Any] = {"id": vid, "kind": "derived", "as_printed": None, "value": value,
                               "unit_as_printed": None, "fmt": fmt,
                               "derivation": {"op": "count", "inputs": [], "tool": TOOL}}
        if note:
            rec["note"] = note
        self.values[vid] = rec
        self.meta[vid] = {"kind": "derived", "op": "count"}
        return vid


def _page_locators(pdf_sha256: str) -> dict[int, PageLocator]:
    words = read_words(pdf_sha256)
    by_page: dict[int, list[dict[str, Any]]] = {}
    for w in words:
        by_page.setdefault(int(w["page_no"]), []).append(w)
    out: dict[int, PageLocator] = {}
    for page_no, ws in by_page.items():
        lt = [w for w in ws if w["engine"] == "livetext"]
        vis = [w for w in ws if w["engine"] == "vision"]
        chosen, engine = (lt, "livetext") if len(lt) >= max(5, len(vis)) else (vis, "vision")
        out[page_no] = PageLocator(chosen, engine=engine)
    return out


def _text_layer_words(pdf_sha256: str) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for w in read_words(pdf_sha256):
        if w["engine"] == "pdftotext":
            out.setdefault(int(w["page_no"]), []).append(w)
    return out


def _column_x(loc: PageLocator, headers: Iterable[str | None], row: Located) -> tuple[float, float] | None:
    above = row.bbox[1] if row.bbox else None
    for h in headers:
        if not h:
            continue
        x = loc.column_x(h, above_y=above)
        if x:
            return x
    return None


def _lineage(page_row: dict[str, Any], result_row: dict[str, Any], quote: str | None, located: Located,
             unit_source: str | None) -> dict[str, Any]:
    lin = {
        "file_num": result_row["file_num"],
        "file_sha256": result_row["pdf_sha256"],
        "page": int(result_row["page_no"]),
        "bbox": located.bbox,
        "quote": quote or "",
        "quote_located": located.located,
        "model": result_row.get("model_resolved") or result_row.get("model_requested") or "unknown",
        "prompt_version": result_row.get("prompt_version") or "unknown",
        "run_id": result_row.get("run_id") or "unknown",
        "extracted_at": result_row.get("extracted_at") or dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    if unit_source:
        lin["unit_source"] = unit_source
    return lin


def _resolve_depth_unit(cell: dict[str, Any], table: dict[str, Any], page_level: dict[str, Any]
                        ) -> tuple[str | None, str | None, str, str]:
    """(unit, unit_as_printed, unit_source, rule) for a depth cell, preferring the most local statement."""
    printed = cell.get("unit_as_printed")
    if printed:
        unit, rule = parse_depth_unit(printed)
        if unit:
            return unit, printed, cell.get("unit_source") or "cell", rule
    for header in table.get("column_headers_as_printed") or []:
        unit, rule = parse_depth_unit(header)
        if unit:
            return unit, header, "column_header", rule
    title = table.get("title_as_printed")
    unit, rule = parse_depth_unit(title)
    if unit:
        return unit, title, "table_title", rule
    for key, src in (("depth_unit", "page_note"), ("unit_notes", "page_note")):
        f = page_level.get(key) or {}
        if f.get("printed") == "printed":
            unit, rule = parse_depth_unit(f.get("value_as_printed") or f.get("unit_as_printed"))
            if unit:
                return unit, f.get("value_as_printed") or f.get("unit_as_printed"), f.get("source") or src, rule
    return None, None, "not_printed", "no depth unit printed anywhere on the page"


# --------------------------------------------------------------------------------- per-file assembly

def assemble_file(file_num: str, results: list[dict[str, Any]], pages: dict[str, dict[str, Any]],
                  log: Callable[[str], None] = print) -> dict[str, Any]:
    b = Builder(file_num)
    locators: dict[str, dict[int, PageLocator]] = {}
    text_layers: dict[str, dict[int, list[dict[str, Any]]]] = {}
    tables_out: list[dict[str, Any]] = []
    holes: dict[str, dict[str, Any]] = {}
    page_notes: list[dict[str, Any]] = []
    carried_hole: dict[str, tuple[str, int]] = {}   # chain id -> (hole id as printed, page)
    stats = {"values": 0, "located": 0, "rows": 0, "printed_rows": 0, "empty_cells": 0}

    def hole_for(name_printed: str | None, source: str, page: int) -> dict[str, Any] | None:
        if not name_printed or not str(name_printed).strip():
            return None
        key = normalise_hole_name(name_printed) or str(name_printed).strip().upper()
        h = holes.get(key)
        if h is None:
            h = {"hole_id": key, "name_as_printed": str(name_printed).strip(), "name_vid": None,
                 "hole_id_source": source, "pages": [], "collar": {}, "collar_meta": {},
                 "lith": [], "assays": [], "names_seen": []}
            holes[key] = h
        if page not in h["pages"]:
            h["pages"].append(page)
        if str(name_printed).strip() not in h["names_seen"]:
            h["names_seen"].append(str(name_printed).strip())
        return h

    for res in sorted(results, key=lambda r: (r["pdf_sha256"], int(r["page_no"]))):
        pdf_sha = res["pdf_sha256"]
        page_no = int(res["page_no"])
        page_row = pages.get(res["page_id"], {})
        if pdf_sha not in locators:
            locators[pdf_sha] = _page_locators(pdf_sha)
            text_layers[pdf_sha] = _text_layer_words(pdf_sha)
        loc = locators[pdf_sha].get(page_no)
        if loc is None:
            log(f"    {file_num} p{page_no}: no OCR words; values will carry no box")
            loc = PageLocator([])
        wire_result = res["result"]
        page_level = wire_result.get("page_level") or {}
        chain_key = f"{pdf_sha}:{res.get('chain_id')}" if res.get("chain_id") else ""

        # ---- page-level statements
        page_fields: dict[str, str] = {}
        for key in ("hole_id", "datum", "utm_zone", "depth_unit", "unit_notes", "species", "basis", "method"):
            f = page_level.get(key) or {}
            if not isinstance(f, dict):
                continue
            printed = f.get("printed")
            value = f.get("value_as_printed")
            if printed != "printed" or not value:
                continue
            quote = f.get("quote") or value
            row_anchor = loc.locate_row(quote)
            label_x = None
            if quote and value:
                label = quote.replace(str(value), " ").strip()
                if len(label) >= 3:
                    label_x = loc.column_x(label.split()[0])
            located = loc.locate_value(str(value), row_anchor if row_anchor.bbox else None, column_x=label_x)
            vid = b.extracted(
                key_parts=[res["page_id"], "page_level", key, value],
                as_printed=str(value), value=str(value), unit_as_printed=f.get("unit_as_printed"),
                lineage=_lineage(page_row, res, quote, located, f.get("source")),
                meta={"field": key, "scope": "page_level", "page_id": res["page_id"], "page": page_no,
                      "pdf_sha256": pdf_sha, "printed": printed, "uncertain": False,
                      "source": f.get("source"), "locate": located.as_dict(),
                      "digit_agreement": located.digit_agreement, "table_id": None, "row": None},
            )
            page_fields[key] = vid
            stats["values"] += 1
            stats["located"] += 1 if located.located else 0
        page_notes.append({"page_id": res["page_id"], "page": page_no, "pdf_sha256": pdf_sha,
                           "page_kind": page_level.get("page_kind"),
                           "coordinate_kind": page_level.get("coordinate_kind"),
                           "route_class": res.get("route_class"), "fields": page_fields,
                           "legibility_notes": wire_result.get("legibility_notes"),
                           "text_layer_words": len(text_layers[pdf_sha].get(page_no, []))})

        header_hole = (page_level.get("hole_id") or {}).get("value_as_printed") \
            if (page_level.get("hole_id") or {}).get("printed") == "printed" else None
        if header_hole and chain_key:
            carried_hole[chain_key] = (str(header_hole), page_no)

        # ---- tables
        for table in wire_result.get("tables") or []:
            t_index = int(table.get("table_index") or 0)
            table_id = f"t:{file_num}:{pdf_sha[:8]}:{page_no:04d}:{t_index}"
            rows = table.get("rows") or []
            printed_rows = int(table.get("printed_row_count") or 0)
            stats["rows"] += len(rows)
            stats["printed_rows"] += printed_rows
            table_boxes: list[list[float]] = []

            for row in rows:
                r_index = int(row.get("row_index") or 0)
                row_quote = row.get("row_quote")
                row_loc = loc.locate_row(row_quote)
                if row_loc.bbox:
                    table_boxes.append(row_loc.bbox)
                cells = row.get("cells") or []

                name_printed = row.get("hole_id_as_printed")
                source = "row"
                if not name_printed:
                    name_printed = next((c.get("value_as_printed") for c in cells
                                         if c.get("field") == "hole_id" and c.get("value_as_printed")), None)
                if not name_printed and header_hole:
                    name_printed, source = header_hole, "page_header"
                if not name_printed and chain_key and chain_key in carried_hole:
                    name_printed, source = carried_hole[chain_key][0], "carried"
                hole = hole_for(name_printed, source, page_no)

                built: dict[str, list[dict[str, Any]]] = {}
                for c_index, cell in enumerate(cells):
                    fieldname = cell.get("field") or "other"
                    value_printed = cell.get("value_as_printed")
                    printed = cell.get("printed") or "not_printed"
                    quote = cell.get("quote") or row_quote
                    headers = (cell.get("analyte_as_printed"), *HEADER_GUESSES.get(fieldname, ()))
                    col_x = _column_x(loc, headers, row_loc)
                    text_cell = fieldname in ("description", "lith_code", "other")
                    if printed == "printed" and value_printed:
                        located = loc.locate_value(str(value_printed), row_loc, column_x=col_x,
                                                   allow_multiline=text_cell)
                    else:
                        located = loc.empty_cell(row_loc, col_x)
                        stats["empty_cells"] += 1

                    unit_printed = cell.get("unit_as_printed")
                    unit_source = cell.get("unit_source") or "not_printed"
                    unit_norm = None
                    unit_rule = ""
                    if fieldname in DEPTH_FIELDS:
                        unit_norm, unit_printed, unit_source, unit_rule = _resolve_depth_unit(
                            cell, table, page_level)
                    elif fieldname == "grade":
                        unit_norm, unit_rule = parse_grade_unit(unit_printed or cell.get("analyte_as_printed"))

                    parsed = parse_number(value_printed) if printed == "printed" else parse_number(None)
                    status = "pass"
                    if printed == "illegible":
                        status = "flag"
                    elif printed == "not_printed":
                        status = "flag" if fieldname in DEPTH_FIELDS or fieldname == "grade" else "pass"
                    elif cell.get("uncertain"):
                        status = "flag"

                    vid = b.extracted(
                        key_parts=[res["page_id"], t_index, r_index, c_index, fieldname, value_printed,
                                   cell.get("analyte_as_printed")],
                        as_printed=value_printed, unit_as_printed=unit_printed,
                        value=parsed.value if parsed.value is not None else (
                            parsed.non_numeric if parsed.non_numeric else value_printed),
                        lineage=_lineage(page_row, res, quote, located, unit_source),
                        status=status,
                        note=(f"{unit_rule}" if unit_rule and unit_norm else None),
                        meta={
                            "field": fieldname, "scope": "cell", "page_id": res["page_id"], "page": page_no,
                            "pdf_sha256": pdf_sha, "printed": printed, "uncertain": bool(cell.get("uncertain")),
                            "table_id": table_id, "row": r_index, "cell": c_index,
                            "analyte_as_printed": cell.get("analyte_as_printed"),
                            "unit_norm": unit_norm, "unit_rule": unit_rule, "unit_source": unit_source,
                            "qualifier": parsed.qualifier, "non_numeric": parsed.non_numeric,
                            "below_detection": parsed.below_detection, "parse_rule": parsed.rule,
                            "locate": located.as_dict(), "digit_agreement": located.digit_agreement,
                            "row_quote": row_quote, "row_locate": row_loc.as_dict(),
                            "hole_id": hole["hole_id"] if hole else None,
                            "table_kind": table.get("kind"),
                        },
                    )
                    stats["values"] += 1
                    stats["located"] += 1 if located.located else 0
                    built.setdefault(fieldname, []).append({
                        "vid": vid, "value": parsed.value, "unit": unit_norm, "printed": printed,
                        "as_printed": value_printed, "analyte": cell.get("analyte_as_printed"),
                        "unit_as_printed": unit_printed, "unit_source": unit_source,
                        "qualifier": parsed.qualifier, "below_detection": parsed.below_detection,
                    })

                if hole is None:
                    continue
                if hole["name_vid"] is None:
                    # the name's quote is the text it was actually printed in: the row when the row
                    # prints it, the page header when it was taken from there
                    if source == "row":
                        name_quote = row_quote or hole["name_as_printed"]
                        name_loc = loc.locate_value(str(hole["name_as_printed"]),
                                                    row_loc if row_loc.bbox else None)
                    else:
                        header_field = page_level.get("hole_id") or {}
                        name_quote = header_field.get("quote") or hole["name_as_printed"]
                        name_loc = loc.locate_value(str(hole["name_as_printed"]))
                    hole["name_vid"] = b.extracted(
                        key_parts=[res["page_id"], t_index, r_index, "hole_name", hole["name_as_printed"]],
                        as_printed=hole["name_as_printed"], value=hole["name_as_printed"],
                        unit_as_printed=None,
                        lineage=_lineage(page_row, res, name_quote, name_loc, None),
                        meta={"field": "hole_name", "scope": "hole", "page_id": res["page_id"], "page": page_no,
                              "pdf_sha256": pdf_sha, "printed": "printed", "uncertain": False,
                              "hole_id": hole["hole_id"], "hole_id_source": source,
                              "locate": name_loc.as_dict(), "digit_agreement": name_loc.digit_agreement,
                              "table_id": table_id, "row": r_index},
                    )
                    stats["values"] += 1
                    stats["located"] += 1 if name_loc.located else 0

                kind = table.get("kind") or "other"
                _attach_row(b, hole, kind, built, table_id, r_index, res, page_level, page_fields)

                # collar fields printed in a row (a collar table has one hole per row)
                for fname in COLLAR_FIELDS:
                    for item in built.get(fname, []):
                        hole["collar"].setdefault(fname, item["vid"])
                        hole["collar_meta"].setdefault(fname, item)

            tables_out.append({
                "table_id": table_id, "page_id": res["page_id"], "pdf_sha256": pdf_sha, "page": page_no,
                "kind": table.get("kind") or "other",
                "bbox": _union_boxes(table_boxes),
                "rows_stored": len(rows), "rows_printed": printed_rows,
                "truncated": bool(table.get("truncated")),
                "continued_from_prev": bool(table.get("continued_from_prev")),
                "continues_on_next": bool(table.get("continues_on_next")),
                "title_as_printed": table.get("title_as_printed"),
                "column_headers_as_printed": table.get("column_headers_as_printed") or [],
                "chain_id": res.get("chain_id"), "chain_pos": res.get("chain_pos"),
                "route_class": res.get("route_class"),
            })

        # A datum or UTM zone printed once on a page applies to every hole logged on that page. It is
        # attached with unit_source "page_note", never invented, and V10 still checks it was printed.
        for key in ("datum", "utm_zone"):
            if key not in page_fields:
                continue
            for hole in holes.values():
                if page_no in hole["pages"]:
                    hole["collar"].setdefault(key, page_fields[key])

        # a page header with collar fields but no table row still describes a hole
        if header_hole:
            hole = hole_for(header_hole, "page_header", page_no)
            if hole is not None:
                if hole["name_vid"] is None:
                    hole["name_vid"] = page_fields.get("hole_id")
                for key in ("datum", "utm_zone"):
                    if key in page_fields:
                        hole["collar"].setdefault(key, page_fields[key])
                hole.setdefault("page_level", {}).update(page_fields)

    # continuation links between tables
    by_chain: dict[str, list[dict[str, Any]]] = {}
    for t in tables_out:
        if t["chain_id"]:
            by_chain.setdefault(t["chain_id"], []).append(t)
    for chain in by_chain.values():
        chain.sort(key=lambda t: (t["page"], t["table_id"]))
        for prev, nxt in zip(chain, chain[1:], strict=False):
            nxt["continues_from"] = prev["table_id"] if nxt["continued_from_prev"] or prev["continues_on_next"] else None
    for t in tables_out:
        t.setdefault("continues_from", None)

    report_sha = next((r["pdf_sha256"] for r in sorted(results, key=lambda r: int(r["page_no"]))), "")
    return {
        "version": ASSEMBLE_VERSION, "pipeline_version": __version__, "file_num": file_num,
        "file_sha256": report_sha,
        "assembled_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "pages": page_notes, "tables": tables_out,
        "holes": [holes[k] for k in sorted(holes)],
        "values": b.values, "value_meta": b.meta, "stats": stats,
    }


def _union_boxes(boxes: list[list[float]]) -> list[float] | None:
    if not boxes:
        return None
    return [round(min(b[0] for b in boxes), 5), round(min(b[1] for b in boxes), 5),
            round(max(b[2] for b in boxes), 5), round(max(b[3] for b in boxes), 5)]


def _attach_row(b: Builder, hole: dict[str, Any], kind: str, built: dict[str, list[dict[str, Any]]],
                table_id: str, r_index: int, res: dict[str, Any], page_level: dict[str, Any],
                page_fields: dict[str, str]) -> None:
    """Turn one transcribed row into a lithology or assay interval, with derived metres."""
    def first(*names: str) -> dict[str, Any] | None:
        for n in names:
            if built.get(n):
                return built[n][0]
        return None

    frm = first("from_depth", "sample_from_depth")
    to = first("to_depth", "sample_to_depth")
    grades = built.get("grade", [])
    if frm is None and to is None and not grades:
        return

    def metres(item: dict[str, Any] | None, label: str) -> str | None:
        if item is None:
            return None
        value_m, op, params = to_metres(item["value"], item["unit"])
        if value_m is None:
            return None
        return b.derived(
            key_parts=[table_id, r_index, label, item["vid"]], value=value_m, fmt="m1", op=op,
            inputs=[item["vid"]], params={**params, "unit_source": item["unit_source"]}, unit="m",
            note=("converted from feet at 0.3048 m/ft" if op == "ft_to_m" else
                  ("the printed unit is metres" if item["unit"] == "m" else
                   "no depth unit is printed on this page: the printed number is carried through unchanged")),
        )

    from_m = metres(frm, "from_m")
    to_m = metres(to, "to_m")
    depth_unit_printed = (frm or to or {}).get("unit_as_printed")

    interval_id = f"{hole['hole_id']}:{table_id.rsplit(':', 2)[-2]}:{r_index}"
    common = {
        "id": interval_id, "from": frm["vid"] if frm else None, "to": to["vid"] if to else None,
        "from_m": from_m, "to_m": to_m, "table_id": table_id, "row": r_index, "status": "pass",
        "depth_unit_as_printed": depth_unit_printed,
        "from_value": frm["value"] if frm else None, "to_value": to["value"] if to else None,
        "from_m_value": None, "to_m_value": None,
        "depth_unit": (frm or to or {}).get("unit"),
        "page": int(res["page_no"]), "page_id": res["page_id"],
    }
    if from_m:
        common["from_m_value"] = b.values[from_m]["value"]
    if to_m:
        common["to_m_value"] = b.values[to_m]["value"]

    if kind in ("assay", "probe") or grades:
        sample = first("sample_id")
        width = first("interval_width", "sample_width")
        species_printed = [g.get("analyte") for g in grades]
        page_species = (page_level.get("species") or {}).get("value_as_printed")
        page_basis = (page_level.get("basis") or {}).get("value_as_printed")
        page_method = (page_level.get("method") or {}).get("value_as_printed")
        out_grades = []
        for g in grades:
            sp, sp_rule = parse_species(g.get("analyte"), page_species)
            ba, ba_rule = parse_basis(g.get("analyte"), page_basis, page_method,
                                      "probe" if kind == "probe" else None)
            out_grades.append({
                "value": g["vid"], "analyte_as_printed": g.get("analyte"), "species": sp, "basis": ba,
                "method_as_printed": page_method,
                "unit": g.get("unit"), "unit_as_printed": g.get("unit_as_printed"),
                "grade_value": g.get("value"), "qualifier": g.get("qualifier"),
                "below_detection": g.get("below_detection"),
                "species_rule": sp_rule, "basis_rule": ba_rule,
            })
        hole["assays"].append({**common, "sample_id": sample["vid"] if sample else None,
                               "width": width["vid"] if width else None,
                               "grades": out_grades, "kind": kind,
                               "species_printed": [s for s in species_printed if s]})
    else:
        code = first("lith_code")
        desc = first("description")
        hole["lith"].append({**common, "code": code["vid"] if code else None,
                             "description": desc["vid"] if desc else None, "kind": "lith"})


# --------------------------------------------------------------------------------- stage

def stage_assemble(files: list[str] | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    results = read_results()
    if not results:
        raise RuntimeError("no extraction results; run `ue extract` first")
    page_rows = {r["page_id"]: r for r in read_pages()}
    by_file: dict[str, list[dict[str, Any]]] = {}
    for row in results.values():
        if files and row["file_num"] not in files:
            continue
        by_file.setdefault(row["file_num"], []).append(row)

    assemble_dir().mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"files": {}}
    for file_num in sorted(by_file):
        log(f"  {file_num}: {len(by_file[file_num])} pages")
        doc = assemble_file(file_num, by_file[file_num], page_rows, log=log)
        path = assembled_path(file_num)
        tmp = path.with_suffix(".part")
        tmp.write_text(json.dumps(doc, separators=(",", ":"), default=str))
        tmp.replace(path)
        s = doc["stats"]
        located_share = s["located"] / s["values"] if s["values"] else 0.0
        summary["files"][file_num] = {
            "pages": len(doc["pages"]), "tables": len(doc["tables"]), "holes": len(doc["holes"]),
            "values": s["values"], "located": s["located"], "located_share": round(located_share, 3),
            "rows_stored": s["rows"], "rows_printed": s["printed_rows"],
            "lith": sum(len(h["lith"]) for h in doc["holes"]),
            "assays": sum(len(h["assays"]) for h in doc["holes"]),
            "path": str(path.relative_to(PATHS.pipeline)),
        }
        log(f"    {len(doc['holes'])} holes, {len(doc['tables'])} tables, {s['rows']}/{s['printed_rows']} rows, "
            f"{s['values']} values ({located_share:.0%} with a located quote)")
    return summary
