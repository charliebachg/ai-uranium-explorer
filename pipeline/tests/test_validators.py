"""V01 to V20, one seeded error at a time, against a document that is otherwise clean.

The seeds are the traps the spec's gold set is built around: feet read as metres, ppm read as percent,
percent U stored as percent U3O8, a probe reading stored as a chemical assay, 28 printed rows with 25
stored, a datum the page never printed, swapped easting and northing, "<0.01" turned into 0, a
duplicated hole, and a value that is not inside its own quote.
"""

from __future__ import annotations

import pytest

from uranium_explorer.validators import (
    REGISTRY,
    SHADOW_MODE_IDS,
    apply_findings,
    run_validators,
)

from doc_builder import TABLE, Doc, clean


def fired(doc, ctx, vid: str | None = None):
    findings = run_validators(doc, ctx)
    return [f for f in findings if vid is None or f.validator == vid]


def errors(doc, ctx):
    return [f for f in run_validators(doc, ctx) if f.severity == "error" or f.outcome == "fail"]


# ------------------------------------------------------------------ the clean case

def test_all_twenty_validators_are_registered():
    assert sorted(REGISTRY) == [f"V{n:02d}" for n in range(1, 21)]
    for vid, v in REGISTRY.items():
        assert v.severity in ("info", "warn", "error"), vid
        assert v.title and v.version


def test_a_clean_table_produces_no_error():
    doc, ctx = clean()
    assert errors(doc, ctx) == [], [f.message for f in errors(doc, ctx)]


def test_a_clean_table_leaves_every_value_passing():
    doc, ctx = clean()
    apply_findings(doc, run_validators(doc, ctx))
    assert doc["validators"]["flagged_values"] == 0
    assert {v["status"] for v in doc["values"].values() if v["kind"] == "extracted"} == {"pass"}


# ------------------------------------------------------------------ V01 depth units

def test_v01_flags_an_interval_with_no_printed_depth_unit():
    d = Doc()
    hole = d.hole()
    d.interval(hole, "lith", 0.0, 10.0, unit=None, unit_printed=None)
    found = fired(d.doc, {}, "V01")
    assert found and "no depth unit is printed" in found[0].message
    assert found[0].value_ids


def test_v01_catches_feet_read_as_metres_against_the_province():
    """The province logs 60.4 m; feet stored as metres make the hole 3.28 times too deep."""
    d = Doc()
    hole = d.hole()
    d.interval(hole, "lith", 0.0, 198.0, unit="m", unit_printed="m")  # printed feet, stored as metres
    ctx = {"geods_holes": [{"id": 1, "name": "R-78-27", "total_depth_m": 60.35}], "compilation_holes": []}
    found = [f for f in fired(d.doc, ctx, "V01") if f.outcome == "fail"]
    assert found, "a 3.28 ratio against the provincial depth is the feet-as-metres signature"
    assert found[0].class_a is True and found[0].severity == "error"
    assert found[0].details["ratio"] == pytest.approx(3.28, abs=0.05)
    assert "read as metres" in found[0].message


def test_v01_stays_quiet_when_the_same_hole_is_logged_in_feet_properly():
    d = Doc()
    hole = d.hole()
    d.interval(hole, "lith", 0.0, 198.0, unit="ft", unit_printed="ft")
    ctx = {"geods_holes": [{"id": 1, "name": "R-78-27", "total_depth_m": 60.35}], "compilation_holes": []}
    assert [f for f in fired(d.doc, ctx, "V01") if f.outcome == "fail"] == []


# ------------------------------------------------------------------ V02 grade units

def test_v02_flags_a_grade_with_no_printed_unit():
    d = Doc()
    hole = d.hole()
    g = d.grade(hole, "0.35", 0.35, unit=None, unit_norm=None, analyte="U3O8", species="U3O8",
                basis="chemical")
    d.interval(hole, "assays", 10.0, 11.0, grades=[g])
    found = fired(d.doc, {}, "V02")
    assert found and "no grade unit" in found[0].message


def test_v02_fails_a_percentage_above_one_hundred():
    """ppm read as percent: 1250 ppm U3O8 transcribed into a column headed %."""
    d = Doc()
    hole = d.hole()
    g = d.grade(hole, "1250", 1250.0, unit="%", analyte="U3O8 %", species="U3O8", basis="chemical")
    d.interval(hole, "assays", 10.0, 11.0, grades=[g])
    found = [f for f in fired(d.doc, {}, "V02") if f.outcome == "fail"]
    assert found and found[0].class_a is True
    assert "greater than 100" in found[0].message


