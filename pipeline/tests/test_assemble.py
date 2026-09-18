"""Assembly: hole-id carry, the feet-to-metres derivation, and the shape the later stages read."""

from __future__ import annotations

import pytest

from legacy_reader.assemble import assemble_file
from legacy_reader.extract import carry_from_result
from legacy_reader.normalise import (
    FT_TO_M,
    basis,
    depth_unit,
    grade_unit,
    normalise_hole_name,
    parse_number,
    species,
    to_metres,
)
from legacy_reader.ocr import read_words

from fake_page import ASSAY_ROWS, FILE_NUM, LITH_ROWS, PAGE_ID, PAGE_NO, PDF_SHA, extract_row, page_result


@pytest.fixture(scope="module")
def ocr_present() -> bool:
    if not [w for w in read_words(PDF_SHA) if w["page_no"] == PAGE_NO]:
        pytest.skip("OCR words missing; run `lr ocr`")
    return True


def build(rows) -> dict:
    pages = {r["page_id"]: {"width_px": 1700, "height_px": 2200} for r in rows}
    return assemble_file(FILE_NUM, rows, pages, log=lambda *a: None)


def test_a_page_becomes_one_hole_with_its_intervals(ocr_present):
    doc = build([extract_row()])
    assert len(doc["holes"]) == 1
    hole = doc["holes"][0]
    assert hole["hole_id"] == "R7827" and hole["name_as_printed"] == "R-78-27"
    assert len(hole["lith"]) == len(LITH_ROWS)
    assert len(hole["assays"]) == len(ASSAY_ROWS)
    assert doc["stats"]["rows"] == len(LITH_ROWS) + len(ASSAY_ROWS)
    assert doc["stats"]["printed_rows"] == doc["stats"]["rows"]
    assert doc["stats"]["located"] / doc["stats"]["values"] > 0.7


def test_feet_are_converted_once_into_a_separate_derived_value(ocr_present):
    doc = build([extract_row()])
    interval = doc["holes"][0]["lith"][1]
    printed = doc["values"][interval["from"]]
    derived = doc["values"][interval["from_m"]]
    assert printed["as_printed"] == "37.0" and printed["value"] == 37.0
    assert printed["unit_as_printed"] == "ft", "the printed unit survives untouched"
    assert derived["kind"] == "derived"
    assert derived["derivation"]["op"] == "ft_to_m"
    assert derived["derivation"]["params"]["factor"] == FT_TO_M
    assert derived["derivation"]["inputs"] == [interval["from"]]
    assert derived["value"] == pytest.approx(37.0 * FT_TO_M, abs=1e-3)
    assert derived["unit"] == "m"
    assert interval["depth_unit"] == "ft" and interval["depth_unit_as_printed"] == "ft"
    assert doc["values"][interval["from"]]["lineage"]["unit_source"] == "column_header"


def test_metres_and_an_unprinted_unit_are_both_recorded_honestly():
    assert to_metres(100.0, "ft")[0] == pytest.approx(30.48)
    assert to_metres(100.0, "ft")[1] == "ft_to_m"
    assert to_metres(100.0, "m") == (100.0, "identity", {"from_unit": "m", "to_unit": "m"})
    value, op, params = to_metres(100.0, None)
    assert value == 100.0 and op == "identity"
    assert params["from_unit"] == "not_printed" and "flagged by V01" in params["assumption"]


def test_the_hole_id_comes_from_the_page_header_when_no_row_prints_one(ocr_present):
    doc = build([extract_row()])
    assert doc["holes"][0]["hole_id_source"] == "page_header"


def test_a_row_that_prints_its_own_hole_id_wins(ocr_present):
    result = page_result()
    result["tables"][0]["rows"][0]["hole_id_as_printed"] = "R-78-99"
    doc = build([extract_row(result)])
    ids = {h["hole_id"]: h["hole_id_source"] for h in doc["holes"]}
    assert ids["R7899"] == "row"
    assert "R7827" in ids


