"""Selection on a synthetic candidate table: determinism, era counts, company cap, zone 12, split rule."""

from __future__ import annotations

import json
import random

import pytest

from uranium_explorer import select
from uranium_explorer.select import ERAS, choose_phase1, classify_listing, company_key, datum_signal, era_for_year


def candidate(n: int, era: str, company: str, score: float, zone12: bool = False, mb: float = 10.0) -> dict:
    return {"file_num": f"F{n:03d}", "era": era, "score": score, "tiebreak": 0, "pre1968": False,
            "company_key": company, "zone12": zone12, "split_hash": select.split_hash(f"F{n:03d}"),
            "report_total_mb": mb}


def table() -> list[dict]:
    """Six candidates per era; two share a company inside each era, so the cap has something to do."""
    rows = []
    i = 0
    for e, era in enumerate(ERAS):
        for c in range(6):
            i += 1
            rows.append(candidate(i, era, f"CO{e}{min(c, 4)}", score=6 - c,
                                  zone12=(c in (0, 5) and era != "2000s+")))
    return rows


def test_era_buckets():
    assert era_for_year(1978) == ("1970s", False)
    assert era_for_year(1967) == ("1970s", True)
    assert era_for_year(1980) == ("1980s-1990s", False)
    assert era_for_year(1999) == ("1980s-1990s", False)
    assert era_for_year(2000) == ("2000s+", False)
    assert era_for_year(None) == (None, False)


def test_company_key_families():
    assert company_key("ASAMERA OIL CORPORATION LTD") == "ASAMERA"
    assert company_key("ASAMERA INC") == "ASAMERA"
    assert company_key("AMOK LTD-MOKTA CANADA LTD") == "AMOK"
    assert company_key("CAMECO (OPERATOR); (JV: CAMECO-AGIP-THORN)") == "CAMECO"
    assert company_key("E & B EXPLORATIONS LTD") == "E & B"
    assert company_key(None) == "UNKNOWN"


def test_solver_meets_the_constraints():
    res = select.solve_final(table())
    assert len(res["selected"]) == 12
    picked = {r["file_num"]: r for r in table() if r["file_num"] in res["selected"]}
    assert all(sum(1 for r in picked.values() if r["era"] == e) == 4 for e in ERAS)
    counts: dict[str, int] = {}
    for r in picked.values():
        counts[r["company_key"]] = counts.get(r["company_key"], 0) + 1
    assert max(counts.values()) <= 2
    assert sum(1 for r in picked.values() if r["zone12"]) >= 2
    assert res["unmet"] == []
    assert all(res["reasons"][n] for n in res["selected"])


def test_solver_is_deterministic_and_order_independent():
    rows = table()
    first = select.solve_final(rows)
    for seed in range(5):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert select.solve_final(shuffled)["selected"] == first["selected"]


def test_solver_swaps_to_reach_zone_12():
    rows = []
    for i, era in enumerate(ERAS):
        for c in range(5):
            # the only zone 12 candidates are the lowest scoring ones
            rows.append(candidate(i * 10 + c, era, f"C{i}{c}", score=5 - c, zone12=(c == 4)))
    res = select.solve_final(rows)
    picked = [r for r in rows if r["file_num"] in res["selected"]]
    assert sum(1 for r in picked if r["zone12"]) >= 2
    assert res["swaps"] and all(s["why"] == "zone 12 minimum" for s in res["swaps"])
    assert [c for c in res["constraints"] if c["name"] == "min_zone12"][0]["met"] is True


def test_solver_reports_unmet_constraints():
    rows = [candidate(i, "1970s", f"C{i}", score=3) for i in range(4)]  # one era only, no zone 12
    res = select.solve_final(rows)
    assert set(res["unmet"]) == {"per_era", "min_zone12", "total"}
    assert len(res["selected"]) == 4


def test_company_cap_blocks_a_third_file():
    rows = [candidate(i, "1970s", "SAME", score=9 - i) for i in range(5)]
    res = select.solve_final(rows)
    assert len(res["selected"]) == 2
    assert any("company cap" in v for v in res["skipped"].values())