def test_v02_cross_checks_ppm_u_against_percent_u3o8_on_the_same_interval():
    """% U stored as % U3O8: the printed ppm U says 0.2360 %, the printed percent says 0.2000."""
    d = Doc()
    hole = d.hole()
    ppm = d.grade(hole, "2000", 2000.0, unit="ppm", analyte="U ppm", species="U", basis="chemical")
    pct = d.grade(hole, "0.20", 0.20, unit="%", analyte="U3O8 %", species="U3O8", basis="chemical")
    d.interval(hole, "assays", 10.0, 11.0, grades=[ppm, pct])
    found = [f for f in fired(d.doc, {}, "V02") if "U3O8" in f.message and "ppm" in f.message]
    assert found and found[0].outcome == "fail" and found[0].class_a is True
    assert found[0].details["pct_u3o8_implied"] == pytest.approx(0.2358, abs=1e-3)
    assert found[0].details["pct_u_implied"] == pytest.approx(0.2000, abs=1e-4)
    assert found[0].details["factor"] == 1.17924
    assert "never to rewrite one" in found[0].details["note"]

    # the consistent pair raises nothing
    d2 = Doc()
    h2 = d2.hole()
    ok_ppm = d2.grade(h2, "2000", 2000.0, unit="ppm", analyte="U ppm", species="U", basis="chemical")
    ok_pct = d2.grade(h2, "0.236", 0.236, unit="%", analyte="U3O8 %", species="U3O8", basis="chemical")
    d2.interval(h2, "assays", 10.0, 11.0, grades=[ok_ppm, ok_pct])
    assert [f for f in fired(d2.doc, {}, "V02") if "ppm" in f.message] == []


# ------------------------------------------------------------------ V03 species

def test_v03_flags_a_grade_whose_species_the_page_never_printed():
    d = Doc()
    hole = d.hole()
    g = d.grade(hole, "0.35", 0.35, unit="%", analyte=None, species="not_printed", basis="chemical")
    d.interval(hole, "assays", 10.0, 11.0, grades=[g])
    found = fired(d.doc, {}, "V03")
    assert found and "does not print which species" in found[0].message


# ------------------------------------------------------------------ V04 basis

def test_v04_fails_a_probe_reading_stored_as_a_chemical_assay():
    d = Doc()
    hole = d.hole()
    g = d.grade(hole, "250", 250.0, unit="cps", analyte="probe peak (counts/second)", species="not_printed",
                basis="chemical", method="ra probe")
    d.interval(hole, "assays", 10.0, 11.0, grades=[g])
    found = [f for f in fired(d.doc, {}, "V04") if f.outcome == "fail"]
    assert found and found[0].class_a is True and found[0].severity == "error"
    assert "basis 'chemical'" in found[0].message


def test_v04_flags_a_probe_equivalent_stored_under_a_chemical_species():
    d = Doc()
    hole = d.hole()
    g = d.grade(hole, "0.35", 0.35, unit="%", analyte="eU3O8 %", species="U3O8",
                basis="probe_equivalent")
    d.interval(hole, "assays", 10.0, 11.0, grades=[g])
    found = [f for f in fired(d.doc, {}, "V04") if f.outcome == "flag"]
    assert found and "rather than eU3O8" in found[0].message


# ------------------------------------------------------------------ V05 to V08 depth geometry

def test_v05_fails_an_inverted_interval_and_flags_a_zero_length_one():
    d = Doc()
    hole = d.hole()
    d.interval(hole, "lith", 20.0, 10.0, row=0)
    d.interval(hole, "lith", 30.0, 30.0, row=1)
    found = fired(d.doc, {}, "V05")
    assert [f for f in found if f.outcome == "fail"] and [f for f in found if f.outcome == "flag"]


def test_v06_flags_an_overlap_but_allows_a_nested_including_interval():
    d = Doc()
    hole = d.hole()
    d.interval(hole, "lith", 0.0, 10.0, unit="m", unit_printed="m", row=0)
    d.interval(hole, "lith", 8.0, 18.0, unit="m", unit_printed="m", row=1)
    assert fired(d.doc, {}, "V06")

    nested = Doc()
    h = nested.hole()
    nested.interval(h, "assays", 0.0, 10.0, unit="m", unit_printed="m", row=0)
    nested.interval(h, "assays", 3.0, 5.0, unit="m", unit_printed="m", row=1)
    assert fired(nested.doc, {}, "V06") == [], "an 'including' interval inside a wider one is normal"


