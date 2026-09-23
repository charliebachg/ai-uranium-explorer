"""The evidence readers over real cells for the dashboard: each result kept whole, its numbers minted as values,
stored in the agent tier and served newest first, a refused reading shown as refused. Fake packs and a scripted
backend, so no model is called; the store is a temporary DuckDB with the real schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uranium_explorer.analyst import readers as RD
from uranium_explorer.store import connect

from test_analyst_families import GEO_ID, ReadingBackend, d2_arm, rich_pack

CELL = "0201_0072"


def packs(cell_id: str) -> dict:
    """The fixture's rich pack; its ids and the fake answers' ids share the fixture's own cell label."""
    return rich_pack()


def test_a_run_keeps_each_reading_whole_and_mints_its_numbers(tmp_path: Path) -> None:
    out = tmp_path / "20260924T000000Z-readers"
    backend = ReadingBackend(fail={"structure": 2})
    summary = RD.run_cells([CELL], d2_arm(), lambda arm: backend, 5.0, out, log=lambda _m: None, workers=1,
                           packs=packs, cache_root=tmp_path / "cache")
    assert summary["done"] == 1 and not summary["failed"]
    rec = json.loads((out / "rows.jsonl").read_text().splitlines()[0])
    assert rec["reading_run_id"] == f"{out.name}:{CELL}" and rec["published"] is True
    geo = rec["readings_json"]["geochemistry"]
    assert geo["summary"] == "Read as instructed." and geo["claim_list"][0]["value_ids"] == [GEO_ID]
    base = RD.value_base(CELL, out.name)
    values = rec["values_json"]
    assert values[f"{base}:probability"]["value"] == rec["probability"]
    assert values[f"{base}:geochemistry:strength"]["value"] == 0.7
    assert GEO_ID in values, "a value a reading cites travels with the row"
    # resumable: a second run over the same directory reads nothing again
    again = RD.run_cells([CELL], d2_arm(), lambda arm: ReadingBackend(), 5.0, out, log=lambda _m: None,
                         packs=packs, cache_root=tmp_path / "cache")
    assert again["skipped"] == 1 and len((out / "rows.jsonl").read_text().splitlines()) == 1


def test_stored_results_are_served_newest_first_and_a_refused_reading_shows_nothing_of_itself(tmp_path: Path) -> None:
    out = tmp_path / "20260924T000000Z-readers"
    RD.run_cells([CELL], d2_arm(), lambda arm: ReadingBackend(fail={"structure": 2}), 5.0, out, log=lambda _m: None,
                 packs=packs, cache_root=tmp_path / "cache")
    con = connect(tmp_path / "ue.duckdb")
    try:
        assert RD.store_run(con, out) == 1
        assert RD.store_run(con, out) == 1, "storing a run twice replaces its rows"
        assert con.execute("select count(*), min(tier) from agent.reading_run").fetchone() == (1, "agent")
        [(rec, values)] = RD.for_cell(con, CELL)
    finally:
        con.close()
    assert rec["published"] and rec["answer"]["verdict"] == rec["verdict"]
    assert rec["probability_id"] in values
    by = {r["family"]: r for r in rec["readings"]}
    assert set(by) == {"geochemistry", "dispersal", "structure", "setting"}
    assert by["geochemistry"]["strength_id"] in values and by["geochemistry"]["claims"]
    assert by["structure"]["published"] is False
    assert by["structure"]["summary"] == "" and by["structure"]["claims"] == [], "refused: nothing of it is shown"
    assert by["structure"]["problems"], "but why it was refused is"


def test_a_store_without_the_table_serves_no_results(tmp_path: Path) -> None:
    import duckdb

    con = duckdb.connect(str(tmp_path / "bare.duckdb"))
    try:
        assert RD.for_cell(con, CELL) == []
    finally:
        con.close()


def test_only_an_evidence_readers_arm_runs(tmp_path: Path) -> None:
    from uranium_explorer.analyst.arms import load_arm

    with pytest.raises(ValueError, match="v2"):
        RD.run_cells([CELL], load_arm("v0"), lambda arm: ReadingBackend(), 1.0, tmp_path / "r", packs=packs)


def test_the_chat_reads_the_stored_result_with_every_number_beside_its_id(tmp_path: Path) -> None:
    from uranium_explorer.interface import router as R
    from uranium_explorer.interface.readers_tool import TOOL, evidence_readers

    out = tmp_path / "20260924T000000Z-readers"
    RD.run_cells([CELL], d2_arm(), lambda arm: ReadingBackend(fail={"structure": 2}), 5.0, out, log=lambda _m: None,
                 packs=packs, cache_root=tmp_path / "cache")
    con = connect(tmp_path / "ue.duckdb")
    try:
        RD.store_run(con, out)
        result = evidence_readers(CELL, con=con)
        none = evidence_readers("0001_0001", con=con)
    finally:
        con.close()
    answer, *readings = result.rows
    assert answer["row"] == "answer" and answer["verdict"] and answer["probability_id"] in result.values
    assert answer["probability"] == result.values[answer["probability_id"]]["value"]
    geo = next(r for r in readings if r["family"] == "geochemistry")
    assert geo["assessment"] == "for" and geo["strength"] == 0.7 and geo["strength_id"] in result.values
    struct = next(r for r in readings if r["family"] == "structure")
    assert struct["assessment"] is None and struct["summary"] == "" and struct["problems"]
    assert none.rows == [] and "enabled cells only" in none.note
    # a lookup about the readers is planned onto this tool
    route = R.Route(kind="lookup", cell_ids=[CELL], topic="readers")
    assert [s.tool for s in R.plan(route, "what did the evidence readers conclude?")] == [TOOL]
