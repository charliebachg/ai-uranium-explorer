"""Assay spreadsheets into the native tier: header finding, column roles, printed values, and what is refused."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from legacy_reader.assay_sheets import find_header, map_columns, parse_number, read_certificate, read_sheet, stage_assay_sheets


def fission_like() -> pd.DataFrame:
    """Header on row 0, as Fission's 2012 sheet: Lab Tag_ID, DDH, From, To, Interval, ..., U3O8 wt%."""
    return pd.DataFrame([
        ["Lab Tag_ID", "DDH", "From", "To", "Interval", "Sample Type", "U3O8 wt% ICP", "Pb ppm", "U ppm ICP"],
        [65701, "PLS12-017", 101, 101.5, 0.5, "Normal", 0.018, 12, 152],
        [65702, "PLS12-017", 103.5, 104, 0.5, "Normal", "<0.005", 9, "<10"],
        [65703, "PLS12-017", 106, 106.5, 0.5, "Normal", None, None, None],
        [None, None, None, None, None, None, None, None, None],
    ], dtype=object)


def horseshoe_like() -> pd.DataFrame:
    """Header on row 3 after a title block, as the Horseshoe selective geochem sheet."""
    return pd.DataFrame([
        ["Horseshoe selective geochemistry", None, None, None, None, None],
        [None, None, None, None, None, None],
        ["units", None, None, "m", "m", "%"],
        ["Hole", "Sample", "From", "To", "Interval_Width", "U3O8_ICPOES%"],
        ["HU-001", 1001, 300.0, 300.5, 0.5, 0.412],
        ["HU-001", 1002, 300.5, 301.0, 0.5, 0.09],
    ], dtype=object)


def certificate_like() -> pd.DataFrame:
    """A lab certificate sheet: sample ids and U3O8 only, no hole and no depths. Still ingested, hole null."""
    return pd.DataFrame([
        ["SRC Geoanalytical Laboratories", None], ["Samples: 2", None],
        ["Sample", "U3O8 %"], ["G-12-795-1", 0.031], ["G-12-795-2", "<0.001"],
    ], dtype=object)


def test_column_roles_are_found_on_a_fission_header() -> None:
    roles = map_columns(fission_like().iloc[0].tolist())
    assert roles["hole"] == 1 and roles["sample"] == 0 and roles["from"] == 2 and roles["to"] == 3
    assert roles["interval"] == 4 and roles["u3o8"] == 6 and roles["u_ppm"] == 8


def test_the_header_row_is_found_below_a_title_block() -> None:
    h, roles = find_header(horseshoe_like())
    assert h == 3 and roles["hole"] == 0 and roles["u3o8"] == 5


def test_rows_keep_the_printed_value_and_flag_below_detection() -> None:
    meta, rows = read_sheet("MAW00509", "abc", "DDH Core Assay Results.xlsx", "Sheet1", fission_like(), "t")
    assert meta["status"] == "ingested" and meta["header_row"] == 0 and meta["n_rows"] == 3
    assert rows[0]["hole_id"] == "PLS12-017" and rows[0]["from_m"] == 101 and rows[0]["to_m"] == 101.5
    assert rows[0]["u3o8_pct"] == 0.018 and rows[0]["u_ppm"] == 152 and rows[0]["below_detection"] is False
    assert rows[1]["u3o8_pct"] == 0.005 and rows[1]["u3o8_as_printed"] == "<0.005" and rows[1]["below_detection"] is True
    assert rows[2]["u3o8_pct"] is None and rows[2]["from_m"] == 106, "a depth row with no assay is still a sample interval"
    assert rows[0]["u3o8_column"] == "u3o8 wt% icp"
    assert len({r["row_id"] for r in rows}) == 3


def test_a_certificate_sheet_is_ingested_by_sample_with_no_hole() -> None:
    meta, rows = read_sheet("64L04-0130", "def", "G-08-1223U3O8.xls", "G-08-1223", certificate_like(), "t")
    assert meta["status"] == "ingested" and len(rows) == 2
    assert rows[0]["hole_id"] is None and rows[0]["sample_id"] == "g-12-795-1" and rows[0]["u3o8_pct"] == 0.031
    assert rows[1]["below_detection"] is True


def test_a_sheet_with_no_usable_header_is_recorded_not_dropped() -> None:
    df = pd.DataFrame([["Notes", "for", "the", "geologist"], ["nothing", "numeric", "here", None]], dtype=object)
    meta, rows = read_sheet("X", "ghi", "notes.xls", "Notes", df, "t")
    assert meta["status"] == "no_header" and rows == [] and "no row names" in meta["note"]


def test_parse_number_forms() -> None:
    assert parse_number(0.5) == (0.5, "0.5", False)
    assert parse_number("<0.01") == (0.01, "<0.01", True)
    assert parse_number("1,5") == (1.5, "1,5", False)
    assert parse_number("n/a") == (None, "n/a", False)
    assert parse_number(None) == (None, "", False)