def test_v07_flags_a_printed_width_that_does_not_match_the_depths():
    d = Doc()
    hole = d.hole()
    d.interval(hole, "assays", 10.0, 11.5, width=5.0, grades=[])
    found = fired(d.doc, {}, "V07")
    assert found and found[0].details["to_minus_from"] == 1.5

    ok = Doc()
    h = ok.hole()
    ok.interval(h, "assays", 10.0, 11.5, width=1.5, grades=[])
    assert fired(ok.doc, {}, "V07") == []


def test_v08_fails_an_interval_past_the_printed_total_depth():
    d = Doc()
    hole = d.hole()
    td = d.value("total_depth", "100.0", 100.0, unit="m", unit_norm="m", hole_id=hole["hole_id"])
    hole["collar"]["total_depth"] = td
    d.interval(hole, "lith", 0.0, 150.0, unit="m", unit_printed="m")
    found = [f for f in fired(d.doc, {}, "V08") if f.outcome == "fail"]
    assert found and "total depth" in found[0].message
    assert found[0].details["total_depth_source"] == "printed on the page"


# ------------------------------------------------------------------ V09 row count

def test_v09_fails_when_two_counters_agree_that_rows_are_missing():
    """28 printed rows, 25 stored: the model's own count and the page geometry both say so."""
    d = Doc()
    d.table(stored=25, printed=28)
    ctx = {"geometric_rows": {TABLE: 28}}
    found = [f for f in fired(d.doc, ctx, "V09") if f.outcome == "fail"]
    assert found and found[0].class_a is True
    assert found[0].details == {"stored": 25, "model_count": 28, "geometric": 28,
                                "agreed_by": ["model_count", "geometric"]}
    assert "rows are missing" in found[0].message


def test_v09_only_flags_when_one_counter_disagrees():
    d = Doc()
    d.table(stored=25, printed=28)
    found = fired(d.doc, {"geometric_rows": {TABLE: 25}}, "V09")
    assert found and found[0].outcome == "flag" and found[0].class_a is False


def test_v09_is_silent_when_all_three_counters_agree():
    d = Doc()
    d.table(stored=28, printed=28)
    assert fired(d.doc, {"geometric_rows": {TABLE: 28}}, "V09") == []


def test_v09_can_be_shadowed_so_it_records_without_flagging():
    d = Doc()
    hole = d.hole()
    g = d.grade(hole, "0.35", 0.35, unit="%", analyte="U3O8 %", species="U3O8", basis="chemical")
    d.interval(hole, "assays", 10.0, 11.0, grades=[g], width=1.0)
    d.table(stored=25, printed=28)
    ctx = {"geometric_rows": {TABLE: 28}}
    findings = run_validators(d.doc, ctx)
    assert "V09" in SHADOW_MODE_IDS

    shadowed = apply_findings({**d.doc, "values": {k: dict(v) for k, v in d.doc["values"].items()}},
                              findings, shadow=SHADOW_MODE_IDS)
    assert shadowed["validators"]["counts"]["V09"] > 0, "the finding is still recorded"
    assert not any("V09" in [o["id"] for o in (v.get("lineage") or {}).get("validators", [])]
                   for v in shadowed["values"].values()), "but no value carries it"

    enforced = apply_findings({**d.doc, "values": {k: dict(v) for k, v in d.doc["values"].items()}},
                              findings, shadow=())
    assert any("V09" in [o["id"] for o in (v.get("lineage") or {}).get("validators", [])]
               for v in enforced["values"].values())


# ------------------------------------------------------------------ V10 to V12 coordinates

def test_v10_flags_coordinates_printed_without_a_datum():
    d = Doc()
    hole = d.hole()
    hole["collar"] = {"easting": d.value("easting", "612345", 612345.0, hole_id="R7827"),
                      "northing": d.value("northing", "6431234", 6431234.0, hole_id="R7827")}
    found = fired(d.doc, {}, "V10")
    assert found and "no datum is printed" in found[0].message


def test_v10_fails_an_asserted_datum():
    """The page prints no datum but the position was transformed anyway."""
    d = Doc()
    hole = d.hole()
    hole["collar"] = {"easting": d.value("easting", "612345", 612345.0, hole_id="R7827")}
    ctx = {"positions": {"holes": [{"hole_id": "R7827", "status": "transformed",
                                    "position_source": "extracted_transformed", "lonlat": [-104.5, 57.5],
                                    "value_ids": [], "checks": {}}]}}
    found = [f for f in fired(d.doc, ctx, "V10") if f.outcome == "fail"]
    assert found and found[0].class_a is True
    assert "although the page prints none" in found[0].message


