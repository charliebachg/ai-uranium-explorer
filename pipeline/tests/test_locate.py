"""The locator, against the real Apple Vision words of a real page.

Page 4 of 74H09-0039 is a 1978 landscape drill-log sheet. Three things about it make it the right test:
the footage is in feet, the collar is on a local grid with no printed datum, and Apple Vision merges
several pairs of printed depths into one token ("42.0.43.5" for a printed "42.0  43.5"), which is the
disagreement the demo has to report rather than hide.
"""

from __future__ import annotations

import pytest

from uranium_explorer.locate import (
    PageLocator,
    bare,
    digit_agreement,
    fold,
    norm,
    squash,
)
from uranium_explorer.ocr import read_words

from fake_page import PAGE_NO, PDF_SHA


@pytest.fixture(scope="module")
def locator() -> PageLocator:
    words = [w for w in read_words(PDF_SHA) if w["page_no"] == PAGE_NO and w["engine"] == "livetext"]
    if not words:
        pytest.skip(f"no OCR words for {PDF_SHA[:12]} page {PAGE_NO}; run `ue ocr`")
    return PageLocator(words)


def test_normalisation_is_applied_to_both_readers():
    assert norm("  U3O8 – GRADE ") == "u3o8 - grade"
    assert squash("1, 753, 200") == "1,753,200"
    assert bare("1,753,200E;") == bare("1.753.200e")


def test_confusables_fold_the_ocr_side_only():
    assert fold("O.5") == "0.5"          # OCR read a letter O where the page printed a zero
    assert fold("l41.5") == "141.5"
    assert squash("O.5") != "0.5", "the model's characters are never folded"


def test_digit_agreement_separates_exact_confusable_and_mismatch():
    assert digit_agreement("141.5", "141.5") == "exact"
    assert digit_agreement("141.5", "l41.5") == "confusable"
    assert digit_agreement("153.6", "154.4") == "mismatch"
    assert digit_agreement("Regolith", "Regolith") == "na"


def test_a_header_value_in_feet_is_located_on_its_own_line(locator):
    found = locator.locate_value("197.0")
    assert found.located and found.method == "exact"
    assert found.digit_agreement == "exact"
    # the printed "LENGTH 197.0 ft" line sits in the upper third of the sheet
    assert 0.29 < found.bbox[1] < 0.35
    assert found.bbox[2] - found.bbox[0] < 0.1


def test_the_hole_identifier_is_located(locator):
    found = locator.locate_value("R-78-27")
    assert found.located and found.digit_agreement == "exact"
    assert found.bbox is not None


def test_a_local_grid_coordinate_with_separators_is_located(locator):
    row = locator.locate_row("GRID LATITUDE 1,753,200E; 20,960,600 N")
    assert row.bbox is not None
    east = locator.locate_value("1,753,200E", row)
    assert east.located, east.note
    assert east.note == "separators differ between the two readings"
    assert east.digit_agreement == "exact"
    north = locator.locate_value("20,960,600", row)
    assert north.located and north.digit_agreement == "exact"
    assert north.bbox[0] > east.bbox[0], "the northing is printed to the right of the easting"


def test_a_row_quote_lands_on_the_right_printed_line(locator):
    row = locator.locate_row("141.5 - 142.9 Regolith")
    assert row.bbox is not None and row.method in ("exact", "fuzzy")
    value = locator.locate_value("142.9", row)
    assert value.located
    # the value's box sits inside the row's vertical band
    assert row.bbox[1] - 0.005 <= value.bbox[1] and value.bbox[3] <= row.bbox[3] + 0.005


def test_the_merged_token_case_is_located_and_reported(locator):
    """Apple Vision returns "42.0.43.5" for a printed "42.0   43.5": one token, two printed values."""
    row = locator.locate_row("42.0 43.5 Gritty sandstone: c.g., buff-white.")
    assert row.bbox is not None
    frm = locator.locate_value("42.0", row)
    to = locator.locate_value("43.5", row)
    for found, printed in ((frm, "42.0"), (to, "43.5")):
        assert found.located, printed
        assert found.ocr_text == "42.0.43.5"
        assert found.note == "OCR merged this value with an adjacent one into one token"
        assert found.method == "fuzzy"
    assert frm.bbox == to.bbox, "both printed values are inside the one OCR token, so they share its box"


def test_a_value_the_second_reader_read_differently_is_a_mismatch(locator):
    """OCR reads "241.5" where the sheet prints "141.5" in the sample footage column."""
    row = locator.locate_row("217 141.5 146.5 0.9 110")
    assert row.bbox is not None
    assert "241.5" in row.ocr_text
    assert digit_agreement("141.5", "241.5") == "mismatch"


def test_a_value_that_is_not_on_the_page_falls_back_to_the_row_and_says_so(locator):
    row = locator.locate_row("141.5 - 142.9 Regolith")
    found = locator.locate_value("9999.9", row)
    assert not found.located
    assert found.method == "row_anchor"
    assert "not found" in found.note


def test_a_cell_box_is_never_wider_than_60_percent_of_the_page(locator):
    found = locator.locate_value("Apeh bian Basement: Metarkose.")
    assert found.bbox is None or found.bbox[2] - found.bbox[0] <= 0.60


def test_an_empty_printed_cell_gets_a_box_from_its_column_header(locator):
    row = locator.locate_row("216 136.5 141.5 0.8 20")
    assert row.bbox is not None
    col = locator.column_x("Ni", above_y=row.bbox[1])
    assert col is not None, "the Ni column header is printed on this sheet"
    empty = locator.empty_cell(row, col)
    assert empty.bbox is not None
    assert empty.located is False, "nothing was matched, so the quote is not located"
    assert empty.digit_agreement == "na"
    assert empty.method == "row_anchor"
    # the synthesised box sits on the row's y and under the column's x
    assert empty.bbox[1] == row.bbox[1] and empty.bbox[3] == row.bbox[3]
    assert col[0] - 0.1 < (empty.bbox[0] + empty.bbox[2]) / 2 < col[1] + 0.1


def test_an_empty_cell_with_no_column_header_falls_back_to_the_row(locator):
    row = locator.locate_row("216 136.5 141.5 0.8 20")
    empty = locator.empty_cell(row, None)
    assert empty.bbox == list(row.bbox)
    assert "column header not located" in empty.note


def test_locate_records_ambiguity(locator):
    """"37.0" is printed twice on this sheet (the summary log and the detailed log)."""
    found = locator.locate_value("37.0")
    assert found.located
    assert found.ambiguous is True


def test_an_empty_page_locates_nothing():
    empty = PageLocator([])
    assert empty.locate_row("anything").bbox is None
    assert empty.locate_value("141.5").bbox is None
    assert empty.column_x("FROM") is None