def test_the_stage_reads_the_manifest_and_can_run_without_writing(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "MAW00509" / "assays").mkdir(parents=True)
    fission_like().to_excel(raw / "MAW00509" / "assays" / "a.xlsx", header=False, index=False)
    (raw / "manifest.json").write_text(json.dumps({
        "MAW00509/assays/a.xlsx": {"file_num": "MAW00509", "kind": "assay_xls", "name": "a.xlsx", "sha256": "s1"},
        "MAW00509/report.pdf": {"file_num": "MAW00509", "kind": "report_pdf", "name": "report.pdf", "sha256": "s2"},
    }))
    out = stage_assay_sheets(log=lambda *a: None, raw_dir=raw, write=False)
    assert out["sheets"] == 1 and out["by_status"] == {"ingested": 1} and out["rows"] == 3
    assert out["by_file"] == {"MAW00509": 3}


def test_fission_2012_headers_map_the_test_report_column_and_guess_the_house_hole_name() -> None:
    df = pd.DataFrame([
        ["Lab Tag_ID", "Fission_ID", "From", "To", "Interval", "Lithology", "U; ICP ICP1 Total Digestion (ppm)", "U3O8 TEST REPORT (wt%)"],
        [17801, "PLS12-001", 66.14, 66.64, 0.5, "Basement", 152, 0.018],
        [17802, "PLS12-001", 70.65, 70.93, 0.28, "Basement", 12, "<0.005"],
        [17803, "PLS12-002", 71.64, 72.14, 0.5, "Basement", 9, 0.02],
    ], dtype=object)
    h, roles = find_header(df)
    assert h == 0 and roles["u3o8"] == 7 and roles["u_ppm"] == 6
    assert roles["hole"] == 1, "Fission_ID is the hole column, by the look of its values"
    _, rows = read_sheet("MAW00131", "x", "2012 Drill Core Assay Results.xls", "PLS", df, "t")
    assert [r["hole_id"] for r in rows] == ["PLS12-001", "PLS12-001", "PLS12-002"]
    assert rows[0]["u3o8_pct"] == 0.018 and rows[0]["u_ppm"] == 152 and rows[0]["kind"] == "interval"


def test_src_certificate_form_is_read_with_analyte_unit_and_qc_rows_flagged() -> None:
    df = pd.DataFrame([
        ["SRC Geoanalytical Laboratories", None, None, None],
        ["Company", "NexGen Energy", None, None], ["Group", "G-2019-1009", None, None], ["Samples", "3", None, None],
        ["Analyte", "U3O8", None, None], ["Unit", "wt %", None, None], ["Detection", "0.001", None, None],
        [None, None, None, None],
        ["Description", "Sample Type", "Preparation Code", None],
        ["BL4A ", "Standard", 0.148, None],
        ["144586 ", "Basement", "C/S/A", 0.004],
        ["144587 ", "Basement", "C/S/A", "<0.001"],
    ], dtype=object)
    assert find_header(df) is None, "not a columns sheet"
    block, raw = read_certificate(df)
    assert block["analyte"] == "u3o8" and block["unit"] == "wt %" and block["detection"] == "0.001" and len(raw) == 3
    meta, rows = read_sheet("MAW02845", "y", "G-2019-1009.xls", "Sheet1", df, "t")
    assert meta["status"] == "ingested" and meta["format"] == "certificate" and meta["n_rows"] == 3
    std, a, b = rows
    assert std["sample_type"] == "standard" and std["u3o8_pct"] == 0.148, "QC rows are kept and labelled, not silently dropped"
    assert a["sample_id"] == "144586" and a["u3o8_pct"] == 0.004 and a["hole_id"] is None and a["kind"] == "certificate"
    assert b["below_detection"] is True and b["u3o8_as_printed"] == "<0.001"
    assert a["u3o8_column"] == "u3o8 (wt %)"


def test_src_certificate_with_a_blank_first_column_still_reads() -> None:
    """The real SRC sheets leave column A empty; every cell sits one column to the right."""
    df = pd.DataFrame([
        [None, "SRC Geoanalytical Laboratories", None, None, None],
        [None, "Analyte", "U3O8", None, None], [None, "Unit", "wt %", None, None],
        [None, "Description", "Sample Type", "Preparation Code", None],
        [None, "144586 ", "Basement", "C/S/A", 0.004],
        [None, "144587 ", "Basement", "C/S/A", "<0.001"],
    ], dtype=object)
    meta, rows = read_sheet("MAW02845", "z", "G-2019-1010.xls", "Sheet1", df, "t")
    assert meta["status"] == "ingested" and meta["format"] == "certificate" and len(rows) == 2
    assert rows[0]["sample_id"] == "144586" and rows[0]["u3o8_pct"] == 0.004 and rows[0]["sample_type"] == "basement"