def test_v10_flags_a_datum_whose_quote_could_not_be_located():
    d = Doc()
    hole = d.hole()
    hole["collar"] = {"datum": d.value("datum", "NAD 27", "NAD 27", hole_id="R7827", located=False)}
    found = fired(d.doc, {}, "V10")
    assert found and "could not be located" in found[0].message


def test_v11_fails_a_transformed_collar_outside_the_files_sheets():
    doc, ctx = clean()
    ctx["positions"]["holes"][0]["checks"]["inside_nts"] = False
    found = [f for f in fired(doc, ctx, "V11") if f.outcome == "fail"]
    assert found and "outside the file's NTS sheets" in found[0].message


def test_v12_fails_swapped_easting_and_northing():
    d = Doc()
    hole = d.hole()
    hole["collar"] = {"easting": d.value("easting", "6431234", 6431234.0, hole_id="R7827"),
                      "northing": d.value("northing", "612345", 612345.0, hole_id="R7827"),
                      "datum": d.value("datum", "NAD 27", "NAD 27", hole_id="R7827")}
    found = [f for f in fired(d.doc, {}, "V12") if f.outcome == "fail"]
    assert found and "look swapped" in found[0].message


def test_v12_flags_coordinates_outside_saskatchewan():
    d = Doc()
    hole = d.hole()
    hole["collar"] = {"latitude": d.value("latitude", "37.5", 37.5, hole_id="R7827")}
    found = fired(d.doc, {}, "V12")
    assert found and "outside Saskatchewan" in found[0].message


# ------------------------------------------------------------------ V13 text layer

def test_v13_flags_a_disagreement_with_the_pdfs_own_text_layer():
    d = Doc()
    vid = d.value("from_depth", "121.7", 121.7, hole_id="R7827")
    d.doc["value_meta"][vid]["locate"]["bbox"] = [0.10, 0.20, 0.20, 0.22]
    ctx = {"text_layer": {("a" * 64, 4): [{"text": "L2L.-7", "x0": 0.11, "y0": 0.205, "x1": 0.19, "y1": 0.215}]}}
    found = fired(d.doc, ctx, "V13")
    assert found and "text layer reads" in found[0].message
    assert found[0].details["text_layer"] == "27"


def test_v13_is_silent_when_the_text_layer_agrees():
    d = Doc()
    vid = d.value("from_depth", "121.7", 121.7, hole_id="R7827")
    d.doc["value_meta"][vid]["locate"]["bbox"] = [0.10, 0.20, 0.20, 0.22]
    ctx = {"text_layer": {("a" * 64, 4): [{"text": "121.7", "x0": 0.11, "y0": 0.205, "x1": 0.19, "y1": 0.215}]}}
    assert fired(d.doc, ctx, "V13") == []


# ------------------------------------------------------------------ V14 duplicates

def test_v14_flags_the_same_hole_stored_twice():
    d = Doc()
    d.hole("R7827")
    d.hole("R7827", name="R 78 27")
    found = fired(d.doc, {}, "V14")
    assert found and "appears 2 times" in found[0].message


def test_v14_flags_conflicting_printed_collar_numbers_for_one_hole():
    d = Doc()
    hole = d.hole()
    a = d.value("total_depth", "197.0", 197.0, hole_id="R7827")
    d.value("total_depth", "179.0", 179.0, hole_id="R7827")
    hole["collar"]["total_depth"] = a
    found = [f for f in fired(d.doc, {}, "V14") if f.details.get("field") == "total_depth"]
    assert found and sorted(found[0].details["values"]) == [179.0, 197.0]


# ------------------------------------------------------------------ V15 and V16 evidence

def test_v15_fails_a_value_that_is_not_inside_its_own_quote():
    d = Doc()
    d.hole()
    d.value("from_depth", "141.5", 141.5, quote="37.0 - 142.9 Regolith", hole_id="R7827")
    found = [f for f in fired(d.doc, {}, "V15") if f.outcome == "fail"]
    assert found and found[0].class_a is True
    assert "does not appear in its own quote" in found[0].message


def test_v15_accepts_separator_differences_between_value_and_quote():
    d = Doc()
    d.hole()
    d.value("easting", "1,753,200", 1753200.0, quote="GRID LATITUDE 1.753.200E; 20,960,600 N",
            hole_id="R7827")
    assert [f for f in fired(d.doc, {}, "V15") if f.outcome == "fail"] == []


