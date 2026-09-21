"""The MCP server over the live analytics store, read-only: the real tools on real cells through a stock
client. Gated on UE_REAL_DATA=1 like the other real-data test, and skipped cleanly when the store is absent
or held by another process. Nothing here writes: `record_insight` is not called, and a session's run
directory goes under the test's temporary path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Awaitable, Callable

import anyio
import duckdb
import pytest
from mcp import Client

from uranium_explorer import store as ST
from uranium_explorer.mcp import contract as C
from uranium_explorer.mcp import holes as HOLES
from uranium_explorer.mcp import server as SV

from mcp_world import numbers_outside_vals

pytestmark = pytest.mark.real_data

#: the dashboard's default cell (`ue prospect record`), an enabled cell with files read
CELL = "0201_0072"


@pytest.fixture
def live() -> Path:
    if os.environ.get("UE_REAL_DATA") != "1":
        pytest.skip("UE_REAL_DATA=1 to run against the live store")
    db = ST.db_path()
    if not db.is_file():
        pytest.skip(f"no live store at {db}")
    try:
        con = duckdb.connect(str(db), read_only=True)
    except duckdb.Error as err:
        pytest.skip(f"the live store cannot be opened read-only: {err}")
    try:
        if con.execute("select count(*) from derived.cell where cell_id = ?", [CELL]).fetchone()[0] != 1:
            pytest.skip(f"cell {CELL} is not in the live store")
    finally:
        con.close()
    return db


def run(tmp_path: Path, scenario: Callable[[Client], Awaitable[None]]) -> None:
    ue = SV.build(environ={}, runs_dir=tmp_path / "runs")

    async def main() -> None:
        try:
            async with Client(ue.server) as client:
                await scenario(client)
        finally:
            ue.close()

    anyio.run(main)


def test_every_read_on_a_real_cell_carries_ids_and_validates(live: Path, tmp_path: Path) -> None:
    calls = [("cell_features", {}), ("cell_scores", {}), ("criteria_breakdown", {}), ("label_context", {}),
             ("nearby", {"layer": "em_conductors", "radius_m": 5000, "k": 3}), ("coverage", {}), ("crosscheck", {}),
             ("retrieve", {"query": "graphitic conductor", "k": 3})]

    async def scenario(client: Client) -> None:
        r = await client.call_tool("open_session", {"cell_id": CELL, "purpose": "dashboard"})
        assert not r.is_error, r.content[0].text
        sid = r.structured_content["session_id"]
        for name, args in calls:
            res = await client.call_tool(name, {"session_id": sid, **args})
            assert not res.is_error, (name, res.content[0].text)
            sc = res.structured_content
            assert sc["tool"] == name and sc["rows"], name
            assert numbers_outside_vals(sc) == [], (name, numbers_outside_vals(sc)[:5])
            for vid in sc["values"]:
                assert f"[{vid}]" in res.content[0].text, (name, vid)
        feats = (await client.call_tool("cell_features", {"session_id": sid})).structured_content
        states = {row["feature"]: row["value"].get("state", "known") for row in feats["rows"]}
        assert set(states.values()) <= {"known", "unknown"}, "a feature is measured or unknown, never absent or null"

    run(tmp_path, scenario)


def test_a_scored_session_on_a_real_cell_serves_out_of_fold_scores_and_blind_lists(live: Path, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        r = await client.call_tool("open_session", {"cell_id": CELL, "purpose": "scored"})
        assert not r.is_error, r.content[0].text
        doc = r.structured_content
        sid = doc["session_id"]
        if doc["fold"].get("state") == "absent":
            pytest.skip("the out-of-fold table holds no single fold for this cell")
        scores = await client.call_tool("cell_scores", {"session_id": sid})
        assert not scores.is_error, scores.content[0].text
        rows = scores.structured_content["rows"]
        assert rows and all(row["out_of_fold"] is True and row["fold"]["value"] == doc["fold"]["value"] for row in rows)
        passages = await client.call_tool("retrieve", {"session_id": sid, "query": "drilling", "k": 5})
        assert not passages.is_error
        for row in passages.structured_content["rows"]:
            assert "file" not in row and "citation" not in row, "a scored session scrubs the file"
        labels = await client.call_tool("label_context", {"session_id": sid})
        assert all(row["distance_km"]["value"] > 0 for row in labels.structured_content["rows"] if "value" in row["distance_km"])

    run(tmp_path, scenario)


def test_hole_crosscheck_reads_the_crosscheck_outputs(live: Path, tmp_path: Path) -> None:
    files = HOLES.crosschecked_files()
    if not files:
        pytest.skip("no crosscheck outputs under data/out/crosscheck")

    async def scenario(client: Client) -> None:
        r = await client.call_tool("open_session", {"cell_id": CELL, "purpose": "dashboard"})
        sid = r.structured_content["session_id"]
        res = await client.call_tool("hole_crosscheck", {"session_id": sid, "file_num": files[0]})
        assert not res.is_error, res.content[0].text
        sc = res.structured_content
        assert sc["rows"][0]["kind"] == "summary" and sc["rows"][0]["file"] == files[0]
        assert numbers_outside_vals(sc) == []
        matches = [row for row in sc["rows"] if row["kind"] == "match"]
        for row in matches:
            assert row["offset_m"].get("state") == "absent" or row["offset_m"]["id"].startswith(f"d:{files[0]}:off_")
        assert C.CATALOGUE["hole_crosscheck"].kind == "read"

    run(tmp_path, scenario)
