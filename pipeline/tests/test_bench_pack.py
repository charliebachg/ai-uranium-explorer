"""Packs: the cell id and every value id are rewritten consistently, and nothing that places the ground survives."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacy_reader import store as ST
from legacy_reader.bench import pack as P
from legacy_reader.bench import spec as S
from legacy_reader.prospect import retrieve as R
from bench_store import make_bench_store

CELL = "0001_0053"


def payload() -> dict:
    return {
        "tool": "cell_features", "args": {"cell_id": CELL},
        "rows": [
            {"feature": "d_conductor_m", "value": 820.0, "value_id": f"c:cell:{CELL}:d_conductor_m",
             "observations_id": f"c:cell:{CELL}:d_conductor_m:n_obs", "lon": -108.4651, "lat": 58.7805,
             "note": "nearest conductor to cell 0001_0053, filed under 74H09-0039 by SMDC on sheet 74H09"},
            {"criterion": "conductor_proximity", "evidence": "Jefferson et al. 2007 on the McArthur River camp",
             "caveat": "assumed", "weight_id": f"c:crit:{CELL}:conductor_proximity:weight"},
            {"tier": "deposit", "name": "Cigar Lake", "distance_km_id": f"c:near:{CELL}:0",
             "text": "12.4 km to Cigar Lake at 57.7149 N, -104.2825 E, easting 512000 northing 6409000"},
        ],
        "values": {
            f"c:cell:{CELL}:d_conductor_m": {"id": f"c:cell:{CELL}:d_conductor_m", "value": 820.0,
                                             "note": f"d_conductor_m for cell {CELL}"},
            "c:cov:d_fault_m": {"id": "c:cov:d_fault_m", "value": 1.0, "note": "coverage of d_fault_m"},
        },
        "cx": 173000.0, "cy": 6409000.0, "file_num": "MAW00509", "company": "SMDC",
    }


def test_rewrite_id_keeps_the_suffix_and_drops_the_cell() -> None:
    assert P.rewrite_id(f"c:cell:{CELL}:d_conductor_m", CELL, "b-0007") == "b:b-0007:cell:d_conductor_m"
    assert P.rewrite_id(f"c:cell:{CELL}:d_conductor_m:n_obs", CELL, "b-0007") == "b:b-0007:cell:d_conductor_m:n_obs"
    assert P.rewrite_id(f"c:crit:{CELL}:fault_proximity:lo", CELL, "b-0007") == "b:b-0007:crit:fault_proximity:lo"
    assert P.rewrite_id("c:cov:d_fault_m", CELL, "b-0007") == "b:b-0007:cov:d_fault_m"
    assert P.rewrite_id("c:pass:0:page", CELL, "b-0007") == "b:b-0007:pass:0:page"
    assert P.rewrite_id("x:64L05-0060:abc", CELL, "b-0007") == "x:64L05-0060:abc", "only c: ids are rewritten"


def test_anonymise_rewrites_registry_keys_row_references_and_prose_consistently() -> None:
    out = P.anonymise(payload(), CELL, "b-0007")
    assert set(out["values"]) == {"b:b-0007:cell:d_conductor_m", "b:b-0007:cov:d_fault_m"}
    v = out["values"]["b:b-0007:cell:d_conductor_m"]
    assert v["id"] == "b:b-0007:cell:d_conductor_m" and v["note"] == "d_conductor_m for cell b-0007"
    assert out["rows"][0]["value_id"] == "b:b-0007:cell:d_conductor_m"
    assert out["rows"][0]["observations_id"] == "b:b-0007:cell:d_conductor_m:n_obs"
    assert out["rows"][1]["weight_id"] == "b:b-0007:crit:conductor_proximity:weight"
    assert out["args"]["cell_id"] == "b-0007"
    assert CELL not in json.dumps(out)


def test_scrub_drops_placing_keys_and_redacts_ids_coordinates_sheets_files_and_names() -> None:
    out = P.scrub(P.anonymise(payload(), CELL, "b-0007"), {"SMDC", "Cigar Lake", "McArthur River"})
    text = json.dumps(out)
    for forbidden in ("cx", "cy", "file_num", "company", "lon", "lat", "\"name\"", "cell_id"):
        assert forbidden not in text, forbidden
    for leak in ("74H09-0039", "74H09", "SMDC", "Cigar Lake", "57.7149", "-104.2825", "6409000", "MAW00509", CELL):
        assert leak not in text, leak
    assert "[redacted]" in out["rows"][0]["note"] and "cell b-0007" in out["rows"][0]["note"]
    assert "12.4 km to [redacted]" in out["rows"][2]["text"]
    assert out["rows"][1]["evidence"] == "Jefferson et al. 2007 on the McArthur River camp", \
        "literature text is identical for every cell and is left alone"
    assert out["values"]["b:b-0007:cell:d_conductor_m"]["value"] == 820.0, "numbers that are not coordinates survive"


def test_scrub_text_handles_every_spelling_the_index_uses() -> None:
    s = P.scrub_text("files 64L04-NW-0105, 74H-0012, MAW2110 and MAW 02110 on 74-H-09 near 58.35 N", set())
    assert "64L04" not in s and "74H-0012" not in s and "MAW" not in s and "74-H-09" not in s
    assert "58.35 N" in s, "two decimals is a rounded figure, not a position"
    assert "[redacted]" in P.scrub_text("a reading of 58.3512 degrees", set())


def test_company_names_strip_roles_split_ventures_and_keep_the_core() -> None:
    assert P.company_names("UEX CORPORATION (Tenure Holder)") == {"UEX CORPORATION"}, "a three-letter core is not scrubbed alone"
    assert P.company_names("CAMECO CORPORATION (Tenure Holder)") == {"CAMECO CORPORATION", "CAMECO"}
    got = P.company_names("DENISON MINES CORP/JNR RESOURCES (Tenure Holder)")
    assert {"DENISON MINES CORP", "DENISON", "JNR RESOURCES"} <= got
    assert "JNR" not in got, "three letters is too short to scrub safely"
    assert P.company_names("UEM INC (OPERATOR) (Tenure Holder); (JV: CAMECO (75%)-UEM INC (25%)) (JV Partner)") == {"UEM INC"}
    assert P.company_names(None) == set()
    assert "URANIUM" not in P.company_names("URANIUM CANADA LTD"), "a generic word alone is not a name"


# ---------------------------------------------------------------- against a synthetic store


@pytest.fixture
def store(prospect_sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = prospect_sandbox.db
    make_bench_store(db)
    monkeypatch.setattr(ST, "db_path", lambda: db)
    R.load_corpus.cache_clear()
    yield db
    R.load_corpus.cache_clear()


def v1() -> S.BenchSpec:
    return S.load_spec("v1")


def test_build_pack_carries_the_three_tools_and_no_effort_feature(store: Path) -> None:
    con = ST.connect(store, read_only=True)
    try:
        pack = P.build_pack("0000_0000", "b-0001", v1(), con=con)
    finally:
        con.close()
    assert pack["bench_id"] == "b-0001" and pack["version"] == "v1"
    assert set(pack["tools"]) == {"cell_features", "criteria_breakdown", "coverage"}
    feats = {r["feature"] for r in pack["tools"]["cell_features"]["rows"]}
    assert "d_conductor_m" in feats and not feats & {"holes_n", "airborne_surveys_n", "holes_first_year"}
    assert not {r["feature"] for r in pack["tools"]["coverage"]["rows"]} & {"holes_n"}
    assert all(k.startswith("b:b-0001:") for k in pack["values"])
    assert not any("holes_n" in k for k in pack["values"])
    text = json.dumps(pack)
    assert "0000_0000" not in text and "ASAMERA" not in text and "Cluff Lake" not in text and "Cigar Lake" not in text
    row = next(r for r in pack["tools"]["cell_features"]["rows"] if r["feature"] == "d_conductor_m")
    assert row["value_id"] in pack["values"] and pack["values"][row["value_id"]]["value"] == row["value"]


def test_switches_add_label_context_effort_and_out_of_fold_scores(store: Path) -> None:
    import pandas as pd

    oof = pd.DataFrame({"cell_id": ["0000_0000"] * 3, "model": ["learned", "effort", "criteria"],
                        "fold_kind": "spatial", "fold": [2, 2, 2], "score": [0.7, 0.9, 0.55]})
    con = ST.connect(store, read_only=True)
    try:
        pack = P.build_pack("0000_0000", "b-0002", v1(), con=con,
                            switches={"label_context": True, "oof_scores": True, "effort_features": True}, oof=oof)
    finally:
        con.close()
    assert {"label_context", "cell_scores"} <= set(pack["tools"])
    assert {r["feature"] for r in pack["tools"]["cell_features"]["rows"]} >= {"holes_n", "ground_surveys_n"}
    scores = {r["model"]: r["score"] for r in pack["tools"]["cell_scores"]["rows"]}
    assert scores == {"learned": 0.7, "effort": 0.9, "criteria": 0.55}
    assert "b:b-0002:score:learned" in pack["values"]
    near = pack["tools"]["label_context"]["rows"]
    assert near and "name" not in near[0] and near[0]["distance_km_id"].startswith("b:b-0002:near:")
    assert "Cigar Lake" not in json.dumps(pack) and "Rabbit Lake" not in json.dumps(pack)


def test_pack_text_is_compact_and_stable(store: Path) -> None:
    con = ST.connect(store, read_only=True)
    try:
        pack = P.build_pack("0000_0000", "b-0001", v1(), con=con)
    finally:
        con.close()
    text = P.pack_text(pack)
    assert text == P.pack_text(json.loads(json.dumps(pack)))
    assert text.startswith("bench cell b-0001\nfeatures:")
    assert "b:b-0001:cell:d_conductor_m |" in text and "criteria:" in text and "coverage:" in text
    assert "lake_water_uranium | unknown | -" in text
    assert "0000_0000" not in text and "holes_n" not in text