def test_v16_flags_an_ocr_digit_disagreement_and_notes_a_confusable_one():
    d = Doc()
    d.hole()
    d.value("from_depth", "153.6", 153.6, digit_agreement="mismatch", ocr_text="154.4", hole_id="R7827")
    d.value("to_depth", "141.5", 141.5, digit_agreement="confusable", ocr_text="l41.5", hole_id="R7827")
    found = {f.severity: f for f in fired(d.doc, {}, "V16")}
    assert "warn" in found and found["warn"].class_a is True
    assert "154.4" in found["warn"].message and "check by eye" in found["warn"].message
    assert "info" in found and found["info"].class_a is False


# ------------------------------------------------------------------ V17 qualifiers

def test_v17_fails_a_below_detection_result_stored_as_zero():
    d = Doc()
    hole = d.hole()
    g = d.grade(hole, "<0.01", 0.0, unit="%", analyte="U3O8 %", species="U3O8", basis="chemical",
                qualifier="<", below_detection=True)
    d.interval(hole, "assays", 10.0, 11.0, grades=[g])
    found = [f for f in fired(d.doc, {}, "V17") if f.outcome == "fail"]
    assert found and found[0].class_a is True
    assert "not zero" in found[0].message


def test_v17_fails_a_qualifier_that_was_thrown_away():
    d = Doc()
    d.hole()
    d.value("grade", "<0.01", 0.01, qualifier=None, hole_id="R7827")
    found = [f for f in fired(d.doc, {}, "V17") if "none was recorded" in f.message]
    assert found and found[0].class_a is True


def test_v17_accepts_a_preserved_qualifier():
    d = Doc()
    hole = d.hole()
    g = d.grade(hole, "<0.01", 0.01, unit="%", analyte="U3O8 %", species="U3O8", basis="chemical",
                qualifier="<", below_detection=True)
    d.interval(hole, "assays", 10.0, 11.0, grades=[g])
    assert [f for f in fired(d.doc, {}, "V17") if f.outcome == "fail"] == []


def test_v17_fails_a_non_numeric_answer_stored_as_a_number():
    d = Doc()
    d.hole()
    d.value("grade", "tr", 0.0, non_numeric="tr", hole_id="R7827")
    found = [f for f in fired(d.doc, {}, "V17") if f.outcome == "fail"]
    assert found and "is not a number" in found[0].message


# ------------------------------------------------------------------ V18 to V20

def test_v18_flags_a_dip_and_an_azimuth_out_of_range():
    d = Doc()
    hole = d.hole()
    hole["collar"] = {"dip": d.value("dip", "-170", -170.0, hole_id="R7827"),
                      "azimuth": d.value("azimuth", "460", 460.0, hole_id="R7827")}
    messages = " ".join(f.message for f in fired(d.doc, {}, "V18"))
    assert "dip -170.0 is outside" in messages and "azimuth 460.0 is outside" in messages


def test_v19_records_that_a_hole_id_was_carried():
    d = Doc()
    d.hole(source="carried")
    found = fired(d.doc, {}, "V19")
    assert found and found[0].severity == "info"
    assert "the previous page of this table" in found[0].message

    own = Doc()
    own.hole(source="row")
    assert fired(own.doc, {}, "V19") == []


def test_v20_flags_fewer_holes_than_the_work_text_and_the_province_list():
    d = Doc()
    d.hole()
    ctx = {"work": {"hole_count": 6, "hole_names": ["R-78-27", "R-78-28"]},
           "geods_holes": [{"id": 1, "name": "R-78-27"}, {"id": 2, "name": "R-78-28"}]}
    messages = [f.message for f in fired(d.doc, ctx, "V20")]
    assert any("lists 6 drillholes" in m for m in messages)
    assert any("no extracted collar" in m for m in messages)


# ------------------------------------------------------------------ applying findings

def test_a_finding_flags_its_value_and_never_drops_it():
    d = Doc()
    hole = d.hole()
    g = d.grade(hole, "1250", 1250.0, unit="%", analyte="U3O8 %", species="U3O8", basis="chemical")
    interval = d.interval(hole, "assays", 10.0, 11.0, grades=[g])
    before = len(d.doc["values"])
    apply_findings(d.doc, run_validators(d.doc, {}))
    assert len(d.doc["values"]) == before, "nothing is ever dropped"
    value = d.doc["values"][g["value"]]
    assert value["status"] == "flag"
    assert value["as_printed"] == "1250", "the printed value is untouched"
    assert [o["id"] for o in value["lineage"]["validators"]] == ["V02"]
    assert value["lineage"]["validators"][0]["class_a"] is True
    assert interval["status"] == "flag" and hole["status"] == "flag"
