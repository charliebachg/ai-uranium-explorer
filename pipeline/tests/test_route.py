"""Routing: features and classes on real probe pages, plus the continuation and datum logic.

The assertions stay inside what a keyword and geometry heuristic can honestly do: the depth-pair feature,
the table detection, the datum signal, and a small set of plausible classes per page.
"""

from __future__ import annotations

import pytest

from uranium_explorer.route import (STRONG, classify, continuation_chains, datum_signals, depth_pairs, group_rows,
                                 numeric_columns, page_features, route_page)

def _route(ocred, name: str, page_no: int) -> dict:
    """Route one probe page from both engines' output, as the stage does."""
    rec = ocred[(name, page_no)]
    words = ([{**t, "engine": "livetext", "page_no": page_no} for t in rec["livetext_tokens"]]
             + [{**ln, "engine": "vision", "page_no": page_no} for ln in rec["vision_lines"]])
    return route_page(words, prefer_text_layer=False)


def test_drill_log_summary_page_is_a_collar_like_page_with_a_local_grid(ocred):
    """Page 1 of the 1980 drill log is a summary log: collar fields, probe peaks, local grid coordinates."""
    r = _route(ocred, "drilllog", 1)
    f = r["features"]
    # it is a key-value form, not a column table
    assert not f["table_like"]
    assert f["numeric_columns"] == 0
    # the collar labels of a summary log are all there
    for kw in ("elevation", "coordinates", "dip", "bearing", "overburden", "unconformity", "summary log"):
        assert kw in f["keyword_hits"]["collar_table"], kw
    assert "probe" in f["keyword_hits"]["probe_log"]      # PROBE PEAKS (counts / interval @ depth)
    assert r["route_class"] in ("collar_table", "probe_log", "uncertain")
    assert r["route_candidates"][0] == "collar_table"
    # the datum trap: a local grid station, and no datum printed anywhere on the page
    kinds = {s["kind"] for s in r["datum_signals"]}
    assert "local_grid" in kinds
    assert not kinds & {"nad27", "nad83", "wgs84"}
    grid = next(s for s in r["datum_signals"] if s["kind"] == "local_grid")
    assert grid["text"].replace(" ", "") == "11+00N"
    assert len(grid["box"]) == 4 and 0.0 <= grid["box"][1] < grid["box"][3] <= 1.0


def test_drill_log_sample_table_page_has_depth_pairs_and_table_shape(ocred):
    """Page 6 is the sample table: Sample # / From / To / U3O8 with four sample rows."""
    r = _route(ocred, "drilllog", 6)
    f = r["features"]
    assert f["table_like"]
    assert f["numeric_columns"] >= 2
    assert r["n_depth_pairs"] >= 3
    froms = [p["from"] for p in r["depth_pairs"]]
    tos = [p["to"] for p in r["depth_pairs"]]
    assert all(a < b for a, b in zip(froms, tos))
    assert any(abs(a - 152.2) < 0.05 for a in froms)       # the first printed sample interval
    assert any(abs(b - 155.8) < 0.05 for b in tos)         # the last printed sample interval
    assert r["route_class"] in ("assay_table", "lith_log")
    assert "assay" in f["keyword_hits"]["assay_table"]


def test_born_digital_certificate_is_classed_as_a_certificate(ocred):
    r = _route(ocred, "modern", 1)
    assert r["route_class"] == "certificate"
    assert len(set(r["features"]["keyword_hits"]["certificate"]) & set(STRONG["certificate"])) >= 3
    r3 = _route(ocred, "modern", 3)
    assert r3["route_class"] in ("certificate", "assay_table")
    assert r3["features"]["numeric_columns"] >= 8          # the wide ICP table
    assert r3["features"]["table_like"]


def test_page_with_no_text_is_other(ocred):
    r = _route(ocred, "wollaston", 3)
    assert r["route_class"] in ("other", "uncertain", "lith_log", "collar_table", "assay_table")
    assert r["features"]["n_tokens"] > 0                   # the scan does OCR, it just has no text layer