def test_a_continuation_page_carries_the_previous_page_s_hole_id(ocr_present):
    first = extract_row(chain_id="c1", chain_pos=1)
    follow = page_result()
    follow["page_level"]["hole_id"] = {"value_as_printed": None, "unit_as_printed": None,
                                       "printed": "not_printed", "source": "not_printed", "quote": None}
    follow["tables"][0]["continued_from_prev"] = True
    second = extract_row(follow, page_no=PAGE_NO + 1, chain_id="c1", chain_pos=2)
    doc = build([first, second])
    holes = {h["hole_id"]: h for h in doc["holes"]}
    assert set(holes) == {"R7827"}, "both pages describe the same hole"
    assert sorted(holes["R7827"]["pages"]) == [PAGE_NO, PAGE_NO + 1]
    assert holes["R7827"]["hole_id_source"] == "page_header"
    # the second page's rows were attached through the carry, not by inventing a name
    assert len(holes["R7827"]["lith"]) == 2 * len(LITH_ROWS)
    tables = [t for t in doc["tables"] if t["page"] == PAGE_NO + 1 and t["kind"] == "lith"]
    assert tables and tables[0]["continues_from"] is not None


def test_carry_text_is_printed_facts_with_their_page_and_quote():
    carry = carry_from_result(page_result(), PAGE_NO)
    labels = {c.label: c for c in carry}
    assert labels["hole identifier"].value == "R-78-27"
    assert labels["hole identifier"].page == PAGE_NO
    assert labels["hole identifier"].quote == "HOLE NO - R-78-27"
    assert "FROM" in labels["column headers"].value
    assert all(c.quote for c in carry), "nothing is carried without the text it was printed in"
    assert not any(FILE_NUM in c.value for c in carry)


def test_an_empty_printed_cell_keeps_its_place_and_is_flagged(ocr_present):
    doc = build([extract_row()])
    grades = doc["holes"][0]["assays"][0]["grades"]
    u = next(g for g in grades if g["analyte_as_printed"] == "U")
    value = doc["values"][u["value"]]
    assert value["as_printed"] is None and value["value"] is None
    assert value["status"] == "flag"
    assert value["lineage"]["bbox"] is not None, "a not-printed cell still points at the page"
    assert value["lineage"]["quote_located"] is False
    assert doc["value_meta"][u["value"]]["printed"] == "not_printed"


def test_tables_record_both_row_counts_and_the_continuation_flags(ocr_present):
    doc = build([extract_row(page_result(truncate_rows=2, printed_row_count=5))])
    lith = next(t for t in doc["tables"] if t["kind"] == "lith")
    assert lith["rows_printed"] == 5 and lith["rows_stored"] == 3
    assert lith["truncated"] is True
    assert lith["continues_on_next"] is True
    assert lith["bbox"] is not None


def test_value_ids_are_namespaced_stable_and_unique(ocr_present):
    a = build([extract_row()])
    b = build([extract_row()])
    assert set(a["values"]) == set(b["values"]), "the same page gives the same value ids"
    assert all(v.startswith(("x:74H09-0039:", "d:74H09-0039:")) for v in a["values"])
    assert len(a["values"]) == len(set(a["values"]))


def test_qualifiers_and_non_numeric_answers_are_preserved():
    assert parse_number("<0.01").qualifier == "<"
    assert parse_number("<0.01").value == 0.01
    assert parse_number("<0.01").below_detection is True
    assert parse_number("tr").non_numeric == "tr" and parse_number("tr").value is None
    assert parse_number("nil").below_detection is True
    assert parse_number("1,753,200").value == 1753200.0
    assert parse_number(None).value is None


def test_unit_species_and_basis_come_only_from_printed_text():
    assert depth_unit("ft")[0] == "ft" and depth_unit("metres")[0] == "m"
    assert depth_unit("")[0] is None and depth_unit("banana")[0] is None
    assert grade_unit("ppm")[0] == "ppm" and grade_unit("%")[0] == "%"
    assert species("U3O8 %")[0] == "U3O8"
    assert species("U ppm")[0] == "U"
    assert species("eU3O8")[0] == "eU3O8"
    assert species(None)[0] == "not_printed"
    assert species("Ni")[0] == "other"
    assert basis("U3O8 assay")[0] == "chemical"
    assert basis("eU3O8 from probe")[0] == "probe_equivalent"
    assert basis(None)[0] == "not_printed"


def test_hole_names_are_normalised_only_for_matching():
    assert normalise_hole_name("R-78-27") == normalise_hole_name("R 78 27")
    assert normalise_hole_name("KL005") == normalise_hole_name("KL-5")
    assert normalise_hole_name("Q9-6") == "Q96"
    assert normalise_hole_name(None) == ""
