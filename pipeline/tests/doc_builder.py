"""A minimal assembled document, so a validator test can seed exactly one error and nothing else.

`clean()` returns a document that must produce no error-severity finding. Every test then seeds one of
the traps from the spec's gold-set list and asserts which validator catches it.
"""

from __future__ import annotations

from typing import Any

FILE = "74H09-0039"
SHA = "a" * 64
TABLE = f"t:{FILE}:aaaaaaaa:0004:0"


class Doc:
    def __init__(self, file_num: str = FILE):
        self.doc: dict[str, Any] = {
            "version": "assemble/v1", "file_num": file_num, "file_sha256": SHA,
            "pages": [{"page_id": "pg:aaaaaaaaaaaa:0004", "page": 4, "pdf_sha256": SHA,
                       "page_kind": "assay_table", "coordinate_kind": "utm", "route_class": "assay_table",
                       "fields": {}, "legibility_notes": None, "text_layer_words": 0}],
            "tables": [], "holes": [], "values": {}, "value_meta": {}, "stats": {},
        }
        self._n = 0

    # ---- values

    def value(self, field: str, as_printed: str | None, value: Any, *, unit: str | None = None,
              unit_norm: str | None = None, quote: str | None = None, printed: str = "printed",
              hole_id: str | None = None, table_id: str | None = TABLE, row: int | None = 0,
              digit_agreement: str = "exact", ocr_text: str | None = None, located: bool = True,
              qualifier: str | None = None, non_numeric: str | None = None,
              below_detection: bool = False, analyte: str | None = None,
              unit_source: str = "column_header", scope: str = "cell", page: int = 4) -> str:
        self._n += 1
        vid = f"x:{self.doc['file_num']}:{self._n:08x}"
        quote = quote if quote is not None else (as_printed or "")
        self.doc["values"][vid] = {
            "id": vid, "kind": "extracted", "as_printed": as_printed, "value": value,
            "unit_as_printed": unit, "status": "pass",
            "lineage": {"file_num": self.doc["file_num"], "file_sha256": SHA, "page": page,
                        "bbox": [0.1, 0.2, 0.2, 0.22] if located else None, "quote": quote,
                        "quote_located": located, "unit_source": unit_source, "model": "test",
                        "prompt_version": "test", "run_id": "test", "extracted_at": "2026-09-18T00:00:00Z",
                        "validators": []},
        }
        self.doc["value_meta"][vid] = {
            "field": field, "scope": scope, "page": page, "pdf_sha256": SHA, "printed": printed,
            "uncertain": False, "table_id": table_id, "row": row, "hole_id": hole_id,
            "analyte_as_printed": analyte, "unit_norm": unit_norm, "unit_source": unit_source,
            "qualifier": qualifier, "non_numeric": non_numeric, "below_detection": below_detection,
            "digit_agreement": digit_agreement,
            "locate": {"bbox": [0.1, 0.2, 0.2, 0.22] if located else None,
                       "locate_method": "exact" if located else "none", "locate_score": 100.0,
                       "digit_agreement": digit_agreement, "locate_ambiguous": False,
                       "locate_ocr_text": ocr_text if ocr_text is not None else (as_printed or ""),
                       "locate_words": 1, "locate_note": ""},
            "row_quote": quote, "row_locate": {}, "table_kind": "assay",
        }
        return vid

    # ---- records

    def hole(self, hole_id: str = "R7827", *, name: str = "R-78-27", source: str = "row",
             collar: dict[str, str] | None = None) -> dict[str, Any]:
        h = {"hole_id": hole_id, "name_as_printed": name,
             "name_vid": self.value("hole_name", name, name, hole_id=hole_id, scope="hole"),
             "hole_id_source": source, "pages": [4], "collar": collar or {}, "collar_meta": {},
             "lith": [], "assays": [], "names_seen": [name], "status": "pass"}
        self.doc["holes"].append(h)
        return h

    def interval(self, hole: dict[str, Any], group: str, frm: float, to: float, *, unit: str = "ft",
                 unit_printed: str | None = "ft", width: float | None = None,
                 grades: list[dict[str, Any]] | None = None, row: int = 0) -> dict[str, Any]:
        from legacy_reader.normalise import to_metres

        f_vid = self.value("from_depth", f"{frm}", frm, unit=unit_printed, unit_norm=unit,
                           hole_id=hole["hole_id"], row=row,
                           quote=f"{frm} {to}" + (f" {width}" if width is not None else ""))
        t_vid = self.value("to_depth", f"{to}", to, unit=unit_printed, unit_norm=unit,
                           hole_id=hole["hole_id"], row=row,
                           quote=f"{frm} {to}" + (f" {width}" if width is not None else ""))
        fm, _op, _p = to_metres(frm, unit)
        tm, _op, _p = to_metres(to, unit)
        fm_vid = self.derived(f"{f_vid}_m", fm, [f_vid])
        tm_vid = self.derived(f"{t_vid}_m", tm, [t_vid])
        rec: dict[str, Any] = {
            "id": f"{hole['hole_id']}:{group}:{row}", "from": f_vid, "to": t_vid,
            "from_m": fm_vid, "to_m": tm_vid, "table_id": TABLE, "row": row, "status": "pass",
            "depth_unit_as_printed": unit_printed, "from_value": frm, "to_value": to,
            "from_m_value": fm, "to_m_value": tm, "depth_unit": unit, "page": 4,
            "page_id": "pg:aaaaaaaaaaaa:0004",
        }
        if width is not None:
            rec["width"] = self.value("interval_width", f"{width}", width, hole_id=hole["hole_id"], row=row,
                                      quote=f"{frm} {to} {width}")
        if group == "assays":
            rec["sample_id"] = None
            rec["grades"] = grades or []
            rec["kind"] = "assay"
            hole["assays"].append(rec)
        else:
            rec["code"] = None
            rec["description"] = None
            rec["kind"] = "lith"
            hole["lith"].append(rec)
        return rec

    def grade(self, hole: dict[str, Any], as_printed: str | None, value: Any, *, unit: str,
              unit_norm: str | None = None, analyte: str, species: str, basis: str,
              method: str | None = None, qualifier: str | None = None, non_numeric: str | None = None,
              below_detection: bool = False, row: int = 0, printed: str = "printed") -> dict[str, Any]:
        vid = self.value("grade", as_printed, value, unit=unit, unit_norm=unit_norm or unit,
                         analyte=analyte, hole_id=hole["hole_id"], row=row, qualifier=qualifier,
                         non_numeric=non_numeric, below_detection=below_detection, printed=printed,
                         quote=f"{as_printed} {unit}" if as_printed else "the column is empty here")
        return {"value": vid, "analyte_as_printed": analyte, "species": species, "basis": basis,
                "method_as_printed": method, "unit": unit_norm or unit, "unit_as_printed": unit,
                "grade_value": value, "qualifier": qualifier, "below_detection": below_detection,
                "species_rule": "test", "basis_rule": "test"}

    def derived(self, key: str, value: Any, inputs: list[str]) -> str:
        vid = f"d:{self.doc['file_num']}:{abs(hash(key)) % (1 << 40):010x}"
        self.doc["values"][vid] = {
            "id": vid, "kind": "derived", "as_printed": None, "value": value, "unit_as_printed": None,
            "fmt": "m1", "unit": "m",
            "derivation": {"op": "ft_to_m", "inputs": inputs, "tool": "test"}}
        self.doc["value_meta"][vid] = {"kind": "derived", "op": "ft_to_m", "inputs": inputs}
        return vid

    def table(self, *, stored: int, printed: int, truncated: bool = False, kind: str = "assay",
              table_id: str = TABLE) -> dict[str, Any]:
        t = {"table_id": table_id, "page_id": "pg:aaaaaaaaaaaa:0004", "pdf_sha256": SHA, "page": 4,
             "kind": kind, "bbox": [0.05, 0.4, 0.95, 0.95], "rows_stored": stored,
             "rows_printed": printed, "truncated": truncated, "continued_from_prev": False,
             "continues_on_next": False, "title_as_printed": None, "column_headers_as_printed": [],
             "chain_id": None, "chain_pos": None, "route_class": "assay_table", "continues_from": None}
        self.doc["tables"].append(t)
        return t


