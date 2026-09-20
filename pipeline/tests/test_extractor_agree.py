"""The agreement rule: what counts as the same value, what counts as equal, and how the rates are tallied."""

from __future__ import annotations

import pytest

from uranium_explorer.extractor import agree as AG
from uranium_explorer.locate import PageLocator

from fake_page import page_result


def cand(field: str, value: str, bbox=None, analyte=None, row=None, vid=None) -> AG.Candidate:
    return AG.Candidate(field=field, scope="cell", as_printed=value, analyte=analyte, bbox=bbox, row_index=row,
                        value_id=vid)


# ---------------------------------------------------------------- equality


@pytest.mark.parametrize("a, b, equal", [
    ("478.6", "478.60", True),        # the second reader printed a trailing zero: the same number
    ("478.6", "478.5", False),        # a tenth apart at one printed decimal: different
    ("1,753", "1753", True),          # a thousands separator is not a disagreement about the number
    ("136", "136.4", True),           # the first reader printed no decimals: within half a unit
    ("136", "136.6", False),
    ("<0.01", "0.01", False),         # below detection against a measurement
    ("<0.01", "< 0.01", True),
    ("tr", "TR", True),               # a printed qualifier compares as text
    ("tr", "nil", False),
])
def test_numeric_equality_is_held_to_the_first_readers_precision(a: str, b: str, equal: bool) -> None:
    assert AG.values_equal(a, b, numeric=True)[0] is equal


@pytest.mark.parametrize("a, b, equal", [
    ("R-78-27", "R 78 27", True),
    ("R-78-27", "R-78-28", False),
    ("Conglomerate: buff-yellow white", "conglomerate buff yellow white", True),
    ("NAD 27", "NAD27", True),
])
def test_text_equality_folds_case_whitespace_and_separators(a: str, b: str, equal: bool) -> None:
    assert AG.values_equal(a, b, numeric=False)[0] is equal


def test_field_types_split_numbers_from_text() -> None:
    assert AG.is_numeric("from_depth") and AG.is_numeric("grade") and AG.is_numeric("easting")
    assert not AG.is_numeric("description") and not AG.is_numeric("hole_id") and not AG.is_numeric("datum")
    assert AG.field_type("hole_name") == "identifier" and AG.field_type("nonsense") == "text"


# ---------------------------------------------------------------- alignment


def test_values_are_paired_by_field_and_overlapping_box() -> None:
    a = [cand("to_depth", "42.0", bbox=[0.30, 0.20, 0.36, 0.22]), cand("to_depth", "43.5", bbox=[0.30, 0.23, 0.36, 0.25])]
    b = [cand("to_depth", "43.5", bbox=[0.31, 0.23, 0.35, 0.25]), cand("to_depth", "42.5", bbox=[0.30, 0.20, 0.36, 0.22])]
    pairs = AG.align(a, b)
    by_a = {p.a.as_printed: p for p in pairs if p.a}
    assert by_a["42.0"].status == "disagreed" and by_a["42.0"].b.as_printed == "42.5" and by_a["42.0"].matched_by == "box"
    assert by_a["43.5"].status == "agreed" and by_a["43.5"].matched_by == "box"


def test_two_located_cells_that_do_not_overlap_are_never_paired_by_text() -> None:
    """The same number in two different cells is two values: one found only by each reader."""
    a = [cand("grade", "20", bbox=[0.50, 0.20, 0.54, 0.22], analyte="Ni")]
    b = [cand("grade", "20", bbox=[0.50, 0.40, 0.54, 0.42], analyte="Ni")]
    statuses = sorted(p.status for p in AG.align(a, b))
    assert statuses == ["only_a", "only_b"]


def test_a_value_whose_quote_did_not_locate_can_only_agree_by_text() -> None:
    a = [cand("sample_id", "216", bbox=None), cand("sample_id", "217", bbox=None)]
    b = [cand("sample_id", "217", bbox=[0.1, 0.1, 0.2, 0.12]), cand("sample_id", "219", bbox=[0.1, 0.2, 0.2, 0.22])]
    pairs = AG.align(a, b)
    got = {(p.status, p.matched_by, (p.a or p.b).as_printed) for p in pairs}
    assert got == {("agreed", "text", "217"), ("only_a", "none", "216"), ("only_b", "none", "219")}


def test_grades_are_grouped_by_analyte_and_hole_names_by_hole_id() -> None:
    a = [cand("grade", "20", bbox=[0.5, 0.2, 0.54, 0.22], analyte="U ppm"),
         AG.Candidate(field="hole_name", scope="hole", as_printed="R-78-27", bbox=[0.1, 0.05, 0.2, 0.07])]
    b = [cand("grade", "20", bbox=[0.5, 0.2, 0.54, 0.22], analyte="Ni ppm"),
         cand("hole_id", "R 78 27", bbox=[0.1, 0.05, 0.2, 0.07])]
    pairs = AG.align(a, b)
    grades = [p for p in pairs if p.field == "grade"]
    assert sorted(p.status for p in grades) == ["only_a", "only_b"], "a U grade and a Ni grade are not the same value"
    names = [p for p in pairs if p.field == "hole_id"]
    assert len(names) == 1 and names[0].status == "agreed"


# ---------------------------------------------------------------- tallies


