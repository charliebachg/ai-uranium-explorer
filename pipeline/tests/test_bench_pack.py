"""Packs: the cell id and every value id are rewritten consistently, and nothing that places the ground survives."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uranium_explorer import store as ST
from uranium_explorer.bench import pack as P
from uranium_explorer.bench import spec as S
from uranium_explorer.prospect import retrieve as R
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


def test_build_pack_carries_every_part_under_v1_and_only_three_tools_with_the_switches_off(store: Path) -> None:
    off = {"label_context": False, "oof_scores": False, "effort_features": False}
    con = ST.connect(store, read_only=True)
    try:
        full = P.build_pack("0000_0000", "b-0001", v1(), con=con)
        lean = P.build_pack("0000_0000", "b-0001", v1(), con=con, switches=off)
    finally:
        con.close()
    # v1 carries every part, so an arm can strip what it does not use and never has to add
    assert full["bench_id"] == "b-0001" and full["version"] == "v1"
    assert {"cell_features", "criteria_breakdown", "coverage"} <= set(full["tools"])
    assert full["switches"] == {"label_context": True, "oof_scores": True, "effort_features": True}
    assert all(k.startswith("b:b-0001:") for k in full["values"])
    # with the switches off the pack is the closed-book minimum: three tools, no effort feature anywhere
    assert set(lean["tools"]) == {"cell_features", "criteria_breakdown", "coverage"}
    feats = {r["feature"] for r in lean["tools"]["cell_features"]["rows"]}
    assert "d_conductor_m" in feats and not feats & {"holes_n", "airborne_surveys_n", "holes_first_year"}
    assert not {r["feature"] for r in lean["tools"]["coverage"]["rows"]} & {"holes_n"}
    assert not any("holes_n" in k for k in lean["values"])
    text = json.dumps(full)
    assert "0000_0000" not in text and "ASAMERA" not in text and "Cluff Lake" not in text and "Cigar Lake" not in text
    row = next(r for r in full["tools"]["cell_features"]["rows"] if r["feature"] == "d_conductor_m")
    assert row["value_id"] in full["values"] and full["values"][row["value_id"]]["value"] == row["value"]


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
        pack = P.build_pack("0000_0000", "b-0001", v1(), con=con,
                            switches={"label_context": False, "oof_scores": False, "effort_features": False})
    finally:
        con.close()
    text = P.pack_text(pack)
    assert text == P.pack_text(json.loads(json.dumps(pack)))
    assert text.startswith("bench cell b-0001\nevery number has an id")
    assert "features: id | value | unit | observations [count id] | status" in text
    assert "b:b-0001:cell:d_conductor_m |" in text and "criteria:" in text and "coverage:" in text
    assert "lake_water_uranium | unknown | -" in text
    assert "0000_0000" not in text and "holes_n" not in text


def test_pack_text_quotes_a_text_valued_feature_as_known() -> None:
    from uranium_explorer.bench.pack import pack_text

    pack = {"bench_id": "b-0009", "tools": {"cell_features": {"rows": [
        {"feature": "surficial_class", "value": None, "text": "Glaciofluvial hummocky", "observations": 2},
        {"feature": "sed_u_max_ppm", "value": None, "observations": 0, "nearest_observation_id": "b:b-0009:cell:sed_u_max_ppm:nearest_m"},
    ]}}}
    text = pack_text(pack)
    assert "surficial_class | Glaciofluvial hummocky | - | 2 | known (text)" in text
    assert "sed_u_max_ppm | - | - | 0 | unknown (nearest observation id" in text


def _later_spec(**on: bool) -> S.BenchSpec:
    from dataclasses import replace

    spec = v1()
    return replace(spec, pack=replace(spec.pack, **on))


def _fake_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evidence tools that return one record each, one of them worded with a place name."""
    from uranium_explorer.prospect import evidence as E
    from uranium_explorer.prospect import tools as T

    def tool(name: str):
        def run(cell_id: str) -> T.ToolResult:
            vid = f"c:ev:{cell_id}:x0:dist_m"
            return T.ToolResult(name, {"cell_id": cell_id},
                                [{"kind": "boulder", "rock": "athabasca sandstone", "dist_m": 900.0, "dist_m_id": vid}],
                                {vid: {"id": vid, "value": 900.0, "note": f"record near cell {cell_id}"}}, "a note")
        return run

    for fam in E.FAMILIES:
        monkeypatch.setattr(E, f"evidence_{fam}", tool(f"evidence_{fam}"))
    monkeypatch.setattr(E, "region", tool("region"))


def test_the_later_switches_add_the_evidence_and_the_region_anonymised_and_scrubbed(store: Path, monkeypatch) -> None:
    _fake_evidence(monkeypatch)
    con = ST.connect(store, read_only=True)
    try:
        rich = P.build_pack("0000_0000", "b-0001", _later_spec(evidence=True, region=True), con=con)
        plain = P.build_pack("0000_0000", "b-0001", v1(), con=con)
    finally:
        con.close()
    assert {"evidence_geochem", "evidence_boulders", "evidence_structure", "evidence_bedrock", "region"} <= set(rich["tools"])
    assert not {"evidence_geochem", "region"} & set(plain["tools"])
    assert "b:b-0001:ev:x0:dist_m" in rich["values"]
    assert "0000_0000" not in json.dumps(rich)
    # the new tools are scrubbed of place names (the criteria table's cited literature is kept, as always, and
    # never rendered), and so is everything the model is shown
    new = json.dumps({k: v for k, v in rich["tools"].items() if k.startswith("evidence_") or k == "region"})
    assert "athabasca" not in new.lower() and "[redacted] sandstone" in new
    assert "athabasca" not in P.pack_text(rich).lower()
    assert rich["switches"]["evidence"] is True and "evidence" not in plain["switches"]
    rendered = P.pack_text(rich)
    assert "radioactive boulders within 10 km" in rendered
    assert "dist_m 900.0 [b:b-0001:ev:x0:dist_m]" in rendered


def test_extended_features_are_hidden_unless_switched_on_and_the_domains_always_are(store: Path, monkeypatch) -> None:
    from uranium_explorer.prospect import tools as T

    def features(cell_id: str) -> T.ToolResult:
        out = T.ToolResult("cell_features", {"cell_id": cell_id})
        for key, v in (("d_conductor_m", 820.0), ("sed_u_th_max", 3.1), ("domain_wollaston", 1.0)):
            vid = f"c:cell:{cell_id}:{key}"
            out.rows.append({"feature": key, "value": v, "value_id": vid, "observations": 1})
            out.values[vid] = {"id": vid, "value": v}
        return out

    monkeypatch.setattr(T, "cell_features", features)
    con = ST.connect(store, read_only=True)
    try:
        plain = P.build_pack("0000_0000", "b-0001", v1(), con=con)
        rich = P.build_pack("0000_0000", "b-0001", _later_spec(extended_features=True), con=con)
    finally:
        con.close()
    feats = lambda pack: [r["feature"] for r in pack["tools"]["cell_features"]["rows"]]  # noqa: E731
    assert feats(plain) == ["d_conductor_m"]
    assert feats(rich) == ["d_conductor_m", "sed_u_th_max"], "the domain one-hot names ground and is never shown"
    assert "b:b-0001:cell:domain_wollaston" not in rich["values"]