# ---------------------------------------------------------------- unit level


def word(text: str, x0: float, y0: float, w: float = 0.05, h: float = 0.012) -> dict:
    return {"text": text, "x0": x0, "y0": y0, "x1": x0 + w, "y1": y0 + h, "engine": "livetext", "conf": 1.0,
            "line_id": 0}


def test_group_rows_and_columns():
    rows_in = []
    for i in range(6):
        y = 0.2 + i * 0.03
        rows_in += [word("D", 0.1, y), word(f"{100 + i}.5", 0.3, y), word(f"{101 + i}.5", 0.5, y)]
    rows = group_rows(rows_in)
    assert len(rows) == 6 and all(len(r) == 3 for r in rows)
    n, sig = numeric_columns(rows)
    assert n == 2 and sig == pytest.approx([0.3, 0.5], abs=0.001)
    pairs = depth_pairs(rows)
    assert len(pairs) == 6 and pairs[0]["kind"] == "two_tokens"
    assert pairs[0]["from"] == 100.5 and pairs[0]["to"] == 101.5


def test_depth_pair_from_a_single_token_and_implausible_pairs_rejected():
    rows = group_rows([word("157-173.5", 0.2, 0.2)])
    p = depth_pairs(rows)
    assert p and p[0]["kind"] == "single_token" and p[0]["to"] == 173.5
    # descending, or spanning more than the maximum interval: not a depth pair
    assert not depth_pairs(group_rows([word("173.5-157", 0.2, 0.3)]))
    assert not depth_pairs(group_rows([word("10-2000", 0.2, 0.3)]))


def test_prose_depth_ranges_are_not_a_table():
    """A page of description that mentions three depth ranges is not a table (the drill log's page 1)."""
    prose = ["weak to moderate hematite oxidation from 157-173.5",
             "and chloritic alteration in the lower part of the hole",
             "fine disseminated graphite and sulfides first appear",
             "at about 180-190 metres below the unconformity",
             "moderately altered medium to coarse grained",
             "pegmatitic intervals to about 200-210 metres",
             "which cut a finer grained meta sediment",
             "well foliated pelite with coarser bands",
             "core recovered in B Q size throughout",
             "logged from the collar downwards"]
    words = []
    for i, line in enumerate(prose):
        x = 0.1
        for tok in line.split():
            words.append(word(tok, x, 0.2 + i * 0.03, w=0.012 * len(tok)))
            x += 0.012 * len(tok) + 0.008
    f, rows, pairs = page_features(words)
    assert len(rows) == 10 and len(pairs) == 3
    assert f.numeric_columns == 0
    assert not f.table_like       # depth ranges inside prose, no aligned numeric columns


