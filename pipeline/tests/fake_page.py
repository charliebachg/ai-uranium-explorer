"""A synthetic model answer for a real page, so the downstream stages can be tested without a call.

Page 4 of 74H09-0039 is the 1978 Conwest "DIAMOND DRILL HOLE RECORD" sheet for hole R-78-27: footage in
feet, a collar on a local grid with no printed datum, and an assay column that is empty on most rows.
The values below were read off that page by hand; the OCR words they are located against are the real
ones in `data/ocr/<sha>/words.parquet`.
"""

from __future__ import annotations

from typing import Any

PDF_SHA = "a16bba66ed6462ad8f459d364fcdb9ba565c81c5850d10fb4ba7cf05905a0d09"
FILE_NUM = "74H09-0039"
PAGE_NO = 4
PAGE_ID = f"pg:{PDF_SHA[:12]}:{PAGE_NO:04d}"


def _pf(value: str | None, *, printed: str = "printed", source: str = "page_note",
        quote: str | None = None, unit: str | None = None) -> dict[str, Any]:
    return {"value_as_printed": value, "unit_as_printed": unit, "printed": printed, "source": source,
            "quote": quote if quote is not None else value}


def _cell(field: str, value: str | None, quote: str, *, unit: str | None = None,
          unit_source: str = "not_printed", analyte: str | None = None, printed: str = "printed",
          uncertain: bool = False) -> dict[str, Any]:
    return {"field": field, "value_as_printed": value, "unit_as_printed": unit,
            "unit_source": unit_source, "analyte_as_printed": analyte, "quote": quote,
            "printed": printed, "uncertain": uncertain}


LITH_ROWS = [
    ("0.0", "37.0", "Casing"),
    ("37.0", "42.0", "Conglomerate: buff-yellow white: 15% quartz clasts: minor red-brown interstitial hematite."),
    ("42.0", "43.5", "Gritty sandstone: c.g., buff-white."),
    ("43.5", "45.5", "Conglomerate: dark grey, 25% quartz clasts."),
    ("45.5", "53.0", "Pebbly grit: buff-white with occasional scattered quartz clasts (5%)."),
]

ASSAY_ROWS = [
    ("216", "136.5", "141.5", "0.8", "20"),
    ("217", "141.5", "146.5", "0.9", "110"),
]


def page_result(*, truncate_rows: int = 0, printed_row_count: int | None = None) -> dict[str, Any]:
    lith_rows = []
    for i, (frm, to, desc) in enumerate(LITH_ROWS[: len(LITH_ROWS) - truncate_rows]):
        quote = f"{frm} {to} {desc}"
        lith_rows.append({
            "row_index": i, "hole_id_as_printed": None, "row_quote": quote,
            "cells": [
                _cell("from_depth", frm, quote, unit="ft", unit_source="column_header"),
                _cell("to_depth", to, quote, unit="ft", unit_source="column_header"),
                _cell("description", desc, quote),
            ],
        })
    assay_rows = []
    for i, (sample, frm, to, width, ni) in enumerate(ASSAY_ROWS):
        quote = f"{sample} {frm} {to} {width} {ni}"
        assay_rows.append({
            "row_index": i, "hole_id_as_printed": None, "row_quote": quote,
            "cells": [
                _cell("sample_id", sample, quote),
                _cell("sample_from_depth", frm, quote, unit="ft", unit_source="column_header"),
                _cell("sample_to_depth", to, quote, unit="ft", unit_source="column_header"),
                _cell("sample_width", width, quote),
                _cell("grade", None, "the U column of this row is printed but empty",
                      analyte="U", unit="ppm", unit_source="column_header", printed="not_printed"),
                _cell("grade", ni, quote, analyte="Ni", unit="ppm", unit_source="column_header"),
            ],
        })
    return {
        "page_level": {
            "page_kind": "lith_log",
            "hole_id": _pf("R-78-27", source="cell", quote="HOLE NO - R-78-27"),
            "datum": _pf(None, printed="not_printed", source="not_printed", quote=None),
            "utm_zone": _pf(None, printed="not_printed", source="not_printed", quote=None),
            "coordinate_kind": "local_grid",
            "depth_unit": _pf("ft", source="column_header", quote="LENGTH 197.0 ft"),
            "unit_notes": _pf(None, printed="not_printed", source="not_printed", quote=None),
            "species": _pf(None, printed="not_printed", source="not_printed", quote=None),
            "basis": _pf(None, printed="not_printed", source="not_printed", quote=None),
            "method": _pf(None, printed="not_printed", source="not_printed", quote=None),
        },
        "tables": [
            {"table_index": 0, "kind": "lith", "title_as_printed": "DIAMOND DRILL HOLE RECORD",
             "column_headers_as_printed": ["FROM", "TO", "WIDTH", "RCVRY", "DESCRIPTION"],
             "printed_row_count": printed_row_count if printed_row_count is not None else len(LITH_ROWS),
             "continued_from_prev": False, "continues_on_next": True, "truncated": truncate_rows > 0,
             "rows": lith_rows},
            {"table_index": 1, "kind": "assay", "title_as_printed": "ASSAYS ppm",
             "column_headers_as_printed": ["No", "FROM", "TO", "WIDTH", "RCVRY", "U", "Ni"],
             "printed_row_count": len(ASSAY_ROWS), "continued_from_prev": False,
             "continues_on_next": True, "truncated": False, "rows": assay_rows},
        ],
        "legibility_notes": None,
    }


def extract_row(result: dict[str, Any] | None = None, page_no: int = PAGE_NO, chain_id: str | None = None,
                chain_pos: int | None = None) -> dict[str, Any]:
    return {
        "version": "extract/v1", "page_id": f"pg:{PDF_SHA[:12]}:{page_no:04d}", "file_num": FILE_NUM,
        "pdf_sha256": PDF_SHA, "pdf_name": "74H09-0039_diamond_drill_hole_record_101-U2.pdf",
        "page_no": page_no, "image_path": f"pages/{PDF_SHA}/p{page_no:04d}.png",
        "image_sha256": "0" * 64, "route_class": "lith_log", "chain_id": chain_id, "chain_pos": chain_pos,
        "run_id": "test-run", "config_id": "phase2", "cache_key": "k" * 64,
        "model_requested": "claude-sonnet-5", "model_resolved": "claude-sonnet-5",
        "prompt_version": "testprompt", "schema_version": "testschema",
        "extracted_at": "2026-09-18T00:00:00+00:00", "from_cache": False, "num_turns": 4,
        "duration_s": 20.0, "usage": {}, "cost_usd": 0.05, "attempt": 1,
        "result": result if result is not None else page_result(),
    }
