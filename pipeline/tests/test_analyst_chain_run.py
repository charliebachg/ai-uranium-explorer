"""The dashboard chain run: real cells, nothing blinded, chains stored in the agent tier as they publish."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from legacy_reader.analyst import arms as A
from legacy_reader.analyst import chains as CH
from legacy_reader.analyst import run as RUN
from legacy_reader.analyst import score as SC
from legacy_reader.analyst.session import Session
from legacy_reader.runtime.spend import BudgetExhausted
from legacy_reader.store import connect

from fake_bench import FakeManifest, install_runtime
from fake_loop_world import CELL as WORLD_CELL, LoopBackend, LoopWorld, bad_node, loop_registry

CELLS = ["0001_0001", "0001_0002"]


def dashboard_factory(world: LoopWorld):
    """Every requested cell opens on the fake world's one cell: its scripted nodes cite that cell's ids.
    The runner still keys each row by the cell it asked for."""
    def factory(cell_id: str, arm, stage: Path, shared):
        return Session.open(WORLD_CELL, "dashboard", fold=None, switches=arm.switches, stage=stage,
                            tools=loop_registry(world))
    return factory


def fake_cards(cell_ids, out_dir, log=print, con=None):
    """One distinct card per cell, so two cells over the fake world's one cell never share a cache key."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for cid in cell_ids:
        (out_dir / f"{cid}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + cid.encode() * 16)
        out[cid] = out_dir / f"{cid}.png"
    return out


@pytest.fixture
def world(tmp_path, monkeypatch):
    rt = install_runtime(monkeypatch, tmp_path)
    con = connect(tmp_path / "chains.duckdb")
    yield rt, con
    con.close()


def arm_v1():
    a = A.load_arm("v1")
    return replace(a, switches=replace(a.switches, effort_features=True))


def run(rt, con, backend, cells=CELLS, **over):
    kw = dict(budget_usd=20.0, log=lambda *_: None, workers=1, track=True, cache_root=rt.cache,
              session_factory=dashboard_factory(LoopWorld()), cards=fake_cards, con=con)
    kw.update(over)
    return RUN.run_cells(cells, arm_v1(), lambda _arm: backend, **kw)


def test_chains_over_real_cells_land_in_the_agent_tier_with_their_rows_and_stage_metrics(world) -> None:
    rt, con = world
    s = run(rt, con, LoopBackend())
    assert s["done"] == 2 and s["published"] == 2 and s["pending"] == [] and s["kind"] == "chain"
    stored = CH.chains_for_cell(con, WORLD_CELL)
    assert len(stored) == 2 and {c["run_id"] for c in stored} == {s["run_id"]} and all(c["published"] for c in stored)
    assert {c["chain_id"] for c in stored} == {f"{s['run_id']}:{c}" for c in CELLS}
    chain = CH.load_chain(con, stored[0]["chain_id"])
    assert len(CH.current_nodes(chain["nodes"])) == 10 and chain["decision"]["published"] is True
    assert (rt.runs / s["run_id"] / "cards" / "0001_0001.png").is_file()
    assert chain["chain"]["purpose"] == "dashboard" and chain["chain"]["arm"] == "v1"
    rows = SC.read_cells(rt.runs / s["run_id"])
    assert set(rows) == set(CELLS) and rows["0001_0002"]["session"]["purpose"] == "dashboard"
    assert s["stages"]["stage_n_chains"] == 2.0 and s["stages"]["stage_valid_rate"] == 1.0
    m = FakeManifest.started[-1]
    assert m.kind == "chain" and m.bench is None and m.config["cells"] == CELLS and set(m.prompt_hashes) == {
        "executor_system", "verifier_system", "adjudicator_system", "planner_system"}
    assert rt.logged[0]["tags"]["kind"] == "chain" and "stage_gate_rejection_rate" in rt.logged[0]["metrics"]
    written = json.loads((rt.runs / s["run_id"] / "summary.json").read_text())
    assert written["published"] == 2


def test_a_withheld_chain_is_stored_unpublished_and_the_budget_stops_the_run(world) -> None:
    rt, con = world
    backend = LoopBackend(executor={"s01": [bad_node(), bad_node(), bad_node()]},
                          raise_on_executor_call=15, error=BudgetExhausted("ceiling"))
    s = run(rt, con, backend)
    assert s["done"] == 1 and s["published"] == 0 and s["budget_exhausted"] is True and s["pending"] == ["0001_0002"]
    stored = CH.chains_for_cell(con, WORLD_CELL)
    assert len(stored) == 1 and stored[0]["published"] is False
    assert s["stages"]["stage_unknown_recorded_rate"] == pytest.approx(0.1)


def test_only_a_v1_arm_may_store_chains(world) -> None:
    rt, con = world
    with pytest.raises(ValueError, match="v1"):
        RUN.run_cells(CELLS, A.load_arm("v0"), lambda _arm: LoopBackend(), budget_usd=1.0, con=con)