def test_split_rule_one_per_era_plus_datum_file():
    rows = [r for r in table() if r["file_num"] in select.solve_final(table())["selected"]]
    # nothing is a likely NAD27 file: the fourth held-out file is then the lowest remaining hash
    none_likely = select.split_files(rows, {r["file_num"]: False for r in rows})
    assert len(none_likely["heldout"]) == 4 and len(none_likely["dev"]) == 8
    assert none_likely["note"]
    by_hash = sorted(rows, key=lambda r: r["split_hash"])
    for era in ERAS:
        lowest = next(r for r in by_hash if r["era"] == era)
        assert lowest["file_num"] in none_likely["heldout"]
    # make one file likely: it must end up held out even if its hash is not the lowest remaining
    remaining = [r for r in by_hash if r["file_num"] not in none_likely["heldout"][:3]]
    target = remaining[-1]["file_num"]
    with_likely = select.split_files(rows, {r["file_num"]: r["file_num"] == target for r in rows})
    assert target in with_likely["heldout"]
    assert with_likely["heldout_has_likely_datum"] and not with_likely["note"]


def test_split_is_stable_across_runs():
    rows = [r for r in table() if r["file_num"] in select.solve_final(table())["selected"]]
    likely = {r["file_num"]: r["era"] == "1970s" for r in rows}
    a = select.split_files(rows, likely)
    b = select.split_files(list(reversed(rows)), likely)
    assert a["heldout"] == b["heldout"]


def test_phase1_picks_one_per_era_plus_the_datum_file():
    rows = [r for r in table() if r["file_num"] in select.solve_final(table())["selected"]]
    split = select.split_files(rows, {r["file_num"]: False for r in rows})
    datum = {r["file_num"]: {"score": 3.0 if r["file_num"] == split["dev"][-1] else 0.5} for r in rows}
    p1 = choose_phase1(rows, split["dev"], datum)
    assert len(p1["files"]) == 4
    assert set(p1["files"]) <= set(split["dev"])
    assert {r["era"] for r in rows if r["file_num"] in p1["files"][:3]} == set(ERAS)
    assert p1["files"][3] == split["dev"][-1]
    assert all(p1["reasons"][f] for f in p1["files"])


# ---------------------------------------------------------------- folder listings and datum priors


def listing_rows() -> list[dict]:
    return [
        {"FILE_NAME": "report.PDF", "FILE_LCATN": "NTS 74 FILES/74H09/74H09-0039/Reports", "FILE_SIZE": 12.0,
         "FILE_LNK": "https://x/report.PDF"},
        {"FILE_NAME": "Thumbs.db", "FILE_LCATN": "NTS 74 FILES/74H09/74H09-0039/Reports", "FILE_SIZE": 0.01,
         "FILE_LNK": "https://x/Thumbs.db"},
        {"FILE_NAME": "main.pdf", "FILE_LCATN": "MAW413/Digital Submissions", "FILE_SIZE": 1.1,
         "FILE_LNK": "https://x/main.pdf"},
        {"FILE_NAME": "UVN-DeclarationOfResponsibility.pdf", "FILE_LCATN": "MAW413/Digital Submissions",
         "FILE_SIZE": 0.1, "FILE_LNK": "https://x/decl.pdf"},
        {"FILE_NAME": "MN-022.pdf", "FILE_LCATN": "MAW413/Digital Submissions/Appendix I - Computer Coded DDH Logs",
         "FILE_SIZE": 0.2, "FILE_LNK": "https://x/MN-022.pdf"},
        {"FILE_NAME": "Strip Plot - MN-022.pdf", "FILE_LCATN": "MAW413/Digital Submissions/Appendix II - Strip Plots",
         "FILE_SIZE": 0.25, "FILE_LNK": "https://x/strip.pdf"},
        {"FILE_NAME": "HL-Lithogeochem.xlsx", "FILE_LCATN": "MAW413/Digital Submissions/Appendices/Appendix B",
         "FILE_SIZE": 0.3, "FILE_LNK": "https://x/litho.xlsx"},
        {"FILE_NAME": "VAN12004434_Certificate_1.PDF",
         "FILE_LCATN": "MAW413/Digital Submissions/Appendices/Appendix A", "FILE_SIZE": 0.22,
         "FILE_LNK": "https://x/cert.PDF"},
        {"FILE_NAME": "data.XYZ", "FILE_LCATN": "MAW413/Digital Submissions/XYZ", "FILE_SIZE": 0.02,
         "FILE_LNK": "https://x/data.XYZ"},
    ]


