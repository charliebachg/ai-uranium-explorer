"""Retrieval: the place filters before the words rank, and every passage says how far it can be trusted."""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from uranium_explorer.prospect import retrieve as R
from uranium_explorer.store import append_frame, apply_schema

PAGES = [
    # near the query point, and about what is asked
    ("64L05-0060", "annual report.pdf", "sha-a", 12,
     "Drilling tested the graphitic conductor at the unconformity; hole RL-12 intersected pitchblende "
     "over 2.4 metres with strong clay alteration in the sandstone above."),
    ("64L05-0060", "annual report.pdf", "sha-a", 13,
     "Ground gravity was completed over the grid. No significant anomaly was outlined in the survey area."),
    # near, but about something else
    ("64L05-0134", "logs.pdf", "sha-b", 3,
     "Overburden drilling recovered till samples for heavy mineral separation; no bedrock was reached."),
    # far away, and about exactly what is asked: the spatial filter has to keep this out
    ("74I-0039", "far.pdf", "sha-c", 7,
     "The graphitic conductor was drilled at the unconformity and returned pitchblende with clay alteration."),
]

FILES = [
    ("64L05-0060", "ASAMERA OIL", "Cluff Lake", "1979-80", "74K", "Diamond drilling, 12 DH", -108.4, 58.35, 355),
    ("64L05-0134", "CANADIAN OCCIDENTAL", "Maurice Bay", "1989", "74K", "Overburden drilling", -108.45, 58.40, 248),
    ("74I-0039", "SMDC", "Key Lake", "1983", "74I", "Diamond drilling", -105.6, 57.2, 301),
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A temporary store holding a tiny corpus, in place of the project's own."""
    path = tmp_path / "t.duckdb"
    con = duckdb.connect(str(path))
    apply_schema(con)
    append_frame(con, "native", "corpus_file", pd.DataFrame([
        {"file_num": f, "company": c, "property": p, "work_period": w, "nts": n, "work_description": d,
         "lon": lon, "lat": lat, "n_holes": h, "hole_names": None, "retrieved_at": "2026-09-18"}
        for f, c, p, w, n, d, lon, lat, h in FILES
    ]), "native")
    append_frame(con, "read", "corpus_page", pd.DataFrame([
        {"file_num": f, "doc_name": d, "doc_sha256": s, "page": pg, "chars": len(t), "text": t,
         "extracted_at": "2026-09-18"}
        for f, d, s, pg, t in PAGES
    ]), "read")
    con.execute(
        "create table if not exists read.field_value (file_num text, value_id text, page integer, "
        "as_printed text, unit_as_printed text, quote text, field text, status text, tier text)"
    )
    con.execute(
        "insert into read.field_value values ('64L05-0060', 'x:64L05-0060:abc', 12, '2.4', 'm', "
        "'2.4 metres', 'interval_length', 'pass', 'read')"
    )
    con.close()

    monkeypatch.setattr(R, "connect", lambda read_only=False: duckdb.connect(str(path), read_only=read_only))
    R.load_corpus.cache_clear()
    yield path
    R.load_corpus.cache_clear()


# ---------------------------------------------------------------- tokens


def test_tokens_drop_the_words_every_report_contains():
    toks = R.tokenize("The Assessment Report for Saskatchewan describes graphitic conductors")
    assert "graphitic" in toks and "conductors" in toks
    assert "assessment" not in toks and "report" not in toks and "the" not in toks


# ---------------------------------------------------------------- the spatial filter


def test_files_near_keeps_the_neighbours_and_drops_the_far_district(store):
    near = R.files_near(-108.4, 58.35, radius_km=40)
    assert set(near) == {"64L05-0060", "64L05-0134"}
    assert near["64L05-0060"] < 1.0
    assert "74I-0039" not in near, "a file 200 km away is not evidence about this ground"


def test_a_distant_page_is_not_returned_even_when_it_matches_perfectly(store):
    hits = R.retrieve("graphitic conductor unconformity pitchblende", lon=-108.4, lat=58.35, radius_km=40)
    assert hits, "the near page should come back"
    assert all(p.file_num != "74I-0039" for p in hits)
    assert any(p.file_num == "64L05-0060" and p.page == 12 for p in hits if p.tier == "page")


def test_without_a_position_the_whole_corpus_is_ranked(store):
    hits = R.retrieve("graphitic conductor unconformity pitchblende", k=10)
    files = {p.file_num for p in hits if p.tier == "page"}
    assert "74I-0039" in files, "with no place to filter on, the far district is a legitimate hit"


# ---------------------------------------------------------------- ranking


def test_the_page_about_the_question_outranks_the_page_that_is_merely_nearby(store):
    hits = [p for p in R.retrieve("pitchblende clay alteration", lon=-108.4, lat=58.35) if p.tier == "page"]
    assert hits[0].page == 12
    assert hits[0].score > 0


def test_a_query_with_no_matching_terms_returns_no_pages(store):
    hits = [p for p in R.retrieve("molybdenum porphyry stockwork", lon=-108.4, lat=58.35) if p.tier == "page"]
    assert hits == []


def test_the_snippet_is_built_around_the_query_term(store):
    hits = [p for p in R.retrieve("pitchblende", lon=-108.4, lat=58.35, snippet_chars=80) if p.tier == "page"]
    assert "pitchblende" in hits[0].text.lower()
    assert len(hits[0].text) <= 90


# ---------------------------------------------------------------- tier discipline


def test_every_passage_declares_its_tier_and_what_it_may_be_used_for(store):
    hits = R.retrieve("pitchblende alteration drilling", lon=-108.4, lat=58.35)
    assert {p.tier for p in hits} <= {"extracted", "page", "metadata"}
    for p in hits:
        if p.tier == "metadata":
            assert not p.quotable and not p.carries_numbers
            assert "provincial index entry" in p.cite()
        if p.tier == "page":
            assert p.quotable and not p.carries_numbers, "a number may not be taken from an OCR text layer"
            assert p.page and p.doc_name
        if p.tier == "extracted":
            assert p.quotable and p.carries_numbers
            assert p.value_id and p.value_id.startswith("x:")


def test_the_extracted_tier_comes_first(store):
    hits = R.retrieve("2.4 metres interval length", lon=-108.4, lat=58.35)
    assert hits[0].tier == "extracted", "a value with a box and a quote outranks a page of OCR text"


def test_metadata_passages_describe_the_work_without_opening_a_document(store):
    hits = [p for p in R.retrieve("drilling", lon=-108.4, lat=58.35) if p.tier == "metadata"]
    assert hits
    assert "ASAMERA" in hits[0].text or "CANADIAN OCCIDENTAL" in hits[0].text
    assert hits[0].page is None


def test_summary_counts_what_the_corpus_holds(store):
    s = R.summary()
    assert s["files_indexed"] == 3
    assert s["files_with_text"] == 3 and s["pages"] == 4
    assert s["terms"] > 10