def test_the_rate_is_over_the_union_and_the_pair_rate_over_matches() -> None:
    pairs = [AG.Pair("agreed", cand("from_depth", "1"), cand("from_depth", "1"), "box"),
             AG.Pair("agreed", cand("grade", "2", analyte="U"), cand("grade", "2", analyte="U"), "box"),
             AG.Pair("disagreed", cand("grade", "3", analyte="U"), cand("grade", "4", analyte="U"), "box"),
             AG.Pair("only_a", cand("description", "x"), None, "none"),
             AG.Pair("only_b", None, cand("grade", "5", analyte="U"), "none")]
    t = AG.tally(pairs)
    assert t["n"] == 5 and t["agreed"] == 2 and t["rate"] == 0.4 and t["pair_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert t["by_field_type"]["grade"] == {"agreed": 1, "disagreed": 1, "only_a": 0, "only_b": 1, "n": 3,
                                           "rate": pytest.approx(1 / 3, abs=1e-4), "pair_rate": 0.5}
    merged = AG.merge_tallies([t, t])
    assert merged["pages"] == 2 and merged["n"] == 10 and merged["rate"] == 0.4
    assert merged["by_field_type"]["text"]["only_a"] == 2


def test_an_empty_page_has_no_rate_rather_than_a_perfect_one() -> None:
    t = AG.tally([])
    assert t["n"] == 0 and t["rate"] is None and t["pair_rate"] is None


# ---------------------------------------------------------------- candidates from a wire result


def test_candidates_from_a_wire_result_carry_every_printed_value_and_no_blank_cell() -> None:
    result = page_result()
    values = AG.candidates_from_wire(result, PageLocator([]), model="m", prompt_version="p", run_id="r")
    fields = sorted({c.field for c in values})
    assert "grade" in fields and "from_depth" in fields and "description" in fields and "hole_id" in fields
    assert all(c.as_printed for c in values), "a not-printed cell is not a candidate"
    assert all(c.bbox is None for c in values), "no OCR words: no box, and nothing invented"
    page_level = [c for c in values if c.scope == "page_level"]
    assert {c.field for c in page_level} == {"hole_id", "depth_unit"}
    assert all(c.model == "m" and c.run_id == "r" for c in values)


def test_candidates_from_an_assembled_document_keep_the_store_ids_and_located_boxes() -> None:
    doc = {
        "values": {
            "x:f:1": {"kind": "extracted", "as_printed": "42.0", "lineage": {"quote": "37.0 42.0", "bbox": [0.1, 0.1, 0.2, 0.12],
                                                                        "quote_located": True, "model": "claude-opus-5",
                                                                        "prompt_version": "pv", "run_id": "run1"}},
            "x:f:2": {"kind": "extracted", "as_printed": "37.0", "lineage": {"quote": "37.0 42.0", "bbox": [0.0, 0.1, 0.5, 0.12],
                                                                        "quote_located": False}},
            "x:f:3": {"kind": "extracted", "as_printed": None, "lineage": {}},
            "d:f:4": {"kind": "derived", "value": 12.8},
        },
        "value_meta": {
            "x:f:1": {"field": "to_depth", "scope": "cell", "page": 4, "printed": "printed", "table_id": "t:f:ab:0004:1", "row": 1},
            "x:f:2": {"field": "from_depth", "scope": "cell", "page": 4, "printed": "printed", "table_id": "t:f:ab:0004:1", "row": 1},
            "x:f:3": {"field": "grade", "scope": "cell", "page": 4, "printed": "not_printed"},
        },
    }
    values = {c.value_id: c for c in AG.candidates_from_assembled(doc, 4)}
    assert set(values) == {"x:f:1", "x:f:2"}
    assert values["x:f:1"].bbox == [0.1, 0.1, 0.2, 0.12] and values["x:f:1"].model == "claude-opus-5"
    assert values["x:f:2"].bbox is None, "a row-anchor box is not a located quote and must not pair by box"
    assert values["x:f:1"].table_index == 1 and values["x:f:1"].row_index == 1
    assert AG.candidates_from_assembled(doc, 5) == []


def test_a_hole_name_the_assembler_minted_beside_a_hole_id_cell_counts_once() -> None:
    """The assembler files the row's printed name twice (the cell and the hole's name value); the second
    reader transcribes it once, so the comparison must too, or every hole is a phantom disagreement."""
    lin = {"quote": "MN-001 7500 1200", "bbox": [0.1, 0.2, 0.15, 0.22], "quote_located": True}
    doc = {
        "values": {"x:f:cell": {"kind": "extracted", "as_printed": "MN-001", "lineage": lin},
                   "x:f:name": {"kind": "extracted", "as_printed": "MN-001", "lineage": lin},
                   "x:f:lone": {"kind": "extracted", "as_printed": "MN-002", "lineage": lin}},
        "value_meta": {"x:f:cell": {"field": "hole_id", "scope": "cell", "page": 10, "printed": "printed", "row": 0},
                       "x:f:name": {"field": "hole_name", "scope": "hole", "page": 10, "printed": "printed", "row": 0,
                                    "hole_id_source": "row"},
                       "x:f:lone": {"field": "hole_name", "scope": "hole", "page": 10, "printed": "printed", "row": 1,
                                    "hole_id_source": "row"}},
    }
    got = {c.value_id for c in AG.candidates_from_assembled(doc, 10)}
    assert got == {"x:f:cell", "x:f:lone"}, "the name beside a cell is dropped; a name with no cell stands in"


def test_a_page_level_statement_pairs_by_text_even_when_the_locator_anchored_it_elsewhere() -> None:
    """A unit printed in three column headers locates in three places; the page prints one depth unit."""
    a = [AG.Candidate(field="depth_unit", scope="page_level", as_printed="m", bbox=[0.1, 0.1, 0.12, 0.12])]
    b = [AG.Candidate(field="depth_unit", scope="page_level", as_printed="m", bbox=[0.5, 0.1, 0.52, 0.12])]
    pairs = AG.align(a, b)
    assert len(pairs) == 1 and pairs[0].status == "agreed" and pairs[0].matched_by == "text"