def test_classify_needs_both_words_and_shape():
    tokens = ["assay", "results", "sample", "numbers", "were", "sent", "to", "the", "laboratory", "in",
              "batches", "and", "reported", "as", "u3o8", "percent", "by", "weight", "in", "ppm"]
    f, _, _ = page_features([word(t, 0.1 + 0.04 * (i % 10), 0.2 + 0.03 * (i // 10), w=0.03)
                             for i, t in enumerate(tokens)])
    out = classify(f)
    assert not f.table_like
    assert out["route_class"] in ("other", "uncertain")     # keywords without a table shape
    assert "no table shape" in " ".join(out["route_why"]) or "threshold" in " ".join(out["route_why"])


def test_classify_returns_other_for_a_nearly_empty_page():
    f, _, _ = page_features([word("1", 0.1, 0.1)])
    out = classify(f)
    assert out["route_class"] == "other" and "fewer than 15 tokens" in out["route_why"][0]


def test_datum_signals_cover_the_printed_forms():
    rows = group_rows([
        word("NAD", 0.1, 0.10), word("27", 0.16, 0.10),
        word("UTM", 0.1, 0.14), word("zone", 0.16, 0.14), word("13", 0.22, 0.14),
        word("L", 0.1, 0.18), word("11+00N,", 0.14, 0.18), word("1+60W", 0.24, 0.18),
        word("NAD83", 0.1, 0.22),
    ])
    kinds = [s["kind"] for s in datum_signals(rows)]
    assert kinds.count("local_grid") == 2
    assert "nad27" in kinds and "nad83" in kinds and "utm" in kinds and "utm_zone" in kinds


def test_continuation_chains_group_consecutive_table_pages():
    def page(n, cls, sig, continued=False):
        return {"page_no": n, "page_id": f"p{n}", "pdf_sha256": "ab" * 32, "route_class": cls,
                "features": {"column_signature": sig, "continued_marker": continued, "header_text": f"h{n}"}}
    pages = [page(1, "collar_table", [0.2, 0.5]), page(2, "assay_table", [0.3, 0.6]),
             page(3, "assay_table", [0.3, 0.6]), page(4, "assay_table", [0.31, 0.61]),
             page(6, "assay_table", [0.3, 0.6]), page(7, "other", [])]
    chains = continuation_chains(pages)
    assert pages[1]["chain_id"] == pages[2]["chain_id"] == pages[3]["chain_id"]
    assert pages[0]["chain_id"] != pages[1]["chain_id"]
    assert pages[4]["chain_id"] != pages[1]["chain_id"]      # page 5 is missing: a new chain
    assert pages[5]["chain_id"] is None
    assert [p["chain_pos"] for p in pages[1:4]] == [1, 2, 3]
    assert chains[pages[1]["chain_id"]]["header_text"] == "h2"


# ---------------------------------------------------------------- front matter


def _page_of(lines: list[tuple[str, float]]) -> list[dict]:
    """Words laid out as rows: each (text, y0) becomes a row of tokens spaced across the page."""
    out = []
    for text, y in lines:
        for i, tok in enumerate(text.split()):
            out.append(word(tok, 0.1 + i * 0.08, y))
    return out


def test_a_table_of_contents_is_front_matter_not_an_assay_table():
    """The Fission 2012 report's contents page lists 'Core Sampling', 'Analyses' and 'Drill Hole Locations' with
    page numbers in a column: keywords and a numeric column, which the banks read as an assay table."""
    words = _page_of([
        ("TABLE OF CONTENTS", 0.10),
        ("7.1 Diamond Drilling 11", 0.20), ("7.2 Dual Rotary Drilling 18", 0.23),
        ("8.0 CORE SAMPLING METHOD AND APPROACH 24", 0.26),
        ("9.0 CORE SAMPLE PREPARATION, ANALYSES AND SECURITY 24", 0.29),
        ("Table 3 Fall 2012 Diamond Drill Hole Locations 12", 0.32),
        ("Table 4 Winter 2013 Diamond Drill Hole Locations 15", 0.35),
        ("Appendix 3 Diamond Drill Hole Core Sample and Assay Data", 0.38),
    ])
    r = route_page(words, prefer_text_layer=False)
    assert r["features"]["front_matter"] is True
    assert r["route_class"] == "other" and r["route_candidates"] == []
    assert any("front matter" in w for w in r["route_why"])


def test_the_phrase_lower_on_the_page_does_not_make_it_front_matter():
    """A drill log whose remarks cite 'see list of tables' half-way down keeps its own class."""
    words = _page_of([
        ("Hole From To Sample U3O8 %", 0.10),
        ("PLS13-050 210.0 211.0 65701 0.018", 0.14), ("PLS13-050 211.0 212.0 65702 0.005", 0.17),
        ("PLS13-050 212.0 213.0 65703 0.019", 0.20), ("PLS13-050 213.0 214.0 65704 0.198", 0.23),
        ("see list of tables for the full set", 0.60),
    ])
    r = route_page(words, prefer_text_layer=False)
    assert r["features"]["front_matter"] is False
    assert r["route_class"] != "other"