def test_classify_listing_historic_and_modern_layouts():
    s = classify_listing(listing_rows())
    assert [r["name"] for r in s["report_pdfs"]] == ["main.pdf", "report.PDF"]
    assert [r["name"] for r in s["appendix_pdfs"]] == ["MN-022.pdf"]           # strip plots excluded
    assert [r["name"] for r in s["assay_xls"]] == ["HL-Lithogeochem.xlsx"]
    assert [r["name"] for r in s["certificate_pdfs"]] == ["VAN12004434_Certificate_1.PDF"]
    assert s["report_total_mb"] == pytest.approx(13.3)
    assert s["report_max_mb"] == pytest.approx(12.0)
    assert s["extensions"][".pdf"] == 6 and s["extensions"][".db"] == 1


def test_postprobe_filters():
    s = classify_listing(listing_rows())
    assert select.postprobe_filters(s) == {"has_report_pdf": True, "report_total_max": True, "report_single_max": True}
    big = dict(s, report_total_mb=400.0, report_max_mb=90.0)
    assert select.postprobe_filters(big) == {"has_report_pdf": True, "report_total_max": False,
                                             "report_single_max": False}
    empty = classify_listing([])
    assert select.postprobe_filters(empty)["has_report_pdf"] is False


def test_datum_signal_uses_the_best_available_evidence():
    old = {"year": 1975, "grid_mentioned": True}
    prior = datum_signal(old)
    assert prior["likely"] and not prior["confirmed"] and prior["score"] == pytest.approx(2.5)
    modern = {"year": 2013, "grid_mentioned": False}
    assert not datum_signal(modern)["likely"]
    # a text layer that prints NAD83 and never NAD27 pushes a file out of the "likely" set
    confirmed = datum_signal({"year": 1996, "grid_mentioned": False}, {"nad83": 4, "nad27": 0})
    assert confirmed["confirmed"] and not confirmed["likely"]
    # NAD27 in the text layer confirms it
    nad27 = datum_signal(modern, {"nad27": 2})
    assert nad27["likely"] and nad27["confirmed"]
    grid = datum_signal(modern, {"local_grid": 15})  # station coordinates printed: no UTM datum to state
    assert grid["likely"] and "local-grid" in " ".join(grid["reasons"])


def test_prefetch_filters_funnel():
    f = {"uranium_tagged": True, "era": "1970s", "drilling_evidence": True, "provincial_holes": 7,
         "hole_count_for_filter": 9, "folder_size_mb": 26.0}
    assert all(select.prefetch_filters(f).values())
    assert not select.prefetch_filters({**f, "provincial_holes": 4})["provincial_holes_min"]
    assert not select.prefetch_filters({**f, "hole_count_for_filter": 45})["hole_count_range"]
    assert not select.prefetch_filters({**f, "folder_size_mb": 900.0})["folder_size_max"]
    assert not select.prefetch_filters({**f, "era": None})["era_known"]


def test_score_components_only_include_what_is_known():
    f = {"geods_litho_obs": 3, "probed": True}
    assert select.score_components(f) == {"provincial_lithology": 2.0, "probed": 1.0}
    with_probe = select.score_components(f, probe={"assay_xls": [1], "certificate_pdfs": []})
    assert with_probe["xls_assays"] == 3.0 and with_probe["certificates"] == 0.0
    with_datum = select.score_components(f, datum={"confirmed": True, "likely": True})
    assert with_datum["nad27_or_no_datum"] == 2.0
    assert "nad27_or_no_datum" not in select.score_components(f, datum={"confirmed": False, "likely": True})


def test_lock_refuses_to_overwrite(tmp_path):
    lock = tmp_path / "heldout.lock"
    lock.write_text(json.dumps({"file_nums": ["X"], "heldout": []}))
    sel = {"shortlist": {"candidates": []}, "probe": {"files": {}},
           "final": {"selected": [], "split_provisional": {"heldout": []}}}
    with pytest.raises(FileExistsError):
        select.lock_split(sel, {}, lock)
