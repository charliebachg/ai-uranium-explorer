"""worktext parsing, on the real strings from the provincial layers."""

from uranium_explorer.worktext import first_year, join_work_fields, parse_work_text

MAW02110 = ("Drilling: 27 DH (# SB-10 to SB-16, SP-03 to SP-12, LS-87 to LS-96), 5339.3 m. Analysis: 623 "
            "lithogeochemical assay, 1691 spectral clay analysis, 49 core samples studied for resistivity and "
            "chargeability. Geophysics: Borehole EM survey conducted on hole SB-11. Small Moving Loop Transient "
            "Electromagnetics (ML-TEM) survey (45.09 Lkm, 50m line spacing, 200 X 200 m loop size) 1 USB, 565 MB")
AE = ("5 ddh records, gamma and E logs (#AE-1 to 5 - Ahenakew East) Assays Exploration report, map 1 in = 400 ft "
      "NOTE: CBS 2658 later ML 5225")
MFU = "Drilling: 4DH (#MFU-MC-006 to 009), 2468m, Analyses: all probed excpet 006), core samples: 187 for lithogeochem"


def test_modern_drilling_sentence():
    w = parse_work_text(MAW02110)
    assert w.hole_count == 27
    assert w.metres == 5339.3
    assert len(w.hole_names) == 27 and w.hole_names_complete
    assert w.hole_names[:2] == ["SB-10", "SB-11"] and w.hole_names[-1] == "LS-96"
    assert w.assay and w.geochem and w.drilling
    assert w.feet is None
    # "565 MB" must not become metres, and the geophysics line spacing must not either
    assert w.metres == 5339.3


def test_historic_ddh_records_with_range_and_label():
    w = parse_work_text(AE)
    assert w.hole_count == 5
    assert w.hole_names == ["AE-1", "AE-2", "AE-3", "AE-4", "AE-5"]
    assert w.hole_names_complete
    assert w.assay and not w.geochem


def test_short_range_and_probed_flag():
    w = parse_work_text(MFU)
    assert (w.hole_count, w.metres, w.probed) == (4, 2468.0, True)
    assert w.hole_names == ["MFU-MC-006", "MFU-MC-007", "MFU-MC-008", "MFU-MC-009"]


def test_dash_only_end_of_range():
    w = parse_work_text("8 ddh (# KN-01 to -08): gamma logged: S-101300 Drill core petrographic study")
    assert w.hole_count == 8
    assert w.hole_names == [f"KN-0{i}" for i in range(1, 9)]
    assert w.probed


def test_percussion_holes_counted_separately():
    w = parse_work_text("1 ddh record and probe log (# 78-1) Drilling report 20 pdh, probe logs, sections "
                        "(# 78-2, 4 to 7) Assays: U3O8")
    assert w.hole_count == 1
    assert w.percussion_count == 20
    assert w.u3o8 and w.probed


def test_repeated_clause_is_counted_once():
    text = ("Drilling: 52 DH, 19,849.7 m (# LE20-30, LE20-31). Drilling: 52 DH, 19,849.7 m (# LE20-30, LE20-31).")
    w = parse_work_text(text)
    assert w.hole_count == 52
    assert w.metres == 19849.7


def test_two_separate_drilling_mentions_add_up():
    w = parse_work_text("6 ddh records and sections (# NL-1 to 6) Drilling report by I H Milne "
                        "14 ddh records and sections 1951 drilling report by A B Ferguson")
    assert w.hole_count == 20
    assert not w.hole_names_complete  # only the first six names are printed


def test_no_drilling_no_count():
    w = parse_work_text("Fixed-wing airborne magnetic survey Report, 6 maps by P Leriche")
    assert w.hole_count is None and not w.drilling


def test_hole_mention_without_a_count_is_not_a_count():
    w = parse_work_text("Borehole EM survey conducted on hole SB-11.")
    assert w.hole_count is None


def test_join_work_fields_fixed_width_split():
    # historic rows were cut at about 104 characters, sometimes mid-word
    assert join_work_fields(["er geochemical sampling S", "ummary report, 6 maps, 12", " ", " "]) == \
        "er geochemical sampling Summary report, 6 maps, 12"
    assert join_work_fields(["Assessment report by Paul Beck Analyses: Cu Pb", "Zn (core) NOTE: later MPP", None, None]) == \
        "Assessment report by Paul Beck Analyses: Cu Pb Zn (core) NOTE: later MPP"
    assert join_work_fields([None, None]) == ""


def test_first_year():
    assert first_year("1971") == 1971
    assert first_year("2008-2009") == 2008
    assert first_year("1953,55") == 1953
    assert first_year("195?") is None
    assert first_year(None) is None