def clean() -> tuple[dict[str, Any], dict[str, Any]]:
    """A document that no validator should raise an error on."""
    d = Doc()
    hole = d.hole(collar={})
    datum = d.value("datum", "NAD 27", "NAD 27", hole_id=hole["hole_id"], scope="page_level",
                    quote="UTM NAD 27 Zone 13", unit_source="page_note")
    easting = d.value("easting", "612345", 612345.0, hole_id=hole["hole_id"], quote="612345 E")
    northing = d.value("northing", "6431234", 6431234.0, hole_id=hole["hole_id"], quote="6431234 N")
    total = d.value("total_depth", "200.0", 200.0, unit="m", unit_norm="m", hole_id=hole["hole_id"],
                    quote="TOTAL DEPTH 200.0 m")
    dip = d.value("dip", "-70", -70.0, hole_id=hole["hole_id"], quote="DIP -70")
    az = d.value("azimuth", "045", 45.0, hole_id=hole["hole_id"], quote="AZIMUTH 045")
    hole["collar"] = {"datum": datum, "easting": easting, "northing": northing, "total_depth": total,
                      "dip": dip, "azimuth": az}
    d.interval(hole, "lith", 0.0, 10.0, unit="m", unit_printed="m", row=0)
    d.interval(hole, "lith", 10.0, 20.0, unit="m", unit_printed="m", row=1)
    g = d.grade(hole, "0.35", 0.35, unit="%", analyte="U3O8 %", species="U3O8", basis="chemical",
                method="fluorimetry", row=2)
    d.interval(hole, "assays", 12.0, 13.5, unit="m", unit_printed="m", width=1.5, grades=[g], row=2)
    d.table(stored=3, printed=3)
    ctx = {"file_num": FILE, "geods_holes": [], "compilation_holes": [], "nts_sheets": ["74H09"],
           "work": {"hole_count": 1, "hole_names": ["R-78-27"]}, "geometric_rows": {TABLE: 3},
           "text_layer": {}, "positions": {"holes": [
               {"hole_id": "R7827", "status": "transformed", "position_source": "extracted_transformed",
                "lonlat": [-104.5, 57.5], "value_ids": [easting, northing],
                "checks": {"inside_nts": True, "inside_file_polygon": True,
                           "distance_to_file_holes_km": None, "nts_sheets": ["74H09"]}}]}}
    return d.doc, ctx
